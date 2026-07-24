# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""PG-reconcile core — the SURVIVING verify-fill machinery (RDR-155 P4b P0e).

P0e rehome (nexus-g37fr plan v3, partition record T2
``nexus/p4b-sqlite-partition-2026-07-23``): this module is the new
permanent home of the verify-fill (delta reconcile) core that used to
live in :mod:`nexus.migration.vector_etl`. ``vector_etl`` — the
Chroma→pgvector migration ETL — DELETES WHOLE-FILE at P2 of the combined
7.0.0 wave; the pg-source reconcile (nexus-te885.8: rows written directly
to LOCAL pgvector post-cutover, reconciled into the cloud target) is a
standing operational capability, not migration plumbing, so it moves here
first. ``vector_etl`` keeps thin re-export shims delegating to this
module until it dies; dying consumers (``SubstrateEtlRung``,
``storage_cmd``) stay pointed at ``vector_etl`` and die with it.

What lives here (moved verbatim — pure move, no behavior change):

* :func:`verify_fill_collections` — the per-collection diff/fill loop.
* :func:`_verify_fill_one` — the delta worker (never-blind-fill guard).
* :func:`verify_fill_pg_source` + :func:`resolve_local_service_endpoint`
  — the local-PG→cloud reconcile entry point (nexus-te885.8.2).
* The shared classification closure both the above and ``vector_etl``'s
  dying full-migrate legs consume: :class:`CollectionResult` /
  :class:`MigrationReport` / ``MigrationStatus``, the model/dim
  registries, the nonconformant/derived/ephemeral disposition helpers,
  and the id-page iterator. ``vector_etl`` re-imports these from here
  (surviving module never imports from the dying one).

