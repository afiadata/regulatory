"""Tests for the SAHPRA Product Recalls adapter."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from regulatory.models import DocumentRef, RawDocument
from regulatory.sources.sahpra_recalls import (
    SahpraRecallsSource,
    _infer_severity,
    _parse_date_flexible,
    _parse_title,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def _make_raw(fixture_name: str, url: str, extra: dict[str, Any] | None = None) -> RawDocument:
    content = _load_fixture(fixture_name)
    ref = DocumentRef(
        source_id="sahpra_recalls",
        url=url,
        extra=extra or {"listing_page_number": 1},
    )
    return RawDocument(
        ref=ref,
        content=content,
        content_type="text/html; charset=utf-8",
        source_hash=hashlib.sha256(content).hexdigest(),
        fetched_at=datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc),
    )


def _mock_http_response(text: str, status: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.text = text
    resp.content = text.encode("utf-8")
    resp.headers = {"content-type": "text/html; charset=utf-8"}
    return resp


# ---------------------------------------------------------------------------
# Unit tests: pure helpers
# ---------------------------------------------------------------------------


class TestParseDateFlexible:
    def test_space_separated(self) -> None:
        assert _parse_date_flexible("04 May 2026") == date(2026, 5, 4)

    def test_dot_separated(self) -> None:
        assert _parse_date_flexible("17.April.2026") == date(2026, 4, 17)

    def test_dot_month_name(self) -> None:
        assert _parse_date_flexible("20.April.2026") == date(2026, 4, 20)

    def test_leading_zero(self) -> None:
        assert _parse_date_flexible("06 December 2024") == date(2024, 12, 6)

    def test_unparseable_returns_none(self) -> None:
        assert _parse_date_flexible("Not available") is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_date_flexible("") is None


class TestInferSeverity:
    from regulatory.models import Severity

    def test_class_i(self) -> None:
        from regulatory.models import Severity
        assert _infer_severity("Class I Type B") == Severity.class_1

    def test_class_ii(self) -> None:
        from regulatory.models import Severity
        assert _infer_severity("Class II Type C") == Severity.class_2

    def test_class_iii(self) -> None:
        from regulatory.models import Severity
        assert _infer_severity("Class III Type C") == Severity.class_3

    def test_numeric_class_2(self) -> None:
        from regulatory.models import Severity
        assert _infer_severity("Class 2 Type A") == Severity.class_2

    def test_unclassified(self) -> None:
        from regulatory.models import Severity
        assert _infer_severity("Voluntary Recall") == Severity.unclassified

    def test_empty_string(self) -> None:
        from regulatory.models import Severity
        assert _infer_severity("") == Severity.unclassified

    def test_class_iii_not_confused_with_class_i(self) -> None:
        from regulatory.models import Severity
        assert _infer_severity("Class III Type A") == Severity.class_3


class TestParseTitle:
    def test_product_and_single_ingredient(self) -> None:
        product, ingredients = _parse_title("Halaven (Eribulin)")
        assert product == "Halaven"
        assert ingredients == ["Eribulin"]

    def test_product_and_multi_ingredient(self) -> None:
        product, ingredients = _parse_title(
            "Citro-Soda (Sodium bicarbonate, Tartaric acid, Citric acid, Sodium citrate)"
        )
        assert product == "Citro-Soda"
        assert len(ingredients) == 4
        assert "Sodium bicarbonate" in ingredients
        assert "Sodium citrate" in ingredients

    def test_no_parens(self) -> None:
        product, ingredients = _parse_title("Kiwi Complete Vacuum Delivery System")
        assert product == "Kiwi Complete Vacuum Delivery System"
        assert ingredients == []

    def test_product_with_numbers(self) -> None:
        product, ingredients = _parse_title("Apixaban 2,5 DRL (Apixaban)")
        assert product == "Apixaban 2,5 DRL"
        assert ingredients == ["Apixaban"]


# ---------------------------------------------------------------------------
# Integration tests: discover()
# ---------------------------------------------------------------------------


MINIMAL_LISTING = """<!DOCTYPE html>
<html><body>
<article class="post-1 dlp_document type-dlp_document status-publish">
  <div class="post_content_holder">
    <div class="post_text_inner">
      <h2 itemprop="name" class="entry_title">
        <span class="date entry_date">07 May</span>
        <a itemprop="url" href="https://www.sahpra.org.za/document/halaven-eribulin/">Halaven (Eribulin)</a>
      </h2>
      <p itemprop="description" class="post_excerpt">
