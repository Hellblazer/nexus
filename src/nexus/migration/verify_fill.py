# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-178 wave-2 (nexus-s3dd4): verify-fill (delta) migration mode.

P2 (nexus-s3dd4.2) — the outer count-diff loop. A ``migrate --verify-fill``
re-run should not re-send an entire store to patch a handful of missing
rows (the 2026-07-01 incident: a 270-row hole in a 138,327-row catalog
manifest patched by re-sending ~158k rows). The outer loop is the cheap
pre-check that makes that possible: diff each table's already-known
source (SQLite) row count against the target's count via the
ALREADY-DEPLOYED ``relation_counts`` REST surface
(:class:`~nexus.migration.orchestrator.CountSource`,
``HttpCatalogClient.relation_counts`` — see
``POST /v1/catalog/verify/relation-counts``). NO new engine endpoint.

A table diffed as count-parity is the caller's signal to SKIP the
(later-bead) inner identity-diff + fill loop entirely — a clean store
verifies in one batched HTTP call per store, never a row-by-row identity
fetch. A table diffed as divergent is the caller's signal to run the inner
loop. A table that cannot be diffed (unmapped, or the count source is
unreachable / omits the relation) is ``indeterminate`` — NEVER a silent
pass (nexus-r0esi): the caller must treat it the same as ``divergent`` for
safety (or surface it as an operator warning), never as ``parity``.

This module deliberately reuses — never duplicates — the
:class:`~nexus.migration.orchestrator.CountSource` Protocol, the
``(store, table) -> relation`` mapping (``_VERIFY_TABLES``), and the
plans-convergence dedup guard (``_VERIFY_TABLES_DEDUP``) from
``nexus.migration.orchestrator``. Those constants are the single source of
truth for what a "row" means per relation; a second copy here would drift.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Protocol, TypedDict

import structlog

# nexus.retry does not import orchestrator — no cycle risk here (unlike the
# orchestrator constants, which are deferred to call sites; R2 critique).
from nexus.retry import EtlCircuitBreaker, _etl_batch_with_breaker

if TYPE_CHECKING:
    from nexus.migration.orchestrator import CountSource

_log = structlog.get_logger(__name__)

#: Per-table verdict status. Never a fourth value — the vocabulary is
#: strictly ``parity`` | ``divergent`` | ``indeterminate`` (an ``indeterminate``
#: table is never a silent pass; see module docstring / nexus-r0esi).
_STATUS = frozenset({"parity", "divergent", "indeterminate"})


class TableVerdict(TypedDict):
    """One table's outer count-diff result."""

    source_count: int
    #: ``None`` when the target count could not be obtained (unmapped
    #: table, unreachable count source, or the source omitted this
    #: relation from its response).
    target_count: int | None
    status: str  # one of _STATUS


