# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-220: scripts/ci_status.py folds CI board posts into per-job status.

The fold is tested on the adapter's documented post shape. The read path is
tested against the REAL engine substrate: adapter-shaped posts are written
to a board topic and the script reads them back, paging included.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci_status.py"
spec = importlib.util.spec_from_file_location("ci_status", _SCRIPT)
assert spec is not None and spec.loader is not None
cs = importlib.util.module_from_spec(spec)
sys.modules["ci_status"] = cs  # a @dataclass resolves its annotations through sys.modules
spec.loader.exec_module(cs)

SHA = "c" * 40
GH = {"from": "github", "kind": "job"}


def _p(ts: str, *, state: str, job: str = "lint", conclusion: str | None = None,
       attempt: int = 1, sha: str = SHA, workflow: str = "CI", dims=GH, run: int | None = 222,
       url: str = "u"):
    body = {"state": state, "workflow": workflow, "sha": sha, "run": run,
            "attempt": attempt, "conclusion": conclusion, "url": url}
    if job:
        body["job"] = job
    return (ts, body, dims)


def test_latest_state_wins_per_job() -> None:
    posts = [_p("2026-09-26T10:00:00Z", state="queued"),
             _p("2026-09-26T10:01:00Z", state="in_progress"),
             _p("2026-09-26T10:05:00Z", state="completed", conclusion="success")]
    [s] = cs.fold(posts, SHA)
    assert (s.state, s.verdict) == ("completed", "green")


def test_a_late_queued_post_never_masks_completed_on_a_tie() -> None:
    posts = [_p("2026-09-26T10:05:00Z", state="completed", conclusion="failure"),
             _p("2026-09-26T10:05:00Z", state="queued")]
    [s] = cs.fold(posts, SHA)
    assert s.verdict == "failed"


def test_a_stale_redelivery_with_a_later_timestamp_never_masks_completed() -> None:
    # A manual redelivery of an old queued event, after its tuple was purged,
    # lands as a new row with a LATER created_at (RDR-220 gate round 2).
    posts = [_p("2026-09-26T10:05:00Z", state="completed", conclusion="success"),
             _p("2026-09-26T12:00:00Z", state="queued")]
    [s] = cs.fold(posts, SHA)
    assert (s.state, s.verdict) == ("completed", "green")


def test_a_rerun_attempt_supersedes_the_failed_one() -> None:
    posts = [_p("2026-09-26T10:05:00Z", state="completed", conclusion="failure", attempt=1),
             _p("2026-09-26T10:09:00Z", state="in_progress", attempt=2)]
    [s] = cs.fold(posts, SHA)
    assert (s.attempt, s.verdict) == (2, "pending")


def test_other_commits_and_non_github_posts_are_ignored() -> None:
    posts = [_p("t", state="completed", conclusion="success", sha="d" * 40),
             _p("t", state="completed", conclusion="failure",
                dims={"from": "ci", "kind": "ci-verdict"})]
    assert cs.fold(posts, SHA) == []


@pytest.mark.parametrize(
    ("conclusions", "code"),
    [(["success", "skipped"], 0), (["success", "failure"], 1),
     (["success", None], 2), ([], 3)],
)
def test_exit_code(conclusions, code) -> None:
    posts = [_p(f"2026-09-26T10:0{i}:00Z", job=f"j{i}",
                state="completed" if c else "in_progress", conclusion=c)
             for i, c in enumerate(conclusions)]
    assert cs.exit_code(cs.fold(posts, SHA)) == code


def _superseded_run() -> list:
    # The shape of a run the concurrency group cancelled when a newer commit
    # pushed: the run post and the shard jobs read cancelled, and the
    # pytest-gate aggregator alone reads failure because its shards never
    # reported (nexus-lgx93).
    return [_p("2026-09-26T10:05:00Z", state="completed", conclusion="cancelled", job="",
               dims={"from": "github", "kind": "run"}),
            _p("2026-09-26T10:04:00Z", state="completed", conclusion="cancelled", job="shard-1"),
            _p("2026-09-26T10:04:00Z", state="completed", conclusion="cancelled", job="shard-2"),
            _p("2026-09-26T10:04:30Z", state="completed", conclusion="failure", job="pytest-gate"),
            _p("2026-09-26T10:02:00Z", state="completed", conclusion="success", job="lint"),
            # the newer commit's run, first seen before the cancellations
            _p("2026-09-26T10:03:00Z", state="queued", job="", dims={"from": "github", "kind": "run"},
               run=333, sha="d" * 40)]


