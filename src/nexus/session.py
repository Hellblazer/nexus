# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import structlog

from nexus._install import winproc_core
from nexus.bounded_subprocess import run_bounded
from nexus.util.nx_argv import argv_off_cwd

_log = structlog.get_logger()

def _nexus_config_dir_at_import() -> Path:
    """Resolve the Nexus config dir honouring ``NEXUS_CONFIG_DIR`` at import time.

    ``session.py`` holds module-level path constants that must be redirectable
    under sandbox / test isolation. Callers setting ``NEXUS_CONFIG_DIR`` in
    the shell before invoking ``nx`` see the constants resolved against the
    sandbox.
    """
    import os as _os  # noqa: PLC0415 - branch-local; deferred to call time

    override = _os.environ.get("NEXUS_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".config" / "nexus"


def generate_session_id() -> str:
    """Return a new UUID4 session ID string."""
    return str(uuid4())


#: Environment variables a process started by Claude Code carries, measured on
#: live ``nx-mcp`` children of the Claude Code CLI (nexus-xs7fp): the CLI sets
#: both on every subprocess it spawns, MCP servers included. A server started
#: by another host (Cursor, Codex, Gemini, Claude Desktop chat) has neither.
#: ``CLAUDE_CODE_SESSION_ID`` is not listed: :func:`resolve_session_id` reads it
#: as a session id, not as a marker.
_CLAUDE_CODE_ENV_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")

def started_by_claude_code() -> bool:
    """True when the environment carries a Claude Code marker (non-blank)."""
    return any(os.environ.get(name, "").strip() for name in _CLAUDE_CODE_ENV_MARKERS)


def _token_file_session_id() -> str | None:
    """The session id this process's token file records (RDR-253): the file of its nearest Claude ancestor, or the one
    a delegated child was given. Per process, never machine-wide, so a second top-level session cannot clobber it the
    way it clobbered the retired ``current_session`` file. Read whether or not the token is usable now: the session id
    is a label, and an expired token (a laptop sleep, until the MCP heartbeat renews it) does not change which session
    this is. None with no Claude ancestor or no readable file."""
    try:
        from nexus.identity import token_header  # noqa: PLC0415 — deferred: leaf import cost, rare tier

        record = token_header.recorded()
    except Exception:  # noqa: BLE001 — a label lookup never fails its caller
        return None
    sid = record.session_id if record is not None else None
    return (sid.strip() or None) if isinstance(sid, str) else None


def resolve_session_id(arg: str | None = None) -> str | None:
    """The Claude session id this process acts for, or None.

    In order: an explicit *arg*; ``NX_SESSION_ID`` (nexus's own override, which nested ``claude -p`` dispatch and tests
    set); ``CLAUDE_CODE_SESSION_ID`` (Claude Code sets it in every process it spawns; correct at spawn, so a long-lived
    MCP server keeps it across ``/clear`` and the T1 handoff layer moves its scope); then the session id in this
    process's own token file (:func:`_token_file_session_id`). Blank values count as unset.

    RDR-253 Phase 2 retired the fourth tier this used to have, the machine-wide ``~/.config/nexus/current_session``
    file that the last SessionStart on the box wrote: it could name another session's scratch. With it went the gate
    that kept an MCP server some other host started from reading it. A process with no Claude ancestor and no env
    resolves nothing here, and callers keep their own fallback (``"unknown"`` for T1 metadata and tier-write rows; no
    summary for the session-end launcher).
    """
    if arg and arg.strip():
        return arg.strip()
    return resolve_explicit_session_id() or _token_file_session_id()


def resolve_explicit_session_id() -> str | None:
    """The session id ONLY when the environment names one: ``NX_SESSION_ID`` or ``CLAUDE_CODE_SESSION_ID``.

    Unlike :func:`resolve_session_id` this never reads the token file. A caller that sets ``NX_SESSION_ID=<id>`` is
    asking for THAT session's scope (nexus-f7xyq): when no usable T1 lease exists for it, substituting another
    identity's scope would read or write a different session's data while the caller believes it addressed the one it
    named, which the MCP side already treats as security-relevant (``mcp/core.py``'s ``t1_session_unresolved``). A bare
    invocation makes no such claim and keeps :func:`nexus.db.t1.get_t1_database`'s CLI-dedicated tier.

    Returns ``None`` when neither env var is set (non-empty after strip).
    """
    env = os.environ.get("NX_SESSION_ID", "").strip()
    if env:
        return env
    claude_code_env = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if claude_code_env:
        return claude_code_env
    return None


# ── Orphan multiprocessing-tracker sweep (nexus-9h1s) ────────────────────────
#
# RDR-155 P4b: the chroma-backed T1 server management (start/stop, tmpdir
# store dirs, orphan-chroma sweeps) died with the chroma substrate. The
# resource-tracker sweep below survives: PPID=1 multiprocessing
# resource_tracker orphans hold POSIX named semaphores regardless of what
# spawned them, and `nx doctor --check-resources` still counts + names it.


def _is_windows(platform: str | None) -> bool:
    return (platform if platform is not None else sys.platform) == "win32"


def _win_info(
    api: "winproc_core.WinProcessInfoApi | None",
) -> "winproc_core.WinProcessInfoApi":
    return api if api is not None else winproc_core.ctypes_win_info_api()


def _ppid_of(
    pid: int,
    *,
    platform: str | None = None,
    win_info_api: "winproc_core.WinProcessInfoApi | None" = None,
) -> int | None:
    """Return the parent PID of *pid*, or None if the process is gone.

    Tries ``/proc/{pid}/status`` first (Linux; works in minimal containers
    without ``ps``), then falls back to ``ps`` (macOS + Linux with procps).
    Windows (RDR-224, nexus-f9bgu.21) asks the kernel through
    ``NtQueryInformationProcess`` and refuses a parent created after its
    child, since Windows never reparents and a stale parent number can be
    recycled (:func:`nexus._install.winproc_core.parent_pid`).
    """
    if _is_windows(platform):
        return winproc_core.parent_pid(pid, _win_info(win_info_api))
    # Linux: /proc is more reliable than ps in containers (Alpine, distroless).
    status_path = Path(f"/proc/{pid}/status")
    if status_path.exists():
        try:
            for line in status_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("PPid:"):
                    val = int(line.split()[1])
                    return val if val > 1 else None
        except (OSError, ValueError) as exc:
            _log.debug("ppid_proc_read_failed", pid=pid, error=str(exc))

    # Fallback: ps (macOS + Linux with procps)
    try:
        out = subprocess.check_output(
            argv_off_cwd(["ps", "-o", "ppid=", "-p", str(pid)]),
            text=True,
            encoding="utf-8",  # ps is POSIX-only: UTF-8 (nexus-ikoqj)
            stderr=subprocess.DEVNULL,
        ).strip()
        val = int(out)
        return val if val > 1 else None
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError, OSError):
        return None  # intentional: process gone or ps unavailable — expected during PPID walk





