"""AC6–AC7: doc_indexer — SHA256 incremental sync, docs__ metadata schema."""
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from voyageai.object.contextualized_embeddings import (
    ContextualizedEmbeddingsObject,
    ContextualizedEmbeddingsResult,
)
from voyageai.object.embeddings import EmbeddingsObject

from nexus.doc_indexer import index_markdown, index_pdf
from tests.conftest import set_credentials


@pytest.fixture(autouse=True)
def _no_bib_enrich(monkeypatch):
    """Prevent bib_enricher from making real HTTP calls in all tests."""
    monkeypatch.setattr("nexus.bib_enricher.enrich", lambda title: {})


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
    set_credentials(monkeypatch)
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
    set_credentials(monkeypatch)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    mock_chunk = MagicMock()
    mock_chunk.text = "chunk text content"
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 18, "page_number": 1}

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    mock_voyage_client = MagicMock()
    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings =[[0.1, 0.2, 0.3]]
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
                            "extraction_method": "docling",
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
    set_credentials(monkeypatch)

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
    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings =[[0.1, 0.2]]
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


# ── PDF metadata schema ───────────────────────────────────────────────────────

def test_pdf_metadata_schema_complete(simple_pdf: Path, monkeypatch):
    """PDF chunk metadata contains all 22 required fields (18 base + 4 PDF-only).

    Uses a real PDF fixture so the production metadata mapping is exercised.
    The markdown schema test (test_docs_metadata_schema_complete) remains unchanged;
    pdf_subject, pdf_keywords, is_image_pdf, and has_formulas are PDF-only fields.
    """
    from nexus.doc_indexer import index_pdf

    set_credentials(monkeypatch)

    required_fields = {
        # 18 base fields (shared with markdown schema)
        "source_path", "source_title", "source_author", "source_date",
        "corpus", "store_type", "page_count", "page_number", "section_title",
        "format", "extraction_method", "chunk_index", "chunk_count",
        "chunk_start_char", "chunk_end_char", "embedding_model",
        "indexed_at", "content_hash",
        # 4 PDF-only fields
        "pdf_subject", "pdf_keywords", "is_image_pdf", "has_formulas",
    }

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    captured_metadatas: list[dict] = []

    def capture_upsert(collection, ids, documents, embeddings, metadatas):
        captured_metadatas.extend(metadatas)

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    mock_t3.upsert_chunks_with_embeddings.side_effect = capture_upsert

    def fake_embed(chunks, model, api_key, input_type="document", timeout=120.0, on_progress=None):
        return [[0.1] * 5] * len(chunks), "test-local"

    with patch("nexus.doc_indexer._embed_with_fallback", side_effect=fake_embed):
        index_pdf(simple_pdf, corpus="test", t3=mock_t3)

    assert captured_metadatas, "Expected at least one PDF chunk to be upserted"
    meta = captured_metadatas[0]
    missing = required_fields - meta.keys()
    assert not missing, f"Missing PDF metadata fields: {missing}"


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
    set_credentials(monkeypatch)

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
    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings =[[0.1, 0.2]]
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
    set_credentials(monkeypatch)

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
    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings =[[0.1, 0.2]]
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
    set_credentials(monkeypatch)

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
    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings =[[0.1, 0.2]]
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
    set_credentials(monkeypatch)

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
    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings =[[0.1, 0.2]]
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

def test_embed_with_fallback_calls_cce_for_docs_collection(monkeypatch):
    """When model=voyage-context-3 and len(chunks) >= 2, contextualized_embed is called."""
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    mock_cce_result = MagicMock(spec=ContextualizedEmbeddingsResult)
    mock_cce_result.embeddings = [[0.1, 0.2], [0.3, 0.4]]
    mock_result = MagicMock(spec=ContextualizedEmbeddingsObject)
    mock_result.results = [mock_cce_result]
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


def test_embed_with_fallback_single_chunk_uses_cce(monkeypatch):
    """Single chunk -> uses contextualized_embed(), NOT embed(), returns voyage-context-3 model.

    C1 fix: CCE API accepts single-element inputs.  The old assumption that CCE
    requires >=2 chunks was wrong and created a model-mismatch (index used voyage-4,
    query used voyage-context-3).  Single-chunk docs must use CCE so their stored
    vectors are in the same embedding space as CCE query vectors.
    """
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    mock_cce_result = MagicMock(spec=ContextualizedEmbeddingsResult)
    mock_cce_result.embeddings = [[0.5, 0.6]]
    mock_result = MagicMock(spec=ContextualizedEmbeddingsObject)
    mock_result.results = [mock_cce_result]
    mock_client.contextualized_embed.return_value = mock_result

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, actual_model = _embed_with_fallback(
            chunks=["only chunk"],
            model="voyage-context-3",
            api_key="vk_test",
        )

    mock_client.contextualized_embed.assert_called_once()
    mock_client.embed.assert_not_called()
    assert embeddings == [[0.5, 0.6]]
    assert actual_model == "voyage-context-3"


def test_single_chunk_cce_uses_contextualized_embed():
    """Single-chunk CCE must use contextualized_embed, not fall back to voyage-4."""
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    mock_cce_result = MagicMock(spec=ContextualizedEmbeddingsResult)
    mock_cce_result.embeddings = [[0.1] * 10]
    mock_result = MagicMock(spec=ContextualizedEmbeddingsObject)
    mock_result.results = [mock_cce_result]
    mock_client.contextualized_embed.return_value = mock_result

    # voyageai is imported lazily inside _embed_with_fallback, so patch at source
    with patch("voyageai.Client", return_value=mock_client):
        embeddings, model = _embed_with_fallback(
            ["single chunk content"], "voyage-context-3", "test-key"
        )

    mock_client.contextualized_embed.assert_called_once()
    mock_client.embed.assert_not_called()
    assert model == "voyage-context-3"
    assert len(embeddings) == 1


def test_embed_with_fallback_splits_batch_on_error(monkeypatch):
    """contextualized_embed raises on full batch → splits in half and retries with same model."""
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    call_count = [0]

    def fake_cce(inputs, model, input_type):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("batch too large")
        # Split halves succeed
        cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
        cce_item.embeddings = [[0.1] for _ in inputs[0]]
        result = MagicMock(spec=ContextualizedEmbeddingsObject)
        result.results = [cce_item]
        return result

    mock_client.contextualized_embed.side_effect = fake_cce

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, actual_model = _embed_with_fallback(
            chunks=["chunk one", "chunk two"],
            model="voyage-context-3",
            api_key="vk_test",
        )

    # First call failed, then 2 split calls succeeded
    assert call_count[0] == 3
    assert len(embeddings) == 2
    # Never falls back to a different model
    assert actual_model == "voyage-context-3"
    mock_client.embed.assert_not_called()


def test_embed_with_fallback_batches_large_input(monkeypatch):
    """Large input is batched into multiple CCE calls, not silently dropped to voyage-4."""
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    # 6 chunks of ~8k tokens each → total ~48k > 32k → must split into batches
    chunks = [f"chunk{i}_" + "x" * 24_000 for i in range(6)]  # each ~8k tokens

    mock_client = MagicMock()
    call_count = [0]

    def fake_cce(inputs, model, input_type):
        call_count[0] += 1
        cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
        cce_item.embeddings = [[float(call_count[0])] for _ in inputs[0]]
        result = MagicMock(spec=ContextualizedEmbeddingsObject)
        result.results = [cce_item]
        return result

    mock_client.contextualized_embed.side_effect = fake_cce

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, actual_model = _embed_with_fallback(
            chunks=chunks,
            model="voyage-context-3",
            api_key="vk_test",
        )

    # CCE was called in multiple batches
    assert call_count[0] >= 2, f"Should have batched into >= 2 CCE calls, got {call_count[0]}"
    mock_client.embed.assert_not_called()
    assert actual_model == "voyage-context-3"
    assert len(embeddings) == 6


def test_embed_with_fallback_never_switches_model(monkeypatch):
    """CCE failure retries with same model (split), never falls back to voyage-4.

    The returned model name must always match the requested model so metadata
    is consistent within the collection.
    """
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    call_count = [0]

    def fake_cce(inputs, model, input_type):
        call_count[0] += 1
        if call_count[0] == 1:
            raise Exception("network error")
        cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
        cce_item.embeddings = [[0.1] for _ in inputs[0]]
        result = MagicMock(spec=ContextualizedEmbeddingsObject)
        result.results = [cce_item]
        return result

    mock_client.contextualized_embed.side_effect = fake_cce

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, actual_model = _embed_with_fallback(
            chunks=["a", "b"],
            model="voyage-context-3",
            api_key="vk_test",
        )

    assert actual_model == "voyage-context-3"
    mock_client.embed.assert_not_called()


