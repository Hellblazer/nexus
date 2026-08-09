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
import threading
import time

import pytest
import structlog
from structlog.testing import capture_logs

from tests._engine_substrate import (
    _boot_semaphore_slot,
    _try_acquire_boot_slot,
)


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
            start = time.monotonic()
            with pytest.raises(RuntimeError):
                with _boot_semaphore_slot(
                    max_concurrent=1, lock_dir=tmp_path,
                    timeout_s=0.3, poll_s=0.05,
                ):
                    pass  # pragma: no cover
            elapsed = time.monotonic() - start
            assert elapsed < 3.0, (
                f"timeout took {elapsed}s for a 0.3s budget -- looks like a "
                "silent hang, not a bounded wait"
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
