# SPDX-License-Identifier: AGPL-3.0-or-later
"""T1: indexer.py — status transitions, error path, credential skip, hidden file filter."""
import hashlib
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from voyageai.object.embeddings import EmbeddingsObject

from nexus.indexer import CredentialsMissingError, index_repository
from tests.conftest import make_vector_test_client

# RDR-109 Phase 2: this file asserts cloud-mode canonical behavior
# (voyage-* embedder names, canonical-set defaults). The cloud_mode
# fixture sets credentials and forces ``is_local_mode()`` to False so
# the assertions hold regardless of the host environment.
#
# CATALOG SEAM (nexus-i711w C-store; supersedes the nexus-aqbrk
# ``local_catalog_backend`` module pin): this module drives the indexer with a
# FULLY STUBBED config — ``_patches`` replaces ``nexus.config.load_config``
# with ``_DEFAULT_CONFIG`` and ``get_credential`` with a fixed fake — so the
# service catalog's endpoint resolution can never work here BY
# CONSTRUCTION (the aqbrk finding: ``_prune_deleted_files`` ->
# ``chashes_for_collection`` reached HttpCatalogClient with a garbage
# base_url and died on ``httpx.UnsupportedProtocol``). The old module pin
# expressed "the catalog resolves to nothing" INCIDENTALLY (SQLite backend +
# an uninitialised tmp config dir); that pin retires with the SQLite catalog
# itself, so ``_patches`` now states the same thing EXPLICITLY:
#   - nexus-sghyo (2026-08-06): the ``_legacy_vector_backend`` autouse
#     fixture that used to pin ``NX_STORAGE_BACKEND_VECTORS=chroma`` here
#     is RETIRED — the legacy chroma/local embed pipeline it opted into is
#     deleted outright (client-side Voyage embedding retired, Hal
#     determination 2026-07-28). The module now runs under the ambient
#     SERVICE-mode default, which resolves T3 through
#     ``mcp_infra.get_t3()``'s memoised singleton rather than calling
#     ``make_t3()`` per-invocation; ``_reset_t3_singleton_per_test`` below
#     (and ``_patches``' own reset) evict it so no test leaks a stale
#     ``db`` mock into a later one.
#   - ``make_catalog_reader`` -> None — the catalog-absent no-op contract
#     every ``_run_index`` journey here was written against (migration,
#     prune, self-heal and head-hash writes all None-guard), and
#   - ``_catalog_hook`` -> {} — its graceful catalog-absent return value
#     (the "Registering N catalog entries" phases are emitted by
#     ``_run_index`` around the call, so the on_phase assertions still
#     exercise real code).
#
# NOT scope reduction — the indexer->catalog journey on the LIVE substrate is
# covered where it can actually run: tests/test_catalog_indexer_hook.py
# drives ``_catalog_hook`` through tests/_catalog_fixture_ops.ActiveCatalog
# (owner create/reuse, register/update, batching, fairness yields, per-file
# fallbacks), and the catalog->GC round trip is exercised against the ACTIVE
# catalog by test_prune_deleted_files_round_trip_with_real_catalog below.
#
# ONE list, not a second assignment: a second ``pytestmark = ...`` REPLACES
# the first rather than appending (it silently no-opped a pin in
# test_catalog_consolidation.py — commit 0eefc06a).
pytestmark = pytest.mark.usefixtures("cloud_mode")


@pytest.fixture(autouse=True)
def _reset_t3_singleton_per_test():
    """Evict ``mcp_infra``'s memoised T3 singleton on both sides of every
    test in this file.

    nexus-sghyo (2026-08-06): with the ``_legacy_vector_backend`` chroma
    pin retired, ``_run_index`` / ``_run_index_frecency_only`` run under
    the ambient SERVICE-mode default and resolve T3 via
    ``mcp_infra.get_t3()``, which memoises the FIRST call's result
    (``_t3_instance``) for the life of the process. Tests here patch
    ``nexus.db.make_t3`` per-test (directly or via ``_patches``) expecting
    a fresh ``db`` mock every time; without this reset, the first test in
    file order to populate the singleton leaks its mock into every later
    test that never re-triggers construction — same hazard class as
    ``tests/conftest.py``'s ``_reset_service_t2_db`` one tier over.
    """
    from nexus.mcp_infra import reset_singletons

    reset_singletons()
    yield
    reset_singletons()


_DEFAULT_CONFIG = {
    "server": {"ignorePatterns": []},
    "indexing": {"code_extensions": [], "prose_extensions": [],
                 "rdr_paths": ["docs/rdr"], "include_untracked": False},
}
_BASE_REG = {
    "collection": "code__repo",
    "code_collection": "code__repo",
    "docs_collection": "docs__repo",
}


def _voyage(n):
    r = MagicMock(spec=EmbeddingsObject)
    r.embeddings = [[float(i)] * 3 for i in range(n)]
    m = MagicMock(); m.embed.return_value = r
    return m


def _chunk(text="x = 1", fname="main.py", ext=".py", idx=0, count=1, ls=1, le=1):
    return {"line_start": ls, "line_end": le, "text": text, "chunk_index": idx,
            "chunk_count": count, "ast_chunked": False, "filename": fname, "file_extension": ext}


def _tracking_db():
    ups: dict[str, list] = {}
    cols: dict[str, MagicMock] = {}
    def goc(name):
        if name not in cols:
            c = MagicMock(); c.get.return_value = {"metadatas": [], "ids": []}; cols[name] = c
        return cols[name]
    def cap(collection_name, ids, documents, embeddings, metadatas, *, force_re_embed=False):
        ups.setdefault(collection_name, []).extend(metadatas)
    db = MagicMock()
    db.get_or_create_collection.side_effect = goc
    db.get_collection.side_effect = goc
    db.get_collection.side_effect = goc
    db.upsert_chunks_with_embeddings.side_effect = cap
    return db, ups, cols


def _mock_db():
    col = MagicMock(); col.get.return_value = {"metadatas": [], "ids": []}
    db = MagicMock(); db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    return db, col


def _reg(override=None):
    m = MagicMock(); m.get.return_value = {**(override or _BASE_REG)}; return m


@contextmanager
def _patches(db, *, cfg=None, extra=None):
    patches = {
        "nexus.frecency.batch_frecency": {"return_value": {}},
        "nexus.ripgrep_cache.build_cache": {},
        "nexus.indexer._git_metadata": {"return_value": {}},
        "nexus.config.load_config": {"return_value": cfg or _DEFAULT_CONFIG},
        "nexus.config.get_credential": {"return_value": "fake-key"},
        "nexus.db.make_t3": {"return_value": db},
        "voyageai.Client": {},
        # Catalog seam (nexus-i711w, see module header): the stubbed config
        # above severs service-catalog endpoint resolution, so the catalog is
        # explicitly ABSENT for these journeys — reader None makes migration/
        # prune/self-heal take their documented catalog-absent no-op paths,
        # and the hook returns its documented empty doc_id map. Both call
        # sites import from nexus.catalog.factory / resolve
        # nexus.indexer._catalog_hook at CALL time, so these targets hold.
        # PORT-VERIFY: no journey below asserts _catalog_hook side effects
        # (the "Registering N catalog entries" phases are emitted by
        # _run_index around the call) — if one starts failing on a missing
        # doc_id, it was silently depending on the hook and needs its own
        # explicit fixture, not a weaker patch.
        "nexus.catalog.factory.make_catalog_reader": {"return_value": None},
        "nexus.indexer._catalog_hook": {"return_value": {}},
    }
    if extra: patches.update(extra)
    mocks, stack = {}, []
    for t, kw in patches.items():
        p = patch(t, **kw); m = p.start(); stack.append(p); mocks[t.split(".")[-1]] = m
    # nexus-sghyo (2026-08-06): with _legacy_vector_backend retired, _run_index
    # runs under ambient SERVICE mode by default, which resolves T3 through
    # mcp_infra.get_t3()'s memoised singleton (_t3_instance) rather than
    # calling nexus.db.make_t3() directly on every invocation. Without a
    # reset, the FIRST test in file order to populate the singleton leaks
    # its `db` mock into every later test — the exact class of hazard
    # tests/conftest.py's _reset_service_t2_db docstring describes one
    # tier over. Evict on both sides so this test cannot leak forward or
    # start dirty.
    from nexus.mcp_infra import reset_singletons
    reset_singletons()
    try: yield mocks
    finally:
        for p in reversed(stack): p.stop()
        reset_singletons()


@contextmanager
def _cb_patches(db, *, cfg=None, code=1, prose=1, rdr_written=None):
    """``rdr_written``, when set, is the NUMBER OF RDR FILES that should
    count as written this run (run_file_loop counts files with a nonzero
    return, not summed chunk counts — nexus-3lswy) — the first
    ``rdr_written`` rdr__ calls return 1, the rest return 0. Distinguishes
    RDR-collection calls to the now-shared ``_index_prose_file`` (RDR files
    route through the same 4th-loop wrapper as prose files, targeting
    ``rdr__`` instead of ``docs__``) from ordinary prose calls, which keep
    returning ``prose``."""
    _rdr_call_count = [0]

    def _prose_side_effect(_file, _repo, collection_name, *_a, **_kw):
        if rdr_written is not None and collection_name.startswith("rdr__"):
            _rdr_call_count[0] += 1
            return 1 if _rdr_call_count[0] <= rdr_written else 0
        return prose

    prose_patch = (
        {"side_effect": _prose_side_effect} if rdr_written is not None
        else {"return_value": prose}
    )
    extra = {
        "nexus.indexer._index_code_file": {"return_value": code},
        "nexus.indexer._index_prose_file": prose_patch,
        "nexus.indexer._prune_misclassified": {},
        "nexus.indexer._prune_deleted_files": {},
    }
    with _patches(db, cfg=cfg, extra=extra) as mocks: yield mocks


@pytest.fixture
def registry():
    return _reg()


def _init_git(repo):
    for cmd in [["git","init","-b","main"], ["git","config","user.email","t@t"],
                ["git","config","user.name","T"], ["git","add","."], ["git","commit","-m","init"]]:
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)


# ── Status transitions ──────────────────────────────────────────────────────

# ── Credentials / early exit ────────────────────────────────────────────────

def test_run_index_raises_credentials_missing_without_credentials(tmp_path, monkeypatch):
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "hello.py").write_text("print('hi')\n")
    reg = _reg()
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    # nexus-sghyo (2026-08-06): non-service mode must be forced explicitly now
    # (ambient default is service mode, which no longer raises CredentialsMissingError
    # for a missing Voyage credential — the client does not embed at all).
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")

    # Reader None (nexus-i711w seam): the non-conformant registry names
    # (code__repo/docs__repo) re-route through _repo_collection_or_legacy
    # BEFORE the credentials check; keep it on the no-catalog synth path.
    with patch("nexus.frecency.batch_frecency", return_value={}), \
         patch("nexus.ripgrep_cache.build_cache"), \
         patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
         patch("nexus.db.make_t3") as mt3:
        with pytest.raises(CredentialsMissingError): _run_index(repo, reg)
    mt3.assert_not_called()


# ── Cache path collision ────────────────────────────────────────────────────

def test_cache_path_includes_repo_hash(tmp_path, monkeypatch):
    from nexus.indexer import _run_index
    a, b = tmp_path / "myproject", tmp_path / "other" / "myproject"
    a.mkdir(); b.mkdir(parents=True)
    seen: list[Path] = []
    reg = _reg({"collection": "code__myproject", "code_collection": "code__myproject",
                "docs_collection": "docs__myproject"})
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    # nexus-sghyo (2026-08-06): non-service mode must be forced explicitly now
    # (ambient default is service mode, which no longer raises CredentialsMissingError
    # for a missing Voyage credential — the client does not embed at all).
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")

    with patch("nexus.frecency.batch_frecency", return_value={}), \
         patch("nexus.ripgrep_cache.build_cache", side_effect=lambda r, cp, s: seen.append(cp)), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
         patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG):
        with pytest.raises(CredentialsMissingError): _run_index(a, reg)
        with pytest.raises(CredentialsMissingError): _run_index(b, reg)
    assert len(seen) == 2 and seen[0].name != seen[1].name


# ── Hidden file filter ──────────────────────────────────────────────────────

def test_run_index_skips_hidden_files(tmp_path, monkeypatch):
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "main.py").write_text("x = 1\n"); _init_git(repo)
    reg = _reg()
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    # nexus-sghyo (2026-08-06): non-service mode must be forced explicitly now
    # (ambient default is service mode, which no longer raises CredentialsMissingError
    # for a missing Voyage credential — the client does not embed at all).
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")

    seen: list[Path] = []
    with patch("nexus.frecency.batch_frecency", return_value={}), \
         patch("nexus.ripgrep_cache.build_cache", side_effect=lambda r, cp, s: seen.extend(f for _, f in s)), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
         patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG):
        with pytest.raises(CredentialsMissingError): _run_index(repo, reg)
    assert all(".git" not in str(p) for p in seen)
    assert any("main.py" in str(p) for p in seen)


# ── source_path absent (RDR-102 D2) ─────────────────────────────────────────

