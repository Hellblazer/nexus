# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-process concurrent-PG-boot semaphore (nexus-ui654).

macOS's SysV shm budget (``kern.sysv.shmmni``) is small and machine-wide;
concurrent pytest sessions each independently booting a T2 engine
substrate (one boot per xdist WORKER process -- see
``tests/_engine_substrate.py``'s ``_WORKER_SHARD_WIDTH`` comment) can spike
past it during the initdb/pg_ctl-start window even though the steady-state
running-cluster count is fine. ``_boot_semaphore_slot`` bounds how many of
those boot sequences can run concurrently across every process on the
machine.

Entirely hermetic: every test here uses a *lock_dir* under ``tmp_path`` and
never touches the real ``_BOOT_SEMAPHORE_DIR`` or boots a real PG/JVM.

    NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_engine_substrate_boot_semaphore.py -q
"""
from __future__ import annotations

import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import pytest
import structlog
from structlog.testing import capture_logs

import tests._engine_substrate as _substrate
from tests import _tmp_root

from tests._engine_substrate import (
    _boot_semaphore_slot,
    _try_acquire_boot_slot,
)
from tests._module_seam import patch_time

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Bounds a HANG in the subprocess probes below, never performance (nexus-61vos).
_NESTED_PYTEST_HANG_TIMEOUT_S = 300


@pytest.fixture
def _enable_info_logging():
    """The suite default is WARNING (tests/conftest.py::pytest_configure) --
    the semaphore's loud-wait/acquired lines are INFO, so capture_logs()
    needs the filter raised to see them at all (matches
    tests/test_silent_error_logging.py's identical pattern)."""
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    yield
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))


class TestTryAcquireBootSlot:
    """Non-blocking, side-effect-free primitive underlying the context
    manager: acquires the first free slot in ascending order, or returns
    None without touching anything if every slot is held."""

    def test_acquires_slot_zero_when_all_free(self, tmp_path) -> None:
        fh = _try_acquire_boot_slot(tmp_path, max_concurrent=3)
        try:
            assert fh is not None
            assert fh.name.endswith("slot-0.lock")
        finally:
            if fh is not None:
                fh.close()

    def test_second_acquirer_gets_the_next_slot_in_order(self, tmp_path) -> None:
        first = _try_acquire_boot_slot(tmp_path, max_concurrent=3)
        try:
            second = _try_acquire_boot_slot(tmp_path, max_concurrent=3)
            try:
                assert first.name.endswith("slot-0.lock")
                assert second.name.endswith("slot-1.lock")
            finally:
                if second is not None:
                    second.close()
        finally:
            if first is not None:
                first.close()

    def test_returns_none_when_every_slot_is_held(self, tmp_path) -> None:
        held = [_try_acquire_boot_slot(tmp_path, max_concurrent=2) for _ in range(2)]
        try:
            assert all(fh is not None for fh in held)
            assert _try_acquire_boot_slot(tmp_path, max_concurrent=2) is None
        finally:
            for fh in held:
                fh.close()

    def test_slot_frees_up_after_close(self, tmp_path) -> None:
        first = _try_acquire_boot_slot(tmp_path, max_concurrent=1)
        assert first is not None
        assert _try_acquire_boot_slot(tmp_path, max_concurrent=1) is None
        first.close()  # closing a flock'd fd releases the lock (kernel behavior)
        second = _try_acquire_boot_slot(tmp_path, max_concurrent=1)
        try:
            assert second is not None
        finally:
            if second is not None:
                second.close()


