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
    established that, which is why ``nx tuple watch`` takes it as an
    explicit ``--instance``-shaped positional literal, never reading it
    from the process environment. The only persistence point that exists
    today is the shared registry ``mailbox_drain.py`` already reads,
    ``<config>/tuple-watch/addresses`` -- one address per line, populated
    by a human or a future arming action (that file's own docstring: "arming
    writes to it, and so can a human"). Nothing currently writes to it;
    this bead only *reads* it. Because the file is machine-wide, not scoped
    to one session, :func:`known_instance_name` trusts it only when it
    names exactly ONE candidate that is not this session's own id --
    zero or several candidates degrade to "not known" (session-id-only)
    rather than risk arming the wrong mailbox as though it were this
    session's own.
"""
from __future__ import annotations

import json
import re
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
_REGISTRY_NAME = "addresses"
_PROBE_CACHE_NAME = "arm-probe-cache.json"

#: Same charset discipline as tuple_watch.py's ``_SAFE_NAME`` and
#: mailbox_drain.py's ``_valid_address`` -- a registry line becomes a
#: literal token in a CLI instruction shown to the model, so anything
#: outside a safe, boring charset is dropped rather than escaped.
_SAFE_ADDRESS = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

#: Bounded well under this hook's own 10s SessionStart budget (hooks.json:
#: ``"command": "nx hook session-start", "timeout": 10``) -- the store's
#: own default HTTP timeout is 30s, which alone would blow the budget.
PROBE_TIMEOUT_S = 2.0

#: How long a probe verdict is trusted before re-probing. SessionStart
#: fires on startup/resume/clear/compact, potentially several times in a
#: short span (e.g. repeated ``/compact``); this bounds how often the
#: probe actually touches the network rather than re-probing every fire.
PROBE_CACHE_TTL_S = 120.0


def _registry_path(config_dir: Path) -> Path:
    return config_dir / _STATE_SUBDIR / _REGISTRY_NAME


def _probe_cache_path(config_dir: Path) -> Path:
    return config_dir / _STATE_SUBDIR / _PROBE_CACHE_NAME


def _valid_address(address: str) -> bool:
    return bool(_SAFE_ADDRESS.match(address))


def _read_registry(config_dir: Path) -> list[str]:
    """Addresses registered by arming or by hand, one per line.

    A missing or unreadable file is an empty registry, never a failure --
    the session-id mailbox never depends on it. Blank lines, ``#``
    comments, and anything outside the safe charset are dropped.
    """
    try:
        raw = _registry_path(config_dir).read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    for line in raw.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if _valid_address(entry):
            out.append(entry)
    return out


def known_instance_name(session_id: str, config_dir: Path) -> str:
    """The instance-name mailbox to arm alongside *session_id*, or ``""``.

    See the module docstring: trusts the shared, unscoped registry only
    when it names exactly one candidate distinct from ``session_id``.
    """
    candidates = [a for a in _read_registry(config_dir) if a != session_id]
    if len(candidates) == 1:
        return candidates[0]
    return ""


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


def mailbox_arm_instruction(session_id: str, instance: str = "") -> str:
    """The literal arm-instruction text for *session_id* (and *instance*
    when known). Pure text -- no I/O, no probing -- so tests and MM-3.3's
    future skill rule can both call it directly.
    """
    if instance:
        command = f"nx tuple watch {session_id} {instance}"
        label = f"{session_id} and {instance}"
    else:
        command = f"nx tuple watch {session_id}"
        label = session_id
    return (
        f"{ARM_MARKER} (RDR-205, nexus-6konb): arm a background watcher ONCE, "
        f"now, so mail sent to your mailbox ({label}) pings you between prompts "
        "instead of waiting for your next turn. Call Monitor exactly once this "
        "session:\n\n"
        "    Monitor({\n"
        f'      command: "{command}",\n'
        f'      description: "mailbox watch for {label}",\n'
        "      persistent: true,\n"
        "      timeout_ms: 3600000\n"
        "    })\n\n"
        "timeout_ms is required by the tool's own schema even though "
        "persistent: true makes it ignored -- any valid value works. Arming "
        "twice is harmless: a second `nx tuple watch` for the same address "
        "refuses itself (one lock per address) and exits at once, so this is "
        "never a doubled watcher.\n\n"
        "Every line it prints is a PING, never the message: sender, kind and a "
        "tuple id, nothing more. On a ping, drain the address it names -- "
        "`nx tuple in mailbox/<address> --pattern to=<address> --claimant "
        "<your-id> --lease-s 60` -- and handle what comes back yourself. The "
        "watcher never claims and never acks."
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
    instance = known_instance_name(session_id, config_dir)
    return mailbox_arm_instruction(session_id, instance)
