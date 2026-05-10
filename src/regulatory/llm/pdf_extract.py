"""Structured PDF → schema extraction via Anthropic API.

Used by the ``ppb_ke_alerts`` adapter when pdfplumber and PyMuPDF both fail
to extract useful text from scanned or malformed PDFs.
"""

from __future__ import annotations

from typing import Any

import structlog

from regulatory.ingestion.pdf import extract_structured

log = structlog.get_logger(__name__)

# Schema hint sent to the model when extracting PPB alert data
PPB_ALERT_SCHEMA: dict[str, Any] = {
    "title": "string — document title",
    "product_names": ["list of product names"],
    "active_ingredients": ["list of active ingredients"],
    "manufacturers": ["list of manufacturer names"],
    "batch_numbers": ["list of batch/lot numbers"],
    "reason": "string — reason for recall or alert",
    "date_published": "YYYY-MM-DD",
    "severity": "one of: class_1, class_2, class_3, unclassified",
    "regions_affected": ["list of regions / countries"],
}


def extract_ppb_alert(pdf_bytes: bytes, source_hash: str) -> dict[str, Any] | None:
    """Extract structured PPB alert data from a PDF using the Anthropic API.

    Args:
        pdf_bytes: Raw PDF bytes.
        source_hash: sha256 hash for caching.

    Returns:
        Dict matching ``PPB_ALERT_SCHEMA``, or ``None`` on failure.
    """
    log.info("extracting_ppb_alert_structured", source_hash=source_hash)
    return extract_structured(pdf_bytes, source_hash, PPB_ALERT_SCHEMA)
