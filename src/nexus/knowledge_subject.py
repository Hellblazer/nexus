# SPDX-License-Identifier: AGPL-3.0-or-later
"""Finding a knowledge subject's collection by its catalog row (RDR-229 P2.8, nexus-p2m1x.13).

Moved out of :mod:`nexus.corpus` to keep that file under its size ceiling;
``nexus.corpus`` re-exports every public name here, and its callers import them from there.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import click
import structlog

if TYPE_CHECKING:
    from nexus.catalog.collection_name import CollectionAttributes

_log = structlog.get_logger(__name__)


class CollectionRegistryUnreadableError(click.ClickException, RuntimeError):
    """A write could not read the catalog row for the collection it names
    (RDR-229 P2.8, item 1's Unreadable outcome).

    A read falls back to the name resolution when the registry cannot be read,
    but a write must not: the subject's collection may be registered under a
    label the render does not produce, and writing to the rendered name would
    split the subject across two collections. A ``ClickException``, so a CLI
    writer (``nx index md``/``pdf``, ``nx dt index``) exits 1 with the message.
    """


class CollectionStoreUnreadableError(CollectionRegistryUnreadableError):
    """A write could not probe the vector store for the collection it names (RDR-229 P2.8).

    A read falls back to the rendered name; a write must not guess it, since the subject's data
    may sit under another name. Same exit as the registry error, a message naming the probe.
    """

    @classmethod
    def for_probe(cls, user_arg: str, rendered: str, exc: BaseException) -> "CollectionStoreUnreadableError":
        """The error for a write whose existence probe of *user_arg* raised *exc*."""
        return cls(
            f"cannot probe the vector store for the collection of {user_arg!r}; "
            f"refusing to write under the rendered name {rendered!r}: {exc}"
        )


SubjectFinder = Callable[[str, str, str], "CollectionAttributes | None"]
"""``(content_type, owner_id, embedding_model) -> the registered row or None``:
what :meth:`nexus.db.http_vector_client.HttpVectorClient.find_collection` and
:func:`catalog_subject_finder` answer."""


def catalog_subject_finder() -> SubjectFinder:
    """A :data:`SubjectFinder` that reads the catalog registry row, and nothing else
    (RDR-229 P2.8, item 5).

    For the writers that hold no T3 handle (``nx index md``, ``nx index pdf``,
    ``nx dt index``). It is :func:`nexus.catalog.rows.resolve.find_collection`: a read of
    ``make_catalog_reader().find_collection(...)`` for the process-default tenant that
    closes the reader as ``HttpVectorClient.find_collection`` does. It never
    probes T3 names, so the nexus-hmxi rule (indexers do not grandfather onto
    legacy names) is untouched. The reader is built per call, never at
    construction. A failed read raises; :func:`t3_collection_name` turns it into
    :class:`CollectionRegistryUnreadableError` on a write.
    """
    from nexus.catalog.rows.resolve import find_collection  # noqa: PLC0415 — deferred to avoid import cycle

    return find_collection


def knowledge_subject_label(
    finder: "SubjectFinder | None", content_type: str, subject: str, rendered: str, *, for_write: bool,
) -> "str | None":
    """The label of the knowledge collection registered for *subject*, found by its
    row's attributes, never by rendering (RDR-229 P2.8, item 5).

    A knowledge collection's row carries its subject as ``owner_id``
    (``catalog-044`` exempts it from the owner rewrite; RDR-229 item 5),
    so ``finder('knowledge', subject, <read model>)`` finds it whatever shape
    its label has. Answers the label only when it differs from *rendered*, the
    name the resolution below would try first: a row under the rendered name
    leaves that resolution, with its legacy fallbacks, exactly as it was.

    ``None`` (the name resolution stands) when *content_type* is not ``knowledge``,
    *finder* is ``None``, or no row
    backs the subject. A store's ``find_collection`` answers only for its
    own tenant (:meth:`nexus.db.http_vector_client.HttpVectorClient.find_collection`).
    When the row cannot be read, a read falls back to the name resolution and a
    write raises :class:`CollectionRegistryUnreadableError`.
    """
    from nexus.catalog.collection_name import CollectionAttributes  # noqa: PLC0415 — circular-dep avoidance (catalog)

    from nexus.corpus import _resolve_promoted_model_token  # noqa: PLC0415 — circular-dep avoidance (nexus.corpus imports this module)

    if finder is None or content_type != "knowledge":
        return None
    try:
        row = finder("knowledge", subject, _resolve_promoted_model_token("knowledge", for_write=False))
    except Exception as exc:  # noqa: BLE001 — any failure is "unreadable": a read falls back, a write refuses
        if for_write:
            raise CollectionRegistryUnreadableError(
                f"cannot read the catalog row for knowledge subject {subject!r}; "
                f"refusing to write under the rendered name {rendered!r}: {exc}"
            ) from exc
        _log.warning("t3_collection_name_subject_row_read_failed", subject=subject, exc_info=True)
        return None
    if not isinstance(row, CollectionAttributes) or row.label == rendered:
        return None
    return row.label
