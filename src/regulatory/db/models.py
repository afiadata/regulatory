"""SQLAlchemy ORM models for the regulatory document store.

Tables:
- ``documents`` — one row per unique content hash (canonical record).
- ``document_versions`` — history when a URL's content changes.
- ``fetch_log`` — per-attempt audit trail.
- ``manufacturers`` — deduplicated manufacturer directory.
- ``counties`` — Kenyan county reference data.
- ``suppliers`` — supplier/distributor directory linked to manufacturers.
- ``county_supply`` — county-level active-ingredient supply shares.
- ``risk_signals`` — active risk signals emitted by the risk engine.
- ``risk_signal_events`` — append-only audit log of signal state transitions.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from regulatory.models import NormalizedDocument


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class Document(Base):
    """One row per unique source_hash (content-addressed document store).

    When the content at a URL changes, the old row is archived in
    ``document_versions`` and a new ``Document`` row is inserted.
    """

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_source_date", "source_id", "date_published"),
        Index(
            "ix_documents_jurisdiction_type_date",
            "jurisdiction",
            "document_type",
            "date_published",
        ),
        Index("ix_documents_active_ingredients_gin", "active_ingredients", postgresql_using="gin"),
        Index("ix_documents_manufacturers_gin", "manufacturers", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    jurisdiction: Mapped[str] = mapped_column(String(8), nullable=False)
    document_type: Mapped[str] = mapped_column(String(32), nullable=False)
    document_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    product_names: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    active_ingredients: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    active_ingredients_normalized: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    active_ingredients_raw: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    manufacturers: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    marketing_authorization_holders: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    date_published: Mapped[date] = mapped_column(Date, nullable=False)
    date_effective: Mapped[date | None] = mapped_column(Date, nullable=True)
    regions_affected: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(tz=timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
        onupdate=lambda: datetime.now(tz=timezone.utc),
    )
    # Resolved canonical manufacturer IDs (populated by reconciliation job).
    canonical_manufacturer_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list
    )

    versions: Mapped[list[DocumentVersion]] = relationship(
        "DocumentVersion", back_populates="document", cascade="all, delete-orphan"
    )

    @classmethod
    def from_normalized(cls, doc: NormalizedDocument) -> Document:
        """Construct a ``Document`` ORM instance from a ``NormalizedDocument``.

        Args:
            doc: The Pydantic normalized document.

        Returns:
            A new (unsaved) ``Document`` instance.
        """
        from regulatory.risk.ingredient_normalize import normalize_ingredient

        return cls(
            source_id=doc.source_id,
            source_url=str(doc.source_url),
            source_hash=doc.source_hash,
            jurisdiction=doc.jurisdiction,
            document_type=doc.document_type.value,
            document_id=doc.document_id,
            title=doc.title,
            product_names=doc.product_names,
            active_ingredients=doc.active_ingredients,
            active_ingredients_normalized=[
                normalize_ingredient(ing) for ing in doc.active_ingredients
            ],
            active_ingredients_raw=doc.active_ingredients_raw,
            manufacturers=doc.manufacturers,
            marketing_authorization_holders=doc.marketing_authorization_holders,
            severity=doc.severity.value if doc.severity else None,
            date_published=doc.date_published,
            date_effective=doc.date_effective,
            regions_affected=doc.regions_affected,
            language=doc.language,
            raw_text=doc.raw_text,
            raw_metadata=doc.raw_metadata,
            extracted_at=doc.extracted_at,
        )


class DocumentVersion(Base):
    """Historical snapshot when a document's content at a URL changes."""

    __tablename__ = "document_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
    )

    document: Mapped[Document] = relationship("Document", back_populates="versions")


