# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The RDR-205 ledger projections, off the hook's own path (bead nexus-q02nx.20).

Ports ``conexus/hooks/scripts/subagent-start-tuple-async.sh`` and
``subagent-stop-tuple-async.sh``. Both were pure detachment machinery —
read the payload, spawn ``tuple_ledger_project.py`` in a background
subshell, ``disown``, exit 0 — so what is ported is the DETACHMENT, not
the projection.

**TWO TOOLS, NOT ONE, AND NOT FOLDED INTO THE HOOKS THEY SIT BESIDE.**
The wrappers are SIBLINGS of ``subagent-start.sh`` and
``subagent-stop.sh`` in their hooks.json arrays, never children, and
``subagent-start-tuple-async.sh``'s own header calls that out as CA 4.
The point is independence: if the main hook fails, the projection still
runs. Folding this into ``subagent_stop.run()`` would make it a child
and quietly undo that, so the sibling shape is preserved as two
separately-registered tools.

**THE PROJECTOR IS STILL A SUBPROCESS, deliberately.** It lives in
plugin content, is stdlib-only by contract, and carries its own HTTP and
token handling — some 400 lines. Importing it would invert this epic's
dependency direction (the wheel importing from the plugin) and would
create a second live copy while the bash still runs it until bead .21.
An earlier note of mine on bead .14 said this bead "moves the work into
the server"; that over-promised. The bead moves the WRAPPERS. The
projector stays where it is and the mirrored charset guard stays with
it.

**ONE REAL DURABILITY REGRESSION, recorded because the bead does not
mention it.** A disowned process survives the hook's exit; a daemon
thread does not — it dies at interpreter exit. So a server that stops
between the event and the thread's HTTP POST loses that projection,
where the bash would have completed it. The write IS idempotent
(``id_from: keys`` derives the tuple id from ``(agent_id, kind)`` alone,
per the projector's own docstring), so a retry would be safe — but
nothing retries, and a stopped agent has no later firing. A lost
projection is simply lost. Smaller than it sounds and strictly worse
than the bash, which is the honest way round to say it.
"""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from nexus._hook_runtime._io import HookResult, _emit
from nexus.hooks._plugin import checkout_plugin_root

__all__ = ["run_start", "run_stop"]

#: Long enough for a slow POST, short enough that a wedged projector
#: cannot hold a thread for the life of the server.
_TIMEOUT_S = 120.0


def _projector() -> Path | None:
    """The plugin-side projector, or None.

    Resolved off ``CLAUDE_PLUGIN_ROOT`` and then off this checkout, the
    same two-candidate shape the close gate uses. A missing projector is
    a no-op: the bash's own failure mode was an empty background
    subshell, and a projection that cannot run must not disturb a hook.
    """
    candidates = []
    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if root:
        candidates.append(Path(root) / "hooks" / "scripts" / "tuple_ledger_project.py")
    candidates.append(
        checkout_plugin_root() / "hooks" / "scripts" / "tuple_ledger_project.py"
    )
    return next((c for c in candidates if c.is_file()), None)


def _project(verb: str, payload_json: str) -> None:
    """The thread body. Never raises; logs its own outcome.

    The bash sent both streams to ``/dev/null``, which was not silence —
    the projector writes its own skip and failure reasons to a file
    beside the session's ledger. That file is still where the detail
    goes; this adds only a line saying the attempt happened at all,
    because a thread that dies quietly is harder to notice than a
    process that was never spawned.
    """
    script = _projector()
    if script is None:
        _emit("warning", "tuple_projection_no_projector", verb=verb)
        return
    try:
        proc = subprocess.run(
            ["python3", str(script), verb],
            input=payload_json,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — a projection must never reach the hook
        _emit("warning", "tuple_projection_failed", verb=verb, error=str(exc))
        return
    if proc.returncode != 0:
        _emit(
            "warning",
            "tuple_projection_nonzero",
            verb=verb,
            returncode=proc.returncode,
            stderr=(proc.stderr or "").strip()[:300],
        )
        return
    # BOTH outcomes are logged. The success line is what makes the
    # failure lines mean something: with nothing here, a projection that
    # ran cleanly and a thread that never started are the same absence in
    # the hook log, which is the distinction the docstring above says
    # this logging exists to draw (nexus-q02nx.24).
    _emit("info", "tuple_projection_ok", verb=verb)


def _spawn(verb: str, payload: dict | None) -> HookResult:
    """Start the projection and return immediately, emitting nothing.

    Stdout stays empty on every path: these entries are siblings of the
    real hooks and must not contribute to any decision.
    """
    import json  # noqa: PLC0415 — only a real firing pays it

    # ``default=str`` is not cosmetic. The encode happens HERE, on the
    # hook's own thread, before the spawn — so an unserialisable field
    # would raise into the hook rather than into the thread that is
    # allowed to fail. The projector reads a handful of string fields
    # and ignores the rest, so stringifying an odd value costs nothing
    # and losing the whole payload would cost the projection.
    body = json.dumps(payload if isinstance(payload, dict) else {}, default=str)
    thread = threading.Thread(
        target=_project,
        args=(verb, body),
        name=f"tuple-projection-{verb}",
        daemon=True,
    )
    thread.start()
    return HookResult()


def run_start(payload: dict | None) -> HookResult:
    """Project the ledger START tuple for a subagent that just began."""
    return _spawn("start", payload)


def run_stop(payload: dict | None) -> HookResult:
    """Project the ledger REPORT tuple for a subagent that just stopped.

    Needs no cooperation from the stopping agent: the tuple is keyed on
    the same harness-issued ``agent_id`` the SubagentStart payload
    carried, which ``subagent-start.sh`` already injected into that
    agent's context as its claimant id.
    """
    return _spawn("report", payload)
