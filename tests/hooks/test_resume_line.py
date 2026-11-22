# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Step 6 part 2: SessionStart prints the newest identity-scratch resume pointer, one short line, on ``clear`` only.

Pins ``nexus.hooks.resume_line`` and its two call sites (the ``session-start`` verb and ``nx hook session-start``), plus
the one guidance sentence in the T1 block. The store is a stub except in the two tests that drive the real opener against
a patched ``identity_scratch`` (this module opts out of ``tests/conftest.py``'s autouse stub for that:
``REAL_RESUME_POINTER_STORE``). The writer half is ``tests/hooks/test_resume_pointer.py``; the engine-backed row is
``tests/identity/test_work_engine_rows.py``.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.commands import hook as hook_cmd
from nexus.hooks import resume_line as rl
from nexus.hooks import session_context, session_start_verb
from nexus.identity.scratch_scope import IdentityScopeError

#: Opt out of the autouse ``_no_real_resume_pointer_store``: the real-opener tests below need ``_open_store`` itself.
REAL_RESUME_POINTER_STORE = True

NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)


def stamp(delta: timedelta) -> str:
    return (NOW - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def pointer(delta: timedelta, body: str = "thread: new | worktree: /w/x | state: going", tags: str = "resume-pointer") -> dict:
    return {"id": "x", "tags": tags, "content": f"resume {stamp(delta)} | {body}"}


NEW = pointer(timedelta(hours=3))
OLD = pointer(timedelta(hours=30), body="thread: old")
WORKING = {"id": "w", "tags": "working", "content": "resume 2030-01-01T00:00:00Z | thread: not a pointer"}


class Store:
    def __init__(self, entries=(), boom=None):
        self.entries, self.boom, self.opened = list(entries), boom, 0

    def list_entries(self):
        if self.boom:
            raise self.boom
        return self.entries


def opener(store):
    def open_store():
        store.opened += 1
        return store

    return open_store


@pytest.fixture()
def events(monkeypatch):
    got: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(rl, "_emit", lambda level, event, **fields: got.append((level, event, fields)))
    return got


def read(payload, store, now=NOW):
    return rl.resume_line(payload, open_store=opener(store), now=now)


class TestSources:
    def test_clear_prints_the_newest_pointer_as_exactly_one_line_with_its_age(self, events) -> None:
        line = read({"source": "clear"}, Store([OLD, WORKING, NEW]))  # the stamp, not the list order, decides
        assert line == "Resume pointer (identity scratch, written 3h ago): thread: new | worktree: /w/x | state: going"
        assert "\n" not in line

    @pytest.mark.parametrize("source", ["resume", "startup", "compact", "fork", "", None, 7])
    def test_every_other_source_prints_nothing_logs_nothing_and_never_opens_the_store(self, source, events) -> None:
        """``resume`` too: an in-place /restart restores the conversation, so a pointer would be noise."""
        store = Store([NEW])
        assert read({"source": source}, store) is None
        assert store.opened == 0 and events == []

    def test_no_payload_and_a_payload_without_a_source_print_nothing(self, events) -> None:
        store = Store([NEW])
        assert read(None, store) is None and read({}, store) is None
        assert store.opened == 0


class TestWhichEntry:
    def test_a_stamp_beats_list_order(self, events) -> None:
        for entries in ([OLD, NEW], [NEW, OLD]):
            assert "thread: new" in read({"source": "clear"}, Store(entries))

    def test_the_exact_tag_and_the_head_are_both_required(self, events) -> None:
        tag_only = {"id": "1", "tags": "resume-pointer", "content": "wears the tag, no head"}
        head_only = {"id": "2", "tags": "working", "content": f"resume {stamp(timedelta(hours=1))} | thread: head only"}
        retired = {"id": "3", "tags": "resume", "content": f"resume {stamp(timedelta(hours=1))} | thread: the retired tag"}
        assert read({"source": "clear"}, Store([tag_only, head_only, retired])) is None
        assert "thread: new" in read({"source": "clear"}, Store([tag_only, head_only, retired, NEW]))

    def test_a_tag_in_a_list_is_still_the_exact_tag(self, events) -> None:
        assert "thread: new" in read({"source": "clear"}, Store([pointer(timedelta(hours=3), tags="working, resume-pointer")]))

    def test_a_pointer_older_than_48_hours_is_dropped(self, events) -> None:
        old = pointer(timedelta(hours=49), body="thread: stale")
        assert read({"source": "clear"}, Store([old])) is None
        assert "thread: stale" not in (read({"source": "clear"}, Store([old, NEW])) or "")
        fresh = pointer(timedelta(hours=47), body="thread: edge")
        assert "47h ago" in read({"source": "clear"}, Store([fresh]))

    def test_an_unparseable_stamp_is_not_a_pointer(self, events) -> None:
        bad = {"id": "1", "tags": "resume-pointer", "content": "resume 2026-99-99T99:99:99Z | thread: x"}
        assert read({"source": "clear"}, Store([bad])) is None

    @pytest.mark.parametrize("delta,label", [(timedelta(seconds=20), "just now"), (timedelta(minutes=7), "7m ago"),
                                              (timedelta(hours=3, minutes=40), "3h ago"), (timedelta(hours=30), "30h ago")])
    def test_the_age_is_printed_in_its_largest_whole_unit(self, delta, label, events) -> None:
        assert f"written {label})" in read({"source": "clear"}, Store([pointer(delta)]))


class TestOneShortLine:
    def test_the_line_is_one_printable_line_within_400_bytes(self, events) -> None:
        entry = pointer(timedelta(hours=1), body="thread: a\nb\x00c | worktree: /w/" + "x" * 900 + " | state: " + "y " * 600)
        line = read({"source": "clear"}, Store([entry]))
        assert line is not None and "\n" not in line and "\x00" not in line
        assert len(line.encode("utf-8")) <= rl.MAX_BYTES == 400
        assert line.startswith("Resume pointer (identity scratch, written 1h ago): thread: a"), "the cut keeps the head"

    def test_a_cut_never_splits_a_character(self, events) -> None:
        entry = pointer(timedelta(hours=1), body="thread: " + "\U0001f600" * 300)
        line = read({"source": "clear"}, Store([entry]))
        assert len(line.encode("utf-8")) <= 400 and "�" not in line


class TestMeasure:
    def test_a_hit_logs_one_line_with_the_source_the_outcome_and_the_age(self, events) -> None:
        read({"source": "clear"}, Store([NEW]))
        assert events == [("info", "resume_line", {"source": "clear", "outcome": "hit", "age_s": 3 * 3600})]

    def test_no_pointer_logs_empty(self, events) -> None:
        read({"source": "clear"}, Store([WORKING]))
        assert events == [("info", "resume_line", {"source": "clear", "outcome": "empty", "age_s": None})]

    def test_a_stale_pointer_logs_empty_with_its_age(self, events) -> None:
        read({"source": "clear"}, Store([pointer(timedelta(hours=60))]))
        assert events[0][2]["outcome"] == "empty" and events[0][2]["age_s"] == 60 * 3600

    def test_a_store_that_errors_logs_error_and_its_cause(self, events) -> None:
        assert read({"source": "clear"}, Store([NEW], boom=RuntimeError("401 unauthorized"))) is None
        [(level, name, fields)] = events
        assert name == "resume_line" and fields["outcome"] == "error" and "401 unauthorized" in fields["error"]

    def test_one_event_per_clear_start_never_two(self, events) -> None:
        read({"source": "clear"}, Store([NEW]))
        read({"source": "clear"}, Store([], boom=OSError("down")))
        assert [e[2]["outcome"] for e in events] == ["hit", "error"]


class TestFailsSilent:
    @pytest.mark.parametrize("boom", [RuntimeError("401 unauthorized"), OSError("engine down"), KeyError("x")])
    def test_a_store_that_errors_prints_nothing(self, boom, events) -> None:
        assert read({"source": "clear"}, Store([NEW], boom=boom)) is None

    def test_a_store_that_cannot_be_opened_prints_nothing(self, events) -> None:
        def no_identity():
            raise RuntimeError("no identity is bound to this process")

        assert rl.resume_line({"source": "clear"}, open_store=no_identity, now=NOW) is None
        assert events[0][2]["outcome"] == "error"

    def test_a_read_past_the_deadline_is_abandoned_logged_as_a_timeout_and_the_guidance_is_returned_as_it_is(
            self, monkeypatch, events) -> None:
        release = threading.Event()

        class Stuck:
            def list_entries(self):
                release.wait(10)
                return [NEW]

        monkeypatch.setattr(rl, "DEADLINE_SECONDS", 0.2)
        started = time.monotonic()
        try:
            assert rl.with_resume_line("GUIDANCE", {"source": "clear"}, open_store=lambda: Stuck()) == "GUIDANCE"
            assert time.monotonic() - started < 3.0
            assert [e[2]["outcome"] for e in events] == ["timeout"]
        finally:
            release.set()
        time.sleep(0.05)
        assert [e[2]["outcome"] for e in events] == ["timeout"], "the abandoned thread logs nothing when it finishes"


class TestRealOpener:
    """The default opener, ``identity_scratch()`` for the new session, is what runs at SessionStart. These tests drive it
    (``tests/conftest.py`` stubs it for every other module); the engine-backed read is the identity journey row."""

    def test_the_default_opener_calls_identity_scratch_and_reads_its_rows(self, monkeypatch, events) -> None:
        calls: list[str] = []
        store = Store([NEW])

        def fake_identity_scratch(*_a, **_k):
            calls.append("identity_scratch")
            return store

        monkeypatch.setattr("nexus.identity.scratch_scope.identity_scratch", fake_identity_scratch)
        line = rl.resume_line({"source": "clear"}, now=NOW)
        assert calls == ["identity_scratch"], "the real opener asked for the identity scope"
        assert line is not None and "thread: new" in line, "and its list_entries rows were read"

    def test_the_default_opener_with_no_identity_prints_nothing_and_logs_an_error(self, monkeypatch, events) -> None:
        calls: list[str] = []

        def refuse(*_a, **_k):
            calls.append("identity_scratch")
            raise IdentityScopeError("no identity is bound to this process")

        monkeypatch.setattr("nexus.identity.scratch_scope.identity_scratch", refuse)
        assert rl.resume_line({"source": "clear"}, now=NOW) is None
        assert calls == ["identity_scratch"], "the refusal came from the real seam, not a stubbed opener"
        assert events[0][2]["outcome"] == "error" and "no identity" in events[0][2]["error"]


    def test_no_t1_lease_for_the_new_session_still_prints_the_pointer_and_mints_nothing(self, monkeypatch, events) -> None:
        """After ``/clear`` the new session id has no T1 lease yet, and never will in the 3 s the reader has. The identity
        scope needs no session (nexus-hczdo), so the reader asks ``identity_scratch()`` for it with no base and no session
        lookup, prints the pointer, and creates no session (marginalia's ruling on the round-1 deviation)."""
        minted: list[str] = []
        calls: list[object] = []
        store = Store([NEW])

        def opener(base=None, **_k):
            calls.append(base)
            return store

        def no_lease(*_a, **_k):
            raise AssertionError("the reader asked for the session's own T1 store, which has no lease yet")

        monkeypatch.setattr("nexus.identity.scratch_scope.identity_scratch", opener)
        monkeypatch.setattr("nexus.db.t1.get_t1_database", no_lease)
        monkeypatch.setattr("nexus.db.t1.mint_t1_session_token", lambda sid, *, context: minted.append(sid) or {"session_token": "t"})
        line = rl.resume_line({"source": "clear"}, now=NOW)
        assert line is not None and "thread: new" in line
        assert minted == [], "the reader must never mint a T1 session token"
        assert calls == [None], "identity_scratch is asked once, with no session base"
        assert events[0][2]["outcome"] == "hit"

    def test_no_identity_is_not_worked_around_by_minting(self, monkeypatch, events) -> None:
        minted: list[str] = []
        monkeypatch.setattr("nexus.db.t1.mint_t1_session_token", lambda sid, *, context: minted.append(sid) or {"session_token": "t"})

        def refuse(*_a, **_k):
            raise IdentityScopeError("no identity is bound to this process")

        monkeypatch.setattr("nexus.identity.scratch_scope.identity_scratch", refuse)
        assert rl.resume_line({"source": "clear"}, now=NOW) is None and minted == []


class _FixedClock(datetime):
    """``datetime`` whose ``now`` is NOW: the verb passes no ``now``, so its age check reads ``datetime.now`` through the
    module, and pinning it here keeps the pointer's stamp and the check on one fixed clock (the suite's rule; the wall
    clock made these tests go red once the calendar passed NOW, 2026-11-22)."""

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003 — the signature datetime.now has
        return NOW


@pytest.fixture
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rl, "datetime", _FixedClock)


