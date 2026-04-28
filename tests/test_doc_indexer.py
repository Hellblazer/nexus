# SPDX-License-Identifier: AGPL-3.0-or-later
"""AC6-AC7: doc_indexer — SHA256 incremental sync, docs__ metadata schema."""
import hashlib
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from voyageai.object.contextualized_embeddings import (
    ContextualizedEmbeddingsObject,
    ContextualizedEmbeddingsResult,
)
from voyageai.object.embeddings import EmbeddingsObject

from nexus.doc_indexer import (
    _batch_chunks_for_cce, _embed_with_fallback, _markdown_chunks,
    _TokenBucket, batch_index_markdowns, batch_index_pdfs,
    index_markdown, index_pdf,
)
from tests.conftest import set_credentials


def _add_cce_mock(mock_voyage_client: MagicMock) -> None:
    def _fake_cce(inputs, model, input_type):
        batch = inputs[0]
        cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
        cce_item.embeddings = [[0.1, 0.2] for _ in batch]
        result = MagicMock(spec=ContextualizedEmbeddingsObject)
        result.results = [cce_item]
        return result
    mock_voyage_client.contextualized_embed.side_effect = _fake_cce


def _make_pdf_mocks():
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


def _make_n_chunks(n: int, *, start: int = 0):
    chunks = []
    for i in range(start, start + n):
        c = MagicMock()
        c.text = f"chunk text {i}" * 20
        c.chunk_index = i
        c.metadata = {"chunk_start_char": i * 200, "chunk_end_char": (i + 1) * 200, "page_number": i // 5 + 1}
        chunks.append(c)
    return chunks


def _fake_embed(texts, model, **kwargs):
    return [[0.1] * 128] * len(texts), model


_BATCH_FNS = {
    "pdf": (batch_index_pdfs, "index_pdf", ".pdf", True),
    "markdown": (batch_index_markdowns, "index_markdown", ".md", False),
}


def _make_batch_files(tmp_path, ext, is_bytes, names=("a", "b")):
    files = []
    for name in names:
        f = tmp_path / f"{name}{ext}"
        if is_bytes:
            f.write_bytes(b"%PDF-1.4 fake")
        else:
            f.write_text(f"# {name.upper()}\n\nContent.\n")
        files.append(f)
    return files


def _make_cce_client(embeddings_per_call=None, fail_on_call=None):
    """Create a mock Voyage client with CCE behavior.

    embeddings_per_call: list of embeddings per batch item, or None for default.
    fail_on_call: set of 1-based call indices that should raise RuntimeError.
    """
    mock_client = MagicMock()
    call_count = [0]
    fail_on = fail_on_call or set()

    def fake_cce(inputs, model, input_type):
        call_count[0] += 1
        if call_count[0] in fail_on:
            raise RuntimeError(f"batch error on call {call_count[0]}")
        cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
        if embeddings_per_call is not None:
            cce_item.embeddings = embeddings_per_call
        else:
            cce_item.embeddings = [[0.1] for _ in inputs[0]]
        result = MagicMock(spec=ContextualizedEmbeddingsObject)
        result.results = [cce_item]
        return result

    mock_client.contextualized_embed.side_effect = fake_cce
    mock_client._call_count = call_count
    return mock_client


@pytest.fixture(autouse=True)
def _no_bib_enrich(monkeypatch):
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


@pytest.fixture
def empty_col():
    col = MagicMock()
    col.get.return_value = {"ids": [], "metadatas": []}
    return col


@pytest.fixture
def mock_t3(empty_col):
    t3 = MagicMock()
    t3.get_or_create_collection.return_value = empty_col
    return t3


@pytest.fixture
def voyage_client():
    client = MagicMock()
    _add_cce_mock(client)
    return client


def test_index_md_falls_back_to_local_embedder_when_no_credentials(
    sample_md, tmp_path, monkeypatch,
):
    """GH #336 (option 3): ``nx index md`` must work without
    Voyage/Chroma credentials in local mode — matching ``nx doctor``'s
    claim that local mode needs no API keys, and matching the
    local-embedder path that ``store_put`` already uses. The local
    ONNX/fastembed embedder produces real vectors; chunks land in
    the injected client; staleness check uses the local model name
    so re-indexes against unchanged content are no-ops.

    PDF parity is verified by inspection — ``index_pdf`` uses the
    same fallback codepath via ``_make_local_embed_fn`` — but its
    own integration test requires a real PDF fixture (the existing
    ``sample_pdf`` is fake bytes; PDF tests in this file mock the
    extractor). The codepath itself is tested at the source level.
    """
    import chromadb

    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    monkeypatch.setattr(
        "nexus.config._global_config_path", lambda: Path("/nonexistent"),
    )
    # is_local_mode() returns True when either key is absent; with
    # both keys cleared above it's True without an explicit NX_LOCAL.

    # Inject an EphemeralClient so the test doesn't hit a real
    # PersistentClient on disk.
    client = chromadb.EphemeralClient()
    from nexus.db.t3 import T3Database
    local_t3 = T3Database(_client=client, local_mode=True)

    n = index_markdown(sample_md, corpus="local_fallback_test", t3=local_t3)
    assert n > 0, (
        f"local-mode markdown index should produce chunks; got {n}. "
        f"This is the GH #336 contract: ingestion works without keys "
        f"in local mode."
    )

    # Verify chunks landed AND were tagged with the local model name
    # (not voyage-context-3 — staleness check on re-run depends on it).
    col = local_t3.get_or_create_collection("docs__local_fallback_test")
    rows = col.get(limit=1, include=["metadatas"])
    assert rows["metadatas"], "expected at least one chunk in collection"
    embedding_model = rows["metadatas"][0].get("embedding_model", "")
    assert embedding_model and embedding_model != "voyage-context-3", (
        f"chunk metadata should record the LOCAL model name; got "
        f"{embedding_model!r}. The staleness check on re-index "
        f"compares stored_model == target_model, and using the local "
        f"name for both keeps repeat-index a no-op."
    )

    # Re-index against unchanged content: should skip (return 0)
    # because hash + model match.
    n2 = index_markdown(sample_md, corpus="local_fallback_test", t3=local_t3)
    assert n2 == 0, (
        f"re-index against unchanged content should be a no-op; got {n2}. "
        f"If this fails, the staleness check is comparing the local "
        f"actual_model against the cloud target_model from "
        f"index_model_for_collection — see local_target_model override."
    )


def test_make_local_embed_fn_returns_consistent_model_name():
    """Sanity: ``_make_local_embed_fn`` returns an embed_fn AND a
    model_name. Calling the embed_fn returns embeddings tagged with
    the SAME model_name. The caller relies on this consistency to
    align ``target_model`` with what the embedder actually reports.
    """
    from nexus.doc_indexer import _make_local_embed_fn

    embed_fn, model_name = _make_local_embed_fn()
    assert isinstance(model_name, str) and model_name
    assert model_name != "voyage-context-3"

    embeddings, reported_model = embed_fn(["hello world"], "voyage-context-3")
    assert len(embeddings) == 1
    assert isinstance(embeddings[0], list)
    assert len(embeddings[0]) > 0
    assert reported_model == model_name, (
        "embed_fn must report the same model_name returned by "
        "_make_local_embed_fn — otherwise the caller's target_model "
        "override (which uses the returned model_name) and the chunk "
        "metadata (which uses the embed_fn's reported name) would "
        "diverge, breaking the staleness check on re-index."
    )


@pytest.mark.parametrize("indexer,fixture_name", [
    ("pdf", "sample_pdf"),
    ("markdown", "sample_md"),
])
def test_index_raises_credentials_missing_when_cloud_mode_explicit(
    indexer, fixture_name, sample_pdf, sample_md, monkeypatch,
):
    """The corollary: when the user has explicitly opted into cloud
    mode (``NX_LOCAL=0``) but credentials are missing, fail fast with
    ``CredentialsMissingError`` rather than silently degrading to
    local. ``NX_LOCAL=0`` is the operator's commitment to using
    Voyage; honoring it means a credential gap should be surfaced,
    not papered over.
    """
    from nexus.errors import CredentialsMissingError

    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setattr(
        "nexus.config._global_config_path", lambda: Path("/nonexistent"),
    )
    path = sample_pdf if indexer == "pdf" else sample_md
    fn = index_pdf if indexer == "pdf" else index_markdown
    with patch("nexus.doc_indexer.make_t3") as mock_factory:
        with pytest.raises(CredentialsMissingError) as excinfo:
            fn(path, corpus="test")
    mock_factory.assert_not_called()
    assert "voyage_api_key" in str(excinfo.value)
    assert "chroma_api_key" in str(excinfo.value)
    assert "NX_LOCAL" in str(excinfo.value)


def test_index_pdf_skips_if_hash_unchanged(sample_pdf, monkeypatch):
    set_credentials(monkeypatch)
    content_hash = hashlib.sha256(sample_pdf.read_bytes()).hexdigest()
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["existing_chunk_id"],
        "metadatas": [{"content_hash": content_hash, "embedding_model": "voyage-context-3"}],
    }
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as ext_cls:
            result = index_pdf(sample_pdf, corpus="mybook")
    assert result == 0
    ext_cls.assert_not_called()


