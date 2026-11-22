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
    }
    stale_left = [(w["kind"], w["ref"]) for w in report["work"] if w["state"] == "running" and w["stale"]]
    assert stale_left == [("worktree", refs["tree_gone"])], "a worktree row is never reaped: it may be another host's path"
    running = {(w["kind"], w["ref"]) for w in report["work"] if w["state"] == "running"}
    assert running == {("labrun", refs["lab_live"]), ("agent", refs["agent_live"]), ("worktree", refs["tree_live"]),
                       ("worktree", refs["tree_gone"])}
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
    from nexus.identity import reap as work_reap

    i1, _ = _bobs_list(box, tmp_path)
    seen: list[tuple[str, dict]] = []

    class Log:
        def info(self, event: str, **kw) -> None:
            seen.append((event, kw))

        warning = info

    monkeypatch.setattr(work_reap, "_log", Log())
    work_reap.reap(box.env(), i1)
    counts = [kw for event, kw in seen if event == "identity_work_reaped"]
    assert counts == [{"identity": i1, "count": 4, "kinds": ["agent", "labrun"]}]
    assert sum(1 for event, _ in seen if event == "identity_work_reaped_row") == 4


def test_an_identity_that_is_not_alive_keeps_rows_nothing_says_have_ended(box: Box, tmp_path) -> None:
    """A crashed session's lab run can still run on its host, and a seat's worktree rows are its ownership when the seat is
    bound again: "the identity is not alive" alone reads stale and never reaps."""
    from nexus.identity import reap as work_reap

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
    from nexus.identity import reap as work_reap
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
    from tests._module_seam import setattr_in

    setattr_in(monkeypatch, work_reap_child, "subprocess.Popen", lambda argv, **k: started.append(argv))
    assert work_reap_child.spawn("sid-1") is False, "conftest sets NX_WORK_REAP_DISABLED=1"
    monkeypatch.delenv(work_reap_child.DISABLE_ENV)
    assert work_reap_child.spawn("") is False and work_reap_child.spawn("unknown") is False
    assert work_reap_child.spawn("sid-1") is True
    assert started and started[0][-2:] == ["nexus.hooks.work_reap_child", "sid-1"]


# ── round 1 (code-review-expert and substantive-critic on aa88d0a7e1) ──────────────────────────────────────────────────


