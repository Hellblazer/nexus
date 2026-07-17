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
from unittest.mock import patch

import pytest

from nexus.migration.detection import CollectionClassification
from nexus.migration.etl_ports import EtlRunResult
from nexus.migration.remap_cascade import StoreCascadeResult
from nexus.upgrade_ladder.protocol import ConvergeOutcome, Rung, RungStatus
from nexus.upgrade_ladder.registry import RUNG_SUBSTRATE_ETL, default_registry
from nexus.upgrade_ladder.rungs.substrate_etl import (
    LegPlan,
    SourceGoneDecision,
    SubstrateEtlRung,
    SubstratePlan,
    _default_cost_gate,
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
    assert _default_cost_gate(SubstratePlan(legs=[], billed_reembed=False)) is True


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
    from unittest.mock import MagicMock

    from nexus.upgrade_ladder.rungs import substrate_etl as mod

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
    from unittest.mock import MagicMock

    from nexus.upgrade_ladder.rungs import substrate_etl as mod

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
