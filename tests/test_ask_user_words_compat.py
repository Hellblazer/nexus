# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``--sam-words`` / ``sam_words`` to ``--user-words`` / ``user_words`` compatibility tests (nexus-sby49).

Split out of ``tests/test_ask_cmd.py`` to keep that file under its size ceiling. The
fixtures and helpers are the ones ``test_ask_cmd`` defines.
"""
from __future__ import annotations

import json

from tests.test_ask_cmd import (  # noqa: F401  (``world`` is a pytest fixture, imported by name)
    MEDIATOR,
    TITLE,
    World,
    cli_args,
    forged,
    js,
    mediator_args,
    untouched,
    world,
    VERSION,
    REF,
)


# --- the --sam-words / sam_words -> --user-words / user_words rename (nexus-sby49) ----------------


def test_the_sam_words_flag_still_works_for_now_and_says_it_is_deprecated(world: World) -> None:
    r = world.run("answer", REF, "1", "--expect", VERSION, "--via", "mediator", "--sam-words", "accept the gap on k9kr9")
    assert r.exit_code == 0, r.output
    assert "Deprecated: --sam-words is now --user-words" in r.stderr
    assert len(r.stderr.strip().splitlines()) == 1 and "deprecated" not in r.stdout.lower()
    rec = world.t2.record()
    assert rec["user_words"] == rec["sam_words"] == "accept the gap on k9kr9"
    assert world.tuples.calls[0]["dims"]["from"] == MEDIATOR  # the answer was sent as usual


def test_the_documented_flag_prints_no_deprecation_and_is_all_the_help_shows(world: World) -> None:
    r = world.run(*mediator_args())
    assert r.exit_code == 0, r.output
    assert "deprecated" not in r.output.lower()
    helped = world.run("answer", "--help").output
    assert "--user-words" in helped and "--sam-words" not in helped  # the alias is hidden


def test_the_alias_keeps_the_mediator_checks(world: World) -> None:
    r = world.run("answer", REF, "1", "--expect", VERSION, "--via", "mediator", "--sam-words", "x" * 501)
    assert r.exit_code != 0 and "--user-words is 501 characters" in r.output
    r = world.run("answer", REF, "1", "--expect", VERSION, "--via", "mediator", "--sam-words", "   ")
    assert r.exit_code != 0 and "--via mediator needs --user-words" in r.output
    r = world.run(*cli_args("--sam-words", "x"))
    assert r.exit_code != 0 and "--user-words and --batch-words are for --via mediator" in r.output
    untouched(world)


def test_both_flags_at_once_are_a_usage_error(world: World) -> None:
    r = world.run(*mediator_args("--sam-words", "x"))
    assert r.exit_code != 0 and "not both" in r.output
    untouched(world)


def test_a_new_record_carries_both_keys_with_one_value(world: World) -> None:
    # sam_words is the key the 8.5.9 plugin's drain hook reads; it is written until the second client
    # release after the one that first ships user_words, so an older plugin copy still finds the words in a new client's record.
    r = world.run(*mediator_args())
    assert r.exit_code == 0, r.output
    rec = world.t2.record()
    assert rec["user_words"] == rec["sam_words"] == "accept the gap on k9kr9"


def test_the_digest_reads_a_record_stored_before_the_rename() -> None:
    w = World()
    legacy = {k: v for k, v in js("answer_record_v1.json").items() if k != "user_words"}
    legacy["sam_words"] = "an answer stored before the rename"
    w.t2.add("asks", TITLE, json.dumps(legacy))
    out = w.run("digest").output
    assert "an answer stored before the rename" in out and "skipped" not in out, out
    entry = json.loads(w.run("list", "--json").stdout)["digest"][0]
    assert entry["user_words"] == "an answer stored before the rename"


def test_list_json_digest_keeps_sam_words_as_a_copy_of_user_words_for_one_release() -> None:
    w = World()
    w.t2.add("asks", TITLE, forged(user_words="the new key", sam_words="the old key"))
    entry = json.loads(w.run("list", "--json").stdout)["digest"][0]
    assert entry["user_words"] == "the new key"
    assert entry["sam_words"] == entry["user_words"]


def test_the_digest_prefers_user_words_when_both_keys_differ() -> None:
    w = World()
    w.t2.add("asks", TITLE, forged(user_words="the new key", sam_words="the old key"))
    out = w.run("digest").output
    assert "the new key" in out and "the old key" not in out, out
