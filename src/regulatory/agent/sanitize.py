"""Content sanitization and untrusted-content wrapping for prompt injection defence.

All text originating outside system control (document raw_text, manufacturer
descriptions, scraped recall notices) passes through this module before it is
included in the LLM context.  The two-step approach — sanitize then wrap —
limits the blast radius of any injection attempt that reaches the model.

Named constants for the count_inflation_likely heuristic live here so they
are easy to find and tune independently of the tool logic.
"""

from __future__ import annotations

import re

# Heuristic thresholds for repeat-violator count inflation detection.
# A repeat_violator signal is flagged when the document count meets the floor
# AND the openFDA share meets or exceeds the share threshold.
# See docs/followup_issues/recall_event_clustering.md for rationale.
COUNT_INFLATION_FLOOR: int = 10
COUNT_INFLATION_OPENFDA_SHARE: float = 0.8

# Strip everything in \x00-\x1f except newline (\n = \x0a) and tab (\t = \x09).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# Closing and opening tag patterns that would escape the wrapper boundary.
_CLOSE_TAG_RE = re.compile(r"</untrusted_content", re.IGNORECASE)
_OPEN_TAG_RE = re.compile(r"<untrusted_content(?=[^_]|$)", re.IGNORECASE)

_SNIPPET_MAX_CHARS: int = 300
_RAW_TEXT_PAGE_CHARS: int = 8000


def sanitize_text(text: str) -> str:
    """Strip control characters and escape untrusted_content tag boundaries.

    Args:
        text: Raw string from an external source.

    Returns:
        Sanitized string safe to embed inside an <untrusted_content> block.
    """
    text = _CONTROL_CHARS_RE.sub("", text)
    text = _CLOSE_TAG_RE.sub("</untrusted_content_ESCAPED>", text)
    text = _OPEN_TAG_RE.sub("<untrusted_content_ESCAPED", text)
    return text


def wrap_untrusted(text: str, source: str, content_type: str) -> str:
    """Sanitize and wrap text in injection-resistant delimiters.

    The system prompt instructs the LLM to treat anything inside these tags
    as data, not instructions.

    Args:
        text: Raw untrusted content.
        source: Human-readable identifier, e.g. ``"document:abc-123"``.
        content_type: E.g. ``"raw_text"`` or ``"snippet"``.

    Returns:
        Delimited string ready for insertion into the assistant context.
    """
    sanitized = sanitize_text(text)
    return (
        f'<untrusted_content source="{source}" type="{content_type}">\n'
        f"{sanitized}\n"
        f"</untrusted_content>"
    )


def truncate_snippet(text: str) -> str:
    """Return the first SNIPPET_MAX_CHARS characters, sanitized.

    Args:
        text: Source text.

    Returns:
        Sanitized snippet of at most SNIPPET_MAX_CHARS characters.
    """
    return sanitize_text(text[:_SNIPPET_MAX_CHARS])


def paginate_raw_text(text: str, offset: int = 0) -> tuple[str, bool]:
    """Return one page of raw_text starting at offset, sanitized.

    Args:
        text: Full raw text.
        offset: Character offset to start from.

    Returns:
        Tuple of (page_text, truncated) where ``truncated`` is True if there
        is more text beyond this page.
    """
    page = text[offset : offset + _RAW_TEXT_PAGE_CHARS]
    truncated = len(text) > offset + _RAW_TEXT_PAGE_CHARS
    return sanitize_text(page), truncated
