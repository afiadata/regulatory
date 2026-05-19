"""Smoke tests — every CLI subcommand must be registered and show --help without error.

These tests do NOT connect to a database or network. They verify that typer wires up
each subcommand correctly and that --help exits 0 with the expected name in the output.
"""

from __future__ import annotations

from typer.testing import CliRunner

from regulatory.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _help_ok(args: list[str]) -> None:
    """Assert that ``args + [--help]`` exits 0 and prints something useful."""
    result = runner.invoke(app, args + ["--help"])
    assert result.exit_code == 0, (
        f"'{' '.join(args)} --help' exited {result.exit_code}:\n{result.output}"
    )
    assert result.output.strip(), f"'{' '.join(args)} --help' produced no output"


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def test_top_level_help() -> None:
    _help_ok([])


# ---------------------------------------------------------------------------
# regulatory sources
# ---------------------------------------------------------------------------


def test_sources_list_help() -> None:
    _help_ok(["sources", "list"])


# ---------------------------------------------------------------------------
# regulatory ingest
# ---------------------------------------------------------------------------


def test_ingest_run_help() -> None:
    _help_ok(["ingest", "run"])


# ---------------------------------------------------------------------------
# regulatory manufacturers
# ---------------------------------------------------------------------------


def test_manufacturers_reconcile_help() -> None:
    _help_ok(["manufacturers", "reconcile"])


def test_manufacturers_review_help() -> None:
    _help_ok(["manufacturers", "review"])


def test_manufacturers_show_help() -> None:
    _help_ok(["manufacturers", "show"])


# ---------------------------------------------------------------------------
# regulatory procurement
# ---------------------------------------------------------------------------


def test_procurement_load_help() -> None:
    _help_ok(["procurement", "load"])


# ---------------------------------------------------------------------------
# regulatory risk
# ---------------------------------------------------------------------------


def test_risk_run_help() -> None:
    _help_ok(["risk", "run"])


def test_risk_list_help() -> None:
    _help_ok(["risk", "list"])


def test_risk_show_help() -> None:
    _help_ok(["risk", "show"])


def test_risk_explain_help() -> None:
    _help_ok(["risk", "explain"])


def test_risk_resolve_help() -> None:
    _help_ok(["risk", "resolve"])


def test_risk_suppress_help() -> None:
    _help_ok(["risk", "suppress"])


# ---------------------------------------------------------------------------
# Functional: sources list prints adapter table without a DB
# ---------------------------------------------------------------------------


def test_sources_list_prints_adapters() -> None:
    result = runner.invoke(app, ["sources", "list"])
    assert result.exit_code == 0, result.output
    assert "openfda_drug" in result.output
    assert "ppb_ke_alerts" in result.output


# ---------------------------------------------------------------------------
# Functional: ingest run errors cleanly on bad source ID
# ---------------------------------------------------------------------------


def test_ingest_run_unknown_source_exits_nonzero() -> None:
    result = runner.invoke(app, ["ingest", "run", "--source", "nonexistent_source_xyz"])
    assert result.exit_code != 0
