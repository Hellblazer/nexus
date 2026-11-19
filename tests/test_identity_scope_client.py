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
from nexus.commands.scratch import scratch
from nexus.db.t2.http_memory_store import identity_project
from nexus.identity import scratch_scope
from nexus.identity.scratch_scope import IdentityScopeError, scope_id, scope_owner


class Client:
    def __init__(self, **views: dict[str, Any]) -> None:
        self.views = views

    def show(self, identity: str) -> dict[str, Any]:
        return self.views[identity]


def holds(monkeypatch: pytest.MonkeyPatch, identity: str | None) -> None:
    from nexus.identity import token_header

    monkeypatch.setattr(token_header, "current", lambda **k: None if identity is None else SimpleNamespace(identity=identity))


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


def test_the_store_carries_the_identity_scope_and_the_sessions_own_token(monkeypatch) -> None:
    holds(monkeypatch, "i7")
    built: list[dict[str, Any]] = []

    class Store:
        def __init__(self, **kw: Any) -> None:
            built.append(kw)

    monkeypatch.setattr("nexus.db.http_scratch_store.HttpScratchStore", Store)
    base = SimpleNamespace(_store=SimpleNamespace(_session_token="minted-token"))        # the CLI-dedicated wrapper
    scratch_scope.identity_scratch(base, client=Client(i7={"id": "i7", "kind": "seat"}))
    scratch_scope.identity_scratch(SimpleNamespace(_session_token="direct"), client=Client(i7={"id": "i7", "kind": "seat"}))
    assert built == [{"session_id": "identity:i7", "_session_token": "minted-token"},
                     {"session_id": "identity:i7", "_session_token": "direct"}]


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


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> dict[str, Entries]:
    made = {"session": Entries("session"), "identity": Entries("identity")}
    monkeypatch.setattr("nexus.commands.scratch.get_t1_database", lambda: made["session"])
    monkeypatch.setattr("nexus.identity.scratch_scope.identity_scratch", lambda base=None, **k: made["identity"])
    return made


def test_the_default_scope_is_the_session(stores) -> None:
    out = CliRunner().invoke(scratch, ["list"])
    assert out.exit_code == 0 and "session" in out.output and "identity" not in out.output


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
