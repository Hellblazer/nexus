# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx ci wait <sha>``: one wake for a pushed sha, with the result (nexus-1d6pz.11.4).

A session launched without the Claude Code channel sees board posts only at
its next user prompt, so a session waiting on CI with nobody at the keyboard
never wakes. Run as a background command, this parks on the CI board's
``--done`` topic (``board/ci/<repo>-<branch>--done``) for the sha and exits
when its row arrives: the exit is the wake, and the one printed line is the
result. On develop and main it reads the tuple space only; for any other branch,
or a topic the engine refuses, it waits on GitHub check-runs instead (GitHub-only mode, below).

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

``--latest-attempt`` (:func:`wait_latest_attempt`) follows a re-run instead of
returning the replaced attempt's row: a row counts only when GitHub's push runs
show no newer attempt of any run in it.

A branch the board does not watch (anything but :data:`BOARD_BRANCHES`: a
release branch, a feature branch) has no ``--done`` topic, and its CI runs on
``pull_request`` events the push-run reads above never see. For those, and for
a watched branch whose topic the engine refuses as unknown, :func:`wait_on_github`
skips the board and reads the sha's check-runs (every event) once per
:data:`GITHUB_POLL_S` until all are completed or the timeout passes
(nexus-x4jok).
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import httpx

from nexus import ci_base
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
#: The branches the engine's CI adapter posts ``board/ci/<repo>-<branch>--done``
#: for (``nx tuple list`` shows only these two). Any other branch is waited on
#: through GitHub alone.
BOARD_BRANCHES: frozenset[str] = frozenset({"develop", "main"})
#: Seconds between reads of GitHub's check-runs in GitHub-only mode: one
#: request per interval, never faster, so a long wait stays far under the rate limit.
GITHUB_POLL_S: float = 60.0


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
    return f"board/ci/{ci_base.join(repo, branch)}--done"


class BoardUnreadableError(RuntimeError):
    """The board could not be read before the deadline (engine down, refused read)."""


class BoardSubspaceUnknownError(BoardUnreadableError):
    """The engine has no template for the topic: no adapter posts it for this branch."""


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


def _unknown_subspace(exc: BaseException) -> bool:
    from nexus.db.t2.http_tuple_store import UnknownSubspaceError  # noqa: PLC0415 — deferred: CLI startup cost
    return isinstance(exc, UnknownSubspaceError)


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
                if _unknown_subspace(exc):
                    raise BoardSubspaceUnknownError(f"{type(exc).__name__}: {exc}") from exc
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


def behind_latest(row: DoneRow, listed: list[Run]) -> list[Run]:
    """The runs GitHub's *listed* push runs show at a newer attempt than *row* knows: a re-run started
    after the row was written. A run of the row GitHub does not list is taken at the attempt the row
    names. For a capped row (:attr:`DoneRow.capped`), a listed run above the row's highest id on an
    attempt after the first is behind too: the cap dropped it, and a later attempt is a re-run."""
    newest: dict[int, Run] = {}
    for r in listed:
        if r.run_id not in newest or r.attempt > newest[r.run_id].attempt:
            newest[r.run_id] = r
    behind = [newest[r.run_id] for r in row.runs if r.run_id in newest and newest[r.run_id].attempt > r.attempt]
    if row.capped:
        top = max((r.run_id for r in row.runs), default=0)
        behind += [r for i, r in sorted(newest.items()) if i > top and r.attempt > 1]
    return behind


@dataclass(frozen=True)
class LatestAttempt:
    """What :func:`wait_latest_attempt` found. ``row``: the row to report, every run at its latest
    attempt (or, with ``unchecked``, a row GitHub was never readable to confirm). With no ``row``,
    ``stale`` is the newest row seen that a newer attempt replaced (None when no row came) and
    ``listed`` the last GitHub listing; ``settled`` says that listing showed every run finished on two
    consecutive reads, so the newer attempt's board row was dropped and GitHub stands for it."""

    row: DoneRow | None
    listed: list[Run] | None = None
    stale: DoneRow | None = None
    settled: bool = False
    unchecked: bool = False


