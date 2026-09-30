#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fold the CI board posts for one commit into per-job status (RDR-220).

The GitHub webhook adapter (conexus, RDR-220) writes one post to
``board/ci/<repo>-<branch>`` (the ``board/ci/<topic>`` template) for every state change of every workflow run
and job. This script reads that topic, keeps the latest state of each
(workflow, job, attempt) in the one run of each workflow that speaks for
the commit, and prints what is green, what is pending, and what failed,
for one commit.

It is a repository development helper, deliberately not an ``nx`` command
or MCP tool (Sam, 2026-09-26). A session that only wants to be told reads
the same posts with ``tuple_rd`` after ``tuple_subscribe``.

Post contract (RDR-220): ``dims`` ``{"from": "github", "kind": "run"|"job"}``;
body JSON ``{"state", "workflow", "job"?, "sha", "run", "attempt",
"conclusion", "url"}``; ``state`` is ``queued``, ``in_progress`` or
``completed``; run posts omit ``job``.

Verdicts: ``green``, ``pending``, ``failed``, ``cancelled``. A run the
concurrency group cancelled when a newer commit pushed (a superseded run)
posts ``cancelled`` for the run and its jobs, and ``failure`` for an
aggregator job whose shards never reported; every row of such a run reads
``cancelled``, because the failure is a consequence of the cancellation,
not a red (a job that failed on its merits before the supersede reads
``cancelled`` too; its ``conclusion`` column still says ``failure``, so an
audit of a superseded commit reads conclusions, not verdicts). A
``cancelled`` job inside a run GitHub did NOT cancel was not superseded
(a job past its time limit, for one) and reads ``failed``. A ``cancelled``
job with no run post reads ``cancelled``, since the fold cannot tell which
it was; the run post is a separate delivery and can be missing.

GitHub also gives a job that hit its time limit the conclusion ``cancelled``,
and cancels the run with it when that job was the run's only real one
(nexus-rjk2a: Service CI's Java job, measured 2026-09-30, twice), so the
run-post rule above cannot separate a timeout from a supersede. The board
history can. A job that has an ``in_progress`` post before its ``cancelled``
post ran; it reads ``failed`` unless a run of the same workflow for a
different commit posted its first post within ``SUPERSEDE_WINDOW_S`` before
the cancellation (a newer push cancelling an in-progress run, measured 60 to
92 s ahead) or up to ``CLOCK_SKEW_S`` after it. A cancelled job that never
started is a pending run cancelled, and keeps reading ``cancelled``. This
reading can be wrong in two ways, both stated: a timeout that lands within
the window of an unrelated newer push reads ``cancelled`` (as it did before),
and a run someone cancelled by hand while it ran reads ``failed``. It needs
the ``in_progress`` post to still be on the board; without it (posts expire)
the older reading stands.

Exit status: 0 when every job completed green, 1 when any job failed,
2 when nothing failed but something is still pending, 3 when the topic has
no post for the commit, 4 when nothing failed or is pending but something
was cancelled. Usage::

    uv run python scripts/ci_status.py <sha> [--topic nexus-develop] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

DEFAULT_TOPIC: str = "nexus-develop"
#: Conclusions GitHub reports for a job that did not break the run.
GREEN: frozenset[str] = frozenset({"success", "skipped", "neutral"})
_STATE_RANK: dict[str, int] = {"queued": 0, "in_progress": 1, "completed": 2}
_PAGE: int = 200
#: How long before a job's ``cancelled`` post a newer run of its workflow may have
#: appeared for the cancellation to count as that run superseding it. Measured
#: 2026-09-30 on run 36733252313: 60 to 92 s (the runner takes a while to stop).
SUPERSEDE_WINDOW_S: int = 180
#: Board deliveries can reorder by seconds; a newer run's first post this far AFTER
#: the cancel post still counts as its cause.
CLOCK_SKEW_S: int = 15
#: Exit codes, by precedence: a genuine red outranks a wait, which outranks a supersede.
EXIT_GREEN: int = 0
EXIT_FAILED: int = 1
EXIT_PENDING: int = 2
EXIT_NO_POSTS: int = 3
EXIT_CANCELLED: int = 4


@dataclass(frozen=True)
class Status:
    workflow: str
    job: str  # "" for the workflow run itself
    attempt: int
    state: str
    conclusion: str
    url: str
    updated_at: str
    verdict: str

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f"Status.verdict must be one of {sorted(VERDICTS)}, got {self.verdict!r}")


VERDICTS: frozenset[str] = frozenset({"green", "pending", "failed", "cancelled"})


def _verdict(state: str, conclusion: str, run_conclusion: str | None) -> str:
    """*run_conclusion* is the workflow's own run row conclusion, None when unposted."""
    if state != "completed":
        return "pending"
    if conclusion in GREEN:
        return "green"
    if run_conclusion == "cancelled":
        return "cancelled"
    if conclusion == "cancelled" and run_conclusion is None:
        return "cancelled"
    return "failed"