def verify_store_counts(
    store: str,
    count_source: "CountSource",
    source_counts: dict[str, int],
) -> dict[str, TableVerdict]:
    """Diff *store*'s per-table source counts against the target.

    *source_counts* is ``{table: source_row_count}`` as already computed by
    the store's own ``count_source_rows`` (each ETL module owns its own
    per-table SQLite counting; this function only diffs, it never re-derives
    a source count). Returns ``{table: TableVerdict}`` for every table key
    present in *source_counts* — including tables this outer loop cannot map
    to a relation (``indeterminate``, ``target_count=None``), so the caller
    can iterate the full input without a second lookup.

    Batching: every table that DOES map to a relation is queried in exactly
    ONE ``count_source.counts(...)`` call (deduplicated, sorted for
    determinism) — a clean store's outer verify is one HTTP round trip per
    store, matching the design's "no-op verify = one HTTP call" goal.

    Status semantics:

    - **parity** — ``target_count >= source_count`` for a normal relation,
      or (for a dedup relation) ``source_count == 0`` (trivial pass) or
      ``0 < target_count <= source_count`` (exact match or convergence
      collapse — see the dedup branch below). The caller SKIPS the inner
      fill loop for a parity table.
    - **divergent** — the target under-counts a normal relation, or (for a
      dedup relation) ``target_count == 0`` with a non-zero write
      (nothing landed) or ``target_count > source_count`` (impossible in
      steady state under ``ON CONFLICT DO UPDATE``, treated defensively).
      The caller runs the inner loop.
    - **indeterminate** — the table has no ``_VERIFY_TABLES`` mapping, the
      count source returned ``None`` (unreachable), or its response
      omitted the relation. NEVER treated as a pass.

    Dedup guard (audit correction 2, nexus-s3dd4 comment 2026-07-02):
    relations in ``_VERIFY_TABLES_DEDUP`` (currently only ``nexus.plans``,
    keyed ``UNIQUE(tenant_id, project, query)`` with ``ON CONFLICT DO
    UPDATE``) converge source duplicates onto one target row server-side.
    A landed count below the written count there is convergence-by-design,
    not a hole — this mirrors ``orchestrator.verify_counts``'s dedup branch
    (orchestrator.py) so both surfaces agree on what "parity" means for a
    dedup relation.

    Caveat (R2 critique, 2026-07-02): count-parity is a NECESSARY but not
    SUFFICIENT condition for row-identity parity — a same-count-but-
    different-rows state is invisible to this outer loop by design (the
    accepted Approach A tradeoff: cheap count pre-check, identity diff only
    on divergence). No live write path can produce that state absent a bug
    elsewhere (importBatch keys are drawn 1:1 from source rows under
    ``ON CONFLICT DO UPDATE``), which is why it is a safe skip signal for
    FILL purposes specifically — not a general drift detector.
    """
    if not source_counts:
        return {}

    # Deferred import: P4 wires verify-fill INTO orchestrator.py, so a
    # module-level import here would complete an orchestrator→verify_fill→
    # orchestrator cycle (same reason ServiceCountSource.counts defers its
    # catalog-factory import). R2 substantive-critic finding, 2026-07-02.
    from nexus.migration.orchestrator import _VERIFY_TABLES  # noqa: PLC0415 — import-cycle guard, see above

    table_relations: dict[str, str] = {
        table: relation
        for table in source_counts
        if (relation := _VERIFY_TABLES.get((store, table))) is not None
    }

    relations = sorted(set(table_relations.values()))
    target_counts = count_source.counts(relations) if relations else None

    result: dict[str, TableVerdict] = {}
    for table, source_count in source_counts.items():
        relation = table_relations.get(table)
        if relation is None:
            _log.debug(
                "verify_fill.table_unmapped", store=store, table=table,
            )
            result[table] = _verdict(source_count, None, "indeterminate")
            continue

        if target_counts is None or relation not in target_counts:
            _log.warning(
                "verify_fill.table_indeterminate",
                store=store, table=table, relation=relation,
                reason="count source unreachable" if target_counts is None
                else "relation omitted from response",
            )
            result[table] = _verdict(source_count, None, "indeterminate")
            continue

        target_count = int(target_counts[relation])
        status = _table_status(relation, source_count, target_count)
        if status == "divergent":
            _log.info(
                "verify_fill.table_divergent",
                store=store, table=table, relation=relation,
                source_count=source_count, target_count=target_count,
            )
        result[table] = _verdict(source_count, target_count, status)

    return result


def _table_status(relation: str, source_count: int, target_count: int) -> str:
    """Parity/divergent verdict for one already-resolved relation count."""
    from nexus.migration.orchestrator import _VERIFY_TABLES_DEDUP  # noqa: PLC0415 — import-cycle guard, see verify_store_counts

    if relation in _VERIFY_TABLES_DEDUP:
        # Mirrors orchestrator.verify_counts's convergence-aware dedup
        # branch: written=0 is a trivial pass; 0 < target <= source is an
        # exact match or a by-design convergence collapse; target == 0 from
        # a non-zero write, or target > source, is a real divergence.
        if source_count == 0:
            return "parity"
        if 0 < target_count <= source_count:
            return "parity"
        return "divergent"
    return "parity" if target_count >= source_count else "divergent"