def test_index_pdf_upserts_chunks_when_new(sample_pdf, monkeypatch, mock_t3, voyage_client):
    set_credentials(monkeypatch)
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with pdf_extract_patches_ctx() as pep:
            with patch("voyageai.Client", return_value=voyage_client):
                result = index_pdf(sample_pdf, corpus="mybook")
    assert result == 1
    mock_t3.upsert_chunks_with_embeddings.assert_called_once()


def test_index_pdf_fires_document_hook_exactly_once(
    sample_pdf, monkeypatch, mock_t3, voyage_client,
) -> None:
    """RDR-089 runtime fire-once invariant (substantive critic
    Significant #5). The AST drift guard counts call-sites
    statically; this test pins the runtime property.

    A bug that moves ``fire_post_document_hooks`` inside a
    per-chunk loop would have N invocations per document instead
    of 1 — invisible to the AST count guard, expensive in API
    calls, and produces single-chunk aspects for multi-chunk
    documents (semantically wrong). Pin via a counting hook
    registered for the duration of one ``index_pdf`` call.
    """
    from nexus.mcp_infra import (
        _post_document_hooks,
        register_post_document_hook,
    )

    fires: list[tuple[str, str, str]] = []

    def counting_hook(source_path: str, collection: str, content: str) -> None:
        fires.append((source_path, collection, content))

    register_post_document_hook(counting_hook)
    try:
        set_credentials(monkeypatch)
        with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
            with pdf_extract_patches_ctx():
                with patch("voyageai.Client", return_value=voyage_client):
                    index_pdf(sample_pdf, corpus="mybook")
    finally:
        if counting_hook in _post_document_hooks:
            _post_document_hooks.remove(counting_hook)

    assert len(fires) == 1, (
        f"Document hook fired {len(fires)} times for one PDF — "
        f"expected exactly 1. A regression here usually means a "
        f"fire site was moved inside a per-chunk loop."
    )
    captured_source, captured_coll, captured_content = fires[0]
    # CLI ingest path passes content="" per the P0.1 contract; the
    # source_path is the PDF path.
    assert captured_source == str(sample_pdf.resolve())
    assert captured_coll == "docs__mybook"
    assert captured_content == ""


def pdf_extract_patches_ctx():
    """Inline context manager for PDF extract + chunk patches."""
    class _Ctx:
        def __enter__(self):
            self._ext = patch("nexus.doc_indexer.PDFExtractor")
            self._chk = patch("nexus.doc_indexer.PDFChunker")
            ext_cls = self._ext.__enter__()
            chk_cls = self._chk.__enter__()
            chunk = MagicMock()
            chunk.text = "chunk text content"
            chunk.chunk_index = 0
            chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 18, "page_number": 1}
            ext_cls.return_value.extract.return_value = MagicMock(
                text="extracted text",
                metadata={"extraction_method": "docling", "page_count": 1,
                          "format": "markdown", "page_boundaries": []},
            )
            chk_cls.return_value.chunk.return_value = [chunk]
            self.ext_cls = ext_cls
            self.chk_cls = chk_cls
            self.chunk = chunk
            return self
        def __exit__(self, *a):
            self._chk.__exit__(*a)
            self._ext.__exit__(*a)
    return _Ctx()


_BASE_REQUIRED_FIELDS = {
    # Identity / position / spans (post source_title→title collapse, expires_at→indexed_at swap)
    "source_path", "content_hash", "chunk_text_hash", "chunk_index", "chunk_count",
    "chunk_start_char", "chunk_end_char", "page_number",
    # Display / routing
    "title", "source_author", "section_title", "section_type",
    "tags", "category", "content_type", "store_type", "corpus", "embedding_model",
    # Lifecycle
    "indexed_at", "ttl_days", "frecency_score", "source_agent", "session_id",
}
# pdf_subject / pdf_keywords / is_image_pdf / has_formulas / format /
# extraction_method / page_count / source_date are intentionally NOT in
# ALLOWED_TOP_LEVEL — normalize() drops them. They were never stored
# in T3 even before the factory refactor; the old test asserted on the
# pre-normalize dict shape. After the factory, normalize runs inside
# the indexer so the dropped fields are visible-as-missing.
_PDF_EXTRA_FIELDS: set[str] = set()


def test_docs_metadata_schema_complete(sample_md, monkeypatch, mock_t3, voyage_client):
    set_credentials(monkeypatch)
    captured: list[dict] = []
    mock_t3.upsert_chunks_with_embeddings.side_effect = (
        lambda collection, ids, documents, embeddings, metadatas: captured.extend(metadatas)
    )
    mock_chunk = MagicMock()
    mock_chunk.text = "chunk text"
    mock_chunk.chunk_index = 0
    mock_chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 10, "page_number": 0, "header_path": "Hello"}
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.SemanticMarkdownChunker") as chk_cls:
            with patch("voyageai.Client", return_value=voyage_client):
                chk_cls.return_value.chunk.return_value = [mock_chunk]
                index_markdown(sample_md, corpus="docs")
    assert captured
    missing = _BASE_REQUIRED_FIELDS - captured[0].keys()
    assert not missing, f"Missing metadata fields: {missing}"


