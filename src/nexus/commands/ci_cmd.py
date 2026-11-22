# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx ci``: CI wakes read from the CI board (nexus-1d6pz.11.4)."""
from __future__ import annotations

import math
import re
import subprocess
import time
import uuid
from typing import Any, NoReturn

import click

from nexus import ci_ack, ci_reds, ci_wait
from nexus.bounded_subprocess import run_bounded

_SHA = re.compile(r"[0-9a-fA-F]{7,40}")
_GH_REPO = re.compile(r"[\w.-]+/[\w.-]+")
#: A board topic's owner post: the base topic, ``--``, ``owner`` (RDR-252).
_OWNER_TOPIC = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*--owner")
#: Rows of the owner subspace one ``nx ci ack`` reads, newest first: far more than one red writes.
_ACK_READ_ROWS = 300
#: The longest note a disclaim carries: one line a threads entry or a terminal shows whole.
_NOTE_MAX = 200
#: The clock the unknown-subspace fallback measures the board wait with (a seam for tests).
_now = time.monotonic
#: The sleep between GitHub reads under --latest-attempt (a seam for tests).
_sleep = time.sleep


def _store():
    from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — deferred: CLI startup cost
    return HttpTupleStore()


@click.group(name="ci")
def ci_group() -> None:
    """CI results: develop and main from the CI board, any other branch from GitHub's check-runs."""


@ci_group.command(name="wait")
@click.argument("sha")
@click.option("--timeout", "timeout_min", type=click.FloatRange(min=0, max=24 * 60), default=60.0,
              show_default=True, help="Minutes to wait for the sha's --done row, or, in GitHub-only mode, for its "
                   "check-runs to complete (at most a day).")
@click.option("--fresh", is_flag=True,
              help="Count only a row that arrives after the wait starts: for a wait on a rerun, "
                   "while the earlier attempt's row is still on the topic. Ignored in GitHub-only mode.")
@click.option("--latest-attempt", is_flag=True,
              help="Follow a re-run: accept the sha's row only when GitHub shows no newer attempt of any run in "
                   "it, and otherwise wait for the newer attempt's row (GitHub read at most once a minute). A "
                   "green row GitHub could never confirm exits 4. Cannot be combined with --fresh (exit 5). "
                   "Ignored in GitHub-only mode, which reads the latest attempts already.")
@click.option("--repo", default=None,
              help="Repository part of the board topic [default: the repository name of the origin remote; "
                   "the wait refuses when there is none].")
@click.option("--branch", default="develop", show_default=True,
              help="Branch part of the board topic. Only develop and main have one; any other branch "
                   "(a release or feature branch) is waited on through GitHub's check-runs, not the board.")
@click.option("--gh-repo", default=None,
              help="owner/name for the GitHub reads (a row with no conclusions, a row capped by the "
                   "board's size limit, the one read at the deadline, and every read in GitHub-only mode) "
                   "[default: the origin remote's owner/name, when --repo is its repository; otherwise required].")
@click.option("--park-s", type=click.IntRange(1, ci_wait.PARK_CAP_S), default=ci_wait.PARK_CAP_S,
              hidden=True, help="Seconds per park (tests).")
