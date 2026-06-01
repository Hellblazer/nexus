# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #1048: t2_index_write daemon-unreachable diagnosis + rate-limiting.

Three problems fixed:
  (a) Misleading "start the daemon" hint when daemon IS alive but unresponsive.
  (b) No distinction between "daemon absent" vs "daemon alive but unreachable".
  (c) Per-write log spam — same warning emitted on every degraded write.

This test file pins all three fixes WITHOUT touching the RDR-141 version-skew
arm (T2SchemaVersionMismatchError), which is a separate code path.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_dead_client() -> object:
    """A T2Client stub whose hello() raises T2DaemonNotReachableError."""
    from nexus.daemon.t2_client import T2DaemonNotReachableError

    class _DB:
        def hello(self) -> None:
            raise T2DaemonNotReachableError("test: daemon not reachable")

    class _Client:
        database = _DB()

        def close(self) -> None:
            pass

    return _Client()


def _reset_warn_rate_limiter() -> None:
    """Reset the module-level rate-limiter state between tests via the
    production helper (acquires the lock; no direct global poking)."""
    import nexus.mcp_infra as mi
    mi._reset_warn_rate_limiter_for_tests()


@pytest.fixture(autouse=True)
def _reset_limiter():
    """Ensure a clean rate-limiter for every test in this module."""
    _reset_warn_rate_limiter()
    yield
    _reset_warn_rate_limiter()


# ── (a) + (b): distinct events for "alive but unresponsive" vs "absent" ──────

def test_alive_daemon_hello_fails_emits_distinct_alive_event(
    monkeypatch, tmp_path: Path,
) -> None:
    """When find_t2_daemon() returns a non-None payload (daemon IS alive) but
    hello() raises T2DaemonNotReachableError, the emitted event must be
    ``t2_index_write_daemon_unreachable_but_alive`` — NOT the generic
    ``t2_index_write_daemon_unreachable_fallback`` which carries the wrong
    "start the daemon" advice.
    """
    from structlog.testing import capture_logs

    import nexus.daemon.t2_client as t2c
    import nexus.daemon.discovery as disc
    import nexus.mcp_infra as mi

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _make_dead_client())
    # Daemon IS alive (discovery returns a payload with a live PID)
    monkeypatch.setattr(disc, "find_t2_daemon", lambda *_a, **_kw: {"pid": 12345, "status": "running"})
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "default_db_path", lambda: db_path)

    from nexus.mcp_infra import t2_index_write

    with capture_logs() as cap:
        t2_index_write(lambda db: None)

    events = [e["event"] for e in cap]
    assert "t2_index_write_daemon_unreachable_but_alive" in events, (
        "expected the alive-but-unresponsive event; got: %s" % events
    )
    assert "t2_index_write_daemon_unreachable_fallback" not in events, (
        "must NOT emit the 'start the daemon' banner when daemon is alive"
    )


def test_absent_daemon_hello_fails_emits_start_daemon_banner(
    monkeypatch, tmp_path: Path,
) -> None:
    """When find_t2_daemon() returns None (no daemon) and hello() raises
    T2DaemonNotReachableError, the original ``t2_index_write_daemon_unreachable_fallback``
    event with "start the daemon" advice must still fire.
    """
    from structlog.testing import capture_logs

    import nexus.daemon.t2_client as t2c
    import nexus.daemon.discovery as disc
    import nexus.mcp_infra as mi

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _make_dead_client())
    # No daemon: discovery returns None
    monkeypatch.setattr(disc, "find_t2_daemon", lambda *_a, **_kw: None)
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "default_db_path", lambda: db_path)

    from nexus.mcp_infra import t2_index_write

    with capture_logs() as cap:
        t2_index_write(lambda db: None)

    events = [e["event"] for e in cap]
    assert "t2_index_write_daemon_unreachable_fallback" in events, (
        "expected the 'start the daemon' banner for absent daemon; got: %s" % events
    )
    assert "t2_index_write_daemon_unreachable_but_alive" not in events, (
        "must NOT emit the alive event when daemon is absent"
    )


