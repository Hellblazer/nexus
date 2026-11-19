# SPDX-License-Identifier: AGPL-3.0-or-later
"""Background work an identity owns, and the code that reads it (RDR-253 Step 6, nexus-hczdo.1.9).

Tenant-free: :class:`tests.identity.fake_engine.FakeEngine` stands in for the engine's ``/v1/identity`` work routes
and a dict stands in for the process table. What the real engine does with the same calls is checked by the rows of
``tests/identity/test_work_engine_rows.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_identity_cmd import Box


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def test_tmux_server_of_a_live_identity_is_not_stopped(box: Box) -> None:
    """Identity i1 registers kind=tmux for server s1 and its holder process runs. The cleanup helper asked to stop s1
    refuses and names the owner: ownership is read from the work row and liveness from the holder check, never from
    the server's name or a process listing."""
    i1 = box.running_seat("alpha", 100)
    assert i1 == "i1"
    box.engine.put_work(i1, "tmux", "s1", "running")
    result = box.run("may-stop", "--kind", "tmux", "--ref", "s1")
    assert result.exit_code == 1, result.output
    assert "i1" in result.output and "alpha" in result.output and "s1" in result.output, result.output


# ── helpers ─────────────────────────────────────────────────────────────────


def as_caller(monkeypatch: pytest.MonkeyPatch, identity: str | None) -> None:
    """The process under test holds *identity* (its token file), or none."""
    from nexus.identity import work

    monkeypatch.setattr(work, "caller_identity", lambda: identity)


def crash(box: Box, pid: int) -> None:
    """The holder exits without ending its identity: the registry file stays, the process is gone."""
    box.procs.stop(pid)


# ── may-stop: ownership from work rows, liveness from the holder check ──────


