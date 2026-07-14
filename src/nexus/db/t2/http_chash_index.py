# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpChashIndex — thin HTTP client over the RDR-152 Java chash_index service.

Drop-in replacement for :class:`~nexus.db.t2.chash_index.ChashIndex`.
Activated by setting ``NX_STORAGE_BACKEND=service`` (or
``NX_STORAGE_BACKEND_CHASH_INDEX=service``).

Config:
    NX_SERVICE_HOST  — service host (default: 127.0.0.1)
    NX_SERVICE_PORT  — service port (required; raises if missing)
    NX_SERVICE_TOKEN — bearer token (required; raises if missing)

All methods send ``Authorization: Bearer <token>`` and
``X-Nexus-Tenant: default`` (``DEFAULT_TENANT``) on every request.

Interface parity (bead nexus-gmiaf.16, RDR-152 P2.6):
    upsert, upsert_many, lookup, delete_collection, distinct_collections,
    rename_collection, delete_stale, is_empty, count_for_collection,
    registered_chashes_for_collection, close

Method-by-method parity vs ChashIndex (SQLite):
    upsert(*, chash, collection) -> None
    upsert_many(*, chashes, collection) -> None
    lookup(chash) -> list[dict[str, Any]]
    delete_collection(collection) -> int
    distinct_collections() -> set[str]
    rename_collection(*, old, new) -> int
    delete_stale(*, chash, collection) -> int
    is_empty() -> bool
    count_for_collection(collection) -> int
    registered_chashes_for_collection(collection) -> set[str]
    close() -> None