def test_alive_event_hint_does_not_say_start_daemon(
    monkeypatch, tmp_path: Path,
) -> None:
    """The hint in t2_index_write_daemon_unreachable_but_alive must NOT instruct
    the user to start the daemon (it is already running).  It should mention
    load, timeout, or contention.
    """
    from structlog.testing import capture_logs

    import nexus.daemon.t2_client as t2c
    import nexus.daemon.discovery as disc
    import nexus.mcp_infra as mi

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _make_dead_client())
    monkeypatch.setattr(disc, "find_t2_daemon", lambda *_a, **_kw: {"pid": 99, "status": "running"})
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "default_db_path", lambda: db_path)

    from nexus.mcp_infra import t2_index_write

    with capture_logs() as cap:
        t2_index_write(lambda db: None)

    alive_events = [e for e in cap if e.get("event") == "t2_index_write_daemon_unreachable_but_alive"]
    assert alive_events, "expected the alive event"
    hint = alive_events[0].get("hint", "")
    # Must NOT tell the user to "start" anything — the daemon is already running.
    assert "start" not in hint.lower(), (
        "hint must not tell user to start a daemon that is already running; got: %r" % hint
    )
    # Should mention load or timeout or contention
    hint_lower = hint.lower()
    assert any(kw in hint_lower for kw in ("load", "timeout", "contention", "unresponsive", "busy")), (
        "hint should explain WHY (load/timeout/contention); got: %r" % hint
    )


# ── (c): rate-limiting — no per-write spam ────────────────────────────────────

def test_rate_limiter_suppresses_repeated_warns_absent_daemon(
    monkeypatch, tmp_path: Path,
) -> None:
    """N consecutive writes with an absent daemon must emit exactly ONE warning
    (the first), not N.  The second call and beyond are suppressed within the
    rate window.
    """
    from structlog.testing import capture_logs

    import nexus.daemon.t2_client as t2c
    import nexus.daemon.discovery as disc
    import nexus.mcp_infra as mi

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _make_dead_client())
    monkeypatch.setattr(disc, "find_t2_daemon", lambda *_a, **_kw: None)
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "default_db_path", lambda: db_path)

    # Freeze time so all calls appear within the same rate window
    frozen_time = 1000.0
    monkeypatch.setattr(mi.time, "monotonic", lambda: frozen_time)

    from nexus.mcp_infra import t2_index_write

    N = 10
    with capture_logs() as cap:
        for _ in range(N):
            t2_index_write(lambda db: None)

    unreachable_events = [
        e for e in cap
        if e.get("event") in (
            "t2_index_write_daemon_unreachable_fallback",
            "t2_index_write_daemon_unreachable_but_alive",
        )
    ]
    assert len(unreachable_events) == 1, (
        "expected exactly 1 warning for %d writes (rate-limited); got %d: %s"
        % (N, len(unreachable_events), unreachable_events)
    )


def test_rate_limiter_suppresses_repeated_warns_alive_daemon(
    monkeypatch, tmp_path: Path,
) -> None:
    """Same as above but for the alive-but-unresponsive path."""
    from structlog.testing import capture_logs

    import nexus.daemon.t2_client as t2c
    import nexus.daemon.discovery as disc
    import nexus.mcp_infra as mi

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _make_dead_client())
    monkeypatch.setattr(disc, "find_t2_daemon", lambda *_a, **_kw: {"pid": 42, "status": "running"})
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "default_db_path", lambda: db_path)

    frozen_time = 2000.0
    monkeypatch.setattr(mi.time, "monotonic", lambda: frozen_time)

    from nexus.mcp_infra import t2_index_write

    N = 8
    with capture_logs() as cap:
        for _ in range(N):
            t2_index_write(lambda db: None)

    unreachable_events = [
        e for e in cap
        if e.get("event") in (
            "t2_index_write_daemon_unreachable_fallback",
            "t2_index_write_daemon_unreachable_but_alive",
        )
    ]
    assert len(unreachable_events) == 1, (
        "expected exactly 1 warning for %d writes (rate-limited); got %d"
        % (N, len(unreachable_events))
    )


