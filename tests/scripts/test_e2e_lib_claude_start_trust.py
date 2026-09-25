# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""tests/e2e/lib.sh's claude_start must accept the workspace-trust screen
whichever option the Claude Code build highlights (nexus-wauo1.11 proof).

Claude Code 2.1.282 highlights "No, exit" on the trust screen, so the bare
Enter claude_start used to send quit Claude and left the pane at a shell.
The test sources the real lib.sh, replaces `capture` with a scripted pane
sequence and `_tmux` with a key log, and runs claude_start for real.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "tests" / "e2e" / "lib.sh"

_DRIVER = r"""
set -u
source "$LIB"
sleep() { :; }
# capture runs in a $(...) subshell, so its call count lives in a file.
capture() {
    if [ ! -e "$SEEN" ]; then : > "$SEEN"; printf '%s\n' "$TRUST_SCREEN"
    else printf '%s\n' "bypass permissions on"; fi
}
_tmux() { printf '%s\n' "${@: -1}" >> "$KEYLOG"; }
claude_start >/dev/null
"""


def _keys_after_launch(tmp_path: Path, trust_screen: str) -> list[str]:
    keylog = tmp_path / "keys.log"
    subprocess.run(
        ["bash", "-c", _DRIVER],
        env={"LIB": str(LIB), "KEYLOG": str(keylog), "TRUST_SCREEN": trust_screen,
             "SEEN": str(tmp_path / "seen"),
             "PATH": "/usr/bin:/bin"},
        check=True, timeout=30,
    )
    keys = keylog.read_text().splitlines()
    # The first logged key is the Enter that launches claude.
    return keys[1:]


@pytest.mark.parametrize("screen", [
    "Do you trust this folder?\n  1. Yes, I trust this folder\n❯ 2. No, exit",
    "Do you trust this folder?\n  Yes, I trust this folder\n❯ No, exit",
])
def test_trust_screen_defaulting_to_no_moves_up_before_enter(tmp_path, screen) -> None:
    assert _keys_after_launch(tmp_path, screen)[:2] == ["Up", "Enter"]


def test_trust_screen_defaulting_to_yes_sends_only_enter(tmp_path) -> None:
    screen = "Do you trust this folder?\n❯ 1. Yes, I trust this folder\n  2. No, exit"
    keys = _keys_after_launch(tmp_path, screen)
    assert keys[:1] == ["Enter"]
    assert "Up" not in keys
