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
row arrives. The body's ``n`` counts every run in the fold; the adapter's
1024-byte cap drops trailing entries, so when ``n`` is more than the runs
listed and none of them failed, the sha's push runs are read from GitHub
once and used only when they are the fold by identity (:func:`complete_runs`);
otherwise the result is unknown.

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
from urllib.parse import quote

import httpx

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
    #: The body's ``n``: how many runs the fold holds. More than ``len(runs)``
    #: when the adapter's 1024-byte cap dropped trailing entries; None on a row
    #: written before .11.1, which is never capped.
    total: int | None = None

    @property
    def capped(self) -> bool:
        return self.total is not None and self.total > len(self.runs)


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
    except (TypeError, ValueError, OverflowError):
        return None
    total = body.get("n")
    if total is not None and (
        not isinstance(total, int) or isinstance(total, bool) or total < max(1, len(runs))
    ):
        return None
    outcome = row.dims.get("outcome") if isinstance(row.dims, dict) else None
    return DoneRow(row.id, body["sha"].lower(), tuple(runs), str(body.get("url") or ""), outcome, total)


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


def gh_push_runs(gh_repo: str, sha: str, branch: str) -> list[Run] | None:
    """The push runs GitHub lists for *sha* (a full sha) on *branch*, each
    with its conclusion or None while it runs; None when GitHub cannot be read
    or the list may be cut short. Read at most once per wait: at the
    deadline, to tell a dropped board wake from CI still running, or for a
    capped row (:func:`complete_runs`). The branch filter keeps out the same
    sha's runs elsewhere (main after a promotion, a release tag)."""
    got = _gh_json(
        f"repos/{gh_repo}/actions/runs?head_sha={sha}&event=push&branch={quote(branch, safe='')}&per_page=100",
        "[.total_count, [.workflow_runs[] | [.id, .run_attempt, .name, .conclusion, .head_branch]]]",
    )
    if not (isinstance(got, list) and len(got) == 2 and isinstance(got[1], list)):
        return None
    total, rows = got
    if not isinstance(total, int) or total > len(rows):
        return None
    got = [r for r in rows if isinstance(r, list) and len(r) == 5 and r[4] == branch]
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
    return DoneRow(row.tuple_id, row.sha, tuple(runs), row.url, row.outcome_dim, row.total)


def complete_runs(
    row: DoneRow, gh_repo: str, branch: str,
    fetch: Callable[[str, str, str], list[Run] | None] | None = None,
) -> DoneRow:
    """A capped row with the runs the cap dropped, read from GitHub once.

    The adapter folds each run id once, at one attempt, sorts the fold by run
    id and drops whole trailing entries, so the missing runs are the highest
    run ids. A listed failure already settles the outcome, and an uncapped row
    is whole: neither reads GitHub. Otherwise GitHub's push runs for the sha on
    *branch* are accepted only when they are the fold, by identity and not just
    by count: exactly ``total`` runs, the ids up to the highest listed one
    exactly the listed ids, each listed run at the attempt the row names (a
    newer attempt is a rerun that started after the row), and each dropped run
    on its first attempt (a later attempt cannot be told from a rerun since
    the fold). Anything else leaves the row capped, which :func:`summarize`
    reports as unknown: a capped row whose dropped run was rerun stays exit 4.

    Why the listing can stand for the fold (bob, conexus 051060d): "A --done
    fold's members are the runs in GitHub's actions/runs?head_sha=<sha>&event=push
    listing, at fold time, whose head_branch equals the branch and
    head_repository equals the repo. The triggering run is added if the listing
    lagged it. No workflow or other event filter applies." A later listing is
    therefore a superset of the fold, and a run created after it has a higher
    id: it makes more than ``total`` runs, never a wrong one.
    """
    if not row.capped or any(r.conclusion in FAILURE_CONCLUSIONS for r in row.runs):
        return row
    listed = (fetch or gh_push_runs)(gh_repo, row.sha, branch)
    if listed is None:
        return row
    by_id = {r.run_id: r for r in listed}
    if len(by_id) != len(listed) or len(listed) != row.total:
        return row
    ids = {r.run_id for r in row.runs}
    top = max(ids, default=0)
    if {i for i in by_id if i <= top} != ids or any(by_id[r.run_id].attempt != r.attempt for r in row.runs):
        return row
    dropped = sorted((r for r in listed if r.run_id > top), key=lambda r: r.run_id)
    if any(r.attempt != 1 for r in dropped):
        return row
    return DoneRow(row.tuple_id, row.sha, row.runs + tuple(dropped), row.url, row.outcome_dim, row.total)


def _matches(row: TupleRow, sha: str) -> DoneRow | None:
    done = parse_done_row(row)
    return done if done is not None and done.sha.startswith(sha) else None


def _transient(exc: BaseException) -> bool:
    """A failed read worth retrying: the engine unreachable, timing out, or
    answering 5xx or 429. Anything else (auth, a refused read, a bug) is
    reported at once rather than retried for the whole timeout."""
    if isinstance(exc, (httpx.TransportError, ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return False


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
            except Exception as exc:  # noqa: BLE001 — classified below: retried if transient, else reported
                remaining = deadline - clock()
                if not _transient(exc) or remaining <= 0:
                    raise BoardUnreadableError(f"{type(exc).__name__}: {exc}") from exc
                sleep(min(_RETRY_S, remaining))

    def advance(page: list[TupleRow]) -> None:
        nonlocal cursor
        last = next((r for r in reversed(page) if r.created_at), None)
        moved = (last.created_at, last.id) if last is not None and last.created_at else None
        if moved is None or moved == cursor:
            raise BoardUnreadableError(f"{subspace}: a page of rows with no created_at; the read cannot advance")
        cursor = moved

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
    if row.capped and outcome != "failure":
        # The runs the cap dropped are unread: only a listed failure settles it.
        outcome = None
    line = result_line(row, outcome)
    if row.capped:
        rest = "" if outcome == "failure" else "; GitHub did not confirm the rest"
        line += f" ({len(row.runs)} of {row.total} runs listed{rest})"
    if row.outcome_dim and outcome and row.outcome_dim != outcome:
        return f"{line} (outcome mismatch: the board says {row.outcome_dim})", EXIT_UNKNOWN
    return line, wait_exit_code(outcome)


__all__: list[str] = [
    "BoardUnreadableError", "DoneRow", "Run", "complete_runs", "done_subspace", "fill_conclusions", "find_done_row",
    "gh_push_runs", "gh_run_conclusion", "parse_done_row", "reduce_outcome", "result_line",
    "summarize", "wait_exit_code",
    "EXIT_CANCELLED", "EXIT_FAILURE", "EXIT_SUCCESS", "EXIT_TIMEOUT", "EXIT_UNKNOWN", "EXIT_UNUSABLE",
]
