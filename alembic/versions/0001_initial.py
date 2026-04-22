"""Initial schema: documents, document_versions, fetch_log, manufacturers.

Revision ID: 0001
Revises:
Create Date: 2025-01-01 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create all tables and indexes."""
    # ── documents ──────────────────────────────────────────────────────────
    op.create_table(
        "documents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("jurisdiction", sa.String(8), nullable=False),
        sa.Column("document_type", sa.String(32), nullable=False),
        sa.Column("document_id", sa.String(256), nullable=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("product_names", ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("active_ingredients", ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("active_ingredients_raw", ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("manufacturers", ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("marketing_authorization_holders", ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("severity", sa.String(16), nullable=True),
        sa.Column("date_published", sa.Date, nullable=False),
        sa.Column("date_effective", sa.Date, nullable=True),
        sa.Column("regions_affected", ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("language", sa.String(8), nullable=False, server_default="en"),
        sa.Column("raw_text", sa.Text, nullable=False, server_default=""),
        sa.Column("raw_metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_documents_source_id", "documents", ["source_id"])
    op.create_index("ix_documents_source_hash", "documents", ["source_hash"], unique=True)
    op.create_index(
        "ix_documents_source_date", "documents", ["source_id", "date_published"]
    )
    op.create_index(
        "ix_documents_jurisdiction_type_date",
        "documents",
        ["jurisdiction", "document_type", "date_published"],
    )
    # GIN indexes for array columns
    op.create_index(
        "ix_documents_active_ingredients_gin",
        "documents",
        ["active_ingredients"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_documents_manufacturers_gin",
        "documents",
        ["manufacturers"],
        postgresql_using="gin",
    )

    # ── document_versions ──────────────────────────────────────────────────
    op.create_table(
        "document_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("raw_text", sa.Text, nullable=False, server_default=""),
        sa.Column("raw_metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])

    # ── fetch_log ──────────────────────────────────────────────────────────
    op.create_table(
        "fetch_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bytes_fetched", sa.Integer, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
    )
    op.create_index("ix_fetch_log_source_id", "fetch_log", ["source_id"])

    # ── manufacturers ──────────────────────────────────────────────────────
    op.create_table(
        "manufacturers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("canonical_name", sa.Text, nullable=False, unique=True),
        sa.Column("aliases", JSONB, nullable=False, server_default="[]"),
        sa.Column("countries", ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "uq_manufacturers_canonical", "manufacturers", ["canonical_name"], unique=True
    )


def downgrade() -> None:
    """Drop all tables in reverse dependency order."""
    op.drop_table("manufacturers")
    op.drop_table("fetch_log")
    op.drop_table("document_versions")
    op.drop_table("documents")
