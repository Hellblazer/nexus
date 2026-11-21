# SPDX-License-Identifier: AGPL-3.0-or-later
"""The client refuses a write to ``identity/<id>`` unless the caller is that seat (RDR-253 Step 6 part 3, nexus-hczdo).

The engine fences the ``threads`` project only; it takes a write to ``identity/<id>`` from anyone. So the client refuses,
before any request, with one line, a write from an ephemeral or delegated identity, from a seat to another identity's
project, and when the caller's kind cannot be learned. A refused or failed check is logged and never retried. A process
whose Claude ancestor has NO token file (a person at a shell) is not refused. The check is client-side only: a caller
that never goes through ``HttpMemoryStore`` (raw HTTP, a process with no Claude ancestor) is not stopped by it.

The caller is the identity the token FILE names, expired token or not: ``token_header.current()`` answers None for a lapsed
token and that must never read as "no identity".
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nexus.db.t2.http_memory_store import HttpMemoryStore
from nexus.identity import notes
from nexus.identity.notes import IdentityNotesRefused
from nexus.identity.token_file import TokenFile, write_token_file


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


@pytest.fixture
def holds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """``holds("i5")`` makes this process's token file name i5 with a live token; ``expired=True`` a lapsed one;
    ``token=None`` an engine that issues none; ``holds(None)`` no token file at all; ``delegated=True`` a child."""
    from nexus.identity import token_header

    def _holds(identity: str | None, *, expired: bool = False, token: str | None = "tok", delegated: bool = False) -> None:
        path = tmp_path / "1.1.token"
        path.unlink(missing_ok=True)
        if identity is not None:
            write_token_file(path, TokenFile(
                identity=identity, generation=1, token=token,
                expires_at=None if token is None else time.time() + (-3600 if expired else 3600),
                session_id="s", pid=1, started_at=1, proc_start=None))
        monkeypatch.setattr(token_header, "token_file_path", lambda: path if identity is not None else None)
        if delegated:
            monkeypatch.setenv("NX_IDENTITY_TOKEN", str(path))
        else:
            monkeypatch.delenv("NX_IDENTITY_TOKEN", raising=False)

    return _holds


def engine(monkeypatch: pytest.MonkeyPatch, view: dict[str, Any] | Exception) -> Client:
    client = Client(view)
    monkeypatch.setattr("nexus.identity.bind_client.IdentityClient.from_config", classmethod(lambda cls, **k: client))
    return client


def test_a_seat_may_write_its_own_notes(monkeypatch, holds) -> None:
    holds("i5")
    engine(monkeypatch, {"id": "i5", "kind": "seat"})
    notes.check_write("identity/i5")


# ── whose project: a seat writes only its own ───────────────────────────────


def test_a_seat_may_not_write_another_identitys_project(monkeypatch, holds) -> None:
    holds("i5")
    engine(monkeypatch, {"id": "i5", "kind": "seat"})
    with pytest.raises(IdentityNotesRefused, match="own") as refused:
        notes.check_write("identity/i6")
    assert "identity/i5" in str(refused.value) and "\n" not in str(refused.value)


def test_a_process_with_no_token_file_may_write_any_identity_project(monkeypatch, holds) -> None:
    """A person at a shell (an operator tidying, a migration): the engine does not fence the project."""
    holds(None)
    client = engine(monkeypatch, {"kind": "ephemeral"})
    notes.check_write("identity/i6")
    assert client.shown == []


# ── the caller is read from the token FILE, expiry or not ───────────────────


@pytest.mark.parametrize("state", ["expired", "no_token"])
def test_an_ephemeral_whose_token_lapsed_is_still_refused(monkeypatch, holds, state) -> None:
    """token_header.current() is None for a lapsed token (or an engine that issues none); that is not "no identity"."""
    holds("i5", expired=state == "expired", token=None if state == "no_token" else "tok")
    engine(monkeypatch, {"id": "i5", "kind": "ephemeral"})
    with pytest.raises(IdentityNotesRefused, match="ephemeral"):
        notes.check_write("identity/i5")


def test_a_seat_whose_token_lapsed_still_writes_its_own_notes(monkeypatch, holds) -> None:
    holds("i5", expired=True)
    engine(monkeypatch, {"id": "i5", "kind": "seat"})
    notes.check_write("identity/i5")


@pytest.mark.parametrize("kind", ["ephemeral", "delegated"])
def test_an_ephemeral_or_delegated_identity_is_refused_in_one_line(monkeypatch, holds, kind) -> None:
    holds("i5")
    engine(monkeypatch, {"id": "i5", "kind": kind})
    with pytest.raises(IdentityNotesRefused) as refused:
        notes.check_write("identity/i5")
    message = str(refused.value)
    assert kind in message and "\n" not in message and "seat" in message


def test_a_delegated_child_is_refused_without_asking_the_engine(monkeypatch, holds) -> None:
    """``NX_IDENTITY_TOKEN`` names a delegated child's token file: the environment says it, no lookup is needed."""
    holds("i9", delegated=True)
    client = engine(monkeypatch, {"id": "i9", "kind": "seat"})   # would pass if it were asked
    with pytest.raises(IdentityNotesRefused, match="delegated") as refused:
        notes.check_write("identity/i7")
    assert client.shown == [] and "\n" not in str(refused.value)


