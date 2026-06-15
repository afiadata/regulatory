"""AgentRunner: Anthropic API call loop with tool dispatch, budget enforcement,
and append-only audit logging.

Architecture:
  - run_turn() drives one user-message → response cycle
  - Tool calls are dispatched via regulatory.agent.tools.dispatch()
  - Every event (user_message, tool_call, tool_result, agent_response,
    refusal, error) is written to agent_audit_log
  - Budget limits (tool calls, tokens, cost, daily) are enforced before
    each LLM call and after each tool batch
"""

from __future__ import annotations

import json
import time
import traceback
import uuid as _uuid_mod
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import anthropic
import structlog
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from regulatory.agent import tools as agent_tools
from regulatory.agent.prompts import assemble_system_prompt, get_corpus_date_range
from regulatory.db.models import AgentAuditLog
from regulatory.db.session import get_session
from regulatory.risk.config import load_config

log = structlog.get_logger(__name__)

_CONFIG_PATH = Path(__file__).parents[3] / "config" / "agent.yaml"

# Audit log payload size limits.
_AUDIT_DOC_TEXT_SNIPPET_CHARS = 500


def _load_agent_config() -> dict[str, Any]:
    """Load config/agent.yaml."""
    with _CONFIG_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)  # type: ignore[no-any-return]


def _estimate_cost(
    model_id: str,
    tokens_input: int,
    tokens_output: int,
    pricing: dict[str, Any],
) -> Decimal:
    """Compute cost estimate from token counts and price table.

    Args:
        model_id: The model used (e.g. "claude-sonnet-4-6").
        tokens_input: Input token count.
        tokens_output: Output token count.
        pricing: Dict mapping model_id to {"input": ..., "output": ...} per M tokens.

    Returns:
        Estimated cost in USD as a Decimal.
    """
    rates = pricing.get(model_id, pricing.get("claude-sonnet-4-6", {"input": 3.0, "output": 15.0}))
    cost = Decimal(str(tokens_input)) * Decimal(str(rates["input"])) / Decimal("1000000")
    cost += Decimal(str(tokens_output)) * Decimal(str(rates["output"])) / Decimal("1000000")
    return cost.quantize(Decimal("0.000001"))


def _truncate_tool_result_payload(result_dict: dict[str, Any]) -> dict[str, Any]:
    """Cap raw_text_wrapped in tool results to _AUDIT_DOC_TEXT_SNIPPET_CHARS.

    The audit log records what tool was called and what document was returned,
    but not the full document text (which duplicates data already in `documents`).

    Args:
        result_dict: Serialised tool result dict.

    Returns:
        A copy with long text fields truncated.
    """
    out = dict(result_dict)
    wrapped = out.get("raw_text_wrapped")
    if isinstance(wrapped, str) and len(wrapped) > _AUDIT_DOC_TEXT_SNIPPET_CHARS:
        out["raw_text_wrapped"] = wrapped[:_AUDIT_DOC_TEXT_SNIPPET_CHARS] + "…[truncated for audit]"
    return out


class BudgetExhaustedError(Exception):
    """Raised when a per-turn or per-conversation budget is exhausted."""


