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

import re
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


def _register_every_file(**kw):
    """Stand-in for ``_catalog_hook``: a document id for every file offered."""
    return {p: f"1.1.{i}" for i, (p, _t, _c) in enumerate(kw["indexed_files"], 1)}


@contextmanager
def _service_mode_patches(db, *, extra=None):
    patches = {
        "nexus.frecency.batch_frecency": {"return_value": {}},
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
        "nexus.catalog.factory.make_catalog_reader": {"return_value": None},
        "nexus.catalog.factory.make_catalog_writer": {"return_value": None},
        # nexus-z0o2p.20: the run refuses a file with no catalog document
        # before chunking it, so a test that stubs the per-file indexers models
        # a catalog that registered every file.
        "nexus.indexer._catalog_hook": {"side_effect": _register_every_file},
        # nexus-bd44g fix check: _run_index's pre-staleness-sweep
        # registration loop now calls ensure_collection_registered before
        # any per-file write. That seam reads the engine's embedding
        # profile via make_catalog_reader() (stubbed to None above for
        # this journey), so left unpatched it raises
        # CatalogReaderUnavailableError on every test in this file --
        # a boundary these tests never previously reached (make_t3
        # already stubs every write wholesale). No-op stub, same
        # fidelity as make_catalog_reader/make_catalog_writer above.
        "nexus.corpus.ensure_collection_registered": {},
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

    def __init__(self, *, flush, failed=None, throttled=None, retry_after=None, breaker_open=False, **_kw):
        self._flush = flush
        self._failed = dict(failed or {})
        # nexus-eoido: the throttled subset is part of failed_files, as on the real batcher
        self._throttled = dict(throttled or {})
        self._failed.update(self._throttled)
        self._retry_after = retry_after
        self._breaker_open = breaker_open

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
    def throttled_files(self) -> dict:
        return dict(self._throttled)

    @property
    def throttle_retry_after(self):
        return self._retry_after

    @property
    def throttle_breaker_open(self) -> bool:
        return self._breaker_open

    @property
    def stats(self) -> dict:
        return {"flushes": 0.0, "flush_seconds": 0.0, "upload_seconds": 0.0}


def _run_index_with_fake_batcher(tmp_path, monkeypatch, *, failed_files, **batcher_kw):
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
        return _FakeBatcherWithFailures(flush=flush, failed=failed_files, **batcher_kw, **kw)

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


def test_run_index_counts_throttled_files_apart_from_rejected_ones(tmp_path, monkeypatch):
    """nexus-eoido: a throttled file is not a poisoned one. chunk_flush_failed_files counts only the
    rejected files; the throttled ones get their own count, paths and Retry-After."""
    stats = _run_index_with_fake_batcher(
        tmp_path, monkeypatch,
        failed_files={"bad.py": "400 from engine"},
        throttled={"t2.py": "429", "t1.py": "429"},
        retry_after=17.0, breaker_open=True,
    )
    assert stats["chunk_flush_failed_files"] == 1
    assert stats["chunk_flush_throttled_files"] == 2
    assert stats["chunk_flush_throttled_paths"] == ["t1.py", "t2.py"]
    assert stats["chunk_flush_throttle_retry_after"] == 17.0
    assert stats["chunk_flush_throttle_breaker_open"] is True


def test_run_index_with_no_throttle_reports_zero_throttled_files(tmp_path, monkeypatch):
    stats = _run_index_with_fake_batcher(tmp_path, monkeypatch, failed_files={"bad.py": "boom"})
    assert stats["chunk_flush_throttled_files"] == 0
    assert stats["chunk_flush_throttled_paths"] == []
    assert stats["chunk_flush_throttle_retry_after"] is None
    assert stats["chunk_flush_throttle_breaker_open"] is False


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


def test_index_repo_deferred_files_are_named_and_fail_the_run(runner, repo_dir, mock_reg):
    """RDR-223 P2.4 review (nexus-z0o2p.14): a file deferred on a transient
    write error wrote nothing this run. The summary must print the count and
    the paths with the remedy, and the command must exit non-zero, as for
    chunk_flush_failed_files; before, it was one structlog WARNING and rc=0."""
    result, _ = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={
            "files_changed": 3, "transient_upsert_deferred_files": 2,
            "transient_upsert_deferred_paths": ["src/big.py", "docs/huge.md"],
        },
    )
    assert result.exit_code != 0, result.output
    assert re.search(r"2/\d+ file\(s\) deferred on a transient write error", result.stdout), result.stdout
    assert "src/big.py" in result.stdout and "docs/huge.md" in result.stdout, result.stdout
    assert "Re-run 'nx index repo' to retry" in result.stdout, result.stdout
    assert "Done." in result.output          # the rest of the run still completed


