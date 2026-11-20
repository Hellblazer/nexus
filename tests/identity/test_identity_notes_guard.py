# SPDX-License-Identifier: AGPL-3.0-or-later
"""Only a seat writes its own T2 notes, ``identity/<id>`` (RDR-253 Step 6 part 3, nexus-hczdo).

The engine fences the ``threads`` project only; it takes a write to ``identity/<id>`` from anyone. So the client refuses
the write from an ephemeral or delegated identity, before any request, with one line, and a refused or failed check is
logged and never retried. A process that holds no identity (a person at a shell) is not refused: the engine does not
fence the project and a person is no ephemeral session.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexus.db.t2.http_memory_store import HttpMemoryStore
from nexus.identity import notes
from nexus.identity.notes import IdentityNotesRefused


class Client:
    def __init__(self, view: dict[str, Any] | Exception) -> None:
        self.view = view
        self.shown: list[str] = []
        self.closed = 0

    def show(self, identity: str) -> dict[str, Any]:
        self.shown.append(identity)
        if isinstance(self.view, Exception):
            raise self.view
        return self.view

    def close(self) -> None:
        self.closed += 1


@pytest.fixture(autouse=True)
def fresh_cache() -> None:
    notes.reset_cache()


def holds(monkeypatch: pytest.MonkeyPatch, identity: str | None, *, delegated: bool = False) -> None:
    from nexus.identity import token_header

    monkeypatch.setattr(token_header, "current", lambda **k: None if identity is None else SimpleNamespace(identity=identity))
    if delegated:
        monkeypatch.setenv("NX_IDENTITY_TOKEN", "/some/token/file")
    else:
        monkeypatch.delenv("NX_IDENTITY_TOKEN", raising=False)


def engine(monkeypatch: pytest.MonkeyPatch, view: dict[str, Any] | Exception) -> Client:
    client = Client(view)
    monkeypatch.setattr("nexus.identity.bind_client.IdentityClient.from_config", classmethod(lambda cls, **k: client))
    return client


def test_a_seat_may_write_its_notes(monkeypatch) -> None:
    holds(monkeypatch, "i5")
    engine(monkeypatch, {"id": "i5", "kind": "seat"})
    notes.check_write("identity/i5")


@pytest.mark.parametrize("kind", ["ephemeral", "delegated"])
def test_an_ephemeral_or_delegated_identity_is_refused_in_one_line(monkeypatch, kind) -> None:
    holds(monkeypatch, "i5")
    engine(monkeypatch, {"id": "i5", "kind": kind})
    with pytest.raises(IdentityNotesRefused) as refused:
        notes.check_write("identity/i5")
    message = str(refused.value)
    assert kind in message and "\n" not in message and "seat" in message


def test_a_delegated_child_is_refused_without_asking_the_engine(monkeypatch) -> None:
    """``NX_IDENTITY_TOKEN`` names a delegated child's token file: the environment says it, no lookup is needed."""
    holds(monkeypatch, "i9", delegated=True)
    client = engine(monkeypatch, {"id": "i9", "kind": "seat"})   # would pass if it were asked
    with pytest.raises(IdentityNotesRefused, match="delegated"):
        notes.check_write("identity/i7")
    assert client.shown == []


def test_a_kind_that_cannot_be_learned_refuses_never_guesses(monkeypatch) -> None:
    from nexus.identity.bind_client import IdentityUnavailableError

    holds(monkeypatch, "i5")
    engine(monkeypatch, IdentityUnavailableError("connection refused"))
    with pytest.raises(IdentityNotesRefused, match="could not confirm"):
        notes.check_write("identity/i5")


def test_a_process_with_no_identity_is_not_refused_and_asks_nothing(monkeypatch) -> None:
    holds(monkeypatch, None)
    client = engine(monkeypatch, {"kind": "ephemeral"})
    notes.check_write("identity/i5")
    assert client.shown == []


def test_every_other_project_is_untouched_and_costs_no_lookup(monkeypatch) -> None:
    holds(monkeypatch, "i5")
    client = engine(monkeypatch, {"kind": "ephemeral"})
    for project in ("nexus", "threads", "identityish/i5", "identity", "my-identity/i5"):
        notes.check_write(project)
    assert client.shown == []


