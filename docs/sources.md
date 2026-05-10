# Regulatory Source Inventory

This document tracks every planned data source, its access tier, and implementation status.
Before writing any Tier 3 adapter, conduct a 30-minute manual inspection and document
findings in `docs/sources/<source_id>.md`.

---

## Tier 1 — Proper APIs

| Source ID | Authority | Base URL | Status |
|---|---|---|---|
| `openfda_drug` | FDA (USA) | `https://api.fda.gov/drug/enforcement.json` | ✅ Implemented |
| `clinicaltrials_gov` | ClinicalTrials.gov | `https://clinicaltrials.gov/api/v2/studies` | Backlog |
| `ema_medicines` | EMA (EU) | `https://www.ema.europa.eu/en/medicines/download-medicine-data` | Backlog |

### Notes
- **openFDA**: No API key required, but optional key (`OPENFDA_API_KEY`) raises limit
  from 240 req/min to 120k/day. Register at https://open.fda.gov/apis/authentication/
- **ClinicalTrials.gov v2**: v1 API was retired June 2024. Always use v2. Token-based
  pagination. See https://clinicaltrials.gov/data-api/api
- **EMA**: Multiple endpoints — nightly downloadable tables for approved medicines,
  `epi.developer.ema.europa.eu` for ePI documents, EudraGMDP for GMP certificates.

---

## Tier 2 — Structured HTML, Stable

| Source ID | Authority | Base URL | Status |
|---|---|---|---|
| `mhra_alerts` | MHRA (UK) | `https://www.gov.uk/drug-safety-update` | Backlog |
| `sahpra_alerts` | SAHPRA (ZA) | `https://www.sahpra.org.za/safety-updates-and-recalls/` | Backlog |
| `pactr` | PACTR (Africa) | `https://pactr.samrc.ac.za/` | Backlog |

### Notes
- **SAHPRA**: Stable listing page with pagination. Mix of HTML entries and PDF
  attachments. Requires South Africa-specific product naming conventions.
- **PACTR**: Pan African Clinical Trials Registry — useful backup for clinical trial
  data when ClinicalTrials.gov coverage is sparse for African sites.

---

## Tier 3 — Scrape, PDF-Heavy, Fragile

> Before implementing any Tier 3 adapter, complete a 30-min manual inspection
> and create `docs/sources/<source_id>.md` with findings.

| Source ID | Authority | Base URL | Status |
|---|---|---|---|
| `ppb_ke_alerts` | PPB Kenya | `https://web.pharmacyboardkenya.org/` | ✅ Implemented |
| `tmda_tz` | TMDA Tanzania | `https://www.tmda.go.tz/` | Backlog |
| `mcaz_zw` | MCAZ Zimbabwe | `http://www.mcaz.co.zw/` | Backlog |
| `nda_ug` | NDA Uganda | `https://www.nda.or.ug/` | Backlog |
| `zamra_zm` | ZAMRA Zambia | `https://www.zamra.co.zm/` | Backlog |
| `efda_et` | EFDA Ethiopia | `https://www.efda.gov.et/` | Backlog |

### Implementation notes by source

#### `ppb_ke_alerts` (Kenya)
- Landing page URL discovered dynamically at runtime — do not hardcode.
- PDFs are primary format. Three-stage extraction: pdfplumber → PyMuPDF → Anthropic API.
- Severity often stated in body text; regex heuristic + Anthropic structured extraction.
- Resume-safe via Postgres `fetch_log` table.

#### `tmda_tz` (Tanzania)
- Site content in Swahili and English; language detection required.
- Inspect before implementing: check for feeds/sitemaps, determine update cadence.

#### `mcaz_zw` (Zimbabwe)
- HTTP (not HTTPS) — handle redirect and SSL issues carefully.
- Minimal digital presence; inspect for scraping feasibility before committing to adapter.

#### `nda_ug` (Uganda)
- Alerts typically posted as PDFs linked from news/announcement pages.
- Check if site has any structure (categories, dates) or is a flat list.

#### `zamra_zm` (Zambia)
- Inspect for API or feed; site has undergone recent redesigns.
- Product registration database may be accessible separately.

#### `efda_et` (Ethiopia)
- Bilingual: Amharic + English. Amharic text requires language detection and may need
  transliteration or translation before INN normalization.
- Check EFDA ePortal at https://eportal.efda.gov.et/ for structured data.

---

## Data Quality Notes

- **INN normalization**: The lookup table at `src/regulatory/inn/inn_lookup.json` covers
  common African market formulations. Expand it as new ingredients are encountered.
- **Manufacturer deduplication**: The `manufacturers` table is populated by a future
  nightly reconciliation job. Initial data arrives as raw strings per-document.
- **Recall data gap**: PPB Kenya does not currently expose a structured recalls API.
  The `ppb_ke_alerts` adapter scrapes the human-facing alerts page. Historical recall
  data (`data/ppb_recalls_update/`) does not exist in this repo.

---

## Adding a New Source

1. Create `docs/sources/<source_id>.md` with manual inspection findings.
2. Create `src/regulatory/sources/<source_id>.py`.
3. Decorate the class with `@register_source`.
4. Add it to `config/sources.yaml` with appropriate rate limits.
5. Add a fixture in `tests/fixtures/` and a test in `tests/test_<source_id>.py`.
6. Import it in `src/regulatory/cli.py` so it self-registers on startup.
