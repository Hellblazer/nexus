# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline self-test for the upgrade rehearsal's PCI keep check (nexus-f3yqd.5).

No engine, no PG, no network: PG is a scripted callable, time is a fake clock. The watcher, the
restart trigger, the engine-log reader, the judge and the check loop are pinned here, including the
case the check exists for (a restart that kills the in-flight build and rebuilds it under a new pid)
and the vacuous cases that must FAIL rather than pass (no build in flight, no readable engine log).
The write-rate probe, the version selection and the verdict are in test_upgrade_rehearsal_write_pci.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time as real_time
from pathlib import Path

DRIVER = Path(__file__).resolve().parent.parent / "scripts" / "upgrade_rehearsal.py"


def _load():
    spec = importlib.util.spec_from_file_location("_upgrade_rehearsal_pci", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_upgrade_rehearsal_pci"] = module
    spec.loader.exec_module(module)
    return module


ur = _load()

A = "pci_" + "a" * 24
B = "pci_" + "b" * 24
C = "pci_" + "c" * 24


# --- the named constants -------------------------------------------------------------------


def test_the_index_name_pattern_matches_pci_indexes_only() -> None:
    assert ur.PCI_INDEX_RE.match(A)
    assert ur.PCI_INDEX_RE.match("nexus." + B)
    for bad in (
        "pci_" + "a" * 23,
        "pci_" + "a" * 25,
        "pci_" + "g" * 24,
        "xpci_" + "a" * 24,
        "nexus.pci_" + "a" * 24 + "_x",
        "other." + A,
        "hnsw_" + "a" * 24,
    ):
        assert not ur.PCI_INDEX_RE.match(bad), bad


def test_the_failure_events_are_the_three_named_strings() -> None:
    assert ur.PCI_FAILURE_EVENTS == {
        "pci_index_dropped": ("event=pci_index_dropped", "reason=failed_build"),
        "pci_index_build_not_done": (
            "event=pci_index_build_not_done",
            "outcome=CONNECTION_LOST",
        ),
        "pci_ddl_failed": ("event=pci_ddl_failed", "sqlstate=57P01"),
    }
    assert ur.EVENT_BUILDERS_KEPT == (
        "event=pci_builders_kept",
        "reason=only_run_always_pending",
    )
    assert ur.EVENT_INDEX_ADOPTED == ("event=pci_anchor_adopted",)


def test_pre_pci_is_below_7_77_0() -> None:
    assert ur.PRE_PCI_VERSION == "7.76.0"
    assert ur.PCI_FIRST_VERSION == "7.77.0"
    assert ur.is_pre_pci("7.76.0")
    assert ur.is_pre_pci("7.5.0")
    assert not ur.is_pre_pci("7.77.0")
    assert not ur.is_pre_pci("7.99.0")
    assert not ur.is_pre_pci("8.0.0.post1")


# --- progress rows and the watcher ---------------------------------------------------------------


def test_parse_progress_keeps_pci_rows_only_and_strips_the_schema() -> None:
    rows = [
        ["nexus." + A, "101", "building index: scanning table", "5", "10", "50", "100"],
        [B, "102", "building index: loading tuples in tree", "10", "10", "99", "100"],
        ["nexus.hnsw_leaf_1", "103", "x", "0", "0", "0", "0"],
        ["-", "104", "initializing", "0", "0", "0", "0"],
    ]
    obs = ur.parse_progress(rows, t=3.0)
    assert [(o.index, o.pid, o.t) for o in obs] == [(A, 101, 3.0), (B, 102, 3.0)]
    assert obs[0].phase.startswith("building index: scanning")
    assert (obs[0].blocks_done, obs[0].blocks_total) == (5, 10)
    assert (obs[0].tuples_done, obs[0].tuples_total) == (50, 100)


def test_parse_validity_reads_t_and_f() -> None:
    assert ur.parse_validity([[A, "t"], [B, "f"], ["not_pci", "t"]]) == {
        A: True,
        B: False,
    }


class FakeCluster:
    """A scripted PG: ``builds`` is a list of (index, pid, start, end); ``valid_at`` says when each
    index turns valid. ``sql`` answers the two queries the check issues."""

    def __init__(self, builds, valid_at):
        self.now = 0.0
        self.builds = builds
        self.valid_at = valid_at
        self.down = False

    def clock(self) -> float:
        return self.now

    def sql(self, query: str):
        if self.down:
            return None
        if "pg_stat_progress_create_index" in query:
            return [
                [
                    "nexus." + i,
                    str(pid),
                    "building index: loading tuples in tree",
                    "5",
                    "10",
                    "50",
                    "100",
                ]
                for i, pid, s, e in self.builds
                if s <= self.now < e
            ]
        if "indisvalid" in query:
            return [
                [i, "t" if self.now >= v else "f"]
                for i, v in self.valid_at.items()
                if v is not None and (self.now >= v or self.started(i))
            ]
        raise AssertionError(query)

    def started(self, index: str) -> bool:
        return any(i == index and s <= self.now for i, _, s, _ in self.builds)


class FakeLog:
    def __init__(self, lines=None, readable=True):
        self.lines = lines or []
        self.readable = readable
        self.marked_at = None

    def mark(self):
        self.marked_at = "mark"
        return {"m": 1}

    def lines_since(self, marks):
        assert marks == {"m": 1}, (
            "the log must be read from the mark taken at the restart"
        )
        return (
            list(self.lines),
            ["storage_service_native.log"] if self.readable else [],
        )


def _outcome(ok: bool = True):
    steps = [
        {
            "name": f"nx daemon service {sub}",
            "status": "ok",
            "exit_code": 0,
            "wall_s": 1.0,
            "reason": "",
            "tail": "",
        }
        for sub in ("stop", "start")
    ]
    return ur.RestartOutcome(None, steps)


def _run(
    cluster,
    *,
    expected,
    log=None,
    restart=None,
    end=900.0,
    upgrade_end=100.0,
    restart_takes=0.0,
):
    """nx upgrade runs 0..upgrade_end on the fake clock with the watcher polling every 0.5 s and the
    trigger looking after each poll; then the post phase (the check) runs, as after the search step."""
    w = ur.PciWatcher(cluster.sql, clock=cluster.clock)
    log = log or FakeLog()
    restarted: dict = {}

    def default_restart():
        restarted["t"] = cluster.now
        restarted["upgrade_running"] = cluster.now < upgrade_end
        t_end = cluster.now + restart_takes
        while cluster.now < t_end:
            cluster.now += 0.5
            w.poll_once()
        return _outcome()

    trig = ur.PciRestartTrigger(
        w,
        restart or default_restart,
        lambda: cluster.now < upgrade_end,
        log,
        clock=cluster.clock,
    )
    while cluster.now < upgrade_end:
        cluster.now += 0.5
        w.poll_once()
        trig.poll_once()
    trig.poll_once()  # nx upgrade has returned
    while (
        cluster.now < upgrade_end + 60.0
    ):  # doctor and search: the watcher thread keeps polling
        cluster.now += 0.5
        w.poll_once()

    def sleep(dt: float) -> None:
        cluster.now += dt
        w.poll_once()

    rep = ur.run_pci_check(
        watcher=w,
        sql=cluster.sql,
        record=trig.record,
        engine_log=log,
        expected=expected,
        end_bound_s=end,
        clock=cluster.clock,
        sleep=sleep,
    )
    return rep, restarted, trig


def _check(cluster, **kw):
    rep, restarted, _ = _run(cluster, **kw)
    return rep, restarted


def test_kept_build_passes_and_records_the_green_signs() -> None:
    # A builds 10..40 under pid 11, B 40..70 under pid 12; restart lands while A builds.
    cluster = FakeCluster(
        [(A, 11, 10.0, 40.0), (B, 12, 40.0, 70.0)], {A: 40.0, B: 70.0}
    )
    log = FakeLog(
        [
            "x event=pci_builders_kept reason=only_run_always_pending n=1",
            "x event=pci_anchor_adopted reason=missing index="
            + A
            + " leaf=l rows=27001 bytes=1",
        ]
    )
    rep, restarted = _check(cluster, expected=2, log=log)
    assert rep.passed, rep.findings
    ev = rep.evidence
    assert ev["in_flight_at_restart"][0]["index"] == A
    assert ev["in_flight_at_restart"][0]["pid"] == 11
    assert ev["builders"] == {A: [11], B: [12]}
    assert ev["valid_at_end"] == sorted([A, B])
    assert ev["log"]["kept"] and ev["log"]["adopted"]
    assert ev["log"]["failure_events"] == []
    assert ev["restart_s"] == restarted["t"]
    json.dumps(ev)  # evidence goes into verdict.json


def test_a_rebuilt_build_fails_and_names_the_pids_and_the_dropped_event() -> None:
    # The restart kills A (pid 11) at the restart (t~10.5); a sweep rebuilds it as pid 99 at 400..430.
    cluster = FakeCluster([(A, 11, 10.0, 10.6), (A, 99, 400.0, 430.0)], {A: 430.0})
    log = FakeLog(["x event=pci_index_dropped index=" + A + " reason=failed_build"])
    rep, _ = _check(cluster, expected=1, log=log)
    assert not rep.passed
    text = " | ".join(rep.findings)
    assert A in text and "11" in text and "99" in text
    assert "rebuilt" in text, "the in-flight build's own finding must name the rebuild"
    assert "pci_index_dropped" in text
    assert rep.evidence["builders"][A] == [11, 99]


def test_a_connection_lost_build_and_a_57p01_ddl_failure_are_findings() -> None:
    cluster = FakeCluster([(A, 11, 10.0, 40.0)], {A: 40.0})
    log = FakeLog(
        [
            "x event=pci_index_build_not_done index=A outcome=CONNECTION_LOST",
            "x event=pci_ddl_failed index=A sqlstate=57P01",
            "x event=pci_ddl_failed index=A sqlstate=42P07",
        ]
    )
    rep, _ = _check(cluster, expected=1, log=log)
    assert not rep.passed
    text = " ".join(rep.findings)
    assert "pci_index_build_not_done" in text and "pci_ddl_failed" in text
    assert "42P07" not in text, "only sqlstate=57P01 is the restart's signature"


def test_no_build_ever_in_flight_is_a_vacuous_failure_not_a_pass() -> None:
    cluster = FakeCluster([], {})
    rep, restarted = _check(cluster, expected=0)
    assert not rep.passed
    assert restarted == {}, "nothing was in flight: the engine must not be restarted"
    assert any("no pci_ index build" in f and "nx upgrade" in f for f in rep.findings)
    assert rep.evidence["in_flight_at_restart"] == []


def test_builds_that_finished_before_the_trigger_could_see_them_say_so() -> None:
    cluster = FakeCluster([(A, 11, 0.0, 0.2)], {A: 0.2})
    rep, restarted = _check(cluster, expected=1)
    assert not rep.passed and restarted == {}
    assert any("pci-collections" in f for f in rep.findings)


def test_a_build_seen_only_after_nx_upgrade_returned_does_not_count_and_is_named() -> (
    None
):
    # The oddity of the 2026-11-10 run: the check said "never seen in flight" while listing builders.
    # A build that starts after nx upgrade ended is not a restart-during-upgrade proof: vacuous, no
    # restart, and the message says the builds WERE seen, after nx upgrade.
    cluster = FakeCluster([(A, 11, 120.0, 150.0)], {A: 150.0})
    rep, restarted, trig = _run(cluster, expected=1, upgrade_end=100.0)
    assert not rep.passed and restarted == {} and trig.record is None
    text = " ".join(rep.findings)
    assert "while nx upgrade was running" in text and A in text
    assert "after it returned" in text
    assert rep.evidence["builders"] == {A: [11]}
    lines = " ".join(ur.format_pci(rep.evidence))
    assert "no pci_ build was in flight while nx upgrade ran" in lines
    assert "was ever seen" not in lines and "never seen" not in lines


def test_an_index_that_never_turns_valid_fails_at_the_bound() -> None:
    cluster = FakeCluster([(A, 11, 10.0, 40.0)], {A: None})
    rep, _ = _check(cluster, expected=1, end=60.0)
    assert not rep.passed
    assert any("valid" in f for f in rep.findings)
    assert rep.evidence["end_state_reached"] is False


def test_too_few_valid_indexes_at_the_end_is_a_failure() -> None:
    cluster = FakeCluster([(A, 11, 10.0, 40.0)], {A: 40.0})
    rep, _ = _check(cluster, expected=3, end=30.0)
    assert not rep.passed
    assert any("3" in f and "valid" in f for f in rep.findings)


def test_an_index_built_by_two_pids_fails_even_when_it_ends_valid() -> None:
    cluster = FakeCluster(
        [(A, 11, 10.0, 40.0), (B, 12, 40.0, 50.0), (B, 13, 60.0, 70.0)],
        {A: 40.0, B: 70.0},
    )
    rep, _ = _check(cluster, expected=2)
    assert not rep.passed
    assert any(B in f and "2 pids" in f for f in rep.findings)


def test_an_unreadable_engine_log_is_a_finding_not_a_clean_scan() -> None:
    # A kept build, an adoption line, every index valid: the ONLY thing wrong is that no engine log
    # file was readable. Deleting the `if not log_files` rule makes this pass.
    cluster = FakeCluster([(A, 11, 10.0, 40.0)], {A: 40.0})
    rep, _ = _check(cluster, expected=1, log=FakeLog([ADOPT_A], readable=False))
    assert not rep.passed
    assert len(rep.findings) == 1 and "no engine log was readable" in rep.findings[0]


def test_a_failed_restart_is_a_finding_and_stops_the_check() -> None:
    cluster = FakeCluster([(A, 11, 10.0, 40.0)], {A: 40.0})
    rep, _ = _check(
        cluster,
        expected=1,
        restart=lambda: ur.RestartOutcome(
            "nx daemon service stop failed: exit 1",
            [{"name": "nx daemon service stop", "status": "failed", "exit_code": 1}],
        ),
    )
    assert not rep.passed
    assert any("restart" in f and "exit 1" in f for f in rep.findings)
    assert "end_state_reached" not in rep.evidence, "a failed restart ends the check"
    assert rep.evidence["restart"]["steps"][0]["exit_code"] == 1


def test_a_build_still_running_under_its_original_pid_at_the_bound_is_named() -> None:
    cluster = FakeCluster([(A, 11, 10.0, 10_000.0)], {A: None})
    rep, _ = _check(cluster, expected=1, end=40.0)
    assert not rep.passed
    text = " ".join(rep.findings)
    assert "still building" in text and "11" in text


# --- the restart trigger: the second shell, during nx upgrade --------------------------------------------


def test_the_restart_fires_on_the_first_sighting_while_nx_upgrade_is_still_running() -> (
    None
):
    # A builds 10..40 (pid 11), B 40..70 (pid 12); nx upgrade runs to t=100.
    cluster = FakeCluster(
        [(A, 11, 10.0, 40.0), (B, 12, 40.0, 70.0)], {A: 40.0, B: 70.0}
    )
    rep, restarted, trig = _run(cluster, expected=2, restart_takes=5.0)
    assert restarted["upgrade_running"] is True, (
        "the restart began while nx upgrade ran"
    )
    assert 10.0 <= restarted["t"] <= 10.6, "on the first sighting, not later"
    rec = trig.record
    assert rec is not None and trig.finished
    assert [(o.index, o.pid) for o in rec.in_flight] == [(A, 11)]
    assert rec.upgrade_running_at_start is True
    assert rec.upgrade_running_after is True, (
        "nx upgrade is still going when the restart ends"
    )
    assert rec.outcome is not None and rec.outcome.error is None
    assert [x["name"] for x in rec.outcome.steps] == [
        "nx daemon service stop",
        "nx daemon service start",
    ]
    ev = rep.evidence
    assert ev["restart"]["upgrade_running_at_start"] is True
    assert ev["restart"]["upgrade_running_after"] is True
    assert [x["exit_code"] for x in ev["restart"]["steps"]] == [0, 0]
    assert ev["in_flight_at_restart"][0]["index"] == A
    json.dumps(ev)


def test_the_restart_fires_once_even_though_a_later_build_is_also_in_flight() -> None:
    cluster = FakeCluster(
        [(A, 11, 10.0, 40.0), (B, 12, 40.0, 70.0)], {A: 40.0, B: 70.0}
    )
    calls = []

    def once():
        calls.append(cluster.now)
        return _outcome()

    _run(cluster, expected=2, restart=once)
    assert len(calls) == 1


def test_a_restart_never_starts_after_nx_upgrade_has_returned() -> None:
    cluster = FakeCluster([(A, 11, 120.0, 150.0)], {A: 150.0})
    calls = []
    rep, _, trig = _run(
        cluster,
        expected=1,
        upgrade_end=100.0,
        restart=lambda: calls.append(1) or _outcome(),
    )
    assert calls == [] and trig.record is None and trig.finished
    assert not rep.passed


def test_a_nx_upgrade_that_ends_during_the_restart_is_recorded_not_hidden() -> None:
    cluster = FakeCluster([(A, 11, 95.0, 130.0)], {A: 130.0})
    rep, restarted, trig = _run(
        cluster, expected=1, upgrade_end=100.0, restart_takes=10.0
    )
    assert restarted["upgrade_running"] is True
    assert trig.record.upgrade_running_after is False
    assert rep.evidence["restart"]["upgrade_running_after"] is False


def test_a_restart_that_raises_is_a_finding_not_a_crash() -> None:
    cluster = FakeCluster([(A, 11, 10.0, 40.0)], {A: 40.0})

    def boom():
        raise OSError("no such nx")

    rep, _, trig = _run(cluster, expected=1, restart=boom)
    assert not rep.passed
    assert any("OSError" in f and "no such nx" in f for f in rep.findings)


def test_the_trigger_thread_restarts_on_a_sighting_and_stops_cleanly() -> None:
    rows = {"v": []}

    def sql(q: str):
        return rows["v"]

    w = ur.PciWatcher(sql, poll_s=0.01)
    running = threading.Event()
    running.set()
    fired = threading.Event()

    def restart():
        assert running.is_set(), "nx upgrade must still be running at the restart"
        fired.set()
        return _outcome()

    trig = ur.PciRestartTrigger(w, restart, running.is_set, FakeLog(), poll_s=0.01)
    w.start()
    trig.start()
    real_time.sleep(0.1)
    assert not fired.is_set(), "no build in flight: no restart"
    rows["v"] = [["nexus." + A, "11", "building index", "1", "2", "0", "0"]]
    assert fired.wait(5.0)
    assert trig.join(5.0)
    assert trig.record is not None and trig.record.in_flight[0].pid == 11
    w.stop()

    # a trigger that never sees a build ends when nx upgrade does
    w2 = ur.PciWatcher(lambda q: [], poll_s=0.01)
    up = threading.Event()
    up.set()
    t2 = ur.PciRestartTrigger(w2, restart, up.is_set, FakeLog(), poll_s=0.01)
    t2.start()
    up.clear()
    assert t2.join(5.0) and t2.record is None and t2.finished


def _chunks(tmp_path: Path, rows) -> Path:
    f = tmp_path / "upgrade.chunks.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return f


def test_output_after_keeps_what_the_step_printed_from_the_restart_on(
    tmp_path: Path,
) -> None:
    f = _chunks(
        tmp_path,
        [
            {"t": 1.0, "n": 9, "s": "before"},
            {"t": 20.0, "n": 30, "s": "\x1b[2mWaiting for engine\x1b[0m\r"},
            {"t": 21.0, "n": 30, "s": "\x1b[2mWaiting for engine\x1b[0m\r"},
            {"t": 30.0, "n": 9, "s": "Done."},
        ],
    )
    assert ur.output_after(f, 10.0) == ["Waiting for engine", "Done."]
    assert ur.output_after(tmp_path / "missing.jsonl", 0.0) == []


def test_upgrade_after_restart_says_what_nx_upgrade_did_next(tmp_path: Path) -> None:
    f = _chunks(tmp_path, [{"t": 50.0, "n": 5, "s": "step 2 failed"}])
    step = ur.StepResult(name="upgrade", status="failed", exit_code=1, wall_s=60.0)
    info = ur.upgrade_after_restart(step, f, 40.0, 48.0)
    assert info["status"] == "failed" and info["exit_code"] == 1
    assert (
        info["ran_on_after_restart_s"] == 12.0 and info["ended_during_restart"] is False
    )
    assert info["output_after_restart"] == ["step 2 failed"]
    line = " ".join(
        ur.format_pci(
            {
                "in_flight_at_restart": [],
                "nx_upgrade": info,
                "restart": {},
                "restarted_s": 48.0,
                "expected_pci_indexes": 1,
            }
        )
    )
    assert "nx upgrade after the restart" in line and "exit 1" in line
    assert "step 2 failed" in line


def test_the_watcher_records_a_pid_for_every_poll_and_survives_pg_going_down() -> None:
    cluster = FakeCluster([(A, 11, 0.0, 10.0)], {A: 10.0})
    w = ur.PciWatcher(cluster.sql, clock=cluster.clock)
    w.poll_once()
    assert [o.pid for o in w.latest()] == [11]
    cluster.down = True
    assert w.poll_once() == []
    assert w.latest() == []
    assert len(w.history()) == 1


def test_the_watcher_swallows_a_probe_error_and_counts_it() -> None:
    def boom(q: str):
        raise ur.ProbeError("psql failed: x")

    w = ur.PciWatcher(boom, clock=lambda: 0.0)
    assert w.poll_once() == []
    assert w.errors == 1


# --- the engine log reader --------------------------------------------------------------------------


def _logs(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    return d


def test_engine_log_reads_only_what_came_after_the_mark(tmp_path: Path) -> None:
    d = _logs(tmp_path)
    f = d / "storage_service_native.log"
    f.write_text("old line event=pci_index_dropped reason=failed_build\n")
    log = ur.EngineLog(tmp_path)
    marks = log.mark()
    with f.open("a") as fh:
        fh.write("new line one\nnew line two\n")
    lines, files = log.lines_since(marks)
    assert [ln.strip() for ln in lines] == ["new line one", "new line two"]
    assert files == ["storage_service_native.log"]


def test_engine_log_follows_a_rotation(tmp_path: Path) -> None:
    d = _logs(tmp_path)
    f = d / "storage_service_native.log"
    f.write_text("pre-restart filler line\n")
    log = ur.EngineLog(tmp_path)
    marks = log.mark()
    # the restart rotates: the old file becomes .1 and keeps growing a little, a new file starts
    (d / "storage_service_native.log.1").write_text(
        "pre-restart filler line\nshutdown event=pci_index_dropped\n"
    )
    f.write_text("fresh boot line\n")
    lines, _ = log.lines_since(marks)
    assert any("shutdown event=pci_index_dropped" in ln for ln in lines)
    assert any("fresh boot line" in ln for ln in lines)
    assert not any(ln.startswith("pre-restart filler") for ln in lines)


def test_engine_log_reads_the_jar_stem_too_and_reports_no_file_when_absent(
    tmp_path: Path,
) -> None:
    d = _logs(tmp_path)
    log = ur.EngineLog(tmp_path)
    assert log.lines_since(log.mark()) == ([], [])
    marks = log.mark()
    (d / "storage_service_jar.log").write_text("jar boot line\n")
    lines, files = log.lines_since(marks)
    assert lines and files == ["storage_service_jar.log"]


ADOPT_A = (
    "x event=pci_anchor_adopted reason=missing index="
    + A
    + " leaf=l rows=27001 bytes=1"
)


def test_a_build_that_left_the_progress_view_with_no_adoption_line_is_kept_then_terminated() -> (
    None
):
    # same pid before and after the restart, gone from the view at 13 s, index valid anyway (e.g. a
    # sweep rebuilt nothing visible): pid continuity alone must NOT pass it
    cluster = FakeCluster([(A, 11, 10.0, 13.0)], {A: 14.0})
    rep, _ = _check(cluster, expected=1, log=FakeLog([]))
    assert not rep.passed
    text = " ".join(rep.findings)
    assert "no adoption" in text or "carries no event=pci_anchor_adopted" in text
    assert "kept, then terminated" in text


def test_an_adopted_build_that_ends_invalid_fails() -> None:
    cluster = FakeCluster([(A, 11, 10.0, 13.0)], {A: None})
    rep, _ = _check(cluster, expected=1, log=FakeLog([ADOPT_A]), end=30.0)
    assert not rep.passed
    assert any(A in f and "not valid at the end" in f for f in rep.findings)


def test_an_adoption_line_for_another_index_does_not_keep_this_one() -> None:
    cluster = FakeCluster([(A, 11, 10.0, 40.0)], {A: 40.0})
    other = (
        "x event=pci_anchor_adopted reason=missing index="
        + B
        + " leaf=l rows=1 bytes=1"
    )
    rep, _ = _check(cluster, expected=1, log=FakeLog([other]))
    assert not rep.passed
    assert any("kept, then terminated" in f for f in rep.findings)


def test_the_adoption_line_is_tied_to_the_build_by_the_index_field() -> None:
    lines = [
        "event=pci_anchor_adopted reason=missing index=" + A + " leaf=l",
        "event=pci_index_built index=" + B,  # not an adoption
        "event=pci_anchor_adopted reason=missing leaf=" + C,  # no index= field
    ]
    assert ur.adopted_indexes(lines) == {A}


def test_a_build_not_seen_again_after_the_restart_fails_even_with_an_adoption_line() -> (
    None
):
    cluster = FakeCluster([(A, 11, 9.8, 10.2)], {A: 12.0})
    rep, _ = _check(cluster, expected=1, log=FakeLog([ADOPT_A]))
    assert not rep.passed
    assert any("not seen building again" in f for f in rep.findings)


def test_seen_after_the_restart_is_anchored_at_its_finish_not_its_start() -> None:
    # The restart takes 5 s (fires at ~10.5, finishes at ~15.5). A is seen at 10.5-13 under pid 11, all
    # of it DURING the restart, and is gone from the view before the restart is over. Anchored at the
    # restart's start those sightings count as "after the restart" and the adoption line passes it.
    cluster = FakeCluster([(A, 11, 10.0, 13.0)], {A: 14.0})
    rep, _ = _check(cluster, expected=1, log=FakeLog([ADOPT_A]), restart_takes=5.0)
    assert not rep.passed
    assert any("not seen building again" in f for f in rep.findings), rep.findings
    assert rep.evidence["restarted_s"] > rep.evidence["restart_s"] + 4.0


def test_a_build_seen_after_the_restart_finished_still_passes() -> None:
    cluster = FakeCluster([(A, 11, 10.0, 40.0)], {A: 40.0})
    rep, _ = _check(cluster, expected=1, log=FakeLog([ADOPT_A]), restart_takes=5.0)
    assert rep.passed, rep.findings


def test_a_watcher_whose_probe_kept_failing_leaves_a_pid_history_nobody_can_trust() -> (
    None
):
    cluster = FakeCluster([(A, 11, 10.0, 40.0)], {A: 40.0})
    log = FakeLog([ADOPT_A])

    def run(errors: int):
        w = ur.PciWatcher(cluster.sql, clock=cluster.clock)
        cluster.now = 0.0
        trig_rec = ur.RestartRecord(
            in_flight=[ur.PciObs(10.5, A, 11, "p", 1, 2, 3, 4)],
            t_s=10.5,
            at_clock=10.5,
            upgrade_running_at_start=True,
            marks={"m": 1},
            outcome=_outcome(),
            restarted_s=10.5,
            done_clock=10.5,
        )
        for _ in range(
            60
        ):  # sightings from 0.5 to 30 s: the build is in view until 40 s
            cluster.now += 0.5
            w.poll_once()
        cluster.now = 45.0
        w.poll_once()  # the build is gone from the view
        w.errors = errors
        return ur.run_pci_check(
            watcher=w,
            sql=cluster.sql,
            record=trig_rec,
            engine_log=log,
            expected=1,
            end_bound_s=0.0,
            clock=cluster.clock,
            sleep=lambda dt: None,
        )

    assert run(ur.PCI_PROBE_ERROR_LIMIT).passed
    rep = run(ur.PCI_PROBE_ERROR_LIMIT + 1)
    assert not rep.passed
    assert any("PG probe failed" in f for f in rep.findings)
    assert rep.evidence["watcher_probe_errors"] == ur.PCI_PROBE_ERROR_LIMIT + 1
