# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who may write ``identity/<id>``: that seat, and nobody else (RDR-253 Step 6 part 3, nexus-hczdo).

An ephemeral identity ends with its session and a delegated one with its child, so notes written under either orphan at
once, and another seat's notes would reach that seat's SessionStart line and its ``nx brief me``. The engine fences only the
``threads`` project and takes a write to ``identity/<id>`` from any caller, so **the refusal is the client's, and only the
client's**: ``identity/<id>`` is client-guarded, not engine-fenced. A caller that never goes through
:class:`~nexus.db.t2.http_memory_store.HttpMemoryStore` (raw HTTP, a process with no Claude ancestor) is not stopped.

:func:`check_write` runs first in every project-keyed write of the store: ``put``, ``put_or_merge``, ``put_replacing``,
``import_entry``, ``import_entries_batch`` (each row's project), ``delete`` by project and title, and ``insert_summary``. The
id-keyed paths (``delete`` by id, ``merge_memories``, ``restore``) name a row, not a project, and are NOT covered.

* The caller is the identity its token FILE names, whether or not the token has lapsed
  (:func:`~nexus.identity.token_header.current` answers None for a lapsed token, which is not "no identity"). A process
  whose Claude ancestor has no token file at all (a person at a shell) holds no identity and is not refused.
* A delegated child is known from its environment (``NX_IDENTITY_TOKEN``): refused, no lookup.
* Any other caller asks the engine for its identity's ``kind`` and is refused when it is not ``seat``, or when the kind
  cannot be learned. Only ``seat`` is remembered for the process (a promote takes effect without a restart); a failed
  lookup is never a verdict.
* A seat writes only ``identity/<its own id>``.
* A refusal is one line, logged (``identity_notes_write_refused``), raised as :class:`IdentityNotesRefused`, which the CLI
  and the MCP tool report as an error. Nothing retries it.
"""
from __future__ import annotations

import os
import threading

import structlog

from nexus.db.t2.http_memory_store import IDENTITY_PROJECT_PREFIX

__all__ = ["IdentityNotesRefused", "check_write", "reset_cache", "write_refusal"]

_log = structlog.get_logger(__name__)

_LOCK = threading.Lock()
_KINDS: dict[str, str] = {}


class IdentityNotesRefused(RuntimeError):
    """A write to ``identity/<id>`` from a caller that is not a seat."""


def reset_cache() -> None:
    """Forget every kind learned (tests)."""
    with _LOCK:
        _KINDS.clear()


def _refuse(caller: str, reason: str) -> IdentityNotesRefused:
    _log.warning("identity_notes_write_refused", identity=caller, reason=reason)
    return IdentityNotesRefused(reason)


def _kind_of(identity: str) -> str:
    """*identity*'s kind from the engine, remembered for the process once learned; ``""`` when it could not be learned."""
    with _LOCK:
        known = _KINDS.get(identity)
    if known:
        return known
    from nexus.identity.bind_client import IdentityApiError, IdentityClient, IdentityUnavailableError  # noqa: PLC0415 — deferred: cold path

    try:
        client = IdentityClient.from_config()
        try:
            kind = str(client.show(identity).get("kind") or "")
        finally:
            client.close()
    except (IdentityApiError, IdentityUnavailableError, OSError, RuntimeError) as exc:
        _log.warning("identity_notes_kind_lookup_failed", identity=identity, error=f"{type(exc).__name__}: {exc}")
        return ""
    if kind == "seat":     # only a seat is remembered: an ephemeral may be promoted while this process runs
        with _LOCK:
            _KINDS[identity] = kind
    return kind


def _caller() -> str:
    """The identity this process's token file names, lapsed token or not; ``""`` when it has no token file."""
    from nexus.identity import token_header  # noqa: PLC0415 — deferred: only an identity project pays
    from nexus.identity.token_file import read_token_file  # noqa: PLC0415

    path = token_header.token_file_path()
    record = read_token_file(path) if path is not None else None
    return str(record.identity) if record is not None else ""


def write_refusal(project: str) -> tuple[str, str] | None:
    """``(caller, reason)`` when the caller may not write *project*, else ``None``. Side-effect free apart from the kind
    lookup it may make: it logs no refusal, so a reader that only wants to know (``nx brief me``) does not leave
    ``identity_notes_write_refused`` lines for a write nobody tried. :func:`check_write` raises from the same answer, so the
    two cannot drift. A project that is not ``identity/<id>`` costs nothing: no import, no lookup."""
    if not project.startswith(IDENTITY_PROJECT_PREFIX) or len(project) == len(IDENTITY_PROJECT_PREFIX):
        return None
    caller = _caller()
    if os.environ.get("NX_IDENTITY_TOKEN", "").strip():
        return caller, ("a delegated identity keeps no identity notes (they would orphan when the child ends); "
                        "write the note in project T2")
    if not caller:
        return None
    kind = _kind_of(caller)
    if not kind:
        return caller, (f"could not confirm that {caller} is a seat, so its identity notes are not written "
                        "(the identity engine did not answer); try again")
    if kind != "seat":
        return caller, (f"this {kind} identity keeps no identity notes (they would orphan when its session ends); "
                        "only a seat does: write the note in project T2, or start the session with a seat")
    own = f"{IDENTITY_PROJECT_PREFIX}{caller}"
    if project != own:
        return caller, f"a seat writes only its own identity notes ({own}), not {project}"
    return None


def check_write(project: str) -> None:
    """Raise :class:`IdentityNotesRefused` when the caller may not write *project*, logging the refusal."""
    refusal = write_refusal(project)
    if refusal is not None:
        raise _refuse(*refusal)
