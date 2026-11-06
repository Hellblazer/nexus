# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hck2c: a cold start refuses a data directory made by PostgreSQL below the floor, before pg_ctl runs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import nexus.daemon.storage_service_daemon as ssd
import nexus.db.pg_provision as pp
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
