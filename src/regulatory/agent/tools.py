"""Read-only tool implementations for the regulatory agent.

All six tools query the database through a caller-supplied AsyncSession.
No tool accepts arbitrary SQL; every query uses bound parameters or ORM
expressions.  Input strings are validated via regex before reaching the DB.

The TOOL_DEFINITIONS list provides the Anthropic API tool schemas.
``dispatch()`` maps a tool_name + tool_input dict to the correct function.
"""

from __future__ import annotations

import re
import uuid as _uuid_mod
from datetime import date
from typing import Any, Literal

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from regulatory.agent.models import (
    CountyExposure,
    DocumentDetail,
    DocumentSearchResponse,
    DocumentSearchResult,
    ManufacturerProfile,
    RecallSummary,
    RiskSignalDetail,
    RiskSignalListResponse,
    RiskSignalSummary,
    SupplyMixRow,
)
from regulatory.agent.sanitize import (
    COUNT_INFLATION_FLOOR,
    COUNT_INFLATION_OPENFDA_SHARE,
    paginate_raw_text,
    truncate_snippet,
    wrap_untrusted,
)
from regulatory.db.models import (
    County,
    CountySupply,
    Document,
    Manufacturer,
    RiskSignal,
    Supplier,
)
from regulatory.risk.persist import explain_signal

log = structlog.get_logger(__name__)

# Hard caps enforced regardless of caller-supplied limit.
_MAX_SIGNALS_LIMIT: int = 50
_MAX_DOCS_LIMIT: int = 25

# Input validation patterns.
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9\s.\-,&()/\']{1,200}$")
_QUERY_PATTERN = re.compile(r"^[\w\s.,;:!?()\-\'\"&/\[\]@#%+=*~`|^{}\\<>]{1,200}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f]")


def _validate_name(value: str, field: str) -> str:
    """Validate a name-shaped string input.

    Args:
        value: The input string to validate.
        field: Field label for error messages.

    Returns:
        The validated string.

    Raises:
        ValueError: If the string fails validation.
    """
    if not _NAME_PATTERN.match(value):
        raise ValueError(
            f"{field!r} failed validation: must match "
            r"^[a-zA-Z0-9\s.\-,&()/\']{1,200}$"
        )
    return value


def _validate_uuid(value: str, field: str) -> _uuid_mod.UUID:
    """Parse and validate a UUID string.

    Args:
        value: UUID string to validate.
        field: Field label for error messages.

    Returns:
        Parsed UUID.

    Raises:
        ValueError: If the string is not a valid UUID.
    """
    try:
        return _uuid_mod.UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field!r} is not a valid UUID: {value!r}") from exc


def _validate_query(value: str) -> str:
    """Sanitize and validate a free-text search query.

    Args:
        value: Raw query string.

    Returns:
        Cleaned query string.

    Raises:
        ValueError: If the query fails validation.
    """
    cleaned = _CONTROL_CHARS_RE.sub("", value).strip()
    if len(cleaned) > 200:
        cleaned = cleaned[:200]
    if not cleaned:
        raise ValueError("search query must not be empty after sanitization")
    return cleaned


def _compute_count_inflation_likely(
    signal: RiskSignal, evidence_docs: list[Document]
) -> bool:
    """Heuristic: flag if recall count is high AND source mix is openFDA-dominant.

    openFDA's per-SKU filing convention is the cluster-inflation source identified
    in live validation Round 3.  See docs/followup_issues/recall_event_clustering.md.
    SAHPRA and PPB Kenya have not been observed to exhibit the same pattern.

    Args:
        signal: The risk signal to evaluate.
        evidence_docs: Documents referenced in signal.evidence["document_ids"].

    Returns:
        True if count inflation is likely.
    """
    if signal.kind != "repeat_violator":
        return False
    doc_count = len(evidence_docs)
    if doc_count < COUNT_INFLATION_FLOOR:
        return False
    openfda_count = sum(1 for d in evidence_docs if d.source_id == "openfda_drug")
    return openfda_count >= COUNT_INFLATION_OPENFDA_SHARE * doc_count


