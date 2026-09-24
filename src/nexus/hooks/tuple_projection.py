# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The RDR-205 ledger projections, off the hook's own path (bead nexus-q02nx.20).

Ports ``conexus/hooks/scripts/subagent-start-tuple-async.sh`` and
``subagent-stop-tuple-async.sh``. Both were pure detachment machinery —
read the payload, spawn ``tuple_ledger_project.py`` in a background
subshell, ``disown``, exit 0 — so what was ported at nexus-q02nx.20 was the
DETACHMENT, not the projection.

**TWO TOOLS, NOT ONE, AND NOT FOLDED INTO THE HOOKS THEY SIT BESIDE.** The
wrappers are SIBLINGS of ``subagent-start.sh`` and ``subagent-stop.sh`` in
their hooks.json arrays, never children, and ``subagent-start-tuple-async.sh``'s
own header calls that out as CA 4. The point is independence: if the main
hook fails, the projection still runs. Folding this into
``subagent_stop.run()`` would make it a child and quietly undo that, so the
sibling shape is preserved as two separately-registered tools.

**THE PROJECTOR IS NOW IN-PROCESS (bead nexus-b5ugt), superseding this
module's earlier note that it "stays where it is [in plugin content] ...
because importing it would invert this epic's dependency direction."**
That reasoning held only while an installed-wheel user's ``nx-mcp`` could
still find the plugin-resident script under ``CLAUDE_PLUGIN_ROOT`` — but
``conexus/.mcp.json`` sets that env var to the LITERAL, unexpanded string
``${CLAUDE_PLUGIN_ROOT}`` (Claude Code does not expand ``${...}`` in an MCP
``env`` block), so every real ``nx-mcp`` process on this box carried that
literal, the plugin-root candidate never resolved, the checkout-relative
fallback is absent under any installed wheel, and every RDR-205 ledger
projection since has silently written nothing (``tuple_projection_no_projector``
on every SubagentStart/SubagentStop — confirmed live in
``~/.config/nexus/logs/mcp.log``). RDR-215's own point is eliminating the
plugin-resident hook layer so a native client with no ``conexus/`` checkout
sibling becomes viable at all — "find the plugin script more reliably"
would still assume the file exists somewhere findable, which is exactly the
assumption this bead removes. :func:`nexus.hooks.tuple_ledger_project.project`
is the ported body; see that module's own docstring for what changed
(endpoint/credential resolution now calls :mod:`nexus.db.service_endpoint`
and :mod:`nexus.db.data_token` directly instead of a stdlib-only mirror) and
what did not (the wire shape, the BEARER PRECEDENCE credential policy, the
per-session log file).

``conexus/hooks/scripts/tuple_ledger_project.py`` was NOT deleted by this
bead, on the stated ground that ``mailbox_drain.py`` and the ``routing/``
guards still imported its sibling ``_endpoint_resolve.py``. nexus-t9klx
ported all three, and the file and its sibling were deleted at
nexus-z9cz2.

**ONE PROPERTY PRESERVED FROM THE SUBPROCESS DESIGN, RE-HOMED RATHER THAN
DROPPED.** The old ``_project`` bounded a wedged SUBPROCESS with
``subprocess.run(..., timeout=_TIMEOUT_S)``; there is no subprocess to time
out any more, so :func:`_project` now runs :func:`~nexus.hooks.tuple_ledger_project.project`
on its OWN inner daemon thread and joins it with the same
``_TIMEOUT_S`` deadline, abandoning it (never raising, never blocking the
caller) if it is still alive past that point. This is not purely
defensive: ``project()``'s own HTTP POST already bounds a single network
round trip to a 5s whole-call deadline (see that module's ``_post_via_urllib``),
but local file I/O — reading a stopping agent's transcript for VERIFY
lines — carries no such bound, and a pathological or NFS-stalled
transcript file is exactly the kind of "wedged" case this thread+timeout
exists to survive without ever holding up the caller (see
``TestItReturnsImmediately`` in ``tests/hooks/test_tuple_projection_module.py``,
which the OUTER daemon thread below still satisfies unconditionally: a
hung ``project()`` call never delays ``run_start``/``run_stop`` themselves,
regardless of what happens to the inner bounded thread).

**ONE REAL DURABILITY REGRESSION, recorded because the bead does not
mention it (carried over from nexus-q02nx.20, still true here).** Nothing
in this process outlives the interpreter; a daemon thread dies at
interpreter exit. So a server that stops between the event and the
thread's own HTTP POST loses that projection, where a detached subprocess
would have completed it. The write IS idempotent (``id_from: keys``
derives the tuple id from ``(agent_id, kind)`` alone), so a retry would be
safe — but nothing retries, and a stopped agent has no later firing. A
lost projection is simply lost.
"""
from __future__ import annotations

import threading

from nexus._hook_runtime._io import HookResult, _emit
from nexus.hooks import tuple_ledger_project

__all__ = ["run_start", "run_stop"]

#: Bound on the WHOLE in-process projection call (endpoint/credential
#: resolution, transcript read, POST — including the one schema-fallback
#: retry). Long enough for a slow POST plus a slow transcript read; short
#: enough that a wedged projection cannot hold this module's own inner
#: thread open forever. The outer daemon thread (:func:`_spawn`) already
#: makes ``run_start``/``run_stop`` return immediately regardless of this
#: deadline — this bound exists so an abandoned, wedged call does not
#: linger indefinitely as a leaked thread.
_TIMEOUT_S = 120.0


def _project(verb: str, payload: dict) -> None:
    """The thread body. Never raises; logs its own outcome.

    Runs :func:`nexus.hooks.tuple_ledger_project.project` on an inner
    daemon thread, bounded by :data:`_TIMEOUT_S` — see the module
    docstring's "ONE PROPERTY PRESERVED" section for why this bound moved
    here rather than disappearing when the subprocess did.
    """
    outcome: dict[str, BaseException] = {}

    def _run() -> None:
        try:
            tuple_ledger_project.project(verb, payload)
        except Exception as exc:  # noqa: BLE001 — a projection must never reach the hook
            outcome["error"] = exc

    thread = threading.Thread(target=_run, name=f"tuple-projection-post-{verb}", daemon=True)
    thread.start()
    thread.join(timeout=_TIMEOUT_S)
    if thread.is_alive():
        _emit("warning", "tuple_projection_timeout", verb=verb, timeout_s=_TIMEOUT_S)
        return
    if "error" in outcome:
        _emit("warning", "tuple_projection_failed", verb=verb, error=str(outcome["error"]))
        return
    # BOTH outcomes are logged. The success line is what makes the failure
    # lines mean something: with nothing here, a projection that ran
    # cleanly and a thread that never started are the same absence in the
    # hook log, which is the distinction the docstring above says this
    # logging exists to draw (nexus-q02nx.24).
    _emit("info", "tuple_projection_ok", verb=verb)


def _spawn(verb: str, payload: dict | None) -> HookResult:
    """Start the projection and return immediately, emitting nothing.

    Stdout stays empty on every path: these entries are siblings of the
    real hooks and must not contribute to any decision.
    """
    body = payload if isinstance(payload, dict) else {}
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
