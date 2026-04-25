"""AC1: Session ID is a valid UUID4, written to and readable from a PID-scoped file."""
import json
import os
import re
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.session import (
    _stable_pid,
    find_ancestor_session,
    find_session_by_id,
    generate_session_id,
    read_session_id,
    sweep_stale_sessions,
    write_claude_session_id,
    write_session_file,
    write_session_record,
    write_session_record_by_id,
)


def test_generate_session_id_is_uuid4() -> None:
    sid = generate_session_id()
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", sid)


def test_generate_session_id_unique() -> None:
    assert generate_session_id() != generate_session_id()


def test_write_and_read_session_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = generate_session_id()

    path = write_session_file(sid, ppid=99999)
    assert path.exists()
    assert path.read_text() == sid

    recovered = read_session_id(ppid=99999)
    assert recovered == sid


def test_read_session_id_missing_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert read_session_id(ppid=99998) is None


def test_session_file_is_pid_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    write_session_file("session-a", ppid=1001)
    write_session_file("session-b", ppid=1002)

    assert read_session_id(ppid=1001) == "session-a"
    assert read_session_id(ppid=1002) == "session-b"


# ── Behavior 1: _stable_pid() env var path ────────────────────────────────────

def test_stable_pid_env_var_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """When NX_SESSION_PID is set, _stable_pid() returns that value and ignores getsid(0)."""
    monkeypatch.setenv("NX_SESSION_PID", "77777")
    with patch("nexus.session.os.getsid", return_value=99999):
        result = _stable_pid()
    assert result == 77777


# ── Behavior 2: _stable_pid() getsid fallback ────────────────────────────────

def test_stable_pid_falls_back_to_getsid(monkeypatch: pytest.MonkeyPatch) -> None:
    """When NX_SESSION_PID is unset, _stable_pid() returns os.getsid(0)."""
    monkeypatch.delenv("NX_SESSION_PID", raising=False)
    with patch("nexus.session.os.getsid", return_value=55555) as mock_getsid:
        result = _stable_pid()
    assert result == 55555
    mock_getsid.assert_called_once_with(0)


# ── Behavior 3: _stable_pid() invalid env var falls back ─────────────────────

def test_stable_pid_invalid_env_var_falls_back_to_getsid(monkeypatch: pytest.MonkeyPatch) -> None:
    """When NX_SESSION_PID is non-integer, _stable_pid() silently falls back to getsid(0)."""
    monkeypatch.setenv("NX_SESSION_PID", "not-a-number")
    with patch("nexus.session.os.getsid", return_value=44444):
        result = _stable_pid()
    assert result == 44444


# ── write_session_record ──────────────────────────────────────────────────────

def test_write_session_record_creates_json_file(tmp_path: Path) -> None:
    """write_session_record writes a parseable JSON record at sessions/{ppid}.session."""
    sessions = tmp_path / "sessions"
    path = write_session_record(sessions, ppid=1234, session_id="uuid-abc",
                                host="127.0.0.1", port=51000, server_pid=9999, tmpdir="/tmp/x")
    assert path == sessions / "1234.session"
    record = json.loads(path.read_text())
    assert record["session_id"] == "uuid-abc"
    assert record["server_host"] == "127.0.0.1"
    assert record["server_port"] == 51000
    assert record["server_pid"] == 9999
    assert record["tmpdir"] == "/tmp/x"
    assert "created_at" in record


def test_write_session_record_mode_600(tmp_path: Path) -> None:
    """Session record file is created with permissions 0o600."""
    sessions = tmp_path / "sessions"
    path = write_session_record(sessions, ppid=1235, session_id="s",
                                host="127.0.0.1", port=1, server_pid=1)
    assert oct(path.stat().st_mode)[-3:] == "600"


# ── find_ancestor_session ─────────────────────────────────────────────────────

