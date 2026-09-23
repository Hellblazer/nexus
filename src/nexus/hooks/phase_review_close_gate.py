# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``phase-review-close-gate`` hook verb (nexus-t9klx).

Port of ``conexus/hooks/scripts/routing/phase_review_close_requires_gate.py``,
the routing framework's one ``fail_closed`` rule. It stays on the COMMAND
tier, which is where a rule that must still deny when it crashes belongs:
``nexus.mcp.hooks._NEVER_TOOL_TIER`` refuses to register this name as an
``mcp_tool`` at all, because a tool-boundary crash renders as
``isError=False`` — an allow — and that is precisely backwards here. An
``nx-hook`` verb is the command tier, so the port does not move it.

**"Move, do not rewrite."** The checks are carried across unchanged: the
same trigger regexes with their GH #931 narrowing, the same three sentinel
conditions, the same escape-token audit, the same redirect message. Two
changes were unavoidable:

1. **The interpreter preamble is gone**, as in every verb: the wheel runs
   under conexus's own interpreter.

2. **The emitters return instead of exiting.** ``_lib.allow()`` /
   ``_lib.deny()`` printed an envelope and ``sys.exit(0)``; this body
   returns ``_lib.allow_result()`` / ``_lib.deny_result()`` and
   ``run_hook_result`` hands it back. That matters more here than
   elsewhere: a ``sys.exit`` inside a verb reaches ``never_fail``'s
   SystemExit passthrough and ends the hook process mid-dispatch, which
   for a fail-closed rule is the one shape it must never take. The
   fail-closed branch itself is unchanged and now returns a deny envelope
   on ANY exception, which ``run_hook_result``'s own tests exercise.

The original module docstring follows, unedited:

RDR-121 Phase 2 hook 2: phase-review close requires a PASSED gate.

Denies ``bd close <bead-id>`` for phase-review beads (title contains
``phase`` or ``review``) unless a fresh PASSED sentinel exists for the
bead's ``(rdr-id, phase)`` tuple. Fail-closed: an exception while
verifying the gate denies the close instead of allowing it. This is
the safety-critical instance of the framework's fail-closed opt-in;
see ``_lib.run_hook(fail_closed=True)``.

Sentinel path: ``${TMPDIR:-/tmp}/nx-phase-gate-sentinel/<claude_pid>-<rdr-id>-<phase>.json``

Three checks (all must hold for ``allow``):
  (a) sentinel file exists
  (b) sentinel mtime is newer than the session-start time (mtime of
      ``~/.config/nexus/current_session``; RDR-149 P4 retired the
      ``t1_addr.<claude_pid>`` anchor)
  (c) sentinel content reports ``outcome: PASSED``

Escape token ``# routing-allow: <reason>=8 chars>`` allows the close to
proceed, audited in the routing log.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
from typing import Any

from nexus._hook_runtime._io import HookResult, configure_hook_logging
from nexus.hooks import _routing_lib as _lib

RULE_NAME = "phase_review_close_requires_gate"

_BD_CLOSE_RE = re.compile(
    r"\bbd\s+(?:close|done)\s+(?P<bead_id>[A-Za-z0-9._-]+)",
)
_RDR_RE = re.compile(r"\brdr[-_ ]?(?P<id>\d+)\b", re.IGNORECASE)
_PHASE_RE = re.compile(r"\bphase[\s-]?(?P<phase>\d+)\b", re.IGNORECASE)
_P_LEAF_RE = re.compile(r"\bP(?P<phase>\d+)(?:\.\d+)*\b")
# Trigger must match the bead TITLE line only (not the full description).
# Implementation beads in phased plans routinely mention "phase" or "review"
# in their description, parent epic, or rationale text. To distinguish a
# phase-review-gate bead from an implementation bead in the same phase, the
# trigger requires either:
#   1. The phrase "Phase N review gate" (case-insensitive, with optional
#      sub-phase letter or decimal, e.g. "Phase 3b" / "Phase 1.5"), OR
#   2. The literal slash-command name "phase-review-gate" preceded by a
#      phase prefix like "P3b" or "Phase 0".
# A bare mention of "phase-review-gate" anywhere (e.g. in a meta-task title
# "phase-review-gate skill: recognize ...") is intentionally NOT matched —
# the bead must be an actual phase-N gate execution to trigger the sentinel
# check. See GH issue #931 / bead nexus-1pr9n for the regression that
# motivated the tighter trigger.
_GATE_TITLE_RE = re.compile(
    r"\b(?:phase|p)[\s-]?\d+[\w.]*\s+(?:phase[\s-]?)?review[\s-]?gate\b",
    re.IGNORECASE,
)