def test_index_repo_many_deferred_files_are_truncated_to_ten_paths(runner, repo_dir, mock_reg):
    paths = [f"f{i}.py" for i in range(13)]
    result, _ = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={"transient_upsert_deferred_files": 13,
                      "transient_upsert_deferred_paths": paths},
    )
    assert result.exit_code != 0
    assert re.search(r"13/\d+ file\(s\) deferred on a transient write error", result.stdout), result.stdout
    assert "f9.py" in result.stdout and "f10.py" not in result.stdout, result.stdout
    assert "and 3 more" in result.stdout, result.stdout


def test_index_repo_deferred_and_chunk_flush_failures_both_print(runner, repo_dir, mock_reg):
    result, _ = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={"chunk_flush_failed_files": 1, "transient_upsert_deferred_files": 1,
                      "transient_upsert_deferred_paths": ["a.py"]},
    )
    assert result.exit_code != 0
    assert re.search(r"1/\d+ file\(s\) deferred on a transient write error", result.stdout), result.stdout
    assert re.search(r"1/\d+ file\(s\) failed to flush chunk uploads", result.stdout), result.stdout


def test_index_repo_throttled_files_are_named_with_retry_after_and_fail_the_run(runner, repo_dir, mock_reg):
    """nexus-eoido: files the service throttled are reported AS throttled (not as a failed flush),
    with their paths and the Retry-After, and the run still exits non-zero."""
    result, _ = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={
            "files_changed": 3, "chunk_flush_throttled_files": 2,
            "chunk_flush_throttled_paths": ["src/big.py", "docs/huge.md"],
            "chunk_flush_throttle_retry_after": 30.0,
        },
    )
    assert result.exit_code != 0, result.output
    assert re.search(r"2/\d+ file\(s\) throttled by the service", result.stdout), result.stdout
    assert "src/big.py" in result.stdout and "docs/huge.md" in result.stdout, result.stdout
    assert "Retry-After" in result.stdout and "30s" in result.stdout, result.stdout
    assert "failed to flush chunk uploads" not in result.stdout, result.stdout   # not counted as poisoned
    assert "throttled" in result.output.split("Error:")[-1], result.output
    assert "Done." in result.output


def test_index_repo_throttled_paths_are_truncated_to_ten(runner, repo_dir, mock_reg):
    paths = [f"f{i}.py" for i in range(13)]
    result, _ = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={"chunk_flush_throttled_files": 13, "chunk_flush_throttled_paths": paths},
    )
    assert result.exit_code != 0
    assert "f9.py" in result.stdout and "f10.py" not in result.stdout, result.stdout
    assert "and 3 more" in result.stdout, result.stdout


def test_index_repo_open_throttle_breaker_is_the_one_clear_error_message(runner, repo_dir, mock_reg):
    """nexus-eoido: when the breaker cut the run short the exit message names the throttle and the
    breaker, ahead of a per-file failure that also happened this run."""
    result, _ = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={
            "chunk_flush_failed_files": 1,
            "chunk_flush_throttled_files": 40, "chunk_flush_throttled_paths": ["a.py"],
            "chunk_flush_throttle_retry_after": 300.0, "chunk_flush_throttle_breaker_open": True,
        },
    )
    assert result.exit_code != 0, result.output
    assert result.output.count("Error:") == 1, result.output
    error_line = next(line for line in result.output.splitlines() if line.startswith("Error:"))
    assert "throttled" in error_line and "consecutive throttled flushes" in error_line, error_line
    assert "300s" in error_line, error_line
    assert re.search(r"1/\d+ file\(s\) failed to flush chunk uploads", result.stdout), result.stdout


def test_index_repo_no_throttled_files_exit_zero(runner, repo_dir, mock_reg):
    result, _ = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={"files_changed": 3, "chunk_flush_throttled_files": 0,
                      "chunk_flush_throttled_paths": []},
    )
    assert result.exit_code == 0, result.output
    assert "throttled by the service" not in result.stdout


