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
