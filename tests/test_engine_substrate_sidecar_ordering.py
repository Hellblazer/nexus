# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sidecar-write ordering invariant (nexus-ui654 follow-up, rounds 1 & 2).

CENTRAL FINDING round 1 pins: the "(b) startup orphan reap is already done
by nexus-lgdy1" claim (this bead's own first-pass report) was an
OVERCLAIM. Before round 1, ``_write_sidecar()`` was called only at the
very END of ``_boot()`` -- AFTER createdb + role bootstrap + JVM spawn +
up to 60s of TCP-wait. A kill anywhere in that window left a SIDECAR-LESS
cluster, which ``_sweep_legacy_cluster()`` explicitly and permanently
refuses to auto-reap (by design -- see that function's docstring). That
is a live, ongoing generator of exactly the un-reapable debris the bead
was filed about, not the "one-time, shrinking population" the pre-fix
docstring claimed.

CENTRAL FINDING round 2 pins: round 1's fix (write the first sidecar
right after the postmaster comes up) still left a real gap under the
exact contention this bead targets -- the boot semaphore's own wait can
legitimately run up to 300s, and that wait sits BEFORE round 1's write
point. The correct fix moves ``tempfile.mkdtemp()`` itself inside the
semaphore and writes the first (placeholder) sidecar immediately after
``initdb`` succeeds -- the earliest point a sidecar CAN legally exist,
since ``initdb`` refuses a non-empty target directory.

Every test here drives ``_boot()`` itself (not just the sweep helpers)
against entirely FAKE process/subprocess machinery -- no real PG, no real
JVM. ``TestClusterDirectoryNeverExistsWithoutASidecar`` was written to
FAIL against the pre-round-2 ordering first (see the bead/commit history
for the falsifying run), then made to pass by moving ``mkdtemp()`` inside
the semaphore and the sidecar write to right after ``initdb``.

    NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_engine_substrate_sidecar_ordering.py -q
