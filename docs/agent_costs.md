# Agent Costs

---

## Configuration (`config/agent.yaml`)

```yaml
version: "1.0"

models:
  primary: claude-sonnet-4-6
  fallback: claude-haiku-4-5-20251001

generation:
  max_tokens_per_turn: 4096
  temperature: 0.0

budgets:
  tool_calls_per_turn: 8       # hard cap per agent turn
  tokens_per_turn: 10000       # input + output combined
  tokens_per_conversation: 50000
  cost_per_conversation_usd: 0.50
  cost_per_day_usd: 5.0        # suitable for dev; raise for demo/prod
  budget_remaining_threshold_usd: 0.10  # below this, switch to fallback

model_pricing_usd_per_million_tokens:
  claude-sonnet-4-6:
    input: 3.00
    output: 15.00
  claude-haiku-4-5-20251001:
    input: 0.80
    output: 4.00
```

Bump `version` whenever the prompt template or budget structure changes. The version is stored in every audit row so historical conversations can be re-interpreted.

---

## Per-turn budget

- **Tool calls**: enforced at exactly `tool_calls_per_turn` (default 8). When exhausted, the runner returns the partial response with a note. The system prompt informs the model of this limit.
- **Tokens**: enforced at `tokens_per_turn`. When exhausted, the runner terminates the tool loop and returns the partial response.

Neither limit raises an exception; both degrade gracefully with a user-visible note.

---

## Per-conversation budget

Default: 50 000 tokens or $0.50, whichever exhausts first.

When either limit is reached, the runner refuses new turns with a "start a new session" message.

---

## Daily cap

Default: $5.00 across all conversations. Suitable for development. Demo deployments may raise this; production caps are a deploy-time decision.

When the daily cap is hit, the agent declines all new turns until the next UTC day.

---

## Model fallback

When the remaining conversation cost drops below `budget_remaining_threshold_usd` ($0.10), the runner switches to the fallback model (`claude-haiku-4-5-20251001`) for the remainder of that conversation. The transition is recorded in the audit log.

---

## Cost tracking

Every audit row records `model_id`, `tokens_input`, `tokens_output`, and `cost_usd_estimate`. Aggregate cost:

```bash
regulatory agent audit cost --since 2026-06-01
```

The pricing table in `agent.yaml` drives the estimate. Update it when Anthropic publishes new pricing.

---

## Eval cost estimate

A full 40-question live eval (`regulatory agent eval run --live`) costs approximately:
- ~$0.72 at Sonnet 4.6 rates (assuming ~600 input + 200 output tokens per question)

The eval runner shows the estimate and requires explicit `y` confirmation before spending begins.

---

## Tuning guidance

| Scenario | Action |
|---|---|
| Accuracy issues in live eval (missed caveats, hallucinated citations) | Bump `primary` to `claude-opus-4-8` in `agent.yaml` |
| Cost too high for demo | Lower `cost_per_conversation_usd` or raise `budget_remaining_threshold_usd` to force earlier fallback |
| Tool loop too chatty | Lower `tool_calls_per_turn` (minimum 3 to handle most queries) |
| Context blowout on large document | Reduce `get_document` page size in `sanitize._RAW_TEXT_PAGE_CHARS` |
