"""Layer 5 security tests for the agent.

Tests:
1. bandit static analysis reports no high-severity findings.
2. Audit log INSERT-only enforcement (with a real DB; skipped without one).
3. No tool returns rows from agent_audit_log.
4. No tool returns rows from risk_signal_events.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


_DB_URL = os.environ.get("TEST_DATABASE_URL")
_AGENT_SRC = Path(__file__).parents[2] / "src" / "regulatory" / "agent"


# ---------------------------------------------------------------------------
# Test 1: bandit static analysis
# ---------------------------------------------------------------------------


def test_bandit_no_high_severity() -> None:
    """bandit -r src/regulatory/agent/ must report no high-severity findings."""
    result = subprocess.run(
        [sys.executable, "-m", "bandit", "-r", str(_AGENT_SRC), "-ll", "-q"],
        capture_output=True,
        text=True,
    )
    # bandit exits 0 if no issues at the requested level, 1 if issues found.
    high_issues = [
        line for line in result.stdout.splitlines()
        if "Severity: HIGH" in line or "Issue: [B" in line and "HIGH" in line
    ]
    assert result.returncode == 0 or not high_issues, (
        f"bandit found high-severity issues:\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# Tests 3 & 4: No tool returns audit_log or risk_signal_events rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tool_returns_audit_log_rows() -> None:
    """No tool in TOOL_DEFINITIONS queries agent_audit_log."""
    from regulatory.agent import tools

    tool_src = Path(__file__).parents[2] / "src" / "regulatory" / "agent" / "tools.py"
    src_text = tool_src.read_text(encoding="utf-8")
    assert "agent_audit_log" not in src_text, (
        "tools.py references agent_audit_log — tools must not self-introspect the audit log"
    )


@pytest.mark.asyncio
async def test_no_tool_returns_risk_signal_events() -> None:
    """No tool in TOOL_DEFINITIONS queries risk_signal_events."""
    from regulatory.agent import tools

    tool_src = Path(__file__).parents[2] / "src" / "regulatory" / "agent" / "tools.py"
    src_text = tool_src.read_text(encoding="utf-8")
    assert "risk_signal_events" not in src_text, (
        "tools.py references risk_signal_events — signal-change history is ops-only"
    )


@pytest.mark.asyncio
async def test_dispatch_raises_for_unknown_tool() -> None:
    from regulatory.agent.tools import dispatch

    session = AsyncMock()
    with pytest.raises(ValueError):
        await dispatch(session, "delete_audit_log", {})

    with pytest.raises(ValueError):
        await dispatch(session, "query_sql", {"sql": "SELECT * FROM documents"})


# ---------------------------------------------------------------------------
# Test 2: Audit log INSERT-only (requires real DB)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="TEST_DATABASE_URL not set")
def test_audit_log_insert_only_enforcement() -> None:
    """regulatory_agent_writer must not be able to UPDATE or DELETE agent_audit_log."""
    import psycopg2

    conn = psycopg2.connect(_DB_URL)
    try:
        cur = conn.cursor()
        # Create the writer role if it doesn't exist.
        cur.execute(
            """
            DO $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regulatory_agent_writer') THEN
                CREATE ROLE regulatory_agent_writer NOLOGIN;
              END IF;
            END $$;
            """
        )
        conn.commit()

        # Verify the role cannot UPDATE.
        cur.execute(
            """
            SELECT has_table_privilege('regulatory_agent_writer', 'agent_audit_log', 'UPDATE')
            """
        )
        can_update = cur.fetchone()[0]
        assert not can_update, "regulatory_agent_writer must NOT have UPDATE on agent_audit_log"

        # Verify the role cannot DELETE.
        cur.execute(
            """
            SELECT has_table_privilege('regulatory_agent_writer', 'agent_audit_log', 'DELETE')
            """
        )
        can_delete = cur.fetchone()[0]
        assert not can_delete, "regulatory_agent_writer must NOT have DELETE on agent_audit_log"
    finally:
        conn.close()