@pytest.mark.usefixtures("fixed_clock")
class TestWiring:
    def _verb(self, source, store):
        with patch("nexus.hooks.session_start", return_value="GUIDANCE"), \
                patch.object(rl, "_open_store", lambda: store), \
                patch("nexus.hooks.memory_curation_check.with_curation_line", side_effect=lambda o, p: o):
            return session_start_verb.run({"session_id": "s", "source": source}).stdout

    def test_the_session_start_verb_appends_one_line_on_clear(self, events) -> None:
        out = self._verb("clear", Store([NEW]))
        assert out.startswith("GUIDANCE\n\nResume pointer (identity scratch, written ")
        assert out.count("\n") == 2, "one blank line and one resume line"

    @pytest.mark.parametrize("source", ["startup", "resume", "compact", "fork"])
    def test_the_verb_adds_nothing_on_any_other_source(self, source, events) -> None:
        assert self._verb(source, Store([NEW])) == "GUIDANCE"

    def test_a_resume_reader_that_blows_up_cannot_cost_the_guidance(self) -> None:
        with patch("nexus.hooks.session_start", return_value="GUIDANCE"), \
                patch("nexus.hooks.resume_line.with_resume_line", side_effect=RuntimeError("x")), \
                patch("nexus.hooks.memory_curation_check.with_curation_line", side_effect=lambda o, p: o):
            assert session_start_verb.run({"session_id": "s", "source": "clear"}).stdout == "GUIDANCE"

    def test_the_cli_entry_prints_the_same_line(self, events) -> None:
        with patch("nexus.hooks.session_start", return_value="GUIDANCE"), \
                patch.object(rl, "_open_store", lambda: Store([NEW])), \
                patch("nexus.hooks.memory_curation_check.with_curation_line", side_effect=lambda o, p: o):
            result = CliRunner().invoke(hook_cmd.hook_group, ["session-start"], input='{"session_id":"s","source":"clear"}')
        assert result.exit_code == 0
        assert "GUIDANCE\n\nResume pointer (identity scratch, written " in result.output


def test_the_tag_constant_is_the_writers() -> None:
    assert rl.TAG == "resume-pointer"


def test_the_t1_block_says_where_notes_you_want_after_clear_go(monkeypatch) -> None:
    monkeypatch.setattr(session_context, "which", lambda _c: False)
    monkeypatch.setattr(session_context, "run_command", lambda *_a, **_k: None)
    out = session_context.run(None).stdout or ""
    assert "Working notes you want after `/clear` go to `scratch` with `scope=identity` and tag `working`" in out
    assert "session scope stays the layer your subagents share" in out
