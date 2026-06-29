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
server-side. By default NO source embedding vectors cross the ETL —
``iter_collection_chunks`` omits them (RDR-109 cross-model-contamination
guard). Recall equivalence with identical embedders was established by the
Phase 3 dual-run harness.

SAME-MODEL PASSTHROUGH EXCEPTION (nexus-hxry2): when a collection migrates
SAME-model into a WIRED model (:data:`_PASSTHROUGH_MODELS` — bge / voyage;
see :func:`_is_same_model_passthrough`), the stored vectors ARE fetched
(``include_embeddings=True``) and forwarded so the service stores them
verbatim, skipping a needless re-embed (a billed Voyage call for a managed
user, a wasted ONNX recompute for a local user). The contamination guard
still holds: passthrough fires only when the source model equals the
target's wired model, and the service rejects any vector whose dimension
disagrees with the dispatched table. A batch with any missing source vector
falls back to the server-side re-embed (logged), never a null vector.

COPY-NOT-MOVE: the Chroma source is opened read-only by convention and is
never modified — not by migration, not by rollback. The source is also the
rollback manifest: :func:`rollback_collections` deletes from pgvector
exactly the chashes present in the source collection.

COLLECTION NAMES VERBATIM (same-model default): no namespace normalization —
the pgvector ``collection`` column carries the source name byte-for-byte so
``topic_assignments.source_collection`` references stay valid (the
string-copy-orphan class RDR-108 fixed).

CROSS-MODEL EXCEPTION (RDR-162): when a caller passes ``target_names`` (a source
-> target map), a collection whose model the service cannot serve (e.g. a legacy
``minilm-l6-v2-384`` source) is re-embedded into a model-remapped TARGET name
(``...bge-base-en-v15-768...``) — read from the source, upsert + verify on the
target, dim dispatched from the target segment. The stored chunk text (not the
source vectors) is what the service re-embeds, so NO source file is required
(this covers ``sourceless`` manual-note collections too). Because the target
name differs from the source, the caller MUST remap the catalog/topic
``source_collection`` references to the target AFTER post-write verification (the
ref-remap is owned by the orchestrator, ordered after the verified-populated
gate so a mid-migrate failure never leaves dangling references).

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
import dataclasses
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import structlog

from nexus.db.chroma_quotas import QUOTAS
from nexus.migration.chroma_read import (
    iter_collection_chunks,
    list_collection_names,
    open_cloud_read_client,
    open_local_read_client,
)

_log = structlog.get_logger(__name__)

# "skipped-empty" (nexus-pebfx.3): non-conformant AND source has 0 chunks —
# nothing can be lost by definition, so it does not redden the run. A
# non-conformant collection WITH data stays "skipped" and red: the
# partial-migration-never-green contract is preserved exactly where it
# protects data (locked test: test_nonconformant_collection_skipped_loud).
# "excluded" (pebfx.3 follow-up, Hal 2026-06-11): tuples__* collections are
# session-ephemeral hook/tuplespace state that dies with Chroma at P4b and
# is never migrated. They are excluded from DEFAULT enumeration (reported,
# never silent) so accumulating tuples data cannot fail the straggler
# sweep; naming one explicitly via --collections still migrates/refuses it.
MigrationStatus = Literal[
    "migrated", "failed", "skipped", "skipped-empty", "excluded", "dry-run",
]

#: Collection-name prefixes excluded from DEFAULT enumeration (explicit
#: --collections naming overrides). Session-ephemeral, die-with-Chroma data.
EPHEMERAL_EXCLUDE_PREFIXES: tuple[str, ...] = ("tuples__",)

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

#: Voyage models — the same-model re-embeds that BILL the operator key. Used by
#: the cost guardrail (detection.py) to estimate the cross-model→voyage charge.
_VOYAGE_MODELS: frozenset[str] = frozenset(
    {"voyage-code-3", "voyage-context-3", "voyage-3"}
)

