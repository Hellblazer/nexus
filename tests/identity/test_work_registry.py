# SPDX-License-Identifier: AGPL-3.0-or-later
"""Background work an identity owns, and the code that reads it (RDR-253 Step 6, nexus-hczdo.1.9).

Tenant-free: :class:`tests.identity.fake_engine.FakeEngine` stands in for the engine's ``/v1/identity`` work routes
and a dict stands in for the process table. What the real engine does with the same calls is checked by the rows of
``tests/identity/test_work_engine_rows.py``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests._module_seam import setattr_in
from tests.test_identity_cmd import Box

#: The CLI resolves a worktree ref to its real path ("/p" is "C:\\p" on Windows), so rows seeded straight into the
#: fake engine use the same spelling.
P_REAL = os.path.realpath("/p")


def old_session(n: int = 0) -> str:
    """The id of an EARLIER session (a Claude session id is a UUID: that shape is what makes a wait ref a session's)."""
    import uuid

    return str(uuid.UUID(int=0xABCDEF0000 + n))


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
    assert result.exit_code == 3, result.output            # 3: a live identity owns it; 1 is every other failure
    assert "i1" in result.output and "alpha" in result.output and "s1" in result.output, result.output
    assert " is owned by " in result.output, "the sandbox's refusal contract is the exit status; the wording is for people"


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
    real = str(path.resolve())                                          # the CLI spells a worktree by its real path
    box.engine.put_work(i1, "worktree", real, "running")
    assert 100 in box.procs.procs and not any(p for p in box.procs.procs if p != 100), "only the owner's holder runs"
    result = box.run("may-stop", "--kind", "worktree", "--ref", str(path))
    assert result.exit_code == 3, result.output
    assert real in result.output and "i1" in result.output and "alpha" in result.output


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
    box.engine.put_work(i1, "worktree", P_REAL, "running")
    box.engine.records[i1].update(state="unheld", holder=None)         # released: no holder at all
    assert box.run("may-stop", "--kind", "worktree", "--ref", "/p").exit_code == 0


def test_an_ended_identity_is_not_alive_even_while_its_holder_process_runs(box: Box) -> None:
    """The state alone decides: the holder record and its running process stay, so only ``ended`` can free it (r1
    item 5: the old test cleared the holder first, so the ended half passed on the released rule)."""
    i1 = box.running_seat("alpha", 100)
    box.engine.put_work(i1, "worktree", P_REAL, "running")
    assert box.run("may-stop", "--kind", "worktree", "--ref", "/p").exit_code == 3, "control: live and held blocks"
    box.engine.records[i1].update(state="ended")
    assert box.engine.records[i1]["holder"] is not None
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
    assert box.run("may-stop", "--kind", "tmux", "--ref", "s1").exit_code == 3


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
    box.engine.put_work(live, "worktree", P_REAL, "running")
    box.engine.put_work(dead, "worktree", P_REAL, "running")
    crash(box, 101)
    result = box.run("may-stop", "--kind", "worktree", "--ref", "/p")
    assert result.exit_code == 3 and "alpha" in result.output and "beta" not in result.output, result.output


# ── register, done, forget, list, run ───────────────────────────────────────


def test_register_done_and_forget_write_the_callers_row(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    i1 = box.running_seat("alpha", 100)
    as_caller(monkeypatch, i1)
    assert box.run("work", "register", "--kind", "worktree", "--ref", "/p").exit_code == 0
    assert box.engine.work[(i1, "worktree", P_REAL)][0] == "running"
    assert box.run("work", "done", "--kind", "worktree", "--ref", "/p").exit_code == 0
    assert box.engine.work[(i1, "worktree", P_REAL)][0] == "done"
    out = box.run("work", "forget", "--kind", "worktree", "--ref", "/p")
    assert out.exit_code == 0 and (i1, "worktree", P_REAL) not in box.engine.work


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
        box.engine.put_work(i1, "labrun", f"w{n}", "done" if n < 150 else "running")
    row = work.register(box.engine, i1, "wait", "fresh")
    assert row.state == "running"
    left = {k[2] for k in box.engine.work if k[0] == i1}
    assert "fresh" in left and not any(f"w{n}" in left for n in range(150)) and "w199" in left, "only done rows went"


def test_the_limit_also_forgets_worktree_rows_whose_directory_is_gone(box: Box, tmp_path) -> None:
    """An agent's isolation worktree registers from the preflight and is removed by the harness; nothing marks it done,
    so the limit's purge drops a worktree row whose directory no longer exists, and keeps one that still does."""
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    kept = tmp_path / "kept"
    kept.mkdir()
    box.engine.put_work(i1, "worktree", str(kept), "running")
    for n in range(199):
        box.engine.put_work(i1, "worktree", str(tmp_path / f"gone{n}"), "running")
    row = work.register(box.engine, i1, "wait", "fresh")
    assert row.state == "running"
    left = {k[2] for k in box.engine.work if k[0] == i1}
    assert left == {str(kept), "fresh"}


def test_the_limit_with_nothing_finished_to_forget_is_the_engines_refusal(box: Box) -> None:
    from nexus.identity import work
    from nexus.identity.bind_client import IdentityApiError

    i1 = box.running_seat("alpha", 100)
    for n in range(200):
        box.engine.put_work(i1, "labrun", f"w{n}", "running")
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
    box.engine.put_work(i1, "labrun", "w", "running")
    box.engine.sessions["sid-100"] = (i1, 1)
    live = json.loads(box.run("work", "list", "--session", "sid-100", "--running", "--json").stdout)
    assert live == {"identity": i1, "alive": True,
                    "work": [{"kind": "labrun", "ref": "w", "state": "running", "updated_at": 1000.0, "stale": False}]}
    crash(box, 100)
    dead = json.loads(box.run("work", "list", "--session", "sid-100", "--json").stdout)
    assert dead["alive"] is False and dead["work"][0]["stale"] is True
    assert json.loads(box.run("work", "list", "--session", "sid-100", "--running", "--json").stdout)["work"] == []


# ── agent work and the liveness probe (RDR-253 Step 6 part 1, nexus-hczdo) ───


HOUR = 3600.0


def listed(box: Box, session: str = "sid-100") -> dict[tuple[str, str], bool]:
    """``(kind, ref) -> stale`` of the identity's running rows, as ``nx identity work list --json`` reports them."""
    import json

    out = json.loads(box.run("work", "list", "--session", session, "--json").stdout)
    return {(w["kind"], w["ref"]): w["stale"] for w in out["work"] if w["state"] == "running"}


def seed(box: Box, identity: str, kind: str, ref: str, *, age_s: float = 0.0, state: str = "running") -> None:
    box.engine.put_work(identity, kind, ref, state)
    box.engine.work[(identity, kind, ref)] = (state, box.env().clock() - age_s)


def test_agent_is_a_kind_of_work_the_cli_accepts(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.identity import work

    assert "agent" in work.KINDS
    i1 = box.running_seat("alpha", 100)
    as_caller(monkeypatch, i1)
    assert box.run("work", "register", "--kind", "agent", "--ref", "agent:a1").exit_code == 0
    assert box.engine.work[(i1, "agent", "agent:a1")][0] == "running"
    assert box.run("work", "done", "--kind", "agent", "--ref", "agent:a1").exit_code == 0
    assert box.engine.work[(i1, "agent", "agent:a1")][0] == "done"


def test_an_engine_that_does_not_know_the_agent_kind_is_logged_and_the_registrar_carries_on(box: Box) -> None:
    """Engine 0.1.198 answers 400 bad_kind for a kind it does not list. The hooks register through register_own: one log
    line, False, never an exception into the dispatch."""
    from nexus.identity import work
    from nexus.identity.bind_client import IdentityApiError

    i1 = box.running_seat("alpha", 100)

    def refuse(*a, **k):
        raise IdentityApiError(400, "bad_kind", "kind must be one of ['labrun', 'tmux', 'wait', 'watcher', 'worktree']", {})

    box.engine.put_work = refuse                                       # type: ignore[method-assign]
    assert work.register_own("agent", "agent:a1", client=box.engine, identity=i1) is False


def test_a_running_row_older_than_the_ttl_is_stale_for_the_kinds_that_end_with_their_process(box: Box) -> None:
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    early(box, i1)
    box.engine.sessions["sid-100"] = (i1, 1)
    for kind in ("wait", "labrun", "agent", "watcher"):
        seed(box, i1, kind, "x:old", age_s=work.ROW_TTL_S + HOUR)
        seed(box, i1, kind, "x:new", age_s=HOUR)
    seed(box, i1, "tmux", "x:old", age_s=work.ROW_TTL_S + HOUR)
    seed(box, i1, "worktree", P_REAL, age_s=work.ROW_TTL_S + HOUR)
    rows = listed(box)
    assert work.ROW_TTL_S == 12 * HOUR
    for kind in ("wait", "labrun", "agent", "watcher"):
        assert rows[(kind, "x:old")] is True, kind
        assert rows[(kind, "x:new")] is False, kind
    assert rows[("tmux", "x:old")] is False, "a tmux server outlives the session: no TTL"


def test_a_row_whose_registering_process_is_gone_is_stale_and_one_whose_process_runs_is_not(box: Box) -> None:
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    box.engine.sessions["sid-100"] = (i1, 1)
    box.procs.start(300, "2026-11-17T09:00:00Z", registry=False)
    box.procs.start(301, "2026-11-17T09:00:00Z", registry=False)
    for pid, ref in ((300, "alive"), (301, "dead"), (302, "reused")):
        seed(box, i1, "labrun", ref, age_s=work.ROW_TTL_S + HOUR if ref == "alive" else 60)
    work.record_owner(box.identity_dir, i1, "labrun", "alive", pid=300, start="2026-11-17T09:00:00Z", host=box.procs.host)
    work.record_owner(box.identity_dir, i1, "labrun", "dead", pid=301, start="2026-11-17T09:00:00Z", host=box.procs.host)
    work.record_owner(box.identity_dir, i1, "labrun", "reused", pid=300, start="2026-11-16T01:00:00Z", host=box.procs.host)
    box.procs.stop(301)
    rows = listed(box)
    assert rows[("labrun", "alive")] is False, "the process is the authority: older than the TTL is not stale while it runs"
    assert rows[("labrun", "dead")] is True, "its process is gone"
    assert rows[("labrun", "reused")] is True, "the pid runs, but it is another process (a reused pid)"


def test_a_labrun_ref_that_names_its_host_pid_and_start_is_probed_without_any_owner_record(box: Box) -> None:
    """lab-remote registers kind=labrun with ref ``<host>:<pid>:<started>`` (host = this machine's short name, pid = its
    own pid, started = epoch seconds at its start). A SIGKILL'd lab-remote leaves such a row: its pid is gone, or the pid
    runs but began after <started> (reused). The ready check must list it as stale, never block on it."""
    from nexus.identity import holder, work

    i1 = box.running_seat("alpha", 100)
    box.engine.sessions["sid-100"] = (i1, 1)
    began = holder.epoch_seconds("2026-11-17T09:00:00Z")
    box.procs.start(400, "2026-11-17T09:00:00Z", registry=False)                  # running, began exactly at <started>
    box.procs.start(401, "2026-11-17T09:00:00Z", registry=False)
    box.procs.start(402, "2026-11-17T10:00:00Z", registry=False)                  # running, began an hour after <started>
    box.procs.stop(401)
    rows = {"alive": f"{box.procs.host}:400:{began}", "dead": f"{box.procs.host}:401:{began}",
            "reused": f"{box.procs.host}:402:{began}", "elsewhere": f"another-host:403:{began}"}
    for name, ref in rows.items():
        seed(box, i1, "labrun", ref, age_s=work.ROW_TTL_S + HOUR if name == "alive" else 60)
    got = listed(box)
    assert got[("labrun", rows["alive"])] is False, "its process runs and began at <started>: the pid is the authority, not the TTL"
    assert got[("labrun", rows["dead"])] is True, "its pid is gone"
    assert got[("labrun", rows["reused"])] is True, "the pid runs but began after <started>: another process"
    assert got[("labrun", rows["elsewhere"])] is False, "another host's pid says nothing here"
    assert rows["dead"] not in box.run("work", "list", "i1", "--running").output, "--running drops it"
    assert "its process is gone" in box.run("work", "list", "i1").output


def test_a_labrun_ref_whose_process_start_cannot_be_read_is_not_judged_gone(box: Box) -> None:
    from nexus.identity import holder

    i1 = box.running_seat("alpha", 100)
    box.engine.sessions["sid-100"] = (i1, 1)
    began = holder.epoch_seconds("2026-11-17T09:00:00Z")
    box.procs.start(400, "2026-11-17T09:00:00Z", registry=False)
    box.procs.os_start = lambda pid: None                                          # type: ignore[method-assign]
    ref = f"{box.procs.host}:400:{began}"
    seed(box, i1, "labrun", ref, age_s=60)
    assert listed(box)[("labrun", ref)] is False


def test_an_owner_record_from_another_host_is_not_judged_here(box: Box) -> None:
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    box.engine.sessions["sid-100"] = (i1, 1)
    seed(box, i1, "labrun", "elsewhere", age_s=60)
    work.record_owner(box.identity_dir, i1, "labrun", "elsewhere", pid=999, start="x", host="another-host")
    assert listed(box)[("labrun", "elsewhere")] is False


def test_the_stale_listing_says_why(box: Box) -> None:
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    early(box, i1)
    seed(box, i1, "agent", "agent:old", age_s=work.ROW_TTL_S + HOUR)
    out = box.run("work", "list", "i1").output
    assert "stale" in out and "agent:old" in out and "12 h" in out, out


def test_running_filter_drops_an_expired_row(box: Box) -> None:
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    seed(box, i1, "agent", "agent:old", age_s=work.ROW_TTL_S + HOUR)
    seed(box, i1, "agent", "agent:new", age_s=60)
    out = box.run("work", "list", "i1", "--running").output
    assert "agent:new" in out and "agent:old" not in out, out


def test_the_label_records_its_process_at_start_and_clears_it_at_done(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    box.procs.start(os.getpid(), "2026-11-17T09:00:00Z", registry=False)
    assert work.register_own("wait", "ci:develop@abc", client=box.engine, identity=i1, owner_dir=box.identity_dir, procs=box.procs)
    record = work.read_owner(box.identity_dir, i1, "wait", "ci:develop@abc")
    assert record is not None and record["pid"] == os.getpid() and record["start"] == "2026-11-17T09:00:00Z"
    assert work.finish_own("wait", "ci:develop@abc", client=box.engine, identity=i1, owner_dir=box.identity_dir, procs=box.procs)
    assert work.read_owner(box.identity_dir, i1, "wait", "ci:develop@abc") is None


def test_a_label_asks_register_own_to_record_its_process_and_work_run_does_too(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    """The long-lived registrars (Label: nx ci wait, nx tuple wait; work run) record their pid so a killed process reads
    stale; the hooks, whose process exits at once, do not."""
    from nexus.identity import work

    seen: list[dict] = []
    monkeypatch.setattr(work, "register_own", lambda kind, ref, state="running", **k: seen.append(k) or True)
    as_caller(monkeypatch, "i1")
    label = work.Label("wait", "ci:develop@abc")
    assert label.start() is True
    label.finish()
    assert [bool(k.get("owner_dir")) for k in seen] == [True, True]


def test_work_run_records_its_process_while_the_command_runs_and_clears_it_after(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    box.procs.start(os.getpid(), "2026-11-17T09:00:00Z", registry=False)
    as_caller(monkeypatch, i1)
    during: list[object] = []
    work.run_command(box.env(), "labrun", "full", ["x"], runner=lambda argv: during.append(work.read_owner(box.identity_dir, i1, "labrun", "full")) or 0)
    assert during[0] is not None and during[0]["pid"] == os.getpid()
    assert work.read_owner(box.identity_dir, i1, "labrun", "full") is None


# ── review round 1 (nexus-hczdo) ────────────────────────────────────────────


def early(box: Box, identity: str) -> None:
    """The holder began long before the rows a test seeds (the fake clock sits at 1000 s, holders at pid*1000 ms)."""
    box.engine.records[identity]["holder"]["started_at"] = -10**12


def test_the_skills_literal_hand_wait_is_live_and_blocking_and_survives_the_purge(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ship-blocker 1: ``nx identity work register --kind wait --ref develop-ci`` (the skill's own command, a plain ref) read
    as 'a wait from an earlier session': stale at once and dropped by the limit's purge while it ran."""
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    box.engine.sessions["sid-100"] = (i1, 1)
    early(box, i1)
    as_caller(monkeypatch, i1)
    assert box.run("work", "register", "--kind", "wait", "--ref", "develop-ci").exit_code == 0
    assert listed(box)[("wait", "develop-ci")] is False, "live, so it blocks the ready check"
    assert "develop-ci" in box.run("work", "list", "i1", "--running").output
    for n in range(199):
        box.engine.put_work(i1, "labrun", f"d{n}", "done")
    assert work.register(box.engine, i1, "labrun", "fresh", probe=box.env()).state == "running"
    assert (i1, "wait", "develop-ci") in box.engine.work, "the purge left a live hand wait alone"


def test_an_agent_row_older_than_the_holders_start_is_a_ghost_and_reads_stale_while_the_identity_is_alive(box: Box) -> None:
    """Critical 2: a restart with an agent in flight never runs its SubagentStop; the identity re-binds, so the row is neither
    stale by identity nor probed. It predates this holder's start: it cannot belong to this process."""
    i1 = box.running_seat("alpha", 100)                       # holder started_at = 100 * 1000 ms
    box.engine.sessions["sid-100"] = (i1, 1)
    box.engine.work[(i1, "agent", "agent:ghost")] = ("running", 40.0)
    box.engine.work[(i1, "agent", "agent:mine")] = ("running", 500.0)
    box.engine.work[(i1, "wait", "hand-ghost")] = ("running", 40.0)
    box.engine.work[(i1, "labrun", "hand-mine")] = ("running", 500.0)
    got = listed(box)
    assert got[("agent", "agent:ghost")] is True and got[("wait", "hand-ghost")] is True
    assert got[("agent", "agent:mine")] is False and got[("labrun", "hand-mine")] is False
    assert "registered before this session started" in box.run("work", "list", "i1").output
    assert "agent:ghost" not in box.run("work", "list", "i1", "--running").output


def test_a_row_with_an_owner_record_is_judged_by_its_process_not_by_the_holders_start(box: Box) -> None:
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    box.engine.sessions["sid-100"] = (i1, 1)
    box.procs.start(300, "2026-11-17T09:00:00Z", registry=False)
    box.engine.work[(i1, "wait", "ci:x@y")] = ("running", 40.0)             # older than the holder: but its process runs
    work.record_owner(box.identity_dir, i1, "wait", "ci:x@y", pid=300, start="2026-11-17T09:00:00Z", host=box.procs.host)
    assert listed(box)[("wait", "ci:x@y")] is False


def test_the_purge_also_drops_stale_running_rows_so_orphans_cannot_fill_the_limit(box: Box) -> None:
    from nexus.identity import holder, work

    i1 = box.running_seat("alpha", 100)
    early(box, i1)
    clock = box.env().clock()
    began = holder.epoch_seconds("2026-11-17T09:00:00Z")
    box.procs.start(400, "2026-11-17T09:00:00Z", registry=False)
    box.procs.start(401, "2026-11-17T09:00:00Z", registry=False)
    box.procs.stop(401)
    live_ref, dead_ref = f"{box.procs.host}:400:{began}", f"{box.procs.host}:401:{began}"
    box.engine.work[(i1, "labrun", live_ref)] = ("running", clock - 60)
    box.engine.work[(i1, "labrun", dead_ref)] = ("running", clock - 60)
    box.engine.work[(i1, "agent", "agent:fresh")] = ("running", clock - 60)
    for n in range(197):
        box.engine.work[(i1, "agent", f"agent:orphan{n}")] = ("running", clock - work.ROW_TTL_S - HOUR)
    row = work.register(box.engine, i1, "worktree", "/wt/new", probe=box.env())
    assert row.state == "running"
    left = {k[2] for k in box.engine.work if k[0] == i1}
    assert left == {live_ref, "agent:fresh", "/wt/new"}, "the TTL-old orphans and the dead process's row went; live ones stayed"


def test_a_registration_with_purge_off_is_refused_at_the_limit_and_deletes_nothing(box: Box) -> None:
    """Ship-blocker 2: the SubagentStart hook has 10 s and the purge is up to ~200 sequential calls: hooks never purge."""
    from nexus.identity import work
    from nexus.identity.bind_client import IdentityApiError

    i1 = box.running_seat("alpha", 100)
    for n in range(200):
        box.engine.put_work(i1, "labrun", f"w{n}", "done")
    with pytest.raises(IdentityApiError) as err:
        work.register(box.engine, i1, "agent", "agent:a1", purge=False)
    assert err.value.code == "work_limit" and not box.engine.ids_called("remove_work") and not box.engine.ids_called("work_of")
    assert work.register_own("agent", "agent:a1", client=box.engine, identity=i1, purge=False) is False
    assert box.engine.ids_called("remove_work") == [], "a hook's registration never deletes a row"


def test_forgetting_an_agent_row_removes_it_and_a_second_forget_is_a_no_op(box: Box) -> None:
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    work.register(box.engine, i1, "agent", "agent:a1")
    assert work.forget_own("agent", "agent:a1", client=box.engine, identity=i1) is True
    assert (i1, "agent", "agent:a1") not in box.engine.work
    assert work.forget_own("agent", "agent:a1", client=box.engine, identity=i1) is True, "nothing to remove is not a failure"
    assert work.forget_own("agent", "agent:a1", client=box.engine, identity=None) is False


def test_an_engine_that_refuses_a_kind_is_asked_once_per_process(box: Box) -> None:
    """Review item 8: on an engine from before the kind, every dispatch cost two failed calls (start and stop)."""
    from nexus.identity import work
    from nexus.identity.bind_client import IdentityApiError

    i1 = box.running_seat("alpha", 100)
    calls: list[str] = []

    def refuse(identity, kind, ref, state="running"):
        calls.append(kind)
        raise IdentityApiError(400, "bad_kind", "kind must be one of ['labrun']", {})

    box.engine.put_work = refuse                                       # type: ignore[method-assign]
    for n in range(3):
        assert work.register_own("agent", f"agent:a{n}", client=box.engine, identity=i1, purge=False) is False
    assert work.forget_own("agent", "agent:a0", client=box.engine, identity=i1) is False
    assert calls == ["agent"], "one failed call, then the process knows"
    assert work.register_own("labrun", "x", client=box.engine, identity=i1) is False and calls == ["agent", "labrun"], "per kind"


def test_an_iso_updated_at_from_the_real_engine_reads_the_ttl_too(box: Box) -> None:
    i1 = box.running_seat("alpha", 100)
    box.engine.sessions["sid-100"] = (i1, 1)
    early(box, i1)
    box.engine.work[(i1, "agent", "agent:old")] = ("running", "1969-12-31T00:00:00.123456Z")     # the clock sits at 1000 s
    box.engine.work[(i1, "agent", "agent:new")] = ("running", "1970-01-01T00:10:00Z")
    got = listed(box)
    assert got[("agent", "agent:old")] is True and got[("agent", "agent:new")] is False


def test_an_owner_record_survives_one_second_of_lstart_jitter(box: Box) -> None:
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    box.engine.sessions["sid-100"] = (i1, 1)
    early(box, i1)
    box.procs.start(300, "2026-11-17T09:00:01Z", registry=False)
    seed(box, i1, "wait", "ci:j@k", age_s=60)
    work.record_owner(box.identity_dir, i1, "wait", "ci:j@k", pid=300, start="2026-11-17T09:00:00Z", host=box.procs.host)
    assert listed(box)[("wait", "ci:j@k")] is False, "one second apart is the same process"
    box.procs.start(300, "2026-11-17T09:00:30Z", registry=False)
    assert listed(box)[("wait", "ci:j@k")] is True, "half a minute apart is another process"


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_owner_records_live_in_a_private_directory(box: Box) -> None:
    from nexus.identity import work

    work.record_owner(box.identity_dir, "i1", "wait", "x", pid=1, start=None, host="h", procs=box.procs)
    records = box.identity_dir / work.OWNER_DIR_NAME
    assert records.is_dir() and (records.stat().st_mode & 0o077) == 0, "0700: nobody else reads who runs what"


def test_owner_records_are_pruned_by_liveness_never_by_age(box: Box) -> None:
    """Review item 5: a standing wait older than two weeks lost its record to the old mtime rule; a record whose process is
    gone (and a leaked .tmp of a dead writer) must go."""
    import time as _time

    from nexus.identity import work

    host = box.procs.host
    box.procs.start(300, "2026-11-17T09:00:00Z", registry=False)
    box.procs.start(301, "2026-11-17T09:00:00Z", registry=False)
    work.record_owner(box.identity_dir, "i1", "wait", "alive", pid=300, start="2026-11-17T09:00:00Z", host=host)
    work.record_owner(box.identity_dir, "i1", "wait", "dead", pid=301, start="2026-11-17T09:00:00Z", host=host)
    box.procs.stop(301)
    records = box.identity_dir / work.OWNER_DIR_NAME
    ancient = _time.time() - 90 * 86400
    for f in records.glob("*.json"):
        os.utime(f, (ancient, ancient))
    leaked_dead, leaked_live = records / "abc.301.tmp", records / "def.300.tmp"
    leaked_dead.write_text("x", encoding="utf-8")
    leaked_live.write_text("x", encoding="utf-8")
    work.record_owner(box.identity_dir, "i1", "wait", "third", pid=300, start="2026-11-17T09:00:00Z", host=host, procs=box.procs)
    assert work.read_owner(box.identity_dir, "i1", "wait", "alive") is not None, "ninety days old, its process runs: kept"
    assert work.read_owner(box.identity_dir, "i1", "wait", "dead") is None, "its process is gone: pruned"
    assert not leaked_dead.exists() and leaked_live.exists()


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


# ── fix round 1: a label never fails or delays the work it labels ───────────


def test_work_run_runs_the_command_when_the_engine_env_cannot_be_built(box: Box, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """B1: with no resolvable endpoint ``IdentityEnv.real`` raises. The lab run must still start, and its status passes through."""
    import sys

    from click.testing import CliRunner

    from nexus.commands.identity_cmd import identity_group
    from nexus.identity.ops import IdentityCommandError, IdentityEnv

    as_caller(monkeypatch, "i1")

    def no_engine(cls):
        raise IdentityCommandError("no engine to talk to (nothing resolves)")

    monkeypatch.setattr(IdentityEnv, "real", classmethod(no_engine))
    marker = tmp_path / "ran"
    result = CliRunner().invoke(identity_group, ["work", "run", "--kind", "labrun", "--ref", "r", "--", sys.executable, "-c",
                                                 f"import sys; open({str(marker)!r}, 'w').write('x'); sys.exit(6)"])
    assert result.exit_code == 6 and marker.exists(), result.output


def test_work_run_runs_the_command_when_the_engine_answers_something_unexpected(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    """B1: a row the client cannot read (``Work.of`` raising KeyError) is no reason to skip the command."""
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    as_caller(monkeypatch, i1)
    box.engine.put_work = lambda *a, **k: {"unexpected": True}          # type: ignore[method-assign]
    ran: list[str] = []
    assert work.run_command(box.env(), "labrun", "r", ["x"], runner=lambda argv: ran.append("ran") or 4) == 4
    assert ran == ["ran"]


def test_work_run_forwards_a_signal_to_the_child_and_marks_done(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    """S8: TERM sent to ``work run`` reaches the wrapped command, the row ends done, and the status is 128 + the signal."""
    import os
    import signal
    import sys
    import threading

    from nexus.identity import work

    if not hasattr(signal, "SIGTERM") or sys.platform == "win32":
        pytest.skip("POSIX signals")
    i1 = box.running_seat("alpha", 100)
    as_caller(monkeypatch, i1)
    before = signal.getsignal(signal.SIGTERM)
    timer = threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM))     # the handler is installed before the child starts
    timer.start()
    try:
        code = work.run_command(box.env(), "labrun", "r", [sys.executable, "-c", "import time; time.sleep(60)"])
    finally:
        timer.cancel()
    assert code == 128 + int(signal.SIGTERM), "the child died of the forwarded TERM"
    assert box.engine.work[(i1, "labrun", "r")][0] == "done"
    assert signal.getsignal(signal.SIGTERM) == before, "the handlers are put back"


def test_work_done_for_work_with_no_row_is_a_no_op_that_says_so(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    as_caller(monkeypatch, box.running_seat("alpha", 100))
    result = box.run("work", "done", "--kind", "tmux", "--ref", "never-registered")
    assert result.exit_code == 0 and "nothing to mark done" in result.output
    assert box.engine.work == {}, "no finished row is made for work nobody registered"


# ── fix round 1: a worktree is its real path ────────────────────────────────


def test_a_worktree_is_spelled_by_its_real_path_on_every_verb(box: Box, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S3: a trailing slash, a relative path or a symlinked prefix must find the row, not read as unowned."""
    import os

    real = tmp_path / "real" / "wt"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")
    i1 = box.running_seat("alpha", 100)
    as_caller(monkeypatch, i1)
    assert box.run("work", "register", "--kind", "worktree", "--ref", f"{link}/wt/").exit_code == 0
    assert [k[2] for k in box.engine.work] == [str(real.resolve())], "registered under the real path"
    as_caller(monkeypatch, "i999")                                       # someone else asks about it, by other spellings
    for spelling in (str(real), f"{link}/wt", f"{link}/wt/", os.path.relpath(real)):
        out = box.run("may-stop", "--kind", "worktree", "--ref", spelling)
        assert out.exit_code == 3, (spelling, out.output)
    assert "alpha" in box.run("work", "owners", "--kind", "worktree", "--ref", f"{link}/wt").output
    as_caller(monkeypatch, i1)
    assert box.run("work", "done", "--kind", "worktree", "--ref", f"{link}/wt/").exit_code == 0
    assert box.engine.work[(i1, "worktree", str(real.resolve()))][0] == "done"
    assert box.run("work", "forget", "--kind", "worktree", "--ref", f"{link}/wt").exit_code == 0
    assert box.engine.work == {}


def test_may_stop_says_it_cannot_tell_who_the_caller_is_when_it_holds_no_readable_identity(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    i1 = box.running_seat("alpha", 100)
    box.engine.put_work(i1, "tmux", "s1", "running")
    as_caller(monkeypatch, None)                                          # an expired token reads as no identity
    out = box.run("may-stop", "--kind", "tmux", "--ref", "s1")
    assert out.exit_code == 3 and "cannot tell whether it is the owner" in out.output, out.output
    as_caller(monkeypatch, "i999")
    assert "cannot tell" not in box.run("may-stop", "--kind", "tmux", "--ref", "s1").output


# ── fix round 1: is_alive on the branches that were not tested ──────────────


def holder_on(host: str, pid: int) -> dict:
    return {"host": host, "pid": pid, "started_at": pid * 1000, "proc_start": "2026-11-17T08:00:00Z", "session_id": "s", "pane": None}


def test_a_holder_on_another_host_is_alive_while_its_lease_is_live_and_not_once_lapsed(box: Box) -> None:
    from nexus.identity.ops import is_alive

    live = box.engine.stage(kind="seat", name="far", state="live", generation=1, holder=holder_on("elsewhere", 500))
    lapsed = box.engine.stage(kind="seat", name="gone-far", state="lapsed", generation=1, holder=holder_on("elsewhere", 501))
    assert is_alive(box.env(), live) is True
    assert is_alive(box.env(), lapsed) is False, "a lapsed lease on another host is not alive"
    box.engine.put_work(lapsed, "tmux", "s9", "running")
    assert box.run("may-stop", "--kind", "tmux", "--ref", "s9").exit_code == 0, "its running row is stale"
    box.engine.put_work(live, "tmux", "s8", "running")
    assert box.run("may-stop", "--kind", "tmux", "--ref", "s8").exit_code == 3


def test_a_lapsed_holder_on_this_host_is_judged_by_its_process(box: Box) -> None:
    from nexus.identity.ops import is_alive

    i1 = box.running_seat("alpha", 100)
    box.engine.records[i1]["state"] = "lapsed"                            # the lease lapsed; the process still runs
    assert is_alive(box.env(), i1) is True
    crash(box, 100)
    assert is_alive(box.env(), i1) is False


def test_a_delegated_identity_is_judged_by_its_dispatcher_process(box: Box) -> None:
    from nexus.identity.ops import is_alive

    parent = box.running_seat("alpha", 100)
    box.procs.procs[777] = "2026-11-17T08:00:00Z"                          # the dispatcher: a running process with no registry file
    child = box.engine.stage(kind="delegated", state="live", generation=1, parent=parent, holder=holder_on(box.procs.host, 777))
    assert is_alive(box.env(), child) is True
    box.procs.procs.pop(777)
    assert is_alive(box.env(), child) is False, "the dispatcher is gone"
    box.engine.put_work(child, "labrun", "r", "running")
    assert box.run("may-stop", "--kind", "labrun", "--ref", "r").exit_code == 0


def test_the_may_stop_exit_statuses(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    """S7: 0 no live owner, 3 a live identity owns it, 1 anything else (the engine not answering, a bad ref).
    A caller outside the package keys on the 3 alone, so the number is pinned here."""
    from nexus.identity import work
    from nexus.identity.bind_client import IdentityUnavailableError

    assert work.EXIT_OWNED == 3

    i1 = box.running_seat("alpha", 100)
    box.engine.put_work(i1, "tmux", "owned", "running")
    assert box.run("may-stop", "--kind", "tmux", "--ref", "free").exit_code == 0
    owned = box.run("may-stop", "--kind", "tmux", "--ref", "owned")
    assert owned.exit_code == 3 and " is owned by " in owned.output

    def down(kind: str, ref: str):
        raise IdentityUnavailableError("connection refused")

    box.engine.work_owners = down                                      # type: ignore[method-assign]
    assert box.run("may-stop", "--kind", "tmux", "--ref", "owned").exit_code == 1, "an engine that does not answer is not an owner"
    assert box.run("may-stop", "--kind", "daemon", "--ref", "x").exit_code == 2, "a bad kind is a usage error"


def test_running_lists_no_gone_worktree_and_no_wait_from_an_earlier_session(box: Box, tmp_path: Path) -> None:
    """r1 items 3 and 4: ``list --running`` (the restart report and the checkpoint read it) leaves out a worktree whose
    directory is gone and a wait for a session the identity no longer holds; the full list marks both stale and says why."""
    import json

    i1 = box.running_seat("alpha", 100)                                  # its holder's session is sid-100
    live_wt, gone_wt = tmp_path / "live", tmp_path / "gone"
    live_wt.mkdir()
    for kind, ref in (("worktree", str(live_wt)), ("worktree", str(gone_wt)), ("wait", "sid-100"), ("wait", old_session())):
        box.engine.put_work(i1, kind, ref, "running")
    running = json.loads(box.run("work", "list", "i1", "--running", "--json").stdout)["work"]
    assert sorted((r["kind"], r["ref"]) for r in running) == [("wait", "sid-100"), ("worktree", str(live_wt))]
    full = box.run("work", "list", "i1")
    assert full.exit_code == 0
    assert f"{gone_wt}  (stale: the worktree is gone)" in full.output
    assert f"{old_session()}  (stale: a wait from an earlier session)" in full.output
    assert "sid-100  (stale" not in full.output


def test_the_limit_also_forgets_waits_from_an_earlier_session(box: Box) -> None:
    """A wait killed by SIGKILL never writes its end; once the identity holds another session, the limit's purge drops
    it and keeps the current session's wait (r1 item 3)."""
    from nexus.identity import work

    i1 = box.running_seat("alpha", 100)
    box.engine.put_work(i1, "wait", "sid-100", "running")
    for n in range(199):
        box.engine.put_work(i1, "wait", old_session(n), "running")
    assert work.register(box.engine, i1, "labrun", "fresh").state == "running"
    assert {k[2] for k in box.engine.work if k[0] == i1} == {"sid-100", "fresh"}


def test_no_current_session_judges_no_wait_superseded() -> None:
    from nexus.identity.work import wait_superseded

    assert wait_superseded({"kind": "wait", "ref": old_session()}, "s-new")
    assert not wait_superseded({"kind": "wait", "ref": old_session()}, None)
    assert not wait_superseded({"kind": "wait", "ref": old_session()}, old_session())
    assert not wait_superseded({"kind": "labrun", "ref": old_session()}, "s-new")
    for plain in ("develop-ci", "s-old", "pytest-full", "ci:develop@abc", "wait for peer"):
        assert not wait_superseded({"kind": "wait", "ref": plain}, "s-new"), f"{plain!r} names no session: it is a hand wait"


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGHUP"), reason="POSIX signals")
def test_work_run_holds_a_signal_sent_before_the_child_exists_and_never_forwards_sigint(monkeypatch) -> None:
    """r1 item 5 (the r2 N5 ordering): TERM and INT arrive while Popen is still starting the child. The TERM is held and
    sent once the child exists; the INT is caught and dropped, so this process lives on to mark the row done."""
    import signal

    from nexus.identity import work

    sent: list[int] = []

    class Child:
        def send_signal(self, signum: int) -> None:
            sent.append(signum)

        def wait(self) -> int:
            return 0

    def popen(argv):                                                     # the signals land before Popen returns
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGINT)
        return Child()

    setattr_in(monkeypatch, "nexus.identity.work", "subprocess.Popen", popen)
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    assert work._spawn(["true"]) == 0
    assert sent == [signal.SIGTERM], "TERM held and forwarded once the child existed; INT never forwarded"
    assert {s: signal.getsignal(s) for s in before} == before, "every handler is put back"


def test_a_command_that_cannot_be_executed_is_a_clean_error(monkeypatch) -> None:
    from nexus.identity import work
    from nexus.identity.ops import IdentityCommandError

    def popen(argv):
        raise PermissionError(13, "Permission denied")

    setattr_in(monkeypatch, "nexus.identity.work", "subprocess.Popen", popen)
    with pytest.raises(IdentityCommandError, match="not executable"):
        work._spawn(["./not-x"])


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGHUP"), reason="POSIX signals")
def test_work_run_under_nohup_keeps_the_hangup_ignored(monkeypatch) -> None:
    """``nohup nx identity work run ...``: HUP is SIG_IGN on entry. Installing the forwarder over it would make the wrapper
    pass a hangup on to a command nohup was protecting (confirmation N3)."""
    import signal

    from nexus.identity import work

    seen: dict[str, object] = {}

    class Child:
        def send_signal(self, signum: int) -> None:
            seen.setdefault("sent", []).append(signum)  # type: ignore[union-attr]

        def wait(self) -> int:
            return 0

    def popen(argv):
        seen["hup_during"] = signal.getsignal(signal.SIGHUP)
        return Child()

    setattr_in(monkeypatch, "nexus.identity.work", "subprocess.Popen", popen)
    before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        assert work._spawn(["true"]) == 0
        assert seen["hup_during"] == signal.SIG_IGN, "the forwarder was installed over nohup's ignore"
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, before)
