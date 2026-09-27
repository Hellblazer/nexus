#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mechanized release-skill Step 11d, local-supervisor box class, qwentescence (WSL2).

(bead nexus-u0mcx, following nexus-xii3o's manual runs for 7.56.0/7.57.0.
Round 2 of this bead's own review replaced the dispatch mechanism and added
the safety gates below -- see findings 6 and 7.)

Step 11d requires a real Agent dispatch in a live Claude Code SESSION on
EACH box class -- managed cloud and local supervisor -- then
``tests/e2e/post-publish-dispatch-check.sh`` against that session, ending
``POST-PUBLISH DISPATCH CHECK PASSED``. The local-supervisor leg was run by
hand from qwentescence for 7.56.0 and 7.57.0; this script mechanizes it end
to end from this Mac so the release skill stops needing a human at the
keyboard on the Windows box.

Findings this script encodes, or it reports green/red for the wrong reason:

1. THE ~30s RESTART LOOP IS THE WSL2 IDLE SHUTDOWN, NOT CONTENDING
   SUPERVISORS (nexus-xii3o's first attribution was retracted). WSL2 stops
   the distro roughly 15s after the LAST `wsl -d <distro> ...` session
   ends, taking the supervisor down with it as a clean SIGTERM. Any script
   issuing separate `ssh ... wsl ...` calls in sequence sees this by
   construction, on a correctly configured box, with no misconfiguration at
   all -- so :class:`DistroHold` opens ONE `wsl --exec /bin/sleep N`
   session under a live ssh connection before anything else runs, and
   holds it for the whole gate. `Start-Process -WindowStyle Hidden` does
   NOT hold it (measured); a live ssh connection running `sleep` inside
   the distro does. The remedy is holding the distro open, never retrying
   a transient failure -- retrying would turn a real (if intermittent)
   defect into an intermittently-green gate, exactly the failure mode
   nexus-moht0 names.

2. WORK AS THE `nexus` USER, WITH XDG_RUNTIME_DIR AND
   DBUS_SESSION_BUS_ADDRESS SET. The distro's default user is root, and
   `nx init` refuses there (`initdb` cannot run as root). The durable
   service path is the lingering systemd user unit
   (`systemctl --user enable --now nexus-service`, already enabled on
   qwentescence) -- starting it by piping `nx daemon service start`
   through a one-shot `wsl ... bash -s` session gets it reaped when that
   session exits, since it is not supervisor-owned. Bringing the unit
   active when it is NOT already is itself a live provisioning action;
   see finding 7 for why it is gated behind `--allow-enable-unit`.

3. A FRESH WSL2 BOOT MARKS THE SYSTEMD UNIT ACTIVE BEFORE THE SERVICE IS
   READY (live finding, nexus-u0mcx, qwentescence): `systemctl --user
   is-active` flips to `active` as soon as the unit's ExecStart process
   launches, not once PG has initialized and the lease is published.
   :func:`wait_for_service_ready` polls `nx daemon service status --json`
   for up to 60s after the unit reports active, rather than reading once
   and reporting a false prerequisite-absent on a real boot in progress.

4. THE LEASE PORT CHANGES EVERY WSL2 RESTART CYCLE. A runner that reads the
   lease once and caches host:port can be talking to nothing a cycle later.
   :func:`assert_lease_port_stable` reads `nx daemon service status --json`
   TWICE, a few seconds apart, and asserts the port (and health/pg fields)
   agree -- this tests the thing that was actually broken, not the symptom
   (nexus-xii3o's own "assert lease port is stable across two reads"
   advice, which the retraction says stands under either cause).

5. `uv tool upgrade conexus` ON THAT BOX IS A SILENT NO-OP (conexus was
   installed with an exact pin there). :func:`ensure_version` uses
   `uv tool install "conexus==X.Y.Z" --python 3.12 --force` and re-verifies
   `nx --version`, never trusting an upgrade command's own exit code
   alone. Reinstalling is itself a live provisioning action; see finding 7.

6. THE REAL DISPATCH MUST BE A LIVE INTERACTIVE SESSION, NEVER `claude -p`
   (round-2 review, BLOCKER, reversing this script's own first version).
   Both nexus-xii3o's bead text ("NOT a claude -p subprocess or a
   fixture") and `post-publish-dispatch-check.sh`'s own docstring ("not a
   fixture/subprocess claude -p run") name `claude -p` ITSELF as excluded,
   not merely a fixture standing in for it. RDR-218 Test Plan item 5 ("A
   plain `claude -p` returns rather than hanging") is a DIFFERENT check --
   a Gap-4 hang-regression probe for a FUTURE Windows box class -- not a
   sanction for THIS box class's dispatch mechanism; item 4 on that same
   list is the one requiring "a real Agent dispatch in a live Claude Code
   session", and a live session is interactive by definition.
   :func:`dispatch_via_interactive_session` drives the repo's OWN
   interactive harness (`tests/e2e/lib.sh`'s
   `claude_start`/`claude_prompt`/`claude_wait`/`claude_exit`, tmux-based,
   already proven to accept the trust/bypass-permissions/login screens),
   staged onto the box verbatim and run there -- never reimplemented.
   Confirmed available with NOTHING new to install: qwentescence's `nexus`
   user already has tmux 3.6 on PATH (probed live, nexus-u0mcx round 2; no
   `apt install` was run or would be needed). Two mechanical points this
   staging inherited from the first round's own findings:
   (a) `claude --session-id <uuid>` forces the interactive session's OWN
       identity to a value THIS script mints and validates (see finding 7
       and :func:`mint_session_id`) -- never left to auto-discovery, so a
       concurrent session on the shared box can never make the check
       script's own session-id resolution ambiguous. `CLAUDE_EXTRA_ARGS`
       (an existing `lib.sh` hook) carries the flag into `claude_start`'s
       launch line.
   (b) Both the harness files and the driver script that calls them are
       STAGED as real files and invoked BY PATH, for the same two reasons
       item 6(a)/(b) of this list's prior version named: a PowerShell
       remote re-parses an inlined command line, and `lib.sh` itself
       computes `CLAUDE_FD_EXEC` from its own `${BASH_SOURCE[0]}`, which
       is only meaningful when it is sourced from a real file.

7. EVERY VALUE THAT CROSSES THE qwentescence ssh/PowerShell BOUNDARY IS
   VALIDATED IN PYTHON BEFORE USE (round-2 review). `host`, `distro`,
   `remote_user`, `session_id` (the check script's own
   `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`) and `target_version` are each
   checked against a strict charset (:func:`validate_options`) before any
   of them is embedded in an argv element or a remote script body -- an
   unvalidated `target_version`, in particular, would otherwise be
   spliced directly into a remote-executed `uv tool install "conexus==
   {version}"` line. Two further live-provisioning actions are gated
   behind explicit, OFF-BY-DEFAULT flags rather than run unattended, since
   Sam has not yet authorized either as a default: `--allow-reinstall`
   (required whenever :func:`ensure_version` would actually run
   `uv tool install`) and `--allow-downgrade` (required IN ADDITION,
   whenever the target version is older than what is installed);
   `--allow-enable-unit` (required whenever
   :func:`ensure_systemd_unit_active` would actually run
   `systemctl --user enable --now nexus-service`, i.e. the unit was found
   inactive). On the box as measured (2026-09-27, conexus 7.63.0, unit
   already active) none of the three flags is needed for an ordinary,
   already-converged run; they exist for the day the box's state has
   drifted, so that day requires a human's explicit opt-in rather than a
   silent reinstall. Each of these three flags also needs SAM'S EXPLICIT
   GO for that specific run before it is passed (round-3 review) -- "off
   by default" states the SCRIPT's posture; it does not by itself
   authorize a human to flip one on unattended.

8. UI-STRING BRITTLENESS HAS A NAMED DIAGNOSTIC, NOT A BARE TIMEOUT
   (round-3 review). `_UI_READY_PATTERN` and `_BUSY_INDICATOR_PATTERN`
   (one named "UI STRINGS" block, just above :data:`_TMUX_NAME_PREFIX`)
   are inherently coupled to Claude Code's own rendered TUI text, and a
   future Claude Code release changing that text would otherwise turn
   this gate permanently red with nothing naming the real cause. Every
   failure site in :func:`_build_driver_script` that stems from a UI
   string not matching calls `_ui_diagnostic`, which prints WHICH pattern
   (by constant name) was expected, the actual `claude --version` read
   from the box at failure time, and a pointer to update that named
   constant in this file. This fails SAFE regardless of whether the
   diagnostic is ever read: the real PASS verdict always comes from
   `post-publish-dispatch-check.sh`'s own independent ledger read, never
   from this driver's own `DRIVER_OK` claim, so a stale UI string can only
   ever produce an unneeded DRIVER FAILURE (exit 3, finding 9), never a
   false green.

9. DRIVER FAILURE AND LEDGER MISS ARE DISTINCT VERDICTS WITH DISTINCT EXIT
   CODES (round-3 review; see EXIT CODES above for the full table). A
   `GateFailure` from :func:`dispatch_via_interactive_session` (the
   interactive session never started / never went idle / timed out) means
   the check script was never even reached -- it carries NO evidence about
   the box's own hook wiring, and the right operator action is "rerun
   once". A `check_result.returncode == 1` (the check script ran to
   completion and reported a real miss) is the opposite: a genuine
   finding that must NOT be waved away with a rerun. Folding both into one
   generic FAILED string, as this script's own round-2 version did, is
   exactly the ambiguity finding 1's "never retry a transient failure"
   rule exists to prevent, reproduced one layer up.

10. THE FOUR LIVE ATTEMPTS ROUND 2'S INTERACTIVE REWORK ACTUALLY TOOK,
    RECORDED PRECISELY (round-3 review: this history belongs where a
    future maintainer would find it, not only in a chat transcript).
    Against the real box, conexus 7.63.0, in order:
    (1) FAILED, `NO_TERMINAL` (TSV START with zero REPORTED rows). The
        first interactive version polled for a literal marker token this
        script itself had instructed Claude to print at the end of its
        reply -- but `claude_prompt` pastes the prompt into the pane,
        which echoes the submitted user turn (marker text included, since
        it has to be NAMED in the instruction) before Claude even starts
        responding. The poll matched its own prompt's echo within
        roughly a second and called `claude_exit`, killing the dispatched
        subagent before it had done anything.
    (2) FAILED, `NO_TERMINAL` again. The marker was replaced with a
        two-phase busy-indicator wait (poll for `esc to interrupt` to
        APPEAR, confirming Claude actually started this turn, then a
        single recheck for its absence after `claude_wait`). Still killed
        the subagent early: live-observed on Claude Code v2.1.283, an
        Explore dispatch runs as a BACKGROUND agent, and the status bar's
        hint region rotates through OTHER text for a few seconds while
        that background agent is still genuinely running, so a single
        absence check reads as "finished" when it is not.
    (3) FAILED, `BLOCKED_UNRESOLVED` (progress: the tuple space now held a
        matching `kind=report` tuple, but the TSV ledger's own REPORTED
        row was still missing). A debounce (`_IDLE_DEBOUNCE_COUNT`
        consecutive absent checks) fixed the flicker from (2), but the
        RDR-184 completion hook for a BACKGROUNDED agent fires on a LATER
        tick than the UI's own "finished" render -- `claude_exit` still
        raced ahead of it even with a stable-idle UI.
    (4) PASSED. A short settle window (`_POST_IDLE_SETTLE_SECONDS`)
        between the debounce succeeding and `claude_exit` closed the gap:
        `POST-PUBLISH DISPATCH CHECK PASSED -- violations=0`. Every fix
        from (1)-(3) is retained in the code (never reverted away once
        diagnosed) -- (3)'s debounce did not replace (2)'s two-phase wait,
        and (4)'s settle window did not replace (3)'s debounce.

11. ORPHAN CLEANUP IS KEYED STRICTLY BY THIS GATE'S OWN NAME PREFIX, NEVER
    ANYTHING ELSE ON THE BOX (round-3 review). A remote tmux session (and
    whatever it is running, `claude` included) from a run that never
    reached its own `trap cleanup EXIT` -- the whole ssh/wsl chain killed
    abruptly, for instance -- would otherwise linger forever unrevisited,
    since every run's own socket name is freshly minted from ITS OWN
    session id (:data:`_TMUX_NAME_PREFIX` + 8 hex chars). Plausibly the
    origin of round-1's own "unexplained second session ledger" finding.
    :func:`cleanup_orphan_sessions` sweeps for exactly `_TMUX_NAME_PREFIX`
    and kills the tmux SERVER for each match it finds -- killing a tmux
    server kills every process inside every one of its panes too, so no
    separate `claude`-process scan is needed. Run at TWO points: once at
    the START of :func:`dispatch_via_interactive_session` (so a stray
    orphan from a prior failed run is swept before this run starts its
    own), and once from `run_gate`'s own `finally` (:func:`_cleanup_staged`,
    covering the case where THIS run's own driver never reached its trap).
    Both log what was cleaned, by socket path, through the same `log`
    callback as everything else in this script's report.

    LIVE FINDING while proving this fix (same day): a live round-3 run's
    own tmux server, killed cleanly by its own `trap cleanup EXIT`, still
    left its socket SPECIAL-FILE behind on disk (tmux 3.6) -- a later
    sweep's `kill-server` attempt against it correctly reports "no server
    running" and fails, since there genuinely is no server, only a stale
    file. The sweep script falls back to removing that file directly
    (`ORPHAN_STALE_REMOVED`, counted as cleaned exactly like
    `ORPHAN_CLEANED`) rather than reporting it as a cleanup failure on
    every subsequent run forever.

USAGE:
    uv run python scripts/qwentescence_local_supervisor_gate.py [VERSION]
        [--host HOST] [--distro DISTRO] [--remote-user USER]
        [--hold-seconds N] [--session-id SID] [--skip-dispatch]
        [--allow-reinstall] [--allow-downgrade] [--allow-enable-unit]

VERSION defaults to the currently published conexus version on PyPI
(``https://pypi.org/pypi/conexus/json``) when omitted.

`--session-id` is accepted ONLY together with `--skip-dispatch` (the
manual-rerun path: reuse an already-completed dispatch's evidence without
issuing a new one). The live-dispatch path (the default) always MINTS its
own session id and forces it into the interactive session via
`claude --session-id`; passing `--session-id` without `--skip-dispatch` is
refused, precisely so nothing in this script's own default path can ever
depend on auto-discovery.

EXIT CODES (same discipline as ``tests/e2e/post-publish-dispatch-check.sh``
-- never a silent skip-pass, nexus-moht0; FOUR distinct codes, not three --
see finding 9): each carries its own OPERATOR ACTION, not just its own
name, because collapsing "rerun this" and "do not just rerun this" into
one generic FAILED is precisely the ambiguity nexus-moht0 exists to
prevent:
    0 = QWENTESCENCE LOCAL-SUPERVISOR 11d GATE PASSED.
    1 = ... GATE FAILED -- LEDGER MISS. The check script ran to completion
        and reported a genuine miss -- a real finding. OPERATOR ACTION:
        investigate; do not just rerun.
    2 = ... GATE FAILED -- PREREQUISITE ABSENT. Nothing was checkable at
        all -- box unreachable, an unsafe argument value, WSL/systemd unit
        could not be brought up (or needed `--allow-enable-unit`), version
        could not be made to match (or needed
        `--allow-reinstall`/`--allow-downgrade`), lease port unstable, or
        the check script itself reported its own prerequisite-absent (its
        own exit 2). OPERATOR ACTION: fix the named prerequisite; this is
        not evidence either way about the dispatch/ledger.
    3 = ... GATE FAILED -- DRIVER FAILURE. The interactive session itself
        never reached a checkable state -- `claude_start` never reached
        the main prompt, the busy indicator never appeared, or the debounced
        idle wait timed out (see finding 8's UI-string diagnostic for
        which). It never even reached the check script, so it carries NO
        evidence that anything is wrong with the box's own hook wiring.
        OPERATOR ACTION: rerun once (a UI-timing flake is the common
        cause); investigate only if it recurs.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = REPO_ROOT / "tests" / "e2e" / "post-publish-dispatch-check.sh"
CLAUDE_CREDENTIALS = REPO_ROOT / "tests" / "e2e" / "lib" / "claude_credentials.py"

DEFAULT_HOST = "qwentescence"
DEFAULT_DISTRO = "Ubuntu"
DEFAULT_REMOTE_USER = "nexus"
#: Generous: long enough to cover version-check, possible upgrade, the
#: interactive dispatch and the check script, with margin. Overridable for
#: a slow box.
DEFAULT_HOLD_SECONDS = 900
DEFAULT_DISPATCH_TIMEOUT = 360.0
DEFAULT_CLAUDE_WAIT_SECONDS = 150

VERDICT_PASS = "QWENTESCENCE LOCAL-SUPERVISOR 11d GATE PASSED"
#: Three DISTINCT verdicts for three DISTINCT failure classes (finding 9,
#: round 3 review) -- collapsing them under one `VERDICT_FAIL` string gave
#: the operator no way to tell "rerun this, it is probably a UI-timing
#: flake" from "do not just rerun, the ledger genuinely never saw a
#: report" from "nothing was even checkable". Exactly the ambiguity
#: finding 1's own "never retry" rule exists to prevent, now reproduced at
#: the verdict layer if these three are folded back into one string.
VERDICT_LEDGER_MISS = (
    "QWENTESCENCE LOCAL-SUPERVISOR 11d GATE FAILED -- LEDGER MISS "
    "(real finding: the check script ran and reported a miss; do not just rerun)"
)
VERDICT_DRIVER_FAILURE = (
    "QWENTESCENCE LOCAL-SUPERVISOR 11d GATE FAILED -- DRIVER FAILURE "
    "(the interactive session itself never started/never went idle/timed out; "
    "rerun once -- investigate only if it recurs)"
)
VERDICT_PREREQUISITE_ABSENT = (
    "QWENTESCENCE LOCAL-SUPERVISOR 11d GATE FAILED -- PREREQUISITE ABSENT "
    "(nothing was checkable; this is not evidence either way)"
)

# ---------------------------------------------------------------------------
# UI STRINGS (finding 8, round 3 review). Every literal string this driver
# matches against the Claude Code TUI lives HERE, in one named block,
# because each is inherently coupled to Claude Code's own rendered text and
# WILL need updating when that text changes -- a future Claude Code UI
# change must never silently turn this gate permanently red with no
# diagnostic naming the real cause. Fails SAFE regardless: the actual PASS
# verdict always comes from `post-publish-dispatch-check.sh`'s own
# independent ledger read, never from the driver's own `DRIVER_OK` claim,
# so a UI-string mismatch here can only ever produce a false NEGATIVE
# (an unneeded DRIVER FAILURE), never a false positive.
# ---------------------------------------------------------------------------

#: What `claude_start`'s own loop is polled against to confirm it reached
#: the main prompt (workspace-trust/bypass-permissions/login screens all
#: cleared).
_UI_READY_PATTERN = "bypass permissions on|Type a message"
#: The busy-indicator pattern `tests/e2e/lib.sh`'s own `claude_wait` polls
#: for absence of. `_build_driver_script` also polls for its PRESENCE right
#: after submitting the prompt -- see the two-phase-wait comment there for
#: why a marker token embedded IN the prompt text cannot be used as a
#: completion signal on this harness (live finding, qwentescence,
#: nexus-u0mcx round 2). `esc to interrupt` is the one substring observed
#: stable across Claude Code's rotating "thinking" verbs (`Channeling…`,
#: `Flibbertigibbeting…`, `Hatching…`, ...; the fixed word list this
#: pattern started as, `Simmering…|Running…|Cerebrating…`, does not cover
#: the verbs actually seen live and is kept only as an extra, harmless
#: alternation -- `esc to interrupt` alone is load-bearing). It is NOT
#: monotonic, though: a background-agent dispatch (Claude Code v2.1.283,
#: live-observed) flickers it absent for a few seconds mid-wait even while
#: the subagent is genuinely still running (the status bar's hint region
#: rotates through other text). A single absence check is a false
#: "finished" -- :func:`_build_driver_script`'s wait DEBOUNCES: several
#: consecutive absent checks, spaced seconds apart, before it trusts idle.
_BUSY_INDICATOR_PATTERN = "Simmering…|Running…|Cerebrating…|esc to interrupt"
#: How many consecutive absence-checks (see above) must pass, and how far
#: apart, before phase 2 trusts idle. 3 checks x 4s = at least ~12s of
#: sustained absence -- the live-observed flicker cleared within a couple
#: of seconds each time, so this is a wide margin, not a tight fit.
_IDLE_DEBOUNCE_COUNT = 3
_IDLE_DEBOUNCE_GAP_SECONDS = 4
#: A background agent's RDR-184 completion hook can fire on a later tick
#: than the UI's own "finished" render -- live-observed as a BLOCKED_
#: UNRESOLVED ledger classification even after the debounced idle wait
#: above passed. This settle window runs once, right before `claude_exit`,
#: giving that deferred hook time to land before the session (and its
#: process) is torn down.
_POST_IDLE_SETTLE_SECONDS = 10

#: Every tmux socket/session this gate ever creates is named with this
#: prefix plus 8 hex chars of the run's own session id (finding 11, round
#: 3 review). Orphan cleanup greps for EXACTLY this prefix and nothing
#: else, so it can never reach a socket or session belonging to anyone or
#: anything but this gate.
_TMUX_NAME_PREFIX = "nx-u0mcx-"

DISPATCH_PROMPT = (
    "This is an automated, mechanized run of nexus release-skill Step 11d, "
    "bead nexus-u0mcx: the local-supervisor post-publish dispatch check. "
    "Use the Agent tool to dispatch exactly one trivial Explore subagent "
    "with the task list the files in the current directory and wait for "
    "it to finish. Do nothing else, no other tool calls, no commentary "
    "beyond confirming the subagent finished."
)


class GateError(RuntimeError):
    """A prerequisite step failed outright (exit 2 territory) -- distinct
    from a real MISS the check script itself reports (exit 1 territory)."""


class GateFailure(RuntimeError):
    """A real, checked failure (exit 1 territory): the box answered and
    something it checked was wrong (a bad dispatch, a FAILED check-script
    verdict)."""


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        return self.stdout + self.stderr


#: A runner is anything shaped like ``run(argv, input=None, timeout=None) ->
#: CommandResult`` -- real subprocess.run in production, a recording fake in
#: tests. Keeping this a plain Callable (not a class) is what makes
#: dependency injection trivial from a test file with no ssh, no qwentescence,
#: no WSL.
Runner = Callable[..., CommandResult]


def _decode_subprocess_text(value: bytes | str | None) -> str:
    """`subprocess.TimeoutExpired.stdout`/`.stderr` are typed `bytes | str |
    None` regardless of whether the call used `text=True` -- this narrows
    honestly (decoding real bytes rather than assuming str) instead of
    relying on an `isinstance` check inside a ternary, which pyright does
    not always carry through to the ternary's own inferred type."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def default_runner(
    argv: Sequence[str], *, input: str | None = None, timeout: float | None = None
) -> CommandResult:
    try:
        proc = subprocess.run(
            list(argv), input=input, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        out = _decode_subprocess_text(exc.stdout)
        err = _decode_subprocess_text(exc.stderr)
        return CommandResult(124, out, err + f"\n[timed out after {timeout}s]")
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


@dataclass
class Options:
    host: str = DEFAULT_HOST
    distro: str = DEFAULT_DISTRO
    remote_user: str = DEFAULT_REMOTE_USER
    hold_seconds: int = DEFAULT_HOLD_SECONDS
    #: ONLY meaningful together with ``skip_dispatch=True`` -- see finding 7
    #: and :func:`run_gate`'s own refusal when the two disagree.
    session_id: str | None = None
    skip_dispatch: bool = False
    dispatch_timeout: float = DEFAULT_DISPATCH_TIMEOUT
    #: Off by default (finding 7): Sam has not authorized an unattended
    #: `uv tool install` on this box. Required whenever the installed
    #: version does not already match the target.
    allow_reinstall: bool = False
    #: Off by default, and required IN ADDITION to `allow_reinstall`
    #: whenever the target version is genuinely older than installed.
    allow_downgrade: bool = False
    #: Off by default: required whenever the systemd --user unit is found
    #: inactive and this gate would otherwise run `enable --now` itself.
    allow_enable_unit: bool = False


# ---------------------------------------------------------------------------
# Finding 7: boundary validation for every value that reaches an ssh argv
# element or a remote script body.
# ---------------------------------------------------------------------------

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_DISTRO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
#: Identical to post-publish-dispatch-check.sh's own SID guard -- the two
#: must agree, since this script hands the check script exactly this value.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z_.+-]{0,63}$")


def _validate_token(field: str, value: str, pattern: re.Pattern[str]) -> None:
    if not pattern.match(value):
        raise GateError(
            f"refusing an unsafe {field} value {value!r}: it does not match "
            f"{pattern.pattern!r} -- every value that crosses the qwentescence "
            "ssh/PowerShell boundary is validated before use (nexus-u0mcx finding 7)"
        )


def validate_options(opts: Options, target_version: str) -> None:
    _validate_token("host", opts.host, _HOST_RE)
    _validate_token("distro", opts.distro, _DISTRO_RE)
    _validate_token("remote_user", opts.remote_user, _USER_RE)
    _validate_token("target_version", target_version, _VERSION_RE)
    if opts.session_id is not None:
        _validate_token("session_id", opts.session_id, _SESSION_ID_RE)


def mint_session_id(factory: Callable[[], str] = lambda: str(uuid.uuid4())) -> str:
    """Mints a fresh session id and validates it against the same charset
    the check script itself enforces -- defence in depth against a hostile
    or malformed injected factory in tests, since a real ``uuid.uuid4()``
    always passes."""
    sid = factory()
    _validate_token("session_id", sid, _SESSION_ID_RE)
    return sid


REMOTE_ENV_PREFIX = (
    'export PATH="$HOME/.local/bin:$PATH"\n'
    'export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"\n'
    'export DBUS_SESSION_BUS_ADDRESS='
    '"${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/1000/bus}"\n'
)


def remote_wsl_argv(opts: Options, extra_argv: Sequence[str] | None = None) -> list[str]:
    """ssh argv that runs a script as ``opts.remote_user`` inside
    ``opts.distro``, reading the script from stdin (``bash -s --``) with
    ``extra_argv`` becoming its own positional params (``"$@"``). Every
    token here is a separate argv element -- ssh joins them with spaces for
    the remote PowerShell to parse, and none of these tokens carries a
    PowerShell metacharacter, so this is safe by the same reasoning
    ``claude_credentials.py``'s own ``_run_remote`` relies on. Callers pass
    validated (finding 7) ``opts`` fields only."""
    argv = [
        "ssh",
        opts.host,
        "wsl",
        "-d",
        opts.distro,
        "-u",
        opts.remote_user,
        "--exec",
        "/bin/bash",
        "-s",
        "--",
    ]
    if extra_argv:
        argv.extend(extra_argv)
    return argv


def run_remote_script(
    runner: Runner,
    opts: Options,
    script_body: str,
    *,
    extra_argv: Sequence[str] | None = None,
    timeout: float | None = 60.0,
) -> CommandResult:
    argv = remote_wsl_argv(opts, extra_argv)
    return runner(argv, input=REMOTE_ENV_PREFIX + script_body, timeout=timeout)


# ---------------------------------------------------------------------------
# Step 0: reachability
# ---------------------------------------------------------------------------


def ensure_reachable(runner: Runner, opts: Options) -> None:
    result = runner(["ssh", opts.host, "echo", "REACHABLE"], timeout=20.0)
    if result.returncode != 0 or "REACHABLE" not in result.stdout:
        raise GateError(
            f"qwentescence ({opts.host}) unreachable via ssh (rc={result.returncode}): "
            f"{result.combined.strip()}"
        )


# ---------------------------------------------------------------------------
# Step 1: hold the distro open for the whole run (finding 1)
# ---------------------------------------------------------------------------

PopenFactory = Callable[..., "subprocess.Popen[bytes]"]


class DistroHold:
    """A background ``wsl --exec /bin/sleep N`` session under a live ssh
    connection. Constructed with an injectable Popen factory so tests never
    launch a real ssh/wsl process. ``stop()`` is idempotent."""

    def __init__(self, popen_factory: PopenFactory = subprocess.Popen) -> None:
        self._popen_factory = popen_factory
        self._proc: "subprocess.Popen[bytes] | None" = None

    def start(
        self,
        opts: Options,
        *,
        startup_check_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        argv = [
            "ssh",
            opts.host,
            "wsl",
            "-d",
            opts.distro,
            "--exec",
            "/bin/sleep",
            str(opts.hold_seconds),
        ]
        try:
            self._proc = self._popen_factory(argv)
        except OSError as exc:
            self._proc = None
            raise GateError(
                f"could not start the qwentescence distro-hold ssh session "
                f"({argv[0]!r} unavailable or refused to spawn): {exc}"
            ) from exc
        # A real failure (host unreachable, wsl invocation rejected) exits
        # near-instantly; give it a brief moment to fail fast rather than
        # discovering the hold never actually held anything only when a
        # later step's ssh call independently times out.
        startup_check_sleep(0.2)
        rc = self._proc.poll()
        if rc is not None:
            self._proc = None
            raise GateError(
                f"distro-hold ssh session for {opts.host} exited immediately "
                f"(rc={rc}) -- host unreachable or the wsl invocation failed "
                "before it could hold the distro open"
            )

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Step 2: the systemd unit + linger (finding 2), enable gated (finding 7)
# ---------------------------------------------------------------------------

_SYSTEMD_STATE_SCRIPT = """
if systemctl --user is-active --quiet nexus-service; then
    echo "UNIT_STATE=active"
else
    echo "UNIT_STATE=inactive"
fi
echo "LINGER=$(loginctl show-user "$(id -un)" --property=Linger --value 2>/dev/null || echo unknown)"
"""
_SYSTEMD_ENABLE_SCRIPT = "systemctl --user enable --now nexus-service"


def _parse_systemd_state(text: str) -> tuple[str, str]:
    state_match = re.search(r"^UNIT_STATE=(\S+)$", text, re.MULTILINE)
    linger_match = re.search(r"^LINGER=(\S+)$", text, re.MULTILINE)
    return (
        state_match.group(1) if state_match else "unknown",
        linger_match.group(1) if linger_match else "unknown",
    )


def ensure_systemd_unit_active(runner: Runner, opts: Options) -> None:
    result = run_remote_script(runner, opts, _SYSTEMD_STATE_SCRIPT, timeout=30.0)
    if result.returncode != 0:
        raise GateError(
            "could not read nexus-service systemd --user unit state on "
            f"{opts.host} (rc={result.returncode}): {result.combined.strip()}"
        )
    state, linger = _parse_systemd_state(result.stdout)

    if state != "active":
        if not opts.allow_enable_unit:
            raise GateError(
                f"nexus-service systemd --user unit is {state!r} on {opts.host}, "
                "not 'active', and enabling it is a live provisioning action "
                "this gate will not take unattended -- pass --allow-enable-unit "
                "to let it run `systemctl --user enable --now nexus-service`, "
                "or enable it by hand first"
            )
        enable_result = run_remote_script(runner, opts, _SYSTEMD_ENABLE_SCRIPT, timeout=60.0)
        if enable_result.returncode != 0:
            raise GateError(
                "`systemctl --user enable --now nexus-service` failed on "
                f"{opts.host} (rc={enable_result.returncode}): "
                f"{enable_result.combined.strip()}"
            )
        recheck = run_remote_script(runner, opts, _SYSTEMD_STATE_SCRIPT, timeout=30.0)
        state, linger = _parse_systemd_state(recheck.stdout)
        if state != "active":
            raise GateError(
                f"nexus-service systemd --user unit still {state!r} on "
                f"{opts.host} after `enable --now`"
            )

    if linger != "yes":
        raise GateError(
            f"loginctl shows Linger={linger} for the nexus user on {opts.host} "
            "(expected 'yes') -- the unit will not survive session end even "
            "though it is active; run `loginctl enable-linger nexus` as root there"
        )


# ---------------------------------------------------------------------------
# Step 3: lease-port stability across two reads (finding 3/4)
# ---------------------------------------------------------------------------

_STATUS_SCRIPT = "nx daemon service status --json"


def read_service_status(runner: Runner, opts: Options) -> dict:
    result = run_remote_script(runner, opts, _STATUS_SCRIPT, timeout=30.0)
    if result.returncode != 0:
        raise GateError(
            f"`nx daemon service status --json` failed on {opts.host} "
            f"(rc={result.returncode}): {result.combined.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise GateError(
            f"`nx daemon service status --json` on {opts.host} did not print "
            f"valid JSON: {exc}; output was: {result.stdout.strip()!r}"
        ) from None


def wait_for_service_ready(
    runner: Runner,
    opts: Options,
    *,
    timeout: float = 60.0,
    poll_interval: float = 3.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict:
    """Polls `nx daemon service status --json` until it succeeds or
    `timeout` elapses. Needed because a FRESH WSL2 VM boot (finding 1: the
    idle-shutdown tears down the whole guest kernel, not just a login
    session) marks the systemd unit `active` as soon as its ExecStart
    process launches -- well before PG has initialized and the service has
    published its lease -- so a single read right after
    `ensure_systemd_unit_active` can race a real boot in progress (finding
    3). Live finding, qwentescence, nexus-u0mcx: `systemctl --user
    is-active` reported active immediately while `nx daemon service status
    --json` still answered 'No storage service lease found' for several
    seconds."""
    attempts = max(1, int(timeout // poll_interval))
    last_error: GateError | None = None
    for attempt in range(attempts):
        try:
            return read_service_status(runner, opts)
        except GateError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                sleep_fn(poll_interval)
    raise GateError(
        f"nx daemon service status --json never succeeded on {opts.host} within "
        f"{timeout}s of the systemd unit reporting active (last error: {last_error})"
    )


def assert_lease_port_stable(
    runner: Runner,
    opts: Options,
    *,
    first_status: dict | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    gap_seconds: float = 4.0,
) -> dict:
    first = first_status if first_status is not None else read_service_status(runner, opts)
    sleep_fn(gap_seconds)
    second = read_service_status(runner, opts)
    port_a, port_b = first.get("port"), second.get("port")
    if port_a is None or port_b is None or port_a != port_b:
        raise GateError(
            "lease port is not stable across two reads a few seconds apart "
            f"({port_a!r} then {port_b!r}) -- see finding 1: the WSL2 distro "
            "likely idle-shut-down between reads, which means DistroHold "
            "failed to keep it open, not that the supervisor is misbehaving"
        )
    if second.get("health") != "ok":
        raise GateError(
            f"nexus-service health is {second.get('health')!r} on {opts.host}, expected 'ok'"
        )
    if second.get("pg") != "up":
        raise GateError(f"PG is {second.get('pg')!r} on {opts.host}, expected 'up'")
    return second


# ---------------------------------------------------------------------------
# Step 4: version match, with the exact-pin upgrade trap (finding 5),
# reinstall/downgrade gated (finding 7)
# ---------------------------------------------------------------------------

_VERSION_SCRIPT = "nx --version"
_UPGRADE_SCRIPT_TEMPLATE = 'uv tool install "conexus=={version}" --python 3.12 --force'


def _parse_nx_version(text: str) -> str | None:
    match = re.search(r"version\s+(\S+)", text)
    return match.group(1) if match else None


def _version_tuple(v: str) -> tuple[int, ...]:
    """A permissive dotted-numeric-prefix comparator -- good enough to
    order this project's `X.Y.Z`-shaped releases without a `packaging`
    dependency. A non-numeric component (rare, pre-release suffixes) reads
    as 0, which only ever biases toward treating it as NOT older, i.e.
    toward the safer `--allow-reinstall`-only path rather than silently
    admitting a real downgrade."""
    parts = []
    for chunk in v.split("."):
        m = re.match(r"\d+", chunk)
        parts.append(int(m.group(0)) if m else 0)
    return tuple(parts)


def ensure_version(
    runner: Runner,
    opts: Options,
    target_version: str,
    *,
    log: Callable[[str], None] = lambda _: None,
) -> str:
    result = run_remote_script(runner, opts, _VERSION_SCRIPT, timeout=30.0)
    if result.returncode != 0:
        raise GateError(
            f"`nx --version` failed on {opts.host} (rc={result.returncode}): "
            f"{result.combined.strip()}"
        )
    installed = _parse_nx_version(result.stdout)
    if installed is None:
        raise GateError(
            f"could not parse `nx --version` output on {opts.host}: "
            f"{result.stdout.strip()!r}"
        )
    if installed == target_version:
        log(
            f"version transition: found {installed}, target {target_version}, "
            "action=none (already matches)"
        )
        return installed

    is_downgrade = installed is not None and _version_tuple(target_version) < _version_tuple(
        installed
    )
    action = "downgrade" if is_downgrade else "upgrade"
    log(f"version transition: found {installed}, target {target_version}, action={action}")

    if is_downgrade and not opts.allow_downgrade:
        raise GateError(
            f"target {target_version} is OLDER than the installed {installed} on "
            f"{opts.host} -- refusing a downgrade unless --allow-downgrade is passed"
        )
    if not opts.allow_reinstall:
        raise GateError(
            f"installed conexus ({installed}) does not match target "
            f"({target_version}) on {opts.host}, and reinstalling is a live "
            "provisioning action this gate will not take unattended -- pass "
            "--allow-reinstall to let it run `uv tool install`, or reinstall by "
            "hand and re-run"
        )

    upgrade = run_remote_script(
        runner,
        opts,
        _UPGRADE_SCRIPT_TEMPLATE.format(version=target_version),
        timeout=180.0,
    )
    if upgrade.returncode != 0:
        raise GateError(
            f"`uv tool install conexus=={target_version} --force` failed on "
            f"{opts.host} (rc={upgrade.returncode}): {upgrade.combined.strip()}"
        )

    reresult = run_remote_script(runner, opts, _VERSION_SCRIPT, timeout=30.0)
    reinstalled = _parse_nx_version(reresult.stdout) if reresult.returncode == 0 else None
    if reinstalled is None or reinstalled != target_version:
        raise GateError(
            f"after `uv tool install conexus=={target_version} --force`, "
            f"`nx --version` on {opts.host} still reports {reinstalled!r} "
            f"(expected {target_version!r}) -- exact-pin installs can report "
            "success while resolving only transitive deps; this is the "
            "documented qwentescence upgrade trap"
        )
    log(f"version transition: {action} completed, now {reinstalled}")
    return reinstalled


# ---------------------------------------------------------------------------
# Step 5: the real dispatch -- a live interactive Claude Code session
# (finding 6). `claude -p` is REJECTED here; see the module docstring.
# ---------------------------------------------------------------------------

_HARNESS_DIR_NAME = "nexus-u0mcx-harness"
_HARNESS_FILES: tuple[tuple[str, Path], ...] = (
    ("lib.sh", REPO_ROOT / "tests" / "e2e" / "lib.sh"),
    ("lib/claude_fd_exec.sh", REPO_ROOT / "tests" / "e2e" / "lib" / "claude_fd_exec.sh"),
    ("lib/gate_advisory.sh", REPO_ROOT / "tests" / "e2e" / "lib" / "gate_advisory.sh"),
)
_DRIVER_SCRIPT_NAME = "nexus-u0mcx-interactive-dispatch.sh"


def _harness_dir(opts: Options) -> str:
    return f"/home/{opts.remote_user}/{_HARNESS_DIR_NAME}"


def _driver_script_path(opts: Options) -> str:
    return f"/home/{opts.remote_user}/{_DRIVER_SCRIPT_NAME}"


def _stage_harness_files(runner: Runner, opts: Options) -> str:
    """Stages the REAL repo files verbatim -- never a reimplementation --
    so the interactive harness this gate drives is the exact one the
    repo's own e2e suite already validated against real Claude Code
    sessions (`tests/e2e/run.sh`)."""
    harness_dir = _harness_dir(opts)
    parts = [f"mkdir -p {harness_dir}/lib"]
    for rel, src in _HARNESS_FILES:
        content = src.read_text()
        marker = "HARNESS_FILE_EOF_" + re.sub(r"[^A-Za-z0-9]", "_", rel)
        parts.append(f"cat > {harness_dir}/{rel} <<'{marker}'\n{content}\n{marker}")
    parts.append(f"chmod +x {harness_dir}/lib.sh {harness_dir}/lib/claude_fd_exec.sh")
    script_body = "\n".join(parts) + "\n"
    result = run_remote_script(runner, opts, script_body, timeout=30.0)
    if result.returncode != 0:
        raise GateError(
            f"could not stage the interactive harness at {harness_dir} on "
            f"{opts.host} (rc={result.returncode}): {result.combined.strip()}"
        )
    return harness_dir


def _build_driver_script(
    session_id: str,
    harness_dir: str,
    *,
    claude_wait_seconds: int = DEFAULT_CLAUDE_WAIT_SECONDS,
) -> str:
    tmux_name = f"{_TMUX_NAME_PREFIX}{session_id[:8]}"
    return f"""#!/bin/bash
set -u
export TMUX_SESSION="{tmux_name}"
export NX_TMUX_SOCKET="{tmux_name}"
export CLAUDE_EXTRA_ARGS="--session-id {session_id}"
source "{harness_dir}/lib.sh"
cleanup() {{
    _tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
    _tmux kill-server 2>/dev/null || true
}}
trap cleanup EXIT
# UI STRINGS (finding 8, round 3 review): every failure below that stems
# from a UI-string mismatch prints WHICH pattern it expected, the Claude
# Code version actually on the box (captured here, once, as a plain
# one-shot command outside tmux -- cheap, and unaffected by anything that
# happens inside the pane later), and the exact constant name to update
# in scripts/qwentescence_local_supervisor_gate.py. This never masks a
# false PASS: the real verdict always comes from
# post-publish-dispatch-check.sh's own ledger read, never from this
# script's own DRIVER_OK claim, so a stale UI string can only ever cause
# an unneeded DRIVER FAILURE, not a false green.
CC_VERSION="$(claude --version 2>&1)" || CC_VERSION="unknown (claude --version itself failed)"
_ui_diagnostic() {{
    local pattern_name="$1" pattern_value="$2"
    echo "DRIVER_FAILED: expected UI pattern $pattern_name=\\"$pattern_value\\" was not observed" >&2
    echo "DRIVER_FAILED: Claude Code version on box: $CC_VERSION" >&2
    echo "DRIVER_FAILED: if Claude Code's rendered UI text has changed, update $pattern_name in scripts/qwentescence_local_supervisor_gate.py (see its 'UI STRINGS' block)" >&2
}}
_tmux new-session -d -s "$TMUX_SESSION" -c "$HOME" -x 220 -y 50
claude_start
if ! capture | grep -qiE "{_UI_READY_PATTERN}"; then
    _ui_diagnostic "_UI_READY_PATTERN" "{_UI_READY_PATTERN}"
    capture -80 >&2
    exit 1
fi
read -r -d '' PROMPT_TEXT <<'PROMPT_EOF' || true
{DISPATCH_PROMPT}
PROMPT_EOF
claude_prompt "$PROMPT_TEXT"
# TWO-PHASE, DEBOUNCED wait (live findings, qwentescence, nexus-u0mcx
# round 2). A marker token NAMED inside the prompt text cannot be used as
# a completion signal on this harness: `claude_prompt` pastes the prompt
# into the pane, which echoes the submitted user turn (including that
# literal token, since it has to be NAMED in the instruction) well before
# Claude starts responding -- a first attempt using exactly that
# technique matched its own prompt's echo and called `claude_exit` within
# ~1s, killing the dispatched subagent before it ever reported (TSV showed
# START with no REPORTED, `NO_TERMINAL`). The busy-indicator text is never
# something we typed, so it cannot suffer this echo -- but it is not
# monotonic either: live-observed on Claude Code v2.1.283, an Explore
# dispatch runs as a BACKGROUND agent ("Backgrounded agent", "Waiting for
# N background agent(s) to finish"), and the status bar's own
# `esc to interrupt` hint flickers absent for several seconds mid-wait
# while that background agent is still genuinely running (rotating hint
# text, not a real idle state) -- a SINGLE absence check is a false
# "finished" and reproduced `NO_TERMINAL` a second time even after adding
# phase 1. Phase 1 confirms Claude actually started processing THIS turn
# (never a stale/echoed screen). Phase 2 requires the busy indicator
# absent on {_IDLE_DEBOUNCE_COUNT} CONSECUTIVE checks, {_IDLE_DEBOUNCE_GAP_SECONDS}s
# apart, before trusting idle -- long enough to ride out the observed
# flicker, which cleared within a couple of seconds every time it was
# live-observed.
if ! poll_for "{_BUSY_INDICATOR_PATTERN}" 30 "processing started"; then
    _ui_diagnostic "_BUSY_INDICATOR_PATTERN" "{_BUSY_INDICATOR_PATTERN}"
    echo "DRIVER_FAILED: Claude never appeared to start processing the prompt (no busy indicator within 30s)" >&2
    capture -80 >&2
    claude_exit
    exit 1
fi
_deadline=$(( $(date +%s) + {claude_wait_seconds} ))
_stable_clear=0
while [[ $(date +%s) -lt $_deadline ]]; do
    sleep {_IDLE_DEBOUNCE_GAP_SECONDS}
    if capture | grep -qiE "{_BUSY_INDICATOR_PATTERN}"; then
        _stable_clear=0
    else
        _stable_clear=$((_stable_clear + 1))
        if [[ $_stable_clear -ge {_IDLE_DEBOUNCE_COUNT} ]]; then
            break
        fi
    fi
done
if [[ $_stable_clear -lt {_IDLE_DEBOUNCE_COUNT} ]]; then
    _ui_diagnostic "_BUSY_INDICATOR_PATTERN" "{_BUSY_INDICATOR_PATTERN}"
    echo "DRIVER_FAILED: dispatch never reached a stable idle state within {claude_wait_seconds}s" >&2
    capture -150 >&2
    claude_exit
    exit 1
fi
# Live finding, qwentescence, nexus-u0mcx round 2: the UI showing a
# background agent as "finished" does not mean its RDR-184 completion hook
# has ALREADY fired -- the ledger recorded BLOCKED_UNRESOLVED (the verb
# nexus's own hook design uses for exactly a still-pending background
# dispatch) even after the debounced idle wait above passed, meaning
# `claude_exit`'s termination raced ahead of a hook event that fires on a
# later tick than the UI's own render. A short settle window here, before
# exiting, is the fix -- not a longer debounce (idle was already stable).
sleep {_POST_IDLE_SETTLE_SECONDS}
claude_exit
echo "DRIVER_OK session_id={session_id}"
"""


def _stage_interactive_driver(
    runner: Runner, opts: Options, session_id: str, harness_dir: str
) -> str:
    path = _driver_script_path(opts)
    driver = _build_driver_script(session_id, harness_dir)
    staging_script = f"cat > {path} <<'DRIVER_SCRIPT_EOF'\n{driver}\nDRIVER_SCRIPT_EOF\nchmod +x {path}\n"
    result = run_remote_script(runner, opts, staging_script, timeout=30.0)
    if result.returncode != 0:
        raise GateError(
            f"could not stage the interactive dispatch driver at {path} on "
            f"{opts.host} (rc={result.returncode}): {result.combined.strip()}"
        )
    return path


# ---------------------------------------------------------------------------
# Orphan cleanup (finding 11, round 3 review). A remote tmux session (and
# whatever it is running, `claude` included) from a PRIOR run that never
# reached its own `trap cleanup EXIT` -- the whole ssh/wsl chain killed
# abruptly, for instance -- would otherwise linger on the box forever,
# unrevisited, since each run's own socket name is freshly minted from ITS
# session id. Keyed STRICTLY by `_TMUX_NAME_PREFIX`: this can never reach a
# socket or session belonging to anyone or anything else on the box.
# Killing a tmux SERVER kills every process running inside every one of
# its panes too, so no separate process scan is needed to also reap a
# still-running `claude`.
# ---------------------------------------------------------------------------

_ORPHAN_SWEEP_SCRIPT_TEMPLATE = """
found=0
for sockdir in /tmp/tmux-*; do
    [ -d "$sockdir" ] || continue
    for sock in "$sockdir"/{prefix}*; do
        [ -e "$sock" ] || continue
        found=1
        if tmux -S "$sock" kill-server 2>/dev/null; then
            echo "ORPHAN_CLEANED $sock"
        elif rm -f "$sock" 2>/dev/null; then
            # Live finding, qwentescence, nexus-u0mcx round 3: tmux 3.6
            # leaves the socket special-file on disk even after a clean
            # `kill-server` terminates the server process -- a later sweep
            # sees `-e "$sock"` still true but `kill-server` itself then
            # (correctly) reports "no server running" and fails. That is
            # not a session still running; it is a stale file, and the fix
            # is to remove the file directly rather than re-report it as a
            # cleanup failure forever.
            echo "ORPHAN_STALE_REMOVED $sock"
        else
            echo "ORPHAN_CLEAN_FAILED $sock"
        fi
    done
done
if [ "$found" = 0 ]; then
    echo "ORPHAN_NONE_FOUND"
fi
"""


def cleanup_orphan_sessions(runner: Runner, opts: Options) -> list[str]:
    """Finds and kills any remote tmux session/socket THIS GATE could have
    started in a prior run and never cleaned up, keyed strictly by
    `_TMUX_NAME_PREFIX` -- never anything else on the box. Returns the
    socket paths actually cleaned (for the caller to log); best-effort,
    never raises, since a sweep failure must not mask the real verdict."""
    try:
        script = _ORPHAN_SWEEP_SCRIPT_TEMPLATE.format(prefix=_TMUX_NAME_PREFIX)
        result = run_remote_script(runner, opts, script, timeout=20.0)
    except Exception:
        return []
    cleaned = []
    for line in result.stdout.splitlines():
        for prefix in ("ORPHAN_CLEANED ", "ORPHAN_STALE_REMOVED "):
            if line.startswith(prefix):
                cleaned.append(line[len(prefix) :])
                break
    return cleaned


def dispatch_via_interactive_session(
    runner: Runner,
    opts: Options,
    session_id: str,
    *,
    log: Callable[[str], None] = lambda _: None,
) -> CommandResult:
    _validate_token("session_id", session_id, _SESSION_ID_RE)
    cleaned = cleanup_orphan_sessions(runner, opts)
    if cleaned:
        log(f"orphan cleanup (start): killed {len(cleaned)} stray tmux socket(s): {cleaned}")
    harness_dir = _stage_harness_files(runner, opts)
    driver_path = _stage_interactive_driver(runner, opts, session_id, harness_dir)
    remote_shell = f"wsl -d {opts.distro} -u {opts.remote_user} --exec /bin/bash -s --"
    argv = [
        sys.executable,
        str(CLAUDE_CREDENTIALS),
        "run",
        "--remote",
        opts.host,
        "--remote-shell",
        remote_shell,
        "--",
        "bash",
        driver_path,
    ]
    result = runner(argv, timeout=opts.dispatch_timeout)
    if result.returncode != 0 or "DRIVER_OK" not in result.stdout:
        raise GateFailure(
            f"real interactive-session dispatch on {opts.host} failed "
            f"(rc={result.returncode}): {result.combined.strip()[-3000:]}"
        )
    return result


# ---------------------------------------------------------------------------
# Step 6: the assertion half -- the check script itself, run remotely
#
# STAGED, never piped through `bash -s --`: `post-publish-dispatch-check.sh`
# computes `SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"`
# under its own `set -euo pipefail`, and `BASH_SOURCE[0]` is EMPTY when a
# script runs from stdin rather than a real file -- under `set -u` that is
# an unbound-variable hard abort at the very first line, before any of the
# four checks run. Live finding, qwentescence, nexus-u0mcx: this happened
# on the first end-to-end run and reported a bare rc=1 with no MISS lines
# at all -- nothing wrong with the four checks themselves, everything
# wrong with HOW the script was invoked. Staging it as a real file and
# invoking it BY PATH gives `BASH_SOURCE[0]` a real value.
# ---------------------------------------------------------------------------

_CHECK_SCRIPT_NAME = "nexus-u0mcx-post-publish-dispatch-check.sh"


def _check_script_path(opts: Options) -> str:
    return f"/home/{opts.remote_user}/{_CHECK_SCRIPT_NAME}"


def _stage_check_script(runner: Runner, opts: Options) -> str:
    path = _check_script_path(opts)
    embedded = REMOTE_ENV_PREFIX + CHECK_SCRIPT.read_text()
    staging_script = f"cat > {path} <<'CHECK_SCRIPT_EOF'\n{embedded}\nCHECK_SCRIPT_EOF\nchmod +x {path}\n"
    result = run_remote_script(runner, opts, staging_script, timeout=30.0)
    if result.returncode != 0:
        raise GateError(
            f"could not stage the check script at {path} on {opts.host} "
            f"(rc={result.returncode}): {result.combined.strip()}"
        )
    return path


def run_check_script(runner: Runner, opts: Options) -> CommandResult:
    if opts.session_id is not None:
        _validate_token("session_id", opts.session_id, _SESSION_ID_RE)
    path = _stage_check_script(runner, opts)
    argv = [
        "ssh",
        opts.host,
        "wsl",
        "-d",
        opts.distro,
        "-u",
        opts.remote_user,
        "--exec",
        "/bin/bash",
        path,
    ]
    if opts.session_id:
        argv.append(opts.session_id)
    return runner(argv, timeout=90.0)


# ---------------------------------------------------------------------------
# Cleanup: remove everything this gate staged, best-effort (review item 5)
# ---------------------------------------------------------------------------


def _cleanup_staged(
    runner: Runner, opts: Options, *, log: Callable[[str], None] = lambda _: None
) -> None:
    """Best-effort removal of every file/dir this gate staged on the box,
    AND (finding 11) a sweep for any orphaned tmux socket this gate itself
    could have left behind -- covering the "in finally" half of that
    finding regardless of whether this run actually dispatched (the
    "at start" half is `dispatch_via_interactive_session`'s own sweep).
    Never raises -- a cleanup failure must not mask the real verdict, and
    a staged artifact left behind after a genuine cleanup failure is
    harmless (each is overwritten wholesale on the next run)."""
    cleaned = cleanup_orphan_sessions(runner, opts)
    if cleaned:
        log(f"orphan cleanup (finally): killed {len(cleaned)} stray tmux socket(s): {cleaned}")
    try:
        cleanup_script = (
            f"rm -rf {_harness_dir(opts)} {_driver_script_path(opts)} "
            f"{_check_script_path(opts)}\n"
        )
        run_remote_script(runner, opts, cleanup_script, timeout=20.0)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def fetch_current_published_version(
    fetch: Callable[[str], bytes] | None = None,
) -> str:
    fetch = fetch or (lambda url: urllib.request.urlopen(url, timeout=15).read())  # noqa: S310
    data = json.loads(fetch("https://pypi.org/pypi/conexus/json"))
    version = data.get("info", {}).get("version")
    if not version:
        raise GateError("could not determine the currently published conexus version from PyPI")
    return version


def run_gate(
    opts: Options,
    target_version: str,
    *,
    runner: Runner = default_runner,
    hold: DistroHold | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    session_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> tuple[int, str]:
    """Runs the whole mechanized leg. Returns (exit_code, report_text).
    Never raises for a checked failure -- only for a programming error --
    so callers (and tests) get one place to read the verdict."""
    hold = hold if hold is not None else DistroHold()
    lines: list[str] = []
    #: Cleanup only makes sense (and only avoids a pointless network call on
    #: a pure argument-policy refusal) once we know the box was reached.
    reached_box = False

    def log(msg: str) -> None:
        lines.append(msg)

    try:
        log(f"=== qwentescence local-supervisor 11d gate: target_version={target_version} ===")

        # Finding 7 / point 2: never rely on auto-discovery. The live-
        # dispatch path always mints its own session id; --session-id is
        # accepted only for the documented --skip-dispatch manual-rerun.
        if opts.skip_dispatch:
            if not opts.session_id:
                raise GateError(
                    "--skip-dispatch requires --session-id (the manual-rerun "
                    "path never relies on auto-discovery; see release skill "
                    "§11d)"
                )
            effective_session_id = opts.session_id
            log(f"session id: {effective_session_id} (given, --skip-dispatch)")
        else:
            if opts.session_id:
                raise GateError(
                    "--session-id is only accepted together with --skip-dispatch "
                    "-- the live-dispatch path always mints and forces its own "
                    "session id so a concurrent session on the box can never "
                    "make the result ambiguous"
                )
            effective_session_id = mint_session_id(session_id_factory)
            log(f"session id: {effective_session_id} (minted)")

        validate_options(opts, target_version)

        ensure_reachable(runner, opts)
        reached_box = True
        log("reachable: yes")

        hold.start(opts)
        log(f"distro hold started (hold_seconds={opts.hold_seconds})")

        ensure_systemd_unit_active(runner, opts)
        log("nexus-service systemd --user unit: active, Linger=yes")

        ready_status = wait_for_service_ready(runner, opts, sleep_fn=sleep_fn)
        log(f"service ready: port={ready_status.get('port')}")

        status = assert_lease_port_stable(
            runner, opts, first_status=ready_status, sleep_fn=sleep_fn
        )
        log(f"lease port stable: port={status.get('port')} health=ok pg=up")

        installed = ensure_version(runner, opts, target_version, log=log)
        log(f"installed version confirmed: {installed}")

        if not opts.skip_dispatch:
            dispatch_via_interactive_session(runner, opts, effective_session_id, log=log)
            log("real dispatch: interactive Claude Code session completed (DRIVER_OK)")
        else:
            log("dispatch SKIPPED (--skip-dispatch; reusing an existing completed dispatch)")

        check_opts = replace(opts, session_id=effective_session_id)
        check_result = run_check_script(runner, check_opts)
        log("")
        log("--- post-publish-dispatch-check.sh output ---")
        log(check_result.stdout.rstrip("\n"))
        if check_result.stderr.strip():
            log("--- stderr ---")
            log(check_result.stderr.rstrip("\n"))
        log("--- end post-publish-dispatch-check.sh output ---")
        log("")

        # Finding 9 (round 3 review): a LEDGER MISS (the check script ran
        # and reported a real miss) and a PREREQUISITE-ABSENT report from
        # the check script's own exit 2 are DISTINCT outcomes with DISTINCT
        # exit codes -- never folded into one generic "FAILED" the way
        # `GateFailure` below is also distinct from both.
        if check_result.returncode == 0:
            log(VERDICT_PASS)
            return 0, "\n".join(lines)
        if check_result.returncode == 1:
            log(VERDICT_LEDGER_MISS)
            return 1, "\n".join(lines)
        log(
            f"{VERDICT_PREREQUISITE_ABSENT} (the check script itself reported "
            f"prerequisite-absent, rc={check_result.returncode})"
        )
        return 2, "\n".join(lines)
    except GateFailure as exc:
        # Finding 9: a DRIVER failure (the interactive session itself never
        # started / never went idle / timed out) is a DIFFERENT class from
        # a ledger miss -- it never even reached the check script, so it
        # carries no evidence that anything is actually wrong with the
        # box's own hook wiring. Distinct verdict, distinct exit code (3):
        # the operator action is "rerun once", not "investigate the ledger".
        log(f"DRIVER FAILURE: {exc}")
        log(VERDICT_DRIVER_FAILURE)
        return 3, "\n".join(lines)
    except GateError as exc:
        log(f"PREREQUISITE ABSENT: {exc}")
        log(VERDICT_PREREQUISITE_ABSENT)
        return 2, "\n".join(lines)
    finally:
        if reached_box:
            _cleanup_staged(runner, opts, log=log)
        hold.stop()


def parse_args(argv: Sequence[str]) -> tuple[Options, str | None]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "version",
        nargs="?",
        default=None,
        help="Target conexus version (default: currently published on PyPI).",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--distro", default=DEFAULT_DISTRO)
    parser.add_argument("--remote-user", default=DEFAULT_REMOTE_USER)
    parser.add_argument("--hold-seconds", type=int, default=DEFAULT_HOLD_SECONDS)
    parser.add_argument(
        "--session-id",
        default=None,
        help="Only valid together with --skip-dispatch (manual rerun).",
    )
    parser.add_argument("--skip-dispatch", action="store_true", default=False)
    parser.add_argument("--dispatch-timeout", type=float, default=DEFAULT_DISPATCH_TIMEOUT)
    parser.add_argument(
        "--allow-reinstall",
        action="store_true",
        default=False,
        help="Permit `uv tool install` when the installed version does not match.",
    )
    parser.add_argument(
        "--allow-downgrade",
        action="store_true",
        default=False,
        help="Permit installing an OLDER version than what is currently installed.",
    )
    parser.add_argument(
        "--allow-enable-unit",
        action="store_true",
        default=False,
        help="Permit `systemctl --user enable --now nexus-service` when inactive.",
    )
    args = parser.parse_args(argv)
    opts = Options(
        host=args.host,
        distro=args.distro,
        remote_user=args.remote_user,
        hold_seconds=args.hold_seconds,
        session_id=args.session_id,
        skip_dispatch=args.skip_dispatch,
        dispatch_timeout=args.dispatch_timeout,
        allow_reinstall=args.allow_reinstall,
        allow_downgrade=args.allow_downgrade,
        allow_enable_unit=args.allow_enable_unit,
    )
    return opts, args.version


def main(argv: Sequence[str] | None = None) -> int:
    opts, version = parse_args(argv if argv is not None else sys.argv[1:])
    if version is None:
        try:
            version = fetch_current_published_version()
        except GateError as exc:
            print(f"PREREQUISITE ABSENT: {exc}", file=sys.stderr)
            print(VERDICT_PREREQUISITE_ABSENT, file=sys.stderr)
            return 2
    exit_code, report = run_gate(opts, version)
    print(report)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
