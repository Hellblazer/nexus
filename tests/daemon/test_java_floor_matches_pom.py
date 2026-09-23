# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""The JVM-fallback error's Java floor must match ``service/pom.xml``.

``_resolve_java_executable`` tells a user with no JVM which JDK to install.
It said ">= 21" while ``service/pom.xml`` set ``maven.compiler.release`` to
25 (nexus-ijue9.9, RDR-218 Phase 3). A user who follows a wrong floor installs
a JDK that cannot run the jar and gets a SECOND, less legible failure — a
class-file-version error from the JVM rather than an actionable message from
nexus.

WHY THIS IS A TEST AND NOT JUST A CORRECTED NUMBER. A string that restates a
build-file fact drifts, and this one already had. The wheel does NOT ship
``service/pom.xml``, so the runtime cannot read the pom and the constant has
to exist in Python; what keeps the two honest is this test, which reads the
pom and never types the number.

It drives the REAL function rather than asserting on the constant, because a
test that compares a constant to the pom would pass while the message
interpolated something else entirely.
"""

from __future__ import annotations

import pathlib
import xml.etree.ElementTree as ET

import pytest

from nexus.daemon.storage_service_daemon import (
    StorageServiceStartError,
    _resolve_java_executable,
)

_MAVEN_NS = "{http://maven.apache.org/POM/4.0.0}"
_POM = pathlib.Path(__file__).parents[2] / "service" / "pom.xml"


def _pom_release() -> int:
    """``maven.compiler.release`` from the pom's properties block.

    Read from the PROPERTY at line 13, deliberately, not from the
    maven-compiler-plugin's ``<release>``: the plugin block retypes the
    number instead of reading ``${maven.compiler.release}``, so the property
    is the source and the plugin's copy is itself drift.
    """
    root = ET.parse(_POM).getroot()
    props = root.find(f"{_MAVEN_NS}properties")
    assert props is not None, f"{_POM} has no <properties> block"
    node = props.find(f"{_MAVEN_NS}maven.compiler.release")
    assert node is not None, (
        f"{_POM} properties has no maven.compiler.release; this test cannot "
        "derive the floor and must not pass by defaulting"
    )
    text = (node.text or "").strip()
    assert text.isdigit(), (
        f"maven.compiler.release is {text!r}, not an integer; a non-numeric "
        "read must fail here rather than produce '>= None' downstream"
    )
    return int(text)


def test_pom_release_is_readable_and_plausible() -> None:
    """Non-vacuity. Without this, a failed XPath would make the pin trivially
    satisfiable by an error message containing '>= '."""
    release = _pom_release()
    assert 17 <= release <= 99, f"implausible maven.compiler.release: {release}"


def test_missing_jvm_message_names_the_pom_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """With no JVM reachable, the raised message must name the pom's floor.

    Red on the pre-fix tree, where the message says 21 and the pom says 25.
    """
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setenv(
        "PATH", str(tmp_path)
    )  # an empty dir: shutil.which finds nothing

    with pytest.raises(StorageServiceStartError) as excinfo:
        _resolve_java_executable()

    message = str(excinfo.value)
    expected = f">= {_pom_release()}"
    assert expected in message, (
        f"the JVM-fallback message does not name the pom's Java floor.\n"
        f"  expected substring: {expected!r}\n"
        f"  message: {message!r}\n"
        f"service/pom.xml maven.compiler.release is the source of truth; the "
        f"message must interpolate a constant kept equal to it."
    )


def test_the_probe_actually_removed_the_jvm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Non-vacuity for the test above.

    If `java` were still reachable the function would RETURN instead of
    raising, and `pytest.raises` would fail loudly — but only if the removal
    works. This asserts the precondition directly so a future change to how
    the lookup resolves cannot turn that test into one that never runs its
    assertion.
    """
    import shutil

    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert shutil.which("java") is None, (
        "the empty-PATH probe did not hide java, so the message assertion "
        "above would never be reached"
    )
