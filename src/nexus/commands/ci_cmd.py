# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx ci``: CI wakes read from the CI board (nexus-1d6pz.11.4)."""
from __future__ import annotations

import math
import re
import time
import uuid
from typing import Any, NoReturn

import click

from nexus import ci_ack, ci_reds, ci_wait

_SHA = re.compile(r"[0-9a-fA-F]{7,40}")
_GH_REPO = re.compile(r"[\w.-]+/[\w.-]+")
#: A board topic's owner post: the base topic, ``--``, ``owner`` (RDR-252).
_OWNER_TOPIC = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*--owner")
#: Rows of the owner subspace one ``nx ci ack`` reads, newest first: far more than one red writes.
_ACK_READ_ROWS = 300
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
@click.option("--repo", default="nexus", show_default=True, help="Repository part of the board topic.")
@click.option("--branch", default="develop", show_default=True,
              help="Branch part of the board topic. Only develop and main have one; any other branch "
                   "(a release or feature branch) is waited on through GitHub's check-runs, not the board.")
@click.option("--gh-repo", default=None,
              help="owner/name for the GitHub reads (a row with no conclusions, a row capped by the "
                   "board's size limit, the one read at the deadline, and every read in GitHub-only mode) "
                   "[default: Hellblazer/<repo>].")
@click.option("--park-s", type=click.IntRange(1, ci_wait.PARK_CAP_S), default=ci_wait.PARK_CAP_S,
              hidden=True, help="Seconds per park (tests).")