"""
from __future__ import annotations

import contextlib
import json
import subprocess
import tempfile
from pathlib import Path

from tests import _engine_substrate as sub


class TestSidecarWrittenBeforeTcpWait:
    """Regression guard: the sidecar (or at minimum the postmaster-identity
    half of it) must exist BEFORE the up-to-60s ``_wait_tcp`` window, not
    only after the whole boot succeeds."""

    def test_sidecar_exists_with_postmaster_identity_before_wait_tcp(
        self, tmp_path, monkeypatch,
    ) -> None:
        # Redirect the substrate's cluster dir into tmp_path instead of the
        # real system tempdir -- fully hermetic, never touches a real or
        # concurrent session's clusters.
        real_mkdtemp = tempfile.mkdtemp
        created: list[Path] = []

        def _fake_mkdtemp(*, prefix, dir=None):  # noqa: A002 — mirrors tempfile.mkdtemp, which _initdb_cluster now calls with dir= (nexus-rbc7k)
            d = real_mkdtemp(prefix=prefix, dir=str(tmp_path))
            created.append(Path(d))
            return d

        monkeypatch.setattr(sub.tempfile, "mkdtemp", _fake_mkdtemp)
        # Never touch the box's real stray population, and never wait on
        # the real cross-process boot semaphore for this ordering-only test
        # (the semaphore itself has its own dedicated test file).
        monkeypatch.setattr(
            sub, "sweep_stale_substrate_clusters", lambda **_kw: sub.SweepResult(),
        )
        monkeypatch.setattr(sub, "_boot_semaphore_slot", contextlib.nullcontext)
        monkeypatch.setattr(sub, "jar_freshness_skip_reason", lambda *_a, **_kw: None)
        monkeypatch.setattr(sub, "_pg_bin_resolved", tmp_path)  # any dir that exists

        def _fake_run(args, **_kwargs):
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sub.subprocess, "run", _fake_run)

        class _FakePopen:
            def __init__(self, *_args, **_kwargs) -> None:
                self.pid = 424242

        monkeypatch.setattr(sub.subprocess, "Popen", _FakePopen)

        # _read_postmaster_pid reads a real postmaster.pid file our fake
        # pg_ctl never writes -- feed it a deterministic fake identity
        # instead of leaving it to resolve None, so this test can assert
        # on it landing in the early sidecar write.
        monkeypatch.setattr(sub, "_read_postmaster_pid", lambda _pgdata: 313131)

        observed: list[dict | None] = []

        def _fake_wait_tcp(_host, _port, timeout):
            assert created, "cluster dir must exist by the time _wait_tcp is called"
            sidecar_path = created[0] / sub._SIDECAR_FILENAME
            observed.append(
                json.loads(sidecar_path.read_text()) if sidecar_path.exists() else None,
            )

        monkeypatch.setattr(sub, "_wait_tcp", _fake_wait_tcp)

        sub._boot()

        assert observed and observed[0] is not None, (
            "REGRESSION GUARD (nexus-ui654 follow-up, critic Q3): the "
            "sidecar must exist BEFORE _wait_tcp's up-to-60s window -- a "
            "kill during that window used to leave a permanently "
            "un-reapable sidecar-less cluster (_sweep_legacy_cluster "
            "refuses to auto-reap those by design)."
        )
        assert observed[0]["postmaster_pid"] == 313131, (
            "the EARLY sidecar write must already carry the postmaster "
            "identity -- the one piece needed to reap a cluster killed "
            "before the engine ever starts"
        )

    def test_final_sidecar_carries_full_engine_identity_too(
        self, tmp_path, monkeypatch,
    ) -> None:
        """The two-checkpoint write must not regress the FINAL sidecar's
        completeness -- engine_pid/svc_port must still land once known,
        immediately at JVM spawn (not deferred to TCP-ready either)."""
        real_mkdtemp = tempfile.mkdtemp
        created: list[Path] = []

        def _fake_mkdtemp(*, prefix, dir=None):  # noqa: A002 — mirrors tempfile.mkdtemp, which _initdb_cluster now calls with dir= (nexus-rbc7k)
            d = real_mkdtemp(prefix=prefix, dir=str(tmp_path))
            created.append(Path(d))
            return d

        monkeypatch.setattr(sub.tempfile, "mkdtemp", _fake_mkdtemp)
        monkeypatch.setattr(
            sub, "sweep_stale_substrate_clusters", lambda **_kw: sub.SweepResult(),
        )
        monkeypatch.setattr(sub, "_boot_semaphore_slot", contextlib.nullcontext)
        monkeypatch.setattr(sub, "jar_freshness_skip_reason", lambda *_a, **_kw: None)
        monkeypatch.setattr(sub, "_pg_bin_resolved", tmp_path)

        def _fake_run(args, **_kwargs):
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sub.subprocess, "run", _fake_run)

        class _FakePopen:
            def __init__(self, *_args, **_kwargs) -> None:
                self.pid = 424242

        monkeypatch.setattr(sub.subprocess, "Popen", _FakePopen)
        monkeypatch.setattr(sub, "_read_postmaster_pid", lambda _pgdata: 313131)

        sidecar_at_wait_tcp: list[dict] = []

        def _fake_wait_tcp(_host, _port, timeout):
            sidecar_path = created[0] / sub._SIDECAR_FILENAME
            sidecar_at_wait_tcp.append(json.loads(sidecar_path.read_text()))

        monkeypatch.setattr(sub, "_wait_tcp", _fake_wait_tcp)

        sub._boot()

        # engine_pid/svc_port must already be present BY THE TIME
        # _wait_tcp runs -- Popen returns the pid synchronously, there is
        # no reason to defer recording it until TCP readiness.
        assert sidecar_at_wait_tcp, "the wait_tcp fake must have been called"
        assert sidecar_at_wait_tcp[0]["engine_pid"] == 424242, (
            "engine_pid is known synchronously at Popen() -- it must be "
            "recorded immediately, not deferred to _wait_tcp succeeding"
        )

        final_sidecar = json.loads((created[0] / sub._SIDECAR_FILENAME).read_text())
        assert final_sidecar["postmaster_pid"] == 313131
        assert final_sidecar["engine_pid"] == 424242
        assert final_sidecar["svc_port"] is not None


class TestClusterDirectoryNeverExistsWithoutASidecar:
    """Round 2 (critic Q1/Q3 on the round-1 follow-up): round 1 moved the
    first sidecar write to right after the postmaster comes up and claimed
    this shrank the un-reapable window to "milliseconds". That claim was
    WRONG under the exact contention this bead targets: the boot semaphore
    sits BEFORE that write in program order, and under real N-session x
    M-worker contention its own wait can legitimately run up to
    ``_BOOT_SEMAPHORE_ACQUIRE_TIMEOUT_S`` (300s) -- so the true pre-fix
    window was bounded by the semaphore's wait, not milliseconds.

    A first attempted round-2 fix -- write a placeholder sidecar
    immediately after ``mkdtemp()``, before even acquiring the semaphore
    -- was ALSO wrong, for a different reason: ``initdb`` refuses to
    initialize a non-empty target directory, so a sidecar file landing
    inside pgdata before ``initdb`` runs breaks the boot outright. The
    actual fix moves ``mkdtemp()`` itself INSIDE the semaphore, and writes
    the first sidecar immediately after ``initdb`` succeeds (before
    ``pg_ctl start``).

    This test pins BOTH halves of that fix: (1) the cluster directory
    does not exist AT ALL while a boot is merely queued for the
    semaphore -- no debris accumulates during a long wait, because there
    is nothing on disk yet to accumulate; (2) by the time ``pg_ctl
    start`` runs, a placeholder sidecar is already on disk.
    """

    @staticmethod
    def _common_mocks(tmp_path, monkeypatch, created: list[Path]):
        real_mkdtemp = tempfile.mkdtemp

        def _fake_mkdtemp(*, prefix, dir=None):  # noqa: A002 — mirrors tempfile.mkdtemp, which _initdb_cluster now calls with dir= (nexus-rbc7k)
            d = real_mkdtemp(prefix=prefix, dir=str(tmp_path))
            created.append(Path(d))
            return d

        monkeypatch.setattr(sub.tempfile, "mkdtemp", _fake_mkdtemp)
        monkeypatch.setattr(
            sub, "sweep_stale_substrate_clusters", lambda **_kw: sub.SweepResult(),
        )
        monkeypatch.setattr(sub, "jar_freshness_skip_reason", lambda *_a, **_kw: None)
        monkeypatch.setattr(sub, "_pg_bin_resolved", tmp_path)

        class _FakePopen:
            def __init__(self, *_args, **_kwargs) -> None:
                self.pid = 424242

        monkeypatch.setattr(sub.subprocess, "Popen", _FakePopen)
        monkeypatch.setattr(sub, "_read_postmaster_pid", lambda _pgdata: 313131)
        monkeypatch.setattr(sub, "_wait_tcp", lambda *_a, **_kw: None)

    def test_no_directory_exists_while_queued_for_the_semaphore(
        self, tmp_path, monkeypatch,
    ) -> None:
        created: list[Path] = []
        self._common_mocks(tmp_path, monkeypatch, created)

        def _fake_run(args, **_kwargs):
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sub.subprocess, "run", _fake_run)

        observed: dict[str, bool | None] = {"dir_exists_at_acquire": None}

        class _RecordingSemaphore:
            def __call__(self, *_args, **_kwargs):
                return self

            def __enter__(self):
                # This fires BEFORE _boot()'s body inside the `with`
                # block runs -- i.e. before mkdtemp() -- so `created`
                # must still be empty here if the fix is correct.
                observed["dir_exists_at_acquire"] = bool(created)
                return None

            def __exit__(self, *_exc):
                return False

        monkeypatch.setattr(sub, "_boot_semaphore_slot", _RecordingSemaphore())

        sub._boot()

        assert observed["dir_exists_at_acquire"] is False, (
            "REGRESSION GUARD (nexus-ui654 follow-up round 2): the cluster "
            "directory must not exist at all while a boot is merely "
            "queued behind the semaphore -- a directory that exists "
            "during a long queue-wait (up to 300s under real contention) "
            "is exactly the debris shape this bead is about, regardless "
            "of whether it eventually gets a sidecar."
        )

    def test_placeholder_sidecar_exists_before_pg_ctl_start(
        self, tmp_path, monkeypatch,
    ) -> None:
        created: list[Path] = []
        self._common_mocks(tmp_path, monkeypatch, created)
        monkeypatch.setattr(sub, "_boot_semaphore_slot", contextlib.nullcontext)

        observed: dict[str, bool | dict | None] = {
            "sidecar_present_before_pg_ctl_start": None,
            "sidecar_content_before_pg_ctl_start": None,
        }

        def _fake_run(args, **_kwargs):
            is_pg_ctl_start = (
                args and "pg_ctl" in str(args[0]) and "start" in args
            )
            if is_pg_ctl_start:
                assert created, "cluster dir must exist before pg_ctl start"
                sidecar_path = created[0] / sub._SIDECAR_FILENAME
                present = sidecar_path.exists()
                observed["sidecar_present_before_pg_ctl_start"] = present
                if present:
                    observed["sidecar_content_before_pg_ctl_start"] = json.loads(
                        sidecar_path.read_text(),
                    )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sub.subprocess, "run", _fake_run)

        sub._boot()

        assert observed["sidecar_present_before_pg_ctl_start"] is True, (
            "REGRESSION GUARD (nexus-ui654 follow-up round 2, critic "
            "Q1/Q3): a placeholder sidecar must exist by the time "
            "pg_ctl start runs -- initdb succeeding is the earliest point "
            "a sidecar CAN legally be written (pgdata must be empty for "
            "initdb itself), and this proves the write happens at that "
            "earliest legal point, not deferred any further."
        )
        placeholder = observed["sidecar_content_before_pg_ctl_start"]
        assert placeholder is not None
        assert placeholder["postmaster_pid"] is None, (
            "postgres is not up yet at this point -- the placeholder must "
            "honestly record that as None, not guess"
        )
        assert placeholder["engine_pid"] is None
        assert placeholder["owner_pytest_pid"] == sub.os.getpid()
