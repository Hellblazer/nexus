# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the relocatable build directory in ``service/pom.xml`` (nexus-0a1hw).

A gate-jar miss wrote about 0.5 GB to a lab host's NVMe because Maven's whole build
directory lives in ``service/target``. ``scripts/build-gate-jar.sh`` moves it to tmpfs
with ``-Dnexus.build.dir=<dir>``, and that only works while three things stay true:

* ``<build><directory>`` is ``${nexus.build.dir}`` and the property's default is
  ``${project.basedir}/target`` (an IDE, a bare ``mvn`` and every CI job keep ``target``);
* nothing in the pom spells ``target`` as a build output path outside that property,
  so every output (the jOOQ trees, the stamps, the jlink image) follows the relocation;
* the jlink-image execution is skippable through ``nexus.jlink.skip``, which the script
  sets, and that property defaults to ``false`` so a release build still gets the image.

The script side (what it passes, what lands in ``service/target``) is
``scripts/build-gate-jar_test.sh``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_REPO = Path(__file__).resolve().parents[1]
_POM = _REPO / "service" / "pom.xml"
_NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def _root() -> ET.Element:
    return ET.parse(_POM).getroot()


def _prop(name: str) -> str | None:
    el = _root().find(f"m:properties/m:{name}", _NS)
    return None if el is None else (el.text or "").strip()


def test_build_directory_is_the_relocatable_property() -> None:
    build = _root().find("m:build", _NS)
    assert build is not None
    directory = build.find("m:directory", _NS)
    assert directory is not None and (directory.text or "").strip() == "${nexus.build.dir}", (
        "<build><directory> must be ${nexus.build.dir}: build-gate-jar.sh relocates the "
        "build with -Dnexus.build.dir (nexus-0a1hw)"
    )


def test_the_default_build_directory_is_still_target() -> None:
    assert _prop("nexus.build.dir") == "${project.basedir}/target", (
        "the default must stay ${project.basedir}/target: an IDE, a bare mvn and CI read service/target"
    )


def test_no_output_path_in_the_pom_bypasses_the_property() -> None:
    """Every executable path goes through ``${project.build.directory}`` or a property that does.
    A literal ``${project.basedir}/target`` or ``${basedir}/target`` would stay on disk when the build
    is relocated (comments and the property's own default excepted)."""
    text = re.sub(r"<!--.*?-->", "", _POM.read_text(encoding="utf-8"), flags=re.S)
    default = "<nexus.build.dir>${project.basedir}/target</nexus.build.dir>"
    assert default in text
    rest = text.replace(default, "")
    offenders = re.findall(r"\$\{(?:project\.)?basedir\}/target", rest)
    assert not offenders, f"output paths that ignore nexus.build.dir: {offenders}"


def test_the_jlink_image_is_skippable_and_on_by_default() -> None:
    assert _prop("nexus.jlink.skip") == "false"
    antrun = next(
        p
        for p in _root().findall("m:build/m:plugins/m:plugin", _NS)
        if (p.findtext("m:artifactId", namespaces=_NS) or "") == "maven-antrun-plugin"
    )
    jlink = next(
        e
        for e in antrun.findall("m:executions/m:execution", _NS)
        if e.findtext("m:id", namespaces=_NS) == "jlink-image"
    )
    skip = jlink.find("m:configuration/m:skip", _NS)
    assert skip is not None and (skip.text or "").strip() == "${nexus.jlink.skip}"


def test_build_gate_jar_passes_what_the_pom_reads() -> None:
    """The two properties the script sets are the two the pom defines."""
    script = (_REPO / "scripts" / "build-gate-jar.sh").read_text(encoding="utf-8")
    assert "-Dnexus.build.dir=" in script
    assert "-Dnexus.jlink.skip=true" in script
