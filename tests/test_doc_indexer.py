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
    with patch("nexus.doc_indexer.make_t3") as mock_factory:
        result = index_pdf(sample_pdf, corpus="test")
    assert result == 0
    mock_factory.assert_not_called()


def test_index_markdown_skips_without_credentials(sample_md, monkeypatch):
    """Without credentials, index_markdown returns 0."""
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    with patch("nexus.doc_indexer.make_t3") as mock_factory:
        result = index_markdown(sample_md, corpus="test")
    assert result == 0
    mock_factory.assert_not_called()


# ── SHA256 incremental sync ───────────────────────────────────────────────────

def test_index_pdf_skips_if_hash_unchanged(sample_pdf, monkeypatch):
    """If content_hash AND embedding_model already match T3, extraction is skipped."""
    _set_credentials(monkeypatch)
    content_hash = hashlib.sha256(sample_pdf.read_bytes()).hexdigest()

    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["existing_chunk_id"],
        # docs__ collections target voyage-context-3; include both fields for skip
        "metadatas": [{"content_hash": content_hash, "embedding_model": "voyage-context-3"}],
    }

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_extractor_class:
            result = index_pdf(sample_pdf, corpus="mybook")

    assert result == 0
    mock_extractor_class.assert_not_called()


# ── chunk upsert ──────────────────────────────────────────────────────────────

def test_index_pdf_upserts_chunks_when_new(sample_pdf, monkeypatch):
    """New file: extracts, chunks, and upserts into T3 collection.

    With a single chunk, CCE is skipped (requires >= 2 chunks) and the
    standard embed() path is used, which calls upsert_chunks_with_embeddings.
    """
    _set_credentials(monkeypatch)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    mock_chunk = MagicMock()
    mock_chunk.text = "chunk text content"
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 18, "page_number": 1}

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    mock_voyage_client = MagicMock()
    mock_voyage_result = MagicMock()
    mock_voyage_result.embeddings = [[0.1, 0.2, 0.3]]
    mock_voyage_client.embed.return_value = mock_voyage_result

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_extractor_class:
            with patch("nexus.doc_indexer.PDFChunker") as mock_chunker_class:
                with patch("voyageai.Client", return_value=mock_voyage_client):
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
    # Single chunk → CCE skipped; standard embed used → upsert_chunks_with_embeddings called
    mock_t3.upsert_chunks_with_embeddings.assert_called_once()


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

    def capture_upsert_with_embeddings(collection, ids, documents, embeddings, metadatas):
        captured_metadatas.extend(metadatas)

    mock_chunk = MagicMock()
    mock_chunk.text = "chunk text"
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {
        "chunk_start_char": 0,
        "chunk_end_char": 10,
        "page_number": 0,
        "header_path": "Hello",
    }

    mock_voyage_client = MagicMock()
    mock_voyage_result = MagicMock()
    mock_voyage_result.embeddings = [[0.1, 0.2]]
    mock_voyage_client.embed.return_value = mock_voyage_result

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    mock_t3.upsert_chunks_with_embeddings.side_effect = capture_upsert_with_embeddings
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.SemanticMarkdownChunker") as mock_chunker_class:
            with patch("voyageai.Client", return_value=mock_voyage_client):
                mock_chunker = MagicMock()
                mock_chunker_class.return_value = mock_chunker
                mock_chunker.chunk.return_value = [mock_chunk]

                index_markdown(sample_md, corpus="docs")

    assert len(captured_metadatas) >= 1, "Expected at least one chunk to be upserted"
    meta = captured_metadatas[0]
    missing = required_fields - meta.keys()
    assert not missing, f"Missing metadata fields: {missing}"


# ── nexus-3zj: _sha256 uses streaming hash, not read_bytes ───────────────────

def test_sha256_does_not_call_read_bytes(tmp_path: Path):
    """_sha256 streams the file instead of loading it all at once."""
    import nexus.doc_indexer as di_mod

    large_file = tmp_path / "large.bin"
    large_file.write_bytes(b"x" * 1024)

    # Verify that open() is called and read_bytes() is NOT called
    real_open = large_file.open
    opened = []

    class _TrackingPath(type(large_file)):
        def read_bytes(self):  # type: ignore[override]
            raise AssertionError("read_bytes() called — should stream instead")

        def open(self, *a, **kw):  # type: ignore[override]
            fh = real_open(*a, **kw)
            opened.append(True)
            return fh

    tracked = _TrackingPath(large_file)
    result = di_mod._sha256(tracked)
    assert len(result) == 64  # hex SHA256 is 64 chars
    assert opened  # open() was actually called


