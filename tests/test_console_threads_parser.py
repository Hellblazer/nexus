# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure tests for the console Threads parser (nexus-i99vk). No T2, no I/O."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nexus.console.threads import (
    Bead,
    BeadsResult,
    build_board,
    format_age,
    has_value,
    headline,
    merge_asks,
    parse_beads_json,
    parse_session,
    parse_timestamp,
)

NOW = datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc)

SAMPLE = """session: nexus_c9c
updated: 2026-10-25T18:05Z
## RDR-234 auto-continuation
ref: RDR-234, nexus-qlrj7
state: spike proven; rewriting the draft
waiting_on: none
needs_sam: none
## nx console Threads panel
ref: nexus-i99vk
state: building
waiting_on: review
needs_sam: pick the port
"""


def test_parses_threads_and_drops_the_header_lines() -> None:
    s = parse_session("nexus_c9c", SAMPLE)
    assert s.name == "nexus_c9c"
    assert [t.title for t in s.threads] == [
        "RDR-234 auto-continuation",
        "nx console Threads panel",
    ]
    first, second = s.threads
    assert first.refs == ("RDR-234", "nexus-qlrj7")
    assert first.state == "spike proven; rewriting the draft"
    assert first.waiting_on == "none"
    assert first.needs_sam == "none"
    assert second.refs == ("nexus-i99vk",)
    assert second.needs_sam == "pick the port"
    # `session:` and `updated:` are ignored, not shown as notes anywhere.
    assert s.notes == ()
    assert all(t.notes == () for t in s.threads)


def test_unknown_lines_are_kept_as_notes() -> None:
    text = (
        "random preamble line\n"
        "updated: 2026-10-25T18:05Z\n"
        "## T1\n"
        "state: x\n"
        "owner: somebody\n"
        "free text under the thread\n"
    )
    s = parse_session("a", text)
    assert "random preamble line" in s.notes
    t = s.threads[0]
    assert t.state == "x"
    assert "owner: somebody" in t.notes
    assert "free text under the thread" in t.notes


@pytest.mark.parametrize("line", ["updated: 2026-10-25T18:05Z", "updated: junk", "session: other", "Session: x"])
def test_updated_and_session_lines_are_ignored_not_notes(line: str) -> None:
    pre = parse_session("a", f"{line}\n## T\nstate: x\n")
    assert pre.notes == ()
    assert len(pre.threads) == 1 and pre.threads[0].notes == ()
    inside = parse_session("a", f"## T\nstate: x\n{line}\n")
    assert inside.threads[0].notes == ()


def test_headingless_fields_belong_to_an_implicit_thread_named_for_the_session() -> None:
    s = parse_session("nexus_c9c", "state: building\nneeds_sam: pick the port\n")
    assert [t.title for t in s.threads] == ["nexus_c9c"]
    assert s.threads[0].state == "building"
    assert s.threads[0].needs_sam == "pick the port"
    assert s.notes == ()
    board = build_board([_entry("nexus_c9c", "state: building\nneeds_sam: pick the port\n", "2026-10-25T19:00:00Z")], NOW)
    assert [(i.session, i.thread.title, i.thread.needs_sam) for i in board.needs_you] == [
        ("nexus_c9c", "nexus_c9c", "pick the port")
    ]


def test_preamble_fields_then_a_heading_keep_the_implicit_thread_separate() -> None:
    s = parse_session("a", "state: first\n## Named\nstate: second\n")
    assert [(t.title, t.state) for t in s.threads] == [("a", "first"), ("Named", "second")]


def test_keys_are_case_insensitive_and_values_may_contain_colons() -> None:
    s = parse_session("a", "## T\nState: waiting on http://x:80/y\nNeeds_Sam: YES\n")
    t = s.threads[0]
    assert t.state == "waiting on http://x:80/y"
    assert t.needs_sam == "YES"


@pytest.mark.parametrize("value", ["", "none", "None", " NONE ", "-", "n/a", "no"])
def test_needs_sam_negative_values(value: str) -> None:
    assert has_value(value) is False


@pytest.mark.parametrize("value", ["pick the port", "yes", "approve RDR-234"])
def test_needs_sam_positive_values(value: str) -> None:
    assert has_value(value) is True


