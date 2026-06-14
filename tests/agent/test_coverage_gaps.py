"""Coverage gap tests — targets the modules below 85% threshold.

Covers: audit.py (0%), tools.py tool bodies (50%), eval._check_response /
_run_recorded (30%), key_handling file I/O (56%), prompts.get_corpus_date_range,
runner error branches.
"""

from __future__ import annotations

import json
import stat
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# audit.py — full coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_audit_events_no_filter() -> None:
    from regulatory.agent.audit import list_audit_events

    row = MagicMock()
    row.id = uuid.uuid4()
    row.conversation_id = uuid.uuid4()
    row.turn_index = 0
    row.ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row.event_type = "user_message"
    row.model_id = "claude-sonnet-4-6"
    row.tokens_input = 100
    row.tokens_output = 50
    row.cost_usd_estimate = Decimal("0.002")
    row.payload = {"text": "what are the recalls?"}

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [row]
    session.execute = AsyncMock(return_value=result_mock)

    events = await list_audit_events(session)
    assert len(events) == 1
    assert events[0]["event_type"] == "user_message"
    assert events[0]["cost_usd_estimate"] is not None


@pytest.mark.asyncio
async def test_list_audit_events_with_conversation_filter() -> None:
    from regulatory.agent.audit import list_audit_events

    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result_mock)

    conv_id = uuid.uuid4()
    events = await list_audit_events(session, conversation_id=conv_id, since=date(2026, 1, 1))
    assert events == []


@pytest.mark.asyncio
async def test_get_audit_event_found() -> None:
    from regulatory.agent.audit import get_audit_event

    row = MagicMock()
    row.id = uuid.uuid4()
    row.conversation_id = uuid.uuid4()
    row.turn_index = 2
    row.ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row.event_type = "tool_call"
    row.model_id = "claude-sonnet-4-6"
    row.tokens_input = 200
    row.tokens_output = 0
    row.cost_usd_estimate = Decimal("0.001")
    row.config_version = "1.0"
    row.payload = {"tool_name": "list_risk_signals", "inputs": {}}

    session = AsyncMock()
    session.get = AsyncMock(return_value=row)

    result = await get_audit_event(session, uuid.uuid4())
    assert result is not None
    assert result["event_type"] == "tool_call"
    assert result["config_version"] == "1.0"


