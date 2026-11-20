# SPDX-License-Identifier: AGPL-3.0-or-later
"""The client halves of identity scratch and per-identity T2 (RDR-253 Step 6, nexus-hczdo.1.9), tenant-free.

What the engine does with them (a scope that survives ``/clear``, a write refused to a non-owner, a delegated child that
reads its parent's scope and cannot write it, a note found after a rename) is checked against the real engine by the rows of
tests/identity/test_work_engine_rows.py. Here: which scope the CLI and the MCP tool pick, which identity's scope that is,
and that a process with no identity never falls back to the session scope.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from nexus.commands.memory import memory
from nexus.db import http_scratch_store as hss
from nexus.db import t1 as t1_mod
from nexus.commands.scratch import scratch
from nexus.db.t2.http_memory_store import identity_project
from nexus.identity import scratch_scope
from nexus.identity.scratch_scope import IdentityScopeError, scope_id, scope_owner


_REAL_IDENTITY_SCRATCH = scratch_scope.identity_scratch     # the `stores` fixture replaces the module attribute


class Client:
    def __init__(self, **views: dict[str, Any]) -> None:
        self.views = views

    def show(self, identity: str) -> dict[str, Any]:
        return self.views[identity]

    def close(self) -> None:
        pass


def holds(monkeypatch: pytest.MonkeyPatch, identity: str | None) -> None:
    from nexus.identity import token_header

    monkeypatch.setattr(token_header, "current", lambda **k: None if identity is None else SimpleNamespace(identity=identity))
    monkeypatch.setattr(token_header, "has_claude_ancestor", lambda: False)    # no ancestor: the message names no identity, wherever this runs


# ── whose scope ─────────────────────────────────────────────────────────────


def test_a_seat_or_ephemeral_works_in_its_own_scope(monkeypatch) -> None:
    holds(monkeypatch, "i7")
    assert scope_owner(Client(i7={"id": "i7", "kind": "seat"})) == "i7"
    assert scope_id("i7") == "identity:i7"


def test_a_delegated_child_works_in_its_parents_scope(monkeypatch) -> None:
    holds(monkeypatch, "i9")
    assert scope_owner(Client(i9={"id": "i9", "kind": "delegated", "parent": "i7"})) == "i7"


def test_no_identity_is_an_error_naming_it_never_a_fall_back(monkeypatch) -> None:
    holds(monkeypatch, None)
    with pytest.raises(IdentityScopeError, match="no identity is bound"):
        scope_owner(Client())


def test_the_store_is_bound_to_the_identity_scope_and_sends_no_session_token(monkeypatch) -> None:
    """nexus-hczdo: the engine needs no session for an identity-scope call, and at ``/clear`` the new session id has no
    lease to borrow a token from, so the store takes nothing from *base* and never asks for a session."""
    holds(monkeypatch, "i7")
    built: list[dict[str, Any]] = []

    class Store:
        def __init__(self, **kw: Any) -> None:
            built.append(kw)

    def never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("identity_scratch asked for the session's own T1 store")

    monkeypatch.setattr("nexus.db.http_scratch_store.HttpScratchStore", Store)
    monkeypatch.setattr("nexus.db.t1.get_t1_database", never)
    base = SimpleNamespace(_store=SimpleNamespace(_session_token="minted-token"))        # the CLI-dedicated wrapper
    scratch_scope.identity_scratch(base, client=Client(i7={"id": "i7", "kind": "seat"}))
    scratch_scope.identity_scratch(client=Client(i7={"id": "i7", "kind": "seat"}))
    expected = {"session_id": "identity:i7", "send_session_token": False, "export_adopted_token": False}
    assert built == [expected, expected], "never the process's own scope, and no token borrowed from a base"


def test_a_real_identity_store_has_no_session_header_and_no_session_recovery_with_no_lease_and_no_env(monkeypatch, tmp_path) -> None:
    """The unit twin of the ``/clear`` journey row: an explicit session id, no lease file, no ``NX_T1_SESSION``."""
    holds(monkeypatch, "i7")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    for name in ("NX_T1_SESSION", "NX_T1_SESSION_ID", "NX_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s2-brand-new")
    monkeypatch.setattr(hss, "_resolve_endpoint", lambda: ("http://127.0.0.1:1", "bearer"))
    called: list[str] = []
    monkeypatch.setattr(t1_mod, "get_t1_database", lambda *a, **k: called.append("get_t1_database"))
    monkeypatch.setattr(t1_mod, "mint_t1_session_token", lambda *a, **k: called.append("mint"))
    monkeypatch.setattr(t1_mod, "read_t1_session_lease", lambda *a, **k: called.append("lease_read"))

    store = scratch_scope.identity_scratch(client=Client(i7={"id": "i7", "kind": "seat"}))
    try:
        assert store.session_id == "identity:i7"
        assert hss._HEADER_T1_SESSION not in store._headers and hss._HEADER_T1_SESSION not in store._client.headers
        assert store._headers["Authorization"] == "Bearer bearer", "the bearer is untouched"
        store.session_recovery = lambda *a, **k: called.append("recovery") or "fresh"      # a registered hook must not be consulted
        assert store._refresh_session_token_from_lease("x") is False
        assert store._recover_borrowed_session("x") is False
        store._adopt_session_token("fresh", "identity:i7")
        assert hss._HEADER_T1_SESSION not in store._headers, "an adopt never installs a session header"
    finally:
        store.close()
    assert called == [] and not list(tmp_path.glob("t1_session_lease.*")), called


def test_a_session_store_still_sends_its_session_token() -> None:
    store = hss.HttpScratchStore(base_url="http://127.0.0.1:1", session_id="s", _token="x", _session_token="tok")
    try:
        assert store._headers[hss._HEADER_T1_SESSION] == "tok" and store._client.headers[hss._HEADER_T1_SESSION] == "tok"
    finally:
        store.close()


# ── nx scratch --scope ──────────────────────────────────────────────────────


class Entries:
    def __init__(self, label: str) -> None:
        self.label = label
        self.puts: list[dict[str, Any]] = []

    def list_entries(self) -> list[dict[str, Any]]:
        return [{"id": "abcdef12-0000", "tags": self.label, "flagged": False, "content": self.label}]

    def put(self, **kw: Any) -> str:
        self.puts.append(kw)
        return f"{self.label}-id"

    def clear(self) -> int:
        return 0

    def close(self) -> None:
        pass


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> dict[str, Entries]:
    made = {"session": Entries("session"), "identity": Entries("identity")}
    monkeypatch.setattr("nexus.commands.scratch.get_t1_database", lambda: made["session"])
    monkeypatch.setattr("nexus.identity.scratch_scope.identity_scratch", lambda base=None, **k: made["identity"])
    return made


def test_the_default_scope_is_the_session(stores) -> None:
    out = CliRunner().invoke(scratch, ["list"])
    assert out.exit_code == 0 and "session" in out.output and "identity" not in out.output


def test_scope_identity_needs_no_session_lease(monkeypatch, stores) -> None:
    """nexus-hczdo: ``nx scratch --scope identity`` never asks for the session's T1 store, so it works with no lease."""
    def never() -> Any:
        raise AssertionError("nx scratch --scope identity asked for the session's own T1 store")

    monkeypatch.setattr("nexus.commands.scratch.get_t1_database", never)
    out = CliRunner().invoke(scratch, ["--scope", "identity", "list"])
    assert out.exit_code == 0 and "identity" in out.output, out.output


