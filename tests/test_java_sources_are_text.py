# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Every Java source under ``service/src`` is text: no raw NUL bytes.

nexus-eeuyg: ``AspectRepository.java`` embedded a literal NUL inside a
string-literal map key (``collection + NUL + source_path``). ``file(1)``
reported the 1950-line source as ``data`` and ``grep -I`` treated it as
binary, so every grep-based audit over the engine tree silently returned
zero hits for that file. The fix is the ``\\u0000`` escape (identical
runtime bytes, text source). This lint keeps the tree greppable: a raw NUL
in any Java source is a defect regardless of intent.

Non-vacuity: the scan must see a realistic number of sources; an empty walk
is a failure, not a pass.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
_JAVA_ROOT = REPO_ROOT / "service" / "src"
_MIN_SOURCES = 100


def test_no_java_source_contains_a_raw_nul_byte() -> None:
    sources = sorted(_JAVA_ROOT.rglob("*.java"))
    assert len(sources) >= _MIN_SOURCES, f"scan looks broken: {len(sources)} Java sources under {_JAVA_ROOT}"
    offenders = [
        str(p.relative_to(REPO_ROOT)) for p in sources if b"\x00" in p.read_bytes()
    ]
    assert not offenders, (
        "raw NUL byte(s) in Java source — file(1)/grep -I treat the whole file as "
        f"binary and every grep audit silently zero-hits it (nexus-eeuyg); use the "
        f"\\u0000 escape instead: {offenders}"
    )
