# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-185 P2.5: the substrate rung's co-resident leg planner + executor.

RQ2 edges 4-5 (binding): chunk-identity and embedder-era are CO-RESIDENT
inside the substrate ETL rung — never sequential rungs. Each data-bearing
collection gets ONE leg composing what it needs:

- ``needs_reid`` — legacy (non-32-char) chunk ids become an IN-FLIGHT
  wire transform (the .15 ``make_wire_reid_transform``). This is the
  RDR-185 retirement of "the migration NEVER rewrites ids": for the RUNG
  path legacy ids are converged, not blocked. (The legacy-path block in
  ``detection``/``_migrate_one`` stands untouched until P4 demotes it.)
- ``needs_reembed`` — an unsupported embedder re-embeds server-side from
  the stored text into the model-remapped TARGET collection (RDR-162
  machinery: ``cross_model_target_model`` + ``cross_model_target_name``).
  The map records ``target_collection`` for every re-id'd row (the .13
  audit C2 "where did it land" answer).

Consent only at genuine decisions (RDR-185 Constraints):

- SOURCE-GONE (nexus-8jlsl): a collection known from prior migration
  state that no longer exists in the source surfaces as an explicit
  :class:`SourceGoneDecision` (re-acquire vs drop) — never a silent skip.
- The billed Voyage re-embed keeps the EXISTING cost prompt
  (``_confirm_voyage_cost``); the plan flags ``billed_reembed`` so the
  trigger knows to route through it.
- Everything derivable is automatic: a conformant install plans zero
  legs and zero prompts.

Rung registration + doctor-census fold happen at P2 validation / P4 (the
walk skeleton already detect-and-skips the unregistered rung).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nexus.migration.detection import CollectionClassification
    from nexus.migration.etl_ports import EtlRunResult, EtlSource, EtlTarget
    from nexus.migration.wire_reid import ChashRemapStore

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class LegPlan:
    """One collection's composed transition (co-resident by construction)."""

    source_collection: str
    target_collection: str
    needs_reid: bool
    needs_reembed: bool


@dataclass(frozen=True)
class SourceGoneDecision:
    """A genuine decision the product cannot make: the source collection
    vanished. Options are the nexus-8jlsl pair."""

    collection: str
    options: tuple[str, ...] = ("re-acquire", "drop")


@dataclass(frozen=True)
class SubstratePlan:
    legs: list[LegPlan] = field(default_factory=list)
    decisions: list[SourceGoneDecision] = field(default_factory=list)
    #: True when any leg re-embeds into a billed Voyage model — the trigger
    #: must route through the existing cost prompt (_confirm_voyage_cost).
    billed_reembed: bool = False


def plan_substrate_legs(
    classifications: Iterable["CollectionClassification"],
    *,
    prior_collections: frozenset[str],
    voyage_key_present: bool,
) -> SubstratePlan:
    """Compose one leg per data-bearing collection + surface genuine decisions.

    ``prior_collections`` is the set of source collections known from prior
    migration state (the chash_remap map's source collections, watermark
    keys) — anything there that no longer classifies from the live source
    is the SOURCE-GONE decision case.
    """
    from nexus.migration.detection import remap_target_model, wired_models  # noqa: PLC0415 — deferred, detection is heavy
    from nexus.migration.vector_etl import _VOYAGE_MODELS, cross_model_target_name  # noqa: PLC0415 — deferred, vector_etl is heavy

    wired = wired_models(voyage_key_present=voyage_key_present)
    legs: list[LegPlan] = []
    billed = False
    live_names: set[str] = set()
    for c in classifications:
        live_names.add(c.collection)
        if not c.has_data:
            continue
        needs_reid = bool(c.legacy_ids)
        # Model-based, NOT support-based: classification flips support to
        # "unsupported" for legacy ids too (detection.py:428), and legacy is
        # the co-resident RE-ID axis, not a re-embed reason. A voyage-model
        # collection without the key stays with the upstream credential gate
        # (C3) — re-embedding voyage text into bge would silently change
        # recall (cross_model_remappable's deliberate exclusion).
        if c.model in _VOYAGE_MODELS and not voyage_key_present:
            continue  # credential gate territory, not a leg
        needs_reembed = c.model not in wired
        target = c.collection
        if needs_reembed:
            target_model = remap_target_model(c, voyage_key_present=voyage_key_present)
            target = cross_model_target_name(c.collection, target_model)
            if target_model in _VOYAGE_MODELS:
                billed = True
        if not needs_reid and not needs_reembed:
            continue  # conformant: nothing to do, nothing to ask
        legs.append(
            LegPlan(
                source_collection=c.collection,
                target_collection=target,
                needs_reid=needs_reid,
                needs_reembed=needs_reembed,
            )
        )
    decisions = [
        SourceGoneDecision(collection=name)
        for name in sorted(prior_collections - live_names)
    ]
    if decisions:
        _log.warning(
            "substrate_leg_source_gone",
            collections=[d.collection for d in decisions],
            note="genuine decision surfaced (re-acquire vs drop) — never silent",
        )
    return SubstratePlan(legs=legs, decisions=decisions, billed_reembed=billed)


