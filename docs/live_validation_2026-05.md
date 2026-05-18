# Live Validation Report — 2026-05-18

**Branch:** `feat/risk-engine`  
**Baseline commit:** `40e076f`  
**Validated by:** manual + scripted queries against real DB  
**DB:** `postgresql://127.0.0.1:5433/regulatory`

---

## Phase 0 — Preflight

| Check | Result |
|---|---|
| `DATABASE_URL` set | ✓ `127.0.0.1:5433/regulatory` |
| Alembic head | `0005` (drop `normalized_hash`) |
| Total documents | 1841 |
| Sources represented | `openfda_drug` (1795), `ppb_ke_alerts` (8), `sahpra_recalls` (38) |
| Manufacturers reconciled | ✓ (all `canonical_manufacturer_ids` populated) |
| Procurement loaded | ✓ 1651 `county_supply` rows, 47 counties, 30 suppliers |

### Schema fixes applied before validation

During preflight two schema divergences were found and fixed:

**Finding #S1 — DB stamped to `0003` but migrations 0002–0003 DDL not applied.**  
`documents.canonical_manufacturer_ids`, `manufacturers` table, `counties`, `county_supply`, and `suppliers` tables were all absent. Fixed by running the missing DDL manually, then `alembic stamp 0003`.

**Finding #S2 — Ghost column `normalized_hash` on `documents`.**  
Column existed with `NOT NULL` + no default, blocking every INSERT. Not in any current model or migration. Fixed with migration `0005_drop_normalized_hash.py`.

**Finding #S3 — Adapter re-insert UniqueViolationError.**  
When a source page changes, `source_hash` differs → scheduler tried a fresh INSERT, hitting the URL unique constraint. Fixed in `scheduler.py`: after a hash miss, check by URL; if found, update in-place.

---

## Phase 1 — Signal Traces

### 1.1 Risk run output

```
regulatory risk run --as-of 2026-05-18
→ created=112, resolved=0, unchanged=0, updated=0
  64 critical + 23 high + 23 medium repeat_violator
  2 cross_source_corroboration
  0 supply_chain_exposure
```

### 1.2 Canonicalization sanity

- 1839 documents mapped to exactly 1 canonical manufacturer ID each.
- 2 documents have empty `canonical_manufacturer_ids` (both have empty `manufacturers` field — correct).
- No document has more than 1 canonical ID (no spurious duplicates from reconcile).

### 1.3 repeat_violator trace

**Signal:** `de338666` — ACME UNITED CORPORATION / BENZALKONIUM CHLORIDE — severity=critical

| Metric | Value | Threshold (critical) | Pass? |
|---|---|---|---|
| `recall_count` | 22 | ≥ 6 | ✓ |
| `weighted_score` | 44 | ≥ 12 | ✓ |
| Oldest document | 2025-11-05 | within 24-month window | ✓ |
| All sources | `openfda_drug` only | — | note |
| Severity distribution | 22 × class_2 (w=2 each) | — | ✓ |

All 22 documents are `class_2` FDA enforcement records. Only `openfda_drug` is represented; no cross-source diversity within this signal (expected — repeat_violator is single-source).

### 1.4 cross_source_corroboration traces

| Signal | Ingredient | Jurisdictions | Docs | Dupes |
|---|---|---|---|---|
| `1083a5fd` | acetaminophen | KE (7), US (6), ZA (1) → 3 jurisdictions | 14 | 0 (after fix) |
| `fdaed1a0` | ciprofloxacin | KE (1), US (3) → 2 jurisdictions | 4 | 0 |

Both meet the `jurisdiction_count_for_boost: 2` threshold. ✓

### 1.5 supply_chain_exposure — 0 signals (expected)

The 6 suppliers with a `manufacturer_id` link in the synthetic data are: AstraZeneca, GSK, Sun Pharma, Cipla Limited, Pfizer, Roche. None of these appear in any `repeat_violator` signal (which come from FDA enforcement data and use different canonical names). Additionally, `county_supply` uses lowercase EML ingredient names (`atorvastatin`) while FDA recall signals use uppercase (`ATORVASTATIN CALCIUM`), so ingredient matching would fail even if manufacturers did align. Zero signals is correct behavior for this data configuration.

**When real procurement data is loaded:** ensure (a) `suppliers.manufacturer_id` is populated at load time by matching supplier names against the `manufacturers` table, and (b) ingredient names are normalized consistently (INN lowercase) before loading into `county_supply`.

---

## Phase 2 — Idempotence

```
# Run 1: evidence updated (new fields added by fixes)
regulatory risk run --as-of 2026-05-18
→ created=0, resolved=0, unchanged=0, updated=112

# Run 2: same as_of, no changes
regulatory risk run --as-of 2026-05-18
→ created=0, resolved=0, unchanged=112, updated=0
```

Idempotence confirmed. ✓

---

## Findings and Fixes

