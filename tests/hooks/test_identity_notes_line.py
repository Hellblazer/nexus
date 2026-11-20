# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart's one identity-notes line (RDR-253 Step 6 part 3, nexus-hczdo).

``nexus.hooks.identity_notes.notes_lines`` prints at most ONE line: up to five titles of the bound identity's own T2
project ``identity/<id>``, newest first. It prints nothing for an empty project or a session with no identity, and a
read that fails or runs long prints nothing too: SessionStart must never wait on it or break on it.
"""
from __future__ import annotations

import contextlib
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from nexus.hooks import identity_notes, session_context
from nexus.identity.token_file import TokenFile, identity_dir, write_token_file


class Store:
    """The slice of ``HttpMemoryStore`` the hook may use: ``list_entries`` only (a ``get`` or ``search`` would count as a
    read of an entry in the transcript-and-engine measures that retire this guidance)."""

    def __init__(self, titles: list[str]) -> None:
        self.titles = titles
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_entries(self, **kw: Any) -> list[dict[str, Any]]:
        self.calls.append(("list_entries", kw))
        limit = kw.get("limit")
        rows = [{"title": t} for t in self.titles]
        return rows[:limit] if limit else rows


def lines(titles: list[str], identity: str = "i5", **kw: Any) -> list[str]:
    return identity_notes.notes_lines("sess-1", identity=identity, store=Store(titles), **kw)


def test_lists_titles_newest_first_for_the_bound_identity() -> None:
    store = Store(["newest", "middle", "oldest"])
    out = identity_notes.notes_lines("sess-1", identity="i5", store=store)
    assert out == ["Your notes (identity/i5): newest, middle, oldest"]
    assert [c[0] for c in store.calls] == ["list_entries"], "a title list only: no get or search"
    kw = store.calls[0][1]
    assert kw["project"] == "identity/i5" and kw["limit"] == 50
    assert kw["retry_read_timeout"] is False and kw["timeout"] > 0, "one bounded attempt, as board_due makes"


def test_more_than_five_titles_are_counted_not_listed() -> None:
    assert lines([f"t{i}" for i in range(7)]) == ["Your notes (identity/i5): t0, t1, t2, t3, t4 (+2 more)"]


def test_exactly_five_has_no_overflow_note() -> None:
    assert lines([f"t{i}" for i in range(5)]) == ["Your notes (identity/i5): t0, t1, t2, t3, t4"]


def test_a_read_that_hit_the_row_cap_says_the_count_is_a_lower_bound() -> None:
    """The read is capped at 50 rows: with 50 back there may be more, so the count is "+45+", not "+45"."""
    assert lines([f"t{i}" for i in range(60)])[0].endswith("t4 (+45+ more)")
    assert lines([f"t{i}" for i in range(49)])[0].endswith("t4 (+44 more)")


def test_an_empty_project_prints_nothing() -> None:
    assert lines([]) == []


def test_a_session_with_no_identity_prints_nothing_and_reads_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    from nexus.identity import token_header

    monkeypatch.setattr(token_header, "token_file_path", lambda: None)
    store = Store(["a"])
    assert identity_notes.notes_lines("sess-1", store=store) == []
    assert identity_notes.notes_lines("", store=store) == []
    assert store.calls == []


def test_the_line_is_one_line_whatever_the_titles_hold() -> None:
    out = lines(["multi\nline\r\ntitle " + "x" * 400, "b"])
    assert len(out) == 1 and "\n" not in out[0] and "\r" not in out[0]
    assert len(out[0].encode("utf-8")) <= identity_notes.MAX_LINE


def test_the_line_stays_inside_its_byte_cap_for_multibyte_titles() -> None:
    """The SessionStart budgets count bytes, and a CJK character or the cut mark ``…`` is three of them."""
    out = lines(["注" * 80 + str(i) for i in range(6)])
    assert len(out) == 1 and len(out[0].encode("utf-8")) <= identity_notes.MAX_LINE
    assert out[0].endswith("…") and out[0].startswith("Your notes (identity/i5): ")


# ── a read that fails prints nothing, and says why ──────────────────────────


@pytest.fixture
def hook_logging(monkeypatch) -> list[str]:
    """``configure_hook_logging`` rewires the process's logging (``basicConfig(force=True)``, ``structlog.configure``, the
    hook log file): a test never runs the real one. It records which thread asked."""
    from nexus._hook_runtime import _io

    seen: list[str] = []
    monkeypatch.setattr(_io, "configure_hook_logging", lambda: seen.append(threading.current_thread().name))
    return seen


def test_a_read_that_raises_prints_nothing_and_logs_why() -> None:
    class Boom(Store):
        def list_entries(self, **kw):
            raise RuntimeError("engine down")

    with capture_logs() as logs:
        assert identity_notes.notes_lines("sess-1", identity="i5", store=Boom([])) == []
    failed = [e for e in logs if e.get("event") == "identity_notes_lookup_failed"]
    assert failed and "RuntimeError: engine down" in failed[0]["error"]


def test_a_store_that_cannot_be_built_prints_nothing_and_logs_why(monkeypatch, hook_logging) -> None:
    def refuse(timeout: float) -> Any:
        raise RuntimeError("no endpoint")

    monkeypatch.setattr(identity_notes, "_engine_store", refuse)
    with capture_logs() as logs:
        assert identity_notes.notes_lines("sess-1", identity="i5") == []
    assert any(e.get("event") == "identity_notes_lookup_failed" and "no endpoint" in e["error"] for e in logs)


def test_a_read_over_budget_prints_nothing_within_the_budget_and_logs_it() -> None:
    release = threading.Event()

    class Slow(Store):
        def list_entries(self, **kw):
            release.wait(10)
            return [{"title": "late"}]

    started = time.monotonic()
    try:
        with capture_logs() as logs:
            out = identity_notes.notes_lines("sess-1", identity="i5", store=Slow([]), budget_s=0.3)
    finally:
        release.set()
    assert out == [] and time.monotonic() - started < 3
    assert any(e.get("event") == "identity_notes_over_budget" for e in logs)


def test_hook_logging_is_set_up_on_the_main_thread_before_the_read_and_the_mint_is_cut_to_the_budget(
    monkeypatch, hook_logging
) -> None:
    """As ``board_due`` does: the store's own log lines must not reach the hook's stdout, and a data-token mint inside the
    worker is cut to the budget (``mint_deadline``), so a slow mint cannot hold the rest of SessionStart."""
    from nexus.db import data_token

    minted: list[float] = []

    @contextlib.contextmanager
    def deadline(seconds: float):
        minted.append(seconds)
        yield

    monkeypatch.setattr(data_token, "mint_deadline", deadline)
    monkeypatch.setattr(identity_notes, "_engine_store", lambda timeout: Store(["a"]))
    assert identity_notes.notes_lines("sess-1", identity="i5", budget_s=1.25) == ["Your notes (identity/i5): a"]
    assert hook_logging == [threading.main_thread().name], "configured once, on the thread that spawns the worker"
    assert minted == [1.25]


# ── the identity: read from the token file the session-start hook wrote, in either hook order ──


def _bound(config: Path, *, name: str, session_ids: tuple[str, ...], identity: str = "i5") -> Path:
    path = identity_dir(config) / f"{name}.token"
    write_token_file(path, TokenFile(
        identity=identity, generation=1, token="tok", expires_at=time.time() + 3600, session_id=session_ids[-1],
        pid=1, started_at=1, proc_start=None, session_ids=session_ids))
    return path


@pytest.fixture
def config(monkeypatch, tmp_path) -> Path:
    from nexus.identity import token_header

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("NX_IDENTITY_TOKEN", raising=False)
    monkeypatch.setattr(token_header, "token_file_path", lambda: None)
    return tmp_path


def test_the_line_reads_the_identity_the_session_start_hook_bound(config: Path) -> None:
    """Hook order, bind first: ``nx-hook session-start`` has written the token file naming the new session id, and the line
    finds the identity through it (the real ``identity_for_session``, no patch)."""
    _bound(config, name="10.1", session_ids=("sess-old", "sess-new"))
    assert identity_notes.notes_lines("sess-new", store=Store(["a"])) == ["Your notes (identity/i5): a"]


def test_the_line_falls_back_to_the_ancestors_token_file_when_the_bind_has_not_listed_the_session_yet(
    config: Path, monkeypatch
) -> None:
    """Hook order, bind not yet (Claude Code starts a matcher group's hooks without an order): after ``/clear`` the
    process's token file still names the old session id only. The session is not found by id, so the hook reads the
    token file of its own Claude ancestor."""
    from nexus.identity import token_header

    path = _bound(config, name="10.1", session_ids=("sess-old",))
    monkeypatch.setattr(token_header, "token_file_path", lambda: path)
    assert identity_notes.notes_lines("sess-new", store=Store(["a"])) == ["Your notes (identity/i5): a"]


def test_a_fresh_start_the_bind_has_not_reached_prints_nothing(config: Path) -> None:
    """No token file anywhere yet: the line is missing for this start, silently (the next start finds it)."""
    store = Store(["a"])
    assert identity_notes.notes_lines("sess-new", store=store) == [] and store.calls == []


def test_a_delegated_child_never_falls_back_to_its_token_file(config: Path, monkeypatch) -> None:
    """A delegated child keeps no identity notes (``nexus.identity.notes``), so its token file names no notes to list."""
    from nexus.identity import token_header

    path = _bound(config, name="10.1", session_ids=("sess-old",), identity="i9")
    monkeypatch.setattr(token_header, "token_file_path", lambda: path)
    monkeypatch.setenv("NX_IDENTITY_TOKEN", str(path))
    store = Store(["a"])
    assert identity_notes.notes_lines("sess-new", store=store) == [] and store.calls == []


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