def test_scope_identity_reads_and_writes_the_identity_store(stores) -> None:
    runner = CliRunner()
    assert "identity" in runner.invoke(scratch, ["--scope", "identity", "list"]).output
    done = runner.invoke(scratch, ["--scope", "identity", "put", "note", "--tags", "x"])
    assert done.exit_code == 0 and "Stored: identity-id" in done.output
    assert stores["identity"].puts and not stores["session"].puts


def test_persist_is_refused_for_identity_scratch(stores) -> None:
    out = CliRunner().invoke(scratch, ["--scope", "identity", "put", "note", "--persist"])
    assert out.exit_code == 2 and "never ends with a session" in out.output
    assert not stores["identity"].puts


def test_no_identity_is_a_clean_error_exit_1(monkeypatch, stores) -> None:
    def refuse(base=None, **k):
        raise IdentityScopeError("no identity is bound to this process")

    monkeypatch.setattr("nexus.identity.scratch_scope.identity_scratch", refuse)
    out = CliRunner().invoke(scratch, ["--scope", "identity", "list"])
    assert out.exit_code == 1 and "no identity is bound" in out.output
    assert not any(s.puts for s in stores.values())


# ── the MCP tool ────────────────────────────────────────────────────────────


def test_the_mcp_scratch_tool_takes_a_scope(monkeypatch, stores) -> None:
    from nexus.mcp import core

    monkeypatch.setattr(core, "_get_t1", lambda: (stores["session"], False))
    monkeypatch.setattr(core, "_record_tier_write", lambda **k: None)          # the audit row is a T2 write; not this test's subject
    assert "session" in core.scratch(action="list") and "[identity]" not in core.scratch(action="list")
    listed = core.scratch(action="list", scope="identity")
    assert listed.startswith("[identity] ") and "identity" in listed
    assert core.scratch(action="put", content="n", scope="identity") == "[identity] Stored: identity-id"
    assert "unknown scope" in core.scratch(action="list", scope="galaxy")
    assert stores["identity"].puts and not stores["session"].puts