def _is_pid_alive(pid: int) -> bool:
    """Return True if *pid* names a running process (liveness probe).

    Delegates to :func:`nexus.daemon.service_registry.pid_alive`, the one
    probe with a Windows branch (nexus-f9bgu.25). Kept as a name because
    the test harness's ``_AliveSet`` patches this seam. Invalid pids (<=0)
    are dead; a process owned by another uid is alive.
    """
    from nexus.daemon.service_registry import pid_alive  # noqa: PLC0415 — deferred: keeps nexus.session's import cost flat for the hooks

    return pid_alive(pid)


def _parse_etime_seconds(etime: str) -> float | None:
    """Parse a ``ps -o etime`` value into seconds.

    Accepts the four shapes ``ps`` emits:

    * ``MM:SS``
    * ``HH:MM:SS``
    * ``DD-HH:MM:SS``
    * Trailing whitespace tolerated.

    Returns ``None`` on parse failure so the caller can decide a
    safe default (typically: skip the row).
    """
    s = etime.strip()
    if not s:
        return None
    days = 0
    if "-" in s:
        d, _, rest = s.partition("-")
        try:
            days = int(d)
        except ValueError:
            return None
        s = rest
    parts = s.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        h, m, sec = 0, nums[0], nums[1]
    elif len(nums) == 3:
        h, m, sec = nums
    else:
        return None
    return float(days * 86400 + h * 3600 + m * 60 + sec)


