# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for repo-aware helpers in indexer_utils (nexus-h74)."""
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.indexer_utils import (
    find_repo_root,
    is_gitignored,
    load_ignore_patterns,
    should_ignore,
)


# ── should_ignore ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("node_modules/pkg/index.js", True),
    ("src/main.py", False),
    (".venv/lib/python3.12/site.py", True),
    ("dist/bundle.js", True),
    ("package-lock.lock", True),   # *.lock matches any file ending in .lock
    ("yarn.lock", True),           # *.lock matches
    ("src/vendor/lib.py", True),   # vendor in any component
    ("build/output.js", True),
])
def test_should_ignore(path: str, expected: bool) -> None:
    from nexus.indexer_utils import _DEFAULT_IGNORE
    assert should_ignore(Path(path), _DEFAULT_IGNORE) == expected


# ── find_repo_root ──────────────────────────────────────────────────────────

def test_find_repo_root_in_git_repo(tmp_path: Path) -> None:
    """find_repo_root returns repo root when inside a git repo."""
    import subprocess
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subdir = repo / "sub" / "deep"
    subdir.mkdir(parents=True)

    root = find_repo_root(subdir)
    assert root is not None
    assert root.resolve() == repo.resolve()


def test_find_repo_root_not_git(tmp_path: Path) -> None:
    """find_repo_root returns None for non-git directories."""
    assert find_repo_root(tmp_path) is None


def test_find_repo_root_from_file(tmp_path: Path) -> None:
    """find_repo_root works when given a file path (uses parent dir)."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    f = repo / "test.txt"
    f.write_text("hello")

    root = find_repo_root(f)
    assert root is not None
    assert root.resolve() == repo.resolve()


# ── is_gitignored ──────────────────────────────────────────────────────────

def test_is_gitignored_respected(tmp_path: Path) -> None:
    """is_gitignored returns True for .gitignore'd files."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    (repo / ".gitignore").write_text("*.pdf\n")
    (repo / "paper.pdf").write_bytes(b"%PDF-1.4 test")
    (repo / "code.py").write_text("x = 1\n")

    assert is_gitignored(repo / "paper.pdf", repo) is True
    assert is_gitignored(repo / "code.py", repo) is False


def test_is_gitignored_non_repo(tmp_path: Path) -> None:
    """is_gitignored returns False gracefully for non-repo dirs."""
    (tmp_path / "file.txt").write_text("test")
    assert is_gitignored(tmp_path / "file.txt", tmp_path) is False


# ── load_ignore_patterns ────────────────────────────────────────────────────

def test_load_ignore_patterns_defaults() -> None:
    """Without repo config, returns at least the default patterns."""
    patterns = load_ignore_patterns()
    assert "node_modules" in patterns
    assert ".git" in patterns
    assert "*.lock" in patterns


def test_load_ignore_patterns_merges_config(tmp_path: Path) -> None:
    """load_ignore_patterns picks up .nexus.yml ignorePatterns."""
    nexus_yml = tmp_path / ".nexus.yml"
    nexus_yml.write_text("server:\n  ignorePatterns:\n    - '*.bak'\n    - tmp\n")
    patterns = load_ignore_patterns(tmp_path)
    assert "*.bak" in patterns
    assert "tmp" in patterns
    assert "node_modules" in patterns  # defaults still present


# ── Path resolution in index_pdf ────────────────────────────────────────────

def test_index_pdf_resolves_path(tmp_path: Path, monkeypatch) -> None:
    """index_pdf normalizes pdf_path to absolute before staleness check."""
    import hashlib
    from nexus.doc_indexer import index_pdf

    pdf = tmp_path / "test.pdf"
    content = b"%PDF-1.4 test content"
    pdf.write_bytes(content)
    real_hash = hashlib.sha256(content).hexdigest()

    # Mock credentials away. Also force is_local_mode() to False so
    # the local-embedder fallback (GH #336) doesn't fire — this test
    # exercises the cloud-path staleness check, which expects to
    # reach the staleness comparison without a fallback diversion.
    monkeypatch.setattr("nexus.doc_indexer._has_credentials", lambda: True)
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)

    captured_source_paths: list[str] = []

    def fake_chroma_retry(fn, **kwargs):
        if "where" in kwargs:
            sp = kwargs["where"].get("source_path", "")
            captured_source_paths.append(sp)
        # Return matching hash+model so staleness check says "skip"
        return {
            "metadatas": [{"content_hash": real_hash, "embedding_model": "model"}],
            "ids": ["x"],
        }

    # col.get is passed to _chroma_with_retry as the fn argument
    mock_col = type("FakeCol", (), {"get": lambda self, **kw: None})()
    mock_db = type("FakeDB", (), {
        "get_or_create_collection": lambda self, name: mock_col,
    })()

    monkeypatch.setattr("nexus.doc_indexer.make_t3", lambda: mock_db)
    monkeypatch.setattr("nexus.doc_indexer.index_model_for_collection", lambda n: "model")
    monkeypatch.setattr("nexus.doc_indexer._chroma_with_retry", fake_chroma_retry)

    result = index_pdf(pdf, "test")

    # Staleness check skipped (returned 0) and used the resolved absolute path
    assert result == 0
    assert captured_source_paths
    assert Path(captured_source_paths[0]).is_absolute()
    assert captured_source_paths[0] == str(pdf.resolve())


# ── backward compat: indexer._should_ignore ─────────────────────────────────

def test_indexer_should_ignore_reexport() -> None:
    """indexer._should_ignore is the same function as indexer_utils.should_ignore."""
    from nexus.indexer import _should_ignore
    assert _should_ignore is should_ignore


def test_indexer_default_ignore_matches() -> None:
    """indexer.DEFAULT_IGNORE matches indexer_utils._DEFAULT_IGNORE."""
    from nexus.indexer import DEFAULT_IGNORE
    from nexus.indexer_utils import _DEFAULT_IGNORE
    assert DEFAULT_IGNORE is _DEFAULT_IGNORE
