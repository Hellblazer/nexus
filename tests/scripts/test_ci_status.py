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
       attempt: int = 1, sha: str = SHA, workflow: str = "CI", dims=GH):
    body = {"state": state, "workflow": workflow, "sha": sha, "run": 222,
            "attempt": attempt, "conclusion": conclusion, "url": "u"}
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
