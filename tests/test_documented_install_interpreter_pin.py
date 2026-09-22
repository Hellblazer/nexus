# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The documented first-install command names its interpreter, and the gate
that proves that command runs the same one (nexus-sa187).

THE BUG. Ubuntu 26.04 LTS ships CPython 3.14 as ``/usr/bin/python3``. On a
box with no other interpreter, ``uv tool install conexus`` picks it, and the
resolve fails outright -- the torch pin (``>=2.8,<2.9``) has no ``cp314``
wheels. Measured 2026-09-21 on qwentescence's WSL2 Ubuntu 26.04.1; the same
resolution reproduces offline with
``uv pip compile --universal --python-version 3.14``. Every fresh install on
the current Linux LTS hit it, Windows or not.

WHY NO GATE SAW IT. ``tests/e2e/fresh-install-mvv.sh --published`` proves the
uv-tool RESOLUTION layer, which is the right layer -- but it passes
``--python 3.12`` explicitly, while README told the user to pass nothing. The
interpreter was the uncontrolled variable, so the gate's domain never
contained the documented command.

THIS LINT is the structural half of the fix. The docs now name the
interpreter, which is what makes the ambient ``python3`` stop mattering, and
this pins the three facts that have to move together: what README tells a
user to type, what the site tells a user to type, and which interpreter the
MVV's published-install leg actually exercises. It also checks the pin
against ``requires-python``, so raising the floor cannot silently leave the
docs recommending an interpreter the package no longer admits.
"""
from __future__ import annotations

import pathlib
import re
import tomllib

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

pytestmark = pytest.mark.lint

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_README = _REPO_ROOT / "README.md"
_SITE = _REPO_ROOT / "web" / "index.html"
_MVV = _REPO_ROOT / "tests" / "e2e" / "fresh-install-mvv.sh"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

#: The documented install command, wherever it is written for a user to type.
#: The ``--python`` flag is required by construction: a match without it is
#: not a match, so a reverted flag reads as "command missing", not as a pass.
_INSTALL_RE = re.compile(r"uv tool install conexus --python (\d+\.\d+)")

#: The MVV's published-install leg, the gate that resolves against real PyPI.
_MVV_RE = re.compile(r"tool install --python (\d+\.\d+) \"\$PKG_SPEC\"")

#: Non-vacuity floors: the count of typed-by-a-user install commands each
#: surface carries today. A refactor that moves or reworks these blocks must
#: notice this test rather than pass with zero matches.
_MIN_README_OCCURRENCES = 1
_MIN_SITE_OCCURRENCES = 2


def _pins(path: pathlib.Path, pattern: re.Pattern[str]) -> list[str]:
    return pattern.findall(path.read_text(encoding="utf-8"))


def _readme_pin() -> str:
    """The interpreter README tells a user to install under.

    Every other check here compares against this one, so it fails with the
    named reason rather than an ``IndexError`` when the pin is gone.
    """
    pins = _pins(_README, _INSTALL_RE)
    assert pins, "README.md names no interpreter; see the failure below it."
    return pins[0]


def test_readme_install_command_names_an_interpreter() -> None:
    pins = _pins(_README, _INSTALL_RE)
    assert len(pins) >= _MIN_README_OCCURRENCES, (
        "README.md carries no `uv tool install conexus --python X.Y`. Either "
        "the interpreter pin was dropped -- which puts every fresh install on "
        "a 3.14-default distro back on the nexus-sa187 resolver failure -- or "
        "the install section moved and this lint now reads nothing."
    )
    assert len(set(pins)) == 1, f"README.md names more than one interpreter: {sorted(set(pins))}"


def test_site_install_commands_name_the_same_interpreter_as_readme() -> None:
    site_pins = _pins(_SITE, _INSTALL_RE)
    assert len(site_pins) >= _MIN_SITE_OCCURRENCES, (
        f"web/index.html carries {len(site_pins)} pinned install commands, "
        f"expected at least {_MIN_SITE_OCCURRENCES} (the plugin flow and the "
        "Claude Desktop flow each have one)."
    )
    readme_pin = _readme_pin()
    assert set(site_pins) == {readme_pin}, (
        f"web/index.html names {sorted(set(site_pins))}, README.md names "
        f"{readme_pin}. The site and the README are the same instruction to "
        "the same user; they cannot disagree about which interpreter to use."
    )


def test_mvv_published_leg_exercises_the_documented_interpreter() -> None:
    mvv_pins = _pins(_MVV, _MVV_RE)
    assert len(mvv_pins) == 1, (
        f"fresh-install-mvv.sh has {len(mvv_pins)} published-install legs "
        "matching the expected shape; this lint cannot tell which interpreter "
        "the gate runs."
    )
    readme_pin = _readme_pin()
    assert mvv_pins[0] == readme_pin, (
        f"The MVV's published-install leg runs Python {mvv_pins[0]} while the "
        f"documented command says {readme_pin}. That gap IS nexus-sa187: the "
        "gate proves an install nobody was told to run."
    )


def test_the_documented_interpreter_is_one_the_package_admits() -> None:
    with _PYPROJECT.open("rb") as handle:
        requires_python = tomllib.load(handle)["project"]["requires-python"]
    pin = _readme_pin()
    # A bare "3.12" is not a release; check the floor of that series, which
    # is what uv resolves the request to.
    assert Version(f"{pin}.0") in SpecifierSet(requires_python), (
        f"The docs tell users to install under Python {pin}, which "
        f"requires-python ({requires_python}) does not admit."
    )