# ── nexus-blz: store_type correctness ────────────────────────────────────────

def test_index_pdf_sets_store_type_pdf(sample_pdf, monkeypatch):
    """index_pdf stores store_type='pdf', not 'docs'."""
    _set_credentials(monkeypatch)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    captured: list[dict] = []

    def capture_upsert_with_embeddings(collection, ids, documents, embeddings, metadatas):
        captured.extend(metadatas)

    mock_chunk = MagicMock()
    mock_chunk.text = "text"
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 4, "page_number": 1}

    mock_voyage_client = MagicMock()
    mock_voyage_result = MagicMock()
    mock_voyage_result.embeddings = [[0.1, 0.2]]
    mock_voyage_client.embed.return_value = mock_voyage_result

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    mock_t3.upsert_chunks_with_embeddings.side_effect = capture_upsert_with_embeddings
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_extractor_class:
            with patch("nexus.doc_indexer.PDFChunker") as mock_chunker_class:
                with patch("voyageai.Client", return_value=mock_voyage_client):
                    mock_extractor = MagicMock()
                    mock_extractor_class.return_value = mock_extractor
                    mock_extractor.extract.return_value = MagicMock(
                        text="txt", metadata={"page_count": 1, "format": "pdf", "extraction_method": "x"}
                    )

                    mock_chunker = MagicMock()
                    mock_chunker_class.return_value = mock_chunker
                    mock_chunker.chunk.return_value = [mock_chunk]

                    index_pdf(sample_pdf, corpus="mybook")

    assert captured, "No metadata captured"
    assert captured[0]["store_type"] == "pdf", f"Expected 'pdf', got {captured[0]['store_type']!r}"


def test_index_markdown_sets_store_type_markdown(sample_md, monkeypatch):
    """index_markdown stores store_type='markdown', not 'docs'."""
    _set_credentials(monkeypatch)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    captured: list[dict] = []

    def capture_upsert_with_embeddings(collection, ids, documents, embeddings, metadatas):
        captured.extend(metadatas)

    mock_chunk = MagicMock()
    mock_chunk.text = "text"
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 4, "page_number": 0, "header_path": "H"}

    mock_voyage_client = MagicMock()
    mock_voyage_result = MagicMock()
    mock_voyage_result.embeddings = [[0.1, 0.2]]
    mock_voyage_client.embed.return_value = mock_voyage_result

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    mock_t3.upsert_chunks_with_embeddings.side_effect = capture_upsert_with_embeddings
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.SemanticMarkdownChunker") as mock_chunker_class:
            with patch("voyageai.Client", return_value=mock_voyage_client):
                mock_chunker = MagicMock()
                mock_chunker_class.return_value = mock_chunker
                mock_chunker.chunk.return_value = [mock_chunk]

                index_markdown(sample_md, corpus="docs")

    assert captured, "No metadata captured"
    assert captured[0]["store_type"] == "markdown", f"Expected 'markdown', got {captured[0]['store_type']!r}"


# ── nexus-bkk: chunk offsets adjusted for frontmatter ────────────────────────

def test_index_markdown_offsets_account_for_frontmatter(tmp_path: Path, monkeypatch):
    """chunk_start_char/chunk_end_char are adjusted by the frontmatter length."""
    _set_credentials(monkeypatch)

    # File with 30-char frontmatter prefix
    fm = "---\ntitle: Test\n---\n"  # 20 chars
    body_content = "# Hello\n\nWorld content."
    md_path = tmp_path / "fm_doc.md"
    md_path.write_text(fm + body_content)

    frontmatter_len = len(fm)  # should be added to chunk offsets

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    captured: list[dict] = []

    def capture_upsert_with_embeddings(collection, ids, documents, embeddings, metadatas):
        captured.extend(metadatas)

    # Simulate naive chunking: offsets are relative to body (0-based)
    mock_chunk = MagicMock()
    mock_chunk.text = "Hello World content."
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {
        "chunk_start_char": 0,
        "chunk_end_char": len(body_content),
        "page_number": 0,
        "header_path": "Hello",
    }

    mock_voyage_client = MagicMock()
    mock_voyage_result = MagicMock()
    mock_voyage_result.embeddings = [[0.1, 0.2]]
    mock_voyage_client.embed.return_value = mock_voyage_result

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    mock_t3.upsert_chunks_with_embeddings.side_effect = capture_upsert_with_embeddings
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.SemanticMarkdownChunker") as mock_chunker_class:
            with patch("voyageai.Client", return_value=mock_voyage_client):
                mock_chunker = MagicMock()
                mock_chunker_class.return_value = mock_chunker
                mock_chunker.chunk.return_value = [mock_chunk]

                index_markdown(md_path, corpus="docs")

    assert captured, "No metadata captured"
    # Offsets must be shifted by frontmatter_len
    assert captured[0]["chunk_start_char"] == frontmatter_len
    assert captured[0]["chunk_end_char"] == frontmatter_len + len(body_content)


