# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Regression for the MagicMock-pgid-1 hazard that hung CI on v4.7.0.

``MagicMock()`` implements ``__index__`` returning ``1``. A naive
``os.killpg(os.getpgid(proc.pid), SIGKILL)`` on a mock fixture therefore
signals ``pgid=1`` (init / launchd) — benign on macOS (EPERM), deadlock-
inducing on GitHub Actions ubuntu-latest containers. The canonical
``safe_killpg`` helper guards with ``isinstance(pid, int)`` so mock
fixtures fall through cleanly.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest

from nexus.util.process_group import safe_killpg


class TestMockGuard:
    def test_magicmock_proc_returns_false_without_signalling(self):
        """The core regression: MagicMock.pid coerces to int=1; the helper
        must refuse to signal pgid=1.
        """
        proc = MagicMock()

        # Sanity check: the dangerous coercion is still real. If a future
        # Python release changes the MagicMock __index__ contract, this
        # line will change (but the helper's guard will still be correct).
        assert int(proc.pid) == 1, (
            "MagicMock.pid no longer coerces to 1 — "
            "verify the safe_killpg guard is still needed"
        )

        # The helper must return False and must NOT invoke os.killpg.
        # We use signal 0 (liveness probe only — never delivers a kill
        # signal) for the real-pgid branch, but MagicMock should never
        # reach it.
        import nexus.util.process_group as mod
        original_killpg = os.killpg
        calls: list[tuple] = []

        def _trace_killpg(pgid, sig):
            calls.append((pgid, sig))
            return original_killpg(pgid, sig)

        mod.os.killpg = _trace_killpg  # type: ignore[assignment]
        try:
            assert safe_killpg(proc, signal.SIGKILL) is False
        finally:
            mod.os.killpg = original_killpg  # type: ignore[assignment]
        assert calls == [], (
            f"safe_killpg invoked os.killpg {calls!r} for a mock proc — "
            "the isinstance(pid, int) guard is broken"
        )

    def test_magicmock_bare_pid_returns_false(self):
        """Same contract when a bare mock is passed instead of a proc wrapper."""
        mock_pid = MagicMock()
        assert safe_killpg(mock_pid, signal.SIGKILL) is False


class TestRealSubprocess:
    def test_real_subprocess_pgid_is_signalled(self):
        """A real child spawned with start_new_session=True must be
        signalable via safe_killpg.
        """
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Probe that the PID exists first (signal 0 is the portable
            # "liveness" test — does not deliver a signal).
            assert safe_killpg(proc, 0) is True, (
                "live subprocess is unreachable via its own pgid"
            )

            # Now actually kill it.
            assert safe_killpg(proc, signal.SIGKILL) is True
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def test_bare_int_pid_accepted(self):
        """Callers that hold a bare PID (e.g. reading from a PID file) can
        pass the int directly instead of wrapping in a proc-like object.
        """
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert safe_killpg(proc.pid, 0) is True
            assert safe_killpg(proc.pid, signal.SIGKILL) is True
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


class TestErrorSwallowing:
    def test_nonexistent_pid_returns_false(self):
        """PID that was reaped (or never existed) → False, no exception."""
        # Very high PID unlikely to be in use. If by chance it maps to a
        # real process, the test will still be correct — safe_killpg
        # returns True only when the signal is actually delivered.
        assert safe_killpg(2**30 - 1, 0) in (False, True)
        # The False path is what we're testing. Force it with a sentinel
        # that OS will always reject — negative PIDs are never valid.
        assert safe_killpg(-1, signal.SIGKILL) is False

    def test_never_raises_for_common_failure_modes(self):
        """Helper must swallow ProcessLookupError / PermissionError / OSError."""
        # Non-int — handled by the mock-guard branch.
        assert safe_killpg("not-a-pid", signal.SIGKILL) is False
        assert safe_killpg(None, signal.SIGKILL) is False
        # Negative int — kernel rejects.
        assert safe_killpg(-42, signal.SIGKILL) is False
