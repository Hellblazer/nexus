# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-0rwwv: the substrate-migration bridge pointer.

Two upgrade commands, no bridge: a local-mode user with a pending
Chroma→pgvector cutover ran ``nx upgrade``, saw "migrations complete",
and got zero pointer to ``nx guided-upgrade``. ``pending_migration_notice``
is the shared best-effort probe both ``nx upgrade`` (interactive mode only)
and ``nx doctor`` (default health path) append. Auto-DETECT without
auto-EXECUTE: the notice points, never runs, the cutover.

Lives in ``nexus.migration.guided_upgrade`` deliberately — the whole bridge
dies with the migration module at RDR-155 P4b.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.migration.guided_upgrade import (
    PreflightDetection,
    pending_migration_notice,
)

_MOD = "nexus.migration.guided_upgrade"


class _Cls:
    def __init__(self, has_data: bool = True):
        self.has_data = has_data


class _Report:
    def __init__(self, n_data_bearing: int):
        self.classifications = [_Cls() for _ in range(n_data_bearing)]


def _detection(needs: bool, n: int = 0) -> PreflightDetection:
    return PreflightDetection(report=_Report(n), needs_migration=needs)


@pytest.fixture(autouse=True)
def _enable_notice(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Opt back in past the suite-wide conftest kill switch — every probe
    # here is mocked, so no real store can be touched. The cheap gate's
    # dir-existence check is pinned at an existing tmp dir (NOT the real
    # XDG store); the real-service-evidence seams are pinned ABSENT so the
    # default state here is "pending" (individual tests flip one signal).
    monkeypatch.setenv("NX_MIGRATION_NOTICE", "1")
    monkeypatch.setattr(
        "nexus.migration.detection.resolve_default_local_leg",
        lambda: tmp_path,
    )
    monkeypatch.setattr("nexus.config.get_credential", lambda _k: None)
    monkeypatch.setattr(
        "nexus.db.service_endpoint.discover_lease", lambda: (None, None)
    )


class TestPendingMigrationNotice:
    def test_kill_switch_skips_everything(self, monkeypatch):
        monkeypatch.setenv("NX_MIGRATION_NOTICE", "0")
        with patch(f"{_MOD}.detect_pending_migration") as det:
            assert pending_migration_notice() is None
        det.assert_not_called()

    def test_real_service_evidence_returns_none_without_probing(self, monkeypatch):
        # An already-migrated install (here: configured service_url, the
        # managed/cloud shape) must never see the banner — and must not pay
        # the chroma-open cost of the probe.
        monkeypatch.setattr(
            "nexus.config.get_credential",
            lambda k: "https://api.example.com" if k == "service_url" else None,
        )
        with patch(f"{_MOD}.detect_pending_migration") as det:
            assert pending_migration_notice() is None
        det.assert_not_called()

    def test_local_mode_pending_returns_pointer(self):
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_detection(True, 3)):
            notice = pending_migration_notice()
        assert notice is not None
        assert "nx guided-upgrade" in notice
        assert "3" in notice  # data-bearing collection count
        assert "cost preview" in notice  # consent stays explicit

    def test_local_mode_no_footprint_returns_none(self):
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_detection(False)):
            assert pending_migration_notice() is None

    def test_probe_failure_is_silent(self):
        # Best-effort: a broken chroma store must not break nx upgrade/doctor.
        with patch(f"{_MOD}.detect_pending_migration",
                   side_effect=RuntimeError("chroma store corrupt")):
            assert pending_migration_notice() is None


