# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog

_log = structlog.get_logger()

# Flat file written by SessionStart hook with the Claude session ID.
# Shared by all Bash subprocesses within one Claude Code conversation.
# os.getsid(0) is NOT used: Claude Code spawns each Bash(...) call in its own
# process session, making getsid different per invocation.
def _nexus_config_dir_at_import() -> Path:
    """Resolve the Nexus config dir honouring ``NEXUS_CONFIG_DIR`` at import time.

    ``session.py`` holds module-level path constants that must be redirectable
    under sandbox / test isolation. Callers setting ``NEXUS_CONFIG_DIR`` in
    the shell before invoking ``nx`` see the constants resolved against the
    sandbox. Tests that need to flip the dir mid-process still monkeypatch
    the module attribute (``nexus.session.CLAUDE_SESSION_FILE``).
    """
    import os as _os  # noqa: PLC0415 - branch-local; deferred to call time

    override = _os.environ.get("NEXUS_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".config" / "nexus"


#: Import-time snapshot kept for backward-compatibility with callers
#: that import the constant directly. New code uses
#: :func:`claude_session_file` (re-resolved per call so tests and
#: subprocesses honour mid-process ``NEXUS_CONFIG_DIR`` flips). The
#: read/write helpers below resolve the path per call so the constant
#: is no longer load-bearing.
CLAUDE_SESSION_FILE = _nexus_config_dir_at_import() / "current_session"


def claude_session_file() -> Path:
    """Return the path to ``current_session`` honouring the live
    ``NEXUS_CONFIG_DIR`` env. Re-resolved per call so a subprocess
    that inherits a different ``NEXUS_CONFIG_DIR`` (test isolation,
    sandbox, sub-agent dispatch) sees its own config dir without
    re-importing the module.
    """
    return _nexus_config_dir_at_import() / "current_session"


def generate_session_id() -> str:
    """Return a new UUID4 session ID string."""
    return str(uuid4())


def write_claude_session_id(session_id: str) -> None:
    """Write the Claude session ID to the stable flat file (mode 0o600)."""
    path = claude_session_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, session_id.encode())
    finally:
        os.close(fd)


def read_claude_session_id() -> str | None:
    """Read the Claude session ID from the flat file, or None if not set."""
    try:
        text = claude_session_file().read_text().strip()
        return text or None
    except OSError:
        return None  # intentional: file not created yet, normal on first run


def resolve_active_session_id(arg: str | None = None) -> str | None:
    """Single source of truth for the active Claude session_id.

    Resolution chain (highest priority first):

    1. Explicit ``arg`` (caller-supplied; non-empty after strip).
    2. ``NX_SESSION_ID`` env var (non-empty after strip). Nexus's own
       override — stays the highest env-based priority so nested
       ``claude -p`` dispatch and tests that force a specific session id
       are never shadowed by tier 3 below.
    3. ``CLAUDE_CODE_SESSION_ID`` env var (non-empty after strip).
       Claude Code sets this natively in every subprocess it spawns
       (Bash tool calls, and Agent-tool-dispatched subagents inherit the
       IDENTICAL value from their parent top-level session — verified
       empirically). It is harness-provided per-process, not file-based,
       so it does not suffer the flat-file's last-writer-wins clobbering
       when a second top-level Claude Code session starts on the same
       machine (nexus-36q84).
    4. ``~/.config/nexus/current_session`` via ``read_claude_session_id``.
       Machine-wide and unscoped — a second top-level Claude Code session
       overwrites this file unconditionally on its own SessionStart
       (``hooks.session_start()``). Now the last-resort fallback for
       genuinely no-harness contexts (e.g. a bare shell invocation with
       neither env var set).
    5. ``None``.

    Returns ``None`` when nothing in the chain resolves. Callers choose
    their own fallback at the call site:

    * ``T1Database._resolve_session_id`` and ``mcp/core._record_tier_write``
      substitute ``"unknown"`` so the per-entry / per-row session_id is
      never empty and the audit log and the T1 chunk store agree on
      attribution. Pre-PR T1 fell back to ``uuid4()`` while tier-write
      fell back to ``"unknown"`` -- the divergence that produced the
      nexus-h8ge bug class even after PR #590 lifted the chain into
      ``T1Database._resolve_session_id``: each open-coded copy could
      drift independently.
    * ``_session_end_launcher._print_tier_status_summary`` short-circuits
      on ``None`` (no useful per-session summary without a bound session
      -- querying ``WHERE session_id = "unknown"`` would leak rows from
      unrelated invocations into the user-facing summary).

    Issue #594 / nexus-9e9a: this helper is the structural fix for the
    three-site drift class. Any future change to the chain happens here
    once.

    nexus-36q84: added the ``CLAUDE_CODE_SESSION_ID`` tier (3) between
    ``NX_SESSION_ID`` and the flat file to close a machine-wide session
    collision — see tier docs above.
    """
    if arg:
        stripped = arg.strip()
        if stripped:
            return stripped
    env = os.environ.get("NX_SESSION_ID", "").strip()
    if env:
        return env
    claude_code_env = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if claude_code_env:
        return claude_code_env
    file_id = read_claude_session_id()
    if file_id:
        return file_id
    return None


# ── Orphan multiprocessing-tracker sweep (nexus-9h1s) ────────────────────────
#
# RDR-155 P4b: the chroma-backed T1 server management (start/stop, tmpdir
# store dirs, orphan-chroma sweeps) died with the chroma substrate. The
# resource-tracker sweep below survives: PPID=1 multiprocessing
# resource_tracker orphans hold POSIX named semaphores regardless of what
# spawned them, and `nx doctor --check-resources` still counts + names it.


