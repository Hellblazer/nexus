# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hck2c: a cold start refuses a data directory made by PostgreSQL below the floor, before pg_ctl runs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import nexus.db.pg_provision as pp
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
    assert f"PostgreSQL {PG_VERSION_FLOOR} or newer" in text
    assert "pg_upgrade" in text
    assert "A local cluster older than PostgreSQL 17" in text


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


@pytest.mark.parametrize("marker", [None, "", "garbage", "9.6"])
def test_an_absent_or_unreadable_marker_is_not_refused_but_an_old_dotted_one_is(tmp_path: Path, marker) -> None:
    pgdata = _pgdata(tmp_path, marker)
    if marker == "9.6":
        with pytest.raises(PgClusterBelowFloorError, match=r"PostgreSQL 9\.6"):
            refuse_cluster_below_floor(pgdata)
    else:
        refuse_cluster_below_floor(pgdata)
