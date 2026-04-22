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
        """A datetime object is returned as-is (made UTC-aware)."""
        dt = datetime(2024, 3, 1, 0, 0, 0)
        result = _parse_since(dt)
        assert result is not None
        assert result.tzinfo is not None
        assert result.year == 2024


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