class FetchLog(Base):
    """Per-attempt audit log for discover / fetch operations."""

    __tablename__ = "fetch_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # "success" | "error"
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bytes_fetched: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Manufacturer(Base):
    """Deduplicated manufacturer directory (populated by nightly reconciliation job)."""

    __tablename__ = "manufacturers"
    __table_args__ = (UniqueConstraint("canonical_name", name="uq_manufacturers_canonical"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    countries: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(tz=timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
        onupdate=lambda: datetime.now(tz=timezone.utc),
    )

    suppliers: Mapped[list[Supplier]] = relationship("Supplier", back_populates="manufacturer")
    risk_signals: Mapped[list[RiskSignal]] = relationship(
        "RiskSignal", back_populates="manufacturer"
    )


class County(Base):
    """Kenyan county reference data (47 counties, 2019 census)."""

    __tablename__ = "counties"
    __table_args__ = (UniqueConstraint("name", name="uq_counties_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str] = mapped_column(Text, nullable=False)
    population: Mapped[int | None] = mapped_column(Integer, nullable=True)
    health_facilities: Mapped[int | None] = mapped_column(Integer, nullable=True)

    supply_rows: Mapped[list[CountySupply]] = relationship(
        "CountySupply", back_populates="county"
    )


class Supplier(Base):
    """Supplier/distributor directory, optionally linked to a canonical manufacturer."""

    __tablename__ = "suppliers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    manufacturer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("manufacturers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # manufacturer/distributor/agent
    countries_served: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)

    manufacturer: Mapped[Manufacturer | None] = relationship(
        "Manufacturer", back_populates="suppliers"
    )
    supply_rows: Mapped[list[CountySupply]] = relationship(
        "CountySupply", back_populates="supplier"
    )


class CountySupply(Base):
    """County-level active-ingredient supply share per supplier."""

    __tablename__ = "county_supply"
    __table_args__ = (
        CheckConstraint("share_pct >= 0 AND share_pct <= 100", name="ck_county_supply_share_pct"),
        Index("ix_county_supply_county_ingredient", "county_id", "active_ingredient"),
        Index("ix_county_supply_supplier_ingredient", "supplier_id", "active_ingredient"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    county_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("counties.id", ondelete="CASCADE"), nullable=False
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False
    )
    active_ingredient: Mapped[str] = mapped_column(Text, nullable=False)
    share_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    contract_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    data_source: Mapped[str] = mapped_column(Text, nullable=False, default="synthetic_v1")

    county: Mapped[County] = relationship("County", back_populates="supply_rows")
    supplier: Mapped[Supplier] = relationship("Supplier", back_populates="supply_rows")


class RiskSignal(Base):
    """A risk signal emitted by the risk engine for a manufacturer/ingredient pair.

    The partial unique constraint (kind, manufacturer_id, active_ingredient) WHERE
    status = 'active' prevents duplicate active signals for the same situation.
    Re-running the engine updates last_updated + evidence rather than inserting a
    duplicate row.
    """

    __tablename__ = "risk_signals"
    __table_args__ = (
        Index(
            "uq_risk_signals_active",
            "kind",
            "manufacturer_id",
            "active_ingredient",
            unique=True,
            postgresql_where="status = 'active'",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    manufacturer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("manufacturers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    active_ingredient: Mapped[str | None] = mapped_column(Text, nullable=True)
    regions_affected: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    exposure_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    alternative_supplier_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False, default="")
    time_to_expiry_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
        onupdate=lambda: datetime.now(tz=timezone.utc),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    manufacturer: Mapped[Manufacturer | None] = relationship(
        "Manufacturer", back_populates="risk_signals"
    )
    events: Mapped[list[RiskSignalEvent]] = relationship(
        "RiskSignalEvent", back_populates="signal", cascade="all, delete-orphan"
    )


class RiskSignalEvent(Base):
    """Append-only audit log of every risk signal state transition.

    Rows are never updated or deleted (no UPDATE/DELETE grants for any role).
    """

    __tablename__ = "risk_signal_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("risk_signals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # created/updated/resolved/suppressed
    old_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    new_state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
    )

    signal: Mapped[RiskSignal] = relationship("RiskSignal", back_populates="events")
