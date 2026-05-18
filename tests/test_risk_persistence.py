"""Tests for risk signal persistence, idempotence, audit trail, and merge semantics (§8)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from regulatory.risk.config import (
    CorroborationConfig,
    RepeatViolatorConfig,
    RepeatViolatorThreshold,
    RiskRulesConfig,
    RiskSignalCandidate,
    SupplyChainConfig,
)
from regulatory.risk.persist import (
    _evidence_changed,
    _signal_key,
    boost_severity,
    explain_signal,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AS_OF = date(2026, 5, 18)


def _make_config(version: str = "1.0") -> RiskRulesConfig:
    return RiskRulesConfig(
        version=version,
        repeat_violator=RepeatViolatorConfig(
            window_months=24,
            severity_weights={"class_1": 3, "class_2": 2, "class_3": 1, "unclassified": 1},
            thresholds={
                "medium": RepeatViolatorThreshold(min_recalls=3, min_weighted_score=4),
                "high": RepeatViolatorThreshold(min_recalls=4, min_weighted_score=8),
                "critical": RepeatViolatorThreshold(min_recalls=6, min_weighted_score=12),
            },
        ),
        supply_chain_exposure=SupplyChainConfig(
            min_county_share_pct=25.0,
            min_alternative_suppliers_for_low=3,
            time_to_expiry_floor_days=60,
        ),
        cross_source_corroboration=CorroborationConfig(
            enable=True,
            jurisdiction_count_for_boost=2,
            boost_levels=1,
        ),
    )


def _make_candidate(
    kind: str = "repeat_violator",
    severity: str = "medium",
    mfr_id: str | None = None,
    ingredient: str | None = "amoxicillin",
) -> RiskSignalCandidate:
    return RiskSignalCandidate(
        kind=kind,
        severity=severity,
        manufacturer_id=mfr_id or str(uuid.uuid4()),
        active_ingredient=ingredient,
        evidence_document_ids=["doc-1", "doc-2"],
    )


def _make_signal_mock(
    kind: str = "repeat_violator",
    severity: str = "medium",
    status: str = "active",
    mfr_id: uuid.UUID | None = None,
    ingredient: str | None = "amoxicillin",
    evidence: dict | None = None,
    first_seen: datetime | None = None,
    last_updated: datetime | None = None,
) -> MagicMock:
    sig = MagicMock()
    sig.id = uuid.uuid4()
    sig.kind = kind
    sig.severity = severity
    sig.status = status
    sig.manufacturer_id = mfr_id or uuid.uuid4()
    sig.active_ingredient = ingredient
    sig.regions_affected = []
    sig.exposure_pct = None
    sig.alternative_supplier_count = None
    sig.recommended_action = ""
    sig.time_to_expiry_days = None
    sig.evidence = evidence or {
        "document_ids": ["doc-1", "doc-2"],
        "rule_version": "1.0",
        "config_hash": "abc",
    }
    sig.first_seen = first_seen or datetime(2025, 1, 1, tzinfo=timezone.utc)
    sig.last_updated = last_updated or datetime(2026, 1, 1, tzinfo=timezone.utc)
    sig.resolved_at = None
    return sig


# ---------------------------------------------------------------------------
# Test: _signal_key is deterministic
# ---------------------------------------------------------------------------


def test_signal_key_deterministic() -> None:
    mfr = uuid.uuid4()
    k1 = _signal_key("repeat_violator", mfr, "amoxicillin")
    k2 = _signal_key("repeat_violator", mfr, "amoxicillin")
    assert k1 == k2


# ---------------------------------------------------------------------------
# Test: _evidence_changed ignores computed_at
# ---------------------------------------------------------------------------


def test_evidence_changed_ignores_computed_at() -> None:
    base = {"document_ids": ["d1"], "computed_at": "2026-01-01T00:00:00", "rule_version": "1.0"}
    updated = dict(base)
    updated["computed_at"] = "2026-06-01T00:00:00"
    assert not _evidence_changed(base, updated)


def test_evidence_changed_detects_new_document() -> None:
    base = {"document_ids": ["d1"], "computed_at": "2026-01-01T00:00:00"}
    updated = {"document_ids": ["d1", "d2"], "computed_at": "2026-01-01T00:00:00"}
    assert _evidence_changed(base, updated)


# ---------------------------------------------------------------------------
# Test: boost_severity
# ---------------------------------------------------------------------------


def test_boost_severity_increments() -> None:
    assert boost_severity("low", 1) == "medium"
    assert boost_severity("medium", 1) == "high"
    assert boost_severity("high", 1) == "critical"
    assert boost_severity("critical", 1) == "critical"  # capped


def test_boost_severity_unknown_unchanged() -> None:
    assert boost_severity("unknown_level", 1) == "unknown_level"


# ---------------------------------------------------------------------------
# Test: explain_signal is deterministic
# ---------------------------------------------------------------------------


def test_explain_signal_is_deterministic() -> None:
    sig = _make_signal_mock(
        evidence={
            "document_ids": ["doc-a", "doc-b"],
            "supply_ids": ["sup-1"],
            "rule_version": "1.0",
            "config_hash": "deadbeef" * 8,
            "computed_at": "2026-05-18T00:00:00+00:00",
            "as_of": "2026-05-18",
        }
    )
    sig.recommended_action = "Diversify amoxicillin sourcing."
    output1 = explain_signal(sig)
    output2 = explain_signal(sig)
    assert output1 == output2
    assert "doc-a" in output1
    assert "doc-b" in output1
    assert "sup-1" in output1
    assert "Diversify amoxicillin sourcing." in output1


# ---------------------------------------------------------------------------
# Test: persist_signals — idempotent re-run (unit, in-memory mocks)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_rerun() -> None:
    """Running persist_signals twice with unchanged candidates → second run all unchanged."""
    from regulatory.risk.persist import persist_signals

    mfr_id = str(uuid.uuid4())
    candidate = _make_candidate(mfr_id=mfr_id)
    config = _make_config()

    # Build a mock session that simulates: first run inserts, second run finds existing.
    existing_signal = _make_signal_mock(
        mfr_id=uuid.UUID(mfr_id),
        evidence={
            "document_ids": ["doc-1", "doc-2"],
            "supply_ids": [],
            "rule_version": "1.0",
            "config_hash": config.config_hash,
            "computed_at": "2026-01-01T00:00:00+00:00",
            "as_of": "2026-05-18",
            "inputs": {
                "manufacturer_id": mfr_id,
                "active_ingredient": "amoxicillin",
                "kind": "repeat_violator",
            },
        },
    )

    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = [existing_signal]
    session.execute.return_value = execute_result
    session.get = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()

    counts = await persist_signals(
        session, [candidate], config=config, as_of=_AS_OF, dry_run=False, actor="test"
    )
    # Evidence unchanged → should be "unchanged"
    assert counts["unchanged"] == 1
    assert counts["updated"] == 0
    assert counts["created"] == 0


# ---------------------------------------------------------------------------
# Test: signal resolved when rule stops firing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signal_resolved_when_rule_stops_firing() -> None:
    """An active signal with no matching candidate → should be resolved."""
    from regulatory.risk.persist import persist_signals

    config = _make_config()
    existing_signal = _make_signal_mock()

    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = [existing_signal]
    session.execute.return_value = execute_result
    session.get = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()

    # No candidates → rule no longer fires
    counts = await persist_signals(
        session, [], config=config, as_of=_AS_OF, dry_run=False, actor="test"
    )
    assert counts["resolved"] == 1
    assert existing_signal.status == "resolved"
    assert existing_signal.resolved_at is not None


# ---------------------------------------------------------------------------
# Test: audit log captures transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_captures_create_transition() -> None:
    """Creating a new signal writes one event row."""
    from regulatory.risk.persist import persist_signals

    mfr_id = str(uuid.uuid4())
    candidate = _make_candidate(mfr_id=mfr_id)
    config = _make_config()

    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []
    session.execute.return_value = execute_result
    session.get = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    added_objects: list[object] = []
    session.add = MagicMock(side_effect=added_objects.append)

    counts = await persist_signals(
        session, [candidate], config=config, as_of=_AS_OF, dry_run=False, actor="test"
    )
    assert counts["created"] == 1
    # Should have added one RiskSignal + one RiskSignalEvent
    from regulatory.db.models import RiskSignal, RiskSignalEvent
    signal_adds = [o for o in added_objects if isinstance(o, RiskSignal)]
    event_adds = [o for o in added_objects if isinstance(o, RiskSignalEvent)]
    assert len(signal_adds) == 1
    assert len(event_adds) == 1
    assert event_adds[0].event_type == "created"
    assert event_adds[0].actor == "test"


# ---------------------------------------------------------------------------
# Test: dry_run doesn't commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_does_not_write() -> None:
    """dry_run=True computes counts but never calls session.add or commit."""
    from regulatory.risk.persist import persist_signals

    mfr_id = str(uuid.uuid4())
    candidate = _make_candidate(mfr_id=mfr_id)
    config = _make_config()

    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []
    session.execute.return_value = execute_result
    session.get = AsyncMock(return_value=None)

    counts = await persist_signals(
        session, [candidate], config=config, as_of=_AS_OF, dry_run=True, actor="test"
    )
    assert counts["created"] == 1
    session.add.assert_not_called()
    session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Test: manufacturer merge carries first_seen forward
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_resolves_old_signal_and_carries_first_seen() -> None:
    """Merge A→B: old signal resolved, new signal's first_seen = old signal's first_seen."""
    from regulatory.risk.persist import (
        resolve_signal_for_manufacturer_merge,
    )

    old_first_seen = datetime(2025, 1, 1, tzinfo=timezone.utc)
    old_mfr_id = uuid.uuid4()
    ingredient = "amoxicillin"

    old_signal = _make_signal_mock(
        mfr_id=old_mfr_id,
        ingredient=ingredient,
        first_seen=old_first_seen,
        status="active",
    )

    # Step 1: resolve old signal (simulates merge --apply calling this).
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = [old_signal]
    session.execute.return_value = execute_result
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()

    count = await resolve_signal_for_manufacturer_merge(session, old_mfr_id, actor="cli-user")
    assert count == 1
    assert old_signal.status == "resolved"
    assert old_signal.resolved_at is not None

    # Verify an event was written with reason=manufacturer_merged.
    from regulatory.db.models import RiskSignal, RiskSignalEvent

    event_adds = [o for o in session.add.call_args_list if isinstance(o.args[0], RiskSignalEvent)]
    assert len(event_adds) == 1
    assert event_adds[0].args[0].reason == "manufacturer_merged"

    # Step 2: new signal for merged manufacturer B carries first_seen forward.
    from regulatory.risk.persist import persist_signals

    new_mfr_id = str(uuid.uuid4())
    candidate_b = RiskSignalCandidate(
        kind="repeat_violator",
        severity="medium",
        manufacturer_id=new_mfr_id,
        active_ingredient=ingredient,
        evidence_document_ids=["doc-1", "doc-2"],
        first_seen_override=old_first_seen.isoformat(),
    )
    session2 = AsyncMock()
    execute_result2 = MagicMock()
    execute_result2.scalars.return_value.all.return_value = []
    session2.execute.return_value = execute_result2
    session2.get = AsyncMock(return_value=None)
    session2.flush = AsyncMock()
    session2.commit = AsyncMock()
    added_objects2: list[object] = []
    session2.add = MagicMock(side_effect=added_objects2.append)

    counts2 = await persist_signals(
        session2,
        [candidate_b],
        config=_make_config(),
        as_of=_AS_OF,
        dry_run=False,
        actor="test",
    )
    assert counts2["created"] == 1  # assertion (ii): new signal on B was created

    signal_adds2 = [o for o in added_objects2 if isinstance(o, RiskSignal)]
    assert len(signal_adds2) == 1
    assert signal_adds2[0].first_seen == old_first_seen  # assertion (iii): first_seen carried


# ---------------------------------------------------------------------------
# Test: config_hash change marks signal stale
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test: persist updates signal when evidence changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_updates_when_evidence_changes() -> None:
    """Existing signal with different document_ids → updated path writes event."""
    from regulatory.risk.persist import persist_signals

    mfr_id = str(uuid.uuid4())
    config = _make_config()
    candidate = _make_candidate(mfr_id=mfr_id)

    # Existing signal has doc-99, candidate has doc-1, doc-2 → evidence differs.
    existing_signal = _make_signal_mock(
        mfr_id=uuid.UUID(mfr_id),
        evidence={
            "document_ids": ["doc-99"],
            "supply_ids": [],
            "rule_version": "1.0",
            "config_hash": config.config_hash,
            "computed_at": "2026-01-01T00:00:00+00:00",
            "as_of": "2026-05-18",
            "inputs": {
                "manufacturer_id": mfr_id,
                "active_ingredient": "amoxicillin",
                "kind": "repeat_violator",
            },
        },
    )

    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = [existing_signal]
    session.execute.return_value = execute_result
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    added_objects: list[object] = []
    session.add = MagicMock(side_effect=added_objects.append)

    counts = await persist_signals(
        session, [candidate], config=config, as_of=_AS_OF, dry_run=False, actor="test"
    )
    assert counts["updated"] == 1
    assert counts["unchanged"] == 0
    # An update event should have been written.
    from regulatory.db.models import RiskSignalEvent

    event_adds = [o for o in added_objects if isinstance(o, RiskSignalEvent)]
    assert len(event_adds) == 1
    assert event_adds[0].event_type == "updated"


# ---------------------------------------------------------------------------
# Test: persist applies corroboration boost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_applies_corroboration_boost() -> None:
    """Signal with matching corroboration candidate gets its severity boosted."""
    from regulatory.risk.persist import persist_signals

    mfr_id = str(uuid.uuid4())
    config = _make_config()
    candidate = _make_candidate(mfr_id=mfr_id, severity="low")

    corroboration = _make_candidate(
        kind="cross_source_corroboration",
        severity="medium",
        mfr_id=None,
        ingredient="amoxicillin",
    )

    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []
    session.execute.return_value = execute_result
    session.get = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    added_objects: list[object] = []
    session.add = MagicMock(side_effect=added_objects.append)

    counts = await persist_signals(
        session,
        [candidate],
        config=config,
        as_of=_AS_OF,
        corroboration_candidates=[corroboration],
        dry_run=False,
        actor="test",
    )
    # Both candidate and corroboration signal → 2 created.
    assert counts["created"] == 2
    from regulatory.db.models import RiskSignal

    signal_adds = [o for o in added_objects if isinstance(o, RiskSignal)]
    # Find the repeat_violator signal and verify it was boosted low→medium.
    rv_signals = [s for s in signal_adds if s.kind == "repeat_violator"]
    assert len(rv_signals) == 1
    assert rv_signals[0].severity == "medium"


# ---------------------------------------------------------------------------
# Test: config_hash change marks signal stale
# ---------------------------------------------------------------------------


def test_config_hash_differs_when_version_changes() -> None:
    """Two configs with different versions produce different config_hash."""
    config_v1 = _make_config("1.0")
    config_v2 = _make_config("2.0")
    assert config_v1.config_hash != config_v2.config_hash
