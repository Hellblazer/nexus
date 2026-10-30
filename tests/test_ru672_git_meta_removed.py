# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-ru672: the indexer's git_meta plumbing is gone.

RDR-236 Phase 0 removed the repo-PDF ``git_*`` writer, which left
``_git_metadata(repo)`` running git subprocesses on every index run for a
value no reader consumed. The owner's ``head_hash`` (RDR-101 5c) is a
separate path (``_current_head``) and stays. The subprocess-count pin for a
code run is in
``tests/test_index_since_head.py::test_since_head_without_base_logs_fallback``
(it needs a tenant, and a new one would raise the tenant count). The PDF and
markdown entry points kept a dead ``git_meta=`` kwarg that ran
``detect_git_metadata`` (``find_repo_root`` plus three git spawns) per file and
discarded the result; the pins below cover them without a tenant.
"""
from __future__ import annotations

import dataclasses
import inspect
import shutil
import subprocess
from pathlib import Path

import pytest

from nexus import bounded_subprocess, doc_indexer, index_context, indexer_utils, pipeline_stages
from nexus import indexer as idx
from nexus.indexer_utils import find_repo_root
from tests._module_seam import setattr_in


def test_index_context_has_no_git_meta_field():
    names = {f.name for f in dataclasses.fields(index_context.IndexContext)}
    assert "git_meta" not in names


def test_git_metadata_function_is_gone():
    assert not hasattr(idx, "_git_metadata")


@pytest.fixture
def repo_with_files(tmp_path: Path, simple_pdf: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    shutil.copy2(simple_pdf, repo / "docs" / "simple.pdf")
    (repo / "docs" / "note.md").write_text("# Note\n\nSome body text for the chunker.\n")
    for args in (
        ["init", "-b", "main"],
        ["-c", "user.email=t@nexus", "-c", "user.name=T", "add", "."],
        ["-c", "user.email=t@nexus", "-c", "user.name=T", "commit", "-m", "fixture"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def git_spawns(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    spawned: list[tuple[str, ...]] = []
    real_popen = bounded_subprocess.subprocess.Popen

    def recording_popen(args, *a, **k):
        if isinstance(args, (list, tuple)) and args and args[0] == "git":
            spawned.append(tuple(str(x) for x in args[1:]))
        return real_popen(args, *a, **k)

    # Local to bounded_subprocess's own ``subprocess`` name, never the stdlib module.
    setattr_in(monkeypatch, bounded_subprocess, "subprocess.Popen", recording_popen)
    return spawned


def test_the_recorder_sees_a_git_spawn(repo_with_files, git_spawns):
    """Non-vacuity: the fixture is a real repo and the recorder catches a git spawn."""
    assert (repo_with_files / ".git").exists()
    assert find_repo_root(repo_with_files / "docs" / "simple.pdf") is not None
    assert ("rev-parse", "--show-toplevel") in git_spawns


def test_chunking_a_repo_pdf_spawns_no_git(repo_with_files, git_spawns):
    assert (repo_with_files / ".git").exists()
    rows = doc_indexer._pdf_chunks(
        repo_with_files / "docs" / "simple.pdf", content_hash="x" * 64,
        target_model="model-ctx", now_iso="2026-01-01T00:00:00+00:00", corpus="docs__t",
    )
    assert rows, "vacuous: the PDF produced no chunks"
    assert git_spawns == []


def test_chunking_a_repo_markdown_file_spawns_no_git(repo_with_files, git_spawns):
    assert (repo_with_files / ".git").exists()
    rows = doc_indexer._markdown_chunks(
        repo_with_files / "docs" / "note.md", "abc123", "model-ctx",
        "2026-01-01T00:00:00+00:00", "docs__t",
    )
    assert rows, "vacuous: the markdown file produced no chunks"
    assert git_spawns == []


def test_no_git_meta_parameter_survives():
    """The dead kwarg is gone from every function that threaded it."""
    for fn in (
        doc_indexer._pdf_chunks, doc_indexer._markdown_chunks,
        pipeline_stages._build_chunk_metadata, pipeline_stages._embed_and_write_batch,
        pipeline_stages.chunker_loop, pipeline_stages.pipeline_index_pdf,
    ):
        assert "git_meta" not in inspect.signature(fn).parameters, fn.__qualname__
    assert not hasattr(indexer_utils, "detect_git_metadata")
