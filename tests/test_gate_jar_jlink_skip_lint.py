# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The gate-jar build skips the jlink image (nexus-0a1hw).

``scripts/build-gate-jar.sh`` passes ``-Dnexus.jlink.skip=true`` so a gate-jar
miss does not write the jlink image nothing reads. The pom property defaults to
``false`` so an IDE, a bare ``mvn``, CI and the engine release build keep the
image, and the ``jlink-image`` execution honours the property.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
_POM = (REPO_ROOT / "service" / "pom.xml").read_text(encoding="utf-8")
_SCRIPT = (REPO_ROOT / "scripts" / "build-gate-jar.sh").read_text(encoding="utf-8")


def test_pom_default_keeps_the_image() -> None:
    m = re.search(r"<nexus\.jlink\.skip>([^<]*)</nexus\.jlink\.skip>", _POM)
    assert m, "service/pom.xml lost the nexus.jlink.skip property"
    assert m.group(1).strip() == "false"


def test_jlink_image_execution_honours_the_property() -> None:
    m = re.search(r"<id>jlink-image</id>(.*?)</execution>", _POM, re.S)
    assert m, "jlink-image execution not found"
    assert "<skip>${nexus.jlink.skip}</skip>" in m.group(1)


def test_gate_jar_script_passes_the_skip_on_its_mvn_command() -> None:
    # The build is launched through leased_build_run (nexus-t6t9f), so the command is
    # `leased_build_run "$lab_class" ./mvnw ... package ...`; a bare ./mvnw line counts too.
    cmds = [ln for ln in _SCRIPT.splitlines()
            if re.match(r'\s*(?:leased_build_run "\$lab_class" )?\./mvnw\b', ln) and "package" in ln]
    assert len(cmds) == 1, cmds  # non-vacuity: exactly one build command
    assert "-Dnexus.jlink.skip=true" in cmds[0]
