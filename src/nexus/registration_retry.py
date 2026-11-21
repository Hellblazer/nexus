# SPDX-License-Identifier: AGPL-3.0-or-later
"""The retry-once half of the unregistered-collection 422 (RDR-229 P3.1, nexus-p2m1x.17).

After P3.1 the engine refuses a write under a collection with no registry row with a typed 422
(``UnregisteredCollectionException``, reason ``unregistered_collection``). This process caches a
name as registered (``nexus.corpus._REGISTERED_COLLECTIONS``), and another process can delete the
row afterwards, so a long-lived writer (an MCP server) can send a write the engine now refuses.

:func:`post_with_registration_retry` is the one recovery every writer shares, installed in the POST
of ``RefreshableHttpStoreMixin`` (every catalog, pipeline and T2 store write goes through it). On
that 422 it invalidates the name in the cache, registers it again and sends the write once more; a
second refusal propagates, never a third send. ``corpus.write_with_registration_retry`` (the chunk,
aspect, taxonomy and chash writers) is the same recovery with a registration in front, and sets
:data:`REGISTRATION_RETRY_ACTIVE` so the two never stack.

It retries only a name this process itself cached: a name it never registered (a writer that skipped
registration, a typo in a rename or supersede) keeps its 422, because registering it here would
create a collection nobody asked for.

A leaf module: ``nexus.corpus`` imports from here at the top, and every function here imports
``nexus.corpus`` lazily.
"""
from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, TypeVar

import structlog

_log = structlog.get_logger(__name__)
_T = TypeVar("_T")

#: True while a registration retry is running in this context: a nested write must not retry again.
REGISTRATION_RETRY_ACTIVE: ContextVar[bool] = ContextVar("nexus_registration_retry_active", default=False)


def unregistered_collection_name(exc: BaseException) -> str | None:
    """The ``collection`` the engine's typed unregistered-collection 422 named, or ``None`` when *exc*
    is any other failure (or the body names no collection).

    Two HTTP-error families reach a writer, as in ``corpus._looks_like_stale_registration_error``:
    ``httpx.HTTPStatusError`` (``.response``) and the vector client's ``VectorServiceError``
    (``.engine_body``)."""
    from nexus import corpus  # noqa: PLC0415 — circular-dep avoidance (corpus)

    if not corpus._looks_like_stale_registration_error(exc):
        return None
    body: Any = getattr(exc, "engine_body", None)
    if body is None:
        response = getattr(exc, "response", None)
        try:
            body = response.json() if response is not None else None
        except Exception:  # noqa: BLE001 — a body that cannot be read names no collection
            body = None
    name = body.get("collection") if isinstance(body, dict) else None
    return name if isinstance(name, str) and name else None


def registration_cached_anywhere(name: str, registrar: "Callable[[], object] | None" = None) -> bool:
    """True when this process holds *name* as registered in the *registrar*'s cache partition or in any
    other one (a name one writer registered through the ambient registrar and another writes through its
    own endpoint-pinned one is still a name this process registered)."""
    from nexus import corpus  # noqa: PLC0415 — circular-dep avoidance (corpus)

    if corpus._registration_cache_state(getattr(registrar, "scope", None), name) != "absent":
        return True
    with corpus._REGISTERED_COLLECTIONS_LOCK:
        return name in corpus._REGISTERED_COLLECTIONS or any(
            e[1] == name for e in corpus._REGISTERED_COLLECTIONS_SCOPED)


def register_collection_for_write(name: str, registrar: "Callable[[], object] | None" = None) -> None:
    """Register *name* in the catalog's collection registry before a write that needs its row.

    The ONE call every registration that precedes a catalog document goes through (RDR-229 P3.1,
    nexus-p2m1x.17): since P3.1 the engine refuses a document under a collection with no registry row
    (422 ``unregistered_collection``), so ``put_note``, the ``.nxexp`` import pre-pass, the ``doc_indexer``
    pre-flight, the 422 retry below and the ``HttpCatalogClient`` manifest writes each register first. They
    share this body so the collection-name census holds one site for the seam, and P3.8's ``collection_mint``
    replaces one function. It adds no behaviour of its own: ``corpus.ensure_collection_registered`` decides
    (per-process cache, the client's profile, retired-name and credential checks).

    A caller that needs a collection the user typed to be REFUSED when unregistered (an MCP tool, a CLI
    argument) does not call this: the typed 422 is the right answer there. So do the callers that read the
    collection from T3's own listing (``orphan_backfill``, the catalog backfill passes) and
    ``HttpCatalogClient.register_many``, which registers no collection and has no caller in this tree.
    """
    from nexus import corpus  # noqa: PLC0415 — circular-dep avoidance (corpus)

    corpus.ensure_collection_registered(name, registrar=registrar)