Company name &amp; Address:
registration number
Batch number(s)
Expiry date
Pack size
First release date
Re-call Classification
Recall date

Eisai Pharmaceuticals Africa (Pty) Ltd
48/26/0047
148482
May 2028
1 vial
06 December 2024
Class II Type C
04 May 2026

Brief description of the problem (reason for recall)
Out of specification results.</p>
      <div class="post_more"><a href="https://www.sahpra.org.za/document/halaven-eribulin/" class="qbutton small">Read More</a></div>
    </div>
  </div>
</article>
<article class="post-2 dlp_document type-dlp_document status-publish">
  <div class="post_content_holder">
    <div class="post_text_inner">
      <h2 itemprop="name" class="entry_title">
        <span class="date entry_date">17 Apr</span>
        <a itemprop="url" href="https://www.sahpra.org.za/document/kiwi-complete-vacuum-delivery-system/">Kiwi Complete Vacuum Delivery System</a>
      </h2>
      <p itemprop="description" class="post_excerpt">
Company name &amp; Address:
Batch number(s)
Recall date

Pharmaco Distribution
251485
17.April.2026

Brief description of the problem (reason for recall)
Manufacturing defect.</p>
      <div class="post_more"><a href="https://www.sahpra.org.za/document/kiwi-complete-vacuum-delivery-system/" class="qbutton small">Read More</a></div>
    </div>
  </div>
