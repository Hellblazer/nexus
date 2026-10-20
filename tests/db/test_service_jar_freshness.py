# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-todyv: unit coverage for the stale-service-jar freshness guard.

Exercises jar_freshness_skip_reason() against a synthetic jar + src tree so the
stale-jar detection is verified without building the real 134MB shaded jar.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus._install.layout_core import exe_name
from tests._platform import posix_only


@pytest.fixture(autouse=True)
def _no_live_build_lease(tmp_path, monkeypatch):
    """Isolate every test here from the BOX's build lease (nexus-pv93h).

    ``jar_freshness_skip_reason`` consults the lease before it looks at the
    jar, and the lease is shared by every worktree of the repo, so while a
    Maven run held it (before nexus-qt6q7, one in any worktree) these tests reported "BUILD IS IN PROGRESS" instead of
    the jar states they exist to pin. Tests that want a lease pass an
    explicit root.
    """
    monkeypatch.setenv("NX_BUILD_LEASE_ROOT", str(tmp_path / "no-lease"))

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
        (bin_dir / exe_name(name)).write_text("#!/bin/sh\n")  # initdb.exe on Windows
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


# The session-start notice (nexus-zryqm) used to be a stderr banner here. It is
# now a hard stop: tests/test_substrate_preflight.py pins that a missing or stale
# jar exits the session with 75 at second 2, which is the same fact delivered
# with the run actually prevented rather than advised against.


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


class TestLeaseScopedToThisCheckout:
    """nexus-qt6q7: the lease is shared by every worktree so builds serialize,
    but a build rewrites only its own checkout's jar. A lease naming another
    worktree is no reason to refuse; one naming this checkout, or naming none
    (an older lib, or mid-populate), still is."""

    @staticmethod
    def _lease(tmp_path, worktree: str | None):
        import os

        root = TestBuildInProgressIsRefusedLoudly._lease(tmp_path, os.getpid())
        if worktree is not None:
            (root / "service" / "worktree").write_text(worktree + "\n")
        return root

    def test_another_worktrees_build_is_not_a_reason(self, tmp_path):
        other = tmp_path / "other-worktree"
        other.mkdir()
        here = tmp_path / "this-worktree"
        here.mkdir()
        assert build_in_progress_reason(self._lease(tmp_path, str(other)), repo_root=here) is None

    def test_this_checkouts_build_is_a_reason(self, tmp_path):
        here = tmp_path / "this-worktree"
        here.mkdir()
        reason = build_in_progress_reason(self._lease(tmp_path, str(here)), repo_root=here)
        assert reason is not None and "BUILD IS IN PROGRESS" in reason

    def test_the_same_checkout_through_a_symlink_is_still_a_reason(self, tmp_path):
        here = tmp_path / "this-worktree"
        here.mkdir()
        (tmp_path / "alias").symlink_to(here)
        assert build_in_progress_reason(self._lease(tmp_path, str(tmp_path / "alias")), repo_root=here) is not None

    def test_a_lease_naming_no_worktree_still_refuses(self, tmp_path):
        here = tmp_path / "this-worktree"
        here.mkdir()
        assert build_in_progress_reason(self._lease(tmp_path, None), repo_root=here) is not None

    def test_a_builder_path_that_cannot_be_read_here_still_refuses(self, tmp_path):
        """Unsure is not foreign: a path this box cannot stat (another mount
        namespace, an MSYS spelling, a symlink loop) may be this checkout."""
        here = tmp_path / "this-worktree"
        here.mkdir()
        (tmp_path / "loop").symlink_to(tmp_path / "loop")
        for builder in (str(tmp_path / "gone"), str(tmp_path / "loop")):
            lease = tmp_path / ("lease-" + builder.rsplit("/", 1)[1])
            lease.mkdir()
            root = self._lease(lease, builder)
            assert build_in_progress_reason(root, repo_root=here) is not None, builder

    def test_a_live_holder_mid_populate_still_refuses(self, tmp_path):
        """The window between the pid claim and the descriptive files: a live
        pid alone, or with an empty worktree file, is still a build."""
        import os

        here = tmp_path / "this-worktree"
        here.mkdir()
        d = tmp_path / "root" / "service"
        d.mkdir(parents=True)
        (d / "pid").write_text(f"{os.getpid()}\n")
        assert build_in_progress_reason(tmp_path / "root", repo_root=here) is not None
        (d / "worktree").write_text("")
        assert build_in_progress_reason(tmp_path / "root", repo_root=here) is not None

    def test_the_session_gate_waits_for_nothing_behind_another_worktrees_build(
        self, tmp_path, monkeypatch,
    ):
        """The callers (conftest's session gate, the boot check) go through
        wait_for_build_lease with the DEFAULT checkout, not an explicit one."""
        from tests.db._service_fixture import wait_for_build_lease

        other = tmp_path / "other-worktree"
        other.mkdir()
        monkeypatch.setenv("NX_BUILD_LEASE_ROOT", str(self._lease(tmp_path, str(other))))
        assert wait_for_build_lease(0, announce=lambda _m: None) is None


