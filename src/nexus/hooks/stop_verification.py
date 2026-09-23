# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The Stop verification hook (RDR-215 bead nexus-q02nx.13).

Port of ``conexus/hooks/scripts/stop_verification_hook.sh`` (contract map
row 16). Advisory only: it warns about uncommitted changes, open beads and
an RDR-184 ledger that still lists background agents the harness no longer
tracks. **It can never emit deny or block** -- "warns only" is the script's
own stated contract, hard enforcement belongs to the PreToolUse close gate,
and :func:`run` has exactly one stdout shape, ``decision: approve``, with
or without a reason.

**THE CATALOG-SYNC LEG IS DELETED, not moved to a thread** (bead
nexus-q02nx.13 planned the move; the bead .16 critique found the premise
false). The bash called ``nx catalog sync`` at session close with its
result discarded by ``|| true``. That command has raised
``click.ClickException`` unconditionally since conexus 7.0.0 --
``commands/catalog.py``'s ``sync_cmd``, "retired: the nexus service's
Postgres is the sole catalog authority" -- so it was already dead when
the bash was written, and the git-backed ``~/.config/nexus/catalog`` it
looked for was itself retired at RDR-158 P4.

Moving a call that cannot succeed onto a daemon thread would have bought
nothing and cost something: the port briefly logged a warning on every
fire where the bash discarded silently, which is new permanent noise for
a permanently-failing command. The whole "a killed thread costs a
deferred sync, never a corrupt one, because the work is idempotent"
analysis in the first draft of this file was reasoning carefully about a
scenario that cannot occur -- inherited unexamined from a bash comment
("auto-commit + push if remote configured") that was stale before it was
written.

DEVIATION, STATED: bead .13's own wording was "moving nx catalog sync off
the synchronous path". Deleting it is a different outcome and this is the
notice. The edge it changes: a box still running a pre-7.0.0 conexus
generation, WITH a git-backed catalog relic, would have had that relic
auto-committed at session close and now will not. Postgres has been the
catalog authority since RDR-158 P4, so nothing reads that relic; it is a
file that stopped being written, not data that stops being saved.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from nexus._hook_runtime._config import stop_guard_mode
from nexus._hook_runtime._io import HookResult, _emit
from nexus.hooks.verification_config import read_verification_config
from nexus.hooks import expectations as _exp

__all__ = ["run"]

#: The reconcile exit code that means "the ledger lists background agents
#: the harness no longer tracks". Every other code, and every failure path,
#: leaves the warning empty and never touches the decision.
_RECONCILE_STRANDED = 4

_UNCOMMITTED_WARNING = (
    "WARNING: Uncommitted changes detected — consider committing before "
    "ending session\n"
)
_BEADS_WARNING = (
    "WARNING: Beads still in progress — consider closing or deferring "
    "before ending session\n"
)


def _approve(reason: str = "") -> HookResult:
    """The only envelope this hook can produce.

    Key order and spacing match the script's ``printf`` byte for byte, and
    a reason is rendered through ``json.dumps`` exactly as the script's own
    escaping helper did.
    """
    if reason:
        return HookResult(
            stdout=json.dumps({"decision": "approve", "reason": reason})
        )
    return HookResult(stdout=json.dumps({"decision": "approve"}))


def _read_config() -> dict:
    """The verification block, read in-process (bead nexus-b5ugt).

    **The spawn this used to do is gone, and its own docstring called
    the shot.** It said: "Bead .17 moves pre_close_verification_hook.sh,
    the other consumer, onto this tier; once both are here the reader can
    become an imported function and this spawn goes with it." Both are
    here. It went.

    What forced the timing rather than leaving it as tidying: the script
    was located off ``$CLAUDE_PLUGIN_ROOT``, and an ``mcp_tool`` hook
    runs inside ``nx-mcp``, which does not get a usable one.
    ``conexus/.mcp.json`` declares the server env as
    ``{"CLAUDE_PLUGIN_ROOT": "${CLAUDE_PLUGIN_ROOT}"}`` and Claude Code
    does not expand ``${...}`` in an MCP ``env`` block, so the literal
    placeholder arrived, the path never resolved, and this returned
    ``{}`` — on_stop false, the session-end gate silently verifying
    nothing. Measured 2026-09-20.

    The alternative fix, making the hook locate the script more reliably,
    is the wrong direction: RDR-215 exists to eliminate the
    plugin-resident layer so a native Windows client becomes viable, and
    a repair that keeps the subprocess keeps the thing being eliminated.

    ``runpy`` was rejected earlier for rebinding a process-global
    ``sys.stdout`` inside a concurrently-serving MCP server. That
    objection dies with the subprocess: an imported function returns a
    value and touches no global stream.

    Still returns ``{}`` on any failure, matching the script's
    ``|| echo '{}'`` posture — a config reader that raised would take
    down the hook that called it.
    """
    try:
        return read_verification_config()
    except Exception:  # noqa: BLE001 — a hook must never fail; see the module docstring
        return {}


def _reconcile_warning(payload: dict) -> str:
    """The RDR-184 stranded-agent warning, or "".

    WARN-ONLY unconditionally, and gated on the same guard as the rest of
    the ledger machinery so a session that opted the whole guard off does
    not pay for this either. It runs independent of the ``on_stop``
    verification toggle: it is a distinct RDR-184 concern, not part of that
    feature.
    """
    if stop_guard_mode() not in ("observe", "block"):
        return ""
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return ""
    try:
        report = _exp.expectations_reconcile(session_id, json.dumps(payload))
    except Exception:  # noqa: BLE001 — every reconcile failure leaves the warning empty
        return ""
    if report.code != _RECONCILE_STRANDED:
        return ""
    joined = " | ".join(report.lines)
    return (
        "WARNING: expectations ledger reconciliation found background "
        "agent(s) the ledger still lists as outstanding but the harness no "
        "longer tracks (nexus-2v0v7) -- possible silent death, verify: "
        f"{joined}\n"
    )


def _git_is_dirty(path: str | None = None) -> bool:
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: hooks fire on every tool call and a module-scope import of this pulls structlog + ~231 modules (measured 14ms -> 62ms); paid only when we actually spawn

    if shutil.which("git") is None:
        return False
    args = ["git"] + (["-C", path] if path else []) + ["status", "--porcelain"]
    try:
        proc = run_bounded(args, timeout=30)
    except Exception:  # noqa: BLE001 — an unavailable git is not a warning
        return False
    return bool(proc.stdout.strip())


def _beads_in_progress() -> bool:
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: hooks fire on every tool call and a module-scope import of this pulls structlog + ~231 modules (measured 14ms -> 62ms); paid only when we actually spawn

    if shutil.which("bd") is None:
        return False
    try:
        proc = run_bounded(
            ["bd", "list", "--status=in_progress"],
            timeout=30,
        )
    except Exception:  # noqa: BLE001 — an unavailable bd is not a warning
        return False
    return "in_progress" in proc.stdout


def run(payload: dict | None) -> HookResult:
    """Approve the stop, with any advisory warnings attached."""
    data = payload if isinstance(payload, dict) else {}
    reconcile = _reconcile_warning(data)

    config = _read_config()
    if config.get("on_stop") is not True:
        return _approve(reconcile)

    warnings = reconcile
    if _git_is_dirty():
        warnings += _UNCOMMITTED_WARNING

    if _beads_in_progress():
        warnings += _BEADS_WARNING

    return _approve(warnings)