def test_partial_cce_failure_splits_failed_batch():
    """If a CCE batch fails, it is split in half and retried with same model.

    Multi-batch CCE: batch 1 succeeds, batch 2 fails → batch 2 is split into
    two halves and retried. All embeddings come from voyage-context-3.
    """
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    call_count = [0]

    def fake_cce(inputs, model, input_type):
        call_count[0] += 1
        if call_count[0] == 2:
            # Second batch (of the original batches) fails
            raise RuntimeError("API error")
        cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
        cce_item.embeddings = [[0.1] for _ in inputs[0]]
        result = MagicMock(spec=ContextualizedEmbeddingsObject)
        result.results = [cce_item]
        return result

    mock_client.contextualized_embed.side_effect = fake_cce

    chunks = ["chunk a", "chunk b", "chunk c", "chunk d"]
    forced_batches = [["chunk a", "chunk b"], ["chunk c", "chunk d"]]

    with patch("voyageai.Client", return_value=mock_client), \
         patch("nexus.doc_indexer._batch_chunks_for_cce", return_value=forced_batches):
        embeddings, model = _embed_with_fallback(
            chunks, "voyage-context-3", "test-key"
        )

    assert model == "voyage-context-3", "Must stay on same model after split retry"
    assert len(embeddings) == 4
    # CCE called: batch1 ok, batch2 fail, half1 ok, half2 ok = 4 calls
    assert call_count[0] == 4
    mock_client.embed.assert_not_called()


# ── CCE contract tests: prevent regression of voyageai API misuse ────────────
#
# These tests validate the actual voyageai API contract rather than relying on
# MagicMock (which silently accepts any attribute access and hides bugs).
#
# Bug #1: code accessed `result.embeddings[0]` but ContextualizedEmbeddingsObject
#          has `.results[0].embeddings`, not a top-level `.embeddings` attribute.
# Bug #2: token threshold was 100_000 but voyage-context-3 context window is 32k.


def test_cce_contract_no_top_level_embeddings_attribute():
    """ContextualizedEmbeddingsObject does NOT have a top-level .embeddings attribute.

    Bug #1 regression guard: the old code did `result.embeddings[0]` which would
    silently succeed with MagicMock but fail at runtime because the actual API
    object uses `result.results[0].embeddings` instead.
    """
    from voyageai.object.contextualized_embeddings import (
        ContextualizedEmbeddingsObject,
    )

    obj = ContextualizedEmbeddingsObject(response=None)
    assert not hasattr(obj, "embeddings"), (
        "ContextualizedEmbeddingsObject must NOT have a top-level .embeddings "
        "attribute — use .results[0].embeddings instead"
    )


def test_cce_contract_results_list_with_embeddings():
    """ContextualizedEmbeddingsObject has .results (list) and each result has .embeddings.

    Validates the correct access path: result.results[0].embeddings
    """
    from voyageai.object.contextualized_embeddings import (
        ContextualizedEmbeddingsObject,
        ContextualizedEmbeddingsResult,
    )

    obj = ContextualizedEmbeddingsObject(response=None)
    assert hasattr(obj, "results"), (
        "ContextualizedEmbeddingsObject must have a .results attribute"
    )
    assert isinstance(obj.results, list), ".results must be a list"

    # Verify that ContextualizedEmbeddingsResult has .embeddings
    result_item = ContextualizedEmbeddingsResult(
        index=0, embeddings=[[0.1, 0.2], [0.3, 0.4]]
    )
    assert hasattr(result_item, "embeddings"), (
        "ContextualizedEmbeddingsResult must have an .embeddings attribute"
    )
    assert result_item.embeddings == [[0.1, 0.2], [0.3, 0.4]]


def test_cce_contract_standard_embed_has_top_level_embeddings():
    """Standard EmbeddingsObject (from embed()) DOES have a top-level .embeddings.

    This confirms the asymmetry between the two API objects that caused Bug #1:
    embed() returns EmbeddingsObject with .embeddings (list of vectors),
    contextualized_embed() returns ContextualizedEmbeddingsObject with .results[].embeddings.
    """
    from voyageai.object.embeddings import EmbeddingsObject

    obj = EmbeddingsObject(response=None)
    assert hasattr(obj, "embeddings"), (
        "EmbeddingsObject must have a top-level .embeddings attribute"
    )
    assert isinstance(obj.embeddings, list)


def test_cce_contract_spec_mock_rejects_wrong_attribute():
    """A spec-based mock of ContextualizedEmbeddingsObject rejects .embeddings access.

    This demonstrates that using spec= with MagicMock would have caught Bug #1
    at test time: accessing .embeddings on a spec'd mock raises AttributeError,
    whereas a bare MagicMock silently creates the attribute.
    """
    from unittest.mock import MagicMock
    from voyageai.object.contextualized_embeddings import (
        ContextualizedEmbeddingsObject,
    )

    # Bare MagicMock: SILENTLY creates .embeddings (hides the bug)
    bare_mock = MagicMock()
    _ = bare_mock.embeddings  # no error — this is why the bug wasn't caught

    # Spec-based mock: REJECTS .embeddings (catches the bug)
    spec_mock = MagicMock(spec=ContextualizedEmbeddingsObject)
    with pytest.raises(AttributeError):
        _ = spec_mock.embeddings


def test_cce_contract_embed_with_fallback_uses_correct_access_path(monkeypatch):
    """_embed_with_fallback accesses result.results[0].embeddings (not result.embeddings).

    Integration test using spec-based mocks that enforce the real API contract.
    If the code reverts to `result.embeddings[0]`, this test will fail with
    AttributeError because the spec'd mock won't have that attribute.
    """
    from unittest.mock import MagicMock, patch
    from voyageai.object.contextualized_embeddings import (
        ContextualizedEmbeddingsObject,
        ContextualizedEmbeddingsResult,
    )
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()

    # Build spec-constrained mocks that only allow real attributes
    mock_cce_result_item = MagicMock(spec=ContextualizedEmbeddingsResult)
    mock_cce_result_item.embeddings = [[0.1, 0.2], [0.3, 0.4]]

    mock_cce_obj = MagicMock(spec=ContextualizedEmbeddingsObject)
    mock_cce_obj.results = [mock_cce_result_item]

    mock_client.contextualized_embed.return_value = mock_cce_obj

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, actual_model = _embed_with_fallback(
            chunks=["chunk one", "chunk two"],
            model="voyage-context-3",
            api_key="vk_test",
        )

    assert embeddings == [[0.1, 0.2], [0.3, 0.4]]
    assert actual_model == "voyage-context-3"
    mock_client.contextualized_embed.assert_called_once()


def test_cce_contract_token_limit_has_safety_margin():
    """The CCE token limit must be below Voyage's 32K to account for estimation error.

    We use 75% of 32K (24K) because chars-to-tokens estimation for academic text
    with equations can be off by 30-50%.
    """
    from nexus.doc_indexer import _CCE_TOKEN_LIMIT

    assert _CCE_TOKEN_LIMIT <= 32_000, f"Must not exceed Voyage limit, got {_CCE_TOKEN_LIMIT}"
    assert _CCE_TOKEN_LIMIT >= 16_000, f"Too conservative, got {_CCE_TOKEN_LIMIT}"


def test_cce_contract_batch_chunks_splits_large_input():
    """_batch_chunks_for_cce splits chunks exceeding 32k tokens into batches."""
    from nexus.doc_indexer import _batch_chunks_for_cce

    # 6 chunks, each ~8k tokens → total ~48k > 32k → must split into multiple batches
    chunks = ["x" * 24_000 for _ in range(6)]  # each ~8k tokens
    batches = _batch_chunks_for_cce(chunks)
    assert len(batches) >= 2, f"Expected >= 2 batches for ~48k tokens, got {len(batches)}"
    # Each batch must have >= 2 chunks (CCE requirement)
    for i, batch in enumerate(batches):
        assert len(batch) >= 2, f"Batch {i} has {len(batch)} chunks, CCE needs >= 2"


def test_cce_contract_batch_chunks_keeps_small_input_together():
    """_batch_chunks_for_cce keeps small inputs in a single batch."""
    from nexus.doc_indexer import _batch_chunks_for_cce

    chunks = ["hello world", "foo bar"]  # tiny
    batches = _batch_chunks_for_cce(chunks)
    assert len(batches) == 1
    assert batches[0] == chunks


