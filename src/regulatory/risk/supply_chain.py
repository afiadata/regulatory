"""Supply-chain exposure risk rule.

For each repeat-violator signal, finds county-ingredient pairs where:
- The manufacturer's county_supply share_pct >= config.min_county_share_pct, and
- The active ingredient of the recall set overlaps the county_supply row.

Computes alternative supplier count, lead time, and time-to-expiry.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from regulatory.db.models import County, CountySupply, Manufacturer, RiskSignal, Supplier
from regulatory.risk.config import RiskSignalCandidate, SupplyChainConfig

log = structlog.get_logger(__name__)


async def detect_supply_chain_exposure(  # pragma: no cover
    session: AsyncSession,
    *,
    as_of: date,
    config: SupplyChainConfig,
    repeat_violator_signals: Sequence[RiskSignal | RiskSignalCandidate],
) -> list[RiskSignalCandidate]:
    """Detect supply-chain exposure for each repeat-violator signal.

    For every active repeat-violator signal, looks up county_supply rows where the
    flagged manufacturer supplies >= config.min_county_share_pct of a county's
    active-ingredient needs. Aggregates exposure across counties.

    Args:
        session: Async SQLAlchemy session.
        as_of: Date for which to compute exposure (used for contract date filtering).
        config: Supply-chain rule configuration.
        repeat_violator_signals: Active repeat-violator signals to evaluate.

    Returns:
        List of :class:`RiskSignalCandidate` for exposed county-ingredient pairs.
    """
    candidates: list[RiskSignalCandidate] = []

    for signal in repeat_violator_signals:
        if isinstance(signal, RiskSignalCandidate):
            mfr_id_str = signal.manufacturer_id
            ingredient = signal.active_ingredient
            rv_doc_ids = list(signal.evidence_document_ids)
        else:
            mfr_id_str = str(signal.manufacturer_id) if signal.manufacturer_id else None
            ingredient = signal.active_ingredient
            rv_doc_ids = list(signal.evidence.get("document_ids", []))

        if not mfr_id_str or not ingredient:
            log.debug(
                "supply_chain_skip_no_ingredient",
                manufacturer_id=mfr_id_str,
                ingredient=ingredient,
            )
            continue

        mfr_uuid: uuid.UUID
        try:
            mfr_uuid = uuid.UUID(mfr_id_str)
        except ValueError:
            log.warning("supply_chain_invalid_manufacturer_id", value=mfr_id_str)
            continue

        # Find all suppliers that are linked to this manufacturer.
        supplier_result = await session.execute(
            select(Supplier.id).where(Supplier.manufacturer_id == mfr_uuid)
        )
        supplier_ids = [row.id for row in supplier_result.fetchall()]

        if not supplier_ids:
            log.debug("supply_chain_no_suppliers", manufacturer_id=mfr_id_str)
            continue

        # Find all county_supply rows for this ingredient and these suppliers.
        supply_result = await session.execute(
            select(CountySupply).where(
                CountySupply.supplier_id.in_(supplier_ids),
                CountySupply.active_ingredient == ingredient,
            )
        )
        supply_rows = supply_result.scalars().all()

        # Group by county and accumulate share.
        county_exposure: dict[uuid.UUID, dict[str, object]] = {}
        supply_row_ids: list[str] = []

        for row in supply_rows:
            supply_row_ids.append(str(row.id))
            if row.county_id not in county_exposure:
                county_exposure[row.county_id] = {
                    "share_pct": Decimal("0"),
                    "lead_time_days": row.lead_time_days,
                }
            county_exposure[row.county_id]["share_pct"] = (
                Decimal(str(county_exposure[row.county_id]["share_pct"])) + row.share_pct
            )
            county_exposure[row.county_id]["lead_time_days"] = max(
                int(county_exposure[row.county_id]["lead_time_days"]),  # type: ignore[call-overload]
                row.lead_time_days,
            )

        # Filter to counties where exposure meets the threshold.
        exposed_counties: list[dict[str, object]] = []
        for county_id, exposure in county_exposure.items():
            share = float(exposure["share_pct"])  # type: ignore[arg-type]
            if share >= config.min_county_share_pct:
                county = await session.get(County, county_id)
                county_name = county.name if county else str(county_id)
                population = county.population if county else 0
                exposed_counties.append(
                    {
                        "county_id": str(county_id),
                        "county_name": county_name,
                        "population": population or 0,
                        "share_pct": share,
                        "lead_time_days": int(exposure["lead_time_days"]),  # type: ignore[call-overload]
                    }
                )

        if not exposed_counties:
            continue

        # Count alternative suppliers (others supplying same ingredient in exposed counties).
        exposed_county_ids = [uuid.UUID(str(c["county_id"])) for c in exposed_counties]
        alt_result = await session.execute(
            select(CountySupply.supplier_id)
            .where(
                CountySupply.county_id.in_(exposed_county_ids),
                CountySupply.active_ingredient == ingredient,
                CountySupply.supplier_id.not_in(supplier_ids),
            )
            .distinct()
        )
        alternative_supplier_count = len(alt_result.fetchall())

        # Aggregate exposure: population-weighted average share_pct.
        total_population = sum(int(c["population"]) for c in exposed_counties) or 1  # type: ignore[call-overload]
        weighted_exposure = sum(
            float(c["share_pct"]) * int(c["population"])  # type: ignore[arg-type, call-overload]
            for c in exposed_counties
        )
        avg_exposure = weighted_exposure / total_population

        # Time-to-expiry: max lead time across exposed counties, floored by config.
        max_lead = max(int(c["lead_time_days"]) for c in exposed_counties)  # type: ignore[call-overload]
        tte = max(max_lead, config.time_to_expiry_floor_days)

        # Severity: if enough alternatives exist, cap at low.
        severity = _exposure_severity(avg_exposure, alternative_supplier_count, config)

        mfr = await session.get(Manufacturer, mfr_uuid)
        mfr_name = mfr.canonical_name if mfr else mfr_id_str
        regions = [str(c["county_name"]) for c in exposed_counties]

        log.info(
            "supply_chain_exposure_detected",
            manufacturer=mfr_name,
            ingredient=ingredient,
            counties=regions,
            avg_exposure=round(avg_exposure, 2),
            alternatives=alternative_supplier_count,
            severity=severity,
        )

        candidates.append(
            RiskSignalCandidate(
                kind="supply_chain_exposure",
                severity=severity,
                manufacturer_id=mfr_id_str,
                active_ingredient=ingredient,
                regions_affected=regions,
                exposure_pct=round(avg_exposure, 2),
                alternative_supplier_count=alternative_supplier_count,
                time_to_expiry_days=tte,
                recommended_action=(
                    f"Diversify {ingredient} sourcing away from {mfr_name} "
                    f"within {tte} days. Covers ~{avg_exposure:.0f}% of "
                    f"{', '.join(regions[:3])}{'...' if len(regions) > 3 else ''}. "
                    f"Alternative suppliers: {alternative_supplier_count}."
                ),
                evidence_document_ids=rv_doc_ids,
                evidence_supply_ids=supply_row_ids,
            )
        )

    log.info("supply_chain_scan_complete", candidates=len(candidates))
    return candidates


def _exposure_severity(
    avg_exposure_pct: float,
    alternative_count: int,
    config: SupplyChainConfig,
) -> str:
    """Determine severity based on exposure percentage and alternative count.

    Args:
        avg_exposure_pct: Population-weighted average share percentage.
        alternative_count: Number of alternative suppliers available.
        config: Supply-chain configuration.

    Returns:
        Severity label string.
    """
    if alternative_count >= config.min_alternative_suppliers_for_low:
        return "low"
    if avg_exposure_pct >= 70:
        return "critical"
    if avg_exposure_pct >= 50:
        return "high"
    if avg_exposure_pct >= 25:
        return "medium"
    return "low"


def detect_supply_chain_exposure_sync(
    supply_rows: Sequence[dict[str, object]],
    counties: Sequence[dict[str, object]],
    *,
    as_of: date,
    config: SupplyChainConfig,
    repeat_violator_signals: Sequence[RiskSignalCandidate],
    supplier_to_manufacturer: dict[str, str],
) -> list[RiskSignalCandidate]:
    """Pure-function version of the supply-chain rule for unit testing.

    Args:
        supply_rows: List of county_supply dicts with keys: ``id``, ``county_id``,
            ``supplier_id``, ``active_ingredient``, ``share_pct``, ``lead_time_days``.
        counties: List of county dicts with keys: ``id``, ``name``, ``population``.
        as_of: Computation date.
        config: Supply-chain configuration.
        repeat_violator_signals: Upstream repeat-violator candidates.
        supplier_to_manufacturer: Mapping of ``supplier_id → manufacturer_id``.

    Returns:
        List of :class:`RiskSignalCandidate`.
    """
    candidates: list[RiskSignalCandidate] = []
    county_lookup: dict[str, dict[str, object]] = {str(c["id"]): c for c in counties}

    for signal in repeat_violator_signals:
        mfr_id_str = signal.manufacturer_id
        ingredient = signal.active_ingredient
        if not mfr_id_str or not ingredient:
            continue

        # Find suppliers linked to this manufacturer.
        linked_supplier_ids = {
            sid for sid, mid in supplier_to_manufacturer.items() if mid == mfr_id_str
        }
        if not linked_supplier_ids:
            continue

        # Find county_supply rows matching this ingredient and these suppliers.
        county_exposure: dict[str, dict[str, object]] = {}
        supply_row_ids: list[str] = []
        for row in supply_rows:
            if row.get("active_ingredient") != ingredient:
                continue
            if str(row.get("supplier_id")) not in linked_supplier_ids:
                continue
            cid = str(row["county_id"])
            supply_row_ids.append(str(row["id"]))
            if cid not in county_exposure:
                county_exposure[cid] = {"share_pct": Decimal("0"), "lead_time_days": 0}
            county_exposure[cid]["share_pct"] = Decimal(
                str(county_exposure[cid]["share_pct"])
            ) + Decimal(str(row["share_pct"]))
            county_exposure[cid]["lead_time_days"] = max(
                int(county_exposure[cid]["lead_time_days"]),  # type: ignore[call-overload]
                int(row.get("lead_time_days", 0)),  # type: ignore[call-overload]
            )

        exposed: list[dict[str, object]] = []
        for cid, exposure in county_exposure.items():
            share = float(exposure["share_pct"])  # type: ignore[arg-type]
            if share >= config.min_county_share_pct:
                county = county_lookup.get(cid, {})
                exposed.append(
                    {
                        "county_id": cid,
                        "county_name": str(county.get("name", cid)),
                        "population": int(county.get("population", 0) or 0),  # type: ignore[call-overload]
                        "share_pct": share,
                        "lead_time_days": int(exposure["lead_time_days"]),  # type: ignore[call-overload]
                    }
                )

        if not exposed:
            continue

        # Alternative supplier count.
        exposed_county_ids = {str(c["county_id"]) for c in exposed}
        alt_supplier_ids: set[str] = set()
        for row in supply_rows:
            if row.get("active_ingredient") != ingredient:
                continue
            if str(row.get("county_id")) not in exposed_county_ids:
                continue
            if str(row.get("supplier_id")) in linked_supplier_ids:
                continue
            alt_supplier_ids.add(str(row.get("supplier_id")))
        alternative_supplier_count = len(alt_supplier_ids)

        total_pop = sum(int(c["population"]) for c in exposed) or 1  # type: ignore[call-overload]
        weighted = sum(float(c["share_pct"]) * int(c["population"]) for c in exposed)  # type: ignore[arg-type, call-overload]
        avg_exposure = weighted / total_pop

        max_lead = max(int(c["lead_time_days"]) for c in exposed)  # type: ignore[call-overload]
        tte = max(max_lead, config.time_to_expiry_floor_days)

        severity = _exposure_severity(avg_exposure, alternative_supplier_count, config)
        regions = [str(c["county_name"]) for c in exposed]

        candidates.append(
            RiskSignalCandidate(
                kind="supply_chain_exposure",
                severity=severity,
                manufacturer_id=mfr_id_str,
                active_ingredient=ingredient,
                regions_affected=regions,
                exposure_pct=round(avg_exposure, 2),
                alternative_supplier_count=alternative_supplier_count,
                time_to_expiry_days=tte,
                recommended_action=(
                    f"Diversify {ingredient} sourcing. "
                    f"Exposure ~{avg_exposure:.0f}%, alternatives: {alternative_supplier_count}."
                ),
                evidence_document_ids=list(signal.evidence_document_ids),
                evidence_supply_ids=supply_row_ids,
            )
        )

    return candidates
