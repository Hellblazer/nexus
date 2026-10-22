# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collection errors the CLI boundary renders cleanly.

Kept apart from ``nexus.corpus`` so ``nexus.cli`` can catch them without
importing the corpus module on every invocation.
"""
from __future__ import annotations

from typing import Any


class SupersededCollectionWriteError(RuntimeError):
    """A write named a collection the catalog has retired (``superseded_by``
    is set) and nothing asked for it back (nexus-wwuzp).

    Raised when a cached registration is re-validated
    (``nexus.corpus.ensure_collection_registered``), when a cold implicit
    write finds the name already retired, from the "not registered" retry in
    ``write_with_registration_retry``, and by ``nx index repo`` when the name
    it is about to index into is a tombstone. The ``/collections/upsert`` a
    registration issues clears ``superseded_by`` unconditionally, so
    proceeding would silently un-retire the collection; refusing names the
    successor instead. Exposes ``name`` and ``successor``.

    Also raised from the engine's own refusal (nexus-mp8ys, nexus-i15oc): a write
    that reaches the engine against a retired name is answered 409
    ``collection_superseded`` naming the successor, which
    :func:`superseded_write_error_from_body` turns into this error. That covers
    chunk writes (combined write, append), manifest and centroid writes, and,
    since nexus-i15oc, the taxonomy, aspect, highlight and aspect-queue writes. It
    reaches the process that did not perform the rename and whose registration
    cache still calls the name live. The ``/import/*`` verbs and a rename's
    re-home onto its target are not refused (a fidelity import; a rename onto an
    empty tombstone revives it).
    """

    def __init__(self, name: str, successor: str, *, remedy: str | None = None) -> None:
        self.name = name
        self.successor = successor
        # A caller that is not itself writing to a name of its choosing (the
        # indexer takes it from the repo's registry) says what to do instead;
        # ``{name}`` and ``{successor}`` in *remedy* are filled in.
        advice = (
            remedy.format(name=name, successor=successor)
            if remedy is not None
            else f"Write to {successor!r} instead."
        )
        super().__init__(
            f"collection {name!r} was superseded by {successor!r}, so a write to "
            f"{name!r} is refused. {advice}"
        )


def superseded_write_error_from_body(
    status: int | None, body: Any,
) -> SupersededCollectionWriteError | None:
    """The typed error for the engine's 409 ``collection_superseded`` body, else None.

    Keyed on the status and the engine's ``reason`` (the body repeats the value
    in ``error`` for a reader of that key). A body that names no collection or
    no successor is not turned into the typed error: the caller keeps its
    generic 409, which is no more retryable.
    """
    from nexus.db.engine_reasons import COLLECTION_SUPERSEDED_REASON, error_reason  # noqa: PLC0415 — deferred: this module stays import-free for the CLI boundary

    if status != 409 or not isinstance(body, dict):
        return None
    if error_reason(body) != COLLECTION_SUPERSEDED_REASON:
        return None
    name, successor = body.get("collection"), body.get("superseded_by")
    if not (isinstance(name, str) and name and isinstance(successor, str) and successor):
        return None
    return SupersededCollectionWriteError(name, successor)
