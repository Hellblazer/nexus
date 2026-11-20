# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who may write ``identity/<id>``: a seat, and no ephemeral or delegated identity (RDR-253 Step 6 part 3, nexus-hczdo).

An ephemeral identity ends with its session and a delegated one with its child, so notes written under either orphan
at once. The engine fences only the ``threads`` project and takes a write to ``identity/<id>`` from any caller, so the
refusal is the client's: :meth:`~nexus.db.t2.http_memory_store.HttpMemoryStore.put`, ``put_or_merge`` and
``put_replacing`` call :func:`check_write` before any request, which covers the CLI and the MCP tool alike.

* A delegated child is known from its environment (``NX_IDENTITY_TOKEN`` names its token file): no lookup.
* Any other caller with an identity asks the engine once per process for the identity's ``kind`` and is refused when it is
  not ``seat``. A kind that cannot be learned refuses too: the write is a durable note and the caller can try again once
  the engine answers. A failed lookup is never cached as a verdict.
* A process with no identity (a person at a shell) is not refused: the engine does not fence the project, and a person is
  not an ephemeral session.
* A refusal is one line, logged (``identity_notes_write_refused``), and raised as :class:`IdentityNotesRefused`, which the
  CLI and the MCP tool report as an error. Nothing retries it.
"""
from __future__ import annotations

import os
import threading

import structlog

from nexus.db.t2.http_memory_store import IDENTITY_PROJECT_PREFIX

__all__ = ["IdentityNotesRefused", "check_write", "reset_cache"]

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
    if kind:
        with _LOCK:
            _KINDS[identity] = kind
    return kind


def check_write(project: str) -> None:
    """Raise :class:`IdentityNotesRefused` when the caller may not write *project*. A project that is not
    ``identity/<id>`` costs nothing: no import, no lookup."""
    if not project.startswith(IDENTITY_PROJECT_PREFIX) or len(project) == len(IDENTITY_PROJECT_PREFIX):
        return
    from nexus.identity import token_header  # noqa: PLC0415 — deferred: only an identity project pays

    record = token_header.current()
    caller = str(getattr(record, "identity", "") or "")
    if os.environ.get("NX_IDENTITY_TOKEN", "").strip():
        raise _refuse(caller, "a delegated identity keeps no identity notes (they would orphan when the child ends); "
                              "write the note in project T2 or in your parent's own session")
    if record is None:
        return
    kind = _kind_of(caller)
    if not kind:
        raise _refuse(caller, f"could not confirm that {caller} is a seat, so its identity notes are not written "
                              "(the identity engine did not answer); try again")
    if kind != "seat":
        raise _refuse(caller, f"this {kind} identity keeps no identity notes (they would orphan when its session ends); "
                              "only a seat does: write the note in project T2, or start the session with a seat")
