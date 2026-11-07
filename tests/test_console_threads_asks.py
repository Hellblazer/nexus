# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured asks on the threads board (RDR-245 Step 1, nexus-wc4jj.6).

Pure tests: entries are dicts shaped like T2 rows, the clock is fixed, answer
records are injected. Fixtures live in ``tests/fixtures/asks/``. The legacy
parser tests stay in ``test_console_threads_parser.py`` and are not edited.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from nexus.console.threads import (
    STALE_AFTER,
    Ask,
    Bead,
    answer_title,
    ask_version,
    build_board,
    classify,
    lint,
    merge_asks,
    parse_session,
)

FIXTURES = Path(__file__).parent / "fixtures" / "asks"
NOW = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)
SESSION = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
FRESH = (NOW - timedelta(hours=1)).isoformat()
OLD = (NOW - timedelta(hours=30)).isoformat()

HALCYON_VERSION = "f22ca640c6bfa3d2"
HALCYON_SESSION = "3f2a9c1e-5b7d-4e2a-9c31-0d8e6f4a2b17"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def row(title: str, content: str, timestamp: str = FRESH) -> dict[str, str]:
    return {"title": title, "content": content, "timestamp": timestamp}


_FIELD_ORDER = ("asked", "question", "context", "option", "rec", "why", "reversible", "blocking", "after", "by")
_DEFAULTS: dict[str, object] = {
    "asked": "2026-11-05T10:00:00+00:00",
    "question": "Ship it?",
    "context": "The goal is shipping; waiting holds the release.",
    "option": ["Ship", "Wait"],
    "rec": "1",
    "why": "It is verified.",
    "reversible": "yes",
    "blocking": "no",
}


def ask_text(ask_id: str = "a1", /, **over: object) -> str:
    """One ask block. A keyword set to None drops that field; ``option`` takes a list."""
    fields = {**_DEFAULTS, **over}
    lines = [f"ask: {ask_id}"]
    for key in _FIELD_ORDER:
        value = fields.get(key)
        if value is None:
            continue
        for v in value if isinstance(value, list) else [value]:
            lines.append(f"{key}: {v}")
    return "\n".join(lines)


def entry_text(*blocks: str, session: str | None = SESSION, heading: str = "## Thread", thread_lines: str = "") -> str:
    head = [f"session: {session}", ""] if session is not None else []
    return "\n".join([*head, heading, thread_lines, *blocks, ""])


def only_ask(content: str):
    asks = parse_session("e", content).threads[0].asks
    assert len(asks) == 1
    return asks[0]


# --- parser -------------------------------------------------------------------------


def test_halcyon_every_field_is_read() -> None:
    s = parse_session("halcyon", fixture("halcyon.txt"))
    assert s.session_id == HALCYON_SESSION
    assert len(s.threads) == 1
    t = s.threads[0]
    assert t.title == "Quarantine restore keying (RDR-222)"
    assert t.refs == ("nexus-k9kr9", "RDR-222")
    assert t.state == "design done; waiting on the build-or-accept choice"
    assert t.waiting_on == "sam"
    (a,) = t.asks
    assert a.id == "d1105-k9kr9-keying"
    assert a.asked == "2026-11-05T10:15Z"
    assert a.question.startswith("Retry-safe keying for quarantine restore")
    assert a.context.startswith("The goal is retry-safe quarantine restore.")
    assert [(o.n, o.text, o.irreversible) for o in a.options] == [
        (1, "Accept the gap and close the bead", False),
        (2, "Build option B at P3 (one transaction per keyed call, engine change plus one client line)", True),
    ]
    assert a.rec == 1
    assert a.why.startswith("Nothing ends up wrong today")
    assert a.reversible is False
    assert a.blocking is False
    assert a.after == ""
    assert a.by == ""
    assert a.version == HALCYON_VERSION
    assert a.entry == "halcyon"
    assert a.session_id == HALCYON_SESSION
    assert a.thread == t.title
    assert a.refs == ("nexus-k9kr9", "RDR-222")
    assert a.missing == ()
    assert s.notes == () and t.notes == ()


def test_after_and_by_are_read() -> None:
    a = only_ask(entry_text(ask_text(after="engine 0.1.180 deploys", by="2026-11-07")))
    assert a.after == "engine 0.1.180 deploys"
    assert a.by == "2026-11-07"


def test_repeated_option_lines_keep_their_order_and_number_from_one() -> None:
    a = only_ask(entry_text(ask_text(option=["third? no, first", "second", "third [irreversible]", "fourth"])))
    assert [(o.n, o.text, o.irreversible) for o in a.options] == [
        (1, "third? no, first", False),
        (2, "second", False),
        (3, "third", True),
        (4, "fourth", False),
    ]


def test_a_block_ends_at_the_next_ask() -> None:
    s = parse_session("e", entry_text(ask_text("a1"), ask_text("a2", question="Other?", option=["X", "Y", "Z"])))
    one, two = s.threads[0].asks
    assert (one.id, two.id) == ("a1", "a2")
    assert one.question == "Ship it?" and len(one.options) == 2
    assert two.question == "Other?" and len(two.options) == 3
    assert s.threads[0].notes == ()


def test_a_block_ends_at_a_heading() -> None:
    content = entry_text(ask_text("a1")) + "\n## Second thread\nwhy: stray\n"
    s = parse_session("e", content)
    first, second = s.threads
    assert [a.id for a in first.asks] == ["a1"]
    assert second.asks == ()
    assert second.notes == ("why: stray",)  # an ask key outside a block is a note


@pytest.mark.parametrize("terminator", [
    "ref: nexus-1", "refs: nexus-1, nexus-2", "state: working", "waiting_on: review", "needs_sam: none",
])
def test_a_block_ends_at_each_thread_field(terminator: str) -> None:
    content = entry_text(ask_text("a1"), terminator, "why: after the block", "option: after the block")
    (t,) = parse_session("e", content).threads
    (a,) = t.asks
    assert a.why == "It is verified."  # the stray lines did not reach the ask
    assert len(a.options) == 2
    assert t.notes == ("why: after the block", "option: after the block")