def test_run_index_chunks_have_no_source_path(tmp_path):
    """RDR-102 D2 retired ``source_path`` from the chunk schema. The
    canonical reference for "which file did this chunk come from" is
    now the catalog tumbler in ``doc_id`` (Phase A wires this for
    standalone indexers; ``nx index repo`` already wired it via
    indexer.py's ``_catalog_hook`` + ``doc_id_resolver`` closure
    pattern). The legacy ``source_path`` key MUST be absent from
    freshly-written chunks; a regression that re-introduces it would
    re-create the prune-vs-write regression cycle this RDR closes
    (RF-8).
    """
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "main.py").write_text("x = 1\n")
    cap: list = []
    db, col = _mock_db()
    db.upsert_chunks_with_embeddings.side_effect = lambda **kw: cap.extend(kw["metadatas"])
    v = _voyage(1)
    with _patches(db, extra={"nexus.chunker.chunk_file": {"return_value": [_chunk()]},
                              "voyageai.Client": {"return_value": v}}):
        _run_index(repo, _reg())
    assert cap, "expected at least one chunk to be upserted"
    leaked = [m for m in cap if "source_path" in m]
    assert not leaked, (
        f"{len(leaked)}/{len(cap)} chunks still carry source_path "
        f"(RDR-102 Phase B regression)"
    )


# ── Content-hash dedup ──────────────────────────────────────────────────────

def test_run_index_reindexes_when_embedding_model_changed(tmp_path):
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    content = "x = 1\n"; (repo / "main.py").write_text(content)
    h = hashlib.sha256(content.encode()).hexdigest()
    col = MagicMock()
    col.get.return_value = {"metadatas": [{"content_hash": h, "embedding_model": "voyage-4"}], "ids": []}
    db = MagicMock(); db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    with _patches(db, extra={"nexus.chunker.chunk_file": {"return_value": [_chunk()]},
                              "voyageai.Client": {"return_value": _voyage(1)}}):
        _run_index(repo, _reg())
    db.upsert_chunks_with_embeddings.assert_called_once()


# ── _run_index_frecency_only ────────────────────────────────────────────────

def test_frecency_only_updates_frecency_score(tmp_path):
    """nexus-afudo (2026-08-05): pinned to the doc_id-keyed path via an
    explicit ``_build_frecency_doc_id_map`` patch. Pre-fix this relied
    on the (now-deleted) legacy source_path where-filter firing when
    the catalog reader is None — the exact dead-code class nexus-afudo
    closed; a MagicMock ``col.get`` that ignores ``where=`` made that
    false confidence indistinguishable from a real pass. See
    ``test_frecency_only_skips_unmapped_files_source_path_fallback_
    deleted_as_dead_code`` for the doc_id-less case, which now skips
    instead of querying.
    """
    from nexus.indexer import _run_index_frecency_only
    repo = tmp_path / "repo"; repo.mkdir()
    src = repo / "main.py"; src.write_text("x = 1\n")
    old = {"frecency_score": 0.1, "title": "main.py:1-1"}
    col = MagicMock(); col.get.return_value = {"ids": ["c1"], "metadatas": [old]}
    db = MagicMock(); db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    with patch(
        "nexus.indexer._build_frecency_doc_id_map",
        return_value={src: "1.1.1"},
    ), \
         patch("nexus.frecency.batch_frecency", return_value={src: 0.75}), \
         patch("nexus.config.get_credential", return_value="fake-key"), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
         patch("nexus.db.make_t3", return_value=db), \
         patch("nexus.db.http_vector_client.get_http_vector_client", return_value=db):
        # nexus-sghyo (2026-08-06): _run_index_frecency_only routes
        # service mode through get_http_vector_client() directly
        # (bypassing make_t3()) — patched above too so these tests keep
        # exercising their own db mock under the ambient service
        # default instead of a real (unreachable) HTTP client.
        _run_index_frecency_only(repo, _reg())
    kw = db.update_chunks.call_args_list[0].kwargs
    assert kw["ids"] == ["c1"]
    assert kw["metadatas"][0]["frecency_score"] == 0.75
    assert kw["metadatas"][0]["title"] == "main.py:1-1"
    where = col.get.call_args.kwargs["where"]
    assert where == {"doc_id": "1.1.1"}


def test_frecency_only_uses_doc_id_when_catalog_has_entry(tmp_path):
    """nexus-f4z9: when the catalog has the file registered under
    the repo owner, the chunk lookup keys on doc_id (post-prune
    safe) instead of source_path. WITH TEETH: a regression that drops
    the doc_id branch fails the where-filter assertion.
    """
    from nexus.indexer import _run_index_frecency_only
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "main.py"
    src.write_text("x = 1\n")
    old = {"frecency_score": 0.1, "source_path": str(src), "title": "main.py:1-1"}
    col = MagicMock()
    col.get.return_value = {"ids": ["c1"], "metadatas": [old]}
    db = MagicMock()
    db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    db.get_collection.return_value = col

    # Mock the catalog map so the file resolves to a known doc_id. The
    # reader is pinned to None (nexus-i711w seam) so the manifest-based
    # chunk resolution is skipped and the legacy where-filter — this
    # test's subject — is what fires.
    with patch(
        "nexus.indexer._build_frecency_doc_id_map",
        return_value={src: "1.1.1"},
    ), \
         patch("nexus.frecency.batch_frecency", return_value={src: 0.75}), \
         patch("nexus.config.get_credential", return_value="fake-key"), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
         patch("nexus.db.make_t3", return_value=db), \
         patch("nexus.db.http_vector_client.get_http_vector_client", return_value=db):
        # nexus-sghyo (2026-08-06): _run_index_frecency_only routes
        # service mode through get_http_vector_client() directly
        # (bypassing make_t3()) — patched above too so these tests keep
        # exercising their own db mock under the ambient service
        # default instead of a real (unreachable) HTTP client.
        _run_index_frecency_only(repo, _reg())
    where = col.get.call_args.kwargs["where"]
    assert where == {"doc_id": "1.1.1"}, (
        f"expected doc_id-keyed lookup, got {where!r}"
    )


def test_frecency_only_skips_unmapped_files_source_path_fallback_deleted_as_dead_code(
    tmp_path,
):
    """nexus-afudo (2026-08-05): the legacy source_path where-filter
    this test used to exercise (files missing from the catalog doc_id
    map) is DELETED dead code. RDR-102 D2 (2026-05-02) removed
    source_path from make_chunk_metadata for every writer, so
    ``where={"source_path": ...}`` always matched zero rows in
    production; a live-store probe (field>=! existence test) found
    zero source_path rows across 13 representative collections
    (~115k chunks). A file with no catalog doc_id now has its
    frecency refresh SKIPPED outright — no query, since the fallback
    it used to fall through to could never find anything.

    Kill control: with ``col.get`` NOT mocked with a specific
    return_value that a stray call could accidentally satisfy, an
    unintended reintroduction of the deleted where-filter would make
    ``col.get`` get called (and ``update_chunks`` would be invoked with
    whatever the default MagicMock().get() shape produces) — this
    assertion fails either way if the dead branch comes back.
    """
    from nexus.indexer import _run_index_frecency_only
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "legacy.py"
    src.write_text("z = 3\n")
    col = MagicMock()
    db = MagicMock()
    db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    with patch(
        "nexus.indexer._build_frecency_doc_id_map",
        return_value={},
    ), \
         patch("nexus.frecency.batch_frecency", return_value={src: 0.42}), \
         patch("nexus.config.get_credential", return_value="fake-key"), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
         patch("nexus.db.make_t3", return_value=db), \
         patch("nexus.db.http_vector_client.get_http_vector_client", return_value=db):
        # nexus-sghyo (2026-08-06): _run_index_frecency_only routes
        # service mode through get_http_vector_client() directly
        # (bypassing make_t3()) — patched above too so these tests keep
        # exercising their own db mock under the ambient service
        # default instead of a real (unreachable) HTTP client.
        _run_index_frecency_only(repo, _reg())
    col.get.assert_not_called()
    db.update_chunks.assert_not_called()


def test_frecency_only_skips_unindexed_files(tmp_path):
    from nexus.indexer import _run_index_frecency_only
    repo = tmp_path / "repo"; repo.mkdir()
    src = repo / "new.py"; src.write_text("y = 2\n")
    col = MagicMock(); col.get.return_value = {"ids": [], "metadatas": []}
    db = MagicMock(); db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    with patch("nexus.frecency.batch_frecency", return_value={src: 0.5}), \
         patch("nexus.config.get_credential", return_value="fake-key"), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
         patch("nexus.db.make_t3", return_value=db), \
         patch("nexus.db.http_vector_client.get_http_vector_client", return_value=db):
        # nexus-sghyo (2026-08-06): _run_index_frecency_only routes
        # service mode through get_http_vector_client() directly
        # (bypassing make_t3()) — patched above too so these tests keep
        # exercising their own db mock under the ambient service
        # default instead of a real (unreachable) HTTP client.
        _run_index_frecency_only(repo, _reg())
    db.update_chunks.assert_not_called()


def test_frecency_only_raises_credentials_missing(tmp_path, monkeypatch):
    from nexus.indexer import _run_index_frecency_only
    repo = tmp_path / "repo"; repo.mkdir()
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    # nexus-sghyo (2026-08-06): non-service mode must be forced explicitly
    # now (ambient default is service mode, which routes to the real
    # HttpVectorClient instead of raising for a missing Voyage credential
    # — the client does not embed at all).
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
    # Reader None (nexus-i711w seam): the rdr_collection fallback at
    # _run_index_frecency_only:1716 resolves _repo_collection_or_legacy
    # BEFORE the credential check; keep it on the no-catalog synth path.
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=None):
        with pytest.raises(CredentialsMissingError): _run_index_frecency_only(repo, _reg())


# ── Debug logging ────────────────────────────────────────────────────────────

def test_run_index_logs_skipped_binary_files(tmp_path):
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "main.py").write_text("x = 1\n")
    (repo / "image.bin").write_bytes(b"\x80\x81\x82\x83\xff\xfe")
    db, col = _mock_db()
    with _patches(db, extra={"nexus.chunker.chunk_file": {"return_value": [_chunk()]},
                              "voyageai.Client": {"return_value": _voyage(1)}}):
        with patch("nexus.indexer._log") as l1, patch("nexus.prose_indexer._log") as l2:
            _run_index(repo, _reg())
    # Binary extensions (.bin) are now caught at classification time (nexus-6e6u1)
    # and SKIP-logged as "skipped non-indexable file" rather than reaching the
    # byte-sniff "skipped non-text file" path in the prose/code indexers.
    calls = l1.debug.call_args_list + l2.debug.call_args_list
    assert any(
        "skipped non-indexable file" in str(c) or "skipped non-text file" in str(c)
        for c in calls
    )


def test_run_index_logs_empty_chunks(tmp_path):
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "empty.py").write_text("   \n\n   \n")
    db, _ = _mock_db()
    with _patches(db, extra={"nexus.chunker.chunk_file": {"return_value": []}}):
        with patch("nexus.indexer._log") as l1, patch("nexus.code_indexer._log") as l2:
            _run_index(repo, _reg())
    assert any("skipped file with no chunks" in str(c) for c in l1.debug.call_args_list + l2.debug.call_args_list)


# ── Content-class routing ───────────────────────────────────────────────────

def test_run_index_excludes_rdr_paths_from_docs(tmp_path):
    """nexus-3lswy: RDR files route through _index_prose_file into their OWN
    rdr__ collection (the 4th run_file_loop category), not doc_indexer's
    batch_index_markdowns — verify by content landing in rdr__ vs docs__,
    not by asserting a call to the now-retired doc_indexer entry point."""
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "README.md").write_text("# README\n\nProject description here.\n")
    rdr = repo / "docs" / "rdr"; rdr.mkdir(parents=True)
    (rdr / "ADR-001.md").write_text("# ADR-001\n\nArchitecture decision.\n")
    db, ups, _ = _tracking_db()
    with _patches(db, extra={}):  # nexus-sghyo: _embed_with_fallback deleted; service-mode stub handles embedding now
        _run_index(repo, _reg())
    # nexus-5ut2a: _run_index re-routes the non-conformant fake registry
    # name (code__repo/docs__repo) through the conformant synth, so key by
    # content-type prefix rather than the literal legacy name.
    docs_paths = [
        m.get("title", "")
        for k, v in ups.items() if k.startswith("docs__") for m in v
    ]
    rdr_paths_out = [
        m.get("title", "")
        for k, v in ups.items() if k.startswith("rdr__") for m in v
    ]
    assert docs_paths, "no docs collection received chunks"
    assert rdr_paths_out, "no rdr collection received chunks"
    # RDR-102 D2: source_path is gone; title carries
    # "{relpath}:chunk-{i}" per prose_indexer.py:96.
    assert any("README.md" in p for p in docs_paths) and not any(
        "ADR-001" in p for p in docs_paths
    )
    assert any("ADR-001.md" in p for p in rdr_paths_out) and not any(
        "README.md" in p for p in rdr_paths_out
    )


def test_run_index_returns_rdr_stats(tmp_path):
    """nexus-3lswy: rdr_indexed/rdr_current derive from the 4th run_file_loop's
    per-file return counts (via _index_prose_file), not doc_indexer's
    batch_index_markdowns result dict."""
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "README.md").write_text("# README\n")
    rdr = repo / "docs" / "rdr"; rdr.mkdir(parents=True)
    (rdr / "001.md").write_text("# D\n"); (rdr / "002.md").write_text("# D2\n")
    db, _, _ = _tracking_db()

    def _prose_side_effect(file, _repo, collection_name, *_a, **_kw):
        if not collection_name.startswith("rdr__"):
            return 1
        return 1 if file.name == "001.md" else 0

    with _patches(db, extra={
        "nexus.indexer._index_prose_file": {"side_effect": _prose_side_effect},
    }):
        stats = _run_index(repo, _reg())
    assert (stats["rdr_indexed"], stats["rdr_current"], stats["rdr_failed"]) == (1, 1, 0)


