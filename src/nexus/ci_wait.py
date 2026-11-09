# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx ci wait <sha>``: one wake for a pushed sha, with the result (nexus-1d6pz.11.4).

A session launched without the Claude Code channel sees board posts only at
its next user prompt, so a session waiting on CI with nobody at the keyboard
never wakes. Run as a background command, this parks on the CI board's
``--done`` topic (``board/ci/<repo>-<branch>--done``) for the sha and exits
when its row arrives: the exit is the wake, and the one printed line is the
result. It reads the tuple space only; it never polls GitHub for the wait.

The ``--done`` row (conexus adapter, conexus-5wjs; nexus-1d6pz.11.1) has the
body ``{state, sha, runs, url}``. Since .11.1 each ``runs`` entry is
``[run_id, attempt, workflow, conclusion]`` with GitHub's conclusion verbatim;
a row written before it has ``[run_id, attempt]``, conclusion unknown, and
for those runs this reads each run's conclusion from GitHub once, after the
row arrives.

Outcome, the reduction the adapter's ``outcome`` dim states (bob, .11.1):
failure if any run is failure, timed_out, startup_failure or
action_required; else cancelled if any is cancelled or stale; else success
(success, neutral and skipped count as success). A pure function of GitHub's
conclusions: required checks and flake judgment stay with the reader.