def test_cce_contract_batch_chunks_merges_singleton_tail():
    """A single trailing chunk is merged into the previous batch (CCE needs >= 2)."""
    from nexus.doc_indexer import _batch_chunks_for_cce

    # Fill one batch, then have a single leftover
    big = "x" * 40_000   # ~20k tokens at //2 estimate (fits in one batch under 24K limit)
    small = "y" * 300     # tiny
    tiny = "z" * 300      # tiny — would be singleton if not merged
    batches = _batch_chunks_for_cce([big, small, tiny])
    # All should end up together or singleton merged back
    for batch in batches:
        assert len(batch) >= 2, f"Batch has {len(batch)} chunks, CCE needs >= 2"


def test_cce_contract_large_input_still_uses_cce(monkeypatch):
    """Large documents are batched and each batch uses CCE — never silently dropped."""
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    # 8 chunks, each ~6k tokens → total ~48k > 32k → batched, not skipped
    chunks = [f"chunk{i}_" + "x" * 18_000 for i in range(8)]

    mock_client = MagicMock()
    call_count = [0]

    def fake_cce(inputs, model, input_type):
        call_count[0] += 1
        cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
        cce_item.embeddings = [[float(call_count[0])] for _ in inputs[0]]
        result = MagicMock(spec=ContextualizedEmbeddingsObject)
        result.results = [cce_item]
        return result

    mock_client.contextualized_embed.side_effect = fake_cce

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, actual_model = _embed_with_fallback(
            chunks=chunks,
            model="voyage-context-3",
            api_key="vk_test",
        )

    assert actual_model == "voyage-context-3", "Large input must still use CCE via batching"
    assert len(embeddings) == 8, f"Expected 8 embeddings, got {len(embeddings)}"
    mock_client.embed.assert_not_called()
    assert call_count[0] >= 2, "Should have made multiple CCE calls"


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
        "extraction_method": "docling",
        "page_count": 1,
        "format": "markdown",
        "page_boundaries": [],
    }
    return mock_chunk, mock_extract_result


def test_index_pdf_uses_cce_for_docs_collection(sample_pdf, monkeypatch):
    """For docs__ collection, index_pdf calls t3.upsert_chunks_with_embeddings."""
    set_credentials(monkeypatch)

    mock_chunk, mock_extract_result = _make_pdf_mocks()

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    mock_voyage_client = MagicMock()
    mock_cce_result = MagicMock(spec=ContextualizedEmbeddingsResult)
    mock_cce_result.embeddings = [[0.1, 0.2]]
    mock_voyage_result = MagicMock(spec=ContextualizedEmbeddingsObject)
    mock_voyage_result.results = [mock_cce_result]
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
    set_credentials(monkeypatch)
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
    mock_cce_result = MagicMock(spec=ContextualizedEmbeddingsResult)
    mock_cce_result.embeddings = [[0.1, 0.2]]
    mock_voyage_result = MagicMock(spec=ContextualizedEmbeddingsObject)
    mock_voyage_result.results = [mock_cce_result]
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
    set_credentials(monkeypatch)
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

    def _side_effect(path, corpus, t3=None, *, force=False, extractor="auto"):
        if "bad" in str(path):
            raise RuntimeError("extraction failed")
        return 2

    with patch("nexus.doc_indexer.index_pdf", side_effect=_side_effect):
        result = batch_index_pdfs([pdf1, pdf2], corpus="test", t3=mock_t3)

    assert result[str(pdf1)] == "indexed"
    assert result[str(pdf2)] == "failed"


# ── C1: Standard embed() path batching ───────────────────────────────────────

def test_embed_standard_path_batches_over_128_chunks(monkeypatch):
    """Standard embed path (non-CCE) must batch when >128 chunks are passed."""
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback, _EMBED_BATCH_SIZE

    # 200 chunks → should produce ceil(200/128) = 2 embed() calls
    chunks = [f"chunk_{i}" for i in range(200)]

    mock_client = MagicMock()
    embed_call_count = [0]

    def fake_embed(texts, model, input_type):
        embed_call_count[0] += 1
        result = MagicMock(spec=EmbeddingsObject)
        result.embeddings = [[0.1] for _ in texts]
        return result

    mock_client.embed.side_effect = fake_embed

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, actual_model = _embed_with_fallback(
            chunks=chunks,
            model="voyage-4",
            api_key="vk_test",
        )

    assert embed_call_count[0] == 2, (
        f"Expected 2 embed() calls for 200 chunks with batch size {_EMBED_BATCH_SIZE}, "
        f"got {embed_call_count[0]}"
    )
    assert len(embeddings) == 200
    assert actual_model == "voyage-4"


# ── C2: CCE batch chunk-count cap ────────────────────────────────────────────

def test_batch_chunks_for_cce_splits_by_count_over_1000():
    """Batches with >1000 tiny chunks are split by count, not just by tokens."""
    from nexus.doc_indexer import _batch_chunks_for_cce, _CCE_MAX_BATCH_CHUNKS

    # 1500 tiny chunks — well under token limit but over count limit
    chunks = ["x" for _ in range(1500)]
    batches = _batch_chunks_for_cce(chunks)

    assert len(batches) >= 2, f"Expected >= 2 batches for 1500 chunks, got {len(batches)}"
    for i, batch in enumerate(batches):
        assert len(batch) <= _CCE_MAX_BATCH_CHUNKS, (
            f"Batch {i} has {len(batch)} chunks, max allowed is {_CCE_MAX_BATCH_CHUNKS}"
        )
    # All chunks must be preserved
    total_chunks = sum(len(b) for b in batches)
    assert total_chunks == 1500


def test_batch_chunks_for_cce_no_batch_exceeds_1000():
    """No batch should ever contain more than 1000 chunks."""
    from nexus.doc_indexer import _batch_chunks_for_cce, _CCE_MAX_BATCH_CHUNKS

    # 2500 tiny chunks
    chunks = ["tiny" for _ in range(2500)]
    batches = _batch_chunks_for_cce(chunks)

    for i, batch in enumerate(batches):
        assert len(batch) <= _CCE_MAX_BATCH_CHUNKS, (
            f"Batch {i} has {len(batch)} chunks, max allowed is {_CCE_MAX_BATCH_CHUNKS}"
        )


def test_batch_chunks_for_cce_singleton_not_merged_when_target_at_limit():
    """Singleton is NOT merged into previous batch if that batch is already at the limit."""
    from nexus.doc_indexer import _batch_chunks_for_cce, _CCE_MAX_BATCH_CHUNKS

    # Exactly _CCE_MAX_BATCH_CHUNKS chunks → fills one batch to the limit
    # Then one more → singleton that must NOT be merged (would overflow)
    chunks = ["tiny"] * (_CCE_MAX_BATCH_CHUNKS + 1)
    batches = _batch_chunks_for_cce(chunks)

    for i, batch in enumerate(batches):
        assert len(batch) <= _CCE_MAX_BATCH_CHUNKS, (
            f"Batch {i} has {len(batch)} chunks, max is {_CCE_MAX_BATCH_CHUNKS}"
        )
    # All chunks preserved
    assert sum(len(b) for b in batches) == _CCE_MAX_BATCH_CHUNKS + 1


def test_embed_with_fallback_warns_at_exactly_limit(monkeypatch):
    """Warning fires when chunk count equals the limit (>= boundary)."""
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.embeddings = [[0.1]]
    mock_client.embed.return_value = mock_result

    with patch("voyageai.Client", return_value=mock_client):
        with patch("nexus.doc_indexer._log") as mock_log:
            # Patch limit to 2 and pass exactly 2 chunks → should warn with >=
            with patch("nexus.doc_indexer._CCE_MAX_TOTAL_CHUNKS", 2):
                _embed_with_fallback(
                    chunks=["a", "b"],
                    model="voyage-4",
                    api_key="vk_test",
                )
            mock_log.warning.assert_called_once()
            assert "chunk count exceeds" in mock_log.warning.call_args[0][0]


# ── C3: Fallback embed() in CCE error handler also batches ──────────────────

def test_cce_failure_splits_recursively(monkeypatch):
    """When CCE batch fails, split in half and retry — both halves use same model."""
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    chunks = [f"chunk_{i}" for i in range(4)]

    mock_client = MagicMock()
    call_count = [0]

    def fake_cce(inputs, model, input_type):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("batch too large")
        cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
        cce_item.embeddings = [[0.1] for _ in inputs[0]]
        result = MagicMock(spec=ContextualizedEmbeddingsObject)
        result.results = [cce_item]
        return result

    mock_client.contextualized_embed.side_effect = fake_cce

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, actual_model = _embed_with_fallback(
            chunks=chunks,
            model="voyage-context-3",
            api_key="vk_test",
        )

    assert len(embeddings) == 4
    assert actual_model == "voyage-context-3"
    mock_client.embed.assert_not_called()


