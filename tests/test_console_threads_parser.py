# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure tests for the console Threads parser (nexus-i99vk). No T2, no I/O."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nexus.console.threads import (
    BeadsResult,
    build_board,
    format_age,
    is_needs_sam,
    parse_beads_json,
    parse_session,
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


def test_parses_header_and_threads() -> None:
    s = parse_session("nexus_c9c", SAMPLE)
    assert s.name == "nexus_c9c"
    assert s.updated == datetime(2026, 10, 25, 18, 5, tzinfo=timezone.utc)
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


@pytest.mark.parametrize(
    "stamp",
    ["", "not a date", "2026-13-45T99:00Z", "yesterday"],
)
def test_bad_updated_is_none_not_an_error(stamp: str) -> None:
    s = parse_session("a", f"updated: {stamp}\n## T\nstate: x\n")
    assert s.updated is None
    assert len(s.threads) == 1


def test_naive_and_offset_timestamps() -> None:
    naive = parse_session("a", "updated: 2026-10-25T18:05:00\n")
    assert naive.updated == datetime(2026, 10, 25, 18, 5, tzinfo=timezone.utc)
    offset = parse_session("a", "updated: 2026-10-25T20:05:00+02:00\n")
    assert offset.updated == datetime(2026, 10, 25, 18, 5, tzinfo=timezone.utc)


def test_empty_and_headingless_content() -> None:
    assert parse_session("a", "").threads == ()
    s = parse_session("a", "state: orphan field\n")
    assert s.threads == ()
    assert "state: orphan field" in s.notes


def test_keys_are_case_insensitive_and_values_may_contain_colons() -> None:
    s = parse_session("a", "## T\nState: waiting on http://x:80/y\nNeeds_Sam: YES\n")
    t = s.threads[0]
    assert t.state == "waiting on http://x:80/y"
    assert t.needs_sam == "YES"


@pytest.mark.parametrize("value", ["", "none", "None", " NONE ", "-", "n/a", "no"])
def test_needs_sam_negative_values(value: str) -> None:
    assert is_needs_sam(value) is False


@pytest.mark.parametrize("value", ["pick the port", "yes", "approve RDR-234"])
def test_needs_sam_positive_values(value: str) -> None:
    assert is_needs_sam(value) is True


def test_missing_needs_sam_field_is_not_needs_sam() -> None:
    s = parse_session("a", "## T\nstate: x\n")
    assert s.threads[0].needs_sam == ""
    assert is_needs_sam(s.threads[0].needs_sam) is False


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
    fresh = _entry("fresh", "updated: 2026-10-25T19:00Z\n## T\nstate: a\n", "2026-10-25T19:00:00Z")
    old = _entry("old", "updated: 2026-10-23T10:00Z\n## T\nstate: b\n", "2026-10-23T10:00:00Z")
    mid = _entry("mid", "updated: 2026-10-25T01:00Z\n## T\nstate: c\n", "2026-10-25T01:00:00Z")
    board = build_board([old, fresh, mid], NOW)
    assert [c.session.name for c in board.cards] == ["fresh", "mid", "old"]
    assert [c.stale for c in board.cards] == [False, False, True]
    assert board.cards[2].age == "2 days ago"
    assert board.cards[0].age == "1 hour ago"


def test_board_uses_the_newer_of_header_and_entry_time() -> None:
    # Header says three days ago; the T2 row was rewritten an hour ago.
    e = _entry("s", "updated: 2026-10-22T00:00Z\n## T\nstate: a\n", "2026-10-25T19:00:00Z")
    card = build_board([e], NOW).cards[0]
    assert card.stale is False
    assert card.age == "1 hour ago"
    # And the other way round: a header newer than the row wins.
    e2 = _entry("s2", "updated: 2026-10-25T19:30Z\n## T\nstate: a\n", "2026-10-20T00:00:00Z")
    assert build_board([e2], NOW).cards[0].age == "30 min ago"


def test_board_with_no_usable_time_is_stale_and_sorted_last() -> None:
    nada = _entry("nada", "## T\nstate: a\n", "")
    ok = _entry("ok", "updated: 2026-10-25T19:00Z\n## T\nstate: a\n", "")
    board = build_board([nada, ok], NOW)
    assert [c.session.name for c in board.cards] == ["ok", "nada"]
    assert board.cards[1].stale is True
    assert board.cards[1].age == "unknown"


def test_needs_you_collects_set_needs_sam_across_sessions() -> None:
    a = _entry(
        "a", "updated: 2026-10-25T19:00Z\n## One\nneeds_sam: none\n## Two\nneeds_sam: decide X\n",
        "2026-10-25T19:00:00Z",
    )
    b = _entry("b", "updated: 2026-10-25T19:10Z\n## Three\nneeds_sam: sign off\n", "2026-10-25T19:10:00Z")
    board = build_board([a, b], NOW)
    assert [(i.session, i.thread.title, i.age) for i in board.needs_you] == [
        ("b", "Three", "50 min ago"),
        ("a", "Two", "1 hour ago"),
    ]


def test_active_and_quiet_split_on_staleness() -> None:
    fresh = _entry("fresh", "updated: 2026-10-25T19:00Z\n## T\nstate: a\n", "")
    old = _entry("old", "updated: 2026-10-23T10:00Z\n## T\nstate: b\n", "")
    board = build_board([old, fresh], NOW)
    assert [c.session.name for c in board.active] == ["fresh"]
    assert [c.session.name for c in board.quiet] == ["old"]


def test_stale_session_threads_still_count_toward_needs_you() -> None:
    a = _entry("a", "updated: 2026-10-01T00:00Z\n## Two\nneeds_sam: decide X\n", "2026-10-01T00:00:00Z")
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