</article>
</body></html>"""

EMPTY_LISTING = """<!DOCTYPE html><html><body><div class="container"></div></body></html>"""


class TestSahpraDiscover:
    @pytest.mark.asyncio
    async def test_discover_paginates_until_404(self) -> None:
        """discover() stops when a page returns 404."""
        source = SahpraRecallsSource()

        page1_resp = _mock_http_response(MINIMAL_LISTING)
        page2_resp = _mock_http_response(MINIMAL_LISTING)
        error_404 = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock(status_code=404)
        )

        call_count = 0

        async def fake_get(url: str, **_: Any) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return page1_resp
            if call_count == 2:
                return page2_resp
            raise error_404

        source._http.get = fake_get  # type: ignore[method-assign]

        refs = [ref async for ref in source.discover()]
        assert len(refs) == 4  # 2 per page × 2 pages
        assert call_count == 3  # page 1, page 2, page 3 (404)

    @pytest.mark.asyncio
    async def test_discover_stops_at_max_pages(self) -> None:
        """discover() never fetches more than MAX_PAGES pages."""
        source = SahpraRecallsSource()
        source.MAX_PAGES = 3

        async def fake_get(url: str, **_: Any) -> httpx.Response:
            return _mock_http_response(MINIMAL_LISTING)

        source._http.get = fake_get  # type: ignore[method-assign]

        refs = [ref async for ref in source.discover()]
        assert len(refs) == 6  # 2 per page × 3 pages (MAX_PAGES cap)

    @pytest.mark.asyncio
    async def test_discover_stops_on_empty_page(self) -> None:
        """discover() stops when a page has no dlp_document articles."""
        source = SahpraRecallsSource()

        call_count = 0

        async def fake_get(url: str, **_: Any) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return _mock_http_response(MINIMAL_LISTING if call_count == 1 else EMPTY_LISTING)

        source._http.get = fake_get  # type: ignore[method-assign]

        refs = [ref async for ref in source.discover()]
        assert len(refs) == 2
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_discover_early_stops_when_all_dates_before_since(self) -> None:
        """discover() stops pagination when all cards on a page pre-date `since`."""
        source = SahpraRecallsSource()

        # Both cards in MINIMAL_LISTING have 2026 recall dates.
        # Set since to 2026-06-01 so both are before since.
        since = datetime(2026, 6, 1, tzinfo=timezone.utc)

        call_count = 0

        async def fake_get(url: str, **_: Any) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return _mock_http_response(MINIMAL_LISTING)

        source._http.get = fake_get  # type: ignore[method-assign]

        refs = [ref async for ref in source.discover(since=since)]
        assert refs == []  # All skipped
        assert call_count == 1  # Stopped after first page

    @pytest.mark.asyncio
    async def test_discover_yields_detail_url_from_card(self) -> None:
        """discover() yields the card heading link href as DocumentRef.url."""
        source = SahpraRecallsSource()

        async def fake_get(url: str, **_: Any) -> httpx.Response:
            if "page/" in url:
                raise httpx.HTTPStatusError(
                    "404", request=MagicMock(), response=MagicMock(status_code=404)
                )
            return _mock_http_response(MINIMAL_LISTING)

        source._http.get = fake_get  # type: ignore[method-assign]

        refs = [ref async for ref in source.discover()]
        urls = [str(ref.url) for ref in refs]
        assert "https://www.sahpra.org.za/document/halaven-eribulin/" in urls
        assert "https://www.sahpra.org.za/document/kiwi-complete-vacuum-delivery-system/" in urls


# ---------------------------------------------------------------------------
# Integration tests: parse()
# ---------------------------------------------------------------------------


class TestSahpraParse:
    def test_parse_standard_recall(self) -> None:
        """Halaven fixture: all table fields, Class II severity, correct date."""
        from regulatory.models import DocumentType, Severity

        source = SahpraRecallsSource()
        raw = _make_raw(
            "sahpra_detail_halaven.html",
            "https://www.sahpra.org.za/document/halaven-eribulin/",
        )
        doc = source.parse(raw)

        assert doc.title == "Halaven (Eribulin)"
        assert doc.product_names == ["Halaven"]
        assert doc.severity == Severity.class_2
        assert doc.date_published == date(2026, 5, 4)
        assert doc.document_type == DocumentType.recall
        assert doc.jurisdiction == "ZA"
        assert doc.regions_affected == ["ZA"]
        assert doc.raw_metadata["recall_type"] == "Class II Type C"
        assert doc.raw_metadata["registration_number"] == "48/26/0047"
        assert doc.manufacturers == ["Eisai Pharmaceuticals Africa (Pty) Ltd"]

    def test_parse_no_parens_in_title(self) -> None:
        """Kiwi fixture: no active ingredients, Class I severity, dot-date."""
        from regulatory.models import Severity

        source = SahpraRecallsSource()
        raw = _make_raw(
            "sahpra_detail_kiwi_no_parens.html",
            "https://www.sahpra.org.za/document/kiwi-complete-vacuum-delivery-system/",
        )
        doc = source.parse(raw)

        assert doc.product_names == ["Kiwi Complete Vacuum Delivery System"]
        assert doc.active_ingredients == []
        assert doc.active_ingredients_raw == []
        assert doc.severity == Severity.class_1
        assert doc.date_published == date(2026, 4, 17)

    def test_parse_multi_batch(self) -> None:
        """Cipla-Pioglitazone fixture: two batch numbers extracted."""
        source = SahpraRecallsSource()
        raw = _make_raw(
            "sahpra_detail_multi_batch.html",
            "https://www.sahpra.org.za/document/cipla-pioglitazone/",
        )
        doc = source.parse(raw)

        batches = doc.raw_metadata["batch_numbers"]
        assert isinstance(batches, list)
        assert len(batches) >= 2
        assert "4GC0350" in batches
        assert "GC30719" in batches

    def test_parse_na_registration(self) -> None:
        """Kiwi has N/A registration: document_id falls back to slug."""
        source = SahpraRecallsSource()
        raw = _make_raw(
            "sahpra_detail_kiwi_no_parens.html",
            "https://www.sahpra.org.za/document/kiwi-complete-vacuum-delivery-system/",
        )
        doc = source.parse(raw)

        assert doc.document_id == "sahpra-kiwi-complete-vacuum-delivery-system"
        assert doc.raw_metadata["registration_number"] == "N/A"

    def test_parse_multi_active_ingredient(self) -> None:
        """Title with 4 comma-separated ingredients in parens."""
        source = SahpraRecallsSource()
        # Build a minimal synthetic detail page with the Citro-Soda title
        html = b"""<!DOCTYPE html><html><body>
