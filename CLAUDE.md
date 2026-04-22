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
