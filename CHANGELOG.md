# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

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