def test_index_markdown_no_frontmatter_offsets_unchanged(tmp_path: Path, monkeypatch):
    """When no frontmatter, chunk offsets are not shifted."""
    _set_credentials(monkeypatch)

    body = "# Hello\n\nWorld."
    md_path = tmp_path / "no_fm.md"
    md_path.write_text(body)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    captured: list[dict] = []

    def capture_upsert_with_embeddings(collection, ids, documents, embeddings, metadatas):
        captured.extend(metadatas)

    mock_chunk = MagicMock()
    mock_chunk.text = "Hello World."
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {
        "chunk_start_char": 5,
        "chunk_end_char": 15,
        "page_number": 0,
        "header_path": "",
    }

    mock_voyage_client = MagicMock()
    mock_voyage_result = MagicMock()
    mock_voyage_result.embeddings = [[0.1, 0.2]]
    mock_voyage_client.embed.return_value = mock_voyage_result

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    mock_t3.upsert_chunks_with_embeddings.side_effect = capture_upsert_with_embeddings
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.SemanticMarkdownChunker") as mock_chunker_class:
            with patch("voyageai.Client", return_value=mock_voyage_client):
                mock_chunker = MagicMock()
                mock_chunker_class.return_value = mock_chunker
                mock_chunker.chunk.return_value = [mock_chunk]

                index_markdown(md_path, corpus="docs")

    assert captured, "No metadata captured"
    assert captured[0]["chunk_start_char"] == 5  # no shift
    assert captured[0]["chunk_end_char"] == 15


# ── nexus-370: CCE helpers ────────────────────────────────────────────────────

def test_estimate_tokens_returns_reasonable_value():
    """300 chars of text should estimate to ~100 tokens (3 chars/token)."""
    from nexus.doc_indexer import _estimate_tokens

    chunks = ["a" * 300]
    result = _estimate_tokens(chunks)
    assert result == 100


def test_estimate_tokens_multi_chunk():
    """Estimate sums all chunk lengths then divides by 3."""
    from nexus.doc_indexer import _estimate_tokens

    chunks = ["a" * 150, "b" * 150]
    assert _estimate_tokens(chunks) == 100


def test_embed_with_fallback_calls_cce_for_docs_collection(monkeypatch):
    """When model=voyage-context-3 and len(chunks) >= 2, contextualized_embed is called."""
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.embeddings = [[[0.1, 0.2], [0.3, 0.4]]]
    mock_client.contextualized_embed.return_value = mock_result

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, actual_model = _embed_with_fallback(
            chunks=["chunk one", "chunk two"],
            model="voyage-context-3",
            api_key="vk_test",
        )

    mock_client.contextualized_embed.assert_called_once()
    assert embeddings == [[0.1, 0.2], [0.3, 0.4]]
    assert actual_model == "voyage-context-3"


def test_embed_with_fallback_skips_cce_for_single_chunk(monkeypatch):
    """Single chunk -> uses embed() not contextualized_embed(), returns voyage-4 model."""
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.embeddings = [[0.5, 0.6]]
    mock_client.embed.return_value = mock_result

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, actual_model = _embed_with_fallback(
            chunks=["only chunk"],
            model="voyage-context-3",
            api_key="vk_test",
        )

    mock_client.contextualized_embed.assert_not_called()
    mock_client.embed.assert_called_once()
    assert embeddings == [[0.5, 0.6]]
    assert actual_model == "voyage-4"