def _ppid_of(pid: int) -> int | None:
    """Return the parent PID of *pid*, or None if the process is gone.

    Tries ``/proc/{pid}/status`` first (Linux; works in minimal containers
    without ``ps``), then falls back to ``ps`` (macOS + Linux with procps).
    """
    # Linux: /proc is more reliable than ps in containers (Alpine, distroless).
    status_path = Path(f"/proc/{pid}/status")
    if status_path.exists():
        try:
            for line in status_path.read_text().splitlines():
                if line.startswith("PPid:"):
                    val = int(line.split()[1])
                    return val if val > 1 else None
        except (OSError, ValueError) as exc:
            _log.debug("ppid_proc_read_failed", pid=pid, error=str(exc))

    # Fallback: ps (macOS + Linux with procps)
    try:
        out = subprocess.check_output(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        val = int(out)
        return val if val > 1 else None
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError, OSError):
        return None  # intentional: process gone or ps unavailable — expected during PPID walk





def _is_pid_alive(pid: int) -> bool:
    """Return True if *pid* names a running process (liveness probe).

    Uses ``os.kill(pid, 0)`` — raises ``ProcessLookupError`` when the
    process is gone, ``PermissionError`` when it exists but is owned
    by a different uid (treated as alive). Invalid pids (<=0) are
    treated as dead.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
) -> int:
    """Reap multiprocessing.resource_tracker / spawn workers re-parented to init.

    Each ungraceful MCP shutdown (SIGKILL/OOM, lost SessionEnd hook)
    leaves chroma's multiprocessing workers' resource_tracker
    subprocesses re-parented to init (PPID=1). The trackers continue
    holding their POSIX named semaphores until killed; the namespace
    is bounded (``kern.posix.sem.max=10000`` on macOS) so chronic
    accumulation produces ``Errno 28`` system-wide.

    This sweep runs from `nx doctor --check-resources`' remediation hint.
    Sends SIGTERM (graceful), then SIGKILL after 3 s for any tracker
    that did not exit. Returns the count signalled.

    *command_substring* is a defence-in-depth filter the caller can
    narrow (e.g. tests pass a unique marker so the sweep cannot
    touch unrelated trackers on the dev machine).

    Live shakeout 2026-05-08: 3,314 trackers / 8,359 semaphores
    cleared in single SIGTERM batch on a system that had been
    accumulating for 11+ days. Bead nexus-9h1s.
    """
    try:
        ps_output = subprocess.check_output(
            ["ps", "-eo", "pid,ppid,etime,command"],
            text=True,
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
                    ["ps", "-o", "command=", "-p", str(pid)],
                    text=True,
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
) -> int:
    """SIGTERM each PID in *pids*; escalate to SIGKILL after
    *grace_seconds* for any survivor. Returns the count of PIDs we
    successfully signalled (SIGTERM-step). Best-effort: missing
    PIDs and EPERM are skipped silently. Pure side effects + return
    count; testable independently of the parser."""
    signalled = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            signalled += 1
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            _log.debug("sweep_orphan_tracker_eperm", pid=pid, error=str(exc))
            continue

    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if not any(_is_pid_alive(pid) for pid in pids):
            break
        time.sleep(0.1)
    for pid in pids:
        if _is_pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                continue
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
# processes inherit it through ``NX_SESSION_ID`` (race-free) with the legacy
# ``current_session`` flat file as a fallback for tools launched outside the
# Claude process tree.

_NX_SESSION_ID_ENV = "NX_SESSION_ID"





def _command_name_of(pid: int) -> str:
    """Return the command name (argv[0] basename) of *pid*, or "" if unknown.

    Used by :func:`find_immediate_claude_pid` to identify which ancestor
    is Claude Code. Falls back to an empty string on any error; the
    caller treats that as "not a match" and keeps walking.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-o", "comm=", "-p", str(pid)],
            stderr=subprocess.DEVNULL, text=True, timeout=2,
        ).strip()
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
    session-id, not the claude_pid). This function is retained for its
    remaining non-T1 consumer, ``nexus.phase_review_sentinel``, which keys
    its phase-gate sentinel files by the immediate Claude pid.

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



_LEGACY_SKIP_T1_IGNORED_WARNED: bool = False


def _t1_isolated_env() -> bool:
    """Return True when the current env opts into per-process T1 ephemeral.

    ``NX_T1_ISOLATED=1`` only. The legacy ``NEXUS_SKIP_T1=1`` alias
    (4.27 -> 4.28 deprecation cycle, RF-4) was removed at 6.5.2 — a full
    major past its promised 5.0 removal. It is recognized-but-IGNORED with
    a one-shot warning (critique 2026-07-13: with a live T1 discoverable, a
    stale alias would otherwise SILENTLY connect the caller to the shared
    T1 instead of the isolation they asked for — the warning is the only
    remaining signal).
    """
    global _LEGACY_SKIP_T1_IGNORED_WARNED
    isolated = os.environ.get("NX_T1_ISOLATED", "").strip().lower() in ("1", "true", "yes")
    legacy = os.environ.get("NEXUS_SKIP_T1", "").strip().lower() in ("1", "true", "yes")
    if legacy and not isolated and not _LEGACY_SKIP_T1_IGNORED_WARNED:
        _LEGACY_SKIP_T1_IGNORED_WARNED = True
        _log.warning(
            "nexus_skip_t1_removed_ignored",
            message="NEXUS_SKIP_T1 was REMOVED at 6.5.2 and is IGNORED — it no "
                    "longer isolates T1. Set NX_T1_ISOLATED=1.",
        )
    return isolated


# ── Private helpers ───────────────────────────────────────────────────────────

def _try_remove_path(path: Path) -> None:
    """Remove *path*, ignoring errors."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # intentional: best-effort file cleanup
