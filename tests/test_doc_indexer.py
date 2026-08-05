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
    _batch_chunks_for_cce, _embed_with_fallback, _identity_where,
    _lookup_existing_doc_id, _markdown_chunks, _TokenBucket,
    batch_index_markdowns, batch_index_pdfs, index_markdown, index_pdf,
)
from tests._catalog_fixture_ops import ActiveCatalog, documents_by_file_path
from tests.conftest import set_credentials
from tests.conftest import make_vector_test_client


# RDR-109 Phase 2: local-token in test collection names varies by whether
# fastembed is installed (tier 1: bge-base-en-v15-768, else tier 0:
# minilm-l6-v2-384). Resolve at runtime so the suite is deterministic on
# CI (no fastembed) and dev machines (fastembed pulled by experiments).


@pytest.fixture(autouse=True)
def _fake_pipeline_engine(monkeypatch):
    """RDR-186 .16: the streaming path's default buffer is the engine-backed
    HttpPipelineDB; wire it to the in-memory fake (one engine per test, shared
    across same-test index_pdf calls so resume/staleness state persists)."""
    from tests.pipeline_fake_engine import make_fake_engine_db

    db, engine = make_fake_engine_db()
    monkeypatch.setattr("nexus.pipeline_stages.HttpPipelineDB", lambda: db)
    return engine

def _local_token() -> str:
    from nexus.db.local_ef import local_model_token
    return local_model_token()


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


@pytest.fixture(autouse=True)
def _no_propagating_fence_complete(monkeypatch):
    """nexus-tp8yk D2a: this file's T3 doubles (``mock_t3``,
    ``make_vector_test_client()``) never write to the real engine's
    pgvector — but every test here DOES resolve a real catalog doc_id
    (the T2-everywhere autouse engine substrate), so the completion
    stamp's fail-closed verify (which compares the manifest against the
    REAL engine's T3, which never received these chunks) now refuses on
    every fenced single-flush/incremental/pipeline run this file drives.
    That refusal is technically accurate in isolation but irrelevant to
    what this file actually tests — content/metadata/hook behavior, not
    the RUNFENCE completion contract itself, which
    ``test_5xn3k_fence_ordering.py`` owns via its own dedicated
    ``_RecordingFenceWriter`` doubles and
    ``tests/db/test_5xn3k_runfence_gate.py`` owns via a real,
    substrate-matched engine. Stub to a no-op here, mirroring
    ``_fence_begin``/``_fence_fail``'s existing advisory-only
    (never-raises) behavior — this file was green before nexus-tp8yk made
    the stamp propagate, and nothing about that substrate mismatch is
    what any of these tests are pinning.
    """
    monkeypatch.setattr("nexus.doc_indexer._fence_complete", lambda *a, **kw: None)


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


# ── nexus-dcym: doc_id-keyed identity helpers ──────────────────────────────


def test_lookup_existing_doc_id_returns_empty_when_catalog_is_none(tmp_path):
    """Catalog uninitialised (caller passes None) → "". Caller falls back."""
    assert _lookup_existing_doc_id(None, "/some/file.pdf", "any-corpus") == ""


def test_lookup_existing_doc_id_finds_registered_entry(tmp_path):
    """When the catalog already registered *file_path* under *corpus*'s
    owner, the helper returns the tumbler stringified as the doc_id."""
    # nexus-i711w: seeds through ActiveCatalog (live catalog) — the local
    # Catalog.init arm is gone.
    cat = ActiveCatalog()
    owner = cat.register_owner("mybook", "curator")
    file_path = "/abs/path/paper.pdf"
    doc = cat.register(
        owner, "Paper Title", content_type="paper",
        file_path=file_path, corpus="mybook",
        physical_collection="docs__mybook",
    )

    result = _lookup_existing_doc_id(cat, file_path, "mybook")
    assert result == str(doc)


def test_identity_where_prefers_content_hash_post_phase_3(tmp_path, monkeypatch):
    """RDR-108 Phase 3: ``doc_id`` was retired from chunk metadata.
    ``_identity_where`` no longer keys on doc_id (the catalog manifest
    is authoritative for doc-to-chunk binding); when ``content_hash``
    is supplied it becomes the staleness key, otherwise ``source_path``
    is the legacy fallback for pruning sites pending Phase 4.
    """
    # nexus-i711w: seeds through ActiveCatalog (live catalog) — the local
    # Catalog.init arm is gone; no catalog_path repoint needed.
    cat = ActiveCatalog()
    owner = cat.register_owner("mybook", "curator")
    file_path = "/abs/path/paper.pdf"
    cat.register(
        owner, "Paper", content_type="paper", file_path=file_path,
        corpus="mybook", physical_collection="docs__mybook",
    )

    # With content_hash → content_hash branch (the new staleness key).
    where = _identity_where(file_path, "mybook", content_hash="abcd")
    assert where == {"content_hash": "abcd"}

    # Without content_hash → source_path branch (legacy / pruning).
    where_legacy = _identity_where(file_path, "mybook")
    assert where_legacy == {"source_path": file_path}


def test_identity_where_falls_back_when_corpus_owner_missing(tmp_path, monkeypatch):
    """No content_hash → source_path fallback."""
    # nexus-i711w: no local catalog to init; the live catalog simply has no
    # owner for this corpus, which is the case under test.
    where = _identity_where("/abs/path/x.pdf", "missing-corpus")
    assert where == {"source_path": "/abs/path/x.pdf"}


