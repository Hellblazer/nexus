# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-235 (nexus-5r2sc.3.2): the indexer's aspect flush hook counts a refused
batch in tuples mode.

``index_repo``'s ChunkBatcher flush hook swallows a failed
``aspect_queue.enqueue_many`` so ingest keeps moving. In tuples mode its except
arm must hand the failure to ``record_batch_enqueue_failure`` so a
``MaxLiveRowsExceeded`` becomes a ``hook_failures`` row; in table mode it must
not. ``record_batch_enqueue_failure`` itself is pinned in
``tests/test_http_aspect_queue_tuples_producer.py``; these tests drive the
branch that calls it, through a REAL ``index_repository()`` flush.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus.db.t2.http_aspect_queue import ASPECT_QUEUE_ENV, HttpAspectQueue
from nexus.db.t2.http_tuple_store import MaxLiveRowsExceededError
from nexus.indexer import index_repository
from nexus.mcp_infra import t2_index_write
from nexus.registry import RepoRegistry

_HOOK = "aspect_extraction_enqueue_hook"

pytestmark = pytest.mark.usefixtures("t2_service_env")


def _git_init(repo: Path) -> None:
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "test@test"],
        ["git", "config", "user.name", "test"],
        ["git", "add", "."],
        ["git", "commit", "-m", "initial"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)


def _rdr_repo(tmp_path: Path, name: str) -> Path:
    """A repo whose only indexable files are RDRs: the rdr__ collection has an
    extractor, so the flush hook reaches the enqueue. Its prose is keyed on *name*,
    so two repos in one test never share a chunk."""
    repo = tmp_path / name
    (repo / "docs" / "rdr").mkdir(parents=True)
    for i in (1, 2):
        (repo / "docs" / "rdr" / f"rdr-00{i}-thing.md").write_text(
            f"---\ntitle: Thing {i}\nstatus: draft\n---\n\n# Thing {i}\n\n"
            f"## Problem\n\nThe tuples-mode batch tripwire needs prose number {i} of {name}.\n"
        )
    _git_init(repo)
    return repo


@pytest.fixture(autouse=True)
def _quiet_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)
    monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "0")
    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.setenv(ASPECT_QUEUE_ENV, "table")  # explicit; each test sets its own mode


def _refuse_every_batch(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    def _refuse(self, rows):  # noqa: ANN001
        calls.append(len(rows))
        raise MaxLiveRowsExceededError("tenant at max_live_rows")

    monkeypatch.setattr(HttpAspectQueue, "enqueue_many", _refuse)
    return calls


def _index(repo: Path, tmp_path: Path) -> None:
    """The real pipeline against the suite's engine substrate: ``make_t3()`` is
    the service-backed ``HttpVectorClient``, which is what arms the
    ChunkBatcher (and so the flush hook under test)."""
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(repo)
    index_repository(repo, reg)


def _failures() -> dict:
    return t2_index_write(lambda t2: t2.telemetry.list_hook_failures(hook_names=[_HOOK]))


@pytest.mark.t2_tenant
def test_the_flush_counts_a_refused_aspect_batch_in_tuples_mode_and_not_in_table_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both modes through a real ``index_repository()`` on one tenant (nexus-9edxe
    folded the two tests): the tuples-mode flush counts the refusal, then, on a
    second repo, the table-mode control does not. Tuples first, so the control's
    'no row added' is read against a count that has just risen."""
    calls = _refuse_every_batch(monkeypatch)

    monkeypatch.setenv(ASPECT_QUEUE_ENV, "tuples")
    before = _failures()["total"]
    _index(_rdr_repo(tmp_path, "tuples-repo"), tmp_path)
    assert calls, "tuples: harness broken: the flush hook never reached enqueue_many"
    failures = _failures()
    assert failures["total"] - before == len(calls) == 1, ("tuples", failures, calls)
    row = str(failures["rows"][0])
    assert "rdr__" in row and "MaxLiveRowsExceededError" in row, f"tuples: {row}"
    assert f"<batch of {calls[0]}>" in row, f"tuples: {row}"

    monkeypatch.setenv(ASPECT_QUEUE_ENV, "table")
    tuples_calls = len(calls)
    counted = failures["total"]
    _index(_rdr_repo(tmp_path, "table-repo"), tmp_path)
    assert len(calls) > tuples_calls, "table: harness broken: the flush hook never reached enqueue_many"
    assert _failures()["total"] == counted, "table: a refused batch left a hook_failures row"
