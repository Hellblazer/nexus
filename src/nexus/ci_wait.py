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

A rerun (a new attempt) posts a new row for the same sha. By default the
newest row for the sha already on the topic, or the first to arrive, is the
one returned; ``--fresh`` counts only rows that arrive after the wait starts,
for a wait started on a rerun while the earlier attempt's row is still there.
The board wake is best effort (a dropped fold or deadline posts nothing), so at
the deadline the command reads GitHub's push runs for the sha once, to tell a
dropped wake from CI still running.
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
#: A run's conclusion could not be read, is one this table does not know, or the
#: row's outcome dim disagrees with the conclusions it carries.
EXIT_UNKNOWN: int = 4
#: The wait could not run: a bad argument, or the board unreadable until the deadline.
EXIT_UNUSABLE: int = 5

OUTCOME_EXIT: dict[str, int] = {"success": EXIT_SUCCESS, "failure": EXIT_FAILURE, "cancelled": EXIT_CANCELLED}

#: The engine refuses a park longer than its cap (25 s by default).
PARK_CAP_S: int = 25
_PAGE: int = 100
#: Seconds between retries of a failed board read.
_RETRY_S: float = 5.0


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
    outcome_dim: str | None = None


def done_subspace(repo: str, branch: str) -> str:
    return f"board/ci/{repo}-{branch}--done"


class BoardUnreadableError(RuntimeError):
    """The board could not be read before the deadline (engine down, refused read)."""


def parse_done_row(row: TupleRow) -> DoneRow | None:
    """The row as a :class:`DoneRow`, or None when its body is not a well-formed
    ``--done`` body. A malformed row is skipped, never fatal: every waiter reads
    every row on the topic, a peer's included."""
    try:
        body = json.loads(row.body or "")
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("sha"), str):
        return None
    entries = body.get("runs") or []
    if not isinstance(entries, list):
        return None
    runs: list[Run] = []
    try:
        for entry in entries:
            if not isinstance(entry, list) or len(entry) < 2:
                return None
            if isinstance(entry[0], bool) or isinstance(entry[1], bool):
                return None
            workflow = entry[2] if len(entry) >= 4 and isinstance(entry[2], str) else None
            conclusion = entry[3] if len(entry) >= 4 and isinstance(entry[3], str) else None
            runs.append(Run(int(entry[0]), int(entry[1]), workflow, conclusion))
    except (TypeError, ValueError):
        return None
    outcome = row.dims.get("outcome") if isinstance(row.dims, dict) else None
    return DoneRow(row.id, body["sha"].lower(), tuple(runs), str(body.get("url") or ""), outcome)


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


def _gh_json(path: str, jq: str) -> object | None:
    try:
        out = run_bounded(
            ["gh", "api", path, "--jq", jq], timeout=30, check=True, stdin=subprocess.DEVNULL,
        ).stdout
        return json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def gh_run_conclusion(gh_repo: str, run_id: int, attempt: int) -> tuple[str | None, str | None]:
    """``(workflow name, conclusion)`` of one attempt of one run, from ``gh api``
    (the attempt's own record, so a later rerun does not answer for it);
    ``(None, None)`` on any failure."""
    got = _gh_json(f"repos/{gh_repo}/actions/runs/{run_id}/attempts/{attempt}", "[.name, .conclusion]")
    if not (isinstance(got, list) and len(got) == 2):
        return None, None
    name, conclusion = got
    return (name if isinstance(name, str) else None), (conclusion if isinstance(conclusion, str) else None)


def gh_push_runs(gh_repo: str, sha: str) -> list[Run] | None:
    """The push runs GitHub lists for *sha* (a full sha), each with its
    conclusion or None while it runs; None when GitHub cannot be read. Read
    once, at the deadline only, to tell a dropped board wake from CI still
    running."""
    got = _gh_json(
        f"repos/{gh_repo}/actions/runs?head_sha={sha}&event=push&per_page=100",
        "[.workflow_runs[] | [.id, .run_attempt, .name, .conclusion]]",
    )
    if not isinstance(got, list):
        return None
    try:
        return [Run(int(r[0]), int(r[1]), r[2] if isinstance(r[2], str) else None,
                    r[3] if isinstance(r[3], str) else None) for r in got]
    except (TypeError, ValueError, IndexError):
        return None


