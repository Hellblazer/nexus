# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""One owner for memory-entry attribution fallbacks (nexus-aqbrk).

``memory_put``'s ``agent`` / ``session`` parameters are documented as
falling back when the caller leaves them unset — ``agent``: "When empty,
falls back to ``NX_AGENT`` env, then NULL"; ``session``: "falls back to the
parent's session_id resolution chain (``NX_SESSION_ID`` env → claude session
file → NULL)". That is the whole point of RDR Phase 1B (nexus-9clx): before
it, 1012 of 1012 production rows had both columns NULL, so ``nx tier-status``
could not slice T2 writes by which agent did the persisting.

:class:`~nexus.db.t2.memory_store.MemoryStore` resolved the chain inline.
:class:`~nexus.db.t2.http_memory_store.HttpMemoryStore` did not — it only
forwarded values it was handed — so in SERVICE mode both columns went back to
NULL for every caller relying on the documented env fallback, silently
undoing nexus-9clx. Found by the RDR-158/155 substrate port
(tests/test_memory_put_attribution.py).

Same failure shape as ``HttpPlanLibrary.set_scope_tags`` (nexus-aqbrk,
commit 5fe9efe6): a client-side omission in the service twin of a method
whose local twin compensates, not an engine gap. Fixed the same way — give
the logic ONE owner instead of a second copy.

NOT for ``import_*`` methods. Those write their inputs VERBATIM by contract
(a migrating tenant's rows must land exactly as they were), so an import that
silently stamped the CURRENT process's agent would corrupt the migration.
"""
from __future__ import annotations

import os

__all__ = ["resolve_attribution"]


def resolve_attribution(
    agent: str | None, session: str | None,
) -> tuple[str | None, str | None]:
    """Apply the documented ``agent`` / ``session`` fallback chain.

    Returns the pair to persist. An explicitly-supplied value always wins;
    only ``None`` triggers a fallback, so a caller that deliberately passes a
    value is never overridden by ambient env.

    ``session`` defers to :func:`nexus.session.read_claude_session_id`, which
    owns the ``NX_SESSION_ID`` env → claude-session-file → ``None`` order.
    """
    from nexus.session import read_claude_session_id  # noqa: PLC0415 — deferred to avoid circular import (session)

    if agent is None:
        agent = os.environ.get("NX_AGENT")
    if session is None:
        session = read_claude_session_id()
    return agent, session