class TestBoundRespected:
    """The high-value contract: at most max_concurrent holders at any
    instant, proven with real concurrent threads (each thread stands in
    for a separate pytest process's boot attempt)."""

    def test_two_fake_acquirers_bound_of_one_serializes_them(self, tmp_path) -> None:
        order: list[str] = []
        release_first = threading.Event()

        def _holder(label: str, wait_for: threading.Event | None) -> None:
            with _boot_semaphore_slot(
                max_concurrent=1, lock_dir=tmp_path,
                timeout_s=10.0, poll_s=0.05,
            ):
                order.append(f"{label}-enter")
                if wait_for is not None:
                    wait_for.wait(timeout=5)
                time.sleep(0.05)
                order.append(f"{label}-exit")

        t1 = threading.Thread(target=_holder, args=("A", None))
        t1.start()
        time.sleep(0.1)  # let A acquire first
        t2 = threading.Thread(target=_holder, args=("B", None))
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # A must fully exit before B enters -- a bound of 1 means no overlap.
        assert order == ["A-enter", "A-exit", "B-enter", "B-exit"], order

    def test_max_concurrent_holders_never_exceeded(self, tmp_path) -> None:
        max_concurrent = 2
        n_workers = 6
        active = 0
        peak = 0
        lock = threading.Lock()

        def _worker() -> None:
            nonlocal active, peak
            with _boot_semaphore_slot(
                max_concurrent=max_concurrent, lock_dir=tmp_path,
                timeout_s=15.0, poll_s=0.02,
            ):
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.1)
                with lock:
                    active -= 1

        threads = [threading.Thread(target=_worker) for _ in range(n_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert peak <= max_concurrent, (
            f"bound of {max_concurrent} was exceeded: peak concurrent "
            f"holders was {peak}"
        )
        assert peak == max_concurrent, (
            "sanity: with 6 workers and a bound of 2, contention should "
            "actually have been exercised"
        )


class TestLoudWaitLine:
    def test_contended_wait_emits_structlog_line_naming_the_semaphore(
        self, tmp_path, _enable_info_logging,
    ) -> None:
        held = _try_acquire_boot_slot(tmp_path, max_concurrent=1)
        assert held is not None
        try:
            release_at = time.monotonic() + 0.3

            def _release_soon() -> None:
                time.sleep(0.3)
                held.close()

            releaser = threading.Thread(target=_release_soon)
            releaser.start()

            with capture_logs() as cap:
                with _boot_semaphore_slot(
                    max_concurrent=1, lock_dir=tmp_path,
                    timeout_s=10.0, poll_s=0.02,
                    # log_every_s is not a public knob; use the module's
                    # default but keep the hold short via the releaser
                    # thread above, then check the acquired-after-wait line.
                ):
                    pass
            releaser.join(timeout=5)
            assert time.monotonic() >= release_at

            events = {e["event"] for e in cap}
            assert "nexus_t2_substrate.boot_semaphore_acquired" in events, cap
        finally:
            if not held.closed:
                held.close()


class TestTimeoutNamesTheBead:
    def test_timeout_message_names_the_bead(self, tmp_path) -> None:
        held = _try_acquire_boot_slot(tmp_path, max_concurrent=1)
        assert held is not None
        try:
            with pytest.raises(RuntimeError, match="nexus-ui654"):
                with _boot_semaphore_slot(
                    max_concurrent=1, lock_dir=tmp_path,
                    timeout_s=0.2, poll_s=0.05,
                ):
                    pass  # pragma: no cover -- must never be entered
        finally:
            held.close()

    def test_timeout_never_hangs_past_the_deadline(self, tmp_path) -> None:
        held = _try_acquire_boot_slot(tmp_path, max_concurrent=1)
        assert held is not None
        try:
            real_sleep = time.sleep
            sleep_calls: list[float] = []

            def _counting_sleep(seconds: float) -> None:
                sleep_calls.append(seconds)
                real_sleep(seconds)  # unchanged real timing -- only counted

            with patch_time("tests._engine_substrate", "sleep", side_effect=_counting_sleep):
                with pytest.raises(RuntimeError):
                    with _boot_semaphore_slot(
                        max_concurrent=1, lock_dir=tmp_path,
                        timeout_s=0.3, poll_s=0.05,
                    ):
                        pass  # pragma: no cover
            # nexus-scc9t round 2 (critic finding): a wall-clock bound
            # loosened to tolerate scheduler delay (3.0s -> 5.0s) stops
            # catching a deadline check that is an order of magnitude too
            # long -- a 0.3s budget effectively becoming 3.0s still
            # passes under a 5.0s bound. Count poll sleeps instead:
            # poll_s=0.05s against timeout_s=0.3s means a correct
            # deadline check polls ~6 times before raising; a regression
            # 10x too long (an effective 3.0s deadline) polls ~60 times,
            # REGARDLESS of machine speed. Scheduler contention can only
            # ever REDUCE the count (each real sleep(0.05) call may
            # itself take longer under load, so fewer ticks fit before
            # the real deadline trips), never inflate it past what the
            # deadline itself allows -- immune to the exact flake class a
            # wall-clock bound is not, while catching the 10x regression
            # the loosened bound stopped catching. _try_acquire_boot_slot
            # is pure file-locking (no subprocess calls), so there is no
            # other source of time.sleep in this code path to conflate
            # with the semaphore's own poll (unlike terminate_pids'
            # `ps`-subprocess-reap noise elsewhere in this round).
            assert len(sleep_calls) < 20, (
                f"{len(sleep_calls)} poll sleeps for a 0.3s timeout_s / "
                f"0.05s poll_s -- looks like the deadline check is off by "
                f"an order of magnitude, not honoring timeout_s: {sleep_calls}"
            )
        finally:
            held.close()


class TestReleaseOnException:
    def test_slot_released_even_if_the_body_raises(self, tmp_path) -> None:
        class _Boom(Exception):
            pass

        with pytest.raises(_Boom):
            with _boot_semaphore_slot(max_concurrent=1, lock_dir=tmp_path):
                raise _Boom("boot failed mid-window")

        # The slot must be free again -- a leaked lock here would wedge
        # every subsequent boot on the machine.
        fh = _try_acquire_boot_slot(tmp_path, max_concurrent=1)
        try:
            assert fh is not None, "slot was not released after body raised"
        finally:
            if fh is not None:
                fh.close()


class TestBootSemaphoreDirIsPerUser:
    """nexus-c6lsu: the boot-lock directory is shared by every Unix user on a box
    unless the name carries the uid. The first user to boot creates it 0775 and a
    second user then gets PermissionError opening ``slot-0.lock``, so EVERY
    substrate-backed test errors at setup (25,716 errors as user ``nxtest``)."""

    def test_two_uids_get_different_directories_under_one_tempdir(self, tmp_path) -> None:
        from tests._engine_substrate import _boot_semaphore_dir

        assert _boot_semaphore_dir(tmp_path, uid=1001) != _boot_semaphore_dir(tmp_path, uid=1002)

    def test_one_uid_always_gets_the_same_directory(self, tmp_path) -> None:
        from tests._engine_substrate import _boot_semaphore_dir

        assert _boot_semaphore_dir(tmp_path, uid=1001) == _boot_semaphore_dir(tmp_path, uid=1001)

    def test_the_directory_stays_directly_under_the_tempdir_and_names_the_uid(self, tmp_path) -> None:
        from tests._engine_substrate import _boot_semaphore_dir

        d = _boot_semaphore_dir(tmp_path, uid=1001)
        assert d.parent == tmp_path
        assert d.name.startswith("nexus_t2_substrate_boot_locks") and "1001" in d.name

    def test_the_real_default_is_the_running_users_directory(self) -> None:
        from nexus.daemon.service_registry import service_identity
        from tests import _engine_substrate as sub

        # The ORIGINAL tempdir: inside a lab-run grant tests/_tmp_root.py moves TMPDIR
        # to a per-run directory, and a per-run slot directory would split the
        # cross-run cap (nexus-kyujr).
        assert sub._BOOT_SEMAPHORE_DIR == sub._boot_semaphore_dir(
            sub._original_tempdir(), uid=service_identity(),
        )

    def test_a_second_users_directory_is_never_the_first_users(self, tmp_path, monkeypatch) -> None:
        """End to end through the DEFAULT uid resolution (no explicit ``uid=``): as user A
        take a slot, make A's directory unwritable to everyone else (as it is on a real box),
        then become user B and take a slot in the directory B's default resolves to."""
        import os
        import stat

        from tests._engine_substrate import _boot_semaphore_dir

        # The identity the default resolves through (``str(os.getuid())`` on POSIX, the
        # user SID on Windows, where ``os.getuid`` does not exist; nexus-f9bgu.19).
        monkeypatch.setattr("nexus.daemon.service_registry.service_identity", lambda: "1001")
        a = _boot_semaphore_dir(tmp_path)
        with _boot_semaphore_slot(lock_dir=a):
            pass
        os.chmod(a, stat.S_IRUSR | stat.S_IXUSR)  # a peer-owned directory this user cannot write
        try:
            monkeypatch.setattr("nexus.daemon.service_registry.service_identity", lambda: "1002")
            b = _boot_semaphore_dir(tmp_path)
            assert b != a
            with _boot_semaphore_slot(lock_dir=b):
                assert b.is_dir()
        finally:
            os.chmod(a, stat.S_IRWXU)


# The split-shard probe's boot-lock guard (nexus-61vos). These two lived in test_mode_declarations_are_explicit.py and
# moved here unchanged (RDR-258 Phase 1 step 3, nexus-2jomg.2), so retiring that file does not drop them.

def test_the_boot_lock_dir_resolves_under_the_running_process_tmpdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
) -> None:
    """The resolution half of the split-shard probe's boot-lock guard (nexus-61vos).

    The probe looks for ``nexus_t2_substrate_boot_locks-<uid>`` inside the private TMPDIR it
    handed the nested run. That is only a guard if a boot attempt actually puts it
    there, which is a claim about ``_BOOT_SEMAPHORE_DIR`` -- and that constant is
    evaluated at IMPORT time from ``_original_tempdir()`` (``NX_TEST_TMP_ORIG``, else
    ``tempfile.gettempdir()``), so no in-process TMPDIR
    change can move it and no call that passes ``lock_dir`` explicitly can observe it.
    A subprocess is the only vantage point: a fresh exec re-imports the module and
    re-resolves the constant against the environment it was given.

    Deleting the ``tempfile.gettempdir()`` fallback in that resolution -- hardcoding ``/tmp``, say
    -- fails this test and nothing else in the suite, which is the point: without it
    the probe would be checking a location a boot never touches, and would pass whether
    or not the nested run booted an engine.

    Cheap by construction: ``_boot_semaphore_slot`` is mkdir plus flock, with no
    PostgreSQL anywhere in it, so this costs an interpreter start.
    """
    # What a lab-run grant gives this process; the child env must drop it (nexus-g2f0p).
    monkeypatch.setenv(_tmp_root.ORIG_ENV, str(tmp_path))
    private = pathlib.Path(tempfile.mkdtemp(prefix="boot-lock-resolve-"))
    try:
        try:
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import tests._engine_substrate as s\n"
                    "with s._boot_semaphore_slot():\n"
                    "    pass\n"
                    "print(s._BOOT_SEMAPHORE_DIR)\n",
                ],
                cwd=_REPO_ROOT,
                # The same environment the split-shard probe hands its nested run:
                # TMPDIR moved and no inherited NX_TEST_TMP_ORIG (which inside a
                # lab-run grant would win over TMPDIR, nexus-kyujr). The test sets a
                # decoy, so the drop matters outside a grant too (nexus-g2f0p).
                env=_tmp_root.child_env_with_tmpdir(os.environ, private),
                capture_output=True,
                text=True,
                # A hang bound, shared with every other nested subprocess in this file.
                timeout=_NESTED_PYTEST_HANG_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            pytest.skip(
                f"the boot-slot probe did not return within "
                f"{_NESTED_PYTEST_HANG_TIMEOUT_S}s ({exc}). Contention on this box, not "
                f"a statement about where _BOOT_SEMAPHORE_DIR resolves (nexus-61vos)."
            )
        assert probe.returncode == 0, (
            f"the boot-slot probe did not run:\n{probe.stdout}\n{probe.stderr}"
        )
        resolved = pathlib.Path(probe.stdout.strip())
        assert resolved == _substrate._boot_semaphore_dir(private), (
            f"_BOOT_SEMAPHORE_DIR resolved to {resolved}, not under the TMPDIR the "
            f"subprocess was given ({private}). The split-shard probe checks the "
            f"private tree, so a boot attempt would land somewhere it never looks."
        )
        assert resolved.is_dir(), (
            "the boot attempt resolved the right path but created nothing there"
        )
    finally:
        shutil.rmtree(private, ignore_errors=True)