def fill_conclusions(
    row: DoneRow, gh_repo: str,
    fetch: Callable[[str, int, int], tuple[str | None, str | None]] | None = None,
) -> DoneRow:
    """Read the conclusion of each run the row leaves unknown (a pre-.11.1 row)."""
    if all(r.conclusion is not None for r in row.runs):
        return row
    fetch = fetch or gh_run_conclusion
    runs = []
    for r in row.runs:
        if r.conclusion is None:
            name, conclusion = fetch(gh_repo, r.run_id, r.attempt)
            r = Run(r.run_id, r.attempt, r.workflow or name, conclusion)
        runs.append(r)
    return DoneRow(row.tuple_id, row.sha, tuple(runs), row.url, row.outcome_dim)


def _matches(row: TupleRow, sha: str) -> DoneRow | None:
    done = parse_done_row(row)
    return done if done is not None and done.sha.startswith(sha) else None


def find_done_row(
    store: _Reader, subspace: str, sha: str, *, timeout_s: float, fresh: bool = False,
    park_s: int = PARK_CAP_S, clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> DoneRow | None:
    """The newest ``--done`` row for *sha* (a full sha or a prefix) already on
    the topic, else the first to arrive within *timeout_s*; None on timeout.
    With *fresh*, rows already on the topic only set the start: a wait on a
    rerun must not return the earlier attempt's row. A read that fails is
    retried until the deadline, then :class:`BoardUnreadableError`."""
    sha = sha.lower()
    deadline = clock() + timeout_s
    cursor: tuple[str, str] | None = None
    found: DoneRow | None = None

    def read(timeout: int = 0) -> list[TupleRow]:
        while True:
            try:
                return store.rd(subspace, n=_PAGE, since=cursor, timeout_s=timeout)
            except Exception as exc:  # noqa: BLE001 — any failed read is retried, then reported
                remaining = deadline - clock()
                if remaining <= 0:
                    raise BoardUnreadableError(f"{type(exc).__name__}: {exc}") from exc
                sleep(min(_RETRY_S, remaining))

    def advance(page: list[TupleRow]) -> None:
        nonlocal cursor
        last = page[-1]
        if last.created_at:
            cursor = (last.created_at, last.id)

    while True:  # what is already there, oldest first; keep the newest match
        page = read()
        if not page:
            break
        if not fresh:
            for row in page:
                found = _matches(row, sha) or found
        advance(page)
    while found is None:
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        page = read(max(1, min(park_s, PARK_CAP_S, int(remaining))))
        for row in page:
            found = _matches(row, sha) or found
        if page:
            advance(page)
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
    """The printed line and the exit code for a found row. When the row
    carries the adapter's ``outcome`` dim and it differs from the reduction of
    the row's own conclusions, both are printed and the exit is 4: the two
    copies of the rule have drifted, and neither is trusted."""
    outcome = reduce_outcome([r.conclusion for r in row.runs]) if row.runs else None
    line = result_line(row, outcome)
    if row.outcome_dim and outcome and row.outcome_dim != outcome:
        return f"{line} (outcome mismatch: the board says {row.outcome_dim})", EXIT_UNKNOWN
    return line, wait_exit_code(outcome)


__all__: list[str] = [
    "BoardUnreadableError", "DoneRow", "Run", "done_subspace", "fill_conclusions", "find_done_row",
    "gh_push_runs", "gh_run_conclusion", "parse_done_row", "reduce_outcome", "result_line",
    "summarize", "wait_exit_code",
    "EXIT_CANCELLED", "EXIT_FAILURE", "EXIT_SUCCESS", "EXIT_TIMEOUT", "EXIT_UNKNOWN", "EXIT_UNUSABLE",
]
