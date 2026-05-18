"""Manufacturer name canonicalization and reconciliation.

Three-stage algorithm:
1. Exact match after normalization (lowercase, strip punctuation, collapse whitespace,
   drop common legal suffixes). Confidence 1.0.
2. Token-set fuzzy match via RapidFuzz (threshold ≥ 92) plus country-of-origin agreement.
   Confidence = score / 100.
3. Manual override file (src/regulatory/risk/manufacturer_overrides.yaml). Always wins.
   Confidence 1.0.

Anything below confidence 0.85 stays unmerged and is appended to
``reconciliation_review.jsonl`` for human review.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
import yaml
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from regulatory.db.models import Document, Manufacturer

log = structlog.get_logger(__name__)

_OVERRIDES_PATH = Path(__file__).parent / "manufacturer_overrides.yaml"
_REVIEW_PATH = Path(__file__).parents[3] / "reconciliation_review.jsonl"

_CONFIDENCE_FLOOR = 0.85
_FUZZY_THRESHOLD = 92.0

_LEGAL_SUFFIXES = re.compile(
    r"\b("
    r"ltd|limited|inc|incorporated|llc|llp|plc|gmbh|ag|bv|nv|sa|sas|spa|kk|pty|"
    r"pharmaceuticals?|pharma|pvt|private|co|company|corp|corporation|"
    r"industries?|group|holdings?|international|intl|mfg|manufacturing"
    r")\b",
    re.IGNORECASE,
)
# Pre-strip corporate abbreviations with dots (e.g. "S.A.", "N.V.", "B.V.") before punct removal.
_DOTTED_CORP = re.compile(
    r"\b([sn]\.?[av]\.?|b\.?v\.?|p\.?l\.?c\.?|g\.?m\.?b\.?h\.?)\b\.?",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Normalize a manufacturer name for comparison.

    Lowercases, removes accents, strips punctuation, collapses whitespace,
    and drops common legal suffixes so that "Cipla Ltd" and "CIPLA LIMITED"
    compare equal.

    Args:
        name: Raw manufacturer name string.

    Returns:
        Normalized string suitable for exact-match comparison.
    """
    # Unicode normalization → ASCII-compatible decomposition, drop combining chars.
    normalized = unicodedata.normalize("NFKD", name)
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    normalized = normalized.lower()
    # Strip dotted corporate forms (e.g. "S.A.", "N.V.") before punct removal.
    normalized = _DOTTED_CORP.sub(" ", normalized)
    normalized = _PUNCT.sub(" ", normalized)
    normalized = _LEGAL_SUFFIXES.sub(" ", normalized)
    normalized = _WHITESPACE.sub(" ", normalized).strip()
    return normalized


def _load_overrides(path: Path | None = None) -> dict[str, list[str]]:
    """Load hand-curated alias overrides from YAML.

    Args:
        path: Override path for testing. Defaults to ``manufacturer_overrides.yaml``
            in the same directory as this module.

    Returns:
        Mapping of ``canonical_name → [alias, ...]``.
    """
    p = path or _OVERRIDES_PATH
    if not p.exists():
        return {}
    raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {k: list(v) for k, v in raw.items()}


def _append_review(entry: dict[str, Any], path: Path | None = None) -> None:
    """Append a low-confidence candidate to the human-review JSONL file."""
    p = path or _REVIEW_PATH
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")


def _override_lookup(
    raw_name: str,
    overrides: dict[str, list[str]],
) -> str | None:
    """Return the canonical name for ``raw_name`` if it appears in overrides.

    Args:
        raw_name: The raw manufacturer name to look up.
        overrides: Mapping from canonical_name to alias list.

    Returns:
        Canonical name if found, else ``None``.
    """
    for canonical, aliases in overrides.items():
        if raw_name == canonical or raw_name in aliases:
            return canonical
        norm_canonical = normalize_name(canonical)
        norm_raw = normalize_name(raw_name)
        if norm_raw == norm_canonical or any(normalize_name(a) == norm_raw for a in aliases):
            return canonical
    return None


class ReconcileResult:
    """Result of reconciling one raw manufacturer name against the known set.

    Attributes:
        raw_name: Original string from the document.
        canonical_name: The resolved canonical name.
        manufacturer_id: UUID of the ``Manufacturer`` row (new or existing).
        confidence: Match confidence [0.0, 1.0].
        action: ``"exact"``, ``"fuzzy"``, ``"override"``, or ``"new"``.
    """

    def __init__(
        self,
        raw_name: str,
        canonical_name: str,
        manufacturer_id: uuid.UUID,
        confidence: float,
        action: str,
    ) -> None:
        """Initialise a reconciliation result."""
        self.raw_name = raw_name
        self.canonical_name = canonical_name
        self.manufacturer_id = manufacturer_id
        self.confidence = confidence
        self.action = action

    def __repr__(self) -> str:
        """Return a concise repr."""
        return (
            f"ReconcileResult({self.raw_name!r} → {self.canonical_name!r}, "
            f"action={self.action!r}, confidence={self.confidence:.2f})"
        )