def _verdict(
    source_count: int, target_count: int | None, status: str,
) -> TableVerdict:
    return {
        "source_count": source_count,
        "target_count": target_count,
        "status": status,
    }


# ═══════════════════════════════════════════════════════════════════════════
# P3a (nexus-s3dd4.4) — the inner identity-diff + fill loop, EXISTING-surface
# tables only (chash_index, catalog owners/collections/document_chunks — the
# 2026-07-01 incident-recovery slice; ZERO new engine dependency).
#
# The outer loop above (``verify_store_counts``) marks a table
# "divergent"/"indeterminate"; the caller then runs the functions below,
# ONE PER (store, table[, scope]), to fetch the target's identity set and
# fill only the rows genuinely missing — never a full re-send.
#
# Fill is idempotent by construction (wave-1 invariant: every importBatch
# route is ``ON CONFLICT DO UPDATE`` / ``DO NOTHING``), so a re-run of any
# function here after a partial failure is always safe.
# ═══════════════════════════════════════════════════════════════════════════


class IdentitySource(Protocol):
    """Target-side identity/presence surface for one EXISTING-surface table
    (already scoped by the caller, e.g. bound to one physical_collection).

    Returns the set of identity keys currently present in the target, or
    ``None`` when the surface is unreachable. Mirrors
    :class:`~nexus.migration.orchestrator.CountSource`: ``None`` resolves to
    an INDETERMINATE fill — never a silent pass and never a blind re-send
    (nexus-r0esi).
    """

    def present(self) -> set[str] | None: ...


class FillResult(TypedDict):
    """One table's (or one table+scope's) inner diff + fill result."""

    source_count: int
    #: ``None`` when the identity source was unreachable (indeterminate).
    target_count: int | None
    #: ``None`` when the hole size could not be computed (indeterminate).
    missing: int | None
    #: Rows ACTUALLY transmitted through ``import_fn`` — the P6 regression's
    #: load-bearing assertion is ``filled == hole_size``, never ``table_size``.
    filled: int
    status: str  # "parity" | "filled" | "indeterminate"


def fill_missing(
    *,
    source_rows: list[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], str],
    identity_source: IdentitySource,
    import_fn: Callable[[list[dict[str, Any]]], Any],
    batch_size: int,
    breaker: EtlCircuitBreaker,
    table: str = "",
) -> FillResult:
    """Diff *source_rows* against *identity_source* and re-send only the
    missing rows through *import_fn* (a batched-array POST), wrapped in the
    shared *breaker* (RDR-178 Gap 3).

    *identity_source* is probed EXACTLY ONCE (never per-row) — a clean
    table's fill is one presence fetch + zero import calls. An empty
    *source_rows* is a trivial parity with NO probe at all (nothing to
    diff).

    *key_fn* extracts each source row's identity key in the SAME key space
    as *identity_source* (e.g. ``chash[:32]``, ``tumbler_prefix``, ``name``)
    — callers own this mapping; this function only diffs.

    An unreachable *identity_source* (``present() is None``) returns
    ``status="indeterminate"`` with ``filled=0`` and NEVER calls
    *import_fn* — when the hole size cannot be computed, a blind re-send
    would defeat the entire point of verify-fill (send only the hole), so
    the caller must escalate (e.g. retry, or fall back to a full ETL leg)
    rather than this function guessing.
    """
    if not source_rows:
        return {
            "source_count": 0,
            "target_count": None,
            "missing": 0,
            "filled": 0,
            "status": "parity",
        }

    target = identity_source.present()
    if target is None:
        _log.warning("verify_fill.identity_source_unreachable", table=table)
        return {
            "source_count": len(source_rows),
            "target_count": None,
            "missing": None,
            "filled": 0,
            "status": "indeterminate",
        }

    missing_rows = [r for r in source_rows if key_fn(r) not in target]
    if not missing_rows:
        return {
            "source_count": len(source_rows),
            "target_count": len(target),
            "missing": 0,
            "filled": 0,
            "status": "parity",
        }

    filled = _send_batches(
        missing_rows, import_fn=import_fn, batch_size=batch_size,
        breaker=breaker, table=table,
    )
    return {
        "source_count": len(source_rows),
        "target_count": len(target),
        "missing": len(missing_rows),
        "filled": filled,
        "status": "filled",
    }