#: Models eligible for same-model PASSTHROUGH (nexus-hxry2): the service can embed
#: QUERIES against them post-migration, so copying stored doc vectors leaves a
#: queryable collection. bge-768 is wired in every mode; voyage models are wired
#: when the key is present (and only reach the same-model path when classified
#: supported-voyage upstream). minilm / unknown models are deliberately ABSENT:
#: the service wires no embedder for them, so they MUST be cross-model remapped
#: (orchestrator-owned) — passthrough would leave an unqueryable collection.
_PASSTHROUGH_MODELS: frozenset[str] = frozenset({"bge-base-en-v15-768"}) | _VOYAGE_MODELS


def _is_same_model_passthrough(name: str, target: str) -> bool:
    """True when this collection migrates SAME-model into a WIRED model.

    Two conditions: (1) target == source (no model change), and (2) the model is
    in :data:`_PASSTHROUGH_MODELS` — one the service can embed queries against, so
    the migrated collection stays queryable. The collection name encodes the model
    (``…__bge-base-en-v15-768__v1``), so a same-name migration into a wired model
    means the stored vectors were produced by exactly the model the target is
    searched against — safe to copy verbatim (guarded further by the server-side
    per-vector dimension check).

    Applies to BOTH deployments: a managed/voyage user avoids the billed Voyage
    re-embed; a LOCAL user avoids a full ONNX (bge-768) recompute of vectors that
    already exist — same logical waste, copied instead of recomputed (nexus-hxry2).
    Cross-model migrations and unsupported-model collections (minilm, which must be
    remapped) return False and re-embed, as required.
    """
    if name != target:
        return False
    segments = name.split("__")
    return len(segments) == 4 and segments[2] in _PASSTHROUGH_MODELS


@dataclass(frozen=True)
class CollectionResult:
    """Per-collection migration outcome (exact counts, never estimates)."""

    collection: str
    source_count: int
    written_count: int
    status: MigrationStatus
    reason: str = ""
    #: Wall-clock seconds for this collection (nexus-pebfx.3 summary table).
    duration_s: float = 0.0
    #: RDR-162 cross-model migrate: the pgvector target collection the source
    #: was re-embedded into when its model segment was remapped (e.g. a legacy
    #: minilm-384 source re-embedded into a bge-768 target). ``None`` for the
    #: same-model path (target == source). The orchestrator keys the
    #: catalog/topic ``source_collection`` ref-remap on (collection -> target).
    target_collection: str | None = None


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
        return all(
            r.status in ("migrated", "dry-run", "skipped-empty", "excluded")
            for r in self.results
        )

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


def cross_model_target_name(source: str, target_model: str) -> str:
    """Remap a conformant collection name's model segment to *target_model*.

    RDR-162 cross-model migrate: a legacy ``minilm-l6-v2-384`` source is
    re-embedded into a ``bge-base-en-v15-768`` target — same content_type, owner,
    and version segments, only the model segment swapped. The service then
    re-embeds the (model-agnostic) stored chunk text with the target model and
    accepts the upsert (its name now matches the wired embedder; RDR-109 /
    nexus-pebfx.2 guard satisfied without weakening it).

    Raises ``ValueError`` on a non-conformant source (the caller must only remap
    four-segment names; a non-conformant source is ``skipped`` upstream).
    """
    segments = source.split("__")
    if len(segments) != 4:
        raise ValueError(
            f"cannot remap non-conformant collection name '{source}' "
            "(<content_type>__<owner>__<model>__v<n>)"
        )
    segments[2] = target_model
    return "__".join(segments)