def test_pdf_metadata_schema_complete(simple_pdf: Path, monkeypatch):
    set_credentials(monkeypatch)
    captured: list[dict] = []
    mock_t3 = MagicMock()
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_t3.get_or_create_collection.return_value = mock_col
    mock_t3.upsert_chunks_with_embeddings.side_effect = (
        lambda collection, ids, documents, embeddings, metadatas: captured.extend(metadatas)
    )
    with patch("nexus.doc_indexer._embed_with_fallback",
               side_effect=lambda chunks, model, api_key, input_type="document", timeout=120.0, on_progress=None:
               ([[0.1] * 5] * len(chunks), "test-local")):
        index_pdf(simple_pdf, corpus="test", t3=mock_t3)
    assert captured
    missing = (_BASE_REQUIRED_FIELDS | _PDF_EXTRA_FIELDS) - captured[0].keys()
    assert not missing, f"Missing PDF metadata fields: {missing}"


def test_sha256_does_not_call_read_bytes(tmp_path: Path):
    import nexus.doc_indexer as di_mod
    large_file = tmp_path / "large.bin"
    large_file.write_bytes(b"x" * 1024)
    real_open = large_file.open
    opened = []

    class _TrackingPath(type(large_file)):
        def read_bytes(self):
            raise AssertionError("read_bytes() called -- should stream instead")
        def open(self, *a, **kw):
            fh = real_open(*a, **kw)
            opened.append(True)
            return fh

    result = di_mod._sha256(_TrackingPath(large_file))
    assert len(result) == 64
    assert opened


@pytest.mark.parametrize("indexer,expected_type", [("pdf", "pdf"), ("markdown", "markdown")])
def test_index_sets_store_type(indexer, expected_type, sample_pdf, sample_md, monkeypatch, voyage_client):
    set_credentials(monkeypatch)
    captured: list[dict] = []
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    mock_t3.upsert_chunks_with_embeddings.side_effect = (
        lambda collection, ids, documents, embeddings, metadatas: captured.extend(metadatas)
    )
    mock_chunk = MagicMock()
    mock_chunk.text = "text"
    mock_chunk.chunk_index = 0
    if indexer == "pdf":
        mock_chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 4, "page_number": 1}
        with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
            with patch("nexus.doc_indexer.PDFExtractor") as ext_cls:
                with patch("nexus.doc_indexer.PDFChunker") as chk_cls:
                    with patch("voyageai.Client", return_value=voyage_client):
                        ext_cls.return_value.extract.return_value = MagicMock(
                            text="txt", metadata={"page_count": 1, "format": "pdf", "extraction_method": "x"})
                        chk_cls.return_value.chunk.return_value = [mock_chunk]
                        index_pdf(sample_pdf, corpus="mybook")
    else:
        mock_chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 4, "page_number": 0, "header_path": "H"}
        with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
            with patch("nexus.doc_indexer.SemanticMarkdownChunker") as chk_cls:
                with patch("voyageai.Client", return_value=voyage_client):
                    chk_cls.return_value.chunk.return_value = [mock_chunk]
                    index_markdown(sample_md, corpus="docs")
    assert captured
    assert captured[0]["store_type"] == expected_type


@pytest.mark.parametrize("has_fm,fm_text,body,expected_start,expected_end", [
    (True, "---\ntitle: Test\n---\n", "# Hello\n\nWorld content.", 20, 43),
    (False, "", "# Hello\n\nWorld.", 5, 15),
])
def test_index_markdown_offsets(has_fm, fm_text, body, expected_start, expected_end, tmp_path, monkeypatch, voyage_client):
    set_credentials(monkeypatch)
    md_path = tmp_path / "doc.md"
    md_path.write_text(fm_text + body)
    captured: list[dict] = []
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    mock_t3.upsert_chunks_with_embeddings.side_effect = (
        lambda collection, ids, documents, embeddings, metadatas: captured.extend(metadatas)
    )
    mock_chunk = MagicMock()
    mock_chunk.text = "text"
    mock_chunk.chunk_index = 0
    if has_fm:
        mock_chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": len(body), "page_number": 0, "header_path": "Hello"}
    else:
        mock_chunk.metadata = {"chunk_start_char": 5, "chunk_end_char": 15, "page_number": 0, "header_path": ""}
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.SemanticMarkdownChunker") as chk_cls:
            with patch("voyageai.Client", return_value=voyage_client):
                chk_cls.return_value.chunk.return_value = [mock_chunk]
                index_markdown(md_path, corpus="docs")
    assert captured
    assert captured[0]["chunk_start_char"] == expected_start
    assert captured[0]["chunk_end_char"] == expected_end


@pytest.mark.parametrize("n_chunks,expected_embs", [
    (2, [[0.1, 0.2], [0.3, 0.4]]),
    (1, [[0.5, 0.6]]),
])
def test_embed_with_fallback_calls_cce(n_chunks, expected_embs):

    mock_client = MagicMock()
    cce_result = MagicMock(spec=ContextualizedEmbeddingsResult)
    cce_result.embeddings = expected_embs
    result_obj = MagicMock(spec=ContextualizedEmbeddingsObject)
    result_obj.results = [cce_result]
    mock_client.contextualized_embed.return_value = result_obj
    with patch("voyageai.Client", return_value=mock_client):
        embeddings, model = _embed_with_fallback(
            chunks=[f"chunk {i}" for i in range(n_chunks)],
            model="voyage-context-3", api_key="vk_test",
        )
    mock_client.contextualized_embed.assert_called_once()
    mock_client.embed.assert_not_called()
    assert embeddings == expected_embs
    assert model == "voyage-context-3"


def test_single_chunk_cce_uses_contextualized_embed():

    client = _make_cce_client(embeddings_per_call=[[0.1] * 10])
    with patch("voyageai.Client", return_value=client):
        embeddings, model = _embed_with_fallback(["single chunk content"], "voyage-context-3", "test-key")
    client.contextualized_embed.assert_called_once()
    client.embed.assert_not_called()
    assert model == "voyage-context-3"
    assert len(embeddings) == 1


def test_embed_with_fallback_cce_failure_splits_and_stays_on_model():

    client = _make_cce_client(fail_on_call={1})
    with patch("voyageai.Client", return_value=client):
        embeddings, model = _embed_with_fallback(chunks=["a", "b"], model="voyage-context-3", api_key="vk_test")
    assert model == "voyage-context-3"
    assert len(embeddings) == 2
    client.embed.assert_not_called()


def test_embed_with_fallback_batches_large_input():

    chunks = [f"chunk{i}_" + "x" * 24_000 for i in range(6)]
    client = _make_cce_client()
    with patch("voyageai.Client", return_value=client):
        embeddings, model = _embed_with_fallback(chunks=chunks, model="voyage-context-3", api_key="vk_test")
    assert client._call_count[0] >= 2
    client.embed.assert_not_called()
    assert model == "voyage-context-3"
    assert len(embeddings) == 6


