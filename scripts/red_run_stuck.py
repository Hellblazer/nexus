# SPDX-License-Identifier: AGPL-3.0-or-later
"""Job timeouts and cancels the red-run report does not see (RDR-258 Phase 1 step 4, nexus-2jomg.2).

``red_run_report.py`` counts a red run as one whose conclusion is ``failure``. A test that fakes a stdlib
function for the whole worker (the ``module_seam`` incident, nexus-hkafl) does not fail: it blocks a teardown until
the job runs out its ``timeout-minutes``. GitHub then ends THAT JOB ``cancelled`` (or ``timed_out``) with a log line
saying it "exceeded the maximum execution time"; the RUN around it usually concludes ``failure`` (job 119003409658 of
run 39531650989, the 2026-10-21 hang), because the fan-in job needs the cancelled one. A report scoped to ``failure``
counts such a run among the red ones with no word that nothing failed in it, and one scoped to ``timed_out`` or
``cancelled`` runs never sees it. This module records every run that carries such a job, whatever the run's own
conclusion, so the report prints their counts and lists the ones whose job hit its timeout.

Three shapes are recorded: a run that ended ``timed_out``, a run that ended ``cancelled``, and a ``failure`` run with a job
that hit its timeout. Most ``cancelled`` runs are not hangs: a newer push supersedes a run in flight (the job log says "The
operation was canceled"), and a ``failure`` run's other jobs are often cancelled when one fails. A run counts as hit-its-timeout
only when a job concluded ``timed_out``, or a ``cancelled`` job's log carries the runner's maximum-execution-time message; a
``failure`` run with no such job is an ordinary red run and is not recorded here. The Phase 0 baseline window
(2026-09-23 to 2026-11-23) has to be extracted again from its snapshot to get the counts.

Pure functions over run and job rows and two loaders, so the extraction and the tests share them.
"""
from __future__ import annotations

import collections
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

Row = dict[str, Any]

STUCK_CONCLUSIONS: tuple[str, ...] = ("timed_out", "cancelled")
#: A run whose own conclusion is this is recorded only when one of its jobs hit its timeout.
FAILURE_CONCLUSION: str = "failure"
#: The line the runner writes into a job log when the job outlives its ``timeout-minutes``.
TIMEOUT_RE: re.Pattern[str] = re.compile(r"exceeded the maximum execution time", re.I)
#: Runs listed by name in the report; the counts cover every run.
LIST_MAX: int = 20


def log_has_timeout(path: Path) -> bool:
    """Whether the job log at *path* carries the runner's maximum-execution-time message. A missing or unreadable log is False."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return any(TIMEOUT_RE.search(line) for line in fh)
    except OSError:
        return False


def log_present(path: Path) -> bool:
    """Whether a job log is in the snapshot: a file with content (the report treats an empty one as no log, too)."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def stuck_rows(runs: Iterable[Row], load_jobs: Callable[[int], Sequence[Row] | None],
               job_hit_timeout: Callable[[int, int], bool],
               log_exists: Callable[[int, int], bool] | None = None) -> list[Row]:
    """One ``stuck`` record per run that ended ``timed_out`` or ``cancelled``, and per ``failure`` run with a job that hit its
    timeout, oldest first.

    ``jobs`` names its final attempt's jobs that ended ``timed_out`` or ``cancelled``; ``timeout_jobs`` the ones that hit their
    timeout: a job that concluded ``timed_out``, or a ``cancelled`` one for which ``job_hit_timeout(run, job)`` says its log has
    the message. The log is read only for a ``cancelled`` job of a run that is itself ``cancelled`` or ``failure``.

    ``no_log_jobs`` names the ``cancelled`` jobs whose log is not in the snapshot (``log_exists(run, job)`` is False, when it is
    given) and that are not otherwise shown to have hit a timeout: a timeout there is neither seen nor ruled out, so they are
    counted apart and never read as "not a hang". A ``failure`` run is recorded when it has such a job as well as when it has a
    ``timeout_jobs`` one.
    """
    out: list[Row] = []
    wanted = (*STUCK_CONCLUSIONS, FAILURE_CONCLUSION)
    for run in sorted((r for r in runs if r["conclusion"] in wanted), key=lambda r: (r["created_at"], r["id"])):
        jobs = load_jobs(run["id"])
        attempt = run.get("run_attempt", 1)
        ended = [j for j in (jobs or []) if j.get("conclusion") in STUCK_CONCLUSIONS and j.get("run_attempt", attempt) == attempt]
        if run["conclusion"] == FAILURE_CONCLUSION and not ended:
            continue
        timeout_jobs = sorted(j["name"] for j in ended
                              if j["conclusion"] == "timed_out" or job_hit_timeout(run["id"], j["id"]))
        no_log_jobs = sorted(j["name"] for j in ended
                             if j["conclusion"] == "cancelled" and j["name"] not in timeout_jobs
                             and log_exists is not None and not log_exists(run["id"], j["id"]))
        if run["conclusion"] == FAILURE_CONCLUSION and not timeout_jobs and not no_log_jobs:
            continue
        out.append({"t": "stuck", "run": run["id"], "sha": run["head_sha"], "workflow": run["name"],
                    "conclusion": run["conclusion"], "event": run.get("event", ""), "created": run["created_at"],
                    "ended": run["updated_at"], "jobs": sorted(j["name"] for j in ended),
                    "timeout_jobs": timeout_jobs, "no_log_jobs": no_log_jobs, "jobs_file": jobs is not None})
    return out


