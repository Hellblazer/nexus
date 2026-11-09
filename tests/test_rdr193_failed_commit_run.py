# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 6 (nexus-u5m5g.7), fix round r1: a failed reconcile commit fails the RUN.

The RDR's line is "any other failed: surface the error and exit non-zero". The hook swallows the error (it is a
best-effort path for the callers that cannot raise), so the run must read the failure from the hook and act on it:

* the run's stats carry ``catalog_hook_failed`` (also for a delta run whose only rows are deletions, where no file is
  named unregistered and the identity-less drop that fails an ordinary run never fires);
* ``index_repository`` does not advance ``owners.head_hash`` over it, or the next ``--since-head`` diff never offers
  the deletions again;
* ``nx index repo`` exits non-zero over it (``tests/integration/test_rdr193_failed_commit_cli.py``: a CLI run that reaches
  the faked indexer ends in storage touches, so it takes a tenant, and the python_unit leg does not count one there);
* no chunk of the run is written under a document the catalog never committed (vector client spied: zero calls).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import structlog.testing

from nexus.catalog.http_catalog_client import CatalogReconcileError
from nexus.indexer import _catalog_hook, _run_index, index_repository
from nexus.mcp_infra import reset_ephemeral_registration_skips, reset_manifest_identity_drops
from tests.test_indexer_seam_b_cutover import _reg, _service_mode_patches
from tests.test_z0o2p20_identity_less_files import _CapturingBatcher, _http_db


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in [
        ("GIT_AUTHOR_NAME", "Test"), ("GIT_AUTHOR_EMAIL", "test@test.invalid"),
        ("GIT_COMMITTER_NAME", "Test"), ("GIT_COMMITTER_EMAIL", "test@test.invalid"),
        ("NEXUS_CATALOG_PATH", str(tmp_path / "catalog")), ("NX_STORAGE_BACKEND_VECTORS", "service"),
        ("NX_LOCAL", "0"), ("VOYAGE_API_KEY", "fake"), ("CHROMA_API_KEY", "fake"),
    ]:
        monkeypatch.setenv(k, v)


@pytest.fixture(autouse=True)
def _collectors():
    reset_manifest_identity_drops()
    reset_ephemeral_registration_skips()
    yield


class _Reader:
    def owner_for_repo(self, repo_hash):  # noqa: ANN001, ARG002
        return "1.7"

    def close(self) -> None:
        pass


class _FailingWriter:
    """Stages fine; the commit fails the way the engine's failed job does."""

    def __init__(self, reason: str = "transient_db_error") -> None:
        self.reason = reason
        self.staged: list[list[dict]] = []

    def reconcile_stage(self, owner, run_id, walk, expected_rows, rows):  # noqa: ANN001, ARG002
        self.staged.append(rows)
        return len(rows)

    def reconcile_commit(self, owner, run_id):  # noqa: ANN001, ARG002
        raise CatalogReconcileError(self.reason, "the engine said no", http_status=503)

    def close(self) -> None:
        pass


def _failing_catalog(writer: _FailingWriter) -> dict:
    return {
        "nexus.catalog.factory.make_catalog_reader": {"side_effect": lambda **kw: _Reader()},
        "nexus.catalog.factory.make_catalog_writer": {"side_effect": lambda **kw: writer},
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n")
    (repo / "b.py").write_text("y = 2\n")
    _git(repo, "add", "a.py", "b.py")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _run(repo: Path, writer: _FailingWriter, **run_kw):
    db = _http_db()
    with _service_mode_patches(db, extra=_failing_catalog(writer)) as mocks, patch(
        "nexus.chunk_batcher.ChunkBatcher", _CapturingBatcher,
    ):
        stats = _run_index(repo, _reg(), force=False, **run_kw)
    return stats, mocks, db


# ── the hook reports the failure to its caller ───────────────────────────────────────────────────────────────────

def test_the_hook_reports_a_failed_commit_through_its_out_param(tmp_path: Path) -> None:
    failures: list[str] = []
    audited: list[dict] = []  # the audit row is a T2 write; this test has no tenant, so the recorder stands in
    with patch("nexus.indexer.record_catalog_hook_failure", lambda **kw: audited.append(kw)), patch(
        "nexus.catalog.factory.make_catalog_reader", lambda **kw: _Reader(),
    ), patch("nexus.catalog.factory.make_catalog_writer", lambda **kw: _FailingWriter("commit_conflict")):
        ids = _catalog_hook(
            repo=tmp_path, repo_name="r", repo_hash="abcd1234", head_hash="h", indexed_files=[],
            walk="partial", deleted_relpaths=["gone.py"], failures=failures,
        )
    assert ids == {}
    assert len(failures) == 1 and "commit_conflict" in failures[0], failures
    assert [a["hook_name"] for a in audited] == ["catalog_index_hook"], "the failure is audited once"


def test_the_hook_reports_no_failure_for_a_commit_that_succeeds(tmp_path: Path) -> None:
    class Ok(_FailingWriter):
        def reconcile_commit(self, owner, run_id):  # noqa: ANN001, ARG002
            return {"rows": []}

    failures: list[str] = []
    with patch("nexus.catalog.factory.make_catalog_reader", lambda **kw: _Reader()), patch(
        "nexus.catalog.factory.make_catalog_writer", lambda **kw: Ok(),
    ):
        _catalog_hook(repo=tmp_path, repo_name="r", repo_hash="abcd1234", head_hash="h", indexed_files=[],
                      walk="partial", deleted_relpaths=["gone.py"], failures=failures)
    assert failures == []


# ── S1: a delta run with only deletions ──────────────────────────────────────────────────────────────────────────

def test_a_failed_commit_on_a_deletions_only_delta_run_is_counted_in_the_run_stats(git_repo: Path) -> None:
    base = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "rm", "-q", "b.py")
    _git(git_repo, "commit", "-q", "-m", "drop b")
    writer = _FailingWriter()
    with patch("nexus.indexer._get_owner_head_hash", lambda repo: base):
        stats, mocks, _db = _run(git_repo, writer, since_head=True)
    assert [r["file_path"] for page in writer.staged for r in page] == ["b.py"], "the run staged only the deletion"
    assert stats["identity_less_dropped_files"] == 0, "no file is unregistered: the identity-less drop cannot fail this run"
    assert stats["catalog_hook_failed"] == 1
    mocks["_index_code_file"].assert_not_called()


