# SPDX-License-Identifier: AGPL-3.0-or-later
"""T1: indexer.py — status transitions, error path, credential skip, hidden file filter."""
import hashlib
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from voyageai.object.embeddings import EmbeddingsObject

from nexus.indexer import CredentialsMissingError, index_repository

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
    def cap(collection_name, ids, documents, embeddings, metadatas):
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
    }
    if extra: patches.update(extra)
    mocks, stack = {}, []
    for t, kw in patches.items():
        p = patch(t, **kw); m = p.start(); stack.append(p); mocks[t.split(".")[-1]] = m
    try: yield mocks
    finally:
        for p in reversed(stack): p.stop()


@contextmanager
def _cb_patches(db, *, cfg=None, code=1, prose=1, rdr=(0, 0, 0)):
    extra = {
        "nexus.indexer._index_code_file": {"return_value": code},
        "nexus.indexer._index_prose_file": {"return_value": prose},
        "nexus.indexer._discover_and_index_rdrs": {"return_value": rdr},
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
    with patch("nexus.frecency.batch_frecency", return_value={}), \
         patch("nexus.ripgrep_cache.build_cache"), \
         patch("nexus.config.load_config", return_value=_DEFAULT_CONFIG), \
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
    with patch("nexus.frecency.batch_frecency", return_value={}), \
         patch("nexus.ripgrep_cache.build_cache", side_effect=lambda r, cp, s: seen.append(cp)), \
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
    seen: list[Path] = []
    with patch("nexus.frecency.batch_frecency", return_value={}), \
         patch("nexus.ripgrep_cache.build_cache", side_effect=lambda r, cp, s: seen.extend(f for _, f in s)), \
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
    from nexus.indexer import _run_index_frecency_only
    repo = tmp_path / "repo"; repo.mkdir()
    src = repo / "main.py"; src.write_text("x = 1\n")
    old = {"frecency_score": 0.1, "source_path": str(src), "title": "main.py:1-1"}
    col = MagicMock(); col.get.return_value = {"ids": ["c1"], "metadatas": [old]}
    db = MagicMock(); db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    with patch("nexus.frecency.batch_frecency", return_value={src: 0.75}), \
         patch("nexus.config.get_credential", return_value="fake-key"), \
         patch("nexus.db.make_t3", return_value=db):
        _run_index_frecency_only(repo, _reg())
    kw = db.update_chunks.call_args_list[0].kwargs
    assert kw["ids"] == ["c1"]
    assert kw["metadatas"][0]["frecency_score"] == 0.75
    assert kw["metadatas"][0]["title"] == "main.py:1-1"


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

    # Mock the catalog map so the file resolves to a known doc_id.
    with patch(
        "nexus.indexer._build_frecency_doc_id_map",
        return_value={src: "1.1.1"},
    ), \
         patch("nexus.frecency.batch_frecency", return_value={src: 0.75}), \
         patch("nexus.config.get_credential", return_value="fake-key"), \
         patch("nexus.db.make_t3", return_value=db):
        _run_index_frecency_only(repo, _reg())
    where = col.get.call_args.kwargs["where"]
    assert where == {"doc_id": "1.1.1"}, (
        f"expected doc_id-keyed lookup, got {where!r}"
    )


def test_frecency_only_falls_back_to_source_path_when_no_catalog_entry(tmp_path):
    """Files missing from the catalog map use the legacy source_path
    filter so chunks predating the catalog backfill keep getting
    frecency updates.
    """
    from nexus.indexer import _run_index_frecency_only
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "legacy.py"
    src.write_text("z = 3\n")
    old = {"frecency_score": 0.1, "source_path": str(src), "title": "legacy.py:1-1"}
    col = MagicMock()
    col.get.return_value = {"ids": ["c1"], "metadatas": [old]}
    db = MagicMock()
    db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    db.get_collection.return_value = col
    with patch(
        "nexus.indexer._build_frecency_doc_id_map",
        return_value={},
    ), \
         patch("nexus.frecency.batch_frecency", return_value={src: 0.42}), \
         patch("nexus.config.get_credential", return_value="fake-key"), \
         patch("nexus.db.make_t3", return_value=db):
        _run_index_frecency_only(repo, _reg())
    where = col.get.call_args.kwargs["where"]
    assert where == {"source_path": str(src)}


def test_frecency_only_skips_unindexed_files(tmp_path):
    from nexus.indexer import _run_index_frecency_only
    repo = tmp_path / "repo"; repo.mkdir()
    src = repo / "new.py"; src.write_text("y = 2\n")
    col = MagicMock(); col.get.return_value = {"ids": [], "metadatas": []}
    db = MagicMock(); db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    with patch("nexus.frecency.batch_frecency", return_value={src: 0.5}), \
         patch("nexus.config.get_credential", return_value="fake-key"), \
         patch("nexus.db.make_t3", return_value=db):
        _run_index_frecency_only(repo, _reg())
    db.update_chunks.assert_not_called()


def test_frecency_only_raises_credentials_missing(tmp_path, monkeypatch):
    from nexus.indexer import _run_index_frecency_only
    repo = tmp_path / "repo"; repo.mkdir()
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
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
    assert any("skipped non-text file" in str(c) for c in l1.debug.call_args_list + l2.debug.call_args_list)


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
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "README.md").write_text("# README\n\nProject description here.\n")
    rdr = repo / "docs" / "rdr"; rdr.mkdir(parents=True)
    (rdr / "ADR-001.md").write_text("# ADR-001\n\nArchitecture decision.\n")
    db, ups, _ = _tracking_db()
    with _patches(db, extra={"nexus.doc_indexer._embed_with_fallback": {"return_value": ([[0.1]*10], "voyage-context-3")},
                              "nexus.doc_indexer.batch_index_markdowns": {}}) as mocks:
        _run_index(repo, _reg())
    if "docs__repo" in ups:
        # RDR-102 D2: source_path is gone; title carries
        # "{relpath}:chunk-{i}" per prose_indexer.py:96.
        paths = [m.get("title", "") for m in ups["docs__repo"]]
        assert any("README.md" in p for p in paths) and not any("ADR-001" in p for p in paths)
    mb = mocks["batch_index_markdowns"]; mb.assert_called_once()
    assert any("ADR-001.md" in str(p) for p in mb.call_args[0][0])


