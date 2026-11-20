# SPDX-License-Identifier: AGPL-3.0-or-later
"""The liveness probe of a registered row, and the registrars that stay out of its way (RDR-253 Step 6 part 1, nexus-hczdo):
the ``agent`` kind, a hand wait's plain ref, ghost rows that predate their holder, the purge of stale rows, owner records, the
``bad_kind`` cache. Tenant-free like tests/identity/test_work_registry.py, which holds the rest of the work tests and the
fixtures this module repeats (what the real engine does with the same calls: tests/identity/test_work_engine_rows.py)."""
from __future__ import annotations

import os

import pytest

from tests.test_identity_cmd import Box


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def as_caller(monkeypatch: pytest.MonkeyPatch, identity: str | None) -> None:
    """The process under test holds *identity* (its token file), or none."""
    from nexus.identity import work

    monkeypatch.setattr(work, "caller_identity", lambda: identity)


#: The CLI resolves a worktree ref to its real path ("/p" is "C:\\p" on Windows).
P_REAL = os.path.realpath("/p")

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


