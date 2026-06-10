# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Chroma → pgvector copy-not-move migration ETL (RDR-155 P5.2, nexus-9n4pn).

Reads every chunk out of the legacy Chroma stores through the surviving
read client (:mod:`nexus.migration.chroma_read` — the ONLY allowed Chroma
constructors since Phase 4a) and writes it through the Seam B HTTP vector
client: the Java service embeds server-side and lands rows in the
``nexus.chunks_<dim>`` table dispatched by the collection's model segment.

BOTH legs (RDR-155 §Migrate — an ETL with only one leg is a silent
half-migration):

* **Local leg** (:func:`migrate_local`) — ``chromadb.PersistentClient``
  over the on-disk store the retired local daemon served.
* **Cloud leg** (:func:`migrate_cloud`) — ChromaCloud has no direct
  psql/pg_restore path; this leg reads via the Chroma REST/auth API and
  writes through the same pgvector upsert.

VECTOR-IDENTITY DECISION (a) (recorded on bead nexus-unp61, 2026-06-10):
chunk TEXT transfers byte-verbatim and the chash (chunk natural ID,
``sha256(text)[:32]``) is preserved verbatim; the pgvector side re-embeds
server-side. NO source embedding vectors cross the ETL —
``iter_collection_chunks`` deliberately omits them (RDR-109
cross-model-contamination guard). Recall equivalence with identical
embedders was established by the Phase 3 dual-run harness.

COPY-NOT-MOVE: the Chroma source is opened read-only by convention and is
never modified — not by migration, not by rollback. The source is also the
rollback manifest: :func:`rollback_collections` deletes from pgvector
exactly the chashes present in the source collection.

COLLECTION NAMES VERBATIM: no namespace normalization — the pgvector
``collection`` column carries the source name byte-for-byte so
``topic_assignments.source_collection`` references stay valid (the
string-copy-orphan class RDR-108 fixed).

POST-WRITE VERIFICATION: each migrated collection is verified with an
exact target count; a mismatch is a FAILED migration, never a green one.

MANIFEST VALIDATION IS DIRECT SQL (P2.1 constraint, recorded on
nexus-unp61): :func:`manifest_backfill_sql` / :func:`manifest_orphan_sql`
are generated here and executed by the cutover operator (psql, superuser
or admin role) — NEVER through ``PgVectorRepository.fetchDocumentChunks``,
which fails loud on partially-migrated documents by design. The Python
engine has no Postgres connection by design (RDR-152: PG access lives in
the Java service); these artifacts are the engine's contribution to the
P5.G cutover-readiness validation.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import structlog

from nexus.db.chroma_quotas import QUOTAS
from nexus.migration.chroma_read import (
    iter_collection_chunks,
    list_collection_names,
    open_cloud_read_client,
    open_local_read_client,
)

_log = structlog.get_logger(__name__)

MigrationStatus = Literal["migrated", "failed", "skipped", "dry-run"]

#: Model-segment → pgvector table dimension. MIRRORS the Java authority
#: ``PgVectorRepository.MODEL_DIMS`` (service/src/main/java/dev/nexus/
#: service/vectors/PgVectorRepository.java) — the server fails loud on any
#: token not in this registry, so the ETL pre-classifies with the same map
#: instead of sending doomed upserts.
_MODEL_DIMS: dict[str, int] = {
    "voyage-code-3": 1024,
    "voyage-context-3": 1024,
    "voyage-3": 1024,
    "bge-base-en-v15-768": 768,
    "minilm-l6-v2-384": 384,
}

#: The per-dim physical tables shipped by vectors-001-baseline.xml.
_KNOWN_DIMS: frozenset[int] = frozenset(_MODEL_DIMS.values())


@dataclass(frozen=True)
class CollectionResult:
    """Per-collection migration outcome (exact counts, never estimates)."""

    collection: str
    source_count: int
    written_count: int
    status: MigrationStatus
    reason: str = ""


@dataclass(frozen=True)
class MigrationReport:
    """One leg's migration outcome.

    ``ok`` is True only when every collection landed in a clean terminal
    state (``migrated`` or ``dry-run``). A skipped or failed collection
    makes the whole report not-ok — partial migrations demand explicit
    operator handling, never a green light.
    """

    leg: Literal["local", "cloud"]
    results: tuple[CollectionResult, ...]

    @property
    def ok(self) -> bool:
        return all(r.status in ("migrated", "dry-run") for r in self.results)

    @property
    def total_source(self) -> int:
        return sum(r.source_count for r in self.results)

    @property
    def total_written(self) -> int:
        return sum(r.written_count for r in self.results)