def test_ask_keys_outside_a_block_are_notes() -> None:
    content = "## T\nquestion: floating\nrec: 2\nask: a1\n" + "\n".join(
        ln for ln in ask_text("a1").splitlines()[1:]
    )
    (t,) = parse_session("e", content).threads
    assert t.notes == ("question: floating", "rec: 2")
    assert t.asks[0].question == "Ship it?"


def test_a_context_naming_sam_is_ask_text_not_a_legacy_ask() -> None:
    a_text = ask_text(context="Sam has not picked a port yet; if you wait nothing else is held.")
    (t,) = parse_session("e", entry_text(a_text)).threads
    assert t.asks[0].context.startswith("Sam has not picked")
    assert t.needs_sam == ""
    assert t.notes == ()
    assert build_board([row("e", entry_text(a_text))], NOW).needs_you == ()


def test_the_sam_backstop_is_off_for_any_line_inside_a_block() -> None:
    content = entry_text(ask_text("a1") + "\nneeds the port from Sam\nsam-note: please look")
    (t,) = parse_session("e", content).threads
    assert t.needs_sam == ""
    assert t.notes == ("needs the port from Sam", "sam-note: please look")


def test_pointer_and_waiting_on_sam_are_not_legacy_asks_in_a_structured_thread() -> None:
    s = parse_session("halcyon", fixture("halcyon.txt"))
    t = s.threads[0]
    assert t.waiting_on == "sam"  # still the state
    assert t.needs_sam == ""
    assert build_board([row("halcyon", fixture("halcyon.txt"))], NOW).needs_you == ()


def test_the_pointer_after_the_block_is_dropped_too() -> None:
    content = entry_text(ask_text("a1"), "waiting_on: Sam", "needs_sam: ask a1")
    (t,) = parse_session("e", content).threads
    assert t.needs_sam == ""


def test_any_other_needs_sam_value_is_still_a_legacy_ask() -> None:
    content = entry_text(ask_text("a1"), thread_lines="waiting_on: sam\nneeds_sam: ask a1\nneeds_sam: pick the port")
    (t,) = parse_session("e", content).threads
    assert t.needs_sam == "pick the port"
    board = build_board([row("e", content)], NOW)
    assert [i.thread.needs_sam for i in board.needs_you] == ["pick the port"]


def test_a_pointer_naming_no_ask_of_the_thread_is_a_legacy_ask() -> None:
    content = entry_text(ask_text("a1"), thread_lines="needs_sam: ask other-id")
    (t,) = parse_session("e", content).threads
    assert t.needs_sam == "ask other-id"


def test_waiting_on_sam_with_more_words_is_still_a_legacy_ask() -> None:
    content = entry_text(ask_text("a1"), thread_lines="waiting_on: Sam to pick the port")
    (t,) = parse_session("e", content).threads
    assert t.needs_sam == "waiting on: Sam to pick the port"


def test_a_thread_without_an_ask_block_keeps_its_pointer_shaped_text() -> None:
    (t,) = parse_session("e", "## T\nwaiting_on: sam\nneeds_sam: ask a1\n").threads
    assert t.needs_sam == "waiting on: sam; ask a1"
    assert t.asks == ()


def test_the_pointer_value_on_the_next_line_is_a_pointer_too() -> None:
    content = entry_text(ask_text("a1"), thread_lines="needs_sam:\nask a1")
    (t,) = parse_session("e", content).threads
    assert t.needs_sam == ""


def test_the_none_valued_needs_sam_survives_when_the_pointer_is_dropped() -> None:
    content = entry_text(ask_text("a1"), thread_lines="needs_sam: none\nneeds_sam: ask a1")
    (t,) = parse_session("e", content).threads
    assert t.needs_sam == "none"


def test_ask_lines_before_any_heading_belong_to_the_session_named_thread() -> None:
    content = f"session: {SESSION}\n" + ask_text("a1")
    s = parse_session("solo", content)
    assert [t.title for t in s.threads] == ["solo"]
    assert s.threads[0].asks[0].id == "a1"


def test_the_session_line_is_read_and_adds_no_thread_or_note() -> None:
    s = parse_session("e", f"session: {SESSION}\n")
    assert s.session_id == SESSION
    assert s.threads == () and s.notes == ()


def test_the_first_session_line_wins() -> None:
    s = parse_session("e", "session: first\n## T\nsession: second\n")
    assert s.session_id == "first"
    assert s.threads[0].notes == ()


def test_freshness_still_comes_from_the_row_not_the_session_line() -> None:
    board = build_board([row("e", entry_text(ask_text()), timestamp=OLD)], NOW)
    assert board.cards[0].stale is True
    assert board.cards[0].effective_updated == datetime.fromisoformat(OLD)


def test_legacy_entry_parses_as_before() -> None:
    s = parse_session("linda", fixture("linda_legacy.txt"))
    assert s.session_id == "82180959-b94a-4da8-9878-2fdf7d8da0ca"
    assert s.notes == ()
    assert [t.title for t in s.threads] == [
        "Retire the aspect queue table (RDR-235 step B)",
        "Sam's decision queue page",
        "Push wrapper flags",
        "RDR-236 sweep slice C",
    ]
    assert all(t.asks == () for t in s.threads)
    first, page, wrapper, sweep = s.threads
    assert first.needs_sam == "none"
    assert page.refs == ("https://claude.ai/artifact/T8f4Dxeo9MHNz3b6yQy9Dx (db collection answers)",)
    assert page.needs_sam == "waiting on: Sam; answer the page (one button takes every recommendation)"
    assert wrapper.needs_sam == "" and wrapper.waiting_on == "me"
    assert sweep.refs == ("nexus-6pfgb.43 (key-retirement fix shipped into 7.96.0 / engine 0.1.180", "tamsin driving)")
    assert sweep.needs_sam == "after engine 0.1.180 deploys, see cloud dry-run counts and say go"
    board = build_board([row("linda", fixture("linda_legacy.txt"))], NOW)
    assert [i.thread.title for i in board.needs_you] == ["Sam's decision queue page", "RDR-236 sweep slice C"]
    assert board.structured == ()


# --- version ------------------------------------------------------------------------


