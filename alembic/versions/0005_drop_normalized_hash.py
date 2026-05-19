"""Drop documents.normalized_hash — ghost column from early model version.

The column was present in the initial DB schema (created via SQLAlchemy create_all
before migrations were in use) but was never added to any migration and does not
appear in the current SQLAlchemy model.  Its NOT NULL constraint blocks all adapter
INSERTs that follow the current model layout.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-18 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the ghost normalized_hash column."""
    op.drop_column("documents", "normalized_hash")


def downgrade() -> None:
    """Re-add normalized_hash as nullable (historical NOT NULL cannot be restored safely)."""
    op.add_column(
        "documents",
        sa.Column("normalized_hash", sa.Text, nullable=True),
    )