def test_the_boot_lock_guard_sees_a_boot_attempt_under_a_private_tmpdir() -> None:
    """The boot-lock half of the split-shard probe's structural check, tested end to
    end rather than by hand (nexus-61vos, review finding 2).

    The probe asserts that a nested run created no boot-lock directory inside its own
    TMPDIR, which is what catches an engine that booted and was swept before the
    cluster-dir check could see it. That guard was verified by a one-off manual check
    when it was written, on the grounds that forcing a real boot is expensive. It is
    not: ``_boot_semaphore_slot`` is a pure mkdir-plus-flock primitive with no
    PostgreSQL dependency at all, so the whole mechanism can be exercised directly
    under a redirected TMPDIR in milliseconds.

    Scope, stated honestly: this passes ``lock_dir`` EXPLICITLY, so it pins only what
    ``_boot_semaphore_slot`` does with the directory it is handed -- creates it on an
    attempt, and leaves it behind when the slot is released. It says nothing about
    where the default comes from, because a caller that names the directory cannot
    observe the resolution. The resolution is the other half of the probe's guarantee
    and is pinned separately, in the test below.
    """
    private = pathlib.Path(tempfile.mkdtemp(prefix="boot-lock-probe-"))
    try:
        lock_dir = _substrate._boot_semaphore_dir(private)
        assert not lock_dir.exists(), "precondition: the private tree starts clean"

        with _substrate._boot_semaphore_slot(lock_dir=lock_dir):
            assert lock_dir.is_dir(), (
                "a boot attempt did not create the directory the probe checks for, so "
                "that guard could never fire"
            )

        # It persists after the slot is released: per-cluster cleanup never removes it,
        # which is exactly why it survives a swept boot and the cluster dir does not.
        assert lock_dir.is_dir(), "the boot-lock directory must outlive the slot"

        # Deliberately NOT asserted here: that nothing landed in the shared temp dir.
        # With lock_dir passed explicitly that holds however _BOOT_SEMAPHORE_DIR is
        # written, so such an assertion would read as coverage of the resolution while
        # being unable to fail. The test below covers it from the only vantage point
        # that can.
    finally:
        shutil.rmtree(private, ignore_errors=True)