def test_a_superseded_run_reads_cancelled_not_failed() -> None:
    statuses = cs.fold(_superseded_run(), SHA)
    by_job = {s.job: s for s in statuses}
    assert by_job[""].verdict == "cancelled"
    assert by_job["shard-1"].verdict == "cancelled"
    # The aggregator's failure is a consequence of the cancellation, not a red.
    assert by_job["pytest-gate"].verdict == "cancelled"
    assert by_job["pytest-gate"].conclusion == "failure"  # the row still says why
    assert by_job["lint"].verdict == "green"
    assert cs.exit_code(statuses) == 4


def test_a_cancelled_job_in_a_run_github_did_not_cancel_is_failed() -> None:
    # A job past its time limit: GitHub cancels the job and fails the run.
    posts = [_p("2026-09-26T10:05:00Z", state="completed", conclusion="failure", job="",
                dims={"from": "github", "kind": "run"}),
             _p("2026-09-26T10:04:00Z", state="completed", conclusion="cancelled", job="slow")]
    statuses = cs.fold(posts, SHA)
    assert {s.job: s.verdict for s in statuses} == {"": "failed", "slow": "failed"}
    assert cs.exit_code(statuses) == 1


def test_a_cancelled_job_with_no_run_post_and_a_newer_run_reads_cancelled() -> None:
    # Without the run post the run-post rule cannot tell a timeout from a
    # supersede; the newer run can.
    posts = [_p("2026-09-26T10:04:00Z", state="completed", conclusion="cancelled", job="slow"),
             _p("2026-09-26T10:04:00Z", state="completed", conclusion="success", job="lint"),
             _p("2026-09-26T10:03:00Z", state="queued", job="", dims={"from": "github", "kind": "run"},
                run=333, sha="d" * 40)]
    statuses = cs.fold(posts, SHA)
    assert {s.job: s.verdict for s in statuses} == {"slow": "cancelled", "lint": "green"}
    assert cs.exit_code(statuses) == 4


def test_a_cancelled_job_with_no_run_post_and_no_sign_it_started_reads_cancelled() -> None:
    posts = [_p("2026-09-26T10:04:00Z", state="completed", conclusion="cancelled", job="slow")]
    assert cs.exit_code(cs.fold(posts, SHA)) == 4


def _duplicate_push_runs() -> list:
    """The d426a1a77 shape (nexus-wqvv9, 2026-09-29): GitHub started two CI
    runs for one push, both attempt 1. The concurrency group cancelled the
    older one, whose matrix job never expanded its name and whose aggregator
    failed for want of shards; the newer one passed."""
    run = {"from": "github", "kind": "run"}
    placeholder = "pytest (Python ${{ matrix.python-version }}, shard ${{ matrix.shard }}/4)"
    return [
        _p("2026-09-29T10:55:40Z", state="completed", conclusion="cancelled", job="", dims=run, run=100),
        _p("2026-09-29T10:55:39Z", state="completed", conclusion="cancelled", job=placeholder, run=100),
        _p("2026-09-29T10:55:41Z", state="completed", conclusion="failure", job="pytest-gate", run=100),
        _p("2026-09-29T11:20:00Z", state="completed", conclusion="success", job="", dims=run, run=200),
        _p("2026-09-29T11:18:00Z", state="completed", conclusion="success", job="pytest (Python 3.12, shard 1/4)", run=200),
        _p("2026-09-29T11:19:00Z", state="completed", conclusion="success", job="pytest-gate", run=200),
    ]


def test_a_duplicate_run_for_the_same_commit_does_not_read_failed() -> None:
    statuses = cs.fold(_duplicate_push_runs(), SHA)
    assert {s.job: s.verdict for s in statuses} == {
        "": "green", "pytest (Python 3.12, shard 1/4)": "green", "pytest-gate": "green",
    }, "only the newest run of a workflow speaks for the commit"
    assert cs.exit_code(statuses) == 0


def test_a_same_named_job_takes_the_newest_run_whatever_order_posts_arrive() -> None:
    # The older run's cancellation is posted AFTER the newer run's success.
    posts = [_p("2026-09-29T11:00:00Z", state="completed", conclusion="success", job="lint", run=200),
             _p("2026-09-29T11:30:00Z", state="completed", conclusion="cancelled", job="lint", run=100)]
    [s] = cs.fold(posts, SHA)
    assert (s.conclusion, s.verdict) == ("success", "green")


def test_a_newer_run_still_in_flight_reads_pending_over_an_older_green_one() -> None:
    posts = [_p("2026-09-29T10:00:00Z", state="completed", conclusion="success", job="lint", run=100),
             _p("2026-09-29T10:30:00Z", state="queued", job="lint", run=200)]
    [s] = cs.fold(posts, SHA)
    assert s.verdict == "pending"