def summarize(rows: Sequence[Row]) -> Row:
    """The counts the extraction's ``meta`` carries: runs per shape, and how many hit a job timeout in all."""
    by = collections.Counter(r["conclusion"] for r in rows)
    return {"timed_out_runs": by.get("timed_out", 0), "cancelled_runs": by.get("cancelled", 0),
            "failure_runs_with_timeout_job": sum(1 for r in rows if r["conclusion"] == FAILURE_CONCLUSION and r["timeout_jobs"]),
            "runs_with_timeout_job": sum(1 for r in rows if r["timeout_jobs"]),
            "cancelled_job_no_log": sum(1 for r in rows if r.get("no_log_jobs")),
            "runs_without_jobs_file": sum(1 for r in rows if not r["jobs_file"])}


def render(meta: Mapping[str, Any], rows: Sequence[Row]) -> list[str]:
    """The report's section: counts, then the runs that hit a timeout (a hang), then how many cancelled runs did not."""
    stuck = meta.get("stuck")
    if stuck is None:
        return ["job timeouts and cancels: not recorded in this extraction (extract again from the snapshot)"]
    hung = [r for r in rows if r["timeout_jobs"]]
    failed = stuck.get("failure_runs_with_timeout_job", 0)
    quiet = stuck["timed_out_runs"] + stuck["cancelled_runs"] - (stuck["runs_with_timeout_job"] - failed)
    lines = [f"job timeouts and cancels: runs that ended timed_out {stuck['timed_out_runs']}, cancelled {stuck['cancelled_runs']}, "
             f"and failed runs carrying a job that hit its timeout {failed}; {stuck['runs_with_timeout_job']} runs in all hit a job "
             f"timeout (a timed_out job, or a cancelled job whose log says it exceeded the maximum execution time), the other "
             f"{quiet} timed_out or cancelled runs showed no job timeout in the logs we have (superseded, cancelled by hand, or no log)",
             "  a test that fakes a stdlib function for the whole worker shows here (its job is cancelled, its run usually failed)"]
    if stuck.get("cancelled_job_no_log"):
        lines.append(f"  {stuck['cancelled_job_no_log']} runs carry a cancelled job whose log is not in the snapshot: a timeout there is "
                     "neither seen nor ruled out, and they are not counted as hangs")
    if stuck.get("runs_without_jobs_file"):
        lines.append(f"  {stuck['runs_without_jobs_file']} of these runs have no jobs file in the snapshot, so a timeout in them is not seen")
    for r in hung[:LIST_MAX]:
        lines.append(f"  run {r['run']} {r['workflow']} {r['sha'][:12]} {r['conclusion']} {r['ended']}: {', '.join(r['timeout_jobs'])}")
    if len(hung) > LIST_MAX:
        lines.append(f"  ... {len(hung) - LIST_MAX} more")
    return lines