def test_run_index_returns_rdr_stats(tmp_path):
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "README.md").write_text("# README\n")
    rdr = repo / "docs" / "rdr"; rdr.mkdir(parents=True)
    (rdr / "001.md").write_text("# D\n"); (rdr / "002.md").write_text("# D2\n")
    db, _, _ = _tracking_db()
    results = {str(rdr / "001.md"): "indexed", str(rdr / "002.md"): "skipped"}
    with _patches(db, extra={
        "nexus.doc_indexer._embed_with_fallback": {"side_effect": lambda c, m, k, **kw: ([[0.1]*4]*len(c), m)},
        "nexus.doc_indexer.batch_index_markdowns": {"return_value": results},
    }):
        stats = _run_index(repo, _reg())
    assert (stats["rdr_indexed"], stats["rdr_current"], stats["rdr_failed"]) == (1, 1, 0)


@pytest.mark.parametrize("rdr_indexed,expect", [(1, True), (0, False)])
def test_index_repo_cmd_rdr_summary(tmp_path, rdr_indexed, expect):
    from click.testing import CliRunner; from nexus.cli import main
    repo = tmp_path / "repo"; repo.mkdir()
    stats = {"rdr_indexed": rdr_indexed, "rdr_current": 0, "rdr_failed": 0}
    runner = CliRunner()
    with patch("nexus.commands.index._registry_path", return_value=tmp_path / "r.json"), \
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
    with _patches(db, extra={
        "nexus.chunker.chunk_file": {"return_value": [_chunk(text="print('hello')")]},
        "voyageai.Client": {"return_value": _voyage(1)},
        "nexus.doc_indexer._embed_with_fallback": {"return_value": ([[0.1]*10], "voyage-context-3")},
    }):
        _run_index(repo, _reg())
    # RDR-102 D2: source_path is gone; title carries
    # "{relpath}:chunk-{i}" per code_indexer.py:393 / prose_indexer.py:96.
    assert any("main.py" in m.get("title", "") for m in ups["code__repo"])
    dp = {m.get("title", "") for m in ups["docs__repo"]}
    assert any("README.md" in p for p in dp) and any("notes.rst" in p for p in dp)
    assert not any("data.txt" in p for p in dp), ".txt files should be SKIP"


# ── Prune helpers ────────────────────────────────────────────────────────────