_RUN_URL_RE: re.Pattern[str] = re.compile(r"/actions/runs/(\d+)")


def _run_id(body: dict[str, Any]) -> int | None:
    """The post's run id: the body's ``run``, else the one in its ``url``.

    None when neither carries one (RDR-220 allows a null ``run``); such a
    post is kept with its workflow's chosen run rather than dropped, so a
    failure cannot vanish behind a numbered sibling.
    """
    raw = body.get("run")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    m = _RUN_URL_RE.search(str(body.get("url") or ""))
    return int(m.group(1)) if m else None


def _newer(cand: tuple[str, str, str, str], prev: tuple[str, str, str, str] | None) -> bool:
    """Within one attempt the most advanced state wins; the newest post breaks a tie."""
    return prev is None or (_STATE_RANK[cand[0]], cand[3]) >= (_STATE_RANK[prev[0]], prev[3])


def _when(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _superseded_by_newer_run(
    cancelled_at: str, workflow: str, sha: str, own_first: str | None,
    run_first: dict[tuple[str, int], tuple[str, str]],
) -> bool:
    """True when a run of *workflow* for another commit began just before *cancelled_at*.

    *run_first* maps (workflow, run) to that run's earliest post and its sha.
    A run that began before this run's own first post is not newer. An
    unparseable cancel time answers True: the caller then keeps the older
    ``cancelled`` reading rather than call a run failed on a time it cannot read.
    """
    cancelled = _when(cancelled_at)
    own = _when(own_first) if own_first else None
    if cancelled is None:
        return True
    for (wf, _run), (first_at, first_sha) in run_first.items():
        if wf != workflow or first_sha == sha:
            continue
        first = _when(first_at)
        if first is None or (own is not None and first < own):
            continue
        if -CLOCK_SKEW_S <= (cancelled - first).total_seconds() <= SUPERSEDE_WINDOW_S:
            return True
    return False


def fold(posts: Iterable[tuple[str, dict[str, Any], dict[str, str]]], sha: str) -> list[Status]:
    """Latest state per (workflow, job, attempt) of each workflow's chosen run for *sha*.

    *posts* are ``(created_at, body, dims)``. GitHub can start two runs of
    one workflow for one push, both attempt 1, and the concurrency group
    cancels one (nexus-wqvv9). The run that speaks for the commit is the
    newest whose own run row is not cancelled, or the newest outright when
    every run was cancelled (a supersede by a newer commit). Newest is not
    enough on its own: two runs created in the same second are cancelled in
    queue order, which need not follow run id. Jobs of any other run are
    dropped, not merged. Within the chosen run only the newest attempt of
    each job is kept, because a rerun keeps the run id and supersedes the
    earlier attempt.

    Within one attempt a job's state only moves forward (queued, then
    in_progress, then completed), so the most advanced state wins and the
    newest post breaks a tie. Recency alone would let a stale delivery win:
    GitHub's manual redelivery of an old ``queued`` event, after its tuple
    expired and was purged, lands as a NEW row with a later ``created_at``
    (RDR-220 gate round 2).
    """
    # (state, conclusion, url, created_at) per key; a Status is built only
    # once its verdict is known, so no Status ever exists without one.
    latest: dict[tuple[str, int | None, str, int], tuple[str, str, str, str]] = {}
    started: set[tuple[str, int | None, str, int]] = set()
    run_first: dict[tuple[str, int], tuple[str, str]] = {}
    for created_at, body, dims in posts:
        if dims.get("from") != "github":
            continue
        state = str(body.get("state", ""))
        if state not in _STATE_RANK:
            continue
        if (rid := _run_id(body)) is not None:
            rk = (str(body.get("workflow", "")), rid)
            if rk not in run_first or created_at < run_first[rk][0]:
                run_first[rk] = (created_at, str(body.get("sha", "")))
        if body.get("sha") != sha:
            continue
        key = (str(body.get("workflow", "")), _run_id(body), str(body.get("job", "")),
               int(body.get("attempt", 1) or 1))
        if state == "in_progress":
            started.add(key)
        cand = (state, str(body.get("conclusion") or ""), str(body.get("url", "")), created_at)
        if _newer(cand, latest.get(key)):
            latest[key] = cand
    chosen = _chosen_runs(latest)
    in_run: dict[tuple[str, str, int], tuple[str, str, str, str]] = {}
    for (wf, run, job, attempt), v in latest.items():
        if run is None or run == chosen.get(wf):
            if _newer(v, in_run.get((wf, job, attempt))):
                in_run[(wf, job, attempt)] = v
    newest_attempt: dict[tuple[str, str], int] = {}
    for wf, job, attempt in in_run:
        newest_attempt[(wf, job)] = max(attempt, newest_attempt.get((wf, job), 0))
    current = {k: v for k, v in in_run.items() if k[2] == newest_attempt[(k[0], k[1])]}
    run_conclusion = {wf: v[1] for (wf, job, _a), v in current.items() if job == "" and v[0] == "completed"}
    # A job that ran and was then cancelled with no newer run of its workflow to
    # blame hit its time limit (or was cancelled by hand): the run was not superseded.
    for (wf, run, job, attempt), (state, conclusion, _url, cancelled_at) in latest.items():
        if not (job and state == "completed" and conclusion == "cancelled"):
            continue
        if attempt != newest_attempt.get((wf, job)) or not (run is None or run == chosen.get(wf)):
            continue
        if (wf, run, job, attempt) not in started:
            continue  # cancelled while pending: a supersede or a manual cancel, not a limit
        own_first = run_first[(wf, run)][0] if run is not None else None
        if not _superseded_by_newer_run(cancelled_at, wf, sha, own_first, run_first):
            run_conclusion[wf] = "timed_out"
    return sorted(
        (Status(wf, job, attempt, state, conclusion, url, created_at,
                _verdict(state, conclusion, run_conclusion.get(wf)))
         for (wf, job, attempt), (state, conclusion, url, created_at) in current.items()),
        key=lambda s: (s.workflow, s.job != "", s.job),
    )


def _chosen_runs(latest: dict[tuple[str, int | None, str, int], tuple[str, str, str, str]]) -> dict[str, int]:
    """Per workflow, the newest run not known to be cancelled, else the newest run.

    A run is cancelled when its newest run row says so and no post of the run
    carries a later attempt: a rerun of a cancelled run posts its jobs before
    its attempt-2 run row, and is alive from the first of them.
    """
    run_rows: dict[tuple[str, int], tuple[int, tuple[str, str, str, str]]] = {}
    top_attempt: dict[tuple[str, int], int] = {}
    runs: dict[str, set[int]] = {}
    for (wf, run, job, attempt), v in latest.items():
        if run is None:
            continue
        runs.setdefault(wf, set()).add(run)
        top_attempt[(wf, run)] = max(attempt, top_attempt.get((wf, run), 0))
        if job == "" and attempt >= run_rows.get((wf, run), (0, v))[0]:
            run_rows[(wf, run)] = (attempt, v)
    chosen: dict[str, int] = {}
    for wf, ids in runs.items():
        alive = [r for r in ids if not (
            (row := run_rows.get((wf, r))) and row[1][0] == "completed" and row[1][1] == "cancelled"
            and top_attempt[(wf, r)] <= row[0])]
        chosen[wf] = max(alive or ids)
    return chosen


def exit_code(statuses: list[Status]) -> int:
    if not statuses:
        return EXIT_NO_POSTS
    verdicts = {s.verdict for s in statuses}
    if "failed" in verdicts:
        return EXIT_FAILED
    if "pending" in verdicts:
        return EXIT_PENDING
    if "cancelled" in verdicts:
        return EXIT_CANCELLED
    return EXIT_GREEN


def _age(ts: str, now: datetime) -> str:
    try:
        then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    secs = int((now - then).total_seconds())
    return f"{secs // 60}m" if secs >= 60 else f"{secs}s"


def render(statuses: list[Status], now: datetime) -> str:
    lines = []
    for s in statuses:
        name = s.workflow if not s.job else f"  {s.job}"
        detail = s.conclusion if s.state == "completed" else s.state
        lines.append(f"{s.verdict:8} {name:48} {detail:12} {_age(s.updated_at, now):>5}")
    return "\n".join(lines)


def read_posts(topic: str) -> list[tuple[str, dict[str, Any], dict[str, str]]]:
    """Every live post on ``board/ci/<topic>``, paged by the (created_at, id) cursor."""
    from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — keep --help and the fold importable without a service

    store = HttpTupleStore()
    posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []
    since: tuple[str, str] | None = None
    try:
        while True:
            rows = store.rd(f"board/ci/{topic}", {"topic": topic}, n=_PAGE, since=since)
            for row in rows:
                try:
                    body = json.loads(row.body or "{}")
                except json.JSONDecodeError:
                    continue
                if isinstance(body, dict):
                    posts.append((row.created_at or "", body, dict(row.dims or {})))
            if len(rows) < _PAGE:
                return posts
            since = (rows[-1].created_at or "", rows[-1].id)
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Per-job CI status for one commit, from the CI board.")
    ap.add_argument("sha", help="full commit sha")
    ap.add_argument("--topic", default=DEFAULT_TOPIC, help="topic, the <repo>-<branch> part of board/ci/<topic>")
    ap.add_argument("--json", action="store_true", help="print the folded status as JSON")
    args = ap.parse_args(argv)

    statuses = fold(read_posts(args.topic), args.sha)
    code = exit_code(statuses)
    if args.json:
        print(json.dumps([s.__dict__ for s in statuses], indent=2))
    elif not statuses:
        print(f"no posts for {args.sha} on board/ci/{args.topic}", file=sys.stderr)
    else:
        print(render(statuses, datetime.now(timezone.utc)))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
