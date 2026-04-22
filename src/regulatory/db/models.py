"""SQLAlchemy ORM models for the regulatory document store.

Tables:
- ``documents`` — one row per unique content hash (canonical record).
- ``document_versions`` — history when a URL's content changes.
- ``fetch_log`` — per-attempt audit trail.
- ``manufacturers`` — deduplicated manufacturer directory.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
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
            "jurisdiction", "document_type", "date_published",
        ),
        Index("ix_documents_active_ingredients_gin", "active_ingredients", postgresql_using="gin"),
        Index("ix_documents_manufacturers_gin", "manufacturers", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    jurisdiction: Mapped[str] = mapped_column(String(8), nullable=False)
    document_type: Mapped[str] = mapped_column(String(32), nullable=False)
    document_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    product_names: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    active_ingredients: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
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

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
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

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    countries: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(tz=timezone.utc)
    )