def test_find_ancestor_session_returns_none_when_no_files(tmp_path: Path) -> None:
    """No session files → returns None."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    result = find_ancestor_session(sessions_dir=sessions, start_pid=os.getpid())
    assert result is None


def test_find_ancestor_session_finds_immediate_ancestor(tmp_path: Path) -> None:
    """Finds a valid JSON record written for the current PID."""
    sessions = tmp_path / "sessions"
    pid = os.getpid()
    write_session_record(sessions, ppid=pid, session_id="found-it",
                         host="127.0.0.1", port=51001, server_pid=8888)
    result = find_ancestor_session(sessions_dir=sessions, start_pid=pid)
    assert result is not None
    assert result["session_id"] == "found-it"
    assert result["server_host"] == "127.0.0.1"
    assert result["server_port"] == 51001


def test_find_ancestor_session_ignores_stale_records(tmp_path: Path) -> None:
    """Records older than 24h are ignored (and the orphan is cleaned up)."""
    sessions = tmp_path / "sessions"
    pid = os.getpid()
    path = write_session_record(sessions, ppid=pid, session_id="stale",
                                host="127.0.0.1", port=51002, server_pid=99)
    # Backdate the created_at field
    record = json.loads(path.read_text())
    record["created_at"] = time.time() - (25 * 3600)
    path.write_text(json.dumps(record))

    result = find_ancestor_session(sessions_dir=sessions, start_pid=pid)
    assert result is None
    # Stale file should have been cleaned up
    assert not path.exists()


def test_find_ancestor_session_ignores_bare_string_files(tmp_path: Path) -> None:
    """Legacy bare-UUID session files (non-JSON) are skipped gracefully."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    pid = os.getpid()
    (sessions / f"{pid}.session").write_text("bare-uuid-not-json")
    result = find_ancestor_session(sessions_dir=sessions, start_pid=pid)
    assert result is None


# ── sweep_stale_sessions ──────────────────────────────────────────────────────

def test_sweep_stale_sessions_removes_old_records(tmp_path: Path) -> None:
    """Records older than max_age_hours are removed."""
    sessions = tmp_path / "sessions"
    path = write_session_record(sessions, ppid=5555, session_id="old",
                                host="127.0.0.1", port=51003, server_pid=101)
    record = json.loads(path.read_text())
    record["created_at"] = time.time() - (25 * 3600)
    path.write_text(json.dumps(record))

    sweep_stale_sessions(sessions_dir=sessions)
    assert not path.exists()


def test_sweep_stale_sessions_keeps_fresh_records(tmp_path: Path) -> None:
    """UUID-keyed records younger than max_age_hours are not removed.

    NB: Numeric-stem files are now swept *unconditionally* as a migration
    from the legacy PID-keyed scheme — see
    test_sweep_stale_sessions_removes_legacy_numeric_stem_unconditionally
    for that path. nexus-99jb added a liveness-based reap: records whose
    ``server_pid`` is dead are reaped regardless of age. Use the test's
    own PID as the stand-in so the record looks live.
    """
    import os as _os
    sessions = tmp_path / "sessions"
    path = write_session_record_by_id(
        sessions, "fresh-uuid",
        host="127.0.0.1", port=51004, server_pid=_os.getpid(),
    )

    sweep_stale_sessions(sessions_dir=sessions)
    assert path.exists()


def test_sweep_stale_sessions_noop_on_missing_dir(tmp_path: Path) -> None:
    """sweep_stale_sessions does not raise when sessions_dir does not exist."""
    sweep_stale_sessions(sessions_dir=tmp_path / "nonexistent")  # must not raise


