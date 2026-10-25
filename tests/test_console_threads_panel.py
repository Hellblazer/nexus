# SPDX-License-Identifier: AGPL-3.0-or-later
"""Route tests for the console Threads panel (nexus-i99vk).

The T2 path is real (a minted tenant, entries written through the same
``t2_handle`` the CLI uses). The two readers are seams on the app, so the
``bd`` and T2-failure cases inject a stand-in; nothing patches ``subprocess``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from nexus.console.app import create_app
from nexus.console.routes import threads as threads_route
from nexus.console.threads import Bead, BeadsResult

NO_BEADS = lambda: BeadsResult(())  # noqa: E731 -- a seam stand-in


def _client(**kw) -> TestClient:
    return TestClient(create_app(**kw))


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%MZ")


# ── real T2 ──────────────────────────────────────────────────────────────────


@pytest.mark.t2_tenant
def test_renders_real_t2_entries_in_project_threads() -> None:
    from nexus.commands._helpers import t2_handle

    now = datetime.now(timezone.utc)
    body = (
        f"session: nexus_c9c\nupdated: {_iso(now)}\n"
        "## RDR-234 auto-continuation\nref: RDR-234, nexus-qlrj7\n"
        "state: spike proven; rewriting the draft\nwaiting_on: none\nneeds_sam: none\n"
        "## Console panel\nref: nexus-i99vk\nstate: building\nwaiting_on: review\n"
        "needs_sam: choose the port\n"
    )
    other = f"session: other\nupdated: {_iso(now)}\n## Quiet thread\nstate: idle\nneeds_sam: none\n"
    with t2_handle() as db:
        db.memory.put(project="threads", title="nexus_c9c", content=body)
        db.memory.put(project="threads", title="other", content=other)
        # A different project must not leak onto the board.
        db.memory.put(project="not_threads", title="intruder", content="## Nope\nstate: x\n")

    resp = _client(bd_runner=NO_BEADS).get("/threads")

    assert resp.status_code == 200
    html = resp.text
    for expected in (
        "nexus_c9c", "other", "RDR-234 auto-continuation", "spike proven; rewriting the draft",
        "Console panel", "building", "review", "Quiet thread",
    ):
        assert expected in html, expected
    assert "intruder" not in html
    assert "1 thing needs you." in html
    needs_you = html.split("<h3>Needs you</h3>", 1)[1].split("<h3>Sessions</h3>", 1)[0]
    assert "choose the port" in needs_you
    assert "asked by nexus_c9c" in needs_you
    assert "Quiet thread" not in needs_you
    assert "thread-card stale" not in html  # both rows are fresh


@pytest.mark.t2_tenant
def test_empty_threads_project_says_so() -> None:
    resp = _client(bd_runner=NO_BEADS).get("/threads")
    assert resp.status_code == 200
    assert "No Claude session has reported in yet." in resp.text
    assert "Nothing needs you right now." in resp.text


# ── seams ────────────────────────────────────────────────────────────────────


def test_bd_absent_renders_one_line_notice_not_a_traceback() -> None:
    def runner() -> BeadsResult:
        return BeadsResult((), "bd is not on PATH")

    resp = _client(bd_runner=runner, threads_reader=lambda: []).get("/threads")
    assert resp.status_code == 200
    assert "Could not read the list of beads waiting on you: bd is not on PATH." in resp.text
    assert "Can't tell what needs you right now." in resp.text
    assert "Traceback" not in resp.text


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


def test_nothing_waiting_message() -> None:
    resp = _client(bd_runner=NO_BEADS, threads_reader=lambda: []).get("/threads")
    assert "Nothing needs you right now." in resp.text


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
    assert "Nothing needs you right now." in resp.text


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


def test_run_bd_reports_missing_binary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # empty dir: no bd
    result = threads_route.run_bd_needs_sam()
    assert not result.ok and "not on PATH" in result.error


def test_run_bd_parses_output_and_passes_the_label_filter(tmp_path, monkeypatch) -> None:
    argv_file = tmp_path / "argv"
    _fake_bd(tmp_path, monkeypatch,
             f'echo "$@" > {argv_file}\n'
             'echo \'[{"id":"nexus-1","title":"T","priority":0,"assignee":"sam"}]\'\n')
    result = threads_route.run_bd_needs_sam()
    assert result.ok and [b.id for b in result.beads] == ["nexus-1"]
    argv = argv_file.read_text()
    assert "--label needs-sam" in argv and "--status open" in argv and "--json" in argv


def test_run_bd_nonzero_exit_is_one_line(tmp_path, monkeypatch) -> None:
    _fake_bd(tmp_path, monkeypatch, 'echo "no beads database found" >&2\nexit 3\n')
    result = threads_route.run_bd_needs_sam()
    assert result.error == "bd list failed (exit 3): no beads database found"


def test_run_bd_garbage_output_is_one_line(tmp_path, monkeypatch) -> None:
    _fake_bd(tmp_path, monkeypatch, 'echo "hello"\n')
    result = threads_route.run_bd_needs_sam()
    assert not result.ok and "not JSON" in result.error


def test_run_bd_timeout(tmp_path, monkeypatch) -> None:
    _fake_bd(tmp_path, monkeypatch, "exec sleep 5\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:/bin:/usr/bin")
    result = threads_route.run_bd_needs_sam(timeout=0.2)
    assert not result.ok and "timed out" in result.error