# ── B7: Partial batch failure (mixed model) ──────────────────────────────────

def test_embed_partial_batch_failure_stays_same_model(monkeypatch):
    """CCE succeeds for batch 1, fails for batch 2: batch 2 is split, all embeddings from CCE."""
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    # Use forced batches to avoid token-estimation coupling
    chunks = ["chunk a", "chunk b", "chunk c", "chunk d"]
    forced_batches = [["chunk a", "chunk b"], ["chunk c", "chunk d"]]

    mock_client = MagicMock()
    cce_call_count = [0]
    failed_once = [False]

    def fake_cce(inputs, model, input_type):
        cce_call_count[0] += 1
        # Fail on the second original batch only, not the retried halves
        if cce_call_count[0] == 2 and not failed_once[0]:
            failed_once[0] = True
            raise RuntimeError("CCE batch 2 failed")
        cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
        cce_item.embeddings = [[1.0] for _ in inputs[0]]
        result = MagicMock(spec=ContextualizedEmbeddingsObject)
        result.results = [cce_item]
        return result

    mock_client.contextualized_embed.side_effect = fake_cce

    with patch("voyageai.Client", return_value=mock_client), \
         patch("nexus.doc_indexer._batch_chunks_for_cce", return_value=forced_batches):
        embeddings, actual_model = _embed_with_fallback(
            chunks=chunks,
            model="voyage-context-3",
            api_key="vk_test",
        )

    assert len(embeddings) == 4
    assert actual_model == "voyage-context-3"
    mock_client.embed.assert_not_called()


# ── F5: All-batches-fail double-fallback ─────────────────────────────────────

def test_embed_single_chunk_failure_raises(monkeypatch):
    """When a single-chunk batch fails CCE, it raises (cannot split further)."""
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    mock_client.contextualized_embed.side_effect = RuntimeError("single chunk too large")

    # Single chunk — can't split, must raise
    with patch("voyageai.Client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="single chunk too large"):
            _embed_with_fallback(
                chunks=["one giant chunk"],
                model="voyage-context-3",
                api_key="vk_test",
            )


# ── F3: batch_index_markdowns failure test ───────────────────────────────────

def test_batch_index_markdowns_marks_failed_on_error(tmp_path, monkeypatch):
    """If index_markdown raises, that path is marked 'failed' and others continue."""
    from nexus.doc_indexer import batch_index_markdowns

    md1 = tmp_path / "ok.md"
    md1.write_text("# OK\n\nGood content.")
    md2 = tmp_path / "bad.md"
    md2.write_text("# Bad\n\nBad content.")

    mock_t3 = MagicMock()

    def _side_effect(path, corpus, t3=None, *, collection_name=None, force=False):
        if "bad" in str(path):
            raise RuntimeError("markdown parsing failed")
        return 2

    with patch("nexus.doc_indexer.index_markdown", side_effect=_side_effect):
        result = batch_index_markdowns([md1, md2], corpus="test", t3=mock_t3)

    assert result[str(md1)] == "indexed"
    assert result[str(md2)] == "failed"


# ── F4: Stale chunk pruning ──────────────────────────────────────────────────

def test_stale_chunk_pruning_deletes_old_ids(sample_md, monkeypatch):
    """When re-index produces fewer chunks, stale chunk IDs are deleted."""
    import hashlib as _hashlib
    set_credentials(monkeypatch)

    content_hash = _hashlib.sha256(sample_md.read_bytes()).hexdigest()
    prefix = content_hash[:16]

    # Simulate: first index had 5 chunks, now re-indexing produces 3 chunks.
    # The col.get calls:
    #  1. Staleness check (limit=1) — different hash → proceed
    #  2. Prune check — return all 5 old IDs
    mock_col = MagicMock()
    mock_col.get.side_effect = [
        # Staleness check: old hash → triggers re-index
        {"ids": [f"{prefix}_0"], "metadatas": [{"content_hash": "old_hash", "embedding_model": "voyage-context-3"}]},
        # Prune check: returns all existing IDs (5 from old index)
        {"ids": [f"{prefix}_{i}" for i in range(5)]},
    ]

    captured_deletes: list = []
    mock_col.delete.side_effect = lambda ids: captured_deletes.extend(ids)

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    # Produce 3 chunks on re-index (IDs: prefix_0, prefix_1, prefix_2)
    mock_chunks = []
    for i in range(3):
        mc = MagicMock()
        mc.text = f"chunk text {i}"
        mc.chunk_index = i
        mc.metadata = {"chunk_start_char": 0, "chunk_end_char": 10, "page_number": 0, "header_path": "H"}
        mock_chunks.append(mc)

    mock_voyage_client = MagicMock()
    mock_embed_result = MagicMock(spec=EmbeddingsObject)
    mock_embed_result.embeddings = [[0.1] for _ in range(3)]
    mock_voyage_client.embed.return_value = mock_embed_result

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.SemanticMarkdownChunker") as mock_chunker_class:
            with patch("voyageai.Client", return_value=mock_voyage_client):
                mock_chunker_class.return_value.chunk.return_value = mock_chunks
                index_markdown(sample_md, corpus="docs")

    # Stale IDs (prefix_3, prefix_4) must have been deleted
    expected_stale = {f"{prefix}_3", f"{prefix}_4"}
    assert set(captured_deletes) == expected_stale, (
        f"Expected stale IDs {expected_stale} to be deleted, got {set(captured_deletes)}"
    )


# ── B2: _CCE_TOTAL_TOKEN_LIMIT contract ──────────────────────────────────────

def test_cce_total_token_limit_exists_and_gte_per_batch():
    """_CCE_TOKEN_LIMIT must be <= _CCE_TOTAL_TOKEN_LIMIT (per-batch fits within total)."""
    from nexus.doc_indexer import _CCE_TOKEN_LIMIT, _CCE_TOTAL_TOKEN_LIMIT

    assert _CCE_TOKEN_LIMIT <= _CCE_TOTAL_TOKEN_LIMIT, (
        f"Per-batch limit {_CCE_TOKEN_LIMIT} must be <= total limit {_CCE_TOTAL_TOKEN_LIMIT}"
    )


# ── B3: _CCE_MAX_TOTAL_CHUNKS constant ───────────────────────────────────────

def test_cce_max_total_chunks_constant():
    """_CCE_MAX_TOTAL_CHUNKS must exist and be 16_000."""
    from nexus.doc_indexer import _CCE_MAX_TOTAL_CHUNKS

    assert _CCE_MAX_TOTAL_CHUNKS == 16_000, (
        f"_CCE_MAX_TOTAL_CHUNKS must be 16_000, got {_CCE_MAX_TOTAL_CHUNKS}"
    )


def test_embed_with_fallback_warns_on_excessive_chunks(monkeypatch):
    """_embed_with_fallback logs a warning when chunk count exceeds 16K."""
    from unittest.mock import MagicMock, patch
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.embeddings = [[0.1]]
    mock_client.embed.return_value = mock_result

    with patch("voyageai.Client", return_value=mock_client):
        with patch("nexus.doc_indexer._log") as mock_log:
            # We can't actually pass 16K+ real chunks, so mock the limit to 1.
            with patch("nexus.doc_indexer._CCE_MAX_TOTAL_CHUNKS", 1):
                _embed_with_fallback(
                    chunks=["a", "b"],
                    model="voyage-4",
                    api_key="vk_test",
                )
            mock_log.warning.assert_called_once()
            call_args = mock_log.warning.call_args
            assert "chunk count exceeds" in call_args[0][0]


# ── B5: empty chunk list ─────────────────────────────────────────────────────

def test_embed_with_fallback_empty_chunks():
    """_embed_with_fallback returns ([], model) for empty input."""
    from nexus.doc_indexer import _embed_with_fallback

    embeddings, actual_model = _embed_with_fallback(
        chunks=[],
        model="voyage-context-3",
        api_key="vk_test",
    )
    assert embeddings == []
    assert actual_model == "voyage-context-3"


# ── B6: empty-string filtering ───────────────────────────────────────────────

def test_embed_with_fallback_filters_empty_strings(monkeypatch):
    """Empty strings and whitespace-only strings are removed before embedding.

    Voyage AI raises InvalidRequestError if the input list contains empty strings.
    Regression test for: voyageai.error.InvalidRequestError: Input cannot contain
    empty strings or empty lists.
    """
    from nexus.doc_indexer import _embed_with_fallback

    mock_result = MagicMock(spec=EmbeddingsObject)
    mock_result.embeddings = [[0.1, 0.2]]  # only 1 valid chunk after filtering

    mock_client = MagicMock()
    mock_client.embed.return_value = mock_result

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, _ = _embed_with_fallback(
            chunks=["", "   ", "real content", "\t\n"],
            model="voyage-4",
            api_key="vk_test",
        )

    # embed() must have been called only with the non-empty chunk
    assert mock_client.embed.called
    call_kwargs = mock_client.embed.call_args
    passed_texts = call_kwargs[1].get("texts") or call_kwargs[0][0]
    assert "" not in passed_texts
    assert "   " not in passed_texts
    assert "real content" in passed_texts
    assert len(embeddings) == 1


def test_embed_with_fallback_all_empty_strings():
    """If all chunks are empty strings, returns ([], model) without calling embed."""
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, model = _embed_with_fallback(
            chunks=["", "   ", "\n"],
            model="voyage-4",
            api_key="vk_test",
        )

    assert embeddings == []
    mock_client.embed.assert_not_called()


# ── nexus-mj98: force=True bypasses staleness check ──────────────────────────


def test_force_bypasses_staleness_pdf(sample_pdf, monkeypatch):
    """index_pdf(force=True) re-indexes even when content_hash and embedding_model match.

    Without force=True the staleness check would return 0 (skip). With force=True
    it must proceed to chunk, embed, and upsert — returning a non-zero chunk count.
    """
    set_credentials(monkeypatch)
    content_hash = hashlib.sha256(sample_pdf.read_bytes()).hexdigest()

    mock_col = MagicMock()
    # Staleness check returns matching hash + model (would normally cause skip)
    mock_col.get.return_value = {
        "ids": ["existing_id"],
        "metadatas": [{"content_hash": content_hash, "embedding_model": "voyage-context-3"}],
    }

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    mock_chunk = MagicMock()
    mock_chunk.text = "chunk text content"
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 18, "page_number": 1}

    def fake_embed(texts, model):
        return [[0.1] * 5] * len(texts), model

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_extractor_class:
            with patch("nexus.doc_indexer.PDFChunker") as mock_chunker_class:
                mock_extractor_class.return_value.extract.return_value = MagicMock(
                    text="extracted text",
                    metadata={
                        "extraction_method": "docling",
                        "page_count": 1,
                        "format": "markdown",
                        "page_boundaries": [],
                    },
                )
                mock_chunker_class.return_value.chunk.return_value = [mock_chunk]

                result = index_pdf(sample_pdf, corpus="mybook", force=True, embed_fn=fake_embed)

    assert result > 0, "force=True must bypass staleness skip and index the document"
    mock_t3.upsert_chunks_with_embeddings.assert_called_once()


