# SPDX-License-Identifier: AGPL-3.0-or-later
"""AC6-AC7: doc_indexer — SHA256 incremental sync, docs__ metadata schema."""
import hashlib
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from voyageai.object.contextualized_embeddings import (
    ContextualizedEmbeddingsObject,
    ContextualizedEmbeddingsResult,
)

from nexus.doc_indexer import (
    _identity_where,
    _lookup_existing_doc_id, _markdown_chunks,
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
    # nexus-sghyo (2026-08-06): embed_fn dispatch reads ambient
    # is_vector_service_mode() regardless of the injected t3= handle —
    # an explicit chroma opt-out is still the only way to reach the
    # LOCAL-MODE embed branch (_make_local_embed_fn) under test here.
    # This is NOT the deleted non-service/non-local CLOUD credential
    # path (client no longer embeds via Voyage) — local-mode client-side
    # embedding (bge-768/fastembed) is unaffected by that retirement.
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
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
    """nexus-sghyo (2026-08-06): non-service, non-local ("legacy cloud")
    ingestion is RETIRED outright — the client no longer embeds via
    Voyage (Hal determination 2026-07-28), regardless of whether
    credentials are present. What used to be "cloud mode explicit but
    credentials missing -> CredentialsMissingError naming the missing
    key" is now "non-service mode at all -> CredentialsMissingError
    naming the retirement", since there is no longer a credential that
    would make this path work.

    ``NX_LOCAL=0`` alone no longer selects this failure: service mode
    (the default) is checked FIRST and silently embeds server-side
    regardless of NX_LOCAL, so an explicit chroma opt-out
    (``NX_STORAGE_BACKEND_VECTORS``) is required to reach the retired
    branch at all.
    """
    from nexus.errors import CredentialsMissingError

    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
    monkeypatch.setattr(
        "nexus.config._global_config_path", lambda: Path("/nonexistent"),
    )
    path = sample_pdf if indexer == "pdf" else sample_md
    fn = index_pdf if indexer == "pdf" else index_markdown
    with patch("nexus.doc_indexer.make_t3") as mock_factory:
        with pytest.raises(CredentialsMissingError) as excinfo:
            fn(path, corpus="test")
    mock_factory.assert_not_called()
    assert "Voyage" in str(excinfo.value)
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
            result = index_pdf(sample_pdf, corpus="mybook", t3=mock_t3)
    assert result == 0
    ext_cls.assert_not_called()


def test_index_pdf_upserts_chunks_when_new(sample_pdf, monkeypatch, mock_t3, voyage_client):
    set_credentials(monkeypatch)
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with pdf_extract_patches_ctx() as pep:
            result = index_pdf(sample_pdf, corpus="mybook", t3=mock_t3)
    assert result == 1
    mock_t3.upsert_chunks_with_embeddings.assert_called_once()


# ── nexus-2xu6t STEP 0: does a preflight register exception feed the
# nexus-94fxl identity-drop collector (get_manifest_identity_drops), the
# same collector nexus-tp8yk D2 wired into nx dt index's / nx index repo's
# non-zero exit? Empirical proof this bead's acceptance criterion demands,
# not inference from reading the ``except Exception`` swallow at
# ``_register_or_lookup_doc_id``'s tail.


def test_register_or_lookup_doc_id_returns_empty_when_writer_register_raises(
    sample_pdf, monkeypatch,
):
    """Direct proof of the swallow itself: ``writer.register`` raising
    inside ``_register_or_lookup_doc_id`` is caught by the broad
    ``except Exception`` (nexus-h9f1w / GH #1350 Fix C) and the function
    returns ``""`` rather than propagating — this is the documented,
    intentional best-effort contract, pinned here so a future change to
    that contract is a deliberate, visible diff rather than a silent
    behavior change underneath the identity-drop test below.
    """
    from nexus.doc_indexer import _register_or_lookup_doc_id

    reader = MagicMock()
    reader.by_file_path.return_value = None
    reader.curator_owner_tumbler_by_name.return_value = "1.99"
    writer = MagicMock()
    writer.register.side_effect = RuntimeError("engine unreachable")

    with patch("nexus.catalog.factory.make_catalog_reader", return_value=reader), \
         patch("nexus.catalog.factory.make_catalog_writer", return_value=writer):
        doc_id = _register_or_lookup_doc_id(
            sample_pdf, "mybook",
            content_type="paper", physical_collection="docs__mybook",
        )
    assert doc_id == ""
    writer.register.assert_called_once()


def test_preflight_register_failure_feeds_identity_drop_collector(
    sample_pdf, monkeypatch, mock_t3, voyage_client,
):
    """nexus-2xu6t STEP 0 verdict test: a catalog-register exception during
    preflight registration must not silently vanish — it must feed the
    identity-drop collector so ``nx dt index`` / ``nx index repo`` (both
    wired by nexus-tp8yk D2) fail loud instead of reporting plain success.

    Runs the REAL ``_register_or_lookup_doc_id`` (only the catalog reader/
    writer are doubled — ``writer.register`` raises) through the REAL
    ``index_pdf`` pipeline, including the REAL (unmocked) default hook
    chain, so this proves the actual production wiring end-to-end rather
    than a mocked collector call.

    Path coverage note (critic round, 2026-08-05): ``sample_pdf`` is fake,
    unopenable-by-pymupdf bytes, so ``index_pdf``'s streaming-routing probe
    (``pymupdf.open`` failing -> ``page_count = -1``) falls through to the
    NON-streaming batch/single-flush path (``_index_document``) — this
    test covers that fallback specifically. ``_STREAMING_THRESHOLD = 0``
    means every REAL, openable PDF is routed through the streaming
    pipeline (``pipeline_index_pdf``) unconditionally instead; that path
    is pinned separately by ``tests/test_pipeline_stages.py::
    TestPipelineIndexPdf::test_streaming_register_failure_feeds_identity_
    drop_collector``, which drives the same swallow through ``uploader_
    loop``'s hook chain.
    """
    from nexus.mcp_infra import (
        get_manifest_identity_drops,
        reset_manifest_identity_drops,
    )

    set_credentials(monkeypatch)
    reset_manifest_identity_drops()

    reader = MagicMock()
    reader.by_file_path.return_value = None
    reader.curator_owner_tumbler_by_name.return_value = "1.99"
    writer = MagicMock()
    writer.register.side_effect = RuntimeError("engine unreachable")

    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=reader), \
         patch("nexus.catalog.factory.make_catalog_writer", return_value=writer), \
         pdf_extract_patches_ctx():
        result = index_pdf(sample_pdf, corpus="mybook", t3=mock_t3)

    # Collect-and-continue (nexus-9800y convention): the registration
    # failure must NOT abort the write — chunks land regardless.
    assert result == 1, "registration failure must not abort the chunk write"
    mock_t3.upsert_chunks_with_embeddings.assert_called_once()

    drops = get_manifest_identity_drops()
    assert drops, (
        "a preflight catalog-register exception did not feed the "
        "identity-drop collector — nx dt index / nx index repo would "
        "report plain success on this failure (nexus-2xu6t unfixed at "
        "this call site)"
    )