def _bd_header_line(bd_output: str) -> str:
    """Return the first non-empty line from ``bd show`` output (the header
    line carrying the bead title), or empty string if none."""
    for line in bd_output.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _claude_pid() -> int:
    """Resolve the active Claude PID; tests override via NX_FAKE_CLAUDE_PID."""
    fake = os.environ.get("NX_FAKE_CLAUDE_PID")
    if fake:
        try:
            return int(fake)
        except ValueError:
            pass
    try:
        # nexus-cnzei.2 (S2, fix round 2): bridge structlog to stderr/logfile
        # BEFORE importing nexus.session below, via the SHARED helper (was a
        # hand-duplicated local try/except; see configure_hook_logging's
        # docstring in nexus._hook_runtime._io for the class-level defect
        # this closes). Structlog's default
        # PrintLoggerFactory writes to STDOUT, the same channel this
        # PreToolUse hook's own JSON envelope goes out on, and a debug line
        # landing there ahead of (or beside) that JSON would corrupt the
        # payload the harness parses.
        #
        # configure_hook_logging() carries its OWN internal
        # best-effort catch (never raises); the outer `except Exception:  # noqa: BLE001 — carried: this hook must reach a verdict, never raise
        # return os.getppid()` below is a SEPARATE, independent guarantee --
        # it also covers a failure of the logging setup itself and the
        # `nexus.session` import/call that follows. See
        # test_claude_pid_survives_a_logging_setup_failure, which asserts
        # the outer catch specifically by bypassing the inner one.
        configure_hook_logging()

        from nexus.session import find_immediate_claude_pid  # noqa: PLC0415 — carried: deferred so the PID path costs nothing when the hook never reaches it

        return find_immediate_claude_pid()
    except Exception:  # noqa: BLE001 — carried: this hook must reach a verdict, never raise
        return os.getppid()


def _session_start_time(claude_pid: int) -> float | None:
    """Return the session-start anchor time, or None if unavailable.

    RDR-149 P4 retired the ``t1_addr.<claude_pid>`` addr file (T1 now keys
    its leased registry record on the session-id, not the claude_pid), so
    this anchors on the ``current_session`` pointer instead: the
    SessionStart hook (re)writes it once per session, so its mtime is the
    session-start proxy. A sentinel older than this is a stale carry-over
    from a previous session. ``claude_pid`` is accepted for caller
    signature stability but no longer selects the file.
    """
    base = os.environ.get("NEXUS_CONFIG_DIR")
    if base:
        marker = pathlib.Path(base) / "current_session"
    else:
        marker = pathlib.Path.home() / ".config" / "nexus" / "current_session"
    if not marker.exists():
        return None
    try:
        return marker.stat().st_mtime
    except OSError:
        return None


def _bd_show(bead_id: str) -> str:
    """Return raw output of ``bd show <bead_id>``; empty string on failure."""
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: a hook process pays its import cost on every invocation, and a module-scope import of this pulls structlog + ~231 modules (measured on verification_config: 14ms/106 -> 62-84ms/337). Deferred, it is paid only when we actually spawn

    if not shutil.which("bd"):
        return ""
    try:
        proc = run_bounded(
            ["bd", "show", bead_id],
            timeout=5,
        )
        return proc.stdout or ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _extract_rdr_phase(text: str) -> tuple[str | None, str | None]:
    """Best-effort extraction of (rdr_id, phase) from bead title + description."""
    rdr_m = _RDR_RE.search(text)
    phase_m = _PHASE_RE.search(text) or _P_LEAF_RE.search(text)
    rdr_id = rdr_m.group("id") if rdr_m else None
    phase = phase_m.group("phase") if phase_m else None
    return rdr_id, phase


