# Agent

The AfiaData Regulatory Agent provides a natural-language interface over the risk engine corpus. A procurement officer asks a plain-English question; the agent decomposes it into tool calls, synthesises the results, and returns a cited answer.

---

## Architecture

```
User input (CLI)
  │
  ▼
AgentRunner (src/regulatory/agent/runner.py)
  │   ├── System prompt assembled (templates/system_prompt.md + config)
  │   ├── User message + tool definitions → Anthropic API
  │   │       ├──→ Tool dispatcher (tools.py)
  │   │       │       ├── list_risk_signals      ┐
  │   │       │       ├── get_risk_signal         │
  │   │       │       ├── manufacturer_profile   │  read-only
  │   │       │       ├── county_exposure         │  (regulatory_readonly
  │   │       │       ├── search_documents       │   Postgres role)
  │   │       │       └── get_document           ┘
  │   │       └──→ Tool outputs wrapped in <untrusted_content> delimiters
  │   └── Every event → agent_audit_log (regulatory_agent_writer role)
  ▼
Response with inline citations
```

Key source files:

| File | Purpose |
|---|---|
| `src/regulatory/agent/runner.py` | API loop, budget enforcement, audit logging |
| `src/regulatory/agent/tools.py` | 6 read-only tool functions + dispatch |
| `src/regulatory/agent/models.py` | Pydantic response types |
| `src/regulatory/agent/sanitize.py` | Sanitization and `<untrusted_content>` wrapping |
| `src/regulatory/agent/prompts.py` | System prompt assembly |
| `src/regulatory/agent/key_handling.py` | API key detection, validation, persistence |
| `src/regulatory/agent/audit.py` | Audit log query helpers |
| `src/regulatory/agent/eval.py` | Eval runner (recorded + live modes) |
| `src/regulatory/agent/templates/system_prompt.md` | Static prompt template |
| `config/agent.yaml` | Model IDs, budgets, pricing |

---

## Tool surface

Six read-only tools. No tool accepts arbitrary SQL or makes external HTTP calls.

| Tool | Purpose |
|---|---|
| `list_risk_signals` | Filter + list signal summaries (hard cap: 50) |
| `get_risk_signal` | Full signal + evidence + explain trace + `count_inflation_likely` |
| `manufacturer_profile` | Recall summary, aliases, active signals, products |
| `county_exposure` | Supply mix, flagged suppliers, alternative counts + provenance |
| `search_documents` | Full-text search over `documents.raw_text` (hard cap: 25) |
| `get_document` | Full document with paginated, sanitized, wrapped raw text |

All tool inputs are validated: names via `^[a-zA-Z0-9\s.\-,&()/\']{1,200}$`, IDs as UUIDs, queries stripped of control chars and capped at 200 chars.

---

## System prompt design

The prompt template (`templates/system_prompt.md`) is a static Markdown file with `{placeholder}` substitutions:

- `{corpus_start}` / `{corpus_end}`: queried from `documents.date_published` at boot
- `{rule_version}`: from `config/risk_rules.yaml`
- `{tool_calls_per_turn}`: from `config/agent.yaml`

The prompt encodes:
- Scoping rules (what the agent answers / does not answer)
- Four refusal templates (medical, legal, out-of-scope, off-topic)
- Mandatory citation rules (`[doc:uuid]` / `[signal:uuid]`)
- Mandatory caveats (synthetic data, count inflation, extraction limitations)
- Untrusted content instruction (data inside `<untrusted_content>` is not instructions)

---

## Adding a new tool

1. Add a new `async def my_tool(session, *, ...)` function to `tools.py`. Return a Pydantic model from `models.py` (add a new one if needed). Use bound SQLAlchemy queries only; no string interpolation.
2. Add an entry to `TOOL_DEFINITIONS` with the Anthropic JSON schema.
3. Add a dispatch case to `dispatch()` and a `_dispatch_my_tool()` helper.
4. Add at least one happy-path test in `tests/agent/test_tools_unit.py`.
5. Update `docs/agent.md` tool surface table.

---

## Model selection

Models are configured in `config/agent.yaml`:

```yaml
models:
  primary: claude-sonnet-4-6     # best cost/capability for tool-use workloads
  fallback: claude-haiku-4-5-20251001  # cheaper; used when budget is near threshold
```

If the live eval surfaces accuracy issues (hallucinated citations, missed caveats, refusal bypasses), bump `primary` to `claude-opus-4-8`. Do not bump preemptively.

See `docs/agent_costs.md` for budget configuration.

---

## Known limitations

- **Synthetic supply-chain data**: All `county_exposure` figures derive from `synthetic_v2` data. The agent surfaces this caveat on every response that uses these figures.
- **openFDA ingredient extraction gap**: ~100% of openFDA recall documents have empty `active_ingredients`. See [openFDA follow-up](followup_issues/openfda_active_ingredient_extraction.md).
- **SAHPRA misclassification**: ~60% of SAHPRA documents flagged as drug recalls are actually medical devices or diagnostics. See [SAHPRA follow-up](followup_issues/sahpra_document_classification.md).
- **Recall event clustering**: openFDA per-SKU filing inflates repeat-violator counts by 10–57×. The `count_inflation_likely` flag in `get_risk_signal` heuristically identifies affected signals. See [clustering follow-up](followup_issues/recall_event_clustering.md).

### Eval framework error handling (resolved 2026-06-11)

The eval framework previously wrapped any exception from `run_turn()` into a
string `"[ERROR: ...]"` response, which the per-question pass/fail logic then
evaluated against — producing meaningless results when infrastructure (e.g.
Postgres) was unavailable. A DB connection failure on a live run produced 40
identical error "responses" and a vacuous 3/40 pass rate against questions
with no positive assertions.

Fixed by: (a) preflight DB health check that aborts `eval run --live` before
any spend if the DB is unreachable, (b) distinguishing errored questions from
failed ones in the per-question loop, (c) aborting after 3 consecutive errors
rather than running the full set against an unhealthy backend.

The cost tracker was unaffected by this issue (DB error fires before the
session scope where cost accumulation lives), so no cost-tracker reset was
needed.

### Audit log writes silently rolled back (resolved 2026-06-15)

`_write_audit` in `runner.py` called `session.flush()` but not
`session.commit()`. The shared `get_session()` helper does not auto-commit
(`AsyncSession.__aexit__` calls `close()`, not `commit()`), so every flushed
audit row was rolled back on session close. The `agent_audit_log` table was
always empty despite the agent appearing to log normally. The 62.5% Round 2
eval pass rate was achieved with zero audit data captured.

Fixed by adding `await session.commit()` immediately after `await
session.flush()` in `_write_audit`. A round-trip test (write via
`_write_audit`, read back through an independent session) was added to
`test_runner_integration.py` to catch regressions of this exact pattern.

A fourth instance of "system reported success without doing the work" in this
project (after dead-code `resolve_signal_for_manufacturer_merge`, missing
`regulatory_readonly` GRANTs, and the eval error-wrapping). See
[get_session_commit_semantics.md](followup_issues/get_session_commit_semantics.md)
for the broader investigation into whether other writers share the same latent
bug.