def _gc_col(rows: list[tuple[str, str]]):
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
    """
    ids = [r[0] for r in rows]
    metas = [{"chunk_text_hash": r[1]} for r in rows]
    state = {"calls": 0}

    def _get(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            return {"ids": list(ids), "metadatas": list(metas)}
        return {"ids": [], "metadatas": []}

    col = MagicMock()
    col.get.side_effect = _get
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
    db.get_or_create_collection.side_effect = lambda name: cols[name]
    db.get_collection.side_effect = lambda name: cols[name]
    # nexus-ks40: read paths now use ``get_collection`` (raises when
    # absent) so the mock must satisfy both name-resolution surfaces.
    db.get_collection.side_effect = lambda name: cols[name]
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


def test_prune_deleted_files_orphan_chunk_deleted(tmp_path):
    """RDR-108 Phase 4 / nexus-dyxe: a T3 chunk whose
    ``chunk_text_hash[:32]`` is NOT in the manifest's referenced-chash
    set is an orphan and must be deleted. Orphans are produced by
    deleted documents (FK CASCADE drops their manifest rows) and by
    re-indexing that supersedes older chunks."""
    from nexus.indexer import _prune_deleted_files
    live_chash = "a" * 64
    orphan_chash = "b" * 64
    col = _gc_col([("live-id-synthetic", live_chash),
                   ("orphan-id-synthetic", orphan_chash)])
    db = MagicMock(); db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    catalog = _gc_catalog({"code__repo": {live_chash[:32]}, "docs__repo": set()})

    _prune_deleted_files("code__repo", "docs__repo", db, catalog=catalog)

    deleted = _deleted_ids(col)
    assert "orphan-id-synthetic" in deleted
    assert "live-id-synthetic" not in deleted


def test_prune_deleted_files_preserves_live_synthetic_id(tmp_path):
    """Newly-indexed chunks still carry synthetic IDs (the indexer write
    path predates Phase 2's content-derived ID change). GC must preserve
    them by matching ``meta.chunk_text_hash[:32]`` against the manifest,
    NOT the chunk's natural ID."""
    from nexus.indexer import _prune_deleted_files
    chash = "a" * 64
    synthetic_id = "0123456789abcdef" * 2  # 32 hex chars unrelated to chash
    col = _gc_col([(synthetic_id, chash)])
    db = MagicMock(); db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    catalog = _gc_catalog({"code__repo": {chash[:32]}, "docs__repo": set()})

    _prune_deleted_files("code__repo", "docs__repo", db, catalog=catalog)

    assert col.delete.call_count == 0


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


def test_prune_deleted_files_chunk_without_chunk_text_hash_skipped(tmp_path):
    """A T3 chunk that lacks ``chunk_text_hash`` metadata cannot be
    proved live by the manifest, BUT silently sweeping such chunks
    would be data loss for pre-RDR-053 relics (~690 chunks in
    ``docs__scheme-evolution-research-b7de0b63`` per RDR-108 RF-1).
    GC must skip them with a warning and let the operator re-index
    or run ``nx t3 reidentify`` to populate the field."""
    from structlog.testing import capture_logs
    from nexus.indexer import _prune_deleted_files
    col = _gc_col([("ancient", ""), ("orphan", "b" * 64)])
    db = MagicMock(); db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    catalog = _gc_catalog({"code__repo": {"a" * 32}, "docs__repo": set()})

    with capture_logs() as cap:
        _prune_deleted_files("code__repo", "docs__repo", db, catalog=catalog)

    deleted = _deleted_ids(col)
    assert "ancient" not in deleted, (
        "chunk lacking chunk_text_hash must NOT be deleted; manifest "
        "cannot decide its liveness safely"
    )
    assert "orphan" in deleted
    # Warning naming the count must surface in the log so operators see it.
    skip_logs = [
        r for r in cap
        if r.get("event") == "skipped chunks without chunk_text_hash"
    ]
    assert skip_logs, (
        f"missing-hash skip must emit a warning event; got {cap}"
    )
    assert skip_logs[0]["count"] == 1
    assert skip_logs[0]["collection"] == "code__repo"


def test_prune_deleted_files_idempotent(tmp_path):
    """Re-running with no new orphans is a no-op (zero deletes)."""
    from nexus.indexer import _prune_deleted_files
    chash = "a" * 64
    catalog = _gc_catalog({"code__repo": {chash[:32]}, "docs__repo": set()})

    col = _gc_col([("live-id", chash)])
    db = MagicMock(); db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    _prune_deleted_files("code__repo", "docs__repo", db, catalog=catalog)
    assert col.delete.call_count == 0

    col2 = _gc_col([("live-id", chash)])
    db2 = MagicMock(); db2.get_or_create_collection.return_value = col2
    db2.get_collection.return_value = col2
    _prune_deleted_files("code__repo", "docs__repo", db2, catalog=catalog)
    assert col2.delete.call_count == 0


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
    from chromadb.errors import NotFoundError as _ChromaNotFoundError
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
    from chromadb.errors import NotFoundError as _ChromaNotFoundError
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
    each doc_id's chashes via the catalog manifest and delete by
    chash[:32] (the RDR-108 D1 natural id), not via where={doc_id}.

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
    # natural id = chash[:32], no doc_id in metadata).
    docs_col = MagicMock()
    docs_col.get.return_value = {
        "ids": [chash_a[:32], chash_b[:32]],
    }

    db = MagicMock()
    db.get_collection.side_effect = lambda name: (
        docs_col if name == "docs__repo" else MagicMock()
    )

    # Catalog manifest reports both chashes belong to the code file's doc.
    catalog = MagicMock()
    catalog.get_manifest.return_value = [
        SimpleNamespace(chash=chash_a, position=0),
        SimpleNamespace(chash=chash_b, position=1),
    ]

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

    # Manifest was queried for the doc_id.
    catalog.get_manifest.assert_any_call("1.1.5")
    # docs col was queried with the chash[:32] IDs.
    get_calls = docs_col.get.call_args_list
    assert any(
        set(call.kwargs.get("ids", [])) == {chash_a[:32], chash_b[:32]}
        for call in get_calls
    ), f"docs col.get must receive chash[:32] IDs; got {get_calls!r}"
    # Both chunks deleted from the wrong collection.
    deleted = _deleted_ids(docs_col)
    assert set(deleted) == {chash_a[:32], chash_b[:32]}


def test_prune_deleted_files_per_collection_orphan_isolation(tmp_path):
    """Both code and docs collections carry their own non-empty chunk
    sets; each collection's orphans must be deleted from its OWN col
    mock, not from a shared one. Locks the per-collection isolation
    that the ``_gc_db`` helper provides; pre-helper, the single shared
    ``col`` mock made it impossible to tell which collection's delete
    fired (and silently passed any test that asserted just the
    aggregate delete count).
    """
    from nexus.indexer import _prune_deleted_files

    code_live = "a" * 64
    code_orphan = "b" * 64
    docs_live = "c" * 64
    docs_orphan = "d" * 64

    db, cols = _gc_db({
        "code__repo": [
            ("code-live-id", code_live),
            ("code-orphan-id", code_orphan),
        ],
        "docs__repo": [
            ("docs-live-id", docs_live),
            ("docs-orphan-id", docs_orphan),
        ],
    })
    catalog = _gc_catalog({
        "code__repo": {code_live[:32]},
        "docs__repo": {docs_live[:32]},
    })

    _prune_deleted_files("code__repo", "docs__repo", db, catalog=catalog)

    code_deleted = _deleted_ids(cols["code__repo"])
    docs_deleted = _deleted_ids(cols["docs__repo"])

    # Each collection's orphan goes through ITS col.delete.
    assert code_deleted == ["code-orphan-id"], (
        f"code__repo deletes must isolate to code's col; got {code_deleted!r}"
    )
    assert docs_deleted == ["docs-orphan-id"], (
        f"docs__repo deletes must isolate to docs's col; got {docs_deleted!r}"
    )
    # Neither collection's live chunk gets touched.
    assert "code-live-id" not in code_deleted
    assert "docs-live-id" not in docs_deleted


def test_prune_deleted_files_round_trip_with_real_catalog(tmp_path):
    """RDR-108 Phase 4 / nexus-dyxe integration test: exercise the full
    catalog -> T3 round trip with a real SQLite-backed Catalog and a
    real ``chromadb.EphemeralClient`` collection. Document deletion via
    FK CASCADE drops manifest rows; the next GC sweep removes the now-
    orphaned T3 chunks while leaving live chunks intact.

    This locks the contract that ``chashes_for_collection`` returns
    correctly-truncated chashes that match T3 chunk metadata's
    ``chunk_text_hash[:32]``, end-to-end."""
    import hashlib

    import chromadb

    from nexus.catalog.catalog import Catalog
    from nexus.indexer import _prune_deleted_files

    # Real Catalog with real SQLite.
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    cat = Catalog(catalog_dir=catalog_dir, db_path=tmp_path / "catalog.sqlite")

    # Two documents in the same physical_collection; each carries one
    # chunk. We will delete the first document and verify GC sweeps
    # exactly its chunk.
    coll_name = "code__rt-test__voyage-code-3__v1"
    live_text = "def live(): return 1\n"
    orphan_text = "def gone(): return 2\n"
    live_chash = hashlib.sha256(live_text.encode()).hexdigest()
    orphan_chash = hashlib.sha256(orphan_text.encode()).hexdigest()

    for tumbler, fname in (("1.1.1", "live.py"), ("1.1.2", "gone.py")):
        cat._db.execute(  # epsilon-allow: integration fixture seeds documents
            "INSERT INTO documents "
            "(tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
            "metadata, source_mtime, alias_of, source_uri) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tumbler, fname, "", 0, "code", f"/tmp/{fname}",
             "", coll_name, 1, "", "", "{}", 0.0, "", ""),
        )
    cat._db.commit()

    cat.write_manifest("1.1.1", [
        {"chash": live_chash, "position": 0, "chunk_index": 0,
         "line_start": 1, "line_end": 1, "char_start": 0,
         "char_end": len(live_text)},
    ])
    cat.write_manifest("1.1.2", [
        {"chash": orphan_chash, "position": 0, "chunk_index": 0,
         "line_start": 1, "line_end": 1, "char_start": 0,
         "char_end": len(orphan_text)},
    ])

    # Real ChromaDB EphemeralClient with both chunks present.
    chroma = chromadb.EphemeralClient()
    col = chroma.get_or_create_collection(coll_name)
    col.upsert(
        ids=[live_chash[:32], orphan_chash[:32]],
        documents=[live_text, orphan_text],
        metadatas=[
            {"chunk_text_hash": live_chash, "title": "live.py:1-1"},
            {"chunk_text_hash": orphan_chash, "title": "gone.py:1-1"},
        ],
    )
    assert len(col.get()["ids"]) == 2

    class _DBShim:
        def get_or_create_collection(self, name):
            return chroma.get_or_create_collection(name)

        # nexus-ks40: read-only GC paths now use ``get_collection``;
        # forward to chroma so the integration test still wires through.
        def get_collection(self, name):
            return chroma.get_collection(name)

    # Delete the second document. FK CASCADE removes its manifest rows.
    cat._db.execute(  # epsilon-allow: integration fixture forces FK CASCADE
        "DELETE FROM documents WHERE tumbler = ?", ("1.1.2",),
    )
    cat._db.commit()

    _prune_deleted_files(coll_name, "docs__unused", _DBShim(), catalog=cat)

    remaining = set(col.get()["ids"])
    assert remaining == {live_chash[:32]}