def _parse_orphan_tracker_candidates(
    ps_output: str,
    *,
    min_age_seconds: float = 60.0,
    protected_pids: set[int] | None = None,
) -> list[int]:
    """Parse the output of ``ps -eo pid,ppid,etime,command`` and
    return the PIDs of orphan multiprocessing trackers safe to reap.

    Conservative match — a row is included iff every condition holds:

    * ``ppid == 1`` (re-parented to init; the original parent is dead).
    * ``command`` contains ``"multiprocessing"`` (matches both
      ``multiprocessing.resource_tracker`` and
      ``multiprocessing.spawn ... --multiprocessing-fork``).
    * Process age >= *min_age_seconds* (avoids racing in-flight
      MCP-startup workers whose parent has not yet attached).
    * ``pid not in protected_pids`` (escape hatch for tests / live
      MCP-managed PIDs the caller wants to spare).

    Returns the list in the order ``ps`` emitted (effectively PID
    order). Pure function for unit testing; callers handle SIGTERM.
    """
    protected = protected_pids or set()
    out: list[int] = []
    for line in ps_output.splitlines():
        s = line.strip()
        if not s or not s[0].isdigit():
            continue  # header row or blank
        # Tokenize: pid, ppid, etime, command-tail
        parts = s.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        if ppid != 1:
            continue
        if pid in protected:
            continue
        if "multiprocessing" not in parts[3]:
            continue
        age = _parse_etime_seconds(parts[2])
        if age is None or age < min_age_seconds:
            continue
        out.append(pid)
    return out


