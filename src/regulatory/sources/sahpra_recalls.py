"""SAHPRA Product Recalls adapter (listing-page + detail-page, table-based).

Discovers recalls via paginated listing at:
  https://www.sahpra.org.za/document-category/product-recall/

Each listing card links to a detail page containing a structured HTML
table with recall metadata and narrative sections (reason, advice).
No JavaScript rendering required.

Column order is NOT fixed: some pages include a "Product strength"
column between Company name and registration number. Columns are
matched by header text, not position.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone

import httpx
import structlog
from bs4 import BeautifulSoup, Tag

from regulatory.ingestion.base import RegulatorySource
from regulatory.ingestion.http import HttpClient
from regulatory.ingestion.registry import register_source
from regulatory.inn import normalize
from regulatory.models import (
    DocumentRef,
    DocumentType,
    NormalizedDocument,
    RawDocument,
    Severity,
)

log = structlog.get_logger(__name__)

_LISTING_BASE = "https://www.sahpra.org.za/document-category/product-recall/"
_MAX_PAGES = 50

_DATE_FORMATS = [
    "%d %B %Y",   # 04 May 2026
    "%d.%B.%Y",   # 20.April.2026 / 18.December.2025 (after dot normalisation)
    "%d.%B %Y",   # 26.November 2024
    "%d %b %Y",   # 10 Mar 2026
    "%d.%b.%Y",   # 17.Mar.2026 / 29.Sep.2022 (after Sept→Sep + dot normalisation)
    "%d.%b %Y",   # 29.Sep 2022 (space between abbrev month and year)
]

# Non-standard month abbreviation seen on older SAHPRA pages.
_MONTH_NORM = re.compile(r"\bSept\b", re.IGNORECASE)

# Known section-header substrings used to stop narrative extraction.
_SECTION_HEADERS = (
    "brief description",
    "advice for health",
    "advise for health",
    "proposed action",
    "reporting side effect",
    "reporting adverse",
)

_SEVERITY_RE = re.compile(r"\bclass\s+(i{1,3}|[123])\b", re.IGNORECASE)

_SEVERITY_MAP: dict[str, Severity] = {
    "i": Severity.class_1,
    "1": Severity.class_1,
    "ii": Severity.class_2,
    "2": Severity.class_2,
    "iii": Severity.class_3,
    "3": Severity.class_3,
}


def _parse_date_flexible(value: str) -> date | None:
    """Try known SAHPRA date formats.

    Args:
        value: Raw date string from table or excerpt.

    Returns:
        Parsed ``date``, or ``None`` if all formats fail.
    """
    cleaned = _MONTH_NORM.sub("Sep", value.strip())
    cleaned = re.sub(r"\s*\.\s*", ".", cleaned)   # strip spaces around dots
    cleaned = " ".join(cleaned.split())            # collapse remaining whitespace
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _infer_severity(classification_text: str) -> Severity:
    """Map Re-call Classification text to a Severity enum value.

    Matches "Class I/1/II/2/III/3" case-insensitively. The Type letter
    (A/B/C) denotes distribution scope, not severity, and is ignored.

    Args:
        classification_text: Full classification string, e.g. ``"Class II Type C"``.

    Returns:
        Corresponding :class:`~regulatory.models.Severity`, defaulting
        to ``unclassified`` when no pattern matches.
    """
    m = _SEVERITY_RE.search(classification_text)
    if m:
        return _SEVERITY_MAP.get(m.group(1).lower(), Severity.unclassified)
    return Severity.unclassified


def _parse_title(title: str) -> tuple[str, list[str]]:
    """Split ``"Product Name (Active Ingredient, ...)"``.

    Args:
        title: Full page title text.

    Returns:
        Tuple of ``(product_name, active_ingredients_raw)``.
        ``active_ingredients_raw`` is empty when the title has no
        parenthesised section.
    """
    m = re.match(r"^(.+?)\s*\(([^)]+)\)\s*$", title.strip())
    if m:
        product = m.group(1).strip()
        ingredients = [p.strip() for p in m.group(2).split(",") if p.strip()]
        return product, ingredients
    return title.strip(), []


def _cell_text(cell: Tag) -> str:
    """Return clean multi-line text from a table cell.

    Strips non-breaking spaces and blank lines produced by the
    ``&nbsp;</p><p>value`` pattern common in SAHPRA tables.

    Args:
        cell: BeautifulSoup ``<td>`` tag.

    Returns:
        Newline-joined non-empty lines.
    """
    raw = cell.get_text(separator="\n", strip=True)
    lines = [ln for ln in raw.replace("\xa0", "").splitlines() if ln.strip()]
    return "\n".join(lines)


def _parse_recall_table(table: Tag) -> dict[str, str]:
    """Parse a SAHPRA recall table into a header-to-value mapping.

    The table uses ``<td><strong>Header</strong></td>`` for columns (no
    ``<th>``).  Column position is NOT assumed stable — some pages insert
    a "Product strength" column.

    Args:
        table: BeautifulSoup ``<table>`` tag.

    Returns:
        Dict mapping normalised column header text to cell text.
        Empty dict if the table has fewer than two rows.
    """
    rows = table.find_all("tr")
    if len(rows) < 2:
        return {}

    header_cells = rows[0].find_all("td")
    headers: list[str] = []
    for cell in header_cells:
        strong = cell.find("strong")
        if strong is not None:
            text = strong.get_text(strip=True)
            headers.append(text if text else cell.get_text(strip=True))
        else:
            headers.append(cell.get_text(strip=True))

    data_cells = rows[1].find_all("td")
    return {
        header: _cell_text(data_cells[i])
        for i, header in enumerate(headers)
        if i < len(data_cells)
    }


def _get_field(fields: dict[str, str], *keys: str) -> str:
    """Case-insensitive substring lookup into a table field dict.

    Args:
        fields: Mapping produced by :func:`_parse_recall_table`.
        *keys: Substrings to search for in header names, tried in order.

    Returns:
        First matching value, or empty string.
    """
    for key in keys:
        key_norm = re.sub(r"[\s\-]", "", key.lower())
        for header, val in fields.items():
            header_norm = re.sub(r"[\s\-]", "", header.lower())
            if key_norm in header_norm:
                return val
    return ""


def _extract_narrative(soup: BeautifulSoup, label: str) -> str:
    """Extract text from a narrative section identified by its label.

    Handles two formats:
    - Block: ``<p><strong><u>Label</u></strong></p>`` followed by ``<p>`` siblings.
    - Inline: ``<p><em>Label:</em> text in same paragraph</p>``.

    Args:
        soup: Parsed page tree.
        label: Section label substring (case-insensitive).

    Returns:
        Extracted text, or empty string if the section is not found.
    """
    for el in soup.find_all(["strong", "b", "em", "u"]):
        if label.lower() not in el.get_text().lower():
            continue
        parent = el.parent
        if parent is None:
            continue

        parent_text = str(parent.get_text(strip=True)).replace("\xa0", "")
        el_text = str(el.get_text(strip=True))

        # Inline case: value sits in the same <p> as the label.
        if parent_text != el_text and parent_text.startswith(el_text):
            return parent_text[len(el_text) :].lstrip(":").strip()

        # Block case: collect following <p> siblings until next section header.
        parts: list[str] = []
        for sib in parent.find_next_siblings("p"):
            sib_text = sib.get_text(strip=True).replace("\xa0", "")
            if not sib_text:
                continue
            first_inline = sib.find(["strong", "b", "em"])
            if first_inline:
                first_text = first_inline.get_text().lower()
                if any(h in first_text for h in _SECTION_HEADERS):
                    break
            parts.append(sib_text)
        return "\n".join(parts)
    return ""


def _extract_listing_recall_date(article: Tag) -> date | None:
    """Try to extract the recall date from the listing card excerpt.

    The excerpt text includes the recall table values; the recall date
    appears as the last date-formatted line before "Brief description".

    Args:
        article: BeautifulSoup ``<article>`` element from the listing page.

    Returns:
        Parsed recall date, or ``None`` if extraction fails.
    """
    excerpt = article.find("p", class_="post_excerpt")
    if not excerpt:
        return None
    lines = [
        ln.strip()
        for ln in excerpt.get_text(separator="\n").splitlines()
        if ln.strip() and ln.strip() != "\xa0"
    ]
    # Scan backwards from the "Brief description" marker.
    for i, line in enumerate(lines):
        if "brief description" in line.lower():
            for j in range(i - 1, max(i - 6, -1), -1):
                d = _parse_date_flexible(lines[j])
                if d is not None:
                    return d
            break
    return None


@register_source
class SahpraRecallsSource(RegulatorySource):
    """Adapter for SAHPRA Product Recalls.

    Discovers recalls via paginated listing pages and parses structured
    HTML tables from individual detail pages.

    Extraction order:
    1. ``discover()`` paginates listing pages, yields a ``DocumentRef``
       per recall card with the detail page URL.
    2. ``fetch()`` downloads the detail page HTML.
    3. ``parse()`` extracts all structured fields.
    """

    source_id = "sahpra_recalls"
    jurisdiction = "ZA"
    check_for_updates = True
    document_types = [DocumentType.recall]

    LISTING_BASE = _LISTING_BASE
    MAX_PAGES = _MAX_PAGES

    def __init__(self) -> None:
        """Initialise the adapter."""
        self._http = HttpClient()

    async def discover(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentRef]:
        """Yield recall document references from SAHPRA listing pages.

        Paginates until a 404, an empty page, or ``MAX_PAGES`` is
        reached. When ``since`` is provided and the listing excerpt
        allows date extraction, cards older than ``since`` are skipped
        and pagination stops when all cards on a page pre-date ``since``.

        Args:
            since: Only yield recalls initiated strictly after this datetime.

        Yields:
            One :class:`~regulatory.models.DocumentRef` per recall card.
        """
        for page_num in range(1, self.MAX_PAGES + 1):
            url = (
                self.LISTING_BASE
                if page_num == 1
                else f"{self.LISTING_BASE}page/{page_num}/"
            )
            try:
                resp = await self._http.get(url)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    log.info("sahpra_listing_end", page=page_num, reason="404")
                else:
                    log.warning(
                        "sahpra_listing_http_error",
                        page=page_num,
                        status=exc.response.status_code,
                    )
                break
            except (httpx.TransportError, OSError) as exc:
                log.warning(
                    "sahpra_listing_transport_error", page=page_num, error=str(exc)
                )
                break

            if resp is None or resp.status_code != 200:
                log.info("sahpra_listing_end", page=page_num, reason="non-200")
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            articles = soup.find_all(
                "article",
                class_=lambda c: c and "dlp_document" in c,
            )
            if not articles:
                log.info("sahpra_listing_empty_page", page=page_num)
                break

            log.debug("sahpra_listing_page", page=page_num, count=len(articles))

            all_before_since = True

            for article in articles:
                h2 = article.find("h2", class_="entry_title")
                if h2 is None:
                    all_before_since = False
                    continue
                link = h2.find("a", href=True)
                if link is None:
                    all_before_since = False
                    continue

                detail_url: str = link["href"]
                title = link.get_text(strip=True)

                card_date = _extract_listing_recall_date(article)

                if since is not None and card_date is not None:
                    card_dt = datetime(
                        card_date.year,
                        card_date.month,
                        card_date.day,
                        tzinfo=timezone.utc,
                    )
                    if card_dt <= since:
                        continue
                    all_before_since = False
                else:
                    all_before_since = False

                yield DocumentRef(
                    source_id=self.source_id,
                    url=detail_url,
                    document_id=None,
                    title=title,
                    extra={"listing_page_number": page_num},
                )

            if since is not None and all_before_since and len(articles) > 0:
                log.info(
                    "sahpra_early_stop",
                    page=page_num,
                    reason="all_dates_before_since",
                )
                break

    async def fetch(self, ref: DocumentRef) -> RawDocument:
        """Download the detail page HTML for a recall.

        Args:
            ref: Reference from :meth:`discover`.

        Returns:
            :class:`~regulatory.models.RawDocument` with the full page HTML.

        Raises:
            httpx.HTTPStatusError: On 4xx/5xx after retries.
            httpx.TransportError: On network-level failure.
        """
        try:
            resp = await self._http.get(str(ref.url))
        except httpx.HTTPStatusError as exc:
            log.warning(
                "sahpra_detail_fetch_error",
                url=str(ref.url),
                status=exc.response.status_code,
            )
            raise
        except (httpx.TransportError, OSError) as exc:
            log.warning(
                "sahpra_detail_transport_error", url=str(ref.url), error=str(exc)
            )
            raise

        if resp is None:
            raise ValueError(f"No response for {ref.url}")

        content = resp.content
        return RawDocument(
            ref=ref,
            content=content,
            content_type=resp.headers.get("content-type", "text/html; charset=utf-8"),
            source_hash=hashlib.sha256(content).hexdigest(),
            fetched_at=datetime.now(tz=timezone.utc),
        )

    def parse(self, raw: RawDocument) -> NormalizedDocument:
        """Extract structured fields from a recall detail page.

        Args:
            raw: The raw document from :meth:`fetch`.

        Returns:
            Fully populated :class:`~regulatory.models.NormalizedDocument`.
        """
        soup = BeautifulSoup(raw.content, "html.parser")

        # Title
        h1 = soup.find("h1")
        h1_tag = h1 if isinstance(h1, Tag) else None
        span = h1_tag.find("span") if h1_tag else None
        span_tag = span if isinstance(span, Tag) else None
        title_el = span_tag or h1_tag
        title_text = title_el.get_text(strip=True) if title_el else ""
        product_name, active_ingredients_raw = _parse_title(title_text)

        # Table
        table = soup.find("table")
        fields = _parse_recall_table(table) if isinstance(table, Tag) else {}

        manufacturer_full = _get_field(fields, "company name")
        reg_number = _get_field(fields, "registration number")
        batch_raw = _get_field(fields, "batch number")
        expiry_raw = _get_field(fields, "expiry date")
        pack_size = _get_field(fields, "pack size")
        first_release_raw = _get_field(fields, "first release")
        classification = _get_field(fields, "re-call classification", "recall classification")
        recall_date_raw = _get_field(fields, "recall date")

        # Manufacturer name vs address split
        mfr_lines = [ln for ln in manufacturer_full.splitlines() if ln.strip()]
        manufacturer_name = mfr_lines[0] if mfr_lines else ""
        manufacturer_address = "\n".join(mfr_lines[1:]) if len(mfr_lines) > 1 else ""

        # Batch numbers: one per non-empty line
        batch_numbers = [b for b in (b.strip() for b in batch_raw.splitlines()) if b]

        # Severity
        severity = _infer_severity(classification)

        # Recall date
        recall_date = _parse_date_flexible(recall_date_raw)
        if recall_date is None:
            log.warning(
                "sahpra_unparseable_recall_date",
                date_raw=recall_date_raw,
                url=str(raw.ref.url),
            )
            raise ValueError(
                f"Cannot parse recall date {recall_date_raw!r} from {raw.ref.url}"
            )

        # Document ID: registration number when valid, else slug-derived
        url_str = str(raw.ref.url).rstrip("/")
        slug = url_str.split("/")[-1]
        doc_id = (
            reg_number
            if reg_number and reg_number.upper() != "N/A"
            else f"sahpra-{slug}"
        )

        # Narrative sections
        recall_reason = _extract_narrative(soup, "Brief description")
        advice = _extract_narrative(soup, "Advice for health")
        if not advice:
            advice = _extract_narrative(soup, "Advise for health")
        proposed_action = _extract_narrative(soup, "Proposed action")
        if proposed_action:
            advice = f"{advice}\n\n{proposed_action}".strip() if advice else proposed_action

        # SharePoint PDF download link (do not fetch in this PR)
        pdf_url: str | None = None
        dl_link = soup.find("a", class_="document-library-pro-button")
        if isinstance(dl_link, Tag) and dl_link.get("href"):
            pdf_url = str(dl_link["href"])

        # INN normalization
        normalized_ingredients = [normalize(ing) for ing in active_ingredients_raw]

        # First release date (optional; store raw string if not parseable)
        first_release_date = _parse_date_flexible(first_release_raw)
        first_release_stored: str = (
            first_release_date.isoformat() if first_release_date else first_release_raw
        )

        listing_page: int = int((raw.ref.extra or {}).get("listing_page_number", 0))

        raw_text = "\n".join(
            t
            for t in [
                title_text,
                manufacturer_full,
                batch_raw,
                classification,
                recall_reason,
                advice,
            ]
            if t
        )

        return NormalizedDocument(
            source_id=self.source_id,
            source_url=raw.ref.url,
            source_hash=raw.source_hash,
            jurisdiction=self.jurisdiction,
            document_type=DocumentType.recall,
            document_id=doc_id or None,
            title=title_text[:500],
            product_names=[product_name] if product_name else [],
            active_ingredients=normalized_ingredients,
            active_ingredients_raw=active_ingredients_raw,
            manufacturers=[manufacturer_name] if manufacturer_name else [],
            marketing_authorization_holders=[],
            severity=severity,
            date_published=recall_date,
            date_effective=None,
            regions_affected=["ZA"],
            language="en",
            raw_text=raw_text,
            raw_metadata={
                "manufacturer_address": manufacturer_address,
                "registration_number": reg_number,
                "batch_numbers": batch_numbers,
                "expiry_date_raw": expiry_raw,
                "pack_size": pack_size,
                "first_release_date": first_release_stored,
                "recall_type": classification,
                "recall_reason": recall_reason,
                "advice": advice,
                "pdf_url": pdf_url,
                "listing_page_number": listing_page,
            },
            extracted_at=raw.fetched_at,
        )