def test_embed_with_fallback_falls_back_on_error(monkeypatch):
    """contextualized_embed raises Exception -> falls back to embed(), returns voyage-4 model."""
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    mock_client.contextualized_embed.side_effect = RuntimeError("API error")
    mock_result = MagicMock()
    mock_result.embeddings = [[0.1, 0.2], [0.3, 0.4]]
    mock_client.embed.return_value = mock_result

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, actual_model = _embed_with_fallback(
            chunks=["chunk one", "chunk two"],
            model="voyage-context-3",
            api_key="vk_test",
        )

    mock_client.contextualized_embed.assert_called_once()
    mock_client.embed.assert_called_once()
    assert embeddings == [[0.1, 0.2], [0.3, 0.4]]
    # Critical: fallback must report voyage-4 so callers store the correct model in metadata
    assert actual_model == "voyage-4"


def test_embed_with_fallback_skips_cce_for_large_input(monkeypatch):
    """estimated_tokens > 100_000 -> falls back to standard embed, skips CCE."""
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    # 4 chars per token * 100_001 tokens = 400_004 chars per chunk
    big_chunk = "x" * 400_004

    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.embeddings = [[0.1, 0.2]]
    mock_client.embed.return_value = mock_result

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, actual_model = _embed_with_fallback(
            chunks=[big_chunk, "second"],
            model="voyage-context-3",
            api_key="vk_test",
        )

    mock_client.contextualized_embed.assert_not_called()
    mock_client.embed.assert_called_once()
    assert actual_model == "voyage-4"


def test_embed_with_fallback_metadata_reflects_actual_model(monkeypatch):
    """When CCE fails and falls back to voyage-4, the returned model is voyage-4.

    This is the companion test to Critical Issue C1: callers must use the returned
    model name (not the requested target_model) when writing embedding_model metadata.
    If this were incorrect, the staleness check would permanently skip re-indexing
    even after the CCE error is resolved.
    """
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    mock_client.contextualized_embed.side_effect = Exception("network error")
    fallback_result = MagicMock()
    fallback_result.embeddings = [[0.1, 0.2], [0.3, 0.4]]
    mock_client.embed.return_value = fallback_result

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, actual_model = _embed_with_fallback(
            chunks=["a", "b"],
            model="voyage-context-3",
            api_key="vk_test",
        )

    # Embeddings come from standard path
    assert embeddings == [[0.1, 0.2], [0.3, 0.4]]
    # Model MUST reflect what was actually used — not the requested voyage-context-3
    assert actual_model == "voyage-4", (
        "Fallback must return 'voyage-4' so callers record the correct model in metadata"
    )


# ── nexus-370: index_pdf CCE integration ─────────────────────────────────────

def _make_pdf_mocks():
    """Helper: return (mock_chunk, mock_extractor_result) for PDF tests."""
    mock_chunk = MagicMock()
    mock_chunk.text = "chunk text content"
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 18, "page_number": 1}

    mock_extract_result = MagicMock()
    mock_extract_result.text = "extracted text"
    mock_extract_result.metadata = {
        "extraction_method": "pymupdf4llm_markdown",
        "page_count": 1,
        "format": "markdown",
        "page_boundaries": [],
    }
    return mock_chunk, mock_extract_result


def test_index_pdf_uses_cce_for_docs_collection(sample_pdf, monkeypatch):
    """For docs__ collection, index_pdf calls t3.upsert_chunks_with_embeddings."""
    _set_credentials(monkeypatch)

    mock_chunk, mock_extract_result = _make_pdf_mocks()

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    mock_voyage_client = MagicMock()
    mock_voyage_result = MagicMock()
    mock_voyage_result.embeddings = [[[0.1, 0.2]]]
    mock_voyage_client.contextualized_embed.return_value = mock_voyage_result

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_extractor_class:
            with patch("nexus.doc_indexer.PDFChunker") as mock_chunker_class:
                with patch("voyageai.Client", return_value=mock_voyage_client):
                    mock_extractor_class.return_value.extract.return_value = mock_extract_result
                    mock_chunker_class.return_value.chunk.return_value = [mock_chunk, mock_chunk]

                    result = index_pdf(sample_pdf, corpus="mybook")

    assert result == 2
    mock_t3.upsert_chunks_with_embeddings.assert_called_once()
    mock_col.upsert.assert_not_called()



