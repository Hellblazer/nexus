# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-159 P4 (nexus-ue6g7.24): the guided-upgrade engine entry point.

``run_guided_upgrade`` is the ONE function both surfaces call — the nexus CLI
(``nx migrate-to-service``) and the deferred conexus veneer (``conexus
upgrade``). It owns the full lifecycle that wraps the P0-P3 primitives in the
single survivable order, so neither surface re-derives the seam:

  1. DETECT     open the Chroma read legs, classify the footprint, then CLOSE
                them BEFORE any ETL (the local leg is WAL single-opener — the
                ETL must be the only opener, so detection cannot still hold it);
  2. SEQUENCE   ``run_sequenced_migration`` drives quiesce → pre-gate → T2 →
                T3-per-leg, leaving the sentinel ``migrated`` (or
                ``migrated-failed`` on any block / partial leg);
  3. VALIDATE   on a clean ``migrated``, REOPEN the data-bearing read legs and
                run the non-vacuous validation gate (taxonomy floor + counts +
                manifest-orphans); clean → unlock (clear sentinel), block →
                leave ``migrated-failed`` + offer rollback. Reopened read legs
                are closed in a ``finally``.

The engine touches NO data of its own and adds NO orchestration beyond
sequencing + lifecycle: every ETL / state / validation primitive is the proven
P0-P3 code. Collaborators (clients, paths) are injected; the low-level engine
functions are module globals so a unit test monkeypatches ``driver.<name>``
without a live service / Chroma (the existing nexus CLI-wiring test idiom).
"""
from __future__ import annotations

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
    remap_target_model,
    open_read_legs,
    voyage_key_available,
)
from nexus.migration.orchestrator import EtlSources
from nexus.migration.sequencer import SequenceOutcome, run_sequenced_migration
from nexus.migration.state import mark_failed
from nexus.migration.validation import (
    ValidationOutcome,
    compose_validation_checks,
    validate_migration,
)
from nexus.migration.vector_etl import (
    MigrationReport,
    _dim_for_collection,
    cross_model_target_name,
    is_never_written,
    migrate_cloud,
    migrate_local,
)

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class GuidedUpgradeResult:
    """The verdict of one guided-upgrade run + the evidence behind it.

    ``ok`` is the single user-facing success bit: True only on a fresh-user
    no-op OR a sequence that reached ``migrated`` AND validated clean (unlocked).
    ``validation`` is ``None`` when the sequence never reached validation (a
    fresh-user no-op, or a sequence block/partial-leg that left the sentinel
    ``migrated-failed`` before any validation could run).
    """

    detection: DetectionReport
    sequence: SequenceOutcome
    validation: ValidationOutcome | None
    ok: bool

    @property
    def rollback_available(self) -> bool:
        """Whether a rollback is offered (a validated block left a pgvector copy
        the user can undo to return to the immutable Chroma source)."""
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
    reimplementation would audit a map no run ever produced.
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
    succeed cleanly (the ETL silently skips both and writes neither).

    ``is_never_written`` is the ONE shared predicate this guard calls, so it
    can never drift from ``vector_etl``'s own disposition logic on "will
    this collection actually be written". It is deliberately NOT the same
    predicate ``vector_etl``'s own ``_skip_result_for_nonconformant`` uses at
    its ``is_derived_skip`` call site — that site must preserve the
    explicit-``--collections``-override nuance for ephemeral collections
    (see ``is_derived_skip``'s docstring), which does not apply here since
    this guard always runs in a default-enumeration context.

    This is a pure read-only check over already-computed classification +
    remap-target data; it MUST run before ``_run_leg``/the sequencer is ever
    invoked so a collision is caught before any write, never discovered
    mid-ETL or after.
    """
    collisions = group_colliding_targets(classifications, target_names)
    if collisions:
        raise TargetNameCollisionBlocked(collisions)