def _send_batches(
    rows: list[dict[str, Any]],
    *,
    import_fn: Callable[[list[dict[str, Any]]], Any],
    batch_size: int,
    breaker: EtlCircuitBreaker,
    table: str,
) -> int:
    """Send *rows* through *import_fn* in ``batch_size`` chunks via the
    shared circuit breaker. Returns the count actually transmitted."""
    filled = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        _etl_batch_with_breaker(import_fn, batch, breaker=breaker)
        filled += len(batch)
        _log.info(
            "verify_fill.batch_filled",
            table=table, batch_start=i, batch_size=len(batch),
        )
    return filled


# ── document_chunks: position-bearing manifest, chash-only identity surface ──


class ManifestSource(Protocol):
    """Precise per-document target-manifest fetch.

    Used ONLY for docs with at least one AMBIGUOUS ("candidate") row — a
    row whose chash IS present somewhere in the collection's target chash
    set, so the cheap collection-level pre-filter alone cannot prove the
    row's specific ``(doc_id, position)`` is present (RDR-108 D1: identical
    chunk text collapses to ONE chash; multiple manifest rows can share it).

    Returns the target's manifest rows for *doc_id* — each either a dict
    with at least ``position`` and ``chash`` keys OR an object with
    ``.position``/``.chash`` attributes (``HttpCatalogClient.get_manifest``
    returns ``list[ManifestRow]`` dataclasses; both shapes are accepted,
    see :func:`_manifest_key`) — or ``None`` when unreachable.
    """

    def manifest_for(self, doc_id: str) -> list[Any] | None: ...


class DocFillResult(TypedDict):
    """``document_chunks``' inner diff + fill result (multi-doc, multi-collection)."""

    source_count: int
    missing: int
    #: Rows ACTUALLY transmitted through ``import_fn``.
    filled: int
    #: Rows that could not be resolved (unreachable collection pre-filter OR
    #: unreachable per-doc manifest) — never silently dropped, never
    #: blind-filled; surfaced here for the caller to escalate.
    indeterminate: int