# ── nx memory --mine ────────────────────────────────────────────────────────


class Db:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.memory = SimpleNamespace(
            put=lambda **kw: self.calls.append(("put", kw)) or 1,
            list_entries=lambda **kw: self.calls.append(("list", kw)) or [],
            search=lambda **kw: self.calls.append(("search", kw)) or [],
            resolve_title=lambda **kw: self.calls.append(("get", kw)) or ({"content": "body"}, []),
        )


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> Db:
    made = Db()
    monkeypatch.setattr("nexus.commands.memory.t2_handle", lambda: contextlib.nullcontext(made))
    return made


def test_mine_puts_gets_lists_and_searches_in_the_identity_project(monkeypatch, db) -> None:
    holds(monkeypatch, "i7")
    runner = CliRunner()
    assert identity_project("i7") == "identity/i7"
    assert runner.invoke(memory, ["put", "body", "--mine", "-t", "plan"]).exit_code == 0
    assert runner.invoke(memory, ["get", "--mine", "-t", "plan"]).exit_code == 0
    assert runner.invoke(memory, ["list", "--mine"]).exit_code == 0
    assert runner.invoke(memory, ["search", "plans", "--mine"]).exit_code == 0
    assert [(verb, kw["project"]) for verb, kw in db.calls] == [("put", "identity/i7"), ("get", "identity/i7"),
                                                                ("list", "identity/i7"), ("search", "identity/i7")]


def test_mine_with_a_project_or_without_an_identity_is_an_error_and_put_still_needs_one_of_them(monkeypatch, db) -> None:
    runner = CliRunner()
    holds(monkeypatch, "i7")
    both = runner.invoke(memory, ["put", "b", "--mine", "-p", "x", "-t", "t"])
    assert both.exit_code == 2 and "not both" in both.output
    neither = runner.invoke(memory, ["put", "b", "-t", "t"])
    assert neither.exit_code == 2 and "--project or --mine" in neither.output
    holds(monkeypatch, None)
    nobody = runner.invoke(memory, ["list", "--mine"])
    assert nobody.exit_code == 1 and "no identity is bound" in nobody.output
    assert db.calls == []
    assert runner.invoke(memory, ["put", "b", "-p", "plain", "-t", "t"]).exit_code == 0
    assert db.calls[0][1]["project"] == "plain", "--project is unchanged"


# ── fix round 1 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("failure", ["unavailable", "refused"])
def test_an_engine_that_cannot_answer_is_a_clean_cli_error_never_a_traceback(monkeypatch, stores, failure) -> None:
    """S1: IdentityUnavailableError and IdentityApiError are plain exceptions; the CLI turned neither into a message."""
    from nexus.identity.bind_client import IdentityApiError, IdentityUnavailableError

    holds(monkeypatch, "i7")

    class Down(Client):
        def show(self, identity):
            raise (IdentityUnavailableError("connection refused") if failure == "unavailable"
                   else IdentityApiError(404, "unknown_identity", "no identity i7", {}))

    monkeypatch.setattr("nexus.identity.bind_client.IdentityClient.from_config", classmethod(lambda cls, **k: Down()))
    monkeypatch.setattr("nexus.identity.scratch_scope.identity_scratch", _REAL_IDENTITY_SCRATCH)
    out = CliRunner().invoke(scratch, ["--scope", "identity", "list"])
    assert out.exit_code == 1 and "Traceback" not in out.output and out.exception.__class__ is SystemExit, out.output
    assert ("not answering" if failure == "unavailable" else "unknown_identity") in out.output


