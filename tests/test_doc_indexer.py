"""AC6–AC7: doc_indexer — SHA256 incremental sync, docs__ metadata schema."""
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.doc_indexer import index_markdown, index_pdf


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "sample.pdf"
    p.write_bytes(b"fake pdf bytes for testing")
    return p


@pytest.fixture
def sample_md(tmp_path: Path) -> Path:
    p = tmp_path / "doc.md"
    p.write_text("---\ntitle: Test Doc\nauthor: Alice\n---\n\n# Hello\n\nWorld.\n")
    return p


def _set_credentials(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "vk_test")
    monkeypatch.setenv("CHROMA_API_KEY", "ck_test")
    monkeypatch.setenv("CHROMA_TENANT", "tenant")
    monkeypatch.setenv("CHROMA_DATABASE", "db")


# ── credential guard ──────────────────────────────────────────────────────────

def test_index_pdf_skips_without_credentials(sample_pdf, monkeypatch):
    """Without VOYAGE_API_KEY + CHROMA_API_KEY, returns 0 and never touches T3."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    with patch("nexus.doc_indexer.T3Database") as mock_t3:
        result = index_pdf(sample_pdf, corpus="test")
    assert result == 0
    mock_t3.assert_not_called()


def test_index_markdown_skips_without_credentials(sample_md, monkeypatch):
    """Without credentials, index_markdown returns 0."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    with patch("nexus.doc_indexer.T3Database") as mock_t3:
        result = index_markdown(sample_md, corpus="test")
    assert result == 0
    mock_t3.assert_not_called()


# ── SHA256 incremental sync ───────────────────────────────────────────────────

def test_index_pdf_skips_if_hash_unchanged(sample_pdf, monkeypatch):
    """If content_hash already in T3, extraction is skipped (no re-embedding)."""
    _set_credentials(monkeypatch)
    content_hash = hashlib.sha256(sample_pdf.read_bytes()).hexdigest()

    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["existing_chunk_id"],
        "metadatas": [{"content_hash": content_hash}],
    }

    with patch("nexus.doc_indexer.T3Database") as mock_t3_class:
        with patch("nexus.doc_indexer.PDFExtractor") as mock_extractor_class:
            mock_t3 = MagicMock()
            mock_t3_class.return_value = mock_t3
            mock_t3.get_or_create_collection.return_value = mock_col
            result = index_pdf(sample_pdf, corpus="mybook")

    assert result == 0
    mock_extractor_class.assert_not_called()


# ── chunk upsert ──────────────────────────────────────────────────────────────

def test_index_pdf_upserts_chunks_when_new(sample_pdf, monkeypatch):
    """New file: extracts, chunks, and upserts into T3 collection."""
    _set_credentials(monkeypatch)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    mock_chunk = MagicMock()
    mock_chunk.text = "chunk text content"
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 18, "page_number": 1}

    with patch("nexus.doc_indexer.T3Database") as mock_t3_class:
        with patch("nexus.doc_indexer.PDFExtractor") as mock_extractor_class:
            with patch("nexus.doc_indexer.PDFChunker") as mock_chunker_class:
                mock_t3 = MagicMock()
                mock_t3_class.return_value = mock_t3
                mock_t3.get_or_create_collection.return_value = mock_col

                mock_extractor = MagicMock()
                mock_extractor_class.return_value = mock_extractor
                mock_extractor.extract.return_value = MagicMock(
                    text="extracted text",
                    metadata={
                        "extraction_method": "pymupdf4llm_markdown",
                        "page_count": 1,
                        "format": "markdown",
                        "page_boundaries": [],
                    },
                )

                mock_chunker = MagicMock()
                mock_chunker_class.return_value = mock_chunker
                mock_chunker.chunk.return_value = [mock_chunk]

                result = index_pdf(sample_pdf, corpus="mybook")

    assert result == 1
    mock_col.add.assert_called_once()


# ── metadata schema ───────────────────────────────────────────────────────────

def test_docs_metadata_schema_complete(sample_md, monkeypatch):
    """All required fields from the spec are present in upserted chunk metadata."""
    _set_credentials(monkeypatch)

    required_fields = {
        "source_path", "source_title", "source_author", "source_date",
        "corpus", "store_type", "page_count", "page_number", "section_title",
        "format", "extraction_method", "chunk_index", "chunk_count",
        "chunk_start_char", "chunk_end_char", "embedding_model",
        "indexed_at", "content_hash",
    }

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    captured_metadatas: list[dict] = []

    def capture_add(**kwargs):
        captured_metadatas.extend(kwargs.get("metadatas", []))

    mock_col.add.side_effect = capture_add

    mock_chunk = MagicMock()
    mock_chunk.text = "chunk text"
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {
        "chunk_start_char": 0,
        "chunk_end_char": 10,
        "page_number": 0,
        "header_path": "Hello",
    }

    with patch("nexus.doc_indexer.T3Database") as mock_t3_class:
        with patch("nexus.doc_indexer.SemanticMarkdownChunker") as mock_chunker_class:
            mock_t3 = MagicMock()
            mock_t3_class.return_value = mock_t3
            mock_t3.get_or_create_collection.return_value = mock_col

            mock_chunker = MagicMock()
            mock_chunker_class.return_value = mock_chunker
            mock_chunker.chunk.return_value = [mock_chunk]

            index_markdown(sample_md, corpus="docs")

    assert len(captured_metadatas) == 1, "Expected exactly one chunk to be upserted"
    meta = captured_metadatas[0]
    missing = required_fields - meta.keys()
    assert not missing, f"Missing metadata fields: {missing}"