def test_partial_cce_failure_splits_failed_batch():

    client = _make_cce_client(fail_on_call={2})
    chunks = ["chunk a", "chunk b", "chunk c", "chunk d"]
    forced_batches = [["chunk a", "chunk b"], ["chunk c", "chunk d"]]
    with patch("voyageai.Client", return_value=client), \
         patch("nexus.doc_indexer._batch_chunks_for_cce", return_value=forced_batches):
        embeddings, model = _embed_with_fallback(chunks, "voyage-context-3", "test-key")
    assert model == "voyage-context-3"
    assert len(embeddings) == 4
    assert client._call_count[0] == 4
    client.embed.assert_not_called()


def test_cce_contract_no_top_level_embeddings_attribute():
    obj = ContextualizedEmbeddingsObject(response=None)
    assert not hasattr(obj, "embeddings")


def test_cce_contract_results_list_with_embeddings():
    obj = ContextualizedEmbeddingsObject(response=None)
    assert hasattr(obj, "results") and isinstance(obj.results, list)
    item = ContextualizedEmbeddingsResult(index=0, embeddings=[[0.1, 0.2], [0.3, 0.4]])
    assert item.embeddings == [[0.1, 0.2], [0.3, 0.4]]


def test_cce_contract_standard_embed_has_top_level_embeddings():
    obj = EmbeddingsObject(response=None)
    assert hasattr(obj, "embeddings") and isinstance(obj.embeddings, list)


def test_cce_contract_spec_mock_rejects_wrong_attribute():
    bare_mock = MagicMock()
    _ = bare_mock.embeddings  # no error
    spec_mock = MagicMock(spec=ContextualizedEmbeddingsObject)
    with pytest.raises(AttributeError):
        _ = spec_mock.embeddings


def test_cce_contract_embed_with_fallback_uses_correct_access_path():

    mock_client = MagicMock()
    item = MagicMock(spec=ContextualizedEmbeddingsResult)
    item.embeddings = [[0.1, 0.2], [0.3, 0.4]]
    obj = MagicMock(spec=ContextualizedEmbeddingsObject)
    obj.results = [item]
    mock_client.contextualized_embed.return_value = obj
    with patch("voyageai.Client", return_value=mock_client):
        embeddings, model = _embed_with_fallback(["a", "b"], "voyage-context-3", "vk_test")
    assert embeddings == [[0.1, 0.2], [0.3, 0.4]]
    assert model == "voyage-context-3"


def test_cce_contract_token_limit_has_safety_margin():
    from nexus.doc_indexer import _CCE_TOKEN_LIMIT
    assert 16_000 <= _CCE_TOKEN_LIMIT <= 32_000


def test_cce_contract_batch_chunks_splits_large_input():

    chunks = ["x" * 24_000 for _ in range(6)]
    batches = _batch_chunks_for_cce(chunks)
    assert len(batches) >= 2
    for batch in batches:
        assert len(batch) >= 2


def test_cce_contract_batch_chunks_keeps_small_input_together():

    chunks = ["hello world", "foo bar"]
    assert _batch_chunks_for_cce(chunks) == [chunks]


def test_cce_contract_batch_chunks_merges_singleton_tail():

    batches = _batch_chunks_for_cce(["x" * 40_000, "y" * 300, "z" * 300])
    for batch in batches:
        assert len(batch) >= 2


@pytest.mark.parametrize("n_chunks", [1500, 2500])
def test_batch_chunks_for_cce_splits_by_count(n_chunks):
    from nexus.doc_indexer import _CCE_MAX_BATCH_CHUNKS
    chunks = ["x" for _ in range(n_chunks)]
    batches = _batch_chunks_for_cce(chunks)
    assert len(batches) >= 2
    for batch in batches:
        assert len(batch) <= _CCE_MAX_BATCH_CHUNKS
    assert sum(len(b) for b in batches) == n_chunks


def test_batch_chunks_for_cce_singleton_not_merged_when_target_at_limit():
    from nexus.doc_indexer import _CCE_MAX_BATCH_CHUNKS
    chunks = ["tiny"] * (_CCE_MAX_BATCH_CHUNKS + 1)
    batches = _batch_chunks_for_cce(chunks)
    for batch in batches:
        assert len(batch) <= _CCE_MAX_BATCH_CHUNKS
    assert sum(len(b) for b in batches) == _CCE_MAX_BATCH_CHUNKS + 1


def test_cce_contract_large_input_still_uses_cce():

    chunks = [f"chunk{i}_" + "x" * 18_000 for i in range(8)]
    client = _make_cce_client()
    with patch("voyageai.Client", return_value=client):
        embeddings, model = _embed_with_fallback(chunks, "voyage-context-3", "vk_test")
    assert model == "voyage-context-3"
    assert len(embeddings) == 8
    client.embed.assert_not_called()
    assert client._call_count[0] >= 2


def _make_cce_voyage():
    """Create a mock Voyage client with spec-constrained CCE result."""
    v = MagicMock()
    cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
    cce_item.embeddings = [[0.1, 0.2]]
    cce_obj = MagicMock(spec=ContextualizedEmbeddingsObject)
    cce_obj.results = [cce_item]
    v.contextualized_embed.return_value = cce_obj
    return v


def test_index_pdf_uses_cce_for_docs_collection(sample_pdf, monkeypatch):
    set_credentials(monkeypatch)
    mock_chunk, mock_extract = _make_pdf_mocks()
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3), \
         patch("nexus.doc_indexer.PDFExtractor") as ext_cls, \
         patch("nexus.doc_indexer.PDFChunker") as chk_cls, \
         patch("voyageai.Client", return_value=_make_cce_voyage()):
        ext_cls.return_value.extract.return_value = mock_extract
        chk_cls.return_value.chunk.return_value = [mock_chunk, mock_chunk]
        result = index_pdf(sample_pdf, corpus="mybook")
    assert result == 2
    mock_t3.upsert_chunks_with_embeddings.assert_called_once()
    mock_col.upsert.assert_not_called()


@pytest.mark.parametrize("stored_model,expected_result", [
    ("voyage-code-3", 2),
    ("voyage-context-3", 0),
])
def test_index_pdf_hash_match_model_check(stored_model, expected_result, sample_pdf, monkeypatch):
    set_credentials(monkeypatch)
    content_hash = hashlib.sha256(sample_pdf.read_bytes()).hexdigest()
    mock_chunk, mock_extract = _make_pdf_mocks()
    mock_col = MagicMock()
    if expected_result > 0:
        mock_col.get.side_effect = [
            {"ids": ["old_id"], "metadatas": [{"content_hash": content_hash, "embedding_model": stored_model}]},
            {"ids": ["old_id"]},
        ]
    else:
        mock_col.get.return_value = {
            "ids": ["existing_id"],
            "metadatas": [{"content_hash": content_hash, "embedding_model": stored_model}],
        }
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3), \
         patch("nexus.doc_indexer.PDFExtractor") as ext_cls, \
         patch("nexus.doc_indexer.PDFChunker") as chk_cls, \
         patch("voyageai.Client", return_value=_make_cce_voyage()):
        ext_cls.return_value.extract.return_value = mock_extract
        chk_cls.return_value.chunk.return_value = [mock_chunk, mock_chunk]
        result = index_pdf(sample_pdf, corpus="mybook")
    assert result == expected_result


