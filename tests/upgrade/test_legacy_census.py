# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P1.2 (nexus-n7u38.9): legacy chunk-id census + doctor surface.

Gap-5 falsifiable criterion: a Chroma-mode install with legacy-id
collections sees them listed in ``nx doctor`` from the release shipping
the detector; conformant / non-Chroma installs skip cleanly. Detect-only
in P1 — the census is deliberately NOT a walk rung (no remediation until
the P2 substrate rung), so nothing here can fail ``nx upgrade``.
"""
from __future__ import annotations

import inspect

import pytest

import nexus.config as config_mod
import nexus.migration.detection as detection_mod
import nexus.migration.guided_upgrade as guided_upgrade
import nexus.upgrade_ladder.census as census_mod
import nexus.upgrade_ladder.rungs.substrate_etl as substrate_mod
from nexus.db.pg_provision import CREDENTIALS_FILENAME
from nexus.health import _check_legacy_id_census, run_health_checks
from nexus.migration.detection import CollectionClassification, DetectionReport
from nexus.migration.guided_upgrade import PreflightDetection
from nexus.upgrade_ladder.census import LegacyCollection, legacy_id_census
from nexus.upgrade_ladder.rungs.substrate_etl import SourceProgress, SubstrateTargetCollision
from nexus.upgrade_ladder.registry import default_registry


def _classification(name: str, *, legacy: bool, count: int = 10) -> CollectionClassification:
    return CollectionClassification(
        collection=name,
        leg="local",
        model=None if legacy else "voyage-context-3",
        dim=None if legacy else 1024,
        support="unsupported" if legacy else "supported-voyage-1024",
        source_count=count,
        has_data=count > 0,
        reason="collection holds legacy non-32-char chunk ids" if legacy else "",
        legacy_ids=legacy,
    )


def _detection(*classifications: CollectionClassification) -> PreflightDetection:
    return PreflightDetection(
        report=DetectionReport(classifications=tuple(classifications)),
        needs_migration=bool(classifications),
    )


def _nothing_converged(_classifications: object) -> SourceProgress:
    """The pre-migration world, injected: the census asks the target and the
    target holds nothing yet. Also keeps unit tests off the network — the
    production ``_default_progress`` opens an HTTP client (its own body is
    exercised by ``test_default_progress_*`` below)."""
    return SourceProgress()


# ── legacy_id_census ─────────────────────────────────────────────────────────


def test_census_skips_without_opening_store_when_no_footprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cheap file-level gate: no local Chroma directory means None
    WITHOUT ever invoking the store-opening classification."""
    monkeypatch.setattr(census_mod, "_chroma_footprint_present", lambda: False)

    def _must_not_run() -> PreflightDetection:
        raise AssertionError("detect_pending_migration must not be called")

    monkeypatch.setattr(guided_upgrade, "detect_pending_migration", _must_not_run)
    assert legacy_id_census() is None