A rerun (a new attempt) posts a new row for the same sha; the newest row for
the sha already on the topic, or the first to arrive, is the one returned.
"""
from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from nexus.bounded_subprocess import run_bounded
from nexus.db.t2.records import TupleRow

#: GitHub conclusions that make the set a failure.
FAILURE_CONCLUSIONS: frozenset[str] = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
#: GitHub conclusions that make a set with no failure cancelled.
CANCELLED_CONCLUSIONS: frozenset[str] = frozenset({"cancelled", "stale"})
#: GitHub conclusions that count as success.
SUCCESS_CONCLUSIONS: frozenset[str] = frozenset({"success", "neutral", "skipped"})

EXIT_SUCCESS: int = 0
EXIT_FAILURE: int = 1
EXIT_TIMEOUT: int = 2
EXIT_CANCELLED: int = 3
#: A run's conclusion could not be read, or is one this table does not know.
EXIT_UNKNOWN: int = 4

OUTCOME_EXIT: dict[str, int] = {"success": EXIT_SUCCESS, "failure": EXIT_FAILURE, "cancelled": EXIT_CANCELLED}

#: The engine refuses a park longer than its cap (25 s by default).
PARK_CAP_S: int = 25
_PAGE: int = 100


class _Reader(Protocol):
    def rd(
        self, subspace: str, keys_pattern: dict[str, str] | None = None, *,
        n: int = 1, since: tuple[str, str] | None = None, timeout_s: int = 0,
    ) -> list[TupleRow]: ...


@dataclass(frozen=True)
class Run:
    run_id: int
    attempt: int
    workflow: str | None
    conclusion: str | None


@dataclass(frozen=True)
class DoneRow:
    tuple_id: str
    sha: str
    runs: tuple[Run, ...]
    url: str


def done_subspace(repo: str, branch: str) -> str:
    return f"board/ci/{repo}-{branch}--done"


def parse_done_row(row: TupleRow) -> DoneRow | None:
    """The row as a :class:`DoneRow`, or None when its body is not a ``--done`` body."""
    try:
        body = json.loads(row.body or "")
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("sha"), str):
        return None
    runs: list[Run] = []
    for entry in body.get("runs") or []:
        if not isinstance(entry, list) or len(entry) < 2:
            return None
        workflow = entry[2] if len(entry) >= 4 and isinstance(entry[2], str) else None
        conclusion = entry[3] if len(entry) >= 4 and isinstance(entry[3], str) else None
        runs.append(Run(int(entry[0]), int(entry[1]), workflow, conclusion))
    return DoneRow(row.id, body["sha"].lower(), tuple(runs), str(body.get("url") or ""))


def reduce_outcome(conclusions: list[str | None]) -> str | None:
    """success / failure / cancelled, or None when a conclusion is unknown
    and no failure settles it."""
    if any(c in FAILURE_CONCLUSIONS for c in conclusions):
        return "failure"
    if any(c is None or c not in CANCELLED_CONCLUSIONS | SUCCESS_CONCLUSIONS for c in conclusions):
        return None
    if any(c in CANCELLED_CONCLUSIONS for c in conclusions):
        return "cancelled"
    return "success"


def gh_run_conclusion(gh_repo: str, run_id: int) -> tuple[str | None, str | None]:
    """``(workflow name, conclusion)`` of one run, from ``gh api``; ``(None, None)`` on any failure."""
    try:
        out = run_bounded(
            ["gh", "api", f"repos/{gh_repo}/actions/runs/{run_id}", "--jq", "[.name, .conclusion]"],
            timeout=30, check=True, stdin=subprocess.DEVNULL,
        ).stdout
        name, conclusion = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None, None
    return name, conclusion


def fill_conclusions(
    row: DoneRow, gh_repo: str,
    fetch: Callable[[str, int], tuple[str | None, str | None]] | None = None,
) -> DoneRow:
    """Read the conclusion of each run the row leaves unknown (a pre-.11.1 row)."""
    if all(r.conclusion is not None for r in row.runs):
        return row
    fetch = fetch or gh_run_conclusion
    runs = []
    for r in row.runs:
        if r.conclusion is None:
            name, conclusion = fetch(gh_repo, r.run_id)
            r = Run(r.run_id, r.attempt, r.workflow or name, conclusion)
        runs.append(r)
    return DoneRow(row.tuple_id, row.sha, tuple(runs), row.url)


def _matches(row: TupleRow, sha: str) -> DoneRow | None:
    done = parse_done_row(row)
    return done if done is not None and done.sha.startswith(sha) else None


def find_done_row(
    store: _Reader, subspace: str, sha: str, *, timeout_s: float,
    park_s: int = PARK_CAP_S, clock: Callable[[], float] = time.monotonic,
) -> DoneRow | None:
    """The newest ``--done`` row for *sha* (a full sha or a prefix) already on
    the topic, else the first to arrive within *timeout_s*; None on timeout."""
    sha = sha.lower()
    deadline = clock() + timeout_s
    cursor: tuple[str, str] | None = None
    found: DoneRow | None = None
    while True:  # what is already there, oldest first; keep the newest match
        page = store.rd(subspace, n=_PAGE, since=cursor)
        for row in page:
            found = _matches(row, sha) or found
        if page:
            cursor = (page[-1].created_at or "", page[-1].id)
        if len(page) < _PAGE:
            break
    while found is None:
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        wait = max(1, min(park_s, PARK_CAP_S, int(remaining)))
        page = store.rd(subspace, n=_PAGE, since=cursor, timeout_s=wait)
        for row in page:
            found = _matches(row, sha) or found
        if page:
            cursor = (page[-1].created_at or "", page[-1].id)
    return found


def result_line(row: DoneRow, outcome: str | None) -> str:
    runs = ", ".join(
        f"{r.workflow or 'run'} {r.conclusion or 'unknown'} (run {r.run_id}"
        + (f" attempt {r.attempt})" if r.attempt != 1 else ")")
        for r in row.runs
    )
    return f"{row.sha[:12]} {outcome or 'unknown'}: {runs or 'no runs'} {row.url}".rstrip()


def wait_exit_code(outcome: str | None) -> int:
    return OUTCOME_EXIT.get(outcome or "", EXIT_UNKNOWN)


def summarize(row: DoneRow) -> tuple[str, int]:
    """The printed line and the exit code for a found row."""
    outcome = reduce_outcome([r.conclusion for r in row.runs]) if row.runs else None
    return result_line(row, outcome), wait_exit_code(outcome)


__all__: list[str] = [
    "DoneRow", "Run", "done_subspace", "fill_conclusions", "find_done_row", "gh_run_conclusion",
    "parse_done_row", "reduce_outcome", "result_line", "summarize", "wait_exit_code",
    "EXIT_CANCELLED", "EXIT_FAILURE", "EXIT_SUCCESS", "EXIT_TIMEOUT", "EXIT_UNKNOWN",
]
