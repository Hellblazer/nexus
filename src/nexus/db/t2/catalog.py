# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""T2 catalog store — eighth domain store (RDR-120 P5.A.1).

Ports the ``CatalogDB`` read/write surface into the T2 substrate. This
is the **skeleton phase**: ``CatalogStore`` wraps an internal
``CatalogDB`` instance and delegates every public method. Production
catalog code in ``src/nexus/catalog/`` keeps using ``CatalogDB``
directly; P5.A.2 inverts the wrapping so ``CatalogDB`` becomes a thin
shim around ``CatalogStore`` (and the raw ``sqlite3.connect`` moves
under ``src/nexus/db/`` where it is allowlisted).

File layout (per Hal-approved P5.A grooming): the catalog file
(``.catalog.db``) stays separate from the seven-store nexus.db.
``CatalogStore`` is the only T2 store that opens a different SQLite
file; the others share ``nexus.db``.

RDR-108 invariants preserved by construction (the wrapped
``CatalogDB`` is the existing implementation):

- ``Document.tumbler`` is doc identity.
- Chunk natural ID = ``sha256(chunk_text)[:32]``.
- ``document_chunks`` manifest is authoritative for doc->chunk joins.

§A8-exempt content writes are inherited from ``CatalogDB.__init__``
(see ``nexus_rdr / 120-research-A9-catalog-extension``):

1. collections auto-backfill (structurally-bound-to-event-sourcing).
2. owners PK-swap (structurally-bound-to-schema).
3. document_chunks PK-swap (structurally-bound-to-schema).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:  # pragma: no cover - import for type hints only
    from nexus.catalog.catalog_db import CatalogDB


class CatalogStore:
    """T2 domain store backing the catalog substrate.

    Public surface mirrors ``CatalogDB`` — see that class for method
    semantics. Method bodies delegate to an internal ``CatalogDB``
    instance during the P5.A.1 skeleton phase; P5.A.2 will invert
    the wrapping so this class owns the SQLite handle.
    """

    def __init__(self, db_path: Path) -> None:
        # Local import: ``CatalogDB`` lives in ``nexus.catalog`` which
        # has its own heavy import chain. Deferring the import keeps
        # the T2 module-load surface cheap (the seven existing stores
        # all hew to this discipline).
        from nexus.catalog.catalog_db import CatalogDB as _CatalogDB

        self._path: Path = db_path
        # The other seven domain stores all live under the nexus
        # config dir which ``nexus_config_dir()`` materialises on
        # first access. The catalog file lives under
        # ``catalog_path()`` which is NOT auto-created in production
        # (Catalog initialisation is the gate). Materialise the
        # parent here so daemon startup can open the file even when
        # the catalog has not yet been initialised by a consumer.
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db: "CatalogDB" = _CatalogDB(db_path)

    # ── identity ──────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        """Path to the underlying ``.catalog.db`` file."""
        return self._path

    @property
    def backfilled_collections(self) -> set[str]:
        """Pass-through to ``CatalogDB._backfilled_collections``.

        Catalog rebuild emits synthetic ``CollectionCreated`` events
        for these names so the event-sourced projection stays bit-equal
        with the live SQLite state (see RDR-101 §A8 carve-out).
        """
        return self._db._backfilled_collections

    # ── document-number sequence ──────────────────────────────────────

    def next_document_number(self, owner_prefix: str) -> int:
        return self._db.next_document_number(owner_prefix)

    # ── search / traversal ────────────────────────────────────────────

    def search(
        self, query: str, *, content_type: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._db.search(query, content_type=content_type)

    def descendants(self, prefix: str) -> list[dict[str, Any]]:
        return self._db.descendants(prefix)

    # ── raw SQL passthrough (used by Projector and audit verbs) ───────

    def execute(
        self, sql: str, params: tuple[Any, ...] | list[Any] = (),
    ) -> sqlite3.Cursor:
        return self._db.execute(sql, params)

    def commit(self) -> None:
        self._db.commit()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._db.transaction():
            yield

    @contextmanager
    def bulk_load_documents(self) -> Iterator[None]:
        with self._db.bulk_load_documents():
            yield

    # ── rebuild (event-replay path) ───────────────────────────────────

    def rebuild(self, *args: Any, **kwargs: Any) -> Any:
        return self._db.rebuild(*args, **kwargs)

    # ── lifecycle ─────────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001
                pass
