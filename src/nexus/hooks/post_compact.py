# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The PostCompact context re-injection (RDR-215 bead nexus-q02nx.19).

Port of ``conexus/hooks/scripts/post_compact_hook.sh``. After a
compaction, ``SessionStart(compact)`` already re-injects skills, T2
memory and ``bd ready``; this adds the one thing that does not cover —
ACTIVE WORK. In-progress beads and the session's own T1 scratch.

**Its output is RAW MARKDOWN, not a JSON envelope**, unlike every
other hook ported in this epic. The harness takes this one's stdout as
the context directly.

**Emits nothing when it has nothing.** The header is written only if a
body was assembled, so a session with no in-progress beads and no
scratch gets silence rather than an empty section. Carried deliberately:
the output budget is 20 lines and an empty header spends one of them
saying nothing.

**It forces ``NX_SESSION_ID`` onto every subprocess** (nexus-7o1zh).
``resolve_active_session_id``'s lowest-priority fallback is the
machine-wide ``~/.config/nexus/current_session`` file, which ANY second
top-level session's SessionStart clobbers unconditionally. This hook
runs detached from any live server and cannot inherit a session, so it
reads the id from its own payload and forces it — otherwise ``nx scratch
list`` returns a sibling session's scratch, which is worse than
returning none.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from nexus._hook_runtime._io import HookResult

__all__ = ["run"]

#: Both listings are truncated to five lines. The hook's whole budget is
#: 20 lines and it has two sections plus a header.
_MAX_LINES = 5

#: The literal ``nx scratch list`` prints when a session has no entries.
#: Compared exactly, as the script does: treating it as content would
#: emit a scratch section whose only content is the word for "empty".
_EMPTY_SCRATCH = "No scratch entries."


def _nx_env(session_id: str) -> dict:
    """The environment every subprocess here needs. See the module docstring."""
    env = dict(os.environ)
    if session_id:
        env["NX_SESSION_ID"] = session_id
    return env


def _capture(argv: list[str], env: dict) -> str:
    """Run *argv*, returning stdout, or "" on any failure.

    Every failure is silent and yields no section. This hook is pure
    context injection: a missing ``bd``, a broken ``nx`` or a timeout
    costs the reader one paragraph of help, and must not cost them the
    compaction.
    """
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=10.0, env=env
        )
    except Exception:  # noqa: BLE001 — a context hook must never fail; see above
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _head(text: str, limit: int = _MAX_LINES) -> str:
    return "\n".join(text.splitlines()[:limit])


def run(payload: dict | None) -> HookResult:
    """Re-inject active-work context after a compaction."""
    data = payload if isinstance(payload, dict) else {}
    env = _nx_env(str(data.get("session_id") or ""))

    body = ""

    if shutil.which("bd") is not None:
        active = _capture(
            ["bd", "list", "--status=in_progress", "--limit=5"], env
        ).strip()
        if active:
            body += (
                "### Active Work\n```\n" + _head(active) + "\n```\n"
            )

    if shutil.which("nx") is not None:
        scratch = _capture(["nx", "scratch", "list"], env).strip()
        if scratch and scratch != _EMPTY_SCRATCH:
            body += "### Session Scratch (T1)\n" + _head(scratch) + "\n"

    if not body:
        return HookResult()

    # RAW TEXT, NOT A JSON ENVELOPE. Checked rather than assumed, and the
    # first draft of this port got it wrong: every sibling hook in this
    # epic emits {"hookSpecificOutput": ...}, so an envelope is what the
    # hand reaches for. This one does not — it `echo`s markdown and the
    # harness takes stdout as the context directly. Its hooks.json entry
    # confirms it: a bare `bash .../post_compact_hook.sh` with no wrapper.
    return HookResult(stdout="## Post-Compaction Context\n" + body)
