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
    rename_collection, delete_stale, is_empty, count_for_collection, close

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
    close() -> None

Note: ``registered_chashes_for_collection`` exists on ChashIndex but is NOT
in the gate requirements public method list and is NOT called via T2Database;
it is a helper used only in audit/backfill paths that open ChashIndex directly.
It is intentionally omitted from HttpChashIndex because those paths bypass
the T2Database seam anyway (and remain SQLite-backed until Phase 4 decommission).
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

#: Default tenant matching TenantConstants.DEFAULT_TENANT in the Java service.
DEFAULT_TENANT: str = "default"

#: Max chashes per upsert_many batch call (service-side limit).
_BATCH_SIZE: int = 200


def _resolve_config() -> tuple[str, int, str]:
    """Return (host, port, token) from environment.

    Raises:
        RuntimeError: if NX_SERVICE_PORT or NX_SERVICE_TOKEN are not set.
    """
    host = os.environ.get("NX_SERVICE_HOST", "127.0.0.1")
    port_str = os.environ.get("NX_SERVICE_PORT", "")
    token = os.environ.get("NX_SERVICE_TOKEN", "")

    if not port_str:
        raise RuntimeError(
            "NX_SERVICE_PORT is required when NX_STORAGE_BACKEND_CHASH_INDEX=service. "
            "Set it to the port where the nexus-service is listening."
        )
    try:
        port = int(port_str)
    except ValueError as exc:
        raise RuntimeError(
            f"NX_SERVICE_PORT must be an integer, got: {port_str!r}"
        ) from exc

    if not token:
        raise RuntimeError(
            "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_CHASH_INDEX=service. "
            "Set it to the bearer token configured in the nexus-service."
        )

    return host, port, token


# ── HttpChashIndex ─────────────────────────────────────────────────────────────


