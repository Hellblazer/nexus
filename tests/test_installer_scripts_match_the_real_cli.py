# SPDX-License-Identifier: AGPL-3.0-or-later
"""The installer scripts' stand-in tests assert the argv the scripts pass to ``nx``; nothing
else ties that argv to the real command (nexus-sv2wx.28). These tests do: every flag the scripts
put on the ``nx init`` call must be an option the real ``init`` command has, and ``nx config
init`` must exist.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.commands.config_cmd import config_group
from nexus.commands.init import init_cmd

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = [REPO / "web" / "install.sh", REPO / "web" / "install.ps1"]

# The flags the scripts put on the `nx init` call: the autostart pair and the beads decline.
PASSED_TO_INIT = ("--yes", "--no-autostart", "--no-beads-prime")


def _init_options() -> set[str]:
    return {opt for param in init_cmd.params for opt in getattr(param, "opts", [])}


@pytest.mark.parametrize("flag", PASSED_TO_INIT)
def test_the_real_init_command_has_the_flag(flag: str) -> None:
    assert flag in _init_options()


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
@pytest.mark.parametrize("flag", PASSED_TO_INIT)
def test_each_script_still_passes_the_flag(script: Path, flag: str) -> None:
    """If a script stops passing one, the list above and the real command need a second look."""
    assert flag in script.read_text()


def test_the_cloud_wizard_is_the_real_config_init() -> None:
    assert "init" in config_group.commands