def _sentinel_path(claude_pid: int, rdr_id: str, phase: str) -> pathlib.Path:
    base = os.environ.get("TMPDIR", "/tmp").rstrip("/")
    return pathlib.Path(base) / "nx-phase-gate-sentinel" / f"{claude_pid}-{rdr_id}-{phase}.json"


def _check_sentinel(
    claude_pid: int, rdr_id: str, phase: str
) -> tuple[bool, str]:
    """Return (ok, reason). reason explains why the gate denied."""
    path = _sentinel_path(claude_pid, rdr_id, phase)
    if not path.exists():
        return False, "sentinel absent"
    try:
        stat = path.stat()
    except OSError as exc:
        return False, f"sentinel unreadable: {exc}"
    session_start = _session_start_time(claude_pid)
    if session_start is not None and stat.st_mtime < session_start:
        return False, "sentinel stale (predates current session)"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"sentinel content corrupt: {exc}"
    outcome = payload.get("outcome") if isinstance(payload, dict) else None
    if outcome != "PASSED":
        return False, f"sentinel outcome != PASSED (got {outcome!r})"
    return True, ""


def _redirect_message(rdr_id: str | None, phase: str | None, reason: str) -> str:
    rdr_part = rdr_id if rdr_id else "<rdr-id>"
    phase_part = phase if phase else "N"
    return (
        f"Phase-review close blocked: {reason}. Run the gate first:\n"
        f"  /conexus:phase-review-gate {rdr_part} --phase {phase_part}\n"
        f"Then re-run `bd close ...`. An escape (`# routing-allow: <reason>`) "
        f"exists for this guard, but only on the user's explicit "
        f"instruction to use it -- it is not yours to reach for."
    )


def body(payload: dict[str, Any]) -> HookResult | None:
    command = _lib.get_bash_command(payload)
    if not command:
        return _lib.allow_result()

    match = _BD_CLOSE_RE.search(command)
    if not match:
        return _lib.allow_result()

    # Escape token takes precedence; audit and pass through.
    if _lib.should_skip_for_reason(command):
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="escape", tool_name="Bash",
            command_fragment=command,
            escape_reason=_lib.extract_escape_reason(command),
        )
        return _lib.allow_result()

    bead_id = match.group("bead_id")
    bd_output = _bd_show(bead_id)
    if not bd_output:
        # Cannot determine if this is a phase-review bead. Allow rather
        # than fail-closed; we have no signal to deny on.
        return _lib.allow_result()

    # Trigger: match against the bead's TITLE line only (the first non-empty
    # line of bd show output), and only for the narrow "Phase N ... review
    # gate" or "Phase N ... phase-review-gate" patterns. Implementation beads
    # in phased plans whose description / parent / rationale mentions "phase"
    # or "review" no longer false-positive (GH #931 / nexus-1pr9n).
    title_line = _bd_header_line(bd_output)
    if not _GATE_TITLE_RE.search(title_line):
        return _lib.allow_result()

    rdr_id, phase = _extract_rdr_phase(bd_output)
    if not rdr_id or not phase:
        # We know it is a phase-review bead but cannot resolve the
        # (rdr-id, phase) tuple. Fail-closed: deny rather than guess.
        reason = "cannot resolve (rdr-id, phase) from bead title or description"
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="deny", tool_name="Bash",
            command_fragment=command,
        )
        return _lib.deny_result(
            _redirect_message(rdr_id, phase, reason),
            summary=f"Phase-review close blocked ({rdr_id} phase {phase}): run the gate first.",
        )

    claude_pid = _claude_pid()
    ok, reason = _check_sentinel(claude_pid, rdr_id, phase)
    if not ok:
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="deny", tool_name="Bash",
            command_fragment=command,
        )
        return _lib.deny_result(
            _redirect_message(rdr_id, phase, reason),
            summary=f"Phase-review close blocked ({rdr_id} phase {phase}): run the gate first.",
        )

    _lib.log_routing_event(
        rule=RULE_NAME, outcome="allow", tool_name="Bash",
        command_fragment=command,
    )
    return _lib.allow_result(
        f"phase-review close approved by sentinel (RDR-{rdr_id} phase {phase})"
    )

def run(payload: dict | None) -> HookResult:
    """Decide whether this ``bd close`` may proceed. Fail-closed."""
    return _lib.run_hook_result(
        body, payload, fail_closed=True, rule_name=RULE_NAME
    )
