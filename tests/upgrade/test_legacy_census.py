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

import nexus.migration.detection as detection_mod
import nexus.migration.guided_upgrade as guided_upgrade
import nexus.upgrade_ladder.census as census_mod
from nexus.health import _check_legacy_id_census, run_health_checks
from nexus.migration.detection import CollectionClassification, DetectionReport
from nexus.migration.guided_upgrade import PreflightDetection
from nexus.upgrade_ladder.census import LegacyCollection, legacy_id_census
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
    result = legacy_id_census()
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
    result = legacy_id_census()
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

    census_rows = _check_legacy_id_census()
    notice = guided_upgrade.pending_migration_notice()

    assert census_rows and census_rows[0].ok is False  # census saw the debt
    assert notice is not None  # notice fired too
    assert calls["n"] == 1  # ...from ONE shared probe


# ── nx doctor surface (Gap-5 falsifiable) ────────────────────────────────────


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


def test_doctor_conformant_chroma_install_reports_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(census_mod, "legacy_id_census", lambda: [])
    results = _check_legacy_id_census()
    assert results[0].ok is True
    assert "conformant" in results[0].detail


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
    assert names == ["t2-schema", "substrate-etl"]