@pytest.mark.parametrize("rdr_indexed,expect", [(1, True), (0, False)])
def test_index_repo_cmd_rdr_summary(tmp_path, rdr_indexed, expect):
    from click.testing import CliRunner; from nexus.cli import main
    repo = tmp_path / "repo"; repo.mkdir(); (repo / ".git").mkdir()
    stats = {"rdr_indexed": rdr_indexed, "rdr_current": 0, "rdr_failed": 0}
    runner = CliRunner()
    # Reader None (nexus-i711w seam): the command's catalog-backed registry
    # adapter (_open_catalog_or_none) degrades to its repos.json fallback,
    # matching the uninitialised-catalog state this test always ran in.
    with patch("nexus.commands.index._registry_path", return_value=tmp_path / "r.json"), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
         patch("nexus.indexer.index_repository", return_value=stats):
        r = runner.invoke(main, ["index", "repo", str(repo)])
    assert r.exit_code == 0
    assert ("RDR documents" in r.output) == expect


# ── Mixed repo routing ──────────────────────────────────────────────────────

def test_run_index_mixed_repo(tmp_path):
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "main.py").write_text("print('hello')\n")
    (repo / "README.md").write_text("# Project\n\nA simple project.\n")
    (repo / "notes.rst").write_text("Some notes about the project.\n")
    (repo / "data.txt").write_text("Should be skipped.\n")
    db, ups, _ = _tracking_db()
    # nexus-sghyo: _embed_with_fallback deleted; service-mode stub handles
    # prose/docs embedding now. voyageai.Client patch stays harmless/inert
    # (code_indexer's service-mode stub also skips it).
    with _patches(db, extra={
        "nexus.chunker.chunk_file": {"return_value": [_chunk(text="print('hello')")]},
        "voyageai.Client": {"return_value": _voyage(1)},
    }):
        _run_index(repo, _reg())
    # RDR-102 D2: source_path is gone; title carries
    # "{relpath}:chunk-{i}" per code_indexer.py:393 / prose_indexer.py:96.
    # nexus-5ut2a: key by content-type prefix — _run_index re-routes the
    # non-conformant fake name (code__repo) through the conformant synth.
    code_ups = [m for k, v in ups.items() if k.startswith("code__") for m in v]
    docs_ups = [m for k, v in ups.items() if k.startswith("docs__") for m in v]
    assert any("main.py" in m.get("title", "") for m in code_ups)
    dp = {m.get("title", "") for m in docs_ups}
    assert any("README.md" in p for p in dp) and any("notes.rst" in p for p in dp)
    assert not any("data.txt" in p for p in dp), ".txt files should be SKIP"


# ── Prune helpers ────────────────────────────────────────────────────────────

def _gc_col(
    rows: list[tuple[str, str]],
    *,
    with_fast_path: bool = False,
    fast_path_side_effect: object = None,
):
    """Build a MagicMock T3 collection. ``rows`` is a list of
    ``(chunk_id, chunk_text_hash)`` pairs; the chunk_text_hash may be
    short (synthetic-write era), [:32] (post-D1), or [:64] (full sha256
    in metadata, the actual production shape).

    ``col.get`` uses a callable ``side_effect`` so the mock survives an
    unlimited number of calls: the seeded page comes back on the first
    call and every subsequent call returns an empty page. That keeps
    the helper safe for callers that paginate (multiple offsets) or
    re-enter ``_prune_deleted_files`` (which loops over both code and
    docs collections; each iteration calls ``col.get`` at least once).

    ``spec=[...]`` (not a bare ``MagicMock()``): a permissive mock's
    ``get_all_metadata`` auto-vivifies as a callable child mock on ANY
    attribute access, so ``getattr(col, "get_all_metadata", None)`` —
    ``_fetch_all_chunk_metadata``'s fast-path probe — would find one on
    every one of this fixture's ~30 existing callers and silently
    exercise the wrong path (the exact "permissive test double" class
    review 4cb743be H3 already flagged for a sibling fixture). ``spec``
    without ``get_all_metadata`` makes that attribute access raise
    ``AttributeError``, which ``getattr``'s default catches — matching a
    pre-nexus-duoak engine / local Chroma collection, which is what
    every EXISTING caller of this fixture is meant to simulate.

    ``with_fast_path=True`` (nexus-<bead>) adds ``get_all_metadata`` to
    the spec and wires it to return the same full-collection view as the
    paginated ``get`` path by default — a single round trip instead of
    N pages, proving the fast path is what actually gets exercised
    end-to-end (``col.get.assert_not_called()``). ``fast_path_side_effect``
    overrides that default, e.g. a ``VectorServiceError`` to simulate the
    server's row-count cap (422) and force the fallback.
    """
    state = {
        "rows": {r[0]: {"chunk_text_hash": r[1]} for r in rows},
    }

    def _get(*args, ids=None, **kwargs):
        if ids is not None:
            # id-keyed fetch (the quarantine copy path): always answered
            # from live state, with embeddings/documents so copy-then-
            # delete round-trips.
            present = [i for i in ids if i in state["rows"]]
            return {
                "ids": present,
                "metadatas": [dict(state["rows"][i]) for i in present],
                "documents": [f"doc-{i}" for i in present],
                "embeddings": [[0.0, 1.0] for _ in present],
            }
        # full-collection page (the classification sweep): true
        # limit/offset pagination over the LIVE state — mirrors
        # _paginated_get's contract, survives re-scans (restore/expiry
        # walk the same collection repeatedly), and exercises real page
        # boundaries when a fixture exceeds the page size.
        offset = kwargs.get("offset", 0) or 0
        limit = kwargs.get("limit") or len(state["rows"])
        keys = list(state["rows"])[offset:offset + limit]
        return {
            "ids": keys,
            "metadatas": [dict(state["rows"][k]) for k in keys],
        }

    def _delete(*args, ids=None, **kwargs):
        for i in ids or []:
            state["rows"].pop(i, None)

    def _upsert(*args, ids=None, metadatas=None, **kwargs):
        for i, m in zip(ids or [], metadatas or []):
            state["rows"][i] = dict(m or {})

    spec = ["get", "delete", "upsert", "count"]
    if with_fast_path:
        spec.append("get_all_metadata")
    col = MagicMock(spec=spec)
    col.get.side_effect = _get
    col.delete.side_effect = _delete
    col.upsert.side_effect = _upsert
    # count() is DELIBERATELY left as a bare spec\'d MagicMock attribute
    # (no return_value/side_effect wired) — several existing callers
    # (test_prune_deleted_files_page_progress_degrades_without_count et
    # al.) rely on col.count() returning a non-int Mock by default to
    # exercise the page-progress denominator's degrade-gracefully path.
    # Callers that want a real count (e.g. the nexus-oqku empty-manifest
    # guard's diagnostic t3_chunks field) wire col.count.return_value
    # themselves.
    col._rows = state["rows"]
    if with_fast_path:
        if fast_path_side_effect is not None:
            col.get_all_metadata.side_effect = fast_path_side_effect
        else:
            def _get_all_metadata(*args, **kwargs):
                return {
                    "ids": list(state["rows"]),
                    "metadatas": [dict(v) for v in state["rows"].values()],
                }
            col.get_all_metadata.side_effect = _get_all_metadata
    return col


def _gc_db(per_collection_rows: dict[str, list[tuple[str, str]]]):
    """Build a MagicMock T3 db whose ``get_or_create_collection(name)``
    returns a per-collection ``_gc_col``. Use when a test needs DIFFERENT
    chunks for the code and docs collections; the single-shared-col
    pattern (``db.get_or_create_collection.return_value = _gc_col(...)``)
    db.get_collection.return_value = _gc_col(...)``)
    silently returns the same chunks for both collections, which would
    mask correctness bugs in any test that exercises non-empty
    references on both sides simultaneously (nexus-v7mn).

    Returns a ``(db, cols)`` pair so per-collection assertions can read
    ``cols["code__repo"].delete.call_args_list`` directly.
    """
    cols: dict[str, MagicMock] = {
        name: _gc_col(rows) for name, rows in per_collection_rows.items()
    }
    db = MagicMock()

    def _goc(name):
        # get_or_create: auto-create (the quarantine sibling path).
        if name not in cols:
            cols[name] = _gc_col([])
        return cols[name]

    def _get_only(name):
        # nexus-ks40: read paths use get_collection (raises when absent).
        if name not in cols:
            raise ValueError(f"collection {name!r} not found")
        return cols[name]

    db.get_or_create_collection.side_effect = _goc
    db.get_collection.side_effect = _get_only
    # Local-mode shape: a bare MagicMock's auto-attrs would make the db
    # look service-mode (truthy upsert_chunks_with_embeddings) and swallow
    # quarantine writes silently (review 4cb743be H3 — permissive mocks).
    db.upsert_chunks_with_embeddings = None
    # RDR-191 Phase 1: same reasoning, extended. A bare MagicMock's
    # auto-attrs make `db.gc_quarantine_orphans` (etc.) truthy, so
    # `chunk_quarantine.py`'s `*_serverside` wrappers would believe this
    # fake `db` has the RDR-191 server route and call it — the MagicMock
    # call then returns another MagicMock, which `int(result.get(...))`
    # happily coerces (MagicMock's `__int__` defaults to 1) into a
    # plausible-looking "1 row moved" success, short-circuiting
    # `_prune_deleted_files` into the server branch's `continue` WITHOUT
    # ever touching `cols[...]._rows` — every assertion in this file reads
    # `_rows` directly, so the whole client-side algorithm under test here
    # would silently never run. None signals "no HTTP GC capability",
    # exactly like `upsert_chunks_with_embeddings = None` above.
    db.gc_quarantine_orphans = None
    db.gc_restore_rereferenced = None
    db.gc_expire_quarantine = None
    return db, cols


def _gc_catalog(per_collection: dict[str, set[str]]):
    """MagicMock catalog whose ``chashes_for_collection`` reads from a dict."""
    cat = MagicMock()
    cat.chashes_for_collection.side_effect = lambda name: per_collection.get(name, set())
    return cat


def _deleted_ids(col) -> list[str]:
    out: list[str] = []
    for dc in col.delete.call_args_list:
        ids = dc.kwargs.get("ids") or (dc.args[0] if dc.args else None)
        if isinstance(ids, list):
            out.extend(ids)
    return out


# test_prune_deleted_files_orphan_chunk_deleted DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
# test_prune_deleted_files_preserves_live_synthetic_id DELETED (code-review-expert
# vacuity finding, folded into the RDR-191 Phase 6 nexus-o8dil.33 fix pass,
# 2026-08-15). Against the mock (InMemoryVectorClient-shaped) substrate this used,
# `_prune_collection_serverside` returns False (no gc_quarantine_orphans
# capability), so the retired client-side fallback it exercised no longer runs —
# `delete.call_count == 0` passed VACUOUSLY (true for any input, orphan or live,
# since nothing is ever pruned via that path any more). Adjudicated DELETE, not
# rewrite: the property itself ("a chunk whose external ID diverges from its
# content hash must still be classified by matching chunk_text_hash metadata, not
# the natural ID") is a Chroma-era artifact with no server-side equivalent to
# rewrite it against — nexus.chunks (the unified PG-native table, RDR-180/191) is
# keyed BY chash directly; there is no "synthetic ID separate from chash" concept
# for the SQL anti-join to preserve or lose, because that split cannot occur in
# this schema at all. Compare test_prune_deleted_files_idempotent below, whose
# property (idempotent re-run) IS still real and was REWRITTEN against the
# server-side path instead of deleted — see
# test_rdr191_gc_serverside_prune.py::test_serverside_prune_rerun_with_no_new_orphans_is_idempotent.


def test_prune_deleted_files_empty_manifest_skips_no_wipe(tmp_path):
    """nexus-oqku (RDR-108 Phase 4 review S1): an empty manifest is
    AMBIGUOUS. It could mean "fully-rotted corpus" OR "manifest
    backfill never ran on a fresh post-migration system." There is
    no way to distinguish these from inside ``_prune_deleted_files``
    without additional state. Safe default: skip + warn, do NOT
    wipe. Operators with a genuine "delete everything" intent have
    ``nx collection delete``; the prune sweep refuses to perform
    that destructive action implicitly.

    Pre-fix this test asserted the WIPE behavior (every chunk
    classified as orphan when ``referenced`` was empty). That was
    documenting a silent-data-loss bug, not a defensible invariant.
    """
    from nexus.indexer import _prune_deleted_files
    col = _gc_col([("id-x", "x" * 64),
                   ("id-y", "y" * 64),
                   ("id-z", "z" * 64)])
    db = MagicMock(); db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    catalog = _gc_catalog({"code__repo": set(), "docs__repo": set()})

    _prune_deleted_files("code__repo", "docs__repo", db, catalog=catalog)

    # No deletions when manifest is empty (vs T3 having chunks).
    deleted = _deleted_ids(col)
    assert deleted == [], (
        f"empty-manifest case must skip, not wipe; got deleted={deleted!r}"
    )


# test_prune_deleted_files_manifest_read_failure_skips_collection DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
# test_prune_deleted_files_chunk_without_chunk_text_hash_skipped DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
# test_prune_deleted_files_idempotent DELETED from THIS file (code-review-expert
# vacuity finding, folded into the RDR-191 Phase 6 nexus-o8dil.33 fix pass,
# 2026-08-15) — against the mock substrate this used, `_prune_collection_
# serverside` returns False and the retired client-side fallback it exercised no
# longer runs, so `delete.call_count == 0` passed VACUOUSLY on both calls
# (nothing is ever pruned via that path). Adjudicated REWRITE, not delete: unlike
# the synthetic-id test above, "re-running with no new orphans is a no-op" IS
# still a real, worth-testing property of the server-side path — see
# test_rdr191_gc_serverside_prune.py::test_serverside_prune_rerun_with_no_new_orphans_is_idempotent,
# which pins it against the real substrate instead of a mock that no longer
# exercises the code under test.


