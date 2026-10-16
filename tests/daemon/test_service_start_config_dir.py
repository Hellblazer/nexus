# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx daemon service start --foreground --config-dir DIR`` reads every setting from DIR
(nexus-sv2wx.24, macOS MVV row 1).

The autostart unit runs exactly that command and sets no ``NEXUS_CONFIG_DIR``. The
supervisor's own config reads (``local.embed_model``, the Voyage key it hands the
engine) went through ``nexus_config_dir()``, which knows only the environment variable
and ``~/.config/nexus``. Under launchd the home is the account's real one, so an install
with its config elsewhere started an engine configured from ``~/.config/nexus``: there
it found a Voyage key and no model choice, came up Voyage-only, and refused the first
store against the bge collections the client builds.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus import config as nx_config
from nexus.commands import daemon as daemon_mod
from nexus.daemon import storage_service_daemon as ssd


def _write_config(directory: Path, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.yml").write_text(text, encoding="utf-8")


def test_foreground_start_reads_the_flagged_config_dir_not_the_home_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Path.home() on Windows
    # Recorded so the teardown removes whatever the command exports.
    monkeypatch.setenv("NEXUS_CONFIG_DIR", "")
    monkeypatch.delenv("NEXUS_CONFIG_DIR")
    _write_config(home / ".config" / "nexus", "credentials:\n  voyage_api_key: not-a-real-key\n")
    flagged = tmp_path / "elsewhere" / "nexus"
    _write_config(flagged, "local:\n  embed_model: BAAI/bge-base-en-v1.5\n")

    seen: dict[str, object] = {}

    def fake_supervisor(*, config_dir: Path) -> int:
        seen["config_dir"] = config_dir
        seen["nexus_config_dir"] = nx_config.nexus_config_dir()
        seen["choice"] = nx_config.local_embed_model_choice()
        return 0

    monkeypatch.setattr(ssd, "run_storage_supervisor", fake_supervisor)
    with pytest.raises(SystemExit) as exit_info:
        daemon_mod.service_start_cmd.callback(config_dir_str=str(flagged), foreground=True, announce_stdout=False)

    assert exit_info.value.code == 0
    assert seen["config_dir"] == flagged
    assert Path(str(seen["nexus_config_dir"])).resolve() == flagged.resolve()
    assert seen["choice"] == "BAAI/bge-base-en-v1.5"


def test_foreground_start_without_the_flag_leaves_the_environment_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Path.home() on Windows
    monkeypatch.setenv("NEXUS_CONFIG_DIR", "")
    monkeypatch.delenv("NEXUS_CONFIG_DIR")
    seen: dict[str, object] = {}

    def fake_supervisor(*, config_dir: Path) -> int:
        seen["config_dir"] = config_dir
        return 0

    monkeypatch.setattr(ssd, "run_storage_supervisor", fake_supervisor)
    with pytest.raises(SystemExit):
        daemon_mod.service_start_cmd.callback(config_dir_str=None, foreground=True, announce_stdout=False)

    assert seen["config_dir"] == home / ".config" / "nexus"
    assert "NEXUS_CONFIG_DIR" not in os.environ
