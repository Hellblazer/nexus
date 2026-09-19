# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The divergence-language advisory (RDR-215 bead nexus-q02nx.19).

Port of ``conexus/hooks/scripts/divergence-language-guard.sh``. Fires
PostToolUse on a Write or Edit to a file under ``docs/rdr/post-mortem/``,
runs the locked Rev 4 eight-pattern bank over it, and — if anything
matches — returns the hits as advisory context.

**ADVISORY ONLY. It has one stdout shape, allow, and no other.** The
hits may indicate acknowledged scope deferral, which is intended, or
silent scope reduction, which is not, and only a reader can tell which.
A guard that cannot distinguish them must not decide.

**Three side effects it performs on the way, all carried:**

* it forces ``NX_SESSION_ID`` from its own payload, because it runs
  detached and the machine-wide session pointer is clobbered by any
  second top-level session;
* it WRITES to T1 scratch — one row per firing, tagged for a
  precision review of the pattern bank. This is the only hook in the
  epic that writes as a side effect of advising, and a grep for shell
  redirects does not find it, because the write is a tool invocation;
* it RESOLVES its scan script as a sibling of itself.

**The scan body stays a separate file, and the reason does not survive
the port** — ``nexus-2gcqk``: bash 5.3 pipes heredoc bodies and a >512B
body deadlocks when macOS degrades pipe buffers, so the eight patterns
could not live inline in the script. Nothing here pipes anything, so the
bank is imported. ``divergence-language-scan.py`` is stdlib-only and
still invoked by the live bash, so it is imported BY PATH rather than
copied: two copies of a locked pattern bank is how a Rev 4 becomes a
Rev 4 and a Rev 4-prime.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from nexus._hook_runtime._io import HookResult
from nexus.hooks._plugin import plugin_script

__all__ = ["run"]

#: Only a post-mortem is scanned. The pattern bank is tuned for that
#: genre and firing it on ordinary prose would be noise.
_SCANNED_PREFIX = "docs/rdr/post-mortem/"

#: PostToolUse on these two only.
_WATCHED_TOOLS = ("Write", "Edit")

_ADVISORY = (
    "These may indicate acknowledged scope deferral (intended) or silent "
    "scope reduction (unintended). Review each hit and decide: is this a "
    "real divergence that should force close_reason=partial, or a "
    "legitimate acknowledged deferral?"
)


def _allow(context: str = "") -> HookResult:
    """The only envelope this hook can produce. Key order carried."""
    out: dict = {"hookEventName": "PostToolUse", "permissionDecision": "allow"}
    if context:
        out["additionalContext"] = context
    return HookResult(stdout=json.dumps({"hookSpecificOutput": out}))


def _nx_env(session_id: str) -> dict:
    env = dict(os.environ)
    if session_id:
        env["NX_SESSION_ID"] = session_id
    return env


def _scan_script() -> Path:
    """The sibling scan, wherever the plugin actually is.

    The bash used ``dirname ${BASH_SOURCE[0]}`` and was always right,
    because it WAS the sibling. This module is not: installed, it sits in
    site-packages, where a checkout-relative anchor resolves to the
    interpreter's lib directory and the scan is simply never found. The
    caller treats a missing script as no hits, so that failure is silent
    -- which is how it shipped. See ``_plugin.py``.
    """
    return plugin_script("divergence-language-scan.py")


def _hits(file_path: str) -> str:
    """The scan's output, or "" on any failure. A missing sibling file
    yields no hits and the advisory no-ops, exactly as in bash."""
    script = _scan_script()
    if not script.is_file():
        return ""
    try:
        proc = subprocess.run(
            ["python3", str(script), file_path],
            capture_output=True, text=True, timeout=30.0,
        )
    except Exception:  # noqa: BLE001 — carried: a crashed scan is an advisory no-op
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _log_hit(file_path: str, hit_count: int, env: dict) -> None:
    """One T1 row per firing, for the precision review of the bank.

    Best-effort and silent on failure, as in bash. This is the write a
    redirect-shaped grep does not find.
    """
    if shutil.which("nx") is None:
        return
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        subprocess.run(
            [
                "nx", "scratch", "put",
                f"divergence-hook hit: {stamp} file={file_path} hits={hit_count}",
                "--tags", "divergence-hook-hit,precision-review",
            ],
            capture_output=True, text=True, timeout=10.0, env=env,
        )
    except Exception:  # noqa: BLE001 — carried: the advisory must not fail on its own logging
        return


def run(payload: dict | None) -> HookResult:
    """Advise on divergence language in a post-mortem just written."""
    data = payload if isinstance(payload, dict) else {}
    if not data:
        return _allow()

    env = _nx_env(str(data.get("session_id") or ""))

    if str(data.get("tool_name") or "") not in _WATCHED_TOOLS:
        return _allow()

    tool_input = data.get("tool_input")
    file_path = ""
    if isinstance(tool_input, dict):
        file_path = str(tool_input.get("file_path") or "")
    if _SCANNED_PREFIX not in file_path:
        return _allow()
    if not Path(file_path).is_file():
        return _allow()

    hits = _hits(file_path)
    if not hits:
        return _allow()

    _log_hit(file_path, len(hits.splitlines()), env)
    return _allow(
        f"Divergence-language hits in {Path(file_path).name}:\n{hits}\n\n"
        + _ADVISORY
    )
