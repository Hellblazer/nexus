#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fold the CI board posts for one commit into per-job status (RDR-220).

The GitHub webhook adapter (conexus, RDR-220) writes one post to
``board/ci/<repo>-<branch>`` (the ``board/ci/<topic>`` template) for every state change of every workflow run
and job. This script reads that topic, keeps the latest state of each
(workflow, job, attempt), and prints what is green, what is pending, and
what failed, for one commit.

It is a repository development helper, deliberately not an ``nx`` command
or MCP tool (Sam, 2026-09-26). A session that only wants to be told reads
the same posts with ``tuple_rd`` after ``tuple_subscribe``.

Post contract (RDR-220): ``dims`` ``{"from": "github", "kind": "run"|"job"}``;
body JSON ``{"state", "workflow", "job"?, "sha", "run", "attempt",
"conclusion", "url"}``; ``state`` is ``queued``, ``in_progress`` or
``completed``; run posts omit ``job``.

Exit status: 0 when every job completed green, 1 when any job failed,
2 when nothing failed but something is still pending, 3 when the topic has
no post for the commit. Usage::

    uv run python scripts/ci_status.py <sha> [--topic nexus-develop] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

DEFAULT_TOPIC: str = "nexus-develop"
#: Conclusions GitHub reports for a job that did not break the run.
GREEN: frozenset[str] = frozenset({"success", "skipped", "neutral"})
_STATE_RANK: dict[str, int] = {"queued": 0, "in_progress": 1, "completed": 2}
_PAGE: int = 200


@dataclass(frozen=True)
class Status:
    workflow: str
    job: str  # "" for the workflow run itself
    attempt: int
    state: str
    conclusion: str
    url: str
    updated_at: str

    @property
    def verdict(self) -> str:
        if self.state != "completed":
            return "pending"
        return "green" if self.conclusion in GREEN else "failed"


def fold(posts: Iterable[tuple[str, dict[str, Any], dict[str, str]]], sha: str) -> list[Status]:
    """Latest state per (workflow, job, attempt) for *sha*.

    *posts* are ``(created_at, body, dims)``. Only the newest attempt of each
    (workflow, job) is kept, because a rerun supersedes the earlier attempt.
    Within one attempt a job's state only moves forward (queued, then
    in_progress, then completed), so the most advanced state wins and the
    newest post breaks a tie. Recency alone would let a stale delivery win:
    GitHub's manual redelivery of an old ``queued`` event, after its tuple
    expired and was purged, lands as a NEW row with a later ``created_at``
    (RDR-220 gate round 2).
    """
    latest: dict[tuple[str, str, int], Status] = {}
    for created_at, body, dims in posts:
        if dims.get("from") != "github" or body.get("sha") != sha:
            continue
        state = str(body.get("state", ""))
        if state not in _STATE_RANK:
            continue
        key = (str(body.get("workflow", "")), str(body.get("job", "")),
               int(body.get("attempt", 1) or 1))
        cand = Status(key[0], key[1], key[2], state,
                      str(body.get("conclusion") or ""), str(body.get("url", "")),
                      created_at)
        prev = latest.get(key)
        if prev is None or (_STATE_RANK[cand.state], cand.updated_at) >= (
                _STATE_RANK[prev.state], prev.updated_at):
            latest[key] = cand
    newest_attempt: dict[tuple[str, str], int] = {}
    for wf, job, attempt in latest:
        newest_attempt[(wf, job)] = max(attempt, newest_attempt.get((wf, job), 0))
    return sorted(
        (s for (wf, job, attempt), s in latest.items() if attempt == newest_attempt[(wf, job)]),
        key=lambda s: (s.workflow, s.job != "", s.job),
    )


def exit_code(statuses: list[Status]) -> int:
    if not statuses:
        return 3
    verdicts = {s.verdict for s in statuses}
    if "failed" in verdicts:
        return 1
    if "pending" in verdicts:
        return 2
    return 0


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
        print(json.dumps([s.__dict__ | {"verdict": s.verdict} for s in statuses], indent=2))
    elif not statuses:
        print(f"no posts for {args.sha} on board/ci/{args.topic}", file=sys.stderr)
    else:
        print(render(statuses, datetime.now(timezone.utc)))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
