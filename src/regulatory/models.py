"""Pydantic schemas for normalized regulatory documents.

Every adapter must produce a ``NormalizedDocument`` instance. The schema is the
canonical data contract between ingestion and storage/risk-engine layers.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, HttpUrl, field_validator, model_validator


class DocumentType(str, Enum):
    """Categories of regulatory document handled by the platform."""

    recall = "recall"
    alert = "alert"
    enforcement = "enforcement"
    trial = "trial"
    guideline = "guideline"
    shortage = "shortage"
    referral = "referral"


class Severity(str, Enum):
    """Risk classification mirroring FDA Class I/II/III conventions."""

    class_1 = "class_1"
    class_2 = "class_2"
    class_3 = "class_3"
    unclassified = "unclassified"


class DocumentRef(BaseModel):
    """Lightweight reference yielded by ``RegulatorySource.discover``.

    Used to decide whether a full fetch is needed before committing to
    downloading large PDF payloads.
    """

    source_id: str
    url: HttpUrl
    document_id: str | None = None
    title: str | None = None
    date_published: date | None = None
    etag: str | None = None
    last_modified: str | None = None


class RawDocument(BaseModel):
    """Raw payload returned by ``RegulatorySource.fetch``."""

    ref: DocumentRef
    content: bytes
    content_type: str  # e.g. "application/pdf", "text/html", "application/json"
    source_hash: str  # sha256 hex of ``content``
    fetched_at: datetime

    @model_validator(mode="before")
    @classmethod
    def compute_hash(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Auto-compute sha256 when not explicitly provided."""
        if "source_hash" not in values or not values["source_hash"]:
            content = values.get("content", b"")
            values["source_hash"] = hashlib.sha256(content).hexdigest()
        return values


class NormalizedDocument(BaseModel):
    """Platform-wide normalized schema for every regulatory document.

    All adapters must produce instances of this model. Fields map directly
    to the ``documents`` Postgres table.
    """

    source_id: str
    source_url: HttpUrl
    source_hash: str  # sha256 of raw content, used for change detection
    jurisdiction: str  # ISO-3166 alpha-2, or "EU" / "GLOBAL"
    document_type: DocumentType
    document_id: str | None = None  # native ID from the source
    title: str
    product_names: list[str] = []
    active_ingredients: list[str] = []  # normalized to INN
    active_ingredients_raw: list[str] = []  # as found in source
    manufacturers: list[str] = []
    marketing_authorization_holders: list[str] = []
    severity: Severity | None = None
    date_published: date
    date_effective: date | None = None
    regions_affected: list[str] = []
    language: str = "en"  # ISO 639-1
    raw_text: str = ""
    raw_metadata: dict[str, Any] = {}
    extracted_at: datetime

    @field_validator("jurisdiction")
    @classmethod
    def validate_jurisdiction(cls, v: str) -> str:
        """Accept ISO-3166 alpha-2 codes and known multi-country codes."""
        allowed_special = {"EU", "GLOBAL", "EAC", "ECOWAS", "AU"}
        if v.upper() in allowed_special or (len(v) == 2 and v.isalpha()):
            return v.upper()
        raise ValueError(
            f"jurisdiction must be ISO-3166 alpha-2 or one of {allowed_special}, got {v!r}"
        )

    @field_validator("language")
    @classmethod
    def validate_language(cls, v: str) -> str:
        """Normalise to lowercase ISO 639-1."""
        return v.lower()
