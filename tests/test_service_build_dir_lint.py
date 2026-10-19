# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the relocatable build directory in ``service/pom.xml`` (nexus-0a1hw).

A gate-jar miss wrote 316 MiB to chas's NVMe because Maven's whole build directory
lives in ``service/target``. ``scripts/build-gate-jar.sh`` moves the build directory to
tmpfs with ``-Dnexus.build.dir=<dir>`` (saving the ~15-35 MiB of Maven output other than
the stamped jar) and skips the jlink image (~83 MiB). The relocation only works while
three things stay true:

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


_DEFAULT = "<nexus.build.dir>${project.basedir}/target</nexus.build.dir>"
# An absolute spelling of target, or a relative one used as an Ant ``dir``/``file``/
# ``todir``/``tofile``/``srcfile``/``destdir`` attribute (relative to the Ant base dir,
# which is the module, so it would stay on disk when the build is relocated).
_BYPASS = re.compile(
    r"\$\{(?:project\.)?basedir\}/target"
    r"|\b(?:dir|file|todir|tofile|srcfile|srcdir|destdir|destfile)\s*=\s*[\"'](?:\./)?target(?:/|[\"'])"
)


def _bypassing_paths(pom_text: str) -> list[str]:
    """Output paths in executable pom text (comments and the property's own default
    excepted) that name ``target`` instead of going through ``${project.build.directory}``."""
    text = re.sub(r"<!--.*?-->", "", pom_text, flags=re.S)
    return _BYPASS.findall(text.replace(_DEFAULT, ""))


def test_no_output_path_in_the_pom_bypasses_the_property() -> None:
    """Every executable path goes through ``${project.build.directory}`` or a property that does.
    A literal ``${project.basedir}/target``, ``${basedir}/target`` or a relative ``dir="target/..."``
    would stay on disk when the build is relocated (comments and the property's own default excepted)."""
    text = _POM.read_text(encoding="utf-8")
    assert _DEFAULT in re.sub(r"<!--.*?-->", "", text, flags=re.S)
    offenders = _bypassing_paths(text)
    assert not offenders, f"output paths that ignore nexus.build.dir: {offenders}"


@pytest.mark.parametrize(
    "snippet",
    [
        '<delete dir="${project.basedir}/target/x"/>',
        '<delete dir="${basedir}/target/x"/>',
        '<delete dir="target/jlink"/>',
        '<mkdir dir="./target/jooq"/>',
        "<delete dir='target'/>",
        '<copy file="target/a.txt" tofile="${x}"/>',
        '<copy file="${x}" tofile="target/a.txt"/>',
    ],
)
def test_the_bypass_lint_sees_each_spelling(snippet: str) -> None:
    """The lint above must fail on a bypass, or it proves nothing about the real pom."""
    assert _bypassing_paths(f"<project>{snippet}</project>"), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        '<delete dir="${project.build.directory}/jlink"/>',
        '<delete dir="${nexus.stamp.dir}"/>',
        "<!-- <delete dir=\"target/x\"/> -->",
        '<delete dir="src/target-like/x"/>',
        '<fileset dir="${project.basedir}/src/main"/>',
    ],
)
def test_the_bypass_lint_passes_the_clean_spellings(snippet: str) -> None:
    assert not _bypassing_paths(f"<project>{snippet}</project>"), snippet


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


def _shell_code(text: str) -> str:
    """The script with comment lines removed: a header that mentions a flag is not the code passing it."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_shell_code_strips_comments_only() -> None:
    sample = "# -Dnexus.build.dir=x in a comment\n    # also this\nmvn_extra+=(-Dnexus.jlink.skip=true)  # trailing\n"
    code = _shell_code(sample)
    assert "-Dnexus.build.dir" not in code and "-Dnexus.jlink.skip=true" in code


def test_build_gate_jar_passes_what_the_pom_reads() -> None:
    """The two properties the script sets are the two the pom defines, as CODE (a header that
    only mentions them does not count)."""
    code = _shell_code((_REPO / "scripts" / "build-gate-jar.sh").read_text(encoding="utf-8"))
    assert re.search(r"""["']-Dnexus\.build\.dir=\$\{?BUILD_TMPFS_DIR\}?["']""", code), (
        "build-gate-jar.sh must pass -Dnexus.build.dir=$BUILD_TMPFS_DIR to Maven"
    )
    assert re.search(r"mvn_extra=\(\s*-Dnexus\.jlink\.skip=true\s*\)", code), (
        "build-gate-jar.sh must start its Maven arguments with -Dnexus.jlink.skip=true"
    )
    assert _prop("nexus.build.dir") is not None and _prop("nexus.jlink.skip") is not None
