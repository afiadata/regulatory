"""openFDA drug enforcement adapter.

Fetches drug enforcement / recall reports from the FDA's public API.

API docs: https://open.fda.gov/apis/drug/enforcement/
Base URL:  https://api.fda.gov/drug/enforcement.json

Supports incremental ingestion via ``report_date`` range filter.
Optional API key raises rate limit from 240 req/min to 120 k/day.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from typing import Any

import structlog

from regulatory.ingestion.base import RegulatorySource
from regulatory.ingestion.http import HttpClient
from regulatory.ingestion.registry import register_source
from regulatory.inn import normalize_list
from regulatory.models import (
    DocumentRef,
    DocumentType,
    NormalizedDocument,
    RawDocument,
    Severity,
)

log = structlog.get_logger(__name__)

_BASE_URL = "https://api.fda.gov/drug/enforcement.json"
_PAGE_SIZE = 100

_FDA_SEVERITY_MAP: dict[str, Severity] = {
    "Class I": Severity.class_1,
    "Class II": Severity.class_2,
    "Class III": Severity.class_3,
}


def _map_severity(raw: str | None) -> Severity | None:
    """Map an FDA classification string to our :class:`~regulatory.models.Severity` enum.

    Args:
        raw: FDA ``classification`` field value, e.g. ``"Class I"``.

    Returns:
        Matching :class:`~regulatory.models.Severity`, or ``None`` if unmapped.
    """
    if not raw:
        return None
    return _FDA_SEVERITY_MAP.get(raw.strip(), Severity.unclassified)


def _parse_fda_date(value: str | None) -> date | None:
    """Parse an FDA date string (``YYYYMMDD``) into a ``date``.

    Args:
        value: Date string in ``YYYYMMDD`` format.

    Returns:
        Parsed ``date``, or ``None`` if *value* is missing or unparseable.
    """
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y%m%d").date()
    except ValueError:
        log.warning("fda_date_parse_failed", value=value)
        return None


def _coerce_list(value: str | list[str] | None) -> list[str]:
    """Coerce an FDA field that may be a string or list into a clean list.

    Args:
        value: Raw FDA field value.

    Returns:
        List of non-empty stripped strings.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [v.strip() for v in value if v and v.strip()]
    return [s.strip() for s in str(value).split(";") if s.strip()]


@register_source
class OpenFdaDrugSource(RegulatorySource):
    """Adapter for openFDA drug enforcement reports.

    Supports incremental ingestion. Results are paginated; the adapter
    walks all pages for the requested date range.
    """

    source_id = "openfda_drug"
    jurisdiction = "US"
    document_types = [DocumentType.recall, DocumentType.enforcement]

    def __init__(self) -> None:
        """Initialise the adapter and pick up the optional API key."""
        self._api_key = os.environ.get("OPENFDA_API_KEY")
        self._http = HttpClient()

    async def discover(
        self,
        since: datetime | None = None,
    ) -> AsyncIterator[DocumentRef]:
        """Yield :class:`~regulatory.models.DocumentRef` for each enforcement report.

        Paginates through the FDA API. Filters by ``report_date`` when *since*
        is provided.

        Args:
            since: Only yield records reported on or after this date.

        Yields:
            One :class:`~regulatory.models.DocumentRef` per FDA record.
        """
        params: dict[str, str] = {"limit": str(_PAGE_SIZE)}
        if self._api_key:
            params["api_key"] = self._api_key

        if since is not None:
            since_str = since.strftime("%Y%m%d")
            today_str = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
            params["search"] = f"report_date:[{since_str}+TO+{today_str}]"

        skip = 0
        while True:
            params["skip"] = str(skip)
            async with HttpClient() as client:
                resp = await client.get(_BASE_URL, params=params)
            if resp is None:
                break

            try:
                data: dict[str, Any] = resp.json()
            except Exception as exc:
                log.error("fda_json_parse_failed", error=str(exc))
                break

            results: list[dict[str, Any]] = data.get("results", [])
            if not results:
                break

            for record in results:
                recall_number = record.get("recall_number", "")
                url = f"{_BASE_URL}?search=recall_number:{recall_number}"
                pub_date = _parse_fda_date(record.get("report_date"))
                yield DocumentRef(
                    source_id=self.source_id,
                    url=url,  # type: ignore[arg-type]
                    document_id=recall_number or None,
                    title=record.get("reason_for_recall", "")[:200],
                    date_published=pub_date,
                )

            total: int = data.get("meta", {}).get("results", {}).get("total", 0)
            skip += _PAGE_SIZE
            if skip >= total:
                break

    async def fetch(self, ref: DocumentRef) -> RawDocument:
        """Return the raw JSON bytes for an enforcement record.

        We re-encode the JSON to bytes so the content-hash pipeline works
        uniformly across all adapters.

        Args:
            ref: Reference from :meth:`discover`.

        Returns:
            :class:`~regulatory.models.RawDocument` with JSON content.
        """

        async with HttpClient() as client:
            resp = await client.get(str(ref.url))

        if resp is None:
            raise RuntimeError(f"No response fetching {ref.url}")

        content = resp.content
        return RawDocument(
            ref=ref,
            content=content,
            content_type="application/json",
            source_hash=hashlib.sha256(content).hexdigest(),
            fetched_at=datetime.now(tz=timezone.utc),
        )

    def parse(self, raw: RawDocument) -> NormalizedDocument:
        """Parse raw FDA JSON into a :class:`~regulatory.models.NormalizedDocument`.

        Args:
            raw: The raw document from :meth:`fetch`.

        Returns:
            Fully populated :class:`~regulatory.models.NormalizedDocument`.

        Raises:
            ValueError: If the JSON does not contain at least one result.
        """
        import json

        data: dict[str, Any] = json.loads(raw.content)
        results: list[dict[str, Any]] = data.get("results", [])
        if not results:
            raise ValueError("No results in FDA response.")
        record = results[0]

        openfda: dict[str, Any] = record.get("openfda", {})
        raw_ingredients = _coerce_list(openfda.get("generic_name"))
        normalized_ingredients, raw_ingredients_list = normalize_list(raw_ingredients)

        pub_date = _parse_fda_date(record.get("report_date")) or date.today()
        effective_date = _parse_fda_date(record.get("recall_initiation_date"))

        distribution = record.get("distribution_pattern", "")
        regions = [r.strip() for r in distribution.split(";") if r.strip()] if distribution else []

        return NormalizedDocument(
            source_id=self.source_id,
            source_url=raw.ref.url,
            source_hash=raw.source_hash,
            jurisdiction=self.jurisdiction,
            document_type=DocumentType.recall,
            document_id=record.get("recall_number"),
            title=record.get("reason_for_recall", "")[:500] or record.get("recall_number", ""),
            product_names=_coerce_list(record.get("product_description")),
            active_ingredients=normalized_ingredients,
            active_ingredients_raw=raw_ingredients_list,
            manufacturers=[record.get("recalling_firm", "").strip()],
            marketing_authorization_holders=[record.get("firm_fei_number", "")],
            severity=_map_severity(record.get("classification")),
            date_published=pub_date,
            date_effective=effective_date,
            regions_affected=regions,
            language="en",
            raw_text=str(record),
            raw_metadata=record,
            extracted_at=raw.fetched_at,
        )
