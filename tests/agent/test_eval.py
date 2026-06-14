"""Tests for the golden QA eval runner (eval.py).

Focused on infrastructure-failure handling:
  - Preflight DB check aborts before any spend when DB is unreachable.
  - Per-question loop aborts after 3 consecutive run_turn errors.
"""

from __future__ import annotations

import json
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