def test_run_index_prune_misclassified(tmp_path):
    from nexus.indexer import _prune_misclassified
    repo = tmp_path / "repo"; repo.mkdir()
    cc = MagicMock(); cc.get.return_value = {"ids": []}
    dc = MagicMock(); dc.get.return_value = {"ids": ["stale-1"]}
    db = MagicMock(); db.get_or_create_collection.side_effect = {"code__repo": cc, "docs__repo": dc}.get
    db.get_collection.side_effect = {"code__repo": cc, "docs__repo": dc}.get
    _prune_misclassified(repo, "code__repo", "docs__repo", [repo/"main.py"], [repo/"README.md"], [], db)
    dc.delete.assert_called_once_with(ids=["stale-1"])


def test_registry_c2_fallback(tmp_path):
    from nexus.indexer import _repo_collection_or_legacy, _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    reg = _reg({"collection": "code__repo", "code_collection": "code__repo"})
    # RDR-103 Phase 5: registry's docs_collection is missing, so the
    # indexer falls back to ``_repo_collection_or_legacy`` which now
    # synthesises a conformant 4-segment name from the path-derived
    # identity instead of returning the pre-Phase-5 legacy 2-segment shape.
    expected = _repo_collection_or_legacy(repo, "docs")
    names: list[str] = []
    col = MagicMock(); col.get.return_value = {"metadatas": [], "ids": []}
    db = MagicMock(); db.get_or_create_collection.side_effect = lambda n: (names.append(n), col)[1]
    db.get_collection.side_effect = lambda n: (names.append(n), col)[1]
    with _patches(db): _run_index(repo, reg)
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
    from nexus.indexer import _index_code_file
    repo = tmp_path / "repo"; repo.mkdir(); (repo / "main.py").write_text("x = 1\n")
    db, col = _mock_db(); v = _voyage(1)
    with patch("nexus.chunker.chunk_file", return_value=[_chunk(), _chunk(text="", idx=1, count=2)]):
        r = _index_code_file(repo/"main.py", repo, "code__repo", "voyage-code-3",
                             col, db, v, git_meta={}, now_iso="2026-01-01T00:00:00", score=1.0)
    assert r == 1
    texts = v.embed.call_args[1].get("texts") or v.embed.call_args[0][0]
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
    with patch("nexus.indexer._run_index") as m: m.return_value = {}; index_repository(repo, registry, force=force_val) if force_val else index_repository(repo, registry)
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
    with _patches(db, extra={target: {}, "nexus.doc_indexer.batch_index_markdowns": {"return_value": {}}}) as mocks:
        _run_index(repo, _reg(), force=True)
    h = mocks[target.split(".")[-1]]; h.assert_called(); assert h.call_args.kwargs.get("force") is True


