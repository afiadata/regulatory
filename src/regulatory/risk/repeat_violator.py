"""Repeat-violator risk rule.

For each canonical manufacturer, counts recalls in the rolling window ending at
``as_of`` and computes a severity-weighted score. Emits a
:class:`~regulatory.risk.config.RiskSignalCandidate` for every manufacturer that
crosses at least one threshold.

The ``as_of`` parameter is explicit and required — never defaults to today. This
makes the engine fully reproducible: backfilling historical signals as of
arbitrary past dates is required for evals and for the agent's "what did we know
on date X" capability.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from regulatory.db.models import Document, Manufacturer
from regulatory.risk.config import RepeatViolatorConfig, RiskSignalCandidate

log = structlog.get_logger(__name__)

_SEVERITY_ORDER = ["medium", "high", "critical"]


def _severity_score(severity_value: str | None, weights: dict[str, int]) -> int:
    """Return the weighted score contribution for one recall."""
    if severity_value is None:
        return weights.get("unclassified", 1)
    return weights.get(severity_value, 1)


def _classify_severity(
    recall_count: int,
    weighted_score: int,
    config: RepeatViolatorConfig,
) -> str | None:
    """Return the highest severity level crossed, or ``None`` if none crossed.

    Args:
        recall_count: Number of recalls in the window.
        weighted_score: Sum of severity-weighted points.
        config: Rule configuration.

    Returns:
        One of ``"medium"``, ``"high"``, ``"critical"``, or ``None``.
    """
    result: str | None = None
    for level in _SEVERITY_ORDER:
        threshold = config.thresholds.get(level)
        if threshold is None:
            continue
        if recall_count >= threshold.min_recalls and weighted_score >= threshold.min_weighted_score:
            result = level
    return result


async def detect_repeat_violators(  # pragma: no cover
    session: AsyncSession,
    *,
    as_of: date,
    config: RepeatViolatorConfig,
) -> list[RiskSignalCandidate]:
    """Detect manufacturers with repeated recall events within the config window.

    Args:
        session: Async SQLAlchemy session (read-only queries).
        as_of: Signals are computed as if today were this date. Required and
            explicit — pass ``date.today()`` at the call site when you want
            the current view.
        config: Repeat-violator rule configuration.

    Returns:
        List of :class:`RiskSignalCandidate` for manufacturers crossing a threshold.
    """
    window_start = as_of - timedelta(days=config.window_months * 30)

    # Load all documents in the window that are recalls/alerts/enforcement.
    recall_types = {"recall", "alert", "enforcement"}
    docs_result = await session.execute(
        select(
            Document.id,
            Document.canonical_manufacturer_ids,
            Document.active_ingredients_normalized,
            Document.severity,
            Document.date_published,
            Document.jurisdiction,
        ).where(
            Document.document_type.in_(recall_types),
            Document.date_published >= window_start,
            Document.date_published <= as_of,
        )
    )
    docs = docs_result.fetchall()
    log.debug("repeat_violator_window_docs", count=len(docs), as_of=str(as_of))

    # Accumulate per-manufacturer recall records.
    mfr_recalls: dict[str, list[dict[str, object]]] = {}
    for doc in docs:
        for mfr_id in doc.canonical_manufacturer_ids or []:
            key = str(mfr_id)
            if key not in mfr_recalls:
                mfr_recalls[key] = []
            mfr_recalls[key].append(
                {
                    "document_id": str(doc.id),
                    "severity": doc.severity,
                    "date_published": doc.date_published.isoformat(),
                    "active_ingredients": list(doc.active_ingredients_normalized or []),
                    "jurisdiction": doc.jurisdiction,
                }
            )

    candidates: list[RiskSignalCandidate] = []

    for mfr_id_str, recalls in mfr_recalls.items():
        recall_count = len(recalls)
        weighted_score = sum(
            _severity_score(r["severity"], config.severity_weights)  # type: ignore[arg-type, misc]
            for r in recalls
        )
        severity_label = _classify_severity(recall_count, weighted_score, config)
        if severity_label is None:
            continue

        # Collect the most commonly recalled active ingredients across all recalls.
        ingredient_counts: dict[str, int] = {}
        for r in recalls:
            for ing in r.get("active_ingredients", []):  # type: ignore[attr-defined]
                ingredient_counts[str(ing)] = ingredient_counts.get(str(ing), 0) + 1
        top_ingredient = (
            max(ingredient_counts, key=lambda k: ingredient_counts[k])
            if ingredient_counts
            else None
        )

        mfr = await session.get(Manufacturer, mfr_id_str)
        mfr_name = mfr.canonical_name if mfr else mfr_id_str

        log.info(
            "repeat_violator_detected",
            manufacturer=mfr_name,
            recall_count=recall_count,
            weighted_score=weighted_score,
            severity=severity_label,
        )

        candidates.append(
            RiskSignalCandidate(
                kind="repeat_violator",
                severity=severity_label,
                manufacturer_id=mfr_id_str,
                active_ingredient=top_ingredient,
                recommended_action=(
                    f"Review procurement from {mfr_name}: "
                    f"{recall_count} recall events (score {weighted_score}) "
                    f"in {config.window_months} months."
                ),
                evidence_document_ids=[str(r["document_id"]) for r in recalls],
                recall_count=recall_count,
                weighted_score=weighted_score,
            )
        )

    log.info("repeat_violator_scan_complete", candidates=len(candidates), as_of=str(as_of))
    return candidates


def detect_repeat_violators_sync(
    documents: Sequence[dict[str, object]],
    *,
    as_of: date,
    config: RepeatViolatorConfig,
) -> list[RiskSignalCandidate]:
    """Pure-function version of the repeat-violator rule for unit testing.

    Accepts an in-memory list of document dicts rather than a DB session.
    Each dict must have keys: ``id``, ``canonical_manufacturer_ids`` (list of str),
    ``active_ingredients`` (list of str), ``severity`` (str|None),
    ``date_published`` (date), ``document_type`` (str), ``jurisdiction`` (str).

    Args:
        documents: In-memory document records.
        as_of: Signals computed as of this date.
        config: Rule configuration.

    Returns:
        List of :class:`RiskSignalCandidate`.
    """
    window_start = as_of - timedelta(days=config.window_months * 30)
    recall_types = {"recall", "alert", "enforcement"}

    mfr_recalls: dict[str, list[dict[str, object]]] = {}
    for doc in documents:
        if doc.get("document_type") not in recall_types:
            continue
        dp = doc["date_published"]
        if not isinstance(dp, date):
            continue
        if dp < window_start or dp > as_of:
            continue
        for mfr_id in doc.get("canonical_manufacturer_ids", []):  # type: ignore[attr-defined]
            key = str(mfr_id)
            if key not in mfr_recalls:
                mfr_recalls[key] = []
            mfr_recalls[key].append(
                {
                    "document_id": str(doc["id"]),
                    "severity": doc.get("severity"),
                    "date_published": dp.isoformat(),
                    "active_ingredients": list(doc.get("active_ingredients", [])),  # type: ignore[call-overload]
                    "jurisdiction": str(doc.get("jurisdiction", "")),
                }
            )

    candidates: list[RiskSignalCandidate] = []

    for mfr_id_str, recalls in mfr_recalls.items():
        recall_count = len(recalls)
        weighted_score = sum(
            _severity_score(r["severity"], config.severity_weights)  # type: ignore[arg-type, misc]
            for r in recalls
        )
        severity_label = _classify_severity(recall_count, weighted_score, config)
        if severity_label is None:
            continue

        ingredient_counts: dict[str, int] = {}
        for r in recalls:
            for ing in r.get("active_ingredients", []):  # type: ignore[attr-defined]
                ingredient_counts[str(ing)] = ingredient_counts.get(str(ing), 0) + 1
        top_ingredient = (
            max(ingredient_counts, key=lambda k: ingredient_counts[k])
            if ingredient_counts
            else None
        )

        candidates.append(
            RiskSignalCandidate(
                kind="repeat_violator",
                severity=severity_label,
                manufacturer_id=mfr_id_str,
                active_ingredient=top_ingredient,
                recommended_action=(
                    f"Review procurement: {recall_count} recall events "
                    f"(score {weighted_score}) in {config.window_months} months."
                ),
                evidence_document_ids=[str(r["document_id"]) for r in recalls],
                recall_count=recall_count,
                weighted_score=weighted_score,
            )
        )

    return candidates