class TestCatalogMarkdownHookEphemeralPathGuard:
    """nexus-u8n4r: ``_catalog_markdown_hook`` refuses a brand-new
    registration when the stored ``file_path`` (absolute here, since no
    ``base_path`` is threaded — the shape ``nx collection reindex`` and
    the standalone RDR-only index command use) sits under an agent
    worktree marker, UNLESS the resolving owner's own ``repo_root`` is
    itself rooted there. See
    ``nexus.repo_identity.should_skip_ephemeral_registration``.

    The hook's default owner is a "curator" (``owner_type="curator"``)
    which normally carries an EMPTY ``repo_root`` — a documented residual
    where the guard never fires. To exercise the owner-root exception
    branch these tests pre-register the curator owner with an explicit
    ``repo_root`` (the catalog protocol accepts it for any owner_type),
    modelling an operator-configured corpus owner rather than the
    hook's own default.
    """

    def test_worktree_marker_path_with_clean_owner_root_is_skipped(
        self, tmp_path, monkeypatch,
    ):
        from nexus.doc_indexer import _catalog_markdown_hook
        from nexus.repo_identity import is_worktree_or_tempdir_path

        # Linux/CI-vs-macOS divergence (nexus-u8n4r CI red, 2026-08-03): on
        # Linux, pytest's ``tmp_path`` lives under ``/tmp/``, matching
        # ``_TEMP_DIR_PREFIXES`` — this test's owner root would look
        # ephemeral too, the owner-root exception would exempt the
        # registration, and the guard would never fire. Force a non-tmp-
        # shaped prefix set so the owner root reads as clean on BOTH
        # platforms — this test is about the WORKTREE MARKER, not the
        # temp-prefix rule. Do not strip this patch; see nexus-u8n4r CI
        # run 30850463195.
        monkeypatch.setattr(
            "nexus.repo_identity._TEMP_DIR_PREFIXES", ("/nonexistent-tmp-prefix/",),
        )

        cat = ActiveCatalog()
        corpus = "u8n4r-md-clean"
        clean_root = str(tmp_path / "clean-repo")
        # Non-vacuity: prove the premise (clean owner root) instead of
        # inheriting it from whichever platform happens to be running.
        assert not is_worktree_or_tempdir_path(clean_root)
        cat.register_owner(corpus, "curator", repo_root=clean_root)

        md_path = (
            Path(clean_root) / ".claude" / "worktrees"
            / "agent-z" / "docs" / "rdr" / "rdr-999.md"
        )

        import structlog.testing
        with structlog.testing.capture_logs() as logs:
            _catalog_markdown_hook(
                md_path, "rdr__u8n4r-md-clean", "rdr", corpus, 3,
            )

        assert documents_by_file_path(str(md_path)) == []
        skipped = [
            log_entry for log_entry in logs
            if log_entry.get("event") == "ephemeral_path_registration_skipped"
        ]
        assert len(skipped) == 1
        assert skipped[0]["path"] == str(md_path)

    def test_worktree_marker_path_with_owner_rooted_in_tmp_is_registered(
        self, tmp_path,
    ):
        from nexus.doc_indexer import _catalog_markdown_hook

        cat = ActiveCatalog()
        corpus = "u8n4r-md-tmp"
        tmp_root = "/tmp/nexus-u8n4r-md-sandbox"
        cat.register_owner(corpus, "curator", repo_root=tmp_root)

        md_path = (
            Path(tmp_root) / ".claude" / "worktrees"
            / "agent-z" / "docs" / "rdr" / "rdr-999.md"
        )

        _catalog_markdown_hook(
            md_path, "rdr__u8n4r-md-tmp", "rdr", corpus, 3,
        )

        docs = documents_by_file_path(str(md_path))
        assert len(docs) == 1


