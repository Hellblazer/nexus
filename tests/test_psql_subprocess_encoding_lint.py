# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Ratchet: every text-mode psql call in the tests names its encoding (nexus-niyl5).

``tests/conftest.py`` sets ``PGCLIENTENCODING=UTF8`` for the whole suite, so a psql child reads its stdin and writes
its pipes as UTF-8. The test's half of that pair is ``encoding="utf-8"`` at the call: with ``text=True`` and no
encoding, Python encodes ``input=`` with the locale's encoding, cp1252 on an English Windows install, and PG refuses
the non-ASCII bytes (``invalid byte sequence for encoding "UTF8": 0xe9``, the P3.6 native-Windows run).

The shipped-code lint, ``tests/test_subprocess_text_encoding_lint.py``, covers only the tests in the Windows CI set.
This one covers every file under ``tests/``, and only calls whose own source names psql: a call that takes its argv
from a variable is not seen. The scan reports how many calls it examined, and fails when that is zero.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_CALLS: frozenset[str] = frozenset({"run", "Popen", "check_output", "check_call", "call"})


def _findings(src: str, rel: str) -> tuple[int, list[str]]:
    """Return (psql calls examined, ``rel:line`` of each text-mode one with no ``encoding``)."""
    tree = ast.parse(src)
    examined = 0
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in _CALLS or "psql" not in (ast.get_source_segment(src, node) or ""):
            continue
        examined += 1
        kws = {k.arg for k in node.keywords if k.arg}
        text_mode = bool({"text", "universal_newlines", "errors"} & kws)
        if text_mode and "encoding" not in kws:
            bad.append(f"{rel}:{node.lineno}")
    return examined, bad


def test_detector_recognises_the_shapes_it_claims_to() -> None:
    src = (
        "import subprocess\n"
        "subprocess.run(['psql', '-c', q], text=True)\n"  # 2: bad
        "subprocess.run([pg / 'psql'], input=s, capture_output=True, text=True, encoding='utf-8')\n"  # 3: good
        "subprocess.check_output([str(b / 'psql')], universal_newlines=True)\n"  # 4: bad
        "subprocess.run(['psql', '-c', q], capture_output=True)\n"  # 5: bytes mode, good
        "subprocess.run(['git', 'log'], text=True)\n"  # 6: not psql, not examined
        "run(['psql'], errors='replace')\n"  # 7: bad
    )
    examined, bad = _findings(src, "x.py")
    assert examined == 5
    assert bad == ["x.py:2", "x.py:4", "x.py:7"]


def test_every_text_mode_psql_call_in_the_tests_names_its_encoding() -> None:
    examined = 0
    bad: list[str] = []
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        if "psql" not in src:
            continue
        n, found = _findings(src, path.relative_to(REPO_ROOT).as_posix())
        examined += n
        bad += found
    print(f"psql encoding lint: {examined} psql calls examined")
    assert examined >= 40, f"the scan examined {examined} psql calls; the walk or the detector is broken"
    assert not bad, "text-mode psql calls with no encoding= (add encoding=\"utf-8\"):\n" + "\n".join(bad)