def test_the_kind_is_asked_once_per_process_and_the_client_is_closed(monkeypatch) -> None:
    holds(monkeypatch, "i5")
    client = engine(monkeypatch, {"kind": "seat"})
    notes.check_write("identity/i5")
    notes.check_write("identity/i5")
    assert client.shown == ["i5"] and client.closed == 1


def test_a_failed_lookup_is_not_cached_as_a_verdict(monkeypatch) -> None:
    from nexus.identity.bind_client import IdentityUnavailableError

    holds(monkeypatch, "i5")
    engine(monkeypatch, IdentityUnavailableError("down"))
    with pytest.raises(IdentityNotesRefused):
        notes.check_write("identity/i5")
    engine(monkeypatch, {"kind": "seat"})
    notes.check_write("identity/i5")


# ── where the check sits: the store, so the CLI and the MCP tool both meet it ──


class Wire(HttpMemoryStore):
    def __init__(self) -> None:                                    # no endpoint, no credential: the check comes first
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def _post(self, path: str, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        self.posts.append((path, payload))
        return {"id": 1, "action": "inserted", "title": payload["title"], "content": payload["content"]}


@pytest.mark.parametrize("verb", ["put", "put_or_merge", "put_replacing"])
def test_the_store_refuses_before_any_request(monkeypatch, verb) -> None:
    holds(monkeypatch, "i5")
    engine(monkeypatch, {"kind": "ephemeral"})
    wire = Wire()
    extra = {"replaces": "old"} if verb == "put_replacing" else {}
    with pytest.raises(IdentityNotesRefused):
        getattr(wire, verb)(project="identity/i5", title="t", content="c", **extra)
    assert wire.posts == [], "a refused write sends nothing and is not retried"


def test_the_store_still_writes_for_a_seat_and_for_every_other_project(monkeypatch) -> None:
    holds(monkeypatch, "i5")
    engine(monkeypatch, {"kind": "seat"})
    wire = Wire()
    wire.put(project="identity/i5", title="t", content="c")
    wire.put(project="nexus", title="t", content="c")
    assert [p[1]["project"] for p in wire.posts] == ["identity/i5", "nexus"]


def test_a_refusal_is_logged(monkeypatch) -> None:
    from structlog.testing import capture_logs

    holds(monkeypatch, "i5")
    engine(monkeypatch, {"kind": "ephemeral"})
    with capture_logs() as logs, pytest.raises(IdentityNotesRefused):
        notes.check_write("identity/i5")
    assert any(entry.get("event") == "identity_notes_write_refused" and entry.get("identity") == "i5" for entry in logs)


def test_the_mcp_tool_returns_the_refusal_as_an_error_line(monkeypatch) -> None:
    import contextlib

    from nexus.mcp import core

    holds(monkeypatch, "i5")
    engine(monkeypatch, {"kind": "ephemeral"})
    wire = Wire()
    db = SimpleNamespace(put=lambda **kw: wire.put(**kw), put_replacing=lambda **kw: wire.put_replacing(**kw))
    monkeypatch.setattr(core, "_t2_ctx", lambda: contextlib.nullcontext(db))
    out = core.memory_put(content="c", project="identity/i5", title="t")
    assert out.startswith("Error") and "ephemeral" in out and wire.posts == []


def test_the_cli_reports_the_refusal_as_one_line_never_a_traceback(monkeypatch) -> None:
    import contextlib

    from click.testing import CliRunner

    from nexus.commands.memory import memory

    holds(monkeypatch, "i5")
    engine(monkeypatch, {"kind": "ephemeral"})
    wire = Wire()
    db = SimpleNamespace(memory=wire)
    monkeypatch.setattr("nexus.commands.memory.t2_handle", lambda: contextlib.nullcontext(db))
    out = CliRunner().invoke(memory, ["put", "body", "--mine", "-t", "plan"])
    assert out.exit_code == 1 and "ephemeral" in out.output and "Traceback" not in out.output, out.output
    assert wire.posts == []
