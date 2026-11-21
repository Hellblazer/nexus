# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 item 1: one read path for a collection's attributes.

:func:`resolve_collection` answers a label or a collection id from the registry row
(the shared, TTL-windowed collections cache the write paths already read), as one of
three outcomes. They are the three the write paths tell apart today:

- :class:`Row`: the row answers, as a
  :class:`~nexus.catalog.collection_name.CollectionAttributes`;
- :class:`NoRow`: no row backs the name. The cache also omits a registered collection
  that holds no chunk yet. A strict reader may turn this into
  :class:`~nexus.corpus.CollectionNotRegisteredError`; from P3.8 the writer uses the row
  its registering call returned, and only a legacy name registered before then answers
  with the mint-class derivation (:func:`embedding_model_for`);
- :class:`Unreadable`: the read failed, or the row's version cannot be read. It is never
  folded into ``NoRow``: a write that meets it answers with the name too, flagged, so the
  unchanged-content skip does not judge a stored stamp against that guess
  (``doc_indexer._target_model``, nexus-s8r5o).

A row whose ``model_version`` is blank (53 live rows until D2, ``nexus-f3yqd.2.23``,
completes) takes the conformant-name fallback ``collection_for`` uses
(:func:`nexus.catalog.rows.legacy_names.version_of_blank_row_label`) with the same
warning; under a non-conformant name it is :class:`Unreadable` for the model. A reader
that takes no version from the row passes ``need_version=False`` and is not refused over
a version it never reads.

:func:`find_collection` is the by-attributes read: the row for
``(content_type, owner_id, embedding_model)`` through ``GET /collections/for_tuple``.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from nexus.catalog.collection_name import KNOWN_EMBEDDING_MODELS, CollectionAttributes, row_version_number
from nexus.catalog.rows.legacy_names import version_of_blank_row_label

_log = structlog.get_logger(__name__)

#: A row reader: ``ref -> the registry row as a dict, or None``. It raises when the read fails.
RowResolver = Callable[[Any], "dict | None"]


@dataclass(frozen=True)
class Row:
    """The registry row answers.

    *raw* is the row dict as the reader returned it, for a caller that must tell a column the
    row lacks from one it holds empty (``resolve_row_preferred``, the funnel helpers).
    """

    attributes: CollectionAttributes
    raw: dict = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class NoRow:
    """No row backs *ref* (never registered, or registered with no chunk yet)."""

    ref: str | int


@dataclass(frozen=True)
class Unreadable:
    """The read failed (``reason="listing"``), the row's version cannot be read (``"model_version"``), or
    the row carries no embedding model when one was asked for (``"embedding_model"``).

    *cause* is the exception that failed the read; a caller that raises on it chains it.
    """

    ref: str | int
    reason: Literal["listing", "model_version", "embedding_model"]
    cause: BaseException | None = None


Resolution = Row | NoRow | Unreadable


def _attributes(ref: str | int, row: dict, *, need_version: bool, need_model: bool) -> Row | Unreadable:
    if need_model and not row.get("embedding_model"):
        return Unreadable(ref, "embedding_model")
    label = row.get("label") or row.get("name") or (ref if isinstance(ref, str) else "")
    version: int | None = None
    if "model_version" in row:
        raw = row["model_version"]
        if raw == "":
            if need_version:
                try:
                    version = version_of_blank_row_label(label, models=KNOWN_EMBEDDING_MODELS)
                except ValueError as exc:
                    return Unreadable(ref, "model_version", exc)
                if version is None:
                    return Unreadable(ref, "model_version")
        else:
            version = row_version_number(raw)
            if version is None and need_version:
                return Unreadable(ref, "model_version")
    cid, dim, state = row.get("collection_id"), row.get("dimension"), row.get("lifecycle_state")
    return Row(raw=row, attributes=CollectionAttributes(
        collection_id=cid if isinstance(cid, int) and not isinstance(cid, bool) else None,
        label=label,
        content_type=row.get("content_type") or "",
        owner_id=row.get("owner_id") or "",
        embedding_model=row.get("embedding_model") or "",
        model_version=version,
        dimension=dim if isinstance(dim, int) and not isinstance(dim, bool) else None,
        lifecycle_state=state if isinstance(state, str) and state else None,
    ))


#: Exceptions that are bugs in the reader or its caller, never a read that could not complete: they
#: are not folded into ``Unreadable`` (they would read as "not registered").
_PROGRAMMING_ERRORS = (TypeError, AttributeError, KeyError)


