"""Add active_ingredients_normalized column to documents.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-18

Adds a pre-normalized (lowercase, salt-suffix-stripped) copy of the
active_ingredients array for cross-source equi-joins in the supply-chain
rule. The raw array is unchanged.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "active_ingredients_normalized",
            postgresql.ARRAY(sa.Text()),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_documents_active_ingredients_normalized_gin",
        "documents",
        ["active_ingredients_normalized"],
        postgresql_using="gin",
    )
    # Backfill: lowercase + strip single trailing salt/ester suffix.
    # Uses a SQL regexp that mirrors normalize_ingredient()'s suffix list.
    # The Python function is authoritative for new writes; this expression
    # handles existing rows at migration time.
    op.execute(
        r"""
        UPDATE documents
        SET active_ingredients_normalized = COALESCE(
            (
                SELECT array_agg(
                    trim(
                        regexp_replace(
                            regexp_replace(
                                lower(trim(ing)),
                                '\s+(hydrochloride|dihydrochloride|monohydrochloride'
                                '|sesquihydrate|monohydrate|dihydrate|anhydrous|hcl'
                                '|sodium|disodium|trisodium|potassium|calcium|magnesium'
                                '|zinc|aluminum|aluminium|ammonium'
                                '|sulfate|sulphate|bisulfate|disulfate'
                                '|phosphate|diphosphate|citrate'
                                '|acetate|diacetate|succinate|hemisuccinate'
                                '|tartrate|bitartrate|hemitartrate'
                                '|maleate|fumarate|hemifumarate'
                                '|mesylate|mesilate|tosylate'
                                '|benzoate|valerate|stearate|palmitate'
                                '|propionate|gluconate|lactate|oxalate'
                                '|bromide|iodide|fluoride|nitrate|nitrite'
                                '|carbonate|bicarbonate)$',
                                '',
                                'i'
                            ),
                            '\s+', ' ', 'g'
                        )
                    )
                )
                FROM unnest(active_ingredients) AS ing
                WHERE ing IS NOT NULL AND trim(ing) != ''
            ),
            ARRAY[]::text[]
        )
        """
    )
    # Ensure column is not-null now that backfill is done.
    op.alter_column("documents", "active_ingredients_normalized", nullable=False)


def downgrade() -> None:
    op.drop_index(
        "ix_documents_active_ingredients_normalized_gin",
        table_name="documents",
    )
    op.drop_column("documents", "active_ingredients_normalized")
