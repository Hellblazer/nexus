# SPDX-License-Identifier: AGPL-3.0-or-later
"""A long-lived process (the MCP server) across an in-place ``/restart``, and what it sends (nexus-hczdo). Tenant-free.

The restart keeps the Claude pid and gives its registry file a new ``startedAt``, so the new generation's token is in a
NEW file, ``<pid>.<new startedAt>.token``. The server outlives the restart, so the file it reads must follow the registry,
never the name it found first. The engine-backed twin is the identity-scratch row of
tests/identity/test_work_engine_rows.py (a restart is answered ``409 generation_superseded`` on a write, not a 401).

Also here: the one re-read of a registry that Claude is mid-write on, the scratch store's identity header (a write only,
re-read once on a 401) and the session check's per-identity memory.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from nexus.db.http_scratch_store import HttpScratchStore
from nexus.identity import holder, token_contract, token_header
from nexus.identity.scratch_scope import IdentityScopeError, scope_owner
from nexus.identity.token_file import TokenFile, token_path, write_token_file
from tests.identity.fakes import FakeProcesses

T0 = "2026-11-17T08:00:00Z"
PID = 4242
FAR = 4_000_000_000.0


def _record(generation: int, token: str, started_at: int, identity: str = "i1") -> TokenFile:
    return TokenFile(identity=identity, generation=generation, token=token, expires_at=FAR, session_id="sid", pid=PID,
                     started_at=started_at, proc_start=T0, host="h")


@pytest.fixture()
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeProcesses:
    procs = FakeProcesses(tmp_path / "claude-sessions", host="h")
    idir = tmp_path / "identity"
    monkeypatch.delenv("NX_IDENTITY_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid")
    monkeypatch.setattr(token_header, "_ancestor", lambda: (PID, idir))
    monkeypatch.setattr(holder, "claude_sessions_dir", lambda: procs.sessions_dir)
    monkeypatch.setattr(token_header, "_verify_session", lambda record, path: None)
    monkeypatch.setattr(token_header, "_pause", lambda seconds: None)
    token_header.reset_for_tests()
    procs.idir = idir  # type: ignore[attr-defined]
    yield procs
    token_header.reset_for_tests()


def test_the_token_follows_the_registry_across_an_in_place_restart(box: FakeProcesses) -> None:
    box.start(PID, T0, started_at=100_000, session_id="sid")
    write_token_file(token_path(box.idir, PID, 100_000), _record(1, "tok-gen-1", 100_000))
    assert token_header.current().token == "tok-gen-1"
    assert token_header.identity_headers() == token_contract.headers("tok-gen-1")   # generation 1's token is what it sends

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


# ── a torn registry, read once more ─────────────────────────────────────────


def test_a_registry_that_is_missing_is_read_once_more_after_a_pause_and_the_new_file_is_found(
        box: FakeProcesses, monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude may be mid-write of its registry file: the read finds nothing. One more read after ~10 ms finds it. The
    second read names the file afresh; it never falls back to the name the first call found."""
    box.start(PID, T0, started_at=100_000, session_id="sid")
    write_token_file(token_path(box.idir, PID, 100_000), _record(1, "tok-gen-1", 100_000))
    assert token_header.current().token == "tok-gen-1"
    (box.sessions_dir / f"{PID}.json").unlink()                                  # the torn moment
    pauses: list[float] = []

    def writer_finishes(seconds: float) -> None:
        pauses.append(seconds)
        box.write_registry(PID, 999_000, T0, session_id="sid")
        write_token_file(token_path(box.idir, PID, 999_000), _record(2, "tok-gen-2", 999_000))

    monkeypatch.setattr(token_header, "_pause", writer_finishes)
    assert token_header.current().token == "tok-gen-2"
    assert pauses == [token_header.REREAD_DELAY_S] and 0 < token_header.REREAD_DELAY_S <= 0.05


def test_a_file_that_stays_unreadable_is_re_read_once_not_forever(box: FakeProcesses, monkeypatch: pytest.MonkeyPatch) -> None:
    box.start(PID, T0, started_at=100_000, session_id="sid")                    # a registry file, and no token file
    pauses: list[float] = []
    monkeypatch.setattr(token_header, "_pause", pauses.append)
    assert token_header.current() is None and len(pauses) == 1


def test_a_process_with_no_claude_ancestor_never_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_IDENTITY_TOKEN", raising=False)
    monkeypatch.setattr(token_header, "_ancestor", lambda: None)
    pauses: list[float] = []
    monkeypatch.setattr(token_header, "_pause", pauses.append)
    token_header.reset_for_tests()
    assert token_header.current() is None and pauses == []
    with pytest.raises(IdentityScopeError, match="no identity is bound"):
        scope_owner(object())
    token_header.reset_for_tests()


def test_scope_owner_says_the_token_file_is_not_readable_yet_when_an_ancestor_exists(box: FakeProcesses) -> None:
    box.start(PID, T0, started_at=100_000, session_id="sid")
    with pytest.raises(IdentityScopeError, match="token file not readable yet"):
        scope_owner(object())


