# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-159 P4 (nexus-ue6g7.24) / RDR-180 (nexus-jxizy.10.7): the guided-upgrade
engine entry point.

``run_guided_upgrade`` is the ONE function both surfaces call — the nexus CLI
(``nx migrate-to-service``) and the deferred conexus veneer (``conexus
upgrade``). It owns the full lifecycle that wraps the P0-P3 primitives in the
single survivable order, so neither surface re-derives the seam:

  1. DETECT     open the Chroma read legs, classify the footprint, then CLOSE
                them BEFORE any landing (the local leg is WAL single-opener —
                the landing read must be the only opener, so detection cannot
                still hold it);
  2. LAND-THEN-TRANSFORM  reopen the data-bearing read legs (now for LANDING,
                not validation) and drive
                :func:`nexus.migration.sequencer.run_land_then_transform_migration`:
                quiesce -> model gate -> pre-land source census + disk
                preflight -> land (pointer stores verbatim + chunk content,
                honest land-time classification) -> non-chash T2 -> per
                collection (embed_fill, promote) -> finalize (once per wave)
                -> verify (count-parity reconciliation) -> clear staging ->
                mark_migrated. Reopened read legs are closed in a
                ``finally``.

RDR-180 retires the old three-phase DETECT / SEQUENCE / VALIDATE shape (the
per-leg in-flight rewrite class it produced — eight missed-leg bugs, see
``tests/test_no_chash_truncation.py``'s history) in favor of the
staging-schema land-then-transform design (nexus_rdr/180-land-transform-design
Q3-Q5): the client's role collapses to CENSUS the source, LAND it verbatim,
drive ``embed_fill -> promote`` per collection and ``finalize`` per wave,
VERIFY counts, CLEAR. The server-side ``StagingPromoteOps`` (Java) owns the
transactional re-id/promote pass and the reference re-point (catalog
manifest, topic assignments) via the in-DB ``chash_alias`` join — there is no
more client-side ``remap_refs`` seam to inject.

The engine touches NO data of its own and adds NO orchestration beyond
sequencing + lifecycle: every ETL / state / validation primitive is the proven
P0-P3 code. Collaborators (clients, paths) are injected; the low-level engine
functions are module globals so a unit test monkeypatches ``driver.<name>``
without a live service / Chroma (the existing nexus CLI-wiring test idiom).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import structlog

from nexus.migration.detection import (
    CollectionClassification,
    DetectionReport,
    classify_collections,
    close_read_client,
    cross_model_remappable,
    is_measured_dim_override,
    open_read_legs,
    probe_has_text,
    remap_target_model,
    resolve_default_local_leg,
    voyage_key_available,
)
from nexus.migration.orchestrator import EtlSources
from nexus.migration.pregate import (
    assert_disk_headroom,
    assert_models_supported,
    estimate_staging_source_bytes,
)
from nexus.migration.sequencer import (
    LandThenTransformOutcome,
    SequenceOutcome,
    run_land_then_transform_migration,
)
from nexus.migration.staging_land import (
    HttpStagingStore,
    chunk_rows,
    pointer_store_rows,
    source_census,
    topic_assignment_orphans,
)
from nexus.migration.state import clear_state
from nexus.migration.validation import ValidationOutcome
from nexus.migration.vector_etl import _dim_for_collection, cross_model_target_name, is_never_written

_log = structlog.get_logger(__name__)

#: The seven non-``chunks`` staging stores landed verbatim from SQLite —
#: mirrors the engine's ``StagingHandler.STORES`` minus ``"chunks"``, which
#: lands from the Chroma source instead (see ``_land`` inside
#: :func:`run_guided_upgrade`).
_POINTER_STORES: tuple[str, ...] = (
    "document_chunks",
    "chash_index",
    "topic_assignments",
    "frecency",
    "relevance_log",
    "document_aspects",
    "aspect_extraction_queue",
)


@dataclass(frozen=True)
class GuidedUpgradeResult:
    """The verdict of one guided-upgrade run + the evidence behind it.

    ``ok`` is the single user-facing success bit: True only on a fresh-user
    no-op OR a land-then-transform run that reached a verified, staging-cleared
    ``migrated``. ``validation`` is ``None`` when nothing was verified (a
    fresh-user no-op, or ANY block along the census / land / T2 / promote /
    finalize / verify / clear chain) — RDR-180 folds the old separate VALIDATE
    phase into the sequence's own ``verify`` step, so there is no longer a
    "T3 copy done but validation failed" middle state to represent; a run is
    either fully verified (``ok=True``, ``validation.unlocked=True``) or it
    never completed (``validation=None``).

    ``sequence`` carries a :class:`~nexus.migration.sequencer.LandThenTransformOutcome`
    for a real run (the field name is retained from the pre-RDR-180 shape for
    caller compatibility — ``migrate_cmd.py``'s ``_render_result`` and the
    cross-repo signature pin in ``test_migration_contract.py`` both consume it
    by duck-typed attribute, not by import type); it stays typed to accept the
    retired :class:`~nexus.migration.sequencer.SequenceOutcome` too so a test
    double built against either shape remains valid.
    """

    detection: DetectionReport
    sequence: SequenceOutcome | LandThenTransformOutcome
    validation: ValidationOutcome | None
    ok: bool

    @property
    def rollback_available(self) -> bool:
        """Whether a rollback is offered.

        Always ``False`` under land-then-transform: RDR-180 design Q4
        "rollback: SIMPLIFIED — staging makes it trivial (nothing promoted on
        abort => drop/truncate staging, nexus never touched)". Recovery from
        any block is re-run (landing + promote are idempotent), never an
        explicit ``nx storage migrate vectors --rollback`` command — there is
        no separate pgvector copy to unwind, only staging rows retained for
        resume.
        """
        return self.validation is not None and self.validation.rollback_available


def _describe_colliding_source(c: CollectionClassification) -> str:
    """Render one colliding source's classification for the operator
    (nexus-5b9v0 Fix 3) — leg, model, measured dim, and the diagnostic
    ``reason`` already computed at classify-time — so the exception message
    is a menu ("here is which one to remove/re-index"), not a puzzle the
    operator has to re-derive by hand from a throwaway repro.

    nexus-5b9v0 Fix C: a classification that is a measured-dim override
    (:func:`~nexus.migration.detection.is_measured_dim_override` — the
    declared name/model says unsupported, but a stored vector measured as
    local bge/ONNX) is explicitly flagged ``LIKELY STALE`` — this is
    precisely the pre-RDR-109 mislabel case the guard exists for, so the
    operator does not have to compare bullets by hand to find it; the
    honest, non-remapped sibling gets no such flag.
    """
    bits = [f"leg={c.leg}", f"model={c.model or 'unknown'}"]
    if c.measured_dim is not None:
        bits.append(f"measured {c.measured_dim}-dim")
    detail = ", ".join(bits)
    reason = f" — {c.reason}" if c.reason else ""
    flag = " — LIKELY STALE (name/model mismatch)" if is_measured_dim_override(c) else ""
    return f"{c.collection} ({detail}){reason}{flag}"


class TargetNameCollisionBlocked(RuntimeError):
    """Raised before any ETL when two or more distinct source collections would
    resolve to the same final migration target name.

    nexus-5b9v0: a pre-RDR-109 collection misnamed with a voyage-model token
    (nexus-59vl / GH#667) but holding MEASURED bge-768 vectors is correctly
    cross-model-remapped onto ``bge-base-en-v15-768`` — but that can be the
    EXACT target name of an honest, non-remapped sibling collection already
    migrating under its own name (or of a second remapped collection that
    measures to the same target independently). Both would write into the same
    pgvector target under one name: depending on whether their chashes
    overlap, this either double-counts the post-write verification (a loud but
    confusing ``vector_etl_count_mismatch``) or silently merges the two
    collections' content with no error at all — strictly worse. This gate
    fails BEFORE any collection is opened for write, while the fix (drop or
    re-index the stale/duplicate collection) is still cheap and safe.

    ``.collisions`` maps each colliding target name to the list of distinct
    :class:`~nexus.migration.detection.CollectionClassification` objects that
    would write into it (len >= 2 per entry) — the FULL classification, not
    just the bare collection name (nexus-5b9v0 Fix 3), so a caller (or the
    rendered message below) can tell which colliding source is the stale
    pre-RDR-109 mislabel versus the honest sibling without re-deriving the
    classification by hand.
    """

    def __init__(self, collisions: dict[str, list[CollectionClassification]]) -> None:
        self.collisions = collisions
        lines = "\n".join(
            f"  - target {target!r} would be written by {len(sources)} "
            "distinct source collections:\n"
            + "\n".join(
                f"      * {_describe_colliding_source(c)}" for c in sources
            )
            for target, sources in collisions.items()
        )
        super().__init__(
            "migration blocked: target-name collision detected across "
            f"{len(collisions)} target collection(s) (no data has been "
            "touched) — this is typically a nexus-59vl-era misnamed "
            "collection colliding with an honest sibling (or two mislabeled "
            "collections measuring to the same target); resolve by removing "
            "or re-indexing the stale/duplicate collection before "
            f"migrating:\n{lines}\n"
            "Run 'nx migration-audit' for the full retroactive report "
            "(whether an EARLIER run already merged these, and per-source "
            "presence in the live target)."
        )


def build_cross_model_target_names(
    detection: "DetectionReport", *, voyage_key_present: bool
) -> dict[str, str]:
    """The source→target remap map exactly as the guided upgrade builds it.

    Extracted from :func:`run_guided_upgrade` (nexus-p9vqa) so the
    retroactive collision audit (:mod:`nexus.migration.collision_audit`)
    reconstructs the historical target-name map with the SAME policy chain
    (``cross_model_remappable`` → ``remap_target_model`` →
    ``cross_model_target_name``) the migration itself uses — a drifted
    reimplementation would audit a map no run ever produced. RDR-180: this is
    ALSO the land-time honest-target derivation ``_land`` (inside
    :func:`run_guided_upgrade`) resolves chunk rows against — the same
    reconciliation H1 the sequencer's own docstring names.
    """
    return {
        c.collection: cross_model_target_name(
            c.collection,
            # nexus-nb7hr: measured-768 collections target ONNX in every
            # mode (provably-bge content must not bill a voyage re-embed).
            remap_target_model(c, voyage_key_present=voyage_key_present),
        )
        for c in detection.classifications
        if cross_model_remappable(c)
    }


def group_colliding_targets(
    classifications: tuple[CollectionClassification, ...],
    target_names: dict[str, str],
) -> dict[str, list[CollectionClassification]]:
    """Group data-bearing, actually-written sources by their final target,
    keeping only targets claimed by two or more distinct sources.

    The predicate chain (``has_data`` → remap-else-own-name →
    :func:`vector_etl.is_never_written`) is the guard's own — extracted
    (nexus-p9vqa) so :func:`_assert_no_target_name_collisions` (the
    pre-flight that BLOCKS a fresh run) and the retroactive audit (which
    REPORTS on an already-migrated store) share one grouping and can never
    drift on "would these sources have collided".
    """
    by_target: dict[str, list[CollectionClassification]] = {}
    for c in classifications:
        if not c.has_data:
            continue
        target = target_names.get(c.collection, c.collection)
        if is_never_written(c.collection, target):
            continue
        by_target.setdefault(target, []).append(c)
    return {
        target: sources for target, sources in by_target.items() if len(sources) > 1
    }


def _assert_no_target_name_collisions(
    classifications: tuple[CollectionClassification, ...],
    target_names: dict[str, str],
) -> None:
    """Pre-flight (nexus-5b9v0): fail before any ETL if two or more distinct
    data-bearing source collections would write into the same final target.

    The final target for a classification is its remap ``target_names`` entry
    when it is cross-model-remappable, else its own collection name (a
    same-name migration). Empty collections are excluded — they write
    nothing, so they cannot collide. A collection the ETL's DEFAULT
    enumeration would itself never actually write
    (:func:`vector_etl.is_never_written`, nexus-5b9v0 Fix A, broadened in
    round-3 remediation) is ALSO excluded. ``is_never_written`` is the
    unifying predicate over every never-written disposition
    ``_skip_result_for_nonconformant`` can produce (target cannot
    dim-dispatch — ``skipped-derived``, ``skipped-empty``, and the generic
    ``skipped`` fallback all land there), plus the ``excluded`` / ephemeral
    (:func:`vector_etl.is_ephemeral_excluded`) case handled by a wholly
    separate enumeration-loop branch. Example: a non-remappable, two-segment
    ``taxonomy__centroids`` present with data on both legs maps to its own
    literal name on each leg — a naive grouping would see two distinct
    sources claiming one target and block a migration that would otherwise
    succeed cleanly (land-then-transform silently skips both and lands
    neither, same as the retired per-leg ETL).

    ``is_never_written`` is the ONE shared predicate this guard calls, so it
    can never drift from ``vector_etl``'s own disposition logic on "will
    this collection actually be written". It is deliberately NOT the same
    predicate ``vector_etl``'s own ``_skip_result_for_nonconformant`` uses at
    its ``is_derived_skip`` call site — that site must preserve the
    explicit-``--collections``-override nuance for ephemeral collections
    (see ``is_derived_skip``'s docstring), which does not apply here since
    this guard always runs in a default-enumeration context.

    This is a pure read-only check over already-computed classification +
    remap-target data; it MUST run before the land-then-transform sequence is
    ever invoked so a collision is caught before any write, never discovered
    mid-land or after.
    """
    collisions = group_colliding_targets(classifications, target_names)
    if collisions:
        raise TargetNameCollisionBlocked(collisions)


class _CompositeReadClient:
    """A read client routing ``get_collection`` to the leg that holds the name.

    RDR-180: the landing phase reads chunk content + probes text presence
    across a migration that can span both legs; this composite preserves the
    single-read-client seam it shares with the (now-retired-from-guided)
    validation count leg — it maps each collection name to the reopened read
    client for its source leg, so a two-leg landing reuses one composite
    instead of forking per-leg logic.
    """

    def __init__(self, by_collection: dict[str, Any]) -> None:
        self._by_collection = by_collection

    def get_collection(self, name: str) -> Any:
        try:
            client = self._by_collection[name]
        except KeyError as exc:  # a collection with no reopened source leg
            raise RuntimeError(
                f"count validation: no source read leg for collection {name!r} "
                "— cannot verify its source==target count"
            ) from exc
        return client.get_collection(name)


def _default_reopen_leg(leg: str, local_path: str | Path | None) -> Any:
    """Reopen one source read leg for landing.

    The detection phase opened + closed its own read client per leg; landing
    needs a FRESH read client (the detection clients were closed before this
    function is invoked).
    """
    from nexus.migration.chroma_read import (  # noqa: PLC0415  — command-local import (nexus.migration.chroma_read)
        open_cloud_read_client,
        open_local_read_client,
    )

    if leg == "cloud":
        return open_cloud_read_client()
    # nexus-id750: a None local_path resolves via the product's env-aware
    # default inside open_local_read_client (this fallback used to hardcode
    # the never-written <config>/chroma).
    return open_local_read_client(local_path)


def _open_source_ro(path: Path) -> sqlite3.Connection:
    """Open a migration SOURCE SQLite file read-only.

    The ONE sanctioned ``sqlite3.connect`` shape in this module (AGENTS.md's
    NO-SQLITE hot rule): an immutable land-then-transform SOURCE, read-only,
    never a new SQLite destination. Mirrors the house idiom
    (``nexus.db.t2.taxonomy_etl.count_source_rows`` et al.):
    ``file:<path>?mode=ro`` URI form + ``check_same_thread=False``.
    """
    uri = f"file:{path}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"cannot open SQLite source for reading: {path}: {exc}"
        ) from exc


