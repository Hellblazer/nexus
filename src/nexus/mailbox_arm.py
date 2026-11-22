# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SessionStart mailbox line (RDR-211 nexus-rplay.14; RDR-253 Phase 2, nexus-hczdo.2.1).

RDR-211: the nexus MCP server is the session's own subscriber and delivery
endpoint, waking the session through the Claude Code channel
(``notifications/claude/channel``) for its mailbox and any subscribed board
topics (see :mod:`nexus.mcp.subscriptions`). Neither it nor the channel is
ever the floor (``nexus-73vnw``'s :mod:`nexus.hooks.mailbox_drain` is).

RDR-253 Phase 2 retired the instruction this module used to put in front of
the model (``tuple_subscribe("mailbox/<name>")``, which armed a
``directory/<name>`` lease so a peer could address the session by its
``ListAgents`` name). Mail goes to the session's identity now, which needs no
subscribe call, so the line says so and keeps its other job: the delivery
setup. Sam's decision of 2026-09-17 (T2 nexus_rdr/211-decision-dev-channel-
dialog-2026-09-17): the channel is a Claude Code research preview, reached only
with a launch flag and a per-launch confirmation dialog, and a session
launched without it is pointed at ``nx tuple wait`` (RDR-233).

Emitted from ``nx hook session-start`` (:func:`nexus.hooks.session_start`),
not the plugin's ``session_start_hook.py``: the ``nx tuple wait`` it names is
client-side, so the instruction that names it must version with the same
wheel that ships it.

Never emitted when it cannot succeed
    :func:`tuple_surface_available` probes the tuple-space engine once
    (bounded to :data:`PROBE_TIMEOUT_S`, well under this hook's own 10s
    SessionStart budget) and caches the verdict for
    :data:`PROBE_CACHE_TTL_S` under ``<config>/tuple-watch/`` -- the same
    directory :mod:`nexus.session_marker` and the drain hook already
    use for their own state files. A below-floor or unreachable engine
    would otherwise make every session run a setup line that points at a
    surface that 404s or hangs, which is worse than saying nothing.
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
ARM_MARKER = "MAILBOX DELIVERY"

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


def mailbox_arm_instruction(*, channel: bool = True, bound: bool = True) -> str:
    """The literal mailbox line. Pure text -- no I/O, no probing -- so tests and
    the mailbox skill can both call it directly.

    Carries no session id and no instance name: mail goes to the session's
    identity (RDR-253 Phase 2), which a peer addresses by its id (``i`` and
    digits) or its seat's name, so there is nothing for the model to subscribe
    and nothing in this line that goes stale on a resume or a ``/rename``.

    RDR-211 (Sam's decision of 2026-09-16): the channel is the push path for a
    session launched with it (RDR-233 adds ``nx tuple wait`` for one launched
    without), and the ``UserPromptSubmit`` drain hook is the unconditional
    floor beneath it regardless of whether a session ever reaches the channel.
    Names the development-channel launch flag and its per-launch confirmation
    dialog as setup (Sam's decision of 2026-09-17): the channel is a Claude
    Code research preview, not yet remembered between launches. nexus-tk2cz
    (2026-09-17) adds the dialog-free plugin form and the alias that makes
    either form stick, in the same one-line byte budget (see
    ``TestGuidanceByteBudgetIntegration`` in ``tests/test_hooks.py``).

    *channel* ``False`` (RDR-233, bead nexus-7hzb2) is a session whose
    ``claude`` process carries no channel launch flag (the Claude Desktop Code
    tab cannot add one): the launch-flag advice is useless there, so it is
    replaced by the channel-free push path, ``nx tuple wait`` run as a
    background task and re-run only when a wake's output says to (a superseded,
    session-changed or refused exit says not to). No longer than the channel
    form, so the same budget covers both.

    *bound* ``False`` is a session whose SessionStart bind did not complete:
    it has no identity, so no mail is addressed to it and the line says that
    instead of promising delivery.
    """
    if not bound:
        return (
            f"{ARM_MARKER}: this session is not bound to an identity, so no mail reaches it until it is. The "
            "SessionStart bind did not complete (engine down, or the Claude registry file came late); it binds at the "
            "next SessionStart (restart, /clear, resume), and the MCP server binds a pending session once the "
            "registry file appears. `nx identity show` says where it stands."
        )
    lead = (
        f"{ARM_MARKER}: mail to this session goes to its identity's mailbox, addressed by identity id "
        "(\"i\" and digits) or seat name, never by session id or ListAgents name; nothing to subscribe"
    )
    if not channel:
        return lead + (
            ". This session was launched without the channel, so nothing pushes "
            "to it: run `nx tuple wait` with run_in_background now, and again "
            "after each wake that says to, to be woken by new mail and board "
            "posts; mail also arrives at your next prompt via the drain hook."
        )
    return lead + (
        "; it is delivered over the channel regardless. "
        "The channel is a Claude Code research preview: launch with "
        "--channels plugin:conexus@nexus-plugins (dialog-free once "
        "allowlisted) or --dangerously-load-development-channels "
        "server:nexus (a one-keystroke confirmation dialog every launch); "
        "alias claude to the first form so it always applies; without "
        "either, mail still arrives at your next prompt through the drain "
        "hook."
    )


def arm_block(session_id: str | None, *, bound: bool = True, config_dir: Path | None = None) -> str:
    """The full mailbox line, or ``""`` when one should not be emitted (no usable
    session id, or the tuple surface is not answering). *session_id* only gates
    emission: the line itself names no session. *bound* is whether the SessionStart
    bind gave the session an identity; an unbound session is told no mail reaches
    it yet (:func:`mailbox_arm_instruction`).
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
    from nexus.channel_launch import channel_launch_state  # noqa: PLC0415 — deferred: one ps read, this path only

    # RDR-233: only a definite "no flag" switches to the channel-free wording;
    # "cannot tell" (None) keeps the channel wording this line always had.
    return mailbox_arm_instruction(channel=channel_launch_state() is not False, bound=bound)