class HttpChashIndex:
    """ChashIndex drop-in that delegates to the RDR-152 Java HTTP service.

    Uses a keep-alive :class:`httpx.Client` connection pool. Reads
    ``NX_SERVICE_HOST``, ``NX_SERVICE_PORT``, and ``NX_SERVICE_TOKEN``
    from the environment at construction time.

    All public methods match the :class:`~nexus.db.t2.chash_index.ChashIndex`
    interface exactly: same parameter names, same return types, same exception
    semantics (``ValueError`` for empty chash/collection).

    Thread safety: ``httpx.Client`` is thread-safe for concurrent requests.
    The ``_lock`` attribute is present on ``ChashIndex`` (SQLite) for its
    own reasons; ``HttpChashIndex`` does not need it but exposes the attribute
    as ``None`` so any caller checking ``hasattr(store, '_lock')`` does not crash.
    """

    _lock = None  # public attribute compatibility

    def __init__(
        self,
        base_url: str | None = None,
        tenant: str = DEFAULT_TENANT,
        _token: str | None = None,
    ) -> None:
        """
        Args:
            base_url: Override for ``http://<host>:<port>`` (used in tests).
                      When ``None``, resolved from NX_SERVICE_HOST + NX_SERVICE_PORT.
            tenant:   Tenant identifier sent in ``X-Nexus-Tenant`` header.
            _token:   Token override (used in tests). When ``None``,
                      resolved from NX_SERVICE_TOKEN env var.
        """
        if base_url is None:
            host, port, resolved_token = _resolve_config()
            base_url = f"http://{host}:{port}"
        else:
            resolved_token = _token or os.environ.get("NX_SERVICE_TOKEN", "")
            if not resolved_token:
                raise RuntimeError(
                    "NX_SERVICE_TOKEN is required when NX_STORAGE_BACKEND_CHASH_INDEX=service."
                )

        self._base_url = base_url.rstrip("/")
        self._tenant = tenant
        self._headers = {
            "Authorization": f"Bearer {resolved_token}",
            "X-Nexus-Tenant": tenant,
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=self._headers,
            timeout=30.0,
        )

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

        resp = self._client.post("/v1/chash/upsert", json={"chash": chash, "collection": collection})
        _raise_for_status(resp, "upsert")

    # ── upsert_many ────────────────────────────────────────────────────────────

    def upsert_many(self, *, chashes: list[str], collection: str) -> None:
        """Register many ``chashes`` in one ``collection``.

        Batches into chunks of ``_BATCH_SIZE`` (200) to respect service limits.
        Blank/whitespace entries are skipped. Empty collection raises ValueError.
        An empty chashes list is a no-op.
        """
        if not collection:
            raise ValueError("collection must not be empty")
        if not chashes:
            return

        valid = [c for c in chashes if isinstance(c, str) and c.strip()]
        if not valid:
            return

        # Batch into chunks
        for i in range(0, len(valid), _BATCH_SIZE):
            batch = valid[i: i + _BATCH_SIZE]
            resp = self._client.post(
                "/v1/chash/upsert_many",
                json={"chashes": batch, "collection": collection},
            )
            _raise_for_status(resp, "upsert_many")

    # ── lookup ─────────────────────────────────────────────────────────────────

    def lookup(self, chash: str) -> list[dict[str, Any]]:
        """Return all (collection, created_at) rows for ``chash``.

        Returns ``[]`` when ``chash`` is unknown.
        """
        resp = self._client.get("/v1/chash/lookup", params={"chash": chash})
        _raise_for_status(resp, "lookup")
        data = resp.json()
        return data.get("rows", [])

    # ── delete_collection ──────────────────────────────────────────────────────

    def delete_collection(self, collection: str) -> int:
        """Drop all rows for ``collection``. Returns deleted row count.

        Idempotent: absent collection yields 0.
        """
        resp = self._client.post("/v1/chash/delete_collection", json={"collection": collection})
        _raise_for_status(resp, "delete_collection")
        return int(resp.json().get("deleted", 0))

    # ── distinct_collections ───────────────────────────────────────────────────

    def distinct_collections(self) -> set[str]:
        """Return every distinct ``physical_collection`` value in the index."""
        resp = self._client.get("/v1/chash/distinct_collections")
        _raise_for_status(resp, "distinct_collections")
        return set(resp.json().get("collections", []))

    # ── rename_collection ──────────────────────────────────────────────────────

    def rename_collection(self, *, old: str, new: str) -> int:
        """Re-point every row from ``old`` -> ``new``. Returns updated row count."""
        resp = self._client.post("/v1/chash/rename_collection", json={"old": old, "new": new})
        _raise_for_status(resp, "rename_collection")
        return int(resp.json().get("updated", 0))

    # ── delete_stale ───────────────────────────────────────────────────────────

    def delete_stale(self, *, chash: str, collection: str) -> int:
        """Drop the single row identified by compound PK (chash, collection).

        Returns 0 when the PK was already absent.
        """
        resp = self._client.post("/v1/chash/delete_stale", json={"chash": chash, "collection": collection})
        _raise_for_status(resp, "delete_stale")
        return int(resp.json().get("deleted", 0))

    # ── is_empty ───────────────────────────────────────────────────────────────

    def is_empty(self) -> bool:
        """True when no rows exist — the "fresh install" guard."""
        resp = self._client.get("/v1/chash/is_empty")
        _raise_for_status(resp, "is_empty")
        return bool(resp.json().get("empty", True))

    # ── count_for_collection ───────────────────────────────────────────────────

    def count_for_collection(self, collection: str) -> int:
        """Return the row count for ``collection``. Returns 0 for unknown collection."""
        resp = self._client.get("/v1/chash/count_for_collection", params={"collection": collection})
        _raise_for_status(resp, "count_for_collection")
        return int(resp.json().get("count", 0))

    # ── close ──────────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the keep-alive HTTP connection pool (idempotent)."""
        self._client.close()

    # ── Context manager ────────────────────────────────────────────────────────

    def __enter__(self) -> "HttpChashIndex":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ── Internal helpers ───────────────────────────────────────────────────────────


def _raise_for_status(resp: httpx.Response, op: str) -> None:
    """Raise RuntimeError on non-2xx, with service error body in message."""
    if resp.is_success:
        return
    try:
        body = resp.json()
        msg = body.get("error", resp.text)
    except Exception:
        msg = resp.text
    raise RuntimeError(f"HttpChashIndex.{op} failed (HTTP {resp.status_code}): {msg}")