def test_index_pdf_rerenders_when_model_changes(sample_pdf, monkeypatch):
    """Re-indexes when embedding_model in store differs from target model."""
    _set_credentials(monkeypatch)
    import hashlib as _hashlib
    content_hash = _hashlib.sha256(sample_pdf.read_bytes()).hexdigest()

    mock_chunk, mock_extract_result = _make_pdf_mocks()

    mock_col = MagicMock()
    # Existing entry: same hash but old model
    mock_col.get.side_effect = [
        # First call: staleness check
        {"ids": ["old_id"], "metadatas": [{"content_hash": content_hash, "embedding_model": "voyage-4"}]},
        # Second call: prune stale check
        {"ids": ["old_id"]},
    ]

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    mock_voyage_client = MagicMock()
    mock_voyage_result = MagicMock()
    mock_voyage_result.embeddings = [[[0.1, 0.2]]]
    mock_voyage_client.contextualized_embed.return_value = mock_voyage_result

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_extractor_class:
            with patch("nexus.doc_indexer.PDFChunker") as mock_chunker_class:
                with patch("voyageai.Client", return_value=mock_voyage_client):
                    mock_extractor_class.return_value.extract.return_value = mock_extract_result
                    # Two chunks so CCE fires (needs >= 2)
                    mock_chunker_class.return_value.chunk.return_value = [mock_chunk, mock_chunk]

                    # Target model is voyage-context-3 (docs__ collection)
                    result = index_pdf(sample_pdf, corpus="mybook")

    # Should NOT be skipped — model changed
    assert result == 2


def test_index_pdf_skips_when_hash_and_model_match(sample_pdf, monkeypatch):
    """Skips re-indexing when both content_hash and embedding_model match target."""
    _set_credentials(monkeypatch)
    import hashlib as _hashlib
    content_hash = _hashlib.sha256(sample_pdf.read_bytes()).hexdigest()

    mock_col = MagicMock()
    # Same hash AND same model as target (voyage-context-3 for docs__)
    mock_col.get.return_value = {
        "ids": ["existing_id"],
        "metadatas": [{"content_hash": content_hash, "embedding_model": "voyage-context-3"}],
    }

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_extractor_class:
            result = index_pdf(sample_pdf, corpus="mybook")

    assert result == 0
    mock_extractor_class.assert_not_called()


# ── nexus-370: batch indexing ─────────────────────────────────────────────────

def test_batch_index_pdfs_returns_status_dict(tmp_path, monkeypatch):
    """batch_index_pdfs returns a dict mapping path -> status string."""
    from nexus.doc_indexer import batch_index_pdfs

    pdf1 = tmp_path / "a.pdf"
    pdf1.write_bytes(b"fake pdf 1")
    pdf2 = tmp_path / "b.pdf"
    pdf2.write_bytes(b"fake pdf 2")

    mock_t3 = MagicMock()

    with patch("nexus.doc_indexer.index_pdf", return_value=3) as mock_index:
        result = batch_index_pdfs([pdf1, pdf2], corpus="test", t3=mock_t3)

    assert result[str(pdf1)] == "indexed"
    assert result[str(pdf2)] == "indexed"
    assert mock_index.call_count == 2


def test_batch_index_markdowns_returns_status_dict(tmp_path, monkeypatch):
    """batch_index_markdowns returns a dict mapping path -> status string."""
    from nexus.doc_indexer import batch_index_markdowns

    md1 = tmp_path / "a.md"
    md1.write_text("# A\n\nContent.")
    md2 = tmp_path / "b.md"
    md2.write_text("# B\n\nContent.")

    mock_t3 = MagicMock()

    with patch("nexus.doc_indexer.index_markdown", return_value=2) as mock_index:
        result = batch_index_markdowns([md1, md2], corpus="test", t3=mock_t3)

    assert result[str(md1)] == "indexed"
    assert result[str(md2)] == "indexed"
    assert mock_index.call_count == 2


def test_batch_index_pdfs_marks_failed_on_error(tmp_path, monkeypatch):
    """If index_pdf raises, that path is marked 'failed' and others continue."""
    from nexus.doc_indexer import batch_index_pdfs

    pdf1 = tmp_path / "ok.pdf"
    pdf1.write_bytes(b"good pdf")
    pdf2 = tmp_path / "bad.pdf"
    pdf2.write_bytes(b"bad pdf")

    mock_t3 = MagicMock()

    def _side_effect(path, corpus, t3=None):
        if "bad" in str(path):
            raise RuntimeError("extraction failed")
        return 2

    with patch("nexus.doc_indexer.index_pdf", side_effect=_side_effect):
        result = batch_index_pdfs([pdf1, pdf2], corpus="test", t3=mock_t3)

    assert result[str(pdf1)] == "indexed"
    assert result[str(pdf2)] == "failed"
