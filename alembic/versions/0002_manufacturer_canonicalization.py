"""Add manufacturer canonicalization columns and document.canonical_manufacturer_ids.

Adds:
- manufacturers.confidence (float, default 1.0)
- manufacturers.updated_at (timestamptz)
- documents.canonical_manufacturer_ids (uuid[])
- GIN index on manufacturers.aliases for efficient alias lookups

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-18 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add canonicalization columns."""
    op.add_column(
        "manufacturers",
        sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
    )
    op.add_column(
        "manufacturers",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_manufacturers_aliases_gin",
        "manufacturers",
        ["aliases"],
        postgresql_using="gin",
    )

    op.add_column(
        "documents",
        sa.Column(
            "canonical_manufacturer_ids",
            ARRAY(UUID(as_uuid=True)),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    """Remove canonicalization columns."""
    op.drop_column("documents", "canonical_manufacturer_ids")
    op.drop_index("ix_manufacturers_aliases_gin", table_name="manufacturers")
    op.drop_column("manufacturers", "updated_at")
    op.drop_column("manufacturers", "confidence")
