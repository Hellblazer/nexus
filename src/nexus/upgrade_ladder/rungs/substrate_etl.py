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

P4.0 (nexus-x3z00) assembles the parts into :class:`SubstrateEtlRung` —
the Rung the walk actually reaches — and registers it after t2-schema
(the RQ2 hard edge).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from nexus.upgrade_ladder.protocol import (
    ConvergeOutcome,
    ConvergeResult,
    ProgressReporter,
    RungStatus,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path  # noqa: F401 — annotation-only; _default_map_path already returns a Path

    from nexus.migration.detection import CollectionClassification  # noqa: F401 — runtime-quoted in the rung's seams
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

    def target_names(self) -> dict[str, str]:
        """source→target for every RENAMED leg — the ``rollback_collections``
        ``target_names`` argument. Load-bearing for PURE-REEMBED legs (P2
        critique residual Medium): conformant ids get NO map entries, so
        without this mapping their rollback would probe the source-named
        collection and silently no-op. Any rollback of a plan's collections
        MUST pass this."""
        return {
            leg.source_collection: leg.target_collection
            for leg in self.legs
            if leg.target_collection != leg.source_collection
        }


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
    from nexus.migration.detection import (  # noqa: PLC0415 — deferred, detection is heavy
        is_measured_dim_override,
        remap_target_model,
        wired_models,
    )
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
        #
        # ...UNLESS a stored vector PROVED the content is local bge/ONNX
        # (nexus-nb7hr measured-dim override). Then the voyage token in the
        # name is a pre-RDR-109 mislabel, the content was never voyage text,
        # no key is needed, and re-embedding bge into bge is loss-free. The
        # ORDER is the whole point and mirrors cross_model_remappable, which
        # checks the override BEFORE its own name-based voyage exclusion —
        # `is_measured_dim_override` exists precisely so callers reuse the
        # identical test "instead of re-deriving it and risking drift".
        # This re-derived it and drifted: bead nexus-j5diu, caught by the
        # P4.3 era-hop when a voyage-NAMED measured-768 collection was
        # silently dropped from the plan and never migrated (service=0 vs
        # seeded=12) — no leg, no error, no decision. A collection that
        # cannot migrate must surface LOUD or as a decision, never vanish.
        if (
            c.model in _VOYAGE_MODELS
            and not voyage_key_present
            and not is_measured_dim_override(c)
        ):
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


# ── P4.0 (nexus-x3z00): the assembled rung ──────────────────────────────────


def drop_converged_legs(
    plan: SubstratePlan,
    source_counts: dict[str, int],
    target_counts: dict[str, int] | None,
) -> SubstratePlan:
    """Drop legs whose TARGET already holds the source's rows (nexus-mapbc).

    The rung's terminal state. Without this the substrate rung can never
    finish: every question it asked was aimed at the SOURCE, which RDR-176
    guarantees is byte-untouched forever, so a perfectly migrated install
    still classified the same collections, still planned the same legs, and
    ``verify()`` still returned False. Observed live (P4.3 era-hop): 4/4 legs
    migrated correctly, verify-failed, completion never recorded, `nx doctor`
    reporting pending rungs forever, and `nx upgrade` re-running the full ETL
    on every invocation — which on a cloud leg re-bills Voyage every time.

    Convergence test is count equality against the target. It is deliberately
    NOT a per-id probe: detect() runs on `nx doctor`'s read-only path, and
    re-deriving every chash there would read every source document on every
    health check.

    WHAT COUNT EQUALITY PROVES, EXACTLY (P4.R1 F2 / P4.R2 F5 — stated
    precisely, because an earlier draft of this docstring claimed "sufficient
    for identity" and that overclaims in two directions):

    * CARDINALITY — every source row has a counterpart. Yes.
    * ID-FORMAT conformance — yes, but only that: GH #1390's chash CHECK
      constraints keep non-32-char ids out of pgvector entirely, so a target
      holding a full count cannot be holding legacy ids. That is a statement
      about id SHAPE, not about the ids being the RIGHT ones.
    * CONTENT equivalence — NO. A target holding the right number of rows
      with wrong bytes, or populated to the same count by some other means,
      reads as converged here. Accepted residual: content is verified per
      batch at ETL time, upstream of this filter (RDR-176), and targets are
      content-type+owner+model scoped so an accidental full-count collision
      from unrelated content is not a reachable shape in the current call
      graph. If that ever stops being true, sample ids rather than counts.
    * That the map was REFLECTED into the local stores — NO, and this is the
      one that bit us: see :func:`remap_cascade.unreflected_stores`, which
      verify() asks separately for exactly that reason.

    ``target_counts`` is the whole target picture in one map (one round trip
    upstream, not one per leg) — or None when the probe could not run at all,
    which drops NOTHING: "I could not tell" is never "converged". A target
    simply absent from the map is not converged either. Decisions pass through
    untouched: a source-gone collection is a question for converge, not a leg.
    """
    if not plan.legs or target_counts is None:
        return plan
    remaining: list[LegPlan] = []
    for leg in plan.legs:
        want = source_counts.get(leg.source_collection)
        got = target_counts.get(leg.target_collection)
        if want is not None and got is not None and got == want:
            _log.info(
                "substrate_leg_already_converged",
                source=leg.source_collection,
                target=leg.target_collection,
                count=got,
            )
            continue
        remaining.append(leg)
    return SubstratePlan(legs=remaining, decisions=list(plan.decisions))


def _default_target_counts() -> dict[str, int] | None:
    """Every target collection's live row count, in ONE round trip.

    `list_collections()` answers from a single ``/v1/vectors/stats`` call
    (it exists precisely to replace an N-way count fan-out). The obvious
    shape here — `count(collection)` per candidate leg — is an N+1 on
    `nx doctor`'s read-only path: 18 sequential HTTP round trips per health
    check on the GH #1408 install shape, forever, since RDR-176 keeps the
    Chroma source (and therefore this rung's applicability) alive for good.
    That is the same N+1 the index-perf arc already removed elsewhere
    (nexus-vgtff: probe before fetch, 300s -> 0.6s).

    Returns None when the whole probe failed — "I could not tell", which is
    never "converged". An absent collection is simply missing from the map,
    which `drop_converged_legs` reads as not-converged: `count()` returns 0
    cleanly for an absent collection, so absence is data, not an error.
    """
    from nexus.db import make_t3  # noqa: PLC0415 — deferred to avoid import cycle

    try:
        rows = make_t3().list_collections()
    except Exception as exc:  # noqa: BLE001 — unreachable service: cannot tell, never "converged"
        # WARNING, not debug (P4.R1 Finding 4): every exception reaching here
        # is unexpected by construction — an absent collection returns 0
        # cleanly rather than raising — so this is a network/auth/5xx failure,
        # and it makes an already-converged install look pending. On the
        # converge() path that means re-running a migration whose content is
        # already there. A silent debug line is how that gets missed.
        _log.warning("substrate_target_counts_failed", error=str(exc))
        return None
    return {str(row["name"]): int(row.get("count") or 0) for row in rows if row.get("name")}


def _cascade_db_paths() -> "tuple[Path, Path]":
    """(catalog_db, memory_db) — the pair every cascade caller resolves the
    same way. One definition so the repair path, the probe and the migration
    can never disagree about which stores they mean."""
    from nexus.config import default_db_path  # noqa: PLC0415 — deferred to avoid import cycle

    db_path = default_db_path()
    return db_path.parent / "catalog" / ".catalog.db", db_path


def _default_unreflected() -> list[str]:
    """Local stores still holding an old id the persisted map re-identified.

    Read-only. An unreadable map/store answers "unreflected", never
    "fine" — a probe that cannot tell must not certify convergence.
    """
    from nexus.migration.remap_cascade import unreflected_stores  # noqa: PLC0415 — deferred to avoid import cost
    from nexus.migration.wire_reid import ChashRemapStore  # noqa: PLC0415 — deferred to avoid import cost

    map_path = _default_map_path()
    if not map_path.exists():
        return []  # nothing was ever re-identified: nothing to reflect
    catalog_db, memory_db = _cascade_db_paths()
    try:
        with ChashRemapStore(map_path) as map_store:
            return unreflected_stores(map_store, catalog_db=catalog_db, memory_db=memory_db)
    except Exception as exc:  # noqa: BLE001 — a probe that cannot tell must never certify convergence
        _log.warning("substrate_unreflected_probe_failed", error=str(exc))
        return ["<probe failed>"]


def _default_cascade_only(report: ProgressReporter) -> str:
    """Re-apply the persisted map to the local stores WITHOUT re-running any
    ETL — the repair for a crash between the last target write and the
    cascade. Returns "" on success, else a reason.

    Idempotent by construction (``cascade_remap``: "rewritten old ids no
    longer match"), so this is a cheap no-op whenever it is not needed.
    """
    from nexus.migration.remap_cascade import cascade_remap  # noqa: PLC0415 — deferred to avoid import cost
    from nexus.migration.wire_reid import ChashRemapStore  # noqa: PLC0415 — deferred to avoid import cost

    catalog_db, memory_db = _cascade_db_paths()
    try:
        with ChashRemapStore(_default_map_path()) as map_store:
            results = cascade_remap(map_store, catalog_db=catalog_db, memory_db=memory_db)
    except Exception as exc:  # noqa: BLE001 — reported as a FAILED rung; never a silent pass
        return f"{type(exc).__name__}: {exc}"
    failed = [r for r in results if not r.ok]
    if failed:
        return "; ".join(f"{r.store}: {r.reason or 'failed'}" for r in failed)
    for r in results:
        report.emit("substrate_cascade_repaired", store=r.store, rewritten=r.rewritten)
    return ""


def _default_footprint() -> bool:
    from nexus.upgrade_ladder.census import _chroma_footprint_present  # noqa: PLC0415 — deferred to avoid import cost

    return _chroma_footprint_present()


def _default_classify() -> list["CollectionClassification"]:
    from nexus.migration.guided_upgrade import detect_pending_migration_memoized  # noqa: PLC0415 — deferred; the bridge dies at RDR-155 P4b

    return list(detect_pending_migration_memoized().report.classifications)


def _default_voyage_key() -> bool:
    from nexus.migration.detection import voyage_key_available  # noqa: PLC0415 — deferred, detection is heavy

    return voyage_key_available()


def _default_prior_collections() -> frozenset[str]:
    """Source collections known from prior migration state — the SOURCE-GONE
    input. Read from the persisted map (the only durable record of what a
    prior run touched); empty on a first run, which correctly yields no
    decisions."""
    from nexus.migration.wire_reid import ChashRemapStore  # noqa: PLC0415 — deferred to avoid import cost

    try:
        with ChashRemapStore(_default_map_path()) as store:
            return frozenset(
                row[0] for row in store._conn.execute(  # noqa: SLF001 — same-package read of its own store
                    "SELECT DISTINCT source_collection FROM chash_remap"
                ).fetchall()
            )
    except Exception as exc:  # noqa: BLE001 — an unreadable map means no prior state to compare; decisions degrade to none, never a crash
        _log.debug("substrate_rung_prior_collections_unreadable", error=str(exc))
        return frozenset()


def _default_map_path():
    from nexus.config import default_db_path  # noqa: PLC0415 — deferred to avoid import cycle

    return default_db_path().parent / "chash_remap.db"


def _default_cost_gate(plan: SubstratePlan) -> bool:
    """The EXISTING billed-Voyage consent gate (nexus-cewad), unchanged: a
    run that bills nothing proceeds silently; a billed run without --yes
    never proceeds unattended (click.confirm aborts on a non-TTY)."""
    if not plan.billed_reembed:
        return True
    import click  # noqa: PLC0415 — deferred, CLI-only path

    return bool(
        click.confirm(
            "This upgrade re-embeds collections with a billed Voyage model. "
            "Proceed?",
            default=False,
        )
    )


class SubstrateEtlRung:
    """The substrate ETL as a ladder rung (chunk-identity + embedder-era
    co-resident). See module docstring.

    Constructor injection throughout; production defaults read the live
    census/classification and the persisted map.
    """

    name: str = "substrate-etl"

    def __init__(
        self,
        *,
        footprint_fn: Callable[[], bool] | None = None,
        classify_fn: Callable[[], list["CollectionClassification"]] | None = None,
        voyage_key_fn: Callable[[], bool] | None = None,
        prior_collections_fn: Callable[[], frozenset[str]] | None = None,
        cost_gate_fn: Callable[[SubstratePlan], bool] | None = None,
        migrate_fn: Callable[..., tuple[list[Any], list[Any]]] | None = None,
        target_counts_fn: Callable[[], dict[str, int] | None] | None = None,
        unreflected_fn: Callable[[], list[str]] | None = None,
        cascade_only_fn: Callable[[ProgressReporter], str] | None = None,
        page: int = 300,
    ) -> None:
        self._footprint = footprint_fn if footprint_fn is not None else _default_footprint
        self._classify = classify_fn if classify_fn is not None else _default_classify
        self._voyage_key = voyage_key_fn if voyage_key_fn is not None else _default_voyage_key
        self._prior = (
            prior_collections_fn if prior_collections_fn is not None else _default_prior_collections
        )
        self._cost_gate = cost_gate_fn if cost_gate_fn is not None else _default_cost_gate
        self._migrate = migrate_fn if migrate_fn is not None else self._default_migrate
        self._target_counts = (
            target_counts_fn if target_counts_fn is not None else _default_target_counts
        )
        self._unreflected = (
            unreflected_fn if unreflected_fn is not None else _default_unreflected
        )
        self._cascade_only = (
            cascade_only_fn if cascade_only_fn is not None else _default_cascade_only
        )
        self._page = page

    # ── plan ─────────────────────────────────────────────────────────────────

    def _plan(self) -> SubstratePlan | None:
        """The live plan of what STILL needs doing, or None when this rung is
        N/A (no Chroma footprint: service-mode or fresh install — the census's
        cheap file-level gate never opens a store).

        The already-converged filter is what gives this rung a TERMINAL STATE
        (bead nexus-mapbc). The source is immutable by design (RDR-176
        copy-not-move keeps it as the rollback target), so "a source exists"
        is true forever and can never mean "work is pending". Asking the
        SOURCE whether the migration happened is unanswerable; asking the
        TARGET is the whole question. This is the same check the P2 seam
        already deferred here — "a resumed run's full-count verification is
        the rung verify()'s job".
        """
        if not self._footprint():
            return None
        classifications = list(self._classify())
        plan = plan_substrate_legs(
            classifications,
            prior_collections=self._prior(),
            voyage_key_present=self._voyage_key(),
        )
        return drop_converged_legs(
            plan,
            {c.collection: c.source_count for c in classifications},
            self._target_counts(),
        )

    # ── detect ───────────────────────────────────────────────────────────────

    def detect(self) -> RungStatus:
        plan = self._plan()
        if plan is None:
            return RungStatus(applicable=False, converged=False)
        if not plan.legs and not plan.decisions:
            return RungStatus(applicable=True, converged=True)
        details: list[str] = []
        if plan.legs:
            shapes = ", ".join(
                f"{leg.source_collection}"
                + (" (legacy ids)" if leg.needs_reid else "")
                + (" (re-embed)" if leg.needs_reembed else "")
                for leg in plan.legs[:6]
            )
            details.append(f"{len(plan.legs)} collection(s) to converge: {shapes}")
        if plan.decisions:
            names = ", ".join(d.collection for d in plan.decisions)
            details.append(
                f"{len(plan.decisions)} genuine decision(s) awaiting an answer "
                f"(source gone — re-acquire vs drop): {names}"
            )
        return RungStatus(applicable=True, converged=False, pending_detail="; ".join(details))

    # ── converge ─────────────────────────────────────────────────────────────

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        plan = self._plan()
        if plan is None:
            return ConvergeResult(ConvergeOutcome.COMPLETED, detail="nothing to converge")
        if not plan.legs and not plan.decisions:
            # An empty plan does NOT mean there is nothing left to do (P4.R2
            # Critical). `run_substrate_migration` writes every leg's target
            # rows FIRST and cascades SECOND; a process death in that window
            # (OOM, host restart, SIGKILL) leaves the vector counts matching,
            # so the next run plans zero legs — while the catalog manifest,
            # chash_index, topic_assignments et al still point at the OLD
            # legacy chashes. Short-circuiting to COMPLETED here would record
            # the rung done forever with a half-applied identity change, and
            # nothing in the registry would ever repair it. Worst case is an
            # install with a SINGLE legacy collection: the entire migration
            # lives inside that window.
            #
            # cascade_remap is idempotent by construction ("rewritten old ids
            # no longer match"), so the repair is a cheap no-op on the healthy
            # path and the fix on the crashed one.
            orphaned = self._unreflected()
            if not orphaned:
                return ConvergeResult(ConvergeOutcome.COMPLETED, detail="nothing to converge")
            report.emit("substrate_rung_cascade_repair", stores=",".join(orphaned))
            failed = self._cascade_only(report)
            if failed:
                # ConvergeOutcome has no FAILED member by design — "failure
                # raises instead" (protocol.py), and the runner's RDR-142
                # guard then records nothing. Half-applied identity is a
                # data-correctness problem: fail loud, never a soft outcome.
                raise RuntimeError(
                    "the re-identification map was never reflected into "
                    f"{', '.join(orphaned)} and re-applying it failed: {failed}"
                )
            return ConvergeResult(
                ConvergeOutcome.COMPLETED,
                detail=f"re-applied the interrupted remap cascade to {', '.join(orphaned)}",
            )
        if plan.decisions:
            names = ", ".join(d.collection for d in plan.decisions)
            detail = (
                f"deferred: {len(plan.decisions)} source-gone decision(s) need an "
                f"operator answer (re-acquire vs drop): {names}"
            )
            report.emit("substrate_rung_deferred_decisions", collections=names)
            return ConvergeResult(ConvergeOutcome.DEFERRED, detail=detail)
        if not self._cost_gate(plan):
            report.emit("substrate_rung_deferred_cost_gate")
            return ConvergeResult(
                ConvergeOutcome.DEFERRED,
                detail="deferred: the billed re-embed cost gate was declined",
            )
        leg_results, cascade_results = self._migrate(plan, report=report)
        failed_legs = [r for r in leg_results if not getattr(r, "ok", True)]
        if failed_legs:
            raise RuntimeError(
                "substrate ETL leg failed: "
                + "; ".join(getattr(r, "reason", "?") for r in failed_legs)
            )
        failed_stores = [r for r in cascade_results if not getattr(r, "ok", True)]
        if failed_stores:
            raise RuntimeError(
                "remap cascade failed: "
                + "; ".join(
                    f"{getattr(r, 'store', '?')}: {getattr(r, 'reason', '?')}"
                    for r in failed_stores
                )
            )
        report.emit("substrate_rung_converged", legs=len(plan.legs))
        return ConvergeResult(ConvergeOutcome.COMPLETED)

    def _default_migrate(
        self, plan: SubstratePlan, *, report: ProgressReporter
    ) -> tuple[list[Any], list[Any]]:
        from nexus.config import default_db_path  # noqa: PLC0415 — deferred to avoid import cycle
        from nexus.db import make_t3  # noqa: PLC0415 — deferred to avoid import cycle
        from nexus.migration.detection import open_read_legs  # noqa: PLC0415 — deferred; RDR-176 read leg
        from nexus.migration.etl_ports import ChromaReadSource, VectorServiceTarget  # noqa: PLC0415 — deferred to avoid import cost
        from nexus.migration.wire_reid import ChashRemapStore  # noqa: PLC0415 — deferred to avoid import cost

        local, _cloud = open_read_legs(None)
        db_path = default_db_path()
        with ChashRemapStore(_default_map_path()) as map_store:
            return run_substrate_migration(
                plan,
                ChromaReadSource(local),
                VectorServiceTarget(make_t3()),
                map_store=map_store,
                catalog_db=db_path.parent / "catalog" / ".catalog.db",
                memory_db=db_path,
                page=self._page,
                provenance=f"substrate-etl-rung:{_installed_version()}",
            )

    # ── verify ───────────────────────────────────────────────────────────────

    def verify(self) -> bool:
        """AUTHORITATIVE post-state check (RDR-142): re-reads the WORLD and
        asks it whether the content ARRIVED — never consults converge's
        bookkeeping (the .14 resume path's relaxed this-run count check
        delegates the full check here).

        The world it must read is the TARGET (bead nexus-mapbc). This used to
        re-plan from the source alone and so could never return True: the
        source is immutable by design, so it describes what the install once
        held, not what it still owes. ``_plan()`` now answers the real
        question — every leg whose target holds the source's full row count is
        converged and gone from the plan — so an empty plan is a genuine
        terminal state rather than an unreachable one.
        """
        plan = self._plan()
        if plan is None:
            return True  # N/A: nothing to verify
        if plan.legs:
            return False
        # A decision the operator has not answered is UNFINISHED WORK, not a
        # converged rung (P4.R2 Medium). converge() returns DEFERRED for this
        # today so the runner never reaches verify — but verify calls itself
        # AUTHORITATIVE and is public Rung-protocol API, so it must not hand a
        # direct caller an answer that only happens to be unreachable.
        if plan.decisions:
            return False
        # The half of the world a vector count cannot see: the map may have
        # been written and never reflected into the local stores (the crash
        # window converge() now repairs). Counting rows in the target proves
        # the CONTENT arrived; it says nothing about whether every reference
        # to it was re-pointed. RDR-142 wants verify to re-read the WORLD —
        # this reads the rest of it, read-only and independent of anything
        # converge recorded.
        return not self._unreflected()


def _installed_version() -> str:
    from importlib.metadata import version  # noqa: PLC0415 — deferred, only on the record path

    try:
        return version("conexus")
    except Exception:  # noqa: BLE001 — provenance must never break the migration
        return "unknown"