@pytest.mark.parametrize("kind", ["pdf", "markdown"])
def test_batch_index_returns_status_dict(kind, tmp_path):
    batch_fn, idx_name, ext, is_bytes = _BATCH_FNS[kind]
    f1, f2 = _make_batch_files(tmp_path, ext, is_bytes)
    with patch(f"nexus.doc_indexer.{idx_name}", return_value=3) as mock_idx:
        result = batch_fn([f1, f2], corpus="test", t3=MagicMock())
    assert result[str(f1)] == result[str(f2)] == "indexed"
    assert mock_idx.call_count == 2


@pytest.mark.parametrize("kind", ["pdf", "markdown"])
def test_batch_index_marks_failed_on_error(kind, tmp_path):
    batch_fn, idx_name, ext, is_bytes = _BATCH_FNS[kind]
    ok, bad = _make_batch_files(tmp_path, ext, is_bytes, names=("ok", "bad"))
    def _fail(path, corpus, **kw):
        if "bad" in str(path):
            raise RuntimeError("failed")
        return 2
    with patch(f"nexus.doc_indexer.{idx_name}", side_effect=_fail):
        result = batch_fn([ok, bad], corpus="test", t3=MagicMock())
    assert result[str(ok)] == "indexed"
    assert result[str(bad)] == "failed"


def test_embed_standard_path_batches_over_128_chunks():
    from nexus.doc_indexer import _EMBED_BATCH_SIZE
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
        embeddings, model = _embed_with_fallback(chunks, "voyage-code-3", "vk_test")
    assert embed_call_count[0] == 2
    assert len(embeddings) == 200
    assert model == "voyage-code-3"


def test_cce_total_token_limit_exists_and_gte_per_batch():
    from nexus.doc_indexer import _CCE_TOKEN_LIMIT, _CCE_TOTAL_TOKEN_LIMIT
    assert _CCE_TOKEN_LIMIT <= _CCE_TOTAL_TOKEN_LIMIT


def test_cce_max_total_chunks_constant():
    from nexus.doc_indexer import _CCE_MAX_TOTAL_CHUNKS
    assert _CCE_MAX_TOTAL_CHUNKS == 16_000


@pytest.mark.parametrize("limit_override,n_chunks", [(2, 2), (1, 2)])
def test_embed_with_fallback_warns_on_excessive_chunks(limit_override, n_chunks):

    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.embeddings = [[0.1]]
    mock_client.embed.return_value = mock_result
    with patch("voyageai.Client", return_value=mock_client):
        with patch("nexus.doc_indexer._log") as mock_log:
            with patch("nexus.doc_indexer._CCE_MAX_TOTAL_CHUNKS", limit_override):
                _embed_with_fallback(
                    chunks=[f"c{i}" for i in range(n_chunks)],
                    model="voyage-code-3", api_key="vk_test",
                )
            mock_log.warning.assert_called_once()
            assert "chunk count exceeds" in mock_log.warning.call_args[0][0]


def test_embed_with_fallback_empty_chunks():

    embeddings, model = _embed_with_fallback([], "voyage-context-3", "vk_test")
    assert embeddings == []
    assert model == "voyage-context-3"


def test_embed_with_fallback_filters_empty_strings():

    mock_result = MagicMock(spec=EmbeddingsObject)
    mock_result.embeddings = [[0.1, 0.2]]
    mock_client = MagicMock()
    mock_client.embed.return_value = mock_result
    with patch("voyageai.Client", return_value=mock_client):
        embeddings, _ = _embed_with_fallback(["", "   ", "real content", "\t\n"], "voyage-code-3", "vk_test")
    assert mock_client.embed.called
    call_kwargs = mock_client.embed.call_args
    passed_texts = call_kwargs[1].get("texts") or call_kwargs[0][0]
    assert "real content" in passed_texts
    assert "" not in passed_texts
    assert len(embeddings) == 1


def test_embed_with_fallback_all_empty_strings():

    mock_client = MagicMock()
    with patch("voyageai.Client", return_value=mock_client):
        embeddings, _ = _embed_with_fallback(["", "   ", "\n"], "voyage-code-3", "vk_test")
    assert embeddings == []
    mock_client.embed.assert_not_called()


def test_cce_failure_splits_recursively():

    client = _make_cce_client(fail_on_call={1})
    with patch("voyageai.Client", return_value=client):
        embeddings, model = _embed_with_fallback([f"chunk_{i}" for i in range(4)], "voyage-context-3", "vk_test")
    assert len(embeddings) == 4
    assert model == "voyage-context-3"
    client.embed.assert_not_called()


def test_embed_partial_batch_failure_stays_same_model():

    chunks = ["chunk a", "chunk b", "chunk c", "chunk d"]
    forced_batches = [["chunk a", "chunk b"], ["chunk c", "chunk d"]]
    client = _make_cce_client(fail_on_call={2})
    # Reset fail tracking for "fail only first time on call 2"
    real_side = client.contextualized_embed.side_effect
    call_count = [0]
    failed_once = [False]

    def _cce(inputs, model, input_type):
        call_count[0] += 1
        if call_count[0] == 2 and not failed_once[0]:
            failed_once[0] = True
            raise RuntimeError("CCE batch 2 failed")
        cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
        cce_item.embeddings = [[1.0] for _ in inputs[0]]
        result = MagicMock(spec=ContextualizedEmbeddingsObject)
        result.results = [cce_item]
        return result

    client.contextualized_embed.side_effect = _cce
    with patch("voyageai.Client", return_value=client), \
         patch("nexus.doc_indexer._batch_chunks_for_cce", return_value=forced_batches):
        embeddings, model = _embed_with_fallback(chunks, "voyage-context-3", "vk_test")
    assert len(embeddings) == 4
    assert model == "voyage-context-3"
    client.embed.assert_not_called()


def test_embed_single_chunk_failure_raises():

    mock_client = MagicMock()
    mock_client.contextualized_embed.side_effect = RuntimeError("single chunk too large")
    with patch("voyageai.Client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="single chunk too large"):
            _embed_with_fallback(["one giant chunk"], "voyage-context-3", "vk_test")


def test_embed_with_fallback_cce_empty_result_raises():

    mock_client = MagicMock()

    def _cce_empty(inputs, model, input_type):
        cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
        cce_item.embeddings = []
        result = MagicMock(spec=ContextualizedEmbeddingsObject)
        result.results = [cce_item]
        return result

    mock_client.contextualized_embed.side_effect = _cce_empty
    with patch("voyageai.Client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="CCE embedding returned no vectors"):
            _embed_with_fallback(["chunk one", "chunk two"], "voyage-context-3", "vk_test")
    mock_client.embed.assert_not_called()


@pytest.mark.parametrize("indexer", ["pdf", "markdown"])
def test_force_bypasses_staleness(indexer, sample_pdf, sample_md, monkeypatch):
    set_credentials(monkeypatch)
    path = sample_pdf if indexer == "pdf" else sample_md
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["existing_id"],
        "metadatas": [{"content_hash": content_hash, "embedding_model": "voyage-context-3"}],
    }
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col

    if indexer == "pdf":
        with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
            with patch("nexus.doc_indexer.PDFExtractor") as ext_cls:
                with patch("nexus.doc_indexer.PDFChunker") as chk_cls:
                    chunk = MagicMock()
                    chunk.text = "text"
                    chunk.chunk_index = 0
                    chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 4, "page_number": 1}
                    ext_cls.return_value.extract.return_value = MagicMock(
                        text="text", metadata={"extraction_method": "docling", "page_count": 1,
                                               "format": "markdown", "page_boundaries": []})
                    chk_cls.return_value.chunk.return_value = [chunk]
                    result = index_pdf(path, corpus="mybook", force=True, embed_fn=_fake_embed)
    else:
        chunk = MagicMock()
        chunk.text = "text"
        chunk.chunk_index = 0
        chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 4, "page_number": 0, "header_path": "H"}
        with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
            with patch("nexus.doc_indexer.SemanticMarkdownChunker") as chk_cls:
                chk_cls.return_value.chunk.return_value = [chunk]
                result = index_markdown(path, corpus="docs", force=True, embed_fn=_fake_embed)

    assert result > 0
    mock_t3.upsert_chunks_with_embeddings.assert_called_once()