def test_batch_index_markdowns_skips_malformed_frontmatter_and_continues(
    tmp_path, monkeypatch,
):
    """nexus-qr9d: malformed YAML frontmatter on one file must not abort
    the batch or hang the post-pass. The offending file is marked
    ``failed`` with its path; sibling files complete normally; the whole
    call returns within a wall-clock bound."""
    from nexus.db.t3 import T3Database

    # nexus-i711w: no local catalog init — the live catalog needs none.
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    monkeypatch.setattr(
        "nexus.config._global_config_path", lambda: Path("/nonexistent"),
    )

    good = tmp_path / "good.md"
    good.write_text(
        "---\ntitle: Good\n---\n\n# Good\n\nContent here.\n",
        encoding="utf-8",
    )
    # YAML flow sequence with unquoted #-refs — the exact hazard from the bead.
    bad = tmp_path / "bad.md"
    bad.write_text(
        "---\nprs: [#381, #382]\nstatus: post-mortem\n---\n\n# Body\n\nText.\n",
        encoding="utf-8",
    )

    client = make_vector_test_client()
    t3 = T3Database(_client=client, local_mode=True)

    t0 = time.monotonic()
    results = batch_index_markdowns(
        [bad, good], corpus="qr9d-test", t3=t3,
        collection_name=f"rdr__qr9d-test__{_local_token()}__v1",
        content_type="rdr",
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 30.0, f"batch hung or far too slow ({elapsed:.1f}s)"
    assert results[str(bad)] == "failed"
    assert results[str(good)] == "indexed"


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

    RDR-102 D2: source_path was removed from the chunk schema, so the
    re-index no-op contract now relies on the doc_id-keyed staleness
    check (chunks must carry doc_id for identity_where to find them).
    The test initializes a catalog at the autouse-fixture path so the
    Phase A pre-flight registration writes doc_id at chunk-write time;
    without that, no-catalog ingest writes chunks with neither
    source_path nor doc_id and the staleness check correctly cannot
    detect "unchanged" — re-index would proceed every time.

    nexus-5xn3k.3 (RUNFENCE): the fence's "unknown state" fallback
    (``_manifest_is_fully_present``) now asks the catalog SERVICE's own
    ``manifest/verify`` — an engine-side SQL anti-join that only sees chunks
    living in THAT SAME Postgres (design memo §3.2: "post-RDR-155 both
    tables live in the same Postgres"). This test injects a raw ephemeral
    ChromaDB client as *t3* specifically to avoid needing a full
    pgvector-backed vector store for a local-embedder unit test — a
    deliberately DECOUPLED T3 substrate the real production topology never
    has. Against a real (test-scoped) service catalog, the engine's verify
    correctly reports every chunk "missing" (it was never written to the
    engine's own tables), which would force a spurious re-index and is
    ORTHOGONAL to what this test actually verifies (the local-embedder
    fallback + doc_id-keyed identity match). Bypassed here; the RUNFENCE
    mechanism itself has its own dedicated coverage
    (tests/test_5xn3k_staleness_three_way.py et al.).

    nexus-5xn3k.4: the ``_manifest_is_fully_present`` bypass above is only
    reached via ``_index_run_fresh``'s "unknown state" fallback — but since
    .4 wires up a REAL ``_fence_begin``/``_fence_complete`` call against the
    live catalog, the fence state for this doc_id would genuinely become
    "indexing" (begin succeeds; complete is refused against this same
    decoupled T3 for the same reason as the paragraph above) and
    ``_index_run_fresh`` would short-circuit via its "indexing" branch
    BEFORE ever consulting ``_manifest_is_fully_present``. Stub both fence
    calls too so the fence stays "unknown" here, matching this test's
    stated intent of bypassing RUNFENCE entirely.
    """
    monkeypatch.setattr(
        "nexus.doc_indexer._manifest_is_fully_present", lambda *a, **k: True,
    )
    monkeypatch.setattr("nexus.doc_indexer._fence_begin", lambda *a, **k: None)
    monkeypatch.setattr("nexus.doc_indexer._fence_complete", lambda *a, **k: None)
    # nexus-i711w: no local catalog init — pre-flight registration goes to
    # the live catalog.
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    monkeypatch.setattr(
        "nexus.config._global_config_path", lambda: Path("/nonexistent"),
    )
    # is_local_mode() returns True when either key is absent; with
    # both keys cleared above it's True without an explicit NX_LOCAL.

    # Inject an EphemeralClient so the test doesn't hit a real
    # PersistentClient on disk.
    client = make_vector_test_client()
    from nexus.db.t3 import T3Database
    local_t3 = T3Database(_client=client, local_mode=True)

    n = index_markdown(sample_md, corpus="local-fallback-test", t3=local_t3)
    assert n > 0, (
        f"local-mode markdown index should produce chunks; got {n}. "
        f"This is the GH #336 contract: ingestion works without keys "
        f"in local mode."
    )

    # Verify chunks landed AND were tagged with the local model name
    # (not voyage-context-3 — staleness check on re-run depends on it).
    col = local_t3.get_or_create_collection(
        f"docs__local-fallback-test__{_local_token()}__v1",
    )
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
    n2 = index_markdown(sample_md, corpus="local-fallback-test", t3=local_t3)
    assert n2 == 0, (
        f"re-index against unchanged content should be a no-op; got {n2}. "
        f"If this fails, the staleness check is comparing the local "
        f"actual_model against the cloud target_model from "
        f"index_model_for_collection — see local_target_model override."
    )


# test_index_markdown_auto_inits_catalog_when_absent_and_prunes_on_reindex
# retired (nexus-i711w terminal deletion): its subject was the SQLite-
# opt-out-mode-only catalog auto-init branch of _register_or_lookup_doc_id,
# which died with the local catalog (service mode never fires it).


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
    # RDR-155 P4b: chroma_api_key died with the chroma credential map — the
    # voyage key is the only cloud-ingest credential named now.
    assert "chroma_api_key" not in str(excinfo.value)
    assert "NX_LOCAL" in str(excinfo.value)


def test_index_pdf_skips_if_hash_unchanged(sample_pdf, monkeypatch, cloud_mode):
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


def test_index_pdf_prune_union_guard_wired_at_call_site(
    sample_pdf, monkeypatch, voyage_client,
) -> None:
    """nexus-tp8yk D3 substantive-critic SIGNIFICANT (nexus-tp8yk-
    substantive-critique-2026-08-04): proves the D3 union guard is
    actually WIRED at index_pdf's small-doc-branch prune block — i.e.
    that ``prune_orphan_candidates``/``orphaned_chashes`` are the real
    gate between "T3 reports a stale id" and "col.delete gets called" —
    at the PRODUCTION entry point (``index_pdf``), not just via the
    helper's own unit tests (tests/test_indexer_utils_prune_orphan_
    candidates.py) or the real-engine helper test (tests/db/
    test_http_catalog_integration.py::TestPruneUnionGuard).

    DISCOVERED while building this test, documented rather than silently
    absorbed: ``_identity_where``'s ``source_path`` fallback (used by
    this exact prune query) filters on a chunk-metadata field RDR-102 D2
    hard-removed from ``make_chunk_metadata`` — every PDF/markdown chunk
    ``doc_indexer.py`` writes in real production carries NO
    ``source_path`` at all, so this specific ``col.get(where={"source_
    path": ...})`` query returns zero rows on a genuinely fresh install
    and ``stale_ids`` is always empty; the union guard code added here
    never fires via real writes. The user-visible cross-document
    protection people actually get on a fresh install comes from
    ``mcp_infra._sweep_superseded_vectors`` (manifest-diff based,
    proven end-to-end against the real engine by tests/integration/
    test_tp8yk_manifest_never_outruns_chunks.py::test_union_guard_keeps_
    shared_chunk_at_the_production_wiring). That gap PRE-DATES nexus-
    tp8yk (RDR-102 D2) and is out of this bead's scope to fix — tracked
    separately. This test forces ``stale_ids`` non-empty via a
    controlled T3 double specifically so the WIRING nexus-tp8yk added
    here is verified on its own, independent of whether real writes
    happen to populate its candidate query today.
    """
    from nexus.doc_indexer import index_pdf

    shared_chash = "a" * 64
    exclusive_chash = "b" * 64
    pdf_path_str = str(sample_pdf.resolve())

    def _col_get(where=None, include=None, limit=None, offset=0, **kw):
        if where == {"source_path": pdf_path_str}:
            if offset == 0:
                return {"ids": [shared_chash, exclusive_chash]}
            return {"ids": []}
        return {"ids": [], "metadatas": []}

    col = MagicMock()
    col.get.side_effect = _col_get
    col.delete = MagicMock()
    t3 = MagicMock()
    t3.get_or_create_collection.return_value = col

    reader = MagicMock()
    # shared_chash: referenced by ANOTHER live document -> must survive.
    # exclusive_chash: referenced by nobody -> genuinely orphaned -> deleted.
    reader.docs_for_chashes.return_value = {shared_chash: ["9.9.9"]}

    set_credentials(monkeypatch)
    with patch("nexus.doc_indexer.make_t3", return_value=t3), \
         patch("nexus.doc_indexer._register_or_lookup_doc_id", return_value="1.2.3"), \
         patch("nexus.doc_indexer._fence_begin"), \
         patch("nexus.doc_indexer._fence_complete"), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=reader), \
         pdf_extract_patches_ctx(), \
         patch("voyageai.Client", return_value=voyage_client):
        result = index_pdf(sample_pdf, corpus="mybook")

    assert result == 1
    reader.docs_for_chashes.assert_called_once()
    called_candidates = sorted(reader.docs_for_chashes.call_args[0][0])
    assert called_candidates == sorted([shared_chash, exclusive_chash]), (
        f"expected both candidates routed through the union guard, got "
        f"{called_candidates}"
    )
    col.delete.assert_called_once()
    deleted_ids = col.delete.call_args.kwargs.get("ids") or col.delete.call_args[0][0]
    assert deleted_ids == [exclusive_chash], (
        "the union guard must delete ONLY the genuinely-orphaned "
        f"candidate and never the one another document references — "
        f"got {deleted_ids}"
    )


def test_index_pdf_fires_document_hook_exactly_once(
    sample_pdf, monkeypatch, mock_t3, voyage_client,
    cloud_mode,
) -> None:
    """RDR-089 runtime fire-once invariant (substantive critic
    Significant #5). The AST drift guard counts call-sites
    statically; this test pins the runtime property.

    A bug that moves ``fire_document`` inside a per-chunk loop would
    have N invocations per document instead of 1 — invisible to the AST
    count guard, expensive in API calls, and produces single-chunk
    aspects for multi-chunk documents (semantically wrong). Pin via a
    counting hook registered on a per-test ``HookRegistry`` instance
    threaded through ``index_pdf(hooks=...)``.
    """
    from nexus.hook_registry import HookRegistry

    fires: list[tuple[str, str, str]] = []

    def counting_hook(source_path: str, collection: str, content: str) -> None:
        fires.append((source_path, collection, content))

    hooks = HookRegistry()
    hooks.register_document(counting_hook)

    set_credentials(monkeypatch)
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with pdf_extract_patches_ctx():
            with patch("voyageai.Client", return_value=voyage_client):
                index_pdf(sample_pdf, corpus="mybook", hooks=hooks)

    assert len(fires) == 1, (
        f"Document hook fired {len(fires)} times for one PDF — "
        f"expected exactly 1. A regression here usually means a "
        f"fire site was moved inside a per-chunk loop."
    )
    captured_source, captured_coll, captured_content = fires[0]
    # CLI ingest path passes content="" per the P0.1 contract; the
    # source_path is the PDF path.
    assert captured_source == str(sample_pdf.resolve())
    assert captured_coll == "docs__mybook__voyage-context-3__v1"
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
    # Identity / position / spans — RDR-102 D2 dropped source_path; the
    # catalog tumbler in doc_id is the canonical reference. RDR-108
    # Phase 3 (nexus-bdag) retired chunk_index, chunk_count, doc_id —
    # the catalog ``document_chunks`` manifest is now authoritative.
    "content_hash", "chunk_text_hash",
    "chunk_start_char", "chunk_end_char", "page_number",
    # Display / routing — RDR-101 Phase 5c (nexus-o6aa.13) dropped
    # ``corpus``, ``store_type``, ``git_meta``. ``title`` kept (audit
    # finding: find_ids_by_title is load-bearing for nx store).
    "title", "source_author", "section_title", "section_type",
    "tags", "category", "content_type", "embedding_model",
    # Lifecycle
    "indexed_at", "ttl_days", "frecency_score", "source_agent", "session_id",
}
# pdf_subject / pdf_keywords / is_image_pdf / has_formulas / format /
# page_count / source_date are intentionally NOT in ALLOWED_TOP_LEVEL —
# normalize() drops them. They were never stored in T3 even before the
# factory refactor; the old test asserted on the pre-normalize dict
# shape. After the factory, normalize runs inside the indexer so the
# dropped fields are visible-as-missing.
#
# extraction_method (nexus-1oguj) IS in ALLOWED_TOP_LEVEL, but is
# deliberately NOT asserted here: with _STREAMING_THRESHOLD=0 every
# openable PDF (like this test's simple_pdf) routes through
# pipeline_stages.pipeline_index_pdf, which writes chunks with
# extraction_method empty at initial-upload time (dropped by
# normalize()) and backfills the real value via the
# _enrich_metadata_from_extraction POST-PASS (a separate
# ``t3.update_chunks`` call this test's bare-MagicMock ``mock_col.get``
# does not simulate). Asserting the field here would require faithfully
# modelling that round-trip; the field's presence is instead verified
# end-to-end by test_pipeline_stages.py::test_metadata_enrichment_postpass
# (streaming path, real fake-engine round-trip) and
# test_index_sets_content_type below (the batch-path fallback, which
# sets it at initial-write time via _pdf_chunks).
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
    # nexus-5xn3k.4: mock_t3 never actually writes chunks to the real
    # (test-scoped) engine's T3, so the fence's fail-closed verify-then-stamp
    # would correctly (but irrelevantly here) refuse completion. This test
    # only cares about chunk metadata shape; stub the fence tail like
    # tests/test_pipeline_stages.py's ``_stub_fence_complete`` fixture.
    monkeypatch.setattr("nexus.doc_indexer._fence_complete", lambda *a, **k: None)
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
def test_index_sets_content_type(indexer, expected_type, sample_pdf, sample_md, monkeypatch, voyage_client):
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
    # RDR-101 Phase 5c: ``store_type`` dropped from chunk schema;
    # ``content_type`` is the canonical routing field.
    assert captured[0]["content_type"] == expected_type
    # nexus-1oguj: the extractor identity from ExtractionResult.metadata
    # is threaded through to the written chunk for PDFs; markdown never
    # sets it (no extractor involved), so normalize() drops the key.
    if indexer == "pdf":
        assert captured[0]["extraction_method"] == "x"
    else:
        assert "extraction_method" not in captured[0]


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
def test_embed_with_fallback_calls_cce(n_chunks, expected_embs, cloud_mode):

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


def test_single_chunk_cce_uses_contextualized_embed(cloud_mode):

    client = _make_cce_client(embeddings_per_call=[[0.1] * 10])
    with patch("voyageai.Client", return_value=client):
        embeddings, model = _embed_with_fallback(["single chunk content"], "voyage-context-3", "test-key")
    client.contextualized_embed.assert_called_once()
    client.embed.assert_not_called()
    assert model == "voyage-context-3"
    assert len(embeddings) == 1


def test_embed_with_fallback_cce_failure_splits_and_stays_on_model(cloud_mode):

    client = _make_cce_client(fail_on_call={1})
    with patch("voyageai.Client", return_value=client):
        embeddings, model = _embed_with_fallback(chunks=["a", "b"], model="voyage-context-3", api_key="vk_test")
    assert model == "voyage-context-3"
    assert len(embeddings) == 2
    client.embed.assert_not_called()


def test_embed_with_fallback_batches_large_input(cloud_mode):

    chunks = [f"chunk{i}_" + "x" * 24_000 for i in range(6)]
    client = _make_cce_client()
    with patch("voyageai.Client", return_value=client):
        embeddings, model = _embed_with_fallback(chunks=chunks, model="voyage-context-3", api_key="vk_test")
    assert client._call_count[0] >= 2
    client.embed.assert_not_called()
    assert model == "voyage-context-3"
    assert len(embeddings) == 6


def test_partial_cce_failure_splits_failed_batch(cloud_mode):

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


def test_cce_contract_embed_with_fallback_uses_correct_access_path(cloud_mode):

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


def test_cce_contract_large_input_still_uses_cce(cloud_mode):

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
def test_index_pdf_hash_match_model_check(stored_model, expected_result, sample_pdf, monkeypatch, cloud_mode):
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


def test_embed_standard_path_batches_over_128_chunks(cloud_mode):
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
def test_embed_with_fallback_warns_on_excessive_chunks(limit_override, n_chunks, cloud_mode):

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


def test_embed_with_fallback_empty_chunks(cloud_mode):

    embeddings, model = _embed_with_fallback([], "voyage-context-3", "vk_test")
    assert embeddings == []
    assert model == "voyage-context-3"


def test_embed_with_fallback_filters_empty_strings(cloud_mode):

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


def test_embed_with_fallback_all_empty_strings(cloud_mode):

    mock_client = MagicMock()
    with patch("voyageai.Client", return_value=mock_client):
        embeddings, _ = _embed_with_fallback(["", "   ", "\n"], "voyage-code-3", "vk_test")
    assert embeddings == []
    mock_client.embed.assert_not_called()


def test_cce_failure_splits_recursively(cloud_mode):

    client = _make_cce_client(fail_on_call={1})
    with patch("voyageai.Client", return_value=client):
        embeddings, model = _embed_with_fallback([f"chunk_{i}" for i in range(4)], "voyage-context-3", "vk_test")
    assert len(embeddings) == 4
    assert model == "voyage-context-3"
    client.embed.assert_not_called()


def test_embed_partial_batch_failure_stays_same_model(cloud_mode):

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


def test_embed_single_chunk_failure_raises(cloud_mode):

    mock_client = MagicMock()
    mock_client.contextualized_embed.side_effect = RuntimeError("single chunk too large")
    with patch("voyageai.Client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="single chunk too large"):
            _embed_with_fallback(["one giant chunk"], "voyage-context-3", "vk_test")


def test_embed_with_fallback_cce_empty_result_raises(cloud_mode):

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
def test_force_bypasses_staleness(indexer, sample_pdf, sample_md, monkeypatch, cloud_mode):
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


def test_force_default_false_still_skips(sample_pdf, monkeypatch, cloud_mode):
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


def test_index_pdf_return_metadata_true_skipped_returns_empty_dict(sample_pdf, monkeypatch, cloud_mode):
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


def test_index_markdown_return_metadata_true_skipped_returns_empty_dict(sample_md, monkeypatch, cloud_mode):
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
def test_embed_progress_callback_fires(model, use_cce, cloud_mode):

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


def test_embed_progress_callback_none_is_noop(cloud_mode):

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
    # nexus-8g79.23: exact count — one mock chunk planted, one indexed.
    assert result == 1
    assert progress


def test_stale_chunk_pruning_deletes_old_ids(sample_md, monkeypatch, voyage_client, cloud_mode):
    """RDR-180 (nexus-jxizy.3): chunk natural ID is the full
    ``chunk_text_hash`` digest. Stale-chunk pruning deletes T3 chunks
    whose ID is no longer in the current upsert set (i.e. their text
    no longer appears in this document)."""
    set_credentials(monkeypatch)
    new_chunk_texts = [f"chunk text {i}" for i in range(3)]
    new_ids = {
        hashlib.sha256(t.encode()).hexdigest() for t in new_chunk_texts
    }
    stale_ids = {
        hashlib.sha256(f"old text {i}".encode()).hexdigest()
        for i in range(2)
    }
    mock_col = MagicMock()
    mock_col.get.side_effect = [
        # First call: staleness check (one prior chunk with old content_hash).
        {"ids": [next(iter(stale_ids))],
         "metadatas": [{"content_hash": "old_hash",
                        "embedding_model": "voyage-context-3"}]},
        # Second call: prune scan returns the union of new + stale IDs.
        {"ids": list(new_ids | stale_ids)},
    ]
    captured_deletes: list = []
    mock_col.delete.side_effect = lambda ids: captured_deletes.extend(ids)
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    chunks = []
    for i, text in enumerate(new_chunk_texts):
        mc = MagicMock()
        mc.text = text
        mc.chunk_index = i
        mc.metadata = {"chunk_start_char": 0, "chunk_end_char": 10,
                       "page_number": 0, "header_path": "H"}
        chunks.append(mc)
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.SemanticMarkdownChunker") as chk_cls:
            with patch("voyageai.Client", return_value=voyage_client):
                chk_cls.return_value.chunk.return_value = chunks
                index_markdown(sample_md, corpus="docs")
    assert set(captured_deletes) == stale_ids


@pytest.fixture
def incr_setup(sample_pdf, monkeypatch, cloud_mode):
    """Common setup for incremental PDF tests."""
    from nexus.doc_indexer import _INCREMENTAL_THRESHOLD
    set_credentials(monkeypatch)
    ckpt_dir = sample_pdf.parent / "ckpt"
    monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", ckpt_dir)
    monkeypatch.setattr("nexus.doc_indexer.CHECKPOINT_DIR", ckpt_dir)
    # nexus-5xn3k.4 review follow-up: _index_pdf_incremental now brackets a
    # real _fence_complete call. This fixture's t3 is a MagicMock — no
    # chunk ever lands in the real (test-scoped) engine's T3 — so the
    # fence's genuine verify-then-stamp would correctly (but irrelevantly
    # for these tests, which only assert chunk/checkpoint bookkeeping)
    # refuse completion. Same stub as test_pipeline_stages.py's
    # _stub_fence_complete / test_pdf_subsystem.py's per-test monkeypatch.
    monkeypatch.setattr("nexus.doc_indexer._fence_complete", lambda *a, **k: None)

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
        pdf=str(incr_setup.path), collection="docs__test__voyage-context-3__v1",
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
    assert not checkpoint_path(incr_setup.content_hash, "docs__test__voyage-context-3__v1").exists()


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
    # nexus-5xn3k.4 review follow-up: see incr_setup's identical stub above.
    monkeypatch.setattr("nexus.doc_indexer._fence_complete", lambda *a, **k: None)
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
        pdf=str(incr_setup.path), collection="docs__test__voyage-context-3__v1",
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
        pdf=str(incr_setup.path), collection="docs__test__voyage-context-3__v1",
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


def test_parallel_embed_preserves_order(cloud_mode):


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


def test_parallel_embed_progress_fires_for_each_batch(cloud_mode):

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
            # GH #336: prevent the local-fallback path from firing —
            # this test exercises the cloud streaming router, not
            # the credential-fallback branch.
            patch("nexus.config.is_local_mode", return_value=False),
            patch("nexus.doc_indexer._sha256", return_value="abc123"),
            patch("nexus.doc_indexer.make_t3"),
            patch("nexus.doc_indexer._vector_with_retry", return_value={"metadatas": []}),
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
            # GH #336: prevent the local-fallback path from firing —
            # this test parametrises over streaming routing, not the
            # credential-fallback branch.
            patch("nexus.config.is_local_mode", return_value=False),
            patch("nexus.doc_indexer._sha256", return_value="abc123"),
            patch("nexus.doc_indexer.make_t3"),
            patch("nexus.doc_indexer._vector_with_retry", return_value={"metadatas": []}),
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


# ── RDR-102 Phase A: pre-flight catalog registration writes doc_id ──────────
#
# These tests pin the Phase A invariant: every doc_indexer entry point
# (index_pdf, index_markdown, batch_index_markdowns) must populate the
# ``doc_id`` field on every chunk it writes when the catalog is initialized.
# Pre-Phase-A behaviour: chunks ship to T3 with no ``doc_id`` because the
# catalog hook fires AFTER the upsert. Post-Phase-A: pre-flight registration
# resolves the catalog tumbler before chunks are built and threads it
# through ``make_chunk_metadata(..., doc_id=...)``.


def _setup_phase_a_catalog(tmp_path, monkeypatch):
    """Initialize a fresh catalog at the path the autouse ``_isolate_catalog``
    fixture configures via ``NEXUS_CATALOG_PATH`` and return an EphemeralClient
    T3 with the local ONNX embedder.

    Forces local-mode ingest by clearing Voyage/Chroma credentials so the
    indexer does not attempt to call the real cloud APIs.

    nexus-i711w: the local Catalog.init is gone — pre-flight registration
    goes to the live catalog, which needs no init.

    nexus-tp8yk D2a: the module-level autouse ``_no_propagating_fence_
    complete`` fixture stubs ``_fence_complete`` for every test in this
    file (this T3 is a fully in-process double, decoupled by design from
    the real engine-backed catalog every test registers against) — see
    that fixture's docstring for the full rationale.
    """
    from nexus.db.t3 import T3Database

    cat_dir = tmp_path / "test-catalog"
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    monkeypatch.setattr(
        "nexus.config._global_config_path", lambda: Path("/nonexistent"),
    )
    client = make_vector_test_client()
    return cat_dir, T3Database(_client=client, local_mode=True)


# _doc_registered_count helper retired with the events.jsonl event log
# (nexus-i711w terminal deletion).


def test_index_pdf_does_not_emit_source_path(
    sample_pdf, tmp_path, monkeypatch,
):
    """RDR-102 Phase B / D2: index_pdf at doc_indexer.py:794 (the
    _pdf_chunks make_chunk_metadata call) must drop source_path from
    its kwargs. After Phase B, every PDF chunk carries no source_path
    key; the catalog tumbler in doc_id is the canonical reference and
    normalize() filters source_path at the schema-level removal.
    """
    cat_dir, t3 = _setup_phase_a_catalog(tmp_path, monkeypatch)

    with pdf_extract_patches_ctx():
        index_pdf(sample_pdf, corpus="rdr102-pdf-b", t3=t3, embed_fn=_fake_embed)

    col = t3.get_or_create_collection(
        f"docs__rdr102-pdf-b__{_local_token()}__v1",
    )
    rows = col.get(include=["metadatas"])
    assert rows["metadatas"], "expected at least one chunk"
    leaked = [m for m in rows["metadatas"] if "source_path" in m]
    assert not leaked, (
        f"{len(leaked)}/{len(rows['metadatas'])} index_pdf chunks still "
        f"carry source_path. Phase B must drop source_path= from "
        f"_pdf_chunks (doc_indexer.py:794) AND remove source_path from "
        f"ALLOWED_TOP_LEVEL so normalize() filters any residual writes."
    )


def test_index_markdown_does_not_emit_source_path(
    sample_md, tmp_path, monkeypatch,
):
    """RDR-102 Phase B / D2: index_markdown at doc_indexer.py:874 (the
    _markdown_chunks make_chunk_metadata call) must drop source_path
    from its kwargs.
    """
    cat_dir, t3 = _setup_phase_a_catalog(tmp_path, monkeypatch)

    n = index_markdown(sample_md, corpus="rdr102-md-b", t3=t3)
    assert n > 0

    col = t3.get_or_create_collection(
        f"docs__rdr102-md-b__{_local_token()}__v1",
    )
    rows = col.get(include=["metadatas"])
    assert rows["metadatas"], "expected at least one chunk"
    leaked = [m for m in rows["metadatas"] if "source_path" in m]
    assert not leaked, (
        f"{len(leaked)}/{len(rows['metadatas'])} index_markdown chunks "
        f"still carry source_path. Phase B must drop source_path= from "
        f"_markdown_chunks (doc_indexer.py:874)."
    )


def test_index_pdf_writes_doc_id_when_catalog_initialized(
    sample_pdf, tmp_path, monkeypatch,
):
    """RDR-102 D4 #2: ``index_pdf`` must populate ``doc_id`` on chunk
    metadata when the catalog is initialized.

    Pre-Phase-A this fails because ``_pdf_chunks`` builds metadata via
    ``make_chunk_metadata()`` with no ``doc_id`` kwarg; the catalog
    Document is registered AFTER chunks are upserted so ``doc_id`` is
    never threaded down. Phase A registers upfront and passes the
    resolved tumbler through to the chunker.
    """
    cat_dir, t3 = _setup_phase_a_catalog(tmp_path, monkeypatch)

    with pdf_extract_patches_ctx():
        index_pdf(sample_pdf, corpus="rdr102-pdf", t3=t3, embed_fn=_fake_embed)

    col = t3.get_or_create_collection(
        f"docs__rdr102-pdf__{_local_token()}__v1",
    )
    rows = col.get(include=["metadatas"])
    assert rows["metadatas"], (
        "expected at least one chunk in docs__rdr102-pdf; staleness "
        "skip would mask the real bug"
    )
    # RDR-108 Phase 3 retired doc_id from chunk metadata. Manifest is
    # authoritative — verify the catalog has manifest rows for this PDF.
    for m in rows["metadatas"]:
        assert "doc_id" not in m
    cat = ActiveCatalog()
    documents = cat.list_by_collection(f"docs__rdr102-pdf__{_local_token()}__v1")
    assert documents, "catalog must have a Document for the indexed PDF"
    for entry in documents:
        assert cat.get_manifest(str(entry.tumbler)), (
            f"manifest_write_batch_hook must populate document_chunks "
            f"for doc_id={str(entry.tumbler)!r}"
        )


def test_index_markdown_writes_doc_id_when_catalog_initialized(
    sample_md, tmp_path, monkeypatch,
):
    """RDR-108 Phase 3: ``index_markdown`` no longer stamps ``doc_id``
    on chunk metadata; the catalog manifest is authoritative. Verify
    the manifest has rows for the indexed markdown file.
    """
    cat_dir, t3 = _setup_phase_a_catalog(tmp_path, monkeypatch)

    n = index_markdown(sample_md, corpus="rdr102-md", t3=t3)
    assert n > 0, "expected index_markdown to upsert chunks"

    col = t3.get_or_create_collection(
        f"docs__rdr102-md__{_local_token()}__v1",
    )
    rows = col.get(include=["metadatas"])
    assert rows["metadatas"], (
        "expected at least one chunk in docs__rdr102-md"
    )
    for m in rows["metadatas"]:
        assert "doc_id" not in m
    cat = ActiveCatalog()
    documents = cat.list_by_collection(f"docs__rdr102-md__{_local_token()}__v1")
    assert documents, "catalog must have a Document for the indexed markdown"
    for entry in documents:
        assert cat.get_manifest(str(entry.tumbler))


def test_batch_index_markdowns_rdr_mode_writes_doc_id_when_catalog_initialized(
    tmp_path, monkeypatch,
):
    """RDR-108 Phase 3: ``batch_index_markdowns`` in RDR mode (``rdr__``
    collection, ``content_type='rdr'``) must populate the catalog
    ``document_chunks`` manifest for each registered Document. Chunk
    metadata no longer carries doc_id directly.
    """
    cat_dir, t3 = _setup_phase_a_catalog(tmp_path, monkeypatch)
    rdr_path = tmp_path / "rdr-102-test.md"
    rdr_path.write_text(
        "---\ntitle: RDR-102 Test\nstatus: draft\n---\n\n"
        "# Section A\n\nBody text alpha.\n\n"
        "# Section B\n\nBody text beta.\n"
    )

    batch_index_markdowns(
        [rdr_path], corpus="rdr102-rdrmode",
        collection_name=f"rdr__rdr102-rdrmode__{_local_token()}__v1",
        content_type="rdr",
        t3=t3,
    )

    col = t3.get_or_create_collection(
        f"rdr__rdr102-rdrmode__{_local_token()}__v1",
    )
    rows = col.get(include=["metadatas"])
    assert rows["metadatas"], (
        "expected at least one chunk in rdr__rdr102-rdrmode"
    )
    for m in rows["metadatas"]:
        assert "doc_id" not in m
    cat = ActiveCatalog()
    documents = cat.list_by_collection(f"rdr__rdr102-rdrmode__{_local_token()}__v1")
    assert documents, "catalog must have a Document for the indexed RDR md"
    for entry in documents:
        assert cat.get_manifest(str(entry.tumbler))


def test_index_markdown_post_hook_updates_chunk_count_after_preflight(
    sample_md, tmp_path, monkeypatch,
):
    """RDR-102 Phase A regression guard: pre-flight registration writes
    a catalog Document with ``chunk_count=0``; the post-hook
    ``_catalog_markdown_hook`` MUST update the existing tumbler with
    the real chunk_count, not call ``cat.register()`` unconditionally
    (which hits the by_file_path early-return and silently leaves
    chunk_count at 0).

    Mirrors the if-existing/update branch ``_catalog_pdf_hook`` already
    has at line 519. Without this branch in the markdown hook, every
    markdown re-index leaves chunk_count stuck at 0 in the catalog —
    invisible to operators who never read the Document row but a
    structural drift between catalog + T3 chunk counts.
    """
    cat_dir, t3 = _setup_phase_a_catalog(tmp_path, monkeypatch)

    n = index_markdown(sample_md, corpus="rdr102-chunkcount", t3=t3)
    assert n > 0, "expected index_markdown to upsert at least one chunk"

    rows = documents_by_file_path(str(sample_md.resolve()))
    assert len(rows) == 1, (
        f"expected exactly 1 catalog Document for the markdown file; "
        f"got {len(rows)} rows. Pre-flight + post-hook double-register "
        f"would show >1 here (file_path-form mismatch); a missing "
        f"post-hook would leave chunk_count at 0."
    )
    cat_chunk_count = rows[0].chunk_count
    assert cat_chunk_count == n, (
        f"catalog chunk_count={cat_chunk_count} but T3 has n={n} chunks. "
        f"_catalog_markdown_hook must call cat.update() on the existing "
        f"tumbler when pre-flight already registered it; the previous "
        f"unconditional cat.register() hit by_file_path's early-return "
        f"and never updated chunk_count off zero."
    )


def test_frontmatter_title_year_reach_catalog_despite_preflight(
    tmp_path, monkeypatch,
):
    """nexus-ivzw8: the pre-flight registers with title=stem; the post-hook's
    update branch previously wrote only chunk_count/mtime — so frontmatter
    title/year NEVER reached the catalog on the standard path. The fix
    threads the parsed frontmatter into the pre-flight AND stem-guard
    backfills on update; the row must carry the frontmatter values."""
    md = tmp_path / "widget-spec.md"
    md.write_text(
        "---\ntitle: The Widget Specification\ncreated: 2026-03-14\n---\n\n"
        "# Widgets\n\nBody.\n"
    )
    cat_dir, t3 = _setup_phase_a_catalog(tmp_path, monkeypatch)

    n = index_markdown(md, corpus="ivzw8-title", t3=t3)
    assert n > 0

    rows = documents_by_file_path(str(md.resolve()))
    assert len(rows) == 1
    title, year = rows[0].title, rows[0].year
    assert title == "The Widget Specification", (
        f"catalog kept the stem title {title!r} — frontmatter never applied "
        f"(nexus-ivzw8)"
    )
    assert year == 2026


def test_curated_title_survives_reindex(tmp_path, monkeypatch):
    """The stem-guard: a curated (non-stem) catalog title must NEVER be
    clobbered by a re-index — backfill applies only to the stem default."""
    md = tmp_path / "notes.md"
    md.write_text("---\ntitle: Frontmatter Title\n---\n\n# N\n\nBody.\n")
    cat_dir, t3 = _setup_phase_a_catalog(tmp_path, monkeypatch)

    assert index_markdown(md, corpus="ivzw8-curated", t3=t3) > 0

    rows = documents_by_file_path(str(md.resolve()))
    assert len(rows) == 1
    ActiveCatalog().update(str(rows[0].tumbler), title="Hand-Curated Title")

    # touch content so the re-index actually re-runs the hook
    md.write_text("---\ntitle: Frontmatter Title\n---\n\n# N\n\nBody v2.\n")
    assert index_markdown(md, corpus="ivzw8-curated", t3=t3) > 0

    after = documents_by_file_path(str(md.resolve()))
    assert len(after) == 1
    assert after[0].title == "Hand-Curated Title"


# test_preflight_registration_idempotent_on_staleness_skip retired
# (nexus-i711w terminal deletion): both of its instruments were local
# artifacts by design — the events.jsonl DocumentRegistered count and
# `nx catalog doctor --replay-equality` — and died with the local catalog.
# The register() same-file_path idempotency contract itself is still owed
# a service-side test (GAP nexus-i711w.1 item 9).


@pytest.fixture(autouse=True)
def _legacy_vector_backend(monkeypatch):
    """nexus-tawx0: service mode is the post-P4a DEFAULT (no-Python-embed
    stubs fire unless opted out). This module tests the legacy
    chroma/local embed pipeline, which is exactly the chroma-injected
    configuration the opt-out exists for."""
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
