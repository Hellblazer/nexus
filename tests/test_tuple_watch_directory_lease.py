# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The watcher's ``directory/<name>`` lease (RDR-208 Phase 2 Step 1, bead
nexus-galkv.9): arm, 60s re-send, re-nonce before the retention ceiling,
release on the marker-mismatch self-stop.

Two fixture styles, matching the bead's own split:

- ``TestDirectoryLeaseUnit`` -- a genuinely FAKE store (records ``out()``
  calls, never touches the engine) plus a fake clock threaded through
  ``run_watch``'s own ``now``/``sleep`` hooks, exactly like the pre-existing
  ``_NeverProbed`` pattern in ``tests/test_tuple_cmd.py``'s stale-watcher
  self-stop tests. No ``t2_service_env`` fixture: these run in milliseconds
  and assert exact ``out()`` call arguments.
- ``TestDirectoryLeaseIntegration`` -- the real engine substrate
  (``t2_service_env`` + ``HttpTupleStore``), proving the lease round-trips
  through the actual ``directory/<name>`` template (RDR-208 Phase 1 Step 1)
  with real, short-overridden TTL/heartbeat.

The lease lives in ``run_watch``'s own local ``_DirectoryLease`` -- purely
in-process, never persisted to disk -- so every multi-cycle unit test below
drives ONE continuous ``run_watch(..., iterations=N, ...)`` call rather than
several separate calls with a fake clock advanced between them (that would
reset the lease each time, unlike the on-disk address seen-set the rest of
this module's tests rely on). ``sleep=clock.advance`` is the trick: passing
the fake clock's own advance method AS the sleep hook lets the loop's
between-cycle ``sleep(config.interval_s)`` calls move fake time forward
exactly like a real wait would, with no actual waiting.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

from nexus.db.t2.http_tuple_store import HttpTupleStore
from nexus.tuple_watch import (
    DIRECTORY_RETENTION_S,
    WatchConfig,
    run_watch,
    write_session_marker,
)


def _uniq(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:10]}"