def _provenance_scrub(target_collection: str):
    """nexus-bfdri mismatch-only provenance check as a batch transform: drop
    the stored vector ONLY when recorded provenance is present and disagrees
    with the target name's declared model segment. Non-conformant target
    names (no declared model) trust all vectors."""
    segments = target_collection.split("__")
    declared = segments[2] if len(segments) >= 3 else None

    def scrub(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if declared is None:
            return batch
        out: list[dict[str, Any]] = []
        for chunk in batch:
            prov = (chunk.get("metadata") or {}).get("embedding_model")
            if prov and prov != declared and chunk.get("embedding") is not None:
                chunk = dict(chunk)
                chunk.pop("embedding", None)
            out.append(chunk)
        return out

    return scrub


def _leg_watermark_key(leg: LegPlan, tenant_id: str) -> str:
    from nexus.upgrade_ladder.registry import RUNG_SUBSTRATE_ETL  # noqa: PLC0415 — deferred, avoids import cycle

    return f"{RUNG_SUBSTRATE_ETL}|{tenant_id}|{leg.source_collection}->{leg.target_collection}"


def execute_leg(
    leg: LegPlan,
    source: "EtlSource",
    target: "EtlTarget",
    *,
    map_store: "ChashRemapStore",
    page: int,
    provenance: str,
    tenant_id: str = "",
) -> "EtlRunResult":
    """Run one composed leg through the .14 seam, RESUMABLY (RDR-178).

    Re-id legs get the .15 wire transform (map batch persists strictly
    before the target write — gate r2 by construction). Re-embed legs send
    NO embeddings (the service re-embeds the stored text with the target
    collection's declared model).

    Resume (P2 critique High): the rung-keyed watermark records how many
    SOURCE rows are verified-sent after each clean batch, trusted against
    the live target count (distrust-on-shrink invalidates after a
    rollback). A crash at 99% of a 90k-chunk collection resumes from the
    floor instead of replaying ~900s — offsets are stable because the
    source is frozen post-cutover (RDR-176). A resumed run's full-count
    verification is the rung verify()'s job; the seam asserts this run's
    own rows.
    """
    from nexus.migration.etl_ports import run_batched_etl  # noqa: PLC0415 — deferred to avoid import cost
    from nexus.migration.verify_fill_watermark import (  # noqa: PLC0415 — deferred to avoid import cost
        advance_rung_watermark,
        usable_rung_watermark,
    )
    from nexus.migration.wire_reid import make_wire_reid_transform  # noqa: PLC0415 — deferred to avoid import cost

    reid = None
    if leg.needs_reid:
        reid = make_wire_reid_transform(
            map_store,
            source_collection=leg.source_collection,
            target_collection=leg.target_collection,
            provenance=provenance,
            tenant_id=tenant_id,
        )
    # Same-model legs PASS THROUGH stored vectors (P2 review High): forcing a
    # server-side re-embed on a re-id-only leg bills Voyage tokens the plan
    # promised it would not (billed_reembed=False) — silently defeating the
    # consent gate. Passthrough carries the nexus-bfdri MISMATCH-ONLY
    # provenance rule from _migrate_one: a chunk whose recorded
    # embedding_model is present AND disagrees with the target's declared
    # model drops its vector (the seam's all-or-none batch check then falls
    # back to a server re-embed for that batch); absent provenance is trusted.
    passthrough = not leg.needs_reembed
    scrub = _provenance_scrub(leg.target_collection) if passthrough else None

    def _compose(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if reid is not None:
            batch = reid(batch)  # map batch persists here (r2 ordering)
        if scrub is not None:
            batch = scrub(batch)
        return batch

    transform = _compose if (reid is not None or scrub is not None) else None
    key = _leg_watermark_key(leg, tenant_id)
    floor = usable_rung_watermark(
        key, trusted_count=int(target.count(leg.target_collection))
    )
    if floor:
        _log.info(
            "substrate_leg_resuming_from_watermark",
            leg=key,
            floor=floor,
        )

    def _advance(written: int, source_rows_this_run: int) -> None:
        advance_rung_watermark(
            key,
            position=floor + source_rows_this_run,
            trusted_count=int(target.count(leg.target_collection)),
        )

    return run_batched_etl(
        source,
        target,
        source_collection=leg.source_collection,
        target_collection=leg.target_collection,
        page=page,
        include_embeddings=passthrough,  # same-model: carry stored vectors (no bill)
        transform=transform,
        on_batch=_advance,
        skip_rows=floor,
    )


def run_substrate_migration(
    plan: SubstratePlan,
    source: "EtlSource",
    target: "EtlTarget",
    *,
    map_store: "ChashRemapStore",
    catalog_db: Any,
    memory_db: Any,
    page: int,
    provenance: str,
    tenant_id: str = "",
) -> tuple[list["EtlRunResult"], list[Any]]:
    """The audit §2 ordering as CODE, not prose (P2 critique High):

    per leg — map persist + regenerate (``execute_leg``, r2 by
    construction) — THEN the local-store cascade (manifest → chash_index →
    topic_assignments → frecency/relevance_log → aspects, the
    ``CASCADE_STORES`` order). Genuine decisions must be resolved by the
    CALLER before this runs: it refuses a plan with pending decisions
    (consent never happens implicitly).

    Returns (per-leg results, per-store cascade results). The rung's
    converge wraps this; its verify() is the authoritative post-state
    check (RDR-142).
    """
    from nexus.migration.remap_cascade import cascade_remap  # noqa: PLC0415 — deferred to avoid import cost

    if plan.decisions:
        raise RuntimeError(
            "substrate migration has unresolved genuine decisions "
            f"(source-gone: {[d.collection for d in plan.decisions]!r}) — "
            "resolve them before running (consent is never implicit)"
        )
    leg_results = [
        execute_leg(
            leg, source, target,
            map_store=map_store, page=page, provenance=provenance, tenant_id=tenant_id,
        )
        for leg in plan.legs
    ]
    cascade_results = cascade_remap(
        map_store, catalog_db=catalog_db, memory_db=memory_db
    )
    return leg_results, cascade_results
