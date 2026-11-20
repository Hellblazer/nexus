# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart's one identity-notes line (RDR-253 Step 6 part 3, nexus-hczdo).

``nexus.hooks.identity_notes.notes_lines`` prints at most ONE line: up to five titles of the bound identity's own T2
project ``identity/<id>``, newest first. It prints nothing for an empty project or a session with no identity, and a
read that fails or runs long prints nothing too: SessionStart must never wait on it or break on it.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from nexus.hooks import identity_notes, session_context


class Store:
    """The slice of ``HttpMemoryStore`` the hook may use: ``list_entries`` only (a ``get`` or ``search`` would count as a
    read of the entry and move ``access_count``, which is how the 30-day retire rule measures use)."""

    def __init__(self, titles: list[str]) -> None:
        self.titles = titles
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_entries(self, **kw: Any) -> list[dict[str, Any]]:
        self.calls.append(("list_entries", kw))
        limit = kw.get("limit")
        rows = [{"title": t} for t in self.titles]
        return rows[:limit] if limit else rows


def lines(titles: list[str], identity: str = "i5", **kw: Any) -> list[str]:
    store = Store(titles)
    return identity_notes.notes_lines("sess-1", identity=identity, store=store, **kw)


def test_lists_titles_newest_first_for_the_bound_identity() -> None:
    store = Store(["newest", "middle", "oldest"])
    out = identity_notes.notes_lines("sess-1", identity="i5", store=store)
    assert out == ["Your notes (identity/i5): newest, middle, oldest"]
    assert store.calls[0][0] == "list_entries" and store.calls[0][1]["project"] == "identity/i5"
    assert [c[0] for c in store.calls] == ["list_entries"], "a title list only: no get or search that would bump access_count"


def test_more_than_five_titles_are_counted_not_listed() -> None:
    out = lines([f"t{i}" for i in range(7)])
    assert out == ["Your notes (identity/i5): t0, t1, t2, t3, t4 (+2 more)"]


def test_exactly_five_has_no_overflow_note() -> None:
    assert lines([f"t{i}" for i in range(5)]) == ["Your notes (identity/i5): t0, t1, t2, t3, t4"]


def test_an_empty_project_prints_nothing() -> None:
    assert lines([]) == []


def test_a_session_with_no_identity_prints_nothing_and_reads_nothing(monkeypatch) -> None:
    store = Store(["a"])
    monkeypatch.setattr("nexus.identity.board.identity_for_session", lambda sid, *a, **k: "")
    assert identity_notes.notes_lines("sess-1", store=store) == []
    assert identity_notes.notes_lines("", store=store) == []
    assert store.calls == []


def test_the_identity_comes_from_the_sessions_token_file(monkeypatch) -> None:
    monkeypatch.setattr("nexus.identity.board.identity_for_session", lambda sid, *a, **k: "i9" if sid == "sess-1" else "")
    store = Store(["a"])
    assert identity_notes.notes_lines("sess-1", store=store) == ["Your notes (identity/i9): a"]


def test_the_line_is_one_line_whatever_the_titles_hold() -> None:
    out = lines(["multi\nline\r\ntitle " + "x" * 400, "b"])
    assert len(out) == 1 and "\n" not in out[0] and "\r" not in out[0]
    assert len(out[0]) <= identity_notes.MAX_LINE


def test_a_read_that_raises_prints_nothing_and_never_raises(caplog) -> None:
    class Boom(Store):
        def list_entries(self, **kw):
            raise RuntimeError("engine down")

    assert identity_notes.notes_lines("sess-1", identity="i5", store=Boom([])) == []


def test_a_read_over_budget_prints_nothing_within_the_budget() -> None:
    release = threading.Event()

    class Slow(Store):
        def list_entries(self, **kw):
            release.wait(10)
            return [{"title": "late"}]

    started = time.monotonic()
    try:
        out = identity_notes.notes_lines("sess-1", identity="i5", store=Slow([]), budget_s=0.3)
    finally:
        release.set()
    assert out == [] and time.monotonic() - started < 3


def test_a_store_that_cannot_be_built_prints_nothing(monkeypatch) -> None:
    def refuse(timeout: float) -> Any:
        raise RuntimeError("no endpoint")

    monkeypatch.setattr(identity_notes, "_engine_store", refuse)
    assert identity_notes.notes_lines("sess-1", identity="i5") == []


# ── the hook adds exactly that one line ─────────────────────────────────────


@pytest.fixture
def quiet_hook(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(session_context, "which", lambda cmd: False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr("nexus.hooks.board_due.due_lines", lambda sid, **k: [])


def test_session_start_adds_exactly_one_line_when_there_are_notes(monkeypatch, quiet_hook) -> None:
    base = session_context.run({"session_id": "sess-1"}).stdout
    monkeypatch.setattr(identity_notes, "notes_lines", lambda sid, **k: ["Your notes (identity/i5): a, b"])
    with_notes = session_context.run({"session_id": "sess-1"}).stdout
    extra = [ln for ln in with_notes.split("\n") if ln not in base.split("\n")]
    assert extra == ["Your notes (identity/i5): a, b"]
    assert with_notes.count("\n") - base.count("\n") == 1


def test_session_start_is_unchanged_when_there_are_no_notes(monkeypatch, quiet_hook) -> None:
    monkeypatch.setattr(identity_notes, "notes_lines", lambda sid, **k: [])
    plain = session_context.run({"session_id": "sess-1"}).stdout
    monkeypatch.setattr(identity_notes, "notes_lines", lambda sid, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert session_context.run({"session_id": "sess-1"}).stdout == plain, "a failing lookup leaves the context as it was"


def test_a_payload_without_a_session_id_asks_nothing(monkeypatch, quiet_hook) -> None:
    asked: list[str] = []
    monkeypatch.setattr(identity_notes, "notes_lines", lambda sid, **k: asked.append(sid) or [])
    session_context.run({})
    session_context.run(None)
    assert asked == []