def test_run_index_passes_force_to_discover_rdrs(tmp_path):
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir(); (repo / "README.md").write_text("# R\n")
    db, _, _ = _tracking_db()
    with _patches(db, extra={
        "nexus.doc_indexer._embed_with_fallback": {"side_effect": lambda c, m, k, **kw: ([[0.1]*4]*len(c), m)},
        "nexus.indexer._discover_and_index_rdrs": {"return_value": (0, 0, 0)},
    }) as mocks:
        _run_index(repo, _reg(), force=True)
    mocks["_discover_and_index_rdrs"].assert_called_once()
    assert mocks["_discover_and_index_rdrs"].call_args.kwargs.get("force") is True


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
    from nexus.indexer import _index_prose_file
    repo = tmp_path / "repo"; repo.mkdir(); f = repo / "notes.txt"; f.write_text(content)
    db, col = _mock_db()
    if col_meta: col.get.return_value = col_meta
    with patch("nexus.doc_indexer._embed_with_fallback", return_value=([[0.1]*3], "voyage-context-3")):
        return _index_prose_file(f, repo, "docs__repo", "voyage-context-3",
                                 col, db, "fake-key", git_meta={}, now_iso="2026-01-01T00:00:00", score=1.0)

def _run_pdf(tmp_path, col_meta=None, n=1):
    from nexus.indexer import _index_pdf_file
    repo = tmp_path / "repo"; repo.mkdir(); f = repo / "paper.pdf"; f.write_bytes(b"%PDF-1.4 fake content")
    db, col = _mock_db()
    if col_meta: col.get.return_value = col_meta
    prep = [(f"id{i}", f"Page {i}", {"source_title":"T","page_number":i,"source_path":str(f),
             "corpus":"docs__repo","embedding_model":"voyage-context-3","store_type":"prose",
             "source_agent":"nexus-indexer"}) for i in range(1, n+1)]
    with patch("nexus.doc_indexer._pdf_chunks", return_value=prep), \
         patch("nexus.doc_indexer._embed_with_fallback", return_value=([[0.1]*3]*n, "voyage-context-3")):
        return _index_pdf_file(f, repo, "docs__repo", "voyage-context-3",
                               col, db, "fake-key", git_meta={}, now_iso="2026-01-01T00:00:00", score=1.0)

