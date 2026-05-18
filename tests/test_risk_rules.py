"""Tests for the three risk detection rules (§8: repeat-violator, supply-chain, corroboration)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from regulatory.risk.config import (
    CorroborationConfig,
    RepeatViolatorConfig,
    RepeatViolatorThreshold,
    RiskSignalCandidate,
    SupplyChainConfig,
)
from regulatory.risk.corroboration import detect_cross_source_corroboration_sync
from regulatory.risk.repeat_violator import detect_repeat_violators_sync
from regulatory.risk.supply_chain import detect_supply_chain_exposure_sync

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_AS_OF = date(2026, 5, 18)
_WINDOW_START = _AS_OF - timedelta(days=24 * 30)  # 720 days back from AS_OF


def _rv_config(
    *,
    window_months: int = 24,
    weights: dict[str, int] | None = None,
    thresholds: dict[str, RepeatViolatorThreshold] | None = None,
) -> RepeatViolatorConfig:
    return RepeatViolatorConfig(
        window_months=window_months,
        severity_weights=weights or {"class_1": 3, "class_2": 2, "class_3": 1, "unclassified": 1},
        thresholds=thresholds
        or {
            "medium": RepeatViolatorThreshold(min_recalls=3, min_weighted_score=4),
            "high": RepeatViolatorThreshold(min_recalls=4, min_weighted_score=8),
            "critical": RepeatViolatorThreshold(min_recalls=6, min_weighted_score=12),
        },
    )


def _sc_config(**kwargs: object) -> SupplyChainConfig:
    return SupplyChainConfig(
        min_county_share_pct=float(kwargs.get("min_county_share_pct", 25.0)),
        min_alternative_suppliers_for_low=int(kwargs.get("min_alternative_suppliers_for_low", 3)),
        time_to_expiry_floor_days=int(kwargs.get("time_to_expiry_floor_days", 60)),
    )


def _corr_config(**kwargs: object) -> CorroborationConfig:
    return CorroborationConfig(
        enable=bool(kwargs.get("enable", True)),
        jurisdiction_count_for_boost=int(kwargs.get("jurisdiction_count_for_boost", 2)),
        boost_levels=int(kwargs.get("boost_levels", 1)),
    )


def _doc(
    *,
    doc_id: str,
    mfr_ids: list[str],
    severity: str | None,
    date_published: date,
    doc_type: str = "recall",
    ingredients: list[str] | None = None,
    jurisdiction: str = "KE",
) -> dict[str, object]:
    return {
        "id": doc_id,
        "canonical_manufacturer_ids": mfr_ids,
        "severity": severity,
        "date_published": date_published,
        "document_type": doc_type,
        "active_ingredients": ingredients or [],
        "jurisdiction": jurisdiction,
    }


# ============================================================
# Repeat-violator rule tests
# ============================================================


def test_below_threshold_no_signal() -> None:
    """Manufacturer with 2 recalls → no signal (medium threshold requires 3)."""
    docs = [
        _doc(doc_id="d1", mfr_ids=["mfr-a"], severity="class_2", date_published=_AS_OF),
        _doc(doc_id="d2", mfr_ids=["mfr-a"], severity="class_2", date_published=_AS_OF),
    ]
    results = detect_repeat_violators_sync(docs, as_of=_AS_OF, config=_rv_config())
    assert results == []


def test_at_medium_threshold() -> None:
    """3 class_2 recalls (score 6 ≥ 4) → medium signal with correct evidence."""
    docs = [
        _doc(doc_id=f"d{i}", mfr_ids=["mfr-b"], severity="class_2", date_published=_AS_OF)
        for i in range(3)
    ]
    results = detect_repeat_violators_sync(docs, as_of=_AS_OF, config=_rv_config())
    assert len(results) == 1
    sig = results[0]
    assert sig.severity == "medium"
    assert sig.manufacturer_id == "mfr-b"
    assert set(sig.evidence_document_ids) == {"d0", "d1", "d2"}


def test_severity_weighting() -> None:
    """2 class_1 (score 6) + 1 class_3 (score 1) = total 7 → high (requires score ≥ 8? No, 7 < 8).

    Actually: 2*3 + 1*1 = 7 < 8, count=3, so medium. Let's use 3 class_1 = score 9 → high.
    """
    docs = [
        _doc(doc_id=f"d{i}", mfr_ids=["mfr-c"], severity="class_1", date_published=_AS_OF)
        for i in range(4)  # 4 recalls, score=12 → critical
    ]
    results = detect_repeat_violators_sync(docs, as_of=_AS_OF, config=_rv_config())
    assert len(results) == 1
    # 4 recalls, score=12 → critical threshold (min_recalls=6, score=12): score met but not count
    # high threshold (min_recalls=4, score=8): both met → high
    assert results[0].severity == "high"


def test_window_boundary_inclusive_start_exclusive_end() -> None:
    """Recall on window_start is included; recall after as_of is excluded."""
    in_window = _WINDOW_START  # exactly at boundary — should be included
    after_as_of = date(2026, 5, 19)  # one day after — should be excluded
    docs = [
        _doc(doc_id="in1", mfr_ids=["mfr-d"], severity="class_2", date_published=in_window),
        _doc(doc_id="in2", mfr_ids=["mfr-d"], severity="class_2", date_published=_AS_OF),
        _doc(doc_id="in3", mfr_ids=["mfr-d"], severity="class_2", date_published=_AS_OF),
        _doc(doc_id="out1", mfr_ids=["mfr-d"], severity="class_1", date_published=after_as_of),
    ]
    results = detect_repeat_violators_sync(docs, as_of=_AS_OF, config=_rv_config())
    assert len(results) == 1
    # 3 in-window docs → medium; out-of-window excluded
    assert "out1" not in results[0].evidence_document_ids
    assert "in1" in results[0].evidence_document_ids


def test_unclassified_severity_treated_as_one_point() -> None:
    """Unclassified recalls each count as 1 weighted point."""
    docs = [
        _doc(doc_id=f"u{i}", mfr_ids=["mfr-e"], severity="unclassified", date_published=_AS_OF)
        for i in range(5)
    ]
    results = detect_repeat_violators_sync(docs, as_of=_AS_OF, config=_rv_config())
    assert len(results) == 1
    # 5 recalls, score=5 → medium (min_recalls=3, score=4: both met)
    assert results[0].severity == "medium"


def test_canonical_manufacturer_join() -> None:
    """Three docs with same mfr_id produce one signal at count 3."""
    docs = [
        _doc(doc_id=f"c{i}", mfr_ids=["mfr-f"], severity="class_2", date_published=_AS_OF)
        for i in range(3)
    ]
    results = detect_repeat_violators_sync(docs, as_of=_AS_OF, config=_rv_config())
    assert len(results) == 1
    assert len(results[0].evidence_document_ids) == 3


def test_as_of_in_the_past() -> None:
    """as_of=2024-06-01 only includes docs on or before that date."""
    old_as_of = date(2024, 6, 1)
    docs = [
        _doc(doc_id="old1", mfr_ids=["mfr-g"], severity="class_2",
             date_published=date(2024, 5, 1)),
        _doc(doc_id="old2", mfr_ids=["mfr-g"], severity="class_2",
             date_published=date(2024, 5, 15)),
        _doc(doc_id="old3", mfr_ids=["mfr-g"], severity="class_2",
             date_published=date(2024, 6, 1)),
        _doc(doc_id="new1", mfr_ids=["mfr-g"], severity="class_1",
             date_published=date(2025, 1, 1)),
    ]
    results = detect_repeat_violators_sync(docs, as_of=old_as_of, config=_rv_config())
    assert len(results) == 1
    assert "new1" not in results[0].evidence_document_ids
    assert "old1" in results[0].evidence_document_ids


# ============================================================
# Supply-chain rule tests
# ============================================================

_C1_ID = "county-1"
_C2_ID = "county-2"
_S1_ID = "supplier-1"
_S2_ID = "supplier-2"
_S3_ID = "supplier-3"
_MFR_ID = "mfr-sc"
_INGREDIENT = "amoxicillin"

_COUNTIES = [
    {"id": _C1_ID, "name": "Nakuru", "population": 2162202},
    {"id": _C2_ID, "name": "Uasin Gishu", "population": 1163186},
]

_SUPPLIER_TO_MFR = {_S1_ID: _MFR_ID}

_BASE_SIGNAL = RiskSignalCandidate(
    kind="repeat_violator",
    severity="medium",
    manufacturer_id=_MFR_ID,
    active_ingredient=_INGREDIENT,
)


def _supply_row(
    sid: str,
    cid: str,
    share: float,
    lead: int = 30,
    ingredient: str = _INGREDIENT,
) -> dict[str, object]:
    return {
        "id": f"sr-{sid}-{cid}",
        "county_id": cid,
        "supplier_id": sid,
        "active_ingredient": ingredient,
        "share_pct": Decimal(str(share)),
        "lead_time_days": lead,
    }


def test_no_signal_when_manufacturer_below_min_share() -> None:
    """Manufacturer with 20% share (< 25% threshold) → no signal."""
    supply = [_supply_row(_S1_ID, _C1_ID, 20.0)]
    results = detect_supply_chain_exposure_sync(
        supply, _COUNTIES, as_of=_AS_OF, config=_sc_config(),
        repeat_violator_signals=[_BASE_SIGNAL], supplier_to_manufacturer=_SUPPLIER_TO_MFR,
    )
    assert results == []


def test_signal_emitted_with_correct_exposure() -> None:
    """Manufacturer at 40% of Nakuru amoxicillin → signal with exposure_pct=40."""
    supply = [_supply_row(_S1_ID, _C1_ID, 40.0)]
    results = detect_supply_chain_exposure_sync(
        supply, _COUNTIES, as_of=_AS_OF, config=_sc_config(),
        repeat_violator_signals=[_BASE_SIGNAL], supplier_to_manufacturer=_SUPPLIER_TO_MFR,
    )
    assert len(results) == 1
    sig = results[0]
    assert float(sig.exposure_pct or 0) == pytest.approx(40.0, abs=1.0)
    assert "Nakuru" in sig.regions_affected


def test_alternative_supplier_count() -> None:
    """2 other suppliers for the same ingredient in that county → alternative_count=2."""
    supply = [
        _supply_row(_S1_ID, _C1_ID, 40.0),
        _supply_row(_S2_ID, _C1_ID, 30.0),
        _supply_row(_S3_ID, _C1_ID, 30.0),
    ]
    results = detect_supply_chain_exposure_sync(
        supply, _COUNTIES, as_of=_AS_OF, config=_sc_config(),
        repeat_violator_signals=[_BASE_SIGNAL], supplier_to_manufacturer=_SUPPLIER_TO_MFR,
    )
    assert len(results) == 1
    assert results[0].alternative_supplier_count == 2


def test_time_to_expiry_respects_lead_time_floor() -> None:
    """lead_time=45d, config floor=60d → tte=60."""
    supply = [_supply_row(_S1_ID, _C1_ID, 40.0, lead=45)]
    results = detect_supply_chain_exposure_sync(
        supply, _COUNTIES, as_of=_AS_OF, config=_sc_config(time_to_expiry_floor_days=60),
        repeat_violator_signals=[_BASE_SIGNAL], supplier_to_manufacturer=_SUPPLIER_TO_MFR,
    )
    assert len(results) == 1
    assert results[0].time_to_expiry_days == 60


def test_no_signal_when_active_ingredient_unknown() -> None:
    """Signal with empty active_ingredient → skipped."""
    signal_no_ingredient = RiskSignalCandidate(
        kind="repeat_violator", severity="medium",
        manufacturer_id=_MFR_ID, active_ingredient=None,
    )
    supply = [_supply_row(_S1_ID, _C1_ID, 60.0)]
    results = detect_supply_chain_exposure_sync(
        supply, _COUNTIES, as_of=_AS_OF, config=_sc_config(),
        repeat_violator_signals=[signal_no_ingredient],
        supplier_to_manufacturer=_SUPPLIER_TO_MFR,
    )
    assert results == []


def test_multi_county_aggregation() -> None:
    """Same manufacturer exposing Nakuru and Uasin Gishu → one signal, both regions listed."""
    supply = [
        _supply_row(_S1_ID, _C1_ID, 40.0),
        _supply_row(_S1_ID, _C2_ID, 35.0),
    ]
    results = detect_supply_chain_exposure_sync(
        supply, _COUNTIES, as_of=_AS_OF, config=_sc_config(),
        repeat_violator_signals=[_BASE_SIGNAL], supplier_to_manufacturer=_SUPPLIER_TO_MFR,
    )
    assert len(results) == 1
    sig = results[0]
    assert "Nakuru" in sig.regions_affected
    assert "Uasin Gishu" in sig.regions_affected


# ============================================================
# Corroboration rule tests
# ============================================================


def test_single_jurisdiction_no_corroboration() -> None:
    """Only openFDA (US) recalls → no corroboration signal."""
    docs = [
        _doc(doc_id="us1", mfr_ids=[], severity=None,
             date_published=_AS_OF, ingredients=["amoxicillin"], jurisdiction="US"),
    ]
    results = detect_cross_source_corroboration_sync(docs, as_of=_AS_OF, config=_corr_config())
    assert results == []


def test_two_jurisdictions_emits_corroboration() -> None:
    """openFDA (US) + SAHPRA (ZA) recall same ingredient → corroboration signal."""
    docs = [
        _doc(doc_id="us1", mfr_ids=[], severity=None,
             date_published=_AS_OF, ingredients=["amoxicillin"], jurisdiction="US"),
        _doc(doc_id="za1", mfr_ids=[], severity=None,
             date_published=_AS_OF, ingredients=["amoxicillin"], jurisdiction="ZA"),
    ]
    results = detect_cross_source_corroboration_sync(docs, as_of=_AS_OF, config=_corr_config())
    assert len(results) == 1
    sig = results[0]
    assert sig.kind == "cross_source_corroboration"
    assert sig.active_ingredient == "amoxicillin"
    assert "US" in sig.regions_affected and "ZA" in sig.regions_affected


def test_boost_applied_to_existing_signals_only() -> None:
    """Corroboration signal is emitted independently; no orphaned boost logic here."""
    docs = [
        _doc(doc_id="ke1", mfr_ids=[], severity=None,
             date_published=_AS_OF, ingredients=["ceftriaxone"], jurisdiction="KE"),
        _doc(doc_id="et1", mfr_ids=[], severity=None,
             date_published=_AS_OF, ingredients=["ceftriaxone"], jurisdiction="ET"),
    ]
    results = detect_cross_source_corroboration_sync(docs, as_of=_AS_OF, config=_corr_config())
    assert len(results) == 1
    assert results[0].kind == "cross_source_corroboration"


def test_corroboration_disabled_by_config() -> None:
    """enable=False → no corroboration signals emitted."""
    docs = [
        _doc(doc_id="ke1", mfr_ids=[], severity=None,
             date_published=_AS_OF, ingredients=["paracetamol"], jurisdiction="KE"),
        _doc(doc_id="ng1", mfr_ids=[], severity=None,
             date_published=_AS_OF, ingredients=["paracetamol"], jurisdiction="NG"),
    ]
    results = detect_cross_source_corroboration_sync(
        docs, as_of=_AS_OF, config=_corr_config(enable=False)
    )
    assert results == []