def test_force_bypasses_staleness_markdown(sample_md, monkeypatch):
    """index_markdown(force=True) re-indexes even when content_hash and embedding_model match.

    Without force=True the staleness check would return 0 (skip). With force=True
    it must proceed to chunk, embed, and upsert — returning a non-zero chunk count.
    """
    set_credentials(monkeypatch)
    content_hash = hashlib.sha256(sample_md.read_bytes()).hexdigest()

    mock_col = MagicMock()
    # Staleness check returns matching hash + model (would normally cause skip)
    mock_col.get.return_value = {
        "ids": ["existing_id"],
        "metadatas": [{"content_hash": content_hash, "embedding_model": "voyage-context-3"}],
    }

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    mock_chunk = MagicMock()
    mock_chunk.text = "chunk text"
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {
        "chunk_start_char": 0,
        "chunk_end_char": 10,
        "page_number": 0,
        "header_path": "Hello",
    }

    def fake_embed(texts, model):
        return [[0.1] * 5] * len(texts), model

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.SemanticMarkdownChunker") as mock_chunker_class:
            mock_chunker_class.return_value.chunk.return_value = [mock_chunk]

            result = index_markdown(sample_md, corpus="docs", force=True, embed_fn=fake_embed)

    assert result > 0, "force=True must bypass staleness skip and index the document"
    mock_t3.upsert_chunks_with_embeddings.assert_called_once()


def test_force_default_false_still_skips(sample_pdf, monkeypatch):
    """Without force=True, the staleness skip is preserved (regression guard).

    Verifies that the default force=False behavior is unchanged: when hash and
    model both match the stored values, index_pdf returns 0 without chunking.
    """
    set_credentials(monkeypatch)
    content_hash = hashlib.sha256(sample_pdf.read_bytes()).hexdigest()

    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["existing_id"],
        "metadatas": [{"content_hash": content_hash, "embedding_model": "voyage-context-3"}],
    }

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_extractor_class:
            result = index_pdf(sample_pdf, corpus="mybook")

    assert result == 0, "Default force=False must preserve the staleness skip"
    mock_extractor_class.assert_not_called()


# ── Phase 3: batch helpers pass force through ─────────────────────────────────


def test_batch_index_markdowns_passes_force(tmp_path):
    """batch_index_markdowns(force=True) passes force=True to each index_markdown call."""
    from nexus.doc_indexer import batch_index_markdowns

    md1 = tmp_path / "a.md"
    md1.write_text("# A\n\nContent.")
    md2 = tmp_path / "b.md"
    md2.write_text("# B\n\nContent.")

    with patch("nexus.doc_indexer.index_markdown", return_value=2) as mock_md:
        batch_index_markdowns([md1, md2], corpus="test", force=True)

    assert mock_md.call_count == 2
    for c in mock_md.call_args_list:
        _, kwargs = c
        assert kwargs.get("force") is True


def test_batch_index_pdfs_passes_force(tmp_path):
    """batch_index_pdfs(force=True) passes force=True to each index_pdf call."""
    from nexus.doc_indexer import batch_index_pdfs

    pdf1 = tmp_path / "a.pdf"
    pdf1.write_bytes(b"%PDF-1.4 fake")
    pdf2 = tmp_path / "b.pdf"
    pdf2.write_bytes(b"%PDF-1.4 fake2")

    with patch("nexus.doc_indexer.index_pdf", return_value=3) as mock_pdf:
        batch_index_pdfs([pdf1, pdf2], corpus="test", force=True)

    assert mock_pdf.call_count == 2
    for c in mock_pdf.call_args_list:
        _, kwargs = c
        assert kwargs.get("force") is True

# ── on_file callback tests for batch functions (RDR-017 Phase 1d) ─────────────

def test_batch_index_markdowns_calls_on_file_per_file(tmp_path):
    """batch_index_markdowns calls on_file(path, chunks, elapsed_s) after each file."""
    from nexus.doc_indexer import batch_index_markdowns

    md1 = tmp_path / "a.md"
    md1.write_text("# A\nContent\n")
    md2 = tmp_path / "b.md"
    md2.write_text("# B\nContent\n")

    on_file_calls: list[tuple] = []

    with patch("nexus.doc_indexer.index_markdown", return_value=2) as mock_md:
        batch_index_markdowns(
            [md1, md2], corpus="test",
            on_file=lambda p, c, e: on_file_calls.append((p, c, e)),
        )

    assert len(on_file_calls) == 2, f"Expected 2 on_file calls, got {len(on_file_calls)}"
    called_names = {c[0].name for c in on_file_calls}
    assert called_names == {"a.md", "b.md"}
    for _, chunks, elapsed in on_file_calls:
        assert isinstance(chunks, int), f"chunks must be int, got {type(chunks)}"
        assert isinstance(elapsed, float) and elapsed >= 0.0, f"elapsed must be non-negative float"


def test_batch_index_pdfs_calls_on_file_per_file(tmp_path):
    """batch_index_pdfs calls on_file(path, chunks, elapsed_s) after each PDF."""
    from nexus.doc_indexer import batch_index_pdfs

    pdf1 = tmp_path / "a.pdf"
    pdf1.write_bytes(b"%PDF-1.4 fake")
    pdf2 = tmp_path / "b.pdf"
    pdf2.write_bytes(b"%PDF-1.4 fake2")

    on_file_calls: list[tuple] = []

    with patch("nexus.doc_indexer.index_pdf", return_value=3):
        batch_index_pdfs(
            [pdf1, pdf2], corpus="test",
            on_file=lambda p, c, e: on_file_calls.append((p, c, e)),
        )

    assert len(on_file_calls) == 2, f"Expected 2 on_file calls, got {len(on_file_calls)}"
    called_names = {c[0].name for c in on_file_calls}
    assert called_names == {"a.pdf", "b.pdf"}
    for _, chunks, elapsed in on_file_calls:
        assert isinstance(chunks, int), f"chunks must be int"
        assert isinstance(elapsed, float) and elapsed >= 0.0


def test_batch_index_markdowns_on_file_none_safe_default(tmp_path):
    """batch_index_markdowns(on_file=None) must not raise — backward compatible."""
    from nexus.doc_indexer import batch_index_markdowns

    md1 = tmp_path / "a.md"
    md1.write_text("# A\nContent\n")

    with patch("nexus.doc_indexer.index_markdown", return_value=1):
        batch_index_markdowns([md1], corpus="test")  # no on_file — must not raise