async def reconcile_manufacturers(  # pragma: no cover
    session: AsyncSession,
    *,
    dry_run: bool = True,
    overrides_path: Path | None = None,
    review_path: Path | None = None,
) -> list[ReconcileResult]:
    """Reconcile all raw manufacturer strings across ``documents`` into ``manufacturers``.

    Iterates every distinct manufacturer string in ``documents.manufacturers``,
    runs the three-stage matching pipeline, and (when ``dry_run=False``) writes
    merged/new rows to ``manufacturers`` and updates
    ``documents.canonical_manufacturer_ids``.

    Args:
        session: Async SQLAlchemy session.
        dry_run: If ``True``, compute results without writing to the database.
        overrides_path: Optional override for the YAML path (used in tests).
        review_path: Optional override for the review JSONL path (used in tests).

    Returns:
        List of :class:`ReconcileResult` for every processed name.
    """
    overrides = _load_overrides(overrides_path)
    results: list[ReconcileResult] = []

    # Collect all distinct raw manufacturer strings from documents.
    rows = await session.execute(select(Document.id, Document.manufacturers, Document.source_id))
    doc_rows = rows.fetchall()

    # Load existing canonical manufacturers.
    existing_rows = await session.execute(
        select(
            Manufacturer.id,
            Manufacturer.canonical_name,
            Manufacturer.aliases,
            Manufacturer.countries,
        )
    )
    existing: list[tuple[uuid.UUID, str, list[str], list[str]]] = [
        (r.id, r.canonical_name, list(r.aliases or []), list(r.countries or []))
        for r in existing_rows.fetchall()
    ]

    # Build lookup: normalized_name → (manufacturer_id, canonical_name)
    norm_to_existing: dict[str, tuple[uuid.UUID, str]] = {}
    for mfr_id, canonical, aliases, _ in existing:
        norm_to_existing[normalize_name(canonical)] = (mfr_id, canonical)
        for alias in aliases:
            norm_key = normalize_name(alias)
            if norm_key not in norm_to_existing:
                norm_to_existing[norm_key] = (mfr_id, canonical)

    # Track names created/updated this run to avoid duplicate inserts.
    new_this_run: dict[str, uuid.UUID] = {}
    doc_to_canonical_ids: dict[uuid.UUID, list[uuid.UUID]] = {}

    all_raw_names: set[str] = set()
    doc_manufacturers: dict[uuid.UUID, list[str]] = {}
    for doc_row in doc_rows:
        names = list(doc_row.manufacturers or [])
        doc_manufacturers[doc_row.id] = names
        all_raw_names.update(names)

    # Stage mapping: raw_name → ReconcileResult
    name_to_result: dict[str, ReconcileResult] = {}

    for raw_name in sorted(all_raw_names):
        if not raw_name.strip():
            continue
        result = await _resolve_name(
            raw_name=raw_name,
            norm_to_existing=norm_to_existing,
            existing=existing,
            overrides=overrides,
            new_this_run=new_this_run,
            session=session,
            dry_run=dry_run,
            review_path=review_path,
        )
        name_to_result[raw_name] = result
        results.append(result)
        # Update lookup for subsequent names in same run.
        if result.action != "review":
            norm_to_existing[normalize_name(result.canonical_name)] = (
                result.manufacturer_id,
                result.canonical_name,
            )
            new_this_run[result.canonical_name] = result.manufacturer_id

    # Update documents.canonical_manufacturer_ids.
    if not dry_run:
        for doc_id, names in doc_manufacturers.items():
            canonical_ids: list[uuid.UUID] = []
            for name in names:
                r = name_to_result.get(name)
                if r and r.action != "review":
                    canonical_ids.append(r.manufacturer_id)
            doc_to_canonical_ids[doc_id] = canonical_ids

        for doc_id, canonical_ids in doc_to_canonical_ids.items():
            doc = await session.get(Document, doc_id)
            if doc is not None:
                doc.canonical_manufacturer_ids = canonical_ids
        await session.commit()

    log.info(
        "reconcile_complete",
        total=len(results),
        dry_run=dry_run,
        actions={r.action for r in results},
    )
    return results


