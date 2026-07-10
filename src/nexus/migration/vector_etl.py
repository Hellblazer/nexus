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
from nexus.retry import EtlCircuitBreaker, _etl_batch_with_breaker
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
EPHEMERAL_EXCLUDE_PREFIXES: tuple[str, ...] = ("tuples__",)

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

    leg: Literal["local", "cloud"]
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


def cross_model_target_name(source: str, target_model: str) -> str:
    """Remap a conformant collection name's model segment to *target_model*.

    RDR-162 cross-model migrate: a legacy ``minilm-l6-v2-384`` source is
    re-embedded into a ``bge-base-en-v15-768`` target — same content_type, owner,
    and version segments, only the model segment swapped. The service then
    re-embeds the (model-agnostic) stored chunk text with the target model and
    accepts the upsert (its name now matches the wired embedder; RDR-109 /
    nexus-pebfx.2 guard satisfied without weakening it).

    nexus-nb7hr: a TWO-segment pre-RDR-103 source (``content__owner``) gets a
    conformant name SYNTHESIZED by appending the model + ``v1`` segments —
    the measured-dim override makes such collections remappable, and the
    target must dim-dispatch, so it needs a model segment. Other segment
    counts still raise (three-segment names are not a known legacy shape;
    inventing semantics for them would mask genuine corruption).
    """
    segments = source.split("__")
    if len(segments) == 2:
        return f"{source}__{target_model}__v1"
    if len(segments) != 4:
        raise ValueError(
            f"cannot remap non-conformant collection name '{source}' "
            "(<content_type>__<owner>__<model>__v<n>)"
        )
    segments[2] = target_model
    return "__".join(segments)


def _nonconformant_id(ids: list[str]) -> str | None:
    """First id violating the 32-char chash identity, else ``None``.

    GH #1390 / nexus-sot7v: pre-RDR-108-era Chroma stores hold 16/18-char
    chunk ids. The pgvector side keys on ``sha256(chunk_text)[:32]`` and the
    server never recomputes chash from text, so a verbatim copy of such ids
    409s on the chash length CHECK per upsert — the wall that pushed an
    autonomous session into dropping the constraints. This guard fails the
    collection CLEANLY, client-side, before the batch is ever sent.
    """
    return next((i for i in ids if len(i) != 32), None)