def test_a_kind_that_cannot_be_learned_refuses_never_guesses(monkeypatch, holds) -> None:
    from nexus.identity.bind_client import IdentityUnavailableError

    holds("i5")
    engine(monkeypatch, IdentityUnavailableError("connection refused"))
    with pytest.raises(IdentityNotesRefused, match="could not confirm"):
        notes.check_write("identity/i5")


def test_every_other_project_is_untouched_and_costs_no_lookup(monkeypatch, holds) -> None:
    holds("i5")
    client = engine(monkeypatch, {"kind": "ephemeral"})
    for project in ("nexus", "threads", "identityish/i5", "identity", "identity/", "my-identity/i5"):
        notes.check_write(project)
    assert client.shown == []


# ── the cache holds only a seat ─────────────────────────────────────────────


def test_a_seat_is_asked_once_per_process_and_the_client_is_closed(monkeypatch, holds) -> None:
    holds("i5")
    client = engine(monkeypatch, {"kind": "seat"})
    notes.check_write("identity/i5")
    notes.check_write("identity/i5")
    assert client.shown == ["i5"] and client.closed == 1


def test_a_promote_takes_effect_without_a_restart(monkeypatch, holds) -> None:
    """Only "seat" is remembered: an ephemeral that is promoted (``nx identity promote``) writes from then on."""
    holds("i5")
    engine(monkeypatch, {"kind": "ephemeral"})
    with pytest.raises(IdentityNotesRefused):
        notes.check_write("identity/i5")
    engine(monkeypatch, {"kind": "seat"})
    notes.check_write("identity/i5")


