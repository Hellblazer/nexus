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

A job that hit its time limit is the exception (nexus-rjk2a). GitHub gives it
the conclusion ``cancelled`` too, and cancels the run with it when that job was
the run's only real one (Service CI's Java job, twice on 2026-09-30), so the
run-post rule above reads a timeout as a supersede. The fold separates them
per WORKFLOW, because what can cancel a run that has started depends on the
workflow's ``concurrency`` block:

* A workflow NOT in ``PUSH_CANCELS_IN_PROGRESS`` (Service CI: its
  ``cancel-in-progress`` is true for ``pull_request`` only) never has a started
  push run cancelled by a newer push. Its pending runs are cancelled by a
  newer push, but a pending run has no job posts. So a cancelled job in a run
  that started is a time limit or a manual cancel, and the run reads
  ``failed``. No clock is involved.
* A workflow IN the set (CI, and the others the lint test derives from the
  YAMLs) does cancel a started run when a newer push arrives. The run reads
  ``cancelled`` (superseded) when a run of the same workflow for another
  commit and a HIGHER run id has its first retained post no later than the
  run's earliest cancel post plus ``SUPERSEDE_SKEW_S``. There is no lower
  bound: a newer push cancels the old run when it arrives, however long the
  cancellation then takes (measured 0 to 92 s, GitHub allows about 300 s).
  Otherwise the run reads ``failed``.

"Started" uses posts that persist (completed posts last 3 days, ``queued`` and
``in_progress`` posts only 6 h): the cancelled job has an ``in_progress`` post,
or another job of the same run and attempt completed with a conclusion other
than ``cancelled``. A run with only a cancelled RUN row, or whose jobs all
cancelled with no such evidence, was cancelled while pending and reads
``cancelled``.

The policy holds for the develop topic only (push runs). For any other topic
the fold keeps the run-post rule above and reclassifies nothing.

Known misreadings, all of them:

1. A timeout of an in-set workflow's run that lands within ``SUPERSEDE_SKEW_S``
   before an unrelated newer push reads ``cancelled``.
2. A run cancelled by hand after it started reads ``failed``, in and out of
   the set, unless a newer run of an in-set workflow began before the cancel.
3. In-set, a newer run whose ``queued`` post has expired (6 h) and whose
   earliest retained post is ``completed`` has an unknown start; it is treated
   as excusing the cancel, so an in-set timeout older than 6 h can read
   ``cancelled``. Out-of-set workflows do not depend on it.
4. A newer run whose earliest retained post is ``in_progress`` (its ``queued``
   post expired) is dated by that post, which can be later than its real start.
5. A rerun attempt cancelled after a newer run already exists reads
   ``cancelled`` (in-set), because the newer run's first post precedes it.
6. An in-set run cancelled while pending, with a sibling job that completed,
   reads ``failed`` unless a newer run is found: a sibling's completion says
   the run started, not that the cancelled job did.
7. A ``cancelled`` job with no run post at all and no evidence that it started
   keeps ``cancelled``; the run post is a separate delivery and can be missing.