P2 NOTE: :func:`_iter_id_pages` / :func:`verify_fill_collections` consume
the substrate-neutral pagers ``iter_collection_chunks`` /
``list_collection_names`` from :mod:`nexus.migration.chroma_read`. Those
two functions operate on any Chroma-SHAPED client — including the
surviving :class:`~nexus.migration.pg_read.PgReadClient` — so when
``chroma_read``'s Chroma OPENERS die with the Chroma wave, the pagers
must be rehomed (here or alongside ``pg_read``), not deleted with them.
"""
from __future__ import annotations

from collections.abc import Iterator
import dataclasses
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

import structlog

from nexus.db.limits import QUOTAS
from nexus.retry import EtlCircuitBreaker, _etl_batch_with_breaker
from nexus.migration.chroma_read import (
    iter_collection_chunks,
    list_collection_names,
)
from nexus.migration.pg_read import PgReadClient

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
# "skipped-derived" (RDR-178 Gap 6, nexus-t0p7o): a nonconformant collection
# on the EXPLICIT :data:`_DERIVED_COLLECTIONS` allowlist — DERIVED data
# recomputable from a durable source (taxonomy centroids from T2 topics via
# `nx taxonomy discover`), never the migration's source of truth. A
# real-content run must report clean even though this collection cannot
# dim-dispatch. Distinct from "skipped-empty": the exemption is about WHY
# the collection is exempt (derived, not lost data), not its row count —
# unlike an empty nonconformant collection, a non-empty derived one is
# reported here too and still does not redden the run. This is an
# EXPLICIT opt-in registry, never a blanket allow: any nonconformant name
# NOT on the registry still falls through to "skipped" and stays red (the
# guard `test_nonconformant_collection_skipped_loud` protects).
# "verified" / "filled" / "indeterminate" (RDR-178 wave-2, nexus-s3dd4.6):
# the verify-fill (delta) counterpart's terminal states, generalizing
# te885.1's operator-driven pg->pg reconciliation. "verified" is the
# no-op path (target already holds every source chash — zero upsert
# calls). "filled" means a genuine (possibly partial) hole was found and
# ONLY the missing chashes crossed the wire. "indeterminate" is the
# nexus-r0esi never-blind-fill guard: the target-presence probe looked
# unreliable (mirrors rollback_collections' own "swallowed error"
# signature — see :func:`_verify_fill_one`), so the collection needs
# operator attention even though the writes that DID happen are safe
# (every upsert is idempotent on (tenant, target, chash)).
MigrationStatus = Literal[
    "migrated", "failed", "skipped", "skipped-empty", "skipped-derived",
    "excluded", "dry-run", "verified", "filled", "indeterminate",
]

#: Collection-name prefixes excluded from DEFAULT enumeration (explicit
#: --collections naming overrides). Session-ephemeral, die-with-Chroma data.
EPHEMERAL_EXCLUDE_PREFIXES: tuple[str, ...] = (
    "tuples__",
    # nexus-xukbj: quarantine siblings hold soft-deleted orphans awaiting
    # expiry — never worth migrating as first-class data.
    "quarantine-",
)

#: Nonconformant collection names known to hold DERIVED data — recomputable
#: from a durable source, so their absence from pgvector is not data loss.
#: ``taxonomy__centroids`` (447 rows in the 2026-07-01 production dry-run)
#: is regenerated on the target post-cutover via ``nx taxonomy discover``
#: (source of truth: T2 ``topics``/``topic_assignments``). Adding a name
#: here is a deliberate, reviewed claim that the collection is safely
#: regenerable — it is NOT a general nonconformant-name allowlist.
_DERIVED_COLLECTIONS: frozenset[str] = frozenset({"taxonomy__centroids"})

#: Human-readable regeneration hint appended to every derived-skip reason.
_DERIVED_SKIP_HINT = "skipped (derived — regenerate on target via nx taxonomy)"

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
    #: verify-fill only (nexus-s3dd4.6): source chashes NOT found present in
    #: the target by the :meth:`HttpVectorClient.existing_ids` probe. ``0``
    #: for a full (non-delta) migrate result — there is no "missing" concept
    #: on that path (every chunk is unconditionally sent).
    missing_count: int = 0
    #: verify-fill only: chashes ACTUALLY transmitted this run — the P6
    #: regression's load-bearing assertion is ``filled_count == hole size``,
    #: never ``source_count``. ``0`` for a full migrate result (that path's
    #: "everything sent" cost is already captured by ``written_count``).
    filled_count: int = 0
    #: nexus-ekk4o (RDR-176 P4 / RDR-178 Gap 5): True when this collection's
    #: chunks were copied SERVER-SIDE via the ``/v1/migration/ingest-cloud``
    #: delegation (the engine pulls ChromaCloud directly at datacenter
    #: bandwidth) rather than the client-mediated leg (every chunk trombones
    #: ChromaCloud -> laptop -> engine). ``False`` for every non-delegated
    #: result, including the client-mediated fallback for a collection the
    #: delegated job could not complete.
    delegated: bool = False


@dataclass(frozen=True)
class MigrationReport:
    """One leg's migration outcome.

    ``ok`` is True only when every collection landed in a clean terminal
    state (``migrated`` or ``dry-run``). A skipped or failed collection
    makes the whole report not-ok — partial migrations demand explicit
    operator handling, never a green light.
    """

    leg: Literal["local", "cloud", "pg"]
    results: tuple[CollectionResult, ...]

    @property
    def ok(self) -> bool:
        return all(
            r.status in (
                "migrated", "dry-run", "skipped-empty", "skipped-derived",
                "excluded", "verified", "filled",
            )
            for r in self.results
        )

    @property
    def total_source(self) -> int:
        return sum(r.source_count for r in self.results)

    @property
    def total_written(self) -> int:
        return sum(r.written_count for r in self.results)

    @property
    def derived_skipped_count(self) -> int:
        """Count of collections skipped as known-derived (RDR-178 Gap 6) —
        reported separately from ``failed``/``skipped`` in the run summary
        so an operator can see "regenerate these" apart from "fix these"."""
        return sum(1 for r in self.results if r.status == "skipped-derived")

    @property
    def failed_or_skipped_count(self) -> int:
        """Count of collections in a red terminal state (``failed`` or
        ``skipped``) — the ones that actually need operator attention,
        as opposed to :attr:`derived_skipped_count` (informational only)."""
        return sum(1 for r in self.results if r.status in ("failed", "skipped"))


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


def _nonconformant_id(ids: list[str]) -> str | None:
    """First id violating the canonical chash identity, else ``None``.

    GH #1390 / nexus-sot7v lineage: this guard fails a collection CLEANLY,
    client-side, before any batch is sent — never a mid-transaction CHECK
    409 (the wall that once pushed an autonomous session into dropping the
    constraints). RDR-180 (nexus-jxizy.3): the conformant id is the FULL
    64-hex sha256 (``wire_reid.derive_wire_chash`` output); the pre-flip
    ``len != 32`` spelling would reject every correctly re-id'd batch.
    """
    return next((i for i in ids if len(i) != 64), None)


def _legacy_id_failure_reason(collection: str, example: str) -> str:
    """The actionable (and agent-facing) failure text for a legacy-id hit."""
    return (
        f"non-canonical chunk id {example!r} in {collection!r} "
        "(pre-RDR-108 era, or a truncated derivation) — the pgvector chash "
        "identity is the FULL sha256(chunk_text) hexdigest (RDR-180) and "
        "the migration will NOT guess ids. "
        "Re-index this collection from its source content, then re-run the "
        "migration. Do NOT drop or weaken the chash length constraints to "
        "force the upserts through: that silently corrupts the store "
        "(GH #1390 — and on a pre-v0.1.48 char-era engine it also "
        "crash-loops the next boot)."
    )


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


def is_derived_skip(name: str, target: str) -> bool:
    """Whether *name* would be dispositioned ``skipped-derived`` for *target*.

    True iff *target* cannot dim-dispatch (non-conformant name / unknown
    model segment) AND *name* is on the explicit :data:`_DERIVED_COLLECTIONS`
    allowlist — exactly the condition :func:`_skip_result_for_nonconformant`
    uses to route to the ``skipped-derived`` terminal state (RDR-178 Gap 6).

    This predicate is UNCONDITIONAL — it applies regardless of
    default-vs-explicit enumeration (an explicitly-named derived collection
    is still recomputable, so it is still skipped-derived). Contrast
    :func:`is_ephemeral_excluded`, whose exclusion applies ONLY under
    DEFAULT enumeration (explicit naming overrides it — see
    ``TestEphemeralExclusion.test_explicit_naming_overrides_exclusion``) and
    which therefore must NOT be folded into this function or into
    :func:`_skip_result_for_nonconformant`'s call site — doing so would
    silently misclassify an explicitly-named ``tuples__*`` collection as
    derived/regenerable (wrong hint text, wrong semantics) instead of letting
    the explicit-override contract stand. Use ``is_never_written``
    (:mod:`nexus.migration.vector_etl`) when a caller (like the migration
    driver's collision guard, which always runs in a default-enumeration
    context) needs the broader "will the DEFAULT run ever actually write
    this" predicate.
    """
    if name not in _DERIVED_COLLECTIONS:
        return False
    dim, _reason = _dim_for_collection(target)
    return dim is None


def is_ephemeral_excluded(name: str) -> bool:
    """Whether *name* carries an :data:`EPHEMERAL_EXCLUDE_PREFIXES` prefix
    (session-ephemeral tuplespace state, e.g. ``tuples__*``) — the same
    prefix test ``migrate_collections`` / ``migrate_cloud`` /
    :func:`verify_fill_collections` apply in their DEFAULT (non-explicit)
    enumeration loops. A single named predicate so the three call sites
    (and ``is_never_written``) can never drift on the prefix check
    itself (nexus-5b9v0 Fix A).

    Callers remain responsible for the ``not explicit`` gate — this
    function only tests the name, matching the existing enumeration-loop
    contract where explicit ``--collections`` naming overrides the
    exclusion (``TestEphemeralExclusion.test_explicit_naming_overrides_exclusion``).
    """
    return name.startswith(EPHEMERAL_EXCLUDE_PREFIXES)


def _skip_result_for_nonconformant(
    read_client: Any, name: str, target: str,
) -> tuple[int | None, CollectionResult | None]:
    """Resolve *target*'s pgvector dim, or a terminal skip verdict.

    Shared by ``_migrate_one`` and :func:`_verify_fill_one` (nexus-s3dd4.6)
    so the derived/nonconformant classification cannot drift between the full
    and delta entry points — a pure extraction of the original ``_migrate_one``
    logic, no behaviour change.

    Returns ``(dim, None)`` when *target* dim-dispatches (the caller proceeds),
    or ``(None, CollectionResult)`` with a terminal ``skipped*`` verdict when it
    cannot.
    """
    dim, reason = _dim_for_collection(target)
    if dim is not None:
        return dim, None
    # RDR-178 Gap 6 (nexus-t0p7o): an EXPLICIT derived-data exemption,
    # checked before the empty/nonempty disposition below — a derived
    # collection is exempt regardless of row count (its data is not
    # lost, it is recomputed on the target), unlike the generic
    # nonconformant path where only an EMPTY collection is safe.
    if is_derived_skip(name, target):
        try:
            derived_count = int(read_client.get_collection(name).count())
        except Exception:  # noqa: BLE001 - best-effort count probe; degrades to -1 sentinel
            derived_count = -1
        _log.info(
            "vector_etl_skip_derived",
            collection=name,
            count=derived_count,
        )
        return None, CollectionResult(
            name, max(derived_count, 0), 0, "skipped-derived", _DERIVED_SKIP_HINT,
        )
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
        return None, CollectionResult(
            name, 0, 0, "skipped-empty",
            reason + " (source has 0 chunks — nothing to lose)",
        )
    _log.warning("vector_etl_skip_nonconformant", collection=name, reason=reason)
    return None, CollectionResult(name, max(nc_count, 0), 0, "skipped", reason)


def _verify_fill_one(
    read_client: Any,
    vector_client: Any,
    name: str,
    *,
    page: int,
    target_name: str | None = None,
    breaker: EtlCircuitBreaker | None = None,
) -> CollectionResult:
    """Delta counterpart to ``_migrate_one`` (RDR-178 wave-2,
    nexus-s3dd4.6): rather than re-sending every source chunk, diff each
    read-page batch of source ids against the TARGET's presence
    (:meth:`HttpVectorClient.existing_ids` — a membership probe scoped to the
    candidates already in hand, mirroring ``rollback_collections``'s own
    id-presence intersection idiom) and upsert ONLY the missing subset.
    Same-model PASSTHROUGH embeddings (nexus-hxry2) still apply, scoped to
    the missing subset — zero re-embed cost, same as a full migrate.

    Generalizes te885.1's operator-driven pg->pg reconciliation (per-
    collection chash set-difference, embeddings-verbatim upsert, zero
    Voyage cost) as the vectors leg's verify-fill consumer. Gap 8
    cross-substrate scope: the source may be the LOCAL or CLOUD Chroma read
    leg — unchanged from ``_migrate_one``, no new source type
    introduced (te885.1's own local-pgvector-as-source case was operator
    ad hoc; this module's source abstraction stays Chroma-only by design).

    Never-blind-fill (nexus-r0esi): ``existing_ids`` degrades to the EMPTY
    set on a transport failure (its own contract — see
    ``HttpVectorClient.existing_ids``), which is INDISTINGUISHABLE, from a
    single probe call alone, from "the target genuinely holds none of these
    ids". Mirroring ``rollback_collections``'s reachability-probe +
    post-hoc consistency check: a whole-collection count is taken BEFORE the
    loop (``count()`` propagates service errors — unlike the presence
    lookup, it does not swallow them), and if EVERY source id across the
    WHOLE collection reads back "missing" despite the target demonstrably
    holding >0 rows overall, that is the exact "not a single lookup
    resolved despite target holding data" signature — reported
    ``indeterminate`` rather than a silently-successful ``filled``, even
    though the sends that DID happen are safe (idempotent upsert). This
    check is COLLECTION-LEVEL ONLY (same granularity as
    ``rollback_collections``'s own guard): a probe that degrades on only
    ONE read-page mid-run (not the whole collection) is not caught by this
    heuristic — that page's chunks are still safely re-sent (idempotent),
    just without the anomaly being surfaced. Achieving per-page detection
    would need ``existing_ids`` itself to distinguish "empty" from
    "unreachable" (``None``, like ``verify_fill.IdentitySource.present()``
    does) — out of this bead's scope (``http_vector_client.py`` untouched).
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    target = target_name or name
    is_cross_model = target != name
    _dim, skip_result = _skip_result_for_nonconformant(read_client, name, target)
    if skip_result is not None:
        return skip_result

    try:
        read_client.get_collection(name)
    except Exception as exc:  # noqa: BLE001 — every per-collection failure is reported, not raised
        reason = f"source collection unreadable: {exc}"
        _log.error("vector_etl_verify_fill_source_unreadable", collection=name, error=str(exc))
        return CollectionResult(name, 0, 0, "failed", reason)

    # Reachability probe BEFORE any per-page lookup (mirrors
    # rollback_collections): count() propagates service errors, unlike the
    # presence lookup below, which swallows them — an unreachable target
    # fails this collection outright rather than reading as a false "target
    # is empty, everything is missing".
    try:
        target_count_before = int(vector_client.count(target))
    except Exception as exc:  # noqa: BLE001 — reported per-collection, not raised
        reason = f"target unreachable: {exc}"
        _log.error("vector_etl_verify_fill_target_unreachable", collection=name, target=target, error=str(exc))
        return CollectionResult(name, 0, 0, "failed", reason, target_collection=target if is_cross_model else None)

    passthrough = _is_same_model_passthrough(name, target)
    declared_model = name.split("__")[2] if passthrough else None

    def _provenance_ok(c: dict) -> bool:
        # Mirrors _migrate_one's MISMATCH-ONLY provenance check (nexus-bfdri)
        # — see that function's docstring for the full rationale.
        if declared_model is None:
            return False
        prov = (c.get("metadata") or {}).get("embedding_model")
        if not prov:
            return True
        return prov == declared_model

    source_count = 0
    missing_count = 0
    degraded_pages = 0
    filled_count = 0
    try:
        for batch in _iter_id_pages(read_client, name, page, include_embeddings=passthrough):
            source_count += len(batch)
            ids = [c["id"] for c in batch]
            # GH #1390 / nexus-sot7v: same hard guard as _migrate_one — a
            # verify-fill must never re-send legacy-id chunks either.
            bad_id = _nonconformant_id(ids)
            if bad_id is not None:
                reason = _legacy_id_failure_reason(name, bad_id)
                _log.error(
                    "vector_etl_verify_fill_legacy_chunk_id",
                    collection=name,
                    target=target,
                    example_id=bad_id,
                )
                return CollectionResult(
                    name, source_count, filled_count, "failed", reason,
                    target_collection=target if is_cross_model else None,
                )
            try:
                present = vector_client.existing_ids(target, ids)
            except Exception as exc:  # noqa: BLE001 — unreachable page; treated as an anomaly, not as absence
                # nexus-ou4tb: existing_ids no longer degrades to the empty
                # set, so an unreachable page is now DISTINGUISHABLE from a
                # genuinely-absent one. This docstring's own closing note said
                # per-page detection "would need existing_ids itself to
                # distinguish 'empty' from 'unreachable'" — it does now, so
                # the page is recorded as degraded rather than silently
                # counted as missing. The chunks are still re-sent (idempotent
                # upsert), exactly as before; what changes is that the anomaly
                # is no longer invisible at page granularity.
                degraded_pages += 1
                _log.warning(
                    "vector_etl_verify_fill_page_unreachable",
                    collection=name, target=target, page_ids=len(ids),
                    degraded_pages=degraded_pages, error=str(exc),
                )
                present = set()
            missing_idx = [i for i, _id in enumerate(ids) if _id not in present]
            if not missing_idx:
                continue
            missing_batch = [batch[i] for i in missing_idx]
            missing_count += len(missing_batch)

            embeddings = None
            if passthrough:
                if all(
                    c.get("embedding") is not None and _provenance_ok(c)
                    for c in missing_batch
                ):
                    embeddings = [c["embedding"] for c in missing_batch]
                else:
                    missing_vecs = sum(1 for c in missing_batch if c.get("embedding") is None)
                    mis_prov = sum(
                        1 for c in missing_batch
                        if c.get("embedding") is not None and not _provenance_ok(c)
                    )
                    _log.warning(
                        "vector_etl_verify_fill_passthrough_fallback_reembed",
                        collection=name, target=target, batch_size=len(missing_batch),
                        missing_vectors=missing_vecs, provenance_mismatch=mis_prov,
                    )

            _etl_batch_with_breaker(
                vector_client.upsert_chunks,
                target,
                [c["id"] for c in missing_batch],
                [c["document"] for c in missing_batch],
                [c["metadata"] for c in missing_batch],
                breaker=breaker,
                embeddings=embeddings,
            )
            filled_count += len(missing_batch)
    except Exception as exc:  # noqa: BLE001 — report and continue with the next collection
        reason = f"verify-fill upsert failed after {filled_count} chunks: {exc}"
        _log.error(
            "vector_etl_verify_fill_upsert_failed",
            collection=name, target=target, filled=filled_count, error=str(exc),
        )
        return CollectionResult(
            name, source_count, filled_count, "failed", reason,
            target_collection=target if is_cross_model else None,
            missing_count=missing_count, filled_count=filled_count,
        )

    # Post-write verification: the target must hold AT LEAST the source's
    # rows — the same `<` relaxation as _migrate_one's post-write check
    # (nexus-83ld0; co-resident targets legitimately exceed the source
    # count, see the comment there).
    target_count_after = int(vector_client.count(target))
    if target_count_after < source_count:
        reason = (
            f"post-write count mismatch: source={source_count} "
            f"target={target_count_after}"
        )
        _log.error(
            "vector_etl_verify_fill_count_mismatch",
            collection=name, source=source_count, target=target_count_after,
        )
        return CollectionResult(
            name, source_count, filled_count, "failed", reason,
            target_collection=target if is_cross_model else None,
            missing_count=missing_count, filled_count=filled_count,
        )

    # Suspicious-probe heuristic (see docstring): the target demonstrably
    # held data BEFORE this run, yet EVERY source id read back "missing" —
    # the rollback_collections "swallowed error" signature. Flag it even
    # though the writes landed correctly (idempotent, verified above).
    # nexus-ou4tb: a page that could not be probed at all makes the delta
    # untrustworthy regardless of the all-missing signature, and is now
    # detectable per-page rather than only collection-wide.
    suspicious = degraded_pages > 0 or (
        target_count_before > 0
        and source_count > 0
        and missing_count == source_count
    )
    if suspicious:
        if degraded_pages:
            reason = (
                f"{degraded_pages} probe page(s) were UNREACHABLE (not empty) "
                "— the presence delta for this collection cannot be trusted; "
                "writes already landed and are idempotent, but re-run the "
                "verify-fill once the service is healthy before relying on it"
            )
            _log.warning(
                "vector_etl_verify_fill_indeterminate",
                collection=name, target=target, degraded_pages=degraded_pages,
            )
            return CollectionResult(
                name, source_count, filled_count, "indeterminate", reason,
                target_collection=target if is_cross_model else None,
                missing_count=missing_count, filled_count=filled_count,
            )
        reason = (
            f"existing_ids probe reported ALL {source_count} source id(s) "
            f"missing despite the target already holding {target_count_before} "
            "row(s) — the rollback_collections 'swallowed error' signature; "
            "treating as indeterminate rather than a trusted delta (writes "
            "already landed and were verified, but the probe signal itself "
            "is not trustworthy — investigate before relying on future "
            "verify-fill runs against this collection)"
        )
        _log.warning(
            "vector_etl_verify_fill_indeterminate",
            collection=name, target=target,
            target_count_before=target_count_before, source_count=source_count,
        )
        return CollectionResult(
            name, source_count, filled_count, "indeterminate", reason,
            target_collection=target if is_cross_model else None,
            missing_count=missing_count, filled_count=filled_count,
        )

    if missing_count == 0:
        _log.info(
            "vector_etl_verify_fill_verified",
            collection=name, target=target, source_count=source_count,
        )
        return CollectionResult(
            name, source_count, 0, "verified",
            target_collection=target if is_cross_model else None,
            missing_count=0, filled_count=0,
        )

    _log.info(
        "vector_etl_verify_fill_filled",
        collection=name, target=target, source_count=source_count,
        missing=missing_count, filled=filled_count,
    )
    return CollectionResult(
        name, source_count, filled_count, "filled",
        target_collection=target if is_cross_model else None,
        missing_count=missing_count, filled_count=filled_count,
    )


def verify_fill_collections(
    read_client: Any,
    vector_client: Any,
    *,
    leg: Literal["local", "cloud", "pg"],
    collections: list[str] | None = None,
    page_size: int | None = None,
    on_result: "Callable[[CollectionResult], None] | None" = None,
    target_names: dict[str, str] | None = None,
    breaker: EtlCircuitBreaker | None = None,
) -> MigrationReport:
    """Delta (verify-fill) counterpart to ``migrate_collections``
    (RDR-178 wave-2, nexus-s3dd4.6): per collection, diff the source's
    chashes against the target's presence and upsert ONLY the missing
    subset — never a full re-send. See :func:`_verify_fill_one` for the
    diff/fill mechanics and the never-blind-fill (``indeterminate``)
    safeguard.

    Mirrors ``migrate_collections``'s enumeration semantics exactly
    (default-vs-explicit collection scope, the ``EPHEMERAL_EXCLUDE_PREFIXES``
    disposition, live ``on_result`` progress, a leg-shared
    :class:`~nexus.retry.EtlCircuitBreaker`) — only the per-collection
    WORKER differs (:func:`_verify_fill_one` instead of ``_migrate_one``;
    there is no ``dry_run`` concept here, the diff itself IS the cheap
    preview).
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    page = page_size or QUOTAS.MAX_QUERY_RESULTS
    explicit = collections is not None
    names = collections if explicit else list_collection_names(read_client)
    results: list[CollectionResult] = []
    for name in names:
        if not explicit and is_ephemeral_excluded(name):
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
        result = _verify_fill_one(
            read_client, vector_client, name, page=page,
            target_name=(target_names or {}).get(name),
            breaker=breaker,
        )
        result = dataclasses.replace(
            result, duration_s=round(time.monotonic() - t0, 3),
        )
        results.append(result)
        if on_result is not None:
            on_result(result)
    report = MigrationReport(leg=leg, results=tuple(results))
    _log.info(
        "vector_etl_verify_fill_leg_complete",
        leg=leg,
        collections=len(results),
        total_source=report.total_source,
        total_written=report.total_written,
        missing_total=sum(r.missing_count for r in results),
        filled_total=sum(r.filled_count for r in results),
        ok=report.ok,
    )
    return report


def resolve_local_service_endpoint(
    explicit_url: str | None = None,
    explicit_token: str | None = None,
) -> tuple[str, str]:
    """``(base_url, token)`` for the LOCAL pg-source read leg (nexus-te885.8.2).

    Per-field explicit override: an explicitly-given URL or token is used
    verbatim; whichever half is absent falls back to
    :func:`nexus.db.service_endpoint.discover_lease` — called DIRECTLY,
    never through :class:`~nexus.db.http_vector_client.HttpVectorClient`'s
    process-wide singleton (:func:`~nexus.db.http_vector_client.get_http_vector_client`),
    which resolves exactly one endpoint per process and is already in use
    as the migration TARGET (cloud) in the pg-source-reconcile scenario this
    resolver exists for — reusing it here would collapse two distinct
    endpoints (local source, cloud target) onto one.

    Fails loud (no silent fallback) when neither an explicit value nor a
    discoverable lease can fill a gap: a ``(None, None)`` resolution
    means no local supervisor is running and no override was given, so
    there is nothing safe to fall back to.
    """
    from nexus.db.service_endpoint import discover_lease  # noqa: PLC0415 — deferred to avoid import-cycle risk (mirrors resolve_service_endpoint's own pattern)

    url = (explicit_url or "").strip().rstrip("/") or None
    token = (explicit_token or "").strip() or None
    if url is None or token is None:
        lease_url, lease_token = discover_lease()
        url = url or lease_url
        token = token or lease_token
    if not url or not token:
        raise RuntimeError(
            "local pg-source endpoint is not resolvable (no explicit "
            "local-service URL/token AND no local supervisor lease found): "
            "start the local nexus-service with 'nx daemon service start' "
            "(publishes the endpoint lease this resolver auto-discovers), "
            "or pass local_service_url/local_token explicitly."
        )
    return url, token


def verify_fill_pg_source(
    local_service_url: str | None,
    vector_client: Any,
    *,
    local_token: str | None = None,
    collections: list[str] | None = None,
    page_size: int | None = None,
    on_result: "Callable[[CollectionResult], None] | None" = None,
    target_names: dict[str, str] | None = None,
    breaker: EtlCircuitBreaker | None = None,
) -> MigrationReport:
    """PG-SOURCE leg verify-fill (nexus-te885.8.2): reconcile rows written
    directly to LOCAL pgvector post-cutover that exist in no Chroma store at
    all — the substrate behind the 2026-07-01 nexus-te885.1 incident
    (previously reconciled once by an ad hoc manual script).

    Mirrors ``verify_fill_local``/``verify_fill_cloud``'s exact
    shape: resolve an endpoint, open a Chroma-shaped read client
    (:class:`~nexus.migration.pg_read.PgReadClient`), delegate to
    :func:`verify_fill_collections` unchanged. ``local_service_url``/
    ``local_token`` are explicit overrides; ``None`` for either falls back
    to :func:`resolve_local_service_endpoint`'s lease discovery.
    """
    base_url, token = resolve_local_service_endpoint(local_service_url, local_token)
    read_client = PgReadClient(base_url, token)
    return verify_fill_collections(
        read_client,
        vector_client,
        leg="pg",
        collections=collections,
        page_size=page_size,
        on_result=on_result,
        target_names=target_names,
        breaker=breaker,
    )