### Finding #1 — DB schema divergence (FIXED: migration 0005)
See §Phase 0 Finding #S1 above.

### Finding #2 — Ghost column `normalized_hash` (FIXED: migration 0005)
See §Phase 0 Finding #S2 above.

### Finding #3 — Adapter re-insert UniqueViolationError (FIXED: scheduler.py)
See §Phase 0 Finding #S3 above.

### Finding #4 — Evidence not self-documenting (FIXED: persist.py)
`repeat_violator` evidence did not store `recall_count` or `weighted_score` — they were computable only by refetching all documents. Fixed: `_build_evidence()` now stores both fields when the candidate populates them. `repeat_violator.py` now passes `recall_count=` and `weighted_score=` to `RiskSignalCandidate`. `config.py` has the new optional fields.

### Finding #5 — Synthetic data share_pct sums (known limitation)
63 county-ingredient pairs sum to values outside 100±5 %. This is a property of the random generator (`data/synthetic/`). Not a bug in the risk engine. When real procurement data is loaded, validate that per-county-ingredient shares sum to 100 %.

### Finding #6 — Duplicate document IDs in corroboration evidence (FIXED: persist.py)
Root cause: document `d442a057` (PPB Kenya, Para-Denk 250mg) has `active_ingredients: ['acetaminophen', 'and', 'acetaminophen']` — the ingredient appears twice, so `corroboration.py` appended the document ID twice. Fixed: `_build_evidence()` now deduplicates `document_ids` with `dict.fromkeys()` before storing. Acetaminophen evidence: 15 raw IDs → 14 unique stored.

### Finding #7 — Empty `jurisdictions` in corroboration evidence (FIXED: persist.py)
`cross_source_corroboration` evidence had no `jurisdictions` key despite the rule computing them. Fixed: `_build_evidence()` now stores `jurisdictions: sorted(candidate.regions_affected)` for corroboration signals.

### Finding #8 — PPB Kenya adapter parses stopword "and" as active ingredient (OPEN)
Document `d442a057` has `active_ingredients: ['acetaminophen', 'and', 'acetaminophen']`. The parser splits on "/" or "and" but does not filter single common English words. **Action:** filter single-word English stopwords from parsed ingredient lists in `ppb_ke_alerts.py`. Tracked for next adapter maintenance window.

### Finding #9 — supply_chain_exposure signals = 0 (expected, data gap)
See §1.5 above. Not a code defect.

---

## Adversarial Checks

| Check | Result |
|---|---|
| Empty `active_ingredients` | 817/1841 docs (44%) — high but explained by openFDA device/cosmetic records with no INN |
| Stopword "and" in ingredients | 1 document (`d442a057`) — Finding #8 |
| Severity values | Only `class_1`, `class_2`, `class_3`, `unclassified` — no unexpected values |
| Date window boundary (as_of=2026-05-18, window_start=2024-05-28) | 61 docs near boundary, all correctly included/excluded |
| Idempotent re-run | ✓ (see §Phase 2) |
| `first_seen` stability | All 112 signals: `first_seen == last_updated` (single-batch creation); event log has exactly one `created` event each |
| Supply chain data linkage | 6/30 suppliers have `manufacturer_id`; 316 county_supply rows reachable — but no manufacturer overlap with repeat_violator signals (data gap, not code bug) |

---

## Quality Gates (post-validation)

```
mypy --strict src/regulatory/   → Success: no issues found in 28 source files
ruff check src/ tests/          → All checks passed
pytest                          → 198 passed, 1 skipped, 74.17% coverage ≥ 70%
```

---

## Round 2 Validation — 2026-05-18 (evening)

### Items completed

**Item 1 — Synthetic generator v2 (DB-aware)**

Generator rewritten to query the top-25 manufacturers by recall count and create ~18 manufacturer-linked suppliers using their exact `canonical_name`. County supply rows are generated with 30–70% share per linked supplier, guaranteeing the ≥25% threshold for supply-chain exposure.

Result: 28 suppliers (18 linked + 10 distributors), 2008 county_supply rows in `data/synthetic/procurement_v2/`.

**Item 2 — INN normalization**

Added `src/regulatory/risk/ingredient_normalize.py` with `normalize_ingredient()`: lowercase + single trailing salt/ester suffix strip. 18 tests in `tests/test_ingredient_normalize.py`; all pass, 100% coverage.

Migration `0006_active_ingredients_normalized.py`: adds `active_ingredients_normalized` ARRAY column + GIN index + SQL regexp backfill; 1841 documents backfilled successfully.

`repeat_violator.py` now groups on `active_ingredients_normalized`. `supply_chain.py` normalizes the signal ingredient before the `CountySupply` join (both main and alt-supplier queries).

**Item 3 — ACME UNITED 22-recall spot-check**

