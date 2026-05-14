# SAHPRA Product Recalls

## Overview

- **Regulator**: South African Health Products Regulatory Authority (SAHPRA)
- **Jurisdiction**: ZA (South Africa)
- **Source ID**: `sahpra_recalls`
- **Document type**: `recall`
- **Adapter**: `src/regulatory/sources/sahpra_recalls.py`

SAHPRA publishes product recalls as individual detail pages accessible via a
paginated WordPress-based listing. Pages are plain HTML; no JavaScript rendering
is required.

---

## URL Patterns

| Purpose | Pattern |
|---|---|
| Listing page 1 | `https://www.sahpra.org.za/document-category/product-recall/` |
| Listing page N ≥ 2 | `https://www.sahpra.org.za/document-category/product-recall/page/N/` |
| Detail page | `https://www.sahpra.org.za/document/{slug}/` |

Pagination ends when a page returns HTTP 404 or contains zero recall cards
(`<article class="... dlp_document ...">`). A safety cap of 50 pages is enforced.
As of verification (2026-05-10) there are approximately 10–20 pages.

---

## Field Mapping

| Table column | Maps to |
|---|---|
| Company name & Address | `manufacturers[0]` (first line); address → `raw_metadata["manufacturer_address"]` |
| registration number | `raw_metadata["registration_number"]`; used as `document_id` when not `N/A` |
| Batch number(s) | `raw_metadata["batch_numbers"]` (list, split on newlines / `<br>`) |
| Expiry date | `raw_metadata["expiry_date_raw"]` (stored as raw string; month/year only) |
| Pack size | `raw_metadata["pack_size"]` |
| First release date | `raw_metadata["first_release_date"]` (ISO if parseable, else raw) |
| Re-call Classification | `severity` (see mapping below) + `raw_metadata["recall_type"]` (full string) |
| Recall date | `date_published` |

Narrative sections extracted to `raw_metadata`:

| Section label | Key |
|---|---|
| Brief description of the problem | `recall_reason` |
| Advice for health professionals | `advice` |
| Proposed action taken | appended to `advice` when present |
| SharePoint download link | `pdf_url` (stored but not fetched — auth required) |

---

## Severity Mapping

The Type letter (A/B/C) is distribution scope, not severity, and is ignored for the
`severity` field. The full classification string is preserved in `raw_metadata["recall_type"]`.

| Classification text contains | `severity` |
|---|---|
| `Class I` or `Class 1` | `class_1` |
| `Class II` or `Class 2` | `class_2` |
| `Class III` or `Class 3` | `class_3` |
| anything else / missing | `unclassified` |

---

## Title Parsing

Page titles follow the pattern `Product Name (Active Ingredient, ...)`.

- `product_names[0]` = text before the final `(`
- `active_ingredients_raw` = comma-split content inside the final `(...)`
- `active_ingredients` = each raw value looked up in the INN table; falls back to raw on miss

If no parentheses are present (e.g., `Kiwi Complete Vacuum Delivery System`), the full
title becomes `product_names[0]` and `active_ingredients` is empty.

---

## Known Quirks

### Variable column order

Some detail pages include a **Product strength** column inserted between
"Company name & Address" and "registration number" (observed on Visipaque and Omnipaque
entries). The parser matches columns by header text substring, not position.

### Multiple date formats

Verified formats:
- `04 May 2026` (`%d %B %Y`)
- `20.April.2026` (`%d.%B.%Y`)

Both are tried in order. An unparseable recall date causes `parse()` to raise
`ValueError` (the scheduler catches this and skips the record, logging the raw string).

### Cross-border distribution

Narrative sections (particularly `recall_reason`) often name countries outside ZA where
the recalled product was distributed (e.g., Eswatini, Rwanda, Kenya, Tanzania, Nigeria).
This data is **not extracted in this PR** — `regions_affected` is always `["ZA"]`.
Cross-border extraction is a planned future enrichment (separate PR).

### SharePoint PDF

Most recalls include a download link to a SharePoint-hosted PDF
(`sahpraza.sharepoint.com/...`). The PDF is not fetched in this adapter — SharePoint
requires authentication in many cases and the HTML table already contains the structured
data. The URL is stored in `raw_metadata["pdf_url"]` for future retrieval.

---

## Fixture Provenance

| File | Live URL | Fetched |
|---|---|---|
| `tests/fixtures/sahpra_listing_page1.html` | `https://www.sahpra.org.za/document-category/product-recall/` | 2026-05-10 |
| `tests/fixtures/sahpra_detail_halaven.html` | `https://www.sahpra.org.za/document/halaven-eribulin/` | 2026-05-10 |
| `tests/fixtures/sahpra_detail_kiwi_no_parens.html` | `https://www.sahpra.org.za/document/kiwi-complete-vacuum-delivery-system/` | 2026-05-10 |
| `tests/fixtures/sahpra_detail_multi_batch.html` | `https://www.sahpra.org.za/document/cipla-pioglitazone/` (or equivalent) | 2026-05-10 |

Fixtures are trimmed real fetches. Page-level boilerplate (nav, footer, scripts) is
removed; the recall `<article>` or page body content is preserved verbatim.
