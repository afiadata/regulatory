"""Command-line interface for the AfiaData Regulatory Monitor.

Entry point registered as ``regulatory`` in pyproject.toml.

Usage::

    regulatory sources list
    regulatory ingest run --source openfda_drug --since 2024-01-01
    regulatory ingest run --all --since 30d
    regulatory db migrate
    regulatory db reset
    regulatory manufacturers reconcile [--dry-run | --apply]
    regulatory manufacturers review
    regulatory manufacturers show <name>
    regulatory procurement load --version synthetic_v1
    regulatory risk run --as-of 2026-05-18 [--dry-run]
    regulatory risk list [--kind repeat_violator] [--status active]
    regulatory risk show <signal_id>
    regulatory risk explain <signal_id>
    regulatory risk resolve <signal_id> --reason "..."
    regulatory risk suppress <signal_id> --reason "..."
"""

from __future__ import annotations

import asyncio
import csv
import os
import uuid
from datetime import date, datetime
from pathlib import Path

import structlog
import typer
from dotenv import load_dotenv

load_dotenv()

# These must follow load_dotenv() so env vars are set before module-level reads.
import regulatory.sources.openfda_drug  # noqa: E402, F401
import regulatory.sources.ppb_ke_alerts  # noqa: E402, F401
import regulatory.sources.sahpra_recalls  # noqa: E402, F401
from regulatory.ingestion.registry import all_sources  # noqa: E402
from regulatory.ingestion.scheduler import run  # noqa: E402

log = structlog.get_logger(__name__)

app = typer.Typer(
    name="regulatory",
    help="AfiaData Regulatory Monitor — multi-jurisdiction ingestion CLI.",
    no_args_is_help=True,
)

sources_app = typer.Typer(help="Manage source adapters.")
ingest_app = typer.Typer(help="Run ingestion jobs.")
db_app = typer.Typer(help="Database management commands.")
manufacturers_app = typer.Typer(help="Manufacturer canonicalization commands.")
procurement_app = typer.Typer(help="Procurement data management.")
risk_app = typer.Typer(help="Risk signal detection and management.")

app.add_typer(sources_app, name="sources")
app.add_typer(ingest_app, name="ingest")
app.add_typer(db_app, name="db")
app.add_typer(manufacturers_app, name="manufacturers")
app.add_typer(procurement_app, name="procurement")
app.add_typer(risk_app, name="risk")


# ---------------------------------------------------------------------------
# regulatory sources list
# ---------------------------------------------------------------------------


@sources_app.command("list")
def sources_list() -> None:
    """List all registered source adapters."""
    registry = all_sources()
    if not registry:
        typer.echo("No sources registered.")
        raise typer.Exit(0)

    typer.echo(f"\n{'Source ID':<24} {'Jurisdiction':<16} {'Document Types'}")
    typer.echo("-" * 70)
    for sid, cls in sorted(registry.items()):
        types = ", ".join(dt.value for dt in cls.document_types)
        typer.echo(f"{sid:<24} {cls.jurisdiction:<16} {types}")
    typer.echo()


# ---------------------------------------------------------------------------
# regulatory ingest run
# ---------------------------------------------------------------------------


@ingest_app.command("run")
def ingest_run(
    source: str | None = typer.Option(None, "--source", "-s", help="Source ID to ingest."),
    all_sources_flag: bool = typer.Option(False, "--all", help="Ingest all registered sources."),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Ingest documents newer than this value. "
        "Accepts ISO date (2024-01-01), datetime, or relative (30d).",
    ),
) -> None:
    """Run the ingestion pipeline for one or all sources."""
    if not source and not all_sources_flag:
        typer.echo(
            "Specify --source <id> or --all. Run 'regulatory sources list' to see options.",
            err=True,
        )
        raise typer.Exit(1)

    if source and all_sources_flag:
        typer.echo("Use --source or --all, not both.", err=True)
        raise typer.Exit(1)

    if source and source not in all_sources():
        typer.echo(
            f"Unknown source '{source}'. Run 'regulatory sources list' to see options.",
            err=True,
        )
        raise typer.Exit(1)

    since_value: str | datetime | None = since
    typer.echo(f"Starting ingestion: source={'all' if all_sources_flag else source}, since={since}")

    try:
        run(source_id=source, since=since_value)
    except KeyError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except Exception as exc:
        log.error("ingest_failed", error=str(exc))
        typer.echo(f"Ingestion failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo("Ingestion complete.")


# ---------------------------------------------------------------------------
# regulatory db migrate / reset
# ---------------------------------------------------------------------------


@db_app.command("migrate")
def db_migrate() -> None:
    """Run Alembic migrations to the latest revision."""
    import subprocess

    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        capture_output=False,
    )
    if result.returncode != 0:
        raise typer.Exit(result.returncode)
    typer.echo("Database migrated to head.")


