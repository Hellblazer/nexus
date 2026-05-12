# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the extracted indexer sub-modules (RDR-032)."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.errors import CredentialsMissingError
from nexus.index_context import IndexContext
from nexus.indexer_utils import build_context_prefix, check_credentials, check_staleness


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def make_ctx(tmp_path: Path):
    """Factory for IndexContext with sensible defaults."""
    def _make(**overrides):
        defaults = dict(
            col=MagicMock(), db=MagicMock(), voyage_key="key",
            voyage_client=MagicMock(), repo_path=tmp_path,
            corpus="code__test", embedding_model="voyage-code-3",
            git_meta={}, now_iso="2026-01-01T00:00:00+00:00", force=False,
        )
        defaults.update(overrides)
        return IndexContext(**defaults)
    return _make


# ── IndexContext ─────────────────────────────────────────────────────────────

def test_index_context_defaults(make_ctx):
    ctx = make_ctx()
    assert ctx.score == 0.0
    assert ctx.chunk_lines is None
    assert ctx.force is False
    assert ctx.timeout == 120.0
    assert ctx.tuning is None


def test_index_context_score_mutability(make_ctx):
    ctx = make_ctx(score=1.5)
    assert ctx.score == 1.5
    ctx.score = 2.5
    assert ctx.score == 2.5


# ── check_staleness ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("stored_hash,stored_model,query_hash,query_model,expected", [
    ("abc123", "voyage-code-3", "abc123", "voyage-code-3", True),   # current
    ("old_hash", "voyage-code-3", "new_hash", "voyage-code-3", False),  # stale hash
    ("abc123", "old-model", "abc123", "voyage-code-3", False),       # model mismatch
])
def test_check_staleness(tmp_path, stored_hash, stored_model, query_hash, query_model, expected):
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "metadatas": [{"content_hash": stored_hash, "embedding_model": stored_model}],
        "ids": ["id1"],
    }
    assert check_staleness(mock_col, tmp_path / "foo.py", query_hash, query_model) is expected


def test_check_staleness_no_existing_chunks(tmp_path):
    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": [], "ids": []}
    assert check_staleness(mock_col, tmp_path / "foo.py", "abc123", "voyage-code-3") is False


def test_check_staleness_uses_content_hash_when_provided(tmp_path):
    """RDR-108 Phase 3: chunks no longer carry ``doc_id``; ``check_staleness``
    keys on ``content_hash`` (which is in ALLOWED_TOP_LEVEL and shared by
    every chunk of the same file). The ``doc_id=`` kwarg is accepted for
    backwards compatibility but is no longer the where-filter key.
    """
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "metadatas": [{
            "content_hash": "abc",
            "embedding_model": "voyage-code-3",
        }],
        "ids": ["id1"],
    }
    result = check_staleness(
        mock_col, tmp_path / "shared.py", "abc", "voyage-code-3",
        doc_id="ART-deadbeef",
    )
    assert result is True
    where = mock_col.get.call_args.kwargs["where"]
    assert where == {"content_hash": "abc"}


def test_check_staleness_falls_back_to_source_path_when_no_content_hash(tmp_path):
    """No content_hash passed → legacy source_path lookup (back-compat
    for direct test callers; production CLI ingest always supplies a
    content_hash).
    """
    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": [], "ids": []}
    file_path = tmp_path / "foo.py"
    check_staleness(mock_col, file_path, "", "voyage-code-3")
    where = mock_col.get.call_args.kwargs["where"]
    assert where == {"source_path": str(file_path)}


def test_check_staleness_passes_when_chunk_has_matching_content_hash(tmp_path):
    """Inverse: stored chunk has the same content_hash + model → stale."""
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "metadatas": [{
            "content_hash": "abc",
            "embedding_model": "voyage-code-3",
        }],
        "ids": ["chunk-0"],
    }
    result = check_staleness(
        mock_col, tmp_path / "foo.py", "abc", "voyage-code-3",
        doc_id="ART-deadbeef",
    )
    assert result is True


# ── check_credentials ────────────────────────────────────────────────────────

def test_check_credentials_both_present():
    check_credentials("voyage-key", "chroma-key")  # no exception


@pytest.mark.parametrize("voyage,chroma,match", [
    ("", "chroma-key", "voyage_api_key"),
    ("voyage-key", "", "chroma_api_key"),
])
def test_check_credentials_missing(voyage, chroma, match):
    with pytest.raises(CredentialsMissingError, match=match):
        check_credentials(voyage, chroma)


