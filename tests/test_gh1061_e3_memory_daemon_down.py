# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH-1061 E3: nx memory commands print clean one-liner when T2 daemon is down.

T2Client connects lazily: ``make_t2_client()`` just allocates the object; the
socket is only opened on the first ``T2Client.call()`` RPC.  Therefore
``T2DaemonNotReachableError`` fires inside the ``yield client`` block of
``t2_handle()``, NOT at construction time.  The fix wraps the yield block:

    client = make_t2_client()   # cheap allocation, no socket
    try:
        yield client             # <-- error fires here on first RPC
    except T2DaemonNotReachableError:
        raise click.ClickException(...)
    finally:
        client.close()

Tests inject the error via ``T2Client.call`` (the actual lazy-connect site),
not via ``make_t2_client`` side_effect (construction-time, wrong path).

Verifying the lazy path is critical: if the catch were moved back to wrap
``make_t2_client()`` only, these tests would fail — the exception would escape
as a raw traceback because it fires inside yield, outside the catch.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.daemon.t2_client import T2DaemonNotReachableError, T2SchemaVersionMismatchError


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _patch_t2_client_call_raising(exc):
    """Patch T2Client.call to raise *exc* on the first RPC.

    This exercises the REAL lazy-connect failure path: make_t2_client()
    succeeds (returns a T2Client with _sock=None), but the first call()
    triggers the error, which fires inside the ``yield client`` block of
    t2_handle().  The catch in t2_handle must intercept it there.
    """
    return patch("nexus.daemon.t2_client.T2Client.call", side_effect=exc)


class TestMemoryListDaemonDown:
    """nx memory list must print a clean one-liner and exit non-zero when daemon is down."""

    def test_clean_one_liner_not_traceback(self, runner: CliRunner) -> None:
        exc = T2DaemonNotReachableError("TCP connect failed at 127.0.0.1:9999: Connection refused")
        with _patch_t2_client_call_raising(exc):
            result = runner.invoke(main, ["memory", "list"])

        # Must exit non-zero
        assert result.exit_code != 0, (
            f"Expected non-zero exit for daemon-down, got 0.\nOutput: {result.output}"
        )
        # Must NOT contain a raw Python traceback
        assert "Traceback" not in result.output, (
            f"Got raw traceback instead of clean error:\n{result.output}"
        )
        assert "T2DaemonNotReachableError" not in result.output, (
            f"Got raw exception class instead of clean error:\n{result.output}"
        )
        # Must contain an actionable recovery hint
        assert "nx daemon t2 start" in result.output, (
            f"Expected 'nx daemon t2 start' in output, got:\n{result.output}"
        )

    def test_exit_code_nonzero(self, runner: CliRunner) -> None:
        exc = T2DaemonNotReachableError("daemon gone")
        with _patch_t2_client_call_raising(exc):
            result = runner.invoke(main, ["memory", "list"])

        assert result.exit_code != 0

    def test_lazy_path_catches_not_construction_path(self, runner: CliRunner) -> None:
        """Confirm the catch is on the lazy RPC path (yield block), not make_t2_client.

        This test patches T2Client.call (lazy connect) — NOT make_t2_client (eager).
        If the catch were only on make_t2_client(), this test would see a raw
        traceback (exception fires inside yield, outside the catch).  The test
        passing confirms t2_handle wraps the yield block.
        """
        exc = T2DaemonNotReachableError("lazy connect failed")
        # Ensure make_t2_client succeeds (returns a real T2Client object)
        # but T2Client.call raises when the command tries to use it.
        with _patch_t2_client_call_raising(exc):
            result = runner.invoke(main, ["memory", "list"])

        # Must not be a raw traceback — if the catch were on make_t2_client only,
        # this would be a raw traceback because the error fires at a different site.
        assert "Traceback" not in result.output, (
            "Lazy-path catch is missing: error from T2Client.call escaped as traceback.\n"
            f"Output:\n{result.output}"
        )
        assert result.exit_code != 0


class TestMemorySearchDaemonDown:
    """nx memory search must also handle daemon-down cleanly."""

    def test_clean_error_no_traceback(self, runner: CliRunner) -> None:
        exc = T2DaemonNotReachableError("TCP connect failed at 127.0.0.1:9999: Connection refused")
        with _patch_t2_client_call_raising(exc):
            result = runner.invoke(main, ["memory", "search", "myquery"])

        assert result.exit_code != 0
        assert "Traceback" not in result.output
        assert "nx daemon t2 start" in result.output


class TestMemoryPutDaemonDown:
    """nx memory put must handle daemon-down cleanly."""

    def test_clean_error_no_traceback(self, runner: CliRunner) -> None:
        exc = T2DaemonNotReachableError("TCP connect failed")
        with _patch_t2_client_call_raising(exc):
            result = runner.invoke(
                main, ["memory", "put", "hello", "--project", "p", "--title", "t.md"]
            )

        assert result.exit_code != 0
        assert "Traceback" not in result.output
        assert "nx daemon t2 start" in result.output


class TestMemoryVersionSkewDaemonDown:
    """T2SchemaVersionMismatchError (version-skewed daemon) must also be handled cleanly.

    M-1 from review: a version-skewed daemon's __str__ is already actionable;
    surface it as a click.ClickException rather than a raw traceback.
    """

    def test_version_skew_clean_error(self, runner: CliRunner) -> None:
        exc = T2SchemaVersionMismatchError(
            client_version="5.6.0",
            daemon_version="5.5.0",
        )
        with _patch_t2_client_call_raising(exc):
            result = runner.invoke(main, ["memory", "list"])

        assert result.exit_code != 0
        assert "Traceback" not in result.output, (
            f"Version-skew error should be clean, got traceback:\n{result.output}"
        )
        # __str__ of T2SchemaVersionMismatchError contains "5.6.0" and "5.5.0"
        assert "5.6.0" in result.output or "mismatch" in result.output.lower() or "schema" in result.output.lower(), (
            f"Expected version mismatch info in output:\n{result.output}"
        )