_DEFERRED = {"transient_upsert_deferred_files": 2, "transient_upsert_deferred_paths": ["a.py", "b.md"]}
_DEFERRED_LINE = r"2/\d+ file\(s\) deferred on a transient write error"


def test_index_repo_deferral_warning_prints_with_a_taxonomy_failure(runner, repo_dir, mock_reg):
    """A run with deferred files AND lost taxonomy assignments prints both warning lines; the
    deferral raise used to sit before the taxonomy block, so only the deferral printed."""
    result, _ = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={**_DEFERRED, "taxonomy_assign_batches_attempted": 5,
                      "taxonomy_assign_batches_failed": 2, "taxonomy_assign_chunks_failed": 40},
    )
    assert result.exit_code != 0, result.output
    assert re.search(_DEFERRED_LINE, result.stdout), result.stdout
    assert "2/5 taxonomy-assign batch(es) failed" in result.stdout, result.stdout
    assert result.output.count("Error:") == 1, result.output        # one non-zero exit, one message
    error_line = next(line for line in result.output.splitlines() if line.startswith("Error:"))
    assert "deferred on a transient write error" in error_line, error_line   # deferral outranks taxonomy
    assert "(nexus-7lw6a)" not in error_line, error_line


def test_index_repo_deferral_warning_prints_before_an_earlier_failure_raises(runner, repo_dir, mock_reg):
    """The quality-gate and systemic-extraction raises sit above the deferral block; the deferral
    warning line still reaches the operator, who would otherwise re-run blind."""
    for extra, marker in (
        ({"pdf_quality_gate_failed": 1}, "failed the post-extraction quality gate"),
        ({"systemic_extraction_failure": True, "skipped_unextractable_files": 4,
          "files_attempted_total": 5}, "extraction may be broken"),
    ):
        result, _ = _invoke_repo(
            runner, [str(repo_dir)], mock_reg, index_return={**_DEFERRED, **extra})
        assert result.exit_code != 0, result.output
        assert re.search(_DEFERRED_LINE, result.stdout), (extra, result.stdout)
        assert marker in result.output, (extra, result.output)        # the earlier failure still names itself
        assert result.output.count("Error:") == 1, result.output


def test_index_repo_no_deferred_files_exit_zero(runner, repo_dir, mock_reg):
    result, _ = _invoke_repo(
        runner, [str(repo_dir)], mock_reg,
        index_return={"files_changed": 3, "transient_upsert_deferred_files": 0,
                      "transient_upsert_deferred_paths": []},
    )
    assert result.exit_code == 0, result.output
    assert "file(s) deferred" not in result.stdout


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


def test_run_index_reports_transient_upsert_deferred_files_in_stats(tmp_path, monkeypatch):
    """nexus-6m9zy.6 producer side (critique of 891e28aed: the gate tests
    mock ``_run_index`` wholesale, so nothing proved the counter increments,
    resets per run, or lands under its stats key). A per-file indexer that
    times out on upsert is deferred by ``_contain_transient_upsert``; the
    real ``_run_index`` must report it, and a second clean run must report
    zero, not carry the first run's count."""
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.indexer import _run_index
    from nexus.retry import VectorUpsertTimeoutError

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("x = 1\n")
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setenv("CHROMA_API_KEY", "fake")
    db = MagicMock(spec=HttpVectorClient)

    def _batcher_factory(*, flush, **kw):
        return _FakeBatcherWithFailures(flush=flush, failed={}, **kw)

    timeout = {"side_effect": VectorUpsertTimeoutError("upsert timed out")}
    with _service_mode_patches(db, extra={"nexus.indexer._index_code_file": timeout}), patch(
        "nexus.chunk_batcher.ChunkBatcher", _batcher_factory,
    ):
        stats = _run_index(repo, _reg())
    assert stats["transient_upsert_deferred_files"] == 1
    assert [Path(p).name for p in stats["transient_upsert_deferred_paths"]] == ["hello.py"]

    with _service_mode_patches(db), patch("nexus.chunk_batcher.ChunkBatcher", _batcher_factory):
        stats = _run_index(repo, _reg())
    assert stats["transient_upsert_deferred_files"] == 0
    assert stats["transient_upsert_deferred_paths"] == []