def test_missing_needs_sam_field_is_not_needs_sam() -> None:
    s = parse_session("a", "## T\nstate: x\n")
    assert s.threads[0].needs_sam == ""
    assert has_value(s.threads[0].needs_sam) is False


@pytest.mark.parametrize(
    ("delta", "label"),
    [
        (timedelta(seconds=5), "just now"),
        (timedelta(minutes=7), "7 min ago"),
        (timedelta(hours=1, minutes=10), "1 hour ago"),
        (timedelta(hours=3, minutes=10), "3 hours ago"),
        (timedelta(hours=30), "1 day ago"),
        (timedelta(days=4, hours=5), "4 days ago"),
        (timedelta(seconds=-30), "just now"),
    ],
)
def test_format_age(delta: timedelta, label: str) -> None:
    assert format_age(delta) == label


def _entry(title: str, content: str, timestamp: str) -> dict[str, str]:
    return {"title": title, "content": content, "timestamp": timestamp}


def test_board_orders_newest_first_and_marks_stale() -> None:
    fresh = _entry("fresh", "## T\nstate: a\n", "2026-10-25T19:00:00Z")
    old = _entry("old", "## T\nstate: b\n", "2026-10-23T10:00:00Z")
    mid = _entry("mid", "## T\nstate: c\n", "2026-10-25T01:00:00Z")
    board = build_board([old, fresh, mid], NOW)
    assert [c.session.name for c in board.cards] == ["fresh", "mid", "old"]
    assert [c.stale for c in board.cards] == [False, False, True]
    assert board.cards[2].age == "2 days ago"
    assert board.cards[0].age == "1 hour ago"


def test_freshness_is_the_rows_own_time_and_ignores_an_updated_line() -> None:
    # A hand-typed future `updated:` must not turn a three-day-old row into "just now".
    future = "updated: 2026-12-31T00:00Z\n## T\nstate: a\n"
    card = build_board([_entry("s", future, "2026-10-22T20:00:00Z")], NOW).cards[0]
    assert card.stale is True
    assert card.age == "3 days ago"
    # And an old `updated:` does not age a row the engine just rewrote.
    old = "updated: 2026-10-01T00:00Z\n## T\nstate: a\n"
    card2 = build_board([_entry("s2", old, "2026-10-25T19:00:00Z")], NOW).cards[0]
    assert card2.stale is False and card2.age == "1 hour ago"


def test_board_with_no_usable_time_is_stale_and_sorted_last() -> None:
    nada = _entry("nada", "## T\nstate: a\n", "")
    ok = _entry("ok", "## T\nstate: a\n", "2026-10-25T19:00:00Z")
    board = build_board([nada, ok], NOW)
    assert [c.session.name for c in board.cards] == ["ok", "nada"]
    assert board.cards[1].stale is True
    assert board.cards[1].age == "unknown"


def test_needs_you_collects_set_needs_sam_across_sessions() -> None:
    a = _entry("a", "## One\nneeds_sam: none\n## Two\nneeds_sam: decide X\n", "2026-10-25T19:00:00Z")
    b = _entry("b", "## Three\nneeds_sam: sign off\n", "2026-10-25T19:10:00Z")
    board = build_board([a, b], NOW)
    assert [(i.session, i.thread.title, i.age) for i in board.needs_you] == [
        ("b", "Three", "50 min ago"),
        ("a", "Two", "1 hour ago"),
    ]


def test_active_and_quiet_split_on_staleness() -> None:
    fresh = _entry("fresh", "## T\nstate: a\n", "2026-10-25T19:00:00Z")
    old = _entry("old", "## T\nstate: b\n", "2026-10-23T10:00:00Z")
    board = build_board([old, fresh], NOW)
    assert [c.session.name for c in board.active] == ["fresh"]
    assert [c.session.name for c in board.quiet] == ["old"]


def test_stale_session_threads_still_count_toward_needs_you() -> None:
    a = _entry("a", "## Two\nneeds_sam: decide X\n", "2026-10-01T00:00:00Z")
    board = build_board([a], NOW)
    assert board.cards[0].stale is True
    assert len(board.needs_you) == 1