def test_a_stop_is_allowed_for_the_callers_own_resource(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    i1 = box.running_seat("alpha", 100)
    box.engine.put_work(i1, "tmux", "s1", "running")
    as_caller(monkeypatch, i1)
    result = box.run("may-stop", "--kind", "tmux", "--ref", "s1")
    assert result.exit_code == 0, result.output
    assert "no live owner other than this process" in result.output


def test_a_worktree_whose_owner_is_alive_is_kept_even_when_ps_finds_no_process_in_it(box: Box, tmp_path: Path) -> None:
    """The worktree directory exists and nothing runs in it: the process table has no pid with that cwd. The owner's
    holder is another process entirely, and it runs. The removal is still refused (RDR-253 incident I-44: a worktree was
    removed because ps said nobody was in it)."""
    path = tmp_path / "nexus-wt" / "alpha"
    path.mkdir(parents=True)
    i1 = box.running_seat("alpha", 100)
    box.engine.put_work(i1, "worktree", str(path), "running")
    assert 100 in box.procs.procs and not any(p for p in box.procs.procs if p != 100), "only the owner's holder runs"
    result = box.run("may-stop", "--kind", "worktree", "--ref", str(path))
    assert result.exit_code == 1, result.output
    assert str(path) in result.output and "i1" in result.output and "alpha" in result.output


def test_a_resource_nobody_registered_is_unowned_and_may_be_stopped(box: Box) -> None:
    result = box.run("may-stop", "--kind", "tmux", "--ref", "nobody-made-this")
    assert result.exit_code == 0 and "unowned" in result.output, result.output


def test_the_stale_case_a_running_row_of_an_identity_that_is_not_alive_never_blocks(box: Box) -> None:
    """The bead's open item: the owner crashed, so its running row would read as busy forever. Every reader asks the
    holder check first and treats the row as stale."""
    i1 = box.running_seat("alpha", 100)
    box.engine.put_work(i1, "tmux", "s1", "running")
    crash(box, 100)
    result = box.run("may-stop", "--kind", "tmux", "--ref", "s1")
    assert result.exit_code == 0, result.output
    assert "stale rows ignored: i1" in result.output
    listed = box.run("work", "list", "i1", "--running")
    assert listed.exit_code == 0 and "tmux" not in listed.output and "no work" in listed.output, listed.output
    full = box.run("work", "list", "i1")
    assert "stale" in full.output and "not alive" in full.output, full.output
    owners = box.run("work", "owners", "--kind", "tmux", "--ref", "s1")
    assert "stale: holder not alive" in owners.output, owners.output


def test_an_owner_that_ended_or_whose_seat_was_released_is_not_alive(box: Box) -> None:
    i1 = box.running_seat("alpha", 100)
    box.engine.put_work(i1, "worktree", "/p", "running")
    box.engine.records[i1].update(state="unheld", holder=None)         # released: no holder at all
    assert box.run("may-stop", "--kind", "worktree", "--ref", "/p").exit_code == 0
    box.engine.records[i1].update(state="ended")
    assert box.run("may-stop", "--kind", "worktree", "--ref", "/p").exit_code == 0


def test_a_done_row_never_blocks_even_when_the_owner_is_alive(box: Box) -> None:
    i1 = box.running_seat("alpha", 100)
    box.engine.put_work(i1, "tmux", "s1", "done")
    assert box.run("may-stop", "--kind", "tmux", "--ref", "s1").exit_code == 0


def test_an_owner_whose_process_cannot_be_judged_still_blocks(box: Box) -> None:
    """A pid that runs but whose start time cannot be read is running, never gone: the reader that cannot prove a holder
    gone does not act as if it were."""
    i1 = box.running_seat("alpha", 100)
    box.engine.put_work(i1, "tmux", "s1", "running")
    box.procs.os_start = lambda pid: None                               # type: ignore[method-assign]
    assert box.run("may-stop", "--kind", "tmux", "--ref", "s1").exit_code == 1


def test_may_stop_fails_closed_when_the_engine_does_not_answer(box: Box) -> None:
    from nexus.identity.bind_client import IdentityUnavailableError

    def down(kind: str, ref: str):
        raise IdentityUnavailableError("connection refused")

    box.engine.work_owners = down                                      # type: ignore[method-assign]
    result = box.run("may-stop", "--kind", "tmux", "--ref", "s1")
    assert result.exit_code == 1 and "not answering" in result.output, result.output


def test_two_owners_one_alive_one_crashed_names_only_the_live_one(box: Box) -> None:
    live = box.running_seat("alpha", 100)
    dead = box.running_seat("beta", 101)
    box.engine.put_work(live, "worktree", "/p", "running")
    box.engine.put_work(dead, "worktree", "/p", "running")
    crash(box, 101)
    result = box.run("may-stop", "--kind", "worktree", "--ref", "/p")
    assert result.exit_code == 1 and "alpha" in result.output and "beta" not in result.output, result.output


# ── register, done, forget, list, run ───────────────────────────────────────


def test_register_done_and_forget_write_the_callers_row(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    i1 = box.running_seat("alpha", 100)
    as_caller(monkeypatch, i1)
    assert box.run("work", "register", "--kind", "worktree", "--ref", "/p").exit_code == 0
    assert box.engine.work[(i1, "worktree", "/p")][0] == "running"
    assert box.run("work", "done", "--kind", "worktree", "--ref", "/p").exit_code == 0
    assert box.engine.work[(i1, "worktree", "/p")][0] == "done"
    out = box.run("work", "forget", "--kind", "worktree", "--ref", "/p")
    assert out.exit_code == 0 and (i1, "worktree", "/p") not in box.engine.work


def test_a_process_with_no_identity_registers_nothing_and_still_exits_zero(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    as_caller(monkeypatch, None)
    result = box.run("work", "register", "--kind", "tmux", "--ref", "s1")
    assert result.exit_code == 0 and "nothing registered" in result.output
    assert box.engine.work == {} and box.engine.ids_called("put_work") == []


def test_a_bad_kind_is_a_usage_error(box: Box) -> None:
    assert box.run("work", "register", "--kind", "daemon", "--ref", "x").exit_code == 2


def test_the_two_hundred_row_limit_forgets_finished_work_once_and_retries(box: Box) -> None:
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    for n in range(200):
        box.engine.put_work(i1, "wait", f"w{n}", "done" if n < 150 else "running")
    row = work.register(box.engine, i1, "wait", "fresh")
    assert row.state == "running"
    left = {k[2] for k in box.engine.work if k[0] == i1}
    assert "fresh" in left and not any(f"w{n}" in left for n in range(150)) and "w199" in left, "only done rows went"


def test_the_limit_with_nothing_finished_to_forget_is_the_engines_refusal(box: Box) -> None:
    from nexus.identity import work
    from nexus.identity.bind_client import IdentityApiError

    i1 = box.running_seat("alpha", 100)
    for n in range(200):
        box.engine.put_work(i1, "wait", f"w{n}", "running")
    with pytest.raises(IdentityApiError) as err:
        work.register(box.engine, i1, "wait", "fresh")
    assert err.value.code == "work_limit"


def test_a_registrars_failure_is_logged_and_never_raised(box: Box) -> None:
    from nexus.identity import work
    from nexus.identity.bind_client import IdentityUnavailableError

    i1 = box.running_seat("alpha", 100)

    def down(*a, **k):
        raise IdentityUnavailableError("refused")

    box.engine.put_work = down                                         # type: ignore[method-assign]
    assert work.register_own("wait", "w", client=box.engine, identity=i1) is False
    assert work.register_own("wait", "w", client=box.engine, identity=None) is False, "no identity: nothing to label"


def test_work_run_registers_runs_marks_done_and_passes_the_exit_code_through(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    as_caller(monkeypatch, i1)
    seen: list[str] = []

    def runner(argv):
        seen.append(box.engine.work[(i1, "labrun", "full")][0])          # the row is running while the command runs
        return 7

    code = work.run_command(box.env(), "labrun", "full", ["lab-remote", "--", "x"], runner=runner)
    assert code == 7 and seen == ["running"]
    assert box.engine.work[(i1, "labrun", "full")][0] == "done"


def test_work_run_marks_done_when_the_command_raises(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    as_caller(monkeypatch, i1)

    def boom(argv):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        work.run_command(box.env(), "labrun", "full", ["x"], runner=boom)
    assert box.engine.work[(i1, "labrun", "full")][0] == "done"


def test_work_run_still_runs_the_command_when_the_label_is_refused_and_with_no_identity(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.identity import work
    from nexus.identity.bind_client import IdentityUnavailableError

    i1 = box.running_seat("alpha", 100)
    as_caller(monkeypatch, i1)
    box.engine.put_work = lambda *a, **k: (_ for _ in ()).throw(IdentityUnavailableError("refused"))   # type: ignore[method-assign]
    assert work.run_command(box.env(), "wait", "w", ["x"], runner=lambda argv: 0) == 0
    as_caller(monkeypatch, None)
    assert work.run_command(box.env(), "wait", "w", ["x"], runner=lambda argv: 3) == 3


def test_work_run_through_the_cli_execs_the_command_and_exits_with_its_status(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    i1 = box.running_seat("alpha", 100)
    as_caller(monkeypatch, i1)
    result = box.run("work", "run", "--kind", "labrun", "--ref", "r", "--", sys.executable, "-c", "import sys; sys.exit(5)")
    assert result.exit_code == 5, result.output
    assert box.engine.work[(i1, "labrun", "r")][0] == "done"
    missing = box.run("work", "run", "--kind", "labrun", "--ref", "r2", "--", "/no/such/command-xyz")
    assert missing.exit_code == 1 and "not found" in missing.output
    assert box.engine.work[(i1, "labrun", "r2")][0] == "done", "registered, then ended: nothing left running"


def test_list_by_session_and_json_carry_the_stale_flag(box: Box) -> None:
    import json

    i1 = box.running_seat("alpha", 100)
    box.engine.put_work(i1, "wait", "w", "running")
    box.engine.sessions["sid-100"] = (i1, 1)
    live = json.loads(box.run("work", "list", "--session", "sid-100", "--running", "--json").stdout)
    assert live == {"identity": i1, "alive": True,
                    "work": [{"kind": "wait", "ref": "w", "state": "running", "updated_at": 1000.0, "stale": False}]}
    crash(box, 100)
    dead = json.loads(box.run("work", "list", "--session", "sid-100", "--json").stdout)
    assert dead["alive"] is False and dead["work"][0]["stale"] is True
    assert json.loads(box.run("work", "list", "--session", "sid-100", "--running", "--json").stdout)["work"] == []


# ── memberships (the fence is the engine's; the engine rows check the real one) ──


def test_a_membership_write_by_a_non_owner_is_refused_and_by_the_owner_is_kept(box: Box) -> None:
    from nexus.identity import membership
    from nexus.identity.bind_client import IdentityApiError

    i1 = box.running_seat("alpha", 100)
    i2 = box.running_seat("beta", 101)
    box.engine.caller_token = i2
    with pytest.raises(IdentityApiError) as err:
        membership.join(box.engine, i1, "reviewers", "leader")
    assert err.value.status == 403 and err.value.code == "not_owner"
    assert membership.groups(box.engine, i1) == []
    box.engine.caller_token = i1
    joined = membership.join(box.engine, i1, "reviewers", "leader")
    assert (joined.identity, joined.group, joined.role) == (i1, "reviewers", "leader")
    assert [(m.group, m.role) for m in membership.groups(box.engine, i1)] == [("reviewers", "leader")]
    assert [m.identity for m in membership.roster(box.engine, "reviewers")] == [i1]
    assert membership.leave(box.engine, i1, "reviewers") is True and membership.leave(box.engine, i1, "reviewers") is False


def test_a_session_with_no_identity_answers_unknown_session_which_the_restart_script_reads_as_no_work(box: Box) -> None:
    """tests/scripts/test_claude_restart_work.py treats this wording as 'no identity, so no work' and any other failure as
    'could not be read': pinned here against the real command."""
    result = box.run("work", "list", "--session", "no-such-session", "--running", "--json")
    assert result.exit_code == 1 and "unknown_session" in result.output, result.output