def test_check_credentials_both_missing():
    with pytest.raises(CredentialsMissingError) as exc_info:
        check_credentials("", "")
    msg = str(exc_info.value)
    assert "voyage_api_key" in msg and "chroma_api_key" in msg


# ── build_context_prefix ─────────────────────────────────────────────────────

@pytest.mark.parametrize("path,comment,cls,method,ls,le,expected", [
    ("src/foo.py", "#", "MyClass", "my_method", 10, 25,
     "# File: src/foo.py  Class: MyClass  Method: my_method  Lines: 10-25"),
    ("Foo.java", "//", "", "doStuff", 1, 5,
     "// File: Foo.java  Class:   Method: doStuff  Lines: 1-5"),
    ("script.sh", "#", "", "", 1, 20,
     "# File: script.sh  Class:   Method:   Lines: 1-20"),
])
def test_build_context_prefix(path, comment, cls, method, ls, le, expected):
    assert build_context_prefix(path, comment, cls, method, ls, le) == expected


# ── _extract_context backward compatibility ───────────────────────────────────

def test_extract_context_importable_from_nexus_indexer():
    from nexus.indexer import _extract_context as ec_indexer
    from nexus.code_indexer import _extract_context as ec_code
    assert ec_indexer is ec_code


# ── _extract_context with real Python source ─────────────────────────────────

@pytest.mark.parametrize("source,lang,start,end,exp_class,exp_method", [
    (b"def hello():\n    return 42\n", "python", 0, 1, "", "hello"),
    (b"class Foo:\n    def bar(self):\n        return 1\n", "python", 1, 2, "Foo", "bar"),
    (b"@staticmethod\ndef helper():\n    pass\n", "python", 0, 2, "", "helper"),
    (b"def foo():\n    pass\n\ndef bar():\n    pass\n", "python", 0, 4, "", ""),
    (b"some content", "unknown_lang_xyz", 0, 0, "", ""),
])
def test_extract_context(source, lang, start, end, exp_class, exp_method):
    from nexus.code_indexer import _extract_context
    cls, method = _extract_context(source, lang, start, end)
    assert cls == exp_class
    assert method == exp_method


# ── index_code_file ──────────────────────────────────────────────────────────

def test_index_code_file_skips_current_file(tmp_path, make_ctx):
    from nexus.code_indexer import index_code_file
    import hashlib

    py_file = tmp_path / "hello.py"
    py_file.write_text("print('hello')\n")
    h = hashlib.sha256(py_file.read_text().encode("utf-8")).hexdigest()
    ctx = make_ctx()

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry:
        mock_retry.return_value = {
            "metadatas": [{"content_hash": h, "embedding_model": "voyage-code-3"}],
            "ids": ["id1"],
        }
        assert index_code_file(ctx, py_file) == 0


def test_index_code_file_returns_zero_for_non_text_file(tmp_path, make_ctx):
    from nexus.code_indexer import index_code_file

    bin_file = tmp_path / "binary.py"
    bin_file.write_bytes(b"\xff\xfe binary content")
    ctx = make_ctx(col=MagicMock())
    ctx.col.get.return_value = {"metadatas": [], "ids": []}

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry:
        mock_retry.return_value = {"metadatas": [], "ids": []}
        assert index_code_file(ctx, bin_file) == 0


def test_index_code_file_happy_path_new_file(tmp_path, make_ctx):
    from nexus.code_indexer import index_code_file

    py_file = tmp_path / "example.py"
    py_file.write_text("def greet(name):\n    return f'Hello {name}'\n")

    mock_voyage = MagicMock()
    mock_embed_result = MagicMock()
    mock_embed_result.embeddings = [[0.1] * 128]
    mock_voyage.embed.return_value = mock_embed_result
    mock_db = MagicMock()

    ctx = make_ctx(db=mock_db, voyage_client=mock_voyage,
                   git_meta={"git_project_name": "test"})

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry:
        mock_retry.return_value = {"metadatas": [], "ids": []}
        result = index_code_file(ctx, py_file)

    # nexus-8g79.23: small fixtures produce exactly 1 chunk.

    assert result == 1
    mock_voyage.embed.assert_called()
    call_kwargs = mock_db.upsert_chunks_with_embeddings.call_args[1]
    assert call_kwargs["collection_name"] == "code__test"
    assert len(call_kwargs["ids"]) == result


