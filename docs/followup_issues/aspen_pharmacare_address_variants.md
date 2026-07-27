# Manufacturer canonicalization: Aspen Pharmacare address variants unmerged

> Status: open follow-up against `feat/nl-agent`, surfaced during Eval Round 3 (2026-07).

## Problem

Three rows in the `manufacturers` table are address-variant duplicates of the same underlying
entity, Aspen Pharmacare (South Africa):

- Pharmacare Limited t/a Aspen Pharmacare, Healthcare Park, Woodlands Drive, Woodmead, Sandton 2196
- Pharmacare Limited trading as Aspen Pharmacare
- Pharmacare Ltd t/a Aspen Pharmacare Building 12, Healthcare Park Woodlands Drive

The canonicalization pipeline did not merge these because the string differences (full address
vs. partial vs. building suffix) exceed the fuzzy-match threshold used during reconciliation.

## Surfaced by

Eval Round 3 q040 disambiguation query on "Aspen." The agent correctly returned 5 candidates
and explicitly noted that candidates 3–5 appeared to be address-variant duplicates of the same
underlying entity. This is the expected disambiguation behavior (category:
`manufacturer_disambiguation`), but the underlying data quality gap affects downstream signal
counts and risk-profile aggregation for this entity.

## Impact

Aspen Pharmacare's recall count is split across three rows. Signal scores, repeat-violator
counts, and supply-chain exposure figures that reference any one of these rows will be
understated relative to the true consolidated entity count.

## Resolution

1. Add an override entry to `manufacturer_overrides.yaml` explicitly merging all three
   Pharmacare Limited t/a variants into a single canonical row under "Aspen Pharmacare
   (South Africa)".
2. Re-run `regulatory risk reconcile` (or equivalent).
3. Verify the `manufacturers` table drops from 5 Aspen-related rows to ≤3 (preserving the
   "Aspen Pharmacare Holdings" entry and the main "Aspen Pharmacare" row if they are genuinely
   distinct entities).

## Out of scope for this PR

Do not merge the manufacturer rows in the `feat/nl-agent` PR. This touches risk-engine data
and belongs in a small follow-up PR against `main` after this one merges.