@pytest.mark.asyncio
async def test_get_audit_event_not_found() -> None:
    from regulatory.agent.audit import get_audit_event

    session = AsyncMock()
    session.get = AsyncMock(return_value=None)

    result = await get_audit_event(session, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_aggregate_cost_basic() -> None:
    from regulatory.agent.audit import aggregate_cost

    session = AsyncMock()
    row = MagicMock()
    row.total_cost = Decimal("1.23")
    row.total_input = 50000
    row.total_output = 10000
    row.conversations = 5
    result_mock = MagicMock()
    result_mock.one.return_value = row
    session.execute = AsyncMock(return_value=result_mock)

    stats = await aggregate_cost(session)
    assert stats["total_cost_usd"] == "1.23"
    assert stats["conversation_count"] == 5


@pytest.mark.asyncio
async def test_aggregate_cost_with_since_date() -> None:
    from regulatory.agent.audit import aggregate_cost

    session = AsyncMock()
    row = MagicMock()
    row.total_cost = None
    row.total_input = None
    row.total_output = None
    row.conversations = None
    result_mock = MagicMock()
    result_mock.one.return_value = row
    session.execute = AsyncMock(return_value=result_mock)

    stats = await aggregate_cost(session, since=date(2026, 1, 1))
    assert stats["total_cost_usd"] == "0"
    assert stats["conversation_count"] == 0


def test_summarize_payload_all_types() -> None:
    from regulatory.agent.audit import _summarize_payload

    assert "user:" in _summarize_payload("user_message", {"text": "hello"})
    assert "tool:" in _summarize_payload("tool_call", {"tool_name": "list_risk_signals", "tool_call_id": "abc"})
    assert "result:" in _summarize_payload("tool_result", {"error": None, "elapsed_ms": 42})
    assert "response:" in _summarize_payload("agent_response", {"text": "here is the answer"})
    assert "refusal:" in _summarize_payload("refusal", {"category": "medical"})
    assert "error:" in _summarize_payload("error", {"stage": "tool_dispatch", "message": "fail"})
    # Unknown type
    result = _summarize_payload("unknown", {"foo": "bar"})
    assert isinstance(result, str)

def test_summarize_payload_tool_result_with_error() -> None:
    from regulatory.agent.audit import _summarize_payload

    result = _summarize_payload(
        "tool_result",
        {"error": {"message": "not found"}, "elapsed_ms": 10},
    )
    assert "error=" in result


# ---------------------------------------------------------------------------
# tools.py — tool body coverage (happy paths with richer mocks)
# ---------------------------------------------------------------------------


def _make_mock_signal(kind: str = "repeat_violator", severity: str = "high") -> MagicMock:
    s = MagicMock()
    s.id = uuid.uuid4()
    s.kind = kind
    s.severity = severity
    s.status = "active"
    s.manufacturer_id = uuid.uuid4()
    s.active_ingredient = "amoxicillin"
    s.regions_affected = ["Nakuru"]
    s.exposure_pct = None
    s.first_seen = datetime(2025, 6, 1, tzinfo=timezone.utc)
    s.last_updated = datetime(2026, 1, 1, tzinfo=timezone.utc)
    s.recommended_action = "Monitor supply chain"
    s.alternative_supplier_count = 2
    s.evidence = {"document_ids": [], "rule_version": "1.0", "config_hash": "abc"}
    return s


def _make_mock_manufacturer(canonical_name: str = "Pfizer Inc.") -> MagicMock:
    m = MagicMock()
    m.id = uuid.uuid4()
    m.canonical_name = canonical_name
    m.aliases = ["Pfizer", "PFE"]
    m.countries = ["US"]
    m.confidence = 1.0
    return m


def _make_mock_document() -> MagicMock:
    d = MagicMock()
    d.id = uuid.uuid4()
    d.source_id = "openfda_drug"
    d.source_url = "https://api.fda.gov/drug/enforcement.json?search=recall_number:D-0001-2026"
    d.jurisdiction = "GLOBAL"
    d.document_type = "recall"
    d.title = "Voluntary Class I Recall: Amoxicillin Capsules"
    d.product_names = ["Amoxicillin Capsules 500mg"]
    d.active_ingredients = ["amoxicillin"]
    d.manufacturers = ["Pfizer Inc."]
    d.severity = "class_1"
    d.date_published = date(2026, 3, 1)
    d.date_effective = None
    d.regions_affected = ["US"]
    d.language = "en"
    d.raw_text = "Recall notice text for amoxicillin"
    d.raw_metadata = {}
    d.canonical_manufacturer_ids = []
    return d


@pytest.mark.asyncio
async def test_list_risk_signals_with_manufacturer_lookup() -> None:
    """Exercises the manufacturer name lookup branch (lines ~211-215)."""
    from regulatory.agent.tools import list_risk_signals

    sig = _make_mock_signal()
    mfr = _make_mock_manufacturer()

    total_mock = MagicMock()
    total_mock.scalar_one.return_value = 1

    signals_mock = MagicMock()
    signals_mock.scalars.return_value.all.return_value = [sig]

    call_count = 0

    async def fake_execute(stmt: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return total_mock
        return signals_mock

    session = AsyncMock()
    session.execute = fake_execute
    session.get = AsyncMock(return_value=mfr)

    result = await list_risk_signals(session, limit=10)
    assert len(result.signals) == 1
    assert result.signals[0].manufacturer_canonical_name == "Pfizer Inc."
    assert result.signals[0].brief_description == "Monitor supply chain"


@pytest.mark.asyncio
async def test_list_risk_signals_with_date_filters() -> None:
    """Exercises the since/until datetime filter branches."""
    from regulatory.agent.tools import list_risk_signals

    total_mock = MagicMock()
    total_mock.scalar_one.return_value = 0

    signals_mock = MagicMock()
    signals_mock.scalars.return_value.all.return_value = []

    call_count = 0

    async def fake_execute(stmt: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        return total_mock if call_count == 1 else signals_mock

    session = AsyncMock()
    session.execute = fake_execute
    session.get = AsyncMock(return_value=None)

    result = await list_risk_signals(
        session,
        since=date(2026, 1, 1),
        until=date(2026, 6, 30),
    )
    assert result.total_count == 0


@pytest.mark.asyncio
async def test_get_risk_signal_happy_path() -> None:
    """Exercises get_risk_signal with evidence_docs lookup and inflation flag."""
    from regulatory.agent.tools import get_risk_signal

    sig = _make_mock_signal()
    sig.evidence = {
        "document_ids": [str(uuid.uuid4()) for _ in range(12)],
        "rule_version": "1.0",
        "data_provenance": {"supply_chain_source": "synthetic_v2"},
    }
    mfr = _make_mock_manufacturer()
    docs = [_make_mock_document() for _ in range(12)]
    for d in docs:
        d.source_id = "openfda_drug"

    docs_result_mock = MagicMock()
    docs_result_mock.scalars.return_value.all.return_value = docs

    # session.get returns sig for RiskSignal, mfr for Manufacturer
    from regulatory.db.models import RiskSignal  # noqa: PLC0415

    async def fake_get(model_cls: Any, uid: Any) -> Any:
        if model_cls is RiskSignal:
            return sig
        return mfr

    session = AsyncMock()
    session.get = fake_get
    session.execute = AsyncMock(return_value=docs_result_mock)

    with patch("regulatory.agent.tools.explain_signal", return_value="Explain: 12 recalls"):
        result = await get_risk_signal(session, signal_id=str(sig.id))

    assert result.count_inflation_likely is True
    assert result.data_provenance is not None
    assert result.explain_text == "Explain: 12 recalls"


@pytest.mark.asyncio
async def test_manufacturer_profile_uuid_path() -> None:
    """Exercises the UUID lookup path (line ~320-330)."""
    from regulatory.agent.tools import manufacturer_profile

    mfr = _make_mock_manufacturer()
    mfr.aliases = ["Pfizer", "PFE"]
    mfr.countries = ["US"]

    docs_result = MagicMock()
    docs_result.scalars.return_value.all.return_value = []
    sigs_result = MagicMock()
    sigs_result.scalars.return_value.all.return_value = []

    session = AsyncMock()
    session.get = AsyncMock(return_value=mfr)
    session.execute = AsyncMock(side_effect=[docs_result, sigs_result])

    result = await manufacturer_profile(session, name_or_id=str(mfr.id))
    assert result.canonical_name == "Pfizer Inc."
    assert result.disambiguation_needed is False


@pytest.mark.asyncio
async def test_manufacturer_profile_exact_name_match() -> None:
    """Exercises the exact case-insensitive match path."""
    from regulatory.agent.tools import manufacturer_profile

    mfr = _make_mock_manufacturer("Pfizer Inc.")

    exact_result = MagicMock()
    exact_result.scalar_one_or_none.return_value = mfr
    docs_result = MagicMock()
    docs_result.scalars.return_value.all.return_value = []
    sigs_result = MagicMock()
    sigs_result.scalars.return_value.all.return_value = []

    session = AsyncMock()
    session.get = AsyncMock(return_value=None)  # Not a UUID
    session.execute = AsyncMock(side_effect=[exact_result, docs_result, sigs_result])

    result = await manufacturer_profile(session, name_or_id="pfizer inc.")
    assert result.canonical_name == "Pfizer Inc."


@pytest.mark.asyncio
async def test_manufacturer_profile_disambiguation() -> None:
    """Exercises the disambiguation path when multiple fuzzy matches exist."""
    from regulatory.agent.tools import manufacturer_profile

    mfr1 = _make_mock_manufacturer("Pfizer Inc.")
    mfr2 = _make_mock_manufacturer("Pfizer Kenya Ltd")

    exact_result = MagicMock()
    exact_result.scalar_one_or_none.return_value = None  # No exact match

    fuzzy_result = MagicMock()
    fuzzy_result.scalars.return_value.all.return_value = [mfr1, mfr2]

    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=[exact_result, fuzzy_result])

    result = await manufacturer_profile(session, name_or_id="pfizer")
    assert result.disambiguation_needed is True
    assert len(result.candidates) == 2


@pytest.mark.asyncio
async def test_manufacturer_profile_not_found() -> None:
    from regulatory.agent.tools import manufacturer_profile

    exact_result = MagicMock()
    exact_result.scalar_one_or_none.return_value = None
    fuzzy_result = MagicMock()
    fuzzy_result.scalars.return_value.all.return_value = []

    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=[exact_result, fuzzy_result])

    with pytest.raises(ValueError, match="not found"):
        await manufacturer_profile(session, name_or_id="NonExistent Corp")


@pytest.mark.asyncio
async def test_manufacturer_profile_with_docs_and_signals() -> None:
    """Exercises recall summary aggregation (lines ~378-408)."""
    from regulatory.agent.tools import manufacturer_profile

    mfr = _make_mock_manufacturer("Acme Pharma")
    mfr.id = uuid.uuid4()

    docs = []
    for i in range(3):
        d = _make_mock_document()
        d.source_id = "openfda_drug"
        d.severity = "class_1" if i == 0 else "class_2"
        d.product_names = [f"Product {i}"]
        d.active_ingredients = ["amoxicillin"]
        docs.append(d)

    sig = _make_mock_signal()

    exact_result = MagicMock()
    exact_result.scalar_one_or_none.return_value = mfr
    docs_result = MagicMock()
    docs_result.scalars.return_value.all.return_value = docs
    sigs_result = MagicMock()
    sigs_result.scalars.return_value.all.return_value = [sig]

    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=[exact_result, docs_result, sigs_result])

    result = await manufacturer_profile(session, name_or_id="Acme Pharma")
    assert result.recall_summary.total == 3
    assert result.recall_summary.by_source.get("openfda_drug") == 3
    assert "amoxicillin" in result.active_ingredients


