"""Scheduler that loops over registered sources and runs ingestion.

Synchronous entry point for now; will be converted to a proper async task
queue (Celery / Modal) in a future PR.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import structlog

from regulatory.db.session import get_session
from regulatory.ingestion.registry import all_sources

log = structlog.get_logger(__name__)


def _parse_since(since: str | datetime | None) -> datetime | None:
    """Convert a ``since`` argument to a timezone-aware ``datetime``.

    Accepts:
    - ``None`` → ``None``
    - A ``datetime`` → returned as-is (made UTC-aware if naive)
    - A string like ``"30d"``, ``"7d"``, ``"1d"`` → now minus that delta
    - An ISO-8601 string → parsed directly

    Args:
        since: Raw since value from CLI or API.

    Returns:
        A UTC-aware ``datetime``, or ``None`` for full history.
    """
    if since is None:
        return None
    if isinstance(since, datetime):
        if since.tzinfo is None:
            return since.replace(tzinfo=timezone.utc)
        return since
    # Relative shorthand: "30d", "7d", …
    if isinstance(since, str) and since.endswith("d") and since[:-1].isdigit():
        days = int(since[:-1])
        return datetime.now(tz=timezone.utc) - timedelta(days=days)
    # ISO-8601
    dt = datetime.fromisoformat(since)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _run_source(
    source_id: str,
    since: datetime | None,
) -> None:
    """Run discover → fetch → parse → store for a single source.

    Sources with ``check_for_updates = True`` re-fetch known URLs and archive
    the old parsed state to ``document_versions`` when ``normalized_content_hash``
    differs. Sources with the default ``check_for_updates = False`` skip known
    URLs entirely (fast path).

    Args:
        source_id: Identifier of the registered source to run.
        since: Only ingest documents published after this timestamp.
    """
    from sqlalchemy import select

    from regulatory.db.models import Document, DocumentVersion, FetchLog

    sources = all_sources()
    if source_id not in sources:
        log.error("source_not_found", source_id=source_id)
        return

    source_cls = sources[source_id]
    source = source_cls()
    logger = log.bind(source_id=source_id)
    logger.info("ingest_start", since=since)

    async with get_session() as session:
        doc_count = 0
        skip_count = 0
        update_count = 0

        async for ref in source.discover(since=since):
            url_str = str(ref.url)

            # URL-keyed dedup: check if this URL is already in documents.
            url_result = await session.execute(
                select(Document).where(Document.source_url == url_str)
            )
            existing_doc = url_result.scalar_one_or_none()

            if existing_doc is not None and not source.check_for_updates:
                # Fast path: URL known, change detection not enabled for this source.
                skip_count += 1
                logger.debug("skipping_known_url", url=url_str)
                continue

            fetch_start = datetime.now(tz=timezone.utc)
            try:
                raw = await source.fetch(ref)
                normalized = source.parse(raw)
                new_hash = normalized.normalized_content_hash()
                now_utc = datetime.now(tz=timezone.utc)

                if existing_doc is not None:
                    # check_for_updates=True path: compare parsed-field hashes.
                    if existing_doc.normalized_hash == new_hash:
                        skip_count += 1
                        logger.debug("skipping_unchanged_content", url=url_str)
                        continue

                    # Content changed: archive prior state if we have one, then update in
                    # place. A null/empty prior hash means the previous fetch failed before
                    # the hash was written — there is no meaningful content state to
                    # preserve. Log and skip the archive; the documents row still updates.
                    if existing_doc.normalized_hash:
                        session.add(
                            DocumentVersion(
                                document_id=existing_doc.id,
                                normalized_hash=existing_doc.normalized_hash,
                                raw_text=existing_doc.raw_text,
                                raw_metadata=existing_doc.raw_metadata,
                                superseded_at=now_utc,
                            )
                        )
                    else:
                        logger.info(
                            "skip_version_archive_on_null_prior_hash",
                            document_id=str(existing_doc.id),
                            source_url=existing_doc.source_url,
                            source_id=existing_doc.source_id,
                        )
                    existing_doc.normalized_hash = new_hash
                    existing_doc.title = normalized.title
                    existing_doc.product_names = normalized.product_names
                    existing_doc.active_ingredients = normalized.active_ingredients
                    existing_doc.active_ingredients_raw = normalized.active_ingredients_raw
                    existing_doc.manufacturers = normalized.manufacturers
                    existing_doc.marketing_authorization_holders = (
                        normalized.marketing_authorization_holders
                    )
                    existing_doc.severity = (
                        normalized.severity.value if normalized.severity else None
                    )
                    existing_doc.date_published = normalized.date_published
                    existing_doc.date_effective = normalized.date_effective
                    existing_doc.regions_affected = normalized.regions_affected
                    existing_doc.raw_text = normalized.raw_text
                    existing_doc.raw_metadata = normalized.raw_metadata
                    existing_doc.updated_at = now_utc
                    session.add(
                        FetchLog(
                            source_id=source_id,
                            url=url_str,
                            status="success",
                            started_at=fetch_start,
                            finished_at=now_utc,
                            bytes_fetched=len(raw.content),
                        )
                    )
                    await session.commit()
                    update_count += 1
                    logger.info("document_updated", url=url_str)
                    continue

                # Brand new URL: secondary content-hash dedup (catches stable
                # sources that serve identical bytes under a redirected URL).
                hash_result = await session.execute(
                    select(Document).where(Document.source_hash == raw.source_hash)
                )
                if hash_result.scalar_one_or_none() is not None:
                    skip_count += 1
                    logger.debug("skipping_unchanged", url=url_str)
                    continue

                doc = Document.from_normalized(normalized)
                session.add(doc)
                session.add(
                    FetchLog(
                        source_id=source_id,
                        url=url_str,
                        status="success",
                        started_at=fetch_start,
                        finished_at=now_utc,
                        bytes_fetched=len(raw.content),
                    )
                )
                await session.commit()
                doc_count += 1

            except Exception as exc:
                logger.error("fetch_failed", url=url_str, error=str(exc))
                try:
                    session.add(
                        FetchLog(
                            source_id=source_id,
                            url=url_str,
                            status="error",
                            started_at=fetch_start,
                            finished_at=datetime.now(tz=timezone.utc),
                            error=str(exc),
                        )
                    )
                    await session.commit()
                except Exception as db_exc:
                    logger.warning("fetch_log_write_failed", error=str(db_exc))

        logger.info(
            "ingest_complete",
            docs_added=doc_count,
            docs_skipped=skip_count,
            docs_updated=update_count,
        )


def run(
    source_id: str | None = None,
    since: str | datetime | None = None,
) -> None:
    """Synchronous entry point for the CLI.

    Args:
        source_id: Run only this source; ``None`` means all registered sources.
        since: Ingest documents newer than this value.
            Accepts ``datetime``, ISO string, or relative shorthand like ``"30d"``.
    """
    since_dt = _parse_since(since)
    targets: list[str] = [source_id] if source_id is not None else list(all_sources().keys())

    log.info("scheduler_start", targets=targets, since=since_dt)

    async def _main() -> None:
        for sid in targets:
            await _run_source(sid, since_dt)

    asyncio.run(_main())