def test_version_is_the_first_16_hex_of_sha256_over_the_stripped_non_by_ask_lines() -> None:
    lines = ["ask: a1", "  asked: t  ", "question: q", "by: 2026-12-01", "option: x"]
    expected = hashlib.sha256("\n".join(["ask: a1", "asked: t", "question: q", "option: x"]).encode()).hexdigest()[:16]
    assert ask_version(lines) == expected


def test_halcyon_version_is_pinned() -> None:
    assert only_ask(fixture("halcyon.txt")).version == HALCYON_VERSION


def test_version_is_stable_across_whitespace_only_differences() -> None:
    plain = entry_text(ask_text())
    spaced = "\n".join(
        ("    " + ln + "   ") if ln.startswith(("ask", "asked", "question", "option")) else ln
        for ln in plain.splitlines()
    ).replace("\n## Thread\n", "\n\n\n## Thread\n\n")
    assert only_ask(spaced).version == only_ask(plain).version


@pytest.mark.parametrize("field,value", [
    ("asked", "2026-11-05T11:00:00+00:00"),  # a bumped asked: is a new version
    ("question", "Ship it now?"),
    ("context", "Changed context."),
    ("option", ["Ship", "Wait", "Cancel"]),
    ("option", ["Wait", "Ship"]),  # order is part of the ask
    ("rec", "2"),
    ("why", "Different reason."),
    ("reversible", "no"),
    ("blocking", "yes"),
    ("after", "something happens"),
])
def test_version_changes_on_any_field_edit(field: str, value: object) -> None:
    base = only_ask(entry_text(ask_text())).version
    assert only_ask(entry_text(ask_text(**{field: value}))).version != base


def test_version_changes_when_the_id_changes() -> None:
    assert only_ask(entry_text(ask_text("a1"))).version != only_ask(entry_text(ask_text("a2"))).version


def test_version_is_unchanged_when_only_by_changes() -> None:
    base = only_ask(entry_text(ask_text())).version
    assert only_ask(entry_text(ask_text(by="2026-12-01"))).version == base
    assert only_ask(entry_text(ask_text(by="2027-01-01T00:00:00+00:00"))).version == base


def test_version_ignores_thread_fields_and_other_asks() -> None:
    plain = parse_session("e", entry_text(ask_text("a1"), ask_text("a2"))).threads[0].asks[0].version
    noisy = parse_session(
        "e", entry_text(ask_text("a1"), ask_text("a2", question="Changed?"), thread_lines="state: moving\nref: x")
    ).threads[0].asks[0].version
    assert plain == noisy


# --- lint ---------------------------------------------------------------------------


def findings(*entries: dict[str, str]):
    return lint(list(entries))


def pairs(found) -> set[tuple[str, str]]:
    return {(f.rule, f.ask) for f in found}


def test_a_clean_entry_has_no_findings() -> None:
    assert findings(row("halcyon", fixture("halcyon.txt"))) == ()


@pytest.mark.parametrize("bad", ["UPPER", "has_underscore", "x" * 41, "has space"])
def test_lint_flags_an_id_outside_the_charset(bad: str) -> None:
    found = findings(row("e", entry_text(ask_text(bad))))
    assert ("bad-id", bad) in pairs(found)
    assert all(f.entry == "e" for f in found)


def test_lint_accepts_a_40_character_id() -> None:
    assert findings(row("e", entry_text(ask_text("x" * 40)))) == ()


def test_lint_flags_an_id_repeated_in_the_entry() -> None:
    found = findings(row("e", entry_text(ask_text("a1"), ask_text("a1", question="Other?"))))
    assert pairs(found) == {("duplicate-id", "a1")}


def test_the_same_id_in_two_entries_is_not_a_repeat() -> None:
    other = "11111111-2222-4333-8444-555555555555"
    found = findings(row("e1", entry_text(ask_text("a1"))), row("e2", entry_text(ask_text("a1"), session=other)))
    assert found == ()


def test_lint_flags_reversible_yes_with_an_irreversible_option() -> None:
    found = findings(row("e", entry_text(ask_text(option=["Do", "Delete it [irreversible]"], reversible="yes"))))
    assert pairs(found) == {("reversible-conflict", "a1")}
    ok = findings(row("e", entry_text(ask_text(option=["Do", "Delete it [irreversible]"], reversible="no"))))
    assert ok == ()


@pytest.mark.parametrize("missing", ["asked", "question", "context", "reversible", "blocking"])
def test_lint_flags_each_missing_required_field(missing: str) -> None:
    found = findings(row("e", entry_text(ask_text(**{missing: None}))))
    assert pairs(found) == {("missing-field", "a1")}
    assert missing in found[0].message


def test_an_empty_required_value_counts_as_missing() -> None:
    found = findings(row("e", entry_text(ask_text(question=""))))
    assert pairs(found) == {("missing-field", "a1")}


def test_lint_flags_why_missing_with_rec() -> None:
    found = findings(row("e", entry_text(ask_text(why=None))))
    assert pairs(found) == {("missing-field", "a1")}
    assert "why" in found[0].message
    assert findings(row("e", entry_text(ask_text(why=None, rec=None)))) == ()


@pytest.mark.parametrize("options", [[], ["Only one"]])
def test_lint_flags_fewer_than_two_options(options: list[str]) -> None:
    found = findings(row("e", entry_text(ask_text(option=options, rec=None, why=None))))
    assert pairs(found) == {("few-options", "a1")}


@pytest.mark.parametrize("rec", ["0", "3", "999"])
def test_lint_flags_rec_out_of_range(rec: str) -> None:
    found = findings(row("e", entry_text(ask_text(rec=rec))))
    assert pairs(found) == {("rec-range", "a1")}


@pytest.mark.parametrize("rec", ["two", "-1", "1.5", "1000", "+1", "\u0661", "9" * 5000])
def test_a_rec_that_is_not_one_to_three_ascii_digits_is_a_bad_value_never_an_exception(rec: str) -> None:
    content = entry_text(ask_text(rec=rec))
    (a,) = parse_session("e", content).threads[0].asks
    assert a.rec is None and a.rec_raw == rec
    assert pairs(findings(row("e", content))) == {("bad-value", "a1")}