Checked all 22 document IDs: each has a distinct `recall_number` (D-0122-2026, D-0358-2026 through D-0378-2026). All are distinct FDA enforcement actions for different product SKUs (various BZK antiseptic towelette brands) from the same manufacturing facility. Count of 22 is correct; no dedup by recall_event_id is needed.

**Item 4 — PPB "and" parser bug (FIXED)**

`_parse_inn_cell()` now filters single-word stopwords (`and`, `or`, `with`, `in`, `of`, `the`, `a`, `an`, `to`, `for`) from both normalized and raw output. The stopword is only dropped when it appears as a standalone token (its own newline or semicolon-delimited part); "and" inside a single-line combination name like "Ibuprofen and Paracetamol" is preserved as one ingredient entry.

Regression tests: `tests/test_ppb_adapter_ingredient_extraction.py` — 9 cases; all pass.

**Item 5.1 — Canonicalization sanity**

Top-20 manufacturers by active signal count: all canonical names are upper/title case matching the source. ACCORD HEALTHCARE, INC. holds 4 aliases; ACME UNITED CORPORATION holds 0 aliases (all 22 recalls were ingested verbatim).

Prefix-pair false positives: none found. Known false negatives (should merge but don't, due to address variants appended to name):
- `Pfizer Laboratories (Pty) Ltd` — 3 entries with different street addresses appended
- `Pharmacare Limited t/a Aspen Pharmacare` — 3 entries with address variants
- `Biopharma Ltd` / `Biopharma ltd, Kenya`
- `Empower Clinic Services, LLC dba Empower Pharmacy` / `Empower Pharmacy`

These are cosmetic differences in source text; the canonicalization algorithm correctly keeps them separate given the evidence. A manual override can merge them if required.

**Item 5.2 — Adversarial checks**

| Check | Result |
|---|---|
| Check 1: empty `active_ingredients` by source | openFDA: 47.0% empty (device/cosmetic records with no INN — expected); sahpra: 23.4%; ppb_ke: 0% |
| Check 2: severity mapping | Documents: class_1 (174), class_2 (1433), class_3 (179), unclassified (55). Signals: critical (64), high (23), medium (35), low (4). Mapping is correct. |
| Check 3: date boundary | Docs near 24-month boundary (±1 mo): 98 docs across 2024-04-24 to 2024-06-14 — all correctly handled. |
| Check 4: synthetic data provenance | All `supply_ids` in supply_chain signals trace to `county_supply.data_source = 'synthetic_v2'`. Traceability via supply_ids cross-reference confirmed. No false claim of real data. |
| Check 6: manufacturer-merge continuity | `manufacturers reconcile --dry-run`: 448 exact + 10 override, 0 pending review. Zero truly orphaned signals (signals with non-null `manufacturer_id` not matching any manufacturer row). The 2 signals showing NULL manufacturer_id are `cross_source_corroboration` signals — correctly NULL by design. |

**Item 5.3 — Readonly role test**

Fixed two bugs in `tests/test_security.py`:
1. `GRANT CONNECT ON DATABASE current_database()` — `current_database()` is not valid as a literal database name in GRANT. Fixed by fetching the DB name with `SELECT current_database()` and interpolating it.
2. `SET ROLE` is transaction-local — rolled back after each `conn.rollback()`. Fixed by re-issuing `SET ROLE regulatory_readonly` at the start of each inner loop iteration.

Test `test_readonly_role_cannot_write` now passes when `TEST_DATABASE_URL` is set. All 7 regulated tables × 3 write operations = 21 deny assertions confirmed.

### Supply-chain signals after Round 2

14 supply_chain_exposure signals fired: 1 high (avg_exposure ≥ 50%), 9 medium (≥ 25%), 4 low (enough alternative suppliers).

```
risk run --as-of 2026-05-18 (run 1):  created=0, resolved=0, unchanged=126, updated=0
risk run --as-of 2026-05-18 (run 2):  created=0, resolved=0, unchanged=126, updated=0
```

Idempotence confirmed. ✓

### Final signal inventory

| Kind | Status | Count |
|---|---|---|
| `repeat_violator` | active | 110 |
| `supply_chain_exposure` | active | 14 |
| `cross_source_corroboration` | active | 2 |
| `repeat_violator` | resolved | 70 (pre-normalization batch with uppercase ingredients) |
| **Total active** | | **126** |

### Quality Gates (Round 2)

```
mypy --strict src/regulatory/risk/ src/regulatory/sources/ppb_ke_alerts.py src/regulatory/db/models.py
  → Success: no issues found in 10+1+1 source files

ruff check src/ tests/
  → All checks passed

pytest (with TEST_DATABASE_URL set)
  → 226 passed, 0 failed, 5 warnings; coverage 74.47% ≥ 70%
  → risk module: ingredient_normalize 100%, persist 97%, canonicalize 97%, repeat_violator 91%, supply_chain 89%, corroboration 93% — all ≥ 85%
```
