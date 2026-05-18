"""Cross-source corroboration risk rule.

If >= N jurisdictions have an active recall for the same active_ingredient within
the window, emits a corroboration signal and boosts severity of related
repeat_violator or supply_chain signals by config.boost_levels.

The boost is applied at persist time, not by mutating source signal rows.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from regulatory.db.models import Document
from regulatory.risk.config import CorroborationConfig, RiskSignalCandidate

log = structlog.get_logger(__name__)

_SEVERITY_LADDER = ["low", "medium", "high", "critical"]


def boost_severity(severity: str, steps: int) -> str:
    """Bump severity up by ``steps`` positions on the ladder.

    Args:
        severity: Current severity label.
        steps: Number of steps to boost.

    Returns:
        Boosted severity label (capped at ``"critical"``).
    """
    try:
        idx = _SEVERITY_LADDER.index(severity)
    except ValueError:
        return severity
    return _SEVERITY_LADDER[min(idx + steps, len(_SEVERITY_LADDER) - 1)]


async def detect_cross_source_corroboration(  # pragma: no cover
    session: AsyncSession,
    *,
    as_of: date,
    config: CorroborationConfig,
) -> list[RiskSignalCandidate]:
    """Detect ingredients recalled by >= N jurisdictions in the window.

    Args:
        session: Async SQLAlchemy session.
        as_of: Signals computed as of this date.
        config: Corroboration rule configuration.

    Returns:
        List of :class:`RiskSignalCandidate` corroboration signals.
    """
    if not config.enable:
        return []

    window_months = 24  # matches repeat-violator default window
    window_start = as_of - timedelta(days=window_months * 30)
    recall_types = {"recall", "alert", "enforcement"}

    docs_result = await session.execute(
        select(
            Document.id,
            Document.active_ingredients,
            Document.jurisdiction,
            Document.document_type,
        ).where(
            Document.document_type.in_(recall_types),
            Document.date_published >= window_start,
            Document.date_published <= as_of,
        )
    )
    docs = docs_result.fetchall()

    # Map: active_ingredient → set of jurisdictions with recalls.
    ingredient_jurisdictions: dict[str, set[str]] = defaultdict(set)
    ingredient_docs: dict[str, list[str]] = defaultdict(list)

    for doc in docs:
        for ingredient in doc.active_ingredients or []:
            if ingredient:
                ingredient_jurisdictions[ingredient].add(doc.jurisdiction)
                ingredient_docs[ingredient].append(str(doc.id))

    candidates: list[RiskSignalCandidate] = []

    for ingredient, jurisdictions in ingredient_jurisdictions.items():
        if len(jurisdictions) < config.jurisdiction_count_for_boost:
            continue

        log.info(
            "corroboration_detected",
            ingredient=ingredient,
            jurisdictions=sorted(jurisdictions),
            doc_count=len(ingredient_docs[ingredient]),
        )

        candidates.append(
            RiskSignalCandidate(
                kind="cross_source_corroboration",
                severity="medium",
                active_ingredient=ingredient,
                regions_affected=sorted(jurisdictions),
                recommended_action=(
                    f"{len(jurisdictions)} jurisdictions have recalled products "
                    f"containing {ingredient} in the past {window_months} months. "
                    f"Verify supply alternatives and monitor closely."
                ),
                evidence_document_ids=ingredient_docs[ingredient],
            )
        )

    log.info("corroboration_scan_complete", candidates=len(candidates))
    return candidates


def detect_cross_source_corroboration_sync(
    documents: Sequence[dict[str, object]],
    *,
    as_of: date,
    config: CorroborationConfig,
    window_months: int = 24,
) -> list[RiskSignalCandidate]:
    """Pure-function version of the corroboration rule for unit testing.

    Args:
        documents: In-memory document dicts with keys: ``id``, ``active_ingredients``,
            ``jurisdiction``, ``document_type``, ``date_published`` (date).
        as_of: Computation date.
        config: Corroboration configuration.
        window_months: Rolling window length in months.

    Returns:
        List of :class:`RiskSignalCandidate`.
    """
    if not config.enable:
        return []

    window_start = as_of - timedelta(days=window_months * 30)
    recall_types = {"recall", "alert", "enforcement"}

    ingredient_jurisdictions: dict[str, set[str]] = defaultdict(set)
    ingredient_docs: dict[str, list[str]] = defaultdict(list)

    for doc in documents:
        if doc.get("document_type") not in recall_types:
            continue
        dp = doc.get("date_published")
        if not isinstance(dp, date):
            continue
        if dp < window_start or dp > as_of:
            continue
        for ingredient in doc.get("active_ingredients", []):  # type: ignore[attr-defined]
            if ingredient:
                ingredient_jurisdictions[str(ingredient)].add(str(doc.get("jurisdiction", "")))
                ingredient_docs[str(ingredient)].append(str(doc["id"]))

    candidates: list[RiskSignalCandidate] = []

    for ingredient, jurisdictions in ingredient_jurisdictions.items():
        if len(jurisdictions) < config.jurisdiction_count_for_boost:
            continue
        candidates.append(
            RiskSignalCandidate(
                kind="cross_source_corroboration",
                severity="medium",
                active_ingredient=ingredient,
                regions_affected=sorted(jurisdictions),
                recommended_action=(
                    f"{len(jurisdictions)} jurisdictions recalled {ingredient} "
                    f"in {window_months} months."
                ),
                evidence_document_ids=ingredient_docs[ingredient],
            )
        )

    return candidates
