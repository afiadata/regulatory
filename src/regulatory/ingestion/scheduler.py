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

    Args:
        source_id: Identifier of the registered source to run.
        since: Only ingest documents published after this timestamp.
    """
    from sqlalchemy import select

    from regulatory.db.models import Document, FetchLog

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

        async for ref in source.discover(since=since):
            fetch_start = datetime.now(tz=timezone.utc)
            try:
                raw = await source.fetch(ref)

                # Skip unchanged content
                existing = await session.execute(
                    select(Document).where(Document.source_hash == raw.source_hash)
                )
                if existing.scalar_one_or_none() is not None:
                    skip_count += 1
                    logger.debug("skipping_unchanged", url=str(ref.url))
                    continue

                normalized = source.parse(raw)

                doc = Document.from_normalized(normalized)
                session.add(doc)

                fetch_log = FetchLog(
                    source_id=source_id,
                    url=str(ref.url),
                    status="success",
                    started_at=fetch_start,
                    finished_at=datetime.now(tz=timezone.utc),
                    bytes_fetched=len(raw.content),
                )
                session.add(fetch_log)
                await session.commit()
                doc_count += 1

            except Exception as exc:
                fetch_log = FetchLog(
                    source_id=source_id,
                    url=str(ref.url),
                    status="error",
                    started_at=fetch_start,
                    finished_at=datetime.now(tz=timezone.utc),
                    error=str(exc),
                )
                session.add(fetch_log)
                await session.commit()
                logger.error("fetch_failed", url=str(ref.url), error=str(exc))

        logger.info("ingest_complete", docs_added=doc_count, docs_skipped=skip_count)


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
