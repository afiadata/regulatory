"""Add source_url index to documents for URL-keyed dedup.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-15 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add non-unique index on documents.source_url."""
    op.create_index("ix_documents_source_url", "documents", ["source_url"])


def downgrade() -> None:
    """Drop source_url index."""
    op.drop_index("ix_documents_source_url", table_name="documents")