<article class="dlp_document">
<div class="post_text_inner">
<h1><span>Citro-Soda (Sodium bicarbonate, Tartaric acid, Citric acid, Sodium citrate)</span></h1>
<table><tbody>
<tr>
<td><strong>Company name &amp; Address:</strong></td>
<td><strong>registration number</strong></td>
<td><strong>Batch number(s)</strong></td>
<td><strong>Expiry date</strong></td>
<td><strong>Pack size</strong></td>
<td><strong>First release date</strong></td>
<td><strong>Re-call Classification</strong></td>
<td><strong>Recall date</strong></td>
</tr>
<tr>
<td>Adcock Ingram Limited, Clayville</td>
<td>A50/21.1/0034</td>
<td>B240501</td>
<td>January 2027</td>
<td>100g</td>
<td>01 March 2024</td>
<td>Class II Type A</td>
<td>27 April 2026</td>
</tr>
</tbody></table>
<p><strong><u>Brief description of the problem (reason for recall)</u></strong></p>
<p>Foreign material contamination.</p>
</div>
</article>
</body></html>"""
        ref = DocumentRef(
            source_id="sahpra_recalls",
            url="https://www.sahpra.org.za/document/citro-soda/",
            extra={"listing_page_number": 3},
        )
        raw = RawDocument(
            ref=ref,
            content=html,
            content_type="text/html; charset=utf-8",
            source_hash=hashlib.sha256(html).hexdigest(),
            fetched_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        doc = source.parse(raw)

        assert doc.product_names == ["Citro-Soda"]
        assert len(doc.active_ingredients_raw) == 4
        assert "Sodium bicarbonate" in doc.active_ingredients_raw
        assert "Sodium citrate" in doc.active_ingredients_raw

    def test_parse_class_iii_severity(self) -> None:
        """Classification containing 'Class III' maps to class_3."""
        from regulatory.models import Severity

        source = SahpraRecallsSource()
        html = b"""<!DOCTYPE html><html><body>
<h1><span>OXOID (Test)</span></h1>
<table><tbody>
<tr>
<td><strong>Company name &amp; Address:</strong></td>
<td><strong>registration number</strong></td>
<td><strong>Batch number(s)</strong></td>
<td><strong>Expiry date</strong></td>
<td><strong>Pack size</strong></td>
<td><strong>First release date</strong></td>
<td><strong>Re-call Classification</strong></td>
<td><strong>Recall date</strong></td>
</tr>
<tr>
<td>Bioweb (Pty) Ltd</td>
<td>N/A</td>
<td>6253410</td>
<td>02 June 2028</td>
<td>2ml</td>
<td>Not available</td>
<td>Class III Type C</td>
<td>04 May 2026</td>
</tr>
</tbody></table>
</body></html>"""
        ref = DocumentRef(
            source_id="sahpra_recalls",
            url="https://www.sahpra.org.za/document/oxoid/",
            extra={},
        )
        raw = RawDocument(
            ref=ref,
            content=html,
            content_type="text/html",
            source_hash=hashlib.sha256(html).hexdigest(),
            fetched_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        doc = source.parse(raw)
        assert doc.severity == Severity.class_3

    def test_parse_handles_missing_classification(self) -> None:
        """Table without Re-call Classification → severity = unclassified, no crash."""
        from regulatory.models import Severity

        source = SahpraRecallsSource()
        html = b"""<!DOCTYPE html><html><body>
<h1><span>TestProduct (TestING)</span></h1>
<table><tbody>
<tr>
<td><strong>Company name &amp; Address:</strong></td>
<td><strong>registration number</strong></td>
<td><strong>Batch number(s)</strong></td>
<td><strong>Recall date</strong></td>
</tr>
<tr>
<td>Some Company Ltd</td>
<td>12/34/0001</td>
<td>BATCH001</td>
<td>04 May 2026</td>
</tr>
</tbody></table>
</body></html>"""
        ref = DocumentRef(
            source_id="sahpra_recalls",
            url="https://www.sahpra.org.za/document/test/",
            extra={},
        )
        raw = RawDocument(
            ref=ref,
            content=html,
            content_type="text/html",
            source_hash=hashlib.sha256(html).hexdigest(),
            fetched_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        doc = source.parse(raw)
        assert doc.severity == Severity.unclassified
        assert doc.raw_metadata["recall_type"] == ""

    def test_parse_extracts_narrative_sections(self) -> None:
        """Halaven fixture: recall_reason and advice are populated."""
        source = SahpraRecallsSource()
        raw = _make_raw(
            "sahpra_detail_halaven.html",
            "https://www.sahpra.org.za/document/halaven-eribulin/",
        )
        doc = source.parse(raw)

        reason = doc.raw_metadata["recall_reason"]
        advice = doc.raw_metadata["advice"]
        assert len(reason) > 10
        assert "assay" in reason.lower() or "specification" in reason.lower()
        assert len(advice) > 10

    def test_parse_unparseable_date_logs_and_skips(self) -> None:
        """Unparseable recall date → ValueError raised, warning logged, no document."""
        source = SahpraRecallsSource()
        html = b"""<!DOCTYPE html><html><body>
