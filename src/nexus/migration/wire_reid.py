# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-185 P2.2: wire re-id + the persisted old→new chash map.

THE INCIDENT FIX (GH #1408 / Gap-3). Pre-RDR-108 Chroma collections hold
16/18-char legacy chunk ids; the pgvector identity is
``sha256(chunk_text)[:32]``. Historically the migration refused such
collections outright (``_probe_legacy_ids`` — "the migration NEVER
rewrites ids") with a re-index-from-source remedy that the incident
install could not perform. RDR-185 deliberately RETIRES that rationale:
the correct content address is derivable from the chunk text being
carried, so it is computed ON THE WIRE during ETL — no re-embed, no
source files, the Chroma source stays byte-untouched (RDR-176).

**Supersedes** ``db/t3_reidentify.py`` for migration use: that tool
mutates the SOURCE collection in place (delete+re-upsert under new ids)
and derives only from recorded metadata. The wire transform derives from
the carried text (metadata as fallback/cross-check), writes only to the
TARGET, and persists every old→new pair. Derivation equivalence with the
old tool is test-pinned (``chunk_text_hash[:32]``).

**Commit ordering (gate r2, binding)**: each map batch commits (one
SQLite transaction) INSIDE the transform, which the .14 seam runs
STRICTLY BEFORE the target upsert. A crash can therefore produce
map-without-target — safe, the resume re-upserts idempotently on
``(tenant, collection, chash)`` — but never target-without-map, the
rollback-miss reproduction the gate flagged.

**GH #1390 stands**: correct addresses only. An underivable chunk (no
text AND no recorded hash) raises ``WireReidError`` — the batch fails
loudly with nothing persisted for it; wrong ids are never forced through.

The map store is a LOCAL, queryable migration artifact (own sqlite file,
the ``CompletionStore``/``pipeline_buffer`` own-substrate class) per the
.13 audit's local-mode requirement. The PG-side ``chash_remap`` table
(Liquibase, tenant/RLS) and any bulk-remap service endpoint are the .16
cascade's design surface — the local store is the source of truth the
run itself orders against, and the JSON report artifact (RDR-153
envelope) is emitted by the rung's report layer.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

_HEX32 = 32
_SCHEMA = """
CREATE TABLE IF NOT EXISTS chash_remap (
    tenant_id         TEXT NOT NULL DEFAULT '',
    source_collection TEXT NOT NULL,
    old_id            TEXT NOT NULL,
    new_chash         TEXT NOT NULL CHECK (length(new_chash) = 32),
    target_collection TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    provenance        TEXT NOT NULL,
    PRIMARY KEY (tenant_id, source_collection, old_id)
);
"""
_INDEX = """
CREATE INDEX IF NOT EXISTS idx_chash_remap_new
    ON chash_remap (tenant_id, new_chash)
"""


class WireReidError(RuntimeError):
    """A chunk whose correct chash cannot be derived (no text, no recorded
    hash). Loud by design — GH #1390: never force an id through."""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class RemapEntry:
    """One persisted old→new fact (the .13 audit's map schema, local form)."""

    tenant_id: str
    source_collection: str
    old_id: str
    new_chash: str
    target_collection: str
    provenance: str


def derive_wire_chash(chunk: dict[str, Any]) -> str:
    """The correct content address for *chunk*, derived on the wire.

    Primary: ``sha256(document)[:32]`` — the text being carried is what the
    target stores, so the text-derived id is self-consistent with the
    target's identity contract (``chunk_identity.chunk_id`` equivalent).
    Fallback: recorded ``chunk_text_hash[:32]`` metadata (reference-only
    rows carry no document). A recorded hash that disagrees with the
    carried text is tolerated with a warning — the text wins.

    Raises :class:`WireReidError` when neither source exists.
    """
    document = chunk.get("document")
    meta = chunk.get("metadata") or {}
    recorded = meta.get("chunk_text_hash") or ""
    if document:
        derived = hashlib.sha256(document.encode("utf-8")).hexdigest()[:_HEX32]
        if recorded and recorded[:_HEX32] != derived:
            _log.warning(
                "wire_reid_metadata_hash_mismatch",
                chunk_id=chunk.get("id"),
                recorded=recorded[:_HEX32],
                derived=derived,
                note="carried text wins — target keys on sha256(stored_text)[:32]",
            )
        return derived
    if recorded:
        return recorded[:_HEX32]
    raise WireReidError(
        f"cannot derive chash for chunk {chunk.get('id')!r}: no document text "
        "and no recorded chunk_text_hash — refusing to guess (GH #1390: "
        "correct addresses only)"
    )


class ChashRemapStore:
    """The persisted old→new map — a local, queryable migration artifact.

    Own sqlite substrate (WAL, bootstrap-on-open), PERMANENT retention
    (out-of-band references to old ids are unbounded). ``record_batch``
    is ONE transaction — the r2 ordering unit.
    """

    def __init__(self, db_path: Path, *, now_fn: Callable[[], str] | None = None) -> None:
        self._now_fn = now_fn if now_fn is not None else _utc_now_iso
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)  # epsilon-allow: chash_remap migration artifact owns its substrate — the persisted old->new id map must outlive any store it maps (RDR-185 gate r1/r2; RDR-158-exempt; the pipeline_buffer own-substrate shape)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.execute(_INDEX)

    def record_batch(self, entries: list[RemapEntry]) -> None:
        """Persist one map batch in ONE transaction (the r2 ordering unit).

        Upserts: re-recording the same (tenant, source_collection, old_id)
        replaces the fact — resume re-derivation is deterministic, so this
        is idempotent in practice.
        """
        if not entries:
            return
        now = self._now_fn()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.executemany(
                """
                INSERT INTO chash_remap
                    (tenant_id, source_collection, old_id, new_chash,
                     target_collection, created_at, provenance)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, source_collection, old_id) DO UPDATE SET
                    new_chash = excluded.new_chash,
                    target_collection = excluded.target_collection,
                    created_at = excluded.created_at,
                    provenance = excluded.provenance
                """,
                [
                    (
                        e.tenant_id,
                        e.source_collection,
                        e.old_id,
                        e.new_chash,
                        e.target_collection,
                        now,
                        e.provenance,
                    )
                    for e in entries
                ],
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def lookup(self, source_collection: str, old_id: str, *, tenant_id: str = "") -> str | None:
        row = self._conn.execute(
            "SELECT new_chash FROM chash_remap "
            "WHERE tenant_id = ? AND source_collection = ? AND old_id = ?",
            (tenant_id, source_collection, old_id),
        ).fetchone()
        return row[0] if row else None

    def entries_for_collection(
        self, source_collection: str, *, tenant_id: str = ""
    ) -> dict[str, str]:
        """old_id → new_chash for one source collection (the rollback /
        cascade read shape — gate r1: matching goes through THIS, never
        raw id equality)."""
        rows = self._conn.execute(
            "SELECT old_id, new_chash FROM chash_remap "
            "WHERE tenant_id = ? AND source_collection = ?",
            (tenant_id, source_collection),
        ).fetchall()
        return dict(rows)

    def entries_with_targets(
        self, source_collection: str, *, tenant_id: str = ""
    ) -> dict[str, tuple[str, str]]:
        """old_id → (new_chash, target_collection) for one source collection.

        The rollback read shape for CROSS-MODEL legs (P2 critique Critical):
        the re-id'd rows live under the RENAMED target collection, and only
        the map knows where each row landed."""
        rows = self._conn.execute(
            "SELECT old_id, new_chash, target_collection FROM chash_remap "
            "WHERE tenant_id = ? AND source_collection = ?",
            (tenant_id, source_collection),
        ).fetchall()
        return {old: (new, tgt) for old, new, tgt in rows}

    def all_pairs(self, *, tenant_id: str = "") -> list[tuple[str, str]]:
        """Every (old_id, new_chash) pair across all source collections —
        the remap cascade's global-view input (it detects cross-collection
        ambiguity itself)."""
        return self._conn.execute(
            "SELECT old_id, new_chash FROM chash_remap WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchall()

    def old_ids_for(
        self, source_collection: str, new_chash: str, *, tenant_id: str = ""
    ) -> frozenset[str]:
        """Reverse lookup (identical-text collapse is many-to-one)."""
        rows = self._conn.execute(
            "SELECT old_id FROM chash_remap "
            "WHERE tenant_id = ? AND source_collection = ? AND new_chash = ?",
            (tenant_id, source_collection, new_chash),
        ).fetchall()
        return frozenset(r[0] for r in rows)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ChashRemapStore":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def make_wire_reid_transform(
    map_store: ChashRemapStore,
    *,
    source_collection: str,
    target_collection: str,
    provenance: str,
    tenant_id: str = "",
) -> Callable[[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Build the .14-seam transform that re-ids a batch on the wire.

    Per batch: derive every chunk's correct chash FIRST (all-or-nothing —
    an underivable chunk raises before anything persists), persist the
    old→new entries as ONE map transaction, then return the re-id'd batch
    (new dicts; the source chunks are never mutated). The seam sends the
    returned batch to the target strictly AFTER this returns — the r2
    ordering by construction.
    """

    def transform(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rewritten: list[dict[str, Any]] = []
        entries: list[RemapEntry] = []
        for chunk in batch:
            new_chash = derive_wire_chash(chunk)  # raises before any persist
            old_id = chunk["id"]
            if old_id != new_chash:
                entries.append(
                    RemapEntry(
                        tenant_id=tenant_id,
                        source_collection=source_collection,
                        old_id=old_id,
                        new_chash=new_chash,
                        target_collection=target_collection,
                        provenance=provenance,
                    )
                )
            rewritten.append({**chunk, "id": new_chash})
        map_store.record_batch(entries)  # ONE transaction, BEFORE the target write
        return rewritten

    return transform
