# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-161 P3 — native binary lifecycle (renamed from jar_lifecycle).

The JAR install path, fat-JAR provenance extraction, and the schema-skew gate
were removed with the ``java -jar`` launch path. What survives is the read-side
the supervisor + ``service status`` share: well-known binary location, the
installed-binary provenance sidecar, the ``/version`` handshake, and the psql
discovery helpers used by the Postgres probe.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.daemon.binary_install import binary_sidecar_path
from nexus.daemon.binary_lifecycle import (
    _db_name_from_creds,
    _psql_bin,
    fetch_service_version,
    read_installed_provenance,
    well_known_binary_path,
)


class TestWellKnownBinaryPath:
    def test_points_at_service_subdir(self, tmp_path: Path) -> None:
        assert well_known_binary_path(tmp_path) == tmp_path / "service" / "nexus-service"


class TestReadInstalledProvenance:
    def test_none_when_absent(self, tmp_path: Path) -> None:
        assert read_installed_provenance(tmp_path) is None

    def test_reads_binary_sidecar(self, tmp_path: Path) -> None:
        sidecar = binary_sidecar_path(tmp_path)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({"version": "0.1.3", "tag": "engine-service-v0.1.3"}))
        prov = read_installed_provenance(tmp_path)
        assert prov is not None
        assert prov["version"] == "0.1.3"

    def test_malformed_sidecar_returns_none(self, tmp_path: Path) -> None:
        sidecar = binary_sidecar_path(tmp_path)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("{ not json")
        assert read_installed_provenance(tmp_path) is None


class TestFetchServiceVersion:
    def test_unreachable_returns_none(self) -> None:
        # No service is listening on this port; the probe degrades to None.
        assert fetch_service_version("127.0.0.1", 1, timeout=0.2) is None


class TestPsqlHelpers:
    def test_db_name_from_creds_parses_jdbc_url(self) -> None:
        assert _db_name_from_creds(
            {"NX_DB_URL": "jdbc:postgresql://127.0.0.1:15432/nexus"}
        ) == "nexus"

    def test_db_name_defaults_to_nexus_when_absent(self) -> None:
        assert _db_name_from_creds({}) == "nexus"

    def test_psql_bin_returns_str_or_none(self) -> None:
        # Either pg_provision discovery or shutil.which resolves it; both yield
        # a str path or None — never raises.
        result = _psql_bin()
        assert result is None or isinstance(result, str)


class TestStatusVersionHandshake:
    """`nx daemon service status` surfaces /version and warns when the running
    app_version drifts from the installed binary's provenance sidecar."""

    def _invoke_status(self, tmp_path: Path, svc_version: dict | None):
        from unittest.mock import MagicMock

        from click.testing import CliRunner

        from nexus.cli import main

        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        record = MagicMock()
        record.endpoint = {"host": "127.0.0.1", "port": 5999, "pid": 1234}
        record.generation = 1
        record.version = "5.10.6"
        record.heartbeat_epoch = 0.0
        record.status = "live"
        with patch(
            "nexus.daemon.service_registry.ServiceRegistry.discover",
            return_value=record,
        ), patch(
            "nexus.commands.daemon._probe_health", return_value="ok",
        ), patch(
            "nexus.daemon.binary_lifecycle.fetch_service_version",
            return_value=svc_version,
        ):
            return CliRunner().invoke(main, [
                "daemon", "service", "status", "--config-dir", str(config_dir),
            ]), config_dir

    def test_status_shows_running_versions(self, tmp_path: Path) -> None:
        result, _ = self._invoke_status(tmp_path, {
            "app_version": "1.0-SNAPSHOT",
            "schema_latest_id": "vectors-002",
            "schema_changeset_count": 64,
        })
        assert result.exit_code == 0, result.output
        assert "1.0-SNAPSHOT" in result.output
        assert "vectors-002" in result.output
        assert "warning" not in result.output

    def test_status_warns_on_stale_running_binary(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        # Installed binary provenance says 2.0; the running service reports
        # 1.0-SNAPSHOT — a stale running process that needs a restart.
        sidecar = binary_sidecar_path(config_dir)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({"version": "2.0", "tag": "engine-service-v2.0"}))
        result, _ = self._invoke_status(tmp_path, {
            "app_version": "1.0-SNAPSHOT",
            "schema_latest_id": "x",
            "schema_changeset_count": 1,
        })
        assert result.exit_code == 0, result.output
        assert "warning" in result.output
        assert "restart" in result.output

    def test_status_tolerates_version_endpoint_absent(self, tmp_path: Path) -> None:
        result, _ = self._invoke_status(tmp_path, None)
        assert result.exit_code == 0, result.output
        assert "service_app_version" not in result.output
