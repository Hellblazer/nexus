#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-121 routing-hook framework.

Helpers every routing hook imports. The hook protocol is:

* Read JSON from stdin (the Claude Code PreToolUse payload).
* Print exactly one JSON envelope to stdout.
* Exit 0 on every code path including unexpected exceptions.

Decision envelope shape (PreToolUse):

    allow:
        {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": "..."   # only when allow carries advisory text
        }}

    deny (see ``deny_envelope`` — the reason rides in two audience-specific
    fields, with ``reason`` kept for legacy compatibility):
        {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "<full reason>",   # what the MODEL reads
            "reason": "<full reason>"                       # legacy alias
         },
         "systemMessage": "<short summary>"}                # the USER's transcript banner

Fail-open is the default. Hooks opt in to fail-closed by passing
``fail_closed=True`` to ``run_hook``; the registry.yaml ``fail_closed:
true`` flag is the source of truth and the hook script reads its own
rule entry to decide.

Escape token: a command may include ``# routing-allow: <reason>``
(reason >= 8 characters) to bypass any routing hook. The token is
audited in the telemetry log so over-use is visible.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
import sys
import time
from typing import Any, Callable

ESCAPE_TOKEN = "# routing-allow:"
ESCAPE_REASON_MIN_LENGTH = 8

_DEFAULT_LOG_PATH = pathlib.Path.home() / ".config" / "nexus" / "routing_log.jsonl"


# ---------------------------------------------------------------------------
# Envelope builders (pure — return JSON strings)
# ---------------------------------------------------------------------------


def allow_envelope(context: str = "") -> str:
    """Return an allow envelope as a JSON string."""
    payload: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
    }
    if context:
        payload["additionalContext"] = context
    return json.dumps({"hookSpecificOutput": payload})


def deny_envelope(reason: str, summary: str | None = None) -> str:
    """Return a deny envelope as a JSON string.

    The reason rides in three fields for cross-version robustness:

    * ``permissionDecisionReason`` -- the canonical PreToolUse field
      current Claude Code feeds back to the model on a deny. Carries the
      *full* ``reason`` (cause + remediation) so the model can correct.
    * ``systemMessage`` (top-level) -- surfaced in the user transcript.
      Carries the short ``summary`` so the banner stays a one-liner
      instead of the full remediation essay.
    * ``reason`` -- the legacy key earlier envelopes used.

    Earlier envelopes carried *only* ``reason``, which current Claude
    Code does not read: a deny then arrived as a bare "denied" with no
    cause and no remediation, leaving the model to guess what to do
    next. Emitting the canonical field is what makes the redirect
    message actually reach the model.

    ``summary`` decouples the two audiences. When omitted, the first
    non-empty line of ``reason`` is used so callers that don't supply a
    summary still get a terse banner rather than the whole block.
    """
    # Strip BEFORE the truthiness check: a whitespace-only reason is truthy, so
    # ``reason or default`` would keep it, and ``"".splitlines()[0]`` would then
    # IndexError. deny_envelope is on every routing hook's deny path, so it must
    # never raise. Stripping makes the guard fire and keeps the first-line slice
    # safe (reason is now non-empty).
    reason = reason.strip() or "(no reason provided)"
    system_message = summary or reason.splitlines()[0]
    payload = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
        "reason": reason,
    }
    return json.dumps(
        {"hookSpecificOutput": payload, "systemMessage": system_message}
    )


def warn_envelope(message: str) -> str:
    """Semantic alias for ``allow_envelope`` that signals advisory intent.

    Routing hooks emit warnings when a pattern looks suspicious but the
    command should proceed. The permission decision stays ``allow``;
    the message rides in ``additionalContext`` so the user sees it.
    """
    return allow_envelope(message)


# ---------------------------------------------------------------------------
# Stdout writers (impure — print then exit 0)
# ---------------------------------------------------------------------------


