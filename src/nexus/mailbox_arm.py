# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SessionStart mailbox-subscribe instruction (RDR-211 nexus-rplay.14,
superseding the arm instruction bead nexus-6konb.9, MM-3.1 originally
wrote this module for).

RDR-211: the nexus MCP server is the session's own subscriber and delivery
endpoint, waking the session through the Claude Code channel
(``notifications/claude/channel``) for its own mailbox and any subscribed
board topics (see :mod:`nexus.mcp.subscriptions`). Subscribing the
session's own name (RDR-208 Phase 3, bead nexus-galkv.20: this arms a
``directory/<name>`` lease, never a second delivered mailbox) is a request
the model can decline or forget -- neither it nor the channel is ever the
floor (``nexus-73vnw``'s :mod:`nexus.hooks.mailbox_drain` is) -- so this
module's only job is to put a correct, literal, copy-pasteable subscribe
instruction in front of the model at the start of every session, and to say
nothing when that instruction could not possibly succeed.

Sam's decision of 2026-09-16 (T2 nexus_rdr/211-decision-channel-delivery-
2026-09-16): the prior Monitor-driven CLI watcher loop, the SessionStart
injection that armed it, and its 30-minute re-arm rule are deleted outright,
not kept as a fallback -- two mechanisms for one delivery was the seam that
decision closed. What this module now renders in their place is one line
asking for the ``tuple_subscribe`` MCP call, plus the setup sentence Sam's
2026-09-17 decision (T2 nexus_rdr/211-decision-dev-channel-dialog-2026-09-17)
requires: the channel is a Claude Code research preview, reached only with a
launch flag and a per-launch confirmation dialog. The ``UserPromptSubmit``
drain hook (:mod:`nexus.hooks.mailbox_drain`) remains the
unconditional floor regardless of whether the channel was ever reached.

Emitted from ``nx hook session-start`` (:func:`nexus.hooks.session_start`),
not the plugin's ``session_start_hook.py``: the MCP tool named,
``tuple_subscribe``, is client-side, so the instruction that names it must
version with the same wheel that ships the tool.

Never emitted when it cannot succeed
    :func:`tuple_surface_available` probes the tuple-space engine once
    (bounded to :data:`PROBE_TIMEOUT_S`, well under this hook's own 10s
    SessionStart budget) and caches the verdict for
    :data:`PROBE_CACHE_TTL_S` under ``<config>/tuple-watch/`` -- the same
    directory :mod:`nexus.session_marker` and the drain hook already
    use for their own state files. A below-floor or unreachable engine
    would otherwise make every session run a subscribe instruction that
    404s or hangs forever, which is worse than saying nothing.

The instance-name mailbox
    The ``ListAgents`` row name (e.g. ``nexus-19``) reaches no environment
    variable anywhere and it reaches no FILE this module could read either,
    so this module reads NOTHING to guess it. The name exists only in the
    model's own knowledge, from the ``ListAgents`` tool's "This session is
    <name>" line, and the rendered instruction says so: pass it to
    ``tuple_subscribe("mailbox/<name>")``, taken from a fresh
    ``ListAgents`` call, never from memory. Subscribing it arms a
    ``directory/<name>`` lease so a peer's ``mailbox_send`` can resolve
    this session by name; it does not itself register anything the drain
    hook reads (RDR-208 Phase 3, bead nexus-galkv.20 deleted that
    per-session registry -- :mod:`nexus.hooks.mailbox_drain`'s own
    docstring has the retirement note).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

#: Marker substring every rendered instruction carries, so a caller can
#: grep for presence/absence without matching the full text (skills,
#: tests, MM-3.3's future skill rule).
ARM_MARKER = "MAILBOX SUBSCRIBE"

#: Shared with :mod:`nexus.session_marker` and the drain hook: one
#: subdirectory under the config dir for every mailbox-delivery state file.
_STATE_SUBDIR = "tuple-watch"
_PROBE_CACHE_NAME = "arm-probe-cache.json"

#: Bounded well under this hook's own 10s SessionStart budget (hooks.json:
#: ``"command": "nx hook session-start", "timeout": 10``) -- the store's
#: own default HTTP timeout is 30s, which alone would blow the budget.
PROBE_TIMEOUT_S = 2.0

#: How long a probe verdict is trusted before re-probing. SessionStart
#: fires on startup/resume/clear/compact, potentially several times in a
#: short span (e.g. repeated ``/compact``); this bounds how often the
#: probe actually touches the network rather than re-probing every fire.
PROBE_CACHE_TTL_S = 120.0



def _probe_cache_path(config_dir: Path) -> Path:
    return config_dir / _STATE_SUBDIR / _PROBE_CACHE_NAME


