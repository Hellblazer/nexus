# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-todyv: unit coverage for the stale-service-jar freshness guard.

Exercises jar_freshness_skip_reason() against a synthetic jar + src tree so the
stale-jar detection is verified without building the real 134MB shaded jar.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.db._service_fixture import jar_freshness_skip_reason, pg_bin_dir


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def test_missing_jar_returns_reason(tmp_path: Path) -> None:
    reason = jar_freshness_skip_reason(tmp_path / "absent.jar")
    assert reason is not None
    assert "not built" in reason


def test_fresh_jar_returns_none(tmp_path: Path, monkeypatch) -> None:
    # src files at t=1000, jar at t=2000 -> fresh.
    repo = tmp_path
    src = repo / "service" / "src" / "main" / "java"
    _touch(src / "Main.java", 1000.0)
    jar = repo / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
    _touch(jar, 2000.0)

    monkeypatch.setattr("tests.db._service_fixture._REPO_ROOT", repo)
    monkeypatch.setattr(
        "tests.db._service_fixture._SERVICE_SRC_DIRS",
        (src, repo / "service" / "src" / "main" / "resources"),
    )
    assert jar_freshness_skip_reason(jar) is None


def test_stale_via_changelog_resource_returns_reason(tmp_path: Path, monkeypatch) -> None:
    # The changelog resources dir is also watched: a newer changelog -> stale.
    repo = tmp_path
    java = repo / "service" / "src" / "main" / "java"
    changelog = repo / "service" / "src" / "main" / "resources" / "db" / "changelog"
    jar = repo / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
    _touch(java / "Main.java", 1000.0)
    _touch(jar, 1500.0)
    _touch(changelog / "memory-001.xml", 2000.0)

    monkeypatch.setattr("tests.db._service_fixture._REPO_ROOT", repo)
    monkeypatch.setattr(
        "tests.db._service_fixture._SERVICE_SRC_DIRS", (java, changelog),
    )
    reason = jar_freshness_skip_reason(jar)
    assert reason is not None and "STALE" in reason
    assert "memory-001.xml" in reason


def test_absent_source_tree_returns_reason(tmp_path: Path, monkeypatch) -> None:
    # No source dirs present at all -> freshness unverifiable -> skip, not fresh.
    repo = tmp_path
    jar = repo / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
    _touch(jar, 2000.0)
    monkeypatch.setattr("tests.db._service_fixture._REPO_ROOT", repo)
    monkeypatch.setattr(
        "tests.db._service_fixture._SERVICE_SRC_DIRS",
        (repo / "service" / "src" / "main" / "java",),  # does not exist
    )
    reason = jar_freshness_skip_reason(jar)
    assert reason is not None
    assert "source tree not found" in reason


def test_stale_jar_returns_reason(tmp_path: Path, monkeypatch) -> None:
    # jar at t=1000, a src file at t=2000 -> stale.
    repo = tmp_path
    src = repo / "service" / "src" / "main" / "java"
    jar = repo / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
    _touch(jar, 1000.0)
    _touch(src / "NewHandler.java", 2000.0)

    monkeypatch.setattr("tests.db._service_fixture._REPO_ROOT", repo)
    monkeypatch.setattr(
        "tests.db._service_fixture._SERVICE_SRC_DIRS",
        (src, repo / "service" / "src" / "main" / "resources"),
    )
    reason = jar_freshness_skip_reason(jar)
    assert reason is not None
    assert "STALE" in reason
    assert "NewHandler.java" in reason


# ── pg_bin_dir() (nexus-f4wcg) — the shared PG-discovery contract for the ────
# ── 22 self-provisioning fixture modules; three branches locked in.       ────

_PG_TOOL_NAMES = ("initdb", "pg_ctl", "psql", "createdb")


def _fake_pg_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "pgbin"
    bin_dir.mkdir()
    for name in _PG_TOOL_NAMES:
        (bin_dir / name).write_text("#!/bin/sh\n")
    return bin_dir


def test_pg_bin_dir_honors_nexus_pg_bin_override(tmp_path: Path, monkeypatch) -> None:
    bin_dir = _fake_pg_bin(tmp_path)
    monkeypatch.setenv("NEXUS_PG_BIN", str(bin_dir))
    assert pg_bin_dir() == bin_dir


def test_pg_bin_dir_returns_nonexistent_sentinel_when_nothing_found(
    tmp_path: Path, monkeypatch
) -> None:
    # Nothing discoverable: no override, no candidate dirs, nothing on PATH
    # (the autouse config-dir isolation already empties the bundle leg).
    import nexus.db.pg_provision as pg_provision

    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    monkeypatch.setattr(pg_provision, "_CANDIDATE_DIRS", [])
    monkeypatch.setattr(pg_provision.shutil, "which", lambda _name: None)
    # RDR-155 P4b P0a': discovery-miss now self-provisions the pinned
    # bundle; the sentinel contract applies only when that too is
    # impossible.
    import tests.db._service_fixture as sf
    monkeypatch.setattr(sf, "_self_provision_pg_bundle", lambda: None)
    result = pg_bin_dir()
    # The sentinel's whole contract: every per-module prereq check skips.
    assert not any((result / name).exists() for name in _PG_TOOL_NAMES)


def test_pg_bin_dir_raises_on_set_but_broken_nexus_pg_bin(
    tmp_path: Path, monkeypatch
) -> None:
    # Fail-loud policy: an explicit override pointing at a dir without the
    # binaries is a user error — never mass-skip 22 modules silently.
    from nexus.db.pg_provision import PgBinaryNotFoundError

    monkeypatch.setenv("NEXUS_PG_BIN", str(tmp_path / "nowhere"))
    with pytest.raises(PgBinaryNotFoundError):
        pg_bin_dir()