def sweep_orphan_resource_trackers(
    *,
    min_age_seconds: float = 60.0,
    command_substring: str = "multiprocessing",
    protected_pids: set[int] | None = None,
    platform: str | None = None,
) -> int:
    """Reap multiprocessing.resource_tracker / spawn workers re-parented to init.

    Windows has no POSIX named semaphores to leak and does not reparent an
    orphan to pid 1, so there is nothing to find or reap and no ``ps`` to
    run: returns 0 (RDR-224, nexus-f9bgu.21).

    Each ungraceful MCP shutdown (SIGKILL/OOM, lost SessionEnd hook)
    leaves chroma's multiprocessing workers' resource_tracker
    subprocesses re-parented to init (PPID=1). The trackers continue
    holding their POSIX named semaphores until killed; the namespace
    is bounded (``kern.posix.sem.max=10000`` on macOS) so chronic
    accumulation produces ``Errno 28`` system-wide.

    This sweep runs from `nx doctor --check-resources`' remediation hint.
    Sends SIGTERM (graceful) and waits; a tracker that does not exit is
    reported, never SIGKILLed, because a killed tracker leaks the
    semaphores it holds. Returns the count signalled.

    *command_substring* is a defence-in-depth filter the caller can
    narrow (e.g. tests pass a unique marker so the sweep cannot
    touch unrelated trackers on the dev machine).

    Live shakeout 2026-05-08: 3,314 trackers / 8,359 semaphores
    cleared in single SIGTERM batch on a system that had been
    accumulating for 11+ days. Bead nexus-9h1s.
    """
    if _is_windows(platform):
        return 0
    try:
        ps_output = subprocess.check_output(
            argv_off_cwd(["ps", "-ww", "-eo", "pid,ppid,etime,command"]),
            text=True,
            encoding="utf-8",
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        _log.debug("sweep_orphan_trackers_ps_failed", error=str(exc))
        return 0

    candidates = _parse_orphan_tracker_candidates(
        ps_output,
        min_age_seconds=min_age_seconds,
        protected_pids=protected_pids,
    )
    # Apply the caller-supplied substring filter on top of the
    # parser's hard-coded "multiprocessing" gate so tests can scope
    # the sweep to a marker they injected.
    if command_substring != "multiprocessing":
        narrowed: list[int] = []
        for pid in candidates:
            try:
                cmd = subprocess.check_output(
                    argv_off_cwd(["ps", "-ww", "-o", "command=", "-p", str(pid)]),
                    text=True,
                    encoding="utf-8",
                    stderr=subprocess.DEVNULL,
                ).strip()
            except (subprocess.CalledProcessError, OSError):
                continue
            if command_substring in cmd:
                narrowed.append(pid)
        candidates = narrowed

    if not candidates:
        return 0

    signalled = _kill_orphan_tracker_pids(candidates)
    _log.info(
        "sweep_orphan_trackers_reaped",
        count=signalled,
        candidates=len(candidates),
    )
    return signalled


def _kill_orphan_tracker_pids(
    pids: list[int],
    *,
    grace_seconds: float = 3.0,
    _command: Callable[[int], str] | None = None,
) -> int:
    """SIGTERM each PID in *pids*; after *grace_seconds* hard-kill the
    survivors that are NOT a ``multiprocessing`` resource tracker, then wait
    for the trackers to exit on their own. Returns the count of PIDs we
    successfully signalled (SIGTERM-step). Best-effort: missing PIDs and EPERM
    are skipped silently.

    A resource tracker is never sent ``SIGKILL`` (nexus-x3m8e). It ignores
    ``SIGTERM`` by design, and it is the process that ``sem_unlink``s the
    named semaphores its workers made; a ``SIGKILL`` after the grace period,
    which is what this function used to send every survivor, killed it before
    it could and leaked those semaphores until reboot, the exhaustion this sweep
    exists to relieve. A tracker exits by itself once the last process holding
    its pipe is gone, so the sweep kills the workers (``multiprocessing.spawn``
    children, which are not trackers and may be hard-killed), waits, and
    REPORTS a tracker that is still there (a warning naming the pids), rather
    than killing it. A survivor whose command line cannot be read is left alone
    for the same reason: the cost of leaving a process is a warning, the cost of
    killing a tracker is permanent.

    Unreachable from the production sweep on Windows (it returns before it
    lists anything), but correct for a direct caller there: a gone pid is an
    ``OSError`` on Windows, and the hard kill goes through
    ``service_registry.hard_kill_pid`` (RDR-224, nexus-f9bgu.44)."""
    # Deferred, like the other service_registry import in this module: keeps
    # nexus.session's import cost flat for the hooks.
    from nexus.daemon.service_registry import hard_kill_pid, process_command  # noqa: PLC0415
    from nexus.util.process_group import RESOURCE_TRACKER_MARKER  # noqa: PLC0415

    command = _command if _command is not None else process_command
    signalled = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            signalled += 1
        except PermissionError as exc:
            _log.debug("sweep_orphan_tracker_eperm", pid=pid, error=str(exc))
            continue
        except OSError:
            # ProcessLookupError on POSIX; on Windows a pid that has already
            # exited raises a plain OSError (WinError 87), not that subclass.
            continue

    def _wait_all_gone(targets: list[int]) -> None:
        deadline = time.time() + grace_seconds
        while time.time() < deadline:
            if not any(_is_pid_alive(pid) for pid in targets):
                break
            time.sleep(0.1)

    _wait_all_gone(pids)
    trackers: list[int] = []
    for pid in pids:
        if not _is_pid_alive(pid):
            continue
        cmd = command(pid)
        if RESOURCE_TRACKER_MARKER in cmd:
            trackers.append(pid)
        elif cmd:
            hard_kill_pid(pid)  # a worker: never raises for a pid that is gone
        else:
            _log.debug("sweep_orphan_tracker_unreadable_command_left_alone", pid=pid)
    if trackers:
        _wait_all_gone(trackers)
        left = [pid for pid in trackers if _is_pid_alive(pid)]
        if left:
            _log.warning("sweep_orphan_trackers_survivors_not_killed", pids=left)
    return signalled


# ── UUID-keyed session records (the current scheme) ──────────────────────────
#
# T1 must be scoped to a Claude conversation, not to a terminal session. The
# previous PID-keyed scheme walked the PPID chain to "find the ancestor's
# session file" — which on systems where Claude Code is invoked directly from
# a shell lands on the login shell's PID. Two ``claude`` invocations in the
# same shell then shared one T1 server; the same conversation accessed from a
# different shell could not find it. The UUID-keyed scheme fixes both: the
# Claude session UUID arrives via the SessionStart hook payload, and child
# processes inherit it through ``NX_SESSION_ID`` (race-free), and a process
# with neither finds it in its own token file (RDR-253).

_NX_SESSION_ID_ENV = "NX_SESSION_ID"





def _command_name_of(
    pid: int,
    *,
    platform: str | None = None,
    win_info_api: "winproc_core.WinProcessInfoApi | None" = None,
) -> str:
    """Return the command name (argv[0] basename) of *pid*, or "" if unknown.

    Used by :func:`find_immediate_claude_pid` to identify which ancestor
    is Claude Code. Falls back to an empty string on any error; the
    caller treats that as "not a match" and keeps walking. On Windows the
    name is the image's file name without ``.exe``, which is what POSIX
    ``comm`` looks like (RDR-224, nexus-f9bgu.21).
    """
    if _is_windows(platform):
        return winproc_core.process_command_name(pid, _win_info(win_info_api))
    try:
        out = run_bounded(
            ["ps", "-o", "comm=", "-p", str(pid)],
            stderr=subprocess.DEVNULL, check=True, timeout=2,
        ).stdout.strip()
        return Path(out).name if out else ""
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            OSError):
        return ""





