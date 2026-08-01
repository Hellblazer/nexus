# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH-1061 E3: nx memory commands print a clean one-liner when T2 is unreachable.

Originally about the T2 DAEMON being down; that daemon retired in nexus-i711w
Stage 2 sub-stage B and took its four test classes with it (see the tombstone
below). What survives is the contract that outlived the transport: an
unreachable T2 backend must produce a non-zero exit and a clean one-liner
naming a recovery action, never a raw traceback — now exercised against the
ENGINE, which is what `nx memory` actually talks to.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


# NO daemon-down classes: all four (list / search / put / version-skew) injected
# their failure at ``T2Client.call`` — the SQLite-era daemon RPC socket — and
# both that client and the daemon it spoke to retired in nexus-i711w Stage 2
# sub-stage B. `t2_handle`'s SQLite branch now raises before any client is
# constructed, so the patch target is unreachable.
#
# THEY WERE REMOVED WHOLESALE, not just the ones that turned red. Two of them
# (``test_exit_code_nonzero``, ``test_lazy_path_catches_not_construction_path``)
# still PASSED after the retirement — they assert only "non-zero exit, no
# traceback", which the new fail-loud branch satisfies without their patch ever
# firing. That is a dead patch target passing green: precisely the class this
# file's own docstring warned about, and keeping them would have left two tests
# claiming to prove a lazy-connect catch that no longer exists.
#
# ``TestMemoryEngineUnreachable`` below is the surviving owner: it drives the
# same verbs with the ENGINE unreachable, which is the real failure mode now
# that the backend is a remote service rather than a local socket.


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


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
