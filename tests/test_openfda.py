"""Tests for the openFDA drug enforcement adapter.

Uses recorded fixtures — no live network calls in CI.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from regulatory.models import DocumentRef, DocumentType, RawDocument, Severity
from regulatory.sources.openfda_drug import (
    OpenFdaDrugSource,
    _coerce_list,
    _map_severity,
    _parse_fda_date,
)

# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelpers:
    """Unit tests for openFDA adapter helper functions."""

    def test_parse_fda_date_valid(self) -> None:
        """YYYYMMDD string parses correctly."""
        result = _parse_fda_date("20240115")
        assert result == date(2024, 1, 15)

    def test_parse_fda_date_none(self) -> None:
        """None input returns None."""
        assert _parse_fda_date(None) is None

    def test_parse_fda_date_invalid(self) -> None:
        """Garbage string returns None."""
        assert _parse_fda_date("not-a-date") is None

    def test_map_severity_class1(self) -> None:
        """Class I maps to class_1."""
        assert _map_severity("Class I") == Severity.class_1

    def test_map_severity_class2(self) -> None:
        """Class II maps to class_2."""
        assert _map_severity("Class II") == Severity.class_2

    def test_map_severity_class3(self) -> None:
        """Class III maps to class_3."""
        assert _map_severity("Class III") == Severity.class_3

    def test_map_severity_unknown(self) -> None:
        """Unknown classification maps to unclassified."""
        assert _map_severity("Unknown Class") == Severity.unclassified

    def test_map_severity_none(self) -> None:
        """None input returns None."""
        assert _map_severity(None) is None

    def test_coerce_list_string(self) -> None:
        """Semicolon-delimited string is split into list."""
        result = _coerce_list("amoxicillin; paracetamol")
        assert result == ["amoxicillin", "paracetamol"]

    def test_coerce_list_already_list(self) -> None:
        """An already-list value is returned cleaned."""
        result = _coerce_list(["amoxicillin", "  paracetamol  "])
        assert result == ["amoxicillin", "paracetamol"]

    def test_coerce_list_none(self) -> None:
        """None returns empty list."""
        assert _coerce_list(None) == []


# ---------------------------------------------------------------------------
# Parse tests using fixture
# ---------------------------------------------------------------------------


class TestOpenFdaParse:
    """Tests for OpenFdaDrugSource.parse() using the recorded fixture."""

    def test_parse_happy_path(self, openfda_raw_document: RawDocument) -> None:
        """parse() produces a valid NormalizedDocument from the fixture."""
        source = OpenFdaDrugSource()
        doc = source.parse(openfda_raw_document)

        assert doc.source_id == "openfda_drug"
        assert doc.jurisdiction == "US"
        assert doc.document_type == DocumentType.recall
        assert doc.document_id == "D-001-2024-00001"
        assert doc.severity == Severity.class_2
        assert doc.date_published == date(2024, 1, 10)

    def test_parse_active_ingredients_normalized(self, openfda_raw_document: RawDocument) -> None:
        """amoxicillin is kept as amoxicillin (already canonical)."""
        source = OpenFdaDrugSource()
        doc = source.parse(openfda_raw_document)
        assert "amoxicillin" in doc.active_ingredients

    def test_parse_manufacturers(self, openfda_raw_document: RawDocument) -> None:
        """recalling_firm is mapped to manufacturers."""
        source = OpenFdaDrugSource()
        doc = source.parse(openfda_raw_document)
        assert any("AfriPharma" in m for m in doc.manufacturers)

    def test_parse_regions_affected(self, openfda_raw_document: RawDocument) -> None:
        """distribution_pattern is split into regions_affected."""
        source = OpenFdaDrugSource()
        doc = source.parse(openfda_raw_document)
        assert len(doc.regions_affected) >= 1

    def test_parse_no_results_raises(self) -> None:
        """parse() raises ValueError when the JSON has no results."""
        source = OpenFdaDrugSource()
        content = json.dumps({"meta": {}, "results": []}).encode()
        ref = DocumentRef(
            source_id="openfda_drug",
            url="https://api.fda.gov/drug/enforcement.json",
        )
        raw = RawDocument(
            ref=ref,
            content=content,
            content_type="application/json",
            source_hash=hashlib.sha256(content).hexdigest(),
            fetched_at=datetime.now(tz=timezone.utc),
        )
        with pytest.raises(ValueError, match="No results"):
            source.parse(raw)

    def test_parse_raw_metadata_preserved(self, openfda_raw_document: RawDocument) -> None:
        """raw_metadata contains the original FDA record fields."""
        source = OpenFdaDrugSource()
        doc = source.parse(openfda_raw_document)
        assert "recall_number" in doc.raw_metadata
        assert doc.raw_metadata["recall_number"] == "D-001-2024-00001"


# ---------------------------------------------------------------------------
# Discover tests (mocked HTTP)
# ---------------------------------------------------------------------------


class TestOpenFdaDiscover:
    """Tests for OpenFdaDrugSource.discover() with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_discover_yields_refs(self, openfda_sample_json: dict[str, Any]) -> None:
        """discover() yields DocumentRef instances from a mocked API response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = openfda_sample_json

        with patch(
            "regulatory.sources.openfda_drug.HttpClient",
        ) as MockClient:
            mock_client_instance = AsyncMock()
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client_instance.get = AsyncMock(return_value=mock_resp)
            MockClient.return_value = mock_client_instance

            source = OpenFdaDrugSource()
            refs = []
            async for ref in source.discover(since=None):
                refs.append(ref)

        assert len(refs) == 1
        assert refs[0].document_id == "D-001-2024-00001"
        assert refs[0].source_id == "openfda_drug"
        # pydantic encodes " as %22 in URL query strings
        assert "recall_number" in str(refs[0].url)
        assert "D-001-2024-00001" in str(refs[0].url)