nexus-f2qvx.3 (mixin-adoption sweep, batch C — one of the two OUTLIERS):
construction, credential/endpoint refresh-on-401, and the HTTP transport
itself (``_post``/``_get``) are now inherited wholesale from
:class:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin` —
``HttpChashIndex`` no longer bakes a ``self._headers`` dict or a
``httpx.Client(base_url=..., headers=...)`` at construction time, which is
what let a rotated bearer or a supervisor-restart port change go silently
stale for the life of the instance. See
``nx memory get -p nexus -t design-bikit-refreshable-http-store-mixin.md``.

This was the one store in the whole sweep whose ``__init__`` took ``_token``
POSITIONALLY (no ``*`` before it) — normalized here to keyword-only
(``*, _token=None``), matching every other ``Http*Store`` and the mixin's
own pinned contract. Every call site in the codebase already passed
``_token`` as a keyword or omitted it, so this is a safe, verified-in-place
normalization (not a behavior change for any existing caller).

The ``/v1/chash/import`` (ETL fidelity) endpoint gained a public
:meth:`import_rows` wrapper here — pre-adoption, ``chash_etl.py`` and
``migration/orchestrator.py`` reached into ``http_chash._client.post(...)``
directly (there was no store method for it). The mixin's ``httpx.Client`` is
deliberately constructed WITHOUT a baked ``base_url=`` (see the mixin's own
docstring), so a raw ``_client.post("/v1/chash/import", ...)`` call from
OUTSIDE this class breaks outright post-adoption (a relative path against a
client with no base_url). :meth:`import_rows` closes that gap and routes the
endpoint through the same self-healing transport as every other write here.
"""

from __future__ import annotations

from typing import Any

from nexus.db.t2._raw_handle_guard import RawHandleGuardMixin
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

# ── Constants ──────────────────────────────────────────────────────────────────

#: Default tenant matching TenantConstants.DEFAULT_TENANT in the Java service.
DEFAULT_TENANT: str = "default"

#: Max chashes per upsert_many batch call (service-side limit).
_BATCH_SIZE: int = 200


# ── HttpChashIndex ─────────────────────────────────────────────────────────────


class HttpChashIndex(RawHandleGuardMixin, RefreshableHttpStoreMixin):
    """ChashIndex drop-in that delegates to the RDR-152 Java HTTP service.

    Uses a keep-alive :class:`httpx.Client` connection pool via
    :class:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin`,
    which resolves ``NX_SERVICE_HOST``, ``NX_SERVICE_PORT``, and
    ``NX_SERVICE_TOKEN`` (or a managed ``service_url``/``service_token``)
    fresh on construction AND self-heals (re-resolve + retry once) on a
    401 or a connection-refused/reset — see the mixin's own docstring for
    the full resolution order. ``__init__`` is inherited unchanged (this
    class's constructor signature — ``(base_url=None, tenant=DEFAULT_TENANT,
    *, _token=None)`` — matches the mixin's pinned contract exactly, so no
    override is needed).

    All public methods match the :class:`~nexus.db.t2.chash_index.ChashIndex`
    interface exactly: same parameter names, same return types, same exception
    semantics (``ValueError`` for empty chash/collection).

    Thread safety: ``httpx.Client`` is thread-safe for concurrent requests.
    Like every service-backed store this has no raw SQLite ``.conn`` /
    ``._lock``; both are provided by ``RawHandleGuardMixin`` and fail loud
    with an actionable ``AttributeError`` (so ``hasattr`` /
    ``has_raw_access`` cleanly return False — nexus-9613q.2).
    """

    # ── upsert ─────────────────────────────────────────────────────────────────

    def upsert(self, *, chash: str, collection: str) -> None:
        """Register ``chash`` as living in ``collection``.

        Raises:
            ValueError: if chash or collection is empty.
        """
        if not chash:
            raise ValueError("chash must not be empty")
        if not collection:
            raise ValueError("collection must not be empty")

        self._post("/v1/chash/upsert", {"chash": chash, "collection": collection})

    # ── upsert_many ────────────────────────────────────────────────────────────

    def upsert_many(self, *, chashes: list[str], collection: str) -> None:
        """Register many ``chashes`` in one ``collection``.

        Batches into chunks of ``_BATCH_SIZE`` (200) to respect service limits.
        Blank/whitespace entries are skipped. Empty collection raises ValueError.
        An empty chashes list is a no-op.

        Duplicate chashes are collapsed order-preserving (first-wins) BEFORE
        batching. The service rejects a batch containing the same chash twice
        with HTTP 500 (nexus-85z0y), and real files emit duplicate chunk text
        (license headers, boilerplate), so dedup must happen up front — before
        the ``_BATCH_SIZE`` split — to keep a repeat from spanning or recurring
        across batches.
        """
        if not collection:
            raise ValueError("collection must not be empty")
        if not chashes:
            return

        seen = [c for c in chashes if isinstance(c, str) and c.strip()]
        valid = list(dict.fromkeys(seen))
        if not valid:
            return

        # Batch into chunks
        for i in range(0, len(valid), _BATCH_SIZE):
            batch = valid[i: i + _BATCH_SIZE]
            self._post(
                "/v1/chash/upsert_many",
                {"chashes": batch, "collection": collection},
            )

    # ── lookup ─────────────────────────────────────────────────────────────────

    def lookup(self, chash: str) -> list[dict[str, Any]]:
        """Return all (collection, created_at) rows for ``chash``.

        Returns ``[]`` when ``chash`` is unknown.
        """
        data = self._get("/v1/chash/lookup", params={"chash": chash})
        return (data or {}).get("rows", [])

    # ── delete_collection ──────────────────────────────────────────────────────

    def delete_collection(self, collection: str) -> int:
        """Drop all rows for ``collection``. Returns deleted row count.

        Idempotent: absent collection yields 0.
        """
        data = self._post("/v1/chash/delete_collection", {"collection": collection})
        return int((data or {}).get("deleted", 0))

    # ── distinct_collections ───────────────────────────────────────────────────

    def distinct_collections(self) -> set[str]:
        """Return every distinct ``physical_collection`` value in the index."""
        data = self._get("/v1/chash/distinct_collections")
        return set((data or {}).get("collections", []))

    # ── rename_collection ──────────────────────────────────────────────────────

    def rename_collection(self, *, old: str, new: str) -> int:
        """Re-point every row from ``old`` -> ``new``. Returns updated row count."""
        data = self._post("/v1/chash/rename_collection", {"old": old, "new": new})
        return int((data or {}).get("updated", 0))

    # ── delete_stale ───────────────────────────────────────────────────────────

    def delete_stale(self, *, chash: str, collection: str) -> int:
        """Drop the single row identified by compound PK (chash, collection).

        Returns 0 when the PK was already absent.
        """
        data = self._post("/v1/chash/delete_stale", {"chash": chash, "collection": collection})
        return int((data or {}).get("deleted", 0))

    # ── is_empty ───────────────────────────────────────────────────────────────

    def is_empty(self) -> bool:
        """True when no rows exist — the "fresh install" guard."""
        data = self._get("/v1/chash/is_empty")
        return bool((data or {}).get("empty", True))

    # ── count_for_collection ───────────────────────────────────────────────────

    def count_for_collection(self, collection: str) -> int:
        """Return the row count for ``collection``. Returns 0 for unknown collection."""
        data = self._get("/v1/chash/count_for_collection", params={"collection": collection})
        return int((data or {}).get("count", 0))

    # ── registered_chashes_for_collection ─────────────────────────────────────

    def registered_chashes_for_collection(self, collection: str) -> set[str]:
        """Return the set of ``chash[:32]`` values registered for ``collection``.

        Mirrors :meth:`~nexus.db.t2.chash_index.ChashIndex.registered_chashes_for_collection`:
        returns the 32-char prefix of each stored chash so callers can
        intersect directly with Chroma chunk IDs (RDR-108 D1: natural ID
        = ``chash[:32]``).

        Used by the collection-audit coverage probe (``collection_audit.py``)
        and the catalog orphan-backfill path.

        Args:
            collection: physical collection name to query.

        Returns:
            Set of 32-char hex strings; empty when ``collection`` is unknown.

        Raises:
            ValueError:   if ``collection`` is empty.
            httpx.HTTPStatusError: on a (non-self-healable) HTTP error.
        """
        if not collection:
            raise ValueError("collection must not be empty")
        data = self._get("/v1/chash/registered_chashes", params={"collection": collection})
        return set((data or {}).get("chashes", []))

    # ── import_rows (ETL fidelity) ────────────────────────────────────────────

    def import_rows(self, rows: list[dict[str, str]]) -> int:
        """Fidelity-preserving ETL import: ``POST /v1/chash/import``.

        Writes ``chash``/``collection``/``created_at`` VERBATIM from each row
        (unlike :meth:`upsert`, which stamps ``created_at=now()``) — the
        correct path for any ETL that must preserve the original index
        timestamp. Server-side upsert semantics (``ON CONFLICT ... DO
        UPDATE``) make re-imports idempotent.

        Args:
            rows: dicts with ``chash``, ``collection``, ``created_at`` keys
                  (ISO-8601 UTC string).

        Returns:
            The number of rows the service reports as imported.
        """
        data = self._post("/v1/chash/import", {"rows": rows})
        return int((data or {}).get("imported", 0))

    # ── Context manager ────────────────────────────────────────────────────────

    def __enter__(self) -> "HttpChashIndex":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
