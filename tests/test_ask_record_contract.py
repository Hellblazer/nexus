# SPDX-License-Identifier: AGPL-3.0-or-later
"""The answer-record, batch-record and mail-body contract (RDR-245, nexus-wc4jj.3).

The fixtures under ``tests/fixtures/asks/`` are the one shape the CLI (Step 2)
writes and the drain hook (Step 4) reads. These tests pin that shape and tie
it to the parser: the record title is built from the version ``threads.py``
computes, so a change to the version rule breaks here first. No I/O beyond
reading the fixtures.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nexus.console.threads import answer_title, parse_session, parse_timestamp

FIXTURES = Path(__file__).parent / "fixtures" / "asks"
HALCYON_SESSION = "3f2a9c1e-5b7d-4e2a-9c31-0d8e6f4a2b17"

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
STAMP = r"[0-9]{8}T[0-9]{6}Z"
#: Every answer-record fixture, with the mail that goes with it (None: a failed send posts no mail).
RECORDS = (
    ("answer_record_v1.json", "mail_body_v1.txt"),
    ("answer_record_v1_note_only.json", "mail_body_v1_note_only.txt"),
    ("answer_record_v1_cli.json", "mail_body_v1_cli.txt"),
    ("answer_record_v1_sending.json", "mail_body_v1_sending.txt"),
    ("answer_record_v1_failed.json", None),
)
# The text for a 404 on the record GET ("no matching answer record for this ask version") belongs to Step 4,
# the drain hook; nothing here pins it.
VIA = rf"(?:cli|mediator:{UUID})"

#: RDR-245 lines 575-578: the body is a closed grammar, one pattern per line, in this order.
MAIL_LINES = (
    ("header", re.compile(r"nx-ask-answer v1")),
    ("ask", re.compile(r"ask: ([a-z0-9-]{1,40})")),
    ("version", re.compile(r"version: ([0-9a-f]{16})")),
    ("choice", re.compile(r"choice: ([0-9]{1,3}|none)")),
    ("via", re.compile(rf"via: ({VIA})")),
    ("answer", re.compile(r"answer: ([0-9a-f]{16})")),
)

ANSWER_KEYS = {
    "answer_id", "question", "options", "choice", "note", "sam_words", "batch_title",
    "via", "asked", "blocking", "tty", "time", "status", "tuple_id", "error",
}
BATCH_KEYS = {"batch_words", "via", "time", "answer_titles"}


def _json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _halcyon_ask():
    session = parse_session("halcyon", (FIXTURES / "halcyon.txt").read_text(encoding="utf-8"))
    return session, session.threads[0].asks[0]


def mail_problems(correlation_id: str, body: str) -> list[str]:
    """What is wrong with a mail body, or an empty list. The closed grammar of RDR lines 547-556."""
    lines = body.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # one trailing newline is part of the body
    if len(lines) != len(MAIL_LINES):
        return [f"expected {len(MAIL_LINES)} lines, got {len(lines)}"]
    found: dict[str, str] = {}
    problems: list[str] = []
    for (name, pattern), line in zip(MAIL_LINES, lines):
        m = pattern.fullmatch(line)
        if m is None:
            problems.append(f"{name}: {line!r}")
        elif m.groups():
            found[name] = m.group(1)
    if not problems and correlation_id != f"ask:{found['ask']}@{found['version']}":
        problems.append("correlation id disagrees with the body")
    return problems


# --- answer record -------------------------------------------------------------------

RECORD_FILES = [r for r, _m in RECORDS]


def _mail_choice(record: dict) -> str:
    """The mail's ``choice:`` for a record: the number, or ``none`` for a note-only answer."""
    return "none" if record["choice"] is None else str(record["choice"])


@pytest.mark.parametrize("name", RECORD_FILES)
def test_answer_record_is_one_object_with_exactly_the_named_keys(name: str) -> None:
    assert set(_json(name)) == ANSWER_KEYS


@pytest.mark.parametrize("name", RECORD_FILES)
def test_answer_record_field_types(name: str) -> None:
    rec = _json(name)
    assert re.fullmatch(r"[0-9a-f]{16}", rec["answer_id"])
    assert isinstance(rec["question"], str) and rec["question"]
    assert isinstance(rec["options"], list) and all(isinstance(o, str) for o in rec["options"])
    assert isinstance(rec["note"], str)
    assert isinstance(rec["sam_words"], str)
    assert rec["batch_title"] is None or isinstance(rec["batch_title"], str)
    assert type(rec["blocking"]) is bool
    assert rec["tty"] is None or isinstance(rec["tty"], str)
    assert rec["tuple_id"] is None or isinstance(rec["tuple_id"], str)
    assert rec["error"] is None or isinstance(rec["error"], str)
    assert parse_timestamp(rec["asked"]) is not None
    assert parse_timestamp(rec["time"]) is not None