def test_sweep_stale_sessions_skips_non_json_uuid_files(tmp_path: Path) -> None:
    """UUID-stem files with non-JSON content are left alone (no migration applies)."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    uuid_file = sessions / "abc-123-uuid.session"
    uuid_file.write_text("bare-uuid-not-json")
    sweep_stale_sessions(sessions_dir=sessions)  # must not raise
    assert uuid_file.exists()  # untouched: only json-parseable UUID files are evaluated for staleness


# ── UUID-keyed session records (current scheme; PID-keyed above is legacy) ──

def test_write_session_record_by_id_uses_uuid_filename(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    path = write_session_record_by_id(
        sessions,
        session_id="conv-uuid-1234",
        host="127.0.0.1",
        port=51234,
        server_pid=4321,
    )
    assert path.name == "conv-uuid-1234.session"
    assert path.exists()
    record = json.loads(path.read_text())
    assert record["session_id"] == "conv-uuid-1234"
    assert record["server_port"] == 51234


def test_find_session_by_id_explicit_id(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    write_session_record_by_id(sessions, "uuid-A", "127.0.0.1", 11111, 9001)
    write_session_record_by_id(sessions, "uuid-B", "127.0.0.1", 22222, 9002)

    rec_a = find_session_by_id(sessions, "uuid-A")
    assert rec_a is not None and rec_a["server_port"] == 11111

    rec_b = find_session_by_id(sessions, "uuid-B")
    assert rec_b is not None and rec_b["server_port"] == 22222


def test_find_session_by_id_returns_none_for_unknown(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    write_session_record_by_id(sessions, "exists", "127.0.0.1", 11111, 9001)
    assert find_session_by_id(sessions, "does-not-exist") is None


def test_find_session_by_id_falls_back_to_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When session_id is not passed explicitly, NX_SESSION_ID env wins."""
    sessions = tmp_path / "sessions"
    write_session_record_by_id(sessions, "from-env", "127.0.0.1", 33333, 9003)
    monkeypatch.setenv("NX_SESSION_ID", "from-env")

    rec = find_session_by_id(sessions)  # no explicit id — reads env
    assert rec is not None
    assert rec["server_port"] == 33333


def test_find_session_by_id_falls_back_to_flat_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When NX_SESSION_ID env is absent, fall back to current_session flat file."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("NX_SESSION_ID", raising=False)

    # Re-import-time path resolution honours the env var, but the module-level
    # CLAUDE_SESSION_FILE constant was set at import. Patch it for this test.
    from nexus import session as _session
    monkeypatch.setattr(_session, "CLAUDE_SESSION_FILE", tmp_path / "current_session")

    sessions = tmp_path / "sessions"
    write_session_record_by_id(sessions, "from-flat", "127.0.0.1", 44444, 9004)
    write_claude_session_id("from-flat")

    rec = find_session_by_id(sessions)
    assert rec is not None
    assert rec["server_port"] == 44444


def test_find_session_by_id_returns_none_when_no_id_resolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    from nexus import session as _session
    monkeypatch.setattr(_session, "CLAUDE_SESSION_FILE", tmp_path / "no_such_file")

    assert find_session_by_id(tmp_path / "sessions") is None


def test_two_uuids_same_dir_get_distinct_records(tmp_path: Path) -> None:
    """Bug-fix coverage: two Claude conversations launched from one terminal
    must end up with distinct session files and reachable independently.

    Pre-fix, both wrote to ~/.config/nexus/sessions/{login_shell_pid}.session
    and shared the same T1. Post-fix, the UUID is the key, so two distinct
    UUIDs produce two distinct files with no overlap.
    """
    sessions = tmp_path / "sessions"
    write_session_record_by_id(sessions, "claude-conv-1", "127.0.0.1", 11111, 9101)
    write_session_record_by_id(sessions, "claude-conv-2", "127.0.0.1", 22222, 9102)

    files = sorted(p.name for p in sessions.glob("*.session"))
    assert files == ["claude-conv-1.session", "claude-conv-2.session"]

    rec1 = find_session_by_id(sessions, "claude-conv-1")
    rec2 = find_session_by_id(sessions, "claude-conv-2")
    assert rec1 is not None and rec2 is not None
    assert rec1["server_port"] != rec2["server_port"]


# ── Migration: legacy numeric-stem files swept on first new-code SessionStart

