# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one rule for "did this failed write request possibly reach and change the engine".

Every RDR-223 writer that has to act on a failed request asks the same question: a note writer
(``note_write``: settle from the manifest, or report a confirmed refusal), and the per-file
writers' registration latch (``MetadataMergingCatalog``: keep a freshly minted document, or roll it
back). Two hand-rolled copies of the answer disagreed on 6 of 21 inputs while both suites stayed
green (RDR-223 Phase 2 gate, nexus-z0o2p.35, finding I2), so there is one rule here and both callers
import it.

**"May have written" means "in flight".** A request is in flight unless it positively did not reach
the engine, or the engine positively refused it. The cost of a wrong "in flight" is a leftover
failed registration that a rerun heals; the cost of a wrong "nothing was written" is a rollback of
chunks that landed, which leaves them without an owner.

Shapes (:func:`judge`):

* :data:`UNSENT`: the request never left the client. A connection that was never made
  (``ConnectError``, ``ConnectTimeout``), a connection pool with no free slot (``PoolTimeout``: the
  request is queued for a connection BEFORE any byte is sent), a refusal the client raises before it
  sends (:func:`client_side_refusals`), or an argument check that is named explicitly as one
  (:class:`PreSendArgumentError`).
* :data:`REFUSED`: the engine answered with a 4xx other than 408 and did not commit.
* :data:`IN_FLIGHT`: everything else. A 408, a 5xx, a dropped or reset connection, an embed
  timeout, a failure the client raised while reading the ANSWER (a ``ValueError``, ``TypeError``,
  ``UnicodeDecodeError`` or ``JSONDecodeError`` after a 2xx: the engine had already committed), and
  an exception nobody anticipated. A ``ValueError`` is NOT unsent because of its type: only a check
  raised as :class:`PreSendArgumentError` is.

The WHOLE cause/context chain is judged. The refreshable client retries once from inside its own
``except`` block, so when attempt 1 committed and its response was reset and the retry then fails
cleanly (a connect error while the service restarts, a 401), the exception that propagates is the
retry's, with attempt 1's as ``__context__``. The retry's clean refusal says nothing about attempt 1.
Precedence over the chain: any in-flight node makes it in flight; else any 4xx makes it refused;
else it is unsent, which needs at least one failed connect, pool timeout or client-side refusal
and no other transport node. A chain of nothing recognisable is in flight.
"""
from __future__ import annotations

import httpx

from nexus.errors import CombinedWriteEmbedTimeoutError

__all__ = [
    "IN_FLIGHT", "REFUSED", "REFUSED_BY_CLIENT", "REFUSED_BY_ENGINE", "REFUSED_UNREACHABLE", "UNSENT",
    "PreSendArgumentError", "client_side_refusals", "judge", "may_have_written",
]

#: The request never reached the engine, so nothing of it can commit.
UNSENT = "unsent"
#: The engine answered with a definitive 4xx refusal, so the transaction did not commit.
REFUSED = "refused"
#: Anything else: the request may have reached the engine and may have committed or still commit.
IN_FLIGHT = "in-flight"

#: Who refused a request that did not land (``NoteWriteError.refusal``, ``PutNoteOutcome.refusal``).
REFUSED_BY_CLIENT = "client"
REFUSED_UNREACHABLE = "unreachable"
REFUSED_BY_ENGINE = "engine"


class PreSendArgumentError(ValueError):
    """An argument check the client ran BEFORE it sent anything (a blank collection, an over-cap
    list, delete keys without merge mode). The only ``ValueError`` the classifier calls unsent: a
    plain ``ValueError`` may be a failure to parse the engine's answer after it committed, so the
    type alone proves nothing. Raise this, not ``ValueError``, from a check that precedes the POST."""


def client_side_refusals() -> tuple[type[BaseException], ...]:
    """Refusals the client raises BEFORE it sends: ``write_manifest_many`` registers the collection
    first, and registration refuses a profile that disagrees with the engine's, a voyage intent with
    no key, and a retired collection name; and an argument check named :class:`PreSendArgumentError`.
    Nothing reached the engine. (Imported at call time: ``nexus.corpus`` imports back into the
    catalog package.)"""
    from nexus.collection_errors import SupersededCollectionWriteError  # noqa: PLC0415 — deferred: circular-dep avoidance
    from nexus.corpus import EmbeddingProfileMismatchError, LocalVoyageCredentialMissingError  # noqa: PLC0415 — deferred: circular-dep avoidance

    return (
        EmbeddingProfileMismatchError, LocalVoyageCredentialMissingError,
        SupersededCollectionWriteError, PreSendArgumentError,
    )


def judge(exc: BaseException) -> tuple[str, str]:
    """``(shape, refusal)`` of one failed write attempt.

    *shape* is one of :data:`UNSENT`, :data:`REFUSED`, :data:`IN_FLIGHT`. *refusal* says who refused
    when the shape is definitive (:data:`REFUSED_BY_ENGINE` for a 4xx, else :data:`REFUSED_BY_CLIENT`
    when a node of the chain is a client-side refusal, else :data:`REFUSED_UNREACHABLE`) and is
    ``""`` for an attempt in flight. See the module docstring for the rule and its precedence.
    """
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    refused = unsent = client_side = False
    client_refusals = client_side_refusals()
    while pending:
        cur = pending.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, CombinedWriteEmbedTimeoutError):
            return IN_FLIGHT, ""
        if isinstance(cur, httpx.HTTPStatusError):
            status = cur.response.status_code
            if 400 <= status < 500 and status != 408:
                refused = True
            else:
                return IN_FLIGHT, ""
        elif isinstance(cur, client_refusals):
            unsent = client_side = True
        elif isinstance(cur, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            unsent = True
        elif isinstance(cur, httpx.TransportError):
            return IN_FLIGHT, ""
        pending.extend(n for n in (cur.__cause__, cur.__context__) if n is not None)
    if refused:
        return REFUSED, REFUSED_BY_ENGINE
    if unsent:
        return UNSENT, REFUSED_BY_CLIENT if client_side else REFUSED_UNREACHABLE
    return IN_FLIGHT, ""


def may_have_written(exc: BaseException) -> bool:
    """True when the request that raised *exc* is in flight under :func:`judge`: it may have reached
    the engine and committed, or may still commit."""
    return judge(exc)[0] == IN_FLIGHT