def ci_wait_cmd(
    sha: str, timeout_min: float, fresh: bool, latest_attempt: bool, repo: str | None, branch: str, gh_repo: str | None,
    park_s: int,
) -> None:
    """Wait for SHA's CI runs to complete and print one line with the result.

    Parks on board/ci/<repo>-<branch>--done until the row for SHA (a full sha
    or a prefix of at least 7 hex digits) is there; a row already on the topic
    counts unless --fresh, and when a rerun gave the sha more than one row,
    the newest is used. --latest-attempt follows a re-run instead: a row counts
    only when GitHub's push runs show no newer attempt of any run in it, and
    the wait goes on for the newer attempt's row (GitHub read at most once a
    minute). Meant to run as a background command, so its exit
    wakes the session that pushed. The board wake is best effort, so at the
    deadline a full SHA is looked up on GitHub once: finished runs are
    reported as usual, unfinished ones as a timeout. A row the board capped
    at its size limit is completed from the same GitHub listing, once.

    Exit 0 when every run succeeded (neutral and skipped count), 1 on any
    failure (failure, timed_out, startup_failure, action_required), 3 when
    runs were cancelled or stale and none failed, 2 on timeout, 4 when a
    conclusion could not be read, the board's outcome disagrees with the
    conclusions, or the row lists fewer runs than its count (the board's
    size cap) and GitHub's runs for the sha are not exactly the missing ones,
    and 5 when the wait could not run (a bad SHA, no tuple store, a refused
    board read, or the board unreachable until the deadline).
    Click's own usage errors (a missing SHA, --timeout out of range) exit 2.

    GitHub-only mode: the board has topics for develop and main only. For any
    other --branch (a release or feature branch), or when the engine refuses
    the topic as unknown, the board is skipped and the wait reads SHA's
    check-runs from GitHub (all events, so a pull_request's CI counts), never
    sooner than 60 seconds after the previous read, until every check-run has
    completed or --timeout passes. A finished set that includes a failure is
    reported at once; any other is reported only when two consecutive reads
    (60 s apart) list the same completed check-runs, so a workflow that
    registers within one read interval is not missed. The timeout counts from the start of the
    command, a board wait that fell back included. The result line says
    "GitHub only: the board was not used". Exit codes are the same: 0 all
    passed, 1 any failed, 3 cancelled and none failed, 4 a conclusion this
    table does not know, 2 timeout (check-runs still running or unconfirmed,
    or none yet). Exit 5 when GitHub cannot be read: at once for gh missing,
    HTTP 401, 404 or 422 or a login prompt, after 3 consecutive failed reads
    with the same error, or at the deadline when no read ever worked; the
    line names the last error. --fresh has no meaning without the board and is
    ignored; SHA may be a prefix, which GitHub resolves; --gh-repo must be
    OWNER/NAME (anything else exits 5).
    """
    if fresh and latest_attempt:
        # Exit 5 (unusable), not click's 2, which this command uses for a timeout.
        click.echo(f"{sha} unusable: --fresh and --latest-attempt cannot be combined (--latest-attempt skips a "
                   f"replaced attempt's row once GitHub lists the re-run)")
        raise SystemExit(ci_wait.EXIT_UNUSABLE)
    if not math.isfinite(timeout_min):
        click.echo(f"{sha} unusable: --timeout must be a number of minutes")
        raise SystemExit(ci_wait.EXIT_UNUSABLE)
    if not _SHA.fullmatch(sha):
        click.echo(f"{sha} unusable: give a commit sha, 7 to 40 hex digits")
        raise SystemExit(ci_wait.EXIT_UNUSABLE)
    sha = sha.lower()
    # No default repo and no default owner: the origin remote names both, and when it cannot the flag is named.
    origin = _slug_from_origin(_origin_url())
    if repo is None:
        if origin is None:
            click.echo(f"{sha[:12]} unusable: cannot tell which repo's board to wait on: no origin remote with a repo "
                       "name here; give --repo")
            raise SystemExit(ci_wait.EXIT_UNUSABLE)
        repo = origin[1]
    if gh_repo is not None:
        gh = gh_repo
    elif origin is not None and origin[1] == repo:
        gh = f"{origin[0]}/{repo}"
    else:
        click.echo(f"{sha[:12]} unusable: cannot tell the GitHub owner of {repo}: it is not the origin remote's "
                   "repository; give --gh-repo OWNER/NAME")
        raise SystemExit(ci_wait.EXIT_UNUSABLE)
    if not _GH_REPO.fullmatch(gh) or any(set(part) <= {"."} for part in gh.split("/")):
        click.echo(f"{sha[:12]} unusable: the GitHub repository must be OWNER/NAME, got {gh!r}")
        raise SystemExit(ci_wait.EXIT_UNUSABLE)
    from nexus.identity.work import Label  # noqa: PLC0415 — deferred: CLI startup cost

    # RDR-253 Step 6 part 1: the wait is background work the session owns, so a checkpoint and the restart report list
    # it by identity (kind wait, ref ci:<branch>@<sha>). A label only: written best effort with a short timeout and
    # ended in the finally below, so it never fails or delays the result. A TERM or HUP ends the row too.
    label = Label("wait", f"ci:{branch}@{sha}")
    label.start()
    try:
        try:
            label.arm_sigterm()
        except Exception:  # noqa: BLE001 — a label never fails the wait it labels (the row still ends in the finally)
            pass
        _wait_for_ci(sha, timeout_min, fresh, latest_attempt, repo, branch, gh, park_s)
    finally:
        label.finish()


