"""PPB Kenya product recalls adapter (table-based).

Discovers year-specific listing pages via URL pattern probing and parses
the static HTML recall table. No JavaScript rendering required.

Landing pages follow one of three slug conventions:
  /products-recalled-{year}/
  /products-recalled-in-{year}/
  /product-recall-{year}/

Each page contains a nine-column HTML table; one row per recall event.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from typing import Any, cast
from urllib.parse import urljoin

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

_ROOT = "https://web.pharmacyboardkenya.org"

# Three known slug variants; tried in order for each year.
_YEAR_PATTERNS = [
    _ROOT + "/products-recalled-{year}/",
    _ROOT + "/products-recalled-in-{year}/",
    _ROOT + "/product-recall-{year}/",
]

_SEVERITY_PATTERN = re.compile(r"class\s+(i{1,3}|1|2|3)\b", re.IGNORECASE)

_SEVERITY_MAP: dict[str, Severity] = {
    "i": Severity.class_1,
    "1": Severity.class_1,
    "ii": Severity.class_2,
    "2": Severity.class_2,
    "iii": Severity.class_3,
    "3": Severity.class_3,
}

# Conservative country set for trailing-country extraction from manufacturer cell.
_KNOWN_COUNTRIES = frozenset(
    {
        "kenya",
        "uganda",
        "tanzania",
        "ethiopia",
        "rwanda",
        "burundi",
        "somalia",
        "south sudan",
        "nigeria",
        "ghana",
        "south africa",
        "zambia",
        "zimbabwe",
        "malawi",
        "mozambique",
        "india",
        "china",
    }
)

# Column indices within each data row (0-based).
_COL_DATE = 1
_COL_REF = 2
_COL_PRODUCT = 3
_COL_INN = 4
_COL_BATCH = 5
_COL_MFR = 6
_COL_REASON = 7
_COL_STATUS = 8


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


_RECALL_KEYWORDS = ("recall", "batch", "manufacturer", "product")


def _row_text(row: Tag) -> str:
    """Return lowercased joined text of all cells (th or td) in a row."""
    return " ".join(c.get_text(strip=True).lower() for c in row.find_all(["th", "td"]))


def _contains_recall_table(html: str) -> bool:
    """Return True if the HTML contains a recall-shaped ``<table>``.

    Checks ``<th>`` headers first, then falls back to the first ``<tr>``
    row (some WordPress themes render header rows with ``<td>`` elements).

    Args:
        html: Raw HTML string.

    Returns:
        ``True`` if a matching table is found.
    """
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        th_text = " ".join(
            th.get_text(strip=True).lower() for th in table.find_all("th")
        )
        if any(kw in th_text for kw in _RECALL_KEYWORDS):
            return True
        first_row = table.find("tr")
        if first_row and any(kw in _row_text(first_row) for kw in _RECALL_KEYWORDS):
            return True
    return False


def _find_recall_table(soup: BeautifulSoup) -> Tag | None:
    """Return the first recall-shaped ``<table>`` from a parsed page.

    Applies the same two-pass detection as :func:`_contains_recall_table`.

    Args:
        soup: Parsed BeautifulSoup tree.

    Returns:
        Matching :class:`~bs4.Tag`, or ``None``.
    """
    for tag in soup.find_all("table"):
        th_text = " ".join(
            th.get_text(strip=True).lower() for th in tag.find_all("th")
        )
        if any(kw in th_text for kw in _RECALL_KEYWORDS):
            return cast(Tag, tag)
        first_row = tag.find("tr")
        if first_row and any(kw in _row_text(first_row) for kw in _RECALL_KEYWORDS):
            return cast(Tag, tag)
    return None


def _parse_inn_cell(text: str) -> tuple[list[str], list[str]]:
    """Parse an INN cell (possibly with dosage) into (normalized, raw) lists.

    Cells may contain strength info (e.g. ``"Methyldopa 250mg"``), or
    multiple ingredients separated by newlines or semicolons.  Dosage
    suffixes are stripped before the INN lookup so that
    ``"Methyldopa 250mg"`` correctly normalizes via ``normalize("methyldopa")``.

    Args:
        text: Raw cell text.

    Returns:
        Tuple of ``(normalized_inns, raw_parts)``.
    """
    parts = [p.strip() for p in re.split(r"[\n;]", text) if p.strip()]
    if not parts:
        return [], []

    normalized: list[str] = []
    for part in parts:
        bare = re.sub(
            r"\s+\d[\d.,]*\s*(?:mg|mcg|µg|ug|g|ml|iu|units?)\b.*$",
            "",
            part,
            flags=re.IGNORECASE,
        ).strip()
        normalized.append(normalize(bare if bare else part))

    return normalized, parts


def _parse_table_row(
    row: Tag,
    year_url: str,
    row_index: int,
) -> dict[str, Any] | None:
    """Parse a recall table ``<tr>`` into a structured dict.

    Returns ``None`` for header rows or rows with too few columns.

    Args:
        row: BeautifulSoup ``<tr>`` tag.
        year_url: URL of the year listing page (used to resolve relative hrefs).
        row_index: Zero-based data-row index within the table.

    Returns:
        Dict of parsed fields, or ``None`` if the row is not a data row.
    """
    cells = row.find_all("td")
    if len(cells) < 9:
        return None

    date_text = cells[_COL_DATE].get_text(strip=True)
    recall_ref = cells[_COL_REF].get_text(strip=True)

    product_cell = cells[_COL_PRODUCT]
    anchor = product_cell.find("a", href=True)
    product_name = product_cell.get_text(strip=True)
    detail_url: str | None = None
    if anchor:
        href: str = anchor["href"]
        detail_url = href if href.startswith("http") else urljoin(year_url, href)

    inn_text = cells[_COL_INN].get_text(separator="\n", strip=True)

    batch_raw = cells[_COL_BATCH].get_text(separator="\n", strip=True)
    batches = [b.strip() for b in re.split(r"[\n,]", batch_raw) if b.strip()]

    manufacturer_text = cells[_COL_MFR].get_text(strip=True)
    reason_text = cells[_COL_REASON].get_text(strip=True)
    status_text = cells[_COL_STATUS].get_text(strip=True)

    return {
        "date_text": date_text,
        "recall_ref": recall_ref,
        "product_name": product_name,
        "detail_url": detail_url,
        "inn_text": inn_text,
        "batches": batches,
        "manufacturer_text": manufacturer_text,
        "reason_text": reason_text,
        "status_text": status_text,
        "year_page_url": year_url,
        "row_index_on_page": row_index,
        "row_html": str(row),
    }


@register_source
class PpbKeAlertsSource(RegulatorySource):
    """Adapter for PPB Kenya product recalls.

    Discovers year-specific listing pages via pattern probing and parses
    the static HTML recall table. No JavaScript rendering required.

    Extraction order:
    1. ``discover()`` fetches year pages and parses table rows.
    2. ``fetch()`` wraps the already-captured row HTML — no extra HTTP call.
    3. ``parse()`` reads structured fields from ``ref.extra``.
    """

    source_id = "ppb_ke_alerts"
    jurisdiction = "KE"
    document_types = [DocumentType.recall]

    def __init__(self) -> None:
        """Initialise the adapter."""
        self._http = HttpClient()

    async def discover(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentRef]:
        """Yield recall row references from year-specific PPB listing pages.

        Probes each calendar year's page using the three known slug patterns.
        Rows with unparseable dates are logged and skipped.

        Args:
            since: Only yield recalls initiated strictly after this datetime.

        Yields:
            One :class:`~regulatory.models.DocumentRef` per valid recall row.
        """
        now = datetime.now(tz=timezone.utc)
        since_year = since.year if since is not None else now.year - 1
        current_year = now.year

        for year in range(since_year, current_year + 1):
            found_page = False
            for pattern in _YEAR_PATTERNS:
                url = pattern.format(year=year)
                try:
                    resp = await self._http.get(url)
                except httpx.HTTPStatusError as exc:
                    log.debug(
                        "ppb_year_page_http_error",
                        url=url,
                        status=exc.response.status_code,
                    )
                    continue
                except httpx.TransportError as exc:
                    log.debug("ppb_year_page_transport_error", url=url, error=str(exc))
                    continue
                except OSError as exc:
                    log.debug("ppb_year_page_os_error", url=url, error=str(exc))
                    continue
                if resp is None or resp.status_code != 200:
                    continue
                if not _contains_recall_table(resp.text):
                    log.debug(
                        "ppb_year_page_no_recall_table",
                        url=url,
                        html_length=len(resp.text),
                    )
                    continue

                log.info("ppb_year_page_found", year=year, url=url)
                soup = BeautifulSoup(resp.text, "html.parser")
                table = _find_recall_table(soup)
                if table is None:
                    continue

                row_index = 0
                for row in table.find_all("tr"):
                    row_data = _parse_table_row(row, url, row_index)
                    if row_data is None:
                        continue
                    row_index += 1

                    pub_date = _parse_date_flexible(row_data["date_text"])
                    if pub_date is None:
                        log.warning(
                            "ppb_unparseable_date",
                            date_text=row_data["date_text"],
                            recall_ref=row_data["recall_ref"],
                        )
                        continue

                    if since is not None:
                        pub_dt = datetime(
                            pub_date.year,
                            pub_date.month,
                            pub_date.day,
                            tzinfo=timezone.utc,
                        )
                        if pub_dt <= since:
                            continue

                    recall_ref: str = row_data["recall_ref"]
                    detail_url: str | None = row_data["detail_url"]
                    if detail_url:
                        ref_url = detail_url
                    else:
                        fragment = recall_ref.replace("/", "-")
                        ref_url = f"{url.rstrip('/')}#{fragment}"

                    yield DocumentRef(
                        source_id=self.source_id,
                        url=ref_url,
                        document_id=recall_ref,
                        title=f"{row_data['product_name']} recall ({recall_ref})"[:200],
                        date_published=pub_date,
                        extra=row_data,
                    )

                found_page = True
                break  # don't try other slug patterns for this year

            if not found_page:
                log.warning("ppb_year_page_not_found", year=year)

    async def fetch(self, ref: DocumentRef) -> RawDocument:
        """Return the recall row HTML captured by ``discover()`` as a RawDocument.

        Does not make any HTTP requests; the row HTML is already embedded in
        ``ref.extra`` by ``discover()``.

        Args:
            ref: Reference from :meth:`discover`.

        Returns:
            :class:`~regulatory.models.RawDocument` containing the row HTML.
        """
        extra = ref.extra or {}
        row_html: str = extra.get("row_html", "")
        content = row_html.encode("utf-8")
        return RawDocument(
            ref=ref,
            content=content,
            content_type="text/html; charset=utf-8",
            source_hash=hashlib.sha256(content).hexdigest(),
            fetched_at=datetime.now(tz=timezone.utc),
        )

    def parse(self, raw: RawDocument) -> NormalizedDocument:
        """Extract structured fields from a recall row RawDocument.

        Reads pre-parsed fields from ``raw.ref.extra`` (populated by
        :meth:`discover`) and maps them onto :class:`~regulatory.models.NormalizedDocument`.

        Args:
            raw: The raw document from :meth:`fetch`.

        Returns:
            Fully populated :class:`~regulatory.models.NormalizedDocument`.
        """
        extra = raw.ref.extra or {}

        product_name: str = extra.get("product_name", "")
        recall_ref: str = extra.get("recall_ref", raw.ref.document_id or "")
        inn_text: str = extra.get("inn_text", "")
        batches: list[str] = list(extra.get("batches", []))
        manufacturer_text: str = extra.get("manufacturer_text", "")
        reason_text: str = extra.get("reason_text", "")
        status_text: str = extra.get("status_text", "")
        year_page_url: str = extra.get("year_page_url", str(raw.ref.url))
        row_index: int = int(extra.get("row_index_on_page", 0))

        normalized_ingredients, raw_ingredients = _parse_inn_cell(inn_text)

        manufacturers = [manufacturer_text] if manufacturer_text else []

        # Conservative: only extract trailing country when it matches the known set.
        regions_affected: list[str] = []
        if manufacturer_text:
            parts = [p.strip() for p in manufacturer_text.rsplit(",", 1)]
            if len(parts) == 2 and parts[1].lower() in _KNOWN_COUNTRIES:
                regions_affected = [parts[1].strip()]

        raw_text = "\n".join(
            t for t in [product_name, inn_text, manufacturer_text, reason_text, status_text] if t
        )

        pub_date = raw.ref.date_published or date.today()
        title = (
            f"{product_name} recall ({recall_ref})"
            if product_name
            else (recall_ref or "PPB recall")
        )

        return NormalizedDocument(
            source_id=self.source_id,
            source_url=raw.ref.url,
            source_hash=raw.source_hash,
            jurisdiction=self.jurisdiction,
            document_type=DocumentType.recall,
            document_id=recall_ref or None,
            title=title[:500],
            product_names=[product_name] if product_name else [],
            active_ingredients=normalized_ingredients,
            active_ingredients_raw=raw_ingredients,
            manufacturers=manufacturers,
            marketing_authorization_holders=[],
            severity=Severity.unclassified,
            date_published=pub_date,
            date_effective=None,
            regions_affected=regions_affected,
            language="en",
            raw_text=raw_text,
            raw_metadata={
                "batch_numbers": batches,
                "recall_reason": reason_text,
                "status": status_text,
                "year_page_url": year_page_url,
                "row_index_on_page": row_index,
            },
            extracted_at=raw.fetched_at,
        )