def test_a_successful_delta_run_counts_no_hook_failure(git_repo: Path) -> None:
    class Ok(_FailingWriter):
        def reconcile_commit(self, owner, run_id):  # noqa: ANN001, ARG002
            return {"rows": []}

    base = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "rm", "-q", "b.py")
    _git(git_repo, "commit", "-q", "-m", "drop b")
    with patch("nexus.indexer._get_owner_head_hash", lambda repo: base):
        stats, _mocks, _db = _run(git_repo, Ok(), since_head=True)
    assert stats["catalog_hook_failed"] == 0


@pytest.fixture
def registry():
    mock = MagicMock()
    mock.get.return_value = {"collection": "code__repo", "code_collection": "code__repo",
                             "docs_collection": "docs__repo", "status": "registered"}
    return mock


def test_the_since_head_base_is_not_advanced_over_a_failed_catalog_commit(
    tmp_path: Path, registry, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    with structlog.testing.capture_logs() as logs, patch(
        "nexus.indexer._run_index", return_value={"catalog_hook_failed": 1},
    ), patch("nexus.indexer._current_head", return_value="abc"), patch("nexus.indexer._set_owner_head_hash") as mock_set:
        index_repository(tmp_path, registry)
    mock_set.assert_not_called()
    held = [e for e in logs if e.get("event") == "since_head_base_not_advanced"]
    assert held and held[0]["catalog_hook_failed"] == 1, logs


def test_the_base_is_advanced_when_the_catalog_commit_did_not_fail(
    tmp_path: Path, registry, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: the same call with a zero count advances, so the test above fails for the count and nothing else."""
    monkeypatch.setenv("HOME", str(tmp_path))
    with patch("nexus.indexer._run_index", return_value={"catalog_hook_failed": 0}), patch(
        "nexus.indexer._current_head", return_value="abc",
    ), patch("nexus.indexer._set_owner_head_hash") as mock_set:
        index_repository(tmp_path, registry)
    mock_set.assert_called_once_with(tmp_path, "abc")


# ── S4 / I2: a failed commit writes no chunk ─────────────────────────────────────────────────────────────────────

def test_a_failed_commit_writes_no_chunk_and_the_run_reports_it(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for n in ("hello.py", "other.py"):
        (repo / n).write_text(f"# {n}\nx = 1\n")
    writer = _FailingWriter()
    stats, mocks, db = _run(repo, writer)
    assert [r["file_path"] for page in writer.staged for r in page] == ["hello.py", "other.py"]
    # No vector WRITE of any kind (the run still reads and prepares the collection), and no file reaches the indexer
    # that would write one: nothing of the run lands under a document the catalog never committed.
    writes = [c[0] for c in db.mock_calls if c[0].split(".")[-1].startswith(("upsert", "delete", "add", "write", "put", "update"))]
    assert writes == []
    mocks["_index_code_file"].assert_not_called()
    assert stats["catalog_hook_failed"] == 1
    assert stats["identity_less_dropped_files"] == 2, "both files are named dropped: the run fails on them too"


def test_a_committed_run_dispatches_its_files(tmp_path: Path) -> None:
    """Control for the test above: the same run with a commit that succeeds does hand its files to the indexer, so
    ``assert_not_called`` there is a statement about the failed commit and not about this harness."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for n in ("hello.py", "other.py"):
        (repo / n).write_text(f"# {n}\nx = 1\n")

    class Committing(_FailingWriter):
        def reconcile_commit(self, owner, run_id):  # noqa: ANN001, ARG002
            rows = [{"file_path": r["file_path"], "tumbler": f"1.7.{i}", "action": "created", "sensitive": False,
                     "content_hash_changed": False, "index_state": None, "index_content_hash": ""}
                    for i, page in enumerate(self.staged, 1) for r in page]
            return {"rows": rows, "trashed": [], "renamed": [], "swept": [], "duplicate_path": [],
                    "deleted_skipped_foreign_owner": [], "sensitivity_pending": []}

    stats, mocks, _db = _run(repo, Committing())
    assert stats["catalog_hook_failed"] == 0
    assert mocks["_index_code_file"].call_count == 2