def _dim_for_collection(name: str) -> tuple[int | None, str]:
    """Resolve the pgvector dim for *name*, or (None, reason) when the name
    cannot dim-dispatch (the server would 400 it — classify, don't send)."""
    segments = name.split("__")
    if len(segments) != 4:
        return None, (
            f"collection '{name}' is not four-segment conformant "
            "(<content_type>__<owner>__<model>__v<n>) — cannot dim-dispatch"
        )
    dim = _MODEL_DIMS.get(segments[2])
    if dim is None:
        return None, (
            f"collection '{name}' has unknown embedding-model segment "
            f"'{segments[2]}' — not conformant with the dim registry "
            f"(known: {sorted(_MODEL_DIMS)})"
        )
    return dim, ""


def _iter_id_pages(
    read_client: Any, collection: str, page: int
) -> Iterator[list[dict[str, Any]]]:
    """Group the chunk stream into read-page-aligned batches."""
    batch: list[dict[str, Any]] = []
    for chunk in iter_collection_chunks(read_client, collection, page_size=page):
        batch.append(chunk)
        if len(batch) == page:
            yield batch
            batch = []
    if batch:
        yield batch


def _migrate_one(
    read_client: Any,
    vector_client: Any,
    name: str,
    *,
    dry_run: bool,
    page: int,
) -> CollectionResult:
    dim, reason = _dim_for_collection(name)
    if dim is None:
        _log.warning("vector_etl_skip_nonconformant", collection=name, reason=reason)
        return CollectionResult(name, 0, 0, "skipped", reason)

    try:
        source_col = read_client.get_collection(name)
    except Exception as exc:  # noqa: BLE001 — every per-collection failure is reported, not raised
        reason = f"source collection unreadable: {exc}"
        _log.error("vector_etl_source_unreadable", collection=name, error=str(exc))
        return CollectionResult(name, 0, 0, "failed", reason)

    if dry_run:
        source_count = int(source_col.count())
        _log.info("vector_etl_dry_run", collection=name, source_count=source_count)
        return CollectionResult(name, source_count, 0, "dry-run")

    source_count = 0
    written = 0
    try:
        for batch in _iter_id_pages(read_client, name, page):
            source_count += len(batch)
            vector_client.upsert_chunks(
                name,
                [c["id"] for c in batch],
                [c["document"] for c in batch],
                [c["metadata"] for c in batch],
            )
            written += len(batch)
    except Exception as exc:  # noqa: BLE001 — report and continue with the next collection
        reason = f"upsert failed after {written} chunks: {exc}"
        _log.error(
            "vector_etl_upsert_failed",
            collection=name,
            written=written,
            error=str(exc),
        )
        return CollectionResult(name, source_count, written, "failed", reason)

    # Post-write verification: exact target count or it did not happen.
    target_count = int(vector_client.count(name))
    if target_count != source_count:
        reason = (
            f"post-write count mismatch: source={source_count} "
            f"target={target_count}"
        )
        _log.error(
            "vector_etl_count_mismatch",
            collection=name,
            source=source_count,
            target=target_count,
        )
        return CollectionResult(name, source_count, written, "failed", reason)

    _log.info(
        "vector_etl_collection_migrated",
        collection=name,
        count=source_count,
    )
    return CollectionResult(name, source_count, written, "migrated")


def migrate_collections(
    read_client: Any,
    vector_client: Any,
    *,
    leg: Literal["local", "cloud"],
    collections: list[str] | None = None,
    dry_run: bool = False,
    page_size: int | None = None,
) -> MigrationReport:
    """Copy every chunk of *collections* (default: ALL source collections)
    from the Chroma *read_client* into pgvector via *vector_client*.

    The source is read-only; re-runs are idempotent (server-side upsert on
    ``(tenant_id, collection, chash)``). Per-collection failures are
    reported in the :class:`MigrationReport`, never raised — a single bad
    collection must not abort the run (and must not be silently dropped).

    The post-write count verification assumes a QUIESCENT write window:
    concurrent serving writes into the same collection during the ETL would
    inflate the target count and read as a (conservative) failure. Run the
    migration with indexing paused. ``dry_run`` counts via ``col.count()``
    as a pre-flight estimate, not a binding commitment on a later live run.
    """
    page = page_size or QUOTAS.MAX_QUERY_RESULTS
    names = collections if collections is not None else list_collection_names(read_client)
    results = tuple(
        _migrate_one(read_client, vector_client, name, dry_run=dry_run, page=page)
        for name in names
    )
    report = MigrationReport(leg=leg, results=results)
    _log.info(
        "vector_etl_leg_complete",
        leg=leg,
        collections=len(results),
        total_source=report.total_source,
        total_written=report.total_written,
        ok=report.ok,
    )
    return report


