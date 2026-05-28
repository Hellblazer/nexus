# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-zir76: route the aspect-worker persist path through the T2 daemon.

RDR-128 routed the worker's poll/claim/reclaim through the T2 daemon but
left the *persist* path (``document_aspects.upsert`` +
``aspect_queue.mark_done`` / ``mark_failed``) on the DIRECT ``memory.db``
write path. That direct write competed with the daemon for the single
WAL writer lock (``database is locked``), and a failed direct
``mark_failed`` orphaned the row ``in_progress`` until the 300s
``reclaim_stale`` backstop. Diagnosed 2026-05-27 (a row stuck
``in_progress`` for ~5 minutes during DEVONthink PDF incorporation).

These tests pin the fix:

1. ``T2Database.complete_aspect`` upserts the record AND marks the queue
   row done in one daemon-side call.
2. ``complete_aspect`` is daemon-routable (``database.complete_aspect``)
   and ``T2Client`` carries the facade-parity passthrough so a
   ``t2_index_write(write_fn)`` body reaches it on either path.
3. ``_process_row`` persists via ``t2_index_write`` (routed), never via
   the direct ``t2_ctx`` path — success AND failure.
4. Dead ``aspect_worker.<pid>`` lock files are swept at worker startup.
5. The stuck-row reclaim backstop default dropped 300s -> 60s.
"""
from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database
from nexus.db.t2.aspect_extraction_queue import QueueRow
from nexus.db.t2.document_aspects import AspectRecord


def _record(collection: str, source_path: str) -> AspectRecord:
    return AspectRecord(
        collection=collection,
        source_path=source_path,
        problem_formulation="P",
        proposed_method="M",
        experimental_datasets=["d1"],
        experimental_baselines=["b1"],
        experimental_results="R",
        extras={"venue": "V"},
        confidence=0.9,
        extracted_at="2026-05-27T00:00:00+00:00",
        model_version="claude-haiku-4-5-20251001",
        extractor_name="scholarly-paper-v1",
        doc_id="1.2.3",
    )


# ── 1. complete_aspect: atomic upsert + mark_done ────────────────────────────


def test_complete_aspect_upserts_and_marks_done(tmp_path: Path) -> None:
    db_path = tmp_path / "t2.db"
    coll, src = "knowledge__delos", "/p1.pdf"
    with T2Database(db_path) as db:
        db.aspect_queue.enqueue(coll, src)
        assert db.aspect_queue.pending_count() == 1

        ok = db.complete_aspect(dataclasses.asdict(_record(coll, src)))

        assert ok is True
        rec = db.document_aspects.get(coll, src)
        assert rec is not None
        assert rec.problem_formulation == "P"
        assert rec.proposed_method == "M"
        # mark_done DELETEs the queue row — nothing left to drain.
        assert db.aspect_queue.conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue"
        ).fetchone()[0] == 0


def test_complete_aspect_accepts_plain_dict_from_wire(tmp_path: Path) -> None:
    """The worker sends ``dataclasses.asdict(record)``; the daemon wire
    decodes a dataclass to a plain dict. ``complete_aspect`` must
    reconstruct the ``AspectRecord`` from that dict, not require an object.
    """
    db_path = tmp_path / "t2.db"
    coll, src = "knowledge__delos", "/wire.pdf"
    fields = dataclasses.asdict(_record(coll, src))
    assert isinstance(fields, dict) and not isinstance(fields, AspectRecord)
    with T2Database(db_path) as db:
        db.aspect_queue.enqueue(coll, src)
        db.complete_aspect(fields)
        assert db.document_aspects.get(coll, src) is not None


# ── 2. Daemon routability + client parity ────────────────────────────────────


def test_complete_aspect_is_daemon_routable(tmp_path: Path) -> None:
    from nexus.daemon.t2_daemon import (
        _T2_DATABASE_METHODS,
        _build_dispatch_table,
    )

    assert "complete_aspect" in _T2_DATABASE_METHODS
    with T2Database(tmp_path / "t2.db") as db:
        table = _build_dispatch_table(db)
    assert "database.complete_aspect" in table


def test_t2client_exposes_complete_aspect_passthrough() -> None:
    """``t2_index_write`` may hand the write_fn a ``T2Client``; it must
    expose ``complete_aspect`` at the top level (delegating to
    ``database.complete_aspect``) so the call works on either path.
    """
    from nexus.daemon.t2_client import T2Client

    client = T2Client(skip_handshake=True)
    forwarded: dict[str, object] = {}

    class _FakeDatabaseProxy:
        def complete_aspect(self, *args, **kwargs):
            forwarded["args"] = args
            forwarded["kwargs"] = kwargs
            return True

    client.database = _FakeDatabaseProxy()  # type: ignore[assignment]
    payload = {"collection": "c", "source_path": "s"}
    assert client.complete_aspect(payload) is True
    assert forwarded["args"] == (payload,)


# ── 3. _process_row routes persist through t2_index_write, not t2_ctx ─────────


def _patch_persist_routing(monkeypatch, db_path: Path) -> list[int]:
    """Route ``t2_index_write`` to a direct tmp DB and make ``t2_ctx``
    fail loudly if the persist path ever opens memory.db directly.

    Returns a call-count list for ``t2_index_write``.
    """
    import nexus.mcp_infra as infra

    calls: list[int] = []

    def _routed(write_fn):  # noqa: ANN001
        calls.append(1)
        with T2Database(db_path) as db:
            return write_fn(db)

    def _forbidden_ctx():
        raise AssertionError(
            "persist must route through t2_index_write, not direct t2_ctx"
        )

    monkeypatch.setattr(infra, "t2_index_write", _routed)
    monkeypatch.setattr(infra, "t2_ctx", _forbidden_ctx)
    return calls


def test_process_row_success_persists_via_index_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.aspect_worker import AspectExtractionWorker

    db_path = tmp_path / "t2.db"
    coll, src = "knowledge__delos", "/ok.pdf"
    with T2Database(db_path) as db:
        db.aspect_queue.enqueue(coll, src)

    calls = _patch_persist_routing(monkeypatch, db_path)
    monkeypatch.setattr(
        "nexus.aspect_worker._extract_aspects",
        lambda **_kw: _record(coll, src),
    )

    row = QueueRow(collection=coll, source_path=src, content_hash="h",
                   content="", retry_count=0, doc_id="1.2.3")
    AspectExtractionWorker()._process_row(row)

    assert calls, "persist did not route through t2_index_write"
    with T2Database(db_path) as db:
        assert db.document_aspects.get(coll, src) is not None
        assert db.aspect_queue.conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue"
        ).fetchone()[0] == 0


def test_process_row_failure_marks_failed_via_index_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.aspect_worker import AspectExtractionWorker

    db_path = tmp_path / "t2.db"
    coll, src = "knowledge__delos", "/boom.pdf"
    with T2Database(db_path) as db:
        db.aspect_queue.enqueue(coll, src)

    calls = _patch_persist_routing(monkeypatch, db_path)

    def _raise(**_kw):
        raise RuntimeError("extract boom")

    monkeypatch.setattr("nexus.aspect_worker._extract_aspects", _raise)

    row = QueueRow(collection=coll, source_path=src, content_hash="h",
                   content="", retry_count=0, doc_id="")
    AspectExtractionWorker()._process_row(row)

    assert calls, "failure path did not route through t2_index_write"
    with T2Database(db_path) as db:
        status = db.aspect_queue.conn.execute(
            "SELECT status FROM aspect_extraction_queue "
            "WHERE collection = ? AND source_path = ?",
            (coll, src),
        ).fetchone()
    assert status is not None and status[0] == "failed"


# ── 4. Startup sweep of dead lock files ──────────────────────────────────────


def test_write_worker_lock_sweeps_dead_pid_locks(tmp_path: Path) -> None:
    import os

    from nexus.aspect_worker import _write_worker_lock

    locks_dir = tmp_path / "locks"
    locks_dir.mkdir()

    # A guaranteed-dead PID: spawn a trivial child and reap it.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid
    (locks_dir / f"aspect_worker.{dead_pid}").write_text(str(dead_pid))
    # A non-PID-shaped file must be left untouched.
    (locks_dir / "aspect_worker.notapid").write_text("x")

    _write_worker_lock(locks_dir)

    assert not (locks_dir / f"aspect_worker.{dead_pid}").exists()
    # This process is alive, so its own lock was written and kept.
    assert (locks_dir / f"aspect_worker.{os.getpid()}").exists()
    assert (locks_dir / "aspect_worker.notapid").exists()


# ── 5. Faster reclaim backstop ───────────────────────────────────────────────


def test_reclaim_backstop_default_is_60s() -> None:
    import inspect

    from nexus.aspect_worker import (
        AspectExtractionWorker,
        ensure_worker_started,
    )

    assert AspectExtractionWorker()._stale_timeout_seconds == 60
    sig = inspect.signature(ensure_worker_started)
    assert sig.parameters["stale_timeout_seconds"].default == 60
