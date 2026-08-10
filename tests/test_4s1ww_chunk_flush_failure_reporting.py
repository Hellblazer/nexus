# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4s1ww / GH #1432: ``nx index repo`` printed "Done." and exited 0
when EVERY chunk-flush batch failed and zero chunks landed. The structured
log recorded ``chunk_batch_flush_failed`` events and bisect retries, but
nothing reached stdout or the exit code.

Two layers under test:

1. ``_run_index`` (indexer.py) must propagate ``ChunkBatcher.failed_files``
   -- the count that SURVIVED bisect-retry settlement (chunk_batcher.py's
   own docstring: a batch that fails is bisected by file, and only a
   genuinely poisoned file that fails even as a singleton batch is
   recorded in ``failed_files``) -- into the returned stats dict as
   ``chunk_flush_failed_files``. This is deliberately NOT a raw
   flush-attempt or retry count.
2. ``nx index repo`` (commands/index.py) must turn a non-zero
   ``chunk_flush_failed_files`` into (a) a non-zero exit code and (b) a
   plain STDOUT warning naming the count -- ``click.echo()``, never
   ``print()``, never structlog for the user-facing line -- while leaving
   the existing "Done." success-path output untouched (other gates/
   scripts parse it).

Design decision (stated explicitly, not guessed at silently): ANY
unrecovered flush failure -- not just a total (all-files) one -- drives
the non-zero exit. A single file's chunks permanently missing after
bisect-retry is real, permanent data loss for that file; it is the same
severity class as the existing ``pdf_quality_gate_failed`` precedent
(commands/index.py), which already fails the whole run on ANY count > 0,
not just "every PDF failed." Partial failure is exercised explicitly
below (``test_index_repo_partial_chunk_flush_failure_exit_nonzero`` /
``test_run_index_reports_chunk_flush_failed_files_in_stats``) precisely
so this choice is falsifiable, not just asserted in prose.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main

# ── indexer._run_index wiring ────────────────────────────────────────────────
# Mirrors tests/test_indexer_seam_b_cutover.py's service-mode fixture shape
# (kept local/duplicated rather than cross-imported from that test module --
# tests/*.py files are not meant to be import targets for one another).

_DEFAULT_CONFIG = {
    "server": {"ignorePatterns": []},
    "indexing": {
        "code_extensions": [],
        "prose_extensions": [],
        "rdr_paths": ["docs/rdr"],
        "include_untracked": False,
    },
}


def _reg(override=None):
    base = {
        "collection": "code__repo",
        "code_collection": "code__repo__voyage-code-3__v1",
        "docs_collection": "docs__repo__voyage-context-3__v1",
    }
    m = MagicMock()
    m.get.return_value = {**base, **(override or {})}
    return m


def _mock_db():
    col = MagicMock()
    col.get.return_value = {"metadatas": [], "ids": []}
    db = MagicMock()
    db.get_or_create_collection.return_value = col
    db.get_collection.return_value = col
    return db, col


@contextmanager
def _service_mode_patches(db, *, extra=None):
    patches = {
        "nexus.frecency.batch_frecency": {"return_value": {}},
        "nexus.ripgrep_cache.build_cache": {},
        "nexus.indexer._git_metadata": {"return_value": {}},
        "nexus.config.load_config": {"return_value": _DEFAULT_CONFIG},
        "nexus.config.get_credential": {"return_value": "fake-key"},
        "nexus.mcp_infra.get_t3": {"return_value": db},
        "nexus.db.make_t3": {"return_value": db},
        "nexus.indexer._index_code_file": {"return_value": 0},
        "nexus.indexer._index_prose_file": {"return_value": 0},
        "nexus.indexer._index_pdf_file": {"return_value": 0},
        "nexus.indexer._prune_misclassified": {},
        "nexus.indexer._prune_deleted_files": {},
        "nexus.indexer._migrate_legacy_collections": {"return_value": {}},
        "nexus.indexer.stamp_collection_version": {},
        "nexus.catalog.factory.make_catalog_reader": {"return_value": None},
        "nexus.catalog.factory.make_catalog_writer": {"return_value": None},
    }
    if extra:
        patches.update(extra)

    mocks, stack = {}, []
    for target, kw in patches.items():
        p = patch(target, **kw)
        m = p.start()
        stack.append(p)
        mocks[target.split(".")[-1]] = m
    try:
        yield mocks
    finally:
        for p in reversed(stack):
            p.stop()


