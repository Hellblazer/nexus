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

from nexus.console.threads import parse_session, parse_timestamp

FIXTURES = Path(__file__).parent / "fixtures" / "asks"

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
VIA = rf"(?:cli|mediator:{UUID})"

#: RDR-245 lines 575-578: the body is a closed grammar, one pattern per line, in this order.
MAIL_LINES = (
    ("header", re.compile(r"nx-ask-answer v1")),
    ("ask", re.compile(r"ask: ([a-z0-9-]{1,40})")),
    ("version", re.compile(r"version: ([0-9a-f]{16})")),
    ("choice", re.compile(r"choice: (\d+|none)")),
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


def test_answer_record_is_one_object_with_exactly_the_named_keys() -> None:
    rec = _json("answer_record_v1.json")
    assert set(rec) == ANSWER_KEYS


def test_answer_record_field_types() -> None:
    rec = _json("answer_record_v1.json")
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


def test_answer_status_is_one_of_three() -> None:
    assert _json("answer_record_v1.json")["status"] in {"sending", "sent", "failed"}


def test_choice_is_an_integer_within_the_option_count_or_null() -> None:
    rec = _json("answer_record_v1.json")
    choice = rec["choice"]
    assert type(choice) is int  # a bool or a numeric string is not a choice
    assert 1 <= choice <= len(rec["options"])


def test_via_is_cli_or_a_lower_case_canonical_mediator_uuid() -> None:
    via = _json("answer_record_v1.json")["via"]
    assert re.fullmatch(VIA, via)
    assert not re.fullmatch(VIA, "mediator:8B0E1D42-6C3A-4F19-A2D7-5E9F0C1B3A64")
    assert not re.fullmatch(VIA, "mediator:8b0e1d426c3a4f19a2d75e9f0c1b3a64")
    assert re.fullmatch(VIA, "cli")


def test_answer_title_is_asker_session_ask_version_from_the_parsed_ask() -> None:
    session, ask = _halcyon_ask()
    title = f"{session.session_id}/{ask.id}/{ask.version}"
    batch = _json("batch_record_v1.json")
    assert title in batch["answer_titles"]
    assert re.fullmatch(rf"{UUID}/[a-z0-9-]{{1,40}}/[0-9a-f]{{16}}", title)


def test_answer_record_matches_the_ask_it_answers() -> None:
    _, ask = _halcyon_ask()
    rec = _json("answer_record_v1.json")
    assert rec["question"] == ask.question
    assert rec["asked"] == ask.asked
    assert rec["blocking"] is ask.blocking
    assert [o for o in rec["options"]] == [
        o.text + (" [irreversible]" if o.irreversible else "") for o in ask.options
    ]


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
    assert re.fullmatch(rf"batch/{UUID}/\d{{8}}T\d{{6}}Z", answer["batch_title"])
    # an answer title starts with the asker's session id, never the word "batch"
    for title in batch["answer_titles"]:
        assert not title.startswith("batch/")
        assert re.match(rf"{UUID}/", title)


# --- mail body -----------------------------------------------------------------------


def _mail() -> str:
    return (FIXTURES / "mail_body_v1.txt").read_text(encoding="utf-8")


def test_mail_body_matches_the_closed_grammar() -> None:
    _, ask = _halcyon_ask()
    assert mail_problems(f"ask:{ask.id}@{ask.version}", _mail()) == []


def test_mail_body_agrees_with_the_answer_record_and_the_ask() -> None:
    _, ask = _halcyon_ask()
    rec = _json("answer_record_v1.json")
    fields = dict(line.split(": ", 1) for line in _mail().splitlines()[1:])
    assert fields["ask"] == ask.id
    assert fields["version"] == ask.version
    assert fields["choice"] == str(rec["choice"])
    assert fields["via"] == rec["via"]
    assert fields["answer"] == rec["answer_id"]


@pytest.mark.parametrize(("line", "good", "bad"), [
    ("header", "nx-ask-answer v1", "nx-ask-answer v2"),
    ("ask", "ask: a1-b2", "ask: A1"),
    ("ask", "ask: " + "a" * 40, "ask: " + "a" * 41),
    ("version", "version: 0123456789abcdef", "version: 0123456789ABCDEF"),
    ("version", "version: 0123456789abcdef", "version: 0123456789abcde"),
    ("choice", "choice: 12", "choice: 1.5"),
    ("choice", "choice: none", "choice: None"),
    ("via", "via: cli", "via: mediator"),
    ("via", "via: mediator:8b0e1d42-6c3a-4f19-a2d7-5e9f0c1b3a64", "via: mediator:8B0E1D42-6C3A-4F19-A2D7-5E9F0C1B3A64"),
    ("answer", "answer: 5c1e9a7f0b2d4a83", "answer: 5c1e9a7f0b2d4a8"),
])
def test_each_mail_line_pattern(line: str, good: str, bad: str) -> None:
    pattern = dict(MAIL_LINES)[line]
    assert pattern.fullmatch(good)
    assert not pattern.fullmatch(bad)


@pytest.mark.parametrize("mutate", [
    lambda body: body + "extra: line\n",
    lambda body: body.replace("choice: 1\n", ""),
    lambda body: body.replace("ask: d1105-k9kr9-keying\nversion: f22ca640c6bfa3d2\n",
                              "version: f22ca640c6bfa3d2\nask: d1105-k9kr9-keying\n"),
    lambda body: body.replace("choice: 1", "choice: 01x"),
])
def test_mail_grammar_is_closed(mutate) -> None:
    assert mail_problems("ask:d1105-k9kr9-keying@f22ca640c6bfa3d2", mutate(_mail()))


def test_mail_correlation_id_must_agree() -> None:
    assert mail_problems("ask:d1105-k9kr9-keying@0000000000000000", _mail()) == [
        "correlation id disagrees with the body"
    ]
