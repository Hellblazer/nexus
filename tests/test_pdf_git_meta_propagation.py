# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests: git provenance flows into PDF + markdown ingest paths.

Pre-PR-#164: the single-file CLI (``nx index pdf <file>``) never wrote
git_* metadata because the augment-with-git step lived only in the
repo-walk path (``indexer.py:_pdf_augment``).

This test pins the fix: ``_pdf_chunks`` and ``_markdown_chunks``
auto-detect git metadata via :func:`nexus.indexer_utils.detect_git_metadata`
and emit flat ``git_*`` keys, which ``normalize()`` then packs into a
``git_meta`` JSON string.

nexus-2my fix #3.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────────


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture()
def repo_with_pdf(tmp_path: Path) -> tuple[Path, Path]:
    """A tmpdir initialised as a git repo with one PDF + one commit."""
    pymupdf = pytest.importorskip("pymupdf")

    _git("init", "-b", "main", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    _git("remote", "add", "origin",
         "https://example.com/test-repo.git", cwd=tmp_path)

    pdf_path = tmp_path / "sample.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), "Hello world. " * 50)
    doc.save(str(pdf_path))
    doc.close()

    _git("add", "sample.pdf", cwd=tmp_path)
    _git("commit", "-m", "initial", cwd=tmp_path)
    return tmp_path, pdf_path


@pytest.fixture()
def repo_with_markdown(tmp_path: Path) -> tuple[Path, Path]:
    _git("init", "-b", "main", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    _git("remote", "add", "origin",
         "https://example.com/test-repo.git", cwd=tmp_path)

    md_path = tmp_path / "doc.md"
    md_path.write_text(
        "# Title\n\nFirst section text.\n\n"
        "## Section\n\nMore content here.\n",
    )
    _git("add", "doc.md", cwd=tmp_path)
    _git("commit", "-m", "initial", cwd=tmp_path)
    return tmp_path, md_path


# ── detect_git_metadata helper ──────────────────────────────────────────────


def test_detect_git_metadata_returns_full_set(repo_with_pdf) -> None:
    from nexus.indexer_utils import detect_git_metadata

    repo, pdf = repo_with_pdf
    meta = detect_git_metadata(pdf)
    assert meta["git_project_name"] == repo.name
    assert meta["git_branch"] == "main"
    assert len(meta["git_commit_hash"]) == 40  # full SHA
    assert meta["git_remote_url"] == "https://example.com/test-repo.git"


def test_detect_git_metadata_returns_empty_outside_repo(tmp_path: Path) -> None:
    """Path not in a git repo → empty dict (caller can ``**``-merge safely)."""
    from nexus.indexer_utils import detect_git_metadata

    assert detect_git_metadata(tmp_path) == {}


# ── _pdf_chunks propagation ─────────────────────────────────────────────────


def test_pdf_chunks_emits_flat_git_keys(repo_with_pdf) -> None:
    """``_pdf_chunks`` writes git_* keys auto-detected from the PDF path."""
    from nexus.doc_indexer import _pdf_chunks

    repo, pdf = repo_with_pdf
    chunks = _pdf_chunks(
        pdf, "test-hash", "voyage-context-3", "2026-04-15Z", "test_corpus",
    )
    assert chunks
    meta = chunks[0][2]
    assert meta.get("git_project_name") == repo.name
    assert meta.get("git_branch") == "main"
    assert meta.get("git_remote_url") == "https://example.com/test-repo.git"
    assert len(meta.get("git_commit_hash", "")) == 40


def test_pdf_chunks_normalize_packs_git_meta(repo_with_pdf) -> None:
    """End-to-end: _pdf_chunks → normalize → git_meta JSON populated."""
    from nexus.doc_indexer import _pdf_chunks
    from nexus.metadata_schema import normalize

    repo, pdf = repo_with_pdf
    chunks = _pdf_chunks(
        pdf, "test-hash", "voyage-context-3", "2026-04-15Z", "test_corpus",
    )
    meta = chunks[0][2]
    canonical = normalize(meta, content_type="pdf")
    assert "git_meta" in canonical
    decoded = json.loads(canonical["git_meta"])
    assert decoded["project"] == repo.name
    assert decoded["branch"] == "main"
    assert decoded["remote"] == "https://example.com/test-repo.git"


def test_pdf_chunks_outside_git_repo_no_git_keys(tmp_path: Path) -> None:
    """A PDF outside any git repo gets no git_* keys (and no git_meta
    after normalize, mirroring the omitted-when-empty pattern)."""
    pymupdf = pytest.importorskip("pymupdf")
    from nexus.doc_indexer import _pdf_chunks
    from nexus.metadata_schema import normalize

    pdf_path = tmp_path / "loose.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), "x" * 200)
    doc.save(str(pdf_path))
    doc.close()

    chunks = _pdf_chunks(
        pdf_path, "h", "voyage-context-3", "Z", "test",
    )
    meta = chunks[0][2]
    assert "git_project_name" not in meta
    assert normalize(meta, content_type="pdf").get("git_meta") in (None,)


def test_pdf_chunks_explicit_git_meta_override(repo_with_pdf) -> None:
    """Caller may pass ``git_meta=`` to override auto-detection
    (useful for repo-walk paths that compute it once)."""
    from nexus.doc_indexer import _pdf_chunks

    _, pdf = repo_with_pdf
    chunks = _pdf_chunks(
        pdf, "h", "voyage-context-3", "Z", "test",
        git_meta={
            "git_project_name": "override-name",
            "git_branch": "feature",
            "git_commit_hash": "z" * 40,
            "git_remote_url": "https://override.example/r.git",
        },
    )
    meta = chunks[0][2]
    assert meta["git_project_name"] == "override-name"
    assert meta["git_branch"] == "feature"


# ── _markdown_chunks propagation ────────────────────────────────────────────


def test_markdown_chunks_emits_flat_git_keys(repo_with_markdown) -> None:
    from nexus.doc_indexer import _markdown_chunks

    repo, md = repo_with_markdown
    chunks = _markdown_chunks(
        md, "h", "voyage-context-3", "Z", "test_corpus",
    )
    assert chunks
    meta = chunks[0][2]
    assert meta.get("git_project_name") == repo.name
    assert meta.get("git_branch") == "main"
    assert meta.get("git_remote_url") == "https://example.com/test-repo.git"


def test_markdown_chunks_normalize_packs_git_meta(repo_with_markdown) -> None:
    from nexus.doc_indexer import _markdown_chunks
    from nexus.metadata_schema import normalize

    repo, md = repo_with_markdown
    chunks = _markdown_chunks(md, "h", "voyage-context-3", "Z", "test")
    canonical = normalize(chunks[0][2], content_type="markdown")
    assert "git_meta" in canonical
    decoded = json.loads(canonical["git_meta"])
    assert decoded["project"] == repo.name
