"""Audit log query helpers for the agent CLI commands.

Provides read access to agent_audit_log for forensic review and cost tracking.
"""

from __future__ import annotations

import uuid as _uuid_mod
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from regulatory.db.models import AgentAuditLog

log = structlog.get_logger(__name__)


async def list_audit_events(
    session: AsyncSession,
    *,
    conversation_id: _uuid_mod.UUID | None = None,
    since: date | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return audit log rows, optionally filtered by conversation and date.

    Args:
        session: Database session.
        conversation_id: Optional UUID to filter to one conversation.
        since: Optional date lower bound on ts.
        limit: Max rows to return.

    Returns:
        List of dicts with id, conversation_id, turn_index, ts, event_type, and
        a truncated payload summary.
    """
    q = select(AgentAuditLog).order_by(AgentAuditLog.ts.desc()).limit(min(limit, 200))
    if conversation_id is not None:
        q = q.where(AgentAuditLog.conversation_id == conversation_id)
    if since is not None:
        since_dt = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
        q = q.where(AgentAuditLog.ts >= since_dt)

    rows = list((await session.execute(q)).scalars().all())
    return [
        {
            "id": str(row.id),
            "conversation_id": str(row.conversation_id),
            "turn_index": row.turn_index,
            "ts": row.ts.isoformat() if row.ts else None,
            "event_type": row.event_type,
            "model_id": row.model_id,
            "tokens_input": row.tokens_input,
            "tokens_output": row.tokens_output,
            "cost_usd_estimate": (
                str(row.cost_usd_estimate) if row.cost_usd_estimate is not None else None
            ),
            "payload_summary": _summarize_payload(row.event_type, row.payload),
        }
        for row in rows
    ]


async def get_audit_event(
    session: AsyncSession, event_id: _uuid_mod.UUID
) -> dict[str, Any] | None:
    """Return a single audit log row by its UUID.

    Args:
        session: Database session.
        event_id: UUID of the audit row.

    Returns:
        Full event dict, or None if not found.
    """
    row = await session.get(AgentAuditLog, event_id)
    if row is None:
        return None
    return {
        "id": str(row.id),
        "conversation_id": str(row.conversation_id),
        "turn_index": row.turn_index,
        "ts": row.ts.isoformat() if row.ts else None,
        "event_type": row.event_type,
        "model_id": row.model_id,
        "tokens_input": row.tokens_input,
        "tokens_output": row.tokens_output,
        "cost_usd_estimate": (
            str(row.cost_usd_estimate) if row.cost_usd_estimate is not None else None
        ),
        "config_version": row.config_version,
        "payload": row.payload,
    }


async def aggregate_cost(
    session: AsyncSession,
    *,
    since: date | None = None,
) -> dict[str, Any]:
    """Return aggregate cost statistics.

    Args:
        session: Database session.
        since: Optional date lower bound.

    Returns:
        Dict with total_cost_usd, total_tokens_input, total_tokens_output,
        conversation_count, and per-model breakdowns.
    """
    q = select(
        func.sum(AgentAuditLog.cost_usd_estimate).label("total_cost"),
        func.sum(AgentAuditLog.tokens_input).label("total_input"),
        func.sum(AgentAuditLog.tokens_output).label("total_output"),
        func.count(func.distinct(AgentAuditLog.conversation_id)).label("conversations"),
    )
    if since is not None:
        since_dt = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
        q = q.where(AgentAuditLog.ts >= since_dt)

    row = (await session.execute(q)).one()

    return {
        "total_cost_usd": str(row.total_cost or Decimal("0")),
        "total_tokens_input": row.total_input or 0,
        "total_tokens_output": row.total_output or 0,
        "conversation_count": row.conversations or 0,
    }


def _summarize_payload(event_type: str, payload: dict[str, Any]) -> str:
    """Return a one-line summary of a payload for list output."""
    if event_type == "user_message":
        text = payload.get("text", "")
        return f"user: {text[:80]!r}"
    if event_type == "tool_call":
        return f"tool: {payload.get('tool_name')} id={payload.get('tool_call_id', '')[:8]}"
    if event_type == "tool_result":
        err = payload.get("error")
        status = f"error={err['message'][:40]!r}" if err else "ok"
        return f"result: {status} elapsed={payload.get('elapsed_ms')}ms"
    if event_type == "agent_response":
        text = payload.get("text", "")
        return f"response: {text[:80]!r}"
    if event_type == "refusal":
        return f"refusal: category={payload.get('category')}"
    if event_type == "error":
        return f"error: stage={payload.get('stage')} msg={payload.get('message', '')[:60]!r}"
    return repr(payload)[:100]
