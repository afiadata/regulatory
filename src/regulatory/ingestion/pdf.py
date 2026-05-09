"""PDF text extraction with a three-stage fallback chain.

Extraction order:
1. ``pdfplumber`` — best for text-layer PDFs.
2. ``PyMuPDF`` (``fitz``) — fallback for difficult layouts.
3. Anthropic API — last resort for scanned / malformed PDFs.

Import :func:`extract_text` for all PDF extraction needs.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import anthropic
import structlog

log = structlog.get_logger(__name__)


def _first_text_block(content: list[anthropic.types.ContentBlock]) -> str:
    """Return the text from the first TextBlock in *content*, or empty string.

    Args:
        content: List of Anthropic content blocks.

    Returns:
        Text string from the first ``TextBlock``, or ``""`` if none found.
    """
    for block in content:
        if isinstance(block, anthropic.types.TextBlock):
            return block.text
    return ""


def _extract_pdfplumber(pdf_bytes: bytes) -> str | None:
    """Attempt extraction with pdfplumber.

    Args:
        pdf_bytes: Raw PDF bytes.

    Returns:
        Extracted text string, or ``None`` on failure.
    """
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
            text = "\n".join(pages).strip()
            if text:
                return text
            return None
    except Exception as exc:
        log.debug("pdfplumber_failed", error=str(exc))
        return None


def _extract_pymupdf(pdf_bytes: bytes) -> str | None:
    """Attempt extraction with PyMuPDF (fitz).

    Args:
        pdf_bytes: Raw PDF bytes.

    Returns:
        Extracted text string, or ``None`` on failure.
    """
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = [doc[i].get_text("text") for i in range(doc.page_count)]
        text = "\n".join(pages).strip()
        doc.close()
        if text:
            return text
        return None
    except Exception as exc:
        log.debug("pymupdf_failed", error=str(exc))
        return None


def _extract_anthropic(
    pdf_bytes: bytes,
    source_hash: str,
    prompt: str | None = None,
) -> str | None:
    """Attempt extraction via the Anthropic API (last resort).

    Sends the PDF as a base64-encoded document block. Response is cached
    by content hash to avoid re-billing for the same PDF.

    Args:
        pdf_bytes: Raw PDF bytes.
        source_hash: sha256 hash of *pdf_bytes*, used for cache key.
        prompt: Custom extraction prompt. Defaults to generic text-extraction
            instruction.

    Returns:
        Extracted text string, or ``None`` on failure.
    """
    try:
        from regulatory.llm.client import get_anthropic_client

        client = get_anthropic_client()
        cached = client.get_cached_response(source_hash)
        if cached is not None:
            log.debug("anthropic_cache_hit", source_hash=source_hash)
            return str(cached)

        if prompt is None:
            prompt = (
                "Extract all text from this PDF document. "
                "Return the full text content preserving paragraph structure. "
                "Do not summarize or interpret — return the raw text only."
            )

        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode()
        response = client.client.messages.create(
            model=client.model,
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
        text = _first_text_block(response.content)
        client.cache_response(source_hash, text)
        return text if text else None
    except Exception as exc:
        log.error("anthropic_pdf_extraction_failed", error=str(exc))
        return None


def extract_text(pdf_bytes: bytes, source_hash: str = "") -> tuple[str, str]:
    """Extract text from a PDF using the three-stage fallback chain.

    Args:
        pdf_bytes: Raw PDF bytes.
        source_hash: sha256 hash of *pdf_bytes*, used for Anthropic cache.
            If empty, caching is skipped.

    Returns:
        Tuple of ``(text, method)`` where *method* is one of
        ``"pdfplumber"``, ``"pymupdf"``, or ``"anthropic"``.

    Raises:
        RuntimeError: If all three extraction methods fail.
    """
    text = _extract_pdfplumber(pdf_bytes)
    if text:
        log.debug("pdf_extracted", method="pdfplumber", chars=len(text))
        return text, "pdfplumber"

    text = _extract_pymupdf(pdf_bytes)
    if text:
        log.debug("pdf_extracted", method="pymupdf", chars=len(text))
        return text, "pymupdf"

    log.info("falling_back_to_anthropic_pdf", source_hash=source_hash)
    text = _extract_anthropic(pdf_bytes, source_hash)
    if text:
        log.info("pdf_extracted", method="anthropic", chars=len(text))
        return text, "anthropic"

    raise RuntimeError("All PDF extraction methods failed.")


def extract_structured(
    pdf_bytes: bytes,
    source_hash: str,
    schema_hint: dict[str, Any],
) -> dict[str, Any] | None:
    """Use the Anthropic API to extract structured JSON from a PDF.

    Args:
        pdf_bytes: Raw PDF bytes.
        source_hash: sha256 hash for caching.
        schema_hint: Dict describing the desired JSON output structure.

    Returns:
        Parsed JSON dict, or ``None`` on failure.
    """
    try:
        from regulatory.llm.client import get_anthropic_client

        client = get_anthropic_client()
        cache_key = f"structured:{source_hash}"
        cached = client.get_cached_response(cache_key)
        if cached is not None:
            log.debug("anthropic_structured_cache_hit", source_hash=source_hash)
            return dict(cached)

        schema_str = json.dumps(schema_hint, indent=2)
        prompt = (
            f"Extract information from this regulatory document and return a JSON object "
            f"matching this schema:\n{schema_str}\n\n"
            "Return ONLY valid JSON, no markdown, no explanation."
        )
        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode()
        response = client.client.messages.create(
            model=client.model,
            max_tokens=2048,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
        raw_json = _first_text_block(response.content)
        result: dict[str, Any] = json.loads(raw_json)
        client.cache_response(cache_key, result)
        return result
    except Exception as exc:
        log.error("anthropic_structured_extraction_failed", error=str(exc))
        return None