def reregister_after_unregistered(
    name: str, exc: BaseException, registrar: "Callable[[], object] | None" = None,
) -> None:
    """Repair a cached registration the engine refused (the typed unregistered-collection 422 *exc*):
    invalidate *name* in the cache and register it again, so the caller can send its write ONCE more.
    The recovery ``corpus.write_with_registration_retry`` and :func:`post_with_registration_retry` share.

    Refuses instead of registering a quarantine sibling (the client never registers one:
    ``QuarantineSiblingNotRegisteredError``) and a retired name (the re-registration is an upsert and
    would revive it: ``SupersededCollectionWriteError``).
    """
    from nexus import corpus  # noqa: PLC0415 — circular-dep avoidance (corpus)

    scope = getattr(registrar, "scope", None)
    if corpus._is_quarantine_sibling(name):
        # The funnel never registers a sibling (see
        # ensure_collection_registered), so a retry cannot repair
        # this; name the reason instead of the engine's bare 422.
        raise corpus.QuarantineSiblingNotRegisteredError(
            f"{name!r} is not registered and the client never registers "
            "a quarantine sibling: the engine's GC function registers it "
            "from the origin collection's row when it first quarantines a "
            "chunk, so a sibling that does not exist has nothing to "
            "write into. Check the origin collection and `nx t3 gc`."
        ) from exc
    # nexus-wwuzp: the re-registration below is an upsert, which clears
    # ``superseded_by``. A retired name must be refused, never revived by
    # a retry. A read failure propagates rather than falling open: the
    # alternative is reviving a tombstone on a guess.
    successor = corpus._superseded_successor(corpus._read_collection_row(name, registrar))
    if successor:
        # Keep the entry, stale, so the next call takes the revalidation
        # read and is refused again (and an in-flight read cannot re-stamp
        # it: expire bumps the generation).
        with corpus._REGISTERED_COLLECTIONS_LOCK:
            corpus._registration_cache_set(scope).expire(corpus._registration_cache_key(scope, name))
        raise corpus.SupersededCollectionWriteError(name, successor) from exc
    _log.info("collection_registration_stale_after_boot_sweep_retry", name=name)
    with corpus._REGISTERED_COLLECTIONS_LOCK:
        # P3.1 INVALIDATION POINT (nexus-p2m1x.17): the ONE place a name leaves the registration cache once
        # the engine refused it. A sibling cache hooks here: P3.8's collection_mint forget(name) goes beside
        # this line (colophon adds it); no other site discards on a refusal.
        corpus._registration_cache_discard(scope, name)
    from nexus import collection_mint  # noqa: PLC0415 — circular-dep avoidance (collection_mint imports corpus)

    collection_mint.forget(name)  # RDR-229 P3.8: the minted-row cache must not outlive the engine's refusal
    register_collection_for_write(name, registrar)


def retry_write_once_on_unregistered(
    send: "Callable[[], _T]", *, registrar: "Callable[[], object] | None" = None,
) -> "_T":
    """Run *send*; on the typed unregistered-collection 422 for a name this process cached, invalidate
    it, register it again and run *send* ONCE more. Anything else, and a second refusal, propagates.

    *send* is one request the engine refuses before it writes anything, so the resend cannot apply a
    write twice. Nested inside ``corpus.write_with_registration_retry`` it is a pass-through."""
    if REGISTRATION_RETRY_ACTIVE.get():
        return send()
    try:
        return send()
    except Exception as exc:  # noqa: BLE001 — narrowed below; anything but the one typed 422 is re-raised unchanged
        name = unregistered_collection_name(exc)
        if name is None or not registration_cached_anywhere(name, registrar):
            raise
        token = REGISTRATION_RETRY_ACTIVE.set(True)
        try:
            reregister_after_unregistered(name, exc, registrar)
        finally:
            REGISTRATION_RETRY_ACTIVE.reset(token)
    return send()


def post_with_registration_retry(store: Any, path: str, kwargs: dict[str, Any], *, mutates: bool) -> Any:
    """``store._send("POST", path, **kwargs)``, with :func:`retry_write_once_on_unregistered` around a
    WRITE (``mutates``); a read-shaped POST never registers anything. The one line
    ``RefreshableHttpStoreMixin._post`` calls."""
    if not mutates:
        return store._send("POST", path, **kwargs)
    return retry_write_once_on_unregistered(
        lambda: store._send("POST", path, **kwargs), registrar=store._catalog_registrar)
