# SPDX-License-Identifier: AGPL-3.0-or-later
"""A long-lived process (the MCP server) across an in-place ``/restart`` (nexus-hczdo). Tenant-free.

The restart keeps the Claude pid and gives its registry file a new ``startedAt``, so the new generation's token is in a
NEW file, ``<pid>.<new startedAt>.token``. The server outlives the restart, so the file it reads must follow the registry,
never the name it found first. The engine-backed twin is the identity-scratch row of
tests/identity/test_work_engine_rows.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.identity import holder, token_header
from nexus.identity.token_file import TokenFile, token_path, write_token_file
from tests.identity.fakes import FakeProcesses

T0 = "2026-11-17T08:00:00Z"
PID = 4242
FAR = 4_000_000_000.0


def _record(generation: int, token: str, started_at: int) -> TokenFile:
    return TokenFile(identity="i1", generation=generation, token=token, expires_at=FAR, session_id="sid", pid=PID,
                     started_at=started_at, proc_start=T0, host="h")


@pytest.fixture()
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeProcesses:
    procs = FakeProcesses(tmp_path / "claude-sessions", host="h")
    idir = tmp_path / "identity"
    monkeypatch.delenv("NX_IDENTITY_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid")
    monkeypatch.setattr(token_header, "_ancestor", lambda: (PID, idir))
    monkeypatch.setattr(holder, "claude_sessions_dir", lambda: procs.sessions_dir)
    monkeypatch.setattr(token_header, "_verify_session", lambda record: None)
    token_header.reset_for_tests()
    procs.idir = idir  # type: ignore[attr-defined]
    yield procs
    token_header.reset_for_tests()


def test_the_token_follows_the_registry_across_an_in_place_restart(box: FakeProcesses) -> None:
    box.start(PID, T0, started_at=100_000, session_id="sid")
    write_token_file(token_path(box.idir, PID, 100_000), _record(1, "tok-gen-1", 100_000))
    assert token_header.current().token == "tok-gen-1"
    assert token_header.identity_headers()                                       # generation 1's token is what it sends

    box.write_registry(PID, 999_000, T0, session_id="sid")                       # the in-place /restart
    write_token_file(token_path(box.idir, PID, 999_000), _record(2, "tok-gen-2", 999_000))
    assert token_path(box.idir, PID, 100_000).exists(), "the generation-1 file is still on disk and unexpired"

    record = token_header.current()
    assert (record.generation, record.token) == (2, "tok-gen-2")
    assert token_header.token_file_path() == token_path(box.idir, PID, 999_000)


def test_a_registry_that_is_briefly_unreadable_sends_no_token_never_the_old_generations(box: FakeProcesses) -> None:
    box.start(PID, T0, started_at=100_000, session_id="sid")
    write_token_file(token_path(box.idir, PID, 100_000), _record(1, "tok-gen-1", 100_000))
    assert token_header.current().token == "tok-gen-1"
    (box.sessions_dir / f"{PID}.json").unlink()
    assert token_header.current() is None and token_header.identity_headers() == {}
