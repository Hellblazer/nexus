# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Connection-path regression net for the engine-version floor (nexus-4m6i0.3).

nexus-b6qlf closed a gap CLASS, not just an instance: the probe FUNCTION
(:func:`nexus.db.managed_endpoint.probe_managed_service`) was always correct,
but nothing asserted it sat on the call GRAPH a real cloud-mode client
actually takes. ``tests/db/test_http_vector_client_version_gate.py`` (the
regression suite for the point-fix, nexus-jn0nm) monkeypatches
``probe_managed_service`` wholesale and calls
:func:`nexus.db.http_vector_client.get_http_vector_client` directly — it can
never detect the factory being un-wired from a CLI command, or the probe
being un-wired from the factory. Same anti-pattern in
``tests/commands/test_migrate_cost_guardrail.py`` /
``tests/test_storage_migrate_vectors_cmd.py`` / ``tests/test_health.py`` /
``tests/test_pipeline_version.py`` — all mock at a level that bypasses the
real graph.

This suite drives the REAL graph instead, via :class:`click.testing.CliRunner`
against the actually-registered ``nx`` commands:

    CLI command -> command body -> get_http_vector_client()
      -> probe_managed_service() [REAL] -> httpx.get [the ONLY patched seam]
      -> parse + compare [REAL] -> ManagedServiceIncompatible
      -> _cloud_probe_failure_message() [REAL] -> click.ClickException

Three call sites are exercised end-to-end: ``nx migrate-to-service``
(``migrate_cmd._run_migration``), ``nx storage migrate vectors``
(``storage_cmd.migrate_vectors_cmd``), and a plain ``make_t3()`` consumer
(``nx search``, via ``nexus.commands.store._t3``). A future refactor that
silently un-wires any one of these from the gate fails a test here instead
of shipping the original nexus-b6qlf bug again.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from click.testing import CliRunner

from nexus.commands.migrate_cmd import migrate_to_service_cmd
from nexus.commands.search_cmd import search_cmd
from nexus.commands.storage_cmd import migrate_vectors_cmd
from nexus.db.http_vector_client import reset_http_vector_client_for_tests
from nexus.engine_version import REQUIRED_ENGINE_VERSION
from nexus.migration.detection import DetectionReport
from nexus.migration.driver import GuidedUpgradeResult
from nexus.migration.sequencer import SequenceOutcome
from nexus.migration.vector_etl import MigrationReport

# ── floor-relative version strings (nexus-9qq85 single source of truth) ────
# Derived from REQUIRED_ENGINE_VERSION so a future floor bump does not
# silently invalidate this test (the bead's explicit instruction).


def _floor_str() -> str:
    return ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)


def _below_floor_str() -> str:
    major, minor, patch = REQUIRED_ENGINE_VERSION
    if patch > 0:
        return f"{major}.{minor}.{patch - 1}"
    if minor > 0:
        return f"{major}.{minor - 1}.999"
    return f"{max(major - 1, 0)}.999.999"


def _version_response(release_version: str) -> httpx.Response:
    """A REAL ``httpx.Response`` shaped like the managed ``GET /version`` reply."""
    return httpx.Response(
        status_code=200,
        json={"app_version": "1.0-SNAPSHOT", "release_version": release_version},
        request=httpx.Request("GET", "https://cloud.test.invalid/version"),
    )


@pytest.fixture(autouse=True)
def _reset_probe_and_singleton_cache():
    """The gate caches per-process (pass forever / fail-and-reraise) — reset
    around every test in this file or a leaked cached-fail poisons subsequent
    tests, and a leaked cached-pass makes them vacuous."""
    reset_http_vector_client_for_tests()
    yield
    reset_http_vector_client_for_tests()