class _FakeBatcherWithFailures:
    """Stand-in ChunkBatcher whose ``failed_files`` is fixed at
    construction time -- isolates "does _run_index propagate this count"
    from "does ChunkBatcher compute it correctly" (the latter is
    tests/test_chunk_batcher.py's job)."""

    def __init__(self, *, flush, failed=None, **_kw):
        self._flush = flush
        self._failed = dict(failed or {})

    def add(self, *_a, **_kw):
        return False  # never staged -- file-level indexers are stubbed anyway

    def drain(self, on_progress=None) -> int:
        return 0

    @property
    def pending_summary(self) -> dict:
        return {"chunks": 0, "collections": 0, "in_flight": 0}

    @property
    def failed_files(self) -> dict:
        return dict(self._failed)

    @property
    def stats(self) -> dict:
        return {"flushes": 0.0, "flush_seconds": 0.0, "upload_seconds": 0.0}


def _run_index_with_fake_batcher(tmp_path, monkeypatch, *, failed_files):
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("x = 1\n")
    reg = _reg()

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setenv("CHROMA_API_KEY", "fake")

    db = MagicMock(spec=HttpVectorClient)

    def _batcher_factory(*, flush, **kw):
        return _FakeBatcherWithFailures(flush=flush, failed=failed_files, **kw)

    with _service_mode_patches(db), patch(
        "nexus.chunk_batcher.ChunkBatcher", _batcher_factory,
    ):
        return _run_index(repo, reg)


def test_run_index_reports_chunk_flush_failed_files_in_stats(tmp_path, monkeypatch):
    """Total-failure shape from the GH #1432 transcript, scaled down: every
    staged file's flush fails (post-bisect) -- the stats dict must carry
    the survivor count under ``chunk_flush_failed_files``."""
    stats = _run_index_with_fake_batcher(
        tmp_path, monkeypatch,
        failed_files={"a.py": "boom", "b.py": "boom"},
    )
    assert stats["chunk_flush_failed_files"] == 2


def test_run_index_partial_chunk_flush_failure_reported(tmp_path, monkeypatch):
    """Partial failure: 1 file's chunks permanently lost, not 89 -- the
    count must reflect exactly that, not be coerced to all-or-nothing."""
    stats = _run_index_with_fake_batcher(
        tmp_path, monkeypatch,
        failed_files={"only_one.py": "boom"},
    )
    assert stats["chunk_flush_failed_files"] == 1


def test_run_index_zero_chunk_flush_failures_reports_zero(tmp_path, monkeypatch):
    stats = _run_index_with_fake_batcher(tmp_path, monkeypatch, failed_files={})
    assert stats["chunk_flush_failed_files"] == 0


def test_run_index_batcher_none_reports_zero_chunk_flush_failures(tmp_path, monkeypatch):
    """When ``db`` is not an HttpVectorClient, ChunkBatcher is never
    constructed (``_batcher`` stays None, legacy per-file path) -- the
    stats key must still be present and zero, not absent or crashing."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("x = 1\n")
    reg = _reg()

    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setenv("CHROMA_API_KEY", "fake")

    db, _ = _mock_db()  # plain MagicMock, NOT spec'd to HttpVectorClient
    with _service_mode_patches(db):
        stats = _run_index(repo, reg)

    assert stats["chunk_flush_failed_files"] == 0


# ── nx index repo CLI wiring ─────────────────────────────────────────────────
# Mirrors tests/test_index_cmd.py's pdf_quality_gate_failed pattern exactly
# (that key is the accepted precedent for "a per-file containment count
# drives a non-zero CLI exit after the rest of the run completes").


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def repo_dir(home: Path) -> Path:
    d = home / "myrepo"
    d.mkdir()
    (d / ".git").mkdir()
    return d


@pytest.fixture
def mock_reg():
    reg = MagicMock()
    reg.get.return_value = {"collection": "code__myrepo"}
    return reg


def _invoke_repo(runner, args, mock_reg, index_return=None):
    with patch("nexus.commands.index._registry", return_value=mock_reg):
        with patch(
            "nexus.indexer.index_repository", return_value=index_return or {},
        ) as mock_idx:
            result = runner.invoke(main, ["index", "repo"] + args)
    return result, mock_idx


def test_index_repo_chunk_flush_failures_exit_nonzero(runner, repo_dir, mock_reg):
    """The core GH #1432 assertion: every file's chunk flush failed (0
    chunks landed) -- the CLI must exit non-zero AND print a plain-STDOUT
    warning naming the count. Both channels a human or a script would
    check must say so."""
    result, mock_idx = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={"files_changed": 0, "chunk_flush_failed_files": 64},
    )
    assert result.exit_code != 0, result.output
    assert "64" in result.stdout, result.stdout
    assert "flush" in result.stdout.lower(), result.stdout
    # "Done." (the rest of the run, incl. post-processing) still prints --
    # the exception fires LAST, matching the pdf_quality_gate_failed
    # precedent, so the existing success-path output contract other
    # gates/scripts parse stays intact.
    assert "Done." in result.output


def test_index_repo_partial_chunk_flush_failure_exit_nonzero(runner, repo_dir, mock_reg):
    """Partial failure (1 of many files) is still a non-zero exit -- see
    the module docstring's stated partial-vs-total decision."""
    result, mock_idx = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={"files_changed": 88, "chunk_flush_failed_files": 1},
    )
    assert result.exit_code != 0, result.output
    assert "1" in result.stdout, result.stdout


