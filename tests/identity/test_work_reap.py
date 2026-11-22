# SPDX-License-Identifier: AGPL-3.0-or-later
"""The work-row lifecycle's backstop (nexus-hczdo, Sam 2026-11-22 "i really want all this nailed"): a running row whose work
has ended closes itself at read time, so no session marks rows by hand. The shapes are the 2026-11-21 restart rounds: bob's
41-row list (37 finished lab runs, two lab runs whose pid is gone, two agents registered before the session started),
finished agents whose stop never wrote, dead lab pids, a removed worktree. Tenant-free (the fixtures of
tests/identity/test_work_stale_probe.py)."""
from __future__ import annotations

import json
import os

import pytest

from tests.test_identity_cmd import Box

HOUR = 3600.0


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def _bobs_list(box: Box, tmp_path) -> tuple[str, dict[str, str]]:
    """bob's identity as the 8.5.5 round read it, plus the rows that must survive a reap."""
    from nexus.identity import holder

    i1 = box.running_seat("bob", 100)                        # holder started_at = 100 * 1000 ms
    box.engine.sessions["sid-100"] = (i1, 1)
    began = holder.epoch_seconds("2026-11-17T09:00:00Z")
    box.procs.start(400, "2026-11-17T09:00:00Z", registry=False)                    # a lab run that still runs
    box.procs.start(401, "2026-11-17T09:00:00Z", registry=False)
    box.procs.start(402, "2026-11-17T09:00:00Z", registry=False)
    box.procs.stop(401)
    box.procs.stop(402)
    live_tree = tmp_path / "wt-live"
    live_tree.mkdir()
    refs = {
        "lab_live": f"{box.procs.host}:400:{began}",
        "lab_dead_1": f"{box.procs.host}:401:{began}",
        "lab_dead_2": f"{box.procs.host}:402:{began}",
        "agent_ghost_1": "agent:a59c2a8b6cf7cd835",
        "agent_ghost_2": "agent:abab3684c3791c3cb",
        "agent_live": "agent:a-now",
        "tree_live": os.path.realpath(live_tree),
        "tree_gone": os.path.realpath(tmp_path / "wt-gone"),
    }
    for n in range(37):
        box.engine.work[(i1, "labrun", f"{box.procs.host}:{1000 + n}:{began}")] = ("done", 500.0)
    for name in ("lab_live", "lab_dead_1", "lab_dead_2"):
        box.engine.work[(i1, "labrun", refs[name])] = ("running", 500.0)
    for name in ("agent_ghost_1", "agent_ghost_2"):
        box.engine.work[(i1, "agent", refs[name])] = ("running", 40.0)              # before the holder began (100 s)
    box.engine.work[(i1, "agent", refs["agent_live"])] = ("running", 500.0)
    box.engine.work[(i1, "worktree", refs["tree_live"])] = ("running", 40.0)
    box.engine.work[(i1, "worktree", refs["tree_gone"])] = ("running", 40.0)
    return i1, refs


def _listed(box: Box, *flags: str) -> dict:
    out = box.run("work", "list", "--session", "sid-100", "--json", *flags)
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)


def test_bobs_list_is_reaped_to_the_work_that_still_runs_and_the_reap_says_what_it_closed(box: Box, tmp_path) -> None:
    i1, refs = _bobs_list(box, tmp_path)
    before = _listed(box)
    assert sum(1 for w in before["work"] if w["state"] == "running" and w["stale"]) == 5
    report = _listed(box, "--reap")
    closed = {(r["kind"], r["ref"]): r["closed"] for r in report["reaped"]}
    assert closed == {
        ("labrun", refs["lab_dead_1"]): "done", ("labrun", refs["lab_dead_2"]): "done",
        ("agent", refs["agent_ghost_1"]): "forgotten", ("agent", refs["agent_ghost_2"]): "forgotten",
        ("worktree", refs["tree_gone"]): "forgotten",
    }
    assert not [w for w in report["work"] if w["state"] == "running" and w["stale"]], "nothing stale is left running"
    running = {(w["kind"], w["ref"]) for w in report["work"] if w["state"] == "running"}
    assert running == {("labrun", refs["lab_live"]), ("agent", refs["agent_live"]), ("worktree", refs["tree_live"])}
    assert box.engine.work[(i1, "labrun", refs["lab_dead_1"])][0] == "done"
    assert (i1, "agent", refs["agent_ghost_1"]) not in box.engine.work
    assert sum(1 for k, v in box.engine.work.items() if k[1] == "labrun" and v[0] == "done") == 39
    assert _listed(box, "--reap")["reaped"] == [], "a second reap finds nothing"


