# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Fixed
- Eval framework no longer treats runtime exceptions as agent responses;
  errors are distinguished from failures and surface explicitly. Preflight
  DB check prevents `eval run --live` from spending against an unhealthy
  backend. Aborts after 3 consecutive errors.

### Added — NL Agent (feat/nl-agent)

**Schema**
- `agent_audit_log` table — append-only audit log for every tool call and response (migration 0007)
- `REVOKE ALL` from PUBLIC + `GRANT INSERT` to `regulatory_agent_writer`, `GRANT SELECT` to `regulatory_ops`

**Agent source** (`src/regulatory/agent/`)
- `models.py` — 8 Pydantic response types (RiskSignalListResponse, RiskSignalDetail, ManufacturerProfile, CountyExposure, DocumentSearchResponse, DocumentDetail, etc.)
- `sanitize.py` — control-char stripping, `<untrusted_content>` wrapping, pagination (8000-char pages)
- `tools.py` — 6 read-only tools: `list_risk_signals`, `get_risk_signal`, `manufacturer_profile`, `county_exposure`, `search_documents`, `get_document`
- `prompts.py` + `templates/system_prompt.md` — static system prompt with runtime substitutions (corpus dates, rule version, tool budget)
- `runner.py` — AgentRunner: API loop, tool dispatch, budget enforcement (tool/token/cost/daily), model fallback, audit writes
- `audit.py` — audit log query helpers for CLI
- `key_handling.py` — ANTHROPIC_API_KEY detection, `getpass` prompt, validation, optional persistence to `~/.config/regulatory/secrets.env` (chmod 600)
- `eval.py` — golden QA eval runner (recorded transcript replay + live mode)

**CLI** (`regulatory agent ...`)
- `regulatory agent ask <question>` — single-turn query
- `regulatory agent chat` — multi-turn interactive REPL
- `regulatory agent audit list/show/cost` — forensic audit access
- `regulatory agent eval run [--live]` — golden QA evaluation

**Config**
- `config/agent.yaml` — models (`claude-sonnet-4-6` primary, `claude-haiku-4-5-20251001` fallback), budgets, pricing table

**Ops**
- `scripts/ops/create_agent_roles.sql` — creates `regulatory_agent_writer` and `regulatory_ops` Postgres roles

**Tests** (`tests/agent/`)
- Layer 1 unit tests: tool functions, sanitization, validation, prompt assembly (44 tests)
- Layer 2 integration: budget enforcement, runner budget paths, cost estimation (18 tests)
- Layer 3 adversarial: 12+ prompt injection attack patterns (structural defence verified)
- Layer 4 security: bandit static check, audit log INSERT-only, tool table restrictions
- Layer 5 API key: 12 tests including sentinel-not-in-logs, cost-confirm guard, non-TTY exit-2
- `tests/agent/golden_qa.yaml` — 40 representative Q&A pairs across 7 categories

**Docs**
- `docs/agent.md`, `docs/agent_security.md`, `docs/agent_costs.md`
- `docs/followup_issues/recall_event_clustering.md` (backfilled from risk-engine closeout)
- `CLAUDE.md` Agent section

**Dependencies**
- `anthropic>=0.40.0` (was `>=0.34.2`)
- `bandit>=1.7.9` (dev; for security static analysis)

---

### Added — Risk Engine (feat/risk-engine)

**Schema**
- `manufacturers.confidence` (float, default 1.0) and `manufacturers.updated_at` (migration 0002)
- `documents.canonical_manufacturer_ids` (uuid[], migration 0002)
- `counties`, `suppliers`, `county_supply` tables (migration 0003)
- `risk_signals` table with partial unique index on `(kind, manufacturer_id, active_ingredient) WHERE status='active'` (migration 0004)
- `risk_signal_events` append-only audit table (migration 0004)
- Conditional `GRANT SELECT` to `regulatory_readonly` role in migration 0004

**Risk rules** (rule version `1.0`, config `config/risk_rules.yaml`)
- Repeat-violator rule: severity-weighted recall count in a 24-month rolling window
- Supply-chain exposure rule: county-level market share for recalled manufacturers
- Cross-source corroboration rule: same ingredient recalled in ≥ 2 jurisdictions

**Manufacturer canonicalization**
- 3-stage algorithm: exact-match → RapidFuzz fuzzy (threshold ≥ 92) → override file
- `src/regulatory/risk/manufacturer_overrides.yaml` — 10 pre-seeded canonical manufacturers
- Low-confidence candidates written to `reconciliation_review.jsonl` for human review

**Synthetic procurement data**
- `scripts/generate_synthetic_procurement.py` — deterministic generator for 47 Kenyan counties, 30 suppliers, 15 EML ingredients
- `src/regulatory/risk/eml_ingredients.py` — single source of truth for EML ingredient list

**CLI additions**
- `regulatory manufacturers reconcile [--dry-run | --apply]`
- `regulatory manufacturers review`
- `regulatory manufacturers show <name>`
- `regulatory procurement load [--csv-dir]`
- `regulatory risk run [--as-of] [--dry-run]`
- `regulatory risk list`
- `regulatory risk show <signal-id>`
- `regulatory risk explain <signal-id>`
- `regulatory risk resolve <signal-id> --reason`
- `regulatory risk suppress <signal-id> --reason`

**Security**
- ruff rule `S608` (hardcoded SQL) enabled — zero violations
- `scripts/ops/create_readonly_role.sql` — ops script for `regulatory_readonly` role
- `scripts/ops/README.md` — runbook for role creation + migration order

**Tests**
- `tests/test_canonicalize.py` — 8 tests for manufacturer normalization and reconciliation
- `tests/test_risk_rules.py` — 17 tests for the three rule modules (pure-function `_sync` variants)
- `tests/test_risk_persistence.py` — 13 tests for idempotence, audit trail, merge semantics, dry-run

**Docs**
- `docs/risk_engine.md`
- `docs/manufacturer_canonicalization.md`
- `docs/synthetic_procurement.md`

---

## [0.2.0] — SAHPRA adapter (feat/adapters-sahpra)

### Added
- SAHPRA recalls + alerts adapter (`src/regulatory/sources/sahpra_recalls.py`)
- SAHPRA recalls are split into two document types: `recall` and `alert`
- `cli.py` fixed so adapters self-register on CLI startup

---

## [0.1.0] — Ingestion framework (feat/ingestion-framework)

### Added
- Normalized schema (`NormalizedDocument`) with Pydantic v2
- Adapter ABC (`RegulatorySource`) with `discover` / `fetch` / `parse` interface
- `@register_source` decorator and scheduler
- `openfda_drug` adapter (REST API)
- `ppb_ke_alerts` adapter (HTML scrape + PDF extraction)
- INN lookup table (`src/regulatory/inn/inn_lookup.json`)
- Alembic migrations 0001 (initial schema)
- CLI via typer (`regulatory ingest`, `regulatory sources list`)
- VCR.py / pytest-recording fixtures for network-free tests