async def list_risk_signals(
    session: AsyncSession,
    *,
    kind: Literal[
        "repeat_violator", "supply_chain_exposure", "cross_source_corroboration"
    ]
    | None = None,
    severity: Literal["low", "medium", "high", "critical"] | None = None,
    status: Literal["active", "resolved", "suppressed"] | None = "active",
    region: str | None = None,
    active_ingredient: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: int = 20,
) -> RiskSignalListResponse:
    """Return a filtered list of risk signal summaries.

    Args:
        session: Database session (should use regulatory_readonly role).
        kind: Optional filter by signal kind.
        severity: Optional filter by severity level.
        status: Filter by status; defaults to "active".
        region: Optional county name substring match against regions_affected.
        active_ingredient: Exact normalized INN match.
        since: Only signals with first_seen >= since.
        until: Only signals with first_seen <= until.
        limit: Maximum results; hard-capped at 50.

    Returns:
        RiskSignalListResponse with signals, total_count, and truncated flag.
    """
    effective_limit = min(max(1, limit), _MAX_SIGNALS_LIMIT)

    q = select(RiskSignal)
    if status is not None:
        q = q.where(RiskSignal.status == status)
    if kind is not None:
        q = q.where(RiskSignal.kind == kind)
    if severity is not None:
        q = q.where(RiskSignal.severity == severity)
    if active_ingredient is not None:
        q = q.where(RiskSignal.active_ingredient == active_ingredient)
    if since is not None:
        from datetime import datetime, timezone

        since_dt = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
        q = q.where(RiskSignal.first_seen >= since_dt)
    if until is not None:
        from datetime import datetime, timezone

        until_dt = datetime(until.year, until.month, until.day, 23, 59, 59, tzinfo=timezone.utc)
        q = q.where(RiskSignal.first_seen <= until_dt)

    count_q = select(func.count()).select_from(q.subquery())
    total_count: int = (await session.execute(count_q)).scalar_one()

    q = q.order_by(RiskSignal.first_seen.desc()).limit(effective_limit)
    signals = list((await session.execute(q)).scalars().all())

    # Apply region filter post-query (ARRAY @> operator for substring match).
    if region is not None:
        region_lower = region.lower()
        signals = [
            s for s in signals if any(region_lower in r.lower() for r in (s.regions_affected or []))
        ]
        total_count = len(signals)

    summaries: list[RiskSignalSummary] = []
    for sig in signals:
        mfr_name: str | None = None
        if sig.manufacturer_id is not None:
            mfr = await session.get(Manufacturer, sig.manufacturer_id)
            if mfr is not None:
                mfr_name = mfr.canonical_name

        summaries.append(
            RiskSignalSummary(
                signal_id=str(sig.id),
                kind=sig.kind,
                severity=sig.severity,
                manufacturer_canonical_name=mfr_name,
                active_ingredient=sig.active_ingredient,
                regions=list(sig.regions_affected or []),
                exposure_pct=sig.exposure_pct,
                first_seen=sig.first_seen,
                brief_description=sig.recommended_action[:200] if sig.recommended_action else "",
            )
        )

    return RiskSignalListResponse(
        signals=summaries,
        total_count=total_count,
        truncated=total_count > effective_limit,
    )


