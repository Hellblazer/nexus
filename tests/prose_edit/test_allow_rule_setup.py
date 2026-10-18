# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ger02.28: the Setup section's allow rules match the work directories `tmpdir` makes.

Measured 2026-10-18 (T2 nexus/prose-edit-allow-rule-measurement-2026-10-18): default mode needs both a Read() and an
Edit() rule on <realpath of the temp dir>/prose-edit-*/**, and a rule on a symlinked temp path never matches.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.prose_edit.conftest import Prose
from tests.prose_edit.test_brief import SKILL, brief_ok


def _setup() -> str:
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("\n## Setup\n")
    return text[start:text.index("\n## ", start + 1)]


def test_setup_names_both_rules_on_the_work_directory_glob() -> None:
    setup = _setup()
    assert '"Read(//<R>/prose-edit-*/**)"' in setup
    assert '"Edit(//<R>/prose-edit-*/**)"' in setup
    assert "~/.claude/settings.json" in setup


def test_the_documented_path_command_is_the_base_every_work_directory_sits_under(prose: Prose) -> None:
    cmd = re.search(r"`(python3 -c '[^`]+')`", _setup())
    assert cmd, "Setup must give the command that prints <R>"
    code = cmd.group(1).split("'", 2)[1]
    printed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                             env=dict(os.environ)).stdout.strip()
    assert printed == os.path.realpath(tempfile.gettempdir())
    work = Path(dict(line.split("=", 1) for line in brief_ok(prose, "tmpdir", "--ready").strip().splitlines())["WORK"])
    try:
        assert str(work.parent) == printed
        rule_glob = "/" + printed.lstrip("/") + "/prose-edit-*/**"
        assert fnmatch.fnmatch(str(work / "brief.md"), rule_glob.replace("/**", "/*"))
    finally:
        brief_ok(prose, "rmtmp", str(work))
