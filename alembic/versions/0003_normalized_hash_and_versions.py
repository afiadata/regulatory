"""Add normalized_hash, (source_id, source_url) unique constraint, extend document_versions.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-17 00:00:00.000000

Downgrade note: the pre-upgrade DELETE that removes duplicate (source_id, source_url)
rows is not reversible. ``downgrade()`` removes the schema additions but cannot
restore the deleted rows.

Backfill note: ``normalized_hash`` is backfilled for all existing rows during
``upgrade()`` before the NOT NULL constraint is applied. Skipping this backfill
would cause the next ingest run to treat every existing row as changed (NULL !=
new_hash) and inflate ``docs_updated`` with false-positive update events.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply normalized_hash, dedup constraint, and document_versions extensions."""
    conn = op.get_bind()

    # ── 1. Purge duplicate (source_id, source_url) rows before adding the
    #        unique constraint. Keep the row with the latest created_at.
    #        Rows deleted here are the bug-induced SAHPRA duplicates; the
    #        safer general-purpose tool is `regulatory db dedup`.
    result = conn.execute(
        sa.text("""
            WITH dupes AS (
                SELECT id
                FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY source_id, source_url
                               ORDER BY created_at DESC
                           ) AS rn
                    FROM documents
                ) ranked
                WHERE rn > 1
            )
            DELETE FROM documents WHERE id IN (SELECT id FROM dupes)
        """)
    )
    deleted = result.rowcount
    if deleted:
        print(f"  [0003] pre-dedup: deleted {deleted} duplicate document row(s)")

    # ── 2. Add normalized_hash column (nullable; backfilled in step 2b before
    #        being made NOT NULL)
    op.add_column("documents", sa.Column("normalized_hash", sa.Text, nullable=True))

    # ── 2b. Backfill normalized_hash for all existing rows before applying the
    #        NOT NULL constraint.  Reconstructs NormalizedDocument from stored
    #        columns and calls the same hash method the scheduler uses, so the
    #        next ingest run sees matching hashes and skips rather than treating
    #        every existing row as changed.
    from datetime import datetime, timezone  # noqa: PLC0415

    from regulatory.models import DocumentType, NormalizedDocument, Severity  # noqa: PLC0415

    backfill_rows = conn.execute(
        sa.text("""
            SELECT id, source_id, source_url, document_id, title,
                   product_names, active_ingredients, manufacturers,
                   severity, date_published, date_effective,
                   raw_metadata, jurisdiction, document_type, language
            FROM documents
            WHERE normalized_hash IS NULL
        """)
    ).fetchall()

    backfilled = 0
    for row in backfill_rows:
        nd = NormalizedDocument(
            source_id=row.source_id,
            source_url=row.source_url,  # type: ignore[arg-type]
            source_hash="",  # excluded from hash; any fixed value is fine
            jurisdiction=row.jurisdiction,
            document_type=DocumentType(row.document_type),
            document_id=row.document_id,
            title=row.title,
            product_names=list(row.product_names or []),
            active_ingredients=list(row.active_ingredients or []),
            active_ingredients_raw=[],  # excluded from hash
            manufacturers=list(row.manufacturers or []),
            marketing_authorization_holders=[],  # excluded from hash
            severity=Severity(row.severity) if row.severity else None,
            date_published=row.date_published,
            date_effective=row.date_effective,
            regions_affected=[],  # excluded from hash
            language=row.language or "en",
            raw_text="",  # excluded from hash
            raw_metadata=dict(row.raw_metadata or {}),
            extracted_at=datetime.now(tz=timezone.utc),
        )
        conn.execute(
            sa.text("UPDATE documents SET normalized_hash = :h WHERE id = :id"),
            {"h": nd.normalized_content_hash(), "id": row.id},
        )
        backfilled += 1

    if backfilled:
        print(f"  [0003] backfilled normalized_hash for {backfilled} existing row(s)")

    op.alter_column("documents", "normalized_hash", existing_type=sa.Text(), nullable=False)

    # ── 3. Add unique constraint on (source_id, source_url)
    op.create_unique_constraint(
        "uq_documents_source_id_url", "documents", ["source_id", "source_url"]
    )

    # ── 4. Extend document_versions: rename legacy columns, add superseded_at
    #        and created_at, drop old simple index, add compound index.
    op.alter_column("document_versions", "source_hash", new_column_name="normalized_hash")
    op.alter_column("document_versions", "captured_at", new_column_name="superseded_at")

    op.add_column(
        "document_versions",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.drop_index("ix_document_versions_document_id", table_name="document_versions")
    op.create_index(
        "ix_document_versions_document_id_superseded_at",
        "document_versions",
        ["document_id", "superseded_at"],
    )


def downgrade() -> None:
    """Remove 0003 additions. Deleted duplicate rows are NOT restored."""
    op.drop_index("ix_document_versions_document_id_superseded_at", table_name="document_versions")
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])

    op.drop_column("document_versions", "created_at")
    op.alter_column("document_versions", "superseded_at", new_column_name="captured_at")
    op.alter_column("document_versions", "normalized_hash", new_column_name="source_hash")

    op.drop_constraint("uq_documents_source_id_url", "documents", type_="unique")
    op.drop_column("documents", "normalized_hash")