def test_rate_limiter_re_emits_after_window_expires(
    monkeypatch, tmp_path: Path,
) -> None:
    """After the rate window expires, the next write emits a fresh warning
    (with the accumulated suppressed count).
    """
    from structlog.testing import capture_logs

    import nexus.daemon.t2_client as t2c
    import nexus.daemon.discovery as disc
    import nexus.mcp_infra as mi

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _make_dead_client())
    monkeypatch.setattr(disc, "find_t2_daemon", lambda *_a, **_kw: None)
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "default_db_path", lambda: db_path)

    clock = [500.0]
    monkeypatch.setattr(mi.time, "monotonic", lambda: clock[0])

    from nexus.mcp_infra import t2_index_write

    # First window: 5 writes (1 emitted, 4 suppressed)
    with capture_logs() as cap1:
        for _ in range(5):
            t2_index_write(lambda db: None)

    assert len([e for e in cap1 if "unreachable" in e.get("event", "")]) == 1

    # Advance time past the rate window
    clock[0] += mi._WARN_RATE_LIMIT_SECS + 1.0

    # Second window: 1 write should emit again
    with capture_logs() as cap2:
        t2_index_write(lambda db: None)

    emits2 = [e for e in cap2 if "unreachable" in e.get("event", "")]
    assert len(emits2) == 1, "expected re-emit after window expired"


def test_suppressed_count_surfaced_on_next_emit(
    monkeypatch, tmp_path: Path,
) -> None:
    """When a new window begins after suppressed writes, the new emit must
    include a ``suppressed_count`` field showing how many were held back.
    """
    from structlog.testing import capture_logs

    import nexus.daemon.t2_client as t2c
    import nexus.daemon.discovery as disc
    import nexus.mcp_infra as mi

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _make_dead_client())
    monkeypatch.setattr(disc, "find_t2_daemon", lambda *_a, **_kw: None)
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "default_db_path", lambda: db_path)

    clock = [100.0]
    monkeypatch.setattr(mi.time, "monotonic", lambda: clock[0])

    from nexus.mcp_infra import t2_index_write

    # 4 suppressed writes in window 1
    for _ in range(5):  # 1 emitted + 4 suppressed
        t2_index_write(lambda db: None)

    # Advance to window 2
    clock[0] += mi._WARN_RATE_LIMIT_SECS + 1.0

    # The next emit should carry suppressed_count == 4
    with capture_logs() as cap:
        t2_index_write(lambda db: None)

    emits = [e for e in cap if "unreachable" in e.get("event", "")]
    assert len(emits) == 1
    assert emits[0].get("suppressed_count") == 4, (
        "expected suppressed_count=4 on second window emit; got: %s" % emits[0]
    )


# ── routing correctness: writes still land regardless of daemon state ─────────

def test_writes_route_to_direct_t2database_when_daemon_absent(
    monkeypatch, tmp_path: Path,
) -> None:
    """With an absent daemon, write_fn must still execute against a real
    T2Database (degraded path — functionality preserved).
    """
    import nexus.daemon.t2_client as t2c
    import nexus.daemon.discovery as disc
    import nexus.mcp_infra as mi

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _make_dead_client())
    monkeypatch.setattr(disc, "find_t2_daemon", lambda *_a, **_kw: None)
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "default_db_path", lambda: db_path)

    from nexus.mcp_infra import t2_index_write

    seen_types: list[str] = []

    def _write(db) -> None:
        seen_types.append(type(db).__name__)
        db.chash_index.upsert_many(chashes=["h1", "h2"], collection="code__c")

    t2_index_write(_write)

    assert seen_types == ["T2Database"], (
        "expected write via T2Database fallback; got: %s" % seen_types
    )
    from nexus.db.t2 import T2Database
    with T2Database(db_path) as db:
        assert db.chash_index.lookup("h1")
        assert db.chash_index.lookup("h2")


def test_writes_route_to_direct_t2database_when_daemon_alive_but_unreachable(
    monkeypatch, tmp_path: Path,
) -> None:
    """With a live-but-unresponsive daemon, write_fn must still execute via
    T2Database (degraded path — functionality preserved).
    """
    import nexus.daemon.t2_client as t2c
    import nexus.daemon.discovery as disc
    import nexus.mcp_infra as mi

    monkeypatch.setattr(t2c, "make_t2_client", lambda **_kw: _make_dead_client())
    monkeypatch.setattr(disc, "find_t2_daemon", lambda *_a, **_kw: {"pid": 1, "status": "running"})
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(mi, "default_db_path", lambda: db_path)

    from nexus.mcp_infra import t2_index_write

    seen_types: list[str] = []

    def _write(db) -> None:
        seen_types.append(type(db).__name__)
        db.chash_index.upsert_many(chashes=["h3"], collection="code__c")

    t2_index_write(_write)

    assert seen_types == ["T2Database"]
    from nexus.db.t2 import T2Database
    with T2Database(db_path) as db:
        assert db.chash_index.lookup("h3")