def _accepts_strict(reader: Callable[..., Any]) -> bool:
    try:
        params = inspect.signature(reader).parameters
    except (TypeError, ValueError):
        return False
    return "strict" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def resolve_collection(
    ref: str | int, *, row_resolver: RowResolver | None = None, need_version: bool = True,
    strict: bool = True, need_model: bool = False,
) -> Resolution:
    """The registry row of the collection *ref* names: a label (a name) or a ``collection_id``.

    *row_resolver* reads the row through a given client instead of the shared collections
    cache, so a client for another tenant never reads the process default's catalog
    (nexus-fryrd). Under *strict* it is called with ``strict=True`` when it accepts a ``strict``
    keyword (``HttpVectorClient._resolve_collection_row`` does), so a listing that failed raises
    out of it and answers :class:`Unreadable`; a reader that takes none answers ``None`` for a
    name it has no row for and for a read it could not make, and that is :class:`NoRow`. Without
    one, a label reads ``mcp_infra.get_collection_row`` and an id ``get_collection_row_by_id``.

    A read that fails is :class:`Unreadable` in both modes (a ``TypeError``, ``AttributeError`` or
    ``KeyError`` is a bug and raises instead), never :class:`NoRow`: a write answers Unreadable
    with the name too, flagged (nexus-s8r5o). *strict* (the default) is what makes the shared cache
    raise a service error to be told apart; ``strict=False`` is the legacy contract of the model
    reads and the funnel helpers (:func:`embedding_model_for`, ``nexus.corpus.collection_model``,
    ``resolve_row_preferred``), where the cache itself answers ``None`` on a service error, so
    that read is :class:`NoRow`, and any other failure is :class:`Unreadable`; those callers treat
    the two alike.

    *need_model* is for a caller that reads the row's ``embedding_model``: a row that carries
    none is :class:`Unreadable` (reason ``"embedding_model"``), not a row to be answered from its
    name, since the row is the source (the model reads, :func:`embedding_model_for` and
    ``nexus.corpus.embedding_model_for_write``, pass it).
    """
    if row_resolver is not None:
        try:
            row = row_resolver(ref, strict=True) if strict and _accepts_strict(row_resolver) else row_resolver(ref)
        except _PROGRAMMING_ERRORS:
            raise
        except Exception as exc:  # noqa: BLE001 — a resolver that cannot answer is an Unreadable read, whatever it raised; the caller flags it (nexus-s8r5o)
            _log.warning("resolve_collection_row_resolver_failed", ref=ref, exc_info=True)
            return Unreadable(ref, "listing", exc)
    else:
        from nexus import mcp_infra  # noqa: PLC0415 — circular-dep avoidance (mcp_infra)
        from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — circular-dep avoidance (http_vector_client imports mcp_infra)

        by_id = isinstance(ref, int) and not isinstance(ref, bool)
        try:
            row = (mcp_infra.get_collection_row_by_id(ref, strict=strict) if by_id
                   else mcp_infra.get_collection_row(ref, strict=strict))
        except (mcp_infra.ServiceNotReadyError, VectorServiceError) as exc:
            _log.debug("resolve_collection_listing_failed", ref=ref, error=str(exc))
            return Unreadable(ref, "listing", exc)
        except _PROGRAMMING_ERRORS:
            raise
        except Exception as exc:  # noqa: BLE001 — a write or a read has always gone on with the name when the row fetch cannot complete (a class-(d) diagnostic over drifted or offline state); logged, never raised
            _log.warning("resolve_collection_row_fetch_failed", ref=ref, exc_info=True)
            return Unreadable(ref, "listing", exc)
    if row is None:
        return NoRow(ref)
    return _attributes(ref, row, need_version=need_version, need_model=need_model)


def embedding_model_for(name: str, *, row_resolver: RowResolver | None = None) -> str:
    """The embedding model of collection *name*: the row's when a row backs it.

    The read-path model read (RDR-204 class (b), which RDR-229 turns into a row read).
    ``NoRow`` and ``Unreadable`` (a failed read, or a row that carries no model) answer with the name's own
    derivation, the mint-class answer item 1 prescribes for a legacy name no row backs: the
    conformant name's token, else the prefix dispatch
    (:func:`nexus.corpus.embedding_model_for_collection`). This is the one counted site where
    the NoRow derivation happens, so the sites that read a model through here carry no name
    parse of their own.
    """
    out = resolve_collection(name, row_resolver=row_resolver, need_version=False, strict=False, need_model=True)
    if isinstance(out, Row):
        return out.attributes.embedding_model
    if isinstance(out, Unreadable):
        _log.warning("embedding_model_row_unreadable", collection=name, reason=out.reason)
    from nexus.corpus import embedding_model_for_collection  # noqa: PLC0415 — circular-dep avoidance (corpus imports this package)

    return embedding_model_for_collection(name)


def find_collection(content_type: str, owner_id: str, embedding_model: str) -> CollectionAttributes | None:
    """The registered collection for ``(content_type, owner_id, embedding_model)``, or ``None``.

    RDR-229 item 1 and item 5, through ``GET /collections/for_tuple`` on the process-default
    catalog (``make_catalog_reader().find_collection``), closing the reader it built. *owner_id*
    is the row's owner segment (a knowledge collection's subject). ``None`` means no row backs
    the tuple; a failed read raises, so a caller never takes an unreadable registry for an empty
    one and mints over it. A row for another tuple, or with a malformed version, raises
    ``ValueError`` (:func:`nexus.catalog.collection_name.find_collection_row`). It never probes
    T3 names, so the rule that indexers do not grandfather onto legacy names is untouched.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle

    reader = make_catalog_reader()
    try:
        return reader.find_collection(content_type, owner_id, embedding_model)  # type: ignore[union-attr]
    finally:
        close = getattr(reader, "close", None)
        if close is not None:
            close()