async def get_risk_signal(session: AsyncSession, *, signal_id: str) -> RiskSignalDetail:
    """Return full details for a single risk signal, including explain text.

    Args:
        session: Database session.
        signal_id: UUID string of the risk signal.

    Returns:
        RiskSignalDetail with evidence, explain text, and inflation flag.

    Raises:
        ValueError: If signal_id is not a valid UUID or the signal is not found.
    """
    uid = _validate_uuid(signal_id, "signal_id")
    sig = await session.get(RiskSignal, uid)
    if sig is None:
        raise ValueError(f"Signal not found: {signal_id!r}")

    mfr_name: str | None = None
    if sig.manufacturer_id is not None:
        mfr = await session.get(Manufacturer, sig.manufacturer_id)
        if mfr is not None:
            mfr_name = mfr.canonical_name

    doc_ids: list[str] = sig.evidence.get("document_ids", [])
    evidence_docs: list[Document] = []
    if doc_ids:
        doc_uids = [_uuid_mod.UUID(d) for d in doc_ids if _is_valid_uuid(d)]
        result = await session.execute(
            select(Document).where(Document.id.in_(doc_uids))
        )
        evidence_docs = list(result.scalars().all())

    inflation_likely = _compute_count_inflation_likely(sig, evidence_docs)
    raw_prov = sig.evidence.get("data_provenance")
    data_provenance: dict[str, Any] | None = raw_prov if isinstance(raw_prov, dict) else None

    return RiskSignalDetail(
        signal_id=str(sig.id),
        kind=sig.kind,
        severity=sig.severity,
        status=sig.status,
        manufacturer_canonical_name=mfr_name,
        manufacturer_id=str(sig.manufacturer_id) if sig.manufacturer_id else None,
        active_ingredient=sig.active_ingredient,
        regions_affected=list(sig.regions_affected or []),
        exposure_pct=sig.exposure_pct,
        alternative_supplier_count=sig.alternative_supplier_count,
        recommended_action=sig.recommended_action,
        first_seen=sig.first_seen,
        last_updated=sig.last_updated,
        evidence=dict(sig.evidence or {}),
        explain_text=explain_signal(sig),
        count_inflation_likely=inflation_likely,
        data_provenance=data_provenance,
    )


async def manufacturer_profile(
    session: AsyncSession, *, name_or_id: str
) -> ManufacturerProfile:
    """Return a full manufacturer profile including recall summary and signals.

    Args:
        session: Database session.
        name_or_id: Canonical name (exact or case-insensitive fuzzy) or UUID string.

    Returns:
        ManufacturerProfile.  If the name is ambiguous, returns disambiguation_needed=True
        with up to 5 candidates and no full profile data.

    Raises:
        ValueError: If name_or_id fails validation.
    """
    mfr: Manufacturer | None = None

    if _is_valid_uuid(name_or_id):
        uid = _uuid_mod.UUID(name_or_id)
        mfr = await session.get(Manufacturer, uid)
        if mfr is None:
            raise ValueError(f"Manufacturer not found: {name_or_id!r}")
    else:
        _validate_name(name_or_id, "name_or_id")
        # Exact case-insensitive match first.
        result = await session.execute(
            select(Manufacturer).where(
                func.lower(Manufacturer.canonical_name) == name_or_id.lower()
            )
        )
        mfr = result.scalar_one_or_none()

        if mfr is None:
            # Fuzzy: contains match, up to 5 candidates.
            result = await session.execute(
                select(Manufacturer)
                .where(
                    or_(
                        func.lower(Manufacturer.canonical_name).contains(name_or_id.lower()),
                        func.lower(Manufacturer.canonical_name).contains(name_or_id.lower()[:20]),
                    )
                )
                .limit(5)
            )
            candidates_list = list(result.scalars().all())
            if len(candidates_list) > 1:
                return ManufacturerProfile(
                    manufacturer_id="",
                    canonical_name="",
                    confidence=0.0,
                    recall_summary=RecallSummary(total=0),
                    disambiguation_needed=True,
                    candidates=[
                        {"manufacturer_id": str(c.id), "canonical_name": c.canonical_name}
                        for c in candidates_list
                    ],
                )
            if len(candidates_list) == 1:
                mfr = candidates_list[0]
            else:
                raise ValueError(f"Manufacturer not found: {name_or_id!r}")

    # Recall summary from documents.
    docs_result = await session.execute(
        select(Document).where(
            Document.canonical_manufacturer_ids.any(mfr.id)  # type: ignore[arg-type]
        )
    )
    mfr_docs = list(docs_result.scalars().all())

    by_source: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    product_names_set: set[str] = set()
    ingredients_set: set[str] = set()
    for doc in mfr_docs:
        by_source[doc.source_id] = by_source.get(doc.source_id, 0) + 1
        sev = doc.severity or "unclassified"
        by_severity[sev] = by_severity.get(sev, 0) + 1
        product_names_set.update(doc.product_names or [])
        ingredients_set.update(doc.active_ingredients or [])

    # Active risk signals for this manufacturer.
    sigs_result = await session.execute(
        select(RiskSignal).where(
            RiskSignal.manufacturer_id == mfr.id,
            RiskSignal.status == "active",
        )
    )
    active_signals = list(sigs_result.scalars().all())
    signal_summaries = [
        RiskSignalSummary(
            signal_id=str(s.id),
            kind=s.kind,
            severity=s.severity,
            manufacturer_canonical_name=mfr.canonical_name,
            active_ingredient=s.active_ingredient,
            regions=list(s.regions_affected or []),
            exposure_pct=s.exposure_pct,
            first_seen=s.first_seen,
        )
        for s in active_signals
    ]

    return ManufacturerProfile(
        manufacturer_id=str(mfr.id),
        canonical_name=mfr.canonical_name,
        aliases=list(mfr.aliases or []),
        countries=list(mfr.countries or []),
        confidence=mfr.confidence,
        recall_summary=RecallSummary(
            total=len(mfr_docs),
            by_source=by_source,
            by_severity=by_severity,
        ),
        active_risk_signals=signal_summaries,
        product_names=sorted(product_names_set)[:50],
        active_ingredients=sorted(ingredients_set)[:50],
    )


