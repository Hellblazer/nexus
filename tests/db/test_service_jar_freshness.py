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
    # Nothing discoverable: no override, and no extracted bundle (the autouse
    # config-dir isolation already empties the bundle leg). Host PostgreSQL is
    # no longer a leg at all, so there is nothing else left to neutralise — the
    # _CANDIDATE_DIRS and shutil.which patches this test used to need went away
    # with the fallback legs themselves (tests/db/test_no_host_pg_fallback.py).
    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
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


# ── Session-start banner (nexus-zryqm) ──────────────────────────────────────
#
# The per-test guard below is authoritative and unchanged. What it is BAD at is
# a full-suite run: it surfaces as ~73 identical errors thirteen minutes in,
# after which the run is discarded and repeated. That happened three times in
# one day (2026-07-25), twice after the operator had read a handoff note warning
# about exactly it. The banner delivers the same fact at second 2.


def test_banner_is_silent_when_the_jar_is_current(capsys, monkeypatch) -> None:
    """It must not cry wolf — a false banner every run trains people to ignore
    the real one, which is how the per-test guard's message got tuned out."""
    import tests.conftest as ct

    monkeypatch.setattr(
        "tests.db._service_fixture.jar_freshness_skip_reason", lambda *a, **k: None
    )
    ct._warn_if_service_jar_is_stale()
    assert "SERVICE JAR STALE" not in capsys.readouterr().err


def test_banner_fires_and_names_the_rebuild_command(capsys, monkeypatch) -> None:
    import tests.conftest as ct

    monkeypatch.setattr(
        "tests.db._service_fixture.jar_freshness_skip_reason",
        lambda *a, **k: "service jar is STALE: predates TaxonomyHandler.java",
    )
    ct._warn_if_service_jar_is_stale()
    err = capsys.readouterr().err

    assert "SERVICE JAR STALE" in err
    # The actionable half: a warning that does not carry the fix is one the
    # reader has to go look up, which is what makes it skippable.
    assert "mvn -f service/pom.xml package -DskipTests" in err
    assert "TaxonomyHandler.java" in err, "must pass through the concrete reason"


def test_banner_never_breaks_collection(capsys, monkeypatch) -> None:
    """Advisory only. If the freshness probe itself explodes, the suite must
    still run — a broken advisory must never become a broken test session."""
    import tests.conftest as ct

    def _boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(
        "tests.db._service_fixture.jar_freshness_skip_reason", _boom
    )
    ct._warn_if_service_jar_is_stale()  # must not raise
    assert "SERVICE JAR STALE" not in capsys.readouterr().err