def _iter_id_pages(
    read_client: Any, collection: str, page: int, *, include_embeddings: bool = False
) -> Iterator[list[dict[str, Any]]]:
    """Group the chunk stream into read-page-aligned batches.

    ``include_embeddings`` flows to :func:`iter_collection_chunks` so the
    same-model passthrough (nexus-hxry2) carries each chunk's stored vector.
    """
    batch: list[dict[str, Any]] = []
    for chunk in iter_collection_chunks(
        read_client, collection, page_size=page, include_embeddings=include_embeddings
    ):
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
    target_name: str | None = None,
) -> CollectionResult:
    # RDR-162 cross-model migrate: when *target_name* differs from *name*, read
    # the stored chunk text from the SOURCE (*name*) but upsert + verify against
    # the TARGET (the model-remapped name). The service re-embeds the text with
    # the target's model. The pgvector dim is dispatched from the TARGET segment.
    target = target_name or name
    is_cross_model = target != name
    dim, reason = _dim_for_collection(target)
    if dim is None:
        # nexus-pebfx.3 disposition rule: probe the source count. Empty +
        # non-conformant cannot lose data — report "skipped-empty" (clean).
        # Unreadable counts as data (conservative: stays red).
        try:
            nc_count = int(read_client.get_collection(name).count())
        except Exception:  # noqa: BLE001 - best-effort count probe; degrades to -1 sentinel
            nc_count = -1
        if nc_count == 0:
            _log.info(
                "vector_etl_skip_empty_nonconformant",
                collection=name,
                reason=reason,
            )
            return CollectionResult(
                name, 0, 0, "skipped-empty",
                reason + " (source has 0 chunks — nothing to lose)",
            )
        _log.warning("vector_etl_skip_nonconformant", collection=name, reason=reason)
        return CollectionResult(name, max(nc_count, 0), 0, "skipped", reason)

    try:
        source_col = read_client.get_collection(name)
    except Exception as exc:  # noqa: BLE001 — every per-collection failure is reported, not raised
        reason = f"source collection unreadable: {exc}"
        _log.error("vector_etl_source_unreadable", collection=name, error=str(exc))
        return CollectionResult(name, 0, 0, "failed", reason)

    if dry_run:
        source_count = int(source_col.count())
        _log.info(
            "vector_etl_dry_run", collection=name, target=target,
            source_count=source_count, cross_model=is_cross_model,
        )
        return CollectionResult(
            name, source_count, 0, "dry-run",
            target_collection=target if is_cross_model else None,
        )

    # Same-model migration → PASSTHROUGH: fetch the stored vectors and send them
    # so the service stores them verbatim, skipping the re-embed (nexus-hxry2) —
    # avoids a billed Voyage re-embed for a managed user AND a wasted local ONNX
    # recompute for a local user. Any chunk missing a stored vector falls back to
    # a server-side re-embed for that batch (correctness over cost — never store a
    # null vector).
    passthrough = _is_same_model_passthrough(name, target)
    # nexus-bfdri: the model the collection name DECLARES (segment 3 of the
    # conformant <ct>__<owner>__<embedding_model>__v<n> shape). Passthrough only
    # copies a stored vector verbatim when each chunk's recorded provenance
    # (metadata["embedding_model"], written by make_chunk_metadata at index time)
    # MATCHES this declared model — the name segment alone is not proof the
    # vectors came from the embedder the target is searched against.
    # nexus-bfdri: the model the conformant name DECLARES (segment 3 of
    # <ct>__<owner>__<embedding_model>__v<n>; passthrough already asserts 4
    # segments). ``None`` only on the non-passthrough path (helper unused there).
    declared_model = name.split("__")[2] if passthrough else None

    def _provenance_ok(c: dict) -> bool:
        """MISMATCH-ONLY provenance check (nexus-bfdri).

        Re-embed ONLY when a chunk's recorded ``embedding_model`` is PRESENT and
        DISAGREES with the declared model — that is the detectable mislabel the
        bead targets (vectors from a different embedder than the name claims).

        ABSENT/blank provenance is TRUSTED (passed through), NOT re-embedded:
        ``code_indexer`` did not stamp ``embedding_model`` until the
        ``make_chunk_metadata`` factory landed (2026-04-26), but conformant
        ``code__*__voyage-code-3__v1`` names existed from 2026-02-22 — so
        pre-factory chunks have a conformant name and no provenance, yet their
        vectors DID come from the named embedder (just unstamped). Forcing those
        to re-embed would silently revert the nexus-hxry2 passthrough
        optimization (a billed Voyage re-embed / wasted local ONNX) with no
        correctness gain. Absent ≠ mislabel; only present-and-wrong is evidence.
        """
        if declared_model is None:
            return False  # defensive: meaningless without a declared target
        prov = (c.get("metadata") or {}).get("embedding_model")
        if not prov:  # absent/blank -> unverifiable but benign -> trust
            return True
        return prov == declared_model

    source_count = 0
    written = 0
    try:
        for batch in _iter_id_pages(read_client, name, page, include_embeddings=passthrough):
            source_count += len(batch)
            # Read from the SOURCE (*name*); upsert into the TARGET (model-remapped
            # for cross-model). For the re-embed path the server embeds the stored
            # text with the target's model; for passthrough it stores the supplied
            # vectors verbatim. chash (sha256(text)[:32]) is identical either way,
            # so re-runs stay idempotent on (tenant, target, chash).
            embeddings = None
            if passthrough:
                if all(
                    c.get("embedding") is not None and _provenance_ok(c)
                    for c in batch
                ):
                    embeddings = [c["embedding"] for c in batch]
                else:
                    # Fallback: a batch with any missing source vector OR any chunk
                    # whose recorded provenance does not match the declared model
                    # re-embeds server-side (never copy a null or mis-provenanced
                    # vector) — and that re-embed bills. Logged so a mixed
                    # passthrough/re-embed run is auditable (the dry-run cost caveat
                    # warns this is possible).
                    missing = sum(1 for c in batch if c.get("embedding") is None)
                    mis_provenance = sum(
                        1 for c in batch
                        if c.get("embedding") is not None and not _provenance_ok(c)
                    )
                    _log.warning(
                        "vector_etl_passthrough_fallback_reembed",
                        collection=name,
                        target=target,
                        batch_size=len(batch),
                        missing_vectors=missing,
                        provenance_mismatch=mis_provenance,
                    )
            vector_client.upsert_chunks(
                target,
                [c["id"] for c in batch],
                [c["document"] for c in batch],
                [c["metadata"] for c in batch],
                embeddings=embeddings,
            )
            written += len(batch)
    except Exception as exc:  # noqa: BLE001 — report and continue with the next collection
        reason = f"upsert failed after {written} chunks: {exc}"
        _log.error(
            "vector_etl_upsert_failed",
            collection=name,
            target=target,
            written=written,
            error=str(exc),
        )
        return CollectionResult(
            name, source_count, written, "failed", reason,
            target_collection=target if is_cross_model else None,
        )

    # Post-write verification: exact TARGET count or it did not happen.
    target_count = int(vector_client.count(target))
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
        return CollectionResult(
            name, source_count, written, "failed", reason,
            target_collection=target if is_cross_model else None,
        )

    _log.info(
        "vector_etl_collection_migrated",
        collection=name,
        target=target,
        count=source_count,
        cross_model=is_cross_model,
    )
    return CollectionResult(
        name, source_count, written, "migrated",
        target_collection=target if is_cross_model else None,
    )