async def county_exposure(
    session: AsyncSession,
    *,
    county: str,
    active_ingredient: str | None = None,
) -> CountyExposure:
    """Return county-level supply-chain exposure data.

    Args:
        session: Database session.
        county: Exact county name (case-insensitive).
        active_ingredient: Limit results to a single normalized ingredient.

    Returns:
        CountyExposure with supply mix, flagged suppliers, and data_provenance.

    Raises:
        ValueError: If county name fails validation or county is not found.
    """
    _validate_name(county, "county")

    result = await session.execute(
        select(County).where(func.lower(County.name) == county.lower())
    )
    county_obj = result.scalar_one_or_none()
    if county_obj is None:
        raise ValueError(f"County not found: {county!r}")

    supply_q = (
        select(CountySupply, Supplier)
        .join(Supplier, CountySupply.supplier_id == Supplier.id)
        .where(CountySupply.county_id == county_obj.id)
        .order_by(CountySupply.share_pct.desc())
    )
    if active_ingredient is not None:
        supply_q = supply_q.where(CountySupply.active_ingredient == active_ingredient)

    supply_rows = list((await session.execute(supply_q)).all())

    # Identify suppliers with active signals; collect signal IDs for agent citation.
    flagged_supplier_names: list[str] = []
    flagged_supplier_ids: set[_uuid_mod.UUID] = set()
    supplier_signal_ids: dict[_uuid_mod.UUID, list[str]] = {}
    flagged_supplier_signal_ids: list[str] = []
    for _cs, supplier in supply_rows:
        if supplier.manufacturer_id is not None:
            sig_result = await session.execute(
                select(RiskSignal).where(
                    RiskSignal.manufacturer_id == supplier.manufacturer_id,
                    RiskSignal.status == "active",
                    RiskSignal.kind.in_(["repeat_violator", "supply_chain_exposure"]),
                )
            )
            active_sigs = list(sig_result.scalars().all())
            if active_sigs:
                flagged_supplier_ids.add(supplier.id)
                if supplier.name not in flagged_supplier_names:
                    flagged_supplier_names.append(supplier.name)
                sig_ids = [str(s.id) for s in active_sigs]
                supplier_signal_ids[supplier.id] = sig_ids
                for sid in sig_ids:
                    if sid not in flagged_supplier_signal_ids:
                        flagged_supplier_signal_ids.append(sid)

    # Alternative supplier counts per ingredient.
    alt_counts: dict[str, int] = {}
    for cs, _supplier in supply_rows:
        ing = cs.active_ingredient
        total_result = await session.execute(
            select(func.count(CountySupply.id)).where(
                CountySupply.county_id == county_obj.id,
                CountySupply.active_ingredient == ing,
            )
        )
        total = (total_result.scalar_one() or 1) - 1  # exclude current
        alt_counts[ing] = max(alt_counts.get(ing, 0), total)

    # Collect unique data sources.
    data_sources = list({cs.data_source for cs, _ in supply_rows})
    data_provenance: dict[str, Any] = {
        "data_sources": data_sources,
        "synthetic": any("synthetic" in ds for ds in data_sources),
        "caveat": (
            "Supply-chain figures derive from synthetic procurement data (v2). "
            "Real KEMSA/county procurement integration is pending."
        )
        if any("synthetic" in ds for ds in data_sources)
        else "",
    }

    mix_rows = [
        SupplyMixRow(
            supplier_name=supplier.name,
            supplier_id=str(supplier.id),
            active_ingredient=cs.active_ingredient,
            share_pct=cs.share_pct,
            lead_time_days=cs.lead_time_days,
            contract_end=cs.contract_end,
            data_source=cs.data_source,
            has_active_signal=supplier.id in flagged_supplier_ids,
            active_signal_ids=supplier_signal_ids.get(supplier.id, []),
        )
        for cs, supplier in supply_rows
    ]

    return CountyExposure(
        county_name=county_obj.name,
        region=county_obj.region,
        population=county_obj.population,
        health_facilities=county_obj.health_facilities,
        supply_mix=mix_rows,
        flagged_supplier_names=flagged_supplier_names,
        flagged_supplier_signal_ids=flagged_supplier_signal_ids,
        alternative_supplier_counts=alt_counts,
        data_provenance=data_provenance,
    )