def test_clear_is_refused_for_identity_scratch_because_the_engine_would_report_success_and_delete_nothing(stores) -> None:
    out = CliRunner().invoke(scratch, ["--scope", "identity", "clear", "-y"])
    assert out.exit_code == 2 and "delete identity scratch entries one by one" in out.output
    assert CliRunner().invoke(scratch, ["clear", "-y"]).exit_code == 0, "the session scope still clears"


def test_flag_is_refused_for_identity_scratch(stores) -> None:
    out = CliRunner().invoke(scratch, ["--scope", "identity", "flag", "abcdef12"])
    assert out.exit_code == 2 and "never ends with a session" in out.output


def test_the_mcp_tool_refuses_persist_and_flag_for_identity_scratch_and_closes_the_store(monkeypatch, stores) -> None:
    from nexus.mcp import core

    closed: list[str] = []
    stores["identity"].close = lambda: closed.append("closed")                         # type: ignore[attr-defined]
    stores["identity"].resolve_prefix_candidates = lambda i: [i]                       # type: ignore[attr-defined]
    stores["identity"].promote = lambda *a, **k: SimpleNamespace(action="new")         # type: ignore[attr-defined]
    monkeypatch.setattr(core, "_get_t1", lambda: (stores["session"], False))
    monkeypatch.setattr(core, "_record_tier_write", lambda **k: None)
    monkeypatch.setattr(core, "_t2_ctx", lambda: contextlib.nullcontext(None))
    assert "never runs for identity" in core.scratch(action="put", content="n", scope="identity", persist=True)
    assert not stores["identity"].puts
    assert "never runs for identity" in core.scratch_manage(action="flag", entry_id="e", scope="identity")
    done = core.scratch_manage(action="promote", entry_id="e", project="p", title="t", scope="identity")
    assert done.startswith("[identity] Promoted") and "action=new" in done
    core.scratch(action="list", scope="identity")
    assert closed and set(closed) == {"closed"}, "the per-call identity store is closed"
    assert "unknown scope" in core.scratch_manage(action="promote", entry_id="e", scope="galaxy")


def test_the_mcp_identity_scope_needs_no_session_store(monkeypatch, stores) -> None:
    """nexus-hczdo: ``scratch``/``scratch_manage`` with ``scope="identity"`` never resolve the session's T1 store, so they work
    when it cannot be resolved (no lease for the session id). The session scope still needs it."""
    from nexus.mcp import core

    def no_session_store() -> Any:
        raise RuntimeError("no T1 session resolves for this process")

    stores["identity"].resolve_prefix_candidates = lambda i: [i]                       # type: ignore[attr-defined]
    stores["identity"].promote = lambda *a, **k: SimpleNamespace(action="new")         # type: ignore[attr-defined]
    monkeypatch.setattr(core, "_get_t1", no_session_store)
    monkeypatch.setattr(core, "_record_tier_write", lambda **k: None)
    monkeypatch.setattr(core, "_t2_ctx", lambda: contextlib.nullcontext(None))
    assert core.scratch(action="list", scope="identity").startswith("[identity]")
    assert core.scratch(action="put", content="n", tags="x", scope="identity").startswith("[identity] Stored")
    assert core.scratch_manage(action="promote", entry_id="e", project="p", title="t", scope="identity").startswith("[identity] Promoted")
    assert "no T1 session resolves" in core.scratch(action="list"), "the session scope still resolves the session store"


def test_mine_for_a_delegated_child_reads_its_parents_project_and_never_writes_it(monkeypatch, db) -> None:
    """Reads follow the child's identity scratch to the parent. Its WRITES are refused (RDR-253 Step 6 part 3,
    nexus-hczdo): a delegated identity keeps no identity notes, so none orphan when the child ends."""
    holds(monkeypatch, "i9")
    monkeypatch.setenv("NX_IDENTITY_TOKEN", "/some/token/file")
    closed: list[bool] = []
    client = SimpleNamespace(show=lambda i: {"id": i, "kind": "delegated", "parent": "i7"}, close=lambda: closed.append(True))
    monkeypatch.setattr("nexus.identity.bind_client.IdentityClient.from_config", classmethod(lambda cls, **k: client))
    assert CliRunner().invoke(memory, ["list", "--mine"]).exit_code == 0
    assert db.calls[0][1]["project"] == "identity/i7" and closed == [True]
    out = CliRunner().invoke(memory, ["put", "body", "--mine", "-t", "plan"])
    assert out.exit_code == 1 and "delegated" in out.output and "Traceback" not in out.output
    assert [verb for verb, _ in db.calls] == ["list"], "the refused put reached no store"
