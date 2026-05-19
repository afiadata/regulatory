"""Tests for the ingestion framework core: registry, models, http, pdf, INN."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

import pytest

from regulatory.ingestion.registry import (
    _REGISTRY,
    all_sources,
    get_source,
    register_source,
)
from regulatory.ingestion.scheduler import _parse_since
from regulatory.inn import normalize, normalize_list
from regulatory.models import (
    DocumentRef,
    DocumentType,
    NormalizedDocument,
    RawDocument,
    Severity,
)

# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestRegistry:
    """Tests for the @register_source decorator and registry lookups."""

    def test_register_and_retrieve(self) -> None:
        """A registered source can be retrieved by source_id."""
        from regulatory.ingestion.base import RegulatorySource

        # Use a unique ID to avoid collision with real adapters
        @register_source
        class _TestSource(RegulatorySource):
            source_id = "_test_registry_source"
            jurisdiction = "GLOBAL"
            document_types = [DocumentType.alert]

            async def discover(self, since=None):  # type: ignore[override]
                return
                yield

            async def fetch(self, ref):  # type: ignore[override]
                ...

            def parse(self, raw):  # type: ignore[override]
                ...

        cls = get_source("_test_registry_source")
        assert cls is _TestSource
        # Cleanup
        del _REGISTRY["_test_registry_source"]

    def test_duplicate_raises(self) -> None:
        """Registering two sources with the same ID raises ValueError."""
        from regulatory.ingestion.base import RegulatorySource

        @register_source
        class _DupA(RegulatorySource):
            source_id = "_dup_test"
            jurisdiction = "GLOBAL"
            document_types = []

            async def discover(self, since=None):  # type: ignore[override]
                return
                yield

            async def fetch(self, ref):  # type: ignore[override]
                ...

            def parse(self, raw):  # type: ignore[override]
                ...

        with pytest.raises(ValueError, match="already registered"):

            @register_source
            class _DupB(RegulatorySource):
                source_id = "_dup_test"
                jurisdiction = "GLOBAL"
                document_types = []

                async def discover(self, since=None):  # type: ignore[override]
                    return
                    yield

                async def fetch(self, ref):  # type: ignore[override]
                    ...

                def parse(self, raw):  # type: ignore[override]
                    ...

        # Cleanup
        del _REGISTRY["_dup_test"]

    def test_missing_source_raises_key_error(self) -> None:
        """Looking up an unregistered source_id raises KeyError."""
        with pytest.raises(KeyError, match="no_such_source"):
            get_source("no_such_source")

    def test_all_sources_returns_dict(self) -> None:
        """all_sources() returns a dict snapshot."""
        result = all_sources()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    """Tests for Pydantic schema validation."""

    def test_document_ref_basic(self) -> None:
        """DocumentRef accepts minimal valid input."""
        ref = DocumentRef(
            source_id="test",
            url="https://example.com/doc.pdf",  # type: ignore[arg-type]
        )
        assert ref.source_id == "test"

    def test_raw_document_auto_hash(self) -> None:
        """RawDocument auto-computes source_hash when not provided."""
        content = b"hello pdf"
        ref = DocumentRef(source_id="x", url="https://example.com/x.pdf")  # type: ignore[arg-type]
        raw = RawDocument(
            ref=ref,
            content=content,
            content_type="application/pdf",
            source_hash="",
            fetched_at=datetime.now(tz=timezone.utc),
        )
        assert raw.source_hash == hashlib.sha256(content).hexdigest()

    def test_normalized_document_jurisdiction_validation(self) -> None:
        """NormalizedDocument accepts valid jurisdiction codes."""
        doc = NormalizedDocument(
            source_id="test",
            source_url="https://example.com/doc",  # type: ignore[arg-type]
            source_hash="abc123",
            jurisdiction="KE",
            document_type=DocumentType.alert,
            title="Test",
            date_published=date(2024, 1, 1),
            extracted_at=datetime.now(tz=timezone.utc),
        )
        assert doc.jurisdiction == "KE"

    def test_normalized_document_rejects_bad_jurisdiction(self) -> None:
        """NormalizedDocument raises ValueError for invalid jurisdiction."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            NormalizedDocument(
                source_id="test",
                source_url="https://example.com/doc",  # type: ignore[arg-type]
                source_hash="abc",
                jurisdiction="BADCODE",
                document_type=DocumentType.alert,
                title="Test",
                date_published=date(2024, 1, 1),
                extracted_at=datetime.now(tz=timezone.utc),
            )

    def test_severity_enum_values(self) -> None:
        """Severity enum has the expected string values."""
        assert Severity.class_1.value == "class_1"
        assert Severity.class_3.value == "class_3"

    def test_document_type_enum(self) -> None:
        """All expected DocumentType values exist."""
        types = {dt.value for dt in DocumentType}
        assert "recall" in types
        assert "alert" in types
        assert "guideline" in types