@posix_only("the build lease names the ./mvnw build's POSIX process group (pgid; os.getpgid/os.killpg)")
class TestLeaseHolderLiveness:
    """The lease directory in the git common dir never disappears on its own
    (nexus-pv93h): a reader keys on the HOLDER, never on the path. The lease
    lib records the real build's process group as ``pgid`` once the ./mvnw
    child exists; that group outlives a wrapper killed directly."""

    @staticmethod
    def _lease(tmp_path, pid: int, pgid: int | None = None):
        d = tmp_path / "root" / "service"
        d.mkdir(parents=True)
        (d / "pid").write_text(f"{pid}\n")
        (d / "ts").write_text("2026-09-07T20:53:27Z\n")
        (d / "label").write_text("someone\n")
        (d / "command").write_text("scripts/mvnw-leased.sh ./mvnw test\n")
        if pgid is not None:
            (d / "pgid").write_text(f"{pgid}\n")
        return tmp_path / "root"

    def test_a_dead_wrapper_with_a_live_build_group_is_still_a_build(self, tmp_path):
        import os

        # 2^22 is above pid_max on darwin and linux: the wrapper is "dead";
        # this test process's own group is alive.
        root = self._lease(tmp_path, 4194304, pgid=os.getpgid(0))
        assert build_in_progress_reason(root) is not None

    def test_a_dead_build_group_clears_even_with_a_live_wrapper_pid(self, tmp_path):
        import os

        root = self._lease(tmp_path, os.getpid(), pgid=4194304)
        assert build_in_progress_reason(root) is None


class TestWaitForBuildLease:
    """``wait_for_build_lease`` (nexus-pv93h): the pytest-side counterpart of
    ``build_lease_acquire_wait`` -- polls the holder, bounded, announcing at
    the shell lib's cadence, and never sleeps past a cleared lease."""

    def _run(self, reasons: list, max_seconds: int):
        from tests.db._service_fixture import wait_for_build_lease

        seq = iter(reasons)
        sleeps: list[int] = []
        said: list[str] = []
        result = wait_for_build_lease(
            max_seconds, reason_fn=lambda: next(seq), sleep=sleeps.append, announce=said.append,
        )
        return result, sleeps, said

    def test_clear_lease_returns_at_once_without_sleeping_or_announcing(self):
        result, sleeps, said = self._run([None], 3600)
        assert result is None and sleeps == [] and said == []

    def test_zero_bound_is_a_single_look_that_returns_the_reason(self):
        result, sleeps, said = self._run(["held by 1"], 0)
        assert result == "held by 1" and sleeps == [] and said == []

    def test_waits_until_the_holder_is_gone_and_says_so(self):
        result, sleeps, said = self._run(["held", "held", None], 3600)
        assert result is None
        assert sleeps == [5, 5]
        assert said[0].startswith("build lease: held — waiting (0s of 3600s). held")
        assert said[-1] == "build lease: clear after 10s, starting."
        assert len(said) == 2, "one announcement at the first refusal, none per poll"

    def test_gives_up_at_the_bound_with_the_last_reason(self):
        result, sleeps, said = self._run(["a", "b", "c", "d"], 10)
        assert result == "c"
        assert sleeps == [5, 5]

    def test_announces_once_a_minute_while_held(self):
        result, sleeps, said = self._run(["held"] * 30, 120)
        assert result == "held"
        assert len(said) == 2
        assert said[0].startswith("build lease: held — waiting (0s of 120s)")
        assert said[1].startswith("build lease: held — waiting (60s of 120s)")

    def test_env_bound_is_opt_in(self, monkeypatch):
        from tests.db._service_fixture import build_lease_wait_seconds

        monkeypatch.delenv("NX_BUILD_LEASE_WAIT", raising=False)
        assert build_lease_wait_seconds() == 0
        monkeypatch.setenv("NX_BUILD_LEASE_WAIT", "900")
        assert build_lease_wait_seconds() == 900
        monkeypatch.setenv("NX_BUILD_LEASE_WAIT", "soon")
        assert build_lease_wait_seconds() == 0