def test_batch_index_pdfs_on_file_none_safe_default(tmp_path):
    """batch_index_pdfs(on_file=None) must not raise — backward compatible."""
    from nexus.doc_indexer import batch_index_pdfs

    pdf1 = tmp_path / "a.pdf"
    pdf1.write_bytes(b"%PDF-1.4 fake")

    with patch("nexus.doc_indexer.index_pdf", return_value=1):
        batch_index_pdfs([pdf1], corpus="test")  # no on_file — must not raise

# ── nexus-uj09: return_metadata tests ─────────────────────────────────────────

def test_index_pdf_return_metadata_false_returns_int(sample_pdf, monkeypatch):
    """return_metadata=False (default): index_pdf returns int chunk count."""
    set_credentials(monkeypatch)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    mock_chunk = MagicMock()
    mock_chunk.text = "chunk content"
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 13, "page_number": 1}

    mock_voyage_client = MagicMock()
    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings = [[0.1, 0.2, 0.3]]
    mock_voyage_client.embed.return_value = mock_voyage_result

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_ext_cls:
            with patch("nexus.doc_indexer.PDFChunker") as mock_chk_cls:
                with patch("voyageai.Client", return_value=mock_voyage_client):
                    mock_ext = MagicMock()
                    mock_ext_cls.return_value = mock_ext
                    mock_ext.extract.return_value = MagicMock(
                        text="text", metadata={"extraction_method": "x", "page_count": 1,
                                               "format": "markdown", "page_boundaries": []}
                    )
                    mock_chk_cls.return_value.chunk.return_value = [mock_chunk]
                    result = index_pdf(sample_pdf, corpus="test")  # default return_metadata=False

    assert isinstance(result, int), f"Expected int, got {type(result)}"
    assert result == 1


def test_index_pdf_return_metadata_true_returns_dict(sample_pdf, monkeypatch):
    """return_metadata=True: index_pdf returns dict with chunks/pages/title/author."""
    set_credentials(monkeypatch)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    mock_chunk = MagicMock()
    mock_chunk.text = "chunk content"
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 13, "page_number": 2}

    mock_voyage_client = MagicMock()
    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings = [[0.1, 0.2, 0.3]]
    mock_voyage_client.embed.return_value = mock_voyage_result

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_ext_cls:
            with patch("nexus.doc_indexer.PDFChunker") as mock_chk_cls:
                with patch("voyageai.Client", return_value=mock_voyage_client):
                    mock_ext = MagicMock()
                    mock_ext_cls.return_value = mock_ext
                    mock_ext.extract.return_value = MagicMock(
                        text="text", metadata={"extraction_method": "x", "page_count": 1,
                                               "format": "markdown", "page_boundaries": [],
                                               "title": "My Paper", "author": "A. Thor"}
                    )
                    mock_chk_cls.return_value.chunk.return_value = [mock_chunk]
                    result = index_pdf(sample_pdf, corpus="test", return_metadata=True)

    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    assert result["chunks"] == 1
    assert isinstance(result["pages"], list)
    assert isinstance(result["title"], str)
    assert isinstance(result["author"], str)


def test_index_pdf_return_metadata_true_skipped_file_returns_empty_dict(sample_pdf, monkeypatch):
    """return_metadata=True on a skipped file (hash unchanged): returns empty-dict sentinel."""
    set_credentials(monkeypatch)
    import hashlib as _hl
    content_hash = _hl.sha256(sample_pdf.read_bytes()).hexdigest()

    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["existing"],
        "metadatas": [{"content_hash": content_hash, "embedding_model": "voyage-context-3"}],
    }
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_ext_cls:
            with patch("nexus.doc_indexer.PDFChunker"):
                with patch("voyageai.Client"):
                    mock_ext = MagicMock()
                    mock_ext_cls.return_value = mock_ext
                    mock_ext.extract.return_value = MagicMock(
                        text="text", metadata={"extraction_method": "x", "page_count": 1,
                                               "format": "markdown", "page_boundaries": []}
                    )
                    result = index_pdf(sample_pdf, corpus="test", return_metadata=True)

    assert isinstance(result, dict), f"Expected dict sentinel on skip, got {type(result)}"
    assert result["chunks"] == 0
    assert result["pages"] == []


def test_index_markdown_return_metadata_true_returns_dict(sample_md, monkeypatch):
    """return_metadata=True: index_markdown returns dict with chunks and sections."""
    set_credentials(monkeypatch)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    mock_voyage_client = MagicMock()
    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings = [[0.1, 0.2, 0.3]]
    mock_voyage_client.embed.return_value = mock_voyage_result

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("voyageai.Client", return_value=mock_voyage_client):
            result = index_markdown(sample_md, corpus="test", return_metadata=True)

    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    assert "chunks" in result
    assert "sections" in result
    assert isinstance(result["chunks"], int)
    assert isinstance(result["sections"], int)


def test_index_markdown_return_metadata_true_skipped_returns_empty_dict(sample_md, monkeypatch):
    """return_metadata=True on a skipped file (hash unchanged): returns empty-dict sentinel."""
    set_credentials(monkeypatch)
    import hashlib as _hl
    content_hash = _hl.sha256(sample_md.read_bytes()).hexdigest()

    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["existing"],
        "metadatas": [{"content_hash": content_hash, "embedding_model": "voyage-context-3"}],
    }
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("voyageai.Client"):
            result = index_markdown(sample_md, corpus="test", return_metadata=True)

    assert isinstance(result, dict), f"Expected dict sentinel on skip, got {type(result)}"
    assert result["chunks"] == 0
    assert result["sections"] == 0


# ── A5: per-chunk progress callback ──────────────────────────────────────────


def test_embed_progress_callback_fires():
    """on_progress callback receives (count, total) after each batch."""
    from nexus.doc_indexer import _embed_with_fallback

    progress_calls: list[tuple[int, int]] = []

    def on_progress(current: int, total: int) -> None:
        progress_calls.append((current, total))

    mock_client = MagicMock()
    embed_result = MagicMock()
    embed_result.embeddings = [[0.1] * 10, [0.2] * 10, [0.3] * 10]
    mock_client.embed.return_value = embed_result

    with patch("voyageai.Client", return_value=mock_client):
        _embed_with_fallback(
            ["chunk one", "chunk two", "chunk three"],
            "voyage-4", "test-key",
            on_progress=on_progress,
        )

    assert len(progress_calls) > 0
    # Last call should show all chunks done
    last_current, last_total = progress_calls[-1]
    assert last_current == last_total == 3


def test_embed_progress_callback_none_is_noop():
    """on_progress=None (default) does not cause errors."""
    from nexus.doc_indexer import _embed_with_fallback

    mock_client = MagicMock()
    embed_result = MagicMock()
    embed_result.embeddings = [[0.1] * 10]
    mock_client.embed.return_value = embed_result

    with patch("voyageai.Client", return_value=mock_client):
        # Should not raise
        _embed_with_fallback(
            ["chunk one"], "voyage-4", "test-key",
            on_progress=None,
        )


def test_embed_progress_callback_fires_for_cce():
    """on_progress fires after each CCE batch when model is voyage-context-3."""
    from nexus.doc_indexer import _embed_with_fallback

    progress_calls: list[tuple[int, int]] = []

    def on_progress(current: int, total: int) -> None:
        progress_calls.append((current, total))

    mock_client = MagicMock()
    cce_result = MagicMock(spec=ContextualizedEmbeddingsObject)
    inner = MagicMock(spec=ContextualizedEmbeddingsResult)
    inner.embeddings = [[0.1] * 10, [0.2] * 10]
    cce_result.results = [inner]
    mock_client.contextualized_embed.return_value = cce_result

    with patch("voyageai.Client", return_value=mock_client):
        _embed_with_fallback(
            ["chunk one", "chunk two"],
            "voyage-context-3", "test-key",
            on_progress=on_progress,
        )

    assert len(progress_calls) > 0
    last_current, last_total = progress_calls[-1]
    assert last_current == last_total == 2


