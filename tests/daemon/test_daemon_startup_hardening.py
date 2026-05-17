# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bundle B: daemon-startup operational hardening tests (RDR-112 follow-ups).

Covers three operational findings under the ``nexus-gdb3`` epic:

- **nexus-12gb**: ``_unlink_discovery`` writes a shutdown marker before
  attempting removal and retries the unlink once on ``OSError``.
- **nexus-dl3g**: ``_acquire_spawn_lock`` raises on Windows instead of
  silently allowing concurrent daemons to start without a lock.

The matching install-flow tests (``nexus-31cr`` overwrite guard and
``nexus-2wvl`` stale-binary detection in ``nx doctor --check-bridge``)
live in ``tests/commands/test_daemon_autostart.py`` and
``tests/commands/test_doctor_check_bridge.py`` so they sit next to the
code they exercise.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.daemon.t2_daemon import T2Daemon


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon(tmp_path: Path) -> T2Daemon:
    """Construct a daemon without starting it (no sockets, no t2db).

    The constructor wires ``_discovery_path`` + ``_spawn_lock_path``
    eagerly so the unit-level helpers (``_unlink_discovery``,
    ``_acquire_spawn_lock``) are reachable on an unstarted instance.
    """
    return T2Daemon(tmp_path / "config")


# ---------------------------------------------------------------------------
# nexus-12gb: _unlink_discovery shutdown-marker + retry
# ---------------------------------------------------------------------------


class TestUnlinkDiscoveryHardening:
    """Verify shutdown-marker write and single-retry unlink semantics."""

    def test_shutdown_marker_written_before_unlink(self, daemon: T2Daemon) -> None:
        """Successful path: marker is stamped, then file is removed."""
        # Pre-populate the discovery file so the marker write has somewhere
        # to land. (In production, ``_write_discovery`` runs at startup.)
        daemon._config_dir.mkdir(parents=True, exist_ok=True)
        daemon._discovery_path.write_text(json.dumps({"pid": 1234}))

        # Spy on write_text to confirm the marker payload before unlink.
        # Patching on the class makes the function unbound, so the spy
        # receives (self_path, content, ...) — content is the second arg.
        observed_writes: list[str] = []
        original_unbound = type(daemon._discovery_path).write_text

        def _spy_write(self_path, content, *args, **kwargs):
            observed_writes.append(content)
            return original_unbound(self_path, content, *args, **kwargs)

        with patch.object(type(daemon._discovery_path), "write_text", _spy_write):
            daemon._unlink_discovery()

        assert not daemon._discovery_path.exists()
        # Exactly one marker write happened (between _write_discovery and unlink).
        assert len(observed_writes) == 1
        payload = json.loads(observed_writes[0])
        assert payload.get("status") == "shutting_down"
        assert "shutdown_at" in payload

    def test_unlink_retries_once_on_oserror(self, daemon: T2Daemon) -> None:
        """First unlink fails with OSError; second attempt succeeds."""
        daemon._config_dir.mkdir(parents=True, exist_ok=True)
        daemon._discovery_path.write_text("{}")

        call_count = {"n": 0}
        original_unlink = Path.unlink

        def _flaky_unlink(self, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("EAGAIN: transient NFS hiccup")
            return original_unlink(self, *args, **kwargs)

        with patch.object(Path, "unlink", _flaky_unlink):
            daemon._unlink_discovery()

        assert call_count["n"] == 2, "expected one retry after first OSError"
        assert not daemon._discovery_path.exists()

    def test_unlink_swallows_permanent_oserror(self, daemon: T2Daemon) -> None:
        """Both attempts fail; the helper logs and returns, never raising."""
        daemon._config_dir.mkdir(parents=True, exist_ok=True)
        daemon._discovery_path.write_text("{}")

        call_count = {"n": 0}

        def _always_fail(self, *args, **kwargs):
            call_count["n"] += 1
            raise OSError("EROFS: read-only filesystem")

        with patch.object(Path, "unlink", _always_fail):
            # Must NOT raise: shutdown path can't abort on best-effort cleanup.
            daemon._unlink_discovery()

        assert call_count["n"] == 2, (
            "expected exactly two attempts (initial + one retry)"
        )

    def test_marker_write_failure_does_not_block_unlink(
        self, daemon: T2Daemon
    ) -> None:
        """Marker write failure logs but unlink still runs."""
        daemon._config_dir.mkdir(parents=True, exist_ok=True)
        daemon._discovery_path.write_text("{}")

        # write_text fails (e.g. EROFS), but unlink path is unaffected.
        with patch.object(
            type(daemon._discovery_path),
            "write_text",
            side_effect=OSError("EROFS"),
        ):
            daemon._unlink_discovery()

        assert not daemon._discovery_path.exists()


# ---------------------------------------------------------------------------
# nexus-dl3g: Windows refuse on spawn-lock
# ---------------------------------------------------------------------------


class TestSpawnLockWindowsRefusal:
    """Native Windows is out of v1 scope; spawn-lock now raises instead of
    silently allowing concurrent daemons.
    """

    def test_windows_raises_runtime_error(self, daemon: T2Daemon) -> None:
        """sys.platform == 'win32' refuses spawn-lock acquisition."""
        with patch.object(sys, "platform", "win32"):
            with pytest.raises(RuntimeError, match="not supported.*Windows"):
                daemon._acquire_spawn_lock()

    def test_error_message_suggests_tcp_fallback(self, daemon: T2Daemon) -> None:
        """Error names the supported alternative (TCP from Linux/macOS host)."""
        with patch.object(sys, "platform", "win32"):
            with pytest.raises(RuntimeError) as excinfo:
                daemon._acquire_spawn_lock()
        message = str(excinfo.value)
        assert "TCP" in message
        assert "Linux" in message or "macOS" in message

    def test_linux_still_acquires(self, daemon: T2Daemon, tmp_path: Path) -> None:
        """Regression guard: non-Windows path still acquires the lock."""
        if sys.platform == "win32":  # pragma: no cover
            pytest.skip("test pertains to non-Windows behavior")
        daemon._acquire_spawn_lock()
        try:
            assert daemon._spawn_lock_fh is not None
            assert daemon._spawn_lock_path.exists()
        finally:
            daemon._release_spawn_lock()