def _assert_int(r): assert isinstance(r, int) and not isinstance(r, bool)

def test_index_code_file_returns_int_not_bool(tmp_path): _assert_int(_run_code(tmp_path))
def test_index_code_file_returns_zero_when_skipped(tmp_path):
    h = hashlib.sha256(b"x = 1\ny = 2\n").hexdigest()
    r = _run_code(tmp_path, col_meta={"metadatas":[{"content_hash":h,"embedding_model":"voyage-code-3"}],"ids":[]}); assert r == 0; _assert_int(r)
def test_index_code_file_returns_positive_when_indexed(tmp_path):
    r = _run_code(tmp_path, chunks=[_chunk(idx=0,count=2), _chunk(text="y = 2",idx=1,count=2,ls=2,le=2)]); _assert_int(r); assert r == 2

def test_index_prose_file_returns_int_not_bool(tmp_path): _assert_int(_run_prose(tmp_path))
def test_index_prose_file_returns_zero_when_skipped(tmp_path):
    h = hashlib.sha256(b"Line one\nLine two\n").hexdigest()
    r = _run_prose(tmp_path, col_meta={"metadatas":[{"content_hash":h,"embedding_model":"voyage-context-3"}],"ids":[]}); assert r == 0; _assert_int(r)
def test_index_prose_file_returns_positive_when_indexed(tmp_path):
    r = _run_prose(tmp_path, content="Line one\nLine two\nLine three\n"); _assert_int(r); assert r > 0

def test_index_pdf_file_returns_int_not_bool(tmp_path): _assert_int(_run_pdf(tmp_path))
def test_index_pdf_file_returns_zero_when_skipped(tmp_path):
    h = hashlib.sha256(b"%PDF-1.4 fake content").hexdigest()
    r = _run_pdf(tmp_path, col_meta={"metadatas":[{"content_hash":h,"embedding_model":"voyage-context-3"}],"ids":[]}); assert r == 0; _assert_int(r)
def test_index_pdf_file_returns_positive_when_indexed(tmp_path):
    r = _run_pdf(tmp_path, n=2); _assert_int(r); assert r == 2


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

