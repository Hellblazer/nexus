# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SQLite -> Postgres chash_index ETL (bead nexus-gmiaf.16, RDR-152 Phase 2.6).

COPY-NOT-MOVE: reads all rows from the SQLite ``chash_index`` table and writes
them through the validated HTTP seam (``HttpChashIndex`` -> POST /v1/chash/import)
so every write flows via Java -> jOOQ -> Postgres under RLS with tenant stamping.
The SQLite source is NEVER modified.

IDEMPOTENT: relies on ``ON CONFLICT (tenant_id, chash, physical_collection)
DO UPDATE SET created_at = EXCLUDED.created_at`` in ChashRepository.doImport.
Re-running the ETL produces the same row count. Chash entries are content-
addressed (immutable); EXCLUDED verbatim is correct; no GREATEST needed.

FIDELITY-PRESERVING: uses ``POST /v1/chash/import`` which writes all fields
from the source row VERBATIM (chash, collection, created_at). Using
``/upsert`` would stamp ``created_at=now()``, which discards the original
index timestamp.

TENANT STAMPING: the ``HttpChashIndex`` is constructed with
``tenant=DEFAULT_TENANT`` (``"default"``); the service stamps ``tenant_id``
from the ``X-Nexus-Tenant`` request header.

FIELD MAPPING (SQLite ``chash_index`` columns -> /import row fields):
  chash               -> chash               (TEXT NOT NULL)
  physical_collection -> collection          (TEXT NOT NULL)
  created_at          -> created_at          (ISO-8601 UTC string; REQUIRED fidelity)

BATCH SIZE: rows are submitted in batches of ``_IMPORT_BATCH_SIZE`` (200)
to the /v1/chash/import endpoint.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: Number of rows sent per /v1/chash/import POST call.
_IMPORT_BATCH_SIZE: int = 200

# ── Public API ─────────────────────────────────────────────────────────────────


def migrate_chash_rows(
    sqlite_path: Path,
    http_chash: Any,
    *,
    tenant: str = "default",
) -> dict[str, int]:
    """Copy all ``chash_index`` rows from ``sqlite_path`` to the service via ``http_chash``.

    Args:
        sqlite_path: Path to the SQLite T2 database file.
        http_chash:  An :class:`~nexus.db.t2.http_chash_index.HttpChashIndex`
                     (or compatible shim).
        tenant:      Tenant for the HTTP headers (default: ``"default"``).

    Returns:
        A dict with keys:
            - ``total``: total rows read from SQLite
            - ``imported``: total rows successfully sent to the service
            - ``errors``: count of rows that caused an exception
    """
    conn = sqlite3.connect(str(sqlite_path), check_same_thread=False)  # epsilon-allow: ETL source-read; sqlite_path is the migration source SQLite, never T2Database
    conn.row_factory = sqlite3.Row

    total = 0
    imported = 0
    errors = 0

    try:
        rows = conn.execute(
            "SELECT chash, physical_collection, created_at FROM chash_index"
        ).fetchall()
        total = len(rows)
        _log.info("chash_etl_start", source=str(sqlite_path), total=total)

        # Batch into _IMPORT_BATCH_SIZE groups
        for i in range(0, total, _IMPORT_BATCH_SIZE):
            batch = rows[i: i + _IMPORT_BATCH_SIZE]
            payload: list[dict[str, str]] = []
            for row in batch:
                chash      = row["chash"] or ""
                collection = row["physical_collection"] or ""
                created_at = row["created_at"] or "1970-01-01T00:00:00Z"
                if not chash or not collection:
                    _log.warning("chash_etl_skip_blank", chash=chash, collection=collection)
                    continue
                payload.append({
                    "chash":      chash,
                    "collection": collection,
                    "created_at": created_at,
                })

            if not payload:
                continue

            try:
                # Use the import endpoint on the underlying HTTP client directly.
                resp = http_chash._client.post(
                    "/v1/chash/import",
                    json={"rows": payload},
                )
                if not resp.is_success:
                    raise RuntimeError(
                        f"HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                batch_imported = resp.json().get("imported", 0)
                imported += batch_imported
                _log.info(
                    "chash_etl_batch",
                    batch_start=i,
                    batch_size=len(payload),
                    batch_imported=batch_imported,
                )
            except Exception as exc:
                errors += len(payload)
                _log.error(
                    "chash_etl_batch_error",
                    batch_start=i,
                    error=str(exc),
                )

    finally:
        conn.close()

    _log.info("chash_etl_done", total=total, imported=imported, errors=errors)
    return {"total": total, "imported": imported, "errors": errors}
