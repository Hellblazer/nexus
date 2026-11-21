# SPDX-License-Identifier: AGPL-3.0-or-later
"""Registering a NEW collection by its attributes and writing to the label the engine minted (RDR-229 P3.8,
nexus-p2m1x.28).

The client renders no name for a new collection. It registers ``(content_type, owner_id, embedding_model,
model_version)``; the engine mints ``collection_id`` and the label ``c<id>`` and answers the row (or the live row the
tuple already has). A writer then writes to that label. This module sits beside
:func:`nexus.corpus.ensure_collection_registered` and leaves its cache and retry structure untouched: it only seeds the
cache's existing entry for the label it just registered, so the write path's own ``ensure_collection_registered(label)``
is a hit and never re-registers a prefix-free label by deriving attributes from it.

Two process-local facts follow from a registration that has no chunk yet:

* the engine's listing (the ``mcp_infra`` row cache) names only a collection that holds a chunk, so the first write
  into the new collection reads its model from a name that carries none. :func:`minted_row` holds the row this process
  registered, and :func:`nexus.mcp_infra.get_collection_row` answers it while the listing does not name the label.
  The listing wins as soon as it does, and an entry never outlives :data:`_ROW_TTL_SECONDS`;
* a label is registered in this process from the moment the call returns (the registration cache).
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from nexus.catalog.collection_name import CollectionAttributes

_log = structlog.get_logger(__name__)

#: How long this process answers a row it registered while the engine's listing does not name it. A first chunk
#: write lists the collection, so the entry is needed for the length of one write; five minutes is slack, not a cache.
_ROW_TTL_SECONDS: float = 300.0

_rows: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def minted_row(name: str) -> dict | None:
    """The row this process registered for *name*, in the shape of an ``mcp_infra`` listing row, or ``None``."""
    with _lock:
        held = _rows.get(name)
        if held is None:
            return None
        if time.monotonic() >= held[0]:
            del _rows[name]
            return None
        return dict(held[1])


def forget(name: str) -> None:
    """Drop *name*'s row (a rename or supersede retires the label)."""
    with _lock:
        _rows.pop(name, None)


def forget_all() -> None:
    with _lock:
        _rows.clear()


def _remember(row: CollectionAttributes) -> None:
    _hold(row.label, {
        "content_type": row.content_type, "owner_id": row.owner_id,
        "embedding_model": row.embedding_model, "lifecycle_state": row.lifecycle_state or "live",
    })


def _hold(label: str, row: dict) -> None:
    now = time.monotonic()
    with _lock:
        for stale in [n for n, (expires, _) in _rows.items() if expires <= now]:
            del _rows[stale]
        _rows[label] = (now + _ROW_TTL_SECONDS, row)


def note_registered(label: str, row: dict) -> None:
    """A label registered or found BY NAME (a recovery-bundle import keeps the bundle's label, RDR-229 P3.9), whose
    catalog *row* (the registry read: ``content_type``, ``owner_id``, ``embedding_model``, ``lifecycle_state``) has
    no chunk yet. The same two facts :func:`register_by_attributes` leaves behind: the row answers
    :func:`nexus.mcp_infra.get_collection_row` until the listing names the label, so the first note's chunks carry
    the row's model and not the label's own token; and the label is registered in this process, so the write path's
    ``ensure_collection_registered`` is a hit and never derives attributes from a label that may carry none."""
    from nexus.corpus import _registration_cache_add  # noqa: PLC0415 — deferred: nexus.corpus imports back into the catalog
    from nexus.mcp_infra import invalidate_collections_cache  # noqa: PLC0415 — deferred: mcp_infra imports this module's reader

    _hold(label, {k: row.get(k) or "" for k in ("content_type", "owner_id", "embedding_model")}
          | {"lifecycle_state": row.get("lifecycle_state") or "live"})
    _registration_cache_add(None, label)
    invalidate_collections_cache()


def register_by_attributes(
    *, content_type: str, owner_id: str, embedding_model: str, model_version: str = "v1",
    registrar: "Callable[[], object] | None" = None,
) -> CollectionAttributes:
    """Register a collection by its attributes and answer the row the engine minted for it.

    *registrar* is a zero-arg factory returning a catalog writer, as for
    :func:`nexus.corpus.ensure_collection_registered` (default :func:`nexus.catalog.factory.make_catalog_writer`); its
    optional ``scope`` selects the registration cache partition. The profile check
    :func:`~nexus.corpus.ensure_collection_registered` makes before a new name runs here too: an *embedding_model* the
    engine's profile disagrees with raises :class:`~nexus.corpus.EmbeddingProfileMismatchError` before the wire call.
    An engine below the mint's release raises :class:`~nexus.catalog.engine_writes.floors.CollectionMintUnsupportedError`
    and nothing is written. Any other failure propagates; nothing falls back to rendering a name.

    Raises ``TypeError`` for an answer that is not a row, and ``ValueError`` for a row of another tuple.
    """
    from nexus.catalog.collection_name import CollectionAttributes  # noqa: PLC0415 — deferred: nexus.corpus imports this module and the catalog imports nexus.corpus
    from nexus.corpus import (  # noqa: PLC0415 — deferred: nexus.corpus imports back into the catalog
        EmbeddingProfileMismatchError,
        _profile_model_for_content_type,
        _registration_cache_add,
    )
    from nexus.mcp_infra import invalidate_collections_cache  # noqa: PLC0415 — deferred: mcp_infra imports this module's reader

    profile_model = _profile_model_for_content_type(content_type)
    if profile_model is not None and profile_model != embedding_model:
        raise EmbeddingProfileMismatchError(content_type, embedding_model, profile_model)
    scope = getattr(registrar, "scope", None)
    if registrar is None:
        from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred: circular-dep avoidance (catalog)

        registrar = make_catalog_writer
    writer = registrar()
    try:
        row = writer.register_collection(  # type: ignore[attr-defined]
            content_type=content_type, owner_id=owner_id,
            embedding_model=embedding_model, model_version=model_version,
        )
    finally:
        writer.close()  # type: ignore[attr-defined]
    if not isinstance(row, CollectionAttributes) or not isinstance(row.label, str) or not row.label:
        raise TypeError(f"registering by attributes answered {type(row).__name__}, not a collection row with a label")
    if (row.content_type, row.owner_id, row.embedding_model) != (content_type, owner_id, embedding_model):
        raise ValueError(
            f"registering ({content_type!r}, {owner_id!r}, {embedding_model!r}) answered {row.label!r} for "
            f"({row.content_type!r}, {row.owner_id!r}, {row.embedding_model!r})"
        )
    _remember(row)
    _registration_cache_add(scope, row.label)
    invalidate_collections_cache()
    _log.info(
        "collection_registered_by_attributes",
        label=row.label, collection_id=row.collection_id, content_type=content_type, owner_id=owner_id,
    )
    return row


def mint_knowledge_subject_label(subject: str, *, registrar: "Callable[[], object] | None" = None) -> str:
    """The label of a NEW knowledge subject, registered by attributes (``owner_id`` is the subject, RDR-229 item 8).

    The model is :func:`~nexus.corpus.effective_embedding_model_for_writes`, the strict write-time one: it raises when
    ``local.embed_model`` is voyage-shaped and no key is configured, rather than registering a model nothing can embed.
    """
    from nexus.corpus import effective_embedding_model_for_writes  # noqa: PLC0415 — deferred: nexus.corpus imports back into the catalog

    return register_by_attributes(
        content_type="knowledge", owner_id=subject,
        embedding_model=effective_embedding_model_for_writes("knowledge"), model_version="v1", registrar=registrar,
    ).label