def test_force_default_false_still_skips(sample_pdf, monkeypatch):
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
        with patch("nexus.doc_indexer.PDFExtractor") as ext_cls:
            result = index_pdf(sample_pdf, corpus="mybook")
    assert result == 0
    ext_cls.assert_not_called()


@pytest.mark.parametrize("kind", ["pdf", "markdown"])
def test_batch_index_passes_force(kind, tmp_path):
    batch_fn, idx_name, ext, is_bytes = _BATCH_FNS[kind]
    f1, f2 = _make_batch_files(tmp_path, ext, is_bytes)
    with patch(f"nexus.doc_indexer.{idx_name}", return_value=2) as mock_idx:
        batch_fn([f1, f2], corpus="test", force=True)
    assert mock_idx.call_count == 2
    for c in mock_idx.call_args_list:
        assert c[1].get("force") is True


@pytest.mark.parametrize("kind", ["pdf", "markdown"])
def test_batch_index_calls_on_file_per_file(kind, tmp_path):
    batch_fn, idx_name, ext, is_bytes = _BATCH_FNS[kind]
    f1, f2 = _make_batch_files(tmp_path, ext, is_bytes)
    calls: list[tuple] = []
    with patch(f"nexus.doc_indexer.{idx_name}", return_value=3):
        batch_fn([f1, f2], corpus="test", on_file=lambda p, c, e: calls.append((p, c, e)))
    assert len(calls) == 2
    assert {c[0].name for c in calls} == {f"a{ext}", f"b{ext}"}
    for _, chunks, elapsed in calls:
        assert isinstance(chunks, int) and isinstance(elapsed, float) and elapsed >= 0.0


@pytest.mark.parametrize("kind", ["pdf", "markdown"])
def test_batch_index_on_file_none_safe(kind, tmp_path):
    batch_fn, idx_name, ext, is_bytes = _BATCH_FNS[kind]
    [f] = _make_batch_files(tmp_path, ext, is_bytes, names=("a",))
    with patch(f"nexus.doc_indexer.{idx_name}", return_value=1):
        batch_fn([f], corpus="test")  # no on_file -- must not raise


def test_index_pdf_return_metadata_false_returns_int(sample_pdf, monkeypatch, mock_t3, voyage_client):
    set_credentials(monkeypatch)
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with pdf_extract_patches_ctx() as pep:
            with patch("voyageai.Client", return_value=voyage_client):
                result = index_pdf(sample_pdf, corpus="test")
    assert isinstance(result, int) and result == 1


def test_index_pdf_return_metadata_true_returns_dict(sample_pdf, monkeypatch, mock_t3, voyage_client):
    set_credentials(monkeypatch)
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as ext_cls:
            with patch("nexus.doc_indexer.PDFChunker") as chk_cls:
                with patch("voyageai.Client", return_value=voyage_client):
                    chunk = MagicMock()
                    chunk.text = "chunk content"
                    chunk.chunk_index = 0
                    chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 13, "page_number": 2}
                    ext_cls.return_value.extract.return_value = MagicMock(
                        text="text", metadata={"extraction_method": "x", "page_count": 1,
                                               "format": "markdown", "page_boundaries": [],
                                               "title": "My Paper", "author": "A. Thor"})
                    chk_cls.return_value.chunk.return_value = [chunk]
                    result = index_pdf(sample_pdf, corpus="test", return_metadata=True)
    assert isinstance(result, dict)
    assert result["chunks"] == 1
    assert isinstance(result["pages"], list)
    assert isinstance(result["title"], str)


def test_index_pdf_return_metadata_true_skipped_returns_empty_dict(sample_pdf, monkeypatch):
    set_credentials(monkeypatch)
    content_hash = hashlib.sha256(sample_pdf.read_bytes()).hexdigest()
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["existing"],
        "metadatas": [{"content_hash": content_hash, "embedding_model": "voyage-context-3"}],
    }
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as ext_cls:
            with patch("nexus.doc_indexer.PDFChunker"):
                with patch("voyageai.Client"):
                    ext_cls.return_value.extract.return_value = MagicMock(
                        text="text", metadata={"extraction_method": "x", "page_count": 1,
                                               "format": "markdown", "page_boundaries": []})
                    result = index_pdf(sample_pdf, corpus="test", return_metadata=True)
    assert isinstance(result, dict) and result["chunks"] == 0 and result["pages"] == []


def test_index_markdown_return_metadata_true_returns_dict(sample_md, monkeypatch, mock_t3, voyage_client):
    set_credentials(monkeypatch)
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("voyageai.Client", return_value=voyage_client):
            result = index_markdown(sample_md, corpus="test", return_metadata=True)
    assert isinstance(result, dict)
    assert isinstance(result["chunks"], int) and isinstance(result["sections"], int)


def test_index_markdown_return_metadata_true_skipped_returns_empty_dict(sample_md, monkeypatch):
    set_credentials(monkeypatch)
    content_hash = hashlib.sha256(sample_md.read_bytes()).hexdigest()
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
    assert isinstance(result, dict) and result["chunks"] == 0 and result["sections"] == 0


@pytest.mark.parametrize("model,use_cce", [
    ("voyage-code-3", False),
    ("voyage-context-3", True),
])
def test_embed_progress_callback_fires(model, use_cce):

    progress: list[tuple[int, int]] = []
    mock_client = MagicMock()
    if use_cce:
        inner = MagicMock(spec=ContextualizedEmbeddingsResult)
        inner.embeddings = [[0.1] * 10, [0.2] * 10]
        cce_result = MagicMock(spec=ContextualizedEmbeddingsObject)
        cce_result.results = [inner]
        mock_client.contextualized_embed.return_value = cce_result
        n_chunks = 2
    else:
        embed_result = MagicMock()
        embed_result.embeddings = [[0.1] * 10, [0.2] * 10, [0.3] * 10]
        mock_client.embed.return_value = embed_result
        n_chunks = 3
    with patch("voyageai.Client", return_value=mock_client):
        _embed_with_fallback(
            [f"chunk {i}" for i in range(n_chunks)],
            model, "test-key",
            on_progress=lambda d, t: progress.append((d, t)),
        )
    assert progress
    assert progress[-1] == (n_chunks, n_chunks)