def test_a_failed_lookup_is_not_cached_as_a_verdict(monkeypatch, holds) -> None:
    from nexus.identity.bind_client import IdentityUnavailableError

    holds("i5")
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
        return {"id": 1, "action": "inserted", "title": payload.get("title"), "content": payload.get("content"),
                "imported": len(payload.get("rows", ())), "deleted": True,
                "replaced_id": 2, "replaced_title": payload.get("replaces"), "replaced_content": "old"}

    def _delete(self, path: str, params: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        self.posts.append((path, dict(params or {})))
        return {"deleted": True}


def _calls(wire: Wire, project: str) -> dict[str, Any]:
    """Each project-keyed write of the store, as a thunk, against *project*."""
    return {
        "put": lambda: wire.put(project=project, title="t", content="c"),
        "put_or_merge": lambda: wire.put_or_merge(project=project, title="t", content="c"),
        "put_replacing": lambda: wire.put_replacing(project=project, title="t", content="c", replaces="old"),
        "import_entry": lambda: wire.import_entry(project=project, title="t", content="c", timestamp="2026-01-01T00:00:00Z"),
        "import_entries_batch": lambda: wire.import_entries_batch([
            wire.build_import_row("nexus", "ok", "c", "2026-01-01T00:00:00Z"),
            wire.build_import_row(project, "t", "c", "2026-01-01T00:00:00Z")]),
        "delete": lambda: wire.delete(project=project, title="t"),
        "insert_summary": lambda: wire.insert_summary(project=project, content="c", source_ids=[1], model="m"),
    }


VERBS = ["put", "put_or_merge", "put_replacing", "import_entry", "import_entries_batch", "delete", "insert_summary"]


@pytest.mark.parametrize("verb", VERBS)
def test_the_store_refuses_before_any_request(monkeypatch, holds, verb) -> None:
    holds("i5")
    engine(monkeypatch, {"kind": "ephemeral"})
    wire = Wire()
    with pytest.raises(IdentityNotesRefused):
        _calls(wire, "identity/i5")[verb]()
    assert wire.posts == [], "a refused write sends nothing (a batch sends none of its rows) and is not retried"


@pytest.mark.parametrize("verb", VERBS)
def test_a_seat_writing_another_seats_project_is_refused_at_the_store(monkeypatch, holds, verb) -> None:
    holds("i5")
    engine(monkeypatch, {"kind": "seat"})
    wire = Wire()
    with pytest.raises(IdentityNotesRefused, match="own"):
        _calls(wire, "identity/i6")[verb]()
    assert wire.posts == []


@pytest.mark.parametrize("verb", VERBS)
def test_the_store_still_writes_for_a_seat_to_its_own_project_and_everyone_to_other_projects(monkeypatch, holds, verb) -> None:
    holds("i5")
    engine(monkeypatch, {"kind": "seat"})
    wire = Wire()
    _calls(wire, "identity/i5")[verb]()
    assert wire.posts, "a seat's own project is written"
    ephemeral = Wire()
    notes.reset_cache()
    engine(monkeypatch, {"kind": "ephemeral"})
    _calls(ephemeral, "nexus")[verb]()
    assert ephemeral.posts, "any other project is never checked"


def test_the_id_keyed_paths_are_not_covered(monkeypatch, holds) -> None:
    """``delete(id=)``, ``merge_memories`` and ``restore`` name a row by id, so the store cannot know its project
    without a lookup: documented as not covered, and this pins that a delete by id sends its request unchecked."""
    holds("i5")
    client = engine(monkeypatch, {"kind": "ephemeral"})
    wire = Wire()
    wire.delete(id=7)
    assert wire.posts and client.shown == []


def test_a_refusal_is_logged(monkeypatch, holds) -> None:
    from structlog.testing import capture_logs

    holds("i5")
    engine(monkeypatch, {"kind": "ephemeral"})
    with capture_logs() as logs, pytest.raises(IdentityNotesRefused):
        notes.check_write("identity/i5")
    assert any(entry.get("event") == "identity_notes_write_refused" and entry.get("identity") == "i5" for entry in logs)


def test_the_mcp_tool_returns_the_refusal_as_an_error_line(monkeypatch, holds) -> None:
    import contextlib

    from nexus.mcp import core

    holds("i5")
    engine(monkeypatch, {"kind": "ephemeral"})
    wire = Wire()
    db = SimpleNamespace(put=lambda **kw: wire.put(**kw), put_replacing=lambda **kw: wire.put_replacing(**kw))
    monkeypatch.setattr(core, "_t2_ctx", lambda: contextlib.nullcontext(db))
    out = core.memory_put(content="c", project="identity/i5", title="t")
    assert out.startswith("Error") and "ephemeral" in out and wire.posts == []


def test_the_cli_reports_the_refusal_as_one_line_never_a_traceback(monkeypatch, holds) -> None:
    import contextlib

    from click.testing import CliRunner

    from nexus.commands.memory import memory
    from nexus.identity import token_header

    holds("i5")
    monkeypatch.setattr(token_header, "current", lambda **k: SimpleNamespace(identity="i5"))
    engine(monkeypatch, {"kind": "ephemeral"})
    wire = Wire()
    db = SimpleNamespace(memory=wire)
    monkeypatch.setattr("nexus.commands.memory.t2_handle", lambda: contextlib.nullcontext(db))
    out = CliRunner().invoke(memory, ["put", "body", "--mine", "-t", "plan"])
    assert out.exit_code == 1 and "ephemeral" in out.output and "Traceback" not in out.output, out.output
    assert wire.posts == []


def test_the_cli_delete_reports_the_refusal_as_one_line_never_a_traceback(monkeypatch, holds) -> None:
    """Confirmation review r2 suggestion 2: a refused `nx memory delete` of an identity/<id> note was a traceback."""
    import contextlib

    from click.testing import CliRunner

    from nexus.commands.memory import memory
    from nexus.identity import token_header

    holds("i5")
    monkeypatch.setattr(token_header, "current", lambda **k: SimpleNamespace(identity="i5"))
    engine(monkeypatch, {"kind": "ephemeral"})
    wire = Wire()
    db = SimpleNamespace(memory=wire)
    monkeypatch.setattr("nexus.commands.memory.t2_handle", lambda: contextlib.nullcontext(db))
    out = CliRunner().invoke(memory, ["delete", "--project", "identity/i5", "--title", "plan", "--yes"])
    assert out.exit_code == 1 and "ephemeral" in out.output and "Traceback" not in out.output, out.output
    assert wire.posts == []


def test_write_refusal_answers_what_check_write_raises_and_logs_no_refusal(monkeypatch, holds) -> None:
    """gh#1680: ``nx brief me`` asks whether a write would be refused without trying one. ``write_refusal`` is the one
    predicate ``check_write`` raises from, and it leaves no ``identity_notes_write_refused`` line for a write nobody made."""
    events: list[str] = []

    class Recorder:
        def warning(self, event: str, **kw: Any) -> None:
            events.append(event)

    monkeypatch.setattr(notes, "_log", Recorder())
    holds("i5")
    engine(monkeypatch, {"id": "i5", "kind": "ephemeral"})
    refusal = notes.write_refusal("identity/i5")
    assert refusal is not None and refusal[0] == "i5" and "keeps no identity notes" in refusal[1]
    assert events == [], "the predicate logs no refusal"
    with pytest.raises(IdentityNotesRefused) as exc:
        notes.check_write("identity/i5")
    assert str(exc.value) == refusal[1]
    assert events == ["identity_notes_write_refused"], "check_write logs the refusal it raises"


def test_write_refusal_is_none_for_a_seat_and_for_a_project_that_is_not_identity_notes(monkeypatch, holds) -> None:
    holds("i5")
    engine(monkeypatch, {"id": "i5", "kind": "seat"})
    assert notes.write_refusal("identity/i5") is None
    assert notes.write_refusal("some-project") is None