def test_prune_deleted_files_no_catalog_is_noop(tmp_path):
    """Catalog-absent (e.g. catalog not initialized) is a safe no-op:
    GC cannot run without the manifest as the source of truth."""
    from nexus.indexer import _prune_deleted_files
    col = _gc_col([("x", "x" * 64)])
    db = MagicMock(); db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col

    _prune_deleted_files("code__repo", "docs__repo", db, catalog=None)

    assert col.delete.call_count == 0
    db.get_or_create_collection.assert_not_called()


def test_prune_deleted_files_skips_when_manifest_empty_no_wipe(tmp_path):
    """nexus-oqku P0 regression: when the catalog manifest has zero
    referenced chashes for a collection that DOES have T3 chunks,
    the prune sweep MUST treat it as "cannot decide safely" (skip +
    warn), not classify every chunk as orphan and wipe the
    collection.

    Pre-fix, the per-chunk loop ran with referenced=set() and
    classified every chunk as orphan via ``if chash not in referenced``,
    deleting the entire collection silently. This fired on the first
    ``nx index repo`` run after the RDR-108 schema migration on a
    system that had not yet run manifest backfill.
    """
    from structlog.testing import capture_logs

    from nexus.indexer import _prune_deleted_files

    # Catalog has the collection registered but the manifest is empty
    # (chashes_for_collection returns set()).
    catalog = _gc_catalog({"code__repo": set(), "docs__repo": set()})

    # T3 collection exists and has chunks (would all be wiped pre-fix).
    col = _gc_col([
        ("chunk-1", "a" * 64),
        ("chunk-2", "b" * 64),
        ("chunk-3", "c" * 64),
    ])
    col.count.return_value = 3
    db = MagicMock()
    db.get_collection.return_value = col

    with capture_logs() as cap:
        _prune_deleted_files("code__repo", "docs__repo", db, catalog=catalog)

    # CRITICAL: NO chunk deletion happened. The empty-manifest case
    # is treated as a safety abort, not a green light to wipe.
    assert col.delete.call_count == 0, (
        f"empty manifest must NOT trigger deletion; "
        f"got {col.delete.call_count} delete calls"
    )

    # Warning must surface so operators know GC was skipped.
    skip_logs = [
        r for r in cap if r.get("event") == "manifest_empty_skipping_gc"
    ]
    assert skip_logs, (
        f"missing manifest_empty_skipping_gc warning; got events: "
        f"{[r.get('event') for r in cap]}"
    )
    assert skip_logs[0]["t3_chunks"] == 3
    assert skip_logs[0]["collection"] == "code__repo"


def test_prune_deleted_files_does_not_create_zombie_collections(tmp_path):
    """nexus-ks40 regression: an absent T3 collection must NOT be
    speculatively created by the prune sweep. Pre-fix, prune called
    ``db.get_or_create_collection`` which minted an empty zombie T3
    collection whenever the GC ran on a corpus whose ``code__`` or
    ``docs__`` collection had never been written. That zombie then
    showed up in ``nx catalog doctor --collections-drift``'s "T3
    collections without projection rows" list and never got cleaned
    up. Post-fix, prune uses ``get_collection`` and silently skips
    when the collection is absent.
    """
    from nexus.errors import CollectionNotFoundError as _ChromaNotFoundError
    from nexus.indexer import _prune_deleted_files

    catalog = _gc_catalog({"code__repo": set(), "docs__repo": set()})

    db = MagicMock()
    db.get_collection.side_effect = _ChromaNotFoundError(
        "Collection not found"
    )

    _prune_deleted_files("code__repo", "docs__repo", db, catalog=catalog)

    # CRITICAL: get_or_create_collection must NOT have been called
    # (the leak path). get_collection is the read-only correct path.
    db.get_or_create_collection.assert_not_called()
    # get_collection called once per collection (code, docs).
    assert db.get_collection.call_count == 2


def test_prune_misclassified_does_not_create_zombie_collections(tmp_path):
    """nexus-ks40 regression for the misclassification sweep: same
    contract as above. Absent T3 collections must NOT trip
    speculative creation; the corresponding sweep is a no-op.
    """
    from nexus.errors import CollectionNotFoundError as _ChromaNotFoundError
    from nexus.indexer import _prune_misclassified

    db = MagicMock()
    db.get_collection.side_effect = _ChromaNotFoundError(
        "Collection not found"
    )

    repo = tmp_path / "fresh-repo"
    repo.mkdir()
    (repo / "main.py").write_text("x = 1\n")

    _prune_misclassified(
        repo, "code__fresh", "docs__fresh",
        code_files=[repo / "main.py"],
        prose_files=[],
        pdf_files=[],
        db=db,
        file_to_doc_id={},
    )

    db.get_or_create_collection.assert_not_called()
    assert db.get_collection.call_count == 2


def test_prune_misclassified_uses_catalog_manifest_for_phase3_chunks(tmp_path):
    from types import SimpleNamespace
    """nexus-7zcv (RDR-108 Phase 4 review D-H4): when chunks have no
    doc_id metadata (Phase 3 removed it), the prune must resolve
    each doc_id's chashes via the catalog manifest and delete by the
    full chash (RDR-180 natural id), not via where={doc_id}.

    Reverting the manifest path makes this test fail because the
    chunks have no doc_id metadata and the legacy where-filter
    matches nothing.
    """
    from nexus.indexer import _prune_misclassified

    code_path = tmp_path / "main.py"
    code_path.write_text("x = 1\n")

    chash_a = "a" * 64
    chash_b = "b" * 64

    # docs collection contains the misclassified chunks (Phase-3 shape:
    # natural id = full chash, no doc_id in metadata).
    docs_col = MagicMock()
    docs_col.get.return_value = {
        "ids": [chash_a, chash_b],
    }

    db = MagicMock()
    db.get_collection.side_effect = lambda name: (
        docs_col if name == "docs__repo" else MagicMock()
    )

    # Catalog manifest reports both chashes belong to the code file's doc.
    # nexus-yz8bt: the prune now fetches manifests in ONE batched
    # ``get_manifests(doc_ids)`` call (backed by /manifest/get_many),
    # not a per-doc ``get_manifest`` loop.
    catalog = MagicMock()
    catalog.get_manifests.return_value = {
        "1.1.5": [
            SimpleNamespace(chash=chash_a, position=0, collection="docs__repo"),
            SimpleNamespace(chash=chash_b, position=1, collection="docs__repo"),
        ]
    }

    _prune_misclassified(
        repo=tmp_path,
        code_collection="code__repo",
        docs_collection="docs__repo",
        code_files=[code_path],
        prose_files=[],
        pdf_files=[],
        db=db,
        file_to_doc_id={code_path: "1.1.5"},
        catalog=catalog,
    )

    # Manifests were queried via the BATCH API for the doc_id, and the
    # per-doc serial path was NOT taken on the happy path.
    catalog.get_manifests.assert_any_call(["1.1.5"])
    catalog.get_manifest.assert_not_called()
    # docs col was queried with the full-chash IDs.
    get_calls = docs_col.get.call_args_list
    assert any(
        set(call.kwargs.get("ids", [])) == {chash_a, chash_b}
        for call in get_calls
    ), f"docs col.get must receive full-chash IDs; got {get_calls!r}"
    # Both chunks deleted from the wrong collection.
    deleted = _deleted_ids(docs_col)
    assert set(deleted) == {chash_a, chash_b}


def test_prune_misclassified_batch_fetch_and_absent_doc(tmp_path):
    """nexus-yz8bt (duoak.11 sink #3): the manifest resolution is ONE
    batched ``get_manifests(doc_ids)`` call, not N serial ``get_manifest``
    round-trips. A doc absent from the batch result (empty/unknown
    manifest) is treated as "no chunks to prune" — no crash, no delete.
    """
    from types import SimpleNamespace

    from nexus.indexer import _prune_misclassified

    have = tmp_path / "has_chunks.py"
    have.write_text("x = 1\n")
    absent = tmp_path / "no_manifest.py"
    absent.write_text("y = 2\n")

    chash = "a" * 64
    docs_col = MagicMock()
    docs_col.get.return_value = {"ids": [chash[:32]]}
    db = MagicMock()
    db.get_collection.side_effect = lambda name: (
        docs_col if name == "docs__repo" else MagicMock()
    )

    # Batch API returns rows only for the doc that has a manifest; the
    # other doc_id is ABSENT from the dict (per the get_manifests contract).
    catalog = MagicMock()
    catalog.get_manifests.return_value = {
        "1.1.5": [SimpleNamespace(chash=chash, position=0)],
    }

    _prune_misclassified(
        repo=tmp_path,
        code_collection="code__repo",
        docs_collection="docs__repo",
        code_files=[have, absent],
        prose_files=[],
        pdf_files=[],
        db=db,
        file_to_doc_id={have: "1.1.5", absent: "1.1.6"},
        catalog=catalog,
    )

    # Exactly one batched fetch, no per-doc serial calls.
    assert catalog.get_manifests.call_count == 1
    catalog.get_manifest.assert_not_called()
    # BOTH doc_ids must be in the batch request — including the absent one.
    # Guards against a regression that pre-filters doc_ids before the
    # batch (which would defeat the unconditional-prune invariant). Order
    # is not guaranteed: doc_ids is built from set iteration.
    assert set(catalog.get_manifests.call_args_list[0].args[0]) == {"1.1.5", "1.1.6"}
    # The doc with chunks is pruned; the absent doc contributes nothing.
    assert set(_deleted_ids(docs_col)) == {chash[:32]}


def test_prune_misclassified_all_absent_zero_deletes(tmp_path):
    """nexus-yz8bt: a fresh corpus where the batch returns manifests for
    NO doc (all absent) prunes nothing — zero col.get hits with ids, zero
    deletes — without a per-doc round-trip. The common OOTB-index shape.
    """
    from nexus.indexer import _prune_misclassified

    a = tmp_path / "a.py"
    a.write_text("x = 1\n")
    b = tmp_path / "b.py"
    b.write_text("y = 2\n")

    docs_col = MagicMock()
    db = MagicMock()
    db.get_collection.side_effect = lambda name: (
        docs_col if name == "docs__repo" else MagicMock()
    )

    catalog = MagicMock()
    catalog.get_manifests.return_value = {}  # nothing misclassified

    _prune_misclassified(
        repo=tmp_path,
        code_collection="code__repo",
        docs_collection="docs__repo",
        code_files=[a, b],
        prose_files=[],
        pdf_files=[],
        db=db,
        file_to_doc_id={a: "1.1.5", b: "1.1.6"},
        catalog=catalog,
    )

    assert catalog.get_manifests.call_count == 1
    catalog.get_manifest.assert_not_called()
    # No chash resolved => the manifest col.get(ids=) sweep never fires,
    # and nothing is deleted.
    assert _deleted_ids(docs_col) == []


def test_prune_misclassified_fallback_records_skipped_on_doubled_failure(tmp_path):
    """nexus-yz8bt: the doubled-failure path (nexus-8g79.4 class). The
    batch fails loud AND, inside the per-doc fallback, one doc's
    get_manifest also raises. The failing doc is skipped (WARNING logged,
    no crash); the healthy doc is still pruned.
    """
    from types import SimpleNamespace

    from nexus.indexer import _prune_misclassified

    good = tmp_path / "good.py"
    good.write_text("x = 1\n")
    bad = tmp_path / "bad.py"
    bad.write_text("y = 2\n")

    chash = "c" * 64
    docs_col = MagicMock()
    docs_col.get.return_value = {"ids": [chash[:32]]}
    db = MagicMock()
    db.get_collection.side_effect = lambda name: (
        docs_col if name == "docs__repo" else MagicMock()
    )

    catalog = MagicMock()
    catalog.get_manifests.side_effect = RuntimeError("page 400")  # batch fails loud

    def _per_doc(did):
        if did == "1.1.6":  # the bad doc's lookup also fails
            raise RuntimeError("transient")
        return [SimpleNamespace(chash=chash, position=0)]

    catalog.get_manifest.side_effect = _per_doc

    # Must not raise despite the doubled failure.
    _prune_misclassified(
        repo=tmp_path,
        code_collection="code__repo",
        docs_collection="docs__repo",
        code_files=[good, bad],
        prose_files=[],
        pdf_files=[],
        db=db,
        file_to_doc_id={good: "1.1.5", bad: "1.1.6"},
        catalog=catalog,
    )

    # Fell back to per-doc for both docs; the healthy one is still pruned,
    # the failing one is skipped without aborting the sweep.
    assert catalog.get_manifest.call_count == 2
    assert set(_deleted_ids(docs_col)) == {chash[:32]}


def test_prune_misclassified_falls_back_when_batch_raises(tmp_path):
    """nexus-yz8bt: if the batched ``get_manifests`` fails loud (a page
    error propagates per its contract), the prune falls back to the
    per-doc ``get_manifest`` loop unchanged — resilience preserved, the
    stale chunk is still pruned.
    """
    from types import SimpleNamespace

    from nexus.indexer import _prune_misclassified

    code_path = tmp_path / "main.py"
    code_path.write_text("x = 1\n")
    chash = "b" * 64

    docs_col = MagicMock()
    docs_col.get.return_value = {"ids": [chash[:32]]}
    db = MagicMock()
    db.get_collection.side_effect = lambda name: (
        docs_col if name == "docs__repo" else MagicMock()
    )

    catalog = MagicMock()
    catalog.get_manifests.side_effect = RuntimeError("page 400")
    catalog.get_manifest.return_value = [SimpleNamespace(chash=chash, position=0)]

    _prune_misclassified(
        repo=tmp_path,
        code_collection="code__repo",
        docs_collection="docs__repo",
        code_files=[code_path],
        prose_files=[],
        pdf_files=[],
        db=db,
        file_to_doc_id={code_path: "1.1.5"},
        catalog=catalog,
    )

    # Fell back to the per-doc path and still pruned the stale chunk.
    catalog.get_manifest.assert_any_call("1.1.5")
    assert set(_deleted_ids(docs_col)) == {chash[:32]}


