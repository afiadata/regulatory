# Manufacturer Canonicalization

Raw `manufacturers` strings in ingested documents vary widely ("Cipla Ltd", "CIPLA LIMITED",
"Cipla").  The canonicalization pipeline normalises these into a single `manufacturers` table
row so that risk signals are grouped correctly across sources.

---

## Algorithm

Three stages run in priority order.  The first stage that produces a confident match wins.

### Stage 1 — Exact match after normalization

`normalize_name(raw)` produces a lowercase, punctuation-free, suffix-stripped string:

1. Unicode NFKD decomposition, drop combining characters (accents).
2. Lowercase.
3. Strip dotted corporate abbreviations (`S.A.`, `N.V.`, `B.V.`, `GmbH`) before punct removal.
4. Replace all non-word, non-space characters with spaces.
5. Remove legal suffixes: `ltd`, `limited`, `inc`, `llc`, `llp`, `plc`, `gmbh`, `ag`, `bv`,
   `nv`, `sa`, `sas`, `spa`, `kk`, `pty`, `pharmaceuticals`, `pharma`, `pvt`, `private`,
   `co`, `company`, `corp`, `corporation`, `industries`, `group`, `holdings`, `international`,
   `intl`, `mfg`, `manufacturing`.
6. Collapse whitespace.

If the normalized raw name matches any existing canonical name or alias, that manufacturer is
returned.  Confidence: 1.0.  Action: `"exact"`.

### Stage 2 — Fuzzy match

`rapidfuzz.fuzz.token_set_ratio` is used to compare the normalized raw name against all
existing normalized canonical names and aliases.

- Score ≥ 92 → merge.  Confidence: `score / 100`.  Action: `"fuzzy"`.
- 85 ≤ score < 92 → low-confidence: the pair is written to `reconciliation_review.jsonl` for
  human review and **not** merged automatically.
- Score < 85 → distinct manufacturer.

### Stage 3 — Manual override

The file `src/regulatory/risk/manufacturer_overrides.yaml` maps canonical names to lists of
known aliases.  The override check runs before the fuzzy comparison and always wins.
Confidence: 1.0.  Action: `"override"`.

---

## Database Schema

`manufacturers` table (added by migration `0002`):

| Column        | Type      | Notes                                      |
|---------------|-----------|--------------------------------------------|
| id            | UUID PK   |                                            |
| canonical_name | text     | Human-readable canonical form              |
| aliases       | text[]    | All known raw name variants                |
| countries     | text[]    | ISO-3166 country codes                     |
| confidence    | float     | Match confidence (1.0 for manual/exact)    |
| updated_at    | timestamptz |                                          |

`documents.canonical_manufacturer_ids` (UUID[]) links each document to one or more canonical
manufacturers.  A document may link to multiple if different manufacturers appear in the same
recall notice.

---

## CLI

```
# Dry-run: show what would change
regulatory manufacturers reconcile --dry-run

# Apply: write merged rows to the database
regulatory manufacturers reconcile --apply

# List candidates awaiting human review
regulatory manufacturers review

# Show detail for one canonical manufacturer
regulatory manufacturers show "Cipla"
```

---

## Override File Format

`src/regulatory/risk/manufacturer_overrides.yaml`:

```yaml
Cipla:
  - Cipla Ltd
  - CIPLA LIMITED
  - Cipla Pharma

Sun Pharma:
  - Sun Pharmaceutical Industries
  - Sun Pharmaceutical Industries Ltd
```

Any raw name appearing in an alias list is mapped to the canonical name at the top level.
This file takes precedence over the algorithm.  Edit it when you want to hard-code a merge
that the algorithm would not make (e.g., different country registrations of the same company).

---

## Review File Format

`reconciliation_review.jsonl` (one JSON object per line):

```json
{"raw_name": "Alpha Pharma Ltd", "candidate_canonical": "Alpha Generics", "score": 88.4, "confidence": 0.884, "reviewed_at": "2026-05-18T00:00:00+00:00"}
```

Use `regulatory manufacturers review` to list unresolved candidates.  To resolve: either add
the pair to `manufacturer_overrides.yaml` (merge) or take no action (keep them separate).
