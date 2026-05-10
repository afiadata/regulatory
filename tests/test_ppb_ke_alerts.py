"""Tests for the PPB Kenya product recalls adapter (table-based discover/parse).

Fixture HTML is loaded from tests/fixtures/ppb_ke_alerts_2025_listing.html.
No live network calls are made.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from regulatory.models import DocumentRef, DocumentType, RawDocument, Severity
from regulatory.sources.ppb_ke_alerts import (
    PpbKeAlertsSource,
    _contains_recall_table,
    _infer_severity,
    _parse_date_flexible,
    _parse_inn_cell,
    _parse_table_row,
)

_FIXTURE_HTML = Path(__file__).parent / "fixtures" / "ppb_ke_alerts_2025_listing.html"


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelpers:
    """Unit tests for retained PPB adapter helper functions."""

    def test_parse_date_day_month_year(self) -> None:
        """'15 January 2024' parses correctly."""
        assert _parse_date_flexible("15 January 2024") == date(2024, 1, 15)

    def test_parse_date_iso(self) -> None:
        """ISO date string parses correctly."""
        assert _parse_date_flexible("2024-03-20") == date(2024, 3, 20)

    def test_parse_date_slash(self) -> None:
        """DD/MM/YYYY format parses correctly."""
        assert _parse_date_flexible("20/03/2024") == date(2024, 3, 20)

    def test_parse_date_invalid(self) -> None:
        """Garbage string returns None."""
        assert _parse_date_flexible("not-a-date-at-all") is None

    def test_infer_severity_class1(self) -> None:
        """'Class I recall' infers class_1."""
        assert _infer_severity("This is a Class I recall.") == Severity.class_1

    def test_infer_severity_class2(self) -> None:
        """'Class II recall' infers class_2."""
        assert _infer_severity("Class II recall notice.") == Severity.class_2

    def test_infer_severity_none(self) -> None:
        """Text without class mention returns None."""
        assert _infer_severity("Voluntary withdrawal of product.") is None


# ---------------------------------------------------------------------------
# INN cell parsing tests
# ---------------------------------------------------------------------------


class TestParseInnCell:
    """Tests for _parse_inn_cell."""

    def test_strips_dosage(self) -> None:
        """Dosage suffix is stripped before INN lookup."""
        normalized, raw = _parse_inn_cell("Methyldopa 250mg")
        assert raw == ["Methyldopa 250mg"]
        assert normalized == ["methyldopa"]

    def test_known_alias_paracetamol(self) -> None:
        """paracetamol normalizes to acetaminophen after dosage strip."""
        normalized, raw = _parse_inn_cell("Paracetamol 500mg")
        assert "acetaminophen" in normalized
        assert raw == ["Paracetamol 500mg"]

    def test_multi_ingredient_newline(self) -> None:
        """Newline-separated cell yields one entry per ingredient."""
        normalized, raw = _parse_inn_cell("Amoxicillin 500mg\nClavulanate 125mg")
        assert len(raw) == 2
        assert len(normalized) == 2

    def test_empty_returns_empty(self) -> None:
        """Empty string returns two empty lists."""
        normalized, raw = _parse_inn_cell("")
        assert normalized == []
        assert raw == []


# ---------------------------------------------------------------------------
# Table-detection tests
# ---------------------------------------------------------------------------


class TestContainsRecallTable:
    """Tests for _contains_recall_table."""

    def test_true_for_recall_table(self) -> None:
        """Returns True when the table has recall-related headers."""
        html = (
            "<table><thead><tr>"
            "<th>Recall Reference Number</th><th>Batch Number</th>"
            "</tr></thead></table>"
        )
        assert _contains_recall_table(html) is True

    def test_true_for_manufacturer_table(self) -> None:
        """Returns True when 'manufacturer' appears in headers."""
        html = "<table><thead><tr><th>Name of Manufacturer</th></tr></thead></table>"
        assert _contains_recall_table(html) is True

    def test_false_for_generic_table(self) -> None:
        """Returns False for unrelated tables."""
        html = "<table><thead><tr><th>Name</th><th>Email</th></tr></thead></table>"
        assert _contains_recall_table(html) is False


# ---------------------------------------------------------------------------
# Fixture-based discover tests
# ---------------------------------------------------------------------------


class TestPpbDiscover:
    """Tests for PpbKeAlertsSource.discover() using the fixture HTML."""

    def _mock_http_for_fixture(self, fixture_html: str) -> AsyncMock:
        """Return a mock HttpClient whose get() returns the fixture for 2025 URLs."""

        async def mock_get(url: str, **kwargs: object) -> MagicMock:
            r = MagicMock()
            if "products-recalled-2025" in url:
                r.status_code = 200
                r.text = fixture_html
            else:
                r.status_code = 404
                r.text = ""
            return r

        http = AsyncMock()
        http.get = AsyncMock(side_effect=mock_get)
        return http

    @pytest.mark.asyncio
    async def test_discover_extracts_rows_from_listing(self) -> None:
        """discover() yields ≥5 DocumentRefs from the fixture HTML."""
        fixture_html = _FIXTURE_HTML.read_text(encoding="utf-8")
        source = PpbKeAlertsSource()
        source._http = self._mock_http_for_fixture(fixture_html)  # type: ignore[assignment]

        refs = []
        async for ref in source.discover(since=datetime(2025, 1, 1, tzinfo=timezone.utc)):
            refs.append(ref)

        assert len(refs) >= 5
        assert all(r.source_id == "ppb_ke_alerts" for r in refs)
        assert all(r.document_id for r in refs)
        assert all(r.date_published for r in refs)

    @pytest.mark.asyncio
    async def test_discover_ref_has_extra_data(self) -> None:
        """Each yielded ref carries row data in ref.extra."""
        fixture_html = _FIXTURE_HTML.read_text(encoding="utf-8")
        source = PpbKeAlertsSource()
        source._http = self._mock_http_for_fixture(fixture_html)  # type: ignore[assignment]

        refs = []
        async for ref in source.discover(since=datetime(2025, 1, 1, tzinfo=timezone.utc)):
            refs.append(ref)

        first = refs[0]
        assert first.extra is not None
        assert "row_html" in first.extra
        assert "recall_ref" in first.extra
        assert "product_name" in first.extra

    @pytest.mark.asyncio
    async def test_discover_skips_unparseable_date_with_log(self) -> None:
        """Rows with garbage dates are skipped without raising."""
        bad_html = """
        <html><body><table>
          <thead><tr>
            <th>S/N</th><th>Date recall was initiated</th>
            <th>Recall Reference Number</th><th>Product Name/Category</th>
            <th>INN Name(s)</th><th>Batch Number(s)</th>
            <th>Name of Manufacturer</th><th>Reasons for recall</th>
            <th>Status</th>
          </tr></thead>
          <tbody>
            <tr>
              <td>1.</td><td>not-a-date</td><td>REC/2025/001</td>
              <td>Test Product</td><td>Amoxicillin 500mg</td>
              <td>BATCH001</td><td>Test Pharma, Kenya</td>
              <td>Test reason</td><td>Concluded</td>
            </tr>
          </tbody>
        </table></body></html>
        """
        source = PpbKeAlertsSource()
        source._http = self._mock_http_for_fixture(bad_html)  # type: ignore[assignment]

        refs = []
        async for ref in source.discover(since=datetime(2025, 1, 1, tzinfo=timezone.utc)):
            refs.append(ref)

        assert refs == []

    @pytest.mark.asyncio
    async def test_discover_document_id_matches_recall_ref(self) -> None:
        """document_id on each ref equals the Recall Reference Number column."""
        fixture_html = _FIXTURE_HTML.read_text(encoding="utf-8")
        source = PpbKeAlertsSource()
        source._http = self._mock_http_for_fixture(fixture_html)  # type: ignore[assignment]

        refs = []
        async for ref in source.discover(since=datetime(2025, 1, 1, tzinfo=timezone.utc)):
            refs.append(ref)

        ref_ids = {r.document_id for r in refs}
        assert "REC/2025/045" in ref_ids
        assert "REC/2025/038" in ref_ids


# ---------------------------------------------------------------------------
# Parse tests
# ---------------------------------------------------------------------------


def _make_row_raw_document(extra: dict) -> RawDocument:  # type: ignore[type-arg]
    """Build a RawDocument with row data embedded in ref.extra."""
    recall_ref: str = extra.get("recall_ref", "REC/TEST/001")
    product_name: str = extra.get("product_name", "Test Product")
    date_text: str = extra.get("date_text", "01/01/2025")
    pub_date = datetime.strptime(date_text, "%d/%m/%Y").date()

    detail_url: str | None = extra.get("detail_url")
    if detail_url:
        ref_url = detail_url
    else:
        fragment = recall_ref.replace("/", "-")
        ref_url = f"https://web.pharmacyboardkenya.org/products-recalled-2025/#{fragment}"

    ref = DocumentRef(
        source_id="ppb_ke_alerts",
        url=ref_url,
        document_id=recall_ref,
        title=f"{product_name} recall ({recall_ref})",
        date_published=pub_date,
        extra=extra,
    )
    row_html: str = extra.get("row_html", "<tr></tr>")
    content = row_html.encode("utf-8")
    return RawDocument(
        ref=ref,
        content=content,
        content_type="text/html; charset=utf-8",
        source_hash=hashlib.sha256(content).hexdigest(),
        fetched_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


class TestPpbRowParse:
    """Tests for PpbKeAlertsSource.parse() using structured row data."""

    def test_parse_basic_row(self) -> None:
        """parse() returns correct fields for a standard row."""
        extra = {
            "date_text": "29/12/2025",
            "recall_ref": "REC/2025/045",
            "product_name": "Medopress",
            "detail_url": "https://web.pharmacyboardkenya.org/medopress-rec-2025-045/",
            "inn_text": "Methyldopa 250mg",
            "batches": ["BPL.221138"],
            "manufacturer_text": "Cosmos Limited, Kenya",
            "reason_text": "Presence of grey spots on the tablet surface",
            "status_text": "Concluded",
            "year_page_url": "https://web.pharmacyboardkenya.org/products-recalled-2025/",
            "row_index_on_page": 0,
            "row_html": "<tr><td>1.</td></tr>",
        }
        raw = _make_row_raw_document(extra)
        doc = PpbKeAlertsSource().parse(raw)

        assert doc.source_id == "ppb_ke_alerts"
        assert doc.jurisdiction == "KE"
        assert doc.document_type == DocumentType.recall
        assert doc.document_id == "REC/2025/045"
        assert doc.date_published == date(2025, 12, 29)
        assert "Medopress" in doc.product_names
        assert "methyldopa" in doc.active_ingredients
        assert "Methyldopa 250mg" in doc.active_ingredients_raw
        assert "Cosmos Limited, Kenya" in doc.manufacturers
        assert doc.raw_metadata["batch_numbers"] == ["BPL.221138"]
        assert doc.raw_metadata["recall_reason"] == "Presence of grey spots on the tablet surface"
        assert doc.raw_metadata["status"] == "Concluded"
        assert doc.severity == Severity.unclassified

    def test_parse_multi_batch_row(self) -> None:
        """parse() returns a list with all batch numbers."""
        extra = {
            "date_text": "15/10/2025",
            "recall_ref": "REC/2025/038",
            "product_name": "Amoxicillin Capsules 500mg",
            "detail_url": "https://web.pharmacyboardkenya.org/amoxicillin-rec-2025-038/",
            "inn_text": "Amoxicillin 500mg",
            "batches": ["AMX2025001", "AMX2025002", "AMX2025003"],
            "manufacturer_text": "AfriPharma Limited, Kenya",
            "reason_text": "Failed dissolution test",
            "status_text": "Ongoing",
            "year_page_url": "https://web.pharmacyboardkenya.org/products-recalled-2025/",
            "row_index_on_page": 1,
            "row_html": "<tr><td>2.</td></tr>",
        }
        doc = PpbKeAlertsSource().parse(_make_row_raw_document(extra))

        batches = doc.raw_metadata["batch_numbers"]
        assert len(batches) == 3
        assert "AMX2025001" in batches
        assert "AMX2025002" in batches
        assert "AMX2025003" in batches

    def test_parse_ongoing_status(self) -> None:
        """raw_metadata['status'] == 'Ongoing' for an ongoing recall."""
        extra = {
            "date_text": "15/10/2025",
            "recall_ref": "REC/2025/038",
            "product_name": "Amoxicillin Capsules 500mg",
            "detail_url": "https://web.pharmacyboardkenya.org/amoxicillin-rec-2025-038/",
            "inn_text": "Amoxicillin 500mg",
            "batches": ["AMX2025001"],
            "manufacturer_text": "AfriPharma Limited, Kenya",
            "reason_text": "Failed dissolution test",
            "status_text": "Ongoing",
            "year_page_url": "https://web.pharmacyboardkenya.org/products-recalled-2025/",
            "row_index_on_page": 1,
            "row_html": "<tr><td>2.</td></tr>",
        }
        doc = PpbKeAlertsSource().parse(_make_row_raw_document(extra))

        assert doc.raw_metadata["status"] == "Ongoing"

    def test_parse_handles_missing_anchor(self) -> None:
        """When detail_url is None, source_url falls back to year page + fragment."""
        recall_ref = "REC/2025/029"
        extra = {
            "date_text": "20/08/2025",
            "recall_ref": recall_ref,
            "product_name": "Paracetamol Tablets 500mg",
            "detail_url": None,
            "inn_text": "Paracetamol 500mg",
            "batches": ["PT2025A"],
            "manufacturer_text": "Generic Pharma Ltd, Uganda",
            "reason_text": "Subpotent",
            "status_text": "Concluded",
            "year_page_url": "https://web.pharmacyboardkenya.org/products-recalled-2025/",
            "row_index_on_page": 2,
            "row_html": "<tr><td>3.</td></tr>",
        }
        raw = _make_row_raw_document(extra)
        doc = PpbKeAlertsSource().parse(raw)

        assert "REC-2025-029" in str(doc.source_url)
        assert doc.document_id == "REC/2025/029"

    def test_parse_paracetamol_normalizes_to_acetaminophen(self) -> None:
        """paracetamol INN alias is normalized to acetaminophen."""
        extra = {
            "date_text": "20/08/2025",
            "recall_ref": "REC/2025/029",
            "product_name": "Paracetamol Tablets 500mg",
            "detail_url": None,
            "inn_text": "Paracetamol 500mg",
            "batches": ["PT2025A"],
            "manufacturer_text": "Generic Pharma Ltd, Uganda",
            "reason_text": "Subpotent",
            "status_text": "Concluded",
            "year_page_url": "https://web.pharmacyboardkenya.org/products-recalled-2025/",
            "row_index_on_page": 2,
            "row_html": "<tr><td>3.</td></tr>",
        }
        doc = PpbKeAlertsSource().parse(_make_row_raw_document(extra))

        assert "acetaminophen" in doc.active_ingredients
        assert "Paracetamol 500mg" in doc.active_ingredients_raw

    def test_parse_country_extracted_to_regions(self) -> None:
        """Trailing known-country in manufacturer cell is added to regions_affected."""
        extra = {
            "date_text": "29/12/2025",
            "recall_ref": "REC/2025/045",
            "product_name": "Medopress",
            "detail_url": "https://web.pharmacyboardkenya.org/medopress/",
            "inn_text": "Methyldopa 250mg",
            "batches": ["BPL.221138"],
            "manufacturer_text": "Cosmos Limited, Kenya",
            "reason_text": "Spots on tablet",
            "status_text": "Concluded",
            "year_page_url": "https://web.pharmacyboardkenya.org/products-recalled-2025/",
            "row_index_on_page": 0,
            "row_html": "<tr><td>1.</td></tr>",
        }
        doc = PpbKeAlertsSource().parse(_make_row_raw_document(extra))

        assert "Kenya" in doc.regions_affected

    def test_parse_raw_metadata_keys(self) -> None:
        """raw_metadata contains the required keys."""
        extra = {
            "date_text": "29/12/2025",
            "recall_ref": "REC/2025/045",
            "product_name": "Medopress",
            "detail_url": "https://web.pharmacyboardkenya.org/medopress/",
            "inn_text": "Methyldopa 250mg",
            "batches": ["BPL.221138"],
            "manufacturer_text": "Cosmos Limited, Kenya",
            "reason_text": "Spots on tablet",
            "status_text": "Concluded",
            "year_page_url": "https://web.pharmacyboardkenya.org/products-recalled-2025/",
            "row_index_on_page": 0,
            "row_html": "<tr><td>1.</td></tr>",
        }
        doc = PpbKeAlertsSource().parse(_make_row_raw_document(extra))

        assert "batch_numbers" in doc.raw_metadata
        assert "recall_reason" in doc.raw_metadata
        assert "status" in doc.raw_metadata
        assert "year_page_url" in doc.raw_metadata
        assert "row_index_on_page" in doc.raw_metadata


# ---------------------------------------------------------------------------
# _parse_table_row unit tests
# ---------------------------------------------------------------------------


class TestParseTableRow:
    """Tests for _parse_table_row using real fixture HTML."""

    def _get_data_rows(self) -> list:  # type: ignore[type-arg]
        """Return data rows from the fixture table."""
        from bs4 import BeautifulSoup

        html = _FIXTURE_HTML.read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        return soup.find_all("tr")

    def test_header_row_returns_none(self) -> None:
        """<tr> with only <th> cells returns None."""
        from bs4 import BeautifulSoup

        html = (
            "<table><thead><tr>"
            "<th>S/N</th><th>Date</th><th>Ref</th><th>Product</th>"
            "<th>INN</th><th>Batch</th><th>Mfr</th><th>Reason</th><th>Status</th>"
            "</tr></thead></table>"
        )
        soup = BeautifulSoup(html, "html.parser")
        row = soup.find("tr")
        result = _parse_table_row(row, "https://example.com/", 0)  # type: ignore[arg-type]
        assert result is None

    def test_first_data_row_parsed(self) -> None:
        """First data row of the fixture parses correctly."""
        rows = self._get_data_rows()
        data_rows = [r for r in rows if _parse_table_row(r, "https://example.com/", 0)]
        assert data_rows, "No data rows found in fixture"

        result = _parse_table_row(data_rows[0], "https://example.com/", 0)
        assert result is not None
        assert result["recall_ref"] == "REC/2025/045"
        assert result["product_name"] == "Medopress"
        assert result["batches"] == ["BPL.221138"]
        assert result["status_text"] == "Concluded"
        assert result["detail_url"] is not None

    def test_multi_batch_row_parsed(self) -> None:
        """Row with <br>-separated batches produces a list."""
        rows = self._get_data_rows()
        data_rows = [r for r in rows if _parse_table_row(r, "https://example.com/", 0)]
        assert len(data_rows) >= 2

        result = _parse_table_row(data_rows[1], "https://example.com/", 1)
        assert result is not None
        assert len(result["batches"]) == 3
        assert "AMX2025001" in result["batches"]

    def test_no_anchor_row_has_none_detail_url(self) -> None:
        """Row 3 (Paracetamol, no anchor) has detail_url=None."""
        rows = self._get_data_rows()
        data_rows = [r for r in rows if _parse_table_row(r, "https://example.com/", 0)]
        assert len(data_rows) >= 3

        result = _parse_table_row(data_rows[2], "https://example.com/", 2)
        assert result is not None
        assert result["detail_url"] is None
        assert result["product_name"] == "Paracetamol Tablets 500mg"
