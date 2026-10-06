# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx-hook upgrade-auto``: the SessionStart self-upgrade (RDR-215 item 6).

``hooks.json`` used to declare this as a shell string::

    nx upgrade --auto 2>/dev/null || echo '<guidance>' >&2

Exec form has no shell, so the redirect and the ``||`` have nowhere to run.
This module is where they went. It is deliberately a thin wrapper: the work
still happens in a separate ``nx upgrade --auto`` PROCESS, exactly as before.

**Why a subprocess and not an in-process call.** ``nexus.commands.upgrade``
is importable, and calling it here would save a spawn. It would also run the
upgrade ladder inside the hook's own interpreter — and that ladder installs
generations and flips ``<tools>/current`` underneath the caller. ``nx`` is a
shim that resolves ``current`` at spawn time and is meant to BE the process
being upgraded; ``nx-hook`` is not. Keeping the boundary keeps today's
semantics unchanged, which is the whole ask for a re-declaration bead.

**What a nonzero exit means.** ``--auto`` is documented "exit 0 always" and
the code backs it: :func:`nexus.commands.upgrade.upgrade` catches every
exception under ``auto_mode`` and returns. So a nonzero status is not an
upgrade that failed — it is an ``nx`` that is missing, or too old to know the
flag. That is version skew, and :data:`SKEW_GUIDANCE` is what the shell's
``|| echo`` said about it.

**The guidance is not the only skew path, and is not the load-bearing one.**
``version_lockstep_hook.py`` stays plugin-resident and stdlib-only precisely
so it still runs when ``nx`` is old or absent; it detects the skew, emits an
``additionalContext`` nudge naming the versions, and dispatches the detached
reinstall. This line is the cruder duplicate that predates it, kept because a
box with no ``nx`` at all is one the lockstep hook cannot upgrade either, and
then a sentence on stderr is the only thing left.

**The child is detached and the hook does not wait for it (nexus-wozn6).**
Since 7.68 ``nx upgrade --auto`` walks the RDR-192 rung, which is pending
after every package-version change and whose census reads every collection in
turn: measured 51.5 s over 98 collections on 2026-10-05, plus about 2.7 s of
fixed work, against this hook's 30 s cap. The verb used to wait on the child
with its streams on pipes, on the stated premise that Claude Code's kill would
leave the child running to finish in the background.

The most likely reading of the record is that the premise was false, and that
is an inference, not an observation. After 7.70 and 7.71 were installed the
hook was cancelled at its 30 s timeout (6 cancelled SessionStarts in each
window, by the transcript re-count) and the ladder recorded the rung for
neither version, which one surviving 55 s child would have done. Nobody killed
the old verb and looked at its child, and a census that defers records
nothing either, so the missing record cannot tell the two apart. The census
cost explains the cancels after 7.68 and does not explain all of them: 23 fell
on 2026-09-30, before the rung shipped. The mechanism assumed is the
nexus-34f7r measurement of the SessionEnd launcher, where a cancelled hook's
kill walks the process tree and reaches a child through its still-living
parent.

So the child now starts in a session of its own with stdout on the null device
and stderr appended to ``<config>/logs/upgrade-auto-child.log`` (size-capped,
:func:`_open_child_log`), and the hook waits at most :data:`_SKEW_WAIT_S` and
returns, long before any cancel. One ``upgrade_auto_child_spawned`` event with
the child's pid and argv goes to ``hook.log``. No detached real run has been
observed to completion. The acceptance is the outcome: after the next version
change, the upgrade ledger advances to the installed version with no manual
run. If it does not, the premise above is wrong.

