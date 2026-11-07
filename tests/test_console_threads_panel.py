# SPDX-License-Identifier: AGPL-3.0-or-later
"""Route tests for the console Threads panel (nexus-i99vk).

The two readers are seams on the app, so every case injects a stand-in and
nothing patches ``subprocess``. The real T2 reader is covered by the tenant
test in ``tests/test_console_app.py``, so this module provisions no tenant.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nexus.console.app import create_app
from nexus.console.routes import threads as threads_route
from nexus.console.threads import Bead, BeadsResult, answer_title, parse_session

NO_BEADS = lambda: BeadsResult(())  # noqa: E731 -- a seam stand-in


def _client(**kw) -> TestClient:
    kw.setdefault("answers_reader", lambda: [])  # never reach the real T2 'asks' project
    return TestClient(create_app(**kw))


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%MZ")


# T2 itself is exercised once, in tests/test_console_app.py's tenant test,
# so this module provisions no tenant (the CI provisioned-tenant ceiling).


def test_empty_threads_project_says_so() -> None:
    resp = _client(bd_runner=NO_BEADS, threads_reader=lambda: []).get("/threads")
    assert resp.status_code == 200
    assert "No Claude session has reported in yet." in resp.text
    assert "No session has reported yet, so I can't tell what needs you." in resp.text


# ── seams ────────────────────────────────────────────────────────────────────


def test_a_bead_source_error_renders_one_line_notice_not_a_traceback() -> None:
    def runner() -> BeadsResult:
        return BeadsResult((), "bd is not on PATH")

    resp = _client(bd_runner=runner, threads_reader=lambda: []).get("/threads")
    assert resp.status_code == 200
    assert "Could not read the list of beads waiting on you: bd is not on PATH." in resp.text
    assert "Can't tell what needs you right now." in resp.text
    assert "Traceback" not in resp.text


def test_a_user_without_bd_sees_no_error_and_the_board_still_answers() -> None:
    now = datetime.now(timezone.utc)
    rows = [{"title": "s", "timestamp": _iso(now), "content": "## A\nstate: live\n"}]
    skipped = lambda: BeadsResult((), skipped="bd is not installed")  # noqa: E731 -- a seam stand-in
    html = _client(bd_runner=skipped, threads_reader=lambda: rows).get("/threads").text
    assert "Could not read the list of beads" not in html
    assert "Can't tell what needs you right now." not in html
    assert "Nothing needs you. 1 session reported, newest just now." in html
    assert "Beads are not shown: bd is not installed." in html


def test_beads_listed_with_id_title_priority_assignee() -> None:
    beads = BeadsResult((
        Bead("nexus-abc", "Decide the thing", 1, "sam"),
        Bead("nexus-def", "Unowned one", None, ""),
    ))
    resp = _client(bd_runner=lambda: beads, threads_reader=lambda: []).get("/threads")
    html = resp.text
    assert "nexus-abc" in html and "Decide the thing" in html and "P1" in html and "sam" in html
    assert "nexus-def" in html
    assert "assigned to sam" in html and "not assigned" in html
    # The plain title leads; the bead id trails in small grey text.
    assert html.index("Decide the thing") < html.index("nexus-abc")
    assert "Nothing needs you" not in html
    assert "2 things need you." in html


def test_nothing_waiting_message_names_the_sessions_and_the_newest_age() -> None:
    now = datetime.now(timezone.utc)
    rows = [{"title": "s", "timestamp": _iso(now), "content": "## A\nstate: live\n"}]
    resp = _client(bd_runner=NO_BEADS, threads_reader=lambda: rows).get("/threads")
    assert "Nothing needs you. 1 session reported, newest just now." in resp.text


def test_no_sessions_and_no_beads_cannot_tell_rather_than_saying_nothing() -> None:
    resp = _client(bd_runner=NO_BEADS, threads_reader=lambda: []).get("/threads")
    assert "No session has reported yet, so I can't tell what needs you." in resp.text
    assert "Nothing needs you" not in resp.text


def test_t2_failure_is_one_line_naming_it() -> None:
    def broken() -> list[dict]:
        raise RuntimeError("T2 storage service unavailable: no lease")

    resp = _client(bd_runner=NO_BEADS, threads_reader=broken).get("/threads")
    assert resp.status_code == 200
    assert "could not be read: T2 storage service unavailable: no lease" in resp.text
    assert "Can't tell what needs you right now." in resp.text
    assert "Traceback" not in resp.text
    # A T2 outage is not "nothing is waiting on you".
    assert "Nothing needs you" not in resp.text


def test_stale_sessions_collapse_under_one_line_at_the_bottom() -> None:
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=50)
    rows = [
        {"title": "old_one", "content": f"updated: {_iso(old)}\n## B\nstate: parked\nneeds_sam: ship it\n",
         "timestamp": _iso(old)},
        {"title": "old_two", "content": f"updated: {_iso(old)}\n## C\nstate: parked\n",
         "timestamp": _iso(old)},
        {"title": "fresh_one", "content": f"updated: {_iso(now)}\n## A\nstate: live\n",
         "timestamp": _iso(now)},
    ]
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: rows).get("/threads").text
    sessions = html.split("<h3>Sessions</h3>", 1)[1]
    active, quiet = sessions.split("<details", 1)
    assert "fresh_one" in active and "updated just now" in active
    assert "old_one" not in active and "old_two" not in active
    assert "2 quiet sessions (no update in over a day)" in quiet
    assert "old_one" in quiet and "updated 2 days ago" in quiet
    assert "thread-card stale" in quiet
    # A quiet session's open ask still reaches the top, with who asked and how long ago.
    needs_you = html.split("<h3>Needs you</h3>", 1)[1].split("<h3>Sessions</h3>", 1)[0]
    assert "ship it" in needs_you and "asked by old_one" in needs_you and "2 days ago" in needs_you


def test_no_quiet_section_when_every_session_is_active() -> None:
    now = datetime.now(timezone.utc)
    rows = [{"title": "s", "timestamp": _iso(now), "content": "## A\nstate: live\n"}]
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: rows).get("/threads").text
    assert "quiet session" not in html


def test_thread_line_reads_as_work_then_state_and_hides_empty_waiting_on() -> None:
    now = datetime.now(timezone.utc)
    rows = [{"title": "s", "timestamp": _iso(now), "content": (
        "## Port the parser\nref: nexus-1\nstate: tests pass\nwaiting_on: review\n"
        "## Idle thing\nstate: parked\nwaiting_on: none\nneeds_sam: none\n")}]
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: rows).get("/threads").text
    assert "Port the parser: tests pass" in html
    assert "waiting on review" in html
    assert "Idle thing: parked" in html
    assert html.count("waiting on") == 1  # "none" is not shown
    assert html.index("Port the parser: tests pass") < html.index("nexus-1")


@pytest.mark.parametrize("raw", ["needs_sam", "waiting_on", "ref:", "Threads registry"])
def test_no_raw_field_names_on_the_page(raw: str) -> None:
    now = datetime.now(timezone.utc)
    rows = [{"title": "s", "timestamp": _iso(now), "content": (
        "## A\nref: nexus-1\nstate: x\nwaiting_on: review\nneeds_sam: decide\n")}]
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: rows).get("/threads").text
    assert raw not in html


def test_needs_sam_thread_row_is_highlighted_and_none_row_is_not() -> None:
    now = datetime.now(timezone.utc)
    rows = [{"title": "s", "timestamp": _iso(now), "content": (
        "## Asks\nneeds_sam: approve\n## Calm\nneeds_sam: none\n")}]
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: rows).get("/threads").text
    sessions = html.split("<h3>Sessions</h3>", 1)[1]
    assert sessions.count('class="needs-sam"') == 1


def test_refresh_partial_has_no_page_chrome() -> None:
    resp = _client(bd_runner=NO_BEADS, threads_reader=lambda: []).get("/threads/refresh")
    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "No session has reported yet, so I can't tell what needs you." in resp.text


def test_threads_page_is_in_the_nav_and_activity_campaigns_are_not() -> None:
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: []).get("/threads").text
    assert 'href="/threads"' in html and 'href="/health"' in html
    assert "/activity" not in html and "/campaigns" not in html


# ── the real bd runner, against a fake `bd` on PATH ──────────────────────────


def _fake_bd(tmp_path, monkeypatch, script: str) -> None:
    bd = tmp_path / "bd"
    bd.write_text("#!/bin/sh\n" + script)
    bd.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))


def test_run_bd_skips_when_bd_is_missing_and_there_is_no_beads_database(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # empty dir: no bd
    monkeypatch.delenv("BEADS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)  # no .beads at or above (tmp_path is outside the repo)
    result = threads_route.run_bd_needs_sam()
    # No bd and no beads database: a user without beads has no bead list to read
    # (user-surface inventory 2026-11-04, new row 9). Skipped, said once.
    assert result.ok and result.beads == () and "bd is not installed" in result.skipped


def test_run_bd_missing_with_a_beads_dir_above_cwd_is_an_error(tmp_path, monkeypatch) -> None:
    (tmp_path / ".beads").mkdir()
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("BEADS_DIR", raising=False)
    monkeypatch.chdir(deep)
    result = threads_route.run_bd_needs_sam()
    assert not result.ok and result.error == "bd is not on PATH" and result.skipped == ""


def test_run_bd_missing_with_beads_dir_env_set_is_an_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("BEADS_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.chdir(tmp_path)
    assert threads_route.run_bd_needs_sam().error == "bd is not on PATH"


def test_run_bd_skips_on_its_no_beads_database_error(tmp_path, monkeypatch) -> None:
    _fake_bd(tmp_path, monkeypatch, 'echo "Error: no beads database found" >&2\nexit 1\n')
    result = threads_route.run_bd_needs_sam()
    assert result.ok and result.beads == () and "no beads database" in result.skipped


def test_run_bd_broken_bd_is_an_error_not_a_skip(tmp_path, monkeypatch) -> None:
    """A bd whose interpreter is gone raises FileNotFoundError at exec, like a missing bd, and must read as an error
    when the repo has a beads database."""
    (tmp_path / ".beads").mkdir()
    bd = tmp_path / "bin" / "bd"
    bd.parent.mkdir()
    bd.write_text("#!/nonexistent/interpreter\n")
    bd.chmod(0o755)
    monkeypatch.setenv("PATH", str(bd.parent))
    monkeypatch.delenv("BEADS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    result = threads_route.run_bd_needs_sam()
    assert not result.ok and result.skipped == ""


def test_run_bd_parses_output_and_passes_the_label_filter(tmp_path, monkeypatch) -> None:
    argv_file = tmp_path / "argv"
    _fake_bd(tmp_path, monkeypatch,
             f'echo "$@" > {argv_file}\n'
             'echo \'[{"id":"nexus-1","title":"T","priority":0,"assignee":"sam"}]\'\n')
    result = threads_route.run_bd_needs_sam()
    assert result.ok and [b.id for b in result.beads] == ["nexus-1"]
    argv = argv_file.read_text()
    # Live work needs Sam too: in_progress and blocked beads are not dropped.
    assert argv.split() == [
        "list", "--label", "needs-sam", "--status", "open,in_progress,blocked", "--limit", "0", "--json",
    ]


def test_run_bd_nonzero_exit_is_one_line(tmp_path, monkeypatch) -> None:
    _fake_bd(tmp_path, monkeypatch, 'echo "database is locked" >&2\nexit 3\n')
    result = threads_route.run_bd_needs_sam()
    assert result.error == "bd list failed (exit 3): database is locked"


def test_run_bd_garbage_output_is_one_line(tmp_path, monkeypatch) -> None:
    _fake_bd(tmp_path, monkeypatch, 'echo "hello"\n')
    result = threads_route.run_bd_needs_sam()
    assert not result.ok and "not JSON" in result.error


def test_run_bd_timeout(tmp_path, monkeypatch) -> None:
    _fake_bd(tmp_path, monkeypatch, "exec sleep 5\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:/bin:/usr/bin")
    result = threads_route.run_bd_needs_sam(timeout=0.2)
    assert not result.ok and "timed out" in result.error


# ── fixes from the review of the first Threads panel (nexus-i99vk) ───────────

CLOCK = lambda: datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc)  # noqa: E731 -- a seam stand-in


def test_board_polls_the_partial_every_60_seconds() -> None:
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: []).get("/threads").text
    container = html.split('id="threads-board"', 1)[1].split(">", 1)[0]
    assert 'hx-trigger="every 60s"' in container
    assert 'hx-get="/threads/refresh"' in container


def test_page_and_partial_print_when_they_were_read() -> None:
    expected = "As of " + CLOCK().astimezone().strftime("%H:%M")
    client = _client(bd_runner=NO_BEADS, threads_reader=lambda: [], clock=CLOCK)
    page = client.get("/threads").text
    partial = client.get("/threads/refresh").text
    assert expected in page and expected in partial
    # At the top of the board, ahead of the headline.
    assert partial.index(expected) < partial.index("thread-headline")


def test_a_ask_raised_by_both_a_thread_and_a_listed_bead_is_one_row_and_one_count() -> None:
    now = datetime.now(timezone.utc)
    rows = [{"title": "nexus_c9c", "timestamp": _iso(now), "content": (
        "## Port\nref: nexus-abc\nneeds_sam: decide the port\n")}]
    beads = BeadsResult((Bead("nexus-abc", "Decide the thing", 1, "sam"),))
    html = _client(bd_runner=lambda: beads, threads_reader=lambda: rows).get("/threads").text
    assert "1 thing needs you." in html
    needs_you = html.split("<h3>Needs you</h3>", 1)[1].split("<h3>Sessions</h3>", 1)[0]
    assert needs_you.count('class="needs-sam"') == 1
    assert "Decide the thing" in needs_you and "raised by nexus_c9c" in needs_you


def test_counts_read_at_least_when_beads_could_not_be_read() -> None:
    now = datetime.now(timezone.utc)
    rows = [{"title": "s", "timestamp": _iso(now), "content": "## A\nneeds_sam: approve\n"}]

    def runner() -> BeadsResult:
        return BeadsResult((), "bd is not on PATH")

    html = _client(bd_runner=runner, threads_reader=lambda: rows).get("/threads").text
    assert "At least 1 thing needs you." in html


def test_counts_read_at_least_when_t2_could_not_be_read() -> None:
    def broken() -> list[dict]:
        raise RuntimeError("no lease")

    beads = BeadsResult((Bead("nexus-1", "A"), Bead("nexus-2", "B")))
    html = _client(bd_runner=lambda: beads, threads_reader=broken).get("/threads").text
    assert "At least 2 things need you." in html


def test_a_future_updated_line_does_not_make_an_old_row_fresh() -> None:
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=72)
    future = now + timedelta(days=30)
    rows = [{"title": "liar", "timestamp": _iso(old),
             "content": f"session: liar\nupdated: {_iso(future)}\n## A\nstate: parked\n"}]
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: rows).get("/threads").text
    assert "thread-card stale" in html and "updated 3 days ago" in html
    assert "updated: " not in html and "session: " not in html  # header lines are not shown as notes


def test_a_row_with_an_out_of_range_timestamp_does_not_500_the_page() -> None:
    now = datetime.now(timezone.utc)
    rows = [
        {"title": "bad_stamp", "timestamp": "0001-01-01T00:00:00+05:00", "content": "## A\nneeds_sam: help\n"},
        {"title": "far_future", "timestamp": "9999-12-31T23:59:59-05:00", "content": "## B\nstate: x\n"},
        {"title": "good_one", "timestamp": _iso(now), "content": "## G\nstate: all well\n"},
    ]
    resp = _client(bd_runner=NO_BEADS, threads_reader=lambda: rows).get("/threads")
    assert resp.status_code == 200
    assert "good_one" in resp.text and "all well" in resp.text
    assert "bad_stamp" in resp.text and "help" in resp.text  # its ask still reaches the top


class _UnreadableRow(dict):
    def get(self, key, default=None):  # noqa: ANN001
        if key == "content":
            raise RuntimeError("row is corrupt")
        return super().get(key, default)


def test_one_unreadable_entry_becomes_one_line_and_the_rest_still_render() -> None:
    now = datetime.now(timezone.utc)
    rows = [
        _UnreadableRow(title="broken_one", timestamp=_iso(now)),
        {"title": "good_one", "timestamp": _iso(now), "content": "## G\nstate: all well\n"},
    ]
    resp = _client(bd_runner=NO_BEADS, threads_reader=lambda: rows).get("/threads")
    assert resp.status_code == 200
    assert "Could not read broken_one." in resp.text
    assert "good_one" in resp.text and "all well" in resp.text
    assert "Nothing needs you" not in resp.text  # an unread entry might hold an ask


def test_hostile_entry_text_renders_escaped() -> None:
    now = datetime.now(timezone.utc)
    evil = "<script>alert(1)</script>"
    rows = [{"title": "s", "timestamp": _iso(now), "content": f"## {evil}\nstate: {evil}\nneeds_sam: {evil}\n"}]
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: rows).get("/threads").text
    assert evil not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_implicit_thread_and_key_spelling_reach_the_page() -> None:
    now = datetime.now(timezone.utc)
    rows = [{"title": "terse", "timestamp": _iso(now), "content": "Needs-Sam: pick one\n"}]
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: rows).get("/threads").text
    assert "1 thing needs you." in html and "pick one" in html and "asked by terse" in html


# ── structured asks (RDR-245) ────────────────────────────────────────────────

_ASK_FIXTURE = Path(__file__).parent / "fixtures" / "asks" / "halcyon.txt"
_QUESTION = "Retry-safe keying for quarantine restore: accept the gap, or build option B?"


def _halcyon_rows(content: str | None = None) -> list[dict[str, str]]:
    text = _ASK_FIXTURE.read_text(encoding="utf-8") if content is None else content
    return [{"title": "halcyon", "timestamp": _iso(datetime.now(timezone.utc)), "content": text}]


def test_an_unfolded_structured_ask_is_counted_and_shown() -> None:
    html = _client(bd_runner=NO_BEADS, threads_reader=_halcyon_rows).get("/threads").text
    assert "1 thing needs you." in html
    assert "Nothing needs you" not in html
    assert _QUESTION in html
    assert "asked by halcyon" in html


def test_a_structured_ask_folded_into_a_bead_is_one_item_and_shows_its_question() -> None:
    beads = BeadsResult((Bead("nexus-k9kr9", "Keying decision", 2),))
    html = _client(bd_runner=lambda: beads, threads_reader=_halcyon_rows).get("/threads").text
    assert "1 thing needs you." in html
    assert "Keying decision" in html
    assert f"raised by halcyon on &ldquo;Quarantine restore keying (RDR-222)&rdquo;: {_QUESTION}" in html
    assert html.count(_QUESTION) == 1  # not also listed as its own item


def test_a_conditional_structured_ask_is_not_counted() -> None:
    content = _ASK_FIXTURE.read_text(encoding="utf-8") + "after: engine 0.1.180 deploys\n"
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: _halcyon_rows(content)).get("/threads").text
    assert "Nothing needs you." in html
    assert _QUESTION not in html


def test_structured_ask_text_renders_escaped() -> None:
    content = _ASK_FIXTURE.read_text(encoding="utf-8").replace(
        "Retry-safe keying", "<script>alert(1)</script> keying"
    )
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: _halcyon_rows(content)).get("/threads").text
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)" in html


def test_an_entry_with_no_ask_block_renders_as_before() -> None:
    rows = [{"title": "s", "timestamp": _iso(datetime.now(timezone.utc)),
             "content": "## A\nstate: live\nneeds_sam: pick the port\n"}]
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: rows).get("/threads").text
    assert "1 thing needs you." in html and "pick the port" in html


# ── nexus-wc4jj.13 follow-ups ────────────────────────────────────────────────


def test_an_ask_with_an_empty_question_shows_a_placeholder_not_a_blank_strong() -> None:
    content = _ASK_FIXTURE.read_text(encoding="utf-8").replace(_QUESTION, "")
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: _halcyon_rows(content)).get("/threads").text
    assert "1 thing needs you." in html
    assert "<strong></strong>" not in html
    assert "<strong>(no question given)</strong>" in html


def test_a_bead_folded_ask_with_an_empty_question_shows_the_placeholder() -> None:
    content = _ASK_FIXTURE.read_text(encoding="utf-8").replace(_QUESTION, "")
    beads = BeadsResult((Bead("nexus-k9kr9", "Keying decision", 2),))
    html = _client(bd_runner=lambda: beads, threads_reader=lambda: _halcyon_rows(content)).get("/threads").text
    assert "(no question given)" in html


def _sent_record_for_halcyon() -> dict[str, str]:
    ask = parse_session("halcyon", _ASK_FIXTURE.read_text(encoding="utf-8")).threads[0].asks[0]
    return {"title": answer_title(ask), "content": json.dumps({"status": "sent"})}


def test_a_sent_answer_record_takes_the_ask_off_the_board() -> None:
    html = _client(
        bd_runner=NO_BEADS, threads_reader=_halcyon_rows, answers_reader=lambda: [_sent_record_for_halcyon()]
    ).get("/threads").text
    assert "Nothing needs you." in html
    assert "1 thing needs you." not in html


def test_an_ask_with_no_answer_record_is_still_counted() -> None:
    html = _client(bd_runner=NO_BEADS, threads_reader=_halcyon_rows, answers_reader=lambda: []).get("/threads").text
    assert "1 thing needs you." in html


def test_an_unreadable_answers_project_leaves_the_ask_counted() -> None:
    def broken():
        raise RuntimeError("t2 down")

    html = _client(bd_runner=NO_BEADS, threads_reader=_halcyon_rows, answers_reader=broken).get("/threads").text
    assert "1 thing needs you." in html


def test_the_answers_are_not_read_when_no_entry_holds_a_structured_ask() -> None:
    calls: list[int] = []  # not an assert inside the reader: the route swallows a reader's exception

    def reader():
        calls.append(1)
        return []

    rows = [{"title": "s", "timestamp": _iso(datetime.now(timezone.utc)), "content": "## A\nneeds_sam: pick\n"}]
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: rows, answers_reader=reader).get("/threads").text
    assert "1 thing needs you." in html
    assert calls == []
    _client(bd_runner=NO_BEADS, threads_reader=_halcyon_rows, answers_reader=reader).get("/threads")
    assert calls == [1]  # the reader is reached when a structured ask is on the board


def test_an_ask_block_with_a_bad_id_and_no_other_ask_is_counted_on_the_board() -> None:
    content = (
        _ASK_FIXTURE.read_text(encoding="utf-8")
        .replace("needs_sam: ask d1105-k9kr9-keying", "needs_sam: none")
        .replace("ask: d1105-k9kr9-keying", "ask: Bad_Id")
    )
    assert "needs_sam: none" in content and "ask: Bad_Id" in content  # the replacements applied
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: _halcyon_rows(content)).get("/threads").text
    assert "1 thing needs you." in html
    assert "cannot be answered" in html and "Bad_Id" in html


def test_ask_fields_after_a_thread_field_are_counted_on_the_board_not_lost() -> None:
    content = "## T\nask: pick-port\nstate: x\nquestion: Q?\ncontext: c\n"
    rows = [{"title": "s", "timestamp": _iso(datetime.now(timezone.utc)), "content": content}]
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: rows).get("/threads").text
    assert "1 thing needs you." in html
    assert "cannot be answered" in html and "thread field" in html


def test_an_answered_ask_is_noted_as_awaiting_pickup_and_not_counted() -> None:
    html = _client(
        bd_runner=NO_BEADS, threads_reader=_halcyon_rows, answers_reader=lambda: [_sent_record_for_halcyon()]
    ).get("/threads").text
    assert "1 answered, awaiting pickup." in html
    assert "Nothing needs you." in html


def test_no_pickup_note_when_nothing_is_answered() -> None:
    html = _client(bd_runner=NO_BEADS, threads_reader=_halcyon_rows).get("/threads").text
    assert "awaiting pickup" not in html


def test_a_legacy_ask_sam_line_is_counted_on_the_board() -> None:
    rows = [{"title": "s", "timestamp": _iso(datetime.now(timezone.utc)), "content": "## A\nask: sam\n"}]
    html = _client(bd_runner=NO_BEADS, threads_reader=lambda: rows).get("/threads").text
    assert "1 thing needs you." in html