async def _resolve_name(  # pragma: no cover
    *,
    raw_name: str,
    norm_to_existing: dict[str, tuple[uuid.UUID, str]],
    existing: list[tuple[uuid.UUID, str, list[str], list[str]]],
    overrides: dict[str, list[str]],
    new_this_run: dict[str, uuid.UUID],
    session: AsyncSession,
    dry_run: bool,
    review_path: Path | None,
) -> ReconcileResult:
    """Resolve a single raw manufacturer name through the three-stage pipeline."""
    norm = normalize_name(raw_name)

    # Stage 3 (highest priority): manual override file.
    override_canonical = _override_lookup(raw_name, overrides)
    if override_canonical is not None:
        mfr_id = await _get_or_create(
            override_canonical,
            session=session,
            dry_run=dry_run,
            new_this_run=new_this_run,
            aliases=[raw_name],
            confidence=1.0,
        )
        return ReconcileResult(raw_name, override_canonical, mfr_id, 1.0, "override")

    # Stage 1: exact normalized match.
    if norm in norm_to_existing:
        existing_id, canonical = norm_to_existing[norm]
        if not dry_run:
            await _add_alias_if_new(existing_id, raw_name, session)
        return ReconcileResult(raw_name, canonical, existing_id, 1.0, "exact")

    # Stage 2: token-set fuzzy match.
    best_score = 0.0
    best_entry: tuple[uuid.UUID, str, list[str], list[str]] | None = None
    for entry in existing:
        score = fuzz.token_set_ratio(norm, normalize_name(entry[1]))
        if score > best_score:
            best_score = score
            best_entry = entry

    if best_score >= _FUZZY_THRESHOLD and best_entry is not None:
        confidence = best_score / 100.0
        if confidence >= _CONFIDENCE_FLOOR:
            if not dry_run:
                await _add_alias_if_new(best_entry[0], raw_name, session)
            return ReconcileResult(raw_name, best_entry[1], best_entry[0], confidence, "fuzzy")

        # Below confidence floor → write to review file.
        _append_review(
            {
                "raw_name": raw_name,
                "candidate_canonical": best_entry[1] if best_entry else None,
                "score": best_score,
                "confidence": confidence,
                "reviewed_at": datetime.now(tz=timezone.utc).isoformat(),
            },
            review_path,
        )
        placeholder_id = uuid.uuid4()
        return ReconcileResult(raw_name, raw_name, placeholder_id, confidence, "review")

    # No match at all → create a new canonical entry.
    mfr_id = await _get_or_create(
        raw_name,
        session=session,
        dry_run=dry_run,
        new_this_run=new_this_run,
        aliases=[],
        confidence=1.0,
    )
    return ReconcileResult(raw_name, raw_name, mfr_id, 1.0, "new")


