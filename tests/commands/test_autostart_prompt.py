# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-run autostart nudge (RDR-112, nexus-mf91).

Tests cover the gate logic (TTY, env opt-outs, marker, CI, subagent),
the marker write side effect, and the structured ``autostart_status``
helper used by ``nx doctor --check-autostart``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.commands import _autostart_prompt as ap


@pytest.fixture(autouse=True)
def _reset_prompted_sentinel():
    """Reset the once-per-process sentinel between tests."""
    ap._PROMPTED = False
    yield
    ap._PROMPTED = False


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch):
    """Point nexus_config_dir() at a tmp dir so the marker write is sandboxed."""
    monkeypatch.setattr(
        "nexus.config.nexus_config_dir", lambda: tmp_path / "nexus"
    )
    return tmp_path / "nexus"


@pytest.fixture
def tty_stderr(monkeypatch):
    """Make the module's TTY gate report True."""
    monkeypatch.setattr(ap, "_stderr_is_tty", lambda: True)


def _clean_env(monkeypatch) -> None:
    """Clear every env var the gate logic consults."""
    for name in (
        "NEXUS_NO_PROMPTS",
        "NX_STORAGE_MODE",
        "CI",
        "CLAUDECODE",
        "NX_T1_HOST",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMaybeEmitAutostartPrompt:
    """Gate-by-gate semantics of the once-per-machine nudge."""

    def test_prompts_when_all_gates_clear(
        self,
        tmp_path: Path,
        monkeypatch,
        capsys,
        isolated_config,
        tty_stderr,
    ) -> None:
        _clean_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            ap, "_is_autostart_installed", lambda: False
        )

        ap.maybe_emit_autostart_prompt()
        err = capsys.readouterr().err
        assert "T2 daemon autostart not installed" in err
        # Marker file should be written.
        assert (isolated_config / ap._NUDGE_MARKER_NAME).exists()

    def test_does_not_prompt_when_already_prompted_in_process(
        self, monkeypatch, capsys, isolated_config, tty_stderr
    ) -> None:
        _clean_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(ap, "_is_autostart_installed", lambda: False)

        ap.maybe_emit_autostart_prompt()
        first = capsys.readouterr().err
        ap.maybe_emit_autostart_prompt()
        second = capsys.readouterr().err

        assert "autostart not installed" in first
        assert second == ""

    def test_does_not_prompt_when_stderr_not_a_tty(
        self, monkeypatch, capsys, isolated_config
    ) -> None:
        _clean_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(ap, "_is_autostart_installed", lambda: False)
        monkeypatch.setattr(ap, "_stderr_is_tty", lambda: False)

        ap.maybe_emit_autostart_prompt()
        assert capsys.readouterr().err == ""
        assert not (isolated_config / ap._NUDGE_MARKER_NAME).exists()

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
    def test_nexus_no_prompts_suppresses(
        self,
        monkeypatch,
        capsys,
        isolated_config,
        tty_stderr,
        value: str,
    ) -> None:
        _clean_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(ap, "_is_autostart_installed", lambda: False)
        monkeypatch.setenv("NEXUS_NO_PROMPTS", value)

        ap.maybe_emit_autostart_prompt()
        assert capsys.readouterr().err == ""

    def test_nx_storage_mode_direct_suppresses(
        self, monkeypatch, capsys, isolated_config, tty_stderr
    ) -> None:
        _clean_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(ap, "_is_autostart_installed", lambda: False)
        monkeypatch.setenv("NX_STORAGE_MODE", "direct")

        ap.maybe_emit_autostart_prompt()
        assert capsys.readouterr().err == ""

    def test_ci_env_suppresses(
        self, monkeypatch, capsys, isolated_config, tty_stderr
    ) -> None:
        _clean_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(ap, "_is_autostart_installed", lambda: False)
        monkeypatch.setenv("CI", "true")

        ap.maybe_emit_autostart_prompt()
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("var", ["CLAUDECODE", "NX_T1_HOST"])
    def test_subagent_env_suppresses(
        self,
        monkeypatch,
        capsys,
        isolated_config,
        tty_stderr,
        var: str,
    ) -> None:
        _clean_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(ap, "_is_autostart_installed", lambda: False)
        monkeypatch.setenv(var, "1")

        ap.maybe_emit_autostart_prompt()
        assert capsys.readouterr().err == ""

    def test_unsupported_platform_skips(
        self, monkeypatch, capsys, isolated_config, tty_stderr
    ) -> None:
        _clean_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "win32")

        ap.maybe_emit_autostart_prompt()
        assert capsys.readouterr().err == ""

    def test_marker_present_suppresses(
        self, monkeypatch, capsys, isolated_config, tty_stderr
    ) -> None:
        _clean_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(ap, "_is_autostart_installed", lambda: False)
        # Pre-seed the marker so the nudge has already fired once.
        isolated_config.mkdir(parents=True, exist_ok=True)
        (isolated_config / ap._NUDGE_MARKER_NAME).write_text("nudged\n")

        ap.maybe_emit_autostart_prompt()
        assert capsys.readouterr().err == ""

    def test_already_installed_suppresses(
        self, monkeypatch, capsys, isolated_config, tty_stderr
    ) -> None:
        _clean_env(monkeypatch)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(ap, "_is_autostart_installed", lambda: True)

        ap.maybe_emit_autostart_prompt()
        assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# autostart_status helper (consumed by `nx doctor --check-autostart`)
# ---------------------------------------------------------------------------


class TestAutostartStatus:
    def test_darwin_path_shape(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        status = ap.autostart_status()
        assert status["platform_supported"] is True
        assert status["unit_path"].endswith(
            "/Library/LaunchAgents/com.nexus.t2.plist"
        )

    def test_linux_path_shape(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        status = ap.autostart_status()
        assert status["platform_supported"] is True
        assert status["unit_path"].endswith(
            "/.config/systemd/user/nexus-t2.service"
        )

    def test_unsupported_platform(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        status = ap.autostart_status()
        assert status["platform_supported"] is False
        assert status["unit_path"] is None
        assert status["installed"] is False

    def test_storage_mode_surfaced(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
        assert ap.autostart_status()["storage_mode"] == "daemon"