def wait_latest_attempt(
    store: _Reader, subspace: str, sha: str, gh_repo: str, branch: str, *, timeout_s: float,
    park_s: int = PARK_CAP_S, clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    runs: Callable[[str, str, str], list[Run] | None] | None = None,
) -> LatestAttempt:
    """``nx ci wait --latest-attempt``: the ``--done`` row for *sha* whose every run is at its latest
    attempt, as GitHub's push runs for the sha on *branch* list them.

    :func:`find_done_row` gives the newest row already on the topic, or the first to arrive; after a
    re-run starts that is the replaced attempt's row. Each candidate is checked against GitHub, read
    by the row's full sha (so a prefix works) and never more often than :data:`GITHUB_POLL_S`:

    - no run behind (:func:`behind_latest`): the row is returned;
    - a run behind: the wait goes on, reading the board again (the newest row wins). When two reads
      in a row show every listed run finished, the newer attempt's board row was dropped and the
      result is ``settled`` (the caller reports from GitHub). At the deadline it is unsettled;
    - GitHub unreadable: retried at the next poll; a failed read never accepts a row. If no read
      worked by the deadline, the row comes back ``unchecked`` (the caller never reports it green);
      if a read already showed a newer attempt, the row is never returned (and a later read that lags
      back to an earlier attempt does not undo it: the highest attempt seen per run is kept).

    A GitHub listing that lags a re-run requested a moment ago still shows the earlier attempt, and
    its row is accepted: start the wait once GitHub shows the re-run (``gh run view``)."""
    fetch = runs or gh_push_runs
    deadline = clock() + timeout_s
    listed: list[Run] | None = None
    stale: DoneRow | None = None
    finished_reads = 0
    seen: dict[int, Run] = {}    # the highest attempt any read showed per run id: a lagging read cannot undo it
    while True:
        row = find_done_row(store, subspace, sha, timeout_s=max(0.0, deadline - clock()), park_s=park_s,
                            clock=clock, sleep=sleep)
        if row is None:
            return LatestAttempt(None, listed, stale)
        got = fetch(gh_repo, row.sha, branch)
        if got is not None:
            for r in got:
                if r.run_id not in seen or r.attempt >= seen[r.run_id].attempt:
                    seen[r.run_id] = r
            got = list(seen.values()) if seen else got
            listed = got
            if not behind_latest(row, got):
                return LatestAttempt(row, got)
            stale = row
            finished_reads = finished_reads + 1 if all(r.conclusion is not None for r in got) else 0
            if finished_reads >= 2:
                return LatestAttempt(None, got, row, settled=True)
        remaining = deadline - clock()
        if remaining <= 0:
            if stale is None and listed is None:
                return LatestAttempt(row, None, unchecked=True)
            return LatestAttempt(None, listed, stale or row)
        sleep(min(GITHUB_POLL_S, remaining))


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


@dataclass(frozen=True)
class CheckRun:
    check_id: int
    name: str
    status: str
    conclusion: str | None

    @property
    def completed(self) -> bool:
        return self.status == "completed"


