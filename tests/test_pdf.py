"""Tests for the three-stage PDF extraction fallback chain.

Stage order: pdfplumber → PyMuPDF → Anthropic API (last resort).
All Anthropic API calls are mocked — no live network calls.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from regulatory.ingestion.pdf import (
    _extract_anthropic,
    _extract_pdfplumber,
    _extract_pymupdf,
    extract_text,
)

# A real PPB PDF that has a text layer (pdfplumber and PyMuPDF both succeed on it)
_FIXTURE_PDF = Path(__file__).parent.parent / "data" / "ppb_pdfs" / "GUIDELINES" / "ppb_1.pdf"


@pytest.fixture(scope="module")
def pdf_bytes() -> bytes:
    """Return raw bytes for a real PPB guideline PDF."""
    return _FIXTURE_PDF.read_bytes()


@pytest.fixture(scope="module")
def pdf_hash(pdf_bytes: bytes) -> str:
    """Return sha256 hex digest of the fixture PDF."""
    return hashlib.sha256(pdf_bytes).hexdigest()


# ---------------------------------------------------------------------------
# Individual extractor tests
# ---------------------------------------------------------------------------


class TestExtractPdfplumber:
    """pdfplumber extracts text from a text-layer PDF."""

    def test_returns_text(self, pdf_bytes: bytes) -> None:
        text = _extract_pdfplumber(pdf_bytes)
        assert text is not None
        assert len(text) > 50

    def test_returns_none_on_garbage(self) -> None:
        result = _extract_pdfplumber(b"not a pdf at all")
        assert result is None


class TestExtractPymupdf:
    """PyMuPDF extracts text from the same text-layer PDF."""

    def test_returns_text(self, pdf_bytes: bytes) -> None:
        text = _extract_pymupdf(pdf_bytes)
        assert text is not None
        assert len(text) > 50

    def test_returns_none_on_garbage(self) -> None:
        result = _extract_pymupdf(b"not a pdf at all")
        assert result is None


# ---------------------------------------------------------------------------
# Anthropic stage (fully mocked)
# ---------------------------------------------------------------------------


class TestExtractAnthropic:
    """Anthropic extraction and cache behaviour — no live API calls."""

    def _make_mock_client(self, response_text: str) -> MagicMock:
        text_block = MagicMock(spec=anthropic.types.TextBlock)
        text_block.text = response_text

        api_response = MagicMock()
        api_response.content = [text_block]

        inner = MagicMock()
        inner.messages.create.return_value = api_response

        client = MagicMock()
        client.client = inner
        client.model = "claude-sonnet-4-6"
        client.get_cached_response.return_value = None
        client.cache_response = MagicMock()
        return client

    def test_returns_text_from_api(self, pdf_bytes: bytes, pdf_hash: str) -> None:
        mock_client = self._make_mock_client("Extracted PDF text here.")
        with patch(
            "regulatory.llm.client.get_anthropic_client",
            return_value=mock_client,
        ):
            result = _extract_anthropic(pdf_bytes, pdf_hash)
        assert result == "Extracted PDF text here."
        mock_client.cache_response.assert_called_once_with(pdf_hash, "Extracted PDF text here.")

    def test_cache_hit_skips_api(self, pdf_bytes: bytes, pdf_hash: str) -> None:
        mock_client = MagicMock()
        mock_client.get_cached_response.return_value = "Cached text"
        with patch(
            "regulatory.llm.client.get_anthropic_client",
            return_value=mock_client,
        ):
            result = _extract_anthropic(pdf_bytes, pdf_hash)
        assert result == "Cached text"
        mock_client.client.messages.create.assert_not_called()

    def test_returns_none_on_api_error(self, pdf_bytes: bytes, pdf_hash: str) -> None:
        mock_client = MagicMock()
        mock_client.get_cached_response.return_value = None
        mock_client.client.messages.create.side_effect = RuntimeError("API down")
        with patch(
            "regulatory.llm.client.get_anthropic_client",
            return_value=mock_client,
        ):
            result = _extract_anthropic(pdf_bytes, pdf_hash)
        assert result is None


# ---------------------------------------------------------------------------
# Fallback chain integration tests
# ---------------------------------------------------------------------------


class TestExtractTextChain:
    """extract_text() fallback chain behaviour."""

    def test_pdfplumber_wins(self, pdf_bytes: bytes, pdf_hash: str) -> None:
        """Real PDF: pdfplumber succeeds on the first attempt."""
        text, method = extract_text(pdf_bytes, pdf_hash)
        assert method == "pdfplumber"
        assert len(text) > 50

    def test_falls_back_to_pymupdf(self, pdf_bytes: bytes, pdf_hash: str) -> None:
        """When pdfplumber returns None, PyMuPDF takes over."""
        with patch("regulatory.ingestion.pdf._extract_pdfplumber", return_value=None):
            text, method = extract_text(pdf_bytes, pdf_hash)
        assert method == "pymupdf"
        assert len(text) > 50

    def test_falls_back_to_anthropic(self, pdf_bytes: bytes, pdf_hash: str) -> None:
        """When both pdfplumber and PyMuPDF return None, Anthropic is used."""
        mock_client = MagicMock()
        mock_client.get_cached_response.return_value = None
        mock_client.cache_response = MagicMock()

        text_block = MagicMock(spec=anthropic.types.TextBlock)
        text_block.text = "Anthropic extracted text"
        api_resp = MagicMock()
        api_resp.content = [text_block]
        mock_client.client.messages.create.return_value = api_resp

        with (
            patch("regulatory.ingestion.pdf._extract_pdfplumber", return_value=None),
            patch("regulatory.ingestion.pdf._extract_pymupdf", return_value=None),
            patch(
                "regulatory.llm.client.get_anthropic_client",
                return_value=mock_client,
            ),
        ):
            text, method = extract_text(pdf_bytes, pdf_hash)

        assert method == "anthropic"
        assert text == "Anthropic extracted text"

    def test_all_fail_raises(self, pdf_bytes: bytes, pdf_hash: str) -> None:
        """RuntimeError when all three stages fail."""
        with (
            patch("regulatory.ingestion.pdf._extract_pdfplumber", return_value=None),
            patch("regulatory.ingestion.pdf._extract_pymupdf", return_value=None),
            patch("regulatory.ingestion.pdf._extract_anthropic", return_value=None),
            pytest.raises(RuntimeError, match="All PDF extraction methods failed"),
        ):
            extract_text(pdf_bytes, pdf_hash)
