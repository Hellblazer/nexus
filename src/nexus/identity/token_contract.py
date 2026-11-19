# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The identity token's wire contract, in one place (RDR-253 Step 2, nexus-hczdo.1.2).

Step 2 (the engine's tokens and fences) lands in parallel with this client. What this module assumes of it, and
where each assumption is the RDR's wording and where it is read from Step 2's working tree, is listed here so the
rebase onto Step 2 changes this file and nothing else:

* The token travels in the request header :data:`IDENTITY_TOKEN_HEADER`, beside ``Authorization`` and beside the
  session token's ``X-Nexus-T1-Session`` (Step 2 ``AuthFilter.IDENTITY_HEADER``; the RDR does not name the header).
* ``bind`` (an event after which the caller holds the identity: start, resume, takeover, clear, fork, refresh), the
  ephemeral create and the delegated create answer the identity record plus ``identity_token`` and
  ``identity_token_expires_in_seconds`` (Step 2 ``IdentityHandler.withToken``). The RDR says only "returns an
  identity token carrying identity and generation" and "lives one hour". Against a Step 1 engine neither field is
  present: :func:`token_from_response` then answers ``(None, None)`` and nothing downstream breaks.
* An expired or unknown token is ``401`` (RDR: "on an expired token (HTTP 401) it re-reads the file once").
* A token of a generation a later process took is ``409`` with error code ``generation_superseded`` on a fenced
  write (RDR: "HTTP 409 with error code ``generation_superseded``, distinct from the engine's other 409s"). Step 1's
  bind and renew answer ``generation_conflict`` for the same fact (a stale expected generation); Step 2 moves the
  stale-generation cases of clear, fork, refresh, end and renew onto ``generation_superseded``. Both are
  "stop writing": :data:`STALE_GENERATION_CODES`.
* Checked against Step 2 as committed (``c0128ffdb``, T2 ``nexus/rdr253-continuation-state``): an expired token is ignored
  on ``bind`` and ``renew`` only (this client sends none there) and is a 401 elsewhere; the renew of an identity nobody
  holds is ``409 identity_not_held`` (Step 1 spelled it ``not_held``).
* A fenced write with no token is ``403 identity_token_required``; a token of another identity is
  ``403 not_owner`` (Step 2 ``IdentityFence``). Neither is retried here.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

IDENTITY_TOKEN_HEADER: str = "X-Nexus-Identity-Token"

#: The 409 code of a fenced write from a superseded generation: the client's stop-writing signal.
SUPERSEDED_CODE: str = "generation_superseded"

#: ``403`` code of a fenced write, or a data credential's refresh or fork, that sent no live identity token (engine
#: 0.1.198 gives an expired token no grace). The one spelling the binder, the lease and the delegated child match.
TOKEN_REQUIRED_CODE: str = "identity_token_required"

#: The MCP server refreshes a token whose remaining life is under this many seconds (50 minutes of a one-hour token), so a
#: token holds at least 50 minutes at any moment a sleep can begin, and a sleep under about 50 minutes never lapses it.
REFRESH_BELOW_S: float = 3000.0

#: ``409`` code of a fenced write for an identity nobody holds (Step 2).
NOT_HELD_CODE: str = "identity_not_held"

#: Step 1's spelling of a stale expected generation on bind and renew. A renewal that gets either has been
#: superseded: the process no longer holds the generation it carries.
CONFLICT_CODE: str = "generation_conflict"

STALE_GENERATION_CODES: frozenset[str] = frozenset({SUPERSEDED_CODE, CONFLICT_CODE})

#: A token is not attached to a request in its last seconds of life: the engine would answer 401 for one that
#: expires in flight, and the 401 retry cannot tell that from a revoked one.
EXPIRY_MARGIN_S: float = 5.0


def token_from_response(body: Mapping[str, Any] | None) -> tuple[str | None, float | None]:
    """``(token, lifetime seconds)`` from a bind, ephemeral-create or delegated-create answer; ``(None, None)``
    when the engine sent none (a Step 1 engine, or an event after which the caller holds nothing)."""
    if not body:
        return None, None
    token = body.get("identity_token")
    if not isinstance(token, str) or not token:
        return None, None
    lifetime = body.get("identity_token_expires_in_seconds")
    if isinstance(lifetime, bool) or not isinstance(lifetime, (int, float)):
        return token, None
    return token, float(lifetime)


def expires_at(lifetime_s: float | None, *, now: float | None = None) -> float | None:
    """The wall-clock instant a token of *lifetime_s* seconds stops working, or None when its life is unknown."""
    if lifetime_s is None:
        return None
    return (time.time() if now is None else now) + lifetime_s


def usable(token: str | None, expires: float | None, *, now: float | None = None) -> bool:
    """Whether *token* may be attached to a request now. An unknown expiry is attached: the engine decides."""
    if not token:
        return False
    if expires is None:
        return True
    return (time.time() if now is None else now) + EXPIRY_MARGIN_S < expires


def refresh_below(lifetime_s: float) -> float:
    """Remaining life under which a token of *lifetime_s* is refreshed: :data:`REFRESH_BELOW_S` for the hour the engine
    issues, five sixths of a shorter life (an engine that issues one)."""
    return min(REFRESH_BELOW_S, lifetime_s * REFRESH_BELOW_S / 3600.0)


def is_token_required(status: int, code: str | None) -> bool:
    """A refresh, fork or other fenced write the engine refused for want of a live identity token."""
    return status == 403 and code == TOKEN_REQUIRED_CODE


def is_expired(status: int) -> bool:
    return status == 401


def is_superseded(status: int, code: str | None) -> bool:
    """A fenced write refused because the token's generation is no longer the identity's (stop writing)."""
    return status == 409 and code == SUPERSEDED_CODE


def is_stale_generation(status: int, code: str | None) -> bool:
    """A renewal, refresh, clear, fork or end refused because the expected generation is stale (either spelling)."""
    return status == 409 and code in STALE_GENERATION_CODES


def headers(token: str | None) -> dict[str, str]:
    return {IDENTITY_TOKEN_HEADER: token} if token else {}