@pytest.mark.asyncio
async def test_county_exposure_happy_path() -> None:
    """Exercises county supply loop and data_provenance assembly."""
    from regulatory.agent.tools import county_exposure

    county_obj = MagicMock()
    county_obj.id = uuid.uuid4()
    county_obj.name = "Nakuru"
    county_obj.region = "Rift Valley"
    county_obj.population = 2162202
    county_obj.health_facilities = 248

    supplier = MagicMock()
    supplier.id = uuid.uuid4()
    supplier.name = "Acme Supplies Ltd"
    supplier.manufacturer_id = uuid.uuid4()

    cs = MagicMock()
    cs.active_ingredient = "amoxicillin"
    cs.share_pct = Decimal("45.00")
    cs.lead_time_days = 30
    cs.contract_end = date(2026, 12, 31)
    cs.data_source = "synthetic_v2"

    # Signals query — no signals for this supplier.
    no_signals_result = MagicMock()
    no_signals_result.scalars.return_value.first.return_value = None

    # Alt count query.
    alt_count_result = MagicMock()
    alt_count_result.scalar_one.return_value = 3

    county_result = MagicMock()
    county_result.scalar_one_or_none.return_value = county_obj

    supply_result = MagicMock()
    supply_result.all.return_value = [(cs, supplier)]

    session = AsyncMock()
    execute_calls = [county_result, supply_result, no_signals_result, alt_count_result]
    call_idx = 0

    async def fake_execute(stmt: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_idx
        result = execute_calls[min(call_idx, len(execute_calls) - 1)]
        call_idx += 1
        return result

    session.execute = fake_execute

    result = await county_exposure(session, county="nakuru")
    assert result.county_name == "Nakuru"
    assert len(result.supply_mix) == 1
    assert result.supply_mix[0].active_ingredient == "amoxicillin"
    assert result.data_provenance["synthetic"] is True
    assert result.supply_mix[0].has_active_signal is False


@pytest.mark.asyncio
async def test_county_exposure_with_flagged_supplier() -> None:
    """Exercises the flagged_supplier_ids path."""
    from regulatory.agent.tools import county_exposure

    county_obj = MagicMock()
    county_obj.id = uuid.uuid4()
    county_obj.name = "Mombasa"
    county_obj.region = "Coast"
    county_obj.population = 1208333
    county_obj.health_facilities = 120

    supplier = MagicMock()
    supplier.id = uuid.uuid4()
    supplier.name = "Risky Supplier Co."
    supplier.manufacturer_id = uuid.uuid4()

    cs = MagicMock()
    cs.active_ingredient = "amoxicillin"
    cs.share_pct = Decimal("60.00")
    cs.lead_time_days = 45
    cs.contract_end = None
    cs.data_source = "synthetic_v2"

    # Signals query — returns an active signal.
    sig_mock = MagicMock()
    sig_mock.id = uuid.uuid4()
    active_signals_result = MagicMock()
    active_signals_result.scalars.return_value.all.return_value = [sig_mock]

    alt_count_result = MagicMock()
    alt_count_result.scalar_one.return_value = 2

    county_result = MagicMock()
    county_result.scalar_one_or_none.return_value = county_obj

    supply_result = MagicMock()
    supply_result.all.return_value = [(cs, supplier)]

    session = AsyncMock()
    execute_calls = [county_result, supply_result, active_signals_result, alt_count_result]
    call_idx = 0

    async def fake_execute(stmt: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_idx
        result = execute_calls[min(call_idx, len(execute_calls) - 1)]
        call_idx += 1
        return result

    session.execute = fake_execute

    result = await county_exposure(session, county="Mombasa")
    assert "Risky Supplier Co." in result.flagged_supplier_names
    assert result.supply_mix[0].has_active_signal is True


@pytest.mark.asyncio
async def test_search_documents_happy_path() -> None:
    """Exercises search_documents with manufacturer lookup."""
    from regulatory.agent.tools import search_documents

    doc = _make_mock_document()
    mfr = _make_mock_manufacturer()

    total_mock = MagicMock()
    total_mock.scalar_one.return_value = 1

    docs_mock = MagicMock()
    docs_mock.all.return_value = [(doc, 0.95)]

    mfr_mock = MagicMock()
    mfr_mock.scalars.return_value.all.return_value = [mfr]

    call_count = 0

    async def fake_execute(stmt: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return total_mock
        if call_count == 2:
            return docs_mock
        return mfr_mock

    session = AsyncMock()
    session.execute = fake_execute

    result = await search_documents(session, query="amoxicillin recall")
    assert len(result.results) == 1
    assert result.results[0].title == doc.title
    assert not result.truncated


@pytest.mark.asyncio
async def test_get_document_happy_path() -> None:
    """Exercises get_document with paginated raw_text."""
    from regulatory.agent.tools import get_document

    doc = _make_mock_document()
    doc.raw_text = "A" * 5000  # under the 8000-char limit

    session = AsyncMock()
    session.get = AsyncMock(return_value=doc)

    result = await get_document(session, document_id=str(doc.id))
    assert result.document_id == str(doc.id)
    assert result.truncated is False
    assert "<untrusted_content" in result.raw_text_wrapped
    assert "</untrusted_content>" in result.raw_text_wrapped


@pytest.mark.asyncio
async def test_get_document_truncated() -> None:
    """Exercises the truncated=True path."""
    from regulatory.agent.tools import get_document

    doc = _make_mock_document()
    doc.raw_text = "B" * 10000

    session = AsyncMock()
    session.get = AsyncMock(return_value=doc)

    result = await get_document(session, document_id=str(doc.id), offset=0)
    assert result.truncated is True
    assert result.total_chars == 10000


@pytest.mark.asyncio
async def test_dispatch_all_tools() -> None:
    """Exercises all six dispatch helper branches."""
    from regulatory.agent.tools import dispatch

    session = AsyncMock()

    total_mock = MagicMock()
    total_mock.scalar_one.return_value = 0
    signals_mock = MagicMock()
    signals_mock.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(side_effect=lambda *a, **k: total_mock)
    session.get = AsyncMock(return_value=None)

    # list_risk_signals
    r1 = await dispatch(session, "list_risk_signals", {"limit": 5})
    assert r1.total_count == 0

    # get_risk_signal — raises ValueError for missing signal
    session.get = AsyncMock(return_value=None)
    docs_mock = MagicMock()
    docs_mock.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=docs_mock)
    with pytest.raises(ValueError):
        await dispatch(session, "get_risk_signal", {"signal_id": str(uuid.uuid4())})

    # manufacturer_profile — raises ValueError for not found
    exact_mock = MagicMock()
    exact_mock.scalar_one_or_none.return_value = None
    fuzzy_mock = MagicMock()
    fuzzy_mock.scalars.return_value.all.return_value = []
    session.get = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=[exact_mock, fuzzy_mock])
    with pytest.raises(ValueError):
        await dispatch(session, "manufacturer_profile", {"name_or_id": "Unknown Corp"})

    # county_exposure — raises ValueError for not found
    county_mock = MagicMock()
    county_mock.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=county_mock)
    with pytest.raises(ValueError):
        await dispatch(session, "county_exposure", {"county": "NoSuchCounty"})

    # search_documents
    t_mock = MagicMock()
    t_mock.scalar_one.return_value = 0
    d_mock = MagicMock()
    d_mock.all.return_value = []
    session.execute = AsyncMock(side_effect=[t_mock, d_mock])
    r5 = await dispatch(session, "search_documents", {"query": "recall"})
    assert r5.total_count == 0

    # get_document — raises ValueError for not found
    session.get = AsyncMock(return_value=None)
    with pytest.raises(ValueError):
        await dispatch(session, "get_document", {"document_id": str(uuid.uuid4())})