# ---------------------------------------------------------------------------
# INN normalization tests
# ---------------------------------------------------------------------------


class TestINN:
    """Tests for INN lookup and normalization."""

    def test_known_alias(self) -> None:
        """paracetamol normalizes to acetaminophen."""
        assert normalize("paracetamol") == "acetaminophen"

    def test_case_insensitive(self) -> None:
        """Normalization is case-insensitive."""
        assert normalize("Paracetamol") == "acetaminophen"
        assert normalize("PARACETAMOL") == "acetaminophen"

    def test_unknown_passthrough(self) -> None:
        """Unknown ingredient names are returned unchanged."""
        assert normalize("zoflurbigen") == "zoflurbigen"

    def test_normalize_list(self) -> None:
        """normalize_list returns (normalized, raw) tuple."""
        normalized, raw = normalize_list(["amoxycillin", "paracetamol"])
        assert normalized == ["amoxicillin", "acetaminophen"]
        assert raw == ["amoxycillin", "paracetamol"]

    def test_normalize_list_filters_empty(self) -> None:
        """normalize_list strips empty strings."""
        normalized, raw = normalize_list(["", "  ", "aspirin"])
        assert len(raw) == 1
        assert raw[0] == "aspirin"


# ---------------------------------------------------------------------------
# Scheduler parse_since tests
# ---------------------------------------------------------------------------


