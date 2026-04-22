"""Tests for the PPB Kenya product-alerts adapter.

Uses a real PDF from data/ppb_pdfs/ (via conftest fixture) or the
synthetic minimal PDF fallback — no live network calls.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from regulatory.models import DocumentType, RawDocument
from regulatory.sources.ppb_ke_alerts import (
    PpbKeAlertsSource,
    _infer_severity,
    _parse_date_flexible,
)

# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelpers:
    """Unit tests for PPB adapter helper functions."""

    def test_parse_date_day_month_year(self) -> None:
        """'15 January 2024' parses correctly."""
        result = _parse_date_flexible("15 January 2024")
        assert result == date(2024, 1, 15)

    def test_parse_date_iso(self) -> None:
        """ISO date string parses correctly."""
        result = _parse_date_flexible("2024-03-20")
        assert result == date(2024, 3, 20)

    def test_parse_date_slash(self) -> None:
        """DD/MM/YYYY format parses correctly."""
        result = _parse_date_flexible("20/03/2024")
        assert result == date(2024, 3, 20)

    def test_parse_date_invalid(self) -> None:
        """Garbage string returns None."""
        assert _parse_date_flexible("not-a-date-at-all") is None

    def test_infer_severity_class1(self) -> None:
        """'Class I recall' infers class_1 severity."""
        from regulatory.models import Severity

        result = _infer_severity("This product is subject to a Class I recall.")
        assert result == Severity.class_1

    def test_infer_severity_class2(self) -> None:
        """'Class II recall' infers class_2 severity."""
        from regulatory.models import Severity

        result = _infer_severity("Class II recall notice for the following batches.")
        assert result == Severity.class_2

    def test_infer_severity_none(self) -> None:
        """Text with no class mention returns None."""
        result = _infer_severity("Voluntary withdrawal of product.")
        assert result is None


# ---------------------------------------------------------------------------
# Parse tests using PDF fixture
# ---------------------------------------------------------------------------


class TestPpbParse:
    """Tests for PpbKeAlertsSource.parse() using the PDF fixture."""

    def test_parse_returns_normalized_document(self, ppb_raw_document: RawDocument) -> None:
        """parse() returns a NormalizedDocument without raising."""
        source = PpbKeAlertsSource()
        doc = source.parse(ppb_raw_document)

        assert doc.source_id == "ppb_ke_alerts"
        assert doc.jurisdiction == "KE"
        assert doc.document_type == DocumentType.alert

    def test_parse_title_not_empty(self, ppb_raw_document: RawDocument) -> None:
        """Parsed document has a non-empty title."""
        source = PpbKeAlertsSource()
        doc = source.parse(ppb_raw_document)
        assert doc.title

    def test_parse_date_published_set(self, ppb_raw_document: RawDocument) -> None:
        """date_published is set (defaults to today when not extractable)."""
        source = PpbKeAlertsSource()
        doc = source.parse(ppb_raw_document)
        assert doc.date_published is not None
        assert isinstance(doc.date_published, date)

    def test_parse_raw_metadata_has_method(self, ppb_raw_document: RawDocument) -> None:
        """raw_metadata records which extraction method succeeded."""
        source = PpbKeAlertsSource()
        doc = source.parse(ppb_raw_document)
        assert "extraction_method" in doc.raw_metadata

    def test_parse_source_hash_preserved(self, ppb_raw_document: RawDocument) -> None:
        """source_hash in the output matches the raw document hash."""
        source = PpbKeAlertsSource()
        doc = source.parse(ppb_raw_document)
        assert doc.source_hash == ppb_raw_document.source_hash

    def test_heuristic_parser_product_name(self) -> None:
        """_parse_text_heuristic extracts product name from 'Product:' line."""
        text = "Product: Amoxicillin Capsules 500mg\nManufacturer: AfriPharma Ltd\n"
        result = PpbKeAlertsSource._parse_text_heuristic(text)
        assert result.get("product_names") == ["Amoxicillin Capsules 500mg"]

    def test_heuristic_parser_manufacturer(self) -> None:
        """_parse_text_heuristic extracts manufacturer name."""
        text = "Product: Amoxicillin\nManufacturer: AfriPharma Ltd\n"
        result = PpbKeAlertsSource._parse_text_heuristic(text)
        assert result.get("manufacturers") == ["AfriPharma Ltd"]

    def test_heuristic_parser_batch_numbers(self) -> None:
        """_parse_text_heuristic extracts batch numbers."""
        text = "Batch No: A123, B456\nReason: Failed dissolution test."
        result = PpbKeAlertsSource._parse_text_heuristic(text)
        batches = result.get("batch_numbers", [])
        assert "A123" in batches


# ---------------------------------------------------------------------------
# Discover tests (mocked HTTP)
# ---------------------------------------------------------------------------


class TestPpbDiscover:
    """Tests for PpbKeAlertsSource.discover() with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_discover_yields_pdf_refs(self) -> None:
        """discover() yields DocumentRef for each PDF found on the listing page."""
        homepage_html = """
        <html><body>
          <a href="/alerts/">Product Alerts</a>
        </body></html>
        """
        listing_html = """
        <html><body>
          <a href="/download/recall-001.pdf">Product Recall Notice — 15 January 2024</a>
          <a href="/download/recall-002.pdf">Safety Alert — 20 February 2024</a>
        </body></html>
        """

        def make_resp(html: str) -> MagicMock:
            r = MagicMock()
            r.status_code = 200
            r.text = html
            return r

        call_count = 0

        async def mock_get(url: str, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if url.replace("https://web.", "") == "pharmacyboardkenya.org/":
                return make_resp(homepage_html)
            return make_resp(listing_html)

        with patch(
            "regulatory.sources.ppb_ke_alerts.HttpClient",
        ) as MockClient:
            mock_instance = AsyncMock()
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_instance.get = AsyncMock(side_effect=mock_get)
            MockClient.return_value = mock_instance

            source = PpbKeAlertsSource()
            refs = []
            async for ref in source.discover():
                refs.append(ref)

        assert len(refs) >= 1
        assert all(str(r.url).endswith(".pdf") for r in refs)
        assert all(r.source_id == "ppb_ke_alerts" for r in refs)