def test_census_fires_despite_service_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1 critique High (the GH #1408 recurrence shape): a provisioned
    install (service exists) still carrying un-migrated legacy-id Chroma
    collections MUST be censused — the census deliberately does NOT use
    legacy_footprint_pending's service-evidence early-outs (provisioned is
    not migrated: legacy-id collections CANNOT have migrated, GH #1390
    blocks them)."""
    # Simulate the hybrid state: bridge gate says "not pending" (service
    # evidence), yet the Chroma footprint with legacy ids is right there.
    monkeypatch.setattr(guided_upgrade, "legacy_footprint_pending", lambda: False)
    monkeypatch.setattr(census_mod, "_chroma_footprint_present", lambda: True)
    monkeypatch.setattr(
        guided_upgrade,
        "detect_pending_migration",
        lambda: _detection(_classification("knowledge__old_store", legacy=True, count=18)),
    )
    result = legacy_id_census(progress_fn=_nothing_converged)
    assert result is not None
    assert [c.collection for c in result] == ["knowledge__old_store"]


def test_footprint_gate_respects_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_MIGRATION_NOTICE", "0")
    assert census_mod._chroma_footprint_present() is False


def test_footprint_gate_checks_local_chroma_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.delenv("NX_MIGRATION_NOTICE", raising=False)
    monkeypatch.setattr(detection_mod, "resolve_default_local_leg", lambda: str(tmp_path))
    assert census_mod._chroma_footprint_present() is True
    monkeypatch.setattr(
        detection_mod, "resolve_default_local_leg", lambda: str(tmp_path) + "/absent"
    )
    assert census_mod._chroma_footprint_present() is False


def test_census_lists_only_legacy_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(census_mod, "_chroma_footprint_present", lambda: True)
    monkeypatch.setattr(
        guided_upgrade,
        "detect_pending_migration",
        lambda: _detection(
            _classification("knowledge__old_store", legacy=True, count=1234),
            _classification("code__nexus__voyage_code_3__v1", legacy=False),
            _classification("docs__legacy_two", legacy=True, count=7),
        ),
    )
    result = legacy_id_census(progress_fn=_nothing_converged)
    assert result == [
        LegacyCollection(
            collection="knowledge__old_store",
            leg="local",
            source_count=1234,
            reason="collection holds legacy non-32-char chunk ids",
        ),
        LegacyCollection(
            collection="docs__legacy_two",
            leg="local",
            source_count=7,
            reason="collection holds legacy non-32-char chunk ids",
        ),
    ]


def test_census_empty_when_chroma_mode_but_conformant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(census_mod, "_chroma_footprint_present", lambda: True)
    monkeypatch.setattr(
        guided_upgrade,
        "detect_pending_migration",
        lambda: _detection(_classification("code__ok", legacy=False)),
    )
    assert legacy_id_census() == []


def test_census_degrades_to_none_on_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(census_mod, "_chroma_footprint_present", lambda: True)

    def _boom() -> PreflightDetection:
        raise RuntimeError("store exploded")

    monkeypatch.setattr(guided_upgrade, "detect_pending_migration", _boom)
    assert legacy_id_census() is None


# ── nexus-6or3m: the census reports OUTSTANDING debt, not history ────────────
# Reproduced live in a PASSING era-hop run: a fully converged install, every
# collection at parity on the wire, doctor reporting "no pending rungs" — and
# the census still warning about era debt, because it asked the SOURCE. RDR-176
# keeps that source byte-untouched forever as the rollback target, so it holds
# its legacy ids for the rest of the install's life. "A source exists" can never
# mean "work is pending" (the third instance of this class: nexus-mapbc,
# nexus-j5diu, this).


def test_census_drops_a_collection_the_target_has_converged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE pin. The source still holds legacy ids (it always will) — and the
    census must say nothing, because the target already holds its rows."""
    monkeypatch.setattr(census_mod, "_chroma_footprint_present", lambda: True)
    monkeypatch.setattr(
        guided_upgrade,
        "detect_pending_migration",
        lambda: _detection(_classification("knowledge__old_store", legacy=True)),
    )
    assert legacy_id_census(
        progress_fn=lambda _: SourceProgress(converged=frozenset({"knowledge__old_store"}))
    ) == []


