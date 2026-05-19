# Risk Engine

The risk engine turns normalized `documents` rows into actionable `risk_signals` that
procurement officers and regulatory affairs teams can query.  It runs as a deterministic,
replay-safe pipeline: re-running with the same `--as-of` date always produces the same output.

---

## Architecture

```
Documents table
    ↓
[repeat_violator.py]   → list[RiskSignalCandidate]
[supply_chain.py]      → list[RiskSignalCandidate]
[corroboration.py]     → list[RiskSignalCandidate]
    ↓
[persist.py] persist_signals()
    ↓
risk_signals table  +  risk_signal_events table (append-only audit)
```

---

## Rules

### 1. Repeat Violator (`repeat_violator.py`)

Detects manufacturers with multiple recall/alert/enforcement events in a rolling window.

**Window**: `as_of - window_months × 30 days` (default 24 months = 720 days).

**Scoring**: each recall earns a severity weight:

| Class      | Weight |
|------------|--------|
| class_1    | 3      |
| class_2    | 2      |
| class_3    | 1      |
| unclassified | 1    |

**Thresholds** (config-driven, all conditions must be met):

| Level    | min_recalls | min_weighted_score |
|----------|-------------|---------------------|
| medium   | 3           | 4                   |
| high     | 4           | 8                   |
| critical | 6           | 12                  |

One `RiskSignalCandidate` is emitted per canonical manufacturer ID that crosses at least one
threshold. The `active_ingredient` is the most-recalled ingredient across all recall events.

### 2. Supply Chain Exposure (`supply_chain.py`)

For each active repeat-violator signal, checks whether the flagged manufacturer supplies a
significant share of a county's requirement for the same active ingredient.

**Requires**: `counties`, `suppliers`, and `county_supply` tables populated (see
`docs/synthetic_procurement.md`).

**Trigger**: `share_pct ≥ min_county_share_pct` (default 25%) in any county.

**Severity**:
- `critical` if `avg_exposure_pct ≥ 60` or `alternative_supplier_count = 0`
- `high` if `avg_exposure_pct ≥ 40` or `alternative_supplier_count < min_alternative_suppliers_for_low`
- `medium` otherwise

**Time-to-expiry**: `max(lead_time_days, time_to_expiry_floor_days)` across exposed counties.

### 3. Cross-Source Corroboration (`corroboration.py`)

Detects when two or more jurisdictions independently raise recalls for the same active
ingredient within the rolling window.  The signal is a standalone `cross_source_corroboration`
candidate; `persist_signals` then applies a severity boost to any co-occurring repeat-violator
or supply-chain signal for the same ingredient.

**Boost**: `boost_severity(severity, boost_levels)` increments the severity level by
`boost_levels` steps (default 1) on the ladder `low → medium → high → critical`.

---

## Configuration (`config/risk_rules.yaml`)

```yaml
version: "1.0"
repeat_violator:
  window_months: 24
  severity_weights: {class_1: 3, class_2: 2, class_3: 1, unclassified: 1}
  thresholds:
    medium: {min_recalls: 3, min_weighted_score: 4}
    high:   {min_recalls: 4, min_weighted_score: 8}
    critical: {min_recalls: 6, min_weighted_score: 12}
supply_chain_exposure:
  min_county_share_pct: 25.0
  min_alternative_suppliers_for_low: 3
  time_to_expiry_floor_days: 60
cross_source_corroboration:
  enable: true
  jurisdiction_count_for_boost: 2
  boost_levels: 1
```

Every `RiskSignal` row stores a `config_hash` (SHA-256 of the serialised config).  Running
`risk list` flags signals whose stored hash differs from the current config — they need to be
re-evaluated.

---

## Persistence (`persist.py`)

`persist_signals(session, candidates, config, as_of)` is the main entry point.

**Idempotence**: re-running with the same candidates and config produces no database writes
(evidence unchanged → `unchanged` count incremented, no new event row).

**Transition logic**:
- Candidate matches active signal → check `_evidence_changed()` (ignores `computed_at`)
  - unchanged → skip
  - changed → update signal fields, write `updated` event
- Candidate has no matching active signal → create new signal, write `created` event
- Active signal has no matching candidate → resolve signal, write `resolved` event