def test_rdr_files_do_not_trigger_on_file(tmp_path):
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    rdr = repo / "docs" / "rdr"; rdr.mkdir(parents=True)
    (repo / "code.py").write_text("x = 1\n")
    (rdr / "rdr-001.md").write_text("---\ntitle: t\nstatus: draft\ntype: feature\n---\n# T\n")
    cfg = {**_DEFAULT_CONFIG, "indexing": {**_DEFAULT_CONFIG["indexing"], "rdr_paths": ["docs/rdr"]}}
    db, _ = _mock_db(); calls: list[tuple] = []
    with _cb_patches(db, cfg=cfg, rdr=(1,0,0)):
        _run_index(repo, _reg(), on_file=lambda p,c,e: calls.append((p,c,e)))
    assert len(calls) == 1 and calls[0][0].name == "code.py"


# ── on_phase post-processing callbacks (nexus-vatx Gap 2) ───────────────────


def test_on_phase_fires_on_post_processing_phases(tmp_path):
    """`_run_index` emits phase markers for every post-per-file-loop stage
    so the operator can tell hung from busy after "[N/N]" finishes."""
    run, repo = _cb_repo(tmp_path)
    db, _ = _mock_db()
    phases: list[str] = []
    with _cb_patches(db, rdr=(1, 0, 0)):
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
    """The RDR "done" line carries the indexed/current/failed triple so
    operators see the same summary the final stats would show."""
    run, repo = _cb_repo(tmp_path)
    db, _ = _mock_db()
    phases: list[str] = []
    with _cb_patches(db, rdr=(4, 2, 1)):
        run(repo, _reg(), on_phase=phases.append)
    done = next(p for p in phases if p.startswith("RDR indexing done"))
    assert "4 indexed" in done
    assert "2 current" in done
    assert "1 failed" in done


def test_on_phase_none_is_safe(tmp_path):
    """on_phase=None must not raise — matches on_start/on_file idiom."""
    run, repo = _cb_repo(tmp_path)
    db, _ = _mock_db()
    with _cb_patches(db):
        run(repo, _reg())  # on_phase omitted → None default


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
        "chunking_s", "embed_s", "upload_s", "retry_s",
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
        "nexus.indexer._discover_and_index_rdrs": {"return_value": (0, 0, 0)},
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

def test_prune_deleted_files_paginates(tmp_path):
    """RDR-108 Phase 4 / nexus-dyxe: pagination still walks the whole
    collection and deletes orphans across page boundaries."""
    from nexus.indexer import _prune_deleted_files
    live = [(f"live-{i:03d}", f"a{i:03d}" + "0" * 60) for i in range(200)]
    orphan = [(f"orphan-{i:03d}", f"b{i:03d}" + "0" * 60) for i in range(110)]
    live_ids = {r[0] for r in live}
    orphan_ids = {r[0] for r in orphan}
    live_chashes_32 = {r[1][:32] for r in live}
    # Force pagination by filling page 1 to exactly _CHROMA_PAGE_SIZE=300 rows
    # so the helper continues to page 2 instead of short-circuiting.
    p1_rows = live + orphan[:100]
    p2_rows = orphan[100:]
    p1 = {"ids": [r[0] for r in p1_rows],
          "metadatas": [{"chunk_text_hash": r[1]} for r in p1_rows]}
    p2 = {"ids": [r[0] for r in p2_rows],
          "metadatas": [{"chunk_text_hash": r[1]} for r in p2_rows]}
    p3 = {"ids": [], "metadatas": []}
    mock_cols: list[MagicMock] = []
    def mc():
        c = MagicMock(); c.get.side_effect = [p1, p2, p3]; return c
    db = MagicMock()
    db.get_or_create_collection.side_effect = lambda _: (mock_cols.append(mc()), mock_cols[-1])[1]
    db.get_collection.side_effect = lambda _: (mock_cols.append(mc()), mock_cols[-1])[1]
    db.get_collection.side_effect = lambda _: (mock_cols.append(mc()), mock_cols[-1])[1]
    catalog = _gc_catalog({"code__repo": live_chashes_32, "docs__repo": live_chashes_32})

    _prune_deleted_files("code__repo", "docs__repo", db, catalog=catalog)

    for col in mock_cols:
        d = set()
        for c in col.delete.call_args_list:
            d.update(c.kwargs.get("ids") or (c.args[0] if c.args else []))
        assert orphan_ids.issubset(d)
        assert not live_ids.intersection(d)


