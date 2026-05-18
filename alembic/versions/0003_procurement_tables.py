"""Add procurement tables: counties, suppliers, county_supply.

These tables hold synthetic (and later real) supply-chain data linking
manufacturers to Kenyan county-level active-ingredient coverage.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-18 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create procurement tables."""
    op.create_table(
        "counties",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("region", sa.Text, nullable=False),
        sa.Column("population", sa.Integer, nullable=True),
        sa.Column("health_facilities", sa.Integer, nullable=True),
    )
    op.create_index("uq_counties_name", "counties", ["name"], unique=True)

    op.create_table(
        "suppliers",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "manufacturer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("manufacturers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("countries_served", ARRAY(sa.Text), nullable=False, server_default="{}"),
    )
    op.create_index("ix_suppliers_manufacturer_id", "suppliers", ["manufacturer_id"])

    op.create_table(
        "county_supply",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "county_id",
            UUID(as_uuid=True),
            sa.ForeignKey("counties.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "supplier_id",
            UUID(as_uuid=True),
            sa.ForeignKey("suppliers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("active_ingredient", sa.Text, nullable=False),
        sa.Column("share_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("lead_time_days", sa.Integer, nullable=False),
        sa.Column("contract_start", sa.Date, nullable=True),
        sa.Column("contract_end", sa.Date, nullable=True),
        sa.Column("data_source", sa.Text, nullable=False, server_default="synthetic_v1"),
        sa.CheckConstraint("share_pct >= 0 AND share_pct <= 100", name="ck_county_supply_share_pct"),
    )
    op.create_index(
        "ix_county_supply_county_ingredient",
        "county_supply",
        ["county_id", "active_ingredient"],
    )
    op.create_index(
        "ix_county_supply_supplier_ingredient",
        "county_supply",
        ["supplier_id", "active_ingredient"],
    )


def downgrade() -> None:
    """Drop procurement tables in reverse dependency order."""
    op.drop_table("county_supply")
    op.drop_table("suppliers")
    op.drop_table("counties")