**`dry_run=True`**: counts are computed but nothing is written.

### Manufacturer Merge Semantics

When two manufacturer records are merged (A → B):

1. `resolve_signal_for_manufacturer_merge(session, old_mfr_id)` resolves all active signals
   on A with `reason="manufacturer_merged"`.
2. On the next `risk run`, signals are re-computed against the canonical entity B.
3. If the predecessor was resolved within 7 days, `first_seen` is carried forward from A's
   signal to B's new signal via `_find_predecessor_first_seen()`.

---

## CLI

```
# Run the engine (dry-run by default)
regulatory risk run --as-of 2026-05-18 --dry-run

# Commit results to the database
regulatory risk run --as-of 2026-05-18

# List active signals (stale ones flagged with [stale config])
regulatory risk list

# Show detail for one signal
regulatory risk show <signal-id>

# Plain-text trace (deterministic, no LLM)
regulatory risk explain <signal-id>

# Manually resolve a signal
regulatory risk resolve <signal-id> --reason "Manufacturer ceased trading"

# Suppress a signal (exclude from future alerts)
regulatory risk suppress <signal-id> --reason "False positive confirmed"
```

---

## Adding a New Rule

1. Create `src/regulatory/risk/<rule_name>.py` with an `async def detect_<rule>()` and a
   `detect_<rule>_sync()` pure-function variant.
2. Return `list[RiskSignalCandidate]` using `kind="<your_kind>"`.
3. Add configuration fields to `RiskRulesConfig` in `config.py` and `config/risk_rules.yaml`.
4. Pass the candidates to `persist_signals()` in the CLI's `risk run` command.
5. Write ≥ 3 unit tests using the `_sync` variant.

---

## Security

- All DB queries use SQLAlchemy ORM / parameterized statements.  No string interpolation.
- ruff rule `S608` (hardcoded SQL) is enabled — zero violations required.
- `risk_signal_events` is append-only by policy: no `DELETE` or `UPDATE` on event rows.
- `regulatory_readonly` role has `SELECT`-only access to risk tables.
  See `scripts/ops/create_readonly_role.sql`.

---

## Known Limitations

### Recall event clustering (follow-up issue #TBD)

**Recall event clustering:** the engine counts documents (recall enforcement filings), not
underlying root-cause events. Sources like openFDA file one enforcement record per affected
product code, so a single underlying recall event can produce 10–60 records with consecutive
recall_numbers filed on the same day. A manufacturer's repeat-violator count may therefore
over-state the number of independent quality failures by a factor of 10–50×.

Observed extremes (2026-05-18 snapshot):

| Manufacturer | Doc count | Cluster | Likely true events |
|---|---|---|---|
| GenoGenix LLC | 57 | D-0038-2026 → D-0094-2026 (all 57 consecutive) | ~1 |
| GOLD STAR DISTRIBUTION INC | 27 | D-0261-2026 → D-0287-2026 (all 27 consecutive) | ~1 |
| Glenmark Pharmaceuticals | 95 | 73 of 94 gaps ≤ 3 recall_numbers; multiple clusters | ~5–10 |
| ACME UNITED CORPORATION | 22 | 21 consecutive (D-0358–D-0378) + 1 earlier | ~2 |

Distinguishing enforcement filings from root-cause events requires clustering on
`(firm_fei_number, recall_initiation_date, recall_class)` — out of scope for v1. See
follow-up issue #TBD.

### Supply-chain figures are synthetic

All county-level supply data currently loaded into `county_supply` is synthetic
(`data_source = 'synthetic_v2'`). Supply-chain exposure percentages, alternative supplier
counts, and lead times are illustrative, not real procurement data. All supply_chain_exposure
signals carry a `data_provenance` field in their evidence explicitly noting this.

### openFDA ingredient extraction gap

47% of openFDA documents have empty `active_ingredients`. The openFDA adapter reads
`openfda.generic_name`; this field is absent for many compounded drugs, OTC products without
an NDC, and non-standard formulations. The ingredient is present in `product_description` but
the adapter does not parse it. See follow-up issue #TBD (openFDA adapter extraction gap).
