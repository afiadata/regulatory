"""Add agent_audit_log table with INSERT-only grants for agent roles.

The table is append-only by design: no UPDATE or DELETE grants are issued
to any role. SELECT is granted to regulatory_ops for forensic review.

Grants are guarded by IF EXISTS checks so this migration is a no-op in CI
environments where the agent roles have not been created via
scripts/ops/create_agent_roles.sql.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-07
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create agent_audit_log and apply conditional role grants."""
    op.create_table(
        "agent_audit_log",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("turn_index", sa.Integer, nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default="{}"),
        sa.Column("model_id", sa.Text, nullable=True),
        sa.Column("tokens_input", sa.Integer, nullable=True),
        sa.Column("tokens_output", sa.Integer, nullable=True),
        sa.Column("cost_usd_estimate", sa.Numeric(10, 6), nullable=True),
        sa.Column("config_version", sa.String(32), nullable=True),
    )
    op.create_index("ix_agent_audit_log_conversation_id", "agent_audit_log", ["conversation_id"])
    op.create_index("ix_agent_audit_log_ts", "agent_audit_log", ["ts"])

    # Strip all default privileges, then grant only what each role needs.
    op.execute("REVOKE ALL ON agent_audit_log FROM PUBLIC")

    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regulatory_agent_writer') THEN
            GRANT INSERT ON agent_audit_log TO regulatory_agent_writer;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regulatory_ops') THEN
            GRANT SELECT ON agent_audit_log TO regulatory_ops;
          END IF;
        END $$;
        """
    )

    # Allow readonly role to read documents and manufacturers (needed by agent tools).
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regulatory_readonly') THEN
            GRANT SELECT ON documents, document_versions TO regulatory_readonly;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """Drop agent_audit_log."""
    op.drop_index("ix_agent_audit_log_ts", table_name="agent_audit_log")
    op.drop_index("ix_agent_audit_log_conversation_id", table_name="agent_audit_log")
    op.drop_table("agent_audit_log")
