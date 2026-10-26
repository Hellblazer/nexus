# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A mkdir(1) stand-in that reports the loser of a create race as success.

Shared by tests/scripts/test_mkdir_claim_survives_racy_mkdir.py (nexus-bo01z) and
tests/scripts/test_ci_shared_venv.py (nexus-x8sk8): uutils coreutils 0.8.0 (Ubuntu 26.04, chas)
does this for real; the shim reproduces it on any host and logs each race it lost, so a test can
assert a race actually happened before it asserts the claim held.
"""
from __future__ import annotations

import shutil
import stat
import sys
from pathlib import Path

# -p passes straight through: only a bare mkdir is a claim, and a stand-in that
# failed `mkdir -p <existing>` would break unrelated setup. The racer that really
# created the directory returns 0.5 s later than the ones that lost, so a loser
# always reaches its next step first: the order in which a claim built on
# "mkdir, then write the pid" lets two holders in. The create itself is
# os.mkdir, never the host's mkdir(1): on a uutils host that would report the
# lost race as success too, and the log of lost races would stay empty. Each
# check that passes leaves a file in the ``checked`` directory beside the log, so
# a test can start a racer only once another is inside its window.
_RACY_MKDIR = """#!/bin/bash
for a in "$@"; do case "$a" in -p|--parents) exec "{real}" "$@" ;; -*) ;; *) p="$a" ;; esac; done
if [ -e "$p" ]; then echo "mkdir: cannot create directory '$p': File exists" >&2; exit 1; fi
: > "{log}.checked/$$"
sleep 0.3
if "{py}" -c 'import os, sys; os.mkdir(sys.argv[1])' "$p" 2>/dev/null; then sleep 0.5
else echo "$p" >> "{log}"; fi
exit 0
"""


def executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def racy_mkdir_bin(tmp_path: Path) -> tuple[Path, Path]:
    shim, log = tmp_path / "bin", tmp_path / "raced"
    real = shutil.which("mkdir")
    assert real
    executable(shim / "mkdir", _RACY_MKDIR.format(real=real, log=log, py=sys.executable))
    (tmp_path / "raced.checked").mkdir()
    return shim, log