class AgentRunner:
    """Drives one conversation with the Anthropic API including tool calls and audit logging.

    Args:
        api_key: Validated Anthropic API key.
        config: agent.yaml contents.
        conversation_id: UUID grouping all turns in this chat session.
    """

    def __init__(
        self,
        api_key: str,
        config: dict[str, Any] | None = None,
        conversation_id: _uuid_mod.UUID | None = None,
    ) -> None:
        self._config = config or _load_agent_config()
        self._client = anthropic.Anthropic(api_key=api_key)
        self._conversation_id = conversation_id or _uuid_mod.uuid4()
        self._turn_index = 0

        models = self._config.get("models", {})
        self._primary_model: str = models.get("primary", "claude-sonnet-4-6")
        self._fallback_model: str = models.get("fallback", "claude-haiku-4-5-20251001")

        gen = self._config.get("generation", {})
        self._max_tokens: int = int(gen.get("max_tokens_per_turn", 4096))
        self._temperature: float = float(gen.get("temperature", 0.0))

        budgets = self._config.get("budgets", {})
        self._tool_calls_per_turn: int = int(budgets.get("tool_calls_per_turn", 8))
        self._tokens_per_turn: int = int(budgets.get("tokens_per_turn", 10000))
        self._tokens_per_conversation: int = int(budgets.get("tokens_per_conversation", 50000))
        self._cost_per_conversation: Decimal = Decimal(
            str(budgets.get("cost_per_conversation_usd", "0.50"))
        )
        self._cost_per_day: Decimal = Decimal(str(budgets.get("cost_per_day_usd", "5.00")))
        self._fallback_threshold: Decimal = Decimal(
            str(budgets.get("budget_remaining_threshold_usd", "0.10"))
        )

        self._pricing: dict[str, Any] = self._config.get(
            "model_pricing_usd_per_million_tokens", {}
        )
        self._config_version: str = str(self._config.get("version", "1.0"))

        # Running totals for this conversation.
        self._conversation_tokens_used: int = 0
        self._conversation_cost_used: Decimal = Decimal("0")

        # Assembled system prompt (set on first turn).
        self._system_prompt: str | None = None

    @property
    def conversation_id(self) -> _uuid_mod.UUID:
        """UUID grouping all turns in this session."""
        return self._conversation_id

    async def _ensure_system_prompt(self, session: AsyncSession) -> None:
        """Assemble system prompt if not yet done for this session."""
        if self._system_prompt is not None:
            return
        corpus_start, corpus_end = await get_corpus_date_range(session)
        try:
            risk_config = load_config()
            rule_version = risk_config.version
        except Exception:
            rule_version = "unknown"

        self._system_prompt = assemble_system_prompt(
            corpus_start=corpus_start,
            corpus_end=corpus_end,
            rule_version=rule_version,
            tool_calls_per_turn=self._tool_calls_per_turn,
        )

    def _pick_model(self) -> str:
        """Return primary model, or fallback if conversation cost is near the threshold."""
        remaining = self._cost_per_conversation - self._conversation_cost_used
        if remaining <= self._fallback_threshold:
            return self._fallback_model
        return self._primary_model

    async def _write_audit(
        self,
        session: AsyncSession,
        event_type: str,
        payload: dict[str, Any],
        model_id: str | None = None,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        cost_usd: Decimal | None = None,
    ) -> None:
        """Append one row to agent_audit_log.

        DB write errors are caught and logged independently so they do not
        shadow the underlying operation that triggered the event.

        Args:
            session: Database session (should use regulatory_agent_writer role in prod).
            event_type: One of the defined event_type values.
            payload: JSONB event data.
            model_id: Model used this turn (if applicable).
            tokens_input: Input tokens billed (if applicable).
            tokens_output: Output tokens billed (if applicable).
            cost_usd: Estimated cost (if applicable).
        """
        try:
            row = AgentAuditLog(
                conversation_id=self._conversation_id,
                turn_index=self._turn_index,
                event_type=event_type,
                payload=payload,
                model_id=model_id,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                cost_usd_estimate=cost_usd,
                config_version=self._config_version,
            )
            session.add(row)
            await session.flush()
            await session.commit()
        except Exception as audit_exc:
            log.error("audit_write_failed", event_type=event_type, error=str(audit_exc))

    def _tool_result_to_dict(self, result: Any) -> dict[str, Any]:
        """Serialise a Pydantic tool result to a JSON-safe dict."""
        if hasattr(result, "model_dump"):
            raw = result.model_dump(mode="json")
        else:
            raw = {"value": str(result)}
        return _truncate_tool_result_payload(raw)

    async def run_turn(self, user_message: str) -> str:
        """Run one user-message → agent-response cycle.

        Args:
            user_message: The user's plain-text message.

        Returns:
            The agent's text response.
        """
        async with get_session() as session:
            return await self._run_turn_with_session(session, user_message)

    async def _run_turn_with_session(self, session: AsyncSession, user_message: str) -> str:
        """Inner turn logic with an injected session (enables testing)."""
        await self._ensure_system_prompt(session)
        assert self._system_prompt is not None

        # Log user message.
        await self._write_audit(session, "user_message", {"text": user_message})

        # Check per-conversation budget.
        if self._conversation_tokens_used >= self._tokens_per_conversation:
            msg = (
                "This conversation has reached its token budget. "
                "Please start a new session."
            )
            await self._write_audit(
                session, "refusal", {"category": "budget_exhausted", "text": msg}
            )
            return msg
        if self._conversation_cost_used >= self._cost_per_conversation:
            msg = (
                f"This conversation has reached its cost budget "
                f"(${self._cost_per_conversation:.2f}). Please start a new session."
            )
            await self._write_audit(
                session, "refusal", {"category": "budget_exhausted", "text": msg}
            )
            return msg

        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
        tool_calls_used = 0
        turn_tokens_used = 0
        turn_cost_used = Decimal("0")
        model_id = self._pick_model()

        try:
            while True:
                if turn_tokens_used >= self._tokens_per_turn:
                    truncation_note = (
                        " [Token budget for this turn exhausted; response may be incomplete.]"
                    )
                    break

                response = self._client.messages.create(
                    model=model_id,
                    max_tokens=self._max_tokens,
                    temperature=self._temperature,
                    system=self._system_prompt,
                    tools=agent_tools.TOOL_DEFINITIONS,  # type: ignore[arg-type]
                    messages=cast(Any, messages),
                )

                usage = response.usage
                t_in = usage.input_tokens
                t_out = usage.output_tokens
                turn_tokens_used += t_in + t_out
                cost = _estimate_cost(model_id, t_in, t_out, self._pricing)
                turn_cost_used += cost

                if response.stop_reason == "end_turn":
                    text_response = _extract_text(response)
                    self._conversation_tokens_used += turn_tokens_used
                    self._conversation_cost_used += turn_cost_used
                    await self._write_audit(
                        session,
                        "agent_response",
                        {"text": text_response, "citations": _extract_citations(text_response)},
                        model_id=model_id,
                        tokens_input=t_in,
                        tokens_output=t_out,
                        cost_usd=cost,
                    )
                    self._turn_index += 1
                    return text_response

                if response.stop_reason != "tool_use":
                    text_response = _extract_text(response)
                    self._conversation_tokens_used += turn_tokens_used
                    self._conversation_cost_used += turn_cost_used
                    await self._write_audit(
                        session,
                        "agent_response",
                        {"text": text_response, "stop_reason": response.stop_reason},
                        model_id=model_id,
                        tokens_input=t_in,
                        tokens_output=t_out,
                        cost_usd=cost,
                    )
                    self._turn_index += 1
                    return text_response

                # Process tool use blocks.
                tool_result_contents: list[dict[str, Any]] = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue

                    if tool_calls_used >= self._tool_calls_per_turn:
                        budget_msg = (
                            f"[Tool budget of {self._tool_calls_per_turn} calls exhausted "
                            f"for this turn. Stopping tool dispatch.]"
                        )
                        tool_result_contents.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": budget_msg,
                                "is_error": True,
                            }
                        )
                        log.warning(
                            "tool_budget_exhausted",
                            tool_calls_used=tool_calls_used,
                            limit=self._tool_calls_per_turn,
                        )
                        await self._write_audit(
                            session,
                            "error",
                            {
                                "stage": "tool_dispatch",
                                "message": "tool_calls_per_turn budget exhausted",
                                "tool_name": block.name,
                                "tool_call_id": block.id,
                            },
                        )
                        continue

                    tool_calls_used += 1
                    tool_input: dict[str, Any] = dict(cast(Any, block.input))

                    # Log tool call.
                    await self._write_audit(
                        session,
                        "tool_call",
                        {
                            "tool_name": block.name,
                            "inputs": tool_input,
                            "tool_call_id": block.id,
                        },
                    )

                    t_start = time.monotonic()
                    tool_error: dict[str, Any] | None = None
                    tool_output: dict[str, Any] = {}
                    try:
                        result = await agent_tools.dispatch(session, block.name, tool_input)
                        tool_output = self._tool_result_to_dict(result)
                    except ValueError as exc:
                        tool_error = {"type": "ValueError", "message": str(exc)}
                        log.warning(
                            "tool_dispatch_error",
                            tool_name=block.name,
                            error=str(exc),
                        )
                    except Exception as exc:
                        tool_error = {"type": type(exc).__name__, "message": str(exc)}
                        log.error(
                            "tool_dispatch_unexpected_error",
                            tool_name=block.name,
                            error=str(exc),
                        )

                    elapsed_ms = int((time.monotonic() - t_start) * 1000)

                    # Log tool result.
                    await self._write_audit(
                        session,
                        "tool_result",
                        {
                            "tool_call_id": block.id,
                            "outputs": tool_output,
                            "elapsed_ms": elapsed_ms,
                            "error": tool_error,
                        },
                    )

                    if tool_error is not None:
                        tool_result_contents.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"Error: {tool_error['message']}",
                                "is_error": True,
                            }
                        )
                    else:
                        tool_result_contents.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(tool_output, default=str),
                            }
                        )

                # Append assistant turn and tool results to message history.
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_result_contents})

        except anthropic.AuthenticationError as exc:
            sanitized = (
                f"API authentication failed. Check your ANTHROPIC_API_KEY. "
                f"({type(exc).__name__})"
            )
            await self._write_audit(
                session,
                "error",
                {"stage": "llm_call", "message": sanitized, "traceback": ""},
            )
            self._turn_index += 1
            return f"I'm unable to respond right now: {sanitized}"
        except Exception as exc:
            tb = traceback.format_exc()
            sanitized_tb = _sanitize_traceback(tb)
            await self._write_audit(
                session,
                "error",
                {"stage": "llm_call", "message": str(exc), "traceback": sanitized_tb},
            )
            log.error("runner_unexpected_error", error=str(exc))
            self._turn_index += 1
            return "An unexpected error occurred. Please try again."

        self._conversation_tokens_used += turn_tokens_used
        self._conversation_cost_used += turn_cost_used
        final_text = "Response truncated due to budget limits."
        await self._write_audit(
            session,
            "agent_response",
            {"text": final_text + (truncation_note if "truncation_note" in dir() else "")},
            model_id=model_id,
        )
        self._turn_index += 1
        return final_text


def _extract_text(response: anthropic.types.Message) -> str:
    """Extract concatenated text from an Anthropic response."""
    parts = []
    for block in response.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "\n".join(parts)


def _extract_citations(text: str) -> list[str]:
    """Extract [doc:uuid] and [signal:uuid] citation strings from response text."""
    import re

    return re.findall(r"\[(?:doc|signal):[^\]]+\]", text)


def _sanitize_traceback(tb: str) -> str:
    """Remove any API key patterns from a traceback string."""
    import re

    return re.sub(r"sk-ant-[\w\-]+", "[REDACTED]", tb)
