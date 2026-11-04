# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-90flw: the doctor row for a bundled cluster older than PostgreSQL 17.

The engine exits at boot below server_version_num 170000 (``event=pg_version_below_floor``,
service PgVersionFloor), and an established local cluster keeps whatever PostgreSQL created it, so a
pre-bundle data directory leaves the operator with exit 1 and nothing else. The row reads the data
directory's own ``PG_VERSION`` (no connection), as the auth row reads ``pg_hba.conf``.
"""
from __future__ import annotations

import inspect
import os
import re
from pathlib import Path

import pytest

from nexus.health import PG_VERSION_FLOOR, _check_local_pg_version

_REPO = Path(__file__).resolve().parents[1]
_JAVA_FLOOR = (_REPO / "service" / "src" / "main" / "java" / "dev" / "nexus"
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


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads a 000 file")
def test_a_marker_the_doctor_cannot_open_is_a_failing_row_not_silence(tmp_path: Path) -> None:
    config = _cluster(tmp_path, "16")
    marker = config / "postgres" / "PG_VERSION"
    marker.chmod(0)
    try:
        [row] = _check_local_pg_version(config)
    finally:
        marker.chmod(0o600)
    assert row.ok is False and "could not be read" in row.detail


def test_a_leftover_directory_with_no_cluster_in_use_has_no_row(tmp_path: Path) -> None:
    """A machine with no local cluster in use (no PG_DATA) gets no row, whatever is left under the config dir."""
    config = tmp_path / "config"
    (config / "postgres").mkdir(parents=True)
    (config / "postgres" / "PG_VERSION").write_text("16\n", encoding="utf-8")
    assert _check_local_pg_version(config) == []


def test_the_row_is_registered_in_the_doctor_run() -> None:
    import nexus.health as h

    assert "_check_local_pg_version()" in inspect.getsource(h.run_health_checks)


def test_the_runbook_s_initdb_options_are_the_ones_nx_provisions_with() -> None:
    """The upgrade's new cluster must match what nx makes, or pg_upgrade --check refuses it."""
    runbook = (_REPO / "docs" / "migration-runbook.md").read_text(encoding="utf-8")
    section = runbook.split("## A local cluster older than PostgreSQL 17", 1)[1].split("\n## ", 1)[0]
    provision = (_REPO / "src" / "nexus" / "db" / "pg_provision.py").read_text(encoding="utf-8")
    assert '"--no-locale", "-E", "UTF8",' in provision, "_init_cluster's options moved: update the runbook too"
    assert "--no-locale -E UTF8" in section
    assert "pg-bundle/bundle/bin" in section and 'return extract_root / "bundle" / "bin"' in (
        _REPO / "src" / "nexus" / "db" / "pg_bundle.py").read_text(encoding="utf-8")


def test_the_floor_is_the_engine_s() -> None:
    """The row's floor and the engine's refusal must agree, or the row passes a cluster the engine refuses."""
    text = _JAVA_FLOOR.read_text(encoding="utf-8")
    match = re.search(r"MIN_SERVER_VERSION_NUM\s*=\s*([0-9_]+);", text)
    assert match is not None, "PgVersionFloor.MIN_SERVER_VERSION_NUM moved"
    assert int(match.group(1).replace("_", "")) == PG_VERSION_FLOOR * 10_000