def test_frecency_update_paginates(tmp_path):
    from nexus.indexer import _run_index_frecency_only
    repo = tmp_path / "repo"; repo.mkdir(); src = repo / "big.py"; src.write_text("# g\n")
    p1 = {"ids": [f"c-{i}" for i in range(300)], "metadatas": [{"frecency_score":0.0,"source_path":str(src)}]*300}
    p2 = {"ids": [f"c-{i}" for i in range(300,310)], "metadatas": [{"frecency_score":0.0,"source_path":str(src)}]*10}
    cc = MagicMock(); cc.get.side_effect = [p1, p2]
    dc = MagicMock(); dc.get.return_value = {"ids":[],"metadatas":[]}
    db = MagicMock(); db.get_or_create_collection.side_effect = {"code__repo":cc,"docs__repo":dc}.get
    db.get_collection.side_effect = {"code__repo":cc,"docs__repo":dc}.get
    with patch("nexus.frecency.batch_frecency", return_value={src: 0.9}), \
         patch("nexus.config.get_credential", return_value="fake-key"), \
         patch("nexus.db.make_t3", return_value=db):
        _run_index_frecency_only(repo, _reg())
    ids = set()
    for c in db.update_chunks.call_args_list: ids.update(c.kwargs.get("ids") or c.args[0])
    assert len(ids) == 310


def test_prune_misclassified_paginates(tmp_path):
    from nexus.indexer import _prune_misclassified
    repo = tmp_path / "repo"; repo.mkdir(); bp = repo / "g.md"; bp.write_text("# b\n")
    cc = MagicMock(); cc.get.side_effect = [{"ids":[f"s-{i}" for i in range(300)]}, {"ids":[f"s-{i}" for i in range(300,310)]}]
    dc = MagicMock(); dc.get.return_value = {"ids":[]}
    db = MagicMock(); db.get_or_create_collection.side_effect = {"code__repo":cc,"docs__repo":dc}.get
    db.get_collection.side_effect = {"code__repo":cc,"docs__repo":dc}.get
    _prune_misclassified(repo, "code__repo", "docs__repo", [], [bp], [], db)
    d = set()
    for c in cc.delete.call_args_list: d.update(c.kwargs.get("ids") or (c.args[0] if c.args else []))
    assert d == {f"s-{i}" for i in range(310)}


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


def test_prune_misclassified_falls_back_to_source_path_for_unmapped_files(tmp_path):
    """Files missing from ``file_to_doc_id`` use the legacy source_path
    lookup so chunks indexed before the catalog backfill keep getting
    cleaned up.
    """
    from nexus.indexer import _prune_misclassified
    repo = tmp_path / "repo"
    repo.mkdir()
    bp = repo / "legacy.md"
    bp.write_text("# l\n")
    cc = MagicMock()
    cc.get.return_value = {"ids": ["chunk-legacy"]}
    dc = MagicMock()
    dc.get.return_value = {"ids": []}
    db = MagicMock()
    db.get_or_create_collection.side_effect = {"code__repo": cc, "docs__repo": dc}.get
    db.get_collection.side_effect = {"code__repo": cc, "docs__repo": dc}.get
    db.get_collection.side_effect = {"code__repo": cc, "docs__repo": dc}.get
    _prune_misclassified(
        repo, "code__repo", "docs__repo",
        [], [bp], [], db,
        file_to_doc_id={},
    )
    where = cc.get.call_args.kwargs["where"]
    assert where == {"source_path": str(bp)}


# ── Lock file cleanup ───────────────────────────────────────────────────────

@pytest.mark.parametrize("side_effect", [None, RuntimeError("boom"), CredentialsMissingError("x")])
def test_lock_file_deleted_after_index(tmp_path, registry, side_effect):
    repo = tmp_path / "repo"; repo.mkdir(); ld = tmp_path / "locks"; ld.mkdir()
    with patch("nexus.indexer._repo_lock_path", side_effect=lambda r: ld / "test.lock"), \
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
    with patch("nexus.indexer._repo_lock_path", side_effect=lambda r: lf), patch("nexus.indexer._run_index"):
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
    from nexus.indexer import _index_prose_file
    repo = tmp_path / "repo"; repo.mkdir(); f = repo / f"notes{ext}"; f.write_text(content)
    docs: list[str] = []; metas: list[dict] = []; db, col = _mock_db()
    db.upsert_chunks_with_embeddings.side_effect = lambda *a, **kw: (docs.extend(kw.get("documents",a[2] if len(a)>2 else [])), metas.extend(kw.get("metadatas",a[4] if len(a)>4 else [])))
    with patch("nexus.doc_indexer._embed_with_fallback", return_value=([[0.1]*3,[0.2]*3], "voyage-context-3")):
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