class TestBootCheckHonoursTheLeaseWait:
    """nexus-wwaqm. A build that takes the lease after the session-start gate is
    first seen at a worker's first boot. The boot path waits through the same
    NX_BUILD_LEASE_WAIT the gate honours; the shared jar_freshness_skip_reason
    never waits, because the session-start stale-jar warning calls it too and
    must not block."""

    def _stub(self, monkeypatch, *, held: bool, after_wait: str | None):
        from tests import _engine_substrate as es

        calls: list[int] = []

        def _wait(max_seconds):
            calls.append(max_seconds)
            return after_wait

        monkeypatch.setattr(es, "build_in_progress_reason",
                            lambda *a, **k: "a service BUILD IS IN PROGRESS (stub)" if held else None)
        monkeypatch.setattr(es, "wait_for_build_lease", _wait)
        monkeypatch.setattr(es, "jar_freshness_skip_reason", lambda jar: after_wait)
        return es, calls

    def test_a_held_lease_is_waited_for_with_the_configured_bound(
        self, tmp_path, monkeypatch,
    ) -> None:
        es, calls = self._stub(monkeypatch, held=True, after_wait=None)
        monkeypatch.setenv("NX_BUILD_LEASE_WAIT", "900")
        assert es._jar_ready_reason(tmp_path / "x.jar") is None
        assert calls == [900]

    def test_a_lease_still_held_after_the_wait_is_reported(self, tmp_path, monkeypatch) -> None:
        es, calls = self._stub(monkeypatch, held=True, after_wait="a service BUILD IS IN PROGRESS (still)")
        monkeypatch.setenv("NX_BUILD_LEASE_WAIT", "900")
        assert es._jar_ready_reason(tmp_path / "x.jar") == "a service BUILD IS IN PROGRESS (still)"
        assert calls == [900]

    def test_without_the_variable_the_boot_takes_a_single_look(self, tmp_path, monkeypatch) -> None:
        es, calls = self._stub(monkeypatch, held=True, after_wait="held")
        monkeypatch.delenv("NX_BUILD_LEASE_WAIT", raising=False)
        es._jar_ready_reason(tmp_path / "x.jar")
        assert calls == [0]

    def test_no_lease_means_no_wait(self, tmp_path, monkeypatch) -> None:
        es, calls = self._stub(monkeypatch, held=False, after_wait=None)
        monkeypatch.setenv("NX_BUILD_LEASE_WAIT", "900")
        es._jar_ready_reason(tmp_path / "x.jar")
        assert calls == []

    def test_the_shared_freshness_check_never_waits(self, tmp_path, monkeypatch) -> None:
        from tests.db import _service_fixture as sf

        def _forbidden(*_a, **_k):
            raise AssertionError("jar_freshness_skip_reason must not wait: its callers include a non-blocking warning")

        monkeypatch.setattr(sf, "build_in_progress_reason", lambda *a, **k: "held")
        monkeypatch.setattr(sf, "wait_for_build_lease", _forbidden)
        monkeypatch.setenv("NX_BUILD_LEASE_WAIT", "900")
        assert sf.jar_freshness_skip_reason(tmp_path / "x.jar") == "held"


def test_the_freshness_dirs_sit_inside_the_gate_jar_cache_key_paths() -> None:
    """nexus-fkhd3: the gate-jar cache keys on pom + src/main + mvnw + .mvn only, so a
    test-only change under service/ is a cache HIT. The mtime freshness check must agree:
    every directory it watches has to be covered by the key (else an edit there marks the jar
    stale while the rebuild HITS and hands back the old jar with a fresh mtime), and none of
    them may be a test tree (else a test-only commit marks the jar stale for nothing)."""
    import re

    from tests.db._service_fixture import _REPO_ROOT, _SERVICE_SRC_DIRS

    text = (_REPO_ROOT / "scripts" / "lib" / "gate-jar-cache.sh").read_text(encoding="utf-8")
    m = re.search(r"^GATE_JAR_KEY_PATHS=\(([^)]*)\)", text, re.MULTILINE)
    assert m, "GATE_JAR_KEY_PATHS is no longer declared in scripts/lib/gate-jar-cache.sh"
    key_paths = m.group(1).split()
    assert key_paths and not any("src/test" in k for k in key_paths), key_paths
    for watched in _SERVICE_SRC_DIRS:
        rel = watched.relative_to(_REPO_ROOT).as_posix()
        assert "src/test" not in rel, f"{rel}: a test tree marks the jar stale"
        assert any(rel == k or rel.startswith(k + "/") for k in key_paths), (
            f"{rel} is watched for jar staleness but is not part of the gate-jar cache key {key_paths}")


# ── nexus-k6zxp: a linked jar shares its mtime across worktrees ───────────────

_GATE_JAR_LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "gate-jar-cache.sh"


