"""Add risk_signals and risk_signal_events tables with conditional GRANTs.

The GRANT block is guarded by an IF EXISTS check so this migration is a no-op
in CI environments where the regulatory_readonly role has not been created via
scripts/ops/create_readonly_role.sql.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-18 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create risk signal tables and apply conditional read-only grants."""
    op.create_table(
        "risk_signals",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "manufacturer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("manufacturers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("active_ingredient", sa.Text, nullable=True),
        sa.Column("regions_affected", ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("exposure_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("alternative_supplier_count", sa.Integer, nullable=True),
        sa.Column("recommended_action", sa.Text, nullable=False, server_default=""),
        sa.Column("time_to_expiry_days", sa.Integer, nullable=True),
        sa.Column("evidence", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "first_seen",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_updated",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_risk_signals_kind", "risk_signals", ["kind"])
    op.create_index("ix_risk_signals_status", "risk_signals", ["status"])
    op.create_index("ix_risk_signals_manufacturer_id", "risk_signals", ["manufacturer_id"])
    # Partial unique index: only one active signal per (kind, manufacturer_id, active_ingredient).
    op.execute(
        """
        CREATE UNIQUE INDEX uq_risk_signals_active
        ON risk_signals (kind, manufacturer_id, active_ingredient)
        WHERE status = 'active'
        """
    )

    op.create_table(
        "risk_signal_events",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "signal_id",
            UUID(as_uuid=True),
            sa.ForeignKey("risk_signals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("old_state", JSONB, nullable=True),
        sa.Column("new_state", JSONB, nullable=False),
        sa.Column("actor", sa.Text, nullable=False, server_default="system"),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_risk_signal_events_signal_id", "risk_signal_events", ["signal_id"])

    # Conditional GRANTs — no-op if the role doesn't exist (CI environments).
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regulatory_readonly') THEN
            GRANT SELECT ON manufacturers, counties, suppliers,
                            county_supply, risk_signals, risk_signal_events
              TO regulatory_readonly;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """Drop risk signal tables."""
    op.drop_index("uq_risk_signals_active", table_name="risk_signals")
    op.drop_table("risk_signal_events")
    op.drop_table("risk_signals")