# ── RDR-105 hybrid-discovery primitives ──────────────────────────────────────
#
# The single T1 discovery surface as of P4 (nexus-jnx7). The legacy
# session-record machinery (multi-writer ``<uuid>.session`` JSON files,
# the topmost-walk ``find_claude_root_pid``, the watchdog sidecar, the
# reconcile probe) was deleted along with the bug class it produced.


def find_immediate_claude_pid(start_pid: int | None = None) -> int:
    """Return the FIRST ``claude*`` ancestor walking up from *start_pid*.

    RDR-105 RF-6: returns the immediate (not topmost) ``claude*``
    ancestor so nested owned ``claude -p`` subprocesses resolve their own
    immediate Claude rather than the user's top-level one.

    RDR-149 P4 retired the T1 addr-file publish/discovery that originally
    motivated this (T1 now keys its leased registry record on the
    session-id, not the claude_pid). Consumer today: nexus-d76vc's T1
    handoff marker -- both the SessionStart hook's writer (:func:`find_mcp_sibling_pids`, below) and
    the MCP lifespan's watcher use this SAME function so a marker's
    claimed ``claude_pid`` is always checked against the identical
    "immediate, not topmost" ancestor on both sides.

    Falls back to the immediate PPID when no ``claude*`` ancestor is
    found (matches the no-claude-in-chain semantics so consumers behave
    identically in that case). Returns 0 only when the PPID chain cannot
    be walked at all.
    """
    pid = start_pid if start_pid is not None else os.getpid()
    seen: set[int] = set()
    cur = _ppid_of(pid)
    immediate_ppid = cur or 0
    while cur and cur not in seen and cur > 1:
        seen.add(cur)
        if _command_name_of(cur).lower().startswith("claude"):
            return cur
        cur = _ppid_of(cur)
    return immediate_ppid


#: nexus-d76vc: command names the T1 handoff marker writer/watcher treat as
#: an MCP server sibling. Matches the two ``[project.scripts]`` entry
#: points in pyproject.toml (``nx-mcp`` / ``nx-mcp-catalog``) that Claude
#: Code's ``.mcp.json`` spawns under the claude process: as direct children
#: until RDR-228, and behind ``uv`` and the plugin's launcher since.
_MCP_SIBLING_COMMANDS = ("nx-mcp", "nx-mcp-catalog")

#: RDR-228: what the plugin's server entries put between claude and the server,
#: and the only processes :func:`find_mcp_sibling_pids` looks through. ``uv`` is
#: the command the entry names; the script is the launcher it runs.
_LAUNCH_WRAPPER_COMMANDS = frozenset({"uv"})  # a set: this names a process to recognise, never an argv to spawn
_LAUNCHER_SCRIPT = "/launch/nx_launch.py"

