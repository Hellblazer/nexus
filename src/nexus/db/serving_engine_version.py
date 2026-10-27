# SPDX-License-Identifier: AGPL-3.0-or-later
"""The version of the engine THIS process is talking to, and the note-write floor built on it.

:func:`probe_serving_engine_version` is the one probe (nexus-6konb.15). ``nexus.mcp.channel._probe_serving_engine_version`` delegates
to it and stays the channel waiter's test seam, so nothing here is imported from there.

:func:`require_note_uri_engine` is RDR-231's note-write refusal (nexus-bivz3.13, critique
Significant 4): a note write against an engine older than
:data:`~nexus.engine_version.NOTE_URI_MIN_ENGINE_VERSION` is refused before the catalog register
and the T3 write, with the remedy in the message.

Cache policy: a result AT OR ABOVE the floor is cached for the life of the process. A below-floor
result, or no version, is not cached and is probed again on the next note write, so
``nx daemon restart-stale`` takes effect without restarting the client.

No version (a dev or blank jar reports none; an unreachable engine) does not refuse. It logs one
structured warning per process. The protection is therefore best-effort and the backfill guard is
the backstop: this is the corrected form of RDR-231 Critical Assumption 2, whose "true by
construction" holds only when the check fires.

Not covered, by design: aspect and highlight upserts (no unique key on ``source_uri``; the backfill
leaves ``nx-note://`` rows as they are and they join the note once it is backfilled) and reads
(``find_ids_by_title``, ``aspect_sql``), which write nothing.
"""
from __future__ import annotations

import threading

import httpx
import structlog

from nexus._log_safe_url import scrub_transport_error
from nexus.errors import NoteUriEngineTooOldError

_log = structlog.get_logger(__name__)

_lock = threading.Lock()
#: The first version seen at or above the floor; None until then. Process lifetime.
_cleared_version: tuple[int, int, int] | None = None
#: Whether the "no version" warning has been logged in this process.
_warned_unknown = False


def probe_serving_engine_version() -> tuple[int, int, int] | None:
    """Best-effort ``GET /version`` ``release_version`` of the engine this process talks to.

    Resolves the endpoint exactly as the channel waiter does (local lease or managed URL, through
    :func:`~nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate`). Fails closed to
    ``None`` on ANY resolution, transport, non-200 or parse failure, including a blank or dev
    ``release_version``: "don't know" is a value here, never a raise.

    Worst case about 17 s on a local cold lease (the 12 s lease wait plus the 5 s HTTP timeout).
    """
    try:
        from nexus.db.service_endpoint import (  # noqa: PLC0415 — deferred: heavy resolver, rare/branch-local
            resolve_service_endpoint_with_evidence_gate,
        )

        base_url, _token = resolve_service_endpoint_with_evidence_gate()
    except Exception as exc:  # noqa: BLE001 — best-effort: an unresolvable endpoint means "don't know", not a crash
        _log.debug("serving_engine_version_probe_endpoint_unresolvable", error=str(exc))
        return None

    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/version", timeout=5.0)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 — best-effort: a transport blip or a pre-/version engine means "don't know"
        _log.debug("serving_engine_version_probe_failed", error=scrub_transport_error(exc, base_url))
        return None

    if not isinstance(body, dict):
        return None

    from nexus.engine_version import parse_engine_version  # noqa: PLC0415 — leaf module, rare/branch-local path

    return parse_engine_version(body.get("release_version"))


def _fmt(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def require_note_uri_engine() -> None:
    """Raise :class:`~nexus.errors.NoteUriEngineTooOldError` when the serving engine is below the floor.

    Call it before a note writer's first write (before the catalog register and the T3 write), never
    from a hook that swallows exceptions. A floor of ``(0, 0, 0)`` is unset: return without probing.
    """
    global _cleared_version, _warned_unknown
    from nexus import engine_version as ev  # noqa: PLC0415 — read at call time: tests and the landing bead set the floor

    floor = ev.NOTE_URI_MIN_ENGINE_VERSION
    if floor == (0, 0, 0):
        return
    if _cleared_version is not None:
        return
    serving = probe_serving_engine_version()
    if serving is None:
        with _lock:
            first = not _warned_unknown
            _warned_unknown = True
        if first:
            _log.warning(
                "note_uri_engine_version_unknown",
                floor=_fmt(floor),
                detail=("the serving engine reported no version (a dev jar or an unreachable engine): "
                        "note writes are not refused on version; the backfill guard is the backstop"),
            )
        return
    if serving >= floor:
        with _lock:
            _cleared_version = serving
        return

    from nexus.config import is_local_mode  # noqa: PLC0415 — deferred: config pulls the credential layer
    from nexus.aspect_readers import NOTE_URI_SCHEME  # noqa: PLC0415 — deferred: aspect_readers imports nexus.db.limits, and this module lives in nexus.db

    if ev.REQUIRED_ENGINE_VERSION < floor:
        # The client is ahead of its own pin (develop between the engine-tag push and the paired client
        # release): `nx daemon restart-stale` converges to the pin, which is below the floor, so it would loop.
        remedy = (f"this client is ahead of its pinned engine ({_fmt(ev.REQUIRED_ENGINE_VERSION)}): note writes "
                  f"resume when engine {_fmt(floor)} ships and the client's pin moves to it")
    elif is_local_mode():
        remedy = "run `nx daemon restart-stale` to move the local engine onto the pinned version, then retry"
    else:
        remedy = "the managed engine has not been upgraded yet; retry once it is"
    raise NoteUriEngineTooOldError(
        f"the serving engine is {_fmt(serving)}, older than {_fmt(floor)}, the first version that can hold a "
        f"note's {NOTE_URI_SCHEME} source URI. The note was not written and nothing was registered: {remedy}.",
        serving=serving, floor=floor)


def reset_note_uri_guard_for_tests() -> None:
    """Test-only: forget the cached clearance and the warn-once flag."""
    global _cleared_version, _warned_unknown
    with _lock:
        _cleared_version = None
        _warned_unknown = False


def require_note_uri_engine_for(source_uri: str) -> None:
    """:func:`require_note_uri_engine` for a caller-supplied ``source_uri``: only a note URI, in either
    spelling, can mint the nx-note:// twin the floor exists to prevent, so any other URI passes without
    a probe. For the register paths that take the caller's URI (``nx catalog register --source-uri``
    and the MCP ``catalog_register`` tool)."""
    from nexus.aspect_readers import canonical_note_uri, is_note_uri  # noqa: PLC0415 — deferred: keeps this module's import light

    if source_uri and is_note_uri(canonical_note_uri(source_uri)):
        require_note_uri_engine()
