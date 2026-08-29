# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A fault in the worker's own environment is not a verdict about a document.

nexus-yg70j, the third fault class. ``_is_retryable`` describes a TWO-BUCKET
world — TRANSIENT-REMOTE (retry) and BAD-DOCUMENT (terminal) — and a fault in
the worker's own environment is neither. Before this it reached terminal by
OMISSION rather than by judgement: nothing claimed the document was bad, it
simply fell through to the else-branch.

MEASURED, on the 2026-08-24 incident's 26 terminally-failed production rows:

     7  relative source_paths  -- genuine victims of the deleted-cwd fault
    19  absolute source_paths  -- ALL 19 EXIST ON DISK, killed as batch collateral

The 19 were covered by routing the batch arm per row (shipped 7.16.3). These
tests cover the 7: the honest response to "my process cannot resolve its own
cwd" is to write NO row state and stand the worker down, leaving the claimed
rows to ``reclaim_stale``.

RECORDED TRAP, from the bead. ``_is_retryable`` is TYPE-based, not
type-based: ``RuntimeError("connection refused")`` is NOT retryable, because the
real ``httpx`` transport exception type is what matches. Nothing here reaches
for a synthetic lookalike; the tests below use real ``httpx`` and real
``errno``. (The SQLite "database is locked" example this file was written
around is gone with that substrate, 2026-08-29.)
"""
from __future__ import annotations

import errno
import httpx
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from nexus.aspect_worker import (
    _RETRY_MAX_ATTEMPTS,
    AspectExtractionWorker,
    _self_fault_reason,
)


# ── scaffolding ─────────────────────────────────────────────────────────────


class _FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def mark_failed(self, collection, source_path, error) -> None:  # noqa: ANN001
        self.calls.append(("failed", collection, source_path, error))

    def mark_retry(self, collection, source_path, interval_seconds=0) -> None:  # noqa: ANN001
        self.calls.append(("retry", collection, source_path, interval_seconds))

    def mark_done(self, collection, source_path) -> None:  # noqa: ANN001
        self.calls.append(("done", collection, source_path))


class _FakeDb:
    def __init__(self) -> None:
        self.aspect_queue = _FakeQueue()


def _row(retry_count: int = 0, source_path: str = "/p.pdf"):
    return types.SimpleNamespace(
        collection="rdr__1-1__voyage-context-3__v1",
        source_path=source_path,
        content="body",
        retry_count=retry_count,
    )


@pytest.fixture()
def worker_and_db(monkeypatch: pytest.MonkeyPatch):
    import nexus.mcp_infra as infra

    db = _FakeDb()
    monkeypatch.setattr(infra, "t2_index_write", lambda fn: fn(db))
    worker = AspectExtractionWorker(poll_interval=10.0)
    faults: list[str] = []
    worker.set_self_fault_handler(faults.append)
    return worker, db, faults


# ── the mechanism, pinned against a REAL deleted directory ──────────────────


def test_getcwd_raises_once_the_cwd_is_deleted() -> None:
    """The premise of the whole class. ``_self_fault_reason``'s first arm is
    ``os.getcwd()``; if a future Python or platform stops raising here, that arm
    is guarding nothing and this test says so rather than passing quietly."""
    probe = textwrap.dedent(
        """
        import os, shutil, sys, tempfile
        d = tempfile.mkdtemp()
        os.chdir(d)
        shutil.rmtree(d)
        try:
            os.getcwd()
        except OSError:
            sys.exit(42)
        sys.exit(0)
        """
    )
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True)
    assert r.returncode == 42, (
        f"os.getcwd() no longer raises on a deleted cwd (exit {r.returncode}) — "
        "the premise of nexus-yg70j's self-fault detection changed"
    )


def test_predicate_fires_on_a_real_deleted_cwd() -> None:
    """The positive control, driven by the real fault rather than a patched
    ``os.getcwd``. Runs in a subprocess because deleting the cwd would poison
    every later test in this session."""
    repo = Path(__file__).parent.parent
    probe = textwrap.dedent(
        f"""
        import os, shutil, sys, tempfile
        sys.path.insert(0, {str(repo / "src")!r})
        # Import BEFORE the cwd is destroyed — imports resolve paths too.
        from nexus.aspect_worker import _self_fault_reason

        d = tempfile.mkdtemp()
        os.chdir(d)
        shutil.rmtree(d)

        reason = _self_fault_reason(ValueError("a perfectly ordinary row error"))
        sys.exit(42 if (reason or "").startswith("cwd_unresolvable") else 1)
        """
    )
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert r.returncode == 42, (
        "a deleted cwd was not detected as a self fault.\n"
        f"stdout={r.stdout}\nstderr={r.stderr}"
    )


# ── the predicate's boundary: what must NOT be a self fault ─────────────────


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(ValueError("malformed record"), id="value-error"),
        pytest.param(TypeError("programming bug"), id="type-error"),
        pytest.param(
            FileNotFoundError(errno.ENOENT, "No such file or directory", "/gone.pdf"),
            id="enoent-the-document-is-missing",
        ),
        pytest.param(
            PermissionError(errno.EACCES, "Permission denied", "/locked.pdf"),
            id="eacces-this-row-is-unreadable",
        ),
        pytest.param(httpx.ConnectError("connection refused"), id="transport-error"),
    ],
)
def test_a_healthy_process_reports_no_self_fault(exc: BaseException) -> None:
    """The falsification that keeps the errno set narrow.

    ENOENT is the one that matters: it is the exception type the incident
    actually raised, and it is ALSO what a genuinely missing document raises.
    If it were classified as a self fault, every bad row in the corpus would
    strand the worker forever instead of terminal-failing for triage.
    """
    assert _self_fault_reason(exc) is None


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        pytest.param(
            OSError(errno.ENOSPC, "No space left on device"),
            "host_resource_exhausted (ENOSPC)",
            id="enospc",
        ),
        pytest.param(
            OSError(errno.EMFILE, "Too many open files"),
            "host_resource_exhausted (EMFILE)",
            id="emfile",
        ),
        pytest.param(MemoryError(), "host_out_of_memory", id="oom"),
    ],
)
def test_host_resource_exhaustion_is_a_self_fault(exc: BaseException, expected: str) -> None:
    """These cannot be a property of a document by construction: no content
    makes a disk full or a process out of file descriptors."""
    assert _self_fault_reason(exc) == expected


def test_a_wrapped_host_fault_is_still_found() -> None:
    """The batch extractor wraps, so the classifier walks the cause chain."""
    inner = OSError(errno.ENOSPC, "No space left on device")
    outer = RuntimeError("batch extract failed")
    outer.__cause__ = inner
    assert _self_fault_reason(outer) == "host_resource_exhausted (ENOSPC)"


def test_a_self_referential_cause_chain_terminates() -> None:
    """``__context__`` can cycle. The walk is depth-bounded; without that this
    test hangs rather than fails, which is why it exists."""
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__context__ = b
    b.__context__ = a
    assert _self_fault_reason(a) is None


# ── the router: a self fault writes no row state ────────────────────────────


def test_self_fault_writes_no_row_state_and_stands_the_worker_down(worker_and_db) -> None:
    worker, db, faults = worker_and_db

    with capture_logs() as logs:
        worker._mark_retry_or_fail_routed(
            _row(), OSError(errno.ENOSPC, "No space left on device"),
        )

    assert db.aspect_queue.calls == [], (
        "a self fault recorded a verdict about the row: "
        f"{db.aspect_queue.calls!r}"
    )
    assert worker.is_claiming_stopped() is True
    assert worker.self_fault() == "host_resource_exhausted (ENOSPC)"
    assert faults == ["host_resource_exhausted (ENOSPC)"]

    errors = [e for e in logs if e.get("event") == "aspect_worker_self_fault"]
    assert len(errors) == 1
    assert errors[0]["log_level"] == "error", (
        "the incident ran 16 consecutive failed batches at WARNING with nothing "
        "alerting — this class must be louder than that"
    )


@pytest.mark.parametrize(
    ("exc", "retry_count", "expected"),
    [
        pytest.param(httpx.ConnectError("connection refused"), 0, "retry", id="transient-remote"),
        pytest.param(ValueError("malformed"), 0, "failed", id="bad-document"),
        pytest.param(
            httpx.ConnectError("connection refused"), _RETRY_MAX_ATTEMPTS, "failed",
            id="budget-exhausted",
        ),
    ],
)
def test_the_existing_two_buckets_are_unchanged(
    worker_and_db, exc: BaseException, retry_count: int, expected: str,
) -> None:
    """The negative control for the new first branch.

    The self-fault check runs ahead of the existing decision, so it is capable
    of swallowing the whole taxonomy. These three cases are what proves it did
    not: a healthy process still retries what is transient and still
    terminal-fails what is a bad document.
    """
    worker, db, faults = worker_and_db
    worker._mark_retry_or_fail_routed(_row(retry_count), exc)

    assert [c[0] for c in db.aspect_queue.calls] == [expected]
    assert faults == []
    assert worker.is_claiming_stopped() is False


def test_the_stand_down_is_latched_once_per_episode(worker_and_db) -> None:
    """The batch arm calls the router once per row. Five rows must produce one
    episode, not five ERROR lines and five stand-downs."""
    worker, db, faults = worker_and_db
    exc = OSError(errno.ENOSPC, "No space left on device")

    with capture_logs() as logs:
        for i in range(5):
            worker._mark_retry_or_fail_routed(_row(source_path=f"/p{i}.pdf"), exc)

    assert db.aspect_queue.calls == []
    assert faults == ["host_resource_exhausted (ENOSPC)"]
    assert len([e for e in logs if e.get("event") == "aspect_worker_self_fault"]) == 1


def test_a_restarted_worker_can_stand_down_again(worker_and_db) -> None:
    """The latch gates ``_stop_event`` too, so a latch surviving a restart would
    make the worker permanently unable to report the NEXT fault."""
    worker, _db, faults = worker_and_db
    worker._mark_retry_or_fail_routed(_row(), OSError(errno.ENOSPC, "full"))
    assert worker.self_fault() is not None

    worker.start()
    try:
        assert worker.self_fault() is None
    finally:
        worker.stop(timeout=2.0)


# ── the incident path: a whole batch, end to end ────────────────────────────


def test_a_batch_failing_on_a_self_fault_leaves_all_five_rows_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-08-24 shape. Five claimed rows, one environmental fault, and
    the queue must come out of it saying nothing at all about any of them —
    they stay ``in_progress`` for ``reclaim_stale``."""
    import nexus.aspect_worker as aw
    import nexus.mcp_infra as infra

    db = _FakeDb()
    monkeypatch.setattr(infra, "t2_index_write", lambda fn: fn(db))

    def _boom(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(aw, "_extract_aspects_batch", _boom)

    worker = AspectExtractionWorker(poll_interval=10.0)
    faults: list[str] = []
    worker.set_self_fault_handler(faults.append)
    rows = [_row(source_path=f"/p{i}.pdf") for i in range(5)]

    with capture_logs() as logs:
        worker._process_batch(rows)

    assert db.aspect_queue.calls == [], (
        "the batch recorded row state despite an environmental fault: "
        f"{db.aspect_queue.calls!r}"
    )
    assert faults == ["host_resource_exhausted (ENOSPC)"]
    assert worker.is_claiming_stopped() is True
    assert len([e for e in logs if e.get("event") == "aspect_worker_self_fault"]) == 1


# ── the host half: stopping the thread is not enough ────────────────────────


class _FakeHostedWorker:
    """A worker that implements the stand-down hook, without a drain thread."""

    def __init__(self) -> None:
        self.handler = None
        self.started = 0

    def set_self_fault_handler(self, handler) -> None:  # noqa: ANN001
        self.handler = handler

    def start(self) -> None:
        self.started += 1

    def stop(self, timeout: float = 10.0) -> None:
        ...


class _NoopQueue:
    def reclaim_stale(self, timeout_seconds: int = 300) -> int:
        return 0

    def close(self) -> None:
        ...


def test_the_daemon_stands_down_when_its_worker_reports_a_self_fault(tmp_path: Path) -> None:
    """Stopping the worker THREAD alone would leave this daemon heartbeating a
    healthy-looking lease while draining nothing — a second silent permanent
    death. The fault has to reach the process."""
    from nexus.daemon.aspect_worker_daemon import AspectWorkerDaemon

    hosted = _FakeHostedWorker()
    d = AspectWorkerDaemon(
        config_dir=tmp_path, tenant="tenant-A",
        worker_factory=lambda: hosted, queue_factory=_NoopQueue,
    )
    d.start()
    try:
        assert hosted.handler is not None, "the daemon never installed its hook"
        assert d._stop.is_set() is False

        with capture_logs() as logs:
            hosted.handler("cwd_unresolvable (FileNotFoundError: [Errno 2] ...)")

        assert d._stop.is_set() is True, (
            "run_until_signal would keep blocking, so the lease is never "
            "relinquished and no respawn ever happens"
        )
        events = [e for e in logs if e.get("event") == "aspect_worker_daemon.worker_self_fault"]
        assert len(events) == 1
        assert events[0]["log_level"] == "error"
    finally:
        d.stop()


def test_the_real_worker_exposes_the_hook_the_daemon_attaches() -> None:
    """The daemon attaches by duck-typing so the zero-arg fakes its injected
    factory protocol allows keep working. That makes a rename on the worker
    silently turn the wiring into a no-op — this is the check that catches it."""
    assert callable(getattr(AspectExtractionWorker(), "set_self_fault_handler", None))
