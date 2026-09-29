# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collection errors the CLI boundary renders cleanly.

Kept apart from ``nexus.corpus`` so ``nexus.cli`` can catch them without
importing the corpus module on every invocation.
"""
from __future__ import annotations


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