def _legacy_id_failure_reason(collection: str, example: str) -> str:
    """The actionable (and agent-facing) failure text for a legacy-id hit."""
    return (
        f"legacy non-32-char chunk id {example!r} in {collection!r} "
        "(pre-RDR-108 era) — the pgvector chash identity is "
        "sha256(chunk_text)[:32] and the migration will NOT rewrite ids. "
        "Re-index this collection from its source content, then re-run the "
        "migration. Do NOT drop or weaken the chash length constraints to "
        "force the upserts through: that silently corrupts the store and "
        "crash-loops a later engine upgrade (GH #1390)."
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
    the explicit-override contract stand. Use :func:`is_never_written` when a
    caller (like the migration driver's collision guard, which always runs
    in a default-enumeration context) needs the broader "will the DEFAULT
    run ever actually write this" predicate.
    """
    if name not in _DERIVED_COLLECTIONS:
        return False
    dim, _reason = _dim_for_collection(target)
    return dim is None


def is_ephemeral_excluded(name: str) -> bool:
    """Whether *name* carries an :data:`EPHEMERAL_EXCLUDE_PREFIXES` prefix
    (session-ephemeral tuplespace state, e.g. ``tuples__*``) — the same
    prefix test :func:`migrate_collections` / :func:`migrate_cloud` /
    :func:`verify_fill_collections` apply in their DEFAULT (non-explicit)
    enumeration loops. A single named predicate so the three call sites
    (and :func:`is_never_written`) can never drift on the prefix check
    itself (nexus-5b9v0 Fix A).

    Callers remain responsible for the ``not explicit`` gate — this
    function only tests the name, matching the existing enumeration-loop
    contract where explicit ``--collections`` naming overrides the
    exclusion (``TestEphemeralExclusion.test_explicit_naming_overrides_exclusion``).
    """
    return name.startswith(EPHEMERAL_EXCLUDE_PREFIXES)


def is_never_written(name: str, target: str) -> bool:
    """Whether the ETL's DEFAULT (non-explicit) enumeration will NEVER
    actually write *name* anywhere, regardless of its source data.

    True iff EITHER *target* cannot dim-dispatch (:func:`_dim_for_collection`
    returns ``None`` — the exact condition guarding EVERY branch inside
    :func:`_skip_result_for_nonconformant`'s ``if dim is not None`` early
    return: ``skipped-derived``, ``skipped-empty``, and the generic
    ``skipped`` fallback all terminate there without ever reaching an
    upsert) OR *name* carries an ephemeral-exclusion prefix
    (:func:`is_ephemeral_excluded` — handled by a wholly separate
    enumeration-loop branch, never through
    :func:`_skip_result_for_nonconformant` at all). Used by the migration
    driver's pre-flight collision guard
    (``driver._assert_no_target_name_collisions``), which always runs in a
    default-enumeration context (``run_guided_upgrade`` never passes an
    explicit ``collections=`` list to ``migrate_local`` / ``migrate_cloud``).

    This is the unifying predicate over EVERY never-written disposition
    :func:`_skip_result_for_nonconformant` can produce, not an enumerated
    allowlist of specific classes — nexus-5b9v0 round-3 review found the
    prior two-class formulation (``is_derived_skip(name, target) or
    is_ephemeral_excluded(name)``) missed a third: a generic nonconformant
    collection (not on the :data:`_DERIVED_COLLECTIONS` allowlist, not
    ephemeral) that HAS data still disposes to a plain ``"skipped"``
    verdict and is therefore ALSO never written, yet the old formulation
    returned ``False`` for it. Since ``is_derived_skip(name, target)``
    implies ``dim is None`` by construction (see its own docstring), the
    ``dim is None`` disjunct here is a strict superset — folding it in
    only ever WIDENS the predicate, never narrows it, so no prior
    never-written case regresses.

    Deliberately NOT substituted at :func:`_skip_result_for_nonconformant`'s
    own ``is_derived_skip`` call site — that site must preserve the
    explicit-``--collections``-override nuance for ephemeral collections
    (see :func:`is_derived_skip`'s docstring), which this guard's
    always-default-enumeration caller does not need to reproduce.
    """
    dim, _reason = _dim_for_collection(target)
    return dim is None or is_ephemeral_excluded(name)


def _skip_result_for_nonconformant(
    read_client: Any, name: str, target: str,
) -> tuple[int | None, CollectionResult | None]:
    """Resolve *target*'s pgvector dim, or a terminal skip verdict.

    Shared by :func:`_migrate_one` and :func:`_verify_fill_one` (nexus-s3dd4.6)
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


def _excluded_ephemeral_result(read_client: Any, name: str) -> CollectionResult:
    """Terminal ``excluded`` verdict for a ``tuples__*`` (session-ephemeral)
    collection under DEFAULT (non-explicit) enumeration. Shared by
    :func:`migrate_collections` and the ingest-cloud delegation pre-pass
    (:func:`migrate_cloud`, nexus-ekk4o) so the two entry points cannot
    drift on the exclusion disposition."""
    try:
        eph_count = int(read_client.get_collection(name).count())
    except Exception:  # noqa: BLE001 — count is informational here
        eph_count = 0
    return CollectionResult(
        name, eph_count, 0, "excluded",
        "session-ephemeral (dies with Chroma at P4b) — excluded from "
        "default enumeration; pass --collections to act on it",
    )


def _migrate_one(
    read_client: Any,
    vector_client: Any,
    name: str,
    *,
    dry_run: bool,
    page: int,
    target_name: str | None = None,
    breaker: EtlCircuitBreaker | None = None,
) -> CollectionResult:
    # RDR-162 cross-model migrate: when *target_name* differs from *name*, read
    # the stored chunk text from the SOURCE (*name*) but upsert + verify against
    # the TARGET (the model-remapped name). The service re-embeds the text with
    # the target's model. The pgvector dim is dispatched from the TARGET segment.
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    target = target_name or name
    is_cross_model = target != name
    _dim, skip_result = _skip_result_for_nonconformant(read_client, name, target)
    if skip_result is not None:
        return skip_result

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
            batch_ids = [c["id"] for c in batch]
            # GH #1390 / nexus-sot7v hard guard: never send a legacy-id batch
            # (fail cleanly BEFORE the write; the classification-time probe is
            # first-page-only, this is the complete per-batch backstop).
            bad_id = _nonconformant_id(batch_ids)
            if bad_id is not None:
                reason = _legacy_id_failure_reason(name, bad_id)
                _log.error(
                    "vector_etl_legacy_chunk_id",
                    collection=name,
                    target=target,
                    example_id=bad_id,
                    written=written,
                )
                return CollectionResult(
                    name, source_count, written, "failed", reason,
                    target_collection=target if is_cross_model else None,
                )
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
            # RDR-176 Gap 6 + RDR-178 Gap 3: bounded retry (+ circuit-breaker
            # pause on a sustained outage) on a transient edge 403/429/5xx /
            # connection drop / read-timeout — the upsert is idempotent on
            # (tenant, target, chash), so re-sending a batch that may have
            # partially landed is a no-op on the dupes. A genuinely dead
            # endpoint still exhausts breaker.max_trips and raises, so a bad
            # collection cannot hang the whole leg forever.
            _etl_batch_with_breaker(
                vector_client.upsert_chunks,
                target,
                batch_ids,
                [c["document"] for c in batch],
                [c["metadata"] for c in batch],
                breaker=breaker,
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


def _verify_fill_one(
    read_client: Any,
    vector_client: Any,
    name: str,
    *,
    page: int,
    target_name: str | None = None,
    breaker: EtlCircuitBreaker | None = None,
) -> CollectionResult:
    """Delta counterpart to :func:`_migrate_one` (RDR-178 wave-2,
    nexus-s3dd4.6): rather than re-sending every source chunk, diff each
    read-page batch of source ids against the TARGET's presence
    (:meth:`HttpVectorClient.existing_ids` — a membership probe scoped to the
    candidates already in hand, mirroring :func:`rollback_collections`'s own
    id-presence intersection idiom) and upsert ONLY the missing subset.
    Same-model PASSTHROUGH embeddings (nexus-hxry2) still apply, scoped to
    the missing subset — zero re-embed cost, same as a full migrate.

    Generalizes te885.1's operator-driven pg->pg reconciliation (per-
    collection chash set-difference, embeddings-verbatim upsert, zero
    Voyage cost) as the vectors leg's verify-fill consumer. Gap 8
    cross-substrate scope: the source may be the LOCAL or CLOUD Chroma read
    leg — unchanged from :func:`_migrate_one`, no new source type
    introduced (te885.1's own local-pgvector-as-source case was operator
    ad hoc; this module's source abstraction stays Chroma-only by design).

    Never-blind-fill (nexus-r0esi): ``existing_ids`` degrades to the EMPTY
    set on a transport failure (its own contract — see
    ``HttpVectorClient.existing_ids``), which is INDISTINGUISHABLE, from a
    single probe call alone, from "the target genuinely holds none of these
    ids". Mirroring :func:`rollback_collections`'s reachability-probe +
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
            present = vector_client.existing_ids(target, ids)
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

    # Post-write verification: exact TARGET count or it did not happen —
    # the same non-negotiable gate as _migrate_one's post-write check.
    target_count_after = int(vector_client.count(target))
    if target_count_after != source_count:
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
    suspicious = (
        target_count_before > 0
        and source_count > 0
        and missing_count == source_count
    )
    if suspicious:
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
    breaker: EtlCircuitBreaker | None = None,
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

    *breaker* (RDR-178 Gap 3) is a shared :class:`~nexus.retry.EtlCircuitBreaker`
    spanning every collection in this leg — defaults to a fresh instance.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    page = page_size or QUOTAS.MAX_QUERY_RESULTS
    explicit = collections is not None
    names = collections if explicit else list_collection_names(read_client)
    results: list[CollectionResult] = []
    for name in names:
        if not explicit and is_ephemeral_excluded(name):
            result = _excluded_ephemeral_result(read_client, name)
            results.append(result)
            if on_result is not None:
                on_result(result)
            continue
        t0 = time.monotonic()
        result = _migrate_one(
            read_client, vector_client, name, dry_run=dry_run, page=page,
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
    breaker: EtlCircuitBreaker | None = None,
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
        breaker=breaker,
    )


#: Minimum engine-service release carrying the async ingest-cloud job
#: contract (RDR-178 Gap 5, bead nexus-melvx) that ``migrate_cloud``
#: delegates to (nexus-ekk4o). ``POST /v1/migration/ingest-cloud`` itself
#: existed earlier (RDR-176 P4, sync-only), but the SYNCHRONOUS request
#: outlives the nginx proxy timeout on any collection past a few thousand
#: chunks (production 2026-07-01: 5 of 52 detached, no completion signal) —
#: delegation therefore requires the ASYNC contract specifically, not just
#: "the endpoint responds". Deployed + cloud-gated on engine-service-v0.1.18
#: (plan-audit gate note on nexus-ekk4o; relay 2026-07-02).
_INGEST_CLOUD_DELEGATION_MIN_VERSION: tuple[int, int, int] = (0, 1, 18)

#: Poll cadence + budget for a delegated ingest-cloud job (nexus-ekk4o).
#: 124,330 chunks at the production-observed ~105-150 chunks/s server-side
#: rate lands in single-digit minutes; 30 minutes is a generous ceiling
#: before falling back to the (slower but proven) client-mediated leg — a
#: timeout does not abort the migration, it just stops waiting.
_INGEST_CLOUD_POLL_INTERVAL_S: float = 3.0
_INGEST_CLOUD_POLL_TIMEOUT_S: float = 1800.0

#: Trigger-request timeout: the async POST is validated synchronously and
#: returns 202 immediately (the copy itself runs on the service's worker
#: pool) — this is a control-plane call, not the transfer itself.
_INGEST_CLOUD_TRIGGER_TIMEOUT_S: float = 30.0


def probe_ingest_cloud_support(
    service_url: str,
    *,
    http_get: "Callable[[str, float], Any] | None" = None,
    timeout_s: float = 5.0,
) -> bool:
    """True iff *service_url* runs an engine-service new enough to delegate
    ``migrate_cloud`` to server-side ``POST /v1/migration/ingest-cloud``
    (async job contract, RDR-178 Gap 5).

    Reuses :func:`nexus.migration.guided_upgrade.verify_service_version` —
    ONE lightweight, unauthenticated ``GET {service_url}/version`` read and a
    ``release_version >= floor`` compare — rather than inventing a bespoke
    ingest-cloud capability probe. This is the cheapest reliable signal
    available: ``/version`` is already the established engine-capability
    handshake in this codebase (``nexus.engine_version.REQUIRED_ENGINE_VERSION``, the
    ``/v1/telemetry/ids/probe`` mixed-fleet precedent in ``orchestrator.py``
    used the target ENDPOINT itself as the 404-tolerant probe, which is not
    available here — ``POST /v1/migration/ingest-cloud`` already existed
    pre-async as a SYNC-only endpoint, so a bare "does it respond" probe
    cannot distinguish sync-only from async-capable; the version floor can).

    FAIL CLOSED (returns ``False``) on ANY ambiguity — transport error,
    non-200, missing/dev/unparseable ``release_version``, or a version below
    the floor — mirroring :func:`verify_service_version`'s own contract. The
    caller (:func:`migrate_cloud`) treats a ``False`` as "use the
    client-mediated leg", never as a reason to abort.
    """
    from nexus.migration.guided_upgrade import verify_service_version  # noqa: PLC0415 — deferred to avoid CLI startup cost / import cycle

    outcome = verify_service_version(
        service_url,
        required=_INGEST_CLOUD_DELEGATION_MIN_VERSION,
        http_get=http_get,
        timeout_s=timeout_s,
    )
    if not outcome.ok:
        _log.info(
            "vector_etl_ingest_cloud_delegation_unavailable",
            service_url=service_url,
            reason=outcome.reason,
        )
    return outcome.ok


def _resolve_delegation_endpoint(vector_client: Any) -> tuple[str, str, str] | None:
    """``(base_url, token, tenant)`` for the ingest-cloud delegation HTTP
    calls, or ``None`` when the service endpoint cannot be resolved.

    Reuses :mod:`nexus.db.http_vector_client`'s ``_resolve_endpoint`` — the
    SAME lease/env resolution every other Seam B call goes through — so
    delegation never invents a second discovery path. Resolution failure
    (no supervisor lease, no configured managed endpoint) is NOT a migration
    abort: it just means delegation is skipped in favor of the
    client-mediated leg, which is how every environment worked before this
    bead landed.
    """
    try:
        from nexus.db.http_vector_client import _resolve_endpoint  # noqa: PLC0415 — deferred to avoid import cycle

        base_url, token = _resolve_endpoint()
    except Exception as exc:  # noqa: BLE001 — unresolvable endpoint => fall back, never abort
        _log.info(
            "vector_etl_ingest_cloud_endpoint_unresolvable",
            error_type=type(exc).__name__,
        )
        return None
    tenant = getattr(vector_client, "_tenant", "default") or "default"
    return base_url, token, tenant


def _ingest_cloud_trigger(
    http_client: Any,
    base_url: str,
    token: str,
    tenant: str,
    *,
    source_tenant: str,
    source_database: str,
    source_api_key: str,
    collections: list[str],
) -> str:
    """POST ``/v1/migration/ingest-cloud`` (async mode — no ``"sync"`` key).
    Returns the ``job_id``. Raises ``RuntimeError`` on any non-202 response.

    ``source_api_key`` is placed ONLY in this request's JSON body (the
    contract every ``ingest-cloud`` caller — the RDR-176 P4 gate script
    included — follows: never a header, never a log line, never echoed back
    by a caught exception). The failure message below carries the response
    STATUS + TEXT only (server-controlled, no credential ever appears in a
    response body per ``MigrationHandler``'s own credential-non-disclosure
    contract) — never the request.
    """
    resp = http_client.post(
        base_url.rstrip("/") + "/v1/migration/ingest-cloud",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Nexus-Tenant": tenant,
            "Content-Type": "application/json",
        },
        json={
            "source_tenant": source_tenant,
            "source_database": source_database,
            "source_api_key": source_api_key,
            "collections": collections,
        },
        timeout=_INGEST_CLOUD_TRIGGER_TIMEOUT_S,
    )
    if resp.status_code != 202:
        raise RuntimeError(
            f"ingest-cloud trigger failed: HTTP {resp.status_code}: "
            f"{resp.text[:500]}"
        )
    body = resp.json()
    job_id = body.get("job_id")
    if not job_id:
        raise RuntimeError("ingest-cloud trigger returned 202 with no job_id")
    return job_id


def _ingest_cloud_poll(
    http_client: Any,
    base_url: str,
    token: str,
    tenant: str,
    job_id: str,
    *,
    interval_s: float = _INGEST_CLOUD_POLL_INTERVAL_S,
    timeout_s: float = _INGEST_CLOUD_POLL_TIMEOUT_S,
    sleep: "Callable[[float], None]" = time.sleep,
    now: "Callable[[], float]" = time.monotonic,
) -> dict[str, Any] | None:
    """Poll ``GET /v1/migration/jobs/{job_id}`` until ``state`` is terminal
    (``done``/``failed``) or *timeout_s* elapses. Returns the terminal job
    body, or ``None`` on timeout — a timeout is NOT treated as a job failure.

    Timeout semantics (ekk4o review, 2026-07-02): a ``None`` return discards
    ALL per-collection progress — the caller falls back to the client-mediated
    leg for the WHOLE batch, including collections the server-side job may
    already have finished (unlike the granular partial-failure path, which
    credits per-collection parity). That is a bounded instance of the
    re-send class this epic fights, accepted because the server job may
    still be RUNNING: crediting a non-terminal snapshot could false-pass.
    The eventual double-copy is safe — both legs converge through the same
    chash-keyed idempotent upsert, so a late-completing delegated job merely
    overwrites identical rows
    signal (the job may still be running server-side); the caller falls back
    to the client-mediated leg for every collection in the batch rather than
    trust a non-terminal snapshot."""
    deadline = now() + timeout_s
    while True:
        resp = http_client.get(
            base_url.rstrip("/") + f"/v1/migration/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}", "X-Nexus-Tenant": tenant},
        )
        if resp.status_code == 200:
            body = resp.json()
            if body.get("state") in ("done", "failed"):
                return body
        if now() >= deadline:
            return None
        sleep(interval_s)


def _delegate_ingest_cloud(
    names: list[str],
    *,
    tenant: str,
    database: str,
    api_key: str,
    base_url: str,
    token: str,
    nexus_tenant: str,
    on_result: "Callable[[CollectionResult], None] | None" = None,
    http_client: Any = None,
    poll_interval_s: float = _INGEST_CLOUD_POLL_INTERVAL_S,
    poll_timeout_s: float = _INGEST_CLOUD_POLL_TIMEOUT_S,
    sleep: "Callable[[float], None]" = time.sleep,
    now: "Callable[[], float]" = time.monotonic,
) -> tuple[list[CollectionResult], list[str]]:
    """Trigger + poll ONE server-side ``ingest-cloud`` job covering *names*
    (nexus-ekk4o). Returns ``(delegated_results, fallback_names)`` —
    *fallback_names* is disjoint from the collections in *delegated_results*
    and MUST be re-attempted via the client-mediated leg by the caller; a
    collection never appears in both.

    Batches every eligible collection into ONE job (rather than one job per
    collection): the async contract already tracks per-collection progress
    in ``per_collection`` and dedups on the job's (tenant, collection-set)
    idempotency key, so one round-trip covers the whole eligible set.

    Credential non-disclosure: ``source_api_key`` reaches exactly ONE call
    (:func:`_ingest_cloud_trigger`'s request body) and is never captured by
    any variable that a log statement below touches.
    """
    import httpx  # noqa: PLC0415 — deferred; only needed on the delegation path

    owns_client = http_client is None
    client = http_client if http_client is not None else httpx.Client()
    t0 = time.monotonic()
    try:
        try:
            job_id = _ingest_cloud_trigger(
                client, base_url, token, nexus_tenant,
                source_tenant=tenant, source_database=database,
                source_api_key=api_key, collections=names,
            )
        except Exception as exc:  # noqa: BLE001 — any trigger failure => full fallback, never abort
            _log.warning(
                "vector_etl_ingest_cloud_trigger_failed",
                collections=names, error_type=type(exc).__name__,
            )
            return [], list(names)

        _log.info(
            "vector_etl_ingest_cloud_job_started", job_id=job_id, collections=names,
        )
        job = _ingest_cloud_poll(
            client, base_url, token, nexus_tenant, job_id,
            interval_s=poll_interval_s, timeout_s=poll_timeout_s,
            sleep=sleep, now=now,
        )
    finally:
        if owns_client:
            client.close()

    if job is None:
        _log.warning(
            "vector_etl_ingest_cloud_poll_timeout",
            job_id=job_id, collections=names,
            reason=(
                "job did not reach a terminal state within the poll timeout "
                "-- falling back to the client-mediated leg for all requested "
                f"collections (the server-side job may still be running; "
                f"poll GET /v1/migration/jobs/{job_id} to check)"
            ),
        )
        return [], list(names)

    per_collection = job.get("per_collection") or {}
    results: list[CollectionResult] = []
    failed: list[str] = []
    for name in names:
        entry = per_collection.get(name)
        if entry is None:
            failed.append(name)
            continue
        copied = int(entry.get("copied", 0))
        dest = int(entry.get("dest", 0))
        if dest != copied:
            _log.warning(
                "vector_etl_ingest_cloud_collection_parity_mismatch",
                collection=name, copied=copied, dest=dest, job_id=job_id,
            )
            failed.append(name)
            continue
        result = CollectionResult(
            name, copied, dest, "migrated",
            duration_s=round(time.monotonic() - t0, 3),
            delegated=True,
        )
        results.append(result)
        if on_result is not None:
            on_result(result)

    if failed:
        _log.warning(
            "vector_etl_ingest_cloud_partial_fallback",
            job_id=job_id, job_state=job.get("state"),
            failed_collections=failed, job_error=job.get("error"),
        )
    return results, failed


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
    breaker: EtlCircuitBreaker | None = None,
) -> MigrationReport:
    """CLOUD leg: read via the ChromaCloud REST/auth API (no direct
    psql/pg_restore path exists) and write through the same pgvector
    upsert. Credentials fall back to the configured ``chroma_*`` values.

    DELEGATION (nexus-ekk4o, RDR-176 P4 / RDR-178 Gap 5): before falling
    back to the client-mediated leg (every chunk trombones ChromaCloud ->
    laptop -> engine over the operator's uplink), this probes whether the
    target engine-service supports server-side ``ingest-cloud`` delegation
    (:func:`probe_ingest_cloud_support`) and, when it does, triggers ONE
    batched async job for every ELIGIBLE collection — same-name (no
    :data:`target_names` remap; the delegated endpoint has no cross-model
    remap capability, it copies stored vectors verbatim) and dim-dispatchable
    (:func:`_dim_for_collection`). A collection the delegated job could not
    complete (or that was never eligible — cross-model, non-conformant,
    ephemeral-excluded, or the whole probe/trigger/poll path failing) falls
    back to the UNCHANGED client-mediated :func:`migrate_collections` path —
    delegation is a pure optimization layered in front of it, never a
    replacement that can drop a collection.

    ``dry_run`` skips delegation entirely (it is a source-count-only
    pre-flight; there is nothing to delegate).
    """
    read_client = open_cloud_read_client(
        tenant=tenant, database=database, api_key=api_key
    )
    if dry_run:
        return migrate_collections(
            read_client, vector_client, leg="cloud", collections=collections,
            dry_run=True, page_size=page_size, on_result=on_result,
            target_names=target_names, breaker=breaker,
        )

    explicit = collections is not None
    names = collections if explicit else list_collection_names(read_client)
    tmap = target_names or {}

    excluded: list[CollectionResult] = []
    candidates: list[str] = []
    for name in names:
        if not explicit and is_ephemeral_excluded(name):
            result = _excluded_ephemeral_result(read_client, name)
            excluded.append(result)
            if on_result is not None:
                on_result(result)
        else:
            candidates.append(name)

    delegated_results: list[CollectionResult] = []
    remaining = candidates
    endpoint = _resolve_delegation_endpoint(vector_client)
    if endpoint is not None:
        base_url, token, nexus_tenant = endpoint
        if probe_ingest_cloud_support(base_url):
            eligible = [
                n for n in candidates
                if tmap.get(n, n) == n and _dim_for_collection(n)[0] is not None
            ]
            if eligible:
                delegated_results, _fallback = _delegate_ingest_cloud(
                    eligible, tenant=tenant, database=database, api_key=api_key,
                    base_url=base_url, token=token, nexus_tenant=nexus_tenant,
                    on_result=on_result,
                )
                delegated_ok = {r.collection for r in delegated_results}
                remaining = [n for n in candidates if n not in delegated_ok]

    rest = migrate_collections(
        read_client, vector_client, leg="cloud", collections=remaining,
        dry_run=False, page_size=page_size, on_result=on_result,
        target_names=target_names, breaker=breaker,
    )
    combined = tuple(excluded) + tuple(delegated_results) + rest.results
    report = MigrationReport(leg="cloud", results=combined)
    _log.info(
        "vector_etl_leg_complete",
        leg="cloud",
        collections=len(combined),
        total_source=report.total_source,
        total_written=report.total_written,
        ok=report.ok,
        delegated=len(delegated_results),
    )
    return report


def verify_fill_collections(
    read_client: Any,
    vector_client: Any,
    *,
    leg: Literal["local", "cloud"],
    collections: list[str] | None = None,
    page_size: int | None = None,
    on_result: "Callable[[CollectionResult], None] | None" = None,
    target_names: dict[str, str] | None = None,
    breaker: EtlCircuitBreaker | None = None,
) -> MigrationReport:
    """Delta (verify-fill) counterpart to :func:`migrate_collections`
    (RDR-178 wave-2, nexus-s3dd4.6): per collection, diff the source's
    chashes against the target's presence and upsert ONLY the missing
    subset — never a full re-send. See :func:`_verify_fill_one` for the
    diff/fill mechanics and the never-blind-fill (``indeterminate``)
    safeguard.

    Mirrors :func:`migrate_collections`'s enumeration semantics exactly
    (default-vs-explicit collection scope, the ``EPHEMERAL_EXCLUDE_PREFIXES``
    disposition, live ``on_result`` progress, a leg-shared
    :class:`~nexus.retry.EtlCircuitBreaker`) — only the per-collection
    WORKER differs (:func:`_verify_fill_one` instead of :func:`_migrate_one`;
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


def verify_fill_local(
    local_path: str | Path,
    vector_client: Any,
    *,
    collections: list[str] | None = None,
    page_size: int | None = None,
    on_result: "Callable[[CollectionResult], None] | None" = None,
    target_names: dict[str, str] | None = None,
    breaker: EtlCircuitBreaker | None = None,
) -> MigrationReport:
    """LOCAL leg verify-fill: open the on-disk store (same single-opener
    discipline as :func:`migrate_local`) and diff+fill the delta only."""
    read_client = open_local_read_client(local_path)
    return verify_fill_collections(
        read_client,
        vector_client,
        leg="local",
        collections=collections,
        page_size=page_size,
        on_result=on_result,
        target_names=target_names,
        breaker=breaker,
    )


def verify_fill_cloud(
    vector_client: Any,
    *,
    tenant: str = "",
    database: str = "",
    api_key: str = "",
    collections: list[str] | None = None,
    page_size: int | None = None,
    on_result: "Callable[[CollectionResult], None] | None" = None,
    target_names: dict[str, str] | None = None,
    breaker: EtlCircuitBreaker | None = None,
) -> MigrationReport:
    """CLOUD leg verify-fill: read via the ChromaCloud REST/auth API (same
    credential fallback as :func:`migrate_cloud`) and diff+fill the delta
    only — Gap 8 cross-substrate scope (the source may be local OR cloud,
    unchanged from the full-migrate legs)."""
    read_client = open_cloud_read_client(
        tenant=tenant, database=database, api_key=api_key
    )
    return verify_fill_collections(
        read_client,
        vector_client,
        leg="cloud",
        collections=collections,
        page_size=page_size,
        on_result=on_result,
        target_names=target_names,
        breaker=breaker,
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