class TestBeadsJson:
    def test_parses_list_with_trailing_chatter(self) -> None:
        raw = (
            '[{"id": "nexus-1", "title": "T", "priority": 1, "assignee": "sam", "status": "open"},'
            ' {"id": "nexus-2", "title": "U", "priority": 2, "status": "open"}]\n\n'
            "Showing 2 issues; more results matched but were hidden by --limit.\n"
        )
        beads = parse_beads_json(raw)
        assert [b.id for b in beads] == ["nexus-1", "nexus-2"]
        assert beads[0].assignee == "sam"
        assert beads[1].assignee == ""
        assert beads[0].priority == 1
        assert beads[0].updated is None

    def test_bead_updated_at_is_parsed(self) -> None:
        raw = '[{"id": "n", "title": "T", "updated_at": "2026-10-25T17:06:57Z"}]'
        assert parse_beads_json(raw)[0].updated == datetime(2026, 10, 25, 17, 6, 57, tzinfo=timezone.utc)

    def test_empty_list(self) -> None:
        assert parse_beads_json("[]\n") == ()

    def test_garbage_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            parse_beads_json("bd: no database found")

    def test_non_list_json_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            parse_beads_json('{"id": "x"}')

    def test_skips_rows_without_an_id(self) -> None:
        assert parse_beads_json('[{"title": "no id"}, {"id": "k", "title": "ok"}]')[0].id == "k"


def test_beads_result_ok_and_unavailable() -> None:
    assert BeadsResult(beads=(), error="").ok is True
    assert BeadsResult(beads=(), error="bd not found on PATH").ok is False


# ── the asks the parser used to miss (nexus-i99vk) ───────────────────────────


def _asks(content: str) -> list[str]:
    board = build_board([_entry("s", content, "2026-10-25T19:00:00Z")], NOW)
    return [i.thread.needs_sam for i in board.needs_you]


@pytest.mark.parametrize("key", ["needs_sam", "needs-sam", "needs sam", "Needs_Sam", "NEEDS-SAM", "Needs  Sam"])
def test_needs_sam_key_spellings_all_yield_one_ask(key: str) -> None:
    assert _asks(f"## T\n{key}: approve it\n") == ["approve it"]


@pytest.mark.parametrize("key", ["waiting_on", "waiting-on", "waiting on", "Waiting_On"])
def test_waiting_on_key_spellings(key: str) -> None:
    s = parse_session("a", f"## T\n{key}: review\n")
    assert s.threads[0].waiting_on == "review"
    assert s.threads[0].notes == ()


@pytest.mark.parametrize("heading", ["# T", "## T", "### T"])
def test_one_to_three_hash_headings_start_a_thread(heading: str) -> None:
    s = parse_session("a", f"{heading}\nneeds_sam: yes please\n")
    assert [t.title for t in s.threads] == ["T"]
    assert _asks(f"{heading}\nneeds_sam: yes please\n") == ["yes please"]


def test_four_hashes_is_not_a_heading() -> None:
    s = parse_session("a", "#### T\nstate: x\n")
    assert [t.title for t in s.threads] == ["a"]  # implicit thread; the line stays a note
    assert "#### T" in s.threads[0].notes or "#### T" in s.notes


@pytest.mark.parametrize(
    "line",
    ["asks sam: pick the port", "Sam to decide: the port", "needs: a decision", "blocked, needs sam: the port"],
)
def test_backstop_a_key_naming_sam_or_needs_is_surfaced_as_an_ask(line: str) -> None:
    assert len(_asks(f"## T\nstate: x\n{line}\n")) == 1
    assert len(_asks(f"{line}\n")) == 1  # before any heading too


def test_backstop_ignores_a_negative_value_and_unrelated_keys() -> None:
    assert _asks("## T\nasks sam: none\nowner: somebody\n") == []


def test_backstop_and_the_named_field_both_count_once_per_line_and_never_drop() -> None:
    asks = _asks("## T\nasks sam: first\nneeds_sam: second\n")
    assert len(asks) == 1  # one thread, one ask row
    assert "first" in asks[0] and "second" in asks[0]


# ── one decision, one row ────────────────────────────────────────────────────


