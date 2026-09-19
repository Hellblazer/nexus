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

**The one deliberate behaviour change: ``nx catalog sync`` moves off the
synchronous path** (RDR Technical Design, Failure Modes). Today it is a git
commit and push run inline at session close, with its result discarded by
``|| true``. Inline, it holds the hook -- and on the tool tier it would
hold the MCP server -- for the length of a network round trip. Here it runs
on a daemon thread.

Two things about that change, stated rather than glossed:

*It cannot affect the decision, and it never could.* The synchronous call's
result was discarded, so success and failure produced byte-identical
stdout. The approve envelope is now emitted before the sync finishes, which
changes when the envelope is written but not what it says. There is no
outcome the old code could express that the new code cannot.

*It gains a logged outcome and a truncation window.* The RDR names the risk
as "a thread that fails silently where the synchronous call would have
logged" -- but the synchronous call redirected both streams to
``/dev/null`` and logged nothing at all, so the port strictly ADDS
visibility rather than risking its loss. The real new exposure is the other
one: a daemon thread does not keep the interpreter alive, so a server
exiting soon after Stop can kill a sync mid-push. That costs a DEFERRED
sync, never a corrupt one -- the work is gated on the catalog being dirty,
so the next session's Stop finds the same dirty state and syncs again.
Idempotent by construction, which is what makes the daemon thread the right
choice rather than a merely convenient one.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from nexus._hook_runtime._config import stop_guard_mode
from nexus._hook_runtime._io import HookResult, _emit
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


def _plugin_root() -> Path:
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        return Path(root)
    return Path(__file__).resolve().parents[3] / "conexus"


def _read_config() -> dict:
    """Run the standalone config reader and parse its JSON.

    **This is the one subprocess the port keeps, and it is a considered
    choice.** The script is deliberately nexus-import-free so a bare
    ``python3`` hook can run it, it has no importable entry point, and
    ``tests/hooks/test_stop_verification_hook.py`` SUBSTITUTES a fake one
    under a temporary ``CLAUDE_PLUGIN_ROOT`` -- so the path, not the
    module, is the contract.

    ``runpy`` would remove the spawn but needs ``redirect_stdout``, and
    that rebinds a process-global ``sys.stdout`` inside an MCP server that
    is concurrently serving other tools; a neighbouring call's output could
    land in this buffer or this one's in theirs. That is the same
    process-global-state-under-concurrency class as the ledger's contention
    seams, and one spawn is the cheaper mistake to not make.

    Bead ``.17`` moves ``pre_close_verification_hook.sh``, the other
    consumer, onto this tier; once both are here the reader can become an
    imported function and this spawn goes with it.

    Every failure returns ``{}``, matching the script's ``|| echo '{}'``.
    """
    script = _plugin_root() / "hooks" / "scripts" / "read_verification_config.py"
    try:
        proc = subprocess.run(
            ["python3", str(script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        parsed = json.loads(proc.stdout)
    except Exception:  # noqa: BLE001 — a hook must never fail; see the module docstring
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
    if shutil.which("git") is None:
        return False
    args = ["git"] + (["-C", path] if path else []) + ["status", "--porcelain"]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001 — an unavailable git is not a warning
        return False
    return bool(proc.stdout.strip())


def _catalog_sync_target() -> str | None:
    """The catalog path to sync, or None when there is nothing to do.

    Same three conditions the script checks, in the same order: ``nx`` on
    PATH, a git-backed catalog with a ``documents.jsonl``, and at least one
    dirty ``.jsonl`` in it.
    """
    if shutil.which("nx") is None:
        return None
    catalog = os.environ.get("NEXUS_CATALOG_PATH") or str(
        Path.home() / ".config" / "nexus" / "catalog"
    )
    root = Path(catalog)
    if not (root / ".git").is_dir() or not (root / "documents.jsonl").is_file():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", catalog, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:  # noqa: BLE001 — treated as "nothing to sync"
        return None
    if not any(".jsonl" in line for line in proc.stdout.splitlines()):
        return None
    return catalog


def _run_catalog_sync(catalog: str) -> None:
    """The body of the daemon thread. Logs its outcome either way.

    The synchronous version discarded both streams and its exit status, so
    an operator had no way to learn that a session-close push had failed.
    That is the whole reason this logs rather than merely not-crashing.
    """
    try:
        proc = subprocess.run(
            ["nx", "catalog", "sync", "-m", "auto-sync at session close"],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception as exc:  # noqa: BLE001 — a thread must not raise into the server
        _emit(
            "warning",
            "stop_verification_catalog_sync_failed",
            catalog=catalog,
            error=str(exc),
        )
        return
    if proc.returncode == 0:
        _emit("info", "stop_verification_catalog_sync_ok", catalog=catalog)
    else:
        _emit(
            "warning",
            "stop_verification_catalog_sync_failed",
            catalog=catalog,
            returncode=proc.returncode,
            stderr=(proc.stderr or "").strip()[:500],
        )


def _start_catalog_sync(catalog: str) -> threading.Thread:
    thread = threading.Thread(
        target=_run_catalog_sync,
        args=(catalog,),
        name="stop-verification-catalog-sync",
        daemon=True,
    )
    thread.start()
    return thread


def _beads_in_progress() -> bool:
    if shutil.which("bd") is None:
        return False
    try:
        proc = subprocess.run(
            ["bd", "list", "--status=in_progress"],
            capture_output=True,
            text=True,
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

    catalog = _catalog_sync_target()
    if catalog is not None:
        _start_catalog_sync(catalog)

    if _beads_in_progress():
        warnings += _BEADS_WARNING

    return _approve(warnings)
