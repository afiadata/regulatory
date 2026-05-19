# openFDA adapter: active ingredient extraction reads wrong field

> Status: open follow-up against `feat/risk-engine` v1, identified during live validation Round 3 (2026-05).

## Problem

The openFDA adapter populates `documents.active_ingredients` from `openfda.generic_name`. On the current corpus this produces a 100% empty rate — the field is reliably absent on enforcement records. The active ingredient is actually present in `product_description`, but in unstructured form (e.g. `"Atorvastatin Calcium Tablets, 10 mg, 90-count bottle"`).

## Impact

- 47% of all openFDA recall documents have `active_ingredients = '{}'` (i.e. all of them, when narrowed to recall document_type).
- These documents cannot participate in supply_chain_exposure joins (which require active_ingredient match) or cross_source_corroboration (which counts jurisdictions per ingredient).
- The supply-chain join's recall scope is therefore dominated by SAHPRA and PPB Kenya. The "3 manufacturers of amoxicillin generics" demo line is correct against the current corpus only because amoxicillin recalls happen to come from KE/ZA sources.

## Proposed fix (out of scope for v1)

Parse active ingredient from `product_description` using a known-ingredients dictionary (e.g. RxNorm or WHO ATC). Pattern: scan the description for any token matching a known INN; collect all matches; normalize via `ingredient_normalize.py`; store both raw and normalized.

Stretch: when extraction is ambiguous, fall back to an LLM call with a strict structured-output schema. Cap call budget; cache by `product_description` hash.

## Test approach when implemented

- Unit: fixture set of 20 openFDA `product_description` strings spanning single-ingredient, combination, and edge cases (lot info embedded, formulation suffixes, etc.); assert extraction matches expected normalized ingredients.
- Live: re-run extraction backfill against the existing corpus; assert openFDA empty-rate drops from ~100% to <20%. The residual is genuine source-data absence on non-drug records.
- Live: confirm supply_chain_exposure signal count increases as openFDA recalls join correctly.