def test_index_pdf_small_doc_prune_deleted_as_dead_code(
    sample_pdf, monkeypatch, voyage_client,
) -> None:
    """nexus-tbkk1: the stale-chunk prune block formerly at index_pdf's
    small-doc branch (which routed candidates through nexus-tp8yk's D3
    union guard) is DELETED dead code, not merely runtime-unreachable.

    This test SUPERSEDES nexus-tp8yk's test_index_pdf_prune_union_guard_
    wired_at_call_site, whose own docstring discovered and documented the
    gap this bead closes: ``_identity_where``'s ``source_path`` fallback
    (used by this exact prune query) filters on a chunk-metadata field
    RDR-102 D2 hard-removed from ``make_chunk_metadata`` — every PDF/
    markdown chunk doc_indexer.py writes in real production carries NO
    ``source_path`` at all, so ``col.get(where={"source_path": ...})``
    always returned zero rows and the union-guard wiring that test proved
    never fired via real writes. Rather than leave a runtime-dead branch
    with a passing wiring test, nexus-tbkk1 deletes the branch outright.

    The union-guard LOGIC itself (``orphaned_chashes``) is untouched by
    this deletion and remains covered by tests/db/test_http_catalog_
    integration.py::TestPruneUnionGuard. Its thin wrapper
    ``prune_orphan_candidates`` — built specifically for this deleted
    call site and its three siblings — was ALSO deleted in this same
    fix round (substantive-critic Significant #2: zero production
    callers survived this deletion), along with its now-pointless
    dedicated test file; see ``nexus.indexer_utils``'s deletion comment.
    The real cross-document prune protection users actually get is
    ``mcp_infra._sweep_superseded_vectors`` (manifest-diff based, calls
    ``orphaned_chashes`` directly), proven end-to-end at tests/
    integration/test_tp8yk_manifest_never_outruns_chunks.py::
    test_union_guard_keeps_shared_chunk_at_the_production_wiring.

    Kill control: seeds a T3 double that WOULD have produced two prune
    candidates (one genuinely orphaned, one shared with another live
    document) under the old code. If the deleted block were
    reintroduced, ``col.get`` would be called with the source_path
    where-clause, ``reader.docs_for_chashes`` would fire, and ``col.
    delete`` would remove the orphaned candidate — every one of these
    assertions fails the moment that regresses.
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
    reader.docs_for_chashes.return_value = {shared_chash: ["9.9.9"]}

    set_credentials(monkeypatch)
    with patch("nexus.doc_indexer.make_t3", return_value=t3), \
         patch("nexus.doc_indexer._register_or_lookup_doc_id", return_value="1.2.3"), \
         patch("nexus.doc_indexer._fence_begin"), \
         patch("nexus.doc_indexer._fence_complete"), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=reader), \
         pdf_extract_patches_ctx():
        result = index_pdf(sample_pdf, corpus="mybook", t3=t3)

    assert result == 1
    reader.docs_for_chashes.assert_not_called()
    col.delete.assert_not_called()
    # The prune's source_path-keyed query must never even be issued.
    for call in col.get.call_args_list:
        where = call.kwargs.get("where") if call.kwargs else (call.args[0] if call.args else None)
        assert where != {"source_path": pdf_path_str}, (
            f"dead source_path prune query resurrected: {call}"
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
            index_pdf(sample_pdf, corpus="mybook", t3=mock_t3, hooks=hooks)

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
            chk_cls.return_value.chunk.return_value = [mock_chunk]
            index_markdown(sample_md, corpus="docs", t3=mock_t3)
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
    # nexus-sghyo (2026-08-06): no client-side embed function to mock —
    # the service-mode stub (ambient test default) produces the
    # placeholder embeddings; this test only asserts metadata shape.
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
                    ext_cls.return_value.extract.return_value = MagicMock(
                        text="txt", metadata={"page_count": 1, "format": "pdf", "extraction_method": "x"})
                    chk_cls.return_value.chunk.return_value = [mock_chunk]
                    index_pdf(sample_pdf, corpus="mybook", t3=mock_t3)
    else:
        mock_chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 4, "page_number": 0, "header_path": "H"}
        with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
            with patch("nexus.doc_indexer.SemanticMarkdownChunker") as chk_cls:
                chk_cls.return_value.chunk.return_value = [mock_chunk]
                index_markdown(sample_md, corpus="docs", t3=mock_t3)
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
            chk_cls.return_value.chunk.return_value = [mock_chunk]
            index_markdown(md_path, corpus="docs", t3=mock_t3)
    assert captured
    assert captured[0]["chunk_start_char"] == expected_start
    assert captured[0]["chunk_end_char"] == expected_end


# nexus-sghyo (2026-08-06): the whole client-side Voyage CCE embed
# pipeline (``_embed_with_fallback``, ``_batch_chunks_for_cce``,
# ``_TokenBucket``, and the voyageai-object-shape "contract" tests that
# pinned their exact attribute-access path) was DELETED — the client no
# longer embeds via Voyage (Hal determination 2026-07-28: "we do no
# embedding on the client"). The falsification IS the test story here:
# there is no surviving subject for these tests to exercise. CCE
# embedding now happens entirely server-side; the Java-engine parity
# oracle in tests/db/test_embed_parity.py (integration-gated) is the
# surviving proof that server-side CCE embedding is correct.


def test_index_pdf_uses_cce_for_docs_collection(sample_pdf, monkeypatch):
    # nexus-sghyo (2026-08-06): CCE embedding is entirely server-side now
    # (no client-side Voyage mock needed) — the assertion is that docs__
    # collections route through upsert_chunks_with_embeddings (the
    # service-stub embed path), not a specific embedder.
    set_credentials(monkeypatch)
    mock_chunk, mock_extract = _make_pdf_mocks()
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_t3 = MagicMock()
    mock_t3.get_or_create_collection.return_value = mock_col
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3), \
         patch("nexus.doc_indexer.PDFExtractor") as ext_cls, \
         patch("nexus.doc_indexer.PDFChunker") as chk_cls:
        ext_cls.return_value.extract.return_value = mock_extract
        chk_cls.return_value.chunk.return_value = [mock_chunk, mock_chunk]
        result = index_pdf(sample_pdf, corpus="mybook", t3=mock_t3)
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
    # nexus-sghyo (2026-08-06): no client-side Voyage mock needed — the
    # client no longer embeds; service mode (the ambient test default)
    # embeds server-side via a stub, which this test does not assert on.
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3), \
         patch("nexus.doc_indexer.PDFExtractor") as ext_cls, \
         patch("nexus.doc_indexer.PDFChunker") as chk_cls:
        ext_cls.return_value.extract.return_value = mock_extract
        chk_cls.return_value.chunk.return_value = [mock_chunk, mock_chunk]
        result = index_pdf(sample_pdf, corpus="mybook", t3=mock_t3)
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


# nexus-sghyo (2026-08-06): test_embed_standard_path_batches_over_128_chunks,
# test_cce_total_token_limit_exists_and_gte_per_batch,
# test_cce_max_total_chunks_constant,
# test_embed_with_fallback_warns_on_excessive_chunks,
# test_embed_with_fallback_empty_chunks, _filters_empty_strings,
# _all_empty_strings, test_cce_failure_splits_recursively,
# test_embed_partial_batch_failure_stays_same_model,
# test_embed_single_chunk_failure_raises, and
# test_embed_with_fallback_cce_empty_result_raises all directly
# exercised the deleted client-side ``_embed_with_fallback`` /
# ``_CCE_*`` constants — no surviving subject.


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
                    result = index_pdf(path, corpus="mybook", t3=mock_t3, force=True, embed_fn=_fake_embed)
    else:
        chunk = MagicMock()
        chunk.text = "text"
        chunk.chunk_index = 0
        chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 4, "page_number": 0, "header_path": "H"}
        with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
            with patch("nexus.doc_indexer.SemanticMarkdownChunker") as chk_cls:
                chk_cls.return_value.chunk.return_value = [chunk]
                result = index_markdown(path, corpus="docs", t3=mock_t3, force=True, embed_fn=_fake_embed)

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
            result = index_pdf(sample_pdf, corpus="mybook", t3=mock_t3)
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
            result = index_pdf(sample_pdf, corpus="test", t3=mock_t3)
    assert isinstance(result, int) and result == 1


def test_index_pdf_return_metadata_true_returns_dict(sample_pdf, monkeypatch, mock_t3, voyage_client):
    set_credentials(monkeypatch)
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        with patch("nexus.doc_indexer.PDFExtractor") as ext_cls:
            with patch("nexus.doc_indexer.PDFChunker") as chk_cls:
                chunk = MagicMock()
                chunk.text = "chunk content"
                chunk.chunk_index = 0
                chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 13, "page_number": 2}
                ext_cls.return_value.extract.return_value = MagicMock(
                    text="text", metadata={"extraction_method": "x", "page_count": 1,
                                           "format": "markdown", "page_boundaries": [],
                                           "title": "My Paper", "author": "A. Thor"})
                chk_cls.return_value.chunk.return_value = [chunk]
                result = index_pdf(sample_pdf, corpus="test", t3=mock_t3, return_metadata=True)
    assert isinstance(result, dict)
    assert result["chunks"] == 1
    assert isinstance(result["pages"], list)
    assert isinstance(result["title"], str)


def test_index_pdf_return_metadata_true_skipped_returns_empty_dict(sample_pdf, monkeypatch, cloud_mode):
    # ``t3=`` is LOAD-BEARING, not decoration (nexus-c7l4n). Since RDR-152
    # Seam B, ``index_pdf`` resolves a None *t3* through
    # ``mcp_infra.get_t3()`` whenever ``is_vector_service_mode()`` — which is
    # unconditionally True since the RDR-155 P4a.2 serving cutover — and only
    # falls back to ``nexus.doc_indexer.make_t3`` outside service mode. The
    # ``patch("nexus.doc_indexer.make_t3")`` below is therefore a DEAD seam
    # for the write handle: without an explicit ``t3=``, this test reached
    # the REAL factory, whose cloud probe fail-closes against CI's unstamped
    # service jar (``release_version=null`` — CI builds with a plain
    # ``mvn package``; a dev box builds via ``scripts/build-gate-jar.sh``,
    # which stamps, so the failure is CI-only by construction). The two
    # passing siblings above already pass ``t3=mock_t3``; this one did not,
    # and only survived on a MagicMock another test had leaked into the
    # process-wide ``mcp_infra._t3_instance`` — a shield that the
    # ``_restore_t3_singleton`` guard (nexus-jovc9) correctly removed.
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
                    result = index_pdf(
                        sample_pdf, corpus="test", t3=mock_t3, return_metadata=True,
                    )
    assert isinstance(result, dict) and result["chunks"] == 0 and result["pages"] == []


def test_index_markdown_return_metadata_true_returns_dict(sample_md, monkeypatch, mock_t3, voyage_client):
    set_credentials(monkeypatch)
    with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
        result = index_markdown(sample_md, corpus="test", t3=mock_t3, return_metadata=True)
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
        result = index_markdown(sample_md, corpus="test", t3=mock_t3, return_metadata=True)
    assert isinstance(result, dict) and result["chunks"] == 0 and result["sections"] == 0


# nexus-sghyo (2026-08-06): test_embed_progress_callback_fires and
# test_embed_progress_callback_none_is_noop directly exercised the
# deleted client-side ``_embed_with_fallback`` — no surviving subject.
# on_progress threading through the surviving orchestration path is
# still covered by test_index_threads_on_progress below.


@pytest.mark.parametrize("indexer", ["pdf", "markdown"])
def test_index_threads_on_progress(indexer, sample_pdf, sample_md, monkeypatch, mock_t3, voyage_client):
    """nexus-sghyo (2026-08-06): the single-flush (small-doc) path's
    ``on_progress`` firing was wired ENTIRELY through the deleted
    client-side ``_embed_with_fallback`` (per-API-batch progress during
    client embedding) — embedding is server-side now, so a single-chunk
    document through ``_index_document`` has no client-side batching to
    report progress on, and ``on_progress`` no longer fires here.
    Multi-batch progress reporting on the surviving INCREMENTAL path
    (large docs) is still covered by
    ``test_index_pdf_incremental_progress_fires``. This test now only
    proves the callback is accepted and does not break the pipeline.
    """
    set_credentials(monkeypatch)
    progress: list[tuple] = []
    path = sample_pdf if indexer == "pdf" else sample_md
    if indexer == "pdf":
        with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
            with pdf_extract_patches_ctx() as pep:
                result = index_pdf(path, corpus="mybook", t3=mock_t3, on_progress=lambda d, t: progress.append((d, t)))
    else:
        chunk = MagicMock()
        chunk.text = "chunk text"
        chunk.chunk_index = 0
        chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 10, "page_number": 0, "header_path": "Hello"}
        with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
            with patch("nexus.doc_indexer.SemanticMarkdownChunker") as chk_cls:
                chk_cls.return_value.chunk.return_value = [chunk]
                result = index_markdown(path, corpus="docs", t3=mock_t3, on_progress=lambda d, t: progress.append((d, t)))
    # nexus-8g79.23: exact count — one mock chunk planted, one indexed.
    assert result == 1


def test_stale_chunk_pruning_deleted_as_dead_code(sample_md, monkeypatch, voyage_client, cloud_mode):
    """nexus-tbkk1: ``_index_document``'s stale-chunk prune block
    (formerly triggered after the incremental-staleness check, via
    ``_identity_where``'s ``source_path`` fallback) is DELETED dead code.

    Supersedes RDR-180's (nexus-jxizy.3) ``test_stale_chunk_pruning_
    deletes_old_ids``, which asserted this same prune deleted T3 chunks
    whose id fell out of the current upsert set. That test's ``mock_col.
    get`` used an unconditional ``side_effect`` list keyed on CALL ORDER,
    never inspecting the ``where=`` argument — so it never actually
    proved the production where-clause (``{"source_path": file_path}``)
    matches anything. RDR-102 D2 (2026-05-02) removed ``source_path``
    from ``make_chunk_metadata`` entirely, so in real production it
    never does, and the prune query was permanently a zero-row no-op —
    see nexus-tbkk1 and nexus-tp8yk's test_index_pdf_prune_union_guard_
    wired_at_call_site (the sibling PDF-branch discovery that prompted
    this bead). The real cross-document prune protection is
    ``mcp_infra._sweep_superseded_vectors``, proven at tests/
    integration/test_tp8yk_manifest_never_outruns_chunks.py::
    test_union_guard_keeps_shared_chunk_at_the_production_wiring.

    Kill control: only ONE ``col.get`` call is seeded (the surviving
    content_hash-keyed staleness check) — if the deleted prune block
    were reintroduced, it would issue a SECOND ``col.get`` call past the
    seeded ``side_effect`` list, raising ``StopIteration`` inside
    ``index_markdown`` and failing this test.
    """
    set_credentials(monkeypatch)
    new_chunk_texts = [f"chunk text {i}" for i in range(3)]
    stale_ids = {
        hashlib.sha256(f"old text {i}".encode()).hexdigest()
        for i in range(2)
    }
    mock_col = MagicMock()
    mock_col.get.side_effect = [
        # ONLY call expected post-deletion: the content_hash-keyed
        # incremental staleness check (one prior chunk, old content_hash).
        {"ids": [next(iter(stale_ids))],
         "metadatas": [{"content_hash": "old_hash",
                        "embedding_model": "voyage-context-3"}]},
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
            chk_cls.return_value.chunk.return_value = chunks
            index_markdown(sample_md, corpus="docs", t3=mock_t3)
    assert captured_deletes == []
    mock_col.delete.assert_not_called()


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
                        # nexus-sghyo (2026-08-06): explicit t3= bypasses the
                        # service/non-service T3-handle resolution entirely
                        # (doc_indexer.py: "db = t3" short-circuit) — needed
                        # now that the suite runs under the ambient
                        # service-mode default instead of the retired
                        # NX_STORAGE_BACKEND_VECTORS=chroma legacy opt-out,
                        # which used to keep the make_t3 patch load-bearing.
                        result = index_pdf(self.path, corpus="test", t3=t3,
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


def test_index_pdf_incremental_prune_deleted_as_dead_code(incr_setup) -> None:
    """nexus-tbkk1: ``_index_pdf_incremental``'s stale-chunk prune block
    (formerly gated on ``_identity_where``'s ``source_path`` fallback,
    the >128-chunk sibling of the small-doc branch's identical deletion
    — see ``test_index_pdf_small_doc_prune_deleted_as_dead_code`` and
    ``_index_document``'s ``test_stale_chunk_pruning_deleted_as_dead_
    code``) is DELETED dead code. No prior test exercised this specific
    prune site directly; this is new coverage, not a superseding
    rewrite.

    Kill control: seed a T3 double whose ``col.get`` would, under the
    pre-nexus-tbkk1 code, be queried a SECOND time (via the prune's
    ``where={"source_path": ...}`` clause) and report a legacy-shaped
    stale row. Reintroducing the deleted block makes ``col.delete``
    fire; this test fails the moment that regresses.
    """
    n_chunks = incr_setup.threshold + 10
    mock_chunks = _make_n_chunks(n_chunks)
    pdf_path_str = str(incr_setup.path.resolve())

    def _col_get(where=None, include=None, limit=None, offset=0, **kw):
        if where == {"source_path": pdf_path_str}:
            if offset == 0:
                return {"ids": ["stale-legacy-id"]}
            return {"ids": []}
        return {"ids": [], "metadatas": []}

    mock_col = MagicMock()
    mock_col.get.side_effect = _col_get
    mock_col.delete = MagicMock()
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
                result = index_pdf(incr_setup.path, corpus="test", t3=t3, embed_fn=_fake_embed)
    assert result == n_chunks
    mock_col.delete.assert_not_called()


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
                    result = index_pdf(sample_pdf, corpus="test", t3=mock_t3, embed_fn=_fake_embed)
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


# nexus-sghyo (2026-08-06): test_token_bucket_rate_limiter,
# test_token_bucket_zero_burst_still_works, test_parallel_embed_
# preserves_order, and test_parallel_embed_progress_fires_for_each_batch
# directly exercised the deleted ``_TokenBucket`` / ``_embed_with_
# fallback`` parallel-CCE machinery — no surviving subject.


class TestStreamingRouting:
    def test_streaming_never_forces_batch_path(self, tmp_path):
        pdf = tmp_path / "small.pdf"
        pdf.write_bytes(b"dummy")
        with (
            # nexus-sghyo (2026-08-06): _has_credentials is deleted — the
            # service-mode guard (checked first, ambient test default) is
            # what now prevents the retired credential-fallback branch
            # from firing, so no patch is needed here any more.
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
            # nexus-sghyo (2026-08-06): see test_streaming_never_forces_
            # batch_path above — _has_credentials is deleted; the
            # service-mode guard already prevents the retired branch.
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


class TestStreamingReturnMetadata:
    """nexus-w6wp0: index_pdf's streaming return_metadata=True read used to
    query T3 via _identity_where's dead source_path fallback (RDR-102 D2
    removed source_path from make_chunk_metadata), so it silently always
    returned pages=[]/title=""/author="" -- even when the pipeline had just
    written real chunks.

    The fix has two paths (review round, code-review-expert +
    substantive-critic, 2026-08-05):
    - doc_id known (the catalog-registered common case): scope the read to
      THIS document's own catalog manifest (doc_id -> chash list) via
      ``_metadata_for_doc_id`` -- collision-safe even when another document
      in the same collection shares this one's content_hash.
    - doc_id empty (no-catalog ingest contract): fall back to the
      content_hash-keyed where-clause (the identity the staleness check
      and pipeline_stages._enrich_metadata_from_extraction's post-pass
      already use).

    Both paths share a fail-loud guard for the genuinely-anomalous case of
    chunks>0 but no metadata found. ``doc_id`` resolution is explicitly
    controlled per test (patching ``_register_or_lookup_doc_id``) rather
    than left to whatever catalog/service happens to be ambiently
    reachable -- letting it resolve implicitly is exactly what made an
    earlier draft of these tests nondeterministic across environments.
    """

    @staticmethod
    def _vector_with_retry_side_effect(populated_metadatas):
        """Distinguish the staleness-check query (limit=1, must miss so the
        run proceeds) from the no-catalog-fallback metadata-read query
        (limit=300), and within the metadata-read query, distinguish a
        content_hash-keyed where (the fix) from a source_path-keyed where
        (the dead nexus-tbkk1 branch this bug used) -- so the test fails
        loud if a future edit reverts to the dead identity instead of
        merely happening to pass.
        """
        def _side_effect(_fn, *, where, include=None, limit=None, offset=None, **_kw):
            if limit == 1:
                # staleness pre-check: report "not found" so indexing proceeds
                return {"metadatas": []}
            if "content_hash" in where:
                return {"ids": [m["_id"] for m in populated_metadatas], "metadatas": populated_metadatas}
            # dead source_path branch (nexus-tbkk1): matches nothing in
            # production since RDR-102 D2 -- this is the bug being fixed.
            return {"ids": [], "metadatas": []}
        return _side_effect

    def _run(self, tmp_path, *, pipeline_count, populated_metadatas, doc_id="",
              extra_patches=()):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"dummy")
        vwr_side_effect = self._vector_with_retry_side_effect(populated_metadatas)
        with ExitStack() as stack:
            # nexus-sghyo (2026-08-06): _has_credentials is deleted — the
            # service-mode guard (ambient test default) already prevents
            # the retired non-service credential-fallback branch.
            stack.enter_context(patch("nexus.doc_indexer._sha256", return_value="abc123"))
            stack.enter_context(patch("nexus.doc_indexer.make_t3"))
            stack.enter_context(patch("nexus.doc_indexer._register_or_lookup_doc_id", return_value=doc_id))
            stack.enter_context(patch("nexus.doc_indexer._check_document_fork", return_value=[]))
            mock_vwr = stack.enter_context(
                patch("nexus.doc_indexer._vector_with_retry", side_effect=vwr_side_effect)
            )
            mock_pymupdf_open = stack.enter_context(patch("pymupdf.open"))
            stack.enter_context(patch("nexus.pipeline_stages.pipeline_index_pdf", return_value=pipeline_count))
            for p in extra_patches:
                stack.enter_context(p)
            mock_doc = MagicMock()
            mock_doc.__enter__ = MagicMock(return_value=mock_doc)
            mock_doc.__exit__ = MagicMock(return_value=False)
            mock_doc.__len__ = MagicMock(return_value=3)
            mock_pymupdf_open.return_value = mock_doc
            result = index_pdf(pdf, "test", streaming="always", return_metadata=True)
        return result, mock_vwr

    def test_streaming_return_metadata_reflects_pipeline_chunks_no_catalog(self, tmp_path):
        """RED against pre-fix code (source_path where-clause always
        matches zero rows -> pages/title/author silently empty despite
        chunks == 5); GREEN once the metadata query keys on content_hash.
        doc_id="" (no-catalog fallback branch) so this exercises the
        content_hash-keyed query path directly."""
        populated = [
            {"_id": "c1", "page_number": 1, "title": "My Paper", "source_author": "A. Thor"},
            {"_id": "c2", "page_number": 2, "title": "My Paper", "source_author": "A. Thor"},
        ]
        result, mock_vwr = self._run(tmp_path, pipeline_count=5, populated_metadatas=populated, doc_id="")
        assert result == {
            "chunks": 5,
            "pages": [1, 2],
            "title": "My Paper",
            "author": "A. Thor",
        }
        # Kill control: assert the metadata-read call actually used the
        # content_hash identity, not merely that the mock happened to return
        # something. A regression back to the dead source_path where-clause
        # must fail this even if some other mock quirk made the dict match.
        meta_read_calls = [
            c for c in mock_vwr.call_args_list
            if c.kwargs.get("limit") == 300
        ]
        assert meta_read_calls, "expected a limit=300 metadata-read call"
        assert meta_read_calls[0].kwargs["where"] == {"content_hash": "abc123"}

    def test_streaming_return_metadata_uses_manifest_when_doc_id_known(self, tmp_path):
        """doc_id present (the catalog-registered common case): the
        metadata read must go through _metadata_for_doc_id (manifest-
        scoped), not the content_hash where-clause -- proven by mocking
        _metadata_for_doc_id directly and asserting it was called with
        this run's doc_id, and that the where-clause path was NOT
        consulted for the returned metadata."""
        manifest_meta = [
            {"page_number": 1, "title": "My Paper", "source_author": "A. Thor"},
            {"page_number": 4, "title": "My Paper", "source_author": "A. Thor"},
        ]
        mock_meta_for_doc_id = MagicMock(return_value=manifest_meta)
        result, mock_vwr = self._run(
            tmp_path, pipeline_count=5, populated_metadatas=[], doc_id="1.1.1",
            extra_patches=(patch("nexus.doc_indexer._metadata_for_doc_id", mock_meta_for_doc_id),),
        )
        assert result == {
            "chunks": 5,
            "pages": [1, 4],
            "title": "My Paper",
            "author": "A. Thor",
        }
        mock_meta_for_doc_id.assert_called_once()
        call_args = mock_meta_for_doc_id.call_args
        assert call_args.args[1] == "1.1.1" or call_args.kwargs.get("doc_id") == "1.1.1"
        # The content_hash where-branch must not have been consulted at
        # all for the metadata read (only the staleness pre-check, if
        # anything, may have hit _vector_with_retry).
        meta_read_calls = [c for c in mock_vwr.call_args_list if c.kwargs.get("limit") == 300]
        assert not meta_read_calls, "manifest-scoped path must not fall through to the where-clause query"

    def test_streaming_return_metadata_manifest_scoping_avoids_content_hash_collision(self, tmp_path):
        """CRITICAL-2 regression (substantive-critic, 2026-08-05): two
        catalog documents in the same collection share this run's
        content_hash (byte-identical content -- e.g. duplicate PDFs). A
        bare content_hash-keyed query would return the WRONG document's
        title/author (or a mix); the manifest-scoped path (doc_id known)
        must return ONLY this document's own metadata, ignoring whatever
        the content_hash-keyed query would have produced."""
        sibling_content_hash_rows = [
            {"_id": "sibling-1", "page_number": 9, "title": "Sibling Paper", "source_author": "B. Other"},
        ]
        this_doc_manifest_meta = [
            {"page_number": 1, "title": "My Paper", "source_author": "A. Thor"},
        ]
        mock_meta_for_doc_id = MagicMock(return_value=this_doc_manifest_meta)
        result, mock_vwr = self._run(
            tmp_path, pipeline_count=1, populated_metadatas=sibling_content_hash_rows, doc_id="1.1.1",
            extra_patches=(patch("nexus.doc_indexer._metadata_for_doc_id", mock_meta_for_doc_id),),
        )
        assert result["title"] == "My Paper"
        assert result["author"] == "A. Thor"
        assert result["pages"] == [1]
        # The sibling's decoy row must never have leaked into the result --
        # if it had, title would be "Sibling Paper" / pages would include 9.
        assert "Sibling Paper" not in str(result)

    def test_streaming_return_metadata_empty_when_pipeline_wrote_nothing(self, tmp_path):
        """Kill control: a legitimate zero-chunk run (e.g. skipped as
        already-complete) must still return the all-empty dict, not raise --
        the fail-loud guard is for chunks>0-but-no-metadata only."""
        result, _ = self._run(tmp_path, pipeline_count=0, populated_metadatas=[], doc_id="")
        assert result == {"chunks": 0, "pages": [], "title": "", "author": ""}

    def test_streaming_return_metadata_fail_loud_on_inconsistent_empty_read_no_catalog(self, tmp_path):
        """FAIL LOUD (no-catalog branch): the pipeline reports chunks
        written (count > 0) but the (correctly content_hash-keyed)
        metadata query finds none -- a genuine inconsistency, not a
        legitimate empty result. Must raise, never silently return an
        empty dict."""
        from nexus.errors import IndexingError

        with pytest.raises(IndexingError):
            self._run(tmp_path, pipeline_count=5, populated_metadatas=[], doc_id="")

    def test_streaming_return_metadata_fail_loud_on_inconsistent_empty_read_with_doc_id(self, tmp_path):
        """FAIL LOUD (manifest branch): doc_id known, but
        _metadata_for_doc_id finds nothing despite count > 0 -- same
        guard, same message contract, exercised on the collision-safe
        path this time."""
        from nexus.errors import IndexingError

        mock_meta_for_doc_id = MagicMock(return_value=[])
        with pytest.raises(IndexingError):
            self._run(
                tmp_path, pipeline_count=5, populated_metadatas=[], doc_id="1.1.1",
                extra_patches=(patch("nexus.doc_indexer._metadata_for_doc_id", mock_meta_for_doc_id),),
            )

    def test_metadata_for_doc_id_queries_col_by_manifest_chashes(self):
        """Round-2 critic note: the tests above mock _metadata_for_doc_id
        wholesale to pin index_pdf's branch preference (doc_id present ->
        manifest path) -- this test pins the HELPER's own behavior instead,
        exercising its real col.get(ids=...) call against a fake col (no
        mocking of _metadata_for_doc_id itself, and _vector_with_retry runs
        for real too -- only the catalog reader and the T3 collection are
        doubles). Verifies: manifest rows -> deduped, sorted chash list ->
        col.get(ids=<those chashes>, include=["metadatas"]) -> the
        metadatas T3 returned, reader closed."""
        from types import SimpleNamespace

        from nexus.doc_indexer import _metadata_for_doc_id

        manifest_rows = [
            SimpleNamespace(chash="chash-b"),
            SimpleNamespace(chash="chash-a"),
            SimpleNamespace(chash="chash-a"),  # duplicate row, same chash
            SimpleNamespace(chash=""),  # no-chash row must be filtered out
        ]
        fake_reader = MagicMock()
        fake_reader.get_manifest.return_value = manifest_rows

        fake_col = MagicMock()
        fake_col.get.return_value = {
            "ids": ["chash-a", "chash-b"],
            "metadatas": [
                {"page_number": 1, "title": "My Paper", "source_author": "A. Thor"},
                {"page_number": 2, "title": "My Paper", "source_author": "A. Thor"},
            ],
        }

        with patch("nexus.catalog.factory.make_catalog_reader", return_value=fake_reader):
            result = _metadata_for_doc_id(fake_col, "1.1.1")

        fake_reader.get_manifest.assert_called_once_with("1.1.1")
        fake_reader.close.assert_called_once()
        fake_col.get.assert_called_once_with(ids=["chash-a", "chash-b"], include=["metadatas"])
        assert result == [
            {"page_number": 1, "title": "My Paper", "source_author": "A. Thor"},
            {"page_number": 2, "title": "My Paper", "source_author": "A. Thor"},
        ]

    def test_metadata_for_doc_id_empty_manifest_short_circuits_without_querying_col(self):
        """An empty manifest (zero live chash rows) returns [] without
        even calling col.get -- distinct from a genuine "queried and found
        nothing" case, and cheaper (no round-trip for a document with no
        chunks recorded)."""
        from nexus.doc_indexer import _metadata_for_doc_id

        fake_reader = MagicMock()
        fake_reader.get_manifest.return_value = []
        fake_col = MagicMock()

        with patch("nexus.catalog.factory.make_catalog_reader", return_value=fake_reader):
            result = _metadata_for_doc_id(fake_col, "1.1.1")

        assert result == []
        fake_col.get.assert_not_called()
        fake_reader.close.assert_called_once()


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
    # nexus-sghyo (2026-08-06): embed_fn dispatch reads ambient
    # is_vector_service_mode() regardless of the injected t3= handle —
    # an explicit chroma opt-out is still the only way to reach the
    # LOCAL-MODE embed branch (_make_local_embed_fn) these callers need.
    # Not the deleted non-service/non-local CLOUD credential path.
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
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


# nexus-sghyo (2026-08-06): the ``_legacy_vector_backend`` autouse fixture
# that force-pinned this whole module to NX_STORAGE_BACKEND_VECTORS=chroma
# (RDR-152's opt-out for the legacy chroma/local embed pipeline) is
# RETIRED — that pipeline is deleted outright: the client no longer
# embeds via Voyage (Hal determination 2026-07-28), and non-service,
# non-local ingestion now fails loud instead of falling through to a
# client-side embed. The module runs under the ambient service-mode
# default like production; tests that need local-mode behavior pass
# ``embed_fn=`` explicitly (already the pattern most tests here used).