def test_a_post_with_no_run_id_is_kept_not_dropped() -> None:
    # RDR-220 allows run to be null. Such a post must not lose to a numbered
    # sibling and vanish: a failure hidden that way would read green.
    run = {"from": "github", "kind": "run"}
    posts = [_p("2026-09-29T10:00:00Z", state="completed", conclusion="success", job="", dims=run, run=200),
             _p("2026-09-29T10:01:00Z", state="completed", conclusion="failure", job="lint", run=None)]
    statuses = cs.fold(posts, SHA)
    assert {s.job: s.verdict for s in statuses} == {"": "green", "lint": "failed"}
    assert cs.exit_code(statuses) == 1


def test_a_missing_run_id_is_read_from_the_run_url() -> None:
    # The url names run 300, newer than 200, so its failure is the verdict,
    # even though it was posted before run 200's success.
    url = "https://github.com/Hellblazer/nexus/actions/runs/300/job/9"
    posts = [_p("2026-09-29T10:00:00Z", state="completed", conclusion="failure", job="lint", run=None, url=url),
             _p("2026-09-29T10:05:00Z", state="completed", conclusion="success", job="lint", run=200)]
    [s] = cs.fold(posts, SHA)
    assert (s.conclusion, s.verdict) == ("failure", "failed")


def test_a_cancelled_higher_run_does_not_outrank_a_surviving_lower_one() -> None:
    # Two runs created in the same second: the concurrency group cancels by
    # queue order, which need not follow run id.
    run = {"from": "github", "kind": "run"}
    posts = [_p("2026-09-29T10:00:05Z", state="completed", conclusion="cancelled", job="", dims=run, run=200),
             _p("2026-09-29T10:00:04Z", state="completed", conclusion="cancelled", job="lint", run=200),
             _p("2026-09-29T10:20:00Z", state="completed", conclusion="success", job="", dims=run, run=100),
             _p("2026-09-29T10:19:00Z", state="completed", conclusion="success", job="lint", run=100)]
    statuses = cs.fold(posts, SHA)
    assert {s.job: s.verdict for s in statuses} == {"": "green", "lint": "green"}
    assert cs.exit_code(statuses) == 0


def test_a_rerun_of_a_cancelled_run_is_alive_before_its_new_run_row() -> None:
    run = {"from": "github", "kind": "run"}
    posts = [_p("2026-09-29T10:00:00Z", state="completed", conclusion="success", job="", dims=run, run=50),
             _p("2026-09-29T10:10:00Z", state="completed", conclusion="cancelled", job="", dims=run, run=100),
             _p("2026-09-29T10:20:00Z", state="queued", job="lint", run=100, attempt=2)]
    statuses = cs.fold(posts, SHA)
    assert {s.job: s.verdict for s in statuses}["lint"] == "pending"
    assert cs.exit_code(statuses) == 2


def test_when_every_run_was_cancelled_the_newest_speaks() -> None:
    run = {"from": "github", "kind": "run"}
    posts = [_p("2026-09-29T10:00:05Z", state="completed", conclusion="cancelled", job="", dims=run, run=100),
             _p("2026-09-29T10:00:06Z", state="completed", conclusion="success", job="lint", run=100),
             _p("2026-09-29T10:01:05Z", state="completed", conclusion="cancelled", job="", dims=run, run=200),
             _p("2026-09-29T10:01:04Z", state="completed", conclusion="failure", job="pytest-gate", run=200)]
    statuses = cs.fold(posts, SHA)
    assert {s.job for s in statuses} == {"", "pytest-gate"}
    assert cs.exit_code(statuses) == 4


def test_a_genuine_failure_outranks_a_cancelled_sibling_workflow() -> None:
    superseded = _superseded_run()
    other = [_p("2026-09-26T10:06:00Z", state="completed", conclusion="failure",
                job="build", workflow="Service CI")]
    assert cs.exit_code(cs.fold(superseded + other, SHA)) == 1


def test_pending_outranks_cancelled() -> None:
    posts = _superseded_run() + [_p("2026-09-26T10:07:00Z", state="in_progress",
                                    job="build", workflow="Service CI")]
    assert cs.exit_code(cs.fold(posts, SHA)) == 2


def test_a_status_cannot_exist_without_a_verdict() -> None:
    # The verdict is a stored field; a construction site that forgets it
    # would otherwise read green through exit_code's membership checks.
    with pytest.raises(ValueError, match="verdict"):
        cs.Status("CI", "lint", 1, "completed", "success", "u", "t", "")
    with pytest.raises(TypeError):
        cs.Status("CI", "lint", 1, "completed", "success", "u", "t")


def test_json_output_carries_the_verdict(capsys, monkeypatch) -> None:
    monkeypatch.setattr(cs, "read_posts", lambda topic: _superseded_run())
    assert cs.main([SHA, "--json"]) == 4
    rows = json.loads(capsys.readouterr().out)
    assert {r["job"]: r["verdict"] for r in rows}["pytest-gate"] == "cancelled"


def test_run_rows_sort_before_their_jobs() -> None:
    posts = [_p("t1", state="completed", conclusion="success", job="zeta"),
             _p("t2", state="completed", conclusion="success", job="")]
    assert [s.job for s in cs.fold(posts, SHA)] == ["", "zeta"]


