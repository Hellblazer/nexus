# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every e2e harness install of the PDF stack carries ``--overrides`` (nexus-11x9o).

The release's install overrides (``src/nexus/_install/overrides.txt``) keep one
OpenCV: ``opencv-python`` stays out, ``opencv-python-headless`` stays in. uv reads
overrides from the command line or the invoking project, never from an installed
wheel, so a harness that runs a bare ``uv tool install "conexus[pdf]"`` lands both
builds in one ``cv2/``, and on a Linux box without libGL.so.1 the formula-PDF leg
dies on ``import cv2``. The 2026-10-25 shakedown of 7.86.0 hit exactly that, and the
published-artifact gate had never exercised the route users take.

A pdf-capable install here is a ``uv tool|pip install`` statement (continuations
joined, comments dropped) that names ``[pdf]`` or the harness's ``$PKG_SPEC``
(``conexus[pdf]`` under NX_MVV_FORMULA_PDF_CHECK=1). A ``--no-deps`` fetch resolves
no OpenCV, so it is exempt. ``nx self install --extras pdf`` is not a ``uv``
statement: install_generation.sh applies the overrides on that route.
"""
from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.lint

_E2E = pathlib.Path(__file__).parent / "e2e"

_STATEMENT_RE = re.compile(r"^\s*(?:env\s+\S+\s+)*(?:_uv_\w+|uv)\s+(?:tool|pip)\s+install\b")
_PDF_SPEC_RE = re.compile(r"\[pdf\]|\$PKG_SPEC|\$\{PKG_SPEC\}")

#: Non-vacuity: the MVV's registry install is the one pdf-capable uv install in
#: tests/e2e today. A refactor that moves it must notice this test, not pass with zero.
_MIN_PDF_INSTALLS = 1


def pdf_installs(text: str) -> list[str]:
    """The pdf-capable ``uv tool|pip install`` statements in a shell script."""
    joined = re.sub(r"\\\n\s*", " ", text)
    return [
        line.strip()
        for line in joined.splitlines()
        if not line.lstrip().startswith("#")
        and _STATEMENT_RE.match(line)
        and _PDF_SPEC_RE.search(line)
        and "--no-deps" not in line
    ]


def missing_overrides(text: str) -> list[str]:
    return [stmt for stmt in pdf_installs(text) if "--overrides" not in stmt]


def test_every_e2e_pdf_install_carries_overrides() -> None:
    scripts = sorted(_E2E.rglob("*.sh"))
    assert scripts, f"no shell scripts under {_E2E}: the lint examined nothing"
    found: list[tuple[pathlib.Path, str]] = []
    bad: list[tuple[pathlib.Path, str]] = []
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        found += [(path, s) for s in pdf_installs(text)]
        bad += [(path, s) for s in missing_overrides(text)]
    assert len(found) >= _MIN_PDF_INSTALLS, (
        f"examined {len(scripts)} scripts and found {len(found)} pdf-capable installs; "
        "the harness's install moved or this lint reads nothing"
    )
    assert not bad, "a pdf install without --overrides lands two OpenCV builds:\n" + "\n".join(
        f"  {p.relative_to(_E2E.parent)}: {s}" for p, s in bad
    )


def test_the_lint_catches_a_copy_with_the_flag_removed() -> None:
    """Falsification control: take the real harness, strip every ``--overrides FILE``,
    and the lint's own function must report the install."""
    text = (_E2E / "fresh-install-mvv.sh").read_text(encoding="utf-8")
    assert missing_overrides(text) == [], "the real harness should be clean"
    stripped = re.sub(r' --overrides "\$OVERRIDES_FILE"', "", text)
    assert stripped != text, "control setup failed: nothing was stripped"
    assert len(missing_overrides(stripped)) == 1


@pytest.mark.parametrize(
    "line, expected",
    [
        ('uv tool install --python 3.12 "conexus[pdf]==1.0"', 1),
        ('_uv_sandboxed pip install --python "$V" "$PKG_SPEC"', 1),
        ('uv tool install --python 3.12 --overrides o.txt "conexus[pdf]==1.0"', 0),
        ('_uv_sandboxed pip install --no-deps --target d "$PKG_BASE_SPEC"', 0),
        ('_uv_sandboxed pip install --dry-run --no-deps --python "$V" "$PKG_SPEC"', 0),
        ('# uv tool install "conexus[pdf]"', 0),
        ('_fail "uv tool install $PKG_SPEC failed"', 0),
        ('uv tool install --python 3.12 "conexus==1.0"', 0),
    ],
)
def test_statement_classification(line: str, expected: int) -> None:
    assert len(missing_overrides(line + "\n")) == expected