class _Clock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _FakeDirectoryStore:
    """Records every ``out()`` call; ``rd()`` always returns no mail, so
    ``run_watch``'s per-address probe loop (required -- it refuses an empty
    address list) stays silent and only the directory lease logic is under
    test.
    """

    def __init__(self, fail_times: int = 0, fail_error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._fail_times = fail_times
        self._fail_error = fail_error or RuntimeError("engine unreachable")

    def out(self, subspace, keys, dims=None, body=None, *, nonce=None, ttl_seconds=None):  # noqa: ANN001, ANN201 — test double
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._fail_error
        self.calls.append({
            "subspace": subspace, "keys": dict(keys), "dims": dict(dims or {}),
            "nonce": nonce, "ttl_seconds": ttl_seconds,
        })
        return "fake-directory-id"

    def rd(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003, ANN201 — test double
        return []


def _run(
    store, cfg: WatchConfig, sd: Path, clock: _Clock, iterations: int, lines: list, reports: list,
    *, name: str | None, session_id: str | None, pid: int | None = 4242,
    claude_pid: int | None = None, spawn_session_id: str | None = None,
    sleep=None,
):
    return run_watch(
        store, [_uniq("addr")], config=cfg, state_dir=sd, iterations=iterations,
        emit=lines.append, report=reports.append, now=clock.now,
        sleep=sleep if sleep is not None else clock.advance,
        claude_pid=claude_pid, spawn_session_id=spawn_session_id,
        directory_name=name, directory_session_id=session_id, directory_pid=pid,
    )


class TestDirectoryLeaseUnit:
    def test_arm_writes_one_out_with_ttl_300_and_the_arm_nonce(self, tmp_path) -> None:
        store = _FakeDirectoryStore()
        clock = _Clock()
        lines, reports = [], []
        name, session_id = "nexus-19", "S1"
        _run(store, WatchConfig(interval_s=10.0), tmp_path, clock, 1, lines, reports,
             name=name, session_id=session_id, pid=4242)
        assert len(store.calls) == 1
        call = store.calls[0]
        assert call["subspace"] == f"directory/{name}"
        assert call["keys"] == {"name": name}
        assert call["dims"] == {"session_id": session_id}
        assert call["ttl_seconds"] == 300
        # nonce: finer than one second and carries the pid
        assert call["nonce"].endswith("-4242")
        assert float(call["nonce"].rsplit("-", 1)[0]) == pytest.approx(clock.t)

    def test_resends_every_60s_with_the_same_nonce_and_nothing_in_between(self, tmp_path) -> None:
        store = _FakeDirectoryStore()
        clock = _Clock()
        lines, reports = [], []
        # interval=10s, default heartbeat=60s: the 7th cycle (t=60) is the
        # first one due for a re-send; six intermediate cycles must be silent.
        _run(store, WatchConfig(interval_s=10.0), tmp_path, clock, 7, lines, reports,
             name="nexus-19", session_id="S1")
        assert len(store.calls) == 2
        assert store.calls[0]["nonce"] == store.calls[1]["nonce"]
        assert store.calls[1]["ttl_seconds"] == 300

    def test_renonces_at_the_retention_boundary(self, tmp_path) -> None:
        store = _FakeDirectoryStore()
        clock = _Clock()
        lines, reports = [], []
        ttl = 10.0
        boundary = DIRECTORY_RETENTION_S - ttl  # arm_time is 0 (relative to this clock)
        cfg = WatchConfig(interval_s=boundary, directory_ttl_s=ttl, directory_heartbeat_s=1.0)
        _run(store, cfg, tmp_path, clock, 2, lines, reports, name="nexus-19", session_id="S1")
        assert len(store.calls) == 2
        assert store.calls[1]["nonce"] != store.calls[0]["nonce"]
        assert store.calls[1]["ttl_seconds"] == int(ttl)

    def test_a_failing_out_is_reported_once_per_window_and_probing_continues(self, tmp_path) -> None:
        store = _FakeDirectoryStore(fail_times=1000)  # always fails
        clock = _Clock()
        lines, reports = [], []
        cfg = WatchConfig(interval_s=1.0, directory_heartbeat_s=0.5, error_report_every_s=300.0)
        stats = _run(store, cfg, tmp_path, clock, 5, lines, reports, name="nexus-19", session_id="S1")
        assert stats.cycles == 5  # the loop never aborts on a directory failure
        failed = [line for line in lines if "directory/nexus-19 lease failed" in line]
        assert len(failed) == 1
        assert "engine unreachable" in failed[0]

    def test_no_instance_and_a_positional_address_write_nothing(self, tmp_path) -> None:
        store = _FakeDirectoryStore()
        clock = _Clock()
        lines, reports = [], []
        _run(store, WatchConfig(interval_s=1.0), tmp_path, clock, 5, lines, reports,
             name=None, session_id=None)
        assert store.calls == []

        # Also: a session id with no name (e.g. --instance omitted) is a no-op.
        _run(store, WatchConfig(interval_s=1.0), tmp_path, clock, 5, lines, reports,
             name=None, session_id="S1")
        assert store.calls == []

    def test_the_marker_mismatch_self_stop_sends_ttl_1_with_the_current_nonce_after_a_rotation(
        self, tmp_path,
    ) -> None:
        store = _FakeDirectoryStore()
        clock = _Clock()
        lines, reports = [], []
        claude_pid = 4242
        ttl = 10.0
        boundary = DIRECTORY_RETENTION_S - ttl
        cfg = WatchConfig(interval_s=boundary, directory_ttl_s=ttl, directory_heartbeat_s=1.0)

        # Cycle 1 (t=0): arm. Cycle 2 (t=boundary): re-nonce (rotates). The sleep
        # hook writes the mismatching marker only after the SECOND sleep, so the
        # rotation has already happened by the time cycle 3 reads the marker.
        sleeps = {"n": 0}

        def sleep_then_mismatch(seconds: float) -> None:
            clock.advance(seconds)
            sleeps["n"] += 1
            if sleeps["n"] == 2:
                write_session_marker(tmp_path, claude_pid, "S2")

        stats = _run(
            store, cfg, tmp_path, clock, 3, lines, reports,
            name="nexus-19", session_id="S1", claude_pid=claude_pid, spawn_session_id="S1",
            sleep=sleep_then_mismatch,
        )
        assert stats.cycles == 2  # cycle 3 stops before incrementing
        assert len(store.calls) == 3  # arm, rotate-resend, release
        arm_nonce, rotated_nonce, release_nonce = (c["nonce"] for c in store.calls)
        assert rotated_nonce != arm_nonce
        assert release_nonce == rotated_nonce  # the CURRENT nonce, not the arm one
        assert store.calls[2]["ttl_seconds"] == 1
        assert any("STOP" in line for line in lines)

    def test_a_normal_exit_or_sigterm_sends_no_release(self, tmp_path) -> None:
        """SIGTERM is not simulable from inside the loop; a plain exit
        (iterations exhausted) is the same code path this module's own
        docstring names ("Do NOT release on process exit or SIGTERM"): no
        release call rides along with a normal return, only the arm/heartbeat
        writes."""
        store = _FakeDirectoryStore()
        clock = _Clock()
        lines, reports = [], []
        stats = _run(store, WatchConfig(interval_s=10.0), tmp_path, clock, 3, lines, reports,
                      name="nexus-19", session_id="S1")
        assert stats.cycles == 3
        assert all(c["ttl_seconds"] == 300 for c in store.calls)
        assert not any(c["ttl_seconds"] == 1 for c in store.calls)

    def test_no_resolvable_claude_pid_means_no_self_stop_and_no_release(self, tmp_path) -> None:
        store = _FakeDirectoryStore()
        clock = _Clock()
        lines, reports = [], []
        write_session_marker(tmp_path, 4242, "S2")  # would mismatch, if read
        # claude_pid=None (default) and no ancestor process to resolve it from
        # a real environment -- spawn_session_id alone never triggers the
        # self-stop check, exactly like the pre-existing marker tests.
        stats = _run(store, WatchConfig(interval_s=10.0), tmp_path, clock, 3, lines, reports,
                      name="nexus-19", session_id="S1", pid=4242,
                      claude_pid=None, spawn_session_id=None)
        assert stats.cycles == 3
        assert not any(c["ttl_seconds"] == 1 for c in store.calls)
        assert not any("STOP" in line for line in lines)

    def test_two_watchers_of_one_session_arming_in_the_same_second_get_distinct_nonces(
        self, tmp_path,
    ) -> None:
        store_a, store_b = _FakeDirectoryStore(), _FakeDirectoryStore()
        clock = _Clock()  # both watchers share the same instant
        lines_a, reports_a, lines_b, reports_b = [], [], [], []
        _run(store_a, WatchConfig(interval_s=10.0), tmp_path, clock, 1, lines_a, reports_a,
             name="nexus-19", session_id="S1", pid=111)
        _run(store_b, WatchConfig(interval_s=10.0), tmp_path, clock, 1, lines_b, reports_b,
             name="nexus-19", session_id="S1", pid=222)
        assert store_a.calls[0]["nonce"] != store_b.calls[0]["nonce"]


class TestDirectoryLeaseIntegration:
    """Real engine substrate: the directory/<name> template from RDR-208
    Phase 1 Step 1, with test-only short ttl/heartbeat overrides."""

    def test_after_arm_rd_returns_one_live_row_with_this_session_id(
        self, t2_service_env, tmp_path,
    ) -> None:
        store = HttpTupleStore()
        name, session_id = _uniq("agent"), _uniq("S")
        cfg = WatchConfig(interval_s=0.1, directory_ttl_s=5.0, directory_heartbeat_s=60.0)
        run_watch(
            store, [_uniq("addr")], config=cfg, state_dir=tmp_path, iterations=1,
            emit=lambda _s: None, report=lambda _s: None,
            directory_name=name, directory_session_id=session_id, directory_pid=os.getpid(),
        )
        rows = store.rd(f"directory/{name}", {"name": name}, n=10)
        assert len(rows) == 1
        assert (rows[0].dims or {}).get("session_id") == session_id

    def test_resends_keep_exactly_one_row(self, t2_service_env, tmp_path) -> None:
        store = HttpTupleStore()
        name, session_id = _uniq("agent"), _uniq("S")
        cfg = WatchConfig(interval_s=0.2, directory_ttl_s=5.0, directory_heartbeat_s=0.1)
        run_watch(
            store, [_uniq("addr")], config=cfg, state_dir=tmp_path, iterations=5,
            emit=lambda _s: None, report=lambda _s: None,
            directory_name=name, directory_session_id=session_id, directory_pid=os.getpid(),
        )
        rows = store.rd(f"directory/{name}", {"name": name}, n=10)
        assert len(rows) == 1

    def test_self_stop_releases_the_entry(self, t2_service_env, tmp_path) -> None:
        store = HttpTupleStore()
        name, session_id = _uniq("agent"), _uniq("S")
        claude_pid = 424242
        cfg = WatchConfig(interval_s=0.3, directory_ttl_s=5.0, directory_heartbeat_s=60.0)
        marked = {"done": False}

        def sleep_then_mismatch(seconds: float) -> None:
            time.sleep(seconds)
            if not marked["done"]:
                marked["done"] = True
                write_session_marker(tmp_path, claude_pid, "OTHER-SESSION")

        run_watch(
            store, [_uniq("addr")], config=cfg, state_dir=tmp_path, iterations=5,
            emit=lambda _s: None, report=lambda _s: None, sleep=sleep_then_mismatch,
            claude_pid=claude_pid, spawn_session_id=session_id,
            directory_name=name, directory_session_id=session_id, directory_pid=os.getpid(),
        )
        time.sleep(1.5)  # the release's ttl_seconds=1 needs to actually lapse
        rows = store.rd(f"directory/{name}", {"name": name}, n=10)
        assert not rows

    def test_plain_stop_leaves_it_to_lapse_within_one_ttl(self, t2_service_env, tmp_path) -> None:
        store = HttpTupleStore()
        name, session_id = _uniq("agent"), _uniq("S")
        cfg = WatchConfig(interval_s=0.1, directory_ttl_s=1.0, directory_heartbeat_s=60.0)
        run_watch(
            store, [_uniq("addr")], config=cfg, state_dir=tmp_path, iterations=1,
            emit=lambda _s: None, report=lambda _s: None,
            directory_name=name, directory_session_id=session_id, directory_pid=os.getpid(),
        )
        rows = store.rd(f"directory/{name}", {"name": name}, n=10)
        assert len(rows) == 1  # present right after a plain stop -- no release fired

        time.sleep(1.5)
        rows = store.rd(f"directory/{name}", {"name": name}, n=10)
        assert not rows  # lapsed on its own, within one TTL