#: Substrings that make a row worth a second look when its first token names
#: nothing known (a path with a space in it): see :func:`_mcp_process_kind`.
_NAMES_OF_INTEREST = ("nx-mcp", "nx_launch.py", "uv")

#: How many wrapper levels may sit between claude and a server. The longest
#: chain the plugin produces is three, on Windows: ``uv.exe``, uv's
#: ``python.exe`` stub, the interpreter running the launcher.
_MAX_WRAPPER_DEPTH = 4

#: How many server-shaped processes may sit under a server's own launcher. On
#: Windows there are two: the generation's venv ``python.exe`` redirector and
#: the interpreter it starts.
_MAX_SERVER_DEPTH = 3

#: Executables that count as "an interpreter running a console script" for
#: :func:`find_mcp_sibling_pids`. Both entry points above are Python console
#: scripts, so on a real box ``ps`` reports the interpreter as the executable
#: and the script as argv[1]; without this the matcher would have to accept
#: ANY argv[1] match and would then also match ``vim ~/.local/bin/nx-mcp``.
#: Case-insensitive: a macOS framework Python (Homebrew ``python@3.x``, the
#: python.org installer) runs through its app stub, ``.../Python.app/Contents/
#: MacOS/Python``, so the executable is ``Python``; a lowercase-only match found
#: no server there and the /clear handoff marker was never written (nexus-hczdo.1.11).
_PYTHON_EXECUTABLE_RE = re.compile(r"^python[0-9.]*$", re.IGNORECASE)