@pytest.mark.parametrize("name", RECORD_FILES)
def test_answer_status_is_one_of_three(name: str) -> None:
    assert _json(name)["status"] in {"sending", "sent", "failed"}


@pytest.mark.parametrize("name", RECORD_FILES)
def test_choice_is_an_integer_within_the_option_count_or_null(name: str) -> None:
    rec = _json(name)
    choice = rec["choice"]
    if choice is None:
        return
    assert type(choice) is int  # a bool or a numeric string is not a choice
    assert 1 <= choice <= len(rec["options"])


@pytest.mark.parametrize("name", RECORD_FILES)
def test_via_is_cli_or_a_lower_case_canonical_mediator_uuid(name: str) -> None:
    assert re.fullmatch(VIA, _json(name)["via"])


def test_via_pattern_refuses_upper_case_and_bare_uuids() -> None:
    assert not re.fullmatch(VIA, "mediator:8B0E1D42-6C3A-4F19-A2D7-5E9F0C1B3A64")
    assert not re.fullmatch(VIA, "mediator:8b0e1d426c3a4f19a2d75e9f0c1b3a64")
    assert not re.fullmatch(VIA, "mediator")
    assert re.fullmatch(VIA, "cli")


@pytest.mark.parametrize("name", RECORD_FILES)
def test_a_tty_is_recorded_for_cli_and_only_for_cli(name: str) -> None:
    rec = _json(name)
    assert (rec["tty"] is not None) == (rec["via"] == "cli")


def test_a_note_only_answer_has_no_choice_and_records_its_note_as_sam_words() -> None:
    rec = _json("answer_record_v1_note_only.json")
    assert rec["choice"] is None
    assert rec["note"] and rec["sam_words"] == rec["note"]


def test_sending_has_no_tuple_yet_and_failed_carries_an_error() -> None:
    sending = _json("answer_record_v1_sending.json")
    failed = _json("answer_record_v1_failed.json")
    assert (sending["status"], sending["tuple_id"], sending["error"]) == ("sending", None, None)
    assert (failed["status"], failed["tuple_id"]) == ("failed", None)
    assert failed["error"]
    assert _json("answer_record_v1.json")["tuple_id"] and _json("answer_record_v1.json")["error"] is None


def test_answer_title_is_asker_session_ask_version_from_the_parsed_ask() -> None:
    session, ask = _halcyon_ask()
    title = answer_title(ask)
    assert title == f"{session.session_id}/{ask.id}/{ask.version}"
    batch = _json("batch_record_v1.json")
    assert title in batch["answer_titles"]
    assert re.fullmatch(rf"{UUID}/[a-z0-9-]{{1,40}}/[0-9a-f]{{16}}", title)


def test_answer_title_for_an_upper_case_session_line_is_the_lower_case_record_title() -> None:
    text = (FIXTURES / "halcyon.txt").read_text(encoding="utf-8")
    upper = parse_session("halcyon", text.replace(HALCYON_SESSION, HALCYON_SESSION.upper()))
    ask = upper.threads[0].asks[0]
    assert upper.session_id == HALCYON_SESSION.upper()
    assert answer_title(ask) == answer_title(_halcyon_ask()[1])
    assert re.fullmatch(rf"{UUID}/.*", answer_title(ask))


def test_answer_title_is_empty_when_the_entry_has_no_session_line() -> None:
    text = (FIXTURES / "halcyon.txt").read_text(encoding="utf-8").replace(f"session: {HALCYON_SESSION}\n", "")
    ask = parse_session("halcyon", text).threads[0].asks[0]
    assert ask.session_id == ""
    assert answer_title(ask) == ""


@pytest.mark.parametrize("name", RECORD_FILES)
def test_answer_record_matches_the_ask_it_answers(name: str) -> None:
    _, ask = _halcyon_ask()
    rec = _json(name)
    assert rec["question"] == ask.question
    assert rec["asked"] == ask.asked
    assert rec["blocking"] is ask.blocking
    assert rec["options"] == [o.text + (" [irreversible]" if o.irreversible else "") for o in ask.options]


# --- batch record --------------------------------------------------------------------


def test_batch_record_is_one_object_with_exactly_the_named_keys() -> None:
    rec = _json("batch_record_v1.json")
    assert set(rec) == BATCH_KEYS
    assert isinstance(rec["batch_words"], str) and 0 < len(rec["batch_words"]) <= 2000
    assert re.fullmatch(VIA, rec["via"])
    assert parse_timestamp(rec["time"]) is not None
    assert isinstance(rec["answer_titles"], list) and rec["answer_titles"]