def test_lint_flags_a_yes_no_field_that_is_neither() -> None:
    found = findings(row("e", entry_text(ask_text(blocking="maybe"))))
    assert pairs(found) == {("bad-value", "a1")}


def test_lint_flags_a_field_given_twice() -> None:
    content = entry_text(ask_text() + "\nquestion: second thoughts")
    assert pairs(findings(row("e", content))) == {("repeated-field", "a1")}


def test_lint_flags_no_session_line_on_an_entry_with_asks() -> None:
    found = findings(row("e", entry_text(ask_text(), session=None)))
    assert pairs(found) == {("no-session", "")}
    assert found[0].entry == "e"


def test_a_legacy_entry_without_a_session_line_is_not_flagged() -> None:
    assert findings(row("old", "## T\nstate: x\nneeds_sam: pick\n")) == ()


def test_lint_flags_two_entries_carrying_the_same_session_id() -> None:
    found = findings(row("e1", entry_text(ask_text("a1"))), row("e2", entry_text(ask_text("a2"))), row("e3", entry_text(session="other-id")))
    assert {(f.rule, f.entry) for f in found} == {("duplicate-session", "e1"), ("duplicate-session", "e2")}


def test_session_ids_are_compared_case_insensitively() -> None:
    found = findings(
        row("e1", entry_text(ask_text("a1"))),
        row("e2", entry_text(ask_text("a2"), session=SESSION.upper())),
    )
    assert {f.rule for f in found} == {"duplicate-session"}


def test_every_finding_names_the_entry_and_the_ask() -> None:
    found = findings(row("e", entry_text(ask_text("a1", option=["only"], rec="7", question=None, blocking="maybe"))))
    assert {f.rule for f in found} == {"few-options", "rec-range", "missing-field", "bad-value"}
    assert all(f.entry == "e" and f.ask == "a1" for f in found)


def test_a_bad_id_still_names_the_entry_and_the_ask_it_failed_to_open() -> None:
    (f,) = findings(row("e", entry_text(ask_text("Bad_Id"))))
    assert (f.entry, f.ask, f.rule) == ("e", "Bad_Id", "bad-id")


def test_prose_after_ask_colon_in_a_legacy_thread_is_not_a_bad_id_finding() -> None:
    assert findings(row("old", "## T\nask: Sam please pick the port\n")) == ()


def test_lint_flags_an_option_whose_text_is_empty() -> None:
    found = findings(row("e", entry_text(ask_text(option=["Ship", "Wait", "", "[irreversible]"]))))
    assert [(f.rule, f.ask) for f in found if f.rule == "empty-option"] == [("empty-option", "a1")] * 2
    assert all(f.rule in {"empty-option", "reversible-conflict"} for f in found)


# --- classification -----------------------------------------------------------------


def classes_entry(timestamp: str = FRESH) -> dict[str, str]:
    return row("release", fixture("classes.txt"), timestamp)


def ids(group) -> list[str]:
    return [c.ask.id for c in group]


def record_title(ask_id: str, version: str, session: str = SESSION) -> str:
    return f"{session}/{ask_id}/{version}"


def version_of(ask_id: str) -> str:
    s = parse_session("release", fixture("classes.txt"))
    return next(a.version for a in s.threads[0].asks if a.id == ask_id)


def sent_record(ask_id: str, status: str = "sent", version: str | None = None) -> dict[str, str]:
    return {"title": record_title(ask_id, version or version_of(ask_id)), "content": json.dumps({"status": status})}


def test_fresh_entry_classes() -> None:
    c = classify([classes_entry()], NOW, [sent_record("already-answered")])
    assert ids(c.conditional) == ["engine-deploy-go"]
    assert ids(c.answered) == ["already-answered"]
    assert ids(c.interrupt) == ["stop-the-line"]
    assert ids(c.batch) == ["tidy-label"]
    assert c.quiet == ()


def test_a_conditional_ask_is_never_answerable_even_with_blocking_yes_or_a_sent_record() -> None:
    c = classify([classes_entry()], NOW, [sent_record("engine-deploy-go")])
    assert "engine-deploy-go" in ids(c.conditional)
    assert "engine-deploy-go" not in ids(c.interrupt) + ids(c.answered) + ids(c.batch) + ids(c.quiet)


def test_a_blocking_ask_in_a_greyed_entry_stays_interrupt_with_the_age_mark() -> None:
    c = classify([classes_entry(OLD)], NOW, ())
    marks = {i.ask.id: i.mark for i in c.interrupt}
    assert marks == {"already-answered": "entry 30 h old", "stop-the-line": "entry 30 h old"}


def test_a_non_blocking_ask_in_a_greyed_entry_is_quiet() -> None:
    c = classify([classes_entry(OLD)], NOW, ())
    assert ids(c.quiet) == ["tidy-label"]
    assert c.batch == ()
    assert c.quiet[0].mark == ""


def test_a_fresh_blocking_ask_carries_no_age_mark() -> None:
    c = classify([classes_entry()], NOW, ())
    assert {i.mark for i in c.interrupt} == {""}


def test_an_entry_exactly_stale_after_old_is_not_greyed() -> None:
    edge = (NOW - STALE_AFTER).isoformat()
    c = classify([classes_entry(edge)], NOW, ())
    assert ids(c.batch) == ["tidy-label"] and c.quiet == ()
    over = (NOW - STALE_AFTER - timedelta(seconds=1)).isoformat()
    assert ids(classify([classes_entry(over)], NOW, ()).quiet) == ["tidy-label"]


def test_an_entry_with_no_usable_time_is_greyed() -> None:
    c = classify([classes_entry("")], NOW, ())
    assert ids(c.quiet) == ["tidy-label"]
    assert {i.mark for i in c.interrupt} == {"entry age unknown"}


@pytest.mark.parametrize("status", ["sending", "failed"])
def test_a_sending_or_failed_record_does_not_mark_the_ask_answered(status: str) -> None:
    c = classify([classes_entry()], NOW, [sent_record("already-answered", status)])
    assert "already-answered" in ids(c.interrupt)
    assert c.answered == ()