async def search_documents(
    session: AsyncSession,
    *,
    query: str,
    source_id: Literal["openfda_drug", "ppb_ke_alerts", "sahpra_recalls"] | None = None,
    document_type: str | None = None,
    active_ingredient: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: int = 10,
) -> DocumentSearchResponse:
    """Full-text search over documents.raw_text using Postgres ts_query.

    Args:
        session: Database session.
        query: Free-text search query (max 200 chars).
        source_id: Optional filter by source adapter.
        document_type: Optional document_type filter.
        active_ingredient: Exact normalized ingredient filter.
        since: Filter date_published >= since.
        until: Filter date_published <= until.
        limit: Max results; hard-capped at 25.

    Returns:
        DocumentSearchResponse with results, total_count, and truncated.

    Raises:
        ValueError: If the query fails validation.
    """
    clean_query = _validate_query(query)
    effective_limit = min(max(1, limit), _MAX_DOCS_LIMIT)

    # Build tsquery from websearch_to_tsquery (more forgiving than to_tsquery).
    ts_expr = func.websearch_to_tsquery("english", clean_query)
    ts_rank = func.ts_rank(func.to_tsvector("english", Document.raw_text), ts_expr)

    q = (
        select(Document, ts_rank.label("rank"))
        .where(
            func.to_tsvector("english", Document.raw_text).op("@@")(ts_expr)
        )
        .order_by(ts_rank.desc())
    )
    if source_id is not None:
        q = q.where(Document.source_id == source_id)
    if document_type is not None:
        q = q.where(Document.document_type == document_type)
    if active_ingredient is not None:
        q = q.where(Document.active_ingredients.any(active_ingredient))  # type: ignore[arg-type]
    if since is not None:
        q = q.where(Document.date_published >= since)
    if until is not None:
        q = q.where(Document.date_published <= until)

    count_q = select(func.count()).select_from(q.subquery())
    total_count: int = (await session.execute(count_q)).scalar_one()

    q = q.limit(effective_limit)
    rows = list((await session.execute(q)).all())

    results: list[DocumentSearchResult] = []
    for doc, _rank in rows:
        # Resolve canonical manufacturer names.
        mfr_names: list[str] = []
        if doc.canonical_manufacturer_ids:
            mfr_result = await session.execute(
                select(Manufacturer).where(
                    Manufacturer.id.in_(doc.canonical_manufacturer_ids)
                )
            )
            mfr_names = [m.canonical_name for m in mfr_result.scalars().all()]

        snippet = truncate_snippet(doc.raw_text or "")
        results.append(
            DocumentSearchResult(
                document_id=str(doc.id),
                source_url=doc.source_url,
                date_published=doc.date_published,
                title=doc.title,
                source_id=doc.source_id,
                severity=doc.severity,
                active_ingredients=list(doc.active_ingredients or []),
                manufacturer_canonical_names=mfr_names,
                snippet=snippet,
            )
        )

    return DocumentSearchResponse(
        results=results,
        total_count=total_count,
        truncated=total_count > effective_limit,
    )