def migrate_collections(
    read_client: Any,
    vector_client: Any,
    *,
    leg: Literal["local", "cloud"],
    collections: list[str] | None = None,
    dry_run: bool = False,
    page_size: int | None = None,
    on_result: "Callable[[CollectionResult], None] | None" = None,
    target_names: dict[str, str] | None = None,
) -> MigrationReport:
    """Copy every chunk of *collections* (default: ALL source collections)
    from the Chroma *read_client* into pgvector via *vector_client*.

    The source is read-only; re-runs are idempotent (server-side upsert on
    ``(tenant_id, collection, chash)``). Per-collection failures are
    reported in the :class:`MigrationReport`, never raised — a single bad
    collection must not abort the run (and must not be silently dropped).

    *on_result* (nexus-pebfx.3) is invoked once per collection AS IT
    COMPLETES — the CLI uses it for live, flushed progress lines (the
    2026-06-10 production run showed an EMPTY redirected log while 35k+
    rows landed; the only live meter was psql). Callback exceptions
    propagate — a broken progress sink should fail loud, not corrupt the
    operator's picture silently.

    The post-write count verification assumes a QUIESCENT write window:
    concurrent serving writes into the same collection during the ETL would
    inflate the target count and read as a (conservative) failure. Run the
    migration with indexing paused. ``dry_run`` counts via ``col.count()``
    as a pre-flight estimate, not a binding commitment on a later live run.
    """
    page = page_size or QUOTAS.MAX_QUERY_RESULTS
    explicit = collections is not None
    names = collections if explicit else list_collection_names(read_client)
    results: list[CollectionResult] = []
    for name in names:
        if not explicit and name.startswith(EPHEMERAL_EXCLUDE_PREFIXES):
            try:
                eph_count = int(read_client.get_collection(name).count())
            except Exception:  # noqa: BLE001 — count is informational here
                eph_count = 0
            result = CollectionResult(
                name, eph_count, 0, "excluded",
                "session-ephemeral (dies with Chroma at P4b) — excluded from "
                "default enumeration; pass --collections to act on it",
            )
            results.append(result)
            if on_result is not None:
                on_result(result)
            continue
        t0 = time.monotonic()
        result = _migrate_one(
            read_client, vector_client, name, dry_run=dry_run, page=page,
            target_name=(target_names or {}).get(name),
        )
        result = dataclasses.replace(
            result, duration_s=round(time.monotonic() - t0, 3),
        )
        results.append(result)
        if on_result is not None:
            on_result(result)
    report = MigrationReport(leg=leg, results=tuple(results))
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
    on_result: "Callable[[CollectionResult], None] | None" = None,
    target_names: dict[str, str] | None = None,
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
        on_result=on_result,
        target_names=target_names,
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
    on_result: "Callable[[CollectionResult], None] | None" = None,
    target_names: dict[str, str] | None = None,
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
        on_result=on_result,
        target_names=target_names,
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
    target_names: dict[str, str] | None = None,
) -> dict[str, tuple[int, int]]:
    """Exact ``(source, target)`` chunk counts per collection.

    The SOURCE side reads the Chroma collection by its own name. The TARGET
    (pgvector) side reads ``target_names[name]`` when present (RDR-162 P2
    cross-model migrate: the re-embedded chunks land in a model-remapped target
    whose name differs from the source) — else the same name (the byte-for-byte
    same-model path). The counts are equal in both cases (the chunk set is
    identical; only the embedder differs), so the exact-match gate holds.
    """
    tmap = target_names or {}
    return {
        name: (
            int(read_client.get_collection(name).count()),
            int(vector_client.count(tmap.get(name, name))),
        )
        for name in collections
    }


