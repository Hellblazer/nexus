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
    db.upsert_chunks_with_embeddings.side_effect = cap
    return db, ups, cols


def _mock_db():
    col = MagicMock(); col.get.return_value = {"metadatas": [], "ids": []}
    db = MagicMock(); db.get_or_create_collection.return_value = col
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


# ── Absolute source_path ────────────────────────────────────────────────────

def test_run_index_source_path_is_absolute(tmp_path):
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
    assert cap and Path(cap[0]["source_path"]).is_absolute()
    assert cap[0]["source_path"] == str(repo / "main.py")


# ── Content-hash dedup ──────────────────────────────────────────────────────

def test_run_index_reindexes_when_embedding_model_changed(tmp_path):
    from nexus.indexer import _run_index
    repo = tmp_path / "repo"; repo.mkdir()
    content = "x = 1\n"; (repo / "main.py").write_text(content)
    h = hashlib.sha256(content.encode()).hexdigest()
    col = MagicMock()
    col.get.return_value = {"metadatas": [{"content_hash": h, "embedding_model": "voyage-4"}], "ids": []}
    db = MagicMock(); db.get_or_create_collection.return_value = col
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
    with patch("nexus.frecency.batch_frecency", return_value={src: 0.75}), \
         patch("nexus.config.get_credential", return_value="fake-key"), \
         patch("nexus.db.make_t3", return_value=db):
        _run_index_frecency_only(repo, _reg())
    kw = db.update_chunks.call_args_list[0].kwargs
    assert kw["ids"] == ["c1"]
    assert kw["metadatas"][0]["frecency_score"] == 0.75
    assert kw["metadatas"][0]["title"] == "main.py:1-1"


def test_frecency_only_skips_unindexed_files(tmp_path):
    from nexus.indexer import _run_index_frecency_only
    repo = tmp_path / "repo"; repo.mkdir()
    src = repo / "new.py"; src.write_text("y = 2\n")
    col = MagicMock(); col.get.return_value = {"ids": [], "metadatas": []}
    db = MagicMock(); db.get_or_create_collection.return_value = col
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
        paths = [m["source_path"] for m in ups["docs__repo"]]
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
    (repo / "notes.txt").write_text("Some notes about the project.\n")
    db, ups, _ = _tracking_db()
    with _patches(db, extra={
        "nexus.chunker.chunk_file": {"return_value": [_chunk(text="print('hello')")]},
        "voyageai.Client": {"return_value": _voyage(1)},
        "nexus.doc_indexer._embed_with_fallback": {"return_value": ([[0.1]*10], "voyage-context-3")},
    }):
        _run_index(repo, _reg())
    assert any("main.py" in m["source_path"] for m in ups["code__repo"])
    dp = {m["source_path"] for m in ups["docs__repo"]}
    assert any("README.md" in p for p in dp) and any("notes.txt" in p for p in dp)


# ── Prune helpers ────────────────────────────────────────────────────────────

def test_run_index_prune_deleted_files(tmp_path):
    from nexus.indexer import _prune_deleted_files
    col = MagicMock()
    col.get.return_value = {"ids": ["a1","b1","b2"], "metadatas": [
        {"source_path": "/repo/a.py"}, {"source_path": "/repo/b.py"}, {"source_path": "/repo/b.py"}]}
    db = MagicMock(); db.get_or_create_collection.return_value = col
    _prune_deleted_files("code__repo", "docs__repo", {"/repo/a.py"}, db)
    for dc in col.delete.call_args_list:
        ids = dc.kwargs.get("ids") or dc[1].get("ids") if dc[1] else dc[0][0]
        if isinstance(ids, list):
            assert "a1" not in ids and ("b1" in ids or "b2" in ids)


def test_run_index_prune_misclassified(tmp_path):
    from nexus.indexer import _prune_misclassified
    repo = tmp_path / "repo"; repo.mkdir()
    cc = MagicMock(); cc.get.return_value = {"ids": []}
    dc = MagicMock(); dc.get.return_value = {"ids": ["stale-1"]}
    db = MagicMock(); db.get_or_create_collection.side_effect = {"code__repo": cc, "docs__repo": dc}.get
    _prune_misclassified(repo, "code__repo", "docs__repo", [repo/"main.py"], [repo/"README.md"], [], db)
    dc.delete.assert_called_once_with(ids=["stale-1"])


def test_registry_c2_fallback(tmp_path):
    from nexus.indexer import _run_index; from nexus.registry import _docs_collection_name
    repo = tmp_path / "repo"; repo.mkdir()
    reg = _reg({"collection": "code__repo", "code_collection": "code__repo"})
    expected = _docs_collection_name(repo)
    names: list[str] = []
    col = MagicMock(); col.get.return_value = {"metadatas": [], "ids": []}
    db = MagicMock(); db.get_or_create_collection.side_effect = lambda n: (names.append(n), col)[1]
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


# ── Pagination tests ────────────────────────────────────────────────────────

def test_prune_deleted_files_paginates(tmp_path):
    from nexus.indexer import _prune_deleted_files
    sa, sb = "/repo/a.py", "/repo/b.py"
    p1 = {"ids": [f"a-{i}" for i in range(200)] + [f"b-{i}" for i in range(100)],
          "metadatas": [{"source_path": sa}]*200 + [{"source_path": sb}]*100}
    p2 = {"ids": [f"b-{i}" for i in range(100,110)], "metadatas": [{"source_path": sb}]*10}
    mock_cols: list[MagicMock] = []
    def mc():
        c = MagicMock(); c.get.side_effect = [p1, p2]; return c
    db = MagicMock(); db.get_or_create_collection.side_effect = lambda _: (mock_cols.append(mc()), mock_cols[-1])[1]
    _prune_deleted_files("code__repo", "docs__repo", {sa}, db)
    for col in mock_cols:
        d = set()
        for c in col.delete.call_args_list: d.update(c.kwargs.get("ids") or (c.args[0] if c.args else []))
        assert {f"b-{i}" for i in range(110)}.issubset(d) and not {f"a-{i}" for i in range(200)}.intersection(d)


def test_frecency_update_paginates(tmp_path):
    from nexus.indexer import _run_index_frecency_only
    repo = tmp_path / "repo"; repo.mkdir(); src = repo / "big.py"; src.write_text("# g\n")
    p1 = {"ids": [f"c-{i}" for i in range(300)], "metadatas": [{"frecency_score":0.0,"source_path":str(src)}]*300}
    p2 = {"ids": [f"c-{i}" for i in range(300,310)], "metadatas": [{"frecency_score":0.0,"source_path":str(src)}]*10}
    cc = MagicMock(); cc.get.side_effect = [p1, p2]
    dc = MagicMock(); dc.get.return_value = {"ids":[],"metadatas":[]}
    db = MagicMock(); db.get_or_create_collection.side_effect = {"code__repo":cc,"docs__repo":dc}.get
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
    _prune_misclassified(repo, "code__repo", "docs__repo", [], [bp], [], db)
    d = set()
    for c in cc.delete.call_args_list: d.update(c.kwargs.get("ids") or (c.args[0] if c.args else []))
    assert d == {f"s-{i}" for i in range(310)}


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
