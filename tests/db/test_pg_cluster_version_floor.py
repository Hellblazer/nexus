# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hck2c: a cold start refuses a data directory made by PostgreSQL below the floor, before pg_ctl runs."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

import nexus.daemon.storage_service_daemon as ssd
import nexus.db.pg_provision as pp
from nexus.commands import daemon as daemon_mod
from nexus.daemon import replace_quiesce
from nexus.daemon.storage_service_daemon import StorageServiceStartError, StorageServiceSupervisor
from nexus.db.pg_provision import (
    PgBinaries,
    PgClusterBelowFloorError,
    _start_cluster,
    refuse_cluster_below_floor,
)
from nexus.health import PG_VERSION_FLOOR


def _pgdata(tmp_path: Path, marker: str | None) -> Path:
    pgdata = tmp_path / "postgres"
    pgdata.mkdir()
    if marker is not None:
        (pgdata / "PG_VERSION").write_text(marker + "\n")
    return pgdata


@pytest.fixture
def ran(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every pg_ctl call is recorded; `status` answers with the code the test sets."""
    calls: list[list[str]] = []
    status = {"code": 3}  # 3: not running

    def fake_run_bounded(argv, **kw):
        calls.append(list(argv))
        code = status["code"] if argv[-1] == "status" else 0
        return subprocess.CompletedProcess(argv, code, "", "")

    monkeypatch.setattr(pp, "run_bounded", fake_run_bounded)
    monkeypatch.setattr(pp, "refuse_root", lambda: None)
    monkeypatch.setattr(pp, "_port_accepting", lambda host, port: True)
    return calls, status, PgBinaries.from_dir(tmp_path / "bin", platform="linux")


def test_a_stopped_cluster_below_the_floor_is_refused_before_pg_ctl_start(tmp_path: Path, ran) -> None:
    calls, _, bins = ran
    pgdata = _pgdata(tmp_path, "16")
    with pytest.raises(PgClusterBelowFloorError) as err:
        _start_cluster(bins, pgdata, 5433, platform="linux")
    assert [c[-1] for c in calls] == ["status"]  # pg_ctl start never ran
    text = str(err.value)
    assert "PostgreSQL 16" in text
    assert f"needs {PG_VERSION_FLOOR} or newer" in text
    assert "pg_upgrade" in text
    assert "A local cluster older than PostgreSQL 17" in text
    assert str(pgdata) in text
    assert text.index("A local cluster older than PostgreSQL 17") < text.index(str(pgdata))  # survives a 600-char cut


def test_a_cluster_at_the_floor_starts(tmp_path: Path, ran) -> None:
    calls, _, bins = ran
    _start_cluster(bins, _pgdata(tmp_path, str(PG_VERSION_FLOOR)), 5433, platform="linux")
    assert [c[-2:] for c in calls][-1] == ["start", "-w"]


def test_a_running_cluster_is_left_alone_whatever_its_version(tmp_path: Path, ran) -> None:
    # A server already up is the engine's to refuse (event=pg_version_below_floor), not this start's.
    calls, status, bins = ran
    status["code"] = 0
    _start_cluster(bins, _pgdata(tmp_path, "16"), 5433, platform="linux")
    assert [c[-1] for c in calls] == ["status"]


@pytest.mark.parametrize("marker", [None, "", "garbage", "\u00b2", "9.6"])
def test_an_absent_or_unreadable_marker_is_not_refused_but_an_old_dotted_one_is(tmp_path: Path, marker) -> None:
    pgdata = _pgdata(tmp_path, marker)
    if marker == "9.6":
        with pytest.raises(PgClusterBelowFloorError, match=r"PostgreSQL 9\.6"):
            refuse_cluster_below_floor(pgdata)
    else:
        refuse_cluster_below_floor(pgdata)


def test_provision_refuses_before_binary_discovery_and_leaves_the_old_conf_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The code review's case: no extracted bundle, so discovery would fail first; and the conf rewrite
    # would change an old cluster the runbook then has the user start with its own PostgreSQL.
    config_dir = tmp_path / "cfg"  # no postmaster runs in these tests, so no cluster helper
    pgdata = config_dir / "postgres"
    pgdata.mkdir(parents=True)
    (pgdata / "PG_VERSION").write_text("16\n")
    conf = pgdata / "postgresql.conf"
    conf.write_text("port = 5432\n")

    def no_discovery():
        raise AssertionError("binary discovery ran before the refusal")

    monkeypatch.setattr(pp, "refuse_root", lambda: None)
    monkeypatch.setattr(pp, "refuse_smart_app_control", lambda: None)
    monkeypatch.setattr(pp, "discover_pg_binaries", no_discovery)
    with pytest.raises(PgClusterBelowFloorError, match="PostgreSQL 16"):
        pp._provision_impl(config_dir)  # provision()'s body, without the superuser-auth scope it adds
    assert conf.read_text() == "port = 5432\n"



def test_the_supervisor_refuses_before_discovery_with_the_message_unwrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pgdata = _pgdata(tmp_path, "16")
    monkeypatch.setattr(ssd, "_port_accepting", lambda host, port: False)
    monkeypatch.setattr(pp, "discover_pg_binaries", lambda: (_ for _ in ()).throw(AssertionError("discovery ran first")))
    sup = StorageServiceSupervisor(
        config_dir=tmp_path, pg_port=1, service_port=0,
        creds={"PG_DATA": str(pgdata), "PG_PORT": "1", "NX_SERVICE_TOKEN": "t" * 64},
        binary_path=Path("/nonexistent/fake-nexus-service-binary"), launch_kind="native",
    )
    with pytest.raises(StorageServiceStartError) as err:
        sup._ensure_pg_running()
    assert isinstance(err.value.__cause__, PgClusterBelowFloorError)
    assert "A local cluster older than PostgreSQL 17" in str(err.value)
    assert "Check the Postgres data directory" not in str(err.value)


def test_a_running_old_cluster_takes_the_provision_fast_path_unrefused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The hoisted refusal sits after the fast path: a serving cluster is the engine's to refuse.
    config_dir = tmp_path / "cfg"  # no postmaster runs in these tests, so no cluster helper
    (config_dir / "postgres").mkdir(parents=True)
    (config_dir / "postgres" / "PG_VERSION").write_text("16\n")
    (config_dir / pp.CREDENTIALS_FILENAME).write_text(f"PG_PORT=5433\nNX_SERVICE_TOKEN={'t' * 64}\n")

    def no_binaries():
        raise pp.PgBinaryNotFoundError("none in this test")

    monkeypatch.setattr(pp, "refuse_root", lambda: None)
    monkeypatch.setattr(pp, "refuse_smart_app_control", lambda: None)
    monkeypatch.setattr(pp, "_port_accepting", lambda host, port: True)
    monkeypatch.setattr(pp, "discover_pg_binaries", no_binaries)
    # provision()'s body, without the superuser-auth scope it adds
    assert pp._provision_impl(config_dir).already_provisioned


def test_the_quiesce_restart_refuses_before_its_conf_rewrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pgdata = _pgdata(tmp_path, "16")
    (tmp_path / pp.CREDENTIALS_FILENAME).write_text(f"PG_DATA={pgdata}\nPG_PORT=5433\n")
    rewrites: list[Path] = []
    monkeypatch.setattr(pp, "apply_managed_conf_before_start", lambda d, port: rewrites.append(d))
    with pytest.raises(PgClusterBelowFloorError):
        replace_quiesce.default_ops(tmp_path).start_pg()
    assert rewrites == []


class _BelowFloorSup:
    """The part of the supervisor the start loop touches: start() refuses while PG_VERSION is below the floor."""

    def __init__(self, pgdata: Path, *, upgrade_after_first: bool = False, error: Exception | None = None) -> None:
        self._creds = {"PG_DATA": str(pgdata)}
        self._config_dir = pgdata.parent
        self._pgdata = pgdata
        self._upgrade_after_first = upgrade_after_first
        self._error = error
        self.starts = 0
        self.stops = 0

    def start(self, *, stop_requested=None):
        self.starts += 1
        if self._error is not None:
            raise self._error
        try:
            refuse_cluster_below_floor(self._pgdata)
        except PgClusterBelowFloorError as exc:
            if self._upgrade_after_first:
                (self._pgdata / "PG_VERSION").write_text(f"{PG_VERSION_FLOOR}\n")
            raise StorageServiceStartError(str(exc)) from exc
        return {}

    def stop(self) -> None:
        self.stops += 1


def test_a_below_floor_supervisor_stays_up_and_a_stop_ends_it_with_exit_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # nexus-3qhls: exiting non-zero here bought an OS respawn every 30 s forever.
    monkeypatch.setattr(ssd, "_BELOW_FLOOR_RECHECK_S", 0.01)
    sup = _BelowFloorSup(_pgdata(tmp_path, "16"))
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    assert ssd._supervise_until_stopped(sup, stop, lambda: None) == 0  # type: ignore[arg-type]
    assert sup.starts == 1  # it waited on the marker, not by restarting
    assert sup.stops == 1


def test_a_below_floor_supervisor_starts_once_the_cluster_reaches_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ssd, "_BELOW_FLOOR_RECHECK_S", 0.01)
    reached: list[object] = []
    monkeypatch.setattr(ssd, "exit_if_process_unowned", lambda s, f, **kw: reached.append(s) or True)
    sup = _BelowFloorSup(_pgdata(tmp_path, "16"), upgrade_after_first=True)
    assert ssd._supervise_until_stopped(sup, threading.Event(), lambda: None) == 0  # type: ignore[arg-type]
    assert sup.starts == 2
    assert reached == [sup]  # past start(), into the owned-process check


def test_any_other_start_failure_still_raises_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssd, "_BELOW_FLOOR_RECHECK_S", 0.01)
    stop = threading.Event()
    threading.Timer(2.0, stop.set).start()  # bounds a wrong wait: it would end in exit 0, not the raise
    sup = _BelowFloorSup(_pgdata(tmp_path, "17"), error=StorageServiceStartError("PG_DATA not found"))
    with pytest.raises(StorageServiceStartError, match="PG_DATA not found"):
        ssd._supervise_until_stopped(sup, stop, lambda: None)  # type: ignore[arg-type]
    assert sup.starts == 1


class _CountingStop(threading.Event):
    """A stop event that records every wait's timeout and sets itself on the third wait."""

    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.timeouts.append(timeout)
        if len(self.timeouts) >= 3:
            self.set()
        return self.is_set()


def test_the_below_floor_wait_ticks_so_a_windows_stop_is_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A SIGBREAK handler runs only between waits on the main thread (RDR-224); one long wait hid it for 300 s.
    monkeypatch.setattr(ssd, "_BELOW_FLOOR_RECHECK_S", 30.0)
    sup = _BelowFloorSup(_pgdata(tmp_path, "16"))
    stop = _CountingStop()
    assert ssd._supervise_until_stopped(sup, stop, lambda: None) == 0  # type: ignore[arg-type]
    assert len(stop.timeouts) == 3
    assert all(w is not None and w <= ssd.DEFAULT_HEARTBEAT_INTERVAL for w in stop.timeouts), stop.timeouts


def test_a_reinit_while_waiting_exits_3_for_a_fresh_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The waiting process holds the old port and password; the OS unit's restart reads the new ones.
    monkeypatch.setattr(ssd, "_BELOW_FLOOR_RECHECK_S", 0.01)
    pgdata = _pgdata(tmp_path, "16")
    (tmp_path / pp.CREDENTIALS_FILENAME).write_text("PG_PORT=5433\n")
    sup = _BelowFloorSup(pgdata)

    def reinit() -> None:  # moved aside and re-initialised while the supervisor waits
        (tmp_path / pp.CREDENTIALS_FILENAME).write_text("PG_PORT=6543\n")
        (pgdata / "PG_VERSION").write_text(f"{PG_VERSION_FLOOR}\n")

    threading.Timer(0.2, reinit).start()
    assert ssd._supervise_until_stopped(sup, threading.Event(), lambda: None) == 3  # type: ignore[arg-type]
    assert sup.starts == 1
    assert sup.stops == 1


def test_the_recheck_is_short_enough_for_a_unit_start_to_see_the_engine() -> None:
    assert ssd._BELOW_FLOOR_RECHECK_S <= 10 < daemon_mod._UNIT_START_WAIT_S


class _ReachedStart(Exception):
    pass


def _cli_config(tmp_path: Path, marker: str) -> Path:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    pgdata = _pgdata(tmp_path, marker)
    (config_dir / pp.CREDENTIALS_FILENAME).write_text(f"PG_DATA={pgdata}\nPG_PORT=5433\n")
    return config_dir


@pytest.fixture
def start_paths(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Both ways ensure_storage_supervisor starts a supervisor, recorded and stopped at once."""
    reached: list[str] = []

    def via_unit(*a, **kw):
        reached.append("unit")
        raise _ReachedStart

    def popen(*a, **kw):
        reached.append("spawn")
        raise _ReachedStart

    monkeypatch.setattr(daemon_mod, "_start_via_autostart_unit", via_unit)
    monkeypatch.setattr(daemon_mod, "_popen", popen)
    return reached


def test_the_cli_refuses_a_below_floor_cluster_before_starting_any_supervisor(
    tmp_path: Path, start_paths: list[str],
) -> None:
    # nexus-3qhls: a supervisor that meets the refusal waits for an upgrade, so one spawned here would linger.
    with pytest.raises(StorageServiceStartError) as err:
        daemon_mod.ensure_storage_supervisor(_cli_config(tmp_path, "16"))
    assert isinstance(err.value.__cause__, PgClusterBelowFloorError)
    assert "A local cluster older than PostgreSQL 17" in str(err.value)
    assert start_paths == []


def test_the_cli_leaves_a_running_old_cluster_to_the_engine(
    tmp_path: Path, start_paths: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The supervisor refuses only a stopped cluster (nexus-hck2c); the CLI check follows the same rule.
    monkeypatch.setattr(ssd, "_port_accepting", lambda host, port: True)
    with pytest.raises(_ReachedStart):
        daemon_mod.ensure_storage_supervisor(_cli_config(tmp_path, "16"))
    assert start_paths == ["unit"]


def test_a_refused_start_leaves_the_stop_marker(tmp_path: Path, start_paths: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.daemon.service_registry as sr

    cleared: list[object] = []
    monkeypatch.setattr(sr, "clear_stop_marker", lambda *a, **kw: cleared.append(a))
    with pytest.raises(StorageServiceStartError):
        daemon_mod.ensure_storage_supervisor(_cli_config(tmp_path, "16"))
    assert cleared == []


def test_status_says_why_no_lease_exists(tmp_path: Path) -> None:
    from click.testing import CliRunner

    config_dir = _cli_config(tmp_path, "16")
    result = CliRunner().invoke(daemon_mod.service_status_cmd, ["--config-dir", str(config_dir)])
    assert result.exit_code == 1
    assert "No storage service lease found" in result.output
    assert "A local cluster older than PostgreSQL 17" in result.output
    as_json = CliRunner().invoke(daemon_mod.service_status_cmd, ["--config-dir", str(config_dir), "--json"])
    assert "A local cluster older than PostgreSQL 17" in as_json.output


def test_the_cli_starts_a_cluster_at_the_floor_as_before(tmp_path: Path, start_paths: list[str]) -> None:
    with pytest.raises(_ReachedStart):
        daemon_mod.ensure_storage_supervisor(_cli_config(tmp_path, str(PG_VERSION_FLOOR)))
    assert start_paths == ["unit"]
