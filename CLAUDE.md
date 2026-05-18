# AfiaData Regulatory Monitor — Project Constitution

## Mission

Build a system that ingests regulatory and clinical-trial data from multiple African and global
authorities, normalizes it into a shared schema, stores it in Postgres, and surfaces
cross-jurisdictional risk signals (repeat-violator manufacturers, supply-chain exposure, referral
cascades). The eventual product is a Claude-powered agent that procurement officers and regulatory
affairs teams can query in natural language. This PR is the ingestion foundation only.

---

## Tech Stack

| Layer | Choice |
|---|---|
| Language | Python 3.10+, managed by **uv** |
| HTTP | **httpx** (async-capable). `requests` is banned. |
| JS-rendered sites | **playwright** only when httpx + BS4 cannot reach content |
| Schemas | **pydantic** v2 |
| ORM / migrations | **SQLAlchemy** 2.0 + **alembic** |
| PDF extraction | pdfplumber → PyMuPDF → Anthropic API (last resort) |
| LLM | **anthropic** SDK. Default model: `claude-sonnet-4-6` |
| Tests | **pytest** + **pytest-asyncio**. Fixtures via **pytest-recording** / vcr.py |
| Logging | **structlog**. No `print()` statements anywhere. |
| Config | **python-dotenv** for `.env`; `config/sources.yaml` for per-source settings |
| Batch / remote | **Modal** for heavy PDF batch processing |
| CLI | **typer** |

---

## Normalized Schema (`NormalizedDocument`)

Every adapter output must conform to this Pydantic model:

```python
NormalizedDocument:
  source_id: str
  source_url: HttpUrl
  source_hash: str                    # sha256 of raw content
  jurisdiction: str                   # ISO-3166 alpha-2 or "EU"/"GLOBAL"
  document_type: Literal[
      "recall","alert","enforcement","trial","guideline","shortage","referral"
  ]
  document_id: str | None             # native ID from the source
  title: str
  product_names: list[str]
  active_ingredients: list[str]       # normalized to INN; raw kept in active_ingredients_raw
  active_ingredients_raw: list[str]
  manufacturers: list[str]
  marketing_authorization_holders: list[str]
  severity: Literal["class_1","class_2","class_3","unclassified"] | None
  date_published: date
  date_effective: date | None
  regions_affected: list[str]
  language: str                       # ISO 639-1
  raw_text: str
  raw_metadata: dict
  extracted_at: datetime
```

INN normalization: lookup table at `src/regulatory/inn/inn_lookup.json`. Where normalization
fails, keep raw value and flag it.

---

## Adapter Interface (`RegulatorySource` ABC)

```python
class RegulatorySource(ABC):
    source_id: str            # e.g. "ppb_ke", "openfda_drug"
    jurisdiction: str         # ISO country code or "EU", "GLOBAL"
    document_types: list[DocumentType]

    @abstractmethod
    async def discover(self, since: datetime | None) -> AsyncIterator[DocumentRef]:
        """Yield document references newer than `since`."""

    @abstractmethod
    async def fetch(self, ref: DocumentRef) -> RawDocument:
        """Pull full content (HTML, PDF, JSON)."""

    @abstractmethod
    def parse(self, raw: RawDocument) -> NormalizedDocument:
        """Extract structured fields."""
```

Register via `@register_source` decorator. The scheduler iterates all registered sources.

---

## Source Inventory

### Tier 1 — Proper APIs
- **openFDA** drug/device/food enforcement → `https://api.fda.gov/`
- **ClinicalTrials.gov v2** → `https://clinicaltrials.gov/api/v2/studies` (v1 retired June 2024)
- **EMA** → downloadable tables at `https://www.ema.europa.eu/en/medicines/download-medicine-data`; ePI at `epi.developer.ema.europa.eu`; GMP via EudraGMDP

### Tier 2 — Structured HTML, stable
- **MHRA** drug safety alerts
- **SAHPRA** → `https://www.sahpra.org.za/safety-updates-and-recalls/`
- **PACTR** → `https://pactr.samrc.ac.za/` (clinical trial backup)

### Tier 3 — Scrape, PDF-heavy, fragile
- **PPB Kenya** alerts → root `https://web.pharmacyboardkenya.org/` (find alerts section)
- **TMDA Tanzania** → `https://www.tmda.go.tz/` (Swahili + English)
- **MCAZ Zimbabwe** → `http://www.mcaz.co.zw/`
- **NDA Uganda** → `https://www.nda.or.ug/`
- **ZAMRA Zambia** → `https://www.zamra.co.zm/`
- **EFDA Ethiopia** → `https://www.efda.gov.et/` (Amharic + English; language detection required)

Before writing any Tier 3 adapter: 30-min manual inspection → document in `docs/sources/<id>.md`.

---

## Implemented Adapters (this PR)

| Source ID | Type | File |
|---|---|---|
| `openfda_drug` | REST API | `src/regulatory/sources/openfda_drug.py` |
| `ppb_ke_alerts` | Scrape + PDF | `src/regulatory/sources/ppb_ke_alerts.py` |

---

## House Rules