def test_index_repo_no_chunk_flush_failures_exit_zero(runner, repo_dir, mock_reg):
    """Regression: the new stats key must not itself flip a clean run
    non-zero."""
    result, mock_idx = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={"files_changed": 3, "chunk_flush_failed_files": 0},
    )
    assert result.exit_code == 0, result.output


def test_index_repo_chunk_flush_failed_key_absent_exit_zero(runner, repo_dir, mock_reg):
    """Backward compat: an older/mocked index_repository return dict with
    no ``chunk_flush_failed_files`` key at all must not be treated as a
    failure (``.get`` default of 0)."""
    result, mock_idx = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={"files_changed": 3},
    )
    assert result.exit_code == 0, result.output


# ── --monitor must not go silent during the flush drain (GH #1432 item 3) ──
# The drain-phase markers already existed (nexus-uizok, 2026-07-08) for
# progress; these tests are specifically about FAILURE visibility, which
# nexus-uizok's markers never carried.


class _StubBatcherWithFailures:
    """Local double mirroring tests/test_indexer.py's ``_StubBatcher`` shape
    (kept independent — see the module docstring on cross-test-file
    imports), plus a settable ``failed_files`` the real ChunkBatcher
    carries but the plain stub does not."""

    def __init__(self, pend, flushes=2, failed=None):
        self._pend = pend
        self._flushes = flushes
        self._failed = dict(failed or {})

    @property
    def pending_summary(self):
        return self._pend

    @property
    def failed_files(self):
        return dict(self._failed)

    def drain(self, on_progress=None):
        for i in range(self._flushes):
            if on_progress is not None:
                on_progress(i + 1, self._flushes)
        return self._flushes


def test_drain_markers_heartbeat_reports_failures_as_they_happen():
    """A flush failure mid-drain must show up on the NEXT heartbeat line,
    not only in the final summary -- the operator watching --monitor
    output sees it as it happens, not only after the drain finishes."""
    from nexus.indexer import _drain_batcher_with_markers

    phases: list[str] = []
    b = _StubBatcherWithFailures(
        {"chunks": 10, "collections": 1, "in_flight": 0}, flushes=2,
        failed={"broken.py": "boom"},
    )
    _drain_batcher_with_markers(b, phases.append)
    heartbeats = [p for p in phases if p.startswith("  flush ")]
    assert heartbeats, phases
    assert all("1 file(s) failed" in p for p in heartbeats), phases


def test_drain_markers_close_marker_names_failure_count():
    from nexus.indexer import _drain_batcher_with_markers

    phases: list[str] = []
    b = _StubBatcherWithFailures(
        {"chunks": 10, "collections": 1, "in_flight": 0}, flushes=2,
        failed={"a.py": "boom", "b.py": "boom"},
    )
    _drain_batcher_with_markers(b, phases.append)
    close = phases[-1]
    assert close.startswith("Flush drain complete — 2 flushes,")
    assert "2 file(s) failed to flush" in close, close


def test_drain_markers_no_failures_omits_failure_text():
    """Regression: a clean drain must not grow spurious '0 file(s) failed'
    noise -- the suffix is silent when there is nothing to report,
    matching the existing quiet-on-success convention of this function."""
    from nexus.indexer import _drain_batcher_with_markers

    phases: list[str] = []
    b = _StubBatcherWithFailures(
        {"chunks": 10, "collections": 1, "in_flight": 0}, flushes=1, failed={},
    )
    _drain_batcher_with_markers(b, phases.append)
    assert not any("failed" in p for p in phases), phases
