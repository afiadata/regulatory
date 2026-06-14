# Risk engine: repeat-violator counts documents, not root-cause events

> Status: open follow-up against `feat/risk-engine` v1, identified during live validation Round 3 (2026-05).

## Problem

The repeat-violator rule counts `documents` rows (enforcement filings), not the underlying root-cause recall events. Sources like openFDA file one enforcement record per affected product code or SKU, so a single manufacturing quality failure can produce 10–60 consecutive records filed on the same day. A manufacturer's repeat-violator count therefore over-states independent quality failures by a factor of 10–57×, depending on how many product codes were involved.

## Observed extremes (2026-05-18 corpus snapshot)

| Manufacturer | Doc count | Recall number cluster | Likely true events | In risk engine? |
|---|---|---|---|---|
| GenoGenix LLC | 57 | D-0038-2026 → D-0094-2026 (all 57 consecutive) | ~1 | **No** — canonicalization gap; see [canonicalization_drops_genogenix.md](canonicalization_drops_genogenix.md) |
| GOLD STAR DISTRIBUTION INC | 27 | D-0261-2026 → D-0287-2026 (all 27 consecutive) | ~1 | Yes |
| Glenmark Pharmaceuticals | 95 | 73 of 94 gaps ≤ 3 recall_numbers; multiple clusters | ~5–10 | Yes |
| ACME UNITED CORPORATION | 22 | 21 consecutive (D-0358–D-0378) + 1 earlier | ~2 | Yes |

Gold Star Distribution, Glenmark, and ACME appear as repeat violators in the risk engine. GenoGenix LLC has 57 documents in the `documents` table but was not canonicalized into the `manufacturers` table, so no risk signal exists for it. Gold Star and ACME are likely single-event outliers that crossed the high/critical threshold solely due to filing granularity.

## Impact

- `repeat_violator` signal severity is over-stated for openFDA-heavy manufacturers.
- Downstream `supply_chain_exposure` signals inherit this over-statement.
- The `count_inflation_likely` flag on the `get_risk_signal` agent tool surface heuristically flags this condition (recall_count ≥ 10, openFDA source share ≥ 80%) so agents and users can contextualise the count. This is a mitigation, not a fix.
- SAHPRA and PPB Kenya have not been observed to exhibit the same per-SKU filing convention in the current corpus.

## Proposed fix (out of scope for v1)

Cluster enforcement filings into root-cause events by grouping on
`(firm_fei_number, recall_initiation_date, recall_class)`. Each cluster is counted as one event. The repeat-violator rule operates on the event count, not the document count. Recall numbers with consecutive IDs filed on the same day by the same firm are strong evidence of a single event.

Steps:
1. Add a `recall_event_id uuid` column to `documents` (nullable; populated only for openFDA records with FEI + initiation date present).
2. Write a backfill job that assigns the same `recall_event_id` to documents that share `(firm_fei_number, recall_initiation_date, recall_class)`.
3. Modify `repeat_violator.py` to group by `recall_event_id` (where non-null) rather than by document row.
4. Adjust thresholds in `risk_rules.yaml` to reflect event-count semantics; the current thresholds (medium=3, high=4, critical=6) were calibrated against document counts and will need re-tuning.

## Test approach when implemented

- Unit: inject 57 documents sharing the same `(firm_fei_number, recall_initiation_date, recall_class)`; assert the repeat-violator detector counts 1 event, not 57. Confirm GenoGenix-like scenario becomes 1 signal below critical rather than 57-document critical.
- Live: re-run risk engine after backfill; confirm GenoGenix LLC and Gold Star Distribution drop from critical to low/none; Glenmark Pharmaceuticals drops from critical to ~high with 5–10 true events.
- Live: confirm `count_inflation_likely` flag is absent on the corrected signals.