def test_batch_title_prefix_cannot_be_an_answer_title() -> None:
    answer = _json("answer_record_v1.json")
    batch = _json("batch_record_v1.json")
    assert answer["batch_title"] is not None
    mediator = batch["via"].removeprefix("mediator:")
    stamp = parse_timestamp(batch["time"])
    assert stamp is not None
    assert answer["batch_title"] == f"batch/{mediator}/{stamp.strftime('%Y%m%dT%H%M%SZ')}"
    assert re.fullmatch(rf"batch/{UUID}/{STAMP}", answer["batch_title"])
    assert not re.fullmatch(rf"batch/{UUID}/{STAMP}", "batch/" + UUID + "/2026-11-05")
    # an answer title starts with the asker's session id, never the word "batch"
    for title in batch["answer_titles"]:
        assert not title.startswith("batch/")
        assert re.match(rf"{UUID}/", title)


# --- mail body -----------------------------------------------------------------------


def _mail(name: str = "mail_body_v1.txt") -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


MAILS = [(r, m) for r, m in RECORDS if m is not None]


@pytest.mark.parametrize(("_record", "mail"), MAILS)
def test_mail_body_matches_the_closed_grammar(_record: str, mail: str) -> None:
    _, ask = _halcyon_ask()
    assert mail_problems(f"ask:{ask.id}@{ask.version}", _mail(mail)) == []


@pytest.mark.parametrize(("record", "mail"), MAILS)
def test_mail_body_agrees_with_the_answer_record_and_the_ask(record: str, mail: str) -> None:
    _, ask = _halcyon_ask()
    rec = _json(record)
    fields = dict(line.split(": ", 1) for line in _mail(mail).splitlines()[1:])
    assert fields["ask"] == ask.id
    assert fields["version"] == ask.version
    assert fields["choice"] == _mail_choice(rec)
    assert fields["via"] == rec["via"]
    assert fields["answer"] == rec["answer_id"]


@pytest.mark.parametrize(("choice", "line"), [(None, "choice: none"), (1, "choice: 1"), (12, "choice: 12")])
def test_a_record_choice_becomes_the_mail_choice_line(choice: int | None, line: str) -> None:
    assert f"choice: {_mail_choice({'choice': choice})}" == line
    assert dict(MAIL_LINES)["choice"].fullmatch(line)


def test_the_failed_record_has_no_mail() -> None:
    assert [r for r, m in RECORDS if m is None] == ["answer_record_v1_failed.json"]


@pytest.mark.parametrize(("line", "good", "bad"), [
    ("header", "nx-ask-answer v1", "nx-ask-answer v2"),
    ("ask", "ask: a1-b2", "ask: A1"),
    ("ask", "ask: " + "a" * 40, "ask: " + "a" * 41),
    ("version", "version: 0123456789abcdef", "version: 0123456789ABCDEF"),
    ("version", "version: 0123456789abcdef", "version: 0123456789abcde"),
    ("choice", "choice: 12", "choice: 1.5"),
    ("choice", "choice: 999", "choice: 1000"),  # over-long number
    ("choice", "choice: 7", "choice: \u0661"),  # a non-ASCII digit
    ("choice", "choice: 7", "choice: \uff17"),  # a full-width digit
    ("choice", "choice: none", "choice: None"),
    ("via", "via: cli", "via: mediator"),
    ("via", "via: mediator:8b0e1d42-6c3a-4f19-a2d7-5e9f0c1b3a64", "via: mediator:8B0E1D42-6C3A-4F19-A2D7-5E9F0C1B3A64"),
    ("answer", "answer: 5c1e9a7f0b2d4a83", "answer: 5c1e9a7f0b2d4a8"),
])
def test_each_mail_line_pattern(line: str, good: str, bad: str) -> None:
    pattern = dict(MAIL_LINES)[line]
    assert pattern.fullmatch(good)
    assert not pattern.fullmatch(bad)


def test_the_timestamp_pattern_is_ascii_and_fixed_width() -> None:
    assert re.fullmatch(STAMP, "20261105T201500Z")
    assert not re.fullmatch(STAMP, "20261105T20150Z")
    assert not re.fullmatch(STAMP, "\u0662\u0660261105T201500Z")  # Arabic-Indic digits
    assert not re.fullmatch(STAMP, "2026-11-05T20:15:00Z")


@pytest.mark.parametrize("mutate", [
    lambda body: body + "extra: line\n",
    lambda body: body.replace("choice: 1\n", ""),
    lambda body: body.replace("ask: d1105-k9kr9-keying\nversion: f22ca640c6bfa3d2\n",
                              "version: f22ca640c6bfa3d2\nask: d1105-k9kr9-keying\n"),
    lambda body: body.replace("choice: 1", "choice: 01x"),
    lambda body: body.replace("choice: 1", "choice: \u0661"),
])
def test_mail_grammar_is_closed(mutate) -> None:
    assert mail_problems("ask:d1105-k9kr9-keying@f22ca640c6bfa3d2", mutate(_mail()))


def test_mail_correlation_id_must_agree() -> None:
    assert mail_problems("ask:d1105-k9kr9-keying@0000000000000000", _mail()) == [
        "correlation id disagrees with the body"
    ]
