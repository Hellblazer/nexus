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


# nexus-aqbrk: the four daemon-down classes below are PINNED to the local T2
# backend. They inject the failure at ``T2Client.call`` — the SQLite-era daemon
# RPC socket — and in service mode ``nx memory`` never touches T2Client at all;
# it goes over HTTP to the engine. So the patch intercepts NOTHING and every
# command succeeds, which is why all six failed as ``exit_code != 0`` -> got 0.
# A dead patch target, exactly the class this file's own module docstring warns
# about ("if the catch were moved back to wrap make_t2_client() only, these
# tests would fail").
#
# T2Client and the daemon it speaks to are retirement debt (nexus-gmiaf.24
# deletes the SQLite daemon lifecycle), so these are pinned rather than
# rewritten — they test a real mechanism that still exists today.
#
# SERVICE HALF: unowned before this commit. Nothing anywhere drove an
# ``nx memory`` verb with the ENGINE unreachable, even though that is the more
# likely failure now that the backend is a REMOTE service (restart, network
# blip, expired bearer) rather than a local socket. Verified by probe that the
# behaviour is already correct — exit 1, no traceback, clean one-liner with a
# recovery hint — so this adds the missing ASSERTION, not a fix.
# ``TestMemoryEngineUnreachable`` at the bottom owns it.
#
# PER-CLASS, not a module ``pytestmark``: a module-level mark also lands on
# TestMemoryEngineUnreachable, and local_t2_backend (non-autouse) then
# resolves AFTER that class's autouse _engine_down fixture and overwrites
# service with sqlite. The classes want opposite backends.


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


@pytest.mark.usefixtures("local_t2_backend")
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


@pytest.mark.usefixtures("local_t2_backend")
class TestMemorySearchDaemonDown:
    """nx memory search must also handle daemon-down cleanly."""

    def test_clean_error_no_traceback(self, runner: CliRunner) -> None:
        exc = T2DaemonNotReachableError("TCP connect failed at 127.0.0.1:9999: Connection refused")
        with _patch_t2_client_call_raising(exc):
            result = runner.invoke(main, ["memory", "search", "myquery"])

        assert result.exit_code != 0
        assert "Traceback" not in result.output
        assert "nx daemon t2 start" in result.output


@pytest.mark.usefixtures("local_t2_backend")
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


@pytest.mark.usefixtures("local_t2_backend")
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


class TestMemoryEngineUnreachable:
    """GH-1061 E3, service-mode half: the ENGINE down, not the T2 daemon.

    nexus-aqbrk. The classes above inject ``T2DaemonNotReachableError`` at
    ``T2Client.call``, which is the local daemon's RPC socket. In service
    mode that socket does not exist — ``nx memory`` reaches the engine over
    HTTP — so the whole file was silent about the failure mode that actually
    matters on the new substrate. E3's concern (a raw traceback instead of an
    actionable one-liner) does not shrink when the backend moves off-box; it
    grows, because a remote service is unreachable for far more ordinary
    reasons than a local socket.

    These do NOT request ``local_t2_backend``; they set the backend to
    service themselves, which wins as the later ``setenv``. And they use a
    REAL closed port rather than a mock, so the assertion covers the genuine
    httpx/errno path the user hits — a mocked ConnectError would prove the
    handler catches what we chose to throw at it, which is not the question.
    """

    @staticmethod
    def _closed_port() -> int:
        """A port that is definitely not listening.

        Bind :0, read the assigned port, close it. Per the project's
        port-0 rule — never hardcode, and never assume a low port is
        refused (in a container it may be filtered and hang instead).
        """
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    @pytest.fixture(autouse=True)
    def _engine_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        monkeypatch.setenv("NX_SERVICE_URL", f"http://127.0.0.1:{self._closed_port()}")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "unused-the-connect-fails-first")

    @pytest.mark.parametrize("argv", [
        ["memory", "list"],
        ["memory", "search", "anything"],
        ["memory", "get", "-p", "proj", "-t", "some.md"],
    ])
    def test_clean_one_liner_not_traceback(
        self, runner: CliRunner, argv: list[str],
    ) -> None:
        result = runner.invoke(main, argv)

        assert result.exit_code != 0, (
            f"`nx {' '.join(argv)}` exited 0 with the engine unreachable — a "
            f"silent success is the worst outcome here, because a scripted "
            f"caller reads it as 'no entries'.\nOutput: {result.output}"
        )
        assert "Traceback" not in result.output, (
            f"raw traceback instead of a clean error:\n{result.output}"
        )
        for leaked in ("ConnectError", "httpx.", "HTTPStatusError"):
            assert leaked not in result.output, (
                f"leaked the transport exception {leaked!r} into user-facing "
                f"output:\n{result.output}"
            )

    def test_error_names_the_subsystem_and_a_recovery_action(
        self, runner: CliRunner,
    ) -> None:
        """E3's actual requirement: actionable, not merely non-crashing.

        Pins both halves of the current message — that it identifies the
        storage service as the failing subsystem, and that it points at a
        command the user can actually run.
        """
        result = runner.invoke(main, ["memory", "list"])
        out = result.output.lower()
        assert "storage service" in out, (
            f"error does not say WHICH subsystem failed: {result.output!r}"
        )
        assert "nx doctor" in out, (
            f"error gives the user no next step: {result.output!r}"
        )
