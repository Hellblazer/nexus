# SPDX-License-Identifier: AGPL-3.0-or-later
"""The red-run report counts job timeouts and cancels: runs that ended timed_out or cancelled, and failed runs that carry a job past its timeout (RDR-258 Phase 1 step 4, nexus-2jomg.2).

``module_seam`` guards a fake that blocks a teardown until the job outlives its ``timeout-minutes``: the run does not
fail, it ends ``timed_out`` (or ``cancelled`` with a log that says the job exceeded the maximum execution time). A report
scoped to ``failure`` cannot see one, so demoting the check to report mode would hide what it guards. These tests build a
small snapshot with each shape and check the extraction records them, the counts, the report text, and that the red-run
figures (the default scope) do not move.

No network and no tenant fixture: files in ``tmp_path`` and the ``history`` repo of ``tests/_red_run_fixtures.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests._red_run_fixtures import _extract, _job, _run, history, snapshot  # noqa: F401

import red_run_report as rrr
import red_run_stuck as stuck

TIMEOUT_LINE = "2026-10-01T10:30:00.0Z ##[error]The job running on runner GitHub Actions 1 has exceeded the maximum execution time of 35 minutes.\n"
SUPERSEDED_LINE = "2026-10-01T10:30:00.0Z ##[error]The operation was canceled.\n"


def _snapshot(tmp_path: Path, runs: list[dict], jobs: dict[int, list[dict]], logs: dict[tuple[int, int], str]) -> Path:
    snap = tmp_path / "stuck-snap"
    (snap / "jobs").mkdir(parents=True)
    (snap / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runs))
    for rid, js in jobs.items():
        (snap / "jobs" / f"{rid}.json").write_text(json.dumps(js))
    for (rid, jid), text in logs.items():
        (snap / "logs" / str(rid)).mkdir(parents=True, exist_ok=True)
        (snap / "logs" / str(rid) / f"{jid}.log").write_text(text)
    return snap


def _stuck_snapshot(tmp_path: Path, sha: str) -> Path:
    shard = "pytest (Python 3.12, shard 2/6)"
    lint = "pytest (lint markers, group 1/2)"
    runs = [
        _run(1, "CI", sha, "timed_out", 1),            # a job past timeout-minutes: the REST API's timed_out
        _run(2, "CI", sha, "cancelled", 2),            # a cancelled job whose log says it exceeded the maximum time
        _run(3, "CI", sha, "cancelled", 3),            # superseded by a newer push: not a hang
        _run(4, "CI", sha, "cancelled", 4),            # no jobs file in the snapshot
        _run(5, "CI", sha, "success", 5),
        _run(6, "CI", sha, "failure", 6),              # an ordinary red run, to show the default scope does not move
    ]
    jobs = {
        1: [_job(101, lint, [("Run lint-marked tests", "cancelled")], conclusion="timed_out")],
        2: [_job(201, shard, [("Run tests", "cancelled")], conclusion="cancelled"),
            _job(202, "changes", [("Detect", "success")], conclusion="success")],
        3: [_job(301, shard, [("Run tests", "cancelled")], conclusion="cancelled")],
        6: [_job(601, shard, [("Run tests (shard 2/6)", "failure")])],
    }
    logs = {(2, 201): "2026-10-01T10:00:00.0Z running\n" + TIMEOUT_LINE, (3, 301): SUPERSEDED_LINE,
            (6, 601): "2026-10-01T10:00:00.2Z FAILED tests/test_a.py::test_x - AssertionError: boom\n"}
    return _snapshot(tmp_path, runs, jobs, logs)


def test_a_timed_out_run_and_a_cancelled_job_that_hit_its_timeout_are_recorded(tmp_path, history):  # noqa: F811
    snap = _stuck_snapshot(tmp_path, history[1]["c1"])
    rows = [r for r in _extract(snap, history[0], green_jobs=None) if r["t"] == "stuck"]
    assert [(r["run"], r["conclusion"], r["timeout_jobs"]) for r in rows] == [
        (1, "timed_out", ["pytest (lint markers, group 1/2)"]),
        (2, "cancelled", ["pytest (Python 3.12, shard 2/6)"]),
        (3, "cancelled", []),
        (4, "cancelled", [])]
    assert rows[1]["jobs"] == ["pytest (Python 3.12, shard 2/6)"], "only the jobs that ended that way, not the one that passed"
    assert [r["jobs_file"] for r in rows] == [True, True, True, False]
    assert all(r["t"] == "stuck" and r["sha"] == history[1]["c1"] and r["workflow"] == "CI" for r in rows)


def test_the_meta_counts_each_kind_and_the_runs_that_hit_a_timeout(tmp_path, history):  # noqa: F811
    meta = _extract(_stuck_snapshot(tmp_path, history[1]["c1"]), history[0], green_jobs=None)[0]
    assert meta["stuck"] == {
        "timed_out_runs": 1, "cancelled_runs": 3, "failure_runs_with_timeout_job": 0, "runs_with_timeout_job": 2,
        "cancelled_job_no_log": 0, "runs_without_jobs_file": 1}


def test_the_red_run_figures_do_not_move(tmp_path, history):  # noqa: F811
    """The default scope is still ``failure``: a stuck run is counted beside the red runs, never among them."""
    records = _extract(_stuck_snapshot(tmp_path, history[1]["c1"]), history[0], green_jobs=None)
    assert [r["id"] for r in records if r["t"] == "run"] == [6]
    assert records[0]["red_runs"] == 1 and records[0]["conclusions"] == ["failure"]
    assert [(r["id"], r["c"]) for r in records if r["t"] == "decided"] == [(5, "s"), (6, "f")], "the decided runs are unchanged"


def test_the_report_prints_the_counts_and_names_the_job_that_hit_its_timeout(tmp_path, history):  # noqa: F811
    an = rrr.analyze(_extract(_stuck_snapshot(tmp_path, history[1]["c1"]), history[0], green_jobs=None))
    text = rrr.render_report(an)
    assert "runs that ended timed_out 1, cancelled 3, and failed runs carrying a job that hit its timeout 0; 2 runs in all hit a job timeout" in text
    assert "the other 2 timed_out or cancelled runs showed no job timeout in the logs we have" in text
    assert "run 1 CI " in text and "pytest (lint markers, group 1/2)" in text
    assert "run 2 CI " in text and "pytest (Python 3.12, shard 2/6)" in text
    assert "run 3 CI " not in text, "a superseded run is counted and not listed"
    assert "1 of these runs have no jobs file in the snapshot" in text
    assert "shows here (its job is cancelled, its run usually failed)" in text


def test_a_hung_run_with_no_failure_at_all_still_shows(tmp_path, history):  # noqa: F811
    """The shape module_seam guards: nothing fails, one job outlives its timeout."""
    runs = [_run(1, "CI", history[1]["c1"], "timed_out", 1)]
    jobs = {1: [_job(101, "pytest (Python 3.12, shard 1/6)", [("Run tests", "cancelled")], conclusion="timed_out")]}
    records = _extract(_snapshot(tmp_path, runs, jobs, {}), history[0], green_jobs=None)
    assert records[0]["red_runs"] == 0 and records[0]["stuck"]["runs_with_timeout_job"] == 1
    text = rrr.render_report(rrr.analyze(records))
    assert "red runs: 0" in text and "timed_out 1, cancelled 0, and failed runs carrying a job that hit its timeout 0; 1 runs in all hit a job timeout" in text


def test_an_extraction_from_before_the_counts_says_so(tmp_path):
    text = rrr.render_report(rrr.analyze([{"t": "meta", "window": ["a", "b"], "conclusions": ["failure"]}]))
    assert "job timeouts and cancels: not recorded in this extraction (extract again from the snapshot)" in text


def test_the_list_is_cut_but_the_counts_cover_every_run(tmp_path):
    rows = [{"t": "stuck", "run": n, "sha": "a" * 40, "workflow": "CI", "conclusion": "timed_out", "event": "push",
             "created": "2026-10-01T10:00:00Z", "ended": "2026-10-01T10:35:00Z", "jobs": ["j"], "timeout_jobs": ["j"],
             "jobs_file": True} for n in range(stuck.LIST_MAX + 5)]
    meta = {"stuck": stuck.summarize(rows)}
    lines = stuck.render(meta, rows)
    assert f"timed_out {stuck.LIST_MAX + 5}" in lines[0]
    assert sum(1 for ln in lines if ln.startswith("  run ")) == stuck.LIST_MAX and lines[-1] == "  ... 5 more"


def test_the_timeout_message_is_found_in_a_log_and_a_missing_log_is_not_a_timeout(tmp_path):
    hit, miss = tmp_path / "hit.log", tmp_path / "miss.log"
    hit.write_text("a\n" + TIMEOUT_LINE)
    miss.write_text("a\n" + SUPERSEDED_LINE)
    assert stuck.log_has_timeout(hit) and not stuck.log_has_timeout(miss) and not stuck.log_has_timeout(tmp_path / "none.log")


def test_the_extraction_roundtrips_through_the_cli_report(tmp_path, history, capsys):  # noqa: F811
    out = tmp_path / "x.jsonl.gz"
    rrr.write_extraction(_extract(_stuck_snapshot(tmp_path, history[1]["c1"]), history[0], green_jobs=None), out)
    assert rrr.main(["report", "--extraction", str(out)]) == rrr.EXIT_OK
    assert "runs that ended timed_out 1, cancelled 3, and failed runs carrying a job that hit its timeout 0; 2 runs in all" in capsys.readouterr().out


# -- the real shape: the job is cancelled, the run around it failed (job 119003409658 of run 39531650989) -------------------


def _failed_run_snapshot(tmp_path: Path, sha: str) -> Path:
    shard = "pytest (Python 3.12, shard 2/6)"
    lint = "pytest (lint markers, group 1/2)"
    runs = [
        _run(1, "CI", sha, "failure", 1),      # a job outlived its timeout-minutes: the job is cancelled, the gate then failed
        _run(2, "CI", sha, "failure", 2),      # an ordinary red run whose sibling shard was cancelled when it failed: not a hang
        _run(3, "CI", sha, "failure", 3),      # an ordinary red run, no cancelled job at all
        _run(4, "CI", sha, "failure", 4),      # a cancelled job whose log is missing: not shown to be a hang
        _run(5, "CI", sha, "failure", 5),      # a job the API ended timed_out inside a failed run
    ]
    gate = _job(12, "pytest-gate", [("Verify the pytest suite or its legitimate skip", "failure")])
    jobs = {
        1: [_job(11, lint, [("Run lint-marked tests", "cancelled")], conclusion="cancelled"), gate],
        2: [_job(21, shard, [("Run tests", "failure")]), _job(22, "pytest (Python 3.12, shard 3/6)", [("Run tests", "cancelled")], conclusion="cancelled"),
            _job(23, "pytest-gate", [("Verify", "failure")])],
        3: [_job(31, shard, [("Run tests", "failure")])],
        4: [_job(41, lint, [("Run lint-marked tests", "cancelled")], conclusion="cancelled"), _job(42, "pytest-gate", [("Verify", "failure")])],
        5: [_job(51, lint, [("Run lint-marked tests", "cancelled")], conclusion="timed_out"), _job(52, "pytest-gate", [("Verify", "failure")])],
    }
    logs = {(1, 11): "2026-10-01T10:00:00.0Z running\n" + TIMEOUT_LINE, (2, 22): SUPERSEDED_LINE}
    return _snapshot(tmp_path, runs, jobs, logs)


def test_a_failed_run_carrying_a_cancelled_job_that_hit_its_timeout_is_recorded_and_the_other_failed_runs_are_not(tmp_path, history):  # noqa: F811
    records = _extract(_failed_run_snapshot(tmp_path, history[1]["c1"]), history[0], green_jobs=None)
    rows = [r for r in records if r["t"] == "stuck"]
    lint = "pytest (lint markers, group 1/2)"
    assert [(r["run"], r["conclusion"], r["jobs"], r["timeout_jobs"], r["no_log_jobs"]) for r in rows] == [
        (1, "failure", [lint], [lint], []),
        (4, "failure", [lint], [], [lint]),     # a cancelled job whose log is not in the snapshot: neither seen nor ruled out
        (5, "failure", [lint], [lint], [])]
    assert records[0]["stuck"] == {"timed_out_runs": 0, "cancelled_runs": 0, "failure_runs_with_timeout_job": 2,
                                   "runs_with_timeout_job": 2, "cancelled_job_no_log": 1, "runs_without_jobs_file": 0}
    assert records[0]["red_runs"] == 5, "the red-run figures are unchanged: a hang run is still a failed run, counted beside"


def test_the_report_names_the_failed_run_whose_job_hit_its_timeout(tmp_path, history):  # noqa: F811
    text = rrr.render_report(rrr.analyze(_extract(_failed_run_snapshot(tmp_path, history[1]["c1"]), history[0], green_jobs=None)))
    assert "runs that ended timed_out 0, cancelled 0, and failed runs carrying a job that hit its timeout 2; 2 runs in all hit a job timeout" in text
    assert "run 1 CI " in text and "failure" in text.split("run 1 CI ", 1)[1].split("\n", 1)[0]
    assert "run 3 CI " not in text and "run 2 CI " not in text and "run 4 CI " not in text
    assert "1 runs carry a cancelled job whose log is not in the snapshot: a timeout there is neither seen nor ruled out, and they are not counted as hangs" in text


def test_logs_are_read_only_for_a_cancelled_job_of_a_cancelled_or_failed_run():
    asked = []
    rows = [{"id": 1, "name": "CI", "head_sha": "a" * 40, "conclusion": c, "created_at": f"2026-10-01T10:0{n}:00Z", "updated_at": "2026-10-01T10:30:00Z"}
            for n, c in enumerate(("success", "failure", "cancelled", "timed_out"))]
    for n, r in enumerate(rows):
        r["id"] = n + 1
    jobs = {rid: [{"id": rid * 10, "name": "j", "conclusion": "timed_out" if rid == 4 else "cancelled"}] for rid in (1, 2, 3, 4)}
    stuck.stuck_rows(rows, lambda rid: jobs[rid], lambda rid, jid: asked.append(rid) or False)
    assert asked == [2, 3], "a success run is never read, and a timed_out job needs no log"


def test_a_cancelled_job_with_no_log_in_the_snapshot_is_counted_apart_and_never_read_as_not_a_hang(tmp_path, history):  # noqa: F811
    """The log of a cancelled job is what says whether it outlived its timeout. With no log the answer is unknown, so the job is
    its own count: not a hang, and not ruled out. An empty log file is no log, as the report's own reader treats it."""
    runs = [_run(1, "CI", history[1]["c1"], "cancelled", 1), _run(2, "CI", history[1]["c1"], "cancelled", 2),
            _run(3, "CI", history[1]["c1"], "failure", 3)]
    jobs = {1: [_job(11, "shard", [("Run tests", "cancelled")], conclusion="cancelled")],
            2: [_job(21, "shard", [("Run tests", "cancelled")], conclusion="cancelled")],
            3: [_job(31, "shard", [("Run tests", "cancelled")], conclusion="cancelled"), _job(32, "gate", [("Verify", "failure")])]}
    snap = _snapshot(tmp_path, runs, jobs, {(2, 21): SUPERSEDED_LINE})
    (snap / "logs" / "1").mkdir(parents=True)
    (snap / "logs" / "1" / "11.log").write_text("")          # an empty log: no log
    records = _extract(snap, history[0], green_jobs=None)
    rows = {r["run"]: r for r in records if r["t"] == "stuck"}
    assert sorted(rows) == [1, 2, 3] and rows[2]["no_log_jobs"] == [], "run 2's log exists and says it was superseded: nothing unknown"
    assert rows[1]["no_log_jobs"] == ["shard"] and rows[1]["timeout_jobs"] == []
    assert rows[3]["conclusion"] == "failure" and rows[3]["no_log_jobs"] == ["shard"]
    assert records[0]["stuck"]["cancelled_job_no_log"] == 2 and records[0]["stuck"]["runs_with_timeout_job"] == 0
    text = rrr.render_report(rrr.analyze(records))
    assert "2 runs carry a cancelled job whose log is not in the snapshot" in text


def test_stuck_rows_without_a_log_probe_assume_the_log_is_there():
    row = {"id": 1, "name": "CI", "head_sha": "a" * 40, "conclusion": "cancelled", "created_at": "2026-10-01T10:00:00Z",
           "updated_at": "2026-10-01T10:30:00Z"}
    out = stuck.stuck_rows([row], lambda rid: [{"id": 9, "name": "j", "conclusion": "cancelled"}], lambda rid, jid: False)
    assert out[0]["no_log_jobs"] == [] and out[0]["timeout_jobs"] == []