def _wait_for_ci(sha: str, timeout_min: float, fresh: bool, latest_attempt: bool, repo: str, branch: str, gh: str,
                 park_s: int) -> NoReturn:
    """The wait itself, after ``nx ci wait`` has checked its arguments: always ends in ``SystemExit``."""
    if branch not in ci_wait.BOARD_BRANCHES:
        _github_only(sha, gh, branch, timeout_min * 60)
    subspace = ci_wait.done_subspace(repo, branch)
    try:
        store = _store()
    except Exception as exc:  # noqa: BLE001 — no store, no wait: say so, never exit 1 ("a run failed")
        click.echo(f"{sha[:12]} unusable: no tuple store: {type(exc).__name__}: {exc}")
        raise SystemExit(ci_wait.EXIT_UNUSABLE) from None
    started = _now()
    latest: ci_wait.LatestAttempt | None = None
    try:
        if latest_attempt:
            latest = ci_wait.wait_latest_attempt(
                store, subspace, sha, gh, branch, timeout_s=timeout_min * 60, park_s=park_s, clock=_now,
                sleep=_sleep,
            )
            row = latest.row
        else:
            row = ci_wait.find_done_row(
                store, subspace, sha, timeout_s=timeout_min * 60, fresh=fresh, park_s=park_s,
            )
    except ci_wait.BoardSubspaceUnknownError:
        _github_only(sha, gh, branch, max(0.0, timeout_min * 60 - (_now() - started)))
    except ci_wait.BoardUnreadableError as exc:
        click.echo(f"{sha[:12]} unusable: {subspace} could not be read: {exc}")
        raise SystemExit(ci_wait.EXIT_UNUSABLE) from None
    if row is None and latest is not None and latest.stale is not None:
        line, code = _latest_without_row(latest, gh, branch, subspace, timeout_min)
        click.echo(line)
        raise SystemExit(code)
    if row is None:
        if fresh:
            # GitHub cannot tell a rerun that has not started from the earlier
            # attempt it would replace, so a --fresh wait never reads it.
            click.echo(f"{sha[:12]} timeout: no new row on {subspace} after {timeout_min:g} min (--fresh)")
            raise SystemExit(ci_wait.EXIT_TIMEOUT)
        line, code = _at_deadline(sha, gh, branch, subspace, timeout_min)
        click.echo(line)
        raise SystemExit(code)
    line, code = ci_wait.summarize(ci_wait.complete_runs(ci_wait.fill_conclusions(row, gh), gh, branch))
    if latest is not None and latest.unchecked:
        # GitHub was never readable, so a newer attempt cannot be ruled out: never report this row green.
        line += " (latest attempt not checked: GitHub could not be read"
        line += "; not reported as success)" if code == ci_wait.EXIT_SUCCESS else ")"
        code = ci_wait.EXIT_UNKNOWN if code == ci_wait.EXIT_SUCCESS else code
    click.echo(line)
    raise SystemExit(code)