def allow(context: str = "") -> None:
    """Emit allow envelope to stdout and ``exit 0``."""
    sys.stdout.write(allow_envelope(context) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def deny(reason: str, summary: str | None = None) -> None:
    """Emit deny envelope to stdout and ``exit 0`` (never exit 2).

    ``summary`` rides in ``systemMessage`` (the transcript banner);
    ``reason`` rides in ``permissionDecisionReason`` (the model-facing
    feedback). See :func:`deny_envelope`.
    """
    sys.stdout.write(deny_envelope(reason, summary) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def warn(message: str) -> None:
    """Emit warn envelope (allow + additionalContext) and ``exit 0``."""
    sys.stdout.write(warn_envelope(message) + "\n")
    sys.stdout.flush()
    sys.exit(0)


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def parse_stdin(raw: str) -> dict[str, Any]:
    """Parse the Claude Code hook payload; return ``{}`` on any failure."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def get_bash_command(payload: dict[str, Any]) -> str:
    """Extract the Bash ``command`` field; ``""`` if not a Bash call."""
    if payload.get("tool_name") != "Bash":
        return ""
    tool_input = payload.get("tool_input") or {}
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else ""
    return cmd if isinstance(cmd, str) else ""


# ---------------------------------------------------------------------------
# Escape token
# ---------------------------------------------------------------------------

_ESCAPE_RE = re.compile(
    r"#\s*routing-allow\s*:\s*(?P<reason>.+?)\s*$",
    re.MULTILINE,
)


def extract_escape_reason(command: str) -> str:
    """Return the ``# routing-allow:`` reason text, or ``""`` when absent.

    nexus-mzvwa.9: the reason trails the command, so the 200-char
    ``command_fragment`` cap in :func:`log_routing_event` routinely cut
    it — making escape reasons un-auditable from the log. Callers pass
    this as the dedicated ``escape_reason`` field instead.
    """
    if not command or ESCAPE_TOKEN not in command:
        return ""
    match = _ESCAPE_RE.search(command)
    return match.group("reason").strip() if match else ""


def should_skip_for_reason(command: str) -> bool:
    """Return True iff ``command`` carries a valid ``# routing-allow:`` escape.

    Valid means: token present and the trailing reason text is at least
    ``ESCAPE_REASON_MIN_LENGTH`` characters after stripping whitespace.
    """
    if not command or ESCAPE_TOKEN not in command:
        return False
    match = _ESCAPE_RE.search(command)
    if not match:
        return False
    reason = match.group("reason").strip()
    return len(reason) >= ESCAPE_REASON_MIN_LENGTH


def degraded_token_variants(segment: str) -> list[list[str]]:
    """Rough tokenizations of a segment ``shlex`` rejected for unbalanced
    quoting (nexus-2e874). The old ``except ValueError: continue`` silently
    DROPPED the whole segment, so a single stray quote anywhere in a gated
    command fully bypassed the guard (``git push origin main
    --receive-pack="x`` was ALLOWed with zero warning).

    Two variants, because neither alone keeps every anchor visible
    (review Important-1): quote-chars-as-whitespace keeps a quote glued to
    a token BOUNDARY splitting (``--receive-pack="x`` -> ``--receive-pack=``,
    ``x``), while quote-chars-removed keeps a quote INSIDE a verb from
    fracturing it (``gi"t push`` -> ``git``, ``push``). Callers must treat a
    match in EITHER variant as a match — the safe, over-inclusive
    direction; only quoting fidelity inside VALUES is lost.
    """
    blanked = segment.replace('"', " ").replace("'", " ").split()
    stripped = segment.replace('"', "").replace("'", "").split()
    return [blanked] if blanked == stripped else [blanked, stripped]


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def _log_path() -> pathlib.Path:
    override = os.environ.get("NX_ROUTING_LOG_PATH")
    return pathlib.Path(override) if override else _DEFAULT_LOG_PATH


#: Byte cap that triggers rotation (~1 MiB) -- same design and constant
#: as ``nexus._session_end_census``'s capability-census log; see that
#: module's docstring for the full "rotation, not trim-in-place" rationale
#: (a read-modify-write races the many concurrent routing-hook processes
#: that append to this exact file, one per gated tool call, across
#: however many Claude Code sessions are live at once -- worse than the
#: once-per-SessionEnd census log this pattern was first written for). A
#: byte ``stat()`` is O(1); a line count would require reading the whole
#: file on every single hook invocation.
_ROUTING_LOG_ROTATION_MAX_BYTES = 1_048_576

if sys.platform == "win32":
    import msvcrt as _msvcrt
else:
    import fcntl as _fcntl


def _lock_file(file_obj: Any, *, blocking: bool) -> None:
    """Exclusive lock on an already-open regular file. Cross-platform:
    ``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows. Deliberately
    self-contained (duplicates ``nexus._locking.lock_file``'s logic
    rather than importing it) -- this script has no dependency on the
    ``nexus`` package being installed/importable at all, by design (no
    other routing script under ``conexus/hooks/scripts/`` imports it
    either). Raises ``BlockingIOError`` if ``blocking=False`` and the
    lock is contended.
    """
    fd = file_obj.fileno()
    if sys.platform == "win32":
        if blocking:
            while True:
                try:
                    _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)
                    return
                except OSError:
                    time.sleep(0.5)
        else:
            try:
                _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise BlockingIOError(str(e)) from e
    else:
        flag = _fcntl.LOCK_EX if blocking else _fcntl.LOCK_EX | _fcntl.LOCK_NB
        _fcntl.flock(fd, flag)


def _unlock_file(file_obj: Any) -> None:
    """Release a lock acquired by :func:`_lock_file`."""
    fd = file_obj.fileno()
    if sys.platform == "win32":
        try:
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        _fcntl.flock(fd, _fcntl.LOCK_UN)


def _rotate_log_if_oversized(log_path: pathlib.Path) -> None:
    """Rotate ``log_path`` to ``<name>.1`` via atomic rename if it has
    grown past :data:`_ROUTING_LOG_ROTATION_MAX_BYTES`.

    ROTATION, NOT TRIM-IN-PLACE: rewriting the file in place to keep only
    the newest N lines is a foot-cannon here specifically -- MANY routing
    hook processes append to this file concurrently (one per gated
    Bash/Agent/git command), so a read-modify-write can clobber another
    process's line-atomic append mid-flight, and a crash partway through
    the rewrite loses the file outright. ``os.replace`` is atomic on
    POSIX: at every instant the path either names the pre-rotation file
    or nothing, never a half-written intermediate. Do NOT "simplify"
    this back into a rewrite.

    Exactly one older generation is retained -- any existing ``.1`` is
    CLOBBERED, never pushed to ``.2``, bounding total on-disk size at
    roughly 2x the cap ONCE rotation has run at least once. The FIRST
    rotation is an exception to that bound: a pre-existing file already
    over the cap when this code first ships (this project's own real
    ``routing_log.jsonl``, observed at ~5MB against a 1 MiB cap) lands in
    ``.1`` WHOLE, not truncated to the cap -- and persists at that size
    until the NEXT rotation clobbers it. Steady-state is bounded; the
    one-time first-rotation transient is not.

    TOCTOU DOUBLE-ROTATION CLOBBER (code-review Critical, nexus-g3jw6,
    fix pass 2026-08-20) -- why this function takes a lock at all: two
    concurrent rotators, P1 and P2, can BOTH observe the file oversize
    (P2's observation stale by the time it acts). P1 rotates the real
    history into ``.1`` and reappends a small live file. If P2 then
    blindly replays its stale decision, its rename SUCCEEDS AGAIN (the
    live path exists again) and CLOBBERS P1's real ``.1`` with P1's
    small reappended content -- silent, irreversible history loss, not
    the benign "someone else already rotated it" FileNotFoundError case
    below. FIX: a non-blocking advisory lock on a sidecar
    ``<name>.rotate.lock`` serializes the {re-stat, os.replace} critical
    section across rotators. Appends stay completely lock-free
    (unchanged, single ``fh.write()`` in ``log_routing_event``) -- only
    the RARE rotation path pays any lock cost, and only once oversize
    was observed at all. Losing the lock race (``BlockingIOError``)
    means someone else is rotating right now -- skip entirely, do not
    block or retry. Winning the lock means re-stat under it: if the file
    is no longer oversize, skip -- the earlier, unlocked stat() that
    triggered this call may be stale, but the DECISION to actually
    rename is always made with fresh data. This eliminates the
    stale-observation rename by construction, not merely narrows its
    window -- a bare re-stat immediately before ``os.replace`` WITHOUT
    the lock would still let two rotators interleave their re-stat and
    replace calls.

    A concurrent rotation race that manifests as ``FileNotFoundError``
    on the rename itself (another process's rotation completed between
    OUR re-stat-under-the-lock and OUR own replace -- possible only if
    that other process is not participating in this same lock) is still
    tolerated silently: the file is rotated either way. Any OTHER
    failure propagates to the caller, which is responsible for keeping
    rotation failure from blocking the append (see ``log_routing_event``).
    """
    try:
        size = log_path.stat().st_size
    except FileNotFoundError:
        return  # nothing to rotate

    if size < _ROUTING_LOG_ROTATION_MAX_BYTES:
        return  # cheap, lock-free common case: not even apparently oversize

    # Apparently oversize (per a possibly-stale stat()) -- escalate to the
    # serialized, re-checked critical section.
    lock_path = log_path.with_name(log_path.name + ".rotate.lock")
    try:
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return  # can't even open the lockfile -- best-effort, skip rotation
    lock_file_obj = os.fdopen(lock_fd, "r+")
    try:
        try:
            _lock_file(lock_file_obj, blocking=False)
        except BlockingIOError:
            # Someone else is inside the rotation critical section right
            # now -- skip entirely rather than wait or race them.
            return

        # RE-CHECK under the lock: the stat() above may be stale.
        try:
            size = log_path.stat().st_size
        except FileNotFoundError:
            return  # nothing left to rotate
        if size < _ROUTING_LOG_ROTATION_MAX_BYTES:
            return  # a fresher rotator already handled it

        rotated = log_path.with_name(log_path.name + ".1")
        try:
            os.replace(log_path, rotated)
        except FileNotFoundError:
            # Another (non-participating) process removed/rotated the
            # file between our re-stat and our rename -- rotated either
            # way.
            pass
    finally:
        try:
            _unlock_file(lock_file_obj)
        except OSError:
            pass
        lock_file_obj.close()


def log_routing_event(
    rule: str,
    outcome: str,
    *,
    tool_name: str = "",
    command_fragment: str = "",
    escape_reason: str = "",
) -> None:
    """Append one JSON line to the routing log. Never raises."""
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _rotate_log_if_oversized(path)
        except Exception:
            # Rotation is best-effort; the append below must still happen
            # even if rotation itself hit an unexpected error (e.g. a
            # permission error on the rename).
            pass
        record = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "rule": rule,
            "outcome": outcome,
        }
        if tool_name:
            record["tool_name"] = tool_name
        if command_fragment:
            # Cap fragment length so the log stays small.
            record["command_fragment"] = command_fragment[:200]
        if escape_reason:
            # Dedicated field (nexus-mzvwa.9): the reason trails the command,
            # so the fragment cap above routinely truncated it away.
            record["escape_reason"] = escape_reason[:300]
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        # Telemetry must never crash a hook. Swallow.
        pass


# ---------------------------------------------------------------------------
# Top-level runner — wraps every hook entry point
# ---------------------------------------------------------------------------


def run_hook(
    body: Callable[[dict[str, Any]], None],
    *,
    fail_closed: bool = False,
    rule_name: str = "",
) -> None:
    """Execute ``body(payload)`` under the fail-open / fail-closed contract.

    ``body`` is responsible for calling ``allow()`` / ``deny()`` /
    ``warn()`` itself; those calls ``sys.exit(0)``. If ``body`` returns
    normally without emitting an envelope, we fall through to a default
    allow. If ``body`` raises ``SystemExit`` (from our own emitters), we
    re-raise — that is the normal path. Any other exception triggers
    the fail-open / fail-closed branch.
    """
    raw = ""
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""

    payload = parse_stdin(raw)

    try:
        body(payload)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        if fail_closed:
            log_routing_event(
                rule=rule_name or "unknown",
                outcome="deny_fail_closed",
                tool_name=payload.get("tool_name", "") or "",
            )
            deny(f"cannot verify, fail-closed: {exc}")
        else:
            log_routing_event(
                rule=rule_name or "unknown",
                outcome="allow_fail_open",
                tool_name=payload.get("tool_name", "") or "",
            )
            allow()

    # Body returned without emitting — default allow.
    allow()