# ---------------------------------------------------------------------------
# eval.py — _check_response and _run_recorded
# ---------------------------------------------------------------------------


class TestCheckResponse:
    def test_must_cite_passes_with_doc_ref(self) -> None:
        from regulatory.agent.eval import _check_response

        response = "Three recalls found [doc:abc-123] in 2025."
        ok, failures = _check_response(response, {"must_cite": True})
        assert ok
        assert not failures

    def test_must_cite_fails_without_ref(self) -> None:
        from regulatory.agent.eval import _check_response

        ok, failures = _check_response("No citations here.", {"must_cite": True})
        assert not ok
        assert "missing citations" in failures

    def test_must_refuse_passes(self) -> None:
        from regulatory.agent.eval import _check_response

        response = "I can't advise on what medication to take."
        ok, failures = _check_response(response, {"must_refuse": True})
        assert ok

    def test_must_refuse_fails_when_answered(self) -> None:
        from regulatory.agent.eval import _check_response

        ok, failures = _check_response(
            "You should take amoxicillin.",
            {"must_refuse": True},
        )
        assert not ok
        assert "expected refusal but got answer" in failures

    def test_must_contain_phrase(self) -> None:
        from regulatory.agent.eval import _check_response

        ok, failures = _check_response(
            "The answer involves healthcare provider recommendations.",
            {"must_contain_phrases": ["healthcare provider"]},
        )
        assert ok

    def test_must_not_contain_phrase(self) -> None:
        from regulatory.agent.eval import _check_response

        ok, failures = _check_response(
            "You should take amoxicillin daily.",
            {"must_not_contain_phrases": ["you should take"]},
        )
        assert not ok

    def test_must_include_synthetic_caveat(self) -> None:
        from regulatory.agent.eval import _check_response

        ok, failures = _check_response(
            "Supply is 45% synthetic data from county procurement.",
            {"must_include_caveat": "synthetic_data"},
        )
        assert ok

    def test_must_include_count_inflation_caveat(self) -> None:
        from regulatory.agent.eval import _check_response

        ok, failures = _check_response(
            "Note: this count reflects enforcement document filings.",
            {"must_include_caveat": "count_inflation"},
        )
        assert ok

    def test_must_include_count_inflation_caveat_missing(self) -> None:
        from regulatory.agent.eval import _check_response

        ok, failures = _check_response(
            "GenoGenix LLC had 57 recalls.",
            {"must_include_caveat": "count_inflation"},
        )
        assert not ok


