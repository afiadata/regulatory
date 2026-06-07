"""System prompt assembly for the regulatory agent.

The prompt template lives in templates/system_prompt.md and is a static
Markdown file with {placeholder} substitutions.  Runtime values (corpus
date range, rule version, tool budget) are filled in at startup.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import structlog
import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from regulatory.db.models import Document

log = structlog.get_logger(__name__)

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "system_prompt.md"


def _load_config() -> dict[str, object]:
    """Load agent.yaml from the project config directory."""
    config_path = Path(__file__).parents[3] / "config" / "agent.yaml"
    with config_path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)  # type: ignore[no-any-return]


async def get_corpus_date_range(session: AsyncSession) -> tuple[date | None, date | None]:
    """Query the documents table for the earliest and latest date_published.

    Args:
        session: Database session.

    Returns:
        Tuple of (earliest_date, latest_date).  Either may be None if the
        documents table is empty.
    """
    result = await session.execute(
        select(
            func.min(Document.date_published).label("start"),
            func.max(Document.date_published).label("end"),
        )
    )
    row = result.one()
    return row.start, row.end


def assemble_system_prompt(
    *,
    corpus_start: date | None,
    corpus_end: date | None,
    rule_version: str,
    tool_calls_per_turn: int,
) -> str:
    """Assemble the system prompt by substituting runtime values into the template.

    Args:
        corpus_start: Earliest document date in the corpus.
        corpus_end: Latest document date in the corpus.
        rule_version: Risk rules config version string.
        tool_calls_per_turn: Hard cap on tool calls per agent turn.

    Returns:
        Fully assembled system prompt string.
    """
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")

    start_str = corpus_start.isoformat() if corpus_start else "unknown"
    end_str = corpus_end.isoformat() if corpus_end else "unknown"

    return template.format(
        corpus_start=start_str,
        corpus_end=end_str,
        rule_version=rule_version,
        tool_calls_per_turn=tool_calls_per_turn,
    )