def test_sweep_stale_sessions_removes_legacy_numeric_stem_unconditionally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Numeric-stem session files come from the legacy PID-keyed scheme that
    bound T1 to the terminal session. They never did the right thing —
    sweep removes them unconditionally on first new-code SessionStart,
    even if their timestamp is fresh.
    """
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    # Legacy PID-keyed file with a FRESH timestamp — would have survived the
    # 24h sweep policy under the old code path.
    legacy = sessions / "12345.session"
    import time as _time
    legacy.write_text(json.dumps({
        "session_id": "old-uuid",
        "server_host": "127.0.0.1",
        "server_port": 55555,
        "server_pid": 99999,
        "created_at": _time.time(),
        "tmpdir": "",
    }))

    # UUID-keyed file in the new format — should survive. Use this
    # process's own PID as server_pid so the nexus-99jb liveness-based
    # reap path sees a live anchor and leaves the record alone.
    import os as _os
    valid = sessions / "valid-uuid.session"
    valid.write_text(json.dumps({
        "session_id": "valid-uuid",
        "server_host": "127.0.0.1",
        "server_port": 66666,
        "server_pid": _os.getpid(),
        "created_at": _time.time(),
        "tmpdir": "",
    }))

    # No-op the chroma kill so the test doesn't try to signal PID 99999.
    monkeypatch.setattr("nexus.session.stop_t1_server", lambda _pid: None)

    sweep_stale_sessions(sessions_dir=sessions)

    assert not legacy.exists(), "legacy PID-keyed file should be swept regardless of age"
    assert valid.exists(), "UUID-keyed file should survive the sweep"


# ── nexus-99jb Layer 3: aggressive liveness-based reap ───────────────────────


def test_sweep_reaps_when_server_pid_is_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh record whose ``server_pid`` is no longer alive must be reaped
    regardless of age. This covers the canonical leak path: chroma process
    died, SessionEnd didn't fire, the record + tmpdir stayed behind.
    """
    sessions = tmp_path / "sessions"
    # Port any PID we know is dead. 1 is launchd/init on macOS — always
    # alive — so we use a synthetic large PID that the kernel will refuse
    # with ESRCH. Linux treats ``kill(99999999, 0)`` as ProcessLookupError
    # on an unused PID, and we monkeypatch _is_pid_alive to be certain.
    from nexus.session import write_session_record_by_id
    monkeypatch.setattr("nexus.session._is_pid_alive", lambda pid: False)
    monkeypatch.setattr("nexus.session.stop_t1_server", lambda _pid: None)

    path = write_session_record_by_id(
        sessions, "dead-server-uuid",
        host="127.0.0.1", port=57999, server_pid=7777,
    )
    assert path.exists()

    sweep_stale_sessions(sessions_dir=sessions)
    assert not path.exists(), "record with dead server_pid must be reaped eagerly"


def test_sweep_reaps_on_uuid_mismatch_with_live_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-886w: record's session_id doesn't match the current_session
    pointer AND claude_root_pid is alive → reap as 'uuid_mismatch'.

    This closes the same-claude-different-UUID leak the earlier
    nexus-99jb defense-in-depth layers did not cover (watchdog keeps
    seeing the parent PID alive, anchor_dead false, server_dead false,
    age below threshold → no reap trigger).
    """
    sessions = tmp_path / "sessions"
    from nexus.session import write_session_record_by_id

    # Both PIDs 'alive' via monkeypatch. The old session's UUID differs
    # from the current-session pointer — the only reap arm that should
    # fire is uuid_mismatch.
    own = os.getpid()
    monkeypatch.setattr("nexus.session._is_pid_alive", lambda _pid: True)
    monkeypatch.setattr("nexus.session.stop_t1_server", lambda _pid: None)
    monkeypatch.setattr(
        "nexus.session.read_claude_session_id",
        lambda: "current-uuid-after-clear",
    )

    path = write_session_record_by_id(
        sessions, "stale-uuid-pre-clear",
        host="127.0.0.1", port=58010, server_pid=own,
        claude_root_pid=own,
    )
    assert path.exists()

    sweep_stale_sessions(sessions_dir=sessions)
    assert not path.exists(), (
        "record with session_id != current_session must be reaped "
        "when claude_root_pid is alive (uuid_mismatch trigger)"
    )


def test_sweep_keeps_record_matching_current_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-886w: the current session's own record must NOT be reaped by
    the uuid_mismatch arm — the whole point of the trigger is to spare
    live sessions.
    """
    sessions = tmp_path / "sessions"
    from nexus.session import write_session_record_by_id

    own = os.getpid()
    monkeypatch.setattr("nexus.session._is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        "nexus.session.read_claude_session_id",
        lambda: "active-uuid",
    )

    path = write_session_record_by_id(
        sessions, "active-uuid",
        host="127.0.0.1", port=58011, server_pid=own,
        claude_root_pid=own,
    )
    sweep_stale_sessions(sessions_dir=sessions)
    assert path.exists(), "active session must survive sweep"