# ── the session check ───────────────────────────────────────────────────────


def test_the_session_check_is_remembered_per_session_and_identity(box: FakeProcesses, monkeypatch: pytest.MonkeyPatch) -> None:
    """A session id the engine vouched for under identity i1 says nothing about it under i2: the record can change identity
    under a live process (a cross-seat resume rewrites the file). The refusal names the record's own file."""
    import nexus.identity.resolver as resolver

    monkeypatch.undo()                                                           # the fixture's no-op session check goes
    monkeypatch.setattr(token_header, "_ancestor", lambda: (PID, box.idir))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other-sid")
    monkeypatch.delenv("NX_IDENTITY_TOKEN", raising=False)
    token_header.reset_for_tests()
    asked: list[str] = []

    class Answer:
        identity, answers = "i1", []

    monkeypatch.setattr(token_header, "_resolver_client", lambda: object())
    monkeypatch.setattr(resolver, "resolve_identity", lambda **kw: asked.append("asked") or Answer())
    path = token_path(box.idir, PID, 100_000)
    record = _record(1, "t", 100_000)
    token_header._verify_session(record, path)
    token_header._verify_session(record, path)
    assert asked == ["asked"], "the same session and identity is asked once"
    with pytest.raises(Exception) as err:                                        # the engine says i1 owns it, the file says i2
        token_header._verify_session(_record(1, "t", 100_000, identity="i2"), path)
    assert asked == ["asked", "asked"], "another identity under the same session id is asked again"
    assert str(path) in str(err.value)


# ── the scratch store's identity header ─────────────────────────────────────


class _Engine:
    """A fake T1 engine: each request's path and identity header are recorded; *answer* decides the reply."""

    def __init__(self, answer) -> None:
        self.answer, self.seen = answer, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.url.path, request.headers.get(token_contract.IDENTITY_TOKEN_HEADER)))
        status, body = self.answer(request)
        return httpx.Response(status, json=body)


def _identity_store(engine: _Engine) -> HttpScratchStore:
    store = HttpScratchStore(base_url="http://engine.test", tenant="default", session_id="identity:i1", _token="b",
                             _session_token="minted", export_adopted_token=False)
    store._client = httpx.Client(base_url="http://engine.test", headers=store._headers, transport=httpx.MockTransport(engine))
    return store


def test_an_identity_scope_read_sends_no_identity_token_a_write_sends_it(box: FakeProcesses) -> None:
    """The engine answers an identity-scope read to anyone, and answers a token it cannot resolve with a 401 before any
    handler runs: so a read must not carry one, whatever the token file holds."""
    box.start(PID, T0, started_at=100_000, session_id="sid")
    write_token_file(token_path(box.idir, PID, 100_000), _record(1, "unresolvable", 100_000))

    def answer(request: httpx.Request):
        if request.headers.get(token_contract.IDENTITY_TOKEN_HEADER) == "unresolvable":
            return 401, {"error": "identity_token_expired"}
        return 200, {"entries": [], "ids": [], "id": "x", "found": False}

    engine = _Engine(answer)
    store = _identity_store(engine)
    assert store.list_entries() == []
    assert store.flagged_entries() == []
    assert store.resolve_prefix_candidates("ab") == []
    assert engine.seen == [("/v1/t1/list", None), ("/v1/t1/flagged", None), ("/v1/t1/resolve_prefix", None)]
    with pytest.raises(RuntimeError, match="401"):
        store.put(content="a write", tags="")
    assert ("/v1/t1/put", "unresolvable") in engine.seen


def test_an_identity_scope_write_refused_with_401_re_reads_the_token_file_and_resends_once(box: FakeProcesses,
                                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented rule (token_header, groups.py, the T2 stores): a 401 on a request that carried an identity token
    re-reads the file; a different usable token is resent once. The bearer heal is not blamed for it."""
    box.start(PID, T0, started_at=100_000, session_id="sid")
    first = token_path(box.idir, PID, 100_000)
    write_token_file(first, _record(1, "tok-old", 100_000))

    def answer(request: httpx.Request):
        if request.headers.get(token_contract.IDENTITY_TOKEN_HEADER) == "tok-old":
            write_token_file(first, _record(1, "tok-new", 100_000))             # the server refreshed it meanwhile
            return 401, {"error": "identity_token_expired"}
        return 200, {"id": "entry-1"}

    engine = _Engine(answer)
    store = _identity_store(engine)
    healed: list[str] = []
    monkeypatch.setattr(store, "_refresh_session_token_from_lease", lambda sent: healed.append("lease") or False)
    monkeypatch.setattr(store, "_remint_data_token_and_rebuild", lambda sent="": healed.append("remint") or False)
    assert store.put(content="x", tags="") == "entry-1"
    assert engine.seen == [("/v1/t1/put", "tok-old"), ("/v1/t1/put", "tok-new")]
    assert healed == [], "the session-token and bearer heals never ran"