def test_a_record_for_an_older_version_does_not_mark_the_ask_answered() -> None:
    c = classify([classes_entry()], NOW, [sent_record("already-answered", version="0123456789abcdef")])
    assert "already-answered" in ids(c.interrupt)
    assert c.answered == ()


def test_a_record_for_another_session_does_not_mark_the_ask_answered() -> None:
    rec = {"title": record_title("already-answered", version_of("already-answered"), "someone-else"),
           "content": json.dumps({"status": "sent"})}
    assert classify([classes_entry()], NOW, [rec]).answered == ()


def test_records_may_be_a_mapping_of_title_to_record_or_status_text() -> None:
    title = record_title("already-answered", version_of("already-answered"))
    for records in ({title: {"status": "sent"}}, {title: json.dumps({"status": "sent"})}):
        assert ids(classify([classes_entry()], NOW, records).answered) == ["already-answered"]


def test_an_unreadable_record_counts_for_nothing() -> None:
    rec = {"title": record_title("already-answered", version_of("already-answered")), "content": "not json"}
    assert classify([classes_entry()], NOW, [rec]).answered == ()


def test_by_sorts_earliest_first_within_a_group_and_missing_last() -> None:
    blocks = [
        ask_text("late", blocking="yes", by="2026-12-31"),
        ask_text("none", blocking="yes"),
        ask_text("early", blocking="yes", by="2026-11-06T08:00:00+00:00"),
        ask_text("mid", blocking="yes", by="2026-11-20"),
    ]
    c = classify([row("e", entry_text(*blocks))], NOW, ())
    assert ids(c.interrupt) == ["early", "mid", "late", "none"]


def test_by_never_changes_the_class() -> None:
    plain = classify([row("e", entry_text(ask_text("a1", blocking="yes"), ask_text("a2")))], NOW, ())
    dated = classify(
        [row("e", entry_text(ask_text("a1", blocking="yes", by="2020-01-01"), ask_text("a2", by="2099-01-01")))], NOW, ()
    )
    assert (ids(plain.interrupt), ids(plain.batch)) == (ids(dated.interrupt), ids(dated.batch)) == (["a1"], ["a2"])


def test_an_unparseable_by_sorts_last() -> None:
    blocks = [ask_text("junk", blocking="yes", by="whenever"), ask_text("dated", blocking="yes", by="2026-11-06")]
    assert ids(classify([row("e", entry_text(*blocks))], NOW, ()).interrupt) == ["dated", "junk"]


class _UnreadableRow:
    """A row whose content cannot be read; the title is fine."""

    def get(self, key: str, default: object = "") -> object:
        if key == "content":
            raise RuntimeError("unreadable")
        return {"title": "bad", "timestamp": FRESH}.get(key, default)


def _Unreadable() -> Mapping[str, Any]:  # noqa: N802 -- reads as a constructor at the call sites
    return cast(Mapping[str, Any], _UnreadableRow())


def test_classify_reports_the_entry_it_could_not_read_and_still_classifies_the_rest() -> None:
    c = classify([_Unreadable(), row("e", entry_text(ask_text("a1")))], NOW, ())
    assert c.unreadable == ("bad",)
    assert ids(c.batch) == ["a1"]


def test_classify_names_nothing_unreadable_when_every_entry_reads() -> None:
    assert classify([row("e", entry_text(ask_text("a1")))], NOW, ()).unreadable == ()


def test_lint_flags_an_entry_it_could_not_read() -> None:
    found = findings(_Unreadable(), row("e", entry_text(ask_text("a1", option=["only"], rec=None, why=None))))
    assert ("unreadable-entry", "bad") in {(f.rule, f.entry) for f in found}
    assert ("few-options", "e") in {(f.rule, f.entry) for f in found}  # the rest was still linted


def test_every_parsed_ask_lands_in_exactly_one_group() -> None:
    c = classify([classes_entry(), classes_entry(OLD) | {"title": "other"}], NOW, [sent_record("already-answered")])
    total = sum(len(g) for g in (c.interrupt, c.batch, c.quiet, c.conditional, c.answered))
    assert total == 8


# --- bead fold (nexus-wc4jj.4) ------------------------------------------------------


def _fold(content: str, bead_id: str = "nexus-k9kr9"):
    entries = [row("halcyon", content)]
    board = build_board(entries, NOW)
    asks = merge_asks(board.needs_you, (Bead(bead_id, "Keying"),), board.structured)
    return board, asks, classify(entries, NOW, ())


def test_a_folded_ask_stays_in_its_own_group_and_the_bead_points_to_it() -> None:
    _, asks, classed = _fold(fixture("halcyon.txt"))
    (bead,) = asks.beads
    assert bead.ask_pointers == ("halcyon/d1105-k9kr9-keying",)
    assert len(bead.raised_by) == 1
    assert bead.raised_by[0].session == "halcyon"
    assert asks.threads == ()
    assert asks.count == 1
    # the ask is still listed in its own group
    assert ids(classed.batch) == ["d1105-k9kr9-keying"]


def test_a_blocking_folded_ask_stays_interrupt() -> None:
    content = fixture("halcyon.txt").replace("blocking: no", "blocking: yes")
    _, asks, classed = _fold(content)
    assert asks.beads[0].ask_pointers == ("halcyon/d1105-k9kr9-keying",)
    assert ids(classed.interrupt) == ["d1105-k9kr9-keying"]


def test_refs_and_ref_both_fold_and_the_match_ignores_case() -> None:
    for line in ("refs: other, NEXUS-K9KR9", "ref: nexus-k9kr9"):
        content = fixture("halcyon.txt").replace("ref: nexus-k9kr9, RDR-222", line)
        _, asks, _ = _fold(content)
        assert asks.beads[0].ask_pointers == ("halcyon/d1105-k9kr9-keying",), line


def test_a_structured_ask_with_no_matching_bead_does_not_fold() -> None:
    board, asks, _ = _fold(fixture("halcyon.txt"), bead_id="nexus-zzzzz")
    assert asks.beads[0].ask_pointers == () and asks.beads[0].raised_by == ()
    assert asks.threads == ()  # structured asks are listed through classify, not here
    assert len(board.structured) == 1