def test_sweep_keeps_record_when_current_session_pointer_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-886w: when read_claude_session_id returns None (flat file
    absent or unreadable), the uuid_mismatch arm must not fire — that
    would regress the pre-change behaviour to reap every session.
    """
    sessions = tmp_path / "sessions"
    from nexus.session import write_session_record_by_id

    own = os.getpid()
    monkeypatch.setattr("nexus.session._is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        "nexus.session.read_claude_session_id", lambda: None,
    )

    path = write_session_record_by_id(
        sessions, "some-uuid",
        host="127.0.0.1", port=58012, server_pid=own,
        claude_root_pid=own,
    )
    sweep_stale_sessions(sessions_dir=sessions)
    assert path.exists(), (
        "missing current_session pointer must not trigger uuid_mismatch"
    )


def test_sweep_uuid_mismatch_deferred_to_anchor_dead_when_claude_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-886w: when claude_root_pid is dead AND uuid also mismatches,
    the reap should be attributed to ``anchor_dead`` (the more specific
    reason), not ``uuid_mismatch``. Observability contract: logs must
    pin the primary failure mode.
    """
    sessions = tmp_path / "sessions"
    from nexus.session import write_session_record_by_id

    own = os.getpid()
    # server alive, anchor dead, uuid also mismatches.
    def _alive(pid: int) -> bool:
        return pid == own

    monkeypatch.setattr("nexus.session._is_pid_alive", _alive)
    monkeypatch.setattr("nexus.session.stop_t1_server", lambda _pid: None)
    monkeypatch.setattr(
        "nexus.session.read_claude_session_id", lambda: "new-uuid",
    )

    logged: list[dict] = []
    def _fake_info(event: str, **kw):  # noqa: ANN001
        kw["event"] = event
        logged.append(kw)

    monkeypatch.setattr("nexus.session._log.info", _fake_info)

    path = write_session_record_by_id(
        sessions, "old-uuid",
        host="127.0.0.1", port=58013, server_pid=own,
        claude_root_pid=999_999_999,  # dead
    )
    sweep_stale_sessions(sessions_dir=sessions)
    assert not path.exists()
    reasons = [e.get("reason") for e in logged if e.get("event") == "sweep_reaped_session"]
    assert "anchor_dead" in reasons, (
        "anchor_dead must win when both triggers apply; got "
        f"{reasons}"
    )
    assert "uuid_mismatch" not in reasons, (
        "specific reason (anchor_dead) must win over uuid_mismatch"
    )


