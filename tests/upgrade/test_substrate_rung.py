# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P4.0 (nexus-x3z00): the substrate-ETL rung, assembled.

P2 built every part (census, planner, seam, wire re-id, cascade,
rollback-via-map) but nothing assembled them into a Rung the walk can
reach. This is that assembly: detect from the live census/classification,
converge via ``run_substrate_migration`` (genuine decisions resolved
FIRST), verify the post-state authoritatively (the check the .14 resume
path explicitly delegates upward).

N/A shapes detect-and-skip (f0pmd): a service-mode install with no Chroma
footprint, a conformant install, an empty footprint.
"""
from __future__ import annotations

import inspect
import pathlib
from typing import Any
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from nexus.migration.detection import CollectionClassification
from nexus.migration.etl_ports import EtlRunResult
from nexus.migration.remap_cascade import (
    StoreCascadeResult,
    cascade_remap,
    unreflected_stores,
)
from nexus.migration.wire_reid import ChashRemapStore, RemapEntry
from nexus.upgrade_ladder.completion import CompletionStore
from nexus.upgrade_ladder.protocol import ConvergeOutcome, Rung, RungStatus
from nexus.upgrade_ladder.registry import (
    RUNG_SUBSTRATE_ETL,
    LadderRegistry,
    default_registry,
)
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome
from nexus.upgrade_ladder.rungs import substrate_etl as mod
from nexus.upgrade_ladder.rungs.substrate_etl import (
    LegPlan,
    SourceGoneDecision,
    SubstrateEtlRung,
    SubstratePlan,
    SubstrateTargetCollision,
    _default_cost_gate,
    source_progress,
    drop_converged_legs,
    plan_substrate_legs,
)


def _cls(
    name: str,
    *,
    legacy: bool = False,
    support: str = "supported-voyage-1024",
    model: str | None = "voyage-context-3",
    count: int = 10,
) -> CollectionClassification:
    return CollectionClassification(
        collection=name,
        leg="local",
        model=model,
        dim=1024 if model else None,
        support=support,  # type: ignore[arg-type]
        source_count=count,
        has_data=count > 0,
        legacy_ids=legacy,
    )


class _AllTargets(dict):
    """A target world where EVERY collection reports the same row count.

    A plain ``defaultdict`` will not do: ``drop_converged_legs`` reads the map
    with ``.get()``, which never invokes a defaultdict's factory and would
    silently answer None — i.e. "not converged" — turning every
    already-converged fixture into a false negative.
    """

    def __init__(self, count: int) -> None:
        super().__init__()
        self._count = count

    def get(self, _key, _default=None):  # type: ignore[override]
        return self._count


def _all_targets(count: int) -> dict[str, int]:
    return _AllTargets(count)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    def emit(self, event: str, **fields: object) -> None:
        self.events.append(event)


def _rung(**kwargs: Any) -> SubstrateEtlRung:
    defaults: dict[str, Any] = {
        "footprint_fn": lambda: True,
        "classify_fn": lambda: [_cls("knowledge__old", legacy=True)],
        "voyage_key_fn": lambda: True,
        # nexus-mapbc: the already-converged filter probes the LIVE target, and
        # its production default is make_t3().list_collections() — a real
        # service call. Pin it to "no targets" (the pre-filter behaviour:
        # nothing converged, every leg stands) so no unit test can reach real
        # infrastructure the moment a stage gains a real-world default.
        "target_counts_fn": lambda: {},
        # P4.R2 Critical: the cascade probe + repair also have production
        # defaults that touch the real config dir's stores. Pin both — "no map
        # was ever written" is the healthy default for a fixture with no
        # re-identification in flight.
        "unreflected_fn": lambda: [],
        "cascade_only_fn": lambda _report: "",
    }
    defaults.update(kwargs)
    return SubstrateEtlRung(**defaults)


# ── protocol conformance + registration ──────────────────────────────────────


def test_rung_satisfies_the_protocol() -> None:
    assert isinstance(_rung(), Rung)
    assert _rung().name == RUNG_SUBSTRATE_ETL


def test_registered_in_the_default_registry_after_t2_schema() -> None:
    """The RQ2 hard edge, live: t2-schema precedes substrate-etl."""
    names = [r.name for r in default_registry()]
    assert names == ["t2-schema", RUNG_SUBSTRATE_ETL]


# ── detect ───────────────────────────────────────────────────────────────────


def test_detect_pending_on_legacy_footprint() -> None:
    status = _rung().detect()
    assert status.applicable and status.pending
    assert "knowledge__old" in status.pending_detail
    assert "legacy" in status.pending_detail.lower()


def test_detect_not_applicable_without_a_chroma_footprint() -> None:
    """Service-mode / fresh install: no footprint gate fires — the census's
    own cheap file-level check, never opening a store."""
    def _must_not_classify() -> list[CollectionClassification]:
        raise AssertionError("classification must not run without a footprint")

    status = _rung(footprint_fn=lambda: False, classify_fn=_must_not_classify).detect()
    assert status.applicable is False
    assert not status.pending


def test_detect_converged_on_a_conformant_footprint() -> None:
    status = _rung(classify_fn=lambda: [_cls("code__ok")]).detect()
    assert status.applicable is True
    assert status.converged is True


def test_detect_converged_on_an_empty_footprint() -> None:
    status = _rung(classify_fn=lambda: [_cls("knowledge__empty", legacy=True, count=0)]).detect()
    assert status.converged is True


def test_detect_reports_genuine_decisions_as_pending_detail() -> None:
    """A source-gone decision is WORK (it needs an operator answer), so the
    rung is pending and says why — never silently converged."""
    status = _rung(
        classify_fn=lambda: [_cls("knowledge__present")],
        prior_collections_fn=lambda: frozenset({"knowledge__present", "knowledge__gone"}),
    ).detect()
    assert status.pending
    assert "knowledge__gone" in status.pending_detail
    assert "decision" in status.pending_detail.lower()


def test_detect_is_read_only() -> None:
    """The doctor surface: detect classifies and plans, never migrates."""
    migrated = {"n": 0}
    rung = _rung(migrate_fn=lambda *a, **k: migrated.__setitem__("n", migrated["n"] + 1))
    rung.detect()
    rung.detect()
    assert migrated["n"] == 0


def test_detect_degrades_loudly_on_a_broken_census() -> None:
    def _boom() -> list[CollectionClassification]:
        raise RuntimeError("store unreadable")

    with pytest.raises(RuntimeError, match="store unreadable"):
        _rung(classify_fn=_boom).detect()


# ── converge ─────────────────────────────────────────────────────────────────


def test_converge_runs_the_migration_and_completes(tmp_path: pathlib.Path) -> None:
    calls: list[Any] = []

    def _migrate(plan, **kwargs):
        calls.append(plan)
        return ([], [])

    rung = _rung(migrate_fn=_migrate)
    result = rung.converge(_Recorder())
    assert result.outcome is ConvergeOutcome.COMPLETED
    assert len(calls) == 1
    assert calls[0].legs[0].source_collection == "knowledge__old"


def test_converge_defers_on_an_unresolved_genuine_decision(
    tmp_path: pathlib.Path,
) -> None:
    """Consent is never implicit: a source-gone decision the operator has
    not answered DEFERS the rung (non-fatal, records nothing, position
    pinned) rather than guessing or failing the upgrade."""
    migrated = {"n": 0}
    rung = _rung(
        classify_fn=lambda: [_cls("knowledge__present")],
        prior_collections_fn=lambda: frozenset({"knowledge__present", "knowledge__gone"}),
        migrate_fn=lambda *a, **k: migrated.__setitem__("n", migrated["n"] + 1),
    )
    result = rung.converge(_Recorder())
    assert result.outcome is ConvergeOutcome.DEFERRED
    assert "knowledge__gone" in result.detail
    assert migrated["n"] == 0  # nothing ran


def test_converge_defers_when_the_cost_gate_declines() -> None:
    """The billed re-embed keeps its existing consent gate: a declined cost
    prompt DEFERS (the operator's answer is 'not now'), never proceeds."""
    migrated = {"n": 0}
    rung = _rung(
        classify_fn=lambda: [
            _cls("knowledge__notes__all-minilm-l6-v2__v1", support="unsupported", model=None)
        ],
        cost_gate_fn=lambda plan: False,  # operator declined
        migrate_fn=lambda *a, **k: migrated.__setitem__("n", migrated["n"] + 1),
    )
    result = rung.converge(_Recorder())
    assert result.outcome is ConvergeOutcome.DEFERRED
    assert "cost" in result.detail.lower() or "declin" in result.detail.lower()
    assert migrated["n"] == 0


def test_cost_gate_sees_an_unbilled_plan_for_a_same_model_leg() -> None:
    """Derivable transitions stay promptless: the gate is consulted but the
    plan it receives is NOT billed (a same-model re-id leg), so the
    production gate returns True without prompting (nexus-cewad: nothing
    billed → no prompt)."""
    seen: list[Any] = []

    def _gate(plan) -> bool:
        seen.append(plan)
        return True

    _rung(cost_gate_fn=_gate, migrate_fn=lambda *a, **k: ([], [])).converge(_Recorder())
    assert len(seen) == 1
    assert seen[0].billed_reembed is False  # no bill => the real gate never prompts


def test_production_cost_gate_is_promptless_when_nothing_is_billed() -> None:
    """The production default gate, directly: an unbilled plan proceeds
    without any click.confirm (which would abort a non-TTY run)."""
    # No legs => nothing billed (billed_reembed is derived, not passed: as a
    # stored field it was silently dropped by drop_converged_legs — nexus-k1m2f).
    assert _default_cost_gate(SubstratePlan(legs=[])) is True


def test_converge_fails_loud_on_a_failed_leg() -> None:
    """A leg that genuinely failed is a rung FAILURE (raises → the runner
    records FAILED), never a silent completion."""
    rung = _rung(
        migrate_fn=lambda *a, **k: ([EtlRunResult(False, 5, 2, "upsert failed")], [])
    )
    with pytest.raises(RuntimeError, match="upsert failed"):
        rung.converge(_Recorder())


def test_converge_fails_loud_on_a_failed_cascade_store() -> None:
    rung = _rung(
        migrate_fn=lambda *a, **k: (
            [EtlRunResult(True, 5, 5)],
            [StoreCascadeResult("chash_index", False, reason="no such table")],
        )
    )
    with pytest.raises(RuntimeError, match="chash_index"):
        rung.converge(_Recorder())


# ── verify ───────────────────────────────────────────────────────────────────


def test_verify_true_when_the_census_is_clean() -> None:
    """Authoritative post-state: no legacy-id collection remains."""
    assert _rung(classify_fn=lambda: [_cls("code__ok")]).verify() is True


def test_verify_false_while_legacy_ids_remain() -> None:
    assert _rung().verify() is False  # the default fixture has a legacy collection


def test_verify_true_when_not_applicable() -> None:
    """An N/A rung (no footprint) verifies trivially — nothing to check."""
    assert _rung(footprint_fn=lambda: False).verify() is True


def test_verify_is_independent_of_converge_bookkeeping() -> None:
    """RDR-142: verify re-reads the WORLD, never a return value the converge
    handed it (the resume path's full-count check delegates here).

    Behaviour, not source text: this used to assert `"self._classify" in
    inspect.getsource(verify)`, which pinned the IMPLEMENTATION's spelling
    rather than the property — it broke the moment verify delegated to
    _plan() (which reads the world exactly as required) and would equally
    have passed a verify that called _classify and then ignored it.
    """
    # A converge that loudly claims total success cannot make verify true:
    # the target is empty, so the content did not arrive.
    rung = _rung(
        classify_fn=lambda: [_cls("knowledge__old", legacy=True, count=10)],
        target_counts_fn=lambda: _all_targets(0),
        migrate_fn=lambda *_a, **_k: ([], []),
    )
    assert rung.verify() is False

    # ...and the same rung verifies true once the WORLD says the rows landed,
    # with no converge having run at all in this process.
    landed = _rung(
        classify_fn=lambda: [_cls("knowledge__old", legacy=True, count=10)],
        target_counts_fn=lambda: _all_targets(10),
    )
    assert landed.verify() is True


def test_verify_is_reachable_on_an_immutable_source() -> None:
    """nexus-mapbc — the regression that made the rung unfinishable.

    RDR-176 keeps the Chroma source byte-untouched as the rollback target, so
    a source-derived verify asks "does a source exist?" — true forever — and
    the rung could NEVER record completion. Observed live in the P4.3 era-hop:
    4/4 legs migrated perfectly, verify-failed, doctor reported pending rungs
    forever, and every `nx upgrade` re-ran the full ETL (re-billing Voyage on
    a cloud leg). The source being present must not, by itself, mean pending.
    """
    source_still_there = lambda: True  # noqa: E731 — RDR-176: it always is
    rung = _rung(
        footprint_fn=source_still_there,
        classify_fn=lambda: [_cls("knowledge__old", legacy=True, count=10)],
        target_counts_fn=lambda: _all_targets(10),  # the world says: it all arrived
    )
    assert rung.verify() is True, (
        "verify must be satisfiable while the immutable source still exists — "
        "otherwise the rung has no terminal state"
    )
    assert rung.detect().pending is False, (
        "a converged rung must also stop reporting pending, or doctor nags "
        "forever and nx upgrade re-migrates at full cost every run"
    )


# ── nexus-mapbc: the already-converged filter (the rung's terminal state) ────


def _leg(source: str = "knowledge__old", target: str = "knowledge__new") -> LegPlan:
    return LegPlan(
        source_collection=source, target_collection=target,
        needs_reid=True, needs_reembed=False,
    )


def test_converged_leg_is_dropped_when_the_target_holds_the_full_count() -> None:
    plan = SubstratePlan(legs=[_leg()])
    out = drop_converged_legs(plan, {"knowledge__old": 12}, _all_targets(12))
    assert out.legs == []


def test_partial_target_is_not_converged() -> None:
    """A crashed/resumable run must stay planned — never rounded up to done."""
    plan = SubstratePlan(legs=[_leg()])
    out = drop_converged_legs(plan, {"knowledge__old": 12}, _all_targets(11))
    assert len(out.legs) == 1


def test_absent_or_unreachable_target_is_not_converged() -> None:
    """None means "could not tell" — which is never "converged". A silent
    skip here would drop real data on an unreachable service."""
    plan = SubstratePlan(legs=[_leg()])
    assert len(drop_converged_legs(plan, {"knowledge__old": 12}, None).legs) == 1


def test_converged_filter_reads_the_TARGET_name_not_the_source() -> None:
    """The bug's shape: asking the (immutable, always-present) source can only
    ever answer "still there". Convergence is a fact about the TARGET, so the
    lookup must be keyed by the target's name."""
    plan = SubstratePlan(legs=[_leg(source="knowledge__src", target="knowledge__dst")])
    counts = {"knowledge__src": 12}  # source-keyed: 12 rows, in the SOURCE

    # A world where only the source exists must NOT read as converged, no
    # matter how many rows it has — that is the state before any migration.
    assert len(drop_converged_legs(plan, {"knowledge__src": 12}, counts).legs) == 1

    # ...and the same leg drops the moment the TARGET is what holds them.
    assert drop_converged_legs(
        plan, {"knowledge__src": 12}, {"knowledge__dst": 12},
    ).legs == []


def test_decisions_survive_the_converged_filter() -> None:
    """A source-gone decision is a question for converge, not a leg — the
    filter must never silently answer it by dropping it."""
    plan = SubstratePlan(legs=[_leg()], decisions=[SourceGoneDecision(collection="gone")])
    out = drop_converged_legs(plan, {"knowledge__old": 12}, _all_targets(12))
    assert out.legs == []
    assert [d.collection for d in out.decisions] == ["gone"]


def test_converged_install_does_not_re_migrate() -> None:
    """The cost consequence, end to end: a fully-migrated install must plan
    ZERO legs, or every `nx upgrade` re-runs the whole ETL — and re-bills
    Voyage on a cloud leg."""
    rung = _rung(
        classify_fn=lambda: [_cls("knowledge__old", legacy=True, count=10)],
        target_counts_fn=lambda: _all_targets(10),
    )
    status = rung.detect()
    assert status.applicable is True
    assert status.converged is True
    assert status.pending is False


# ── nexus-6or3m: the convergence question, asked without a plan ──────────────
# The Gap-5 census holds classifications, not a plan, and needs the same answers.
# These pin that `source_progress` composes the two primitives above rather than
# becoming a THIRD derivation of "is this converged?" — the drift that produced
# nexus-mapbc and nexus-j5diu.

_BGE = "bge-base-en-v15-768"


def _reid_only(name: str, *, count: int = 12) -> CollectionClassification:
    """Legacy ids on an already-wired model: re-id, no re-embed, target ==
    source. The era-debt shape the census exists to report."""
    return _cls(name, legacy=True, model=_BGE, support="unsupported", count=count)


#: A GENUINE voyage collection (not a measured-dim mislabel): with no key this
#: deployment wires no embedder for it, so no leg is possible.
_GATED = _cls(
    "knowledge__v__voyage-context-3__v1",
    legacy=True,
    model="voyage-context-3",
    count=12,
)


def test_converged_sources_names_the_collection_whose_target_holds_its_rows() -> None:
    cls = [_reid_only("knowledge__proj__bge-base-en-v15-768__v1")]
    assert source_progress(
        cls,
        voyage_key_present=False,
        target_counts={"knowledge__proj__bge-base-en-v15-768__v1": 12},
    ).converged == frozenset({"knowledge__proj__bge-base-en-v15-768__v1"})


def test_converged_sources_is_empty_before_any_migration() -> None:
    """The pre-migration world: the source holds the rows and the target does
    not exist. Nothing is converged — this is what the census must still see."""
    cls = [_reid_only("knowledge__proj__bge-base-en-v15-768__v1")]
    assert source_progress(
        cls, voyage_key_present=False, target_counts={}
    ).converged == frozenset()


def test_converged_sources_answers_nothing_when_the_probe_cannot_tell() -> None:
    """None ("could not tell") certifies NOTHING as converged — the census then
    keeps reporting the debt, which is the safe direction: a momentarily
    unreachable service must not silently erase real era debt from doctor."""
    cls = [_reid_only("knowledge__proj__bge-base-en-v15-768__v1")]
    assert source_progress(
        cls, voyage_key_present=False, target_counts=None
    ).converged == frozenset()


def test_converged_sources_tracks_the_RENAMED_target_of_a_reembed_leg() -> None:
    """A re-embed leg's target is renamed, so only the planner knows which
    collection to count. Answering by source name would report a converged
    cross-model leg as debt forever."""
    cls = [_cls("knowledge__old", legacy=True, model=None, count=12)]
    renamed = f"knowledge__old__{_BGE}__v1"
    assert source_progress(
        cls, voyage_key_present=False, target_counts={renamed: 12}
    ).converged == frozenset({"knowledge__old"})
    # ...and the source's own name holding the rows is NOT convergence.
    assert source_progress(
        cls, voyage_key_present=False, target_counts={"knowledge__old": 12}
    ).converged == frozenset()


def test_credential_gated_legacy_collection_is_never_reported_converged() -> None:
    """The nexus-j5diu shape in the census surface. A voyage-named collection
    with no key is dropped by the PLANNER (credential-gate territory), so it
    has no leg and no target to count. It cannot migrate at all, which makes it
    the realest era debt there is — reporting it converged because the planner
    declined to plan it would vanish it from the one surface that shows it.

    MIXED world deliberately, and the mixing is the whole test (substantive
    critic, 2026-07-16): with the gated collection ALONE the plan is empty and
    the `if not plan.legs` short-circuit answers before the composition runs, so
    a single-collection fixture passes even when the answer is built from
    `classifications` instead of `plan.legs` — the exact drift this pin names.
    Mutation-verified: that mutant returns BOTH names here, and dies.
    """
    progress = source_progress(
        [_GATED, _reid_only("knowledge__b__bge-base-en-v15-768__v1")],
        voyage_key_present=False,
        target_counts=_all_targets(12),
    )
    assert progress.converged == frozenset({"knowledge__b__bge-base-en-v15-768__v1"})


def test_credential_gated_collection_is_NAMED_not_vanished() -> None:
    """nexus-mq42b. The planner cannot give it a leg, but it must still SAY so:
    a bare `continue` left the rung reporting converged over un-migrated data,
    and nothing on the `nx upgrade` path ever named the missing key. (The
    comment that skip deferred to — "the upstream credential gate C3" — lives in
    migrate_cmd / the dry-run preview, both DEMOTED at P4.)"""
    progress = source_progress(
        [_GATED], voyage_key_present=False, target_counts=_all_targets(12)
    )
    assert progress.credential_gated == frozenset({_GATED.collection})
    assert progress.converged == frozenset()  # named AND still outstanding


def test_credential_gate_lifts_when_the_key_is_present() -> None:
    """Non-vacuity for the pin above: the SAME collection is not gated once the
    deployment wires voyage — otherwise the test would pass on any always-gated
    implementation."""
    progress = source_progress(
        [_GATED], voyage_key_present=True, target_counts=_all_targets(12)
    )
    assert progress.credential_gated == frozenset()


def test_converged_sources_ignores_a_conformant_wired_collection() -> None:
    """A collection with conformant ids AND a wired model is never planned, so
    it is never in the answer. NOT the general claim: a conformant-ID collection
    on an UNWIRED model does get a re-embed leg and can appear — harmless for
    the census (which filters to legacy) but not something this pins."""
    assert source_progress(
        [_cls("code__fine", model=_BGE)],
        voyage_key_present=False,
        target_counts=_all_targets(10),
    ).converged == frozenset()


# ── nexus-fffey: two sources, one target — refuse, never merge ───────────────


def test_two_sources_remapping_onto_one_target_are_refused() -> None:
    """The ETL would write BOTH sources' rows into one collection — a silent,
    irreversible merge of two distinct collections. `cross_model_target_name`
    SYNTHESIZES for a 2-segment name and SWAPS for a 4-segment one, so these two
    land on the same target. Reachable on exactly the ancient install GH #1408
    describes (pre-RDR-103 and pre-RDR-109 collections side by side).

    A data-correctness problem fails LOUD; it is not a decision the operator can
    answer from a prompt."""
    colliding = [
        _cls("knowledge__old", legacy=True, model=None, count=12),
        _cls("knowledge__old__minilm-l6-v2-384__v1", legacy=True,
             model="minilm-l6-v2-384", count=12),
    ]
    with pytest.raises(SubstrateTargetCollision) as exc:
        plan_substrate_legs(
            colliding, prior_collections=frozenset(), voyage_key_present=False
        )
    # Names BOTH sources and the target they collide on — a bare "collision"
    # would leave the user with nothing to act on.
    assert "knowledge__old" in str(exc.value)
    assert "knowledge__old__minilm-l6-v2-384__v1" in str(exc.value)
    assert f"knowledge__old__{_BGE}__v1" in str(exc.value)


def test_distinct_targets_are_not_a_collision() -> None:
    """Non-vacuity: the guard must not fire on the ordinary multi-leg plan."""
    fine = [
        _reid_only("knowledge__a__bge-base-en-v15-768__v1"),
        _reid_only("knowledge__b__bge-base-en-v15-768__v1"),
    ]
    plan = plan_substrate_legs(
        fine, prior_collections=frozenset(), voyage_key_present=False
    )
    assert len(plan.legs) == 2


def test_one_collection_seen_on_two_read_legs_is_not_a_collision() -> None:
    """`classify_collections` emits one classification PER READ LEG, so a
    collection present on both local and cloud Chroma classifies twice — the
    SAME source, seen twice, not two sources merging.

    The first draft of the guard keyed on the raw source list and refused this
    outright: `plan_substrate_legs` raised, `detect()` raised, the rung went
    FAILED and `nx upgrade` was bricked forever on a perfectly healthy install
    (code review, 2026-07-17). The guard means DISTINCT sources."""
    def _leg_of(leg: str) -> CollectionClassification:
        return CollectionClassification(
            collection="knowledge__proj__bge-base-en-v15-768__v1",
            leg=leg, model=_BGE, dim=768, support="unsupported",
            source_count=12, has_data=True, legacy_ids=True,
        )

    # Asserts ONLY that the guard does not refuse. Deliberately NOT
    # `len(plan.legs) == 2`: that duplicate is itself a defect (nexus-bmiq9 —
    # the rung classifies the cloud leg and then reads only local, so the second
    # leg is a phantom it can never execute). Pinning the count would make the
    # phantom the contract and force bmiq9's fix to delete this test. The
    # guard's contract is "two DISTINCT sources must not merge"; one source
    # seen twice is simply not its business.
    plan = plan_substrate_legs(
        [_leg_of("local"), _leg_of("cloud")],
        prior_collections=frozenset(),
        voyage_key_present=False,
    )
    assert plan.legs, "the guard refused a single source seen on two read legs"


# ── nexus-k1m2f: the billed-Voyage consent gate must survive the plan filter ──


def _billed_leg() -> LegPlan:
    return LegPlan(
        source_collection="knowledge__old",
        target_collection="knowledge__old__voyage-context-3__v1",
        needs_reid=False, needs_reembed=True, billed=True,
    )


def _spy_confirm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record whether the REAL gate reached click.confirm, WITH a terminal.

    Both halves are necessary. An unpatched confirm raises OSError under
    pytest's captured stdin, which would make "did it prompt?" indistinguishable
    from a harness accident — hence the spy. And pytest has no TTY, so
    `_has_terminal()` is False and the gate now declines WITHOUT ever asking:
    a test that spies on the prompt is by definition a test that presumes
    someone is there to answer it, and must say so."""
    import click

    monkeypatch.setattr(mod, "_has_terminal", lambda: True)
    asked: list[str] = []
    monkeypatch.setattr(
        click, "confirm", lambda msg, **_kw: bool(asked.append(msg)) or False
    )
    return asked


def test_billed_flag_survives_the_converged_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE consent pin. `billed_reembed` used to be a stored field, and
    `drop_converged_legs` rebuilt the plan without it — so the cost gate saw
    False on every production path and billed Voyage with no estimate and no
    prompt (nexus-k1m2f). Deriving it from the surviving legs makes it
    unloseable by any future reconstruction."""
    plan = SubstratePlan(legs=[_billed_leg()])
    assert plan.billed_reembed is True
    # The leg has NOT converged (the target holds nothing), so it survives —
    # and the bill it implies must survive with it.
    survived = drop_converged_legs(plan, {"knowledge__old": 12}, {})
    assert len(survived.legs) == 1
    assert survived.billed_reembed is True

    asked = _spy_confirm(monkeypatch)
    assert _default_cost_gate(survived) is False  # declined => do not bill
    assert asked, "a billed leg survived the filter but the user was never asked"


def test_standing_consent_lets_a_billed_walk_converge_unattended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC-1's unattended channel (critique, 2026-07-17). Making the cost gate
    actually fire (k1m2f) gave `nx upgrade` a prompt no hook or cron can answer
    — trading a silent bill for a silent hang, for exactly the ancient install
    SC-1 promises reaches current UNATTENDED. NX_ASSUME_YES is standing consent;
    the RDR's ## Constraints now enumerate the billed re-embed as the third
    genuine decision, and a permitted prompt must have an unattended channel."""
    plan = SubstratePlan(legs=[_billed_leg()])
    asked = _spy_confirm(monkeypatch)

    monkeypatch.setenv("NX_ASSUME_YES", "1")
    assert _default_cost_gate(plan) is True
    assert asked == [], "standing consent must not stop to ask"

    # Non-vacuity: without it, the same plan DOES stop and ask.
    monkeypatch.delenv("NX_ASSUME_YES")
    assert _default_cost_gate(plan) is False
    assert asked, "without standing consent the user must be asked"


def test_no_terminal_declines_the_bill_without_ever_asking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-TTY is a DECLINE, not a crash (code review, 2026-07-17).

    Letting click.Abort escape made converge() raise, which the runner reports
    as FAILED (not DEFERRED) and `nx upgrade` renders as "did not converge —
    substrate-etl: failed (converge raised: )": an empty reason (Abort's str()
    is ""), exit 1, and no mention of the flag that fixes it — on exactly the
    unattended install SC-1 promises will converge.

    Decided BEFORE asking, so the prompt is never even reached: that is what
    keeps the next test's Ctrl-C distinction possible."""
    import click

    plan = SubstratePlan(legs=[_billed_leg()])
    monkeypatch.delenv("NX_ASSUME_YES", raising=False)
    monkeypatch.setattr(mod, "_has_terminal", lambda: False)

    def _must_not_ask(*_a: object, **_kw: object) -> bool:
        raise AssertionError("asked a question with no terminal to answer it")

    monkeypatch.setattr(click, "confirm", _must_not_ask)
    assert _default_cost_gate(plan) is False  # declined, NOT raised


def test_ctrl_c_at_the_prompt_is_not_a_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half, and the reason `_has_terminal` exists at all.

    click.confirm catches BOTH KeyboardInterrupt and EOFError and raises the
    SAME click.Abort, with an empty str() — so catching Abort to mean "no
    terminal" also swallowed Ctrl-C, and an interrupted upgrade returned a
    clean deferral and exit 0. A script reading that code would believe it
    succeeded. The two causes are indistinguishable AFTER the fact, so the
    question is settled BEFORE asking: with a terminal, Abort means only what
    click means by it, and propagates."""
    import click

    plan = SubstratePlan(legs=[_billed_leg()])
    monkeypatch.delenv("NX_ASSUME_YES", raising=False)
    monkeypatch.setattr(mod, "_has_terminal", lambda: True)

    def _interrupted(*_a: object, **_kw: object) -> bool:
        raise click.Abort()  # what click raises for a Ctrl-C at the prompt

    monkeypatch.setattr(click, "confirm", _interrupted)
    with pytest.raises(click.Abort):
        _default_cost_gate(plan)


def test_a_declined_bill_defers_and_names_the_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declining must be non-fatal AND actionable: DEFERRED (nothing recorded,
    re-derived next run), with the deferral naming the consent channel. A
    non-fatal message that does not say what to do leaves the user exactly as
    stuck as the hard failure did, just quieter."""
    rung = _rung(
        classify_fn=lambda: [_cls("knowledge__old", legacy=True)],
        cost_gate_fn=lambda _plan: False,
    )
    result = rung.converge(_Recorder())
    assert result.outcome is ConvergeOutcome.DEFERRED
    assert "--yes" in result.detail or "NX_ASSUME_YES" in result.detail


def test_a_converged_billed_leg_stops_asking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction, which preserving the flag verbatim would get wrong:
    once the billed leg converges and leaves the plan there is nothing left to
    bill, so a converged install must not prompt on every `nx upgrade`."""
    leg = _billed_leg()
    survived = drop_converged_legs(
        SubstratePlan(legs=[leg]),
        {leg.source_collection: 12},
        {leg.target_collection: 12},   # the billed leg has landed
    )
    assert survived.legs == []
    assert survived.billed_reembed is False

    asked = _spy_confirm(monkeypatch)
    assert _default_cost_gate(survived) is True  # nothing to bill...
    assert asked == []  # ...and nothing to ask


def test_planner_marks_only_the_voyage_targeted_leg_as_billed() -> None:
    """Real body: a cross-model leg targeting a VOYAGE model bills; the same
    shape targeting local bge does not."""
    cross = [_cls("knowledge__old", legacy=True, model=None, count=12)]
    to_voyage = plan_substrate_legs(
        cross, prior_collections=frozenset(), voyage_key_present=True
    )
    assert [leg.billed for leg in to_voyage.legs] == [True]
    assert to_voyage.billed_reembed is True

    to_bge = plan_substrate_legs(
        cross, prior_collections=frozenset(), voyage_key_present=False
    )
    assert [leg.billed for leg in to_bge.legs] == [False]
    assert to_bge.billed_reembed is False


def test_sc1s_own_shape_converges_with_nothing_to_consent_to() -> None:
    """SC-1 + SC-2 together, on the install that motivated the whole RDR: an
    ancient voyage-keyed instance whose collections carry pre-RDR-108 ids.
    SC-2: "Zero re-embedding ... for pure id-scheme conformance". So the leg
    bills NOTHING and must never reach the consent gate.

    This pin exists because a draft broke it. `_leg_can_bill` was widened to the
    target's declared model alone, to cover the mislabel billing path
    (nexus-92vz5) — but a pure re-id leg's target IS its source name, so every
    voyage-declared legacy collection read as billed. Combined with "no terminal
    declines", SC-1's own install stopped converging and reported exit 0 while
    doing it: a silent permanent non-convergence on the RDR's flagship shape,
    invisible to the era-hop (nexus-dnnbl). Reproduced against the real planner
    before the revert."""
    # _GATED is that shape (a genuine voyage collection with legacy ids); with
    # the key present it is planned rather than credential-gated.
    plan = plan_substrate_legs(
        [_GATED], prior_collections=frozenset(), voyage_key_present=True
    )
    (leg,) = plan.legs
    assert leg.needs_reid is True          # ids are rewritten on the wire...
    assert leg.needs_reembed is False      # ...and the vectors ride along free
    assert leg.billed is False             # so there is nothing to consent to
    assert plan.billed_reembed is False


def test_credential_gated_survives_the_converged_filter() -> None:
    """The k1m2f shape, guarded rather than re-lived: `drop_converged_legs`
    reconstructs the plan, and a field that is neither derived nor pinned is one
    refactor from being silently dropped — which is exactly how the billed flag
    died. `billed_reembed` is now derived and cannot be lost; `credential_gated`
    is a fact about the SOURCE world (not about which legs remain), so it is
    copied — and this is the pin that keeps the copy honest.

    The plan MUST carry a leg: `drop_converged_legs` early-returns the plan
    untouched when there are none, so an empty-legs fixture never reaches the
    reconstruction and passes with the copy deleted. Found by falsifying this
    very pin — its first draft was vacuous."""
    plan = SubstratePlan(legs=[_leg()], credential_gated=["knowledge__v"])
    survived = drop_converged_legs(plan, {"knowledge__old": 12}, {})  # leg survives
    assert survived.legs, "the reconstruction must actually run"
    assert survived.credential_gated == ["knowledge__v"]


# ── nexus-j5diu: the measured-768 mislabel must not be silently skipped ──────


def _mislabel(count: int = 12) -> CollectionClassification:
    """The pre-RDR-109 shape: voyage-NAMED, but a stored vector measured as
    local bge/ONNX 768 — the name lies, the content was never voyage text."""
    return CollectionClassification(
        collection="knowledge__mislabel__voyage-context-3__v1",
        leg="local",
        model="voyage-context-3",
        dim=1024,
        support="unsupported",
        source_count=count,
        has_data=True,
        measured_dim=768,
    )


def test_measured_768_mislabel_is_planned_without_a_voyage_key() -> None:
    """It needs no key: bge content re-embedded into bge is loss-free. The
    era-hop caught this collection being silently dropped — no leg, no error,
    no decision, service=0 vs seeded=12."""
    plan = plan_substrate_legs(
        [_mislabel()], prior_collections=frozenset(), voyage_key_present=False,
    )
    assert [leg.source_collection for leg in plan.legs] == [
        "knowledge__mislabel__voyage-context-3__v1"
    ], "the measured-768 mislabel must be a leg, not a silent skip"


def test_measured_768_mislabel_targets_the_local_model_and_never_bills() -> None:
    plan = plan_substrate_legs(
        [_mislabel()], prior_collections=frozenset(), voyage_key_present=False,
    )
    assert plan.legs[0].target_collection.endswith("__bge-base-en-v15-768__v1")
    assert plan.billed_reembed is False, (
        "provably-bge content must never bill a voyage re-embed (nexus-nb7hr)"
    )


def test_genuine_voyage_without_a_key_is_still_the_credential_case() -> None:
    """The rescue must not swallow the gate it is an exception to: real voyage
    content (no measured-768 proof) stays out of the plan without a key —
    re-embedding voyage text into bge would silently change recall."""
    genuine = CollectionClassification(
        collection="knowledge__real__voyage-context-3__v1",
        leg="local", model="voyage-context-3", dim=1024,
        support="unsupported", source_count=12, has_data=True,
        measured_dim=None,  # never probed as local bge
    )
    plan = plan_substrate_legs(
        [genuine], prior_collections=frozenset(), voyage_key_present=False,
    )
    assert plan.legs == []


# ── P4.R2 Critical: the ETL-landed-but-cascade-never-ran crash window ────────
#
# run_substrate_migration writes every leg's target rows FIRST and cascades
# SECOND. A process death in between (OOM, host restart, SIGKILL) leaves the
# vector counts matching — so the next run plans ZERO legs while the catalog
# manifest still points at legacy chashes. Before these pins, converge()
# short-circuited to COMPLETED without ever calling _migrate, verify() saw an
# empty plan and returned True, and the rung recorded COMPLETE FOREVER over a
# half-applied identity change with doctor reporting clean. Nothing in the
# 2-rung registry would ever have repaired it — and RDR-155 P4b deletes the
# only code that could.


def test_verify_false_when_the_map_was_never_reflected() -> None:
    """Counting rows in the target proves the CONTENT arrived; it says nothing
    about whether every reference to it was re-pointed."""
    rung = _rung(
        classify_fn=lambda: [_cls("knowledge__old", legacy=True, count=10)],
        target_counts_fn=lambda: _all_targets(10),          # vectors all landed
        unreflected_fn=lambda: ["document_chunks"],  # ...but the cascade did not
    )
    assert rung.verify() is False, (
        "a verify that only counts vectors records completion over an orphaned "
        "cascade — the manifest still points at legacy chashes"
    )


def test_converge_repairs_an_orphaned_cascade_without_re_running_the_etl() -> None:
    """The empty plan must not short-circuit past unreflected map rows: this is
    the ONLY thing that can ever repair the crash window."""
    repaired: list[str] = []
    migrated = {"n": 0}
    rung = _rung(
        classify_fn=lambda: [_cls("knowledge__old", legacy=True, count=10)],
        target_counts_fn=lambda: _all_targets(10),               # plan is empty
        unreflected_fn=lambda: ["document_chunks"],  # but the cascade is owed
        cascade_only_fn=lambda _r: repaired.append("ran") or "",
        migrate_fn=lambda *_a, **_k: migrated.__setitem__("n", migrated["n"] + 1),
    )
    result = rung.converge(_Recorder())
    assert result.outcome is ConvergeOutcome.COMPLETED
    assert repaired == ["ran"], "the interrupted cascade was never re-applied"
    assert migrated["n"] == 0, (
        "repair must NOT re-run the ETL — the vectors are already there, and "
        "re-running would re-bill a cross-model leg"
    )


def test_converge_fails_loud_when_the_cascade_repair_fails() -> None:
    """No silent fallbacks for data-correctness problems. ConvergeOutcome has
    no FAILED member by design ("failure raises instead") — a half-applied
    identity change must raise so the RDR-142 guard records nothing."""
    rung = _rung(
        classify_fn=lambda: [_cls("knowledge__old", legacy=True, count=10)],
        target_counts_fn=lambda: _all_targets(10),
        unreflected_fn=lambda: ["document_chunks"],
        cascade_only_fn=lambda _r: "document_chunks: disk full",
    )
    with pytest.raises(RuntimeError, match="document_chunks"):
        rung.converge(_Recorder())


def test_healthy_converged_install_does_not_pay_the_repair_path() -> None:
    """Non-vacuity for the pins above: with nothing owed, converge still
    short-circuits and the repair never fires."""
    repaired: list[str] = []
    rung = _rung(
        classify_fn=lambda: [_cls("knowledge__old", legacy=True, count=10)],
        target_counts_fn=lambda: _all_targets(10),
        unreflected_fn=lambda: [],
        cascade_only_fn=lambda _r: repaired.append("ran") or "",
    )
    result = rung.converge(_Recorder())
    assert result.outcome is ConvergeOutcome.COMPLETED
    assert result.detail == "nothing to converge"
    assert repaired == []
    assert rung.verify() is True


def test_a_probe_that_cannot_tell_never_certifies_convergence() -> None:
    """_default_unreflected reports ["<probe failed>"] rather than [] when it
    cannot read the map/stores. Verify must treat that as NOT converged."""
    rung = _rung(
        classify_fn=lambda: [_cls("knowledge__old", legacy=True, count=10)],
        target_counts_fn=lambda: _all_targets(10),
        unreflected_fn=lambda: ["<probe failed>"],
    )
    assert rung.verify() is False


# ── P4.R2 Medium: verify() must not ignore outstanding decisions ─────────────


def test_verify_false_while_a_genuine_decision_is_unanswered() -> None:
    """converge() returns DEFERRED for this today, so the runner never reaches
    verify — but verify calls itself AUTHORITATIVE and is public Rung-protocol
    API. A direct caller must not be told a rung with unanswered work is done.
    """
    rung = _rung(
        classify_fn=lambda: [_cls("knowledge__present")],
        prior_collections_fn=lambda: frozenset({"knowledge__present", "knowledge__gone"}),
        target_counts_fn=lambda: _all_targets(10),
    )
    assert rung.verify() is False


# ── the PRODUCTION default's own body (P4.R1 F3/F4) ─────────────────────────
#
# Every unit test above injects a fake for target_counts_fn, which is correct
# — it keeps them off real infrastructure. But that is exactly why this
# phase's three P0s were invisible to 1568 green tests: NOTHING executed the
# real defaults. These tests execute _default_target_counts' actual body with
# only the transport faked, so its batching and its failure handling are
# covered by something other than a happy-path container run.


def test_default_target_counts_uses_ONE_round_trip_not_N() -> None:
    """`list_collections()` answers every count from a single /stats call.
    The obvious `count(collection)`-per-leg shape is an N+1 on doctor's
    read-only path — 18 sequential HTTP round trips per health check on the
    GH #1408 install shape, forever, because RDR-176 keeps this rung
    applicable for good."""
    client = MagicMock()
    client.list_collections.return_value = [
        {"name": "knowledge__a", "count": 12},
        {"name": "knowledge__b", "count": 0},
    ]
    with patch.object(mod, "make_t3", create=True), \
         patch("nexus.db.make_t3", return_value=client):
        counts = mod._default_target_counts()

    assert counts == {"knowledge__a": 12, "knowledge__b": 0}
    client.list_collections.assert_called_once()
    client.count.assert_not_called(), "per-collection count() is the N+1 shape"


def test_default_target_counts_returns_None_when_it_cannot_tell() -> None:
    """A probe failure is "I could not tell", never "converged" — and never
    "absent" either. An absent collection returns 0 cleanly from the service,
    so every exception here is a network/auth/5xx failure. Returning {} would
    read as "no targets exist" and be indistinguishable from a fresh install;
    None makes drop_converged_legs drop NOTHING.

    This is the re-billing door: on the converge() path, mistaking an
    already-migrated leg for pending re-runs the ETL and re-bills Voyage.
    """
    client = MagicMock()
    client.list_collections.side_effect = RuntimeError("connection refused")
    with patch("nexus.db.make_t3", return_value=client):
        assert mod._default_target_counts() is None


def test_a_probe_failure_never_drops_a_leg() -> None:
    """The end-to-end consequence of the above: an unreachable service must
    not make a pending install look converged, NOR a converged install look
    pending-and-get-re-migrated. None drops nothing; the legs stand."""
    plan = SubstratePlan(legs=[_leg()])
    assert len(drop_converged_legs(plan, {"knowledge__old": 12}, None).legs) == 1


# ── P4.V Finding A: the repair must be reachable THROUGH THE RUNNER ──────────
#
# Every cascade-repair test above calls rung.converge() directly. Those are
# valid unit tests of the method and they were FALSE CONFIDENCE about the walk:
# LadderRunner._run_rung only calls converge() when detect() reports NOT
# converged, so a legs-only detect() made the whole repair unreachable in
# production — VERIFY_FAILED forever, no self-heal, and RDR-155 P4b deletes the
# only module that could fix it by hand. Both stacked reviewers signed off on
# the unreachable fix; the test-validator caught it by driving the real runner.
#
# So this pin drives the REAL LadderRunner + a REAL CompletionStore. If the
# repair is ever moved back behind a detect()-says-converged gate, this fails
# and the direct-converge() tests above will not.


def test_the_crash_window_heals_through_a_real_runner_walk(tmp_path: pathlib.Path) -> None:

    # The crash state: every vector landed (counts match), the map was written,
    # the cascade never ran, nothing was recorded.
    world = {"reflected": False}
    repairs: list[str] = []
    migrated = {"n": 0}

    def _cascade_only(_report) -> str:
        repairs.append("ran")
        world["reflected"] = True
        return ""

    rung = _rung(
        classify_fn=lambda: [_cls("knowledge__old", legacy=True, count=10)],
        target_counts_fn=lambda: _all_targets(10),
        unreflected_fn=lambda: [] if world["reflected"] else ["document_chunks"],
        cascade_only_fn=_cascade_only,
        migrate_fn=lambda *_a, **_k: migrated.__setitem__("n", migrated["n"] + 1),
    )

    with CompletionStore(tmp_path / "ladder.db", now_fn=lambda: "t0") as store:
        report = LadderRunner(
            LadderRegistry((rung,)), store, package_version_fn=lambda: "6.12.0",
        ).run()

        assert repairs == ["ran"], (
            "the walk never reached the cascade repair — detect() reported "
            "converged, so the runner skipped converge() entirely and the fix "
            "is dead code in production"
        )
        assert migrated["n"] == 0, "repair must not re-run the ETL"
        outcomes = [r.outcome for r in report.runs]
        assert outcomes == [RungOutcome.RECORDED], (
            f"a repairable crash window must converge and record, got {outcomes}"
        )
        assert RUNG_SUBSTRATE_ETL in store.verified_rungs()


def test_an_unrepairable_crash_window_never_records(tmp_path: pathlib.Path) -> None:
    """Non-vacuity for the pin above: if the repair genuinely fails, the walk
    must NOT record. The heal path must not become a way to launder a broken
    cascade into a recorded completion."""

    rung = _rung(
        classify_fn=lambda: [_cls("knowledge__old", legacy=True, count=10)],
        target_counts_fn=lambda: _all_targets(10),
        unreflected_fn=lambda: ["document_chunks"],   # never becomes reflected
        cascade_only_fn=lambda _r: "document_chunks: disk full",
    )

    with CompletionStore(tmp_path / "ladder.db", now_fn=lambda: "t0") as store:
        report = LadderRunner(
            LadderRegistry((rung,)), store, package_version_fn=lambda: "6.12.0",
        ).run()
        assert report.hard_failed
        assert RUNG_SUBSTRATE_ETL not in store.verified_rungs()


def test_detect_and_verify_agree_that_an_unreflected_map_is_unfinished() -> None:
    """The root cause of Finding A, pinned directly: the two must not disagree
    about what "converged" means, or the runner's detect-first gate routes past
    the repair. detect() said converged while verify() said no."""
    rung = _rung(
        classify_fn=lambda: [_cls("knowledge__old", legacy=True, count=10)],
        target_counts_fn=lambda: _all_targets(10),
        unreflected_fn=lambda: ["document_chunks"],
    )
    assert rung.detect().converged is False
    assert rung.verify() is False

    healthy = _rung(
        classify_fn=lambda: [_cls("knowledge__old", legacy=True, count=10)],
        target_counts_fn=lambda: _all_targets(10),
        unreflected_fn=lambda: [],
    )
    assert healthy.detect().converged is True
    assert healthy.verify() is True


# ── the cascade defaults' REAL bodies (P4.V Finding B) ───────────────────────
#
# _default_unreflected / _default_cascade_only are the newest production
# defaults and — per Finding A — were literally unreachable in production until
# detect() learned to consult them. Every test above injects fakes for both.
# These execute the real bodies against a real map + real sqlite stores, which
# is the only thing that catches a wrong table/column (the first draft of the
# probe guessed a `chash` column for the aspect tables; they key on
# source_path, and the wrong guess would have been silently blind in exactly
# the direction that certifies an orphaned cascade).


def _seeded_cascade(tmp_path: pathlib.Path) -> tuple[Any, str, str]:
    """A real map with one old->new pair + a real catalog db holding the OLD
    chash in its manifest — i.e. the crash window, on disk."""

    old, new = "a" * 16, "b" * 32
    catalog_db = tmp_path / "catalog.db"
    con = sqlite3.connect(catalog_db)
    con.execute("CREATE TABLE document_chunks (doc_id TEXT, chash TEXT, position INT)")
    con.execute("INSERT INTO document_chunks VALUES ('d1', ?, 0)", (old,))
    con.commit()
    con.close()

    store = ChashRemapStore(tmp_path / "map.db")
    store.record_batch([RemapEntry(
        source_collection="knowledge__old", target_collection="knowledge__new",
        old_id=old, new_chash=new, provenance="test", tenant_id="",
    )])
    return store, str(catalog_db), old


def test_unreflected_stores_sees_an_orphaned_cascade_and_clears_after_it_runs(
    tmp_path: pathlib.Path,
) -> None:
    """The probe's real body against real stores, both directions."""

    store, catalog_db, old = _seeded_cascade(tmp_path)
    memory_db = tmp_path / "memory.db"
    sqlite3.connect(memory_db).close()  # empty: its tables simply do not exist

    with store:
        before = unreflected_stores(store, catalog_db=pathlib.Path(catalog_db), memory_db=memory_db)
        assert "document_chunks" in before, (
            "the probe cannot see an old chash still sitting in the manifest — "
            "it would certify an orphaned cascade as converged"
        )
        cascade_remap(store, catalog_db=pathlib.Path(catalog_db), memory_db=memory_db)
        after = unreflected_stores(store, catalog_db=pathlib.Path(catalog_db), memory_db=memory_db)
        assert "document_chunks" not in after

    con = sqlite3.connect(catalog_db)
    assert con.execute("SELECT chash FROM document_chunks").fetchone()[0] == "b" * 32
    con.close()


def test_unreflected_stores_is_empty_for_an_empty_map(tmp_path: pathlib.Path) -> None:
    """Nothing was ever re-identified -> nothing to reflect. Must not report
    every store as unreflected on an install that never re-id'd anything."""

    with ChashRemapStore(tmp_path / "map.db") as store:
        assert unreflected_stores(
            store, catalog_db=tmp_path / "nope.db", memory_db=tmp_path / "nope2.db",
        ) == []


def test_default_unreflected_is_silent_when_no_map_was_ever_written(
    tmp_path: pathlib.Path, monkeypatch,
) -> None:
    """The production default's own gate: a first-run install has no map file,
    which is [] (nothing owed) — never "<probe failed>", which would make
    detect() report pending forever on a healthy fresh box."""
    monkeypatch.setattr(mod, "_default_map_path", lambda: tmp_path / "absent.db")
    assert mod._default_unreflected() == []


def test_default_unreflected_reports_probe_failure_rather_than_certifying(
    tmp_path: pathlib.Path, monkeypatch,
) -> None:
    """A probe that cannot tell must never answer "converged"."""
    map_path = tmp_path / "map.db"
    map_path.write_text("not a sqlite database")
    monkeypatch.setattr(mod, "_default_map_path", lambda: map_path)
    monkeypatch.setattr(mod, "_cascade_db_paths", lambda: (tmp_path / "c.db", tmp_path / "m.db"))
    assert mod._default_unreflected() == ["<probe failed>"]
