"""PPB Kenya product-alerts / recalls adapter.

Scrapes the Pharmacy and Poisons Board (PPB) Kenya website for product
alerts and recalls. PDFs linked from alert entries are extracted with the
three-stage fallback chain (pdfplumber → PyMuPDF → Anthropic API).

Landing page discovery starts at https://web.pharmacyboardkenya.org/ and
navigates to the product alerts / recalls section. The exact URL is
verified at runtime rather than hardcoded, as PPB has historically
restructured their site.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urljoin

import structlog
from bs4 import BeautifulSoup

from regulatory.ingestion.base import RegulatorySource
from regulatory.ingestion.http import HttpClient
from regulatory.ingestion.pdf import extract_structured, extract_text
from regulatory.ingestion.registry import register_source
from regulatory.inn import normalize_list
from regulatory.llm.pdf_extract import PPB_ALERT_SCHEMA
from regulatory.models import (
    DocumentRef,
    DocumentType,
    NormalizedDocument,
    RawDocument,
    Severity,
)

log = structlog.get_logger(__name__)

_ROOT_URL = "https://web.pharmacyboardkenya.org/"
_ALERT_KEYWORDS = ["alert", "recall", "product alert", "safety alert", "market withdrawal"]
_SEVERITY_PATTERN = re.compile(r"class\s+(i{1,3}|1|2|3)\b", re.IGNORECASE)

_SEVERITY_MAP: dict[str, Severity] = {
    "i": Severity.class_1,
    "1": Severity.class_1,
    "ii": Severity.class_2,
    "2": Severity.class_2,
    "iii": Severity.class_3,
    "3": Severity.class_3,
}


def _infer_severity(text: str) -> Severity | None:
    """Attempt to infer recall class from free text.

    Args:
        text: Extracted text from the alert document.

    Returns:
        :class:`~regulatory.models.Severity` if detectable, else ``None``.
    """
    m = _SEVERITY_PATTERN.search(text)
    if m:
        key = m.group(1).lower()
        return _SEVERITY_MAP.get(key, Severity.unclassified)
    return None


def _parse_date_flexible(value: str) -> date | None:
    """Try several common date formats used by PPB.

    Args:
        value: Raw date string.

    Returns:
        Parsed ``date``, or ``None`` if all formats fail.
    """
    formats = ["%d %B %Y", "%B %d, %Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"]
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


async def _discover_alerts_url(client: HttpClient) -> str | None:
    """Navigate the PPB homepage to find the alerts / recalls section URL.

    Checks anchor text and href patterns for keywords like 'alert', 'recall'.

    Args:
        client: Shared :class:`~regulatory.ingestion.http.HttpClient`.

    Returns:
        Absolute URL of the alerts listing page, or ``None`` if not found.
    """
    resp = await client.get(_ROOT_URL)
    if resp is None:
        log.error("ppb_root_unreachable", url=_ROOT_URL)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    candidates: list[tuple[int, str]] = []

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        text: str = a.get_text(strip=True).lower()
        combined = (text + " " + href).lower()
        score = sum(kw in combined for kw in _ALERT_KEYWORDS)
        if score > 0:
            full_url = urljoin(_ROOT_URL, href)
            candidates.append((score, full_url))

    if not candidates:
        log.warning("ppb_no_alert_links_found", root=_ROOT_URL)
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0][1]
    log.info("ppb_alerts_url_discovered", url=best, score=candidates[0][0])
    return best


async def _scrape_alert_listing(
    client: HttpClient,
    listing_url: str,
) -> list[tuple[str, str, str | None]]:
    """Scrape an alerts listing page for PDF links + titles + dates.

    Args:
        client: Shared HTTP client.
        listing_url: URL of the alerts listing page.

    Returns:
        List of ``(pdf_url, title, raw_date_string)`` tuples.
    """
    resp = await client.get(listing_url)
    if resp is None:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results: list[tuple[str, str, str | None]] = []

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if not href.lower().endswith(".pdf") and ".pdf?" not in href.lower():
            continue
        pdf_url = urljoin(listing_url, href)
        title = a.get_text(strip=True) or pdf_url.split("/")[-1]

        # Attempt to find a nearby date string in the parent element
        parent_text = ""
        parent = a.parent
        if parent:
            parent_text = parent.get_text(separator=" ", strip=True)
        date_match = re.search(
            r"\b(\d{1,2}\s+\w+\s+\d{4}|\w+\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})\b",
            parent_text,
        )
        raw_date = date_match.group(0) if date_match else None
        results.append((pdf_url, title, raw_date))

    log.info("ppb_alert_pdfs_found", listing_url=listing_url, count=len(results))
    return results


@register_source
class PpbKeAlertsSource(RegulatorySource):
    """Adapter for PPB Kenya product alerts and recalls.

    Discovers PDF links from the PPB alerts listing page. Extracts text via
    the three-stage fallback chain. Stores Postgres-backed resume state so
    interrupted runs can continue where they left off.
    """

    source_id = "ppb_ke_alerts"
    jurisdiction = "KE"
    document_types = [DocumentType.alert, DocumentType.recall]

    def __init__(self) -> None:
        """Initialise the adapter."""
        self._http = HttpClient()

    async def discover(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentRef]:
        """Yield PDF document references from the PPB alerts listing page.

        Navigates the PPB homepage to find the alerts section, then scrapes
        all PDF links. Applies date filtering when *since* is provided.

        Args:
            since: Only yield alerts published after this date.

        Yields:
            One :class:`~regulatory.models.DocumentRef` per PDF found.
        """
        alerts_url = await _discover_alerts_url(self._http)
        if alerts_url is None:
            log.error("ppb_alerts_url_not_found")
            return

        pdfs = await _scrape_alert_listing(self._http, alerts_url)

        for pdf_url, title, raw_date in pdfs:
            pub_date = _parse_date_flexible(raw_date) if raw_date else None

            if since is not None and pub_date is not None:
                pub_dt = datetime(pub_date.year, pub_date.month, pub_date.day, tzinfo=timezone.utc)
                if pub_dt <= since:
                    continue

            yield DocumentRef(
                source_id=self.source_id,
                url=pdf_url,
                title=title[:200],
                date_published=pub_date,
            )

    async def fetch(self, ref: DocumentRef) -> RawDocument:
        """Download the PDF bytes for an alert reference.

        Args:
            ref: Reference from :meth:`discover`.

        Returns:
            :class:`~regulatory.models.RawDocument` with PDF content.

        Raises:
            RuntimeError: If the HTTP request returns no response.
        """
        resp = await self._http.get(str(ref.url))

        if resp is None:
            raise RuntimeError(f"No response fetching {ref.url}")

        content = resp.content
        return RawDocument(
            ref=ref,
            content=content,
            content_type=resp.headers.get("content-type", "application/pdf"),
            source_hash=hashlib.sha256(content).hexdigest(),
            fetched_at=datetime.now(tz=timezone.utc),
        )

    def parse(self, raw: RawDocument) -> NormalizedDocument:
        """Extract structured fields from a PPB alert PDF.

        Extraction order:
        1. pdfplumber
        2. PyMuPDF
        3. Anthropic API (structured JSON extraction)

        Args:
            raw: The raw PDF document from :meth:`fetch`.

        Returns:
            Fully populated :class:`~regulatory.models.NormalizedDocument`.
        """
        pdf_bytes = raw.content
        source_hash = raw.source_hash

        # Stage 1 & 2: text extraction for raw_text field
        try:
            raw_text, method = extract_text(pdf_bytes, source_hash)
        except RuntimeError:
            raw_text = ""
            method = "failed"

        log.debug("ppb_pdf_extracted", method=method, url=str(raw.ref.url))

        # Stage 3: structured extraction via Anthropic (or parse text heuristically)
        structured: dict[str, Any] | None = None
        if method in ("pdfplumber", "pymupdf") and raw_text:
            structured = self._parse_text_heuristic(raw_text)
        else:
            structured = extract_structured(pdf_bytes, source_hash, PPB_ALERT_SCHEMA)

        structured = structured or {}

        raw_ingredients = structured.get("active_ingredients") or []
        if isinstance(raw_ingredients, str):
            raw_ingredients = [raw_ingredients]
        normalized_ingredients, raw_ingredients_list = normalize_list(list(raw_ingredients))

        raw_manufacturers = structured.get("manufacturers") or []
        if isinstance(raw_manufacturers, str):
            raw_manufacturers = [raw_manufacturers]
        manufacturers = [str(m).strip() for m in raw_manufacturers if m]

        raw_products = structured.get("product_names") or []
        if isinstance(raw_products, str):
            raw_products = [raw_products]
        product_names = [str(p).strip() for p in raw_products if p]

        # Infer date from structured data or ref
        pub_date: date
        if structured.get("date_published"):
            parsed = _parse_date_flexible(str(structured["date_published"]))
            pub_date = parsed if parsed else (raw.ref.date_published or date.today())
        else:
            pub_date = raw.ref.date_published or date.today()

        severity: Severity | None = None
        if structured.get("severity"):
            try:
                severity = Severity(str(structured["severity"]))
            except ValueError:
                severity = _infer_severity(raw_text)
        else:
            severity = _infer_severity(raw_text)

        title = structured.get("title") or raw.ref.title or str(raw.ref.url).split("/")[-1]

        regions_raw = structured.get("regions_affected") or []
        if isinstance(regions_raw, str):
            regions_raw = [regions_raw]
        regions = [str(r).strip() for r in regions_raw if r]

        return NormalizedDocument(
            source_id=self.source_id,
            source_url=raw.ref.url,
            source_hash=raw.source_hash,
            jurisdiction=self.jurisdiction,
            document_type=DocumentType.alert,
            document_id=None,
            title=str(title)[:500],
            product_names=product_names,
            active_ingredients=normalized_ingredients,
            active_ingredients_raw=raw_ingredients_list,
            manufacturers=manufacturers,
            marketing_authorization_holders=[],
            severity=severity,
            date_published=pub_date,
            date_effective=None,
            regions_affected=regions,
            language="en",
            raw_text=raw_text,
            raw_metadata={
                "extraction_method": method,
                "batch_numbers": structured.get("batch_numbers", []),
                "reason": structured.get("reason", ""),
                **{
                    k: v
                    for k, v in structured.items()
                    if k
                    not in (
                        "title",
                        "product_names",
                        "active_ingredients",
                        "manufacturers",
                        "batch_numbers",
                        "reason",
                        "date_published",
                        "severity",
                        "regions_affected",
                    )
                },
            },
            extracted_at=raw.fetched_at,
        )

    @staticmethod
    def _parse_text_heuristic(text: str) -> dict[str, Any]:
        """Extract key fields from plain text using simple regex heuristics.

        This is a best-effort supplement for well-structured PPB PDFs.

        Args:
            text: Full extracted text from the PDF.

        Returns:
            Partial dict with whatever fields could be detected.
        """
        result: dict[str, Any] = {}

        # Product name: first line or "Product:" label
        product_match = re.search(r"(?:product\s*[:\-]?\s*)(.+)", text, re.IGNORECASE)
        if product_match:
            result["product_names"] = [product_match.group(1).strip()[:200]]

        # Manufacturer
        mfr_match = re.search(
            r"(?:manufacturer|manufactured\s+by)\s*[:\-]?\s*(.+)", text, re.IGNORECASE
        )
        if mfr_match:
            result["manufacturers"] = [mfr_match.group(1).strip()[:200]]

        # Batch numbers
        batch_match = re.search(
            r"(?:batch|lot)\s*(?:number|no\.?|#)?\s*[:\-]?\s*([A-Z0-9,\s\-/]+)",
            text,
            re.IGNORECASE,
        )
        if batch_match:
            batches = [b.strip() for b in re.split(r"[,;]", batch_match.group(1)) if b.strip()]
            result["batch_numbers"] = batches

        # Reason for recall
        reason_match = re.search(
            r"(?:reason\s+for\s+recall|reason\s+for\s+withdrawal|reason)\s*[:\-]?\s*(.+)",
            text,
            re.IGNORECASE,
        )
        if reason_match:
            result["reason"] = reason_match.group(1).strip()[:500]

        return result