def _gate_jar_install(key: str, entry: Path, dest: Path) -> str:
    """Run the REAL ``gate_jar_cache_install`` (what a build-gate-jar.sh hit calls)."""
    import subprocess

    proc = subprocess.run(
        ["bash", "-c", 'source "$1" && gate_jar_cache_install "$2" "$3" "$4" && printf %s "$_GATE_JAR_PLACED"',
         "_", str(_GATE_JAR_LIB), key, str(entry), str(dest)],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout


class TestLinkedJarFreshnessIsPerWorktree:
    """A gate-jar cache hit hard-links one cached jar into every worktree on a key,
    so those worktrees share ONE inode and ONE mtime. Freshness must read the
    worktree's own marker, or another worktree's hit makes an edited worktree's
    stale jar look newer than its sources (a silent stale-jar false pass)."""

    @staticmethod
    def _worktree(tmp_path: Path, monkeypatch, name: str, src_mtime: float) -> tuple[Path, Path]:
        repo = tmp_path / name
        src = repo / "service" / "src" / "main" / "java"
        _touch(src / "Main.java", src_mtime)
        return repo, src

    @staticmethod
    def _watch(monkeypatch, repo: Path, src: Path) -> None:
        monkeypatch.setattr("tests.db._service_fixture._REPO_ROOT", repo)
        monkeypatch.setattr("tests.db._service_fixture._SERVICE_SRC_DIRS", (src,))

    @posix_only("drives the bash gate-jar cache and hard links")
    def test_another_worktrees_hit_cannot_freshen_an_edited_worktree(self, tmp_path: Path, monkeypatch) -> None:
        import time

        # The entry's jar was built at t=1000; both worktrees were checked out
        # later (sources at t=1500), so a bare jar-mtime comparison calls a
        # freshly linked jar STALE.
        entry = tmp_path / "cache" / "k1"
        _touch(entry / "built.jar", 1000.0)  # a real zip (the freshness gate opens it)
        (entry / "built.jar").rename(entry / "jar")
        os.chmod(entry / "jar", 0o444)
        repo_a, src_a = self._worktree(tmp_path, monkeypatch, "A", 1500.0)
        repo_c, src_c = self._worktree(tmp_path, monkeypatch, "C", 1500.0)
        jar_a = repo_a / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
        jar_c = repo_c / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
        for jar in (jar_a, jar_c):
            jar.parent.mkdir(parents=True)
            assert _gate_jar_install("k1", entry, jar) == "link"
        assert os.path.samefile(jar_a, jar_c), "test premise: the two worktrees share one inode"

        # 1. C has just hit: its sources predate its install, so it is FRESH
        #    although the shared jar's own mtime (1000) predates them.
        self._watch(monkeypatch, repo_c, src_c)
        assert jar_freshness_skip_reason(jar_c) is None

        # 2. Time passes; C edits a source after its install.
        marker_c = jar_c.parent / ".gate-jar-key"
        long_ago = time.time() - 100
        os.utime(marker_c, (long_ago, long_ago))
        os.utime(src_c / "Main.java", (long_ago + 50, long_ago + 50))

        # 3. A hits the same entry again now (a repeat build-gate-jar.sh run).
        assert _gate_jar_install("k1", entry, jar_a) == "kept"

        # C's jar is still the one built before its edit: STALE, whatever A did.
        reason = jar_freshness_skip_reason(jar_c)
        assert reason is not None and "STALE" in reason and "Main.java" in reason

        # A, untouched since its own install, stays fresh.
        self._watch(monkeypatch, repo_a, src_a)
        assert jar_freshness_skip_reason(jar_a) is None

    def test_a_jar_rewritten_after_the_marker_is_judged_by_its_own_mtime(self, tmp_path: Path, monkeypatch) -> None:
        """A bare ``mvnw package`` after a hit leaves an old marker beside a NEW jar:
        the jar's own mtime is the reference, so a build of the current sources is fresh."""
        repo, src = self._worktree(tmp_path, monkeypatch, "R", 3000.0)
        jar = repo / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
        _touch(jar, 4000.0)
        marker = jar.parent / ".gate-jar-key"
        marker.write_text("oldkey\n")
        os.utime(marker, (2000.0, 2000.0))
        self._watch(monkeypatch, repo, src)
        assert jar_freshness_skip_reason(jar) is None

    def test_no_marker_means_the_jar_mtime_alone(self, tmp_path: Path, monkeypatch) -> None:
        repo, src = self._worktree(tmp_path, monkeypatch, "P", 3000.0)
        jar = repo / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
        _touch(jar, 2000.0)
        self._watch(monkeypatch, repo, src)
        reason = jar_freshness_skip_reason(jar)
        assert reason is not None and "STALE" in reason
