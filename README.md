# AfiaData Regulatory Monitor

Multi-jurisdiction regulatory intelligence platform. Ingests product alerts, recalls, and
enforcement actions from African and global health authorities, normalizes them into a shared
schema, and stores them in Postgres for cross-jurisdictional risk analysis.

## Quick start

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (`pip install uv` or `brew install uv`)
- Docker + Docker Compose (for local Postgres)

### 1. Clone and install

```bash
git clone <repo-url> regulatory
cd regulatory
uv sync
```

### 2. Configure environment

```bash
cp config/.env.example .env
# Edit .env — at minimum set:
#   ANTHROPIC_API_KEY=sk-ant-...
#   DATABASE_URL=postgresql://regulatory:regulatory@localhost:5432/regulatory
```

### 3. Start Postgres

```bash
docker compose up -d postgres
# Wait for the healthcheck to pass (~5s)
docker compose ps
```

### 4. Run migrations

```bash
uv run regulatory db migrate
```

### 5. Ingest data

```bash
# openFDA drug enforcement — last 30 days
uv run regulatory ingest run --source openfda_drug --since 30d

# PPB Kenya product alerts — everything
uv run regulatory ingest run --source ppb_ke_alerts

# All registered sources — last 7 days
uv run regulatory ingest run --all --since 7d
```

### 6. List registered sources

```bash
uv run regulatory sources list
```

---

## Development

### Run tests

```bash
uv run pytest
```

### Type checking

```bash
uv run mypy --strict src/
```

### Linting and formatting

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

### Pre-commit hooks

```bash
uv run pre-commit install
# Runs ruff + mypy on every commit
```

---

## Architecture overview

```
src/regulatory/
  models.py               # Pydantic NormalizedDocument schema
  cli.py                  # typer CLI entry point
  ingestion/
    base.py               # RegulatorySource ABC
    registry.py           # @register_source decorator
    http.py               # Rate-limited httpx client with retry + robots.txt
    pdf.py                # pdfplumber -> PyMuPDF -> Anthropic fallback chain
    scheduler.py          # Loops over registered sources
  sources/
    openfda_drug.py       # openFDA drug enforcement REST adapter
    ppb_ke_alerts.py      # PPB Kenya product alerts scraper
  db/
    models.py             # SQLAlchemy ORM (documents, fetch_log, ...)
    session.py            # Async session factory
  inn/
    inn_lookup.json       # INN normalization alias table
  llm/
    client.py             # Anthropic SDK wrapper with caching
    pdf_extract.py        # Structured PDF -> schema extraction

config/
  sources.yaml            # Per-domain rate limits and schedules
  .env.example            # Environment variable template

alembic/                  # Database migration scripts
docs/
  sources.md              # Full source inventory and implementation notes
```

## Adding a new source adapter

See [docs/sources.md](docs/sources.md) for the full inventory and step-by-step guide.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `ANTHROPIC_API_KEY` | Yes | For PDF fallback extraction |
| `ANTHROPIC_MODEL` | No | Default: `claude-sonnet-4-6` |
| `OPENFDA_API_KEY` | No | Raises rate limit to 120k/day |

## What is not built yet

- Risk engine (repeat-violator analysis, supply-chain joins)
- Claude-powered natural language agent
- Streamlit dashboard
- Additional Tier 2/3 adapters (SAHPRA, TMDA, MCAZ, NDA, ZAMRA, EFDA)

See [docs/sources.md](docs/sources.md) for the roadmap.