def _list_processes(
    *,
    ppid: int | None = None,
    platform: str | None = None,
    win_info_api: "winproc_core.WinProcessInfoApi | None" = None,
) -> list[tuple[int, int, str]]:
    """Return ``(pid, ppid, command_line)`` for every live process,
    best-effort.

    *ppid* narrows the answer to one parent's children. POSIX reads the
    whole table in one ``ps`` either way and the caller filters; Windows
    pays one process open per row it returns, so it narrows BEFORE opening
    (this runs on the SessionStart hook path). On Windows the command line
    is the space-joined argv from :mod:`nexus._install.winproc_core`
    (RDR-224, nexus-f9bgu.21); a snapshot failure is an empty list, the
    same posture as a failed ``ps``.

    Real process-table enumeration via ``ps -eo pid,ppid,args=`` (portable
    across macOS + Linux; unlike :func:`_ppid_of` this has no ``/proc``
    fast path since a single ``ps`` call already returns the whole table
    in one shot, cheaper than a ``/proc`` walk for this use). Empty list
    on any failure -- callers treat that as "no siblings found", never a
    crash (mirrors :func:`_command_name_of`'s empty-string-on-error
    posture).

    **``args=``, NOT ``comm=``.** ``comm`` is the EXECUTABLE, and every
    process this module cares about finding is a ``[project.scripts]``
    console script -- a shebang wrapper the kernel runs via the
    interpreter -- so ``comm`` reports ``.../bin/python3`` and the script
    name lives in argv[1]. Enumerating by ``comm`` therefore could never
    match ``nx-mcp``, which is why the nexus-d76vc T1 handoff silently
    never fired on any real machine from the day it shipped. See
    :func:`find_mcp_sibling_pids` for the matching rule this feeds.

    **``-ww`` is load-bearing, not decoration.** Without it procps truncates
    the whole line to the terminal width (80 when stdout is not a tty), and
    the real invocation --
    ``/Users/x/.local/share/uv/tools/conexus/bin/python3 /Users/x/.local/bin/nx-mcp``
    at ~104 characters -- loses the ``nx-mcp`` argument that identifies it.
    Measured on a GitHub Actions runner 2026-08-10: rows came back cut at
    exactly 80 columns (``.../.venv/bin/python /tmp/pytest-of-ru``), so the
    matcher below found nothing. macOS ps does not truncate a piped stream,
    which is why this reproduced only in CI; BSD and procps both accept
    ``-ww``.
    """
    if _is_windows(platform):
        try:
            return [
                (pid, parent, command)
                for pid, parent, _age, command in winproc_core.enumerate_processes(
                    _win_info(win_info_api), only_ppid=ppid,
                )
            ]
        except (RuntimeError, OSError):
            return []
    try:
        out = run_bounded(
            ["ps", "-ww", "-eo", "pid,ppid,args="],
            stderr=subprocess.DEVNULL, check=True, timeout=5,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return []
    result: list[tuple[int, int, str]] = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        result.append((pid, ppid, parts[2].strip()))
    return result


def find_mcp_sibling_pids(claude_pid: int) -> list[int]:
    """Return live ``nx-mcp`` / ``nx-mcp-catalog`` pids whose nearest claude
    ancestor is *claude_pid*: a direct child, or one reached through the
    plugin's launch wrappers only.

    **Through the wrappers (RDR-228, nexus-sv2wx.8).** The plugin starts the
    servers as ``uv tool run ... python launch/nx_launch.py nx-mcp``, and uv
    stays alive as the parent of what it runs (measured, uv 0.8.0), so the
    server is a grandchild of claude; on Windows the launcher stays too, and
    uv's ``python.exe`` can be a stub with the interpreter under it. Requiring
    a direct child found nothing there, and the handoff marker below was never
    written. The walk therefore looks through a child that is ``uv`` or a
    python running ``nx_launch.py``, to :data:`_MAX_WRAPPER_DEPTH` levels, and
    through nothing else: not a shell, not a test runner, and never another
    claude, whose servers are its own. That keeps the property the direct-child
    rule gave (see below) for every chain the plugin can produce.

    nexus-d76vc: the ancestry check for the T1-handoff marker writer
    (``nexus.hooks._write_t1_handoff_markers``). A marker is only ever
    written for an MCP pid discovered THIS way -- filtered by immediate
    parentage, never by scanning for the command name alone -- so a
    concurrent session's hook can never target another session's MCP
    server: two distinct top-level claude processes never share a parent
    pid for their own spawned MCP servers (the same "immediate, not
    topmost" ancestry reasoning :func:`find_immediate_claude_pid` already
    relies on, applied in the other direction: parent -> children instead
    of child -> ancestor).

    Returns an empty list for a non-positive *claude_pid* or when process
    enumeration fails (see :func:`_list_processes`) -- fail-safe: no
    siblings found means no markers get written, never a guess.

    **Matching rule.** ``_list_processes`` yields the full command line. A
    row matches when either

    * the EXECUTABLE's basename is an MCP command (``nx-mcp`` exec'd
      directly), or
    * the executable is a PYTHON INTERPRETER and argv[1]'s basename is an
      MCP command (``.../conexus/bin/python3 .../bin/nx-mcp`` -- the shape a
      shebang console script actually produces, and the one the original
      ``comm``-only matcher could never see).

    The interpreter check is what keeps this from matching a child that
    merely NAMES nx-mcp in an argument -- ``vim ~/.local/bin/nx-mcp`` and
    ``grep -r nx-mcp src/`` are both argv[1] matches and neither is an MCP
    server. Writing a marker keyed on such a pid would target an unrelated
    process.

    Note the fail-safe posture cuts both ways: an empty return is
    indistinguishable from "the matcher is broken", which is exactly how
    this function returned ``[]`` for every live MCP server for its whole
    shipped life without producing a single log line. The regression test
    for it enumerates a REAL process table rather than a fabricated one.
    """
    if claude_pid <= 0:
        return []
    # POSIX: one ``ps`` returns the whole table, read once. Windows pays one
    # process open per row, so each level asks for one parent's children.
    table = None if _is_windows(None) else _list_processes()

    def children(parent: int) -> list[tuple[int, str]]:
        rows = _list_processes(ppid=parent) if table is None else table
        return [(pid, command) for pid, ppid, command in rows if ppid == parent]

    matches: list[int] = []
    seen: set[int] = {claude_pid}
    frontier = [claude_pid]
    for _depth in range(_MAX_WRAPPER_DEPTH + 1):
        wrappers: list[int] = []
        for parent in frontier:
            for pid, command_line in children(parent):
                if pid in seen:
                    continue
                seen.add(pid)
                kind = _mcp_process_kind(pid, command_line)
                if kind == "server":
                    matches.append(pid)
                    # Windows: ``nx-mcp.exe`` is a launcher stub, the
                    # generation's ``Scripts\python.exe`` under it is a venv
                    # redirector, and the interpreter under THAT is the server,
                    # the pid that claims the marker (listed from live processes,
                    # nexus-217c3). Every level is returned; a marker nobody
                    # claims is inert.
                    below = [pid]
                    for _level in range(_MAX_SERVER_DEPTH):
                        below = [
                            child for parent_pid in below for child, child_line in children(parent_pid)
                            if child not in seen and _mcp_process_kind(child, child_line) == "server"
                        ]
                        if not below:
                            break
                        seen.update(below)
                        matches.extend(below)
                elif kind == "wrapper":
                    wrappers.append(pid)
        frontier = wrappers
        if not frontier:
            break
    return matches


def _mcp_process_kind(pid: int, command_line: str) -> str:
    """``"server"``, ``"wrapper"`` or ``""`` for one process row.

    A server is ``nx-mcp`` / ``nx-mcp-catalog`` run directly, or a python whose
    script is one of them. A wrapper is ``uv``, or a python running the plugin's
    ``nx_launch.py`` (see :func:`find_mcp_sibling_pids`).

    The executable is read from the first token of the command line. That token
    is wrong when the executable's path holds a space: the listing is the argv
    joined with spaces, so ``C:\\Users\\John Smith\\.local\\bin\\uv.exe`` arrives
    as ``C:\\Users\\John`` and the row matched nothing, for every Windows user
    whose profile name has a space. When the first token names nothing known,
    looks cut short (a path that does not end in ``.exe``) and the line mentions
    a name of interest, the process's own image name is asked
    for (:func:`_command_name_of`), and the script is then looked for in any
    later token that is a path, since the argument positions are unknown too.
    """
    tokens = command_line.split()
    if not tokens:
        return ""
    # ``executable_stem``: either separator, no ``.exe`` -- a Windows
    # ``nx-mcp.exe`` launcher and ``python.exe`` match the same names
    # (RDR-224, nexus-f9bgu.21); the identity of a POSIX name.
    executable = winproc_core.executable_stem(tokens[0])
    script_tokens = tokens[1:2]
    known = executable in _MCP_SIBLING_COMMANDS or executable in _LAUNCH_WRAPPER_COMMANDS \
        or bool(_PYTHON_EXECUTABLE_RE.match(executable))
    if not known:
        first = tokens[0]
        cut_short = ("/" in first or "\\" in first) and not first.lower().endswith(".exe")
        if not cut_short or len(tokens) < 2 \
                or not any(name in command_line for name in _NAMES_OF_INTEREST):
            return ""
        executable = winproc_core.executable_stem(_command_name_of(pid))
        script_tokens = [t for t in tokens[1:] if "/" in t or "\\" in t]
    if executable in _MCP_SIBLING_COMMANDS:
        return "server"
    if executable in _LAUNCH_WRAPPER_COMMANDS:
        return "wrapper"
    if not _PYTHON_EXECUTABLE_RE.match(executable):
        return ""
    if any(winproc_core.executable_stem(t) in _MCP_SIBLING_COMMANDS for t in script_tokens):
        return "server"
    if any(t.replace("\\", "/").endswith(_LAUNCHER_SCRIPT) for t in tokens[1:]):
        return "wrapper"
    return ""



#: ``_t1_isolated_env()`` (the NX_T1_ISOLATED=1 opt-in check) was removed at
#: nexus-4lkmz: the isolated/ephemeral in-process T1 leg it gated is deleted
#: outright (Hal determination 2026-07-28 — "T1 exists in PG only. The need
#: for an isolated, ephemeral T1 has been eliminated."). The env var is now a
#: hard-fail, checked directly in :mod:`nexus.db.t1` via
#: ``_raise_if_t1_isolated_requested`` — there is no "is it set" query left
#: to answer, only "fail loud if it is."


# ── Private helpers ───────────────────────────────────────────────────────────

def _try_remove_path(path: Path) -> None:
    """Remove *path*, ignoring errors."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # intentional: best-effort file cleanup