@pytest.fixture()
def cloud_below_floor(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Pin cloud mode + patch ONLY the httpx boundary to a below-floor reply."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    calls: list[tuple[str, Any]] = []

    def fake_get(url: str, timeout: Any = None, **_: Any) -> httpx.Response:
        calls.append((url, timeout))
        return _version_response(_below_floor_str())

    monkeypatch.setattr("nexus.db.managed_endpoint.httpx.get", fake_get)
    return calls


@pytest.fixture()
def cloud_at_floor(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Pin cloud mode + patch the httpx boundary to an AT-floor reply (accept case)."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    calls: list[tuple[str, Any]] = []

    def fake_get(url: str, timeout: Any = None, **_: Any) -> httpx.Response:
        calls.append((url, timeout))
        return _version_response(_floor_str())

    monkeypatch.setattr("nexus.db.managed_endpoint.httpx.get", fake_get)
    return calls


def _assert_loud_cloud_failure(result, http_calls: list) -> None:
    """Shared assertions: exit!=0, cloud-framed message, no raw traceback,
    the probe's httpx boundary actually fired.

    The self-contradiction check (nexus-b6qlf Fix 2) applies to the ENTIRE
    captured stream, not just the click-rendered ``Error:`` line
    (nexus-dizod): ``get_http_vector_client`` also structlog-ERRORs the
    probe failure, and at the CLI's default WARNING level that line prints
    to the user's real stderr directly above the click error — a
    contradictory remedy phrase escaping via EITHER channel recreates the
    original incident's confusion for a live cloud user. Pre-fix, that log
    carried the raw (pre-rewrite) exception text ending "...upgrade/
    downgrade the nx client to match"; it now carries the same
    cloud-correct rewritten message the raised exception does.
    """
    assert result.exit_code != 0
    lines = result.output.splitlines()
    error_lines = [ln for ln in lines if ln.strip().lower().startswith("error:")]
    assert error_lines, f"no click 'Error:' line found in output: {result.output!r}"
    click_message = error_lines[-1]
    click_message_lower = click_message.lower()
    assert "cannot be fixed locally" in click_message_lower
    assert _below_floor_str() in click_message
    assert _floor_str() in click_message
    # The underlying ManagedServiceIncompatible's own remedy clause
    # ("Upgrade the managed service, or upgrade/downgrade the nx client to
    # match.") must not reach the user through ANY channel -- neither the
    # click Error: line nor the structlog diagnostic line above it
    # (nexus-b6qlf Fix 2 + nexus-dizod).
    output_lower = result.output.lower()
    assert "upgrade/downgrade" not in output_lower, (
        "contradictory local-remedy phrasing leaked into user-visible "
        f"output (structlog or click channel): {result.output!r}"
    )
    assert "traceback (most recent call last)" not in output_lower
    assert len(http_calls) >= 1
    assert all(url.endswith("/version") for url, _ in http_calls)


class TestMigrateToServiceConnectionPath:
    """``nx migrate-to-service`` -> ``migrate_cmd._run_migration`` ->
    ``get_http_vector_client()``."""

    def _invoke(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("NX_SERVICE_TOKEN", "cli-test-token")
        monkeypatch.setenv("NX_SERVICE_URL", "https://cloud.test.invalid")
        # The cost guardrail's cloud read leg (open_read_legs -> open_cloud_
        # read_client) resolves chroma_database/chroma_api_key via
        # nexus.config.get_credential, which falls back to the REAL
        # ~/.config/nexus/config.yml when unset in env — on a machine with
        # real (and here, incidentally invalid) ChromaCloud credentials
        # configured, this constructs a REAL chromadb.CloudClient and hits
        # real network auth, raising a raw ChromaError instead of the
        # deterministic "leg absent" RuntimeError. Force ONLY the chroma_*
        # credentials empty (delegating everything else to the real
        # get_credential, which checks env first — service_url/service_token
        # resolution elsewhere in the command depends on that) so the cloud
        # leg is deterministically unconfigured, mirroring the local leg's
        # "provably does not exist" isolation below — this suite tests the
        # version-floor gate's position on the call graph, not the cost
        # guardrail's cloud-credential resolution.
        import nexus.config as _nexus_config

        _real_get_credential = _nexus_config.get_credential

        def _fake_get_credential(name: str) -> str:
            if name in ("chroma_database", "chroma_api_key", "chroma_tenant"):
                return ""
            return _real_get_credential(name)

        monkeypatch.setattr(
            "nexus.config.get_credential", _fake_get_credential
        )
        t2_path = tmp_path / "t2.db"
        t2_path.touch()
        catalog_path = tmp_path / "catalog.db"
        catalog_path.touch()
        # A local leg that provably does not exist — deterministic
        # "fresh user, nothing billed" cost-preview regardless of the host
        # machine's real local Chroma state.
        missing_local = tmp_path / "no-such-chroma"
        return CliRunner().invoke(
            migrate_to_service_cmd,
            [
                "--db", str(t2_path),
                "--catalog-db", str(catalog_path),
                "--local-path", str(missing_local),
            ],
        )

    def test_below_floor_fails_loud_and_etl_never_runs(
        self, tmp_path, monkeypatch, cloud_below_floor
    ) -> None:
        run_guided_upgrade = MagicMock()
        monkeypatch.setattr(
            "nexus.migration.driver.run_guided_upgrade", run_guided_upgrade
        )

        result = self._invoke(tmp_path, monkeypatch)

        _assert_loud_cloud_failure(result, cloud_below_floor)
        # The highest-stakes cloud operation (a data migration) must never
        # reach the ETL engine when the gate fails.
        run_guided_upgrade.assert_not_called()

    def test_at_floor_gate_passes_version_check(
        self, tmp_path, monkeypatch, cloud_at_floor
    ) -> None:
        stub_result = GuidedUpgradeResult(
            detection=DetectionReport(classifications=(), voyage_key_present=False),
            sequence=SequenceOutcome(
                ok=True,
                phase="not-migrating",
                collections_total=0,
                collections_done=0,
                t2_total_failed=None,
                legs_attempted=(),
                legs_ok=(),
                blocked_reason=None,
                t2_report=None,
            ),
            validation=None,
            ok=True,
        )
        run_guided_upgrade = MagicMock(return_value=stub_result)
        monkeypatch.setattr(
            "nexus.migration.driver.run_guided_upgrade", run_guided_upgrade
        )

        result = self._invoke(tmp_path, monkeypatch)

        # Not asserting full command success in general (robustness per the
        # bead) — only that the version-gate error specifically is absent
        # and the engine was actually reached (proving the gate let a
        # floor-compatible engine through).
        assert "cannot be fixed locally" not in result.output.lower()
        run_guided_upgrade.assert_called_once()
        assert len(cloud_at_floor) >= 1
        assert result.exit_code == 0
        assert "already on the service stack" in result.output


class TestStorageMigrateVectorsConnectionPath:
    """``nx storage migrate vectors`` -> ``storage_cmd.migrate_vectors_cmd`` ->
    ``get_http_vector_client()``."""

    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_SERVICE_TOKEN", "cli-test-token")
        monkeypatch.setenv("NX_SERVICE_URL", "https://cloud.test.invalid")

    def test_below_floor_fails_loud_and_etl_never_runs(
        self, monkeypatch, cloud_below_floor
    ) -> None:
        self._env(monkeypatch)
        migrate_local = MagicMock()
        migrate_cloud = MagicMock()
        monkeypatch.setattr("nexus.migration.vector_etl.migrate_local", migrate_local)
        monkeypatch.setattr("nexus.migration.vector_etl.migrate_cloud", migrate_cloud)

        result = CliRunner().invoke(migrate_vectors_cmd, [])

        _assert_loud_cloud_failure(result, cloud_below_floor)
        migrate_local.assert_not_called()
        migrate_cloud.assert_not_called()

    def test_at_floor_gate_passes_version_check(
        self, monkeypatch, cloud_at_floor
    ) -> None:
        self._env(monkeypatch)
        migrate_local = MagicMock(return_value=MigrationReport(leg="local", results=()))
        monkeypatch.setattr("nexus.migration.vector_etl.migrate_local", migrate_local)

        result = CliRunner().invoke(migrate_vectors_cmd, [])

        assert "cannot be fixed locally" not in result.output.lower()
        migrate_local.assert_called_once()
        assert len(cloud_at_floor) >= 1
        assert result.exit_code == 0

    def test_dry_run_unaffected_control(
        self, monkeypatch, tmp_path
    ) -> None:
        """br90a carve-out: --dry-run never constructs through the gated
        factory (bare ``HttpVectorClient()``, no probe) and must not regress
        into being gated. Control case: cloud mode pinned, but NO httpx
        patch installed at all — if --dry-run regressed into calling the
        gated factory this test would blow up on a real network call
        instead of silently passing, which is the point of leaving the
        boundary unpatched here."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        migrate_local = MagicMock(return_value=MigrationReport(leg="local", results=()))
        monkeypatch.setattr("nexus.migration.vector_etl.migrate_local", migrate_local)

        result = CliRunner().invoke(
            migrate_vectors_cmd,
            ["--dry-run", "--local-path", str(tmp_path / "chroma")],
        )

        assert result.exit_code == 0, result.output
        migrate_local.assert_called_once()
        _, kwargs = migrate_local.call_args
        assert kwargs["dry_run"] is True


class TestPlainMakeT3ConsumerConnectionPath:
    """``nx search`` -> ``nexus.commands.store._t3()`` -> ``make_t3()`` ->
    ``get_http_vector_client()`` — a plain T3 consumer with no migration
    machinery in front of the gate at all."""

    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_SERVICE_TOKEN", "cli-test-token")
        monkeypatch.setenv("NX_SERVICE_URL", "https://cloud.test.invalid")
        # _t3() pre-gates on these three cloud credentials before ever
        # reaching make_t3() — set dummy values so we exercise the
        # engine-floor gate itself, not that unrelated earlier check.
        monkeypatch.setenv("CHROMA_DATABASE", "test-database")
        monkeypatch.setenv("CHROMA_API_KEY", "test-chroma-key")
        monkeypatch.setenv("VOYAGE_API_KEY", "test-voyage-key")

    def test_below_floor_fails_loud(self, monkeypatch, cloud_below_floor) -> None:
        self._env(monkeypatch)

        result = CliRunner().invoke(search_cmd, ["some query"])

        _assert_loud_cloud_failure(result, cloud_below_floor)

    def test_at_floor_gate_passes_version_check(
        self, monkeypatch, cloud_at_floor
    ) -> None:
        self._env(monkeypatch)

        result = CliRunner().invoke(search_cmd, ["some query"])

        # Not asserting full search success (no real collections exist in
        # this isolated test env) — only that the version-gate error
        # specifically is absent, proving the probe let a floor-compatible
        # engine through and the command proceeded past the gate.
        assert "cannot be fixed locally" not in result.output.lower()
        assert len(cloud_at_floor) >= 1