def migrate_local(
    local_path: str | Path,
    vector_client: Any,
    *,
    collections: list[str] | None = None,
    dry_run: bool = False,
    page_size: int | None = None,
) -> MigrationReport:
    """LOCAL leg: open the on-disk store the retired daemon served and
    migrate it. The ETL must be the only opener (WAL single-process
    discipline — see :func:`open_local_read_client`)."""
    read_client = open_local_read_client(local_path)
    return migrate_collections(
        read_client,
        vector_client,
        leg="local",
        collections=collections,
        dry_run=dry_run,
        page_size=page_size,
    )


def migrate_cloud(
    vector_client: Any,
    *,
    tenant: str = "",
    database: str = "",
    api_key: str = "",
    collections: list[str] | None = None,
    dry_run: bool = False,
    page_size: int | None = None,
) -> MigrationReport:
    """CLOUD leg: read via the ChromaCloud REST/auth API (no direct
    psql/pg_restore path exists) and write through the same pgvector
    upsert. Credentials fall back to the configured ``chroma_*`` values."""
    read_client = open_cloud_read_client(
        tenant=tenant, database=database, api_key=api_key
    )
    return migrate_collections(
        read_client,
        vector_client,
        leg="cloud",
        collections=collections,
        dry_run=dry_run,
        page_size=page_size,
    )


def rollback_collections(
    read_client: Any,
    vector_client: Any,
    *,
    collections: list[str] | None = None,
    page_size: int | None = None,
) -> dict[str, int]:
    """Undo the copy: delete from pgvector exactly the chashes present in
    the source Chroma collections. Returns exact per-collection deleted
    counts. The source is the rollback manifest (COPY-NOT-MOVE keeps it
    immutable, so the id set at rollback time equals the id set at
    migration time); the source itself is never modified.
    """
    page = page_size or QUOTAS.MAX_QUERY_RESULTS
    names = collections if collections is not None else list_collection_names(read_client)
    deleted: dict[str, int] = {}
    for name in names:
        handle = vector_client.get_or_create_collection(name)
        # Reachability probe BEFORE any lookup: count() propagates service
        # errors, unlike the collection handle's get(), which swallows them
        # and returns empty — without this, an unreachable service would
        # read as a clean "deleted 0".
        target_before = int(vector_client.count(name))
        removed = 0
        source_ids = 0
        for batch in _iter_id_pages(read_client, name, page):
            ids = [c["id"] for c in batch]
            source_ids += len(ids)
            present = handle.get(ids=ids, limit=len(ids)).get("ids") or []
            if present:
                handle.delete(present)
                removed += len(present)
        if removed == 0 and source_ids > 0 and target_before > 0:
            # The target holds chunks and the source has chashes, yet not a
            # single lookup resolved. The lookup layer swallows transport
            # errors, so this state is indistinguishable from a failed read
            # — refuse to report a clean zero (no-silent-fallback rule).
            raise RuntimeError(
                f"rollback for '{name}': target holds {target_before} chunk(s) "
                f"and the source has {source_ids}, but no source chash resolved "
                "in the target — possible swallowed service errors; refusing to "
                "report a clean zero. Verify the service and re-run (rollback "
                "is idempotent). If this collection legitimately holds only "
                "non-migrated chunks, exclude it via collections=[...]."
            )
        if removed:
            # The delete leg of the collection handle ALSO swallows transport
            # errors — verify the count actually moved by what we deleted
            # (rollback runs in the same quiescent window as migration).
            target_after = int(vector_client.count(name))
            if target_after != target_before - removed:
                raise RuntimeError(
                    f"rollback for '{name}': deleted {removed} chunk(s) but the "
                    f"target count went {target_before} -> {target_after} "
                    f"(expected {target_before - removed}) — deletes may have "
                    "been swallowed by the transport layer; verify the service "
                    "and re-run (rollback is idempotent)."
                )
        deleted[name] = removed
        _log.info("vector_etl_rollback", collection=name, deleted=removed)
    return deleted