# ── index_prose_file ─────────────────────────────────────────────────────────

def test_index_prose_file_skips_current_file(tmp_path, make_ctx):
    from nexus.prose_indexer import index_prose_file
    import hashlib

    md_file = tmp_path / "README.md"
    md_file.write_text("# Hello\n\nSome content here.\n")
    h = hashlib.sha256(md_file.read_text().encode()).hexdigest()
    ctx = make_ctx(corpus="docs__test", embedding_model="voyage-context-3")

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry:
        mock_retry.return_value = {
            "metadatas": [{"content_hash": h, "embedding_model": "voyage-context-3"}],
            "ids": ["id1"],
        }
        assert index_prose_file(ctx, md_file) == 0


def test_index_prose_file_non_markdown_uses_line_chunk(tmp_path, make_ctx):
    from nexus.prose_indexer import index_prose_file

    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("Line one of notes.\nLine two of notes.\nLine three.\n")
    mock_db = MagicMock()
    ctx = make_ctx(db=mock_db, voyage_client=None,
                   corpus="docs__test", embedding_model="voyage-context-3")

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry, \
         patch("nexus.doc_indexer._embed_with_fallback") as mock_embed:
        mock_retry.return_value = {"metadatas": [], "ids": []}
        mock_embed.return_value = ([[0.1] * 128], "voyage-context-3")
        result = index_prose_file(ctx, txt_file)

    # nexus-8g79.23: small fixtures produce exactly 1 chunk.

    assert result == 1
    mock_embed.assert_called_once()
    embed_texts = mock_embed.call_args[0][0]
    upsert_kwargs = mock_db.upsert_chunks_with_embeddings.call_args[1]
    assert embed_texts == upsert_kwargs["documents"]
    meta = upsert_kwargs["metadatas"][0]
    assert "line_start" in meta and meta["line_start"] >= 1


# ── RDR-102 Phase B: source_path absent from indexer-stamped chunk meta ──


def test_index_code_file_does_not_emit_source_path(tmp_path, make_ctx):
    """RDR-102 Phase B / D2: code_indexer at line 402 of code_indexer.py
    must drop the ``source_path=str(file_path)`` kwarg from its
    make_chunk_metadata call. After Phase B, every code chunk landing
    in T3 carries no source_path key — the catalog tumbler in
    ``doc_id`` is the canonical reference and ``normalize()`` filters
    source_path out at the schema-level removal.
    """
    from nexus.code_indexer import index_code_file

    py_file = tmp_path / "phase_b.py"
    py_file.write_text("def fn():\n    return 'phase B'\n")

    mock_voyage = MagicMock()
    mock_embed_result = MagicMock()
    mock_embed_result.embeddings = [[0.1] * 128]
    mock_voyage.embed.return_value = mock_embed_result
    mock_db = MagicMock()
    ctx = make_ctx(
        db=mock_db, voyage_client=mock_voyage,
        git_meta={"git_project_name": "phase-b-test"},
    )

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry:
        mock_retry.return_value = {"metadatas": [], "ids": []}
        result = index_code_file(ctx, py_file)

    # nexus-8g79.23: small fixtures produce exactly 1 chunk.

    assert result == 1
    metadatas = mock_db.upsert_chunks_with_embeddings.call_args[1]["metadatas"]
    leaked = [m for m in metadatas if "source_path" in m]
    assert not leaked, (
        f"{len(leaked)}/{len(metadatas)} code-indexer chunks still carry "
        f"source_path. Phase B must drop the source_path= kwarg from "
        f"the make_chunk_metadata call at code_indexer.py:402, AND "
        f"remove source_path from ALLOWED_TOP_LEVEL so normalize() "
        f"filters any residual writes."
    )


def test_index_prose_file_markdown_does_not_emit_source_path(
    tmp_path, make_ctx,
):
    """RDR-102 Phase B / D2: prose_indexer markdown branch (branch A,
    line 103 of prose_indexer.py) must drop source_path from its
    make_chunk_metadata call.
    """
    from nexus.prose_indexer import index_prose_file

    md_file = tmp_path / "phase_b_branch_a.md"
    md_file.write_text("# Phase B Branch A\n\nMarkdown body content.\n")
    mock_db = MagicMock()
    ctx = make_ctx(
        db=mock_db, voyage_client=None,
        corpus="docs__phase-b-md", embedding_model="voyage-context-3",
    )

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry, \
         patch("nexus.doc_indexer._embed_with_fallback") as mock_embed:
        mock_retry.return_value = {"metadatas": [], "ids": []}
        mock_embed.return_value = ([[0.1] * 128], "voyage-context-3")
        result = index_prose_file(ctx, md_file)

    # nexus-8g79.23: small fixtures produce exactly 1 chunk.

    assert result == 1
    metadatas = mock_db.upsert_chunks_with_embeddings.call_args[1]["metadatas"]
    leaked = [m for m in metadatas if "source_path" in m]
    assert not leaked, (
        f"{len(leaked)}/{len(metadatas)} prose-indexer (markdown branch) "
        f"chunks still carry source_path. Phase B must drop source_path "
        f"from the make_chunk_metadata call at prose_indexer.py:103."
    )