# test_prune_deleted_files_per_collection_orphan_isolation DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
# test_prune_deleted_files_sweeps_rdr_collection_when_passed DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
def test_prune_deleted_files_rdr_collection_none_is_safe(tmp_path):
    """When there are no RDR files this run, rdr_collection defaults to
    None and must not be swept (no col to fetch, no-op — mirrors the
    existing code_col/docs_col is-None contract elsewhere)."""
    from nexus.indexer import _prune_deleted_files
    live_chash = "a" * 64
    col = _gc_col([("live-id", live_chash)])
    db = MagicMock(); db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    catalog = _gc_catalog({"code__repo": {live_chash}, "docs__repo": set()})

    # Must not raise, and must not call chashes_for_collection for a
    # collection name that was never passed.
    _prune_deleted_files("code__repo", "docs__repo", db, catalog=catalog)
    assert "rdr__repo" not in [
        c.args[0] for c in catalog.chashes_for_collection.call_args_list
    ]


# test_prune_deleted_files_round_trip_with_real_catalog DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
def test_run_index_prune_misclassified(tmp_path):
    """nexus-afudo (2026-08-05): pinned to the doc_id-keyed ``$in``
    path via ``file_to_doc_id`` — the legacy source_path where-filter
    this test used to route through (via a default/empty
    ``file_to_doc_id``) is deleted dead code (see
    ``test_prune_misclassified_source_path_fallback_deleted_as_
    dead_code``).
    """
    from nexus.indexer import _prune_misclassified
    repo = tmp_path / "repo"; repo.mkdir()
    main_py = repo / "main.py"
    cc = MagicMock(); cc.get.return_value = {"ids": []}
    dc = MagicMock(); dc.get.return_value = {"ids": ["stale-1"]}
    db = MagicMock(); db.get_or_create_collection.side_effect = {"code__repo": cc, "docs__repo": dc}.get
    db.get_collection.side_effect = {"code__repo": cc, "docs__repo": dc}.get
    _prune_misclassified(
        repo, "code__repo", "docs__repo", [main_py], [repo/"README.md"], [], db,
        file_to_doc_id={main_py: "doc-main"},
    )
    dc.delete.assert_called_once_with(ids=["stale-1"])
    where = dc.get.call_args.kwargs["where"]
    assert where == {"doc_id": {"$in": ["doc-main"]}}


def test_registry_c2_fallback(tmp_path):
    from nexus.indexer import _repo_collection_or_legacy, _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    reg = _reg({"collection": "code__repo", "code_collection": "code__repo"})
    # RDR-103 Phase 5: registry's docs_collection is missing, so the
    # indexer falls back to ``_repo_collection_or_legacy`` which now
    # synthesises a conformant 4-segment name from the path-derived
    # identity instead of returning the pre-Phase-5 legacy 2-segment shape.
    names: list[str] = []
    col = MagicMock(); col.get.return_value = {"metadatas": [], "ids": []}
    db = MagicMock(); db.get_or_create_collection.side_effect = lambda n: (names.append(n), col)[1]
    db.get_collection.side_effect = lambda n: (names.append(n), col)[1]
    with _patches(db):
        # Computed INSIDE the seam (nexus-i711w): with the reader pinned to
        # None both this call and _run_index's internal one take the same
        # no-catalog synth path, keeping the comparison substrate-free (the
        # old placement outside the patches relied on the sqlite pin +
        # uninitialised dir to reach the same branch).
        expected = _repo_collection_or_legacy(repo, "docs")
        _run_index(repo, reg)
    assert expected in names


# ── _git_ls_files ────────────────────────────────────────────────────────────

def test_git_ls_files_returns_tracked_files(tmp_path):
    from nexus.indexer import _git_ls_files
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "tracked.py").write_text("x = 1\n")
    (repo / ".env").write_text("SECRET=abc\n"); (repo / ".gitignore").write_text(".env\n")
    _init_git(repo)
    names = {f.name for f in _git_ls_files(repo)}
    assert "tracked.py" in names and ".env" not in names


def test_git_ls_files_with_untracked(tmp_path):
    from nexus.indexer import _git_ls_files
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "tracked.py").write_text("x = 1\n"); (repo / ".gitignore").write_text(".env\n")
    subprocess.run(["git","init","-b","main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git","config","user.email","t@t"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git","config","user.name","T"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git","add","tracked.py",".gitignore"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git","commit","-m","init"], cwd=repo, check=True, capture_output=True)
    (repo / "new.py").write_text("y = 2\n"); (repo / ".env").write_text("SECRET=abc\n")
    assert "new.py" not in {f.name for f in _git_ls_files(repo, include_untracked=False)}
    names = {f.name for f in _git_ls_files(repo, include_untracked=True)}
    assert "new.py" in names and ".env" not in names


def test_git_ls_files_fallback_on_non_git_dir(tmp_path):
    from nexus.indexer import _git_ls_files
    d = tmp_path / "x"; d.mkdir(); (d / "f.py").write_text("x=1\n")
    assert _git_ls_files(d) == []


def test_git_ls_files_raises_on_failure_in_git_repo(tmp_path):
    from nexus.indexer import _git_ls_files
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "t.py").write_text("x=1\n"); (repo / ".env").write_text("S\n"); (repo / ".gitignore").write_text(".env\n")
    _init_git(repo)
    with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
        with pytest.raises(RuntimeError, match="git ls-files failed"): _git_ls_files(repo)


# ── _should_ignore (parametrized) ───────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("uv.lock", True), ("yarn.lock", True), ("poetry.lock", True),
    ("Gemfile.lock", True), ("Cargo.lock", True), ("go.sum", True),
    ("subdir/uv.lock", True), ("a/b/c/yarn.lock", True),
    ("main.py", False), ("README.md", False), ("pyproject.toml", False), ("go.mod", False),
])
def test_should_ignore(path, expected):
    from nexus.indexer import DEFAULT_IGNORE, _should_ignore
    assert _should_ignore(Path(path), DEFAULT_IGNORE) == expected


# ── Empty-string chunk filtering ────────────────────────────────────────────

def test_index_code_file_skips_empty_text_chunks(tmp_path):
    # nexus-sghyo (2026-08-06): this test used to prove empty-string
    # filtering at the ctx.voyage_client.embed call site — that whole
    # branch is deleted outright (client-side Voyage embedding retired,
    # Hal determination 2026-07-28: "we do no embedding on the client");
    # it now raises unconditionally rather than dispatching to a client
    # embed call. Repointed at the SUPPORTED embed_fn injection point,
    # which still needs empty-chunk filtering to behave correctly (the
    # substantive behavior this test protects).
    from nexus.indexer import _index_code_file
    repo = tmp_path / "repo"; repo.mkdir(); (repo / "main.py").write_text("x = 1\n")
    db, col = _mock_db()
    embed_calls: list[list[str]] = []
    def fake_embed_fn(texts):
        embed_calls.append(list(texts))
        return [[0.1] * 3 for _ in texts]
    with patch("nexus.chunker.chunk_file", return_value=[_chunk(), _chunk(text="", idx=1, count=2)]):
        r = _index_code_file(repo/"main.py", repo, "code__repo", "voyage-code-3",
                             col, db, None, git_meta={}, now_iso="2026-01-01T00:00:00", score=1.0,
                             embed_fn=fake_embed_fn)
    assert r == 1
    assert len(embed_calls) == 1
    texts = embed_calls[0]
    assert "" not in texts and len(texts) == 1


def test_index_code_file_returns_zero_when_all_chunks_empty(tmp_path):
    from nexus.indexer import _index_code_file
    repo = tmp_path / "repo"; repo.mkdir(); (repo / "e.py").write_text("\n\n\n")
    db, col = _mock_db(); v = _voyage(0)
    with patch("nexus.chunker.chunk_file", return_value=[_chunk(text="",fname="e.py",idx=i,count=3,ls=i,le=i) for i in range(3)]):
        r = _index_code_file(repo/"e.py", repo, "code__repo", "voyage-code-3",
                             col, db, v, git_meta={}, now_iso="2026-01-01T00:00:00", score=1.0)
    assert r == 0; v.embed.assert_not_called(); db.upsert_chunks_with_embeddings.assert_not_called()


# ── Force plumbing (parametrized) ───────────────────────────────────────────

@pytest.mark.parametrize("force_val,expected", [(True, True), (False, False)])
def test_index_repository_passes_force(tmp_path, registry, force_val, expected):
    repo = tmp_path / "repo"; repo.mkdir()
    # Reader None (nexus-i711w seam): index_repository's post-run
    # _set_owner_head_hash None-guards instead of resolving a live catalog.
    with patch("nexus.indexer._run_index") as m, \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=None):
        m.return_value = {}; index_repository(repo, registry, force=force_val) if force_val else index_repository(repo, registry)
    assert m.call_args.kwargs.get("force", False) is expected


@pytest.mark.parametrize("fname,content,target", [
    ("main.py", "print('hello')\n", "nexus.indexer._index_code_file"),
    ("README.md", "# Project\n\nLong.\n", "nexus.indexer._index_prose_file"),
    ("spec.pdf", b"%PDF-1.4 fake", "nexus.indexer._index_pdf_file"),
])
def test_run_index_passes_force_to_helpers(tmp_path, fname, content, target):
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    f = repo / fname
    f.write_bytes(content) if isinstance(content, bytes) else f.write_text(content)
    db, _, _ = _tracking_db()
    # target helper returns an int chunk-count per its contract (run_file_loop
    # tallies files_written off it — nexus-qgc4b); {} would yield a MagicMock.
    with _patches(db, extra={target: {"return_value": 1}, "nexus.doc_indexer.batch_index_markdowns": {"return_value": {}}}) as mocks:
        _run_index(repo, _reg(), force=True)
    h = mocks[target.split(".")[-1]]; h.assert_called(); assert h.call_args.kwargs.get("force") is True


def test_run_index_passes_force_to_rdr_loop(tmp_path):
    """nexus-3lswy: RDR files now flow through the 4th run_file_loop call
    (_index_prose_file targeting rdr__), not a separate _discover_and_index_rdrs
    call — force must still reach that call."""
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    rdr = repo / "docs" / "rdr"; rdr.mkdir(parents=True)
    (rdr / "rdr-001.md").write_text("# R\n")
    db, _, _ = _tracking_db()
    with _patches(db, extra={
        "nexus.indexer._index_prose_file": {"return_value": 1},
    }) as mocks:
        _run_index(repo, _reg(), force=True)
    rdr_calls = [
        c for c in mocks["_index_prose_file"].call_args_list
        if c.args[2].startswith("rdr__")
    ]
    assert len(rdr_calls) == 1
    assert rdr_calls[0].kwargs.get("force") is True


# ── Code-path transient-5xx containment (P1, T2 22168 engine-w0-503) ───────

def test_run_index_code_transient_5xx_deferred_not_aborted(tmp_path):
    """A transient upsert 5xx (gateway 502/503/504) on a CODE file — the
    direct-upload fallback taken whenever the ChunkBatcher is absent or
    rejects the file (onnx-local mode's 16-chunk cap makes this the common
    case for real source files) — must DEFER that file to staleness, exactly
    like the prose/PDF/RDR loops do via ``_contain_transient_upsert``. It
    must NOT propagate through ``run_file_loop``'s first-exception-cancels-
    all contract and abort the WHOLE ``nx index repo`` run.

    Pre-fix, ``_index_one_code`` calls ``_index_code_file`` unwrapped
    (indexer.py:4305) on the false premise that code files are always
    contained by the ChunkBatcher — false whenever ``ChunkBatcher.add``
    rejects the file and it falls through to the legacy per-file upsert
    (code_indexer.py:487-522). This is a v0.1.70-BLOCKING regression: a
    503-emitting engine under embed saturation aborts entire index runs on
    the first oversized code file.
    """
    from nexus.db.http_vector_client import VectorServiceError
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "boom.py").write_text("x = 1\n")
    (repo / "ok.py").write_text("y = 2\n")
    db, _ = _mock_db()

    def _code_side_effect(file, *_a, **_kw):
        if file.name == "boom.py":
            raise VectorServiceError("gateway said 503", code=503)
        return 1

    with _patches(db, extra={
        "nexus.indexer._index_code_file": {"side_effect": _code_side_effect},
    }):
        stats = _run_index(repo, _reg())

    # The run must complete without raising, with the transient failure
    # DEFERRED (not counted as written) — ok.py still lands.
    assert stats["files_changed_by_kind"]["code"] == 1


# ── _index_*_file return type ───────────────────────────────────────────────

def _run_code(tmp_path, chunks=None, col_meta=None):
    from nexus.indexer import _index_code_file
    repo = tmp_path / "repo"; repo.mkdir(); (repo / "main.py").write_text("x = 1\ny = 2\n")
    db, col = _mock_db()
    if col_meta: col.get.return_value = col_meta
    ch = chunks or [_chunk()]; v = _voyage(len(ch))
    with patch("nexus.chunker.chunk_file", return_value=ch):
        return _index_code_file(repo/"main.py", repo, "code__repo", "voyage-code-3",
                                col, db, v, git_meta={}, now_iso="2026-01-01T00:00:00", score=1.0)

