# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-079 P2.3 — resolve_t1_session().

``resolve_t1_session()`` is the single entry point for T1 session
discovery. It checks ``NEXUS_T1_SESSION_ID`` env var first; on a matching
session file it returns that record directly. On a missing file it falls
through to the existing PPID-walk ``find_ancestor_session()``. On an
unset env var it behaves identically to the predecessor.

Invariants validated:
- I-1: pool workers (with ``NEXUS_T1_SESSION_ID=pool-<uuid>``) join the
  pool's T1 session, not the user's.
- I-4: with env unset, PPID-walk discovery is unchanged (no regression
  on RDR-078 T1 behavior) — SC-14(a).

Replaces ``find_ancestor_session()`` at four call sites (t1.py:140,
t1.py:175, hooks.py:91, hooks.py:153). This test module covers the
resolver itself; per-site adoption tests live in test_t1_resolve.py
and test_hooks_resolve.py.
"""
from __future__ import annotations

import json
from pathlib import Path


def _write_pool_record(sessions_dir: Path, session_id: str) -> Path:
    """Write a minimal pool session record for tests."""
    from nexus.session import write_session_record
    import os as _os

    return write_session_record(
        sessions_dir=sessions_dir,
        ppid=0,  # unused for pool
        session_id=session_id,
        host="127.0.0.1",
        port=54321,
        server_pid=99999,
        pool_session=True,
        pool_pid=_os.getpid(),
    )


# ── SC-14(a): env unset → falls through to PPID-walk (no regression) ──────


def test_resolve_falls_through_to_ppid_walk_when_env_unset(
    tmp_path: Path, monkeypatch,
) -> None:
    """When NEXUS_T1_SESSION_ID is absent, resolve_t1_session must behave
    identically to find_ancestor_session — the RDR-078 path. SC-14(a)."""
    from nexus import session as sess

    monkeypatch.delenv("NEXUS_T1_SESSION_ID", raising=False)
    monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)

    sentinel_record = {"session_id": "via-ppid", "server_host": "h", "server_port": 1, "server_pid": 1}
    monkeypatch.setattr(sess, "find_ancestor_session", lambda **kw: sentinel_record)

    result = sess.resolve_t1_session()
    assert result == sentinel_record, (
        "env unset must return find_ancestor_session() result verbatim"
    )


def test_resolve_returns_none_when_env_unset_and_no_ancestor(
    tmp_path: Path, monkeypatch,
) -> None:
    from nexus import session as sess

    monkeypatch.delenv("NEXUS_T1_SESSION_ID", raising=False)
    monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(sess, "find_ancestor_session", lambda **kw: None)

    assert sess.resolve_t1_session() is None


# ── env set + file present → return pool record ──────────────────────────


def test_resolve_returns_env_specified_pool_record(
    tmp_path: Path, monkeypatch,
) -> None:
    """SC-11: when NEXUS_T1_SESSION_ID points at an existing pool session
    file, resolve_t1_session returns THAT record (not the user's via
    PPID-walk). This is the mechanism that isolates worker T1 writes."""
    from nexus import session as sess

    _write_pool_record(tmp_path, "pool-xyz-uuid")

    # If we fell through accidentally this would return a user record
    monkeypatch.setattr(sess, "find_ancestor_session", lambda **kw: {"session_id": "user-WRONG"})
    monkeypatch.setenv("NEXUS_T1_SESSION_ID", "pool-xyz-uuid")
    monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)

    result = sess.resolve_t1_session()
    assert result is not None
    assert result["session_id"] == "pool-xyz-uuid", (
        "env-specified pool record must win; must NOT fall through to user"
    )
    assert result.get("pool_session") is True


# ── SC-14(b): env set but file missing → fall through (not error) ─────────


def test_resolve_falls_through_when_env_set_but_file_missing(
    tmp_path: Path, monkeypatch,
) -> None:
    """SC-14(b): NEXUS_T1_SESSION_ID pointing at a nonexistent file must
    gracefully fall through to PPID-walk, not raise. A stale env var
    from a previous session should not break T1 discovery."""
    from nexus import session as sess

    monkeypatch.setenv("NEXUS_T1_SESSION_ID", "pool-nonexistent-uuid-xyz")
    monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)

    sentinel = {"session_id": "fallback-via-ppid"}
    monkeypatch.setattr(sess, "find_ancestor_session", lambda **kw: sentinel)

    result = sess.resolve_t1_session()
    assert result == sentinel, (
        "env pointing to missing file must fall through to PPID-walk — SC-14(b)"
    )


# ── corrupt record → fall through ─────────────────────────────────────────


def test_resolve_falls_through_when_env_record_is_corrupt(
    tmp_path: Path, monkeypatch,
) -> None:
    """A corrupt pool session file (unparseable JSON) behaves like a
    missing file — fall through rather than crash."""
    from nexus import session as sess

    (tmp_path / "pool-corrupt.session").write_text("{{ not valid json")

    monkeypatch.setenv("NEXUS_T1_SESSION_ID", "pool-corrupt")
    monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)

    sentinel = {"session_id": "fallback"}
    monkeypatch.setattr(sess, "find_ancestor_session", lambda **kw: sentinel)

    assert sess.resolve_t1_session() == sentinel


# ── empty-string env is equivalent to unset ───────────────────────────────


def test_resolve_treats_empty_env_as_unset(
    tmp_path: Path, monkeypatch,
) -> None:
    from nexus import session as sess

    monkeypatch.setenv("NEXUS_T1_SESSION_ID", "")
    monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)

    sentinel = {"session_id": "ppid-walk"}
    monkeypatch.setattr(sess, "find_ancestor_session", lambda **kw: sentinel)

    assert sess.resolve_t1_session() == sentinel