def verify_taxonomy_consistency(
    t2_db_path: str | Path,
    vector_client: Any,
    target_names: dict[str, str] | None = None,
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

    ``target_names`` (RDR-162 P2): a cross-model source collection's chunks
    migrated into a model-remapped target (minilm-384 -> bge-768), so the SOURCE
    SQLite still names ``S`` while the migrated pgvector collection is its target
    ``target_names[S]``. Each referenced source name is resolved THROUGH this map
    before the membership check, so a cross-model source is not a false orphan.
    """
    tmap = target_names or {}
    uri = f"file:{Path(t2_db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)  # epsilon-allow: RDR-155 P5 taxonomy-consistency check — read-only T2 source read (mode=ro URI), mirrors the db/t2 ETL readers; never a T2 writer
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_collection FROM topic_assignments"
            " WHERE source_collection IS NOT NULL AND source_collection != ''"
        ).fetchall()
    finally:
        conn.close()
    # Resolve each source name through the cross-model remap before comparison:
    # a source whose bge-768 target is migrated is NOT an orphan.
    referenced = {tmap.get(r[0], r[0]) for r in rows}
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
    collection IS NULL — idempotent re-run.

    .. deprecated::
        Superseded by ``nexus.manifest_backfill()`` stored function
        (catalog-004, RDR-156 P2; bead nexus-70r3c.9). Call the stored
        function via psql instead::

            SELECT nexus.manifest_backfill();

        This function is kept only because bead nexus-g37fr (RDR-155 P4b)
        will delete this entire module wholesale. Do not add new callers.
    """
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

    .. deprecated::
        Superseded by ``nexus.manifest_orphans(dim int)`` stored function
        (catalog-004, RDR-156 P2; bead nexus-70r3c.9). Call the stored
        function via psql instead::

            SELECT * FROM nexus.manifest_orphans(1024);

        Run ``nexus.manifest_backfill()`` first (rows with collection IS NULL
        are pre-backfill state, not orphans). This function is kept only
        because bead nexus-g37fr (RDR-155 P4b) will delete this entire
        module wholesale. Do not add new callers.
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
