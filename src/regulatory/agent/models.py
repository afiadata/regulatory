"""Pydantic response types for the agent tool surface.

All tool functions return one of these types. The runner serialises them into
the Anthropic tool-result payloads; the models are also used to validate that
tool implementations satisfy the documented contracts.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class RiskSignalSummary(BaseModel):
    """Compact summary returned inside list responses."""

    signal_id: str
    kind: Literal["repeat_violator", "supply_chain_exposure", "cross_source_corroboration"]
    severity: Literal["low", "medium", "high", "critical"]
    manufacturer_canonical_name: str | None = None
    active_ingredient: str | None = None
    regions: list[str] = Field(default_factory=list)
    exposure_pct: Decimal | None = None
    first_seen: datetime
    brief_description: str = ""


class RiskSignalListResponse(BaseModel):
    """Response from list_risk_signals."""

    signals: list[RiskSignalSummary]
    total_count: int
    truncated: bool


class RiskSignalDetail(BaseModel):
    """Full signal returned by get_risk_signal, including explain text."""

    signal_id: str
    kind: str
    severity: str
    status: str
    manufacturer_canonical_name: str | None = None
    manufacturer_id: str | None = None
    active_ingredient: str | None = None
    regions_affected: list[str] = Field(default_factory=list)
    exposure_pct: Decimal | None = None
    alternative_supplier_count: int | None = None
    recommended_action: str = ""
    first_seen: datetime
    last_updated: datetime
    evidence: dict[str, Any] = Field(default_factory=dict)
    explain_text: str = ""
    count_inflation_likely: bool = False
    data_provenance: dict[str, Any] | None = None


class RecallSummary(BaseModel):
    """Recall statistics for a manufacturer."""

    total: int
    by_source: dict[str, int] = Field(default_factory=dict)
    by_severity: dict[str, int] = Field(default_factory=dict)


class ManufacturerProfile(BaseModel):
    """Full profile returned by manufacturer_profile."""

    manufacturer_id: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    confidence: float
    recall_summary: RecallSummary
    active_risk_signals: list[RiskSignalSummary] = Field(default_factory=list)
    product_names: list[str] = Field(default_factory=list)
    active_ingredients: list[str] = Field(default_factory=list)
    disambiguation_needed: bool = False
    candidates: list[dict[str, Any]] = Field(default_factory=list)


class SupplyMixRow(BaseModel):
    """One row in the county supply mix table."""

    supplier_name: str
    supplier_id: str
    active_ingredient: str
    share_pct: Decimal
    lead_time_days: int
    contract_end: date | None = None
    data_source: str
    has_active_signal: bool = False


class CountyExposure(BaseModel):
    """County-level exposure returned by county_exposure."""

    county_name: str
    region: str
    population: int | None = None
    health_facilities: int | None = None
    supply_mix: list[SupplyMixRow] = Field(default_factory=list)
    flagged_supplier_names: list[str] = Field(default_factory=list)
    alternative_supplier_counts: dict[str, int] = Field(default_factory=dict)
    data_provenance: dict[str, Any] = Field(default_factory=dict)


class DocumentSearchResult(BaseModel):
    """One hit from search_documents."""

    document_id: str
    source_url: str
    date_published: date
    title: str
    source_id: str
    severity: str | None = None
    active_ingredients: list[str] = Field(default_factory=list)
    manufacturer_canonical_names: list[str] = Field(default_factory=list)
    snippet: str = ""


class DocumentSearchResponse(BaseModel):
    """Response from search_documents."""

    results: list[DocumentSearchResult]
    total_count: int
    truncated: bool


class DocumentDetail(BaseModel):
    """Full document returned by get_document, with sanitized + wrapped raw text."""

    document_id: str
    source_id: str
    source_url: str
    jurisdiction: str
    document_type: str
    title: str
    product_names: list[str] = Field(default_factory=list)
    active_ingredients: list[str] = Field(default_factory=list)
    manufacturers: list[str] = Field(default_factory=list)
    severity: str | None = None
    date_published: date
    date_effective: date | None = None
    regions_affected: list[str] = Field(default_factory=list)
    language: str = "en"
    raw_text_wrapped: str = ""
    truncated: bool = False
    offset: int = 0
    total_chars: int = 0
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
