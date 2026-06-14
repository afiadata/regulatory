"""Layer 5 API key handling tests (Section 9.5.5-12 of PR brief).

All tests mock the Anthropic client so no real API calls are made.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _unset_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ANTHROPIC_API_KEY is absent from the environment."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Test 5: Missing key prompts in interactive mode
# ---------------------------------------------------------------------------


def test_missing_key_prompts_in_interactive_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """In TTY mode with no key, acquire_api_key calls getpass and validates."""
    _unset_key(monkeypatch)

    fake_key = "sk-ant-test-INTERACTIVE12345"
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    with (
        patch("regulatory.agent.key_handling.getpass.getpass", return_value=fake_key) as mock_gp,
        patch("regulatory.agent.key_handling._validate_key", return_value=True) as mock_val,
        patch("builtins.input", return_value="n"),
    ):
        from regulatory.agent.key_handling import acquire_api_key

        result = acquire_api_key(fallback_model="claude-haiku-4-5-20251001")

    assert result == fake_key
    mock_gp.assert_called_once()
    mock_val.assert_called_once_with(fake_key, "claude-haiku-4-5-20251001")
    assert os.environ.get("ANTHROPIC_API_KEY") == fake_key
    # Cleanup.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Test 6: Non-interactive missing key exits with code 2
# ---------------------------------------------------------------------------


def test_missing_key_fails_cleanly_non_interactive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no TTY and no key, acquire_api_key exits with code 2."""
    _unset_key(monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    from regulatory.agent.key_handling import acquire_api_key

    with pytest.raises(SystemExit) as exc_info:
        acquire_api_key(fallback_model="claude-haiku-4-5-20251001")

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY" in captured.err
    assert "not a TTY" in captured.err or "stdin is not a TTY" in captured.err


# ---------------------------------------------------------------------------
# Test 7: Invalid key re-prompts then exits
# ---------------------------------------------------------------------------


def test_invalid_key_reprompts_then_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three failed validations must exit with code 2."""
    _unset_key(monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    call_count = 0

    def fake_validate(key: str, model: str) -> bool:
        nonlocal call_count
        call_count += 1
        return False

    with (
        patch("regulatory.agent.key_handling._validate_key", side_effect=fake_validate),
        patch(
            "regulatory.agent.key_handling.getpass.getpass",
            return_value="sk-ant-test-BADKEY",
        ),
        patch("builtins.input", return_value="n"),
    ):
        from regulatory.agent.key_handling import acquire_api_key

        with pytest.raises(SystemExit) as exc_info:
            acquire_api_key(fallback_model="claude-haiku-4-5-20251001")

    assert exc_info.value.code == 2
    assert call_count == 3


# ---------------------------------------------------------------------------
# Test 8: Key never appears in audit log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_never_appears_in_audit_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """API key sentinel must not appear in any audit log row payloads."""
    sentinel_key = "sk-ant-test-SENTINEL12345ABCDEF"
    monkeypatch.setenv("ANTHROPIC_API_KEY", sentinel_key)

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

    audit_rows: list[dict] = []

    async def fake_execute(stmt, **kwargs):
        m = MagicMock()
        m.scalar_one.return_value = 0
        m.one.return_value = MagicMock(start=None, end=None)
        return m

    mock_session = AsyncMock()
    mock_session.execute = fake_execute
    mock_session.flush = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)

    def fake_add(row):
        if hasattr(row, "payload"):
            audit_rows.append(row.payload)

    mock_session.add = fake_add

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "Here is the answer."
    mock_response = MagicMock()
    mock_response.stop_reason = "end_turn"
    mock_response.content = [text_block]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)

    runner = AgentRunner(api_key=sentinel_key, config=config)

    with (
        patch("regulatory.agent.runner.get_session") as mock_get_session,
        patch.object(runner._client.messages, "create", return_value=mock_response),
        patch("regulatory.agent.runner.get_corpus_date_range", return_value=(None, None)),
        patch("regulatory.agent.runner.load_config") as mock_cfg,
    ):
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_cfg.return_value = MagicMock(version="1.0")
        await runner.run_turn("list signals")

    # Verify sentinel does not appear in any audit row payload.
    for row_payload in audit_rows:
        payload_str = str(row_payload)
        assert "SENTINEL12345ABCDEF" not in payload_str, (
            f"API key sentinel found in audit row payload: {payload_str[:200]}"
        )

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Test 9: Key never appears in application logs
# ---------------------------------------------------------------------------


def test_key_never_appears_in_application_logs(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API key sentinel must not appear in any log record."""
    sentinel_key = "sk-ant-test-SENTINEL12345ABCDEF"
    monkeypatch.setenv("ANTHROPIC_API_KEY", sentinel_key)

    import logging

    from regulatory.agent.key_handling import install_scrub_filter

    install_scrub_filter(sentinel_key)

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("regulatory").info("processing request with key=%s", sentinel_key)

    for record in caplog.records:
        assert "SENTINEL12345ABCDEF" not in record.getMessage(), (
            f"API key sentinel found in log record: {record.getMessage()}"
        )

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Test 10: Key never appears in traceback
# ---------------------------------------------------------------------------


def test_key_never_appears_in_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Traceback sanitizer must remove key-shaped substrings."""
    from regulatory.agent.runner import _sanitize_traceback

    sentinel = "sk-ant-test-SENTINEL12345ABCDEF"
    tb = (
        f"Traceback (most recent call last):\n"
        f"  File 'runner.py', line 100, in _run_turn\n"
        f"    client = anthropic.Anthropic(api_key={sentinel!r})\n"
        f"AuthenticationError: Invalid key."
    )
    result = _sanitize_traceback(tb)
    assert "SENTINEL12345ABCDEF" not in result
    assert "[REDACTED]" in result


# ---------------------------------------------------------------------------
# Test 11: eval run --live confirms cost before running
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_live_confirms_cost_before_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """eval run --live must ask for confirmation; if N, no LLM calls are made."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-FAKE12345")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    api_call_made = False

    async def fake_run_turn(self, question: str) -> str:
        nonlocal api_call_made
        api_call_made = True
        return "response"

    with (
        patch("regulatory.agent.eval._preflight_db_check", new_callable=AsyncMock),
        patch("regulatory.agent.eval._load_golden_qa", return_value=[]),
        patch(
            "regulatory.agent.key_handling.acquire_api_key",
            return_value="sk-ant-test-FAKE",
        ),
        patch("builtins.input", return_value="n"),
    ):
        from regulatory.agent.eval import run_eval

        await run_eval(live=True)

    # User said N; no API calls should have been made.
    assert not api_call_made


# ---------------------------------------------------------------------------
# Test 12: eval recorded mode does not touch key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_recorded_mode_does_not_touch_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recorded eval mode must not attempt to acquire an API key."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    key_acquired = False

    def fake_acquire(*args, **kwargs) -> str:
        nonlocal key_acquired
        key_acquired = True
        return "sk-ant-fake"

    with (
        patch("regulatory.agent.key_handling.acquire_api_key", side_effect=fake_acquire),
        patch("regulatory.agent.eval._find_latest_transcript", return_value=None),
        patch("regulatory.agent.eval._load_golden_qa", return_value=[]),
    ):
        from regulatory.agent.eval import run_eval

        await run_eval(live=False)

    assert not key_acquired, "Recorded eval mode must not acquire an API key"