def test_a_thread_ask_naming_a_listed_bead_is_shown_once_with_the_session() -> None:
    board = build_board(
        [_entry("nexus_c9c", "## Port\nref: nexus-i99vk, RDR-1\nneeds_sam: decide the port\n"
                "## Other\nref: nexus-zzz\nneeds_sam: something else\n", "2026-10-25T19:00:00Z")],
        NOW,
    )
    beads = (Bead("nexus-i99vk", "Decide the port", 1), Bead("nexus-aaa", "Unrelated"))
    asks = merge_asks(board.needs_you, beads)
    assert [i.thread.title for i in asks.threads] == ["Other"]
    assert [b.bead.id for b in asks.beads] == ["nexus-i99vk", "nexus-aaa"]
    assert [i.session for i in asks.beads[0].raised_by] == ["nexus_c9c"]
    assert asks.beads[1].raised_by == ()
    assert asks.count == 3


def test_ref_matching_a_bead_id_is_case_insensitive_and_exact() -> None:
    board = build_board([_entry("s", "## T\nref: NEXUS-1\nneeds_sam: x\n", "2026-10-25T19:00:00Z")], NOW)
    assert merge_asks(board.needs_you, (Bead("nexus-1", "t"),)).count == 1
    assert merge_asks(board.needs_you, (Bead("nexus-10", "t"),)).count == 2


# ── the headline never claims what it cannot know ────────────────────────────


def _board(*rows: dict[str, str]):
    return build_board(list(rows), NOW)


def test_headline_with_no_sessions_and_no_beads_cannot_tell() -> None:
    board = _board()
    asks = merge_asks(board.needs_you, ())
    assert headline(board, asks, True) == "No session has reported yet, so I can't tell what needs you."


def test_headline_nothing_needs_you_names_the_sessions_and_the_newest_age() -> None:
    board = _board(
        _entry("a", "## T\nstate: x\n", "2026-10-25T19:00:00Z"),
        _entry("b", "## T\nstate: x\n", "2026-10-25T10:00:00Z"),
    )
    asks = merge_asks(board.needs_you, ())
    assert headline(board, asks, True) == "Nothing needs you. 2 sessions reported, newest 1 hour ago."
    one = _board(_entry("a", "## T\nstate: x\n", "2026-10-25T19:50:00Z"))
    assert headline(one, merge_asks((), ()), True) == "Nothing needs you. 1 session reported, newest 10 min ago."


def test_headline_when_a_source_failed_never_says_nothing_needs_you() -> None:
    board = _board(_entry("a", "## T\nstate: x\n", "2026-10-25T19:00:00Z"))
    assert headline(board, merge_asks((), ()), False) == "Can't tell what needs you right now."


def test_headline_counts_are_at_least_when_a_source_failed() -> None:
    board = _board(_entry("a", "## T\nneeds_sam: x\n", "2026-10-25T19:00:00Z"))
    asks = merge_asks(board.needs_you, ())
    assert headline(board, asks, False) == "At least 1 thing needs you."
    assert headline(board, asks, True) == "1 thing needs you."
    two = merge_asks(board.needs_you, (Bead("b", "t"),))
    assert headline(board, two, False) == "At least 2 things need you."
    assert headline(board, two, True) == "2 things need you."


@pytest.mark.parametrize(
    "stamp",
    ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00", "not a date", ""],
)
def test_out_of_range_and_junk_timestamps_are_none_not_an_exception(stamp: str) -> None:
    assert parse_timestamp(stamp) is None


def test_a_bead_with_an_out_of_range_updated_at_still_parses() -> None:
    raw = '[{"id": "n", "title": "T", "updated_at": "0001-01-01T00:00:00+05:00"}]'
    beads = parse_beads_json(raw)
    assert beads[0].id == "n" and beads[0].updated is None


def test_build_board_isolates_an_entry_that_cannot_be_read() -> None:
    class Bad(dict):
        def get(self, key, default=None):  # noqa: ANN001
            if key == "content":
                raise RuntimeError("corrupt")
            return super().get(key, default)

    board = build_board(
        [Bad(title="broken", timestamp="2026-10-25T19:00:00Z"),
         _entry("ok", "## T\nstate: a\n", "2026-10-25T19:00:00Z")],
        NOW,
    )
    assert board.unreadable == ("broken",)
    assert [c.session.name for c in board.cards] == ["ok"]
    assert headline(board, merge_asks(board.needs_you, ()), False) == "Can't tell what needs you right now."
