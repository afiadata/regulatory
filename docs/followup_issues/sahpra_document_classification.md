# SAHPRA adapter: misclassified non-drug records

> Status: open follow-up against `feat/risk-engine` v1, identified during live validation Round 3 (2026-05).

## Problem

The SAHPRA adapter ingests all enforcement records from the source as `document_type = 'recall'`. Spot-check in Round 3 found ~60% of the empty-active-ingredient SAHPRA documents are actually medical-device, in-vitro-diagnostic-reagent, or cosmetic recalls being ingested through the same endpoint. The remaining ~40% are drug recalls with genuine extraction gaps.

## Impact

- 23.4% of SAHPRA documents have empty active_ingredients overall.
- The non-drug subset shouldn't participate in drug-focused rules (repeat_violator, supply_chain_exposure, corroboration) at all. Currently they're filtered out at join time silently (empty active_ingredients means no join), but they inflate manufacturer counts in canonicalization and they're counted in the corpus totals reported in the dashboard.
- The drug-extraction-gap subset (~40% of the empty bucket, ~9% of total SAHPRA documents) is a real extraction loss similar to the openFDA case but smaller in magnitude.

## Proposed fix (out of scope for v1)

Two changes:

1. Classify document type at ingest. Read the SAHPRA category field if present, or string-match the record title/body against device/diagnostic keywords. Map non-drug records to `document_type = 'device_recall'` or `'cosmetic_recall'` etc. Risk rules in this PR only operate on `document_type = 'recall'` (drug recalls), so reclassification removes them from the drug-rule scope without losing the record.

2. Improve drug extraction on the remaining drug records similar to the openFDA fix in issue 1a.

## Test approach when implemented

- Unit: fixture set of 15 SAHPRA records spanning drug, device, IVD, and cosmetic categories; assert document_type classification matches expected.
- Live: re-classify the existing SAHPRA corpus; assert drug-recall count drops by ~10–15% and a corresponding set of non-drug-recall rows appears.
- Live: re-run risk engine; confirm canonicalization manufacturer count drops slightly (device-only manufacturers no longer in the drug recall scope) and supply_chain_exposure signal counts are unchanged or rise (drug-recall extraction quality improvement).