def fill_missing_document_chunks(
    *,
    source_rows: list[dict[str, Any]],
    collection_for_doc: dict[str, str],
    identity_source_factory: Callable[[str], IdentitySource],
    manifest_source: ManifestSource,
    import_fn: Callable[[str, list[dict[str, Any]]], Any],
    batch_size: int,
    breaker: EtlCircuitBreaker,
) -> DocFillResult:
    """Diff + fill ``document_chunks`` — the ONE priority table whose only
    identity surface (``GET /v1/catalog/manifest/chashes?collection=``) is
    chash-level while the real conflict key is ``(doc_id, position)``.

    Each *source_rows* entry is a dict carrying at least ``doc_id``,
    ``position``, ``chash`` (plus whatever additional columns *import_fn*
    needs — this function passes rows through unmodified).

    Two-phase diff per doc:

    1. **Cheap pre-filter** (``identity_source_factory(collection)``, called
       and cached ONCE per distinct collection): a row whose chash is
       ABSENT from the collection's target chash set is DEFINITELY missing
       — no further check needed. A row whose chash IS present is merely
       AMBIGUOUS (some OTHER (doc_id, position) row may have contributed
       that chash) and becomes a "candidate".
    2. **Precise per-doc reconciliation** (``manifest_source.manifest_for``,
       called ONLY for docs with >=1 candidate row): fetches the target's
       actual ``(position, chash)`` tuples for that doc; a candidate row is
       truly missing iff its exact ``(position, chash)`` is absent from
       that set.

    An unreachable collection pre-filter treats every row in that
    collection as a candidate (falls through to phase 2 — never silently
    assumed present). An unreachable per-doc manifest fetch marks that
    doc's candidate rows ``indeterminate`` (not filled, not silently
    dropped) rather than blind-refilling them.

    Fill batches are scoped PER DOC_ID (the ``/import/chunk`` envelope is
    doc-scoped: ``{"doc_id": ..., "rows": [...]}"``), each ``<= batch_size``
    rows, sent through *import_fn* wrapped in the shared *breaker*.
    """
    if not source_rows:
        return {"source_count": 0, "missing": 0, "filled": 0, "indeterminate": 0}

    rows_by_doc: dict[str, list[dict[str, Any]]] = {}
    for row in source_rows:
        rows_by_doc.setdefault(row["doc_id"], []).append(row)

    # Cache one identity-set fetch per distinct collection.
    prefilter_cache: dict[str, set[str] | None] = {}

    def _prefilter_for(collection: str) -> set[str] | None:
        if collection not in prefilter_cache:
            prefilter_cache[collection] = identity_source_factory(collection).present()
        return prefilter_cache[collection]

    missing = 0
    filled = 0
    indeterminate = 0

    for doc_id, doc_rows in rows_by_doc.items():
        collection = collection_for_doc.get(doc_id, "")
        target_chashes = _prefilter_for(collection)

        if target_chashes is None:
            # Pre-filter unreachable for this doc's collection -- every row
            # is an ambiguous candidate; never assumed present.
            definite_missing: list[dict[str, Any]] = []
            candidates = doc_rows
        else:
            definite_missing = [
                r for r in doc_rows if (r.get("chash") or "")[:32] not in target_chashes
            ]
            candidates = [
                r for r in doc_rows if (r.get("chash") or "")[:32] in target_chashes
            ]

        to_fill: list[dict[str, Any]] = list(definite_missing)

        if candidates:
            target_manifest = manifest_source.manifest_for(doc_id)
            if target_manifest is None:
                _log.warning(
                    "verify_fill.manifest_source_unreachable",
                    table="document_chunks", doc_id=doc_id,
                )
                indeterminate += len(candidates)
            else:
                target_keys = {_manifest_key(m) for m in target_manifest}
                to_fill.extend(
                    r for r in candidates
                    if (r["position"], (r.get("chash") or "")[:32]) not in target_keys
                )

        missing += len(to_fill)
        if to_fill:
            for i in range(0, len(to_fill), batch_size):
                batch = to_fill[i : i + batch_size]
                _etl_batch_with_breaker(import_fn, doc_id, batch, breaker=breaker)
                filled += len(batch)
                _log.info(
                    "verify_fill.batch_filled",
                    table="document_chunks", doc_id=doc_id,
                    batch_start=i, batch_size=len(batch),
                )

    return {
        "source_count": len(source_rows),
        "missing": missing,
        "filled": filled,
        "indeterminate": indeterminate,
    }


def _manifest_key(row: Any) -> tuple[int, str]:
    """``(position, chash[:32])`` from a target manifest row of EITHER shape.

    R3 substantive-critic finding (2026-07-02): the only real manifest fetch,
    ``HttpCatalogClient.get_manifest``, returns ``list[ManifestRow]``
    (frozen dataclass, attribute access — RDR-168 return-type parity),
    while test fakes and wire-shaped callers hand in plain dicts. A
    dict-only key extraction would raise ``TypeError`` the moment .5/.6
    wire in the real client. Accept both shapes here so the
    :class:`ManifestSource` contract is satisfied by ``get_manifest``
    verbatim.
    """
    if isinstance(row, dict):
        return (row["position"], (row.get("chash") or "")[:32])
    return (row.position, (row.chash or "")[:32])
