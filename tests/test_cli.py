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


class TestDbDedup:
    """Tests for `regulatory db dedup`."""

    # ── fixtures ────────────────────────────────────────────────────────────

    def _make_doc_mock(self, source_id: str, url: str, created_offset: int = 0) -> object:
        """Build a MagicMock standing in for a Document ORM row."""
        import uuid
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        doc = MagicMock()
        doc.id = uuid.uuid4()
        doc.source_id = source_id
        doc.source_url = url
        doc.created_at = datetime(2026, 5, 15 - created_offset, tzinfo=timezone.utc)
        doc.normalized_hash = f"hash_{created_offset}"
        doc.raw_text = f"text_{created_offset}"
        doc.raw_metadata = {"offset": created_offset}
        return doc

    def _mock_dedup_session(self, dup_groups: list, rows_per_group: list) -> tuple:  # type: ignore[type-arg]
        """Return (mock_session, mock_ctx) whose execute() yields the supplied data."""
        from unittest.mock import AsyncMock, MagicMock

        # Build side_effect list: first call → groups, then one call per group
        def _groups_result() -> MagicMock:
            r = MagicMock()
            r.all.return_value = dup_groups
            return r

        def _rows_result(rows: list) -> MagicMock:  # type: ignore[type-arg]
            scalars = MagicMock()
            scalars.all.return_value = rows
            r = MagicMock()
            r.scalars.return_value = scalars
            return r

        side_effects = [_groups_result()] + [_rows_result(rows) for rows in rows_per_group]

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=side_effects)
        mock_session.add = MagicMock()
        mock_session.delete = AsyncMock()
        mock_session.commit = AsyncMock()

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        return mock_session, mock_ctx

    # ── test 11 ─────────────────────────────────────────────────────────────

    def test_dedup_dry_run_reports_without_modifying(self) -> None:
        """--dry-run reports 'would consolidate N rows' and makes no DB writes."""
        from unittest.mock import patch

        keeper = self._make_doc_mock("sahpra_recalls", "https://example.com/doc/", 0)
        dup1 = self._make_doc_mock("sahpra_recalls", "https://example.com/doc/", 1)
        dup2 = self._make_doc_mock("sahpra_recalls", "https://example.com/doc/", 2)

        dup_groups = [("sahpra_recalls", "https://example.com/doc/")]
        rows_per_group = [[keeper, dup1, dup2]]
        mock_session, mock_ctx = self._mock_dedup_session(dup_groups, rows_per_group)

        with patch("regulatory.cli.get_session", return_value=mock_ctx):
            result = runner.invoke(
                app, ["db", "dedup", "--source", "sahpra_recalls", "--dry-run"]
            )

        assert result.exit_code == 0
        assert "would consolidate 2 rows" in result.output
        mock_session.delete.assert_not_called()
        mock_session.commit.assert_not_called()

    # ── test 12 ─────────────────────────────────────────────────────────────

    def test_dedup_executes_and_archives_to_versions(self) -> None:
        """Without --dry-run: duplicates archived to document_versions, then deleted."""
        from unittest.mock import patch

        from regulatory.db.models import DocumentVersion

        keeper = self._make_doc_mock("sahpra_recalls", "https://example.com/doc/", 0)
        dup1 = self._make_doc_mock("sahpra_recalls", "https://example.com/doc/", 1)
        dup2 = self._make_doc_mock("sahpra_recalls", "https://example.com/doc/", 2)

        dup_groups = [("sahpra_recalls", "https://example.com/doc/")]
        rows_per_group = [[keeper, dup1, dup2]]
        mock_session, mock_ctx = self._mock_dedup_session(dup_groups, rows_per_group)

        with patch("regulatory.cli.get_session", return_value=mock_ctx):
            result = runner.invoke(app, ["db", "dedup", "--source", "sahpra_recalls"])

        assert result.exit_code == 0
        added = [c.args[0] for c in mock_session.add.call_args_list]
        version_rows = [o for o in added if isinstance(o, DocumentVersion)]
        assert len(version_rows) == 2
        assert mock_session.delete.call_count == 2
        mock_session.commit.assert_called_once()

    # ── test 13 ─────────────────────────────────────────────────────────────

    def test_dedup_all_iterates_sources(self) -> None:
        """--all processes duplicates across multiple source IDs."""
        from unittest.mock import patch

        keeper_a = self._make_doc_mock("source_a", "https://a.example.com/doc/", 0)
        dup_a = self._make_doc_mock("source_a", "https://a.example.com/doc/", 1)
        keeper_b = self._make_doc_mock("source_b", "https://b.example.com/doc/", 0)
        dup_b = self._make_doc_mock("source_b", "https://b.example.com/doc/", 1)

        dup_groups = [
            ("source_a", "https://a.example.com/doc/"),
            ("source_b", "https://b.example.com/doc/"),
        ]
        rows_per_group = [[keeper_a, dup_a], [keeper_b, dup_b]]
        mock_session, mock_ctx = self._mock_dedup_session(dup_groups, rows_per_group)

        with patch("regulatory.cli.get_session", return_value=mock_ctx):
            result = runner.invoke(app, ["db", "dedup", "--all"])

        assert result.exit_code == 0
        assert "source_a" in result.output
        assert "source_b" in result.output
        assert mock_session.delete.call_count == 2