**No ceiling on the child.** A timeout that killed it would put a kill partway
through a ladder rung, which is worse than letting it finish, and a ceiling
cannot be added from here without a supervising process around ``nx``. A
wedged child is bounded only by the HTTP timeouts inside ``nx upgrade``;
the spawn event and the stderr log are how one is found.
"""
from __future__ import annotations

import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import IO, Any

from nexus._hook_runtime._io import HookResult, _emit  # noqa: PLC2701 — _emit is the shared never-stdout logging spine (its docstring says why)

#: The exact sentence the shell form echoed. Kept verbatim: it is the only
#: user-facing string in this path, it names a remedy, and a box reading it
#: is by definition one whose tooling is too old to have anything better.
SKEW_GUIDANCE = (
    "conexus plugin requires conexus >= 4.2.0 — run: nx self install "
    "(or uv tool upgrade conexus on an un-migrated box)"
)


#: How long the hook waits for the child before leaving it to run on. Long
#: enough to see the skew case: an ``nx`` that does not know ``--auto`` exits
#: 2 on the flag in about 0.17 s (measured 2026-10-05). Far short of the 30 s
#: cap in hooks.json, so the hook is never the process a cancel kills;
#: ``tests/test_hook_budgets_pinned_to_hooks_json.py`` holds it there.
_SKEW_WAIT_S = 2.0

#: Windows, decided once so a test can steer the spawn path without patching
#: ``os.name`` (pathlib reads that on every ``Path()``).
_IS_WINDOWS = os.name == "nt"

# Windows creation flags, by value so this module never imports anything for
# them. CREATE_NO_WINDOW, not DETACHED_PROCESS: a detached process has NO
# console, so a console grandchild it starts (``nx.exe`` is a trampoline that
# starts ``python.exe``) allocates a new visible one and flashes a window on
# every session start. CREATE_NO_WINDOW gives the child a hidden console its
# children inherit. (The two are mutually exclusive: CREATE_NO_WINDOW is
# ignored when DETACHED_PROCESS is set.) CREATE_NEW_PROCESS_GROUP keeps a
# Ctrl-C aimed at the hook off the child; CREATE_BREAKAWAY_FROM_JOB takes it
# out of a job object whose kill-on-close would end it with the hook.
# Untested on a real Windows host: the flag combination is pinned by a
# monkeypatched Popen only.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000

#: The child's stderr log is rotated to ``<name>.1`` when a new child starts
#: and finds it at this size, so the pair holds at most twice this plus one
#: run's output. Stderr under ``--auto`` carries warnings and tracebacks, not
#: progress, so a run writes little.
_CHILD_LOG_MAX_BYTES = 1024 * 1024


def _child_log_path() -> Path:
    """``<config>/logs/upgrade-auto-child.log`` (``NEXUS_CONFIG_DIR`` wins).
    Resolved here with stdlib only: ``nexus.config`` and ``logging_setup``
    cost far more than this hook's whole import."""
    cfg = os.environ.get("NEXUS_CONFIG_DIR") or str(Path.home() / ".config" / "nexus")
    return Path(cfg) / "logs" / "upgrade-auto-child.log"


def _open_child_log() -> tuple[IO[bytes] | None, Path | None]:
    """Open the child's stderr log for append, rotating it first if it is at
    :data:`_CHILD_LOG_MAX_BYTES`. ``(None, None)`` when it cannot be opened
    (read-only home, no HOME): the caller falls back to the null device, so a
    log problem never stops the upgrade."""
    try:
        path = _child_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size >= _CHILD_LOG_MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
        except FileNotFoundError:
            pass
        return path.open("ab"), path
    except (OSError, RuntimeError):  # RuntimeError: Path.home() with no resolvable home
        return None, None


def _spawn_detached(argv: list[str], stderr: IO[bytes] | int = subprocess.DEVNULL) -> subprocess.Popen[Any]:
    """Start *argv* outside the hook's session (POSIX) or process group and
    job (Windows), stdin and stdout on the null device and stderr on *stderr*.
    Raises ``OSError`` when no attempt could start it."""
    common: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": stderr,
        "close_fds": True,
    }
    if not _IS_WINDOWS:
        return subprocess.Popen(argv, start_new_session=True, **common)  # noqa: S603 — argv list, resolved binary, no shell
    base = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(argv, creationflags=base | _CREATE_BREAKAWAY_FROM_JOB, **common)  # noqa: S603 — argv list, resolved binary, no shell
    except OSError:
        # A job that forbids breakaway refuses the flag; start without it.
        return subprocess.Popen(argv, creationflags=base, **common)  # noqa: S603 — argv list, resolved binary, no shell