def test_load_golden_qa(tmp_path: Path) -> None:
    from regulatory.agent.eval import _load_golden_qa

    qa_file = tmp_path / "golden_qa.yaml"
    qa_file.write_text(
        "- id: q001\n  user: hello\n  expected:\n    must_cite: false\n",
        encoding="utf-8",
    )
    with patch("regulatory.agent.eval._GOLDEN_QA_PATH", qa_file):
        questions = _load_golden_qa()
    assert len(questions) == 1
    assert questions[0]["id"] == "q001"


def test_run_recorded_with_transcript(tmp_path: Path) -> None:
    from regulatory.agent.eval import _run_recorded

    questions = [
        {
            "id": "q001",
            "user": "any question",
            "expected": {"must_cite": True},
        }
    ]
    transcript_file = tmp_path / "abc123.jsonl"
    transcript_file.write_text(
        json.dumps({
            "id": "q001",
            "response": "Here is the answer [doc:abc-123] for you.",
        }) + "\n",
        encoding="utf-8",
    )
    _run_recorded(questions, transcript_file)  # should not raise


def test_run_recorded_missing_transcript(tmp_path: Path) -> None:
    """When a question has no matching transcript entry, it's skipped gracefully."""
    from regulatory.agent.eval import _run_recorded

    questions = [
        {"id": "q001", "user": "hello", "expected": {"must_cite": True}},
        {"id": "q002", "user": "other", "expected": {"must_cite": False}},
    ]
    transcript_file = tmp_path / "abc123.jsonl"
    # Only q002 has a transcript entry; q001 should be skipped.
    transcript_file.write_text(
        json.dumps({"id": "q002", "response": "some answer"}) + "\n",
        encoding="utf-8",
    )
    _run_recorded(questions, transcript_file)  # q001 skipped, q002 evaluated, no error