class GhReadError(RuntimeError):
    """A GitHub read failed; *permanent* when a retry cannot help (gh missing, bad credentials,
    an unknown or unprocessable sha or repo)."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


#: The jq filter ``gh api --jq`` applies to each page: one ``[id, name, status, conclusion]``
#: array per line. Kept as a constant so a test runs the real filter over a recorded API page.
CHECK_RUNS_JQ: str = ".check_runs[] | [.id, .name, .status, .conclusion]"
#: A failed read that no retry fixes: HTTP 401 / 404 / 422 or gh asking to log in.
_PERMANENT_GH_ERROR = re.compile(r"HTTP (?:401|404|422)\b|gh auth login|authentication", re.IGNORECASE)
#: Consecutive failed reads with the same error that end a GitHub-only wait.
_GH_FAIL_STREAK: int = 3
#: Floor for one read's time limit, so a read begun at the deadline can still finish.
_MIN_READ_S: float = 5.0
_MAX_READ_S: float = 60.0


def gh_check_runs(gh_repo: str, sha: str, timeout: float = _MAX_READ_S) -> list[CheckRun]:
    """The latest check-run of each check on *sha*, whatever event started it (a push,
    a pull_request), read with ``gh api --paginate``. Raises :class:`GhReadError`
    when GitHub cannot be read or an output line is not a check-run."""
    try:
        out = run_bounded(
            ["gh", "api", f"repos/{gh_repo}/commits/{sha}/check-runs?per_page=100&filter=latest", "--paginate",
             "--jq", CHECK_RUNS_JQ],
            timeout=timeout, check=True, stdin=subprocess.DEVNULL,
        ).stdout
    except FileNotFoundError as exc:
        raise GhReadError(f"gh is not installed: {exc}", permanent=True) from exc
    except subprocess.CalledProcessError as exc:
        err = " ".join(str(exc.stderr or "").split())[:200]
        raise GhReadError(f"gh exited {exc.returncode}: {err or 'no message'}",
                          permanent=bool(_PERMANENT_GH_ERROR.search(err))) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise GhReadError(f"{type(exc).__name__}: {exc}") from exc
    runs: list[CheckRun] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        if not (isinstance(entry, list) and len(entry) == 4 and isinstance(entry[0], int)
                and not isinstance(entry[0], bool) and isinstance(entry[1], str) and isinstance(entry[2], str)):
            raise GhReadError(f"gh printed a line that is not a check-run: {line[:80]!r}")
        runs.append(CheckRun(entry[0], entry[1], entry[2], entry[3] if isinstance(entry[3], str) else None))
    return runs


def wait_on_github(
    gh_repo: str, sha: str, branch: str, *, timeout_s: float,
    clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
    fetch: Callable[[str, str, float], list[CheckRun]] | None = None,
) -> tuple[str, int]:
    """The printed line and exit code for a wait on GitHub alone (no board).

    Reads the sha's check-runs, never sooner than :data:`GITHUB_POLL_S` after the
    previous read (a read that would fall after the deadline is skipped, and the
    timeout is reported from the last one). A set that has a failure and no
    unfinished check-run is final. Any other completed set is reported only when
    the same check-run ids were completed on two consecutive reads, so a workflow
    that registers late cannot turn a partial set into a success. The outcome
    comes from :func:`reduce_outcome` and :func:`wait_exit_code`, as for a board row.
    A set with no check-run at all (CI not yet started) is not complete, and waits.

    A read that fails is retried at the next interval. :data:`EXIT_UNUSABLE` ends
    the wait at once when the failure is permanent (gh missing, HTTP 401, 404 or
    422, gh asking to log in), after :data:`_GH_FAIL_STREAK` consecutive failures
    with the same error, or at the deadline when no read ever succeeded.
    """
    read = fetch or gh_check_runs
    deadline = clock() + timeout_s
    mode = f"GitHub only: the board was not used for {branch}"
    url = f"https://github.com/{gh_repo}/commit/{sha}"
    head = sha[:12]
    runs: list[CheckRun] | None = None
    confirmed: frozenset[int] | None = None  # ids all completed on the previous read
    last_read: float | None = None
    last_error: str | None = None
    streak = 0
    while True:
        if last_read is not None:
            due = last_read + GITHUB_POLL_S
            if due > deadline:
                break
            if due > clock():
                sleep(due - clock())
        last_read = clock()
        try:
            runs = read(gh_repo, sha, min(_MAX_READ_S, max(deadline - last_read, _MIN_READ_S)))
        except GhReadError as exc:
            confirmed = None
            message = str(exc)
            streak = streak + 1 if message == last_error else 1
            last_error = message
            if exc.permanent or streak >= _GH_FAIL_STREAK:
                return f"{head} unusable: GitHub could not be read: {message} ({mode})", EXIT_UNUSABLE
            continue
        last_error, streak = None, 0
        if not runs or not all(r.completed for r in runs):
            confirmed = None
            continue
        outcome = reduce_outcome([r.conclusion for r in runs])
        ids = frozenset(r.check_id for r in runs)
        if outcome != "failure" and ids != confirmed:
            confirmed = ids
            continue
        bad = [r for r in runs if r.conclusion not in SUCCESS_CONCLUSIONS]
        detail = (
            f"{len(runs)} check-runs, all passed" if not bad else
            f"{len(runs)} check-runs, {len(bad)} not passed: "
            + ", ".join(f"{r.name} {r.conclusion or 'unknown'}" for r in bad)
        )
        return f"{head} {outcome or 'unknown'}: {detail} {url} ({mode})", wait_exit_code(outcome)
    if last_error is not None and runs is None:
        return f"{head} unusable: GitHub could not be read until the deadline: {last_error} ({mode})", EXIT_UNUSABLE
    if not runs:
        return f"{head} timeout: GitHub lists no check-runs for it yet ({mode})", EXIT_TIMEOUT
    if confirmed is not None:
        return f"{head} timeout: {len(runs)} check-runs completed, not yet confirmed by a second read ({mode})", EXIT_TIMEOUT
    pending = sum(1 for r in runs if not r.completed)
    return f"{head} timeout: {pending} of {len(runs)} check-runs still running ({mode})", EXIT_TIMEOUT


__all__: list[str] = [
    "BOARD_BRANCHES", "BoardSubspaceUnknownError", "BoardUnreadableError", "CheckRun", "GITHUB_POLL_S",
    "CHECK_RUNS_JQ", "GhReadError", "gh_check_runs", "wait_on_github", "DoneRow", "Run", "complete_runs", "done_subspace", "fill_conclusions", "find_done_row",
    "gh_push_runs", "gh_run_conclusion", "parse_done_row", "reduce_outcome", "result_line",
    "LatestAttempt", "behind_latest", "wait_latest_attempt",
    "summarize", "wait_exit_code",
    "EXIT_CANCELLED", "EXIT_FAILURE", "EXIT_SUCCESS", "EXIT_TIMEOUT", "EXIT_UNKNOWN", "EXIT_UNUSABLE",
]