class TestParseSince:
    """Tests for the _parse_since helper."""

    def test_none_returns_none(self) -> None:
        """None input returns None."""
        assert _parse_since(None) is None

    def test_relative_days(self) -> None:
        """'30d' returns a datetime approximately 30 days ago."""
        result = _parse_since("30d")
        assert result is not None
        delta = datetime.now(tz=timezone.utc) - result
        assert 29 < delta.days <= 31

    def test_iso_string(self) -> None:
        """ISO date string is parsed correctly."""
        result = _parse_since("2024-01-15")
        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15

    def test_datetime_passthrough(self) -> None:
        """A naive datetime object is made UTC-aware."""
        dt = datetime(2024, 3, 1, 0, 0, 0)
        result = _parse_since(dt)
        assert result is not None
        assert result.tzinfo is not None
        assert result.year == 2024

    def test_aware_datetime_returned_unchanged(self) -> None:
        """A tz-aware datetime is returned as-is."""
        dt = datetime(2024, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
        result = _parse_since(dt)
        assert result is dt


# ---------------------------------------------------------------------------
# Scheduler run() tests
# ---------------------------------------------------------------------------


class TestSchedulerRun:
    """Tests for the scheduler run() entry point."""

    def test_run_calls_source(self) -> None:
        """run() invokes _run_source for a single source ID."""
        from unittest.mock import patch

        from regulatory.ingestion.scheduler import run

        async def _noop(source_id: str, since: object) -> None:
            pass

        with patch("regulatory.ingestion.scheduler._run_source", side_effect=_noop) as mock_fn:
            run(source_id="openfda_drug", since="7d")

        mock_fn.assert_called_once()
        call_args = mock_fn.call_args
        assert call_args[0][0] == "openfda_drug"

    def test_run_all_sources(self) -> None:
        """run() without source_id targets all registered sources."""
        from unittest.mock import patch

        from regulatory.ingestion.scheduler import run

        calls: list[str] = []

        async def _noop(source_id: str, since: object) -> None:
            calls.append(source_id)

        with patch("regulatory.ingestion.scheduler._run_source", side_effect=_noop):
            run(source_id=None, since=None)

        assert "openfda_drug" in calls
        assert "ppb_ke_alerts" in calls


# Patch targets reused across scheduler test classes
_PATCH_ALL_SOURCES = "regulatory.ingestion.scheduler.all_sources"
_PATCH_GET_SESSION = "regulatory.ingestion.scheduler.get_session"


# ---------------------------------------------------------------------------
# Scheduler URL-keyed dedup tests
# ---------------------------------------------------------------------------


class TestRunSourceUrlDedup:
    """_run_source skips fetch when the URL is already in documents."""

    @pytest.mark.asyncio
    async def test_skips_fetch_for_known_url(self) -> None:
        """fetch() is never called when source_url already exists in documents."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from regulatory.ingestion.scheduler import _run_source
        from regulatory.models import DocumentRef

        ref = DocumentRef(
            source_id="test_src",
            url="https://example.com/document/known-recall/",  # type: ignore[arg-type]
        )

        # Source that yields one ref and tracks fetch calls
        mock_source = MagicMock()
        mock_source.check_for_updates = False
        mock_source.fetch = AsyncMock()

        async def _discover(since: object):  # type: ignore[override]
            yield ref

        mock_source.discover = _discover

        # Session returns an existing document → URL already known
        mock_doc = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_doc
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_source_cls = MagicMock(return_value=mock_source)

        with (
            patch(
                "regulatory.ingestion.scheduler.all_sources",
                return_value={"test_src": mock_source_cls},
            ),
            patch("regulatory.ingestion.scheduler.get_session", return_value=mock_ctx),
        ):
            await _run_source("test_src", since=None)

        mock_source.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetches_unknown_url(self) -> None:
        """fetch() is called when source_url is not yet in documents."""
        from datetime import date
        from unittest.mock import AsyncMock, MagicMock, patch

        from regulatory.ingestion.scheduler import _run_source
        from regulatory.models import DocumentRef, DocumentType, NormalizedDocument, RawDocument

        ref = DocumentRef(
            source_id="test_src",
            url="https://example.com/document/new-recall/",  # type: ignore[arg-type]
        )
        raw = RawDocument(
            ref=ref,
            content=b"<html>new recall</html>",
            content_type="text/html",
            source_hash="b" * 64,
            fetched_at=__import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
        )
        normalized = NormalizedDocument(
            source_id="test_src",
            source_url="https://example.com/document/new-recall/",  # type: ignore[arg-type]
            source_hash="b" * 64,
            jurisdiction="ZA",
            document_type=DocumentType.recall,
            title="New Recall",
            date_published=date(2026, 5, 1),
            extracted_at=__import__("datetime").datetime.now(
                tz=__import__("datetime").timezone.utc
            ),
        )

        mock_source = MagicMock()
        mock_source.fetch = AsyncMock(return_value=raw)
        mock_source.parse = MagicMock(return_value=normalized)

        async def _discover(since: object):  # type: ignore[override]
            yield ref

        mock_source.discover = _discover

        # First execute (URL check) → None; second (hash check) → None
        none_result = MagicMock()
        none_result.scalar_one_or_none.return_value = None
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=none_result)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_source_cls = MagicMock(return_value=mock_source)

        with (
            patch(
                "regulatory.ingestion.scheduler.all_sources",
                return_value={"test_src": mock_source_cls},
            ),
            patch("regulatory.ingestion.scheduler.get_session", return_value=mock_ctx),
        ):
            await _run_source("test_src", since=None)

        mock_source.fetch.assert_called_once_with(ref)


# ---------------------------------------------------------------------------
# Scheduler version-tracking tests (check_for_updates flag)
# ---------------------------------------------------------------------------


class TestVersionTracking:
    """Tests for the check_for_updates flag and document_versions write path."""

    # ── helpers ────────────────────────────────────────────────────────────

    def _make_ref(self, url: str = "https://example.com/document/recall/") -> DocumentRef:
        return DocumentRef(source_id="test_src", url=url)  # type: ignore[arg-type]

    def _make_normalized(
        self, url: str = "https://example.com/document/recall/"
    ) -> NormalizedDocument:
        return NormalizedDocument(
            source_id="test_src",
            source_url=url,  # type: ignore[arg-type]
            source_hash="c" * 64,
            jurisdiction="ZA",
            document_type=DocumentType.recall,
            title="Test Recall",
            date_published=date(2026, 5, 1),
            extracted_at=datetime.now(tz=timezone.utc),
        )

    def _mock_session(self, url_hit: object = None, hash_hit: object = None):  # type: ignore[no-untyped-def]
        """Return a mock session whose execute() side-effects match url/hash lookups."""
        from unittest.mock import AsyncMock, MagicMock

        def _result(value: object) -> MagicMock:
            r = MagicMock()
            r.scalar_one_or_none.return_value = value
            return r

        mock = AsyncMock()
        mock.execute = AsyncMock(side_effect=[_result(url_hit), _result(hash_hit)])
        mock.add = MagicMock()
        mock.commit = AsyncMock()
        mock.delete = AsyncMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return mock, ctx

    def _source_cls(
        self, check_for_updates: bool = False, normalized: NormalizedDocument | None = None
    ):  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, MagicMock

        ref = self._make_ref()
        norm = normalized or self._make_normalized()
        raw_doc = RawDocument(
            ref=ref,
            content=b"<html/>",
            content_type="text/html",
            source_hash="c" * 64,
            fetched_at=datetime.now(tz=timezone.utc),
        )

        mock_source = MagicMock()
        mock_source.check_for_updates = check_for_updates
        mock_source.fetch = AsyncMock(return_value=raw_doc)
        mock_source.parse = MagicMock(return_value=norm)

        async def _discover(since: object):  # type: ignore[override]
            yield ref

        mock_source.discover = _discover
        return MagicMock(return_value=mock_source), mock_source

    # ── test 6 ─────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_check_for_updates_false_skips_known_url_without_fetching(self) -> None:
        """check_for_updates=False: known URL → fetch never called."""
        from unittest.mock import MagicMock, patch

        from regulatory.ingestion.scheduler import _run_source

        existing = MagicMock()
        mock_session, mock_ctx = self._mock_session(url_hit=existing)
        source_cls, mock_source = self._source_cls(check_for_updates=False)

        with (
            patch(
                "regulatory.ingestion.scheduler.all_sources", return_value={"test_src": source_cls}
            ),
            patch("regulatory.ingestion.scheduler.get_session", return_value=mock_ctx),
        ):
            await _run_source("test_src", since=None)

        mock_source.fetch.assert_not_called()

    # ── test 7 ─────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_check_for_updates_true_fetches_known_url(self) -> None:
        """check_for_updates=True: known URL → fetch IS called."""
        from unittest.mock import MagicMock, patch

        from regulatory.ingestion.scheduler import _run_source

        normalized = self._make_normalized()
        existing = MagicMock()
        existing.normalized_hash = normalized.normalized_content_hash()  # same → skip

        mock_session, mock_ctx = self._mock_session(url_hit=existing)
        source_cls, mock_source = self._source_cls(check_for_updates=True, normalized=normalized)

        with (
            patch(
                "regulatory.ingestion.scheduler.all_sources", return_value={"test_src": source_cls}
            ),
            patch("regulatory.ingestion.scheduler.get_session", return_value=mock_ctx),
        ):
            await _run_source("test_src", since=None)

        mock_source.fetch.assert_called_once()

    # ── test 8 ─────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_check_for_updates_true_skips_when_hash_matches(self) -> None:
        """check_for_updates=True: same normalized hash → docs_skipped, no version row."""
        from unittest.mock import MagicMock, patch

        from regulatory.ingestion.scheduler import _run_source

        normalized = self._make_normalized()
        existing = MagicMock()
        existing.normalized_hash = normalized.normalized_content_hash()

        mock_session, mock_ctx = self._mock_session(url_hit=existing)
        source_cls, _ = self._source_cls(check_for_updates=True, normalized=normalized)

        with (
            patch(
                "regulatory.ingestion.scheduler.all_sources", return_value={"test_src": source_cls}
            ),
            patch("regulatory.ingestion.scheduler.get_session", return_value=mock_ctx),
        ):
            await _run_source("test_src", since=None)

        # No DocumentVersion should have been added

        added_types = [type(c.args[0]).__name__ for c in mock_session.add.call_args_list]
        assert "DocumentVersion" not in added_types

    # ── test 9 ─────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_check_for_updates_true_versions_when_hash_differs(self) -> None:
        """check_for_updates=True: different hash → DocumentVersion written, doc updated."""
        import uuid
        from unittest.mock import MagicMock, patch

        from regulatory.db.models import DocumentVersion
        from regulatory.ingestion.scheduler import _run_source

        normalized = self._make_normalized()
        existing = MagicMock()
        existing.id = uuid.uuid4()
        existing.normalized_hash = "old_hash_that_does_not_match"
        existing.raw_text = "old raw text"
        existing.raw_metadata = {"old": "data"}

        mock_session, mock_ctx = self._mock_session(url_hit=existing)
        source_cls, _ = self._source_cls(check_for_updates=True, normalized=normalized)

        with (
            patch(
                "regulatory.ingestion.scheduler.all_sources", return_value={"test_src": source_cls}
            ),
            patch("regulatory.ingestion.scheduler.get_session", return_value=mock_ctx),
        ):
            await _run_source("test_src", since=None)

        added_objects = [c.args[0] for c in mock_session.add.call_args_list]
        version_rows = [o for o in added_objects if isinstance(o, DocumentVersion)]
        assert len(version_rows) == 1
        assert version_rows[0].normalized_hash == "old_hash_that_does_not_match"
        assert existing.normalized_hash == normalized.normalized_content_hash()

    # ── test 10 ────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_new_url_inserts_with_normalized_hash_populated(self) -> None:
        """New URL insert (check_for_updates=False) sets normalized_hash on the Document."""
        from unittest.mock import patch

        from regulatory.db.models import Document
        from regulatory.ingestion.scheduler import _run_source

        normalized = self._make_normalized()
        # url_hit=None (new URL), hash_hit=None (new hash)
        mock_session, mock_ctx = self._mock_session(url_hit=None, hash_hit=None)
        source_cls, _ = self._source_cls(check_for_updates=False, normalized=normalized)

        with (
            patch(
                "regulatory.ingestion.scheduler.all_sources", return_value={"test_src": source_cls}
            ),
            patch("regulatory.ingestion.scheduler.get_session", return_value=mock_ctx),
        ):
            await _run_source("test_src", since=None)

        added_objects = [c.args[0] for c in mock_session.add.call_args_list]
        doc_rows = [o for o in added_objects if isinstance(o, Document)]
        assert len(doc_rows) == 1
        assert doc_rows[0].normalized_hash == normalized.normalized_content_hash()


# ---------------------------------------------------------------------------
# HTTP layer unit tests
# ---------------------------------------------------------------------------


class TestTokenBucket:
    """Tests for the token bucket rate limiter."""

    @pytest.mark.asyncio
    async def test_acquires_immediately_when_full(self) -> None:
        """A full bucket allows immediate acquisition."""
        from regulatory.ingestion.http import TokenBucket

        bucket = TokenBucket(rate=10.0, capacity=10.0)
        # Should not raise or sleep
        await bucket.acquire()

    @pytest.mark.asyncio
    async def test_content_hash(self) -> None:
        """content_hash returns correct sha256."""
        from regulatory.ingestion.http import HttpClient

        data = b"test content"
        expected = hashlib.sha256(data).hexdigest()
        assert HttpClient.content_hash(data) == expected
