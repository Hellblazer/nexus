# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-079 P2.5 — session-record schema extension.

Adds two optional fields to ``write_session_record`` for pool-scoped
sessions: ``pool_pid: int`` (pool-owner process PID, required for P2.2
reconciliation via ``os.kill(pid, 0)`` liveness probe) and
``pool_session: bool`` (marker distinguishing pool from user session).

User-scoped session records (the existing RDR-078 path) leave both fields
absent — not ``None``, not ``False``, literally absent from the JSON.
Backward compat is preserved: any consumer that ignores unknown keys
keeps working; any consumer that reads these fields with ``dict.get()``
gets the expected defaults (``None`` / ``False``).

Invariants protected: I-1 (pool session identity distinct from user),
I-3 (pool lives inside nexus server; this schema distinction is how
liveness reconciliation can tell a pool record from a user record).
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def test_user_session_record_omits_pool_fields(tmp_path: Path) -> None:
    """Backward compat: existing RDR-078 call shape (no pool_* args) must
    produce a JSON record that does NOT contain pool_pid or pool_session.
    Keys absent, not null — so consumers doing ``if "pool_pid" in data``
    correctly distinguish user from pool records.
    """
    from nexus.session import write_session_record

    path = write_session_record(
        sessions_dir=tmp_path,
        ppid=12345,
        session_id="user-session-abc",
        host="127.0.0.1",
        port=54321,
        server_pid=99999,
    )
    data = json.loads(path.read_text())
    assert "pool_pid" not in data, (
        "user session records must NOT include pool_pid (RDR-079 P2.5)"
    )
    assert "pool_session" not in data, (
        "user session records must NOT include pool_session"
    )
    # Existing fields unchanged.
    assert data["session_id"] == "user-session-abc"
    assert data["server_pid"] == 99999
    assert data["server_port"] == 54321


def test_pool_session_record_includes_pool_fields(tmp_path: Path) -> None:
    """Pool-scoped session records carry ``pool_pid`` (required for P2.2
    liveness reconciliation) and ``pool_session: true`` (marker)."""
    from nexus.session import write_session_record

    my_pid = os.getpid()
    path = write_session_record(
        sessions_dir=tmp_path,
        ppid=12345,  # retained for file-path compatibility, unused for pool
        session_id="pool-abc-def",
        host="127.0.0.1",
        port=54321,
        server_pid=99999,
        pool_pid=my_pid,
        pool_session=True,
    )
    data = json.loads(path.read_text())
    assert data.get("pool_pid") == my_pid
    assert data.get("pool_session") is True
    # Existing fields still present.
    assert data["session_id"] == "pool-abc-def"
    assert data["server_pid"] == 99999


def test_pool_session_file_path_honors_session_id_when_pool_session(
    tmp_path: Path,
) -> None:
    """Pool sessions use their UUID as the filename (``pool-<uuid>.session``)
    rather than the owner's PPID. This keeps pool records discoverable by
    the session_id and eliminates PID-reuse confusion on reconciliation.
    User sessions continue to use ``{ppid}.session`` unchanged."""
    from nexus.session import write_session_record

    path = write_session_record(
        sessions_dir=tmp_path,
        ppid=0,  # unused when pool_session=True
        session_id="pool-unique-7f3a",
        host="127.0.0.1",
        port=54321,
        server_pid=99999,
        pool_pid=os.getpid(),
        pool_session=True,
    )
    # File named by session_id, not by ppid
    assert path.name == "pool-unique-7f3a.session"
    assert path.parent == tmp_path


def test_user_session_file_path_uses_ppid_unchanged(tmp_path: Path) -> None:
    """Regression: user-session filename convention stays ``{ppid}.session``."""
    from nexus.session import write_session_record

    path = write_session_record(
        sessions_dir=tmp_path,
        ppid=7777,
        session_id="user-xyz",
        host="127.0.0.1",
        port=12345,
        server_pid=88888,
    )
    assert path.name == "7777.session"


def test_pool_session_record_without_pool_pid_is_invalid(tmp_path: Path) -> None:
    """Guard: passing ``pool_session=True`` without ``pool_pid`` is a
    contract violation — reconciliation cannot work without a PID to probe.
    Raises ``ValueError`` at record-write time rather than silently
    accepting an unrecoverable record."""
    import pytest

    from nexus.session import write_session_record

    with pytest.raises(ValueError, match="pool_pid.*required"):
        write_session_record(
            sessions_dir=tmp_path,
            ppid=0,
            session_id="pool-broken",
            host="127.0.0.1",
            port=54321,
            server_pid=99999,
            pool_session=True,
            # pool_pid intentionally omitted
        )


def test_existing_user_session_consumers_tolerate_absent_fields(
    tmp_path: Path,
) -> None:
    """Any code reading the session record with ``.get()`` defaults must
    keep working. This test simulates the reader pattern: load user JSON,
    probe for pool fields, expect None/False defaults."""
    from nexus.session import write_session_record

    path = write_session_record(
        sessions_dir=tmp_path,
        ppid=12345,
        session_id="user-sess",
        host="127.0.0.1",
        port=54321,
        server_pid=99999,
    )
    data = json.loads(path.read_text())
    # Reader-pattern probe — the shape the P2.2 reconciliation code will use.
    assert data.get("pool_pid") is None
    assert data.get("pool_session", False) is False
    # Liveness reconciliation keys off pool_session; user records skip the check.
    is_pool = bool(data.get("pool_session"))
    assert is_pool is False
