# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-90flw: the doctor row for a bundled cluster older than PostgreSQL 17.

The engine exits at boot below server_version_num 170000 (``event=pg_version_below_floor``,
service PgVersionFloor), and an established local cluster keeps whatever PostgreSQL created it, so a
pre-bundle data directory leaves the operator with exit 1 and nothing else. The row reads the data
directory's own ``PG_VERSION`` (no connection), as the auth row reads ``pg_hba.conf``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.health import PG_VERSION_FLOOR, _check_local_pg_version

_JAVA_FLOOR = (Path(__file__).resolve().parents[1] / "service" / "src" / "main" / "java" / "dev" / "nexus"
               / "service" / "db" / "PgVersionFloor.java")


def _cluster(tmp_path: Path, version: str | None) -> Path:
    config = tmp_path / "config"
    pgdata = config / "postgres"
    pgdata.mkdir(parents=True)
    if version is not None:
        (pgdata / "PG_VERSION").write_text(version + "\n", encoding="utf-8")
    (config / "pg_credentials").write_text(f"PG_DATA={pgdata}\nPG_PORT=54321\n", encoding="utf-8")
    return config


def test_a_cluster_below_the_floor_fails_naming_the_version_the_floor_and_the_upgrade(tmp_path: Path) -> None:
    [row] = _check_local_pg_version(_cluster(tmp_path, "16"))
    assert row.ok is False
    assert "PostgreSQL 16" in row.detail and "17" in row.detail and "pg_version_below_floor" in row.detail
    assert any("migration-runbook.md" in fix for fix in row.fix_suggestions)


@pytest.mark.parametrize("version", ["17", "18"])
def test_a_cluster_at_or_above_the_floor_passes(tmp_path: Path, version: str) -> None:
    [row] = _check_local_pg_version(_cluster(tmp_path, version))
    assert row.ok is True and f"PostgreSQL {version}" in row.detail


def test_an_old_dotted_version_is_read_by_its_major(tmp_path: Path) -> None:
    [row] = _check_local_pg_version(_cluster(tmp_path, "9.6"))
    assert row.ok is False and "PostgreSQL 9.6" in row.detail


def test_a_marker_that_cannot_be_read_is_not_a_pass(tmp_path: Path) -> None:
    """'Could not look' must not read as 'fine'."""
    [row] = _check_local_pg_version(_cluster(tmp_path, "not-a-version"))
    assert row.ok is False and "could not be read" in row.detail


def test_no_row_without_a_bundled_cluster(tmp_path: Path) -> None:
    """A virgin box (no credentials) and a managed Postgres (no PG_DATA) have nothing to check; neither does a
    data directory initdb has not filled yet."""
    assert _check_local_pg_version(tmp_path / "virgin") == []
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "pg_credentials").write_text("PG_PORT=0\n", encoding="utf-8")
    assert _check_local_pg_version(managed) == []
    assert _check_local_pg_version(_cluster(tmp_path / "empty", None)) == []


@pytest.mark.parametrize(("version", "rows"), [("16", 1), ("17", 0)])
def test_an_old_cluster_left_behind_with_no_local_cluster_in_use_is_a_warning(
    tmp_path: Path, version: str, rows: int,
) -> None:
    """A box that moved to cloud mode keeps its pre-bundle data directory: unused now, but local provisioning keeps
    an existing cluster and the engine would refuse it. A warning, never a failure; nothing for a current one."""
    config = tmp_path / "config"
    (config / "postgres").mkdir(parents=True)
    (config / "postgres" / "PG_VERSION").write_text(version + "\n", encoding="utf-8")
    found = _check_local_pg_version(config)
    assert len(found) == rows
    if rows:
        assert found[0].ok is False and found[0].warn is True and "PostgreSQL 16" in found[0].detail


def test_the_floor_is_the_engine_s() -> None:
    """The row's floor and the engine's refusal must agree, or the row passes a cluster the engine refuses."""
    text = _JAVA_FLOOR.read_text(encoding="utf-8")
    match = re.search(r"MIN_SERVER_VERSION_NUM\s*=\s*([0-9_]+);", text)
    assert match is not None, "PgVersionFloor.MIN_SERVER_VERSION_NUM moved"
    assert int(match.group(1).replace("_", "")) == PG_VERSION_FLOOR * 10_000
