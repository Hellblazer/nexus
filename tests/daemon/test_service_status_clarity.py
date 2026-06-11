# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bead nexus-pebfx.5 — one status surface + PG-lifecycle clarity.

The 2026-06-10 diagnosis loop was ps aux + psql + curl /health + reading
the addr file by hand; `nx daemon service status` must answer "is the
stack healthy and how is it configured" alone. `stop` leaves Postgres
running BY DESIGN — the command must say so (and offer --with-pg).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.pg_provision import (
    PgBinaries,
    PgVectorNotInstalledError,
    check_pgvector_available,
)


def _write_creds(config_dir: Path, port: str = "5499") -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    creds = config_dir / "pg_credentials"
    creds.write_text(
        f"PG_PORT={port}\n"
        "PG_DATA=/tmp/pgdata-test\n"
        "NX_DB_USER=nexus_svc\nNX_DB_PASS=pw\n"
        "NX_DB_ADMIN_USER=nexus_admin\nNX_DB_ADMIN_PASS=apw\n"
        "NX_DB_URL=jdbc:postgresql://127.0.0.1:5499/nexus\n"
    )
    return creds


def _lease_record() -> MagicMock:
    record = MagicMock()
    record.endpoint = {"host": "127.0.0.1", "port": 5999, "pid": 1234}
    record.generation = 3
    record.version = "5.10.6"
    record.heartbeat_epoch = 0.0
    record.status = "live"
    record.payload = {"supervisor_pid": 1111}
    return record


class TestStatusSurface:
    def _invoke(self, config_dir: Path, *, pg_up: bool = True,
                svc_version: dict | None = None):
        with patch(
            "nexus.daemon.service_registry.ServiceRegistry.discover",
            return_value=_lease_record(),
        ), patch(
            "nexus.commands.daemon._probe_health", return_value="ok",
        ), patch(
            "nexus.daemon.storage_service_daemon._port_accepting",
            return_value=pg_up,
        ), patch(
            "nexus.commands.daemon._pgvector_version", return_value="0.8.2",
        ), patch(
            "nexus.daemon.jar_lifecycle.fetch_service_version",
            return_value=svc_version,
        ):
            return CliRunner().invoke(main, [
                "daemon", "service", "status", "--config-dir", str(config_dir),
            ])

    def test_full_stack_surface(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        result = self._invoke(config_dir, svc_version={
            "app_version": "1.0-SNAPSHOT",
            "embedding_mode": "voyage",
            "embedding_models": ["minilm-l6-v2-384", "voyage-code-3"],
            "schema_latest_id": "grants-002-changelog-read",
            "schema_changeset_count": 65,
        })
        assert result.exit_code == 0, result.output
        out = result.output
        assert "supervisor_pid: 1111" in out
        assert "health: ok" in out
        assert "pg: up" in out
        assert "pg_port: 5499" in out
        assert "pg_data: /tmp/pgdata-test" in out
        assert "pgvector: 0.8.2" in out
        assert "embedding_mode: voyage" in out
        assert "voyage-code-3" in out
        assert "pg_credentials" in out
        assert "storage_service_addr." in out

    def test_pg_down_is_loud(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        result = self._invoke(config_dir, pg_up=False, svc_version=None)
        assert result.exit_code == 0, result.output
        assert "pg: DOWN" in result.output
        # pgvector query is skipped when PG is down.
        assert "pgvector" not in result.output

    def test_unprovisioned_pg_hints_init(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True)
        result = self._invoke(config_dir, svc_version=None)
        assert result.exit_code == 0, result.output
        assert "nx init --service" in result.output


class TestStopPgClarity:
    def _invoke_stop(self, config_dir: Path, args: list[str], *,
                     pg_up: bool = True, stop_pid: int | None = 999):
        with patch(
            "nexus.daemon.storage_service_daemon.stop_storage_service",
            return_value=stop_pid,
        ), patch(
            "nexus.daemon.storage_service_daemon._port_accepting",
            return_value=pg_up,
        ):
            return CliRunner().invoke(main, [
                "daemon", "service", "stop", "--config-dir", str(config_dir),
                *args,
            ])

    def test_stop_says_pg_left_running(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        result = self._invoke_stop(config_dir, [])
        assert result.exit_code == 0, result.output
        assert "Postgres left running on 127.0.0.1:5499" in result.output
        assert "--with-pg" in result.output

    def test_stop_with_pg_stops_cluster(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        ran: list[list[str]] = []

        def fake_run(cmd, **kw):
            ran.append([str(c) for c in cmd])
            return MagicMock(returncode=0)

        bins = MagicMock()
        bins.pg_ctl = "/fake/pg_ctl"
        with patch("nexus.db.pg_provision.discover_pg_binaries", return_value=bins), \
             patch("subprocess.run", side_effect=fake_run):
            result = self._invoke_stop(config_dir, ["--with-pg"])
        assert result.exit_code == 0, result.output
        assert "Postgres stopped" in result.output
        assert ran and ran[0][:3] == ["/fake/pg_ctl", "-D", "/tmp/pgdata-test"]

    def test_stop_quiet_when_pg_already_down(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        result = self._invoke_stop(config_dir, [], pg_up=False)
        assert result.exit_code == 0, result.output
        assert "left running" not in result.output


class TestPgVectorPreflight:
    def _bins(self, tmp_path: Path) -> PgBinaries:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        return PgBinaries.from_dir(bin_dir)

    def _write_pg_config(self, bins: PgBinaries, sharedir: Path) -> None:
        pg_config = bins.bin_dir / "pg_config"
        pg_config.write_text(f"#!/bin/sh\necho {sharedir}\n")
        pg_config.chmod(0o755)

    def test_missing_control_file_fails_with_remedy(self, tmp_path: Path) -> None:
        bins = self._bins(tmp_path)
        sharedir = tmp_path / "share"
        (sharedir / "extension").mkdir(parents=True)
        self._write_pg_config(bins, sharedir)
        with pytest.raises(PgVectorNotInstalledError) as exc:
            check_pgvector_available(bins)
        msg = str(exc.value)
        assert "vector.control" in msg
        assert "PG_CONFIG=" in msg
        assert "nx init --service" in msg

    def test_present_control_file_passes(self, tmp_path: Path) -> None:
        bins = self._bins(tmp_path)
        sharedir = tmp_path / "share"
        ext = sharedir / "extension"
        ext.mkdir(parents=True)
        (ext / "vector.control").write_text("# pgvector")
        self._write_pg_config(bins, sharedir)
        check_pgvector_available(bins)  # must not raise

    def test_missing_pg_config_is_indeterminate_not_blocking(
        self, tmp_path: Path,
    ) -> None:
        bins = self._bins(tmp_path)  # no pg_config file
        check_pgvector_available(bins)  # must not raise

    def test_provision_gates_before_cluster_work(self, tmp_path: Path) -> None:
        """provision() must invoke the pre-flight right after binary
        discovery — a missing extension never reaches initdb."""
        from nexus.db import pg_provision

        bins = self._bins(tmp_path)
        with patch.object(pg_provision, "discover_pg_binaries", return_value=bins), \
             patch.object(
                 pg_provision, "check_pgvector_available",
                 side_effect=PgVectorNotInstalledError("nope"),
             ), \
             patch.object(pg_provision, "_init_cluster") as init_cluster:
            with pytest.raises(PgVectorNotInstalledError):
                pg_provision.provision(tmp_path / "cfg")
        init_cluster.assert_not_called()
