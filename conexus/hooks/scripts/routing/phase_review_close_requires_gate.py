#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-121 Phase 2 hook 2: phase-review close requires a PASSED gate.

Denies ``bd close <bead-id>`` for phase-review beads (title contains
``phase`` or ``review``) unless a fresh PASSED sentinel exists for the
bead's ``(rdr-id, phase)`` tuple. Fail-closed: an exception while
verifying the gate denies the close instead of allowing it. This is
the safety-critical instance of the framework's fail-closed opt-in;
see ``_lib.run_hook(fail_closed=True)``.

Sentinel path: ``${TMPDIR:-/tmp}/nx-phase-gate-sentinel/<claude_pid>-<rdr-id>-<phase>.json``

Three checks (all must hold for ``allow``):
  (a) sentinel file exists
  (b) sentinel mtime is newer than the session-start time (ctime of
      ``~/.config/nexus/t1_addr.<claude_pid>``)
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
import sys
from typing import Any

# Hook framework lives next to this script.
sys.path.insert(0, os.path.dirname(__file__))
import _lib  # noqa: E402

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
        from nexus.session import find_immediate_claude_pid

        return find_immediate_claude_pid()
    except Exception:
        return os.getppid()


def _session_start_time(claude_pid: int) -> float | None:
    """Return ctime of the t1_addr.<pid> file, or None if unavailable."""
    base = os.environ.get("NEXUS_CONFIG_DIR")
    if base:
        addr = pathlib.Path(base) / f"t1_addr.{claude_pid}"
    else:
        addr = pathlib.Path.home() / ".config" / "nexus" / f"t1_addr.{claude_pid}"
    if not addr.exists():
        return None
    try:
        return addr.stat().st_ctime
    except OSError:
        return None


def _bd_show(bead_id: str) -> str:
    """Return raw output of ``bd show <bead_id>``; empty string on failure."""
    if not shutil.which("bd"):
        return ""
    try:
        proc = subprocess.run(
            ["bd", "show", bead_id],
            capture_output=True, text=True, timeout=5,
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
        f"Then re-run `bd close ...`. To override, append "
        f"`# routing-allow: <reason>` (>=8 chars) to the command."
    )


def body(payload: dict[str, Any]) -> None:
    command = _lib.get_bash_command(payload)
    if not command:
        _lib.allow()

    match = _BD_CLOSE_RE.search(command)
    if not match:
        _lib.allow()

    # Escape token takes precedence; audit and pass through.
    if _lib.should_skip_for_reason(command):
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="escape", tool_name="Bash",
            command_fragment=command,
        )
        _lib.allow()

    bead_id = match.group("bead_id")
    bd_output = _bd_show(bead_id)
    if not bd_output:
        # Cannot determine if this is a phase-review bead. Allow rather
        # than fail-closed; we have no signal to deny on.
        _lib.allow()

    # Trigger: match against the bead's TITLE line only (the first non-empty
    # line of bd show output), and only for the narrow "Phase N ... review
    # gate" or "Phase N ... phase-review-gate" patterns. Implementation beads
    # in phased plans whose description / parent / rationale mentions "phase"
    # or "review" no longer false-positive (GH #931 / nexus-1pr9n).
    title_line = _bd_header_line(bd_output)
    if not _GATE_TITLE_RE.search(title_line):
        _lib.allow()

    rdr_id, phase = _extract_rdr_phase(bd_output)
    if not rdr_id or not phase:
        # We know it is a phase-review bead but cannot resolve the
        # (rdr-id, phase) tuple. Fail-closed: deny rather than guess.
        reason = "cannot resolve (rdr-id, phase) from bead title or description"
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="deny", tool_name="Bash",
            command_fragment=command,
        )
        _lib.deny(_redirect_message(rdr_id, phase, reason))

    claude_pid = _claude_pid()
    ok, reason = _check_sentinel(claude_pid, rdr_id, phase)
    if not ok:
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="deny", tool_name="Bash",
            command_fragment=command,
        )
        _lib.deny(_redirect_message(rdr_id, phase, reason))

    _lib.log_routing_event(
        rule=RULE_NAME, outcome="allow", tool_name="Bash",
        command_fragment=command,
    )
    _lib.allow(
        f"phase-review close approved by sentinel (RDR-{rdr_id} phase {phase})"
    )


if __name__ == "__main__":
    _lib.run_hook(body, fail_closed=True, rule_name=RULE_NAME)