@db_app.command("reset")
def db_reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Drop and recreate all tables. Destructive — all data is lost."""
    if not yes:
        confirm = typer.confirm(
            "This will DROP all tables and recreate them. Are you sure?",
            default=False,
        )
        if not confirm:
            typer.echo("Aborted.")
            raise typer.Exit(0)

    import subprocess

    result = subprocess.run(["alembic", "downgrade", "base"], capture_output=False)
    if result.returncode != 0:
        raise typer.Exit(result.returncode)

    result = subprocess.run(["alembic", "upgrade", "head"], capture_output=False)
    if result.returncode != 0:
        raise typer.Exit(result.returncode)

    typer.echo("Database reset complete.")


# ---------------------------------------------------------------------------
# regulatory manufacturers reconcile / review / show
# ---------------------------------------------------------------------------


@manufacturers_app.command("reconcile")
def manufacturers_reconcile(
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Preview without writing."),
) -> None:
    """Reconcile raw manufacturer strings into the canonical manufacturers table."""
    from regulatory.db.session import get_session
    from regulatory.risk.canonicalize import reconcile_manufacturers

    async def _run() -> None:
        async with get_session() as session:
            results = await reconcile_manufacturers(session, dry_run=dry_run)
        action_counts: dict[str, int] = {}
        for r in results:
            action_counts[r.action] = action_counts.get(r.action, 0) + 1
        typer.echo(
            f"Reconcile {'(dry-run) ' if dry_run else ''}complete: "
            + ", ".join(f"{k}={v}" for k, v in sorted(action_counts.items()))
        )

    asyncio.run(_run())


@manufacturers_app.command("review")
def manufacturers_review() -> None:
    """Print unmerged manufacturer candidates from the review file."""
    from regulatory.risk.canonicalize import get_unmerged_candidates

    candidates = get_unmerged_candidates()
    if not candidates:
        typer.echo("No unmerged candidates.")
        return
    typer.echo(f"\n{len(candidates)} unmerged candidate(s):\n")
    for c in candidates:
        typer.echo(
            f"  {c.get('raw_name')!r:40s} → {c.get('candidate_canonical')!r} "
            f"(score={c.get('score', 0):.1f}, confidence={c.get('confidence', 0):.2f})"
        )


@manufacturers_app.command("show")
def manufacturers_show(
    name: str = typer.Argument(..., help="Canonical manufacturer name."),
) -> None:
    """Show canonical entry, aliases, and linked document count for a manufacturer."""
    from regulatory.db.session import get_session
    from regulatory.risk.canonicalize import show_manufacturer

    async def _run() -> None:
        async with get_session() as session:
            info = await show_manufacturer(session, name)
        if info is None:
            typer.echo(f"Manufacturer not found: {name!r}", err=True)
            raise typer.Exit(1)
        typer.echo(f"\nCanonical name : {info['canonical_name']}")
        typer.echo(f"Confidence     : {info['confidence']}")
        typer.echo(f"Countries      : {', '.join(info['countries']) or '(none)'}")
        typer.echo(f"Linked docs    : {info['linked_documents']}")
        typer.echo(f"Created        : {info['created_at']}")
        typer.echo(f"Updated        : {info['updated_at']}")
        aliases = info["aliases"]
        if aliases:
            typer.echo("\nAliases:")
            for a in aliases:
                typer.echo(f"  - {a}")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# regulatory procurement load
# ---------------------------------------------------------------------------


@procurement_app.command("load")
def procurement_load(
    version: str = typer.Option("synthetic_v1", "--version", help="Data source version tag."),
    data_dir: str = typer.Option(
        "data/synthetic/procurement_v1",
        "--data-dir",
        help="Directory containing counties.csv, suppliers.csv, county_supply.csv.",
    ),
) -> None:
    """Load procurement CSVs into the database.

    Validates that SUM(share_pct) per (county, ingredient) is approximately
    100 ± 5 before committing.  Rows carry data_source = version.
    """
    from regulatory.db.models import County, CountySupply, Manufacturer, Supplier
    from regulatory.db.session import get_session

    base = Path(data_dir)
    counties_csv = base / "counties.csv"
    suppliers_csv = base / "suppliers.csv"
    supply_csv = base / "county_supply.csv"

    for p in [counties_csv, suppliers_csv, supply_csv]:
        if not p.exists():
            typer.echo(f"Missing file: {p}", err=True)
            raise typer.Exit(1)

    async def _run() -> None:
        async with get_session() as session:
            # Load counties.
            county_id_map: dict[str, uuid.UUID] = {}
            with counties_csv.open(encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    from sqlalchemy import select as _sa_select
                    existing = await session.execute(
                        _sa_select(County).where(County.name == row["name"])
                    )
                    county = existing.scalar_one_or_none()
                    if county is None:
                        county = County(
                            id=uuid.UUID(row["id"]),
                            name=row["name"],
                            region=row["region"],
                            population=int(row["population"]) if row.get("population") else None,
                            health_facilities=(
                            int(row["health_facilities"]) if row.get("health_facilities") else None
                        ),
                        )
                        session.add(county)
                    county_id_map[row["name"]] = county.id

            await session.flush()

            # Load suppliers (resolve manufacturer_id by canonical name).
            supplier_id_map: dict[str, uuid.UUID] = {}
            with suppliers_csv.open(encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    mfr_id: uuid.UUID | None = None
                    if row.get("name") and row["role"] == "manufacturer":
                        from sqlalchemy import select as _sa_select
                        mfr_result = await session.execute(
                            _sa_select(Manufacturer).where(
                                Manufacturer.canonical_name == row["name"]
                            )
                        )
                        mfr = mfr_result.scalar_one_or_none()
                        if mfr:
                            mfr_id = mfr.id
                    supplier = Supplier(
                        id=uuid.UUID(row["id"]),
                        name=row["name"],
                        role=row["role"],
                        manufacturer_id=mfr_id,
                        countries_served=[
                            c.strip()
                            for c in row.get("countries_served", "").split(",")
                            if c.strip()
                        ],
                    )
                    session.add(supplier)
                    supplier_id_map[row["id"]] = uuid.UUID(row["id"])

            await session.flush()

            # Load county_supply and validate share sums.
            supply_rows: list[dict[str, str]] = []
            with supply_csv.open(encoding="utf-8") as fh:
                supply_rows = list(csv.DictReader(fh))

            sums: dict[tuple[str, str], float] = {}
            for row in supply_rows:
                key = (row["county_id"], row["active_ingredient"])
                sums[key] = sums.get(key, 0.0) + float(row["share_pct"])

            violations = [k for k, v in sums.items() if abs(v - 100.0) > 5.0]
            if violations:
                typer.echo(
                    f"WARNING: {len(violations)} county-ingredient pairs "
                    "have share_pct sum outside 100±5.",
                    err=True,
                )

            for row in supply_rows:
                cs = CountySupply(
                    id=uuid.UUID(row["id"]),
                    county_id=uuid.UUID(row["county_id"]),
                    supplier_id=uuid.UUID(row["supplier_id"]),
                    active_ingredient=row["active_ingredient"],
                    share_pct=row["share_pct"],
                    lead_time_days=int(row["lead_time_days"]),
                    contract_start=(
                        date.fromisoformat(row["contract_start"])
                        if row.get("contract_start") else None
                    ),
                    contract_end=(
                        date.fromisoformat(row["contract_end"])
                        if row.get("contract_end") else None
                    ),
                    data_source=version,
                )
                session.add(cs)

            await session.commit()
            typer.echo(
                f"Loaded {len(county_id_map)} counties, "
                f"{len(supplier_id_map)} suppliers, "
                f"{len(supply_rows)} county_supply rows "
                f"(data_source={version!r})."
            )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# regulatory risk run / list / show / explain / resolve / suppress
# ---------------------------------------------------------------------------


@risk_app.command("run")
def risk_run(
    as_of_str: str | None = typer.Option(
        None,
        "--as-of",
        help="Compute signals as of this date (YYYY-MM-DD). Defaults to today.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute without writing."),
) -> None:
    """Run the risk engine and persist signals for all three rules."""
    from regulatory.db.session import get_session
    from regulatory.risk.config import load_config
    from regulatory.risk.corroboration import detect_cross_source_corroboration
    from regulatory.risk.persist import persist_signals
    from regulatory.risk.repeat_violator import detect_repeat_violators
    from regulatory.risk.supply_chain import detect_supply_chain_exposure

    as_of = date.fromisoformat(as_of_str) if as_of_str else date.today()

    async def _run() -> None:
        config = load_config()
        async with get_session() as session:
            rv_candidates = await detect_repeat_violators(
                session, as_of=as_of, config=config.repeat_violator
            )
            sc_candidates = await detect_supply_chain_exposure(
                session,
                as_of=as_of,
                config=config.supply_chain_exposure,
                repeat_violator_signals=rv_candidates,
            )
            corr_candidates = await detect_cross_source_corroboration(
                session, as_of=as_of, config=config.cross_source_corroboration
            )
            counts = await persist_signals(
                session,
                list(rv_candidates) + list(sc_candidates),
                config=config,
                as_of=as_of,
                corroboration_candidates=corr_candidates,
                dry_run=dry_run,
                actor=os.environ.get("USER", "cli"),
            )
        action = "DRY RUN" if dry_run else "complete"
        typer.echo(
            f"Risk run {action} (as_of={as_of}): "
            + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )

    asyncio.run(_run())


@risk_app.command("list")
def risk_list(
    kind: str | None = typer.Option(None, "--kind", help="Filter by signal kind."),
    status: str = typer.Option("active", "--status", help="Filter by status."),
) -> None:
    """List risk signals."""
    from sqlalchemy import select as sa_select

    from regulatory.db.models import RiskSignal
    from regulatory.db.session import get_session
    from regulatory.risk.config import load_config

    async def _run() -> None:
        config = load_config()
        current_config_hash = config.config_hash
        async with get_session() as session:
            q = sa_select(RiskSignal).where(RiskSignal.status == status)
            if kind:
                q = q.where(RiskSignal.kind == kind)
            result = await session.execute(q)
            signals = result.scalars().all()

        if not signals:
            typer.echo("No signals found.")
            return

        header = f"\n{'ID':<10} {'Kind':<28} {'Sev':<10} {'Status':<12} {'Ingredient':<28} Stale?"
        typer.echo(header)
        typer.echo("-" * 100)
        for s in signals:
            stale = "YES" if s.evidence.get("config_hash") != current_config_hash else ""
            sig_id = str(s.id)[:8]
            ingredient = (s.active_ingredient or "")[:26]
            typer.echo(
                f"{sig_id:<10} {s.kind:<26} {s.severity:<10} {s.status:<12} "
                f"{ingredient:<26} {stale}"
            )

    asyncio.run(_run())


@risk_app.command("show")
def risk_show(signal_id: str = typer.Argument(..., help="Signal UUID.")) -> None:
    """Show full details and evidence for a risk signal."""
    from regulatory.db.models import RiskSignal
    from regulatory.db.session import get_session
    from regulatory.risk.persist import explain_signal

    async def _run() -> None:
        async with get_session() as session:
            sig = await session.get(RiskSignal, uuid.UUID(signal_id))
        if sig is None:
            typer.echo(f"Signal not found: {signal_id}", err=True)
            raise typer.Exit(1)
        typer.echo(explain_signal(sig))

    asyncio.run(_run())


@risk_app.command("explain")
def risk_explain(signal_id: str = typer.Argument(..., help="Signal UUID.")) -> None:
    """Human-readable trace of which rule, thresholds, and documents triggered this signal."""
    from regulatory.db.models import RiskSignal
    from regulatory.db.session import get_session
    from regulatory.risk.persist import explain_signal

    async def _run() -> None:
        async with get_session() as session:
            sig = await session.get(RiskSignal, uuid.UUID(signal_id))
        if sig is None:
            typer.echo(f"Signal not found: {signal_id}", err=True)
            raise typer.Exit(1)
        typer.echo(explain_signal(sig))

    asyncio.run(_run())


@risk_app.command("resolve")
def risk_resolve(
    signal_id: str = typer.Argument(..., help="Signal UUID."),
    reason: str = typer.Option(..., "--reason", help="Reason for resolution."),
) -> None:
    """Resolve an active risk signal."""
    from regulatory.db.session import get_session
    from regulatory.risk.persist import transition_signal

    async def _run() -> None:
        async with get_session() as session:
            await transition_signal(
                session,
                uuid.UUID(signal_id),
                "resolved",
                actor=os.environ.get("USER", "cli"),
                reason=reason,
            )
        typer.echo(f"Signal {signal_id[:8]}... resolved.")

    asyncio.run(_run())


@risk_app.command("suppress")
def risk_suppress(
    signal_id: str = typer.Argument(..., help="Signal UUID."),
    reason: str = typer.Option(..., "--reason", help="Reason for suppression."),
) -> None:
    """Suppress an active risk signal (won't be re-fired until manually un-suppressed)."""
    from regulatory.db.session import get_session
    from regulatory.risk.persist import transition_signal

    async def _run() -> None:
        async with get_session() as session:
            await transition_signal(
                session,
                uuid.UUID(signal_id),
                "suppressed",
                actor=os.environ.get("USER", "cli"),
                reason=reason,
            )
        typer.echo(f"Signal {signal_id[:8]}... suppressed.")

    asyncio.run(_run())


if __name__ == "__main__":
    app()