def test_index_prose_file_line_chunk_does_not_emit_source_path(
    tmp_path, make_ctx,
):
    """RDR-102 Phase B / D2: prose_indexer non-markdown branch (branch
    B, line 183 of prose_indexer.py — used for .txt and other plain
    text) must drop source_path from its make_chunk_metadata call.
    """
    from nexus.prose_indexer import index_prose_file

    txt_file = tmp_path / "phase_b_branch_b.txt"
    txt_file.write_text("Line one of phase B branch B.\nLine two.\n")
    mock_db = MagicMock()
    ctx = make_ctx(
        db=mock_db, voyage_client=None,
        corpus="docs__phase-b-txt", embedding_model="voyage-context-3",
    )

    with patch("nexus.indexer_utils._chroma_with_retry") as mock_retry, \
         patch("nexus.doc_indexer._embed_with_fallback") as mock_embed:
        mock_retry.return_value = {"metadatas": [], "ids": []}
        mock_embed.return_value = ([[0.1] * 128], "voyage-context-3")
        result = index_prose_file(ctx, txt_file)

    # nexus-8g79.23: small fixtures produce exactly 1 chunk.

    assert result == 1
    metadatas = mock_db.upsert_chunks_with_embeddings.call_args[1]["metadatas"]
    leaked = [m for m in metadatas if "source_path" in m]
    assert not leaked, (
        f"{len(leaked)}/{len(metadatas)} prose-indexer (line-chunk "
        f"branch) chunks still carry source_path. Phase B must drop "
        f"source_path from the make_chunk_metadata call at "
        f"prose_indexer.py:183."
    )


# ── GH #371 + #436: oversize file guard ─────────────────────────────────────


def test_indexer_oversize_guard_skips_huge_files(tmp_path, monkeypatch) -> None:
    """GH #371 + #436: a file larger than ``indexing.max_file_bytes``
    must be skipped BEFORE classification + read_text. Pre-fix, a
    single huge file (vendored bundle, generated payload) loaded its
    full content into memory in ``read_text`` -- causing the parent
    to OOM (#371) or the per-file loop to stall while the buffer
    allocated under memory pressure (#436).

    Uses the ``include_untracked=True`` path so we can exercise the
    walker without committing files to git, keeping the test unit-fast.
    """
    import subprocess

    from nexus.indexer import index_repository
    from nexus.registry import RepoRegistry

    repo = tmp_path / "test-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@x.io"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=repo, check=True,
    )
    (repo / "small.py").write_text("def f():\n    return 1\n")
    huge = repo / "vendor.min.js"
    huge.write_bytes(b"x" * (6 * 1024 * 1024))  # 6 MiB
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=repo, check=True,
    )

    phases: list[str] = []
    def on_phase(msg: str) -> None:
        phases.append(msg)

    # Sandbox config so the registry doesn't pollute the operator's
    # real ~/.config/nexus.
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("NEXUS_SKIP_T1", "1")
    monkeypatch.setenv("NX_LOCAL", "1")  # local mode, no Voyage credentials

    reg = RepoRegistry(cfg_dir / "repos.json")
    reg.add(repo)
    try:
        index_repository(repo, reg, on_phase=on_phase)
    except Exception:
        # The indexer may bail on missing credentials or on the
        # local-only path's pipeline. The oversize guard fires
        # BEFORE any of that, so the on_phase notice still lands.
        pass

    oversize_msgs = [p for p in phases if "oversized" in p.lower()]
    assert oversize_msgs, (
        f"expected an 'oversized file' phase message; got: {phases}"
    )
    assert any("vendor.min.js" in p for p in oversize_msgs), (
        f"oversize message should name the file; got: {oversize_msgs}"
    )