# ---------------------------------------------------------------------------
# key_handling.py — file I/O coverage
# ---------------------------------------------------------------------------


def test_find_latest_transcript_with_files(tmp_path: Path) -> None:
    from regulatory.agent.eval import _find_latest_transcript

    transcript_dir = tmp_path / "eval_transcripts"
    transcript_dir.mkdir()
    (transcript_dir / "abc123.jsonl").write_text("{}\n", encoding="utf-8")
    (transcript_dir / "abc123_summary.json").write_text("{}", encoding="utf-8")

    with patch("regulatory.agent.eval._TRANSCRIPTS_DIR", transcript_dir):
        result = _find_latest_transcript()
    assert result is not None
    assert result.name == "abc123.jsonl"


def test_find_latest_transcript_no_dir(tmp_path: Path) -> None:
    from regulatory.agent.eval import _find_latest_transcript

    with patch("regulatory.agent.eval._TRANSCRIPTS_DIR", tmp_path / "nonexistent"):
        result = _find_latest_transcript()
    assert result is None


def test_check_response_missing_required_phrase() -> None:
    from regulatory.agent.eval import _check_response

    ok, failures = _check_response(
        "The answer mentions recall data.",
        {"must_contain_phrases": ["healthcare provider"]},
    )
    assert not ok
    assert any("missing phrase" in f for f in failures)


def test_check_response_missing_synthetic_caveat() -> None:
    from regulatory.agent.eval import _check_response

    ok, failures = _check_response(
        "Nakuru has 45% exposure from Acme.",
        {"must_include_caveat": "synthetic_data"},
    )
    assert not ok
    assert any("synthetic" in f for f in failures)


@pytest.mark.asyncio
async def test_run_eval_recorded_with_transcript(tmp_path: Path) -> None:
    """Exercises lines 110-112: the recorded-mode print + _run_recorded call."""
    from regulatory.agent.eval import run_eval

    transcript_dir = tmp_path / "eval_transcripts"
    transcript_dir.mkdir()
    transcript_file = transcript_dir / "abc123.jsonl"
    transcript_file.write_text(
        json.dumps({"id": "q001", "response": "answer [doc:x]"}) + "\n",
        encoding="utf-8",
    )

    qa = [{"id": "q001", "user": "question", "expected": {"must_cite": True}}]

    with patch("regulatory.agent.eval._load_golden_qa", return_value=qa):
        with patch("regulatory.agent.eval._find_latest_transcript", return_value=transcript_file):
            await run_eval(live=False)


def test_load_key_from_secrets_file_missing(tmp_path: Path) -> None:
    from regulatory.agent.key_handling import load_key_from_secrets_file

    with patch("regulatory.agent.key_handling._SECRETS_PATH", tmp_path / "nonexistent.env"):
        result = load_key_from_secrets_file()
    assert result is None


def test_load_key_from_secrets_file_present(tmp_path: Path) -> None:
    from regulatory.agent.key_handling import load_key_from_secrets_file

    secrets = tmp_path / "secrets.env"
    secrets.write_text("ANTHROPIC_API_KEY=sk-ant-test-LOADTEST\n", encoding="utf-8")
    with patch("regulatory.agent.key_handling._SECRETS_PATH", secrets):
        result = load_key_from_secrets_file()
    assert result == "sk-ant-test-LOADTEST"


def test_load_key_from_secrets_file_os_error(tmp_path: Path) -> None:
    """Exercises the OSError except branch (lines 84-86)."""
    from regulatory.agent.key_handling import load_key_from_secrets_file

    secrets = tmp_path / "secrets.env"
    secrets.write_text("ANTHROPIC_API_KEY=sk-ant-test\n", encoding="utf-8")
    with patch("regulatory.agent.key_handling._SECRETS_PATH", secrets):
        with patch.object(secrets.__class__, "read_text", side_effect=OSError("permission denied")):
            # Should return None on OSError.
            pass  # patch the class method — use a simpler approach:
    # Write an unreadable path simulation: just verify None is returned on read_text mock
    with patch("regulatory.agent.key_handling._SECRETS_PATH") as mock_path:
        mock_path.exists.return_value = True
        mock_path.read_text.side_effect = OSError("read error")
        result = load_key_from_secrets_file()
    assert result is None


def test_save_key_to_secrets_file_os_error() -> None:
    """Exercises the OSError branch in save_key_to_secrets_file (lines 103-105)."""
    from regulatory.agent.key_handling import save_key_to_secrets_file

    with patch("regulatory.agent.key_handling._SECRETS_PATH") as mock_path:
        mock_path.parent.mkdir = MagicMock(side_effect=OSError("permission denied"))
        result = save_key_to_secrets_file("sk-ant-test-FAIL")
    assert result is False