def test_two_asks_in_one_thread_add_one_raised_by_and_two_pointers() -> None:
    content = entry_text(ask_text("a1"), ask_text("a2"), thread_lines="ref: nexus-k9kr9")
    _, asks, _ = _fold(content)
    (bead,) = asks.beads
    assert bead.ask_pointers == ("halcyon/a1", "halcyon/a2")
    assert len(bead.raised_by) == 1


def test_a_legacy_ask_on_a_structured_thread_is_not_raised_twice() -> None:
    content = entry_text(ask_text("a1"), thread_lines="ref: nexus-k9kr9\nneeds_sam: pick the port")
    board, asks, _ = _fold(content)
    assert len(board.needs_you) == 1
    (bead,) = asks.beads
    assert len(bead.raised_by) == 1
    assert bead.ask_pointers == ("halcyon/a1",)


def test_merge_asks_without_structured_input_is_as_before() -> None:
    board = build_board([row("halcyon", fixture("halcyon.txt"))], NOW)
    asks = merge_asks(board.needs_you, (Bead("nexus-k9kr9", "Keying"),))
    assert asks.beads[0].ask_pointers == () and asks.beads[0].raised_by == ()


# --- legacy ask: prose (a line that is not an id never opens a block) ---------------------


@pytest.mark.parametrize("prose", ["Sam please pick the port", "pick the port", "Foo_Bar", "UPPER", "x" * 41, ""])
def test_ask_colon_prose_is_not_a_block_and_parses_as_before(prose: str) -> None:
    content = f"## T\nask: {prose}\nstate: x\n"
    (t,) = parse_session("e", content).threads
    assert t.asks == ()
    if "Sam" in prose:
        assert t.needs_sam == f"ask: {prose}" and t.notes == ()
    else:
        assert t.needs_sam == "" and t.notes == (f"ask: {prose}".strip(),)


def test_ask_colon_prose_naming_sam_is_still_a_legacy_ask_on_the_board() -> None:
    board = build_board([row("e", "## T\nask: Sam please pick the port\n")], NOW)
    assert [i.thread.needs_sam for i in board.needs_you] == ["ask: Sam please pick the port"]
    assert board.structured == ()


def test_needs_sam_with_its_value_on_the_next_line_still_takes_an_ask_colon_line_as_that_value() -> None:
    (t,) = parse_session("e", "## T\nneeds_sam:\nask: foo\nstate: s\n").threads
    assert t.needs_sam == "ask: foo"
    assert t.asks == ()


def test_an_ask_line_that_is_not_an_id_inside_a_block_is_a_note_and_not_part_of_the_version() -> None:
    plain = only_ask(entry_text(ask_text("a1"))).version
    content = entry_text(ask_text("a1") + "\nask: pick the port")
    (t,) = parse_session("e", content).threads
    assert t.asks[0].version == plain
    assert t.notes == ("ask: pick the port",)


# --- conditional property -----------------------------------------------------------


@pytest.mark.parametrize(("after", "expected"), [
    ("engine 0.1.180 deploys", True), ("none", False), ("None", False), ("", False), ("-", False), (None, False),
])
def test_conditional_means_after_has_a_value(after: str | None, expected: bool) -> None:
    a = only_ask(entry_text(ask_text(after=after)))
    assert a.conditional is expected


def test_an_ask_with_after_none_is_classified_by_its_other_fields() -> None:
    c = classify([row("e", entry_text(ask_text("a1", after="none", blocking="yes")))], NOW, ())
    assert ids(c.interrupt) == ["a1"] and c.conditional == ()


# --- repeated single-valued keys fail safe ------------------------------------------


def test_a_repeated_reversible_resolves_to_no_whatever_comes_first() -> None:
    for first, second in (("yes", "no"), ("no", "yes"), ("yes", "yes")):
        content = entry_text(ask_text(reversible=first) + f"\nreversible: {second}")
        a = only_ask(content)
        assert a.reversible is False, (first, second)
        assert "repeated-field" in {f.rule for f in lint([row("e", content)])}


def test_a_repeated_blocking_resolves_to_yes_whatever_comes_first() -> None:
    for first, second in (("yes", "no"), ("no", "yes"), ("no", "no")):
        content = entry_text(ask_text(blocking=first) + f"\nblocking: {second}")
        a = only_ask(content)
        assert a.blocking is True, (first, second)
        assert ("repeated-field", "a1") in {(f.rule, f.ask) for f in lint([row("e", content)])}


def test_a_repeated_field_that_is_not_safety_relevant_keeps_the_first_value() -> None:
    a = only_ask(entry_text(ask_text() + "\nquestion: second thoughts"))
    assert a.question == "Ship it?"


# --- the [irreversible] marker -------------------------------------------------------


def test_toggling_the_irreversible_marker_changes_the_version() -> None:
    plain = only_ask(entry_text(ask_text(option=["Ship", "Wait"], reversible="no"))).version
    marked = only_ask(entry_text(ask_text(option=["Ship", "Wait [irreversible]"], reversible="no"))).version
    assert plain != marked


def test_a_marker_in_the_middle_of_an_option_is_not_a_marker() -> None:
    a = only_ask(entry_text(ask_text(option=["Ship [irreversible] later", "Wait"])))
    assert a.options[0].irreversible is False
    assert a.options[0].text == "Ship [irreversible] later"


@pytest.mark.parametrize("text", ["Ship [IRREVERSIBLE]", "Ship [Irreversible]  ", "Ship[irreversible]"])
def test_the_marker_is_case_insensitive_and_needs_no_space_before_it(text: str) -> None:
    # Pinned: missing an irreversible option is the costlier mistake, so more spellings count.
    (o, _) = only_ask(entry_text(ask_text(option=[text, "Wait"]))).options
    assert o.irreversible is True and o.text == "Ship"


# --- answer_title ---------------------------------------------------------------------


def test_answer_title_is_session_ask_version() -> None:
    a = only_ask(fixture("halcyon.txt"))
    assert answer_title(a) == f"{HALCYON_SESSION}/d1105-k9kr9-keying/{HALCYON_VERSION}"


