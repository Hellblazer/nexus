# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SessionStart mailbox-watch arming instruction (bead nexus-6konb.9, MM-3.1).

RDR-205 / epic nexus-6konb: a Claude Code ``Monitor`` armed on ``nx tuple
watch`` gives a session push delivery for its own RDR-205 mailboxes (see
:mod:`nexus.tuple_watch` for the watcher itself). Arming is a request the
model can decline or forget -- it is never the floor (``nexus-73vnw``'s
``conexus/hooks/scripts/mailbox_drain.py`` is) -- so this module's only
job is to put a correct, literal, copy-pasteable arm instruction in front
of the model at the start of every session, and to say nothing when that
instruction could not possibly succeed.

Emitted from ``nx hook session-start`` (:func:`nexus.hooks.session_start`),
not the plugin's ``session_start_hook.py``: the command being armed,
``nx tuple watch``, is client-side, so the instruction that names it must
version with the same wheel that ships the command. A plugin cut that told
a session to run a flag the installed wheel does not have would be exactly
the client/plugin drift ``PENDING_RELEASE.md`` exists to catch.

Never emitted when it cannot succeed
    :func:`tuple_surface_available` probes the tuple-space engine once
    (bounded to :data:`PROBE_TIMEOUT_S`, well under this hook's own 10s
    SessionStart budget) and caches the verdict for
    :data:`PROBE_CACHE_TTL_S` under ``<config>/tuple-watch/`` -- the same
    directory :mod:`nexus.tuple_watch` and ``mailbox_drain.py`` already use
    for their own state files. A below-floor or unreachable engine would
    otherwise make every session run an arm instruction that 404s or hangs
    forever, which is worse than saying nothing.

The instance-name mailbox
    The ``ListAgents`` row name (e.g. ``nexus-19``) reaches no environment
    variable anywhere -- :func:`nexus.tuple_watch.resolve_watch_addresses`
    established that -- and it reaches no FILE this module could read
    either: an earlier version of this module trusted the machine-wide
    ``<config>/tuple-watch/addresses`` file when it named exactly one
    candidate distinct from this session's own id, but nothing writes
    that file, and on a box running several sessions a populated file
    names several candidates with no way to tell whose instance any of
    them is (nexus-6konb.9 defect fix, drained by
    ``conexus/hooks/scripts/mailbox_drain.py``'s PER-SESSION registry
    instead -- see that module's docstring). So this module reads
    NOTHING to guess an instance name. The name exists only in the
    model's own knowledge, from the ``ListAgents`` tool's "This session
    is <name>" line, and the rendered instruction says so: pass it with
    ``--instance NAME``, in the non-positional form (an explicit
    positional address suppresses ``nx tuple watch``'s session-id
    default outright -- :func:`nexus.tuple_watch.resolve_watch_addresses`
    -- so the command this module renders never uses one), or omit
    ``--instance`` entirely when the name is not known.
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
ARM_MARKER = "MAILBOX WATCH"

#: Shared with :mod:`nexus.tuple_watch` and ``mailbox_drain.py``: one
#: subdirectory under the config dir for every tuple-watch state file.
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
    """The literal arm-instruction text for *session_id*. Pure text -- no
    I/O, no probing -- so tests and MM-3.3's future skill rule can both
    call it directly.

    Carries no instance-name literal (nexus-6konb.9 defect fix): that
    name exists only in the model's own knowledge, from ``ListAgents``'s
    "This session is <name>" line, never in this process's environment or
    any file this module could read. The rendered command therefore never
    uses a positional address either -- an explicit positional suppresses
    ``nx tuple watch``'s session-id default outright
    (:func:`nexus.tuple_watch.resolve_watch_addresses`) -- so it is always
    ``--instance NAME`` or nothing, and the session-id mailbox resolves on
    its own, from this session's own environment, the moment the watcher
    spawns.
    """
    return (
        f"{ARM_MARKER} (RDR-205, nexus-6konb): arm a background watcher ONCE, "
        f"now, so mail sent to your mailboxes pings you between prompts instead "
        "of waiting for your next turn. Your session-id mailbox "
        f"({session_id}) is always watched -- it resolves on its own, from "
        "this session's own environment, the moment the watcher spawns; no "
        "flag is needed for it. Your instance-name mailbox (the ListAgents "
        "row, e.g. nexus-19 -- the \"This session is <name>\" line) is "
        "watched too, but ONLY if you supply it yourself: if you know your "
        "ListAgents name, pass it with --instance NAME; if you do not, omit "
        "--instance entirely and only the session-id mailbox is watched. "
        "NEVER pass the session id or the instance name as a bare positional "
        "argument -- a positional address suppresses the session-id default "
        "outright, so the command is always --instance NAME or no arguments "
        "at all, never a literal address. Call Monitor exactly once this "
        "session:\n\n"
        "    Monitor({\n"
        '      command: "nx tuple watch --instance <your ListAgents name, '
        'or omit this flag if you have none>",\n'
        f'      description: "mailbox watch for {session_id}",\n'
        "      persistent: true,\n"
        "      timeout_ms: 3600000\n"
        "    })\n\n"
        "timeout_ms is required by the tool's own schema even though "
        "persistent: true makes it ignored -- any valid value works. Arming "
        "twice is harmless: a second `nx tuple watch` for the same address "
        "refuses itself (one lock per address) and exits at once, so this is "
        "never a doubled watcher.\n\n"
        "Every line it prints is a PING, never the message: sender, kind and a "
        "tuple id, nothing more. On a ping, drain the address it names: call "
        "mcp__plugin_conexus_nexus__tuple_in on that mailbox with a lease, "
        "handle whatever comes back, then call "
        "mcp__plugin_conexus_nexus__tuple_ack to consume it (passing its "
        "reply argument when the message is a request that needs an answer), "
        "or mcp__plugin_conexus_nexus__tuple_nack if you cannot handle it. An "
        "unacked claim lapses, its attempts count goes up, and after three "
        "lapses the message is dead-lettered undelivered -- so a claim you "
        "cannot finish handling right away still needs an ack or a nack, "
        "never silence. The watcher itself never claims and never acks."
    )


def arm_block(session_id: str | None, *, config_dir: Path | None = None) -> str:
    """The full arm instruction for *session_id*, or ``""`` when one
    should not be emitted (no usable session id, or the tuple surface is
    not answering).
    """
    if not session_id or session_id == "unknown":
        return ""
    if config_dir is None:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred: rare/branch-local path

        config_dir = nexus_config_dir()
    if not tuple_surface_available(config_dir):
        return ""
    return mailbox_arm_instruction(session_id)