def verify_counts(
    read_client: Any,
    vector_client: Any,
    collections: list[str],
) -> dict[str, tuple[int, int]]:
    """Exact ``(source, target)`` chunk counts per collection."""
    return {
        name: (
            int(read_client.get_collection(name).count()),
            int(vector_client.count(name)),
        )
        for name in collections
    }


def verify_taxonomy_consistency(
    t2_db_path: str | Path,
    vector_client: Any,
) -> list[str]:
    """T2 consistency check (bead clause (d)): every
    ``topic_assignments.source_collection`` value must resolve to a
    migrated pgvector collection. Returns the sorted unresolved set —
    empty means no orphaned taxonomy attribution (the RDR-108
    string-copy-orphan class). NULL/empty values are unattributed
    pre-projection rows, not orphans.

    Reads the SQLite T2 read-only; the pgvector side is consulted through
    the service (``list_collections``), so the check runs with no direct
    Postgres access.
    """
    uri = f"file:{Path(t2_db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)  # epsilon-allow: RDR-155 P5 taxonomy-consistency check — read-only T2 source read (mode=ro URI), mirrors the db/t2 ETL readers; never a T2 writer
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_collection FROM topic_assignments"
            " WHERE source_collection IS NOT NULL AND source_collection != ''"
        ).fetchall()
    finally:
        conn.close()
    referenced = {r[0] for r in rows}
    migrated = {c.get("name") for c in vector_client.list_collections()}
    if referenced and not migrated:
        # list_collections() swallows service errors and returns [] — an
        # unreachable service and a never-run migration would both produce
        # an all-orphan verdict. Neither deserves a quiet list of "orphans":
        # fail loud and let the operator disambiguate.
        raise RuntimeError(
            "taxonomy-consistency check: no migrated collections are visible "
            "through the service (service down, or migration not yet run) — "
            f"refusing to report all {len(referenced)} referenced "
            "collection(s) as orphans."
        )
    unresolved = sorted(referenced - migrated)
    if unresolved:
        _log.warning(
            "vector_etl_taxonomy_unresolved",
            count=len(unresolved),
            collections=unresolved,
        )
    return unresolved


# ── Direct-SQL validation artifacts (executed by the cutover operator) ───────


def manifest_backfill_sql() -> str:
    """SQL stamping ``catalog_document_chunks.collection`` from the owning
    document's ``physical_collection`` (vectors-001-6: the column ships
    nullable, "backfilled by Phase 5 ETL"). Touches ONLY rows whose
    collection IS NULL — idempotent re-run."""
    return """\
UPDATE nexus.catalog_document_chunks c
   SET collection = d.physical_collection
  FROM nexus.catalog_documents d
 WHERE d.tenant_id = c.tenant_id
   AND d.tumbler = c.doc_id
   AND c.collection IS NULL
   AND d.physical_collection IS NOT NULL
   AND d.physical_collection != ''
"""


def manifest_orphan_sql(dim: int) -> str:
    """SQL listing manifest rows that do NOT resolve to a migrated chunk:
    ``catalog_document_chunks LEFT JOIN chunks_<dim> ... WHERE chash IS
    NULL`` (the P2.1-mandated direct-SQL validation — NEVER
    ``fetchDocumentChunks``, which fails loud on partial documents by
    design).

    Manifest rows are scoped to collections whose model segment dispatches
    to *dim* — without that filter every other-dim row would be a false
    orphan. Rows with ``collection IS NULL`` are pre-backfill state, not
    orphans (run :func:`manifest_backfill_sql` first).

    Returns orphans across ALL tenants (no outer tenant filter) — intended
    for superuser/admin cutover validation, where the whole-database answer
    is the point.
    """
    if dim not in _KNOWN_DIMS:
        raise ValueError(
            f"unknown pgvector dim {dim} — known dims: {sorted(_KNOWN_DIMS)}"
        )
    tokens = sorted(t for t, d in _MODEL_DIMS.items() if d == dim)
    in_list = ", ".join(f"'{t}'" for t in tokens)
    return f"""\
SELECT c.tenant_id, c.doc_id, c.position, c.chash, c.collection
  FROM nexus.catalog_document_chunks c
  LEFT JOIN nexus.chunks_{dim} k
    ON k.tenant_id = c.tenant_id
   AND k.collection = c.collection
   AND k.chash = c.chash
 WHERE c.collection IS NOT NULL
   AND split_part(c.collection, '__', 3) IN ({in_list})
   AND k.chash IS NULL
"""