def test_a_reaping_list_prints_each_closed_row_and_a_plain_list_closes_nothing(box: Box, tmp_path) -> None:
    i1, refs = _bobs_list(box, tmp_path)
    plain = box.run("work", "list", "--session", "sid-100")
    assert "closed" not in plain.output and box.engine.work[(i1, "labrun", refs["lab_dead_1"])][0] == "running"
    text = box.run("work", "list", "--session", "sid-100", "--reap").output
    assert f"closed  labrun    {refs['lab_dead_1']}  (its process is gone; done)" in text
    assert f"closed  agent     {refs['agent_ghost_1']}" in text and "forgotten" in text


def test_the_reap_logs_one_count_line_so_a_missing_end_of_life_path_shows(box: Box, tmp_path, monkeypatch) -> None:
    from nexus.identity import work_reap

    i1, _ = _bobs_list(box, tmp_path)
    seen: list[tuple[str, dict]] = []

    class Log:
        def info(self, event: str, **kw) -> None:
            seen.append((event, kw))

        warning = info

    monkeypatch.setattr(work_reap, "_log", Log())
    work_reap.reap(box.env(), i1)
    counts = [kw for event, kw in seen if event == "identity_work_reaped"]
    assert counts == [{"identity": i1, "count": 5, "kinds": ["agent", "labrun", "worktree"]}]
    assert sum(1 for event, _ in seen if event == "identity_work_reaped_row") == 5


def test_an_identity_that_is_not_alive_keeps_rows_nothing_says_have_ended(box: Box, tmp_path) -> None:
    """A crashed session's lab run can still run on its host, and a seat's worktree rows are its ownership when the seat is
    bound again: "the identity is not alive" alone reads stale and never reaps."""
    from nexus.identity import work_reap

    i1 = box.running_seat("alpha", 100)
    box.engine.sessions["sid-100"] = (i1, 1)
    tree = tmp_path / "wt"
    tree.mkdir()
    box.engine.work[(i1, "worktree", os.path.realpath(tree))] = ("running", 500.0)
    box.engine.work[(i1, "labrun", "elsewhere:5:1")] = ("running", 500.0)          # another host: not judged here
    box.procs.stop(100)                                                            # the holder is gone
    assert _listed(box)["alive"] is False
    assert work_reap.reap(box.env(), i1) == []
    assert box.engine.work[(i1, "worktree", os.path.realpath(tree))][0] == "running"


def test_a_row_whose_close_fails_is_left_and_logged_and_the_others_still_close(box: Box, tmp_path) -> None:
    from nexus.identity import work_reap
    from nexus.identity.bind_client import IdentityApiError

    i1, refs = _bobs_list(box, tmp_path)
    real = box.engine.remove_work

    def flaky(identity: str, kind: str, ref: str) -> dict:
        if ref == refs["agent_ghost_1"]:
            raise IdentityApiError(503, "unavailable", "engine busy", {})
        return real(identity, kind, ref)

    box.engine.remove_work = flaky                                                 # type: ignore[method-assign]
    closed = {r["ref"] for r in work_reap.reap(box.env(), i1)}
    assert refs["agent_ghost_1"] not in closed and refs["agent_ghost_2"] in closed and refs["lab_dead_1"] in closed
    assert box.engine.work[(i1, "agent", refs["agent_ghost_1"])][0] == "running"


def test_session_start_reaps_the_sessions_own_identity(box: Box, tmp_path) -> None:
    """The SessionStart child: an agent in flight across a restart never ran its stop; the new holder's start makes its row
    a ghost, and the start's reap closes it."""
    from nexus.hooks import work_reap_child

    i1, refs = _bobs_list(box, tmp_path)
    closed = work_reap_child.reap_session("sid-100", env=box.env())
    assert {r["ref"] for r in closed} >= {refs["agent_ghost_1"], refs["lab_dead_2"]}
    assert (i1, "agent", refs["agent_ghost_1"]) not in box.engine.work


def test_the_session_start_spawn_is_off_under_the_test_switch_and_for_no_session(monkeypatch) -> None:
    from nexus.hooks import work_reap_child

    started: list[list[str]] = []
    monkeypatch.setattr(work_reap_child.subprocess, "Popen", lambda argv, **k: started.append(argv))
    assert work_reap_child.spawn("sid-1") is False, "conftest sets NX_WORK_REAP_DISABLED=1"
    monkeypatch.delenv(work_reap_child.DISABLE_ENV)
    assert work_reap_child.spawn("") is False and work_reap_child.spawn("unknown") is False
    assert work_reap_child.spawn("sid-1") is True
    assert started and started[0][-2:] == ["nexus.hooks.work_reap_child", "sid-1"]