def test_sweep_reaps_when_claude_root_pid_is_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record whose ``claude_root_pid`` anchor is dead gets reaped even if
    the chroma ``server_pid`` is still alive — the anchor loss means the
    session is effectively over (belt-and-braces for the case where
    Claude Code died hard and the watchdog also missed).
    """
    sessions = tmp_path / "sessions"
    from nexus.session import write_session_record_by_id

    # Live server_pid (us) but dead claude_root_pid (synthetic). Only the
    # anchor-dead liveness arm should fire.
    own = os.getpid()
    calls: list[int] = []
    def _fake_alive(pid: int) -> bool:
        calls.append(pid)
        return pid == own
    monkeypatch.setattr("nexus.session._is_pid_alive", _fake_alive)
    monkeypatch.setattr("nexus.session.stop_t1_server", lambda _pid: None)

    path = write_session_record_by_id(
        sessions, "dead-anchor-uuid",
        host="127.0.0.1", port=58001, server_pid=own,
        claude_root_pid=7654321,
    )
    assert path.exists()

    sweep_stale_sessions(sessions_dir=sessions)
    assert not path.exists(), "anchor-dead record must be reaped regardless of server liveness"


def test_write_session_record_persists_claude_root_pid_and_watchdog_pid(
    tmp_path: Path,
) -> None:
    """The record serializer carries both the claude_root_pid anchor and
    the watchdog's PID when the caller supplies them.
    """
    from nexus.session import write_session_record_by_id

    sessions = tmp_path / "sessions"
    path = write_session_record_by_id(
        sessions, "pid-roundtrip-uuid",
        host="127.0.0.1", port=58002, server_pid=1234,
        claude_root_pid=5678, watchdog_pid=9012,
    )
    record = json.loads(path.read_text())
    assert record["claude_root_pid"] == 5678
    assert record["watchdog_pid"] == 9012
    assert record["server_pid"] == 1234


def test_find_claude_root_pid_returns_ppid_fallback_when_no_claude_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no ancestor has a 'claude*' command name, the function returns
    the immediate PPID so the watchdog at least watches something.
    """
    from nexus.session import find_claude_root_pid

    monkeypatch.setattr("nexus.session._ppid_of", lambda pid: 42 if pid != 42 else 1)
    monkeypatch.setattr("nexus.session._command_name_of", lambda pid: "bash")

    # Our "ppid chain" is start_pid → 42 → 1 (init), none of which are named
    # claude, so we expect the immediate PPID (42) as the fallback.
    result = find_claude_root_pid(start_pid=100)
    assert result == 42


def test_find_claude_root_pid_prefers_claude_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an ancestor's command name starts with 'claude', return it."""
    from nexus.session import find_claude_root_pid

    # Chain: 100 → 200 (bash) → 300 (claude) → 1 (init)
    parents = {100: 200, 200: 300, 300: 1}
    names = {200: "bash", 300: "claude"}
    monkeypatch.setattr("nexus.session._ppid_of", lambda pid: parents.get(pid))
    monkeypatch.setattr(
        "nexus.session._command_name_of",
        lambda pid: names.get(pid, ""),
    )

    assert find_claude_root_pid(start_pid=100) == 300