def _resolve_pg_path() -> Path:
    """Best-effort local filesystem path backing the disk-headroom preflight.

    Local mode's bundled PG data directory (``nexus_config_dir() / "postgres"``,
    see ``nexus.db.pg_provision.provision``) when it exists; otherwise the
    config dir itself — a reasonable client-side proxy for "the machine
    issuing this migration" under managed/cloud mode, where the real PG disk
    lives on a remote server this client cannot introspect.
    ``NX_STAGING_DISK_OVERRIDE_ENV`` remains the escape hatch when this proxy
    does not fit a deployment.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — command-local import (nexus.config)

    pg_dir = nexus_config_dir() / "postgres"
    return pg_dir if pg_dir.exists() else nexus_config_dir()


def run_guided_upgrade(
    *,
    sources: EtlSources,
    vector_client: Any,
    catalog_client: Any,
    t2_db_path: str | Path,
    local_path: str | Path | None = None,
    voyage_key_present: bool | None = None,
    stale_aspects_count: int = 0,
    on_progress: Callable[[int, int], None] | None = None,
    on_leg_result: Callable[[Any], None] | None = None,
    reopen_leg: Callable[[str], Any] | None = None,
    run_t2: Callable[[Any], dict[str, Any]] | None = None,
) -> GuidedUpgradeResult:
    """Run the full detect → land-then-transform guided upgrade.

    ``sources`` (T2 + catalog SQLite paths) drives BOTH the pre-land census /
    pointer-store landing AND the non-chash T2 ``migrate all``.
    ``voyage_key_present`` defaults to the deployment-mode probe.
    ``on_progress(done, total)`` fires per successfully-promoted collection
    (the CLI wires ``click.echo``).

    ``vector_client`` / ``catalog_client`` / ``t2_db_path`` are ACCEPTED but
    UNUSED under RDR-180 land-then-transform — kept in the signature only for
    caller compatibility (the cross-repo contract pin in
    ``test_migration_contract.py`` and ``migrate_cmd.py``'s existing wiring).
    The pgvector write that used to go through ``vector_client`` now happens
    server-side inside ``StagingPromoteOps`` (reached via
    :class:`~nexus.migration.staging_land.HttpStagingStore`'s ``/v1/staging``
    calls, constructed internally here); the catalog-orphan validation leg
    that used to consume ``catalog_client`` is retired (the catalog documents
    ETL runs inside ``run_t2`` and reference re-pointing happens server-side
    via the in-DB ``chash_alias`` join); the taxonomy floor that used to read
    ``t2_db_path`` directly is likewise retired (``run_t2``'s taxonomy-topics
    leg reads ``sources.sqlite_path``, the same file in every known caller).

    ``on_leg_result`` is likewise ACCEPTED but no longer INVOKED: the retired
    per-leg :class:`~nexus.migration.vector_etl.CollectionResult` granularity
    it was built for no longer exists under land-then-transform (there is no
    more per-leg vector-ETL pass to report on) — ``on_progress`` remains the
    live per-collection progress signal.

    ``run_t2`` (RDR-178 Gap 7, nexus-1sx01): an override for the T2
    non-chash ``migrate all`` step, threaded straight to
    :func:`nexus.migration.sequencer.run_land_then_transform_migration`'s own
    ``run_t2`` seam. The guided-upgrade CLI uses this to pass
    ``functools.partial(migrate_all, skip_stores=...)`` once its
    already-migrated pre-flight (:func:`detect_already_migrated`) has
    identified stores with no newer local writes since a clean report.
    ``None`` (the default) preserves the prior behavior exactly — the
    sequencer's own default (:func:`nexus.migration.orchestrator.migrate_all_guided`)
    applies.

    Returns a :class:`GuidedUpgradeResult`. The sentinel is left ``migrated``
    only on a clean unlock-failure window that cannot occur here: a clean run
    clears it, any block leaves ``migrated-failed`` with staging retained for
    an idempotent re-run (RDR-180: there is no separate rollback command —
    see :attr:`GuidedUpgradeResult.rollback_available`).
    """
    key_present = (
        voyage_key_available() if voyage_key_present is None else voyage_key_present
    )

    # 1. DETECT — open read legs, classify, then CLOSE before any landing (the
    #    local leg is a WAL single-opener; the landing reopen must be the sole
    #    opener).
    local, cloud = open_read_legs(local_path)
    try:
        detection = classify_collections(
            local_client=local,
            cloud_client=cloud,
            voyage_key_present=key_present,
        )
    finally:
        for client in (local, cloud):
            _close_quietly(client)

    # RDR-162 P2: a legacy collection the service cannot serve under its current
    # name (e.g. minilm-384 after RDR-160) is auto-migrated by re-embedding its
    # STORED chunk text into a model-remapped target — not blocked with the
    # re-index diagnostic. Policy lives here (the orchestrator), not in the ETL:
    # build the source→target map, pass it down; RDR-180's ``_land`` resolves
    # every data-bearing collection's honest land-time target from this SAME
    # map (reconciliation H1 — the classification must happen once, here).
    # nexus-gilf2: the target model is mode + content-type aware (voyage models
    # in cloud mode, bge-768 in local) so the MIXED migrant (ran local, migrates
    # onto a voyage-mode service) re-embeds into a WIRED model instead of hitting
    # the pebfx.2 fail-loud guard.
    target_names = build_cross_model_target_names(
        detection, voyage_key_present=key_present
    )
    if target_names:
        _log.info("guided_upgrade_cross_model_targets", targets=target_names)

    # nexus-5b9v0 pre-flight: a cross-model remap target can collide with an
    # honest, non-remapped sibling collection's own name (or with a second
    # remap target) — BLOCK before any write, never discover it via a
    # confusing post-write count mismatch (or worse, a silent cross-collection
    # merge). Must run before landing is ever reopened.
    _assert_no_target_name_collisions(detection.classifications, target_names)

    # 2. LAND-THEN-TRANSFORM — reopen ONLY the data-bearing legs (now for
    #    LANDING, not validation), wire the real staging/census/land/promote
    #    collaborators, and drive the sequencer. Reopened legs are always
    #    closed, even on a raise.
    legs = sorted(detection.legs_with_data)
    reopen = reopen_leg or (lambda leg: _default_reopen_leg(leg, local_path))

    opened: dict[str, Any] = {}
    try:
        try:
            for leg in legs:
                opened[leg] = reopen(leg)
        except Exception as exc:  # noqa: BLE001 — wrapped for the CLI's `except RuntimeError` (mirrors the historical validation-reopen guard)
            raise RuntimeError(
                f"could not open source read leg for landing: {exc}"
            ) from exc

        by_collection = {
            c.collection: opened[c.leg]
            for c in detection.classifications
            if c.has_data
        }
        composite = _CompositeReadClient(by_collection)

        # RDR-180 Q4 residual (nexus-jxizy.10.8): the no-text set — sampled
        # BEFORE any row lands, over every data-bearing collection this run
        # would actually land (never-written derived/ephemeral collections
        # have nothing to rehash from and are excluded from the probe, same
        # as they are excluded from landing itself).
        no_text: set[str] = set()
        for c in detection.classifications:
            if not c.has_data:
                continue
            target = target_names.get(c.collection, c.collection)
            if is_never_written(c.collection, target):
                continue
            col = composite.get_collection(c.collection)
            if probe_has_text(col) is False:
                no_text.add(c.collection)

        def _model_gate(
            classifications: Any, *, voyage_key_present: bool, exempt: Any
        ) -> None:
            assert_models_supported(
                classifications,
                voyage_key_present=voyage_key_present,
                exempt=exempt,
                no_text=frozenset(no_text),
            )

        # Constructed LAZILY (only on first actual use, inside the closures
        # below) — ``HttpStagingStore()`` resolves a live service endpoint at
        # construction time, and a fresh-user no-op (``total == 0`` inside
        # :func:`run_land_then_transform_migration`) never reaches
        # ``census_check``/``land``/etc., so it must never be forced eagerly.
        _staging_cache: list[HttpStagingStore] = []

        def _staging() -> HttpStagingStore:
            if not _staging_cache:
                _staging_cache.append(HttpStagingStore())
            return _staging_cache[0]

        promote_reports: dict[str, dict[str, Any]] = {}

        def _census_check() -> None:
            catalog_conn = _open_source_ro(sources.catalog_db_path)
            memory_conn = _open_source_ro(sources.sqlite_path)
            try:
                source_census({"catalog": catalog_conn, "memory": memory_conn})
            finally:
                catalog_conn.close()
                memory_conn.close()

            chroma_dir: Path | None = None
            if "local" in legs:
                chroma_dir = (
                    Path(local_path) if local_path is not None
                    else resolve_default_local_leg()
                )
            estimated = estimate_staging_source_bytes(
                (sources.sqlite_path, sources.catalog_db_path), chroma_dir=chroma_dir,
            )
            assert_disk_headroom(estimated_bytes=estimated, pg_path=_resolve_pg_path())

        def _land() -> dict[str, int]:
            landed: dict[str, int] = {}
            catalog_conn = _open_source_ro(sources.catalog_db_path)
            memory_conn = _open_source_ro(sources.sqlite_path)
            try:
                for store in _POINTER_STORES:
                    rows = pointer_store_rows(store, catalog_conn, memory_conn)
                    landed[store] = _staging().load(store, rows)
                # reviewer-p2 Medium: orphaned-FK assignments the landing
                # skipped must be OPERATOR-visible, not just a log line.
                orphans = topic_assignment_orphans(memory_conn)
                if orphans:
                    landed["topic_assignments_orphaned_skipped"] = orphans
            finally:
                catalog_conn.close()
                memory_conn.close()

            for c in detection.classifications:
                if not c.has_data:
                    continue
                target = target_names.get(c.collection, c.collection)
                if is_never_written(c.collection, target):
                    continue
                target_dim, reason = _dim_for_collection(target)
                if target_dim is None:
                    raise RuntimeError(
                        f"land: {c.collection!r} resolves to target {target!r} "
                        f"which cannot dim-dispatch: {reason}"
                    )
                source_collection = composite.get_collection(c.collection)
                count = 0
                for batch in chunk_rows(
                    source_collection,
                    target_name=target,
                    target_model=target.split("__")[2],
                    target_dim=target_dim,
                    source_model=c.model,
                ):
                    count += _staging().load("chunks", batch)
                landed[c.collection] = count
            return landed

        def _embed_fill(collection: str) -> dict[str, Any]:
            target = target_names.get(collection, collection)
            if is_never_written(collection, target):
                return {}
            return _staging().embed_fill(target)

        def _promote(collection: str) -> dict[str, Any]:
            target = target_names.get(collection, collection)
            if is_never_written(collection, target):
                return {}
            report = _staging().promote(target)
            promote_reports[collection] = report
            return report

        def _verify(finalize_report: dict[str, Any]) -> None:
            staged = _staging().counts()
            staged_chunks = int(staged.get("chunks", 0))
            expected_chunks = sum(
                c.source_count
                for c in detection.classifications
                if c.has_data
                and not is_never_written(
                    c.collection, target_names.get(c.collection, c.collection)
                )
            )
            if staged_chunks != expected_chunks:
                raise RuntimeError(
                    f"count parity: staged chunks={staged_chunks} != detected "
                    f"source chunks={expected_chunks} (pre-clear "
                    "/v1/staging/counts)"
                )

            total_promoted = sum(
                int(r.get("promoted", 0)) for r in promote_reports.values()
            )
            total_staged_content = sum(
                int(r.get("staged_content", 0)) for r in promote_reports.values()
            )
            # reviewer-p2 CRITICAL: the engine's staged_content counts
            # chunk_text <> '' rows ONLY (empty-text rows deliberately wait
            # for finalize's Item8 disposition), while /counts counts EVERY
            # landed row — the reconciliation must fold the finalize
            # envelope's dispositions in, or any tenant with one empty-text
            # chunk false-positive-blocks here.
            disposed = (
                int(finalize_report.get("reference_only_resolved", 0))
                + int(finalize_report.get("orphans_dropped", 0))
                + int(finalize_report.get("orphans_synthesized", 0))
            )
            if total_staged_content + disposed != staged_chunks:
                raise RuntimeError(
                    "count parity: sum of per-collection staged_content="
                    f"{total_staged_content} + finalize-disposed={disposed} "
                    f"!= staged chunks={staged_chunks}"
                )
            collapsed = total_staged_content - total_promoted
            if collapsed < 0:
                raise RuntimeError(
                    f"count parity: promoted={total_promoted} exceeds "
                    f"staged_content={total_staged_content} for the promoted "
                    "collections — impossible, promote overcounted"
                )
            residual_mismatched = int(finalize_report.get("residual_mismatched", 0))
            dangling_manifest = int(finalize_report.get("dangling_manifest", 0))
            if residual_mismatched != 0 or dangling_manifest != 0:
                raise RuntimeError(
                    "finalize in-txn verify failed: "
                    f"residual_mismatched={residual_mismatched} "
                    f"dangling_manifest={dangling_manifest}"
                )
            _log.info(
                "guided_upgrade_verify_reconciled",
                staged_chunks=staged_chunks,
                promoted=total_promoted,
                collapsed=collapsed,
                orphans_dropped=int(finalize_report.get("orphans_dropped", 0)),
                orphans_synthesized=int(finalize_report.get("orphans_synthesized", 0)),
            )

        # Only pass run_t2 through when explicitly given — omitting the kwarg
        # entirely when it is None preserves the exact prior call shape for
        # every existing caller/test (the sequencer's own default applies).
        _seq_kwargs: dict[str, Any] = {}
        if run_t2 is not None:
            _seq_kwargs["run_t2"] = run_t2

        outcome = run_land_then_transform_migration(
            detection,
            sources=sources,
            census_check=_census_check,
            land=_land,
            embed_fill=_embed_fill,
            promote=_promote,
            finalize=lambda: _staging().finalize(),
            verify=_verify,
            clear_staging=lambda: _staging().clear(),
            voyage_key_present=key_present,
            model_gate=_model_gate,
            on_progress=on_progress,
            cross_model_targets=target_names,
            **_seq_kwargs,
        )
    finally:
        for client in opened.values():
            _close_quietly(client)

    # A fresh user (nothing data-bearing) is a clean success with no migration
    # and no validation to run — preserved from the pre-RDR-180 contract.
    if outcome.phase == "not-migrating":
        validation: ValidationOutcome | None = None
    elif outcome.ok:
        # RDR-180: ``verify`` (above) IS the validation gate now — reaching
        # ok=True already means census/land/T2/promote/finalize/verify all
        # passed and staging was cleared. ``run_land_then_transform_migration``
        # itself only calls ``mark_migrated()`` (leaving the sentinel at the
        # terminal ``migrated`` phase, mirroring the pre-RDR-180 sequencer);
        # the OLD flow's separate VALIDATE phase then called ``clear_state()``
        # on a clean unlock (its ``validate_migration``'s ``unlock`` callback,
        # defaulting to ``clear_state``) — the actual UNLOCK, restoring normal
        # (non-degraded) reads. That step has no home under land-then-
        # transform's folded verify, so it belongs here: a genuine clean
        # completion clears the sentinel, exactly as before.
        clear_state()
        # Synthesize the ValidationOutcome shape callers
        # (``migrate_cmd._render_result``) still consume.
        validation = ValidationOutcome(
            unlocked=True,
            verdict="verified",
            blocking_reasons=(),
            taxonomy_orphans=(),
            count_mismatches=(),
            count_indeterminate=False,
            manifest_orphan_count=0,
            manifest_vacuous=False,
            stale_aspects=stale_aspects_count,
            advisory_notes=(),
            rollback_available=False,
        )
    else:
        # Any block (census / land / dirty-T2 / a failed collection / finalize
        # / verify / clear-staging) — nothing was validated to gate; staging
        # is retained for an idempotent resume, never an explicit rollback.
        validation = None

    _log.info(
        "guided_upgrade_complete",
        ok=outcome.ok,
        phase=outcome.phase,
        collections_total=outcome.collections_total,
        collections_done=outcome.collections_done,
    )
    return GuidedUpgradeResult(detection, outcome, validation, ok=outcome.ok)


def _close_quietly(client: Any | None) -> None:
    # Delegates to the canonical leg-teardown primitive (RDR-002 ez5.3:
    # de-duplicated with the guided-upgrade pre-flight). Thin alias preserved
    # for the existing call sites in this module.
    close_read_client(client)
