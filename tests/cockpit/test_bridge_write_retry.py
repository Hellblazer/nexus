# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nexus-wf07: write-retry on sqlite3.OperationalError in hook bridge.

The bridge's direct-out path retries up to 3 times with 50/100/200ms backoff
on ``OperationalError`` whose message indicates lock/busy contention. Other
``OperationalError``s (malformed SQL, etc.) raise immediately.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nexus.cockpit import hook_bridge
from nexus.retry import _sqlite_with_retry


# ---------------------------------------------------------------------------
# Direct tests on the helper
# ---------------------------------------------------------------------------


class TestSqliteWithRetry:
    def test_retries_on_locked_then_succeeds(self) -> None:
        attempts: list[int] = []

        def fn() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with patch("nexus.retry.time.sleep") as sleep_mock:
            result = _sqlite_with_retry(fn)

        assert result == "ok"
        assert len(attempts) == 3
        # Backoff: 50ms then 100ms (no sleep after final success).
        delays = [call.args[0] for call in sleep_mock.call_args_list]
        assert delays == pytest.approx([0.05, 0.10])

    def test_retries_on_busy(self) -> None:
        calls = {"n": 0}

        def fn() -> str:
            calls["n"] += 1
            if calls["n"] < 2:
                raise sqlite3.OperationalError("database is busy")
            return "ok"

        with patch("nexus.retry.time.sleep"):
            assert _sqlite_with_retry(fn) == "ok"
        assert calls["n"] == 2

    def test_non_locking_operationalerror_does_not_retry(self) -> None:
        calls = {"n": 0}

        def fn() -> Any:
            calls["n"] += 1
            raise sqlite3.OperationalError("no such table: foo")

        with patch("nexus.retry.time.sleep") as sleep_mock:
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                _sqlite_with_retry(fn)

        assert calls["n"] == 1
        sleep_mock.assert_not_called()

    def test_other_exception_does_not_retry(self) -> None:
        calls = {"n": 0}

        def fn() -> Any:
            calls["n"] += 1
            raise ValueError("bang")

        with patch("nexus.retry.time.sleep") as sleep_mock:
            with pytest.raises(ValueError):
                _sqlite_with_retry(fn)

        assert calls["n"] == 1
        sleep_mock.assert_not_called()

    def test_final_failure_after_three_attempts(self, caplog: pytest.LogCaptureFixture) -> None:
        calls = {"n": 0}

        def fn() -> Any:
            calls["n"] += 1
            raise sqlite3.OperationalError("database is locked")

        with patch("nexus.retry.time.sleep"):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                _sqlite_with_retry(fn, event="hook_bridge_retried")

        assert calls["n"] == 3


# ---------------------------------------------------------------------------
# Integration with _direct_out
# ---------------------------------------------------------------------------


class TestDirectOutRetry:
    """_direct_out wraps api.out with the SQLite retry helper."""

    def test_direct_out_retries_locked_then_succeeds(self) -> None:
        attempts: list[int] = []

        def fake_out(**kwargs: Any) -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise sqlite3.OperationalError("database is locked")
            return "tuple-xyz"

        with patch("nexus.tuplespace.api.out", fake_out), \
             patch("nexus.retry.time.sleep"):
            result = hook_bridge._direct_out(
                conn=MagicMock(),
                index=MagicMock(),
                registry=MagicMock(),
                subspace="hook_events/tool_call_intent",
                content="{}",
                dimensions={"actor": "a", "session": "s", "project": "p", "timestamp": "0"},
                match_text=None,
            )

        assert result == "tuple-xyz"
        assert len(attempts) == 3

    def test_direct_out_non_locking_error_raises_immediately(self) -> None:
        attempts: list[int] = []

        def fake_out(**kwargs: Any) -> Any:
            attempts.append(1)
            raise sqlite3.OperationalError("syntax error near WHERE")

        with patch("nexus.tuplespace.api.out", fake_out), \
             patch("nexus.retry.time.sleep") as sleep_mock:
            with pytest.raises(sqlite3.OperationalError, match="syntax error"):
                hook_bridge._direct_out(
                    conn=MagicMock(),
                    index=MagicMock(),
                    registry=MagicMock(),
                    subspace="hook_events/tool_call_intent",
                    content="{}",
                    dimensions={"actor": "a", "session": "s", "project": "p", "timestamp": "0"},
                    match_text=None,
                )

        assert len(attempts) == 1
        sleep_mock.assert_not_called()

    def test_direct_out_final_failure_after_three_attempts(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        attempts: list[int] = []

        def fake_out(**kwargs: Any) -> Any:
            attempts.append(1)
            raise sqlite3.OperationalError("database is locked")

        with patch("nexus.tuplespace.api.out", fake_out), \
             patch("nexus.retry.time.sleep"), \
             caplog.at_level(logging.WARNING):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                hook_bridge._direct_out(
                    conn=MagicMock(),
                    index=MagicMock(),
                    registry=MagicMock(),
                    subspace="hook_events/tool_call_intent",
                    content="{}",
                    dimensions={"actor": "a", "session": "s", "project": "p", "timestamp": "0"},
                    match_text=None,
                )

        assert len(attempts) == 3
