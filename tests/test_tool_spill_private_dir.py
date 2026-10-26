# SPDX-License-Identifier: AGPL-3.0-or-later
"""The tool-spill mod's private-directory check, run for real under /bin/sh (nexus-a9e7j).

`claude plugin test` has no process implementation, so the mod's own tests can only stub the check's exit code.
This runs the script text from tool-spill/hooks/register.ts against real directories, on whichever POSIX host runs
the suite (GNU coreutils on Linux CI, BSD on macOS).
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REGISTER: Path = (
    Path(__file__).resolve().parents[1] / "tool-spill" / "hooks" / "register.ts"
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the check runs on POSIX hosts only"
)


def _script() -> str:
    source = REGISTER.read_text()
    block = re.search(r"const PRIVATE_DIR = \[\n(.*?)\n\]\.join\('\\n'\)", source, re.S)
    assert block, "PRIVATE_DIR not found in register.ts"
    lines = re.findall(r"^  '(.*)',$", block.group(1), re.M)
    assert len(lines) >= 5, lines
    return "\n".join(line.replace("\\'", "'") for line in lines)


def _check(path: Path) -> int:
    # The argv the mod passes: /bin/sh -c <script> tool-spill <dir>.
    return subprocess.run(
        ["/bin/sh", "-c", _script(), "tool-spill", str(path)], check=False
    ).returncode


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_a_new_directory_is_created_owner_only(tmp_path: Path) -> None:
    d = tmp_path / "claude-tool-spill-sam"
    assert _check(d) == 0
    assert d.is_dir() and _mode(d) == 0o700 and d.stat().st_uid == os.getuid()


def test_an_existing_open_directory_of_ours_is_set_to_700(tmp_path: Path) -> None:
    d = tmp_path / "claude-tool-spill-sam"
    d.mkdir(mode=0o777)
    d.chmod(0o777)
    assert _check(d) == 0
    assert _mode(d) == 0o700


def test_a_symbolic_link_is_refused_and_its_target_left_alone(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o755)
    link = tmp_path / "claude-tool-spill-sam"
    link.symlink_to(target)
    assert _check(link) == 3
    assert _mode(target) == 0o755


def test_a_directory_another_user_owns_is_refused() -> None:
    # "/" is root's on every POSIX host; a suite running as root has no other user to tell apart.
    if os.getuid() == 0:
        pytest.skip("running as root: every directory is owned by this user")
    root_mode = _mode(Path("/"))
    assert _check(Path("/")) == 5
    assert _mode(Path("/")) == root_mode


def test_a_file_in_the_way_is_refused(tmp_path: Path) -> None:
    f = tmp_path / "claude-tool-spill-sam"
    f.write_text("x")
    assert _check(f) == 4


def test_a_name_with_spaces_and_a_leading_dash_is_handled(tmp_path: Path) -> None:
    d = tmp_path / "-a b" / "claude-tool-spill-s m"
    assert _check(d) == 0
    assert _mode(d) == 0o700
    # A missing parent is created under umask 077 too, so nothing is world-listable even before the chmod.
    assert _mode(d.parent) == 0o700