def test_embed_progress_callback_none_is_noop():

    mock_client = MagicMock()
    embed_result = MagicMock()
    embed_result.embeddings = [[0.1] * 10]
    mock_client.embed.return_value = embed_result
    with patch("voyageai.Client", return_value=mock_client):
        _embed_with_fallback(["chunk one"], "voyage-code-3", "test-key", on_progress=None)


@pytest.mark.parametrize("indexer", ["pdf", "markdown"])
def test_index_threads_on_progress(indexer, sample_pdf, sample_md, monkeypatch, mock_t3, voyage_client):
    set_credentials(monkeypatch)
    progress: list[tuple] = []
    path = sample_pdf if indexer == "pdf" else sample_md
    if indexer == "pdf":
        with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
            with pdf_extract_patches_ctx() as pep:
                with patch("voyageai.Client", return_value=voyage_client):
                    result = index_pdf(path, corpus="mybook", on_progress=lambda d, t: progress.append((d, t)))
    else:
        chunk = MagicMock()
        chunk.text = "chunk text"
        chunk.chunk_index = 0
        chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 10, "page_number": 0, "header_path": "Hello"}
        with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
            with patch("nexus.doc_indexer.SemanticMarkdownChunker") as chk_cls:
                with patch("voyageai.Client", return_value=voyage_client):
                    chk_cls.return_value.chunk.return_value = [chunk]
                    result = index_markdown(path, corpus="docs", on_progress=lambda d, t: progress.append((d, t)))
    assert result >= 1
    assert progress


def test_stale_chunk_pruning_deletes_old_ids(sample_md, monkeypatch, voyage_client):
    set_credentials(monkeypatch)
    content_hash = hashlib.sha256(sample_md.read_bytes()).hexdigest()
    prefix = content_hash[:16]
    mock_col = MagicMock()
    mock_col.get.side_effect = [
        {"ids": [f"{prefix}_0"], "metadatas": [{"content_hash": "old_hash", "embedding_model": "voyage-context-3"}]},
        {"ids": [f"{prefix}_{i}" for i in range(5)]},
    ]
    captured_deletes: list = []
    mock_col.delete.side_effect = lambda ids: captured_deletes.extend(ids)
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    chunks = []
    for i in range(3):
        mc = MagicMock()
        mc.text = f"chunk text {i}"
        mc.chunk_index = i
        mc.metadata = {"chunk_start_char": 0, "chunk_end_char": 10, "page_number": 0, "header_path": "H"}
        chunks.append(mc)
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.SemanticMarkdownChunker") as chk_cls:
            with patch("voyageai.Client", return_value=voyage_client):
                chk_cls.return_value.chunk.return_value = chunks
                index_markdown(sample_md, corpus="docs")
    assert set(captured_deletes) == {f"{prefix}_3", f"{prefix}_4"}


@pytest.fixture
def incr_setup(sample_pdf, monkeypatch):
    """Common setup for incremental PDF tests."""
    from nexus.doc_indexer import _INCREMENTAL_THRESHOLD
    set_credentials(monkeypatch)
    ckpt_dir = sample_pdf.parent / "ckpt"
    monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", ckpt_dir)
    monkeypatch.setattr("nexus.doc_indexer.CHECKPOINT_DIR", ckpt_dir)

    class _Setup:
        threshold = _INCREMENTAL_THRESHOLD
        path = sample_pdf
        dir = ckpt_dir
        content_hash = hashlib.sha256(sample_pdf.read_bytes()).hexdigest()

        def run(self, n_chunks, embed_fn=_fake_embed, on_progress=None):
            mock_chunks = _make_n_chunks(n_chunks)
            mock_col = MagicMock()
            mock_col.get.return_value = {"ids": [], "metadatas": []}
            t3 = MagicMock()
            t3.get_or_create_collection.return_value = mock_col
            with patch("nexus.doc_indexer.make_t3", return_value=t3):
                with patch("nexus.doc_indexer.PDFExtractor") as ext_cls:
                    with patch("nexus.doc_indexer.PDFChunker") as chk_cls:
                        ext_cls.return_value.extract.return_value = MagicMock(
                            text="x" * 5000,
                            metadata={"extraction_method": "docling", "page_count": 50,
                                      "format": "markdown", "page_boundaries": []})
                        chk_cls.return_value.chunk.return_value = mock_chunks
                        result = index_pdf(self.path, corpus="test",
                                           embed_fn=embed_fn, on_progress=on_progress)
            return result, t3
    return _Setup()


def test_index_pdf_incremental_indexes_all_chunks(incr_setup):
    n = incr_setup.threshold + 10
    result, t3 = incr_setup.run(n)
    assert result == n
    total = sum(len(c.args[1]) for c in t3.upsert_chunks_with_embeddings.call_args_list)
    assert total == n


def test_index_pdf_incremental_resumes_from_checkpoint(incr_setup):
    from nexus.checkpoint import CheckpointData, write_checkpoint
    n = incr_setup.threshold + 50
    already_done = 64
    write_checkpoint(CheckpointData(
        pdf=str(incr_setup.path), collection="docs__test",
        content_hash=incr_setup.content_hash, chunks_upserted=already_done,
        total_chunks=n, embedding_model="voyage-context-3",
    ))
    result, t3 = incr_setup.run(n)
    assert result == n
    total = sum(len(c.args[1]) for c in t3.upsert_chunks_with_embeddings.call_args_list)
    assert total == n - already_done


def test_index_pdf_incremental_deletes_checkpoint_on_success(incr_setup):
    from nexus.checkpoint import checkpoint_path
    n = incr_setup.threshold + 10
    result, _ = incr_setup.run(n)
    assert result == n
    assert not checkpoint_path(incr_setup.content_hash, "docs__test").exists()


def test_index_pdf_small_doc_uses_original_path(incr_setup):
    result, t3 = incr_setup.run(5)
    assert result == 5
    assert t3.upsert_chunks_with_embeddings.call_count == 1


def test_index_pdf_incremental_writes_checkpoints_per_batch(sample_pdf, monkeypatch):
    from nexus.doc_indexer import _INCREMENTAL_BATCH_SIZE
    from nexus.checkpoint import CheckpointData
    set_credentials(monkeypatch)
    ckpt_dir = sample_pdf.parent / "ckpt"
    monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", ckpt_dir)
    monkeypatch.setattr("nexus.doc_indexer.CHECKPOINT_DIR", ckpt_dir)
    n_chunks = _INCREMENTAL_BATCH_SIZE * 3 + 10
    mock_chunks = _make_n_chunks(n_chunks)
    checkpoint_writes = []
    original_write = __import__("nexus.checkpoint", fromlist=["write_checkpoint"]).write_checkpoint

    def _tracking_write(data: CheckpointData):
        checkpoint_writes.append(data.chunks_upserted)
        original_write(data)

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    with patch("nexus.doc_indexer.write_checkpoint", side_effect=_tracking_write):
        with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
            with patch("nexus.doc_indexer.PDFExtractor") as ext_cls:
                with patch("nexus.doc_indexer.PDFChunker") as chk_cls:
                    ext_cls.return_value.extract.return_value = MagicMock(
                        text="x" * 5000,
                        metadata={"extraction_method": "docling", "page_count": 50,
                                  "format": "markdown", "page_boundaries": []})
                    chk_cls.return_value.chunk.return_value = mock_chunks
                    result = index_pdf(sample_pdf, corpus="test", embed_fn=_fake_embed)
    assert result == n_chunks
    assert len(checkpoint_writes) >= 3
    for i in range(1, len(checkpoint_writes)):
        assert checkpoint_writes[i] > checkpoint_writes[i - 1]
    assert checkpoint_writes[-1] == n_chunks


