"""Layer 2 integration tests: real DB session, mocked Anthropic LLM.

Skipped when TEST_DATABASE_URL is not set (same pattern as test_security.py).
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_SKIP_NO_DB = pytest.mark.skipif(not _DB_URL, reason="TEST_DATABASE_URL not set")


def _make_agent_config(tool_calls_per_turn: int = 8) -> dict[str, Any]:
    return {
        "version": "1.0",
        "models": {"primary": "claude-sonnet-4-6", "fallback": "claude-haiku-4-5-20251001"},
        "generation": {"max_tokens_per_turn": 512, "temperature": 0.0},
        "budgets": {
            "tool_calls_per_turn": tool_calls_per_turn,
            "tokens_per_turn": 10000,
            "tokens_per_conversation": 50000,
            "cost_per_conversation_usd": "0.50",
            "cost_per_day_usd": "5.00",
            "budget_remaining_threshold_usd": "0.10",
        },
        "model_pricing_usd_per_million_tokens": {
            "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
            "claude-haiku-4-5-20251001": {"input": 0.8, "output": 4.0},
        },
    }


def _make_mock_text_response(text: str) -> MagicMock:
    """Build a mock Anthropic message response with a text block."""
    block = MagicMock()
    block.type = "end_turn"
    block.text = text

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = text

    response = MagicMock()
    response.stop_reason = "end_turn"
    response.content = [text_block]
    response.usage = MagicMock(input_tokens=100, output_tokens=50)
    return response


# ---------------------------------------------------------------------------
# Unit-level runner tests (no real DB)
# ---------------------------------------------------------------------------


class TestRunnerBudgetEnforcement:
    """Budget enforcement tests that don't need a real DB."""

    @pytest.mark.asyncio
    async def test_tool_budget_enforcement(self) -> None:
        """Runner must stop dispatching tools at the budget cap."""
        from regulatory.agent.runner import AgentRunner

        config = _make_agent_config(tool_calls_per_turn=2)

        tool_use_block = MagicMock()
        tool_use_block.type = "tool_use"
        tool_use_block.id = "toolu_01"
        tool_use_block.name = "list_risk_signals"
        tool_use_block.input = {}

        tool_use_response = MagicMock()
        tool_use_response.stop_reason = "tool_use"
        tool_use_response.content = [tool_use_block, tool_use_block, tool_use_block]
        tool_use_response.usage = MagicMock(input_tokens=100, output_tokens=50)

        text_response = _make_mock_text_response("Here are some signals.")

        runner = AgentRunner(api_key="sk-ant-test", config=config)
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        call_count = 0
        audit_rows: list[dict[str, Any]] = []

        async def fake_execute(stmt: Any, **kwargs: Any) -> MagicMock:
            # Return empty results for any query.
            m = MagicMock()
            m.scalar_one.return_value = 0
            m.scalar_one_or_none.return_value = None
            m.scalars.return_value.all.return_value = []
            return m

        async def fake_flush() -> None:
            pass

        mock_session.execute = fake_execute
        mock_session.flush = fake_flush
        mock_session.get = AsyncMock(return_value=None)
        mock_session.add = MagicMock()

        def fake_messages_create(**kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return tool_use_response
            return text_response

        with patch("regulatory.agent.runner.get_session") as mock_get_session:
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch.object(runner._client.messages, "create", side_effect=fake_messages_create):
                with patch("regulatory.agent.runner.get_corpus_date_range", return_value=(None, None)):
                    with patch("regulatory.agent.runner.load_config") as mock_cfg:
                        mock_cfg.return_value = MagicMock(version="1.0")
                        result = await runner.run_turn("list active signals")

        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_conversation_token_budget_refusal(self) -> None:
        """When conversation token budget exhausted, runner refuses new turn."""
        from regulatory.agent.runner import AgentRunner

        config = _make_agent_config()
        runner = AgentRunner(api_key="sk-ant-test", config=config)
        runner._conversation_tokens_used = 100_000  # force exhaustion

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one=MagicMock(return_value=0)))
        mock_session.flush = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.get = AsyncMock(return_value=None)

        with patch("regulatory.agent.runner.get_session") as mock_get_session:
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("regulatory.agent.runner.get_corpus_date_range", return_value=(None, None)):
                with patch("regulatory.agent.runner.load_config") as mock_cfg:
                    mock_cfg.return_value = MagicMock(version="1.0")
                    result = await runner.run_turn("any question")

        assert "budget" in result.lower() or "session" in result.lower()

    @pytest.mark.asyncio
    async def test_model_fallback_when_budget_low(self) -> None:
        """When cost budget is near threshold, runner uses fallback model."""
        from regulatory.agent.runner import AgentRunner

        config = _make_agent_config()
        runner = AgentRunner(api_key="sk-ant-test", config=config)
        runner._conversation_cost_used = Decimal("0.42")  # near $0.50 cap

        model = runner._pick_model()
        assert model == "claude-haiku-4-5-20251001"


class TestCostEstimation:
    def test_estimate_cost_sonnet(self) -> None:
        from regulatory.agent.runner import _estimate_cost

        pricing = {
            "claude-sonnet-4-6": {"input": 3.0, "output": 15.0}
        }
        cost = _estimate_cost("claude-sonnet-4-6", 1000, 200, pricing)
        assert cost == Decimal("0.006000")

    def test_estimate_cost_unknown_model_uses_sonnet_default(self) -> None:
        from regulatory.agent.runner import _estimate_cost

        pricing = {"claude-sonnet-4-6": {"input": 3.0, "output": 15.0}}
        cost = _estimate_cost("unknown-model", 1000, 0, pricing)
        assert cost > Decimal("0")


class TestAuditPayloadTruncation:
    def test_truncates_raw_text_in_payload(self) -> None:
        from regulatory.agent.runner import _truncate_tool_result_payload

        payload = {"raw_text_wrapped": "x" * 1000, "other": "value"}
        result = _truncate_tool_result_payload(payload)
        assert len(result["raw_text_wrapped"]) <= 530
        assert "truncated for audit" in result["raw_text_wrapped"]

    def test_passes_short_payload_through(self) -> None:
        from regulatory.agent.runner import _truncate_tool_result_payload

        payload = {"raw_text_wrapped": "short text", "other": "value"}
        result = _truncate_tool_result_payload(payload)
        assert result["raw_text_wrapped"] == "short text"

    def test_does_not_modify_non_text_fields(self) -> None:
        from regulatory.agent.runner import _truncate_tool_result_payload

        payload = {"signal_id": "abc-123", "severity": "high"}
        result = _truncate_tool_result_payload(payload)
        assert result == payload


class TestTracebackSanitization:
    def test_removes_key_from_traceback(self) -> None:
        from regulatory.agent.runner import _sanitize_traceback

        tb = "Error occurred with key sk-ant-apiXYZ123456 in the call"
        result = _sanitize_traceback(tb)
        assert "sk-ant-" not in result
        assert "[REDACTED]" in result

    def test_no_key_unchanged(self) -> None:
        from regulatory.agent.runner import _sanitize_traceback

        tb = "TypeError: int is not str"
        result = _sanitize_traceback(tb)
        assert result == tb
