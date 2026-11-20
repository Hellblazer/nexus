# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The identity T1 scope (RDR-253 "Identity scratch and per-identity T2"; Step 6, nexus-hczdo.1.9).

Scratch that must cross ``/clear`` and restart but is not worth a permanent record lives in the T1 scope whose
``session_id`` is ``identity:<id>``. It needs no table: the engine's ``ScratchHandler`` accepts that scope, fences its
writes (the owner's token, the current generation), leaves its reads open, and never closes it with a session. This module
is the client half: the scope a caller reads and writes, and a store bound to it.

* The scope is the caller's own identity. A delegated child (``claude -p``) reads its **parent's** scope: its writes
  are refused by the engine (the child's token is not the parent's), which is the point, and its reads are not checked.
* The store is a plain :class:`~nexus.db.http_scratch_store.HttpScratchStore` bound to the identity scope as its
  ``session_id`` and sending **no** ``X-Nexus-T1-Session`` header: the engine admits a request with none, leaves the
  scope's reads open and fences its writes on the identity token alone, which ``HttpScratchStore`` attaches itself for an
  ``identity:`` scope. So the store needs no session lease and mints nothing: a ``/clear`` start can read it before the
  new session id has a lease (nexus-hczdo).
* The scope is bounded by the client, not the engine: an entry over 2 KB is refused, and so is a non-pointer put once the
  scope holds 20 entries (the resume pointer has its own slot); nothing is evicted. The rule is in
  :mod:`nexus.db.http_scratch_store` (``IDENTITY_ENTRY_MAX_BYTES``, ``IDENTITY_SCOPE_MAX_ENTRIES``).
* A process with no identity token holds no scope: asking for it is an error naming that, never a quiet fall back to the
  session scope (an entry written to the wrong scope is lost to the reader who looks in the right one).
"""
from __future__ import annotations

from typing import Any

IDENTITY_SCOPE_PREFIX = "identity:"


class IdentityScopeError(RuntimeError):
    """The caller holds no identity, so it has no identity scratch scope."""


def scope_owner(client: Any, *, identity: str | None = None) -> str:
    """The identity whose scratch the caller works in: its own, or the parent's for a delegated child.

    *client* answers ``show(identity)`` (an :class:`~nexus.identity.bind_client.IdentityClient`). *identity* overrides
    the caller's own (tests). Raises :class:`IdentityScopeError` when no identity is bound to this process."""
    if identity is None:
        from nexus.identity import token_header  # noqa: PLC0415 — deferred: leaf import cost

        record = token_header.current()
        if record is None and token_header.has_claude_ancestor():
            raise IdentityScopeError(
                "this process's identity token file not readable yet (its Claude process has no token file, or it is being "
                "rewritten, as it is while Claude restarts): try again in a moment; if it stays so, the session holds no identity")
        if record is None:
            raise IdentityScopeError(
                "no identity is bound to this process, so it has no identity scratch (it needs a Claude session "
                "with a bound identity, or a delegated child of one)")
        identity = record.identity
    view = client.show(identity)
    parent = view.get("parent") if view.get("kind") == "delegated" else None
    return str(parent or identity)


def scope_id(owner: str) -> str:
    """The T1 ``session_id`` of *owner*'s identity scope."""
    return f"{IDENTITY_SCOPE_PREFIX}{owner}"


def identity_scratch(base: Any = None, *, client: Any = None, identity: str | None = None) -> Any:
    """A T1-shaped store bound to the caller's identity scope.

    The store sends no session token, so it needs no session lease and never creates a session. *base* is accepted for
    old callers and ignored: nothing is borrowed from it (none passes one now). *client* is the identity client that finds the delegated
    parent (built from the config when None, and closed afterwards)."""
    from nexus.db.http_scratch_store import HttpScratchStore  # noqa: PLC0415 — deferred: http store must not load on every CLI start
    from nexus.identity.bind_client import IdentityApiError, IdentityClient, IdentityUnavailableError  # noqa: PLC0415

    owned = client is None
    try:
        if owned:
            client = IdentityClient.from_config()
        owner = scope_owner(client, identity=identity)
    except IdentityUnavailableError as exc:
        raise IdentityScopeError(f"the identity engine is not answering, so the identity scratch cannot be reached ({exc})") from exc
    except IdentityApiError as exc:
        raise IdentityScopeError(f"the engine refused the identity lookup for identity scratch: {exc.code or exc.status}: {exc.message}") from exc
    finally:
        if owned and client is not None:
            client.close()
    # send_session_token=False: the engine needs no session for an identity-scope call (see the module docstring), and
    # a lease for this process's session id may not exist yet. export_adopted_token=False: this store is not the
    # process's own scope, so nothing it adopts may replace the NX_T1_SESSION of the scope the process holds.
    return HttpScratchStore(session_id=scope_id(owner), send_session_token=False, export_adopted_token=False)