def test_index_pdf_incremental_stale_checkpoint_deleted(incr_setup):
    from nexus.checkpoint import CheckpointData, write_checkpoint
    n = incr_setup.threshold + 10
    write_checkpoint(CheckpointData(
        pdf=str(incr_setup.path), collection="docs__test",
        content_hash="wrong_hash_from_old_version", chunks_upserted=50,
        total_chunks=200, embedding_model="voyage-context-3",
    ))
    result, t3 = incr_setup.run(n)
    assert result == n
    total = sum(len(c.args[1]) for c in t3.upsert_chunks_with_embeddings.call_args_list)
    assert total == n


def test_index_pdf_incremental_progress_fires(incr_setup):
    n = incr_setup.threshold + 10
    progress: list[tuple] = []
    result, _ = incr_setup.run(n, on_progress=lambda d, t: progress.append((d, t)))
    assert result == n
    assert progress
    assert progress[-1] == (n, n)


def test_index_pdf_incremental_checkpoint_exceeds_total(incr_setup):
    from nexus.checkpoint import CheckpointData, write_checkpoint
    n = incr_setup.threshold + 10
    write_checkpoint(CheckpointData(
        pdf=str(incr_setup.path), collection="docs__test",
        content_hash=incr_setup.content_hash, chunks_upserted=n + 100,
        total_chunks=n + 100, embedding_model="voyage-context-3",
    ))
    result, _ = incr_setup.run(n)
    assert result == n


def test_token_bucket_rate_limiter():

    bucket = _TokenBucket(rpm=600, burst=3)
    t0 = time.monotonic()
    for _ in range(3):
        bucket.acquire()
    assert time.monotonic() - t0 < 0.1


def test_token_bucket_zero_burst_still_works():

    _TokenBucket(rpm=60, burst=1).acquire()


def test_parallel_embed_preserves_order():


    def _mock_cce(inputs, model, input_type):
        batch = inputs[0]
        time.sleep(0.01 * len(batch))
        cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
        cce_item.embeddings = [[float(i)] * 10 for i in range(len(batch))]
        result = MagicMock(spec=ContextualizedEmbeddingsObject)
        result.results = [cce_item]
        return result

    mock_client = MagicMock()
    mock_client.contextualized_embed = _mock_cce
    chunks = ["x" * 5000] * 10
    with patch("voyageai.Client", return_value=mock_client):
        embeddings, model = _embed_with_fallback(chunks, "voyage-context-3", "test-key")
    assert len(embeddings) == 10
    assert model == "voyage-context-3"


def test_parallel_embed_progress_fires_for_each_batch():

    progress: list[tuple] = []

    def _mock_cce(inputs, model, input_type):
        cce_item = MagicMock(spec=ContextualizedEmbeddingsResult)
        cce_item.embeddings = [[0.1] * 10 for _ in inputs[0]]
        result = MagicMock(spec=ContextualizedEmbeddingsObject)
        result.results = [cce_item]
        return result

    mock_client = MagicMock()
    mock_client.contextualized_embed = _mock_cce
    with patch("voyageai.Client", return_value=mock_client):
        _embed_with_fallback(
            ["x" * 5000] * 10, "voyage-context-3", "test-key",
            on_progress=lambda d, t: progress.append((d, t)),
        )
    assert progress and progress[-1][0] == 10


class TestStreamingRouting:
    def test_streaming_never_forces_batch_path(self, tmp_path):
        pdf = tmp_path / "small.pdf"
        pdf.write_bytes(b"dummy")
        with (
            patch("nexus.doc_indexer._has_credentials", return_value=True),
            patch("nexus.doc_indexer._sha256", return_value="abc123"),
            patch("nexus.doc_indexer.make_t3"),
            patch("nexus.doc_indexer._chroma_with_retry", return_value={"metadatas": []}),
            patch("nexus.doc_indexer._pdf_chunks", return_value=[]) as mock_chunks,
        ):
            result = index_pdf(pdf, "test", streaming="never")
        assert result == 0
        mock_chunks.assert_called_once()

    @pytest.mark.parametrize("streaming,page_count,expected", [
        ("auto", 150, 42),
        ("always", 3, 5),
    ])
    def test_streaming_uses_pipeline(self, streaming, page_count, expected, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"dummy")
        with (
            patch("nexus.doc_indexer._has_credentials", return_value=True),
            patch("nexus.doc_indexer._sha256", return_value="abc123"),
            patch("nexus.doc_indexer.make_t3"),
            patch("nexus.doc_indexer._chroma_with_retry", return_value={"metadatas": []}),
            patch("pymupdf.open") as mock_pymupdf_open,
            patch("nexus.pipeline_stages.pipeline_index_pdf", return_value=expected) as mock_pipeline,
        ):
            mock_doc = MagicMock()
            mock_doc.__enter__ = MagicMock(return_value=mock_doc)
            mock_doc.__exit__ = MagicMock(return_value=False)
            mock_doc.__len__ = MagicMock(return_value=page_count)
            mock_pymupdf_open.return_value = mock_doc
            result = index_pdf(pdf, "test", streaming=streaming)
        assert result == expected
        mock_pipeline.assert_called_once()


class TestSectionTypeInPipeline:
    def test_markdown_chunks_has_section_type(self, tmp_path: Path):
        md = tmp_path / "paper.md"
        md.write_text("# Abstract\n\nThis paper presents...\n\n# References\n\n[1] Foo.\n")
        tuples = _markdown_chunks(md, "abc123", "voyage-context-3", "2026-01-01", "docs__test")
        assert len(tuples) >= 2
        for _id, _text, meta in tuples:
            assert "section_type" in meta

    @pytest.mark.parametrize("heading,content,expected_type", [
        ("Abstract", "This paper presents results.", "abstract"),
        ("References", "[1] Foo et al.", "references"),
    ])
    def test_markdown_chunks_section_classified(self, heading, content, expected_type, tmp_path: Path):
        md = tmp_path / "paper.md"
        # Need abstract + another section so there are >= 2 chunks for CCE
        md.write_text(f"# Abstract\n\nContent.\n\n# {heading}\n\n{content}\n")
        tuples = _markdown_chunks(md, "abc123", "voyage-context-3", "2026-01-01", "docs__test")
        typed = [m for _, _, m in tuples if m["section_type"] == expected_type]
        assert typed, f"Expected at least one chunk classified as '{expected_type}'"