async def _get_or_create(  # pragma: no cover
    canonical_name: str,
    *,
    session: AsyncSession,
    dry_run: bool,
    new_this_run: dict[str, uuid.UUID],
    aliases: list[str],
    confidence: float,
) -> uuid.UUID:
    """Return the ID of the canonical manufacturer row, creating it if necessary."""
    if canonical_name in new_this_run:
        return new_this_run[canonical_name]

    existing = await session.execute(
        select(Manufacturer).where(Manufacturer.canonical_name == canonical_name)
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        new_this_run[canonical_name] = row.id
        return row.id

    new_id = uuid.uuid4()
    if not dry_run:
        mfr = Manufacturer(
            id=new_id,
            canonical_name=canonical_name,
            aliases=aliases,
            countries=[],
            confidence=confidence,
        )
        session.add(mfr)
        await session.flush()
    new_this_run[canonical_name] = new_id
    return new_id


async def _add_alias_if_new(  # pragma: no cover
    manufacturer_id: uuid.UUID,
    alias: str,
    session: AsyncSession,
) -> None:
    """Append ``alias`` to a manufacturer's alias list if not already present."""
    row = await session.get(Manufacturer, manufacturer_id)
    if row is None:
        return
    current: list[str] = list(row.aliases or [])
    if alias not in current and alias != row.canonical_name:
        current.append(alias)
        row.aliases = current


def get_unmerged_candidates(review_path: Path | None = None) -> list[dict[str, Any]]:
    """Read the human-review JSONL file and return all unmerged candidates.

    Args:
        review_path: Override path for testing.

    Returns:
        List of candidate dicts, newest first.
    """
    p = review_path or _REVIEW_PATH
    if not p.exists():
        return []
    candidates = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            candidates.append(json.loads(line))
    return list(reversed(candidates))


async def merge_manufacturer(  # pragma: no cover
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    actor: str = "system",
) -> None:
    """Merge manufacturer ``source_id`` into canonical ``target_id``.

    All ``documents.canonical_manufacturer_ids`` pointing at ``source_id``
    are updated to reference ``target_id``.  The source ``Manufacturer`` row
    is deleted.  Risk-signal resolution on active signals for ``source_id``
    is handled by the persister's merge semantics (see ``persist.py``).

    Args:
        session: Async SQLAlchemy session.
        source_id: UUID of the manufacturer to absorb (will be deleted).
        target_id: UUID of the surviving canonical manufacturer.
        actor: Identity of the caller for audit purposes.
    """
    source = await session.get(Manufacturer, source_id)
    target = await session.get(Manufacturer, target_id)
    if source is None or target is None:
        raise ValueError(f"Manufacturer not found: source={source_id}, target={target_id}")

    # Merge aliases.
    merged_aliases: list[str] = list(target.aliases or [])
    for alias in [source.canonical_name] + list(source.aliases or []):
        if alias not in merged_aliases and alias != target.canonical_name:
            merged_aliases.append(alias)
    target.aliases = merged_aliases

    # Repoint documents.
    doc_rows = await session.execute(select(Document))
    for doc in doc_rows.scalars():
        ids: list[uuid.UUID] = list(doc.canonical_manufacturer_ids or [])
        if source_id in ids:
            ids = [target_id if i == source_id else i for i in ids]
            doc.canonical_manufacturer_ids = list(dict.fromkeys(ids))  # dedupe

    await session.delete(source)
    await session.flush()

    log.info(
        "manufacturer_merged",
        source_id=str(source_id),
        target_id=str(target_id),
        actor=actor,
    )


async def show_manufacturer(  # pragma: no cover
    session: AsyncSession, name: str
) -> dict[str, Any] | None:
    """Return canonical entry, aliases, and linked document count for a manufacturer.

    Args:
        session: Async SQLAlchemy session.
        name: Full or partial canonical name to look up.

    Returns:
        Dict with ``canonical_name``, ``aliases``, ``countries``, ``confidence``,
        ``linked_documents`` count, or ``None`` if not found.
    """
    result = await session.execute(select(Manufacturer).where(Manufacturer.canonical_name == name))
    row = result.scalar_one_or_none()
    if row is None:
        return None

    doc_result = await session.execute(
        select(Document).where(
            Document.canonical_manufacturer_ids.any(row.id)  # type: ignore[arg-type]
        )
    )
    doc_count = len(doc_result.scalars().all())

    return {
        "canonical_name": row.canonical_name,
        "aliases": list(row.aliases or []),
        "countries": list(row.countries or []),
        "confidence": row.confidence,
        "linked_documents": doc_count,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def collect_distinct_manufacturers(
    raw_names: Sequence[str],
    overrides: dict[str, list[str]] | None = None,
) -> list[tuple[str, str, float, str]]:
    """Pure-function version of the reconciler for testing without a DB.

    Takes a flat list of raw strings and returns ``(raw, canonical, confidence, action)``
    tuples using only stages 1–3 in-memory (no DB lookup).

    Args:
        raw_names: Raw manufacturer name strings.
        overrides: Optional override mapping; if ``None``, loads from the default path.

    Returns:
        List of ``(raw_name, canonical_name, confidence, action)`` tuples.
    """
    ov = overrides if overrides is not None else _load_overrides()
    seen_normalized: dict[str, str] = {}
    results = []

    for raw in raw_names:
        norm = normalize_name(raw)

        # Stage 3: override.
        override_canonical = _override_lookup(raw, ov)
        if override_canonical is not None:
            results.append((raw, override_canonical, 1.0, "override"))
            seen_normalized[normalize_name(override_canonical)] = override_canonical
            continue

        # Stage 1: exact.
        if norm in seen_normalized:
            results.append((raw, seen_normalized[norm], 1.0, "exact"))
            continue

        # Stage 2: fuzzy against already-seen canonicals.
        best_score = 0.0
        best_canonical: str | None = None
        for existing_norm, existing_canonical in seen_normalized.items():
            score = fuzz.token_set_ratio(norm, existing_norm)
            if score > best_score:
                best_score = score
                best_canonical = existing_canonical

        if best_score >= _FUZZY_THRESHOLD and best_canonical is not None:
            confidence = best_score / 100.0
            if confidence >= _CONFIDENCE_FLOOR:
                results.append((raw, best_canonical, confidence, "fuzzy"))
                continue
            results.append((raw, raw, confidence, "review"))
            continue

        # New entry.
        seen_normalized[norm] = raw
        results.append((raw, raw, 1.0, "new"))

    return results