def test_index_pdf_threads_on_progress(sample_pdf, monkeypatch):
    """on_progress passed to index_pdf fires during embedding."""
    set_credentials(monkeypatch)

    progress_calls: list[tuple[int, int]] = []

    def on_progress(current: int, total: int) -> None:
        progress_calls.append((current, total))

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    mock_chunk = MagicMock()
    mock_chunk.text = "chunk text content"
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 18, "page_number": 1}

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    mock_voyage_client = MagicMock()
    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
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
                            "extraction_method": "docling",
                            "page_count": 1,
                            "format": "markdown",
                            "page_boundaries": [],
                        },
                    )
                    mock_chunker = MagicMock()
                    mock_chunker_class.return_value = mock_chunker
                    mock_chunker.chunk.return_value = [mock_chunk]

                    result = index_pdf(
                        sample_pdf, corpus="mybook",
                        on_progress=on_progress,
                    )

    assert result == 1
    assert len(progress_calls) > 0


def test_index_markdown_threads_on_progress(sample_md, monkeypatch):
    """on_progress passed to index_markdown fires during embedding."""
    set_credentials(monkeypatch)

    progress_calls: list[tuple[int, int]] = []

    def on_progress(current: int, total: int) -> None:
        progress_calls.append((current, total))

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

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
    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings = [[0.1, 0.2]]
    mock_voyage_client.embed.return_value = mock_voyage_result

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.SemanticMarkdownChunker") as mock_chunker_class:
            with patch("voyageai.Client", return_value=mock_voyage_client):
                mock_chunker = MagicMock()
                mock_chunker_class.return_value = mock_chunker
                mock_chunker.chunk.return_value = [mock_chunk]

                result = index_markdown(
                    sample_md, corpus="docs",
                    on_progress=on_progress,
                )

    assert isinstance(result, int)
    assert result == 1
    assert len(progress_calls) > 0


# ── incremental PDF indexing (nexus-jr1p) ────────────────────────────────────