class _CompositeReadClient:
    """A read client routing ``get_collection`` to the leg that holds the name.

    ``verify_counts`` (the P3 count leg) takes ONE read client + a collection
    list, but a migration can span both legs. This composite preserves the
    single-read-client seam ``compose_validation_checks`` pins: it maps each
    collection name to the reopened read client for its source leg, so a
    two-leg validation reuses the unchanged seam instead of forking it.
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
    """Reopen one source read leg for the validation count check.

    The ETL opened + closed its own read client per leg; validation needs a
    FRESH read client (the detection clients were closed before the ETL ran).
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
    """Run the full detect → sequence → validate guided upgrade.

    ``sources`` (T2 + catalog SQLite paths) drives the T2 ``migrate all``;
    ``vector_client`` is the pgvector write client; ``catalog_client`` backs the
    manifest-orphan validation leg; ``t2_db_path`` is the source SQLite the
    taxonomy floor reads. ``voyage_key_present`` defaults to the deployment-mode
    probe. ``on_progress(done, total)`` / ``on_leg_result(result)`` are pure
    progress sinks (the CLI wires ``click.echo``).

    ``run_t2`` (RDR-178 Gap 7, nexus-1sx01): an override for the T2
    ``migrate all`` step, threaded straight to
    :func:`nexus.migration.sequencer.run_sequenced_migration`'s own
    ``run_t2`` seam. The guided-upgrade CLI uses this to pass
    ``functools.partial(migrate_all, skip_stores=...)`` once its
    already-migrated pre-flight (:func:`detect_already_migrated`) has
    identified stores with no newer local writes since a clean report.
    ``None`` (the default) preserves the prior behavior exactly — the
    sequencer's own default (``migrate_all`` unconditionally) applies.

    Returns a :class:`GuidedUpgradeResult`. The sentinel is left ``migrated``
    only on a clean unlock-failure window that cannot occur here: a clean run
    clears it, any block leaves ``migrated-failed`` with rollback offered.
    """
    key_present = (
        voyage_key_available() if voyage_key_present is None else voyage_key_present
    )

    # 1. DETECT — open read legs, classify, then CLOSE before any ETL (the local
    #    leg is a WAL single-opener; the ETL reopens it and must be the sole
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
    # build the source→target map, pass it down, and the sequencer exempts these
    # from the pre-gate and re-points their references after they verify.
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
    # merge). Must run before `_run_leg` is even defined.
    _assert_no_target_name_collisions(detection.classifications, target_names)

    # 2. SEQUENCE — quiesce → pre-gate → T2 → T3-per-leg. The per-leg ETL opens
    #    + closes its OWN read client internally (we closed ours above).
    def _run_leg(leg: str) -> MigrationReport:
        if leg == "cloud":
            return migrate_cloud(
                vector_client, on_result=on_leg_result, target_names=target_names,
            )
        return migrate_local(
            local_path, vector_client, on_result=on_leg_result,
            target_names=target_names,
        )

    # Only pass run_t2 through when explicitly given — omitting the kwarg
    # entirely when it is None preserves the exact prior call shape for
    # every existing caller/test (the sequencer's own default applies).
    _seq_kwargs: dict[str, Any] = {}
    if run_t2 is not None:
        _seq_kwargs["run_t2"] = run_t2

    sequence = run_sequenced_migration(
        detection,
        sources=sources,
        run_leg=_run_leg,
        voyage_key_present=key_present,
        on_progress=on_progress,
        cross_model_targets=target_names,
        **_seq_kwargs,
    )

    # A fresh-user no-op (nothing data-bearing) is a clean success with no
    # migration and no validation to run.
    if sequence.phase == "not-migrating":
        return GuidedUpgradeResult(detection, sequence, validation=None, ok=True)

    # A block / partial-leg left the sentinel migrated-failed BEFORE T3
    # completed — there is nothing validated to gate; rollback is the per-leg
    # ETL's concern, surfaced via the dry-run/CLI, not this gate.
    if not sequence.ok:
        return GuidedUpgradeResult(detection, sequence, validation=None, ok=False)

    # 3. VALIDATE — reopen ONLY the data-bearing legs, build a composite read
    #    client routing each migrated collection to its source leg, run the
    #    gate, and always close the reopened legs.
    legs = sorted(detection.legs_with_data)
    migrated_collections = [c.collection for c in detection.classifications if c.has_data]
    # RDR-162 P2: a cross-model collection's chunks landed in its bge-768 TARGET,
    # so its validation dim is the TARGET dim (768), not the source dim (384).
    # The count check (below, via target_names) reads the source Chroma count and
    # the TARGET pgvector count; the taxonomy/manifest legs see the remapped refs.
    dims = tuple(
        sorted(
            {
                _dim_for_collection(target_names.get(c.collection, c.collection))[0]
                or c.dim
                for c in detection.classifications
                if c.has_data and (c.dim or c.collection in target_names)
            }
        )
    )
    reopen = reopen_leg or (lambda leg: _default_reopen_leg(leg, local_path))

    opened: dict[str, Any] = {}
    by_collection: dict[str, Any] = {}
    try:
        try:
            for leg in legs:
                opened[leg] = reopen(leg)
            for c in detection.classifications:
                if c.has_data:
                    by_collection[c.collection] = opened[c.leg]
            read_client = _CompositeReadClient(by_collection)
            checks = compose_validation_checks(
                t2_db_path=t2_db_path,
                read_client=read_client,
                vector_client=vector_client,
                catalog_client=catalog_client,
                collections=migrated_collections,
                dims=dims,
                target_names=target_names,
            )
            validation = validate_migration(
                taxonomy_check=checks.taxonomy_check,
                count_check=checks.count_check,
                manifest_orphan_check=checks.manifest_orphan_check,
                stale_aspects_count=stale_aspects_count,
            )
        except Exception as exc:  # noqa: BLE001 — recorded to the sentinel, never silent
            # The T3 copy is done (sentinel == `migrated`) but a validation-leg
            # SETUP step raised (e.g. a reopened read leg vanished, or the gate
            # itself crashed). Leaving the sentinel `migrated` would strand an
            # UNVALIDATED migration looking clean — the P2/P3 CRITICAL-1 class.
            # Transition to `migrated-failed` (degraded-LOUD, rollback offered)
            # before re-raising so the operator gets the recovery path.
            reason = f"validation could not be performed: {exc}"
            _log.error("guided_upgrade_validation_setup_raised", error=str(exc))
            mark_failed(reason)
            # nexus-5b9v0 round-3 Fix D (bead nexus-rndvq, CRITICAL): this used
            # to be a bare `raise`, re-propagating *exc*'s ORIGINAL type
            # unchanged. In production the reopen most commonly raises
            # FileNotFoundError (chroma_read.open_local_read_client, when the
            # local Chroma store vanished between the ETL write and this
            # reopen) — NOT a RuntimeError, so migrate_cmd.py's
            # `except RuntimeError` CLI wrapper could never catch it and a
            # raw traceback still reached the operator for this exact failure
            # mode. Wrap at the origin, matching every sibling guard in this
            # module (ModelPreGateBlocked, MigrationQuiesceBlocked,
            # EtlPreflightFailed, ValidationCheckVacuous,
            # TargetNameCollisionBlocked are all RuntimeError subclasses) —
            # any current or future `except RuntimeError` caller now covers
            # this path unconditionally. `from exc` preserves the original
            # exception as `__cause__` for a caller that needs to recover it.
            raise RuntimeError(reason) from exc
    finally:
        for client in opened.values():
            _close_quietly(client)

    _log.info(
        "guided_upgrade_complete",
        sequence_ok=sequence.ok,
        unlocked=validation.unlocked,
        legs=legs,
        collections=len(migrated_collections),
    )
    return GuidedUpgradeResult(
        detection, sequence, validation, ok=validation.unlocked
    )


def _close_quietly(client: Any | None) -> None:
    # Delegates to the canonical leg-teardown primitive (RDR-002 ez5.3:
    # de-duplicated with the guided-upgrade pre-flight). Thin alias preserved
    # for the existing call sites in this module.
    close_read_client(client)