def test_answer_title_lower_cases_an_upper_case_session_line_to_the_record_title() -> None:
    upper = fixture("halcyon.txt").replace(HALCYON_SESSION, HALCYON_SESSION.upper())
    a = only_ask(upper)
    assert a.session_id == HALCYON_SESSION.upper()
    assert answer_title(a) == f"{HALCYON_SESSION}/d1105-k9kr9-keying/{HALCYON_VERSION}"
    c = classify([row("halcyon", upper)], NOW, {answer_title(a): {"status": "sent"}})
    assert ids(c.answered) == ["d1105-k9kr9-keying"]


def test_answer_title_is_empty_for_an_ask_with_no_session_line_so_no_record_can_match() -> None:
    a = only_ask(entry_text(ask_text("a1"), session=None))
    assert answer_title(a) == ""
    c = classify([row("e", entry_text(ask_text("a1"), session=None))], NOW, {"/a1/" + a.version: {"status": "sent"}})
    assert ids(c.answered) == [] and ids(c.batch) == ["a1"]


def test_answer_title_takes_a_bare_ask() -> None:
    assert answer_title(Ask(id="x", version="0123456789abcdef", session_id="S")) == "s/x/0123456789abcdef"


# --- version vectors --------------------------------------------------------------------


def _vectors() -> list[dict]:
    return json.loads(fixture("version_vectors.json"))


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: v["name"])
def test_version_vectors(vector: dict) -> None:
    if vector["via"] == "ask_version":
        assert ask_version(vector["input"]) == vector["expected"]
        return
    content = "session: s\n## T\n" + "\n".join(vector["input"]) + "\n"
    assert only_ask(content).version == vector["expected"]


def test_the_vectors_cover_the_normalization_cases() -> None:
    names = {v["name"] for v in _vectors()}
    assert {
        "by_spaced_key", "by_upper_key", "upper_case_key_is_hashed_verbatim", "interior_spacing_is_verbatim",
        "non_ascii_value", "stray_non_ask_line_inside_a_block", "updated_line_inside_a_block",
        "empty_after_is_hashed",
    } <= names
    # distinct inputs that must not collide
    by_name = {v["name"]: v["expected"] for v in _vectors()}
    assert by_name["plain"] == by_name["by_spaced_key"] == by_name["by_upper_key"] == by_name["by_in_the_middle"]
    assert by_name["plain"] == by_name["stray_non_ask_line_inside_a_block"] == by_name["updated_line_inside_a_block"]
    assert by_name["plain"] == by_name["indent_and_trailing_space"]
    assert len({by_name[n] for n in ("plain", "upper_case_key_is_hashed_verbatim", "interior_spacing_is_verbatim", "non_ascii_value", "empty_after_is_hashed")}) == 5


# --- an ask: line inside an open block always ends it ---------------------------------------


def test_a_bad_id_ask_line_closes_the_open_block_and_nothing_merges_into_the_previous_ask() -> None:
    first = ask_text("a1")
    alone = only_ask(entry_text(first))
    second = ask_text("Bad_Id", option=["X", "Y"], question="Other?")
    (t,) = parse_session("e", entry_text(first, second)).threads
    (a,) = t.asks
    assert a.id == "a1"
    assert [(o.n, o.text) for o in a.options] == [(1, "Ship"), (2, "Wait")]
    assert a.question == "Ship it?"
    assert a.version == alone.version  # the second ask changed nothing about the first


def test_the_lines_after_a_bad_id_are_notes_not_part_of_any_ask() -> None:
    content = entry_text(ask_text("a1"), ask_text("Bad_Id", option=["X", "Y"], question="Other?"))
    (t,) = parse_session("e", content).threads
    assert "ask: Bad_Id" in t.notes
    assert "question: Other?" in t.notes
    assert "option: X" in t.notes and "option: Y" in t.notes
    assert all("Other?" not in a.question and a.id != "Bad_Id" for a in t.asks)


def test_a_bad_id_ask_line_inside_a_block_is_a_bad_id_finding() -> None:
    content = entry_text(ask_text("a1"), ask_text("Bad_Id"))
    assert ("bad-id", "Bad_Id") in pairs(findings(row("e", content)))


def test_a_valid_id_after_a_bad_id_opens_a_clean_new_block() -> None:
    content = entry_text(ask_text("a1"), ask_text("Bad_Id", question="Lost?"), ask_text("a2", question="Third?"))
    (t,) = parse_session("e", content).threads
    assert [(a.id, a.question, len(a.options)) for a in t.asks] == [("a1", "Ship it?", 2), ("a2", "Third?", 2)]


def test_a_bad_id_ask_line_with_prose_naming_sam_inside_a_block_is_a_legacy_ask_outside_it() -> None:
    content = entry_text(ask_text("a1") + "\nask: Sam please pick the port")
    (t,) = parse_session("e", content).threads
    assert t.asks[0].version == only_ask(entry_text(ask_text("a1"))).version
    assert t.needs_sam == "ask: Sam please pick the port"


# --- a repeated after: is conditional when any value is non-empty ---------------------------


@pytest.mark.parametrize("first,second", [("", "engine deploys"), ("engine deploys", ""), ("none", "engine deploys")])
def test_a_repeated_after_with_any_non_empty_value_is_conditional(first: str, second: str) -> None:
    content = entry_text(ask_text(after=first) + f"\nafter: {second}")
    a = only_ask(content)
    assert a.after == "engine deploys" and a.conditional is True
    assert ("repeated-field", "a1") in pairs(findings(row("e", content)))
    assert ids(classify([row("e", content)], NOW, ()).conditional) == ["a1"]


def test_a_repeated_after_with_only_empty_values_is_not_conditional() -> None:
    a = only_ask(entry_text(ask_text(after="") + "\nafter:"))
    assert a.after == "" and a.conditional is False


# --- nexus-wc4jj.13: S1 follow-ups --------------------------------------------------------


