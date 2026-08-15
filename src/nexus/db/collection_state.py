# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Three-state collection-existence probe (nexus-9n485).

RDR-156 P3 (Decision 6) repointed ``HttpVectorClient.collection_exists()``
onto the tombstone-filtered ``collection_vector_stats`` view: a collection
whose every chunk belongs to a trashed document now reads as *absent*, the
same boolean answer a collection returns that never existed at all.

Three call sites need to tell these apart (bead nexus-9n485 / gate
nexus-70r3c.13):

- ``collection_rename.py``'s ``rename_collection_data_plane`` -- refusing a
  rename of a tombstoned ``old`` with "not found" hides the real remedy
  ("restore the trashed documents first"); and treating a tombstoned ``new``
  as free-to-claim would let a plain rename (no ``cross_model`` escape
  valve) silently land live data on top of dead rows under one name.
- ``commands/catalog_cmds/collections.py``'s ``rename_collection_cmd`` --
  the same two guards, CLI-verb flavored.
- ``indexer.py``'s RDR-103 legacy-to-conformant migration probe and
  ``corpus.py``'s name-resolution probe -- observability only; both
  already want LIVE semantics (do not migrate/read from a name with no
  queryable content), so :func:`probe_collection_state` there is used to
  make the "skip a tombstoned candidate" decision non-silent, not to
  change which name is picked.

Only :class:`~nexus.db.http_vector_client.HttpVectorClient` has this gap.
Tombstoning (catalog-003) sets ``catalog_documents.deleted_at`` -- it never
touches the physical ``nexus.chunks`` rows (purge is a separate, later,
explicit step), so the raw ``GET /v1/vectors/collections`` listing (a bare
``SELECT DISTINCT collection`` scan over that unified table) still names a
tombstoned collection even though ``collection_vector_stats`` -- built on
the tombstone-filtered ``live_chunks`` view -- does not. ``T3Database`` (Chroma / InMemoryVectorClient)
has no such split: its ``collection_exists()`` is already a raw physical
check with zero tombstone concept, so it is a true two-state PRESENT/ABSENT
backend and is exempt from this ambiguity by construction.
"""
from __future__ import annotations

import enum
from typing import Any


class CollectionState(enum.Enum):
    """Result of a three-state collection-existence probe."""

    #: At least one LIVE chunk exists under this name
    #: (``collection_exists() is True``).
    PRESENT = "present"
    #: Physical chunk rows exist under this name, but every one belongs to
    #: a trashed document (``collection_exists() is False`` yet the RAW
    #: ``/v1/vectors/collections`` listing still names it). Only reachable
    #: on :class:`~nexus.db.http_vector_client.HttpVectorClient` -- backends
    #: with no tombstone concept never produce this state.
    TOMBSTONED = "tombstoned"
    #: No chunk rows exist under this name at all, live or tombstoned.
    ABSENT = "absent"


def probe_collection_state(t3_db: Any, name: str) -> CollectionState:
    """Best-effort three-state existence probe across T3 backend types.

    Delegates to ``t3_db.collection_probe(name)`` when *t3_db* is literally
    an :class:`~nexus.db.http_vector_client.HttpVectorClient` instance --
    the only backend where tombstoned-vs-absent is ambiguous. Every other
    backend (``T3Database``, and the ``MagicMock``/fake test doubles used
    throughout the rename test suite) degrades to the plain two-state read
    of ``collection_exists()``: :data:`CollectionState.PRESENT` when True,
    :data:`CollectionState.ABSENT` when False, and :data:`CollectionState.TOMBSTONED`
    is never returned for them.

    The ``isinstance`` gate (rather than duck-typing a ``collection_probe``
    attribute via ``hasattr``/``getattr``) is deliberate: ``MagicMock``
    auto-vivifies ANY attribute access, so a duck-typed check would
    silently "succeed" against the unconfigured fakes that populate the
    existing rename test suite and hand back a meaningless ``Mock`` instead
    of a real :class:`CollectionState` -- corrupting every guard that
    branches on the result.
    """
    from nexus.db.http_vector_client import HttpVectorClient  # noqa: PLC0415 — deferred to avoid an import cycle (http_vector_client -> collection_state)

    if isinstance(t3_db, HttpVectorClient):
        return t3_db.collection_probe(name)
    return CollectionState.PRESENT if t3_db.collection_exists(name) else CollectionState.ABSENT
