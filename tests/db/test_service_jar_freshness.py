# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-todyv: unit coverage for the stale-service-jar freshness guard.

Exercises jar_freshness_skip_reason() against a synthetic jar + src tree so the
stale-jar detection is verified without building the real 134MB shaded jar.
"""
from __future__ import annotations

import os
from pathlib import Path

from tests.db._service_fixture import jar_freshness_skip_reason


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
