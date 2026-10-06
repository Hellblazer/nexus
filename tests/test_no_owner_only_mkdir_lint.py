# SPDX-License-Identifier: AGPL-3.0-or-later
"""No library module makes a user directory with a bare ``mkdir(mode=0o700)``
(RDR-224, nexus-f9bgu.50).

On Windows, Python 3.12.4+ turns ``mode=0o700`` into an ACL of SYSTEM,
Administrators and OWNER RIGHTS. Made by an ELEVATED process, the directory is
then owned by Administrators, and a non-elevated session of the same user gets
WinError 5 on every read and write. ``nexus._winsec.make_user_dir`` adds the
user's own ACE on Windows and is exactly ``mkdir(parents=True, exist_ok=True,
mode=0o700)`` on POSIX, so every such site goes through it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

SRC = Path(__file__).resolve().parent.parent / "src" / "nexus"
#: The one module allowed the bare call: make_user_dir itself.
ALLOWED = {SRC / "_winsec.py"}


def _bare_0o700_mkdirs(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "mkdir"):
            continue
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and kw.value.value == 0o700:
                lines.append(node.lineno)
    return lines


def test_the_detector_sees_the_shape_it_forbids() -> None:
    assert _bare_0o700_mkdirs(ast.parse("p.mkdir(parents=True, exist_ok=True, mode=0o700)")) == [1]
    assert _bare_0o700_mkdirs(ast.parse("make_user_dir(p)\np.mkdir(exist_ok=True)")) == []


def test_no_bare_0o700_mkdir_outside_winsec() -> None:
    files = [p for p in SRC.rglob("*.py") if p not in ALLOWED]
    assert len(files) > 200, f"non-vacuity: scanned only {len(files)} files under {SRC}"
    hits = [
        f"{p.relative_to(SRC.parent)}:{line}"
        for p in files
        for line in _bare_0o700_mkdirs(ast.parse(p.read_text(encoding="utf-8"), filename=str(p)))
    ]
    assert not hits, "use nexus._winsec.make_user_dir (nexus-f9bgu.50): " + ", ".join(hits)
