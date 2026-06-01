# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-141 P1 (RED): version-skew double-writer at the t2_index_write A1 boundary.

`t2_index_write` decides reachability with an up-front `database.hello()`
probe. The probe can fail two ways, with OPPOSITE single-writer implications:

* `T2DaemonNotReachableError` — no daemon writer exists. Degrading to a
  direct `T2Database` is safe (the RDR-128 documented availability fallback).
* `T2SchemaVersionMismatchError` — a stale-version daemon is ALIVE, holds the
  spawn lock, and is actively serving writes. Opening a direct `T2Database`
  here produces a SECOND live writer on `memory.db` — the version-skew
  double-writer this RDR closes.

The fix (RDR-141): split the conflated `except`. The unreachable arm is
unchanged; the version-mismatch arm RE-ASSERTS the supervisor (reap the stale
daemon, spawn a current one) via `mcp_infra._reassert_t2_daemon` and retries
through the fresh daemon, falling back to a direct write only when the
re-assert cannot produce a reachable current daemon.

These tests are RED against the conflated `except` (which opens a direct
writer on mismatch and has no `_reassert_t2_daemon` seam) and GREEN once P2
splits the arms.
"""
from __future__ import annotations

from pathlib import Path

import pytest


class _MismatchThenOkProxy:
    """`database` proxy: first `hello()` raises version-mismatch, then OK.

    Models the stale daemon being reaped + a current daemon spawned: the
    second client (post-re-assert) probes a healthy current daemon.
    """

    def __init__(self) -> None:
        self.calls = 0

    def hello(self) -> dict:
        self.calls += 1
        if self.calls == 1:
            from nexus.daemon.t2_client import T2SchemaVersionMismatchError

            raise T2SchemaVersionMismatchError(
                client_version="5.6.0", daemon_version="5.4.4"
            )
        return {"daemon_schema_version": "9.9.9"}


def _no_direct_open(mi, monkeypatch) -> None:
    """Fail the test if the direct-T2Database fallback path is taken."""
    monkeypatch.setattr(
        mi,
        "default_db_path",
        lambda: pytest.fail(
            "version-skew double-writer: opened a direct T2Database while a "
            "stale daemon is alive — the re-assert path must be taken instead"
        ),
    )


def test_version_mismatch_reasserts_supervisor_not_direct_writer(
    monkeypatch, tmp_path: Path,
) -> None:
    """On schema-version mismatch, t2_index_write re-asserts the supervisor
    and routes through the fresh daemon — it must NOT open a direct writer
    while the stale daemon is alive."""
    import nexus.daemon.t2_client as t2c
    import nexus.mcp_infra as mi

    proxy = _MismatchThenOkProxy()

    class _Store:
        def write(self) -> str:
            return "wrote-through-daemon"

    class _Client:
        def __init__(self) -> None:
            self.database = proxy
            self.store = _Store()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _Client())

    # The re-assert seam: simulate a successful reap+spawn of a current daemon.
    reassert_calls: list[str] = []

    def _fake_reassert() -> bool:
        reassert_calls.append("called")
        return True

    monkeypatch.setattr(mi, "_reassert_t2_daemon", _fake_reassert, raising=False)

    # If the mismatch arm degrades to a direct writer, this fails the test.
    _no_direct_open(mi, monkeypatch)

    from nexus.mcp_infra import t2_index_write

    result = t2_index_write(lambda db: db.store.write())

    assert reassert_calls == ["called"], "must re-assert the supervisor on version mismatch"
    assert proxy.calls == 2, "must re-probe the fresh daemon after re-assert"
    assert result == "wrote-through-daemon", "write must route through the current daemon"


def test_unreachable_arm_still_degrades_to_direct(
    monkeypatch, tmp_path: Path,
) -> None:
    """The daemon-unreachable arm is unchanged: degrade to a direct
    T2Database and never invoke the supervisor re-assert."""
    import nexus.daemon.t2_client as t2c
    import nexus.mcp_infra as mi

    class _DeadProxy:
        def hello(self) -> dict:
            from nexus.daemon.t2_client import T2DaemonNotReachableError

            raise T2DaemonNotReachableError("daemon down")

    class _DeadClient:
        def __init__(self) -> None:
            self.database = _DeadProxy()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _DeadClient())

    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "default_db_path", lambda: db_path)

    reassert_calls: list[str] = []
    monkeypatch.setattr(
        mi,
        "_reassert_t2_daemon",
        lambda: reassert_calls.append("called") or True,
        raising=False,
    )

    from nexus.mcp_infra import t2_index_write

    seen: list[object] = []

    def _write(db) -> str:  # noqa: ANN001
        seen.append(type(db).__name__)
        return "direct-write-ok"

    result = t2_index_write(_write)

    assert reassert_calls == [], "unreachable arm must NOT re-assert the supervisor"
    assert result == "direct-write-ok"
    assert seen == ["T2Database"], "unreachable arm must write through a direct T2Database"


# ---------------------------------------------------------------------------
# P2 (nexus-tzeop): _reassert_t2_daemon outcome→event mapping, defensive
# SystemExit catch, and the single-attempt cap (no retry loop).
# ---------------------------------------------------------------------------


def _capture_warnings(monkeypatch) -> list[tuple[str, dict]]:
    """Capture structlog .warning(event, **kw) calls from _reassert_t2_daemon."""
    import structlog

    events: list[tuple[str, dict]] = []

    class _Log:
        def warning(self, event: str, **kw) -> None:  # noqa: ANN003
            events.append((event, kw))

        def info(self, *a, **k) -> None:  # noqa: ANN002, ANN003
            pass

    monkeypatch.setattr(structlog, "get_logger", lambda *a, **k: _Log())
    return events


def _patch_inner(monkeypatch, outcome) -> None:  # noqa: ANN001
    import nexus.commands.daemon as d

    monkeypatch.setattr(d, "_t2_ensure_running_inner", lambda **_kw: outcome)


def test_reassert_reachable_returns_true_without_warning(monkeypatch) -> None:
    from nexus.commands.daemon import T2EnsureOutcome

    _patch_inner(monkeypatch, T2EnsureOutcome.REACHABLE)
    events = _capture_warnings(monkeypatch)

    from nexus.mcp_infra import _reassert_t2_daemon

    assert _reassert_t2_daemon() is True
    assert events == [], "REACHABLE must not emit a warning"


@pytest.mark.parametrize(
    "outcome_name,expected_event",
    [
        ("DEFERRED_WRITE_LOCK", "t2_index_write_version_skew_cycle_deferred_writelock"),
        ("DEFERRED_SIGTERM", "t2_index_write_version_skew_cycle_deferred_sigterm"),
        ("CRASHLOOP_SUPPRESSED", "t2_index_write_version_skew_crashloop_down"),
        ("SPAWN_FAILED", "t2_index_write_version_skew_spawn_failed"),
    ],
)
def test_reassert_nonreachable_returns_false_with_distinct_event(
    monkeypatch, outcome_name: str, expected_event: str,
) -> None:
    from nexus.commands.daemon import T2EnsureOutcome

    _patch_inner(monkeypatch, getattr(T2EnsureOutcome, outcome_name))
    events = _capture_warnings(monkeypatch)

    from nexus.mcp_infra import _reassert_t2_daemon

    assert _reassert_t2_daemon() is False
    assert [e for e, _ in events] == [expected_event], (
        "each non-reachable outcome must emit exactly its own distinct event"
    )


def test_reassert_catches_systemexit_defensively(monkeypatch) -> None:
    """CA-3 belt-and-suspenders: even if the supervisor sys.exits, the
    re-assert must return False and never propagate SystemExit into the
    calling MCP process."""
    import nexus.commands.daemon as d

    def _boom(**_kw):  # noqa: ANN003
        raise SystemExit(1)

    monkeypatch.setattr(d, "_t2_ensure_running_inner", _boom)
    events = _capture_warnings(monkeypatch)

    from nexus.mcp_infra import _reassert_t2_daemon

    assert _reassert_t2_daemon() is False
    assert [e for e, _ in events] == ["t2_index_write_reassert_systemexit"]


def test_single_attempt_cap_falls_to_direct_when_reprobe_still_mismatches(
    monkeypatch, tmp_path: Path,
) -> None:
    """If the re-assert claims reachable but the re-probe STILL mismatches,
    t2_index_write must fall to a single direct write — no retry loop."""
    import nexus.daemon.t2_client as t2c
    import nexus.mcp_infra as mi

    class _AlwaysMismatchProxy:
        def __init__(self) -> None:
            self.calls = 0

        def hello(self) -> dict:
            self.calls += 1
            from nexus.daemon.t2_client import T2SchemaVersionMismatchError

            raise T2SchemaVersionMismatchError(
                client_version="5.6.0", daemon_version="5.4.4"
            )

    proxy = _AlwaysMismatchProxy()

    class _Client:
        def __init__(self) -> None:
            self.database = proxy
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _Client())
    # Re-assert claims a current daemon is up, but the re-probe will still
    # mismatch — exercising the single-attempt cap.
    monkeypatch.setattr(mi, "_reassert_t2_daemon", lambda: True, raising=False)

    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "default_db_path", lambda: db_path)

    events = _capture_warnings(monkeypatch)

    from nexus.mcp_infra import t2_index_write

    seen: list[str] = []

    def _write(db) -> str:  # noqa: ANN001
        seen.append(type(db).__name__)
        return "direct-after-cap"

    result = t2_index_write(_write)

    assert result == "direct-after-cap"
    assert seen == ["T2Database"], "must degrade to exactly one direct write"
    assert proxy.calls == 2, "exactly two hello() probes (initial + one re-probe), no loop"
    # The re-probe-failed sub-path must emit its OWN distinct event — NOT the
    # generic "start the daemon" banner (which would be wrong: D_old is up).
    assert [e for e, _ in events] == ["t2_index_write_version_skew_reprobe_failed"]


def test_all_nonreachable_outcomes_have_a_distinct_event() -> None:
    """Exhaustiveness guard: every non-REACHABLE T2EnsureOutcome maps to its
    own named event, so a future enum member can't silently fall through to
    the generic catch-all (L-1)."""
    from nexus.commands.daemon import T2EnsureOutcome

    # The events table in _reassert_t2_daemon, mirrored here as the contract.
    mapped = {
        T2EnsureOutcome.DEFERRED_WRITE_LOCK,
        T2EnsureOutcome.DEFERRED_SIGTERM,
        T2EnsureOutcome.CRASHLOOP_SUPPRESSED,
        T2EnsureOutcome.SPAWN_FAILED,
    }
    non_reachable = set(T2EnsureOutcome) - {T2EnsureOutcome.REACHABLE}
    assert non_reachable == mapped, (
        "a T2EnsureOutcome member is not mapped to a distinct event in "
        "_reassert_t2_daemon — add it to the events table (and its test row) "
        "rather than letting it hit the generic fallback"
    )


# ---------------------------------------------------------------------------
# P3 (nexus-9a5zy): cross-module integration through the REAL _reassert +
# _t2_ensure_running_inner, crash-loop-no-storm, and a concurrency-coverage
# note. The single-attempt cap is already covered above.
#
# Concurrency (CA-2: the election lock collapses concurrent client re-asserts
# to a single reap+spawn) is a PROCESS-level property — fcntl locks are
# per-process, so a thread test here would not exercise it. It is verified by
# the multi-process harness tests/daemon/test_t2_multistack_race.py and by
# CA-2 (T2 nexus_rdr/141-research-CA1-CA4). Not re-tested at this seam.
# ---------------------------------------------------------------------------


def test_integration_crashloop_falls_to_direct_with_down_event_no_storm(
    monkeypatch, tmp_path: Path,
) -> None:
    """Full path: t2_index_write -> real _reassert_t2_daemon -> real
    _t2_ensure_running_inner with the crash-loop guard pre-tripped and no live
    daemon. Asserts: (1) exactly one direct write, (2) the distinct
    crashloop_down event (NOT the generic banner), (3) no respawn storm —
    subprocess.Popen is never called even across repeated invocations."""
    import subprocess

    import nexus.commands.daemon as _daemon
    import nexus.daemon.t2_client as t2c
    import nexus.mcp_infra as mi

    # The client always reports a version mismatch (stale daemon alive).
    class _MismatchProxy:
        def hello(self) -> dict:
            from nexus.daemon.t2_client import T2SchemaVersionMismatchError

            raise T2SchemaVersionMismatchError(
                client_version="5.6.0", daemon_version="5.4.4"
            )

    class _Client:
        def __init__(self) -> None:
            self.database = _MismatchProxy()

        def close(self) -> None:
            pass

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _Client())

    # Real supervisor, pointed at an isolated config dir with NO daemon and the
    # crash-loop guard pre-tripped (no discovery file -> running is None ->
    # crash-loop guard fires -> CRASHLOOP_SUPPRESSED, no spawn).
    monkeypatch.setattr(_daemon, "nexus_config_dir", lambda: tmp_path)
    import time as _time

    now = _time.time()
    for _ in range(_daemon._CRASHLOOP_MAX_RESTARTS):
        _daemon._record_restart(tmp_path, now=now)

    spawned: list[object] = []
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **kw: spawned.append((a, kw)),  # records any (forbidden) spawn
    )

    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "default_db_path", lambda: db_path)

    events = _capture_warnings(monkeypatch)

    from nexus.mcp_infra import t2_index_write

    seen: list[str] = []

    def _write(db) -> str:  # noqa: ANN001
        seen.append(type(db).__name__)
        return "direct-ok"

    # Invoke repeatedly — a crash-loop-suppressed daemon must never be respawned.
    for _ in range(3):
        assert t2_index_write(_write) == "direct-ok"

    assert seen == ["T2Database", "T2Database", "T2Database"]
    assert spawned == [], "crash-loop guard must suppress all respawns (no storm)"
    # Every degraded call emits the distinct down-arm event, never the generic
    # 'start the daemon' banner (which would be wrong advice here).
    assert [e for e, _ in events] == [
        "t2_index_write_version_skew_crashloop_down"
    ] * 3