def _run_prose(tmp_path, content="Line one\nLine two\n", col_meta=None):
    # nexus-sghyo (2026-08-06): no client-side embed function to mock —
    # the deleted _embed_with_fallback used to back the non-service path;
    # under the ambient service-mode default the indexer takes the
    # server-embed stub branch instead (embeddings computed server-side).
    from nexus.indexer import _index_prose_file
    repo = tmp_path / "repo"; repo.mkdir(); f = repo / "notes.txt"; f.write_text(content)
    db, col = _mock_db()
    if col_meta: col.get.return_value = col_meta
    return _index_prose_file(f, repo, "docs__repo", "voyage-context-3",
                             col, db, "fake-key", git_meta={}, now_iso="2026-01-01T00:00:00", score=1.0)

def _run_pdf(tmp_path, col_meta=None, n=1):
    # nexus-sghyo: see _run_prose above — no client-side embed mock needed.
    from nexus.indexer import _index_pdf_file
    repo = tmp_path / "repo"; repo.mkdir(); f = repo / "paper.pdf"; f.write_bytes(b"%PDF-1.4 fake content")
    db, col = _mock_db()
    if col_meta: col.get.return_value = col_meta
    prep = [(f"id{i}", f"Page {i}", {"source_title":"T","page_number":i,"source_path":str(f),
             "corpus":"docs__repo","embedding_model":"voyage-context-3","store_type":"prose",
             "source_agent":"nexus-indexer"}) for i in range(1, n+1)]
    # Reader None (nexus-i711w seam): _index_pdf_file is the ONE wrapper
    # that installs DEFAULT post-store hooks when hooks=None
    # (indexer.py:2179), and the catalog store hook resolves the factory at
    # fire time. None keeps it on its documented skip path, as the retired
    # local-catalog pin did incidentally.
    # nexus-sghyo: _index_pdf_file's embed_fn=None branch is unreachable
    # ONLY via the _run_index orchestrator (which always resolves and
    # passes embed_fn before calling per-file helpers) — called directly
    # here, it needs an explicit embed_fn (the local-mode-style injection
    # point), same as the deleted _embed_with_fallback mock used to
    # provide.
    with patch("nexus.doc_indexer._pdf_chunks", return_value=prep), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=None):
        return _index_pdf_file(f, repo, "docs__repo", "voyage-context-3",
                               col, db, "fake-key", git_meta={}, now_iso="2026-01-01T00:00:00", score=1.0,
                               embed_fn=lambda texts: [[0.1] * 3 for _ in texts])

def _assert_int(r): assert isinstance(r, int) and not isinstance(r, bool)

# nexus-test-cleanup P3a: the 3 file kinds (code/prose/pdf) each pinned the
# same 3 assertions (int-not-bool return type; zero when skipped by
# content-hash match; positive when actually indexed) as 9 near-identical
# defs. Substantive-critic review (P3 fix pass): the first collapse chained
# all 3 signals per kind into one sequential-assert body, coupling
# previously-independent failure ids. Re-split via a second parametrize axis
# ("signal") crossed with the kind-triple, so all 9 original signals keep
# independent node ids ([code-int_not_bool], [code-zero_when_skipped], ...)
# while the runner/kwargs boilerplate still collapses to one body.
@pytest.mark.parametrize(
    "runner,content_bytes,model,positive_kwargs,expected_positive",
    [
        pytest.param(
            _run_code, b"x = 1\ny = 2\n", "voyage-code-3",
            dict(chunks=[_chunk(idx=0, count=2), _chunk(text="y = 2", idx=1, count=2, ls=2, le=2)]),
            2, id="code",
        ),
        pytest.param(
            _run_pdf, b"%PDF-1.4 fake content", "voyage-context-3",
            dict(n=2), 2, id="pdf",
        ),
        pytest.param(
            _run_prose, b"Line one\nLine two\n", "voyage-context-3",
            dict(content="Line one\nLine two\nLine three\n"), None, id="prose",
        ),
    ],
)
@pytest.mark.parametrize(
    "signal", ["int_not_bool", "zero_when_skipped", "positive_when_indexed"],
)
def test_index_file_returns_int_reflecting_skip_and_index_state(
    tmp_path, signal, runner, content_bytes, model, positive_kwargs, expected_positive
):
    # Each runner mkdir()s <root>/"repo" itself (exist_ok=False); the subroot
    # name doubles as the tmp_path subtree so no two signal cases collide.
    root = tmp_path / signal
    root.mkdir()

    if signal == "int_not_bool":
        _assert_int(runner(root))
    elif signal == "zero_when_skipped":
        h = hashlib.sha256(content_bytes).hexdigest()
        r = runner(
            root,
            col_meta={"metadatas": [{"content_hash": h, "embedding_model": model}], "ids": []},
        )
        assert r == 0
        _assert_int(r)
    else:
        r = runner(root, **positive_kwargs)
        _assert_int(r)
        if expected_positive is None:
            assert r > 0
        else:
            assert r == expected_positive


# ── on_start / on_file callbacks ────────────────────────────────────────────

def _cb_repo(tmp_path, files=None):
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    for name, content in (files or [("code.py", "x = 1\n")]):
        (repo / name).write_text(content)
    return _run_index, repo

def test_on_file_chunks_zero_for_skipped_files(tmp_path):
    run, repo = _cb_repo(tmp_path)
    db, _ = _mock_db(); calls: list[tuple] = []
    with _cb_patches(db, code=0): run(repo, _reg(), on_file=lambda p,c,e: calls.append((p,c,e)))
    assert len(calls) == 1 and calls[0][1] == 0

def test_on_start_none_and_on_file_none_safe_defaults(tmp_path):
    run, repo = _cb_repo(tmp_path)
    db, _ = _mock_db()
    with _cb_patches(db): run(repo, _reg())

def test_rdr_files_now_trigger_on_file_via_4th_loop(tmp_path):
    """nexus-3lswy: RDR files now flow through run_file_loop as a 4th
    category (same as code/prose/pdf), so they trigger on_file for real
    per-file progress — this is an intentional behavior change from the old
    _discover_and_index_rdrs path, which gave no on_file callback at all."""
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    rdr = repo / "docs" / "rdr"; rdr.mkdir(parents=True)
    (repo / "code.py").write_text("x = 1\n")
    (rdr / "rdr-001.md").write_text("---\ntitle: t\nstatus: draft\ntype: feature\n---\n# T\n")
    cfg = {**_DEFAULT_CONFIG, "indexing": {**_DEFAULT_CONFIG["indexing"], "rdr_paths": ["docs/rdr"]}}
    db, _ = _mock_db(); calls: list[tuple] = []
    with _cb_patches(db, cfg=cfg, rdr_written=1):
        _run_index(repo, _reg(), on_file=lambda p,c,e: calls.append((p,c,e)))
    names = {p.name for p, _, _ in calls}
    assert names == {"code.py", "rdr-001.md"}


def test_rdr_symlink_excluded_from_indexing(tmp_path):
    """nexus-3lswy plan Phase 1 commitment: a symlinked .md under an RDR
    dir must be excluded from rdr_md_paths (and, as a side effect of the
    shared walk, from catalog registration too), matching the retired
    _discover_and_index_rdrs's symlink exclusion."""
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    rdr = repo / "docs" / "rdr"; rdr.mkdir(parents=True)
    real = tmp_path / "outside-rdr-001.md"
    real.write_text("# Real\n")
    (rdr / "rdr-001.md").write_text("# Real RDR\n")
    (rdr / "rdr-002-symlink.md").symlink_to(real)
    cfg = {**_DEFAULT_CONFIG, "indexing": {**_DEFAULT_CONFIG["indexing"], "rdr_paths": ["docs/rdr"]}}
    db, _ = _mock_db(); calls: list[tuple] = []
    with _cb_patches(db, cfg=cfg, rdr_written=1):
        _run_index(repo, _reg(), on_file=lambda p, c, e: calls.append((p, c, e)))
    names = {p.name for p, _, _ in calls}
    assert names == {"rdr-001.md"}


# ── on_phase post-processing callbacks (nexus-vatx Gap 2) ───────────────────


def test_on_phase_fires_on_post_processing_phases(tmp_path):
    """`_run_index` emits phase markers for every post-per-file-loop stage
    so the operator can tell hung from busy after "[N/N]" finishes."""
    run, repo = _cb_repo(tmp_path)
    db, _ = _mock_db()
    phases: list[str] = []
    with _cb_patches(db):
        run(repo, _reg(), on_phase=phases.append)

    # The key beats: RDR start/done, prune misclassified, prune deleted,
    # catalog registration, and the closing "complete" line.
    joined = "\n".join(phases)
    assert "Discovering and indexing RDR markdown files" in joined
    assert "RDR indexing done" in joined
    assert "Pruning misclassified chunks" in joined
    assert "Pruning misclassified done" in joined
    assert "Pruning deleted files" in joined
    assert "Pruning deleted files done" in joined
    assert "Registering" in joined and "catalog entries" in joined
    assert "Catalog registration done" in joined
    assert "Post-processing complete" in joined


def test_on_phase_reports_rdr_counts(tmp_path):
    """The RDR "done" line carries indexed/current counts derived from the
    4th run_file_loop's return, mirroring the summary the old
    _discover_and_index_rdrs path gave. nexus-3lswy: there is no longer a
    distinct "failed" count for RDR files — like code/prose/pdf, a failed
    upload is contained (batcher/_contain_transient_upsert) and surfaced via
    logs/failed_files, not a separate summary bucket; a failing file simply
    isn't counted as indexed (folds into "current")."""
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    rdr = repo / "docs" / "rdr"; rdr.mkdir(parents=True)
    for i in range(6):
        (rdr / f"rdr-{i:03d}.md").write_text(f"# R{i}\n")
    db, _ = _mock_db()
    phases: list[str] = []
    with _cb_patches(db, rdr_written=4):
        _run_index(repo, _reg(), on_phase=phases.append)
    done = next(p for p in phases if p.startswith("RDR indexing done"))
    assert "4 indexed" in done
    assert "2 current" in done
    assert "0 failed" in done


def test_on_phase_none_is_safe(tmp_path):
    """on_phase=None must not raise — matches on_start/on_file idiom."""
    run, repo = _cb_repo(tmp_path)
    db, _ = _mock_db()
    with _cb_patches(db):
        run(repo, _reg())  # on_phase omitted → None default


def test_on_flush_threads_through_to_chunk_batcher(tmp_path, monkeypatch):
    """nexus-rhwg5 / GH #1432 ask 3 residue: _run_index must thread its
    own ``on_flush`` argument straight into ``ChunkBatcher``'s
    constructor unchanged — a fake batcher stands in so this test proves
    ONLY the wiring, not ChunkBatcher's own firing behaviour (that is
    tests/test_chunk_batcher.py::TestOnFlushHook's job)."""
    from nexus.db.http_vector_client import HttpVectorClient

    captured: dict = {}

    class _FakeBatcher:
        def __init__(self, *, flush, on_flush=None, **_kw):
            captured["on_flush"] = on_flush

        def add(self, *_a, **_kw):
            return False  # legacy per-file fallback — nothing else under test here

        def drain(self, on_progress=None) -> int:
            return 0

        @property
        def pending_summary(self) -> dict:
            return {"chunks": 0, "collections": 0, "in_flight": 0}

        @property
        def failed_files(self) -> dict:
            return {}

        @property
        def stats(self) -> dict:
            return {"flushes": 0.0, "flush_seconds": 0.0, "upload_seconds": 0.0}

    run, repo = _cb_repo(tmp_path)
    db = MagicMock(spec=HttpVectorClient)
    monkeypatch.setattr("nexus.chunk_batcher.ChunkBatcher", _FakeBatcher)
    sentinel = lambda *_a: None  # noqa: E731 — identity marker, not real logic
    with _cb_patches(db):
        run(repo, _reg(), on_flush=sentinel)
    assert captured["on_flush"] is sentinel


def test_on_flush_defaults_to_none_when_omitted(tmp_path, monkeypatch):
    """Backward compat: every pre-nexus-rhwg5 caller omits on_flush."""
    from nexus.db.http_vector_client import HttpVectorClient

    captured: dict = {}

    class _FakeBatcher:
        def __init__(self, *, flush, on_flush=None, **_kw):
            captured["on_flush"] = on_flush

        def add(self, *_a, **_kw):
            return False

        def drain(self, on_progress=None) -> int:
            return 0

        @property
        def pending_summary(self) -> dict:
            return {"chunks": 0, "collections": 0, "in_flight": 0}

        @property
        def failed_files(self) -> dict:
            return {}

        @property
        def stats(self) -> dict:
            return {"flushes": 0.0, "flush_seconds": 0.0, "upload_seconds": 0.0}

    run, repo = _cb_repo(tmp_path)
    db = MagicMock(spec=HttpVectorClient)
    monkeypatch.setattr("nexus.chunk_batcher.ChunkBatcher", _FakeBatcher)
    with _cb_patches(db):
        run(repo, _reg())
    assert captured["on_flush"] is None


