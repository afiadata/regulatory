"""Tests for the golden QA eval runner (eval.py).

Focused on infrastructure-failure handling:
  - Preflight DB check aborts before any spend when DB is unreachable.
  - Per-question loop aborts after 3 consecutive run_turn errors.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_preflight_db_check_raises_when_db_unreachable() -> None:
    """Preflight raises RuntimeError with actionable message when DB refuses connection."""
    from regulatory.agent.eval import _preflight_db_check

    with (
        patch(
            "regulatory.db.session.get_session",
            side_effect=OSError(
                "[WinError 1225] The remote computer refused the network connection"
            ),
        ),
        pytest.raises(RuntimeError, match="Database connection failed"),
    ):
        await _preflight_db_check()


@pytest.mark.asyncio
async def test_preflight_db_check_passes_when_db_reachable() -> None:
    """Preflight completes without error when DB executes SELECT 1."""
    from regulatory.agent.eval import _preflight_db_check

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=None)
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("regulatory.db.session.get_session", return_value=mock_cm):
        await _preflight_db_check()  # should not raise


@pytest.mark.asyncio
async def test_eval_aborts_after_3_consecutive_errors(tmp_path: object) -> None:
    """Live eval aborts after 3 consecutive run_turn errors, not 40."""
    import regulatory.agent.eval as eval_mod
    from regulatory.agent.eval import _MAX_CONSECUTIVE_ERRORS

    fake_questions = [
        {"id": f"q{i:03d}", "category": "test", "user": f"question {i}", "expected": {}}
        for i in range(1, 8)  # 7 questions; should abort at 3
    ]

    mock_runner_instance = MagicMock()
    mock_runner_instance.run_turn = AsyncMock(side_effect=OSError("connection refused"))
    mock_runner_instance._conversation_cost_used = Decimal("0")
    mock_runner_class = MagicMock(return_value=mock_runner_instance)

    fake_config: dict[str, Any] = {
        "models": {
            "primary": "claude-sonnet-4-6",
            "fallback": "claude-haiku-4-5-20251001",
        },
        "budgets": {"cost_per_day_usd": 5.0},
    }

    with (
        patch.object(eval_mod, "_load_golden_qa", return_value=fake_questions),
        patch.object(eval_mod, "_TRANSCRIPTS_DIR", tmp_path),
        patch("regulatory.agent.eval._preflight_db_check", new_callable=AsyncMock),
        patch("regulatory.agent.key_handling.acquire_api_key", return_value="sk-ant-fake"),
        patch("regulatory.agent.runner._load_agent_config", return_value=fake_config),
        patch("builtins.input", return_value="y"),
        patch("regulatory.agent.runner.AgentRunner", mock_runner_class),
    ):
        await eval_mod.run_eval(live=True)

    summary_files = list(tmp_path.glob("*_summary.json"))  # type: ignore[attr-defined]
    assert summary_files, "Summary file should have been written"

    summary = json.loads(summary_files[0].read_text(encoding="utf-8"))

    assert summary["aborted"] is True
    assert summary["errored"] == _MAX_CONSECUTIVE_ERRORS
    assert summary["scored"] == 0
    assert len(summary["results"]) == _MAX_CONSECUTIVE_ERRORS
    assert summary["pass_rate"] == "N/A"


@pytest.mark.asyncio
async def test_eval_resets_consecutive_error_count_on_success(tmp_path: object) -> None:
    """A successful turn resets the consecutive-error counter so the run continues."""
    import regulatory.agent.eval as eval_mod

    fake_questions = [
        {"id": f"q{i:03d}", "category": "test", "user": f"q{i}", "expected": {}}
        for i in range(1, 6)  # 5 questions
    ]

    call_count = 0

    async def alternating_run_turn(_msg: str) -> str:
        nonlocal call_count
        call_count += 1
        # q001 fails, q002 succeeds (resets counter), q003 fails, q004 fails,
        # q005 fails → 3 consecutive after the reset → abort after q005
        if call_count in {1, 3, 4, 5}:
            raise OSError("error")
        return "response text"

    mock_runner_instance = MagicMock()
    mock_runner_instance.run_turn = alternating_run_turn
    mock_runner_instance._conversation_cost_used = Decimal("0")
    mock_runner_class = MagicMock(return_value=mock_runner_instance)

    fake_config: dict[str, Any] = {
        "models": {
            "primary": "claude-sonnet-4-6",
            "fallback": "claude-haiku-4-5-20251001",
        },
        "budgets": {"cost_per_day_usd": 5.0},
    }

    with (
        patch.object(eval_mod, "_load_golden_qa", return_value=fake_questions),
        patch.object(eval_mod, "_TRANSCRIPTS_DIR", tmp_path),
        patch("regulatory.agent.eval._preflight_db_check", new_callable=AsyncMock),
        patch("regulatory.agent.key_handling.acquire_api_key", return_value="sk-ant-fake"),
        patch("regulatory.agent.runner._load_agent_config", return_value=fake_config),
        patch("builtins.input", return_value="y"),
        patch("regulatory.agent.runner.AgentRunner", mock_runner_class),
    ):
        await eval_mod.run_eval(live=True)

    summary_files = list(tmp_path.glob("*_summary.json"))  # type: ignore[attr-defined]
    assert summary_files
    summary = json.loads(summary_files[0].read_text(encoding="utf-8"))

    # q001 errors (consecutive=1), q002 passes (reset), q003 errors (1),
    # q004 errors (2), q005 errors (3) → abort. 4 errors total, 1 pass.
    assert summary["aborted"] is True
    assert summary["errored"] == 4
    assert summary["passed"] == 1
    assert summary["scored"] == 1  # only q002 scored


@pytest.mark.asyncio
async def test_eval_does_not_abort_if_errors_are_not_consecutive(tmp_path: object) -> None:
    """Non-consecutive errors do not trigger the abort threshold."""
    import regulatory.agent.eval as eval_mod

    fake_questions = [
        {"id": f"q{i:03d}", "category": "test", "user": f"q{i}", "expected": {}}
        for i in range(1, 6)  # 5 questions: err, ok, err, ok, err = non-consecutive
    ]

    call_count = 0

    async def alternating_run_turn(_msg: str) -> str:
        nonlocal call_count
        call_count += 1
        if call_count % 2 == 1:  # odd calls error
            raise OSError("error")
        return "response text"

    mock_runner_instance = MagicMock()
    mock_runner_instance.run_turn = alternating_run_turn
    mock_runner_instance._conversation_cost_used = Decimal("0")
    mock_runner_class = MagicMock(return_value=mock_runner_instance)

    fake_config: dict[str, Any] = {
        "models": {
            "primary": "claude-sonnet-4-6",
            "fallback": "claude-haiku-4-5-20251001",
        },
        "budgets": {"cost_per_day_usd": 5.0},
    }

    with (
        patch.object(eval_mod, "_load_golden_qa", return_value=fake_questions),
        patch.object(eval_mod, "_TRANSCRIPTS_DIR", tmp_path),
        patch("regulatory.agent.eval._preflight_db_check", new_callable=AsyncMock),
        patch("regulatory.agent.key_handling.acquire_api_key", return_value="sk-ant-fake"),
        patch("regulatory.agent.runner._load_agent_config", return_value=fake_config),
        patch("builtins.input", return_value="y"),
        patch("regulatory.agent.runner.AgentRunner", mock_runner_class),
    ):
        await eval_mod.run_eval(live=True)

    summary_files = list(tmp_path.glob("*_summary.json"))  # type: ignore[attr-defined]
    assert summary_files
    summary = json.loads(summary_files[0].read_text(encoding="utf-8"))

    # 5 questions completed, not aborted
    assert summary["aborted"] is False
    assert summary["errored"] == 3
    assert summary["scored"] == 2
    assert len(summary["results"]) == 5


# ---------------------------------------------------------------------------
# Null-result citation exemption tests (Fix 2, Round 3)
# ---------------------------------------------------------------------------


class TestNullResultCitationExemption:
    """_check_response must not penalize legitimate null-result responses for missing citations."""

    def _check(self, response: str, must_cite: bool = True) -> tuple[bool, list[str]]:
        from regulatory.agent.eval import _check_response

        return _check_response(response, {"must_cite": must_cite})

    def test_response_with_citation_always_passes(self) -> None:
        passed, _ = self._check("Amoxicillin recall [doc:abc123] found in corpus.")
        assert passed is True

    def test_no_flagged_suppliers_passes_without_citation(self) -> None:
        """q015/q016/q017/q034 pattern: correct answer is 'nothing flagged'."""
        response = (
            "Uasin Gishu County has no flagged suppliers for amoxicillin. "
            "Note: supply-chain figures derive from synthetic procurement data (v2)."
        )
        passed, failures = self._check(response)
        assert passed is True, f"Expected pass, got failures: {failures}"

    def test_zero_percent_flagged_passes_without_citation(self) -> None:
        """q034 pattern: 0% from flagged manufacturers."""
        response = "0%  of amoxicillin in Nakuru comes from flagged manufacturers."
        passed, failures = self._check(response)
        assert passed is True, f"Expected pass, got failures: {failures}"

    def test_no_results_in_corpus_passes_without_citation(self) -> None:
        """q038 pattern: corpus boundary with no matching documents."""
        response = (
            "My corpus returned no results for metformin recalls in 2024. "
            "European recall data is not available here."
        )
        passed, failures = self._check(response)
        assert passed is True, f"Expected pass, got failures: {failures}"

    def test_response_with_data_but_no_citation_still_fails(self) -> None:
        """A response that found documents but omitted citations is still a real failure."""
        response = (
            "Glenmark Pharmaceuticals had 95 enforcement actions in 2025. "
            "The repeat-violator signal is active."
        )
        passed, failures = self._check(response)
        assert passed is False
        assert "missing citations" in failures

    def test_must_cite_false_always_passes(self) -> None:
        """When must_cite is False, no citation check is run."""
        passed, _ = self._check("Some response with no citations.", must_cite=False)
        assert passed is True

    def test_truncated_response_is_not_exempt(self) -> None:
        """'Response truncated due to budget limits.' is a real failure, not a null result."""
        passed, failures = self._check("Response truncated due to budget limits.")
        assert passed is False
        assert "missing citations" in failures


# ---------------------------------------------------------------------------
# Budget regression and cost-tracking tests (Fix 1 and Fix 5, Round 3)
# ---------------------------------------------------------------------------


def test_configured_token_budget_sufficient_for_heavy_queries() -> None:
    """tokens_per_turn must be ≥ 20000 — regression test for q009/q011/q032 truncation.

    Glenmark's 95-doc history + active signals requires rendering a long profile
    summary. At 15K the response was always truncated; at 20K it clears.
    """
    from pathlib import Path

    import yaml

    cfg_path = Path(__file__).parents[2] / "config" / "agent.yaml"
    with cfg_path.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    tokens = cfg["budgets"]["tokens_per_turn"]
    assert tokens >= 20000, (
        f"tokens_per_turn={tokens} is too low; Glenmark profile queries truncated at 15K. "
        "Raise to ≥20000 to prevent regression."
    )


@pytest.mark.asyncio
async def test_eval_summary_includes_total_cost_usd(tmp_path: object) -> None:
    """Live eval summary JSON must include total_cost_usd field."""
    import regulatory.agent.eval as eval_mod

    fake_questions = [
        {"id": "q001", "category": "test", "user": "question 1", "expected": {}},
        {"id": "q002", "category": "test", "user": "question 2", "expected": {}},
    ]

    mock_runner_instance = MagicMock()
    mock_runner_instance.run_turn = AsyncMock(return_value="no results for anything")
    mock_runner_instance._conversation_cost_used = Decimal("0.025000")
    mock_runner_class = MagicMock(return_value=mock_runner_instance)

    fake_config: dict[str, Any] = {
        "models": {"primary": "claude-sonnet-4-6", "fallback": "claude-haiku-4-5-20251001"},
        "budgets": {"cost_per_day_usd": 5.0},
    }

    with (
        patch.object(eval_mod, "_load_golden_qa", return_value=fake_questions),
        patch.object(eval_mod, "_TRANSCRIPTS_DIR", tmp_path),
        patch("regulatory.agent.eval._preflight_db_check", new_callable=AsyncMock),
        patch("regulatory.agent.key_handling.acquire_api_key", return_value="sk-ant-fake"),
        patch("regulatory.agent.runner._load_agent_config", return_value=fake_config),
        patch("builtins.input", return_value="y"),
        patch("regulatory.agent.runner.AgentRunner", mock_runner_class),
    ):
        await eval_mod.run_eval(live=True)

    summary_files = list(tmp_path.glob("*_summary.json"))  # type: ignore[attr-defined]
    assert summary_files
    summary = json.loads(summary_files[0].read_text(encoding="utf-8"))

    assert "total_cost_usd" in summary, "Summary must include total_cost_usd"
    assert summary["total_cost_usd"] == pytest.approx(0.05, abs=1e-4), (
        f"Expected ~$0.05 (2 questions × $0.025), got {summary['total_cost_usd']}"
    )