1. **Type hints on every function signature.** `mypy --strict` must pass.
2. **Docstrings on every public class and function.** Google style.
3. **No bare `except`.** Catch specific exceptions. Log with context via `structlog`.
4. **No secrets in code.** All config via `.env` or `config/sources.yaml`.
5. **No live network calls in tests.** Use recorded fixtures (vcr.py / pytest-recording).
6. **No `print()` statements.** Use `structlog` everywhere.
7. **No `requests`.** Use `httpx`.
8. **`ruff check` and `ruff format` must pass** before every commit.
9. **≥70% test coverage** on ingestion framework; each adapter needs ≥1 happy-path test.
10. **Respect `robots.txt`.** Log when backing off.
11. **Content hashing:** skip fetch if sha256 matches last-stored hash.
12. **User-Agent:** `"AfiaData Regulatory Monitor (contact: info@afiadata.org)"`.

---

## Operational Guardrails

### 1. Live-source validation is non-negotiable before any PR merges

Every adapter and every framework change must be validated against the live sources before the PR
is merged. Unit tests pass clean on openFDA, PPB, SAHPRA, and the URL-keyed dedup fix — and every
single one surfaced a bug at the live-validation step anyway. This is not coincidence. Regulatory
sites are messy in ways fixtures cannot fully capture, and framework changes interact with real
data in ways unit tests do not exercise.

**Required validation sequence for every PR:**

1. `regulatory db migrate` — apply migrations against the real database.
2. Run each affected adapter with `--since 30d` (or a date that returns results). Note
   `docs_added / docs_skipped / docs_updated`.
3. Re-run immediately. All counts should flip to `docs_added=0`. If not, there is a dedup bug.
4. For any adapter with `check_for_updates=True`: force a hash mismatch via SQL, re-run, confirm
   `docs_updated=1` and a matching row in `document_versions`.
5. Run `alembic downgrade -1 && alembic upgrade head` to verify the migration round-trips cleanly.

### 2. Multi-part prompts require a checklist confirmation before execution

When given a prompt that contains multiple deliverables, list them back as a numbered checklist
with the file each will touch, then **wait for explicit confirmation before writing any code**.
This is a standing protocol, not a per-session preference. It catches missed scope at the cheapest
possible moment.

### 3. Schema migration discipline

When a migration adds a column to `documents` that participates in
`NormalizedDocument.normalized_content_hash()`, the migration **must** backfill
the column for existing rows before applying any NOT NULL constraint. Backfill by
reconstructing `NormalizedDocument` instances from stored fields and calling
`normalized_content_hash()` — the same method the scheduler calls — so the values
are guaranteed to match on the next ingest run.

Skipping the backfill causes the next ingest to see `NULL != new_hash` for every
existing row, inflate `docs_updated` with false-positive update events, and — without
the scheduler null-hash guard — write empty-prior-state rows into `document_versions`.
The scheduler has a defensive guard (`if existing_doc.normalized_hash`) that
suppresses empty-archive writes, but metric pollution from inflated `docs_updated`
counters must be prevented at source.

**Fields that currently participate in the hash (as of migration 0003):**
`source_id`, `source_url`, `document_id`, `title`, `product_names`,
`active_ingredients`, `manufacturers`, `severity`, `date_published`,
`date_effective`, `raw_metadata`.

When the hash recipe changes, update this list and any migration that backfills
the hash.

### 4. Bug-prevention checklist (apply on every adapter and scheduler touch)

| Rule | Why |
|---|---|
| Reuse a single `httpx.AsyncClient` per source run | Per-request clients leak sockets and ignore rate-limit state |
| `robots.txt` check must **fail open** — log and continue on network error | A dead robots.txt must not block ingestion |
| DB write errors inside `except` handlers must be caught separately | An error writing `FetchLog` must not shadow the original exception |
| Date parsing must raise loudly **per record**, not abort the whole run | One malformed date should log an error and `continue`, not kill the session |
| No bare `except` — catch `Exception` at most, always log with `structlog` | Silent swallowing hides bugs that only appear with live data |
| Type hints on every function signature; `mypy --strict` must pass | Catches class-attribute vs instance-attribute mistakes before runtime |

---

## Out of Scope (do NOT build until explicitly tasked)

- Risk engine (repeat-violator, supply-chain join)
- Agent / LLM orchestration beyond PDF fallback
- Streamlit dashboard
- FastAPI service
- Authentication
- Social media adapters
- Any Tier 2/3 adapter beyond `ppb_ke_alerts`
- County procurement data

---

## Key Data Notes

- `data/ppb_pdfs/` — PPB **guideline** PDFs. Do not confuse with recalls.
- `data/processed_texts/` — extracted text from above. Leave in place.
- `data/ppb_recalls_update/` — **does not exist**. Recall data requires a separate scraper.
- `scripts/modal_setup/` — Modal volume/batch/preprocessing scripts. Do not refactor.

---

## Legacy Script Policy

- `main.py` — stays as a 3-line redirect to the typer CLI (`from regulatory.cli import app`).
  Do not delete.
- `scripts/legacy/ppb_pdf_scraper.py` and `scripts/legacy/pdf_preprocessing.py` — original
  working scripts that produced `data/ppb_pdfs/` and `data/processed_texts/`. Their patterns
  are ported into the ingestion framework but they are kept as:
  1. A working fallback if the new adapters have issues during the validation period.
  2. Inline documentation of source-specific quirks (PPB `/download/` subpage pattern,
     filename collision handling, tolerated request cadence) that the new code inherits.
- **Do not remove these files** until the new adapters (`openfda_drug`, `ppb_ke_alerts`)
  have run cleanly in production for a reasonable period. See `scripts/legacy/README.md`.