def test_on_phase_includes_stamp_phase_every_run(tmp_path):
    """Pipeline-version stamp phase fires on every successful run (nexus-7yfm).

    Earlier behaviour gated stamping on ``force=True``; that meant
    incremental runs that wrote v4 embeddings produced unstamped
    collections, which doctor then nagged about. The remediation
    "index with --force" forced a costly full re-embed to repair a
    state that should never have existed. Stamp now writes
    unconditionally on a successful run.
    """
    run, repo = _cb_repo(tmp_path)
    db, _ = _mock_db()

    # Without force → stamp phase present
    phases_no_force: list[str] = []
    with _cb_patches(db):
        run(repo, _reg(), on_phase=phases_no_force.append)
    assert any("Stamping pipeline version" in p for p in phases_no_force)
    assert any("Pipeline version stamped" in p for p in phases_no_force)

    # With force → stamp phase still present (regression guard)
    phases_force: list[str] = []
    with _cb_patches(db):
        run(repo, _reg(), force=True, on_phase=phases_force.append)
    assert any("Stamping pipeline version" in p for p in phases_force)
    assert any("Pipeline version stamped" in p for p in phases_force)


# ── on_stage_timers callback (nexus-7niu) ──────────────────────────────────


def test_on_stage_timers_fires_per_code_file_when_subscribed(tmp_path):
    """``_run_index`` builds a fresh ``StageTimers`` per code file when
    ``on_stage_timers`` is provided and hands it to the callback. Silent
    (zero invocations) when the callback is ``None``."""
    run, repo = _cb_repo(tmp_path)
    db, _ = _mock_db()

    collected: list[tuple] = []

    def _cb(file, timers) -> None:
        collected.append((file.name, timers))

    with _cb_patches(db):
        run(repo, _reg(), on_stage_timers=_cb)

    # Exactly one callback per code file (one file in the fixture repo).
    assert len(collected) == 1
    name, timers = collected[0]
    assert name == "code.py"
    # StageTimers instance with the expected shape
    snapshot = timers.snapshot()
    assert set(snapshot.keys()) == {
        "chunking_s", "embed_s", "upload_s", "hooks_s", "retry_s",
    }


def test_on_stage_timers_fires_per_prose_file_when_subscribed(tmp_path):
    """Same contract for the prose-file loop (nexus-7niu extension).
    Verifies the instrumentation in ``prose_indexer.index_prose_file``
    runs via the ``_index_prose_file`` wrapper and yields a callback."""
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Title\n\nBody prose.\n")
    (repo / "code.py").write_text("x = 1\n")
    collected: list[tuple] = []

    def _cb(file, timers) -> None:
        collected.append((file.name, timers.snapshot()))

    db, _ = _mock_db()
    with _cb_patches(db):
        _run_index(repo, _reg(), on_stage_timers=_cb)

    # Both the code file AND the prose/markdown file should fire.
    names = {n for n, _ in collected}
    assert "code.py" in names
    assert "README.md" in names


def test_on_stage_timers_fires_per_pdf_file_when_subscribed(tmp_path):
    """Same contract for the PDF-file loop. Verifies
    ``_index_pdf_file``'s instrumentation wires through when
    ``on_stage_timers`` is provided."""
    import pymupdf as _fitz
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    # Minimal PDF so the classifier routes it through the PDF loop
    pdf_doc = _fitz.open()
    page = pdf_doc.new_page()
    page.insert_text((72, 100), "Hello. PDF shakeout.", fontsize=12)
    (repo / "doc.pdf").write_bytes(pdf_doc.tobytes())
    pdf_doc.close()

    collected: list[tuple] = []

    def _cb(file, timers) -> None:
        collected.append((file.name, timers.snapshot()))

    db, _ = _mock_db()
    # Stub the actual _index_pdf_file body via the same _cb_patches shape
    # for code/prose; let _index_pdf_file run through to hit the stage
    # callback wiring we're trying to prove. Mock the expensive
    # extractor + embedder.
    extra = {
        "nexus.indexer._index_pdf_file": {"return_value": 2},
        "nexus.indexer._index_code_file": {"return_value": 0},
        "nexus.indexer._index_prose_file": {"return_value": 0},
        "nexus.indexer._prune_misclassified": {},
        "nexus.indexer._prune_deleted_files": {},
    }
    with _patches(db, extra=extra):
        _run_index(repo, _reg(), on_stage_timers=_cb)

    # _index_pdf_file is mocked so it doesn't actually populate timers —
    # what we're verifying here is the wiring: the orchestrator created
    # and passed a StageTimers, then called the callback afterwards.
    assert any(n == "doc.pdf" for n, _ in collected), (
        f"expected per-PDF callback; got {collected}"
    )


def test_on_stage_timers_none_is_safe(tmp_path):
    """Omitting ``on_stage_timers`` (the default) must not spawn any
    per-file timers or change behaviour — zero-overhead contract."""
    run, repo = _cb_repo(tmp_path)
    db, _ = _mock_db()
    with _cb_patches(db):
        run(repo, _reg())  # no on_stage_timers argument


# ── Pagination tests ────────────────────────────────────────────────────────

# test_prune_deleted_files_paginates DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
# test_prune_deleted_files_uses_get_all_metadata_fast_path_not_pagination DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
# test_prune_deleted_files_fast_path_row_cap_falls_back_without_data_loss DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
# test_prune_deleted_files_fast_path_and_fallback_both_fail_skips_collection DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
# test_prune_deleted_files_fast_path_emits_liveness_ping DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
def test_paginated_get_invokes_on_page_per_page():
    """The 427.5s black hole was this exact loop with no callback. Each
    page must fire on_page(page_num, cumulative_scanned)."""
    from nexus.indexer import _paginated_get
    live = [(f"live-{i:03d}", f"a{i:03d}" + "0" * 60) for i in range(310)]
    col = _gc_col(live)
    pages: list[tuple[int, int]] = []

    _paginated_get(col, include=["metadatas"], on_page=lambda p, s: pages.append((p, s)))

    assert pages == [(1, 300), (2, 310)]


def test_paginated_get_on_page_defaults_to_none_safely():
    """Backward compat: ~20 existing call sites never pass on_page."""
    from nexus.indexer import _paginated_get
    col = _gc_col([("a", "x" * 64)])
    result = _paginated_get(col, include=["metadatas"])
    assert result["ids"] == ["a"]


# test_prune_deleted_files_emits_page_progress_per_collection DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
# test_prune_deleted_files_page_progress_degrades_without_count DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
# test_prune_deleted_files_page_progress_degrades_on_negative_count DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
# test_prune_deleted_files_page_progress_includes_denominator_when_count_available DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
# test_prune_deleted_files_page_progress_no_bracket_nm_form DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.
def test_frecency_update_paginates(tmp_path):
    """nexus-afudo (2026-08-05): pinned to the doc_id-keyed path (a
    real ``_build_frecency_doc_id_map`` patch) so this pagination
    behavior is exercised through code that can still fire in
    production — the legacy source_path where-filter this test used
    to route through is deleted dead code (see
    ``test_frecency_only_skips_unmapped_files_source_path_fallback_
    deleted_as_dead_code``).
    """
    from nexus.indexer import _run_index_frecency_only
    repo = tmp_path / "repo"; repo.mkdir(); src = repo / "big.py"; src.write_text("# g\n")
    p1 = {"ids": [f"c-{i}" for i in range(300)], "metadatas": [{"frecency_score":0.0}]*300}
    p2 = {"ids": [f"c-{i}" for i in range(300,310)], "metadatas": [{"frecency_score":0.0}]*10}
    cc = MagicMock(); cc.get.side_effect = [p1, p2]
    dc = MagicMock(); dc.get.return_value = {"ids":[],"metadatas":[]}
    db = MagicMock(); db.get_or_create_collection.side_effect = {"code__repo":cc,"docs__repo":dc}.get
    db.get_collection.side_effect = {"code__repo":cc,"docs__repo":dc}.get
    with patch(
        "nexus.indexer._build_frecency_doc_id_map",
        return_value={src: "1.1.1"},
    ), \
         patch("nexus.frecency.batch_frecency", return_value={src: 0.9}), \
         patch("nexus.config.get_credential", return_value="fake-key"), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
         patch("nexus.db.make_t3", return_value=db), \
         patch("nexus.db.http_vector_client.get_http_vector_client", return_value=db):
        # nexus-sghyo (2026-08-06): _run_index_frecency_only routes
        # service mode through get_http_vector_client() directly
        # (bypassing make_t3()) — patched above too so this test keeps
        # exercising its own db mock under the ambient service default.
        _run_index_frecency_only(repo, _reg())
    ids = set()
    for c in db.update_chunks.call_args_list: ids.update(c.kwargs.get("ids") or c.args[0])
    assert len(ids) == 310
    where = cc.get.call_args_list[0].kwargs["where"]
    assert where == {"doc_id": "1.1.1"}


def test_prune_misclassified_paginates(tmp_path):
    """nexus-afudo (2026-08-05): pinned to the doc_id-keyed ``$in``
    path (via ``file_to_doc_id``) so this pagination behavior is
    exercised through code that can still fire in production — the
    legacy source_path where-filter this test used to route through
    (via an empty/default ``file_to_doc_id``) is deleted dead code
    (see ``test_prune_misclassified_source_path_fallback_deleted_as_
    dead_code``).
    """
    from nexus.indexer import _prune_misclassified
    repo = tmp_path / "repo"; repo.mkdir(); bp = repo / "g.md"; bp.write_text("# b\n")
    cc = MagicMock(); cc.get.side_effect = [{"ids":[f"s-{i}" for i in range(300)]}, {"ids":[f"s-{i}" for i in range(300,310)]}]
    dc = MagicMock(); dc.get.return_value = {"ids":[]}
    db = MagicMock(); db.get_or_create_collection.side_effect = {"code__repo":cc,"docs__repo":dc}.get
    db.get_collection.side_effect = {"code__repo":cc,"docs__repo":dc}.get
    _prune_misclassified(
        repo, "code__repo", "docs__repo", [], [bp], [], db,
        file_to_doc_id={bp: "doc-1"},
    )
    d = set()
    for c in cc.delete.call_args_list: d.update(c.kwargs.get("ids") or (c.args[0] if c.args else []))
    assert d == {f"s-{i}" for i in range(310)}
    where = cc.get.call_args_list[0].kwargs["where"]
    assert where == {"doc_id": {"$in": ["doc-1"]}}


def test_prune_misclassified_uses_doc_id_when_supplied(tmp_path):
    """nexus-dcym: chunk lookup for the prune keys on doc_id when the
    catalog hook's ``file_to_doc_id`` map is supplied. WITH TEETH:
    a stash-revert to the source_path-keyed form makes the assertion
    on the ``where`` filter fail.

    Updated for the batched ``$in`` form (~300x roundtrip reduction
    on large repos): the where clause is now
    ``{"doc_id": {"$in": [<id>, ...]}}``. The intent — that doc_id is
    the lookup column, not source_path — is preserved.
    """
    from nexus.indexer import _prune_misclassified
    repo = tmp_path / "repo"
    repo.mkdir()
    bp = repo / "ambiguous.md"
    bp.write_text("# a\n")
    cc = MagicMock()
    cc.get.return_value = {"ids": ["chunk-abc"]}
    dc = MagicMock()
    dc.get.return_value = {"ids": []}
    db = MagicMock()
    db.get_or_create_collection.side_effect = {"code__repo": cc, "docs__repo": dc}.get
    db.get_collection.side_effect = {"code__repo": cc, "docs__repo": dc}.get
    db.get_collection.side_effect = {"code__repo": cc, "docs__repo": dc}.get
    _prune_misclassified(
        repo, "code__repo", "docs__repo",
        [], [bp], [], db,
        file_to_doc_id={bp: "ART-deadbeef"},
    )
    where = cc.get.call_args.kwargs["where"]
    assert where == {"doc_id": {"$in": ["ART-deadbeef"]}}
    cc.delete.assert_called_once_with(ids=["chunk-abc"])


def test_prune_misclassified_source_path_fallback_deleted_as_dead_code(tmp_path):
    """nexus-afudo (2026-08-05): the legacy source_path where-filter
    this test used to exercise (files missing from ``file_to_doc_id``)
    is DELETED dead code. RDR-102 D2 (2026-05-02) removed source_path
    from make_chunk_metadata for every writer, so
    ``where={"source_path": ...}`` always matched zero rows in
    production regardless of how many files landed in this "legacy"
    bucket; a live-store probe (field>=! existence test) found zero
    source_path rows across 13 representative collections (~115k
    chunks), including the code__/docs__ collections this exact prune
    targets. An unmapped file (no catalog doc_id) is now silently
    skipped by the prune — no query, since the fallback it used to
    fall through to could never find anything.

    Kill control: if the deleted branch were reintroduced, ``cc.get``
    (mocked with NO return_value here, unlike the old test) would be
    called and ``cc.delete`` would fire off whatever the default
    MagicMock response produces — both assertions below fail either
    way.
    """
    from nexus.indexer import _prune_misclassified
    repo = tmp_path / "repo"
    repo.mkdir()
    bp = repo / "legacy.md"
    bp.write_text("# l\n")
    cc = MagicMock()
    dc = MagicMock()
    db = MagicMock()
    db.get_or_create_collection.side_effect = {"code__repo": cc, "docs__repo": dc}.get
    db.get_collection.side_effect = {"code__repo": cc, "docs__repo": dc}.get
    _prune_misclassified(
        repo, "code__repo", "docs__repo",
        [], [bp], [], db,
        file_to_doc_id={},
    )
    cc.get.assert_not_called()
    cc.delete.assert_not_called()


# ── Lock file cleanup ───────────────────────────────────────────────────────

