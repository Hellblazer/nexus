# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-079 P2.2 — pool session lifecycle + PID-liveness reconciliation.

At first-operator-call the pool:
  1. Runs reconciliation: scan ``~/.config/nexus/sessions/pool-*.session``,
     probe each ``pool_pid`` via ``os.kill(pid, 0)``, remove dead entries,
     preserve live peers.
  2. Generates a pool UUID and starts a dedicated T1 HTTP server.
  3. Writes ``pool-<uuid>.session`` with ``pool_session=True``, ``pool_pid``
     set to ``os.getpid()``, and the T1 endpoint.

On graceful shutdown:
  1. Removes its own session file.
  2. Stops the T1 HTTP server.

Invariants validated: I-1 (pool T1 distinct from user T1), I-3 (no orphan
sessions after shutdown). SC-13 (3 sub-assertions: graceful stop, stale
reconcile, live peer preserve).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# ── Fixtures / helpers ─────────────────────────────────────────────────────


def _write_pool_record(
    sessions_dir: Path,
    session_id: str,
    pool_pid: int,
    host: str = "127.0.0.1",
    port: int = 54321,
    server_pid: int = 99999,
) -> Path:
    """Direct-write a pool session record bypassing the pool lifecycle,
    for reconciliation tests."""
    from nexus.session import write_session_record

    return write_session_record(
        sessions_dir=sessions_dir,
        ppid=0,
        session_id=session_id,
        host=host,
        port=port,
        server_pid=server_pid,
        pool_session=True,
        pool_pid=pool_pid,
    )


def _find_dead_pid() -> int:
    """Return a PID that is guaranteed not to exist.

    Spawn+reap a short-lived child and return its PID. The OS MAY recycle
    the PID, but within a test window the probability is negligible.
    """
    import subprocess
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


# ── Liveness probe ─────────────────────────────────────────────────────────


def test_probe_pid_alive_returns_true_for_self() -> None:
    from nexus.operators.pool import probe_pid_alive

    assert probe_pid_alive(os.getpid()) is True


def test_probe_pid_alive_returns_false_for_dead_pid() -> None:
    from nexus.operators.pool import probe_pid_alive

    dead = _find_dead_pid()
    assert probe_pid_alive(dead) is False


def test_probe_pid_alive_returns_false_for_zero() -> None:
    """PID 0 is the kernel swapper on Linux; not valid for os.kill here."""
    from nexus.operators.pool import probe_pid_alive

    # os.kill(0, 0) targets the whole process group — not what we want.
    # probe_pid_alive should reject zero.
    assert probe_pid_alive(0) is False


# ── Reconciliation ─────────────────────────────────────────────────────────


def test_reconcile_removes_stale_pool_session_with_dead_pid(
    tmp_path: Path,
) -> None:
    """SC-13(b): a pool-*.session whose pool_pid is dead must be removed."""
    from nexus.operators.pool import reconcile_stale_pool_sessions

    dead = _find_dead_pid()
    stale = _write_pool_record(tmp_path, "pool-dead-uuid", dead)
    assert stale.exists()

    removed = reconcile_stale_pool_sessions(tmp_path)
    assert not stale.exists()
    assert removed == 1


def test_reconcile_preserves_live_pool_session(tmp_path: Path) -> None:
    """SC-13(c): a pool-*.session whose pool_pid is alive must be preserved."""
    from nexus.operators.pool import reconcile_stale_pool_sessions

    live = _write_pool_record(tmp_path, "pool-live-uuid", os.getpid())
    assert live.exists()

    removed = reconcile_stale_pool_sessions(tmp_path)
    assert live.exists(), "live-PID session file must be preserved"
    assert removed == 0


def test_reconcile_never_touches_user_sessions(tmp_path: Path) -> None:
    """User session files (no pool_session marker) must be left alone even if
    their server_pid is dead — reconciliation is scoped to pool files only.
    Stale user sessions have their own handler, ``sweep_stale_sessions``.
    """
    from nexus.operators.pool import reconcile_stale_pool_sessions
    from nexus.session import write_session_record

    # User-shaped file (no pool_session marker, no pool_pid)
    user_file = write_session_record(
        sessions_dir=tmp_path,
        ppid=_find_dead_pid(),  # dead PPID, but irrelevant — we're testing scope
        session_id="user-xyz",
        host="127.0.0.1",
        port=12345,
        server_pid=_find_dead_pid(),
    )
    assert user_file.exists()

    reconcile_stale_pool_sessions(tmp_path)
    assert user_file.exists(), (
        "reconcile must never touch user-session files"
    )