# ── a job that hit its time limit versus a superseded run (nexus-rjk2a) ──────
#
# GitHub gives BOTH a timed-out job and a job cancelled by a newer push the
# conclusion ``cancelled``, and cancels the run too when the timed-out job was
# the only real one. What separates them is the workflow: Service CI never has
# a started push run cancelled by a newer push, so a cancelled job in a run
# that started is a timeout there, with no clock involved. CI does cancel
# started runs, so there a cancel is a supersede only when a newer run of the
# workflow exists (higher run id, other commit, first retained post no later
# than the run's earliest cancel post plus the skew).

RUN = {"from": "github", "kind": "run"}
NEWER = "e" * 40
BASE = datetime(2026, 9, 30, 10, 0, 0, tzinfo=timezone.utc)
JAVA = "Java tests + jOOQ codegen drift guard"


def T(sec: float) -> str:
    return (BASE + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")


def _cancelled_run(*, wf: str = "CI", run: int | None = 222, sha: str = SHA, cancels=(("shard-1", 600),),
                   sibling: str | None = "lint", started: bool = True, attempt: int = 1,
                   run_row: bool = True) -> list:
    """A run whose jobs in *cancels* (job, seconds) completed ``cancelled``.

    *sibling* names a job that completed ``success`` first (the persistent
    evidence that the run started), *started* adds the transient
    ``in_progress`` post of each cancelled job.
    """
    kw = {"workflow": wf, "run": run, "sha": sha, "attempt": attempt}
    posts = []
    if sibling:
        posts.append(_p(T(10), state="completed", conclusion="success", job=sibling, **kw))
    for job, sec in cancels:
        if started:
            posts.append(_p(T(20), state="in_progress", job=job, **kw))
        posts.append(_p(T(sec), state="completed", conclusion="cancelled", job=job, **kw))
    if run_row:
        posts.append(_p(T(max(sec for _j, sec in cancels) + 1), state="completed", conclusion="cancelled",
                        job="", dims=RUN, **kw))
    return posts


def _newer_run(first_sec: float, *, wf: str = "CI", run: int | None = 333, sha: str = NEWER,
               state: str = "queued", attempt: int = 1) -> list:
    return [_p(T(first_sec), state=state, job="", dims=RUN, workflow=wf, run=run, sha=sha, attempt=attempt)]


def _codes(posts) -> tuple[dict, int]:
    statuses = cs.fold(posts, SHA)
    return {s.job: s.verdict for s in statuses}, cs.exit_code(statuses)


# -- outside the set: Service CI, no clock --------------------------------------


def _service_ci_timeout(*, transients: bool) -> list:
    """Run 36728708487 (2026-09-30): the Java job started 14:23:33 and was cut at
    14:53:54 by its 30-minute limit; GitHub cancelled the run a second later."""
    wf = "Service CI"
    kw = {"workflow": wf, "run": 36728708487}
    posts = [
        _p("2026-09-30T14:23:31.172155Z", state="completed", conclusion="success",
           job="service change detection", **kw),
        _p("2026-09-30T14:53:54.040293Z", state="completed", conclusion="cancelled", job=JAVA, **kw),
        _p("2026-09-30T14:53:55.099871Z", state="completed", conclusion="cancelled", job="", dims=RUN, **kw),
    ]
    if transients:
        posts += [_p("2026-09-30T14:23:15.551795Z", state="queued", job="", dims=RUN, **kw),
                  _p("2026-09-30T14:23:33.981646Z", state="in_progress", job=JAVA, **kw)]
    return posts


@pytest.mark.parametrize("transients", [True, False], ids=["with-transient-posts", "completed-posts-only"])
def test_a_started_service_ci_job_cut_by_its_time_limit_reads_failed(transients: bool) -> None:
    # The 6 h-expiring queued/in_progress posts are not the evidence: the
    # sibling job that completed is, and completed posts last three days.
    verdicts, code = _codes(_service_ci_timeout(transients=transients))
    assert verdicts == {"service change detection": "green", JAVA: "failed", "": "failed"}
    assert code == 1


def test_the_in_progress_post_alone_is_enough_when_no_sibling_completed() -> None:
    posts = _cancelled_run(wf="Service CI", sibling=None, started=True)
    assert _codes(posts)[1] == 1


def test_a_service_ci_run_cancelled_while_pending_reads_cancelled() -> None:
    # 85462f7b (2026-09-29): a run replaced while queued posts only a cancelled
    # RUN row; no job ever existed.
    posts = [_p(T(5), state="completed", conclusion="cancelled", job="", dims=RUN,
                workflow="Service CI", run=36505694667)]
    verdicts, code = _codes(posts)
    assert verdicts == {"": "cancelled"} and code == 4


def test_service_ci_cancelled_jobs_with_no_sign_they_started_read_cancelled() -> None:
    posts = _cancelled_run(wf="Service CI", sibling=None, started=False)
    assert _codes(posts)[1] == 4


@pytest.mark.parametrize("newer_at", [-300, -5, 0, 5, 300])
def test_a_newer_run_never_excuses_a_service_ci_timeout(newer_at: float) -> None:
    # No clock outside the set: a session that pushes when the previous run ends
    # (gaps cluster at 29 to 31 minutes) must not turn a timeout into a supersede.
    posts = _cancelled_run(wf="Service CI", cancels=(("j", 1800),)) + _newer_run(1800 + newer_at, wf="Service CI")
    assert _codes(posts)[1] == 1


# -- inside the set: CI ---------------------------------------------------------


def test_a_superseded_in_progress_ci_run_reads_cancelled() -> None:
    # Measured: the newer run's first post preceded the cancels by 60 to 92 s.
    posts = _cancelled_run(cancels=(("shard-1", 600), ("shard-2", 603))) + _newer_run(520)
    verdicts, code = _codes(posts)
    assert set(verdicts.values()) == {"cancelled", "green"} and code == 4


def test_a_slow_cancel_is_still_a_supersede() -> None:
    # GitHub's cancel path can take about 300 s; there is no lower bound.
    posts = _cancelled_run(cancels=(("shard-1", 900),)) + _newer_run(500)
    assert _codes(posts)[1] == 4


def test_one_late_cancelled_job_does_not_flip_the_run() -> None:
    # The run is judged on its EARLIEST cancel post, not job by job.
    posts = _cancelled_run(cancels=(("shard-1", 510), ("shard-2", 800))) + _newer_run(500)
    assert _codes(posts)[1] == 4


def test_an_early_timeout_in_a_run_later_superseded_reads_failed() -> None:
    # shard-1 timed out at 600; the newer push only arrived at 2000 and cancelled shard-2.
    posts = _cancelled_run(cancels=(("shard-1", 600), ("shard-2", 2010))) + _newer_run(2000)
    assert _codes(posts)[1] == 1


def test_the_skew_pin_both_sides() -> None:
    assert cs.SUPERSEDE_SKEW_S == 30
    # the earliest cancel post is the job at 600 (the run row is at 601)
    inside = _cancelled_run(cancels=(("s", 600),)) + _newer_run(600 + 30)
    outside = _cancelled_run(cancels=(("s", 600),)) + _newer_run(600 + 30 + 1)
    assert _codes(inside)[1] == 4
    assert _codes(outside)[1] == 1


def test_the_skew_is_compared_as_datetimes_to_the_microsecond() -> None:
    # Strings would order ".70036Z" after ".700361Z" and trim digits differently.
    def run(newer_ts: str) -> int:
        posts = _cancelled_run(cancels=(("s", 600),))
        posts = [(("2026-09-30T10:10:00.700361Z" if p[1].get("job") == "s" and p[1]["state"] == "completed" else p[0]), p[1], p[2])
                 for p in posts]
        posts.append(_p(newer_ts, state="queued", job="", dims=RUN, run=333, sha=NEWER))
        return _codes(posts)[1]

    assert run("2026-09-30T10:10:30.70036Z") == 4    # 1 microsecond inside
    assert run("2026-09-30T10:10:30.700362Z") == 1   # 1 microsecond outside


def test_observed_supersedes_trail_by_a_few_seconds() -> None:
    # Board data: the newer run's first post landed 0 to 7 s AFTER the cancel post.
    posts = _cancelled_run(cancels=(("s", 600),)) + _newer_run(607)
    assert _codes(posts)[1] == 4


def test_an_older_run_id_never_poses_as_newer() -> None:
    # A rerun, or a redelivered post, of an OLDER run keeps its old run id even
    # though its post is fresh.
    posts = _cancelled_run(run=222, cancels=(("s", 600),)) + _newer_run(590, run=111)
    assert _codes(posts)[1] == 1


def test_a_rerun_attempt_of_an_older_run_never_poses_as_newer() -> None:
    posts = _cancelled_run(run=222, cancels=(("s", 600),)) + _newer_run(590, run=111, attempt=2)
    assert _codes(posts)[1] == 1


def test_a_second_run_for_the_same_commit_never_excuses() -> None:
    # The duplicate (higher run id, same sha, itself cancelled) leaves this run
    # the one that speaks; it must not count as the newer push that cancelled it.
    dup = _newer_run(590, sha=SHA) + [
        _p(T(595), state="completed", conclusion="cancelled", job="", dims=RUN, run=333)]
    posts = _cancelled_run(cancels=(("s", 600),), run_row=False) + dup
    assert _codes(posts)[1] == 1


def test_a_newer_run_of_another_workflow_never_excuses() -> None:
    posts = _cancelled_run(cancels=(("s", 600),)) + _newer_run(590, wf="Service CI")
    assert _codes(posts)[1] == 1


def test_no_newer_run_at_all_reads_failed_for_a_started_ci_job() -> None:
    assert _codes(_cancelled_run(cancels=(("s", 600),)))[1] == 1


def test_a_newer_run_whose_queued_post_expired_still_excuses() -> None:
    # Its earliest retained post is `completed`: the start cannot be placed, so
    # the older reading stands rather than a phantom failure.
    posts = _cancelled_run(cancels=(("s", 600),)) + _newer_run(4000, state="completed")
    assert _codes(posts)[1] == 4


def test_a_newer_run_first_seen_long_after_the_cancel_does_not_excuse() -> None:
    posts = _cancelled_run(cancels=(("s", 600),)) + _newer_run(4000, state="queued")
    assert _codes(posts)[1] == 1


def test_a_newer_run_dated_by_its_in_progress_post_when_queued_expired() -> None:
    posts = _cancelled_run(cancels=(("s", 600),)) + _newer_run(590, state="in_progress")
    assert _codes(posts)[1] == 4


def test_a_cancelled_ci_run_with_no_run_id_compares_by_time_only() -> None:
    assert _codes(_cancelled_run(run=None, cancels=(("s", 600),)) + _newer_run(590, run=5))[1] == 4
    assert _codes(_cancelled_run(run=None, cancels=(("s", 600),)))[1] == 1


def test_an_unreadable_cancel_time_keeps_the_older_reading() -> None:
    posts = [("t", p[1], p[2]) if p[1]["state"] == "completed" and p[1]["conclusion"] == "cancelled" else p
             for p in _cancelled_run(cancels=(("s", 600),))]
    assert _codes(posts)[1] == 4


def test_the_time_limit_reading_is_for_the_develop_topic_only() -> None:
    posts = _service_ci_timeout(transients=False)
    assert cs.exit_code(cs.fold(posts, SHA, timeout_reading=False)) == 4


def test_main_applies_the_reading_by_topic(monkeypatch) -> None:
    posts = _service_ci_timeout(transients=False)
    monkeypatch.setattr(cs, "read_posts", lambda topic: posts)
    assert cs.main([SHA]) == 1
    assert cs.main([SHA, "--topic", "nexus-feature-x"]) == 4


def test_the_policy_set_names_ci_and_not_service_ci() -> None:
    assert "CI" in cs.PUSH_CANCELS_IN_PROGRESS
    assert "Service CI" not in cs.PUSH_CANCELS_IN_PROGRESS


# ── an expected job that never posted (nexus-vyg07) ─────────────────────────
#
# Service CI's Java job runs on hellmini-ci for a develop push while the opt-in
# variable SERVICE_CI_PUSH_RUNNER is `hellmini-ci`. When that runner
# is offline the job sits queued; its queued post expires at 6 h, and the only
# post left for the commit is `service change detection` success. The fold saw
# one green job and exit 0 on a commit whose engine suite never ran. A push run
# of Service CI exists only when service/** changed, so on the develop topic the
# Java job is always expected once the change detector has finished.

CHANGES = "service change detection"
SCI_RUN = 36800000001


def _service_ci(*, java: list | None = None, run_row: str | None = None) -> list:
    """The detector finished green at BASE+5 s; *java* posts follow, *run_row* is its conclusion."""
    kw = {"workflow": "Service CI", "run": SCI_RUN}
    posts = [_p(T(5), state="completed", conclusion="success", job=CHANGES, **kw)]
    posts += [_p(T(sec), state=state, conclusion=concl, job=JAVA, **kw) for sec, state, concl in (java or [])]
    if run_row:
        posts.append(_p(T(6), state="completed", conclusion=run_row, job="", dims=RUN, **kw))
    return posts


def _at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def _fold_expected(posts, *, now: datetime) -> list:
    """The fold as main() runs it for the develop topic."""
    return cs.fold(posts, SHA, expected_jobs=cs.EXPECTED_JOBS, now=now)


def test_a_java_job_that_never_posted_reads_pending_not_green() -> None:
    # Only the detector's completed post survives: what the board holds after
    # the queued post expires. Without an expected-job list this reads green.
    posts = _service_ci()
    statuses = _fold_expected(posts, now=_at(60))
    by_job = {s.job: s for s in statuses}
    assert by_job[CHANGES].verdict == "green"
    assert by_job[JAVA].verdict == "pending"
    assert by_job[JAVA].state == "missing"
    assert cs.exit_code(statuses) == cs.EXIT_PENDING


def test_a_missing_java_job_reads_failed_once_old_enough_and_never_green_after_the_expiry() -> None:
    posts = _service_ci()
    grace = cs.MISSING_JOB_GRACE_S
    assert cs.exit_code(_fold_expected(posts, now=_at(5 + grace - 1))) == cs.EXIT_PENDING
    assert cs.exit_code(_fold_expected(posts, now=_at(5 + grace + 1))) == cs.EXIT_FAILED
    # 6 h is when the queued and in_progress posts expire: still red, never green.
    statuses = _fold_expected(posts, now=_at(6 * 3600 + 60))
    assert cs.exit_code(statuses) == cs.EXIT_FAILED
    assert {s.job: s.verdict for s in statuses}[JAVA] == "failed"


@pytest.mark.parametrize("state", ["queued", "in_progress"])
def test_a_posted_java_job_is_read_as_posted_not_as_missing(state: str) -> None:
    statuses = _fold_expected(_service_ci(java=[(10, state, None)]), now=_at(6 * 3600))
    assert {s.job: (s.verdict, s.state) for s in statuses}[JAVA] == ("pending", state)


def test_a_completed_green_java_job_reads_green() -> None:
    posts = _service_ci(java=[(10, "completed", "success")], run_row="success")
    statuses = _fold_expected(posts, now=_at(6 * 3600))
    assert {s.verdict for s in statuses} == {"green"}
    assert cs.exit_code(statuses) == cs.EXIT_GREEN


def test_a_failed_java_job_stays_failed_with_no_missing_row_added() -> None:
    statuses = _fold_expected(_service_ci(java=[(10, "completed", "failure")]), now=_at(60))
    assert sorted(s.job for s in statuses) == sorted([CHANGES, JAVA])
    assert cs.exit_code(statuses) == cs.EXIT_FAILED


def test_no_java_row_is_expected_before_the_detector_finished() -> None:
    kw = {"workflow": "Service CI", "run": SCI_RUN}
    posts = [_p(T(1), state="in_progress", job=CHANGES, **kw)]
    statuses = _fold_expected(posts, now=_at(60))
    assert [s.job for s in statuses] == [CHANGES]  # already pending on its own


def test_a_cancelled_service_ci_run_adds_no_missing_row() -> None:
    statuses = _fold_expected(_service_ci(run_row="cancelled"), now=_at(60))
    assert JAVA not in {s.job for s in statuses}
    assert cs.exit_code(statuses) == cs.EXIT_CANCELLED


def test_a_workflow_with_no_expected_jobs_is_untouched() -> None:
    posts = [_p(T(5), state="completed", conclusion="success", job="lint")]
    assert [s.job for s in _fold_expected(posts, now=_at(6 * 3600))] == ["lint"]


def test_the_expected_job_list_is_for_the_develop_topic_only(monkeypatch) -> None:
    posts = _service_ci()
    monkeypatch.setattr(cs, "read_posts", lambda topic: posts)
    assert cs.main([SHA]) == cs.EXIT_FAILED  # develop topic, posts are hours old by the real clock
    assert cs.main([SHA, "--topic", "nexus-feature-x"]) == cs.EXIT_GREEN


def test_the_expected_java_job_name_matches_the_workflow() -> None:
    import yaml

    wf = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github" / "workflows" / "service-ci.yml").read_text())
    assert wf["name"] in cs.EXPECTED_JOBS
    names = {j.get("name", k) for k, j in wf["jobs"].items()}
    for expected, anchor in cs.EXPECTED_JOBS[wf["name"]].items():
        assert expected in names and anchor in names


# CI's qwen-linux job: a queued post for an offline runner expires at 6 h and pytest-gate
# (which needs the job) never queues, so without an expectation the commit reads green.

QWEN_JOB = "pytest (qwen-linux full suite)"
CHANGES_JOB = "doc-only fast lane predicate"
CI_RUN = 36800000002


SHARD_JOB = "pytest (Python 3.12, shard 1/6)"


def _ci(*, qwen: list | None = None, shards: str | None = None) -> list:
    """CI's posts for one sha; *shards* adds a hosted shard completed with that conclusion."""
    kw = {"workflow": "CI", "run": CI_RUN}
    posts = [_p(T(5), state="completed", conclusion="success", job=CHANGES_JOB, **kw)]
    if shards:
        posts.append(_p(T(600), state="completed", conclusion=shards, job=SHARD_JOB, **kw))
    posts += [_p(T(sec), state=state, conclusion=concl, job=QWEN_JOB, **kw) for sec, state, concl in (qwen or [])]
    return posts


def test_the_expected_qwen_job_and_its_anchor_match_ci_yml() -> None:
    import yaml

    wf = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text())
    assert wf["name"] == "CI"
    names = {j.get("name", k) for k, j in wf["jobs"].items()}
    assert cs.EXPECTED_JOBS["CI"] == {QWEN_JOB: CHANGES_JOB}
    assert QWEN_JOB in names and CHANGES_JOB in names


def test_a_qwen_job_that_never_posted_reads_pending_then_failed_never_green() -> None:
    posts = _ci()
    grace = cs.MISSING_JOB_GRACE_S
    by_job = {s.job: s for s in _fold_expected(posts, now=_at(60))}
    assert (by_job[QWEN_JOB].state, by_job[QWEN_JOB].verdict) == ("missing", "pending")
    assert cs.exit_code(_fold_expected(posts, now=_at(5 + grace + 1))) == cs.EXIT_FAILED
    assert cs.exit_code(_fold_expected(posts, now=_at(6 * 3600 + 60))) == cs.EXIT_FAILED


@pytest.mark.parametrize(("state", "conclusion", "verdict"), [
    ("queued", None, "pending"), ("in_progress", None, "pending"),
    ("completed", "skipped", "green"), ("completed", "success", "green"), ("completed", "failure", "failed"),
])
def test_a_posted_qwen_job_is_read_as_posted(state: str, conclusion: str | None, verdict: str) -> None:
    by_job = {s.job: s for s in _fold_expected(_ci(qwen=[(10, state, conclusion)]), now=_at(6 * 3600))}
    assert by_job[QWEN_JOB].verdict == verdict
    assert by_job[QWEN_JOB].state != "missing"


# A develop sha whose CI run started before the qwen job existed has no row for it, and
# its hosted shards ran. Reading that as `failed` shows a red suite that never was.


def test_the_peer_prefix_is_how_the_hosted_shards_are_named_in_ci_yml() -> None:
    import yaml

    wf = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text())
    [prefix] = cs.EXPECTED_UNLESS_PEER_RAN["CI"].values()
    assert set(cs.EXPECTED_UNLESS_PEER_RAN["CI"]) == {QWEN_JOB}
    assert wf["jobs"]["test"]["name"].startswith(prefix)
    assert SHARD_JOB.startswith(prefix)
    # nothing else in the workflow carries the prefix, so only a shard can excuse the row
    assert [k for k, j in wf["jobs"].items() if str(j.get("name", k)).startswith(prefix)] == ["test"]


@pytest.mark.parametrize("shards", ["success", "failure"])
def test_a_run_whose_hosted_shards_ran_expects_no_qwen_row(shards: str) -> None:
    """Pre-dates the job (or was routed away from it): no `missing` row at any age."""
    posts = _ci(shards=shards)
    for age in (60, 5 + cs.MISSING_JOB_GRACE_S + 1, 6 * 3600 + 60):
        got = _fold_expected(posts, now=_at(age))
        assert QWEN_JOB not in {s.job for s in got}
        # the shard's own conclusion is what speaks for the run
        assert cs.exit_code(got) == (cs.EXIT_FAILED if shards == "failure" else cs.EXIT_GREEN)


@pytest.mark.parametrize("shards", [None, "skipped"])
def test_a_run_whose_shards_did_not_run_still_expects_the_qwen_row(shards: str | None) -> None:
    """The case the expectation exists for: routed to qwen, its post gone, shards skipped."""
    posts = _ci(shards=shards)
    by_job = {s.job: s for s in _fold_expected(posts, now=_at(5 + cs.MISSING_JOB_GRACE_S + 1))}
    assert (by_job[QWEN_JOB].state, by_job[QWEN_JOB].verdict) == ("missing", "failed")


def test_a_shard_that_never_completed_does_not_excuse_a_missing_qwen_row() -> None:
    posts = _ci() + [_p(T(60), state="in_progress", job=SHARD_JOB, workflow="CI", run=CI_RUN)]
    by_job = {s.job: s for s in _fold_expected(posts, now=_at(5 + cs.MISSING_JOB_GRACE_S + 1))}
    assert by_job[QWEN_JOB].verdict == "failed"


# ── against the real engine ─────────────────────────────────────────────────


def test_reads_adapter_posts_from_the_engine_across_pages(t2_service_env, monkeypatch, capsys) -> None:
    from nexus.db.t2.http_tuple_store import HttpTupleStore

    monkeypatch.setattr(cs, "_PAGE", 3)  # force several pages
    topic = "xtest-develop"
    store = HttpTupleStore(tenant=t2_service_env)
    try:
        for i in range(5):
            body = {"state": "completed", "workflow": "CI", "job": f"j{i}", "sha": SHA,
                    "run": 222, "attempt": 1,
                    "conclusion": "failure" if i == 3 else "success", "url": "u"}
            store.out(f"board/ci/{topic}", {"topic": topic}, {"from": "github", "kind": "job"},
                      json.dumps(body), nonce=f"delivery-{i}")
    finally:
        store.close()

    code = cs.main([SHA, "--topic", topic])
    out = capsys.readouterr().out
    assert code == 1
    assert all(f"j{i}" in out for i in range(5)), out
    assert "failed" in out and "j3" in out
