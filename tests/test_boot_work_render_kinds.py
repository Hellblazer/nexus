# SPDX-License-Identifier: AGPL-3.0-or-later
"""Each ``boot_work`` entry kind the engine publishes renders as a sentence (nexus-p2m1x.48).

``render`` put every ``what`` after "The engine is", which holds for a gerund ("building search indexes") and not for the
rest: the restart notice printed "The engine is restart the engine once: ..., running for 27 h 05 m" in every
``nx doctor`` run against an engine that needed its one restart (the 27 h is how long the notice had stood). One row per kind, with the entry shaped as the engine
publishes it (``CompletionBootWork``, ``IdUniqueIndexPass``, ``MoveBootWork``, ``PciBootWork``, ``LeafIndexPass``,
the supervisor's migration record), pins the first line and the line under it.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from nexus.progress import boot_work
from nexus.progress.boot_work import render

NOW = 1_800_000_000.0
ON_STOP_BUILD = "a build in flight keeps running in the database and finishes there"


def _entry(eid: str, what: str, **kw) -> dict:
    base = {
        "id": eid, "what": what, "started_at": "2027-01-15T04:55:00Z", "done": None, "total": None, "unit": None,
        "step": None, "safe_to_stop": True, "on_stop": ON_STOP_BUILD,
    }
    return {**base, **kw}


def test_the_fixture_clock_is_what_the_rows_below_assume() -> None:
    # started_at of every row is 3 h 05 m before NOW: every "for 3 h 05 m" below hangs on this, so a wrong NOW fails here first
    assert datetime.fromisoformat("2027-01-15T04:55:00+00:00").timestamp() == NOW - (3 * 3600 + 5 * 60)


_RESTART = _entry(
    "rdr229-restart",
    "restart the engine once: it applies the P3.5b changesets that a collection rename waits for (15 of 15 not applied yet)",
    on_stop="nothing is lost: the restart is the step that applies them",
)
_HELD_CORRUPT = _entry(
    "id-unique-index-held",
    "the unique id index on catalog_documents was not built: two rows share an id key, which means the table is corrupt "
    "(each collection name maps to one id); the next engine start tries the build again",
    done=0, total=1, unit="indexes",
    on_stop="nothing is lost and nothing is running: this is a standing condition, which a restart does not clear; the corrupt "
    "rows or the other index are the operator's to repair",
)

# (id, what, extra fields) -> (first line, second line). The second line is what stopping or interrupting costs.
_CASES = {
    "restart notice": (
        _RESTART,
        "The engine needs one restart: it applies the P3.5b changesets that a collection rename waits for "
        "(15 of 15 not applied yet), waiting for 3 h 05 m.",
        "  Nothing is lost: the restart is the step that applies them.",
    ),
    "held standing condition": (
        _HELD_CORRUPT,
        "The engine has a standing condition: the unique id index on catalog_documents was not built: two rows share an id "
        "key, which means the table is corrupt (each collection name maps to one id); the next engine start tries the build "
        "again, listed for 3 h 05 m.",
        "  Nothing is lost and nothing is running: this is a standing condition, which a restart does not clear; the corrupt "
        "rows or the other index are the operator's to repair.",
    ),
    "search index build (pci)": (
        _entry("pci-reconcile", "building search indexes", done=3, total=7, unit="indexes"),
        "The engine is building search indexes, running for 3 h 05 m: 3 of 7 indexes.",
        "  Interrupting this command does not stop it. Stopping the service now: " + ON_STOP_BUILD + ".",
    ),
    "unique id index build (leaf pass)": (
        _entry("leaf-index-pass", "building unique id indexes", done=0, total=4, unit="indexes"),
        "The engine is building unique id indexes, running for 3 h 05 m: 0 of 4 indexes.",
        "  Interrupting this command does not stop it. Stopping the service now: " + ON_STOP_BUILD + ".",
    ),
    "unique id index build (rename pass)": (
        _entry("id-unique-index-pass", "building the unique id indexes the rename's upserts use", done=1, total=2, unit="indexes"),
        "The engine is building the unique id indexes the rename's upserts use, running for 3 h 05 m: 1 of 2 indexes.",
        "  Interrupting this command does not stop it. Stopping the service now: " + ON_STOP_BUILD + ".",
    ),
    "supervisor migration": (
        _entry("schema-migration", "migrating its database schema (at vectors-030-1, 12 changeset(s) left)", safe_to_stop=False,
               on_stop="stopping it mid-migration rolls the open changeset back"),
        "The engine is migrating its database schema (at vectors-030-1, 12 changeset(s) left), running for 3 h 05 m.",
        "  Commands that need the engine wait until it finishes; interrupting this command does not stop it. "
        "Stopping the service: stopping it mid-migration rolls the open changeset back.",
    ),
    "data move working": (
        _entry("data-move:rdr229-stamp-topics", "working: rdr229-stamp-topics", done=5, total=10, unit="chunks"),
        "The engine is working: rdr229-stamp-topics, running for 3 h 05 m: 5 of 10 chunks.",
        "  Interrupting this command does not stop it. Stopping the service now: " + ON_STOP_BUILD + ".",
    ),
    "data move stalled": (
        _entry("data-move:rdr229-stamp-topics", "stalled: rdr229-stamp-topics: no chunk has completed for 20 minutes",
               done=5, total=10, unit="chunks"),
        "The engine is stalled: rdr229-stamp-topics: no chunk has completed for 20 minutes, running for 3 h 05 m: 5 of 10 chunks.",
        "  Interrupting this command does not stop it. Stopping the service now: " + ON_STOP_BUILD + ".",
    ),
    "data move runs complete": (
        _entry("data-move:rdr229-stamp-topics", "runs complete: rdr229-stamp-topics (verification pending)"),
        "The engine has a data move whose runs are complete: rdr229-stamp-topics (verification pending), listed for 3 h 05 m.",
        "  Interrupting this command does not stop it. Stopping the service now: " + ON_STOP_BUILD + ".",
    ),
    "data move blocked": (
        _entry("data-move:rdr229-stamp-topics", "blocked: rdr229-stamp-topics: lock timeout (55P03)"),
        "The engine is blocked: rdr229-stamp-topics: lock timeout (55P03), listed for 3 h 05 m.",
        "  Stopping the service now: " + ON_STOP_BUILD + ".",
    ),
    "data move paused": (
        _entry("data-move:rdr229-stamp-topics", "paused: rdr229-stamp-topics by operator"),
        "The engine is paused: rdr229-stamp-topics by operator, listed for 3 h 05 m.",
        "  Stopping the service now: " + ON_STOP_BUILD + ".",
    ),
    "data move queued": (
        _entry("data-move:rdr229-stamp-topics", "queued: rdr229-stamp-topics (the worker has not reached it yet)"),
        "The engine has a data move queued: rdr229-stamp-topics (the worker has not reached it yet), listed for 3 h 05 m.",
        "  Interrupting this command does not stop it. Stopping the service now: " + ON_STOP_BUILD + ".",
    ),
    "data move failed": (
        _entry("data-move:rdr229-stamp-topics", "failed: rdr229-stamp-topics: check violation (23514)"),
        "The engine has a failed data move: rdr229-stamp-topics: check violation (23514), listed for 3 h 05 m.",
        "  Stopping the service now: " + ON_STOP_BUILD + ".",
    ),
    "data-move worker not running": (
        _entry("data-move-worker", "blocked: the data-move worker is not running, so no data move is worked"),
        "The engine is blocked: the data-move worker is not running, so no data move is worked, listed for 3 h 05 m.",
        "  Stopping the service now: " + ON_STOP_BUILD + ".",
    ),
    "an entry that already says The engine": (
        _entry("legacy", "The engine is finishing up"),
        "The engine is finishing up, running for 3 h 05 m.",
        "  Interrupting this command does not stop it. Stopping the service now: " + ON_STOP_BUILD + ".",
    ),
}


@pytest.mark.parametrize("kind", sorted(_CASES))
def test_each_entry_kind_renders_as_a_sentence(kind: str) -> None:
    entry, first, second = _CASES[kind]
    assert render([dict(entry)], now=NOW) == [first, second]


def test_the_restart_notice_names_no_run_and_never_says_the_engine_is_a_verb() -> None:
    first = render([dict(_RESTART)], now=NOW)[0]
    assert "The engine is restart" not in first and "running for" not in first
    assert first.startswith("The engine needs one restart: ")


def test_a_restart_notice_with_no_started_at_has_no_duration() -> None:
    entry = {**_RESTART, "started_at": None}
    assert render([entry], now=NOW)[0] == (
        "The engine needs one restart: it applies the P3.5b changesets that a collection rename waits for "
        "(15 of 15 not applied yet)."
    )


def test_the_service_status_view_of_the_restart_notice_does_not_say_what_stopping_the_service_does() -> None:
    # nx daemon service status passes waiting=False; for a request that nothing runs under, the engine's own sentence stands
    lines = render([dict(_RESTART)], now=NOW, waiting=False)
    assert lines[1] == "  Nothing is lost: the restart is the step that applies them."
    assert not any("Stopping the service" in line or "Interrupting" in line for line in lines)


def test_a_restart_notice_with_no_colon_still_renders_as_a_sentence() -> None:
    entry = {**_RESTART, "what": "restart the engine once"}
    assert render([entry], now=NOW)[0] == "The engine needs one restart, waiting for 3 h 05 m."


def test_the_matrix_covers_every_class_the_client_knows() -> None:
    # non-vacuity: a class added to the renderer without a row here fails this
    pinned = {what.split(":")[0] for entry, *_ in _CASES.values() if (what := entry["what"]) and ": " in what}
    assert set(boot_work._CLASS_LEAD) <= pinned | {"runs complete"}


@pytest.mark.parametrize("cls", ["blocked", "paused", "failed"])
@pytest.mark.parametrize("waiting", [True, False])
def test_an_idle_data_move_never_says_interrupting_this_command_or_waiting_for_the_engine(cls: str, waiting: bool) -> None:
    # nothing runs for a blocked, paused or failed move: the interrupt advice and "commands wait until it finishes" are false
    entry = _entry("data-move:rdr229-stamp-topics", f"{cls}: rdr229-stamp-topics", safe_to_stop=waiting)
    text = "\n".join(render([entry], now=NOW, waiting=waiting))
    assert "Interrupting" not in text and "wait until it finishes" not in text
    assert "Stopping the service now: " + ON_STOP_BUILD in text


@pytest.mark.parametrize("cls", ["blocked", "paused", "failed"])
def test_an_idle_data_move_without_safe_to_stop_still_says_nothing_false(cls: str) -> None:
    entry = _entry("data-move:rdr229-stamp-topics", f"{cls}: rdr229-stamp-topics", safe_to_stop=False)
    text = "\n".join(render([entry], now=NOW))
    assert "wait until it finishes" not in text and "Interrupting" not in text


def test_only_a_running_class_says_running_for() -> None:
    for what in ("working: m", "stalled: m: no chunk"):
        assert ", running for 3 h 05 m" in render([_entry("data-move:m", what)], now=NOW)[0]
    for what in ("blocked: m", "paused: m", "queued: m", "failed: m", "runs complete: m"):
        first = render([_entry("data-move:m", what)], now=NOW)[0]
        assert ", listed for 3 h 05 m" in first and "running for" not in first