async def get_document(
    session: AsyncSession, *, document_id: str, offset: int = 0
) -> DocumentDetail:
    """Return a full document, with raw_text wrapped in injection-resistant delimiters.

    Args:
        session: Database session.
        document_id: UUID string of the document.
        offset: Character offset for raw_text pagination (default 0).

    Returns:
        DocumentDetail with sanitized + wrapped raw_text_wrapped.

    Raises:
        ValueError: If document_id is not valid or the document is not found.
    """
    uid = _validate_uuid(document_id, "document_id")
    doc = await session.get(Document, uid)
    if doc is None:
        raise ValueError(f"Document not found: {document_id!r}")

    raw_text = doc.raw_text or ""
    page_text, truncated = paginate_raw_text(raw_text, offset=offset)
    wrapped = wrap_untrusted(page_text, source=f"document:{document_id}", content_type="raw_text")

    return DocumentDetail(
        document_id=str(doc.id),
        source_id=doc.source_id,
        source_url=doc.source_url,
        jurisdiction=doc.jurisdiction,
        document_type=doc.document_type,
        title=doc.title,
        product_names=list(doc.product_names or []),
        active_ingredients=list(doc.active_ingredients or []),
        manufacturers=list(doc.manufacturers or []),
        severity=doc.severity,
        date_published=doc.date_published,
        date_effective=doc.date_effective,
        regions_affected=list(doc.regions_affected or []),
        language=doc.language,
        raw_text_wrapped=wrapped,
        truncated=truncated,
        offset=offset,
        total_chars=len(raw_text),
        raw_metadata=dict(doc.raw_metadata or {}),
    )


def _is_valid_uuid(value: str) -> bool:
    """Return True if value parses as a valid UUID."""
    try:
        _uuid_mod.UUID(value)
        return True
    except ValueError:
        return False


async def dispatch(
    session: AsyncSession, tool_name: str, tool_input: dict[str, Any]
) -> Any:
    """Route a tool call by name to the correct implementation.

    Args:
        session: Database session passed through to the tool.
        tool_name: Name matching one of the TOOL_DEFINITIONS entries.
        tool_input: Validated input parameters from the Anthropic API.

    Returns:
        Pydantic model returned by the tool function.

    Raises:
        ValueError: If tool_name is unknown.
    """
    handlers = {
        "list_risk_signals": _dispatch_list_risk_signals,
        "get_risk_signal": _dispatch_get_risk_signal,
        "manufacturer_profile": _dispatch_manufacturer_profile,
        "county_exposure": _dispatch_county_exposure,
        "search_documents": _dispatch_search_documents,
        "get_document": _dispatch_get_document,
    }
    handler = handlers.get(tool_name)
    if handler is None:
        raise ValueError(f"Unknown tool: {tool_name!r}")
    return await handler(session, tool_input)


async def _dispatch_list_risk_signals(
    session: AsyncSession, inp: dict[str, Any]
) -> RiskSignalListResponse:
    since_val = date.fromisoformat(inp["since"]) if inp.get("since") else None
    until_val = date.fromisoformat(inp["until"]) if inp.get("until") else None
    return await list_risk_signals(
        session,
        kind=inp.get("kind"),
        severity=inp.get("severity"),
        status=inp.get("status", "active"),
        region=inp.get("region"),
        active_ingredient=inp.get("active_ingredient"),
        since=since_val,
        until=until_val,
        limit=int(inp.get("limit", 20)),
    )


async def _dispatch_get_risk_signal(
    session: AsyncSession, inp: dict[str, Any]
) -> RiskSignalDetail:
    return await get_risk_signal(session, signal_id=inp["signal_id"])


async def _dispatch_manufacturer_profile(
    session: AsyncSession, inp: dict[str, Any]
) -> ManufacturerProfile:
    return await manufacturer_profile(session, name_or_id=inp["name_or_id"])


async def _dispatch_county_exposure(
    session: AsyncSession, inp: dict[str, Any]
) -> CountyExposure:
    return await county_exposure(
        session,
        county=inp["county"],
        active_ingredient=inp.get("active_ingredient"),
    )