<h1><span>TestProd (TestIng)</span></h1>
<table><tbody>
<tr>
<td><strong>Company name &amp; Address:</strong></td>
<td><strong>registration number</strong></td>
<td><strong>Batch number(s)</strong></td>
<td><strong>Re-call Classification</strong></td>
<td><strong>Recall date</strong></td>
</tr>
<tr>
<td>Some Co</td>
<td>XX/YY/ZZ</td>
<td>B001</td>
<td>Class II Type B</td>
<td>NOT A DATE</td>
</tr>
</tbody></table>
</body></html>"""
        ref = DocumentRef(
            source_id="sahpra_recalls",
            url="https://www.sahpra.org.za/document/test-bad-date/",
            extra={},
        )
        raw = RawDocument(
            ref=ref,
            content=html,
            content_type="text/html",
            source_hash=hashlib.sha256(html).hexdigest(),
            fetched_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        import structlog.testing

        with structlog.testing.capture_logs() as cap_logs, pytest.raises(ValueError, match="recall date"):
            source.parse(raw)

        warning_events = [e for e in cap_logs if e.get("log_level") == "warning"]
        assert any("unparseable" in e.get("event", "").lower() for e in warning_events)

    def test_parse_column_order_variant(self) -> None:
        """'Product strength' column inserted between Company and registration."""
        from regulatory.models import Severity

        source = SahpraRecallsSource()
        html = b"""<!DOCTYPE html><html><body>
<h1><span>Visipaque (Iodixanol)</span></h1>
<table width="1057"><tbody>
<tr>
<td><strong>Company name &amp; Address:</strong></td>
<td><strong>Product strength</strong></td>
<td><strong>registration number</strong></td>
<td><strong>Batch number(s)</strong></td>
<td><strong>Expiry date</strong></td>
<td><strong>Pack size</strong></td>
<td><strong>First release date</strong></td>
<td><strong>Re-call Classification</strong></td>
<td><strong>Recall date</strong></td>
</tr>
<tr>
<td>Ge Healthcare (Pty) Ltd, Johannesburg</td>
<td>Visipaque 320 mg/ml 100 ml</td>
<td>31/28/0177</td>
<td>17244629</td>
<td>August 2028</td>
<td>100 ml</td>
<td>12 December 2025</td>
<td>Class II Type B</td>
<td>20.April.2026</td>
</tr>
</tbody></table>
</body></html>"""
        ref = DocumentRef(
            source_id="sahpra_recalls",
            url="https://www.sahpra.org.za/document/visipaque-iodixanol/",
            extra={"listing_page_number": 2},
        )
        raw = RawDocument(
            ref=ref,
            content=html,
            content_type="text/html",
            source_hash=hashlib.sha256(html).hexdigest(),
            fetched_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        doc = source.parse(raw)

        assert doc.product_names == ["Visipaque"]
        assert doc.raw_metadata["registration_number"] == "31/28/0177"
        assert doc.raw_metadata["batch_numbers"] == ["17244629"]
        assert doc.severity == Severity.class_2
        assert doc.date_published == date(2026, 4, 20)

    def test_parse_active_ingredient_inn_normalization(self) -> None:
        """Halaven: 'Eribulin' should normalize (or fall back to raw)."""
        source = SahpraRecallsSource()
        raw = _make_raw(
            "sahpra_detail_halaven.html",
            "https://www.sahpra.org.za/document/halaven-eribulin/",
        )
        doc = source.parse(raw)

        assert "Eribulin" in doc.active_ingredients_raw
        assert len(doc.active_ingredients) == 1

    def test_parse_sets_jurisdiction_and_source_id(self) -> None:
        """Every parsed document has correct jurisdiction and source_id."""
        source = SahpraRecallsSource()
        raw = _make_raw(
            "sahpra_detail_halaven.html",
            "https://www.sahpra.org.za/document/halaven-eribulin/",
        )
        doc = source.parse(raw)

        assert doc.source_id == "sahpra_recalls"
        assert doc.jurisdiction == "ZA"
        assert doc.language == "en"