def ci_wait_cmd(
    sha: str, timeout_min: float, fresh: bool, latest_attempt: bool, repo: str, branch: str, gh_repo: str | None,
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
    gh = gh_repo or f"Hellblazer/{repo}"
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


@ci_group.command(name="ack")
@click.argument("sha")
@click.option("--topic", default="nexus-develop--owner", show_default=True,
              help="The owner post to acknowledge on: board/ci/<TOPIC>. A base topic, a double dash and `owner`.")
def ci_ack_cmd(sha: str, topic: str) -> None:
    """Acknowledge a red develop: post the owner's ack for SHA, signed with this session's identity id.

    SHA is the red's sha or a prefix of at least 7 hex digits; it is resolved against the tips of the CURRENT red on
    board/ci/<TOPIC>: the shas its owner rows carry, since its first row (a red that a newer one replaced, or a clear row
    ended, is not open). The command refuses a sha that is no such tip, an ambiguous prefix, and a caller who is not the
    red's current owner (an ack from anyone else counts for nobody, so posting one would only look like an ack). The row
    is `kind=ack`, `from=<your identity id>` (never a name), the full sha and a fresh nonce. The engine counts an ack by
    the sender-session stamp the board gives the row, which is this session's. With no identity the command refuses;
    `nx identity show` says whether this session holds one.
    """
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        raise click.ClickException(f"{sha!r} is not a commit sha: give 7 to 40 hex digits")
    if not _OWNER_TOPIC.fullmatch(topic):
        raise click.ClickException(f"{topic!r} is not an owner post: name it <base topic>--owner, as in nexus-develop--owner")
    try:
        identity = ci_ack.counted_identity()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if not identity:
        raise click.ClickException(ci_ack.NO_IDENTITY_MESSAGE)
    subspace = f"board/ci/{topic}"
    try:
        store = _store()
        red = _current_red(store, subspace, topic)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary: one line and exit 1
        raise click.ClickException(f"could not read {subspace}: {type(exc).__name__}: {exc}") from exc
    if red is None:
        raise click.ClickException(f"no open red on {subspace}: nothing to acknowledge")
    latest = red.rows[-1][2]
    owner = latest.get("owner")
    if not owner:
        raise click.ClickException(f"the red on {subspace} has no owner; an ack from {identity} counts for nobody")
    if owner != identity:
        named = f"{owner} ({latest['owner_alias']})" if latest.get("owner_alias") else str(owner)
        raise click.ClickException(f"the red is owned by {named}; an ack from {identity} counts for nobody")
    tips = {str((row.dims or {}).get("sha") or "").lower() for _, row, _ in red.rows}
    tips = {t for t in tips if re.fullmatch(r"[0-9a-f]{7,64}", t)}
    found = sorted(t for t in tips if t.startswith(sha.lower()))
    if len(found) != 1:
        named = ", ".join(sorted(t[:7] for t in tips))
        raise click.ClickException(
            f"{sha} is {'ambiguous among' if found else 'not one of'} the red tips on {subspace} ({named})")
    full = found[0]
    try:
        row_id = store.out(subspace, {"topic": topic}, {"kind": "ack", "from": identity, "sha": full},
                           nonce=uuid.uuid4().hex)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary: one line and exit 1
        raise click.ClickException(f"could not post the ack: {type(exc).__name__}: {exc}") from exc
    click.echo(f"acked {full[:7]} on {subspace} as {identity} ({row_id[:12]})")

#: The exit code ``nx ci check-hold`` refuses with; ``scripts/git-push-develop.sh`` passes it on as its own.
CHECK_HOLD_REFUSED = 13


def _push_base(base: str | None, remote: str, branch: str | None) -> str:
    """The board base of a push: *base* as given, else ``<repo>-<branch>`` from *remote*'s URL (:mod:`nexus.ci_base`); a base
    that cannot be derived is a usage error that names ``--base``."""
    from nexus import ci_base  # noqa: PLC0415 -- deferred: CLI startup cost

    if base:
        if not ci_base.BASE.fullmatch(base):
            raise click.UsageError(f"--base {base!r} is not a board topic base: use <repo>-<branch>, as in nexus-develop")
        return base
    try:
        return ci_base.derive(remote, branch)
    except ci_base.BaseUnderivableError as exc:
        raise click.UsageError(str(exc)) from exc


_BASE_OPTIONS = (
    click.option("--base", default=None, help="The board topic base, <repo>-<branch> (nexus-develop): board/ci/<BASE>--owner and "
                 "--push. Without it the base is derived from the --remote URL's repo name and --branch; when that cannot be "
                 "done the command refuses and names this flag."),
    click.option("--remote", default="origin", show_default=True, help="The remote whose URL names the repo when --base is not given."),
    click.option("--branch", default=None, help="The branch being pushed, for the derived base."),
)


def _base_options(fn: Any) -> Any:
    for option in reversed(_BASE_OPTIONS):
        fn = option(fn)
    return fn


@ci_group.command(name="check-hold")
@_base_options
@click.option("--tip", "tips", multiple=True, help="A commit of the push, or the sha the push ends at (repeatable); named in the refusal.")
@click.option("--red-fix", default=None, help="Push past the hold, on the record: the reason, printed as PUSH_RED_FIX.")
def ci_check_hold_cmd(base: str | None, remote: str, branch: str | None, tips: tuple[str, ...], red_fix: str | None) -> None:
    """Whether this session may push now (RDR-252, the push script's hold-your-pushes check).

    While a red is open on board/ci/<BASE>--owner, only its owner and the pusher of one of its suspects may push (a suspect's
    pusher is the identity its push record's sender-session stamp maps to; `from` is never read; a caller with no identity is
    held like any other). Anyone else is refused: PUSH_REFUSED_DEVELOP_RED on standard output and exit code 13. `--red-fix
    <reason>` pushes past it and prints PUSH_RED_FIX with the reason (the push script writes it into the push record). A
    failed read of the red, or of the records, warns (PUSH_GATE_UNCHECKED on standard error) and exits 0.
    """
    from nexus import ci_push_gate  # noqa: PLC0415 -- deferred: CLI startup cost

    base = _push_base(base, remote, branch)
    try:
        identity = ci_ack.counted_identity()
    except ValueError:
        identity = ""
    try:
        verdict = ci_push_gate.check(_store(), base, identity)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary: a read that failed holds nobody
        click.echo(f"PUSH_GATE_UNCHECKED could not read board/ci/{base}--owner: {type(exc).__name__}: {exc}", err=True)
        if red_fix:
            click.echo(f"PUSH_RED_FIX reason={red_fix} (not needed: the red could not be read)")
        return
    who = f"{verdict.owner or 'no owner'}" + (f" ({verdict.owner_alias})" if verdict.owner_alias else "")
    if verdict.refuse and red_fix:
        click.echo(f"PUSH_RED_FIX reason={red_fix} ({base} is red at {verdict.sha}, owned by {who}; the hold was overridden)")
        return
    if verdict.refuse:
        ending = f" ({tips[-1][:12]})" if tips else ""
        click.echo(f"PUSH_REFUSED_DEVELOP_RED {base} is red at {verdict.sha} and owned by {who}; this push{ending} is from neither the "
                   "owner nor the pusher of one of its suspects.")
        click.echo("Hold your pushes until the red clears, or hand the fix to the owner. A push that is the fix: "
                   "--red-fix '<reason>', on the record.")
        raise SystemExit(CHECK_HOLD_REFUSED)
    if verdict.unchecked:
        click.echo(f"PUSH_GATE_UNCHECKED {verdict.detail}", err=True)
    if red_fix:
        click.echo(f"PUSH_RED_FIX reason={red_fix} (not needed: {verdict.detail})")


@ci_group.command(name="record-push")
@_base_options
@click.option("--from-sha", required=True, help="The branch tip the push replaced (the record's `base`); may be empty.")
@click.option("--to-sha", required=True, help="The tip the push landed.")
@click.option("--count", type=click.IntRange(min=0), default=None, help="How many commits the push carried (the record's `n`).")
@click.option("--red-fix", default=None, help="The reason a push overrode the develop-red hold: written into the record body as red_fix.")
def ci_record_push_cmd(base: str | None, remote: str, branch: str | None, from_sha: str, to_sha: str, count: int | None,
                       red_fix: str | None) -> None:
    """Write the push record of a push that landed (RDR-252): one tuple on board/ci/<BASE>--push.

    The record's dims are from=<your identity id, or unknown>, kind=push, sha=<TO_SHA>; its body {base: FROM_SHA, n, red_fix}.
    The owner stamper links base to tip to order the branch's tips and maps the record's sender-session stamp to the pusher.
    A failed stamped write is retried once as a minimal unstamped record (base, tip, from), so the chain keeps its link. One
    status line on standard output: PUSH_RECORD_OK, PUSH_RECORD_FALLBACK or PUSH_RECORD_FAILED. Exit 0 whatever it says: the
    push has landed and the record never fails it (only a base that cannot be derived is refused).
    """
    from nexus import ci_push_record  # noqa: PLC0415 -- deferred: CLI startup cost

    base = _push_base(base, remote, branch)
    try:
        label = ci_ack.own_identity() or "unknown"
    except Exception:  # noqa: BLE001 -- a label only
        label = "unknown"
    try:
        store = _store()
    except Exception as exc:  # noqa: BLE001 -- the push landed
        click.echo(f"PUSH_RECORD_FAILED the tuple store could not be opened: {type(exc).__name__}: {exc} (the push stands, no record was written)")
        return
    click.echo(ci_push_record.write(store, base, from_sha, to_sha, count, label, red_fix))
