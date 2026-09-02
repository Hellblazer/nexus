# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-todyv: unit coverage for the stale-service-jar freshness guard.

Exercises jar_freshness_skip_reason() against a synthetic jar + src tree so the
stale-jar detection is verified without building the real 134MB shaded jar.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.db._service_fixture import (
    _SERVICE_JAR,
    build_in_progress_reason,
    jar_freshness_skip_reason,
    pg_bin_dir,
)


def _touch(path: Path, mtime: float) -> None:
    """Create a placeholder file at *path* with the given mtime.

    Jars written through this helper are REAL (minimal) zips carrying a
    manifest, because the freshness gate now verifies completeness as well
    as staleness (nexus-06fu4): a jar mid-write has a current mtime and is
    still unusable, so "exists and is not stale" was never sufficient. A
    one-byte text file stands in for a jar only until something actually
    tries to open it -- which is the whole point of the new check.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".jar":
        import zipfile

        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
    else:
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


class TestMidWriteJarIsNotFresh:
    """A jar being ACTIVELY REWRITTEN must not read as safe to launch (nexus-06fu4).

    Measured 2026-09-02 across two sessions in this shared checkout: a
    four-file pytest run errored at setup on every test with "no main
    manifest attribute, in service/target/nexus-service-1.0-SNAPSHOT.jar"
    while ``jar_freshness_skip_reason`` returned None. The writer was a
    concurrent container native build; ``mvn package`` (native or plain)
    rewrites the same shaded jar in place, so the partial file IS the path
    every reader consults.

    The mtime comparison cannot see this by construction: a jar mid-write
    has an mtime NEWER than every source, which is exactly the
    "safe to launch" branch. Being-written is a third state the check
    never modelled.

    NON-VACUITY (nexus-moht0): these build a REAL partial artifact -- a
    truncated prefix of the genuine jar with a fresh mtime -- rather than
    racing a live build and hoping to catch the window. If the defect
    returns, the first test here fails deterministically.
    """

    @staticmethod
    def _partial_jar(tmp_path, fraction: float = 0.4):
        """A real prefix of the real jar, with an mtime newer than all sources."""
        import os
        import time

        real = _SERVICE_JAR
        if not real.exists():
            pytest.skip("no built service jar to truncate")
        data = real.read_bytes()
        part = tmp_path / "nexus-service-1.0-SNAPSHOT.jar"
        part.write_bytes(data[: max(1, int(len(data) * fraction))])
        future = time.time() + 5
        os.utime(part, (future, future))
        return part

    def test_a_truncated_jar_is_refused(self, tmp_path):
        """The exact measured state: complete-looking mtime, incomplete bytes."""
        part = self._partial_jar(tmp_path)
        reason = jar_freshness_skip_reason(part)
        assert reason is not None, (
            "a mid-write jar read as safe to launch; this is the defect "
            "nexus-06fu4 was filed for"
        )
        assert "incomplete" in reason.lower() or "corrupt" in reason.lower(), reason

    def test_the_reason_names_the_state_not_just_staleness(self, tmp_path):
        """The original failure surfaced as an unattributable JVM error.

        The whole cost of this defect was a confusing red with a
        plausible-but-wrong cause, so the reason has to say WHAT is wrong
        and that a concurrent build is the likely explanation.
        """
        reason = jar_freshness_skip_reason(self._partial_jar(tmp_path))
        assert "build" in reason.lower(), reason

    def test_a_zero_length_jar_is_refused(self, tmp_path):
        """The very start of a write, not just the middle."""
        import os
        import time

        part = tmp_path / "nexus-service-1.0-SNAPSHOT.jar"
        part.write_bytes(b"")
        future = time.time() + 5
        os.utime(part, (future, future))
        assert jar_freshness_skip_reason(part) is not None

    def test_a_complete_jar_still_passes(self, tmp_path):
        """Non-vacuity in the other direction: the check must not refuse a
        good jar, or it would just be a permanent skip."""
        if not _SERVICE_JAR.exists():
            pytest.skip("no built service jar")
        assert jar_freshness_skip_reason(_SERVICE_JAR) is None


class TestBuildInProgressIsRefusedLoudly:
    """A reader must refuse to start while a service build holds the lease.

    RANKED LAST BY BOTH SESSIONS, THEN PROMOTED BY MEASUREMENT (nexus-06fu4).
    The bead and both authors reasoned that a lease check was the weakest
    option -- advisory, and a build can start just after the check. A live
    run on 2026-09-02 falsified that: a concurrent native build rewrote the
    jar across a full suite and produced 4178 passed / 12494 ERRORS, every
    one an unattributable "no main manifest attribute". The build held the
    lease for the entire window, so a boot-time consult would have refused
    the run outright and said why.

    The completeness check cannot cover this case by construction: _boot()
    checks and THEN launches the JVM, so a build starting after the check
    clobbers the jar while the JVM is still reading it. The general defect
    is not "mtime is a bad signal" -- it is that a POINT-IN-TIME CHECK
    cannot describe a resource that changes after the check. A lease is the
    only one of the three signals that describes an INTERVAL.

    Advisory is fine here: a two-minute build is not a millisecond race, so
    a boot-time consult catches essentially the whole population, and it
    fails loud and attributable instead of silently.
    """

    @staticmethod
    def _lease(tmp_path, pid: int, command: str = "mvn -Pnative package"):
        d = tmp_path / "service" / ".build-lease" / "service"
        d.mkdir(parents=True)
        (d / "pid").write_text(f"{pid}\n")
        (d / "ts").write_text("2026-09-02T23:33:55Z\n")
        (d / "label").write_text("someone\n")
        (d / "command").write_text(command + "\n")
        return tmp_path / "service" / ".build-lease"

    def test_a_live_build_lease_refuses_the_run(self, tmp_path):
        import os

        reason = build_in_progress_reason(self._lease(tmp_path, os.getpid()))
        assert reason is not None, "a held build lease must stop a reader starting"
        assert "build" in reason.lower()

    def test_the_reason_names_the_holder_and_the_command(self, tmp_path):
        """The whole cost of this defect was an unattributable failure.

        A reason that does not say WHO is building and WHAT they are running
        leaves the reader in the same position as the JVM error did.
        """
        import os

        reason = build_in_progress_reason(
            self._lease(tmp_path, os.getpid(), command="tests/e2e/migration-rehearsal/run.sh")
        )
        assert "migration-rehearsal" in reason
        assert str(os.getpid()) in reason

    def test_no_lease_is_not_a_blocker(self, tmp_path):
        (tmp_path / "service" / ".build-lease").mkdir(parents=True)
        assert build_in_progress_reason(tmp_path / "service" / ".build-lease") is None

    def test_a_stale_lease_from_a_dead_pid_does_not_block(self, tmp_path):
        """A crashed builder must not wedge every future test run.

        This check REPORTS; it deliberately does not reclaim. Reclaiming is
        the lease library's job and doing it from a reader would race the
        real acquire path.
        """
        # PID 2^22 is above the darwin/linux default pid_max: reliably absent.
        reason = build_in_progress_reason(self._lease(tmp_path, 4194304))
        assert reason is None

    def test_a_malformed_lease_never_raises(self, tmp_path):
        d = tmp_path / "service" / ".build-lease" / "service"
        d.mkdir(parents=True)
        (d / "pid").write_text("not-a-pid\n")
        assert build_in_progress_reason(tmp_path / "service" / ".build-lease") is None

    def test_a_missing_lease_root_never_raises(self, tmp_path):
        assert build_in_progress_reason(tmp_path / "nope") is None