def test_validate_key_auth_error() -> None:
    """Exercises _validate_key body — returns False on AuthenticationError."""
    import anthropic

    from regulatory.agent.key_handling import _validate_key

    with patch("anthropic.Anthropic") as mock_cls:
        client = MagicMock()
        client.messages.create.side_effect = anthropic.AuthenticationError(
            message="invalid key", response=MagicMock(), body={}
        )
        mock_cls.return_value = client
        result = _validate_key("sk-ant-invalid", "claude-haiku-4-5-20251001")
    assert result is False


def test_validate_key_success() -> None:
    """Exercises _validate_key body — returns True on successful call."""
    from regulatory.agent.key_handling import _validate_key

    with patch("anthropic.Anthropic") as mock_cls:
        client = MagicMock()
        client.messages.create.return_value = MagicMock()
        mock_cls.return_value = client
        result = _validate_key("sk-ant-valid", "claude-haiku-4-5-20251001")
    assert result is True


def test_save_key_to_secrets_file(tmp_path: Path) -> None:
    import platform

    from regulatory.agent.key_handling import save_key_to_secrets_file

    secrets = tmp_path / "secrets.env"
    with patch("regulatory.agent.key_handling._SECRETS_PATH", secrets):
        ok = save_key_to_secrets_file("sk-ant-test-SAVETEST")
    assert ok
    assert secrets.read_text(encoding="utf-8").strip() == "ANTHROPIC_API_KEY=sk-ant-test-SAVETEST"
    if platform.system() != "Windows":
        mode = secrets.stat().st_mode & 0o777
        assert mode == stat.S_IRUSR | stat.S_IWUSR


# ---------------------------------------------------------------------------
# prompts.py — get_corpus_date_range
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_corpus_date_range_returns_dates() -> None:
    from regulatory.agent.prompts import get_corpus_date_range

    session = AsyncMock()
    row = MagicMock()
    row.start = date(2024, 1, 1)
    row.end = date(2026, 6, 1)
    result_mock = MagicMock()
    result_mock.one.return_value = row
    session.execute = AsyncMock(return_value=result_mock)

    start, end = await get_corpus_date_range(session)
    assert start == date(2024, 1, 1)
    assert end == date(2026, 6, 1)


@pytest.mark.asyncio
async def test_get_corpus_date_range_empty_corpus() -> None:
    from regulatory.agent.prompts import get_corpus_date_range

    session = AsyncMock()
    row = MagicMock()
    row.start = None
    row.end = None
    result_mock = MagicMock()
    result_mock.one.return_value = row
    session.execute = AsyncMock(return_value=result_mock)

    start, end = await get_corpus_date_range(session)
    assert start is None
    assert end is None


# ---------------------------------------------------------------------------
# runner.py — _write_audit error isolation + _ensure_system_prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_audit_isolates_db_errors() -> None:
    """DB write failure must not propagate out of _write_audit."""
    from regulatory.agent.runner import AgentRunner

    config = {
        "version": "1.0",
        "models": {"primary": "claude-sonnet-4-6", "fallback": "claude-haiku-4-5-20251001"},
        "generation": {"max_tokens_per_turn": 128, "temperature": 0.0},
        "budgets": {
            "tool_calls_per_turn": 8,
            "tokens_per_turn": 10000,
            "tokens_per_conversation": 50000,
            "cost_per_conversation_usd": "0.50",
            "cost_per_day_usd": "5.00",
            "budget_remaining_threshold_usd": "0.10",
        },
        "model_pricing_usd_per_million_tokens": {
            "claude-sonnet-4-6": {"input": 3.0, "output": 15.0}
        },
    }
    runner = AgentRunner(api_key="sk-ant-test", config=config)

    session = AsyncMock()
    session.add = MagicMock(side_effect=RuntimeError("DB down"))
    session.flush = AsyncMock(side_effect=RuntimeError("DB down"))

    # Must not raise.
    await runner._write_audit(session, "user_message", {"text": "test"})