def test_census_reports_only_the_UNconverged_of_several(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-migrated install still owes the half that did not land — the fix
    must not degrade into "migration started, therefore silence"."""
    monkeypatch.setattr(census_mod, "_chroma_footprint_present", lambda: True)
    monkeypatch.setattr(
        guided_upgrade,
        "detect_pending_migration",
        lambda: _detection(
            _classification("knowledge__done", legacy=True),
            _classification("docs__pending", legacy=True, count=7),
        ),
    )
    result = legacy_id_census(progress_fn=lambda _: SourceProgress(converged=frozenset({"knowledge__done"})))
    assert [c.collection for c in result] == ["docs__pending"]


def test_census_asks_the_target_nothing_when_no_debt_ever_existed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cost pin: `nx doctor` runs constantly and the convergence probe is a
    live round trip. A conformant install has no legacy collection to ask
    about, so it must not pay for one."""
    monkeypatch.setattr(census_mod, "_chroma_footprint_present", lambda: True)
    monkeypatch.setattr(
        guided_upgrade,
        "detect_pending_migration",
        lambda: _detection(_classification("code__ok", legacy=False)),
    )

    def _must_not_run(_classifications: object) -> frozenset[str]:
        raise AssertionError("the target must not be probed with no debt to weigh")

    assert legacy_id_census(progress_fn=_must_not_run) == []


# ── the production default's own body (the injected-fakes-hide-defaults rule) ─


def _legacy_on_wired_model() -> CollectionClassification:
    """Legacy ids on an already-wired model: re-id only, target == source."""
    return CollectionClassification(
        collection="knowledge__proj__bge-base-en-v15-768__v1",
        leg="local",
        model="bge-base-en-v15-768",
        dim=768,
        support="unsupported",
        source_count=12,
        has_data=True,
        legacy_ids=True,
    )


def test_default_progress_reads_the_live_target_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runs `_default_converged`'s REAL body against a faked HTTP boundary.
    Every collaborator above is injected, which is exactly how three P0s hid
    behind green tests this arc: nothing had executed the real thing."""
    monkeypatch.setattr(census_mod, "_no_target_provisioned", lambda: False)
    monkeypatch.setattr(detection_mod, "voyage_key_available", lambda: False)
    monkeypatch.setattr(
        substrate_mod,
        "_default_target_counts",
        lambda: {"knowledge__proj__bge-base-en-v15-768__v1": 12},
    )
    assert census_mod._default_progress([_legacy_on_wired_model()]).converged == frozenset(
        {"knowledge__proj__bge-base-en-v15-768__v1"}
    )


def test_default_progress_reports_nothing_converged_when_the_probe_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that cannot tell must never certify convergence — the debt stays
    visible in doctor rather than being erased by an unreachable service.

    The classification is one that WOULD converge (its target holds the full
    count), so the assertion actually distinguishes the raise from the happy
    path. An empty list would pass whether or not the except branch exists —
    `[]` plans no legs and returns frozenset() regardless (code reviewer,
    2026-07-16; the same non-vacuity discipline the era-hop leg carries)."""
    monkeypatch.setattr(census_mod, "_no_target_provisioned", lambda: False)
    monkeypatch.setattr(
        substrate_mod,
        "_default_target_counts",
        lambda: {"knowledge__proj__bge-base-en-v15-768__v1": 12},
    )
    cls = _legacy_on_wired_model()
    # Control: without the raise, this very input DOES converge.
    monkeypatch.setattr(detection_mod, "voyage_key_available", lambda: False)
    assert census_mod._default_progress([cls]).converged == frozenset(
        {"knowledge__proj__bge-base-en-v15-768__v1"}
    )

    def _boom() -> None:
        raise RuntimeError("service unreachable")

    monkeypatch.setattr(detection_mod, "voyage_key_available", _boom)
    assert census_mod._default_progress([cls]).converged == frozenset()


def test_default_progress_never_probes_an_unprovisioned_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No service provisioned means nothing can have converged INTO it — answer
    from the on-disk fact, do not pay a doomed HTTP attempt and do not fire
    `substrate_target_counts_failed` at WARNING on every `nx doctor` for the
    un-provisioned legacy-bearing install (the GH #1408 population the census
    exists for). The deliberately-loud probe failure must stay rare enough to
    mean something (substantive critic, 2026-07-16).

    Records the call instead of raising on it: `_default_converged` catches
    Exception by design, so a raise-based "must not call" is swallowed and the
    test passes with the gate DELETED. Found by falsifying this very pin — the
    first draft of it was vacuous.

    The stubbed counts are ones that WOULD converge the collection, so removing
    the gate changes the ANSWER as well as firing the probe. Both are asserted.
    """
    monkeypatch.setattr(census_mod, "_no_target_provisioned", lambda: True)
    monkeypatch.setattr(detection_mod, "voyage_key_available", lambda: False)
    probes: list[str] = []

    def _record_probe() -> dict[str, int]:
        probes.append("probed")
        return {"knowledge__proj__bge-base-en-v15-768__v1": 12}

    monkeypatch.setattr(substrate_mod, "_default_target_counts", _record_probe)
    assert census_mod._default_progress([_legacy_on_wired_model()]).converged == frozenset()
    assert probes == [], "probed a target that cannot exist on an unprovisioned install"


def _colliding_pair() -> list[CollectionClassification]:
    """Two DISTINCT sources that remap onto one target: a 2-segment
    (pre-RDR-103) name is SYNTHESIZED into `__bge-base-en-v15-768__v1`, and the
    4-segment (pre-RDR-109) name has its model segment SWAPPED to the same."""
    def _c(name: str, model: str | None) -> CollectionClassification:
        return CollectionClassification(
            collection=name, leg="local", model=model, dim=None,
            support="unsupported", source_count=12, has_data=True, legacy_ids=True,
        )

    return [_c("knowledge__old", None), _c("knowledge__old__minilm-l6-v2-384__v1", "minilm-l6-v2-384")]


def test_default_progress_reports_debt_when_the_world_is_unmigratable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-fffey through this surface: `nx upgrade` refuses a collided world
    LOUDLY, but `nx doctor` must still print its rows. Nothing converged is the
    true and safe answer — the collided collections have not moved.

    Feeds the REAL colliding pair, so the collision is raised where it actually
    is — inside `plan_substrate_legs`, within `source_progress`. An earlier
    draft raised it from a stubbed `_default_target_counts`, a boundary that can
    never raise it: `plan_substrate_legs` runs AFTER those counts are already
    in hand. Both reviewers mutation-proved that draft passed with
    `_refuse_target_collisions` deleted entirely — it named fffey and
    constrained nothing about it.

    The stubbed counts WOULD converge both collections, so a missing guard
    changes the answer and the assertion can tell the difference."""
    monkeypatch.setattr(census_mod, "_no_target_provisioned", lambda: False)
    monkeypatch.setattr(detection_mod, "voyage_key_available", lambda: False)
    monkeypatch.setattr(
        substrate_mod,
        "_default_target_counts",
        lambda: {"knowledge__old__bge-base-en-v15-768__v1": 12},
    )
    progress = census_mod._default_progress(_colliding_pair())
    assert progress.converged == frozenset()


def test_census_marks_a_credential_gated_collection_as_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-mq42b: the row must distinguish debt the upgrade converges from
    debt it CANNOT — telling the owner of a keyless voyage collection to run
    `nx upgrade` is a dead end the planner will silently skip."""
    monkeypatch.setattr(census_mod, "_chroma_footprint_present", lambda: True)
    monkeypatch.setattr(
        guided_upgrade,
        "detect_pending_migration",
        lambda: _detection(
            _classification("knowledge__v", legacy=True),
            _classification("knowledge__ok", legacy=True),
        ),
    )
    result = legacy_id_census(
        progress_fn=lambda _: SourceProgress(
            credential_gated=frozenset({"knowledge__v"})
        )
    )
    blocked = {c.collection: c.blocked_reason for c in result}
    assert "Voyage key" in blocked["knowledge__v"]
    assert blocked["knowledge__ok"] == ""  # the ordinary case stays unadorned


def test_doctor_names_the_real_remedy_for_a_blocked_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one case this row MUST speak up about: a collection `nx upgrade`
    will skip needs the key named, or its owner is told nothing actionable
    while the ladder simultaneously reports converged (nexus-mq42b)."""
    monkeypatch.setattr(
        census_mod,
        "legacy_id_census",
        lambda: [
            LegacyCollection(
                "knowledge__v", "local", 12, "legacy ids",
                blocked_reason="no Voyage key is configured",
            )
        ],
    )
    (result,) = _check_legacy_id_census()
    joined = " ".join(result.fix_suggestions)
    assert "cannot be converged by the upgrade" in joined
    assert "knowledge__v" in joined
    assert "Voyage key" in joined
    # ...and the row must not contradict itself: "No action needed here" cannot
    # print directly above "N of these cannot be converged by the upgrade"
    # (code review, 2026-07-17).
    assert "No action needed here" not in joined


def test_doctor_says_no_action_needed_only_when_nothing_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-vacuity for the assertion above: the reassurance IS the right message
    when every listed collection is one the upgrade converges on its own."""
    monkeypatch.setattr(
        census_mod,
        "legacy_id_census",
        lambda: [LegacyCollection("knowledge__ok", "local", 12, "legacy ids")],
    )
    (result,) = _check_legacy_id_census()
    joined = " ".join(result.fix_suggestions)
    assert "No action needed here" in joined
    assert "cannot be converged by the upgrade" not in joined


# ── detect_pending_migration_memoized (P1 validator gap: the memo itself) ────
# Every consumer test patches detect_pending_migration with a fresh object, so
# the identity-keyed memo is a guaranteed miss there — these test the memo.


def _spy_detection(calls: dict[str, int]) -> object:
    def _spy() -> PreflightDetection:
        calls["n"] += 1
        return _detection(_classification("knowledge__old_store", legacy=True))

    return _spy


def test_memoized_detection_probes_once_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(guided_upgrade, "_detection_memo", None)
    monkeypatch.setattr(guided_upgrade, "detect_pending_migration", _spy_detection(calls))
    first = guided_upgrade.detect_pending_migration_memoized()
    second = guided_upgrade.detect_pending_migration_memoized()
    assert calls["n"] == 1  # one underlying probe
    assert second is first  # the cached object, not a re-probe


def test_memoized_detection_reprobes_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(guided_upgrade, "_detection_memo", None)
    monkeypatch.setattr(guided_upgrade, "_DETECTION_MEMO_TTL_S", 0.0)
    monkeypatch.setattr(guided_upgrade, "detect_pending_migration", _spy_detection(calls))
    guided_upgrade.detect_pending_migration_memoized()
    guided_upgrade.detect_pending_migration_memoized()
    assert calls["n"] == 2  # expired entry re-probes


def test_memoized_detection_misses_on_producer_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity keying: a different producer (a test's monkeypatch) can never
    consume a foreign entry — the no-cross-test-leakage property."""
    calls_a, calls_b = {"n": 0}, {"n": 0}
    monkeypatch.setattr(guided_upgrade, "_detection_memo", None)
    monkeypatch.setattr(guided_upgrade, "detect_pending_migration", _spy_detection(calls_a))
    guided_upgrade.detect_pending_migration_memoized()
    monkeypatch.setattr(guided_upgrade, "detect_pending_migration", _spy_detection(calls_b))
    guided_upgrade.detect_pending_migration_memoized()
    assert calls_a["n"] == 1
    assert calls_b["n"] == 1  # fresh producer → fresh probe, not A's result


def test_doctor_census_and_notice_share_one_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE incident pin (P1 critique High-2): one nx doctor run fires both the
    census check and the bridge notice — together they pay the read-leg
    classification exactly ONCE."""
    calls = {"n": 0}
    monkeypatch.setattr(guided_upgrade, "_detection_memo", None)
    monkeypatch.setattr(guided_upgrade, "detect_pending_migration", _spy_detection(calls))
    monkeypatch.setattr(guided_upgrade, "legacy_footprint_pending", lambda: True)
    monkeypatch.setattr(census_mod, "_chroma_footprint_present", lambda: True)
    monkeypatch.setattr(census_mod, "_default_progress", _nothing_converged)

    census_rows = _check_legacy_id_census()
    notice = guided_upgrade.pending_migration_notice()

    assert census_rows and census_rows[0].ok is False  # census saw the debt
    assert notice is not None  # notice fired too
    assert calls["n"] == 1  # ...from ONE shared probe


# ── nx doctor surface (Gap-5 falsifiable) ────────────────────────────────────


def test_no_target_provisioned_reads_the_on_disk_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """The gate's REAL body: reuses the precondition's file-level test rather
    than a second notion of "is there a service"."""
    monkeypatch.setattr(
        config_mod, "default_db_path", lambda: tmp_path / "nexus.db"  # type: ignore[operator]
    )
    monkeypatch.setattr(config_mod, "get_credential", lambda _k: "")
    assert census_mod._no_target_provisioned() is True

    (tmp_path / CREDENTIALS_FILENAME).write_text("{}")  # type: ignore[operator]
    assert census_mod._no_target_provisioned() is False


def test_doctor_census_row_never_directs_the_user_to_a_verb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gap-4 + the credential-gate dead end (both reviewers, 2026-07-16).

    An earlier draft told this row's reader to "Run: nx upgrade". For a
    voyage-named legacy collection with no key that provably no-ops — the
    planner skips it, the ladder row then reports converged, and this row fires
    again forever. The census's reason to exist is keeping visible what CANNOT
    migrate, so a migrate directive is wrong exactly where it matters most; and
    Gap-4 forbids this row becoming a second authority on pending work."""
    monkeypatch.setattr(
        census_mod,
        "legacy_id_census",
        lambda: [LegacyCollection("knowledge__v", "local", 12, "legacy ids")],
    )
    (result,) = _check_legacy_id_census()
    suggestions = " ".join(result.fix_suggestions).lower()
    assert "run: nx upgrade" not in suggestions
    assert result.warn is True  # still visible, still soft


def test_doctor_lists_legacy_collections_as_pending_debt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gap-5: the census appears in doctor, per collection, as a soft warn."""
    monkeypatch.setattr(
        census_mod,
        "legacy_id_census",
        lambda: [
            LegacyCollection("knowledge__old_store", "local", 1234, "legacy ids"),
            LegacyCollection("docs__legacy_two", "local", 7, "legacy ids"),
        ],
    )
    results = _check_legacy_id_census()
    assert len(results) == 1
    result = results[0]
    assert result.ok is False
    assert result.warn is True  # soft warning — never fails doctor
    assert "knowledge__old_store" in result.detail
    assert "1234 chunks" in result.detail
    assert "docs__legacy_two" in result.detail
    assert result.fix_suggestions  # visibility with guidance, not a dead end


def test_doctor_clean_census_does_not_claim_the_source_is_conformant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty census covers TWO worlds: never-migrated-and-conformant, and
    migrated-and-converged. In the second the immutable RDR-176 source still
    holds its legacy ids and always will, so the old "all collections hold
    conformant 32-char chunk ids" was simply false there (nexus-6or3m). The row
    must state what is true in both: nothing is outstanding."""
    monkeypatch.setattr(census_mod, "legacy_id_census", lambda: [])
    results = _check_legacy_id_census()
    assert results[0].ok is True
    assert "no outstanding" in results[0].detail
    assert "conformant" not in results[0].detail


def test_doctor_non_chroma_install_skips_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None (not applicable) yields NO doctor row at all — a service-mode or
    fresh install never sees chunk-id-era noise."""
    monkeypatch.setattr(census_mod, "legacy_id_census", lambda: None)
    assert _check_legacy_id_census() == []


def test_doctor_census_check_is_crash_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> None:
        raise RuntimeError("census exploded")

    monkeypatch.setattr(census_mod, "legacy_id_census", _boom)
    results = _check_legacy_id_census()
    assert results[0].ok is True
    assert "check failed" in results[0].detail


def test_census_check_is_wired_into_run_health_checks() -> None:
    assert "_check_legacy_id_census()" in inspect.getsource(run_health_checks)


def test_census_is_not_its_own_walk_rung() -> None:
    """The census never became a rung of its own: P1 shipped it detect-only
    (a pending rung with no remediation would have failed `nx upgrade` on
    installs that worked fine), and P4.0 folded its signal into the
    substrate-etl rung's detect() — where remediation now lives — rather
    than registering a second census rung."""
    names = [r.name for r in default_registry()]
    assert all(r.name != "legacy-id-census" for r in default_registry())
    # RDR-180 .6 (nexus-jxizy.6) added the chash-rekey rung to the ladder.
    assert names == ["t2-schema", "substrate-etl", "chash-rekey"]