def test_reconcile_handles_corrupt_pool_session_file(tmp_path: Path) -> None:
    """A corrupt pool-*.session is treated as stale and removed."""
    from nexus.operators.pool import reconcile_stale_pool_sessions

    corrupt = tmp_path / "pool-corrupt.session"
    corrupt.write_text("{{ not valid")
    assert corrupt.exists()

    reconcile_stale_pool_sessions(tmp_path)
    assert not corrupt.exists(), "corrupt pool session file must be removed"


def test_reconcile_mixed_files_only_touches_pool(tmp_path: Path) -> None:
    """End-to-end: dead pool + live pool + user session in same dir.
    Only the dead pool file gets removed."""
    from nexus.operators.pool import reconcile_stale_pool_sessions
    from nexus.session import write_session_record

    dead_file = _write_pool_record(tmp_path, "pool-dead", _find_dead_pid())
    live_file = _write_pool_record(tmp_path, "pool-live", os.getpid())
    user_file = write_session_record(
        sessions_dir=tmp_path, ppid=9999,
        session_id="user-xyz", host="127.0.0.1", port=1, server_pid=_find_dead_pid(),
    )

    reconcile_stale_pool_sessions(tmp_path)

    assert not dead_file.exists()
    assert live_file.exists()
    assert user_file.exists()


# ── create_pool_session / teardown_pool_session (SC-13(a)) ─────────────────


def test_create_pool_session_writes_file_with_pool_marker(
    tmp_path: Path, monkeypatch,
) -> None:
    """create_pool_session generates a pool-<uuid>.session with
    pool_session=True and pool_pid=os.getpid(). The T1 server start is
    stubbed via a fake returning (host, port, pid, tmpdir)."""
    from nexus.operators import pool as pool_mod

    fake_start = lambda: ("127.0.0.1", 65432, 11111, "/tmp/nx_t1_fake")  # noqa: E731
    monkeypatch.setattr(pool_mod, "_start_t1_server", fake_start)

    session = pool_mod.create_pool_session(tmp_path)
    assert session.session_id.startswith("pool-")
    assert session.pool_pid == os.getpid()
    assert session.host == "127.0.0.1"
    assert session.port == 65432

    session_file = tmp_path / f"{session.session_id}.session"
    assert session_file.exists()
    data = json.loads(session_file.read_text())
    assert data["pool_session"] is True
    assert data["pool_pid"] == os.getpid()
    assert data["session_id"] == session.session_id


def test_teardown_pool_session_removes_file_and_stops_server(
    tmp_path: Path, monkeypatch,
) -> None:
    """SC-13(a): teardown_pool_session removes the session file AND calls
    stop_t1_server on the recorded server_pid."""
    from nexus.operators import pool as pool_mod

    fake_start = lambda: ("127.0.0.1", 65432, 11111, "/tmp/nx_t1_fake")  # noqa: E731
    stopped: list[int] = []
    monkeypatch.setattr(pool_mod, "_start_t1_server", fake_start)
    monkeypatch.setattr(pool_mod, "_stop_t1_server", lambda pid: stopped.append(pid))

    session = pool_mod.create_pool_session(tmp_path)
    session_file = tmp_path / f"{session.session_id}.session"
    assert session_file.exists()

    pool_mod.teardown_pool_session(session, tmp_path)
    assert not session_file.exists()
    assert stopped == [11111], "stop_t1_server must be called on the server PID"


def test_teardown_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    """Calling teardown twice does not raise — shutdown can be triggered
    from multiple paths (atexit, explicit stop, signal handler)."""
    from nexus.operators import pool as pool_mod

    fake_start = lambda: ("127.0.0.1", 65432, 11111, "/tmp/nx_t1_fake")  # noqa: E731
    monkeypatch.setattr(pool_mod, "_start_t1_server", fake_start)
    monkeypatch.setattr(pool_mod, "_stop_t1_server", lambda pid: None)

    session = pool_mod.create_pool_session(tmp_path)
    pool_mod.teardown_pool_session(session, tmp_path)
    # Second call: must not raise
    pool_mod.teardown_pool_session(session, tmp_path)


# ── create_pool_session runs reconciliation FIRST ──────────────────────────


def test_create_pool_session_reconciles_before_writing(
    tmp_path: Path, monkeypatch,
) -> None:
    """Pool startup must reconcile stale peers BEFORE writing its own file
    so the new pool's file is never mistaken for a peer during the scan."""
    from nexus.operators import pool as pool_mod

    # Seed a dead-pid stale file
    dead = _find_dead_pid()
    stale = _write_pool_record(tmp_path, "pool-stale", dead)

    fake_start = lambda: ("127.0.0.1", 65432, 11111, "/tmp/nx_t1_fake")  # noqa: E731
    monkeypatch.setattr(pool_mod, "_start_t1_server", fake_start)

    session = pool_mod.create_pool_session(tmp_path)
    # Stale file removed during startup; new file present
    assert not stale.exists()
    assert (tmp_path / f"{session.session_id}.session").exists()
