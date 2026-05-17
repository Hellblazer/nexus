# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-507q: cutover flip — NX_STORAGE_MODE default direct -> daemon.

RDR-112 P6.3 cutover: when ``NX_STORAGE_MODE`` is unset, the resolved
mode is now ``daemon``. Direct mode remains available indefinitely as
the debug fallback (``NX_STORAGE_MODE=direct``).

These tests must explicitly unset the env var because the test
suite's autouse fixture in ``conftest.py`` pins ``direct`` to
preserve the existing test contract. Each test uses
``monkeypatch.delenv(..., raising=False)`` and then asserts the
production default at the resolver level.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Resolver helpers (nexus.db)
# ---------------------------------------------------------------------------


class TestDefaultResolver:
    """``default_storage_mode()`` returns 'daemon' when env is unset."""

    def test_unset_returns_daemon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.db import default_storage_mode
        monkeypatch.delenv("NX_STORAGE_MODE", raising=False)
        assert default_storage_mode() == "daemon"

    def test_direct_returns_direct(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.db import default_storage_mode
        monkeypatch.setenv("NX_STORAGE_MODE", "direct")
        assert default_storage_mode() == "direct"

    def test_daemon_returns_daemon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.db import default_storage_mode
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
        assert default_storage_mode() == "daemon"

    def test_unknown_value_returns_as_is(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown values pass through (callers compare to 'daemon'/'direct')."""
        from nexus.db import default_storage_mode
        monkeypatch.setenv("NX_STORAGE_MODE", "in-process")
        assert default_storage_mode() == "in-process"

    def test_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.db import default_storage_mode
        monkeypatch.setenv("NX_STORAGE_MODE", "DAEMON")
        assert default_storage_mode() == "daemon"


class TestIsDaemonMode:
    """``is_daemon_mode()`` returns True when env unset (the new default)."""

    def test_unset_is_daemon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.db import is_daemon_mode
        monkeypatch.delenv("NX_STORAGE_MODE", raising=False)
        assert is_daemon_mode() is True

    def test_explicit_daemon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.db import is_daemon_mode
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
        assert is_daemon_mode() is True

    def test_direct_is_not_daemon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.db import is_daemon_mode
        monkeypatch.setenv("NX_STORAGE_MODE", "direct")
        assert is_daemon_mode() is False

    def test_unknown_is_not_daemon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.db import is_daemon_mode
        monkeypatch.setenv("NX_STORAGE_MODE", "in-process")
        assert is_daemon_mode() is False


# ---------------------------------------------------------------------------
# reject_under_daemon_mode now fires when env is unset (the new default)
# ---------------------------------------------------------------------------


class TestRejectUnderDaemonMode:
    """The guard fires on the new default: unset env -> daemon -> reject."""

    def test_unset_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.db import DaemonModeDiagnosticError, reject_under_daemon_mode
        monkeypatch.delenv("NX_STORAGE_MODE", raising=False)
        with pytest.raises(DaemonModeDiagnosticError):
            reject_under_daemon_mode("test_op")

    def test_direct_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.db import reject_under_daemon_mode
        monkeypatch.setenv("NX_STORAGE_MODE", "direct")
        reject_under_daemon_mode("test_op")  # should not raise

    def test_explicit_daemon_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.db import DaemonModeDiagnosticError, reject_under_daemon_mode
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
        with pytest.raises(DaemonModeDiagnosticError):
            reject_under_daemon_mode("test_op")


# ---------------------------------------------------------------------------
# Fail-loud-on-missing-daemon (MCP tuplespace bootstrap)
# ---------------------------------------------------------------------------


class TestMcpFailLoudOnMissingDaemon:
    """``_get_tuplespace`` raises clearly when daemon mode is on but no daemon."""

    def test_unset_env_raises_when_daemon_not_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """The new default selects daemon; missing discovery -> RuntimeError.

        nexus-0cf1.5 (TR-5, 2026-05-17): override NEXUS_CONFIG_DIR (not
        just HOME) so the discovery probe consults the tmp dir, not the
        operator's real ~/.config/nexus/. The prior version only set
        HOME, but nexus_config_dir() honours NEXUS_CONFIG_DIR — the
        test passed for the wrong reason (tmp UID-named file absent),
        and would not catch a regression that bypassed the override.
        """
        # Force the resolver: no env at all, no daemon discovery file.
        monkeypatch.delenv("NX_STORAGE_MODE", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "nexus-config"))
        # Wipe the module-level cache so the bootstrap actually runs again.
        from nexus.mcp import core as _core
        _core._TUPLESPACE.clear()
        # The discovery probe consults find_t2_daemon(); when the discovery
        # file is missing it returns None, and the bootstrap is supposed to
        # raise RuntimeError with the migration hint.
        with pytest.raises(RuntimeError, match="NX_STORAGE_MODE=daemon"):
            _core._get_tuplespace()
