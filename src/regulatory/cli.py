"""Command-line interface for the AfiaData Regulatory Monitor.

Entry point registered as ``regulatory`` in pyproject.toml.

Usage::

    regulatory sources list
    regulatory ingest run --source openfda_drug --since 2024-01-01
    regulatory ingest run --all --since 30d
    regulatory db migrate
    regulatory db reset
"""

from __future__ import annotations

from datetime import datetime

import structlog
import typer
from dotenv import load_dotenv

load_dotenv()

# These must follow load_dotenv() so env vars are set before module-level reads.
import regulatory.sources.openfda_drug  # noqa: E402, F401
import regulatory.sources.ppb_ke_alerts  # noqa: E402, F401
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

app.add_typer(sources_app, name="sources")
app.add_typer(ingest_app, name="ingest")
app.add_typer(db_app, name="db")


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
# regulatory db migrate
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


# ---------------------------------------------------------------------------
# regulatory db reset
# ---------------------------------------------------------------------------


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


if __name__ == "__main__":
    app()