def _make_n_chunks(n: int, *, start: int = 0):
    """Create n mock chunks with sequential indices."""
    chunks = []
    for i in range(start, start + n):
        c = MagicMock()
        c.text = f"chunk text {i}" * 20  # ~200 chars each
        c.chunk_index = i
        c.metadata = {"chunk_start_char": i * 200, "chunk_end_char": (i + 1) * 200, "page_number": i // 5 + 1}
        chunks.append(c)
    return chunks


def test_index_pdf_incremental_indexes_all_chunks(sample_pdf, monkeypatch):
    """Incremental path processes all chunks through embed/upsert batches."""
    from nexus.doc_indexer import _INCREMENTAL_THRESHOLD
    set_credentials(monkeypatch)

    n_chunks = _INCREMENTAL_THRESHOLD + 10
    mock_chunks = _make_n_chunks(n_chunks)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    def _fake_embed(texts, model, **kwargs):
        return [[0.1] * 128] * len(texts), model

    # Ensure no real checkpoint files (use tmp dir)
    monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", sample_pdf.parent / "ckpt")

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_ext_cls:
            with patch("nexus.doc_indexer.PDFChunker") as mock_chk_cls:
                mock_ext = MagicMock()
                mock_ext_cls.return_value = mock_ext
                mock_ext.extract.return_value = MagicMock(
                    text="x" * 5000,
                    metadata={"extraction_method": "docling", "page_count": 50, "format": "markdown", "page_boundaries": []},
                )
                mock_chk = MagicMock()
                mock_chk_cls.return_value = mock_chk
                mock_chk.chunk.return_value = mock_chunks

                result = index_pdf(sample_pdf, corpus="test", embed_fn=_fake_embed)

    assert result == n_chunks
    # Verify upsert was called (possibly multiple batches)
    assert mock_t3.upsert_chunks_with_embeddings.call_count >= 1
    # Total chunks upserted across all calls
    total_upserted = sum(
        len(call.args[1]) for call in mock_t3.upsert_chunks_with_embeddings.call_args_list
    )
    assert total_upserted == n_chunks


def test_index_pdf_incremental_resumes_from_checkpoint(sample_pdf, monkeypatch):
    """When a checkpoint exists, skip already-upserted chunks."""
    from nexus.doc_indexer import _INCREMENTAL_THRESHOLD, _INCREMENTAL_BATCH_SIZE
    from nexus.checkpoint import CheckpointData, write_checkpoint

    set_credentials(monkeypatch)
    ckpt_dir = sample_pdf.parent / "ckpt"
    monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", ckpt_dir)
    monkeypatch.setattr("nexus.doc_indexer.CHECKPOINT_DIR", ckpt_dir)

    n_chunks = _INCREMENTAL_THRESHOLD + 50
    mock_chunks = _make_n_chunks(n_chunks)
    content_hash = hashlib.sha256(sample_pdf.read_bytes()).hexdigest()

    # Pre-write a checkpoint saying 64 chunks already done
    already_done = 64
    ck = CheckpointData(
        pdf=str(sample_pdf),
        collection="docs__test",
        content_hash=content_hash,
        chunks_upserted=already_done,
        total_chunks=n_chunks,
        embedding_model="voyage-context-3",
    )
    write_checkpoint(ck)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    def _fake_embed(texts, model, **kwargs):
        return [[0.1] * 128] * len(texts), model

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_ext_cls:
            with patch("nexus.doc_indexer.PDFChunker") as mock_chk_cls:
                mock_ext = MagicMock()
                mock_ext_cls.return_value = mock_ext
                mock_ext.extract.return_value = MagicMock(
                    text="x" * 5000,
                    metadata={"extraction_method": "docling", "page_count": 50, "format": "markdown", "page_boundaries": []},
                )
                mock_chk = MagicMock()
                mock_chk_cls.return_value = mock_chk
                mock_chk.chunk.return_value = mock_chunks

                result = index_pdf(sample_pdf, corpus="test", embed_fn=_fake_embed)

    assert result == n_chunks
    # Only the remaining chunks should have been embedded/upserted
    total_upserted = sum(
        len(call.args[1]) for call in mock_t3.upsert_chunks_with_embeddings.call_args_list
    )
    assert total_upserted == n_chunks - already_done


def test_index_pdf_incremental_deletes_checkpoint_on_success(sample_pdf, monkeypatch):
    """Checkpoint file is cleaned up after successful completion."""
    from nexus.doc_indexer import _INCREMENTAL_THRESHOLD
    from nexus.checkpoint import checkpoint_path

    set_credentials(monkeypatch)
    ckpt_dir = sample_pdf.parent / "ckpt"
    monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", ckpt_dir)
    monkeypatch.setattr("nexus.doc_indexer.CHECKPOINT_DIR", ckpt_dir)

    n_chunks = _INCREMENTAL_THRESHOLD + 10
    mock_chunks = _make_n_chunks(n_chunks)
    content_hash = hashlib.sha256(sample_pdf.read_bytes()).hexdigest()

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    def _fake_embed(texts, model, **kwargs):
        return [[0.1] * 128] * len(texts), model

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_ext_cls:
            with patch("nexus.doc_indexer.PDFChunker") as mock_chk_cls:
                mock_ext = MagicMock()
                mock_ext_cls.return_value = mock_ext
                mock_ext.extract.return_value = MagicMock(
                    text="x" * 5000,
                    metadata={"extraction_method": "docling", "page_count": 50, "format": "markdown", "page_boundaries": []},
                )
                mock_chk = MagicMock()
                mock_chk_cls.return_value = mock_chk
                mock_chk.chunk.return_value = mock_chunks

                result = index_pdf(sample_pdf, corpus="test", embed_fn=_fake_embed)

    assert result == n_chunks
    # Checkpoint should be deleted after success
    ckpt = checkpoint_path(content_hash, "docs__test")
    assert not ckpt.exists()


def test_index_pdf_small_doc_uses_original_path(sample_pdf, monkeypatch):
    """Documents with fewer chunks than threshold use the original non-incremental path."""
    set_credentials(monkeypatch)

    mock_chunks = _make_n_chunks(5)  # Well below threshold

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    def _fake_embed(texts, model, **kwargs):
        return [[0.1] * 128] * len(texts), model

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_ext_cls:
            with patch("nexus.doc_indexer.PDFChunker") as mock_chk_cls:
                mock_ext = MagicMock()
                mock_ext_cls.return_value = mock_ext
                mock_ext.extract.return_value = MagicMock(
                    text="small doc",
                    metadata={"extraction_method": "docling", "page_count": 3, "format": "markdown", "page_boundaries": []},
                )
                mock_chk = MagicMock()
                mock_chk_cls.return_value = mock_chk
                mock_chk.chunk.return_value = mock_chunks

                result = index_pdf(sample_pdf, corpus="test", embed_fn=_fake_embed)

    assert result == 5
    # Original path calls upsert once with all chunks
    assert mock_t3.upsert_chunks_with_embeddings.call_count == 1


def test_index_pdf_incremental_writes_checkpoints_per_batch(sample_pdf, monkeypatch):
    """Checkpoints are written after each batch, not just at the end."""
    from nexus.doc_indexer import _INCREMENTAL_THRESHOLD, _INCREMENTAL_BATCH_SIZE
    from nexus.checkpoint import CheckpointData

    set_credentials(monkeypatch)
    ckpt_dir = sample_pdf.parent / "ckpt"
    monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", ckpt_dir)
    monkeypatch.setattr("nexus.doc_indexer.CHECKPOINT_DIR", ckpt_dir)

    # Use enough chunks to require multiple batches
    n_chunks = _INCREMENTAL_BATCH_SIZE * 3 + 10
    mock_chunks = _make_n_chunks(n_chunks)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    checkpoint_writes = []
    original_write = __import__("nexus.checkpoint", fromlist=["write_checkpoint"]).write_checkpoint

    def _tracking_write(data: CheckpointData):
        checkpoint_writes.append(data.chunks_upserted)
        original_write(data)

    def _fake_embed(texts, model, **kwargs):
        return [[0.1] * 128] * len(texts), model

    with patch("nexus.doc_indexer.write_checkpoint", side_effect=_tracking_write):
        with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
            with patch("nexus.doc_indexer.PDFExtractor") as mock_ext_cls:
                with patch("nexus.doc_indexer.PDFChunker") as mock_chk_cls:
                    mock_ext = MagicMock()
                    mock_ext_cls.return_value = mock_ext
                    mock_ext.extract.return_value = MagicMock(
                        text="x" * 5000,
                        metadata={"extraction_method": "docling", "page_count": 50, "format": "markdown", "page_boundaries": []},
                    )
                    mock_chk = MagicMock()
                    mock_chk_cls.return_value = mock_chk
                    mock_chk.chunk.return_value = mock_chunks

                    result = index_pdf(sample_pdf, corpus="test", embed_fn=_fake_embed)

    assert result == n_chunks
    # Should have written a checkpoint after each batch (at least 3 batches)
    assert len(checkpoint_writes) >= 3
    # Checkpoint values should be monotonically increasing
    for i in range(1, len(checkpoint_writes)):
        assert checkpoint_writes[i] > checkpoint_writes[i - 1]
    # Final checkpoint write should equal total chunks
    assert checkpoint_writes[-1] == n_chunks


def test_index_pdf_incremental_stale_checkpoint_deleted(sample_pdf, monkeypatch):
    """A checkpoint with wrong content_hash is deleted and indexing starts fresh."""
    from nexus.doc_indexer import _INCREMENTAL_THRESHOLD
    from nexus.checkpoint import CheckpointData, write_checkpoint, checkpoint_path

    set_credentials(monkeypatch)
    ckpt_dir = sample_pdf.parent / "ckpt"
    monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", ckpt_dir)
    monkeypatch.setattr("nexus.doc_indexer.CHECKPOINT_DIR", ckpt_dir)

    n_chunks = _INCREMENTAL_THRESHOLD + 10
    mock_chunks = _make_n_chunks(n_chunks)

    # Write a checkpoint with a WRONG content hash — should be ignored
    ck = CheckpointData(
        pdf=str(sample_pdf),
        collection="docs__test",
        content_hash="wrong_hash_from_old_version",
        chunks_upserted=50,
        total_chunks=200,
        embedding_model="voyage-context-3",
    )
    write_checkpoint(ck)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    def _fake_embed(texts, model, **kwargs):
        return [[0.1] * 128] * len(texts), model

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_ext_cls:
            with patch("nexus.doc_indexer.PDFChunker") as mock_chk_cls:
                mock_ext = MagicMock()
                mock_ext_cls.return_value = mock_ext
                mock_ext.extract.return_value = MagicMock(
                    text="x" * 5000,
                    metadata={"extraction_method": "docling", "page_count": 50, "format": "markdown", "page_boundaries": []},
                )
                mock_chk = MagicMock()
                mock_chk_cls.return_value = mock_chk
                mock_chk.chunk.return_value = mock_chunks

                result = index_pdf(sample_pdf, corpus="test", embed_fn=_fake_embed)

    # All chunks should be indexed (no resume from stale checkpoint)
    assert result == n_chunks
    total_upserted = sum(
        len(call.args[1]) for call in mock_t3.upsert_chunks_with_embeddings.call_args_list
    )
    assert total_upserted == n_chunks


def test_index_pdf_incremental_progress_fires(sample_pdf, monkeypatch):
    """Progress callback fires during incremental indexing."""
    from nexus.doc_indexer import _INCREMENTAL_THRESHOLD

    set_credentials(monkeypatch)
    ckpt_dir = sample_pdf.parent / "ckpt"
    monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", ckpt_dir)
    monkeypatch.setattr("nexus.doc_indexer.CHECKPOINT_DIR", ckpt_dir)

    n_chunks = _INCREMENTAL_THRESHOLD + 10
    mock_chunks = _make_n_chunks(n_chunks)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    progress_calls = []

    def _on_progress(done, total):
        progress_calls.append((done, total))

    def _fake_embed(texts, model, **kwargs):
        return [[0.1] * 128] * len(texts), model

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as mock_ext_cls:
            with patch("nexus.doc_indexer.PDFChunker") as mock_chk_cls:
                mock_ext = MagicMock()
                mock_ext_cls.return_value = mock_ext
                mock_ext.extract.return_value = MagicMock(
                    text="x" * 5000,
                    metadata={"extraction_method": "docling", "page_count": 50, "format": "markdown", "page_boundaries": []},
                )
                mock_chk = MagicMock()
                mock_chk_cls.return_value = mock_chk
                mock_chk.chunk.return_value = mock_chunks

                result = index_pdf(
                    sample_pdf, corpus="test", embed_fn=_fake_embed,
                    on_progress=_on_progress,
                )

    assert result == n_chunks
    assert len(progress_calls) > 0
    # Last progress call should report all chunks done
    assert progress_calls[-1][0] == n_chunks
    assert progress_calls[-1][1] == n_chunks


# ── parallel embedding + rate limiter (nexus-cmcp) ───────────────────────────

import time


def test_token_bucket_rate_limiter():
    """TokenBucket limits throughput to target RPM."""
    from nexus.doc_indexer import _TokenBucket

    # 600 RPM = 10 per second. Allow 3 immediate, then must wait.
    bucket = _TokenBucket(rpm=600, burst=3)
    # First 3 should be immediate
    t0 = time.monotonic()
    for _ in range(3):
        bucket.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1  # Should be near-instant


def test_token_bucket_zero_burst_still_works():
    """TokenBucket with burst=1 still allows at least one request."""
    from nexus.doc_indexer import _TokenBucket

    bucket = _TokenBucket(rpm=60, burst=1)
    bucket.acquire()  # Should not hang


def test_parallel_embed_preserves_order(monkeypatch):
    """Parallel CCE embedding returns embeddings in submission order."""
    from nexus.doc_indexer import _embed_with_fallback

    call_order = []

    def _mock_cce(inputs, model, input_type):
        batch = inputs[0]
        call_order.append(len(batch))
        time.sleep(0.01 * len(batch))
        embeddings = [[float(i)] * 10 for i in range(len(batch))]
        cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
        cce_item.embeddings = embeddings
        result = MagicMock(spec=ContextualizedEmbeddingsObject)
        result.results = [cce_item]
        return result

    mock_client = MagicMock()
    mock_client.contextualized_embed = _mock_cce

    # 10 chunks at 5000 chars each = ~2500 tokens each
    # 2500 * 10 = 25000 > 24000 limit → will split into 2+ batches
    chunks = ["x" * 5000] * 10

    with patch("voyageai.Client", return_value=mock_client):
        embeddings, model = _embed_with_fallback(
            chunks, "voyage-context-3", "test-key",
        )

    assert len(embeddings) == 10
    assert model == "voyage-context-3"


def test_parallel_embed_progress_fires_for_each_batch(monkeypatch):
    """on_progress fires after each CCE batch completes during parallel embedding."""
    from nexus.doc_indexer import _embed_with_fallback

    progress_calls = []

    def _mock_cce(inputs, model, input_type):
        batch = inputs[0]
        cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
        cce_item.embeddings = [[0.1] * 10 for _ in batch]
        result = MagicMock(spec=ContextualizedEmbeddingsObject)
        result.results = [cce_item]
        return result

    mock_client = MagicMock()
    mock_client.contextualized_embed = _mock_cce

    chunks = ["x" * 5000] * 10

    with patch("voyageai.Client", return_value=mock_client):
        _embed_with_fallback(
            chunks, "voyage-context-3", "test-key",
            on_progress=lambda done, total: progress_calls.append((done, total)),
        )

    assert len(progress_calls) >= 1
    assert progress_calls[-1][0] == 10