def test_find_claude_root_pid_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Command-name match is case-insensitive (``Claude`` on some platforms)."""
    from nexus.session import find_claude_root_pid

    parents = {100: 200, 200: 1}
    names = {200: "Claude"}
    monkeypatch.setattr("nexus.session._ppid_of", lambda pid: parents.get(pid))
    monkeypatch.setattr(
        "nexus.session._command_name_of",
        lambda pid: names.get(pid, ""),
    )
    assert find_claude_root_pid(start_pid=100) == 200


# ── RDR-094 Phase 3: sweep_orphan_tmpdirs ───────────────────────────────────


class TestSweepOrphanTmpdirs:
    """RDR-094 Phase 3: reap nx_t1_* tmpdirs that no session record
    points at AND are older than max_age_hours. Closes Gap 3 (orphan
    tmpdirs from chroma crashes that the record-based sweep cannot
    see)."""

    def test_reaps_old_orphan_with_no_record(self, tmp_path: Path) -> None:
        from nexus.session import sweep_orphan_tmpdirs

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        tmpdir_root = tmp_path / "tmproot"
        tmpdir_root.mkdir()
        orphan = tmpdir_root / "nx_t1_orphan_xyz"
        orphan.mkdir()
        (orphan / "chroma.sqlite3").write_bytes(b"data")
        # Backdate 30 hours.
        old = time.time() - 30 * 3600
        os.utime(orphan, (old, old))

        reaped = sweep_orphan_tmpdirs(
            sessions_dir=sessions_dir, tmpdir_root=tmpdir_root,
        )
        assert reaped == 1
        assert not orphan.exists()

    def test_skips_recent_tmpdir(self, tmp_path: Path) -> None:
        """In-flight tmpdir (created moments ago, no record yet) must
        not be reaped. The 24h cutoff is the protection."""
        from nexus.session import sweep_orphan_tmpdirs

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        tmpdir_root = tmp_path / "tmproot"
        tmpdir_root.mkdir()
        recent = tmpdir_root / "nx_t1_recent"
        recent.mkdir()
        # mtime is now, well within the cutoff.

        reaped = sweep_orphan_tmpdirs(
            sessions_dir=sessions_dir, tmpdir_root=tmpdir_root,
        )
        assert reaped == 0
        assert recent.exists()

    def test_skips_tmpdir_referenced_by_session_record(
        self, tmp_path: Path,
    ) -> None:
        """A tmpdir mentioned in any session record must not be reaped
        even if its mtime is old."""
        from nexus.session import sweep_orphan_tmpdirs

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        tmpdir_root = tmp_path / "tmproot"
        tmpdir_root.mkdir()
        live = tmpdir_root / "nx_t1_live"
        live.mkdir()
        # Backdate; the record protects it regardless.
        old = time.time() - 48 * 3600
        os.utime(live, (old, old))

        record = {
            "session_id": "abc",
            "server_host": "127.0.0.1",
            "server_port": 1,
            "server_pid": 1,
            "tmpdir": str(live),
            "created_at": time.time(),
        }
        (sessions_dir / "abc.session").write_text(json.dumps(record))

        reaped = sweep_orphan_tmpdirs(
            sessions_dir=sessions_dir, tmpdir_root=tmpdir_root,
        )
        assert reaped == 0
        assert live.exists()

    def test_handles_corrupt_session_files(self, tmp_path: Path) -> None:
        """A corrupt session file must not abort the sweep; the orphan
        next to it should still be reaped."""
        from nexus.session import sweep_orphan_tmpdirs

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "broken.session").write_text("{not-json")
        tmpdir_root = tmp_path / "tmproot"
        tmpdir_root.mkdir()
        orphan = tmpdir_root / "nx_t1_orphan"
        orphan.mkdir()
        old = time.time() - 30 * 3600
        os.utime(orphan, (old, old))

        reaped = sweep_orphan_tmpdirs(
            sessions_dir=sessions_dir, tmpdir_root=tmpdir_root,
        )
        assert reaped == 1
        assert not orphan.exists()

    def test_handles_missing_tmpdir_root(self, tmp_path: Path) -> None:
        from nexus.session import sweep_orphan_tmpdirs

        reaped = sweep_orphan_tmpdirs(
            sessions_dir=tmp_path,
            tmpdir_root=tmp_path / "does-not-exist",
        )
        assert reaped == 0

    def test_ignores_non_nx_t1_directories(self, tmp_path: Path) -> None:
        """Only nx_t1_* prefixed dirs are candidates. Other tmpdirs
        from other tools are safe."""
        from nexus.session import sweep_orphan_tmpdirs

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        tmpdir_root = tmp_path / "tmproot"
        tmpdir_root.mkdir()
        unrelated = tmpdir_root / "tmpXYZ_other"
        unrelated.mkdir()
        old = time.time() - 30 * 3600
        os.utime(unrelated, (old, old))

        reaped = sweep_orphan_tmpdirs(
            sessions_dir=sessions_dir, tmpdir_root=tmpdir_root,
        )
        assert reaped == 0
        assert unrelated.exists()

    def test_reaps_multiple_old_orphans(self, tmp_path: Path) -> None:
        from nexus.session import sweep_orphan_tmpdirs

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        tmpdir_root = tmp_path / "tmproot"
        tmpdir_root.mkdir()
        old = time.time() - 48 * 3600
        for n in range(3):
            d = tmpdir_root / f"nx_t1_o{n}"
            d.mkdir()
            os.utime(d, (old, old))

        reaped = sweep_orphan_tmpdirs(
            sessions_dir=sessions_dir, tmpdir_root=tmpdir_root,
        )
        assert reaped == 3

    def test_uses_system_tempdir_when_root_unspecified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When tmpdir_root is None, fall back to tempfile.gettempdir()."""
        import tempfile as _tempfile

        from nexus.session import sweep_orphan_tmpdirs

        monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(tmp_path))
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        orphan = tmp_path / "nx_t1_o"
        orphan.mkdir()
        old = time.time() - 30 * 3600
        os.utime(orphan, (old, old))

        reaped = sweep_orphan_tmpdirs(sessions_dir=sessions_dir)
        assert reaped == 1
