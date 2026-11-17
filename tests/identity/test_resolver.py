# SPDX-License-Identifier: AGPL-3.0-or-later
"""resolve_identity() (RDR-253 "The resolver"): which identity is this process, answered from the session
index and the process's own token file, refusing when two inputs disagree. It only reads. Tenant-free: the
engine's answers are fixed responses (tests/identity/fakes.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.identity.resolver import IdentityConflictError, resolve_identity
from nexus.identity.token_file import TokenFile, write_token_file
from tests.identity.fakes import HOST, FakeClient, FakeProcesses


def _row(identity: str, generation: int = 1, session_id: str = "") -> dict:
    return {"session_id": session_id, "identity": identity, "generation": generation, "current_generation": generation,
            "kind": "seat", "state": "live", "liveness": "live"}


def test_two_inputs_naming_different_identities_refuse_with_both() -> None:
    """A hook payload session id indexed to i1 and a CLAUDE_CODE_SESSION_ID indexed to i2: refuse, naming both."""
    client = FakeClient(sessions={"sid-payload": _row("i1", session_id="sid-payload"),
                                  "sid-env": _row("i2", session_id="sid-env")})
    with pytest.raises(IdentityConflictError) as err:
        resolve_identity(client=client, payload_session_id="sid-payload", env={"CLAUDE_CODE_SESSION_ID": "sid-env"})
    message = str(err.value)
    assert "i1" in message and "i2" in message and "sid-payload" in message and "sid-env" in message
    assert {a.identity for a in err.value.answers} == {"i1", "i2"}


def test_one_identity_from_two_sources_resolves() -> None:
    """The payload and the environment may name different session ids of one identity (a /clear in the process)."""
    client = FakeClient(sessions={"old": _row("i1", session_id="old"), "new": _row("i1", session_id="new")})
    got = resolve_identity(client=client, payload_session_id="new", env={"CLAUDE_CODE_SESSION_ID": "old"})
    assert (got.identity, got.generation, got.source) == ("i1", 1, "payload")


def test_the_environment_alone_resolves_and_nothing_resolves_to_none() -> None:
    client = FakeClient(sessions={"sid-env": _row("i4", 3, "sid-env")})
    assert resolve_identity(client=client, env={"CLAUDE_CODE_SESSION_ID": "sid-env"}).identity == "i4"
    assert resolve_identity(client=client, env={}) is None
    assert resolve_identity(client=client, payload_session_id="not-indexed", env={}) is None


def test_an_explicit_token_file_wins_and_a_session_for_another_identity_refuses(tmp_path: Path) -> None:
    """A delegated child: NX_IDENTITY_TOKEN names the token file it was given. It is the first input."""
    delegated = tmp_path / "delegated.token"
    write_token_file(delegated, TokenFile(identity="i9", generation=1, token="tok", expires_at=9e9,
                                          session_id="child-sid", pid=1, started_at=1, proc_start=None))
    client = FakeClient(sessions={"parent-sid": _row("i1", session_id="parent-sid")})
    with pytest.raises(IdentityConflictError) as err:
        resolve_identity(client=client, env={"NX_IDENTITY_TOKEN": str(delegated), "CLAUDE_CODE_SESSION_ID": "parent-sid"})
    assert {a.identity for a in err.value.answers} == {"i9", "i1"}
    alone = resolve_identity(client=FakeClient(), env={"NX_IDENTITY_TOKEN": str(delegated)})
    assert (alone.identity, alone.generation, alone.source) == ("i9", 1, "explicit_token")


def test_a_bash_child_from_before_a_cross_seat_resume_is_refused(tmp_path: Path) -> None:
    """The process switched from i1 to i2 (the token file now says i2); a Bash child started before the switch still
    carries the old session id, which the index maps to i1. The resolver compares the ancestor's token file with
    the mapping and refuses (RDR line 361, fix check 253-fix-check-88e4ee8da3a2 item 14)."""
    procs = FakeProcesses(tmp_path / "sessions")
    procs.start(4242, "2026-11-17T08:00:00Z", started_at=4_242_000)
    identity_dir = tmp_path / "identity"
    write_token_file(identity_dir / "4242.4242000.token",
                     TokenFile(identity="i2", generation=1, token="t", expires_at=9e9, session_id="new-sid", pid=4242,
                               started_at=4_242_000, proc_start="2026-11-17T08:00:00Z"))
    client = FakeClient(sessions={"old-sid": _row("i1", session_id="old-sid")})
    with pytest.raises(IdentityConflictError) as err:
        resolve_identity(client=client, env={"CLAUDE_CODE_SESSION_ID": "old-sid"}, claude_pid=4242,
                         sessions_dir=procs.sessions_dir, identity_dir=identity_dir)
    assert {a.identity for a in err.value.answers} == {"i1", "i2"}
    assert {a.source for a in err.value.answers} == {"session_id", "token_file"}


def test_after_a_clear_the_old_session_id_and_the_token_file_agree(tmp_path: Path) -> None:
    """/clear keeps the identity and the generation: a Bash child from before it still resolves."""
    procs = FakeProcesses(tmp_path / "sessions")
    procs.start(4242, "2026-11-17T08:00:00Z", started_at=4_242_000)
    identity_dir = tmp_path / "identity"
    write_token_file(identity_dir / "4242.4242000.token",
                     TokenFile(identity="i1", generation=2, token="t", expires_at=9e9, session_id="new-sid", pid=4242,
                               started_at=4_242_000, proc_start="2026-11-17T08:00:00Z"))
    client = FakeClient(sessions={"old-sid": _row("i1", 2, "old-sid")})
    got = resolve_identity(client=client, env={"CLAUDE_CODE_SESSION_ID": "old-sid"}, claude_pid=4242,
                           sessions_dir=procs.sessions_dir, identity_dir=identity_dir)
    assert (got.identity, got.generation) == ("i1", 2)


def test_an_unknown_session_id_resolves_through_the_registry_pid_and_started_at(tmp_path: Path) -> None:
    """/branch runs no hook: its new session id is in no index. The live registry entry names the process; the
    bindings on this host name the identity that process holds."""
    procs = FakeProcesses(tmp_path / "sessions")
    procs.start(4242, "2026-11-17T08:00:00Z", started_at=4_242_000, session_id="branch-sid")
    held = {"id": "i7", "generation": 5, "state": "live", "kind": "seat",
            "holder": {"host": HOST, "pid": 4242, "started_at": 4_242_000, "proc_start": "2026-11-17T08:00:00Z"}}
    other = {"id": "i8", "generation": 1, "state": "live", "kind": "seat",
             "holder": {"host": HOST, "pid": 4242, "started_at": 1, "proc_start": "2026-11-17T08:00:00Z"}}
    client = FakeClient(bindings=[other, held])
    got = resolve_identity(client=client, env={"CLAUDE_CODE_SESSION_ID": "branch-sid"}, claude_pid=4242,
                           sessions_dir=procs.sessions_dir, identity_dir=tmp_path / "identity", host=HOST)
    assert (got.identity, got.generation, got.source) == ("i7", 5, "registry")
    # it only reads: the fake client has no write method at all, and nothing on disk changed
    assert not (tmp_path / "identity").exists()