@pytest.mark.parametrize("word", ["sam", "ok", "fixme", "Sam"])
def test_a_lone_id_shaped_ask_word_is_legacy_prose_not_a_block(word: str) -> None:
    (t,) = parse_session("e", f"## T\nask: {word}\nstate: x\n").threads
    assert t.asks == ()
    if word.lower() == "sam":
        assert t.needs_sam == f"ask: {word}" and t.notes == ()
    else:
        assert t.needs_sam == "" and t.notes == (f"ask: {word}",)


def test_a_legacy_ask_sam_reaches_needs_sam_on_the_board() -> None:
    board = build_board([row("e", "## T\nask: sam\n")], NOW)
    assert [i.thread.needs_sam for i in board.needs_you] == ["ask: sam"]
    assert board.structured == ()


def test_a_lone_ask_word_at_the_end_of_the_entry_is_legacy_prose() -> None:
    assert parse_session("e", "## T\nstate: x\nask: ok").threads[0].asks == ()


def test_an_id_with_one_following_ask_field_still_opens_a_block() -> None:
    (a,) = parse_session("e", "## T\nask: a1\nquestion: Q?\n").threads[0].asks
    assert (a.id, a.question) == ("a1", "Q?")


def test_ask_fields_after_a_thread_field_do_not_make_the_lone_id_a_block() -> None:
    (t,) = parse_session("e", "## T\nask: a1\nstate: x\nquestion: Q?\n").threads
    assert t.asks == ()


def test_an_empty_needs_sam_then_a_structured_ask_keeps_the_structured_ask() -> None:
    (t,) = parse_session("e", entry_text(ask_text("a1"), thread_lines="needs_sam:")).threads
    assert [a.id for a in t.asks] == ["a1"]
    assert t.needs_sam == ""
    assert t.notes == ()


def test_an_empty_needs_sam_then_a_structured_ask_is_not_a_lint_finding() -> None:
    assert findings(row("e", entry_text(ask_text("a1"), thread_lines="needs_sam:"))) == ()


def test_an_empty_needs_sam_then_a_structured_ask_is_classified() -> None:
    c = classify([row("e", entry_text(ask_text("a1"), thread_lines="needs_sam:"))], NOW, ())
    assert ids(c.batch) == ["a1"]


def test_a_lone_ask_with_a_bad_id_and_fields_is_surfaced_not_lost() -> None:
    content = entry_text(ask_text("Bad_Id", question="Other?"))
    (t,) = parse_session("e", content).threads
    assert t.asks == ()
    assert t.needs_sam != "" and "Bad_Id" in t.needs_sam and "Other?" in t.needs_sam
    assert [(u.id, u.question) for u in t.unopened] == [("Bad_Id", "Other?")]
    board = build_board([row("e", content)], NOW)
    assert len(board.needs_you) == 1


def test_classify_names_an_ask_whose_id_is_bad() -> None:
    c = classify([row("e", entry_text(ask_text("Bad_Id", question="Other?")))], NOW, ())
    assert [(u.entry, u.id, u.question) for u in c.malformed] == [("e", "Bad_Id", "Other?")]
    assert all(not g for g in (c.interrupt, c.batch, c.quiet, c.conditional, c.answered))


def test_a_bad_id_beside_a_good_ask_is_surfaced_too_and_the_good_ask_is_untouched() -> None:
    content = entry_text(ask_text("a1"), ask_text("Bad_Id", question="Lost?"))
    c = classify([row("e", content)], NOW, ())
    assert ids(c.batch) == ["a1"]
    assert [u.id for u in c.malformed] == ["Bad_Id"]


def test_a_bad_id_ask_line_with_no_fields_after_it_is_not_malformed() -> None:
    c = classify([row("old", "## T\nask: Sam please pick the port\n")], NOW, ())
    assert c.malformed == ()


def test_a_bad_id_ask_still_has_its_lint_finding() -> None:
    assert ("bad-id", "Bad_Id") in pairs(findings(row("e", entry_text(ask_text("Bad_Id")))))


@pytest.mark.parametrize("options", [["Ship", ""], ["", ""], ["Ship", "[irreversible]"]])
def test_few_options_counts_only_non_blank_options(options: list[str]) -> None:
    found = findings(row("e", entry_text(ask_text(option=options, rec=None, why=None))))
    assert "few-options" in {f.rule for f in found}
    assert "empty-option" in {f.rule for f in found}  # still fires too


def test_two_real_options_with_a_blank_third_is_not_few_options() -> None:
    found = findings(row("e", entry_text(ask_text(option=["Ship", "Wait", ""]))))
    assert "few-options" not in {f.rule for f in found}


# --- merge_asks with answer records (console) ---------------------------------------------


def _open_asks(records, content: str | None = None, beads=()):
    entries = [row("release", content or fixture("classes.txt"))]
    board = build_board(entries, NOW)
    return merge_asks(board.needs_you, beads, board.structured, records)


def test_merge_asks_drops_an_ask_with_a_sent_record() -> None:
    before = _open_asks(())
    after = _open_asks([sent_record("already-answered")])
    assert "already-answered" in [o.ask.id for o in before.structured]
    assert "already-answered" not in [o.ask.id for o in after.structured]
    assert after.count == before.count - 1


def test_merge_asks_keeps_an_ask_whose_record_is_not_sent() -> None:
    kept = _open_asks([sent_record("already-answered", "sending")])
    assert "already-answered" in [o.ask.id for o in kept.structured]


def test_merge_asks_keeps_an_ask_whose_record_is_for_an_older_version() -> None:
    kept = _open_asks([sent_record("already-answered", version="0123456789abcdef")])
    assert "already-answered" in [o.ask.id for o in kept.structured]


def test_an_answered_ask_folded_into_a_bead_leaves_the_bead_pointer_and_question() -> None:
    board = build_board([row("halcyon", fixture("halcyon.txt"))], NOW)
    ask = board.structured[0].thread.asks[0]
    rec = {"title": answer_title(ask), "content": json.dumps({"status": "sent"})}
    bead = Bead("nexus-k9kr9", "Keying")
    asks = merge_asks(board.needs_you, (bead,), board.structured, [rec])
    assert asks.beads[0].ask_pointers == ()
    assert asks.beads[0].raised_by == ()
    assert asks.count == 1  # the bead itself still needs Sam