def _latest_without_row(latest: ci_wait.LatestAttempt, gh_repo: str, branch: str, subspace: str,
                        timeout_min: float) -> tuple[str, int]:
    """--latest-attempt with only a replaced attempt's row: report the latest attempts from the last
    GitHub listing when they have finished (the newer row was dropped), else a timeout. The full sha
    is the row's, so a prefix given on the command line works here too."""
    stale = latest.stale
    assert stale is not None
    runs = latest.listed
    if runs is None or not latest.settled:
        # Not settled: the last listing may be older than reads that failed since, so it never decides a
        # result. A fresh deadline read does, as without the flag (unreadable or unfinished: a timeout).
        line, code = _at_deadline(stale.sha, gh_repo, branch, subspace, timeout_min)
        return line.replace(f"no row on {subspace}", f"only a replaced attempt's row on {subspace}", 1), code
    line, code = ci_wait.summarize(
        ci_wait.DoneRow("", stale.sha, tuple(runs), f"https://github.com/{gh_repo}/commit/{stale.sha}"),
    )
    return f"{line} (no board row for the latest attempt: read from GitHub)", code


def _github_only(sha: str, gh_repo: str, branch: str, timeout_s: float) -> NoReturn:
    """Wait on GitHub's check-runs alone, print the one line and exit."""
    line, code = ci_wait.wait_on_github(gh_repo, sha, branch, timeout_s=timeout_s)
    click.echo(line)
    raise SystemExit(code)


def _at_deadline(sha: str, gh_repo: str, branch: str, subspace: str, timeout_min: float) -> tuple[str, int]:
    """No row by the deadline: one GitHub read of the sha's push runs tells a
    dropped board wake (every run finished) from CI still running."""
    head = f"{sha[:12]} timeout: no row on {subspace} after {timeout_min:g} min"
    if len(sha) != 40:
        return f"{head}; give the full sha to check GitHub at the deadline", ci_wait.EXIT_TIMEOUT
    runs = ci_wait.gh_push_runs(gh_repo, sha, branch)
    if runs is None:
        return f"{head}; GitHub could not be read", ci_wait.EXIT_TIMEOUT
    if not runs:
        return f"{head}; GitHub lists no push runs for it", ci_wait.EXIT_TIMEOUT
    unfinished = [r for r in runs if r.conclusion is None]
    if unfinished:
        return f"{head}; {len(unfinished)} of {len(runs)} runs still running", ci_wait.EXIT_TIMEOUT
    line, code = ci_wait.summarize(
        ci_wait.DoneRow("", sha, tuple(runs), f"https://github.com/{gh_repo}/commit/{sha}"),
    )
    return f"{line} (no board row: the wake was dropped; read from GitHub)", code


def _current_red(store: Any, subspace: str, topic: str) -> ci_reds.Episode | None:
    """The red open on *subspace*: the last red the rows split into (:func:`nexus.ci_reds.split`, the split
    ``nx group ack-times`` uses), unless a clear row ended it. A red the newest owner row's ``episode_at`` replaced is
    not open even with no clear row between."""
    rows = store.rd(subspace, {"topic": topic}, n=_ACK_READ_ROWS, order="desc")
    reds, _ = ci_reds.split(list(rows))
    return reds[-1] if reds and reds[-1].cleared is None else None


