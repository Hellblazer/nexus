# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-235 (nexus-5r2sc.3.2): the indexer's aspect flush hook counts a refused
batch.

``index_repo``'s ChunkBatcher flush hook swallows a failed
``aspect_queue.enqueue_many`` so ingest keeps moving. Its except arm hands the
failure to ``record_batch_enqueue_failure`` so a ``MaxLiveRowsExceeded`` becomes a
``hook_failures`` row. A stale ``NX_ASPECT_QUEUE=table`` is not a failure: the
producer warns and queues on tuples (nexus-bpikw), so nothing is dropped and
nothing is counted. ``record_batch_enqueue_failure`` itself is pinned in
``tests/test_http_aspect_queue_tuples_producer.py``; this test drives the branch
that calls it, through a REAL ``index_repository()`` flush.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import structlog

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
    monkeypatch.delenv(ASPECT_QUEUE_ENV, raising=False)  # the test sets its own value


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


def _tasks_for(repo_name: str) -> list[dict]:
    """The bodies of the ``aspects`` tasks of *repo_name*'s collection (the repo's name
    is part of its ``rdr__`` collection name)."""
    import json

    from nexus.db.t2.http_tuple_store import HttpTupleStore

    rows = HttpTupleStore().rd("aspects", {"queue": "aspects"}, n=300)
    return [b for r in rows if repo_name in (b := json.loads(r.body))["collection"]]


@pytest.mark.t2_tenant
def test_the_flush_counts_a_refused_batch_under_tuples_and_a_stale_table_value_still_queues_on_tuples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One tenant, a real ``index_repository()`` twice.

    Half one: under ``tuples`` a refused batch (the tenant at its ceiling) is counted
    as a ``hook_failures`` row. Half two: under a leftover ``NX_ASPECT_QUEUE=table``
    nothing is stubbed, so the REAL producer reader runs (nexus-bpikw design decision
    D1): it warns once and uses tuples, the flush's batch lands as ``aspects`` tasks,
    and it is NOT counted as a failure, because it did not fail. A reader that
    refused ``table`` here would drop the batch and count it, which is the outcome
    this half exists to rule out."""
    import nexus.db.t2.http_aspect_queue as mod

    real_enqueue_many = HttpAspectQueue.enqueue_many
    with monkeypatch.context() as mp:
        calls = _refuse_every_batch(mp)
        mp.setenv(ASPECT_QUEUE_ENV, "tuples")
        before = _failures()["total"]
        _index(_rdr_repo(tmp_path, "tuples-repo"), tmp_path)
        assert calls, "tuples: harness broken: the flush hook never reached enqueue_many"
        failures = _failures()
        assert failures["total"] - before == len(calls) == 1, ("tuples", failures, calls)
        row = str(failures["rows"][0])
        assert "rdr__" in row and "MaxLiveRowsExceededError" in row, f"tuples: {row}"
        assert f"<batch of {calls[0]}>" in row, f"tuples: {row}"

    # The stub is gone: enqueue_many is the real one.
    assert HttpAspectQueue.enqueue_many is real_enqueue_many, "harness broken: the refusal stub is still installed"
    mod._warned_table_retired.clear()
    monkeypatch.setenv(ASPECT_QUEUE_ENV, "table")
    counted = _failures()["total"]
    with structlog.testing.capture_logs() as cap:
        _index(_rdr_repo(tmp_path, "table-repo"), tmp_path)
    tasks = _tasks_for("table-repo")
    assert tasks, "table: the stale value dropped the batch: no aspects task was written"
    assert _failures()["total"] == counted, "table: a batch that landed on tuples was counted as a failure"
    warned = [e for e in cap if e["event"] == "aspect_queue_table_mode_retired_using_tuples"]
    assert len(warned) == 1 and warned[0]["bead"] == "nexus-bpikw", warned
