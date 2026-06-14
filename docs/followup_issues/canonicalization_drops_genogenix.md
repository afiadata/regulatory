# Canonicalization gap: GenoGenix LLC present in documents, absent from manufacturers

> Status: open follow-up against `feat/nl-agent`, identified during eval Round 2 (2026-06).

## Problem

57 openFDA enforcement documents (all filed 2025-10-15, recall numbers D-0038-2026 through
D-0094-2026) reference "GenoGenix" or "GenoGenix LLC" in their `manufacturers` and `raw_text`
fields. Despite having the volume of a critical-severity repeat violator, GenoGenix LLC was
**not** written into the `manufacturers` canonical table by the canonicalization job.

Consequence: the agent tool surface cannot surface GenoGenix. `manufacturer_profile` does a
fuzzy match against `manufacturers.canonical_name`; with no row to match against, it returns
"no manufacturer by that name was found." No `repeat_violator` risk signal exists for GenoGenix
because the risk engine operates on `manufacturer_id` foreign keys into `manufacturers`.

## Reproduction

```sql
-- 57 rows present in documents:
SELECT COUNT(*), date_published
FROM documents
WHERE source_id = 'openfda_drug'
  AND raw_text ILIKE '%genogenix%'
GROUP BY date_published;
-- → 57 rows, 2025-10-15

-- 0 rows in manufacturers:
SELECT COUNT(*) FROM manufacturers
WHERE canonical_name ILIKE '%genogenix%';
-- → 0
```

## Root cause (suspected)

The canonicalization job uses fuzzy matching with a confidence threshold to map raw manufacturer
strings to canonical entities. GenoGenix LLC most likely failed to match any existing canonical
entry above the threshold, and was either:

1. Dropped silently (no low-confidence fallback), or
2. Merged into an incorrect canonical entity (less likely given the 0-row result).

The canonicalization job's logs for the batch covering D-0038-2026 through D-0094-2026 should
be inspected to confirm which path was taken.

## Impact

- Procurement officers asking about GenoGenix get a "not found" response instead of a 57-document
  critical recall cluster.
- The risk engine has no repeat-violator signal for GenoGenix, so `county_exposure` will not
  flag suppliers sourcing GenoGenix products.
- The `recall_event_clustering.md` follow-up listed GenoGenix as a known extreme in the risk
  engine — this is incorrect; it is a corpus-only observation from the `documents` table.

## Proposed fix

1. Add a low-confidence fallback path in the canonicalization job: when no existing canonical
   entity matches above threshold, create a new canonical `Manufacturer` row from the raw name.
   Mark it `confidence=<observed_score>` so reconciliation jobs can review.
2. Re-run canonicalization for the GenoGenix batch (source_id=openfda_drug, date=2025-10-15).
3. Re-run the risk engine to generate the repeat-violator signal.
4. Backfill `normalized_content_hash` for affected documents (see migration discipline in
   `CLAUDE.md` §3).

## Note on golden_qa.yaml

q009 and q031 were updated to substitute Glenmark Pharmaceuticals for GenoGenix LLC because
Glenmark is confirmed in the `manufacturers` table (two canonical entries, both with active
signals and `count_inflation_likely: true`). The GenoGenix questions would test the "not found"
path, not manufacturer recall history. Once this gap is resolved, the questions can be reverted.