def _origin_url(cwd: Any = None) -> str:
    """The URL of the ``origin`` remote of the repository *cwd* (default: the current directory) is in, or "" when there
    is none, no git, or no repository. A seam: a test replaces it."""
    try:
        done = run_bounded(["git", "remote", "get-url", "origin"], cwd=cwd, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _slug_from_origin(url: str) -> tuple[str, str] | None:
    """The ``(owner, repo)`` an origin URL ends in: its last two path segments, ``.git`` and a trailing slash dropped.
    None for a URL with no usable pair. Works for ``git@host:owner/repo.git``, ``https://host/owner/repo`` and
    ``ssh://`` forms."""
    match = re.search(r"[/:]([^/:@\s]+)/([^/:@\s]+?)(?:\.git)?/?$", url.strip())
    if match is None or any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", g) is None for g in match.groups()):
        return None
    return match.group(1), match.group(2)


def _repo_from_origin(url: str) -> str | None:
    """The repository name an origin URL ends in, as the board names it (``board/ci/<repo>-<branch>--...``, the repo
    part of ``nx ci wait --repo``). None for a URL with no usable name."""
    slug = _slug_from_origin(url)
    return slug[1] if slug else None


def _board_of_origin(store: Any, repo: str) -> str:
    """The owner post of *repo*'s one board with a red open: ``<repo>-<branch>--owner`` for each branch the CI adapter
    posts boards for (:data:`nexus.ci_wait.BOARD_BRANCHES`). Refuses when no board or more than one has an open red:
    an ack has no push branch to tell them apart, and a guess could acknowledge the wrong red."""
    bases = [f"{repo}-{branch}" for branch in sorted(ci_wait.BOARD_BRANCHES)]
    open_bases = [b for b in bases if _current_red(store, f"board/ci/{b}--owner", f"{b}--owner") is not None]
    if len(open_bases) == 1:
        return f"{open_bases[0]}--owner"
    checked = ", ".join(bases)
    if open_bases:
        raise click.ClickException(f"the origin repo {repo} has an open red on more than one board ({', '.join(open_bases)}); "
                                   "name the one with --base")
    raise click.ClickException(f"no open red on a board of the origin repo {repo} ({checked}); if the red is on "
                               "another board, name it with --base")


@ci_group.command(name="ack")
@click.argument("sha")
@click.option("--base", default=None,
              help="The board base whose red this is: the owner post is board/ci/<BASE>--owner, as in "
                   "<repo>-develop. Without --base or --topic the repo is read from the origin remote and its one "
                   "board with an open red is used; with none or several the command refuses and asks for --base.")
@click.option("--topic", default=None,
              help="The whole owner post, board/ci/<TOPIC>: a base, a double dash and `owner` (the same as --base "
                   "with `--owner` added; give one of the two).")
@click.option("--not-mine", "not_mine", is_flag=True,
              help="Say the red is not yours instead of acknowledging it: post a `disclaim` row for SHA. The engine "
                   "does not act on it yet, so it is recorded only; from the engine cut that reads it, a disclaim "
                   "from the red's current owner hands the red on at once, as a missed deadline does, and one from "
                   "anyone else is ignored.")
@click.option("--note", default=None,
              help=f"With --not-mine, a line saying why or who has it (at most {_NOTE_MAX} characters, no control "
                   "characters). Shown by `nx group ack-times`.")
def ci_ack_cmd(sha: str, base: str | None, topic: str | None, not_mine: bool, note: str | None) -> None:
    """Acknowledge a red develop: post the owner's ack for SHA, signed with this session's identity id.

    With --not-mine the row is a `disclaim` instead: "this red is not mine". It is recorded only until the engine cut
    that reads it; from then on a disclaim from the red's current owner hands the red on at once, without waiting for
    the acknowledgement deadline. It takes the same SHA, the same owner check and the same identity as an ack. An ack
    only says "seen", and a disclaim says "someone else should take it".

    SHA is the red's sha or a prefix of at least 7 hex digits; it is resolved against the tips of the CURRENT red on
    board/ci/<TOPIC>: the shas its owner rows carry, since its first row (a red that a newer one replaced, or a clear row
    ended, is not open). The command refuses a sha that is no such tip, an ambiguous prefix, and a caller who is not the
    red's current owner (an ack from anyone else counts for nobody, so posting one would only look like an ack). The row
    is `kind=ack`, `from=<your identity id>` (never a name), the full sha and a fresh nonce. The engine counts an ack by
    the sender-session stamp the board gives the row, which is this session's. With no identity the command refuses;
    `nx identity show` says whether this session holds one.
    """
    if note is not None and not not_mine:
        raise click.ClickException("--note goes with --not-mine: an ack carries no note")
    if note is not None and (len(note) > _NOTE_MAX or not note.isprintable()):
        raise click.ClickException(f"the note must be a single line of printable characters, at most {_NOTE_MAX} long")
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        raise click.ClickException(f"{sha!r} is not a commit sha: give 7 to 40 hex digits")
    if base is not None and topic is not None:
        raise click.ClickException("give --base or --topic, not both: --topic is the base with `--owner` added")
    if base is not None:
        if base.endswith("--owner") or not _OWNER_TOPIC.fullmatch(f"{base}--owner"):
            raise click.ClickException(f"{base!r} is not a board base: give the bare base, as in <repo>-develop")
        topic = f"{base}--owner"
    if topic is not None and not _OWNER_TOPIC.fullmatch(topic):
        raise click.ClickException(f"{topic!r} is not an owner post: name it <base topic>--owner, as in <repo>-develop--owner")
    repo = None
    if topic is None:
        # No board named: the origin remote names the repo, and never anything else does (no default repo).
        repo = _repo_from_origin(_origin_url())
        if repo is None:
            raise click.ClickException("cannot tell which repo's board this is: no origin remote with a repo name here; "
                                       "name the board with --base <repo>-<branch>, as in --base <repo>-develop")
    try:
        identity = ci_ack.counted_identity()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if not identity:
        raise click.ClickException(ci_ack.NO_IDENTITY_MESSAGE)
    try:
        store = _store()
        if topic is None:
            assert repo is not None
            topic = _board_of_origin(store, repo)
        subspace = f"board/ci/{topic}"
        red = _current_red(store, subspace, topic)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 -- CLI boundary: one line and exit 1
        raise click.ClickException(f"could not read the board: {type(exc).__name__}: {exc}") from exc
    if red is None:
        raise click.ClickException(f"no open red on {subspace}: nothing to {'disclaim' if not_mine else 'acknowledge'}")
    latest = red.rows[-1][2]
    owner = latest.get("owner")
    what, fate = ("a disclaim", "the engine would ignore it") if not_mine else ("an ack", "counts for nobody")
    if not owner:
        raise click.ClickException(f"the red on {subspace} has no owner; {what} from {identity} {fate}")
    if owner != identity:
        named = f"{owner} ({latest['owner_alias']})" if latest.get("owner_alias") else str(owner)
        raise click.ClickException(f"the red is owned by {named}; {what} from {identity} {fate}")
    tips = {str((row.dims or {}).get("sha") or "").lower() for _, row, _ in red.rows}
    tips = {t for t in tips if re.fullmatch(r"[0-9a-f]{7,64}", t)}
    found = sorted(t for t in tips if t.startswith(sha.lower()))
    if len(found) != 1:
        named = ", ".join(sorted(t[:7] for t in tips))
        raise click.ClickException(
            f"{sha} is {'ambiguous among' if found else 'not one of'} the red tips on {subspace} ({named})")
    full = found[0]
    kind = "disclaim" if not_mine else "ack"
    try:
        row_id = store.out(subspace, {"topic": topic}, {"kind": kind, "from": identity, "sha": full},
                           body=note or None, nonce=uuid.uuid4().hex)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary: one line and exit 1
        raise click.ClickException(f"could not post the {kind}: {type(exc).__name__}: {exc}") from exc
    if not_mine:
        click.echo(f"disclaimed {full[:7]} on {subspace} as {identity} ({row_id[:12]}); recorded only until the engine "
                   "cut that reads it hands the red on")
        return
    click.echo(f"acked {full[:7]} on {subspace} as {identity} ({row_id[:12]})")