def run(payload: dict | None) -> HookResult:  # noqa: ARG001 — reads no stdin, as the shell form read none
    """Start ``nx upgrade --auto`` detached; emit :data:`SKEW_GUIDANCE` if it
    fails inside :data:`_SKEW_WAIT_S`.

    Returns a stdout-silent :class:`HookResult` on every path. The child's
    stdout goes to the null device, which is a deliberate tightening: under
    the shell form the child inherited fd 1, so anything ``nx`` printed there
    was handed to Claude Code as this hook's JSON decision. Its stderr goes
    to a log file (:func:`_open_child_log`), where the shell form sent it to
    ``/dev/null``: the child now outlives the hook, so a failure in it has no
    other place to be found. Null or file rather than a pipe because the
    child outlives this process, and a write to a pipe whose reader has
    exited kills the writer with SIGPIPE.

    Discarding stdout loses nothing, and that was checked rather than assumed.
    Every ``click.echo`` on this command's path is guarded by
    ``not auto_mode`` (``nexus.commands.upgrade``: the precondition lines, the
    per-rung convergence lines, the empty-registry line, the deferred-rung
    notices), and the two unguarded ones sit inside ``if dry_run:``, which no
    hook invocation reaches. Under ``--auto`` and without ``--dry-run`` the
    command is silent on stdout by construction, so the shell form was
    forwarding an empty stream to the decision channel.

    No timeout kills the child, deliberately: a kill partway through a ladder
    rung is worse than letting it finish unattended, and the rung's own
    cross-process lock turns concurrent session starts into quick deferrals.
    The spawn is logged once, as ``upgrade_auto_child_spawned`` in
    ``hook.log`` with the pid, argv and (if it exited inside the wait) its
    exit status.
    """
    from nexus.util.nx_argv import nx_argv_for, which_off_cwd  # noqa: PLC0415 — stdlib-only, a few microseconds; function-level so the hook module itself stays import-light

    nx = which_off_cwd("nx")
    if nx is None:
        sys.stderr.write(SKEW_GUIDANCE + "\n")
        return HookResult()
    argv = nx_argv_for(nx, "upgrade", "--auto")
    log_handle, log_path = _open_child_log()
    try:
        proc = _spawn_detached(argv, log_handle if log_handle is not None else subprocess.DEVNULL)
    except (OSError, ValueError) as exc:
        # The spawn itself failed (no fork, bad exec). The shell saw this as a
        # nonzero status and fired the same `||`.
        sys.stderr.write(SKEW_GUIDANCE + "\n")
        _emit("warning", "upgrade_auto_spawn_failed", argv=argv, error_type=type(exc).__name__, error=str(exc))
        return HookResult()
    finally:
        if log_handle is not None:
            log_handle.close()  # the child holds its own copy of the descriptor
    pid = proc.pid
    returncode: int | None
    try:
        returncode = proc.wait(timeout=_SKEW_WAIT_S)
    except subprocess.TimeoutExpired:
        # Still running: an upgrade doing real work, not skew. Leave it. The
        # handle is dropped on purpose; Popen.__del__ would otherwise print
        # "subprocess N is still running" on the hook's stderr.
        returncode = None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            del proc
    if returncode:
        sys.stderr.write(SKEW_GUIDANCE + "\n")
    _emit(
        "info", "upgrade_auto_child_spawned",
        pid=pid, argv=argv, returncode=returncode,
        stderr_log=str(log_path) if log_path is not None else None,
    )
    return HookResult()