def _transcript(tmp_path, session: str, notices: list[tuple[str, str, str]]):
    """A Claude transcript for *session* holding task notifications ``(agent id, status, ISO time)``."""
    import json as _json

    root = tmp_path / "claude-projects"
    proj = root / "-Users-someone-git-nexus"
    proj.mkdir(parents=True)
    with (proj / f"{session}.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(_json.dumps({"type": "user", "timestamp": "2026-11-17T00:00:00Z", "content": "hello"}) + "\n")
        for agent, status, at in notices:
            body = (f"<task-notification>\n<task-id>{agent}</task-id>\n<tool-use-id>toolu_x</tool-use-id>\n"
                    f"<status>{status}</status>\n</task-notification>")
            fh.write(_json.dumps({"type": "queue-operation", "operation": "enqueue", "timestamp": at, "content": body}) + "\n")
    return root


def test_an_agent_whose_task_notification_says_it_ended_is_reaped_inside_a_live_session(box: Box, tmp_path) -> None:
    """S1 (critic): an agent row has no process. A stop that never wrote (TaskStop, a hook killed at its budget, a failed
    engine call) left the row running for 12 hours. The harness writes the agent's end into the holder's transcript."""
    from nexus.identity import reap as work_reap
    from nexus.identity.holder import epoch_seconds

    i1 = box.running_seat("alpha", 100)
    box.engine.sessions["sid-100"] = (i1, 1)
    box.engine.records[i1]["holder"]["session_id"] = "sid-100"
    written = 500.0
    for agent in ("aended", "arunning", "aresumed", "afailed"):
        box.engine.work[(i1, "agent", f"agent:{agent}")] = ("running", written)
    after, before = "1970-01-01T00:10:00Z", "1970-01-01T00:05:00Z"          # 600 s and 300 s: around the row's 500 s
    assert epoch_seconds(after) > written > epoch_seconds(before)
    root = _transcript(tmp_path, "sid-100", [("aended", "completed", after), ("aresumed", "completed", before),
                                             ("afailed", "failed", after), ("arunning", "running", after)])
    closed = {r["ref"]: r["why"] for r in work_reap.reap(box.env(), i1, projects=root)}
    assert closed == {"agent:aended": "its task notification says it ended", "agent:afailed": "its task notification says it ended"}
    assert (i1, "agent", "agent:arunning") in box.engine.work, "no terminal status"
    assert (i1, "agent", "agent:aresumed") in box.engine.work, "written again after its notification: resumed"


def test_ended_agents_reads_nothing_for_no_session_or_no_transcript(tmp_path) -> None:
    from nexus.identity.reap import ended_agents

    assert ended_agents(None, tmp_path) == {} and ended_agents("sid-none", tmp_path) == {}


def test_a_wait_for_an_earlier_session_whose_process_still_runs_is_not_reaped(box: Box) -> None:
    from nexus.identity import reap as work_reap
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    box.engine.sessions["sid-100"] = (i1, 1)
    box.engine.records[i1]["holder"]["session_id"] = "11111111-2222-3333-4444-555555555555"
    old = "99999999-2222-3333-4444-555555555555"
    lone = "88888888-2222-3333-4444-555555555555"
    box.engine.work[(i1, "wait", old)] = ("running", 500.0)
    box.engine.work[(i1, "wait", lone)] = ("running", 500.0)
    box.procs.start(300, "2026-11-17T09:00:00Z", registry=False)
    work.record_owner(box.identity_dir, i1, "wait", old, pid=300, start="2026-11-17T09:00:00Z", host=box.procs.host)
    closed = {r["ref"] for r in work_reap.reap(box.env(), i1)}
    assert closed == {lone}, "the vouched wait is the authority; the lone one names an earlier session"


def test_a_row_written_again_between_the_read_and_the_close_is_left_alone(box: Box, tmp_path) -> None:
    from nexus.identity import reap as work_reap

    i1, refs = _bobs_list(box, tmp_path)
    real = box.engine.work_of
    reads = {"n": 0}

    def rewritten(identity: str):
        reads["n"] += 1
        if reads["n"] == 2:                                    # the registrar writes the row again between the two reads
            box.engine.work[(i1, "labrun", refs["lab_dead_1"])] = ("running", 900.0)
        return real(identity)

    box.engine.work_of = rewritten                             # type: ignore[method-assign]
    closed = {r["ref"] for r in work_reap.reap(box.env(), i1)}
    assert refs["lab_dead_1"] not in closed and refs["lab_dead_2"] in closed


def test_the_reap_stops_at_its_deadline(box: Box, tmp_path) -> None:
    from nexus.identity import reap as work_reap

    i1, _ = _bobs_list(box, tmp_path)
    assert work_reap.reap(box.env(), i1, deadline_s=-1.0) == []


def test_a_reap_that_fails_leaves_the_list_readable_and_says_why(box: Box, tmp_path, monkeypatch) -> None:
    from nexus.identity import reap as work_reap

    _bobs_list(box, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("transcript unreadable")

    class Quiet:
        def warning(self, *a, **k) -> None:
            pass

    from nexus.identity import work

    monkeypatch.setattr(work_reap, "reap", boom)
    monkeypatch.setattr(work, "_log", Quiet())             # CliRunner captures structlog's default print with stdout
    report = _listed(box, "--reap")
    assert report["reap_error"] == "RuntimeError: transcript unreadable" and len(report["work"]) == 45


@pytest.mark.parametrize(("action", "spawned"), [("bound", True), ("ephemeral", True), ("unchanged", True),
                                                 ("skipped", True), ("delegated", False), ("pending", False),
                                                 ("unbound", False), (None, False)])
def test_session_start_reaps_only_for_a_session_that_holds_its_own_identity(monkeypatch, action, spawned) -> None:
    """B2 (code-review-expert): the gate was ``identity is not None``, which is always true. A delegated claude -p child,
    NX_IDENTITY_BIND=0 (outcome None) and an unbound start must start no reap child."""
    import nexus.hooks as hooks
    from nexus.hooks import work_reap_child
    from nexus.hooks.identity_bind import IdentityResult
    from nexus.identity.binder import BindOutcome

    started: list[str] = []
    monkeypatch.setattr(work_reap_child, "spawn", lambda sid: started.append(sid) or True)
    result = IdentityResult(outcome=BindOutcome(action, "row") if action else None)
    monkeypatch.setattr(hooks, "_bind_identity", lambda sid, source: result)
    monkeypatch.setattr(hooks, "render_session_start", lambda *a, **k: "")
    monkeypatch.setattr(hooks, "_mailbox_arm_block", lambda sid: "")
    hooks.session_start("12345678-aaaa-bbbb-cccc-1234567890ab", source="startup")
    assert started == (["12345678-aaaa-bbbb-cccc-1234567890ab"] if spawned else [])


def test_done_and_forget_can_name_the_identity_for_a_process_that_lost_its_session(box: Box, monkeypatch) -> None:
    """labrun_79e, 2026-11-22: a lab-remote whose shell ended before the run did is orphaned (ppid 1), holds no identity,
    and its `done` said "nothing registered" with exit 0, leaving the row running. It names the identity register printed."""
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    monkeypatch.setattr(work, "caller_identity", lambda: None)
    box.engine.work[(i1, "labrun", "h:1:2")] = ("running", 500.0)
    box.engine.work[(i1, "agent", "agent:a1")] = ("running", 500.0)
    assert "nothing registered" in box.run("work", "done", "--kind", "labrun", "--ref", "h:1:2").output
    out = box.run("work", "done", "--kind", "labrun", "--ref", "h:1:2", "--identity", i1)
    assert out.exit_code == 0 and box.engine.work[(i1, "labrun", "h:1:2")][0] == "done"
    assert box.run("work", "forget", "--kind", "agent", "--ref", "agent:a1", "--identity", i1).exit_code == 0
    assert (i1, "agent", "agent:a1") not in box.engine.work
    assert "nothing to mark done" in box.run("work", "done", "--kind", "labrun", "--ref", "h:9:9", "--identity", i1).output
