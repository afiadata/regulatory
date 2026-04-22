"""Shared pytest fixtures for the regulatory test suite."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from regulatory.models import DocumentRef, RawDocument

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture()
def openfda_sample_json() -> dict:  # type: ignore[type-arg]
    """Load the recorded openFDA enforcement response fixture.

    Returns:
        Parsed JSON dict matching the openFDA enforcement API response schema.
    """
    path = FIXTURES_DIR / "openfda_sample.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def openfda_raw_document(openfda_sample_json: dict) -> RawDocument:  # type: ignore[type-arg]
    """Build a ``RawDocument`` wrapping the openFDA fixture.

    Returns:
        A :class:`~regulatory.models.RawDocument` ready to pass to
        ``OpenFdaDrugSource.parse()``.
    """
    content = json.dumps(openfda_sample_json).encode()
    source_hash = hashlib.sha256(content).hexdigest()
    ref = DocumentRef(
        source_id="openfda_drug",
        url="https://api.fda.gov/drug/enforcement.json?search=recall_number:D-001-2024-00001",
        document_id="D-001-2024-00001",
    )
    return RawDocument(
        ref=ref,
        content=content,
        content_type="application/json",
        source_hash=source_hash,
        fetched_at=datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture()
def ppb_pdf_bytes() -> bytes:
    """Load a real PPB PDF from data/ppb_pdfs/ as a test fixture.

    Walks the directory tree and returns the first PDF found. Falls back to
    minimal synthetic PDF bytes if the directory is empty.

    Returns:
        Raw PDF bytes suitable for passing to the PDF extraction pipeline.
    """
    ppb_dir = Path(__file__).parent.parent / "data" / "ppb_pdfs"
    pdfs = list(ppb_dir.rglob("*.pdf"))
    if pdfs:
        return pdfs[0].read_bytes()

    # Minimal valid PDF (1-page, text-layer) as fallback
    minimal = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R"
        b"/Resources<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>"
        b"/Contents 4 0 R>>endobj\n"
        b"4 0 obj<</Length 44>>\nstream\n"
        b"BT /F1 12 Tf 100 700 Td (Product Recall Notice) Tj ET\n"
        b"endstream\nendobj\n"
        b"xref\n0 5\n0000000000 65535 f \n"
        b"trailer<</Size 5/Root 1 0 R>>\nstartxref\n9\n%%EOF"
    )
    return minimal


@pytest.fixture()
def ppb_raw_document(ppb_pdf_bytes: bytes) -> RawDocument:
    """Build a ``RawDocument`` wrapping the PPB PDF fixture.

    Returns:
        A :class:`~regulatory.models.RawDocument` ready to pass to
        ``PpbKeAlertsSource.parse()``.
    """
    source_hash = hashlib.sha256(ppb_pdf_bytes).hexdigest()
    ref = DocumentRef(
        source_id="ppb_ke_alerts",
        url="https://web.pharmacyboardkenya.org/download/product-alert-001.pdf",
        title="Product Recall Notice",
    )
    return RawDocument(
        ref=ref,
        content=ppb_pdf_bytes,
        content_type="application/pdf",
        source_hash=source_hash,
        fetched_at=datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
    )