async def _dispatch_search_documents(
    session: AsyncSession, inp: dict[str, Any]
) -> DocumentSearchResponse:
    since_val = date.fromisoformat(inp["since"]) if inp.get("since") else None
    until_val = date.fromisoformat(inp["until"]) if inp.get("until") else None
    return await search_documents(
        session,
        query=inp["query"],
        source_id=inp.get("source_id"),
        document_type=inp.get("document_type"),
        active_ingredient=inp.get("active_ingredient"),
        since=since_val,
        until=until_val,
        limit=int(inp.get("limit", 10)),
    )


async def _dispatch_get_document(
    session: AsyncSession, inp: dict[str, Any]
) -> DocumentDetail:
    return await get_document(
        session,
        document_id=inp["document_id"],
        offset=int(inp.get("offset", 0)),
    )


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_risk_signals",
        "description": (
            "List risk signals with optional filters. Returns summaries only — "
            "call get_risk_signal for full evidence. Hard cap: 50 results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "repeat_violator",
                        "supply_chain_exposure",
                        "cross_source_corroboration",
                    ],
                    "description": "Filter by signal kind.",
                },
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Filter by severity.",
                },
                "status": {
                    "type": "string",
                    "enum": ["active", "resolved", "suppressed"],
                    "description": "Filter by status (default: active).",
                },
                "region": {
                    "type": "string",
                    "description": "County name substring match against regions_affected.",
                },
                "active_ingredient": {
                    "type": "string",
                    "description": "Exact normalized INN filter.",
                },
                "since": {
                    "type": "string",
                    "format": "date",
                    "description": "ISO date: only signals with first_seen >= since.",
                },
                "until": {
                    "type": "string",
                    "format": "date",
                    "description": "ISO date: only signals with first_seen <= until.",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum results (hard cap 50).",
                },
            },
        },
    },
    {
        "name": "get_risk_signal",
        "description": (
            "Get full details for a risk signal, including evidence, explain trace, "
            "and the count_inflation_likely flag (see recall_event_clustering follow-up)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "signal_id": {
                    "type": "string",
                    "description": "UUID of the risk signal.",
                }
            },
            "required": ["signal_id"],
        },
    },
    {
        "name": "manufacturer_profile",
        "description": (
            "Get a manufacturer's profile: aliases, countries, recall summary by source and "
            "severity, active risk signals, and product/ingredient lists. Returns "
            "disambiguation_needed=true if the name matches multiple canonical entries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name_or_id": {
                    "type": "string",
                    "description": "Canonical name (exact or fuzzy) or UUID.",
                }
            },
            "required": ["name_or_id"],
        },
    },
    {
        "name": "county_exposure",
        "description": (
            "Get county-level supply-chain exposure: supply mix with share percentages, "
            "flagged suppliers, alternative supplier counts. Always includes data_provenance "
            "noting whether figures are synthetic."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "county": {
                    "type": "string",
                    "description": "Exact county name (case-insensitive).",
                },
                "active_ingredient": {
                    "type": "string",
                    "description": "Optional: limit to a single normalized INN.",
                },
            },
            "required": ["county"],
        },
    },
    {
        "name": "search_documents",
        "description": (
            "Full-text search over recall/alert documents. Returns titles, snippets, and "
            "metadata — not full text. Call get_document for raw text. Hard cap: 25 results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text search query (max 200 chars).",
                },
                "source_id": {
                    "type": "string",
                    "enum": ["openfda_drug", "ppb_ke_alerts", "sahpra_recalls"],
                    "description": "Limit to a specific source.",
                },
                "document_type": {
                    "type": "string",
                    "description": "Filter by document_type (e.g. 'recall').",
                },
                "active_ingredient": {
                    "type": "string",
                    "description": "Exact normalized INN filter.",
                },
                "since": {
                    "type": "string",
                    "format": "date",
                    "description": "ISO date lower bound on date_published.",
                },
                "until": {
                    "type": "string",
                    "format": "date",
                    "description": "ISO date upper bound on date_published.",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max results (hard cap 25).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_document",
        "description": (
            "Get the full document including raw text, wrapped in "
            "<untrusted_content> delimiters. Text is paginated at 8000 chars; "
            "use offset to read subsequent pages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "UUID of the document.",
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "description": "Character offset for pagination.",
                },
            },
            "required": ["document_id"],
        },
    },
]