@pytest.mark.asyncio
async def test_ensure_system_prompt_caches() -> None:
    """Second call to _ensure_system_prompt must not re-query DB."""
    from regulatory.agent.runner import AgentRunner

    config = {
        "version": "1.0",
        "models": {"primary": "claude-sonnet-4-6", "fallback": "claude-haiku-4-5-20251001"},
        "generation": {"max_tokens_per_turn": 128, "temperature": 0.0},
        "budgets": {
            "tool_calls_per_turn": 8,
            "tokens_per_turn": 10000,
            "tokens_per_conversation": 50000,
            "cost_per_conversation_usd": "0.50",
            "cost_per_day_usd": "5.00",
            "budget_remaining_threshold_usd": "0.10",
        },
        "model_pricing_usd_per_million_tokens": {},
    }
    runner = AgentRunner(api_key="sk-ant-test", config=config)
    session = AsyncMock()

    with patch("regulatory.agent.runner.get_corpus_date_range", return_value=(None, None)) as mock_cdr:
        with patch("regulatory.agent.runner.load_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(version="1.0")
            await runner._ensure_system_prompt(session)
            await runner._ensure_system_prompt(session)  # second call

    # DB query should only be called once.
    assert mock_cdr.call_count == 1


@pytest.mark.asyncio
async def test_runner_stop_reason_not_tool_use() -> None:
    """Exercises the stop_reason != 'end_turn' and != 'tool_use' branch (lines 311-325)."""
    from regulatory.agent.runner import AgentRunner

    config = {
        "version": "1.0",
        "models": {"primary": "claude-sonnet-4-6", "fallback": "claude-haiku-4-5-20251001"},
        "generation": {"max_tokens_per_turn": 128, "temperature": 0.0},
        "budgets": {
            "tool_calls_per_turn": 8,
            "tokens_per_turn": 10000,
            "tokens_per_conversation": 50000,
            "cost_per_conversation_usd": "0.50",
            "cost_per_day_usd": "5.00",
            "budget_remaining_threshold_usd": "0.10",
        },
        "model_pricing_usd_per_million_tokens": {
            "claude-sonnet-4-6": {"input": 3.0, "output": 15.0}
        },
    }

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "Max tokens reached."

    mock_response = MagicMock()
    mock_response.stop_reason = "max_tokens"  # not end_turn, not tool_use
    mock_response.content = [text_block]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)

    runner = AgentRunner(api_key="sk-ant-test", config=config)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one=MagicMock(return_value=0), one=MagicMock(return_value=MagicMock(start=None, end=None))))
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.get = AsyncMock(return_value=None)

    with patch("regulatory.agent.runner.get_session") as mock_get_session:
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(runner._client.messages, "create", return_value=mock_response):
            with patch("regulatory.agent.runner.get_corpus_date_range", return_value=(None, None)):
                with patch("regulatory.agent.runner.load_config") as mock_cfg:
                    mock_cfg.return_value = MagicMock(version="1.0")
                    result = await runner.run_turn("question")

    assert "Max tokens reached." in result


@pytest.mark.asyncio
async def test_runner_tool_dispatch_value_error() -> None:
    """Exercises the ValueError tool dispatch error path (lines 383-392)."""
    from regulatory.agent.runner import AgentRunner

    config = {
        "version": "1.0",
        "models": {"primary": "claude-sonnet-4-6", "fallback": "claude-haiku-4-5-20251001"},
        "generation": {"max_tokens_per_turn": 128, "temperature": 0.0},
        "budgets": {
            "tool_calls_per_turn": 8,
            "tokens_per_turn": 10000,
            "tokens_per_conversation": 50000,
            "cost_per_conversation_usd": "0.50",
            "cost_per_day_usd": "5.00",
            "budget_remaining_threshold_usd": "0.10",
        },
        "model_pricing_usd_per_million_tokens": {
            "claude-sonnet-4-6": {"input": 3.0, "output": 15.0}
        },
    }

    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.id = "toolu_01"
    tool_block.name = "get_risk_signal"
    tool_block.input = {"signal_id": "not-a-uuid"}

    tool_response = MagicMock()
    tool_response.stop_reason = "tool_use"
    tool_response.content = [tool_block]
    tool_response.usage = MagicMock(input_tokens=100, output_tokens=20)

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "I encountered an error retrieving that signal."

    final_response = MagicMock()
    final_response.stop_reason = "end_turn"
    final_response.content = [text_block]
    final_response.usage = MagicMock(input_tokens=150, output_tokens=50)

    runner = AgentRunner(api_key="sk-ant-test", config=config)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one=MagicMock(return_value=0)))
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.get = AsyncMock(return_value=None)

    call_count = 0

    def fake_create(**kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        return tool_response if call_count == 1 else final_response

    with patch("regulatory.agent.runner.get_session") as mock_get_session:
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(runner._client.messages, "create", side_effect=fake_create):
            with patch("regulatory.agent.runner.get_corpus_date_range", return_value=(None, None)):
                with patch("regulatory.agent.runner.load_config") as mock_cfg:
                    mock_cfg.return_value = MagicMock(version="1.0")
                    result = await runner.run_turn("get signal xyz")

    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_ensure_system_prompt_handles_config_error() -> None:
    """load_config failure must not crash _ensure_system_prompt."""
    from regulatory.agent.runner import AgentRunner

    config = {
        "version": "1.0",
        "models": {"primary": "claude-sonnet-4-6", "fallback": "claude-haiku-4-5-20251001"},
        "generation": {"max_tokens_per_turn": 128, "temperature": 0.0},
        "budgets": {
            "tool_calls_per_turn": 8,
            "tokens_per_turn": 10000,
            "tokens_per_conversation": 50000,
            "cost_per_conversation_usd": "0.50",
            "cost_per_day_usd": "5.00",
            "budget_remaining_threshold_usd": "0.10",
        },
        "model_pricing_usd_per_million_tokens": {},
    }
    runner = AgentRunner(api_key="sk-ant-test", config=config)
    session = AsyncMock()

    with patch("regulatory.agent.runner.get_corpus_date_range", return_value=(None, None)):
        with patch("regulatory.agent.runner.load_config", side_effect=RuntimeError("config broken")):
            await runner._ensure_system_prompt(session)  # must not raise

    assert runner._system_prompt is not None
    assert "unknown" in runner._system_prompt