An expected job that never posted (nexus-vyg07). A board fold only sees posts,
and ``queued``/``in_progress`` posts expire at 6 h while ``completed`` posts last
3 days. Service CI's Java job runs on ``hellmini-ci`` for a develop push; with
that runner offline the job sits queued, its ``queued`` post expires, and the one
post left for the commit is ``service change detection`` success: one green job,
exit 0, for a commit whose engine suite never ran. ``EXPECTED_JOBS`` names, per
workflow, the jobs that must have a row once an anchor job has completed green.
A push run of Service CI exists only when ``service/**`` changed, so on the develop
topic the Java job is always expected. When the row is missing the fold adds one
with state ``missing``: ``pending`` for ``MISSING_JOB_GRACE_S`` after the anchor
completed, ``failed`` after that. It adds nothing for a run whose own row says
``cancelled``. For any other topic nothing is expected. CI's qwen-linux suite job
is expected the same way (it posts ``queued`` or ``completed skipped`` on every
develop push), except in a run where a hosted shard (``pytest (Python ...``)
completed with something other than ``skipped`` (``EXPECTED_UNLESS_PEER_RAN``):
there the job either did not exist yet, for a sha whose CI run predates it, or
was routed away from, and a missing row hides no unrun suite.

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
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

DEFAULT_TOPIC: str = "nexus-develop"
#: Conclusions GitHub reports for a job that did not break the run.
GREEN: frozenset[str] = frozenset({"success", "skipped", "neutral"})
_STATE_RANK: dict[str, int] = {"queued": 0, "in_progress": 1, "completed": 2}
_PAGE: int = 200
#: Workflows (the ``name:`` of the YAML, which is the ``workflow`` field of a post)
#: whose PUSH runs are cancelled in progress by a newer push: the ``push`` trigger
#: is present and ``concurrency.cancel-in-progress`` evaluates true for a push
#: event. ``tests/scripts/test_ci_status_policy_lint.py`` derives this set from
#: ``.github/workflows/*.yml`` and fails when the two disagree.
PUSH_CANCELS_IN_PROGRESS: frozenset[str] = frozenset({
    "CI",
    "CI commit coverage audit (nexus-of2x8)",
    "mac-signing-rehearsal",
    "pg-bundle-cache-seed",
    "plugin drift ledger",
    "plugin release",
})
#: A newer run's first retained post may trail the cancel post it caused by this
#: much (board deliveries reorder). Measured 2026-09-30 over 45 in-set cancelled
#: runs: the likely supersedes trail by 0 to 7 s; the next run of the same
#: workflow after any other cancelled run is 302 s or more away.
SUPERSEDE_SKEW_S: int = 30
#: Per workflow (the ``name:`` of the YAML), the jobs that must post once their ANCHOR
#: job has completed green, as ``{expected job: anchor job}``. Applied to the develop
#: topic only, where every run is a push run. ``tests/scripts/test_ci_status.py`` pins
#: the names against ``.github/workflows/service-ci.yml``.
EXPECTED_JOBS: dict[str, dict[str, str]] = {
    "Service CI": {"Java tests + jOOQ codegen drift guard": "service change detection"},
    # CI's qwen-linux job (owner pushes to develop run the whole suite there). It posts
    # on EVERY develop push once ``changes`` has finished: ``queued`` when it runs, or
    # ``completed skipped`` when the route sent the suite to the hosted shards or the
    # diff was doc-only (a skipped job posts, measured on the board 2026-10-01). So a
    # missing row means the post was lost or has expired, never a legitimate skip.
    # Without this entry a job left queued for an offline runner reads green once its
    # ``queued`` post expires at 6 h, because pytest-gate (which needs it) never queues.
    "CI": {"pytest (qwen-linux full suite)": "doc-only fast lane predicate"},
}
#: Per workflow, ``{expected job: name prefix of a PEER job}``: when a job of the chosen
#: run whose name starts with the prefix completed with a conclusion other than
#: ``skipped``, the expected job is NOT synthesized. The peer ran, so either the run
#: pre-dates the expected job (a develop sha whose CI run was started before the qwen
#: job existed has no row for it, and reading that as ``failed`` would show a red
#: suite that never was), or the route sent the suite to the peer and the expected
#: job is legitimately skipped. Either way a missing row cannot hide a suite that
#: never ran. The one case that matters, a suite routed to the expected job whose
#: post is gone, has the peer skipped, so it still reads as missing. Residual: a
#: doc-only run that pre-dates the job has every shard skipped and still reads
#: ``failed`` once; it ages out with the board's three-day retention.
EXPECTED_UNLESS_PEER_RAN: dict[str, dict[str, str]] = {
    "CI": {"pytest (qwen-linux full suite)": "pytest (Python "},
}
#: How long an expected job may go without any post once its anchor completed. The job
#: posts ``queued`` within seconds of the anchor finishing; a runner that is offline
#: leaves that post standing until it expires at 6 h, so a row absent this long means
#: the post was lost or has expired, and either way nothing proves the job ran.
MISSING_JOB_GRACE_S: int = 1800
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


@dataclass(frozen=True)
class _RunStart:
    """A run's earliest retained post: when, in which state, for which commit."""
    at: datetime
    state: str
    sha: str


def _superseded_by_newer_run(
    cancelled_at: datetime, workflow: str, sha: str, own_run: int | None,
    run_first: dict[tuple[str, int], _RunStart],
) -> bool:
    """True when a newer run of *workflow* for another commit explains a cancel.

    Newer means a HIGHER run id: a rerun or a redelivered post of an older
    run keeps its old id and cannot pose as newer, however late its post is.
    When *own_run* is None the id cannot be compared and any other commit's
    run qualifies. A newer run explains the cancel when its first retained
    post is no later than *cancelled_at* plus ``SUPERSEDE_SKEW_S``, or when
    that post is ``completed`` (its ``queued`` and ``in_progress`` posts have
    expired, so its start cannot be placed and the older reading stands).
    """
    latest_start = cancelled_at + timedelta(seconds=SUPERSEDE_SKEW_S)
    for (wf, rid), start in run_first.items():
        if wf != workflow or start.sha == sha or (own_run is not None and rid <= own_run):
            continue
        if start.state == "completed" or start.at <= latest_start:
            return True
    return False


def _run_timed_out(
    wf: str, sha: str, chosen_run: int | None,
    latest: dict[tuple[str, int | None, str, int], tuple[str, str, str, str]],
    newest_attempt: dict[tuple[str, str], int],
    started: set[tuple[str, int | None, str, int]],
    run_first: dict[tuple[str, int], _RunStart],
) -> bool:
    """True when the chosen run of *wf* was cancelled for want of anything superseding it."""
    rows = [(k, v) for k, v in latest.items()
            if k[0] == wf and (k[1] is None or k[1] == chosen_run) and k[3] == newest_attempt.get((wf, k[2]))]
    cancelled = [(k, v) for k, v in rows if v[0] == "completed" and v[1] == "cancelled"]
    jobs = [k for k, _v in cancelled if k[2]]
    if not jobs:
        return False  # only the run row: cancelled while pending
    ran = any(k in started for k in jobs) or any(
        k[2] and v[0] == "completed" and v[1] not in ("", "cancelled") for k, v in rows)
    if not ran:
        return False
    if wf not in PUSH_CANCELS_IN_PROGRESS:
        return True
    times = [_when(v[3]) for _k, v in cancelled]
    if any(t is None for t in times):
        return False  # a time we cannot read: keep the older reading
    return not _superseded_by_newer_run(min(times), wf, sha, chosen_run, run_first)  # type: ignore[type-var]


def fold(posts: Iterable[tuple[str, dict[str, Any], dict[str, str]]], sha: str, *,
         timeout_reading: bool = True, expected_jobs: dict[str, dict[str, str]] | None = None,
         now: datetime | None = None) -> list[Status]:
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

    *expected_jobs* (``EXPECTED_JOBS`` shape, none by default) adds a ``missing``
    row for each expected job with no post once its anchor job completed green;
    *now* (the wall clock when None) dates it against ``MISSING_JOB_GRACE_S``.
    """
    # (state, conclusion, url, created_at) per key; a Status is built only
    # once its verdict is known, so no Status ever exists without one.
    latest: dict[tuple[str, int | None, str, int], tuple[str, str, str, str]] = {}
    started: set[tuple[str, int | None, str, int]] = set()
    run_first: dict[tuple[str, int], _RunStart] = {}
    for created_at, body, dims in posts:
        if dims.get("from") != "github":
            continue
        state = str(body.get("state", ""))
        if state not in _STATE_RANK:
            continue
        if (rid := _run_id(body)) is not None and (at := _when(created_at)) is not None:
            rk = (str(body.get("workflow", "")), rid)
            if rk not in run_first or at < run_first[rk].at:
                run_first[rk] = _RunStart(at, state, str(body.get("sha", "")))
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
    if timeout_reading:
        for wf in {k[0] for k in current}:
            if _run_timed_out(wf, sha, chosen.get(wf), latest, newest_attempt, started, run_first):
                run_conclusion[wf] = "timed_out"
    statuses = [
        Status(wf, job, attempt, state, conclusion, url, created_at,
               _verdict(state, conclusion, run_conclusion.get(wf)))
        for (wf, job, attempt), (state, conclusion, url, created_at) in current.items()
    ]
    statuses += _missing_expected(current, run_conclusion, expected_jobs or {}, now or datetime.now(timezone.utc),
                                  unless_peer_ran=EXPECTED_UNLESS_PEER_RAN)
    return sorted(statuses, key=lambda s: (s.workflow, s.job != "", s.job))


def _missing_expected(
    current: dict[tuple[str, str, int], tuple[str, str, str, str]],
    run_conclusion: dict[str, str],
    expected_jobs: dict[str, dict[str, str]],
    now: datetime,
    *,
    unless_peer_ran: dict[str, dict[str, str]] | None = None,
) -> list[Status]:
    """A ``missing`` row per expected job with no post once its anchor completed green.

    Not for a job whose peer (``EXPECTED_UNLESS_PEER_RAN``) ran in the same run.
    """
    out: list[Status] = []
    for wf, jobs in expected_jobs.items():
        if run_conclusion.get(wf) == "cancelled":
            continue  # the run row already says why the job never posted
        for job, anchor in jobs.items():
            if any(k[0] == wf and k[1] == job for k in current):
                continue
            peer = (unless_peer_ran or {}).get(wf, {}).get(job)
            if peer and any(k[0] == wf and k[1].startswith(peer) and v[0] == "completed" and v[1] != "skipped"
                            for k, v in current.items()):
                continue
            done = [(k, v) for k, v in current.items()
                    if k[0] == wf and k[1] == anchor and v[0] == "completed" and v[1] in GREEN]
            if not done:
                continue  # the anchor is still pending or red: it speaks for the run
            (_w, _j, attempt), (_st, _c, url, anchored_at) = done[0]
            since = _when(anchored_at)
            old = since is None or (now - since).total_seconds() > MISSING_JOB_GRACE_S
            out.append(Status(wf, job, attempt, "missing", "", url, anchored_at, "failed" if old else "pending"))
    return out


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

    develop = args.topic == DEFAULT_TOPIC
    statuses = fold(read_posts(args.topic), args.sha, timeout_reading=develop,
                    expected_jobs=EXPECTED_JOBS if develop else None)
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
