# SPDX-License-Identifier: AGPL-3.0-or-later
"""``due:`` and ``worktree:`` on board threads (nexus-ct74m items 6 and 7): the parser, the due clock,
the console card and the SessionStart section. Pure: no T2, the search is handed in."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nexus.console.threads import build_board, due_at, due_threads, parse_session
from nexus.hooks.board_due import HEADING, due_lines, session_name

PST = timezone(timedelta(hours=-8))
SID = "f1b056f7-28cb-4c4e-8a76-cbcf9a50a212"

ENTRY = f"""session: {SID}
## RDR-230 readings
ref: nexus-4lnn1.1.8
state: A1 green
waiting_on: the clock (A6)
needs_sam: none
due: 2026-11-14
worktree: /Users/x/git/nexus-wt/marlowe-a6
## Mail recount
ref: nexus-1d6pz
state: counter rebuilt
due: 2026-11-17T09:00
## Re-measure
state: baseline stored
due: 2026-12-10T08:00:00Z
## No date
state: open
due: none
## Typo
state: open
due: next tuesday
"""


def _threads() -> dict:
    return {t.title: t for t in parse_session("marlowe", ENTRY).threads}


def test_due_and_worktree_are_thread_fields_not_notes() -> None:
    t = _threads()
    assert t["RDR-230 readings"].due == "2026-11-14"
    assert t["RDR-230 readings"].worktree == "/Users/x/git/nexus-wt/marlowe-a6"
    assert t["RDR-230 readings"].notes == ()
    assert t["Mail recount"].due == "2026-11-17T09:00"
    assert t["Mail recount"].worktree == ""


def test_a_due_line_ends_an_ask_block_like_any_thread_field() -> None:
    s = parse_session("m", "## T\nask: pick-port\nquestion: which port?\noption: 1\noption: 2\ndue: 2026-11-20\n")
    (t,) = s.threads
    assert t.due == "2026-11-20"
    assert [a.id for a in t.asks] == ["pick-port"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-14", datetime(2026, 11, 14, tzinfo=PST)),  # a date is due from local midnight
        ("2026-11-17T09:00", datetime(2026, 11, 17, 9, 0, tzinfo=PST)),  # no offset: local time
        ("2026-12-10T08:00:00Z", datetime(2026, 12, 10, 8, 0, tzinfo=timezone.utc)),
        ("2026-12-10T08:00:00+01:00", datetime(2026, 12, 10, 7, 0, tzinfo=timezone.utc)),
        ("none", None),
        ("", None),
        ("next tuesday", None),
        ("2026-13-01", None),
        # the pinned shapes only: what fromisoformat would also take is refused, so a regex reader agrees
        ("2026-11-17 09:00", None),
        ("20261117", None),
        ("2026-11-17T09", None),
        ("2026-11-17T09:00:30.5Z", datetime(2026, 11, 17, 9, 0, 30, 500000, tzinfo=timezone.utc)),
    ],
)
def test_due_at(value: str, expected: datetime | None) -> None:
    assert due_at(value, PST) == expected


def test_due_threads_are_those_at_or_past_their_time() -> None:
    s = parse_session("marlowe", ENTRY)
    before = datetime(2026, 11, 13, 23, 59, tzinfo=PST)
    assert due_threads(s, before) == ()
    at_midnight = datetime(2026, 11, 14, 0, 0, tzinfo=PST)
    assert [t.title for t, _ in due_threads(s, at_midnight)] == ["RDR-230 readings"]
    later = datetime(2026, 12, 11, tzinfo=PST)
    assert [t.title for t, _ in due_threads(s, later)] == ["RDR-230 readings", "Mail recount", "Re-measure"]


def test_due_threads_needs_an_aware_clock() -> None:
    with pytest.raises(ValueError, match="aware"):
        due_threads(parse_session("m", ENTRY), datetime(2026, 11, 14))


def test_the_board_card_names_its_due_threads() -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    board = build_board([{"title": "marlowe", "content": ENTRY, "timestamp": "2026-11-18T11:00:00Z"}], now)
    (card,) = board.cards
    assert set(card.due) == {"RDR-230 readings", "Mail recount"}
    assert card.bad_due == ("Typo",)


NO_TITLE = lambda _t: None  # noqa: E731 -- a seam stand-in: no entry by title


def _search(rows):
    calls = []

    def search(query: str):
        calls.append(query)
        return rows

    return search, calls


def test_session_start_prints_one_line_per_due_thread() -> None:
    search, calls = _search([{"title": "marlowe", "content": ENTRY}])
    lines = due_lines(SID, datetime(2026, 11, 18, 9, 0, tzinfo=PST), search=search, by_title=NO_TITLE, name="")
    assert calls == [SID]
    assert lines == [
        HEADING.format(entry="marlowe"),
        "- RDR-230 readings: due 2026-11-14 (ref nexus-4lnn1.1.8; worktree /Users/x/git/nexus-wt/marlowe-a6)",
        "- Mail recount: due 2026-11-17T09:00 (ref nexus-1d6pz)",
        "- Typo: `due: next tuesday` is not an ISO date or datetime, so it never comes due",
        "",
    ]
    assert "move or delete each due: line" in lines[0]


def test_session_start_prints_nothing_when_nothing_is_due() -> None:
    search, _ = _search([{"title": "marlowe", "content": ENTRY}])
    clean = ENTRY.replace("due: next tuesday", "due: none")
    search, _ = _search([{"title": "marlowe", "content": clean}])
    assert due_lines(SID, datetime(2026, 11, 13, 9, 0, tzinfo=PST), search=search, by_title=NO_TITLE, name="") == []


def test_only_the_entry_whose_session_line_matches_counts() -> None:
    # Another session's entry that merely mentions this id (in a note) must not be read as ours.
    other = f"session: 0000aaaa-0000-0000-0000-000000000000\n## Theirs\nstate: mentions {SID}\ndue: 2026-01-01\n"
    search, _ = _search([{"title": "bob", "content": other}])
    assert due_lines(SID, datetime(2026, 11, 18, tzinfo=PST), search=search, by_title=NO_TITLE, name="") == []


def test_a_failing_search_or_no_session_id_prints_nothing() -> None:
    def boom(_q: str):
        raise OSError("engine down")

    assert due_lines(SID, datetime(2026, 11, 18, tzinfo=PST), search=boom, by_title=NO_TITLE, name="") == []
    search, calls = _search([])
    assert due_lines("", datetime(2026, 11, 18, tzinfo=PST), search=search, by_title=NO_TITLE, name="") == []
    assert calls == []


def test_after_a_restart_the_entry_is_found_by_the_session_name() -> None:
    # A restart, /clear, resume or fork gives a new session id; the entry's session: line still names the old one.
    new_sid = "11111111-2222-3333-4444-555555555555"
    rows = [{"title": "marlowe", "content": ENTRY}]
    search, calls = _search(rows)
    titles: list[str] = []

    def by_title(t: str):
        titles.append(t)
        return {"title": "marlowe", "content": ENTRY} if t == "marlowe" else None

    now = datetime(2026, 11, 14, 9, 0, tzinfo=PST)
    lines = due_lines(new_sid, now, search=search, by_title=by_title, name="marlowe")
    assert calls == [new_sid] and titles == ["marlowe"]
    assert lines[1].startswith("- RDR-230 readings: due 2026-11-14")
    # no name: nothing to fall back to
    assert due_lines(new_sid, now, search=search, by_title=by_title, name="") == []
    # a prefix match from the store is not the session's entry
    assert due_lines(new_sid, now, search=search, by_title=lambda t: {"title": "marlowe2", "content": ENTRY},
                     name="marlowe") == []


def test_the_session_line_match_wins_and_the_title_is_not_read() -> None:
    search, _ = _search([{"title": "marlowe", "content": ENTRY}])
    titles: list[str] = []
    due_lines(SID, datetime(2026, 11, 18, tzinfo=PST), search=search,
              by_title=lambda t: titles.append(t), name="marlowe")
    assert titles == []


def test_every_entry_carrying_the_session_line_is_read() -> None:
    second = f"session: {SID}\n## Other\ndue: 2026-11-01\n"
    search, _ = _search([{"title": "marlowe", "content": f"session: {SID}\n## Q\nstate: x\n"},
                         {"title": "marlowe_old", "content": second}])
    lines = due_lines(SID, datetime(2026, 11, 18, tzinfo=PST), search=search, by_title=NO_TITLE, name="")
    assert lines[0] == HEADING.format(entry="marlowe_old") and lines[1] == "- Other: due 2026-11-01"


def test_session_name_reads_the_claude_sessions_file(tmp_path) -> None:
    import json  # noqa: PLC0415

    d = tmp_path / "sessions"
    d.mkdir()
    (d / "123.json").write_text(json.dumps({"sessionId": SID, "name": "marlowe"}), encoding="utf-8")
    (d / "456.json").write_text(json.dumps({"sessionId": "other", "name": "bob"}), encoding="utf-8")
    (d / "bad.json").write_text("{not json", encoding="utf-8")
    assert session_name(SID, tmp_path) == "marlowe"
    assert session_name("nobody", tmp_path) == ""
    assert session_name(SID, tmp_path / "missing") == ""


def test_the_console_card_shows_due_and_worktree_and_marks_what_has_come() -> None:
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from nexus.console.app import create_app  # noqa: PLC0415
    from nexus.console.threads import BeadsResult  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%MZ")
    soon = (now + timedelta(days=30)).strftime("%Y-%m-%d")
    content = (f"## Past\nstate: open\ndue: 2026-01-01\nworktree: /w/marlowe-past\n"
               f"## Future\nstate: open\ndue: {soon}\n")
    rows = [{"title": "marlowe", "content": content, "timestamp": stamp}]
    client = TestClient(create_app(bd_runner=lambda: BeadsResult(()), threads_reader=lambda: rows,
                                   answers_reader=lambda: []))
    html = client.get("/threads").text
    assert '<strong class="due-now">&middot; due 2026-01-01</strong>' in html
    assert f'<span class="ts">&middot; due {soon}</span>' in html
    assert "worktree /w/marlowe-past" in html
    rows[0]["content"] += "## Typo\ndue: soon\n"
    assert "(not an ISO date or datetime: never comes due)" in client.get("/threads").text


def test_session_context_puts_the_due_section_after_the_t1_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.hooks import board_due, session_context  # noqa: PLC0415

    seen: list[str] = []

    def fake(session_id: str) -> list[str]:
        seen.append(session_id)
        return ["## Due on your threads board (marlowe)", "- X: due 2026-11-14", ""]

    monkeypatch.setattr(board_due, "due_lines", fake)
    monkeypatch.setattr(session_context, "which", lambda _cmd: None)
    out = session_context.run({"session_id": SID}).stdout or ""
    assert seen == [SID]
    t1_end = out.index(session_context._T1_TOP_LINES[-1])
    assert t1_end < out.index("## Due on your threads board (marlowe)")
    seen.clear()
    session_context.run({})
    session_context.run(None)
    assert seen == []