class TestLegacyFootprintGate:
    """The CHEAP gate (env + legacy-dir existence + ABSENCE of real-service
    evidence — never opens the store) behind the hook line and the
    endpoint-failure hints. Critique CRITICAL (2026-07-08): the gate must
    NOT consult storage_backend_for — SERVICE is the hard ROUTING default
    for everyone, including the un-migrated 5.x upgrader this exists for."""

    def test_vanilla_upgrader_default_env_is_pending(self):
        # THE mainline case both reviewers demanded a test for: legacy dir
        # present, NOTHING configured (no service_url, no pg_credentials,
        # no lease), no env overrides — the gate must open under the real
        # unpatched storage-mode default.
        from nexus.migration.guided_upgrade import legacy_footprint_pending
        assert legacy_footprint_pending() is True

    def test_configured_service_url_gates_out(self, monkeypatch):
        from nexus.migration.guided_upgrade import legacy_footprint_pending
        monkeypatch.setattr(
            "nexus.config.get_credential",
            lambda k: "https://api.example.com" if k == "service_url" else None,
        )
        assert legacy_footprint_pending() is False

    def test_pg_credentials_artifact_gates_out(self, monkeypatch, tmp_path):
        from nexus.db.pg_provision import CREDENTIALS_FILENAME
        from nexus.migration.guided_upgrade import legacy_footprint_pending
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / CREDENTIALS_FILENAME).write_text("NX_DB_USER=nexus\n")
        monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: cfg)
        assert legacy_footprint_pending() is False

    def test_explicit_host_port_env_gates_out(self, monkeypatch):
        # cre review (Low): the real resolvers accept NX_SERVICE_HOST/PORT
        # as a configured endpoint — the gate must recognize that tier too.
        from nexus.migration.guided_upgrade import legacy_footprint_pending
        monkeypatch.setenv("NX_SERVICE_PORT", "9099")
        assert legacy_footprint_pending() is False

    def test_live_lease_gates_out(self, monkeypatch):
        from nexus.migration.guided_upgrade import legacy_footprint_pending
        monkeypatch.setattr(
            "nexus.db.service_endpoint.discover_lease",
            lambda: ("http://127.0.0.1:4242", "tok"),
        )
        assert legacy_footprint_pending() is False

    def test_missing_dir_gates_out(self, monkeypatch, tmp_path):
        from nexus.migration.guided_upgrade import legacy_footprint_pending
        monkeypatch.setattr(
            "nexus.migration.detection.resolve_default_local_leg",
            lambda: tmp_path / "does-not-exist",
        )
        assert legacy_footprint_pending() is False

    def test_kill_switch_gates_out(self, monkeypatch):
        from nexus.migration.guided_upgrade import legacy_footprint_pending
        monkeypatch.setenv("NX_MIGRATION_NOTICE", "0")
        assert legacy_footprint_pending() is False

    def test_malformed_backend_env_is_harmless(self, monkeypatch):
        # cre-0rwwv round-1 MEDIUM: a malformed NX_STORAGE_BACKEND must not
        # crash the gate. The gate no longer consults storage_backend_for
        # at all, so the env var is simply irrelevant — pending still wins.
        from nexus.migration.guided_upgrade import legacy_footprint_pending
        monkeypatch.setenv("NX_STORAGE_BACKEND", "bogus")
        assert legacy_footprint_pending() is True
        assert pending_migration_notice.__module__  # import still sound

    def test_gate_failure_is_silent(self, monkeypatch):
        from nexus.migration.guided_upgrade import legacy_footprint_pending
        monkeypatch.setattr(
            "nexus.migration.detection.resolve_default_local_leg",
            lambda: (_ for _ in ()).throw(RuntimeError("resolver broken")),
        )
        assert legacy_footprint_pending() is False


class TestEndpointFailureHint:
    def test_pending_appends_pointer(self):
        # No patches beyond the fixture: the vanilla-upgrader default state
        # (legacy dir, zero service evidence) must produce the hint.
        from nexus.migration.guided_upgrade import endpoint_failure_migration_hint
        assert "nx guided-upgrade" in endpoint_failure_migration_hint()

    def test_not_pending_is_empty(self, monkeypatch):
        from nexus.migration.guided_upgrade import endpoint_failure_migration_hint
        monkeypatch.setattr(
            "nexus.config.get_credential",
            lambda k: "https://api.example.com" if k == "service_url" else None,
        )
        assert endpoint_failure_migration_hint() == ""
