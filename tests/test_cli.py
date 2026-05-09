"""Tests for the typer CLI entry point.

Uses typer's CliRunner — no live network or DB calls.
"""

from __future__ import annotations

from typer.testing import CliRunner

from regulatory.cli import app

runner = CliRunner()


class TestSourcesList:
    """Tests for `regulatory sources list`."""

    def test_sources_list_shows_registered_sources(self) -> None:
        """sources list prints at least the two built-in adapters."""
        result = runner.invoke(app, ["sources", "list"])
        assert result.exit_code == 0
        assert "openfda_drug" in result.output
        assert "ppb_ke_alerts" in result.output

    def test_sources_list_shows_jurisdictions(self) -> None:
        """sources list includes jurisdiction codes."""
        result = runner.invoke(app, ["sources", "list"])
        assert result.exit_code == 0
        assert "US" in result.output
        assert "KE" in result.output

    def test_sources_list_shows_document_types(self) -> None:
        """sources list includes document type values."""
        result = runner.invoke(app, ["sources", "list"])
        assert result.exit_code == 0
        assert "recall" in result.output or "alert" in result.output


class TestIngestRun:
    """Tests for `regulatory ingest run` argument validation."""

    def test_ingest_run_requires_source_or_all(self) -> None:
        """ingest run without --source or --all exits non-zero."""
        result = runner.invoke(app, ["ingest", "run"])
        assert result.exit_code != 0

    def test_ingest_run_source_and_all_is_exclusive(self) -> None:
        """--source and --all together exits non-zero."""
        result = runner.invoke(app, ["ingest", "run", "--source", "openfda_drug", "--all"])
        assert result.exit_code != 0

    def test_ingest_run_unknown_source_exits_nonzero(self) -> None:
        """--source with an unregistered ID exits non-zero."""
        result = runner.invoke(app, ["ingest", "run", "--source", "no_such_source_xyz"])
        assert result.exit_code != 0