def _load_probe_cache(path: Path) -> tuple[bool, float] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool(data["available"]), float(data["checked_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_probe_cache(path: Path, available: bool, checked_at: float) -> None:
    """Best-effort. Losing this file just means the next call re-probes --
    never a reason to fail the SessionStart hook over a cache write."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"available": available, "checked_at": checked_at}),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        pass


def _probe_tuple_surface(timeout_s: float) -> bool:
    """One bounded call: is the tuple-space engine answering at all?

    Deliberately coarse -- this is a yes/no gate on whether the arm
    instruction could possibly succeed, not a diagnosis. Any failure
    (unresolvable endpoint, connection refused, timeout, 404 from a
    below-floor engine) means "no arm instruction this time," logged at
    debug so a genuine outage is still traceable without being loud in a
    hook that must never fail the session over it.
    """
    from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — deferred: rare/branch-local path

    try:
        HttpTupleStore(timeout=timeout_s).registry()
    except Exception as exc:  # noqa: BLE001 — availability gate, not diagnosis
        _log.debug("mailbox_arm_probe_failed", error=str(exc))
        return False
    return True


def tuple_surface_available(
    config_dir: Path,
    *,
    now: float | None = None,
    probe_timeout_s: float = PROBE_TIMEOUT_S,
    cache_ttl_s: float = PROBE_CACHE_TTL_S,
) -> bool:
    """Cached, bounded answer to "can an arm instruction possibly work?".

    A cache hit within *cache_ttl_s* costs one file read. A miss costs one
    HTTP round trip bounded to *probe_timeout_s*, then writes the verdict
    back so the next SessionStart fire (startup/resume/clear/compact can
    arrive in a tight span) does not re-probe.
    """
    t = now if now is not None else time.time()
    path = _probe_cache_path(config_dir)
    cached = _load_probe_cache(path)
    if cached is not None:
        available, checked_at = cached
        if 0 <= t - checked_at < cache_ttl_s:
            return available
    available = _probe_tuple_surface(probe_timeout_s)
    _save_probe_cache(path, available, t)
    return available


def mailbox_arm_instruction(session_id: str) -> str:
    """The literal subscribe-instruction text for *session_id*. Pure text
    -- no I/O, no probing -- so tests and the mailbox skill can both call
    it directly.

    Carries no instance-name literal (nexus-6konb.9 defect fix, still true
    under RDR-211): that name exists only in the model's own knowledge,
    from ``ListAgents``'s "This session is <name>" line, never in this
    process's environment or any file this module could read. The
    rendered call therefore always names the placeholder ``<name>``, taken
    from a FRESH ``ListAgents`` call rather than memory (nexus-6konb.20):
    ListAgents renames a session on resume (measured nexus-58 to nexus-03,
    2026-09-14), and reusing an old name from memory would subscribe the
    wrong instance mailbox. The session's own ``mailbox/<session id>`` is
    already subscribed from MCP-server startup and needs no call here
    (:mod:`nexus.mcp.subscriptions`).

    RDR-211 (Sam's decision of 2026-09-16): this replaces the deleted
    Monitor-arm instruction outright, not as a fallback alongside it --
    the channel is the only push path now, and the ``UserPromptSubmit``
    drain hook is the unconditional floor beneath it regardless of whether
    a session ever reaches the channel. Names the development-channel
    launch flag and its per-launch confirmation dialog as setup (Sam's
    decision of 2026-09-17, T2 nexus_rdr/211-decision-dev-channel-dialog-
    2026-09-17): the channel is a Claude Code research preview, not yet
    remembered between launches. nexus-tk2cz (2026-09-17) adds the
    dialog-free plugin form and the alias that makes either form stick,
    in the same one-line byte budget (see ``TestGuidanceByteBudgetIntegration``
    in ``tests/test_hooks.py``).
    """
    return (
        f"{ARM_MARKER}: call "
        f'mcp__plugin_conexus_nexus__tuple_subscribe("mailbox/<name>") once, '
        "with <name> from a fresh ListAgents call now, never from memory (it "
        "changes on resume), so peers can reach this session by name via "
        f"mailbox_send; mailbox/{session_id} is already subscribed and "
        "delivered over the channel regardless. "
        "The channel is a Claude Code research preview: launch with "
        "--channels plugin:conexus@nexus-plugins (dialog-free once "
        "allowlisted) or --dangerously-load-development-channels "
        "server:nexus (a one-keystroke confirmation dialog every launch); "
        "alias claude to the first form so it always applies; without "
        "either, mail still arrives at your next prompt through the drain "
        "hook."
    )


def arm_block(session_id: str | None, *, config_dir: Path | None = None) -> str:
    """The full arm instruction for *session_id*, or ``""`` when one
    should not be emitted (no usable session id, or the tuple surface is
    not answering).
    """
    if not session_id or session_id == "unknown":
        return ""
    if config_dir is None:
        # Module import, not a by-value ``nexus_config_dir`` import, so a test
        # that setattr-patches nexus.config reaches this call (nexus-78blw).
        from nexus import config as _nx_config  # noqa: PLC0415 — deferred: rare/branch-local path

        config_dir = _nx_config.nexus_config_dir()
    if not tuple_surface_available(config_dir):
        return ""
    return mailbox_arm_instruction(session_id)
