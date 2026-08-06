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
                svc_version: dict | None = None,
                installed: dict | None = None):
        with patch(
            "nexus.daemon.binary_lifecycle.read_installed_provenance",
            return_value=installed,
        ), patch(
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
            "nexus.daemon.binary_lifecycle.fetch_service_version",
            return_value=svc_version,
        ):
            return CliRunner().invoke(main, [
                "daemon", "service", "status", "--config-dir", str(config_dir),
            ])

    def test_full_stack_surface(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        # minilm-only model list: a voyage token here would trip the
        # RDR-109 mode lint (full-suite collection only) — the list is a
        # display-passthrough fixture, not a mode assertion.
        result = self._invoke(config_dir, svc_version={
            "app_version": "1.0-SNAPSHOT",
            "embedding_mode": "voyage",
            "embedding_models": ["minilm-l6-v2-384"],
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
        assert "minilm-l6-v2-384" in out
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

    # RDR-002: staleness compares the installed binary's tag version against the
    # running service's release_version (NOT app_version, frozen at 1.0-SNAPSHOT).
    def _svc(self, release_version) -> dict:  # noqa: ANN001
        return {
            "app_version": "1.0-SNAPSHOT",
            "release_version": release_version,
            "embedding_mode": "onnx-local",
            "embedding_models": ["bge-base-en-v15-768"],
            "schema_latest_id": "vectors-002",
            "schema_changeset_count": 64,
        }

    def test_frozen_app_version_does_not_false_positive_stale(
        self, tmp_path: Path
    ) -> None:
        # The regression guard: installed 0.1.6 vs app_version 1.0-SNAPSHOT must
        # NOT report stale — the comparison is against release_version (0.1.6).
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        result = self._invoke(
            config_dir,
            svc_version=self._svc("0.1.6"),
            installed={"version": "0.1.6", "installed_by": ""},
        )
        assert result.exit_code == 0, result.output
        assert "stale" not in result.output.lower()
        assert "restart to pick it up" not in result.output

    def test_release_version_mismatch_reports_stale(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        result = self._invoke(
            config_dir,
            svc_version=self._svc("0.1.5"),
            installed={"version": "0.1.6", "installed_by": ""},
        )
        assert result.exit_code == 0, result.output
        assert "release_version=0.1.5" in result.output
        assert "installed binary is 0.1.6" in result.output

    def test_dev_service_null_release_version_is_not_stale(
        self, tmp_path: Path
    ) -> None:
        # A dev/unstamped service reports release_version=null — cannot assert
        # staleness against an installed tag, so no spurious warning.
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        result = self._invoke(
            config_dir,
            svc_version=self._svc(None),
            installed={"version": "0.1.6", "installed_by": ""},
        )
        assert result.exit_code == 0, result.output
        assert "restart to pick it up" not in result.output


class TestStopPgClarity:
    def _invoke_stop(self, config_dir: Path, args: list[str], *,
                     pg_up: bool = True, stop_pid: int | None = 999):
        from nexus.daemon.storage_service_daemon import StopOutcome
        outcome = (
            StopOutcome(pids=(stop_pid,), stubborn=(), source="lease")
            if stop_pid is not None
            else StopOutcome(pids=(), stubborn=(), source="none")
        )
        with patch(
            "nexus.daemon.storage_service_daemon.stop_storage_service",
            return_value=outcome,
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
        assert "nx init --service" in msg
        # The remedy names the two ways this can now happen. It used to hand
        # out a PG_CONFIG= build-pgvector-from-source recipe aimed at a
        # Homebrew versioned major, which stopped being reachable when host-PG
        # discovery was removed (tests/db/test_no_host_pg_fallback.py): the
        # only PostgreSQL nexus resolves is its own bundle or an explicit
        # NEXUS_PG_BIN, and the bundle always carries pgvector.
        assert "NEXUS_PG_BIN" in msg
        assert "PG_CONFIG=" not in msg, (
            "the remedy tells the user to compile pgvector against a host "
            f"PostgreSQL that nexus will never use: {msg}"
        )

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


class TestProbeImplementations:
    """2026-06-11 review pass: the probe IMPLEMENTATIONS (not just their
    wiring) — db-down classification, creds selection, latency guard."""

    def test_probe_health_503_is_db_down(self) -> None:
        from urllib.error import HTTPError

        from nexus.commands.daemon import _probe_health

        err = HTTPError("http://x/health", 503, "Service Unavailable", {}, None)
        with patch("urllib.request.urlopen", side_effect=err):
            assert _probe_health("127.0.0.1", 5999) == "db-down"

    def test_probe_health_connection_refused_is_unreachable(self) -> None:
        from nexus.commands.daemon import _probe_health

        with patch(
            "urllib.request.urlopen", side_effect=ConnectionRefusedError(),
        ):
            assert _probe_health("127.0.0.1", 5999) == "unreachable"

    def test_pgvector_version_prefers_admin_creds(self) -> None:
        from nexus.commands.daemon import _pgvector_version

        creds = {
            "PG_PORT": "5499",
            "NX_DB_USER": "nexus_svc", "NX_DB_PASS": "svc-pw",
            "NX_DB_ADMIN_USER": "nexus_admin", "NX_DB_ADMIN_PASS": "admin-pw",
            "NX_DB_URL": "jdbc:postgresql://127.0.0.1:5499/nexus",
        }
        captured: dict = {}

        def fake_run(cmd, env=None, **kw):
            captured["cmd"] = [str(c) for c in cmd]
            captured["pgpassword"] = env.get("PGPASSWORD")
            return MagicMock(returncode=0, stdout="0.8.2\n")

        with patch("subprocess.run", side_effect=fake_run), \
             patch(
                 "nexus.daemon.binary_lifecycle._psql_bin",
                 return_value="/fake/psql",
             ):
            version = _pgvector_version(creds)
        assert version == "0.8.2"
        assert "nexus_admin" in captured["cmd"]
        assert captured["pgpassword"] == "admin-pw"

    def test_pgvector_version_falls_back_to_svc_creds(self) -> None:
        from nexus.commands.daemon import _pgvector_version

        creds = {
            "PG_PORT": "5499",
            "NX_DB_USER": "nexus_svc", "NX_DB_PASS": "svc-pw",
            "NX_DB_URL": "jdbc:postgresql://127.0.0.1:5499/nexus",
        }
        captured: dict = {}

        def fake_run(cmd, env=None, **kw):
            captured["cmd"] = [str(c) for c in cmd]
            captured["pgpassword"] = env.get("PGPASSWORD")
            return MagicMock(returncode=0, stdout="0.8.2\n")

        with patch("subprocess.run", side_effect=fake_run), \
             patch(
                 "nexus.daemon.binary_lifecycle._psql_bin",
                 return_value="/fake/psql",
             ):
            _pgvector_version(creds)
        assert "nexus_svc" in captured["cmd"]
        assert captured["pgpassword"] == "svc-pw"

    def test_version_probe_skipped_when_health_unreachable(
        self, tmp_path: Path,
    ) -> None:
        """Latency guard (critic S1): /version cannot succeed against the
        same dead host/port — it must not add a second timeout."""
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        fetch = MagicMock(return_value=None)
        with patch(
            "nexus.daemon.service_registry.ServiceRegistry.discover",
            return_value=_lease_record(),
        ), patch(
            "nexus.commands.daemon._probe_health", return_value="unreachable",
        ), patch(
            "nexus.daemon.storage_service_daemon._port_accepting",
            return_value=True,
        ), patch(
            "nexus.commands.daemon._pgvector_version", return_value="0.8.2",
        ), patch(
            "nexus.daemon.binary_lifecycle.fetch_service_version", fetch,
        ):
            result = CliRunner().invoke(main, [
                "daemon", "service", "status", "--config-dir", str(config_dir),
            ])
        assert result.exit_code == 0, result.output
        fetch.assert_not_called()


class TestStopAlreadyStoppedAdvisory:
    def test_already_stopped_with_pg_up_uses_state_phrasing(
        self, tmp_path: Path,
    ) -> None:
        """critic S4: when stop did nothing (no lease), the PG advisory must
        read as a state report, not as an effect of this command."""
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        from nexus.daemon.storage_service_daemon import StopOutcome
        with patch(
            "nexus.daemon.storage_service_daemon.stop_storage_service",
            return_value=StopOutcome(pids=(), stubborn=(), source="none"),
        ), patch(
            "nexus.daemon.storage_service_daemon._port_accepting",
            return_value=True,
        ):
            result = CliRunner().invoke(main, [
                "daemon", "service", "stop", "--config-dir", str(config_dir),
            ])
        assert result.exit_code == 0, result.output
        assert "already stopped" in result.output
        assert "Postgres is still running on 127.0.0.1:5499" in result.output
        assert "left running" not in result.output


class TestStopReviewRound2Clarity:
    """nexus-oyo2g code review round 2 (T2 [21508]) findings 1 and 2."""

    def test_stop_reports_every_signalled_pid_not_just_the_first(
        self, tmp_path: Path,
    ) -> None:
        """Finding 1: a multi-pid StopOutcome (e.g. a lease-named
        supervisor PLUS a tree-swept surviving engine child) must have
        every pid in the message — printing only ``pids[0]`` is a partial
        truth for a command whose whole point here is honest reporting."""
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        from nexus.daemon.storage_service_daemon import StopOutcome
        outcome = StopOutcome(pids=(111, 222), stubborn=(), source="lease")
        with patch(
            "nexus.daemon.storage_service_daemon.stop_storage_service",
            return_value=outcome,
        ), patch(
            "nexus.daemon.storage_service_daemon._port_accepting",
            return_value=False,
        ):
            result = CliRunner().invoke(main, [
                "daemon", "service", "stop", "--config-dir", str(config_dir),
            ])
        assert result.exit_code == 0, result.output
        assert "111" in result.output and "222" in result.output, (
            f"both signalled pids must be visible: {result.output}"
        )

    def test_process_table_source_with_lease_seen_never_says_no_lease(
        self, tmp_path: Path,
    ) -> None:
        """Finding 2: source="process_table" + lease_seen=True (a lease
        WAS found, it just had no usable pid — a malformed/legacy record)
        must not be worded as "No storage service lease found", which
        would misrepresent a present-but-unusable lease as an absent
        one."""
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        from nexus.daemon.storage_service_daemon import StopOutcome
        outcome = StopOutcome(
            pids=(333,), stubborn=(), source="process_table", lease_seen=True,
        )
        with patch(
            "nexus.daemon.storage_service_daemon.stop_storage_service",
            return_value=outcome,
        ), patch(
            "nexus.daemon.storage_service_daemon._port_accepting",
            return_value=False,
        ):
            result = CliRunner().invoke(main, [
                "daemon", "service", "stop", "--config-dir", str(config_dir),
            ])
        assert result.exit_code == 0, result.output
        lowered = result.output.lower()
        assert "lease was found" in lowered, result.output
        assert "no storage service lease was found" not in lowered, result.output
        assert "333" in result.output

    def test_process_table_source_without_lease_seen_says_no_lease(
        self, tmp_path: Path,
    ) -> None:
        """Contrast case: a genuine lease MISS (lease_seen=False) keeps
        the original "no lease found" wording — the fix must not lose the
        original honest case while adding the new one."""
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        from nexus.daemon.storage_service_daemon import StopOutcome
        outcome = StopOutcome(
            pids=(444,), stubborn=(), source="process_table", lease_seen=False,
        )
        with patch(
            "nexus.daemon.storage_service_daemon.stop_storage_service",
            return_value=outcome,
        ), patch(
            "nexus.daemon.storage_service_daemon._port_accepting",
            return_value=False,
        ):
            result = CliRunner().invoke(main, [
                "daemon", "service", "stop", "--config-dir", str(config_dir),
            ])
        assert result.exit_code == 0, result.output
        assert "no storage service lease was found" in result.output.lower()

    def test_none_source_with_lease_seen_says_lease_was_found_but_unusable(
        self, tmp_path: Path,
    ) -> None:
        """Finding 2, the source="none" side: a malformed lease found but
        nothing at all to signal (no process-table matches either) must
        still say a lease was found, not "No storage service lease
        found"."""
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        from nexus.daemon.storage_service_daemon import StopOutcome
        outcome = StopOutcome(
            pids=(), stubborn=(), source="none", lease_seen=True,
        )
        with patch(
            "nexus.daemon.storage_service_daemon.stop_storage_service",
            return_value=outcome,
        ), patch(
            "nexus.daemon.storage_service_daemon._port_accepting",
            return_value=False,
        ):
            result = CliRunner().invoke(main, [
                "daemon", "service", "stop", "--config-dir", str(config_dir),
            ])
        assert result.exit_code == 0, result.output
        lowered = result.output.lower()
        assert "already stopped" in lowered
        assert "lease was found but had no usable" in lowered, result.output
        assert "no storage service lease found and no matching" not in lowered


class TestStopReviewRound3ExitCodes:
    """nexus-oyo2g substantive-critique round 2 (T2 [21510]) findings.

    CRITICAL: exit code is the only machine-parseable signal
    `service_stop_cmd` has (no --json). The bead's own documented remedy,
    `nx daemon service stop && nx daemon service start`, silently
    proceeds to `start` whenever `stop` exits 0 — so an UNVERIFIED stop
    (a stubborn survivor, or a process table that could not be checked
    with nothing signalled) must break that chain with a non-zero exit.
    """

    def test_stubborn_survivor_exits_nonzero(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        from nexus.daemon.storage_service_daemon import StopOutcome
        outcome = StopOutcome(
            pids=(555,), stubborn=(555,), source="lease",
        )
        with patch(
            "nexus.daemon.storage_service_daemon.stop_storage_service",
            return_value=outcome,
        ), patch(
            "nexus.daemon.storage_service_daemon._port_accepting",
            return_value=False,
        ):
            result = CliRunner().invoke(main, [
                "daemon", "service", "stop", "--config-dir", str(config_dir),
            ])
        assert result.exit_code != 0, (
            f"a stubborn survivor must break the stop&&start chain via a "
            f"non-zero exit code: {result.output}"
        )
        assert "555" in result.output
        assert "FAILED" in result.output

    def test_process_table_unavailable_with_nothing_signalled_exits_nonzero(
        self, tmp_path: Path,
    ) -> None:
        """repro-c-adjacent: no lease, and the process table itself could
        not even be checked — 'already stopped' was never actually
        confirmed. This must not exit 0 either."""
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        from nexus.daemon.storage_service_daemon import StopOutcome
        outcome = StopOutcome(
            pids=(), stubborn=(), source="process_table_unavailable",
            lease_seen=False, sweep_verified=False,
        )
        with patch(
            "nexus.daemon.storage_service_daemon.stop_storage_service",
            return_value=outcome,
        ), patch(
            "nexus.daemon.storage_service_daemon._port_accepting",
            return_value=False,
        ):
            result = CliRunner().invoke(main, [
                "daemon", "service", "stop", "--config-dir", str(config_dir),
            ])
        assert result.exit_code != 0, (
            f"an unverifiable stop must not exit 0: {result.output}"
        )
        assert "could not" in result.output.lower()

    def test_clean_stop_still_exits_zero(self, tmp_path: Path) -> None:
        """Regression guard: a genuinely clean, fully-verified stop must
        NOT be caught by the new non-zero-exit branches."""
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        from nexus.daemon.storage_service_daemon import StopOutcome
        outcome = StopOutcome(pids=(999,), stubborn=(), source="lease")
        with patch(
            "nexus.daemon.storage_service_daemon.stop_storage_service",
            return_value=outcome,
        ), patch(
            "nexus.daemon.storage_service_daemon._port_accepting",
            return_value=False,
        ):
            result = CliRunner().invoke(main, [
                "daemon", "service", "stop", "--config-dir", str(config_dir),
            ])
        assert result.exit_code == 0, result.output

    def test_already_stopped_clean_still_exits_zero(
        self, tmp_path: Path,
    ) -> None:
        """A genuine, fully-verified 'nothing to stop' must also stay
        exit 0 — only UNVERIFIED outcomes are new failures."""
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        from nexus.daemon.storage_service_daemon import StopOutcome
        outcome = StopOutcome(pids=(), stubborn=(), source="none")
        with patch(
            "nexus.daemon.storage_service_daemon.stop_storage_service",
            return_value=outcome,
        ), patch(
            "nexus.daemon.storage_service_daemon._port_accepting",
            return_value=False,
        ):
            result = CliRunner().invoke(main, [
                "daemon", "service", "stop", "--config-dir", str(config_dir),
            ])
        assert result.exit_code == 0, result.output

    def test_lease_success_with_unavailable_sweep_stays_exit_zero_but_caveats(
        self, tmp_path: Path,
    ) -> None:
        """Significant finding 2: when the lease branch already signalled
        something but the tree-completion sweep could not run at all
        (source stays "lease"), this is NOT one of the two Critical
        non-zero-exit cases (something WAS verifiably signalled from a
        live lease) — but the message must still admit the sweep never
        ran, per finding 2."""
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        from nexus.daemon.storage_service_daemon import StopOutcome
        outcome = StopOutcome(
            pids=(777,), stubborn=(), source="lease", sweep_verified=False,
        )
        with patch(
            "nexus.daemon.storage_service_daemon.stop_storage_service",
            return_value=outcome,
        ), patch(
            "nexus.daemon.storage_service_daemon._port_accepting",
            return_value=False,
        ):
            result = CliRunner().invoke(main, [
                "daemon", "service", "stop", "--config-dir", str(config_dir),
            ])
        assert result.exit_code == 0, result.output
        assert "777" in result.output
        assert "could not be checked" in result.output, (
            f"the sweep-unavailable caveat must be visible: {result.output}"
        )

    def test_process_table_unavailable_message_has_no_redundant_caveat(
        self, tmp_path: Path,
    ) -> None:
        """The generic sweep_verified caveat must not duplicate the
        dedicated process_table_unavailable message."""
        config_dir = tmp_path / "cfg"
        _write_creds(config_dir)
        from nexus.daemon.storage_service_daemon import StopOutcome
        outcome = StopOutcome(
            pids=(), stubborn=(), source="process_table_unavailable",
            lease_seen=False, sweep_verified=False,
        )
        with patch(
            "nexus.daemon.storage_service_daemon.stop_storage_service",
            return_value=outcome,
        ), patch(
            "nexus.daemon.storage_service_daemon._port_accepting",
            return_value=False,
        ):
            result = CliRunner().invoke(main, [
                "daemon", "service", "stop", "--config-dir", str(config_dir),
            ])
        # The GENERIC sweep_verified caveat text ("could not be ruled out")
        # is distinct from the dedicated process_table_unavailable message
        # and the exit-failure message (both legitimately mention "could
        # not be checked" — that overlap is fine); what must NOT happen is
        # the generic caveat block firing on top of them.
        assert "could not be ruled out" not in result.output.lower(), (
            f"the generic sweep_verified caveat must be suppressed when "
            f"source is already process_table_unavailable: {result.output}"
        )