@pytest.mark.parametrize("side_effect", [None, RuntimeError("boom"), CredentialsMissingError("x")])
def test_lock_file_deleted_after_index(tmp_path, registry, side_effect):
    repo = tmp_path / "repo"; repo.mkdir(); ld = tmp_path / "locks"; ld.mkdir()
    # Reader None (nexus-i711w seam): keeps the post-run head-hash write a
    # None-guard no-op, as it was under the retired local-catalog pin.
    with patch("nexus.indexer._repo_lock_path", side_effect=lambda r: ld / "test.lock"), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
         patch("nexus.indexer._run_index", side_effect=side_effect):
        if side_effect is None: index_repository(repo, registry)
        elif isinstance(side_effect, CredentialsMissingError):
            with pytest.raises(CredentialsMissingError): index_repository(repo, registry)
        else:
            with pytest.raises(RuntimeError): index_repository(repo, registry)
    assert not (ld / "test.lock").exists()

def test_stale_lock_removed_before_acquire(tmp_path, registry):
    repo = tmp_path / "repo"; repo.mkdir(); ld = tmp_path / "locks"; ld.mkdir()
    lf = ld / "test.lock"; lf.write_text(str(999999999))
    with patch("nexus.indexer._repo_lock_path", side_effect=lambda r: lf), \
         patch("nexus.catalog.factory.make_catalog_reader", return_value=None), \
         patch("nexus.indexer._run_index"):
        index_repository(repo, registry)
    assert not lf.exists()

def test_stale_lock_detection_live_pid_not_removed(tmp_path, registry):
    import fcntl as _fcntl; import os
    repo = tmp_path / "repo"; repo.mkdir(); ld = tmp_path / "locks"; ld.mkdir()
    lf = ld / "test.lock"; lf.write_text(str(os.getpid()))
    fd = open(lf, "r+"); _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)  # noqa: SIM115
    try:
        with patch("nexus.indexer._repo_lock_path", side_effect=lambda r: lf):
            assert index_repository(repo, registry, on_locked="skip") == {}
    finally:
        _fcntl.flock(fd, _fcntl.LOCK_UN); fd.close()
        try: lf.unlink()
        except FileNotFoundError: pass


# ── chunk_text_hash metadata ────────────────────────────────────────────────

def _cap_code(tmp_path, chunks):
    from nexus.indexer import _index_code_file
    repo = tmp_path / "repo"; repo.mkdir(); (repo / "main.py").write_text("x = 1\ny = 2\n")
    cap: list[dict] = []; db, col = _mock_db()
    db.upsert_chunks_with_embeddings.side_effect = lambda *a, **kw: cap.extend(kw.get("metadatas", a[4] if len(a)>4 else []))
    with patch("nexus.chunker.chunk_file", return_value=chunks):
        _index_code_file(repo/"main.py", repo, "code__repo", "voyage-code-3",
                         col, db, _voyage(len(chunks)), git_meta={}, now_iso="2026-01-01T00:00:00", score=1.0)
    return cap

def test_code_indexer_chunk_text_hash(tmp_path):
    ch = [_chunk(idx=0, count=2), _chunk(text="y = 2", idx=1, count=2, ls=2, le=2)]
    m = _cap_code(tmp_path, ch)
    assert len(m) == 2
    assert m[0]["chunk_text_hash"] == hashlib.sha256(b"x = 1").hexdigest()
    assert m[1]["chunk_text_hash"] == hashlib.sha256(b"y = 2").hexdigest()
    assert m[0]["chunk_text_hash"] != m[0]["content_hash"]  # chunk vs file
    assert m[0]["chunk_text_hash"] != m[1]["chunk_text_hash"]  # distinct chunks

def _cap_prose(tmp_path, content, ext):
    # nexus-sghyo (2026-08-06): no client-side embed function to mock —
    # the deleted _embed_with_fallback used to back the non-service path;
    # under the ambient service-mode default the indexer takes the
    # server-embed stub branch instead (embeddings computed server-side).
    from nexus.indexer import _index_prose_file
    repo = tmp_path / "repo"; repo.mkdir(); f = repo / f"notes{ext}"; f.write_text(content)
    docs: list[str] = []; metas: list[dict] = []; db, col = _mock_db()
    db.upsert_chunks_with_embeddings.side_effect = lambda *a, **kw: (docs.extend(kw.get("documents",a[2] if len(a)>2 else [])), metas.extend(kw.get("metadatas",a[4] if len(a)>4 else [])))
    _index_prose_file(f, repo, "docs__repo", "voyage-context-3",
                      col, db, "fake-key", git_meta={}, now_iso="2026-01-01T00:00:00", score=1.0)
    return metas, docs

def test_prose_indexer_markdown_metadata(tmp_path):
    m, _ = _cap_prose(tmp_path, "# Abstract\n\nContent.\n\n# Methods\n\nMore.\n", ".md")
    assert m
    for x in m: assert "chunk_text_hash" in x and "section_type" in x

def test_prose_indexer_non_markdown_metadata(tmp_path):
    m, docs = _cap_prose(tmp_path, "Line one\nLine two\n", ".txt")
    assert m
    for doc, x in zip(docs, m):
        h = x["chunk_text_hash"]; assert len(h) == 64
        assert h == hashlib.sha256(doc.encode()).hexdigest()
        assert x.get("section_type") == ""


# ── nexus-27u7: lazy collection creation ─────────────────────────────────


def test_run_index_skips_docs_collection_for_code_only_repo(tmp_path):
    """nexus-27u7: a code-only repo MUST NOT create the docs__
    collection at the start of ``_run_index``. Pre-fix the indexer
    pre-created BOTH ``code__`` and ``docs__`` regardless of file
    composition; a code-only repo accumulated an empty zombie
    ``docs__`` that ``nx catalog collection-gc`` had to sweep later.

    Reverting the lazy-creation gate (re-introducing
    ``code_col = db.get_or_create_collection(code_collection);
    docs_col = db.get_or_create_collection(docs_collection)``)
    makes this test fail because the docs name appears in the
    get_or_create_collection call list.
    """
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "main.py").write_text("x = 1\n")  # code only
    db, col = _mock_db()
    v = _voyage(1)
    with _patches(db, extra={
        "nexus.chunker.chunk_file": {"return_value": [_chunk()]},
        "voyageai.Client": {"return_value": v},
    }):
        _run_index(repo, _reg())

    # Inspect every collection name passed to get_or_create_collection.
    created = [
        call.args[0] if call.args else call.kwargs.get("name", "")
        for call in db.get_or_create_collection.call_args_list
    ]
    docs_created = [n for n in created if n.startswith("docs__")]
    assert not docs_created, (
        f"docs collection should NOT have been created for code-only "
        f"repo (nexus-27u7); got created: {docs_created!r}"
    )


def test_run_index_skips_code_collection_for_docs_only_repo(tmp_path):
    """nexus-27u7 symmetric case: a docs-only repo (.md files only)
    MUST NOT create the code__ collection.
    """
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "README.md").write_text("# Title\n\nSome prose.\n")  # docs only
    db, col = _mock_db()
    # nexus-sghyo: _embed_with_fallback deleted; service-mode stub handles
    # embedding now.
    with _patches(db, extra={}):
        _run_index(repo, _reg())

    created = [
        call.args[0] if call.args else call.kwargs.get("name", "")
        for call in db.get_or_create_collection.call_args_list
    ]
    code_created = [n for n in created if n.startswith("code__")]
    assert not code_created, (
        f"code collection should NOT have been created for docs-only "
        f"repo (nexus-27u7); got created: {code_created!r}"
    )


def test_run_index_creates_both_for_mixed_repo(tmp_path):
    """nexus-27u7 regression guard: a mixed repo (code + docs)
    creates BOTH collections. Lazy-creation must not regress the
    happy path.
    """
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "main.py").write_text("x = 1\n")
    (repo / "README.md").write_text("# Title\n\nProse.\n")
    db, col = _mock_db()
    v = _voyage(1)
    # nexus-sghyo: _embed_with_fallback deleted; service-mode stub handles
    # prose/docs embedding now. voyageai.Client patch stays harmless/inert.
    with _patches(db, extra={
        "nexus.chunker.chunk_file": {"return_value": [_chunk()]},
        "voyageai.Client": {"return_value": v},
    }):
        _run_index(repo, _reg())

    created = [
        call.args[0] if call.args else call.kwargs.get("name", "")
        for call in db.get_or_create_collection.call_args_list
    ]
    code_created = [n for n in created if n.startswith("code__")]
    docs_created = [n for n in created if n.startswith("docs__")]
    assert code_created, (
        "mixed repo must create code__ collection; "
        "lazy-creation gate is over-eager"
    )
    assert docs_created, (
        "mixed repo must create docs__ collection; "
        "lazy-creation gate is over-eager"
    )


# nexus-sghyo (2026-08-06): the ``_legacy_vector_backend`` autouse
# fixture that force-pinned this whole module to
# NX_STORAGE_BACKEND_VECTORS=chroma (the legacy chroma/local embed
# pipeline opt-out) is RETIRED — that pipeline is deleted outright: the
# client no longer embeds via Voyage (Hal determination 2026-07-28:
# "we do no embedding on the client"). The module runs under the
# ambient service-mode default like production.


# ── drain phase markers (nexus-uizok) ────────────────────────────────────────


class _StubBatcher:
    def __init__(self, pend, flushes=2):
        self._pend = pend
        self._flushes = flushes

    @property
    def pending_summary(self):
        return self._pend

    def drain(self, on_progress=None):
        for i in range(self._flushes):
            if on_progress is not None:
                on_progress(i + 1, self._flushes)
        return self._flushes


def test_drain_markers_busy_emits_open_heartbeat_close():
    from nexus.indexer import _drain_batcher_with_markers
    phases: list[str] = []
    b = _StubBatcher({"chunks": 612, "collections": 3, "in_flight": 2}, flushes=2)
    flushed = _drain_batcher_with_markers(b, phases.append)
    assert flushed == 2
    assert phases[0] == "Flushing 612 staged chunks across 3 collections + 2 in-flight batches…"
    assert phases[1].startswith("  flush 1/2 complete")
    assert phases[2].startswith("  flush 2/2 complete")
    assert phases[3].startswith("Flush drain complete — 2 flushes,")


def test_drain_markers_heartbeats_carry_rate_and_eta(monkeypatch):
    """nexus-zedf7: each heartbeat states the rolling rate and projected
    remaining time — an hour-long legitimate embed drain must never read
    as a hang. Injected clock: 30s per flush."""
    from nexus import indexer as idx

    t = {"now": 1000.0}
    monkeypatch.setattr(idx.time, "monotonic", lambda: t["now"])

    class _TickingBatcher(_StubBatcher):
        def drain(self, on_progress=None):
            for i in range(self._flushes):
                t["now"] += 30.0
                if on_progress is not None:
                    on_progress(i + 1, self._flushes)
            return self._flushes

    phases: list[str] = []
    b = _TickingBatcher({"chunks": 900, "collections": 2, "in_flight": 1}, flushes=4)
    idx._drain_batcher_with_markers(b, phases.append)

    # flush 1/4 at t=30s: rate 2.0 flushes/min, 3 left -> ~90s -> ~1m30s.
    assert "flush 1/4 complete (30.0s" in phases[1]
    assert "~1m30s remaining" in phases[1]
    # flush 3/4 at t=90s: 1 left at 30s/flush -> ~30s remaining.
    assert "~30s remaining" in phases[3]
    # final flush: no remaining estimate on a finished drain.
    assert "remaining" not in phases[4]
    assert phases[5].startswith("Flush drain complete — 4 flushes,")


def test_drain_markers_quiet_drain_is_silent():
    # Nothing pending, nothing in flight → no phantom markers.
    from nexus.indexer import _drain_batcher_with_markers
    phases: list[str] = []
    b = _StubBatcher({"chunks": 0, "collections": 0, "in_flight": 0}, flushes=0)
    _drain_batcher_with_markers(b, phases.append)
    assert phases == []


def test_drain_markers_on_phase_none_safe():
    from nexus.indexer import _drain_batcher_with_markers
    b = _StubBatcher({"chunks": 5, "collections": 1, "in_flight": 0}, flushes=1)
    assert _drain_batcher_with_markers(b, None) == 1


def test_drain_markers_prints_per_flush_not_cumulative_g2(monkeypatch):
    """nexus-lde88 G2: pre-fix, the printed "(Xs" was
    time.monotonic() - t0 — CUMULATIVE since the drain started — so three
    consecutive flushes of different individual durations still each
    printed the SAME growing number, never their own cost. Uneven
    per-flush costs (10s, then 50s) must print 10.0s then 50.0s, not
    10.0s then 60.0s."""
    from nexus import indexer as idx

    t = {"now": 1000.0}
    monkeypatch.setattr(idx.time, "monotonic", lambda: t["now"])

    class _UnevenBatcher(_StubBatcher):
        def drain(self, on_progress=None):
            for i, delta in enumerate((10.0, 50.0), start=1):
                t["now"] += delta
                if on_progress is not None:
                    on_progress(i, self._flushes)
            return self._flushes

    phases: list[str] = []
    b = _UnevenBatcher({"chunks": 2, "collections": 1, "in_flight": 0}, flushes=2)
    idx._drain_batcher_with_markers(b, phases.append)

    assert "flush 1/2 complete (10.0s" in phases[1], phases[1]
    assert "flush 2/2 complete (50.0s" in phases[2], phases[2]
    # The pre-fix bug would have printed cumulative 10.0s then 60.0s here.
    assert "60.0s" not in phases[2]


# TestQuarantineLifecycle DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested chunk_quarantine.py's client-side quarantine_orphans/restore_rereferenced/
# expire_quarantine (all deleted): the manifest-chunk FK makes the completeness
# apparatus this lifecycle existed to prove correct unreachable by construction.
# test_quarantine_siblings_distinct_for_shared_owner_and_chash DELETED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) —
# tested the client-side fetch-diff-copy-delete prune/quarantine fallback,
# retired: the manifest-chunk FK makes the completeness apparatus it proved
# correct unreachable by construction.