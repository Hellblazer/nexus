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
            _p("2026-09-26T10:02:00Z", state="completed", conclusion="success", job="lint")]


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


def test_a_cancelled_job_with_no_run_post_reads_cancelled() -> None:
    # Without the run post the fold cannot tell a timeout from a supersede,
    # so it reports what GitHub said and leaves the reading to the caller.
    posts = [_p("2026-09-26T10:04:00Z", state="completed", conclusion="cancelled", job="slow"),
             _p("2026-09-26T10:04:00Z", state="completed", conclusion="success", job="lint")]
    statuses = cs.fold(posts, SHA)
    assert {s.job: s.verdict for s in statuses} == {"slow": "cancelled", "lint": "green"}
    assert cs.exit_code(statuses) == 4


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
