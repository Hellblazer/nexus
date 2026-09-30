# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-lhr6a: the native binary must embed each platform's own native
libraries exactly once, and nothing else.

Found by the 2026-09-29 native-Windows spike (qwentescence, GraalVM's
-H:+GenerateEmbeddedResourcesFile report): the Windows exe was 803 MB,
724 MB of it embedded resources, against 231 MB for linux-amd64. Three
independent causes, each pinned below:

1. Every embedded native library was embedded TWICE, on every platform.
   The shade plugin runs with shadedArtifactAttached=false, so the uber jar
   BECOMES the project artifact, and native-maven-plugin's default
   classpath is that artifact plus every dependency jar. Each library was
   then found once in the dependency jar (~/.m2) and once in
   nexus-service-1.0-SNAPSHOT.jar. The fix runs the native build before
   shade, so it sees the thin jar. native-build-tools' documented shaded-jar
   recipe (an explicit <classpath> naming the uber jar alone) also dedups,
   but it switches off the reachability-metadata repository, measured.
2. traced/reachability-metadata.json was agent-traced on a Mac, so it
   carried globs for ai/onnxruntime/native/osx-aarch64/*.dylib, and every
   linux and windows binary embedded the mac onnxruntime. Per-platform
   native libraries are selected by the pom's native-libs-* profiles only.
3. The onnxruntime include pattern was `.../${onnx.native.dir}/.*`, which on
   Windows swept in onnxruntime.pdb (290 MB of debug symbols). An added
   -H:ExcludeResources=.*\\.pdb had no measurable effect, so the include is a
   positive pattern naming the loadable library types instead of a broad
   include trimmed by excludes.

Fast (XML/JSON parse only, no build). The size outcome itself is measured
per platform by building with the embedded-resources report; see the bead.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).parent.parent
POM_PATH = ROOT / "service" / "pom.xml"
TRACED_METADATA = (
    ROOT
    / "service"
    / "src"
    / "main"
    / "resources"
    / "META-INF"
    / "native-image"
    / "traced"
    / "reachability-metadata.json"
)
NS = {"m": "http://maven.apache.org/POM/4.0.0"}

# Loadable library names under each onnx.native.dir the native-libs-*
# profiles set. Mostly what the onnxruntime 1.20.0 jar ships; the
# providers_shared entry is a plausible future file the pattern must keep.
ORT_WANTED = [
    "ai/onnxruntime/native/linux-x64/libonnxruntime.so",
    "ai/onnxruntime/native/linux-x64/libonnxruntime4j_jni.so",
    "ai/onnxruntime/native/linux-x64/libonnxruntime_providers_shared.so",
    "ai/onnxruntime/native/linux-aarch64/libonnxruntime.so",
    "ai/onnxruntime/native/osx-aarch64/libonnxruntime.dylib",
    "ai/onnxruntime/native/osx-aarch64/libonnxruntime4j_jni.dylib",
    "ai/onnxruntime/native/win-x64/onnxruntime.dll",
    "ai/onnxruntime/native/win-x64/onnxruntime4j_jni.dll",
]
ORT_UNWANTED = [
    "ai/onnxruntime/native/win-x64/onnxruntime.pdb",
    "ai/onnxruntime/native/win-x64/onnxruntime4j_jni.pdb",
    "ai/onnxruntime/native/osx-aarch64/libonnxruntime.dylib.dSYM/"
    "Contents/Resources/DWARF/libonnxruntime.dylib",
    "ai/onnxruntime/native/osx-aarch64/libonnxruntime.dylib.dSYM/Contents/Info.plist",
]


def _native_plugin_config(root: ET.Element) -> ET.Element:
    """The shared `native` profile's native-maven-plugin <configuration>."""
    for profile in root.findall("./m:profiles/m:profile", NS):
        if profile.findtext("m:id", namespaces=NS) != "native":
            continue
        for plugin in profile.findall(".//m:plugin", NS):
            if plugin.findtext("m:artifactId", namespaces=NS) == "native-maven-plugin":
                config = plugin.find(".//m:execution/m:configuration", NS)
                assert config is not None, "native-maven-plugin execution has no <configuration>"
                return config
    raise AssertionError(f"no native-maven-plugin in the `native` profile of {POM_PATH}")


def _ort_include_pattern(config: ET.Element) -> str:
    prefix = "-H:IncludeResources=ai/onnxruntime/native/"
    matches = [
        (el.text or "").strip()
        for el in config.findall(".//m:buildArg", NS)
        if (el.text or "").strip().startswith(prefix)
    ]
    assert len(matches) == 1, f"expected exactly one onnxruntime IncludeResources buildArg, got {matches}"
    return matches[0].removeprefix("-H:IncludeResources=")


def test_native_build_runs_before_shade() -> None:
    """The native build must see the THIN project jar. Both executions bind to
    `package`, where Maven runs them in POM order; a profile's execution merges
    into the main build's declaration of the same plugin and takes its place.
    Declaring native-maven-plugin ahead of maven-shade-plugin therefore runs
    compile-no-fork after jar:jar and before shade replaces the project
    artifact with the uber jar."""
    root = ET.parse(POM_PATH).getroot()
    order = [
        plugin.findtext("m:artifactId", namespaces=NS)
        for plugin in root.findall("./m:build/m:plugins/m:plugin", NS)
    ]
    assert "native-maven-plugin" in order and "maven-shade-plugin" in order, (
        f"main <build><plugins> must declare both native-maven-plugin and "
        f"maven-shade-plugin, got {order}"
    )
    assert order.index("native-maven-plugin") < order.index("maven-shade-plugin"), (
        f"native-maven-plugin is declared after maven-shade-plugin in {order}: the "
        f"native build then sees the uber jar AND every dependency jar and embeds each "
        f"native library twice (nexus-lhr6a)"
    )


def test_jar_is_rebuilt_on_every_package() -> None:
    """Ordering alone is not enough on an incremental build. Shade leaves the
    uber jar at the thin jar's path (shadedArtifactAttached=false), and on the
    next `package` without `clean` jar:jar treats that file as up to date and
    skips, so the native build would see the uber jar again and embed every
    library twice, silently. forceCreation makes jar:jar rewrite the thin jar
    every time (code review of nexus-lhr6a; build-artifacts.sh runs a JVM
    package and then -Pnative package on the same tree)."""
    root = ET.parse(POM_PATH).getroot()
    jars = [
        plugin
        for plugin in root.findall("./m:build/m:plugins/m:plugin", NS)
        if plugin.findtext("m:artifactId", namespaces=NS) == "maven-jar-plugin"
    ]
    assert len(jars) == 1, "main <build><plugins> must declare maven-jar-plugin once"
    assert jars[0].findtext("m:configuration/m:forceCreation", namespaces=NS) == "true", (
        "maven-jar-plugin needs <forceCreation>true</forceCreation>: otherwise an "
        "incremental -Pnative package sees the previous build's uber jar (nexus-lhr6a)"
    )


def test_native_classpath_is_not_overridden() -> None:
    """An explicit <classpath> also removes the duplicates, but it switches off
    the GraalVM reachability-metadata repository: measured on mac and windows,
    its 29 'metadata repository for' lines went to 0 and ~4,000 compilation
    units disappeared (nexus-lhr6a). Checked across the whole pom: main build
    and every profile, plugin level and execution level."""
    root = ET.parse(POM_PATH).getroot()
    overridden = [
        plugin
        for plugin in root.iter(f"{{{NS['m']}}}plugin")
        if plugin.findtext("m:artifactId", namespaces=NS) == "native-maven-plugin"
        and plugin.find(".//m:classpath", NS) is not None
    ]
    assert not overridden, (
        "a native-maven-plugin declaration carries a <classpath> override, which "
        "disables the reachability-metadata repository; order the plugin before "
        "shade instead"
    )


@pytest.mark.parametrize("resource", sorted(ORT_WANTED))
def test_ort_include_keeps_loadable_libraries(resource: str) -> None:
    pattern = _ort_include_pattern(_native_plugin_config(ET.parse(POM_PATH).getroot()))
    platform = resource.split("/")[3]
    regex = pattern.replace("${onnx.native.dir}", platform)
    assert re.fullmatch(regex, resource), f"{regex!r} no longer embeds {resource}"


@pytest.mark.parametrize("resource", sorted(ORT_UNWANTED))
def test_ort_include_drops_debug_symbols(resource: str) -> None:
    pattern = _ort_include_pattern(_native_plugin_config(ET.parse(POM_PATH).getroot()))
    platform = resource.split("/")[3]
    regex = pattern.replace("${onnx.native.dir}", platform)
    assert not re.fullmatch(regex, resource), (
        f"{regex!r} embeds debug symbols {resource} (onnxruntime.pdb alone is 290 MB)"
    )


def test_traced_metadata_names_no_platform_native_library() -> None:
    data = json.loads(TRACED_METADATA.read_text())
    globs = [entry["glob"] for entry in data.get("resources", []) if "glob" in entry]
    assert len(globs) > 50, (
        f"read only {len(globs)} resource globs from {TRACED_METADATA.name}; the "
        f"schema changed and this check would pass vacuously"
    )
    platform_lib = re.compile(r"^(ai/onnxruntime/native/|native/lib/)[^/]+/")
    offenders = sorted(glob for glob in globs if platform_lib.match(glob))
    assert not offenders, (
        f"{TRACED_METADATA.name} names per-platform native libraries {offenders}; "
        f"it is traced on one host, so these get embedded into every other "
        f"platform's binary (the mac onnxruntime in linux and windows, nexus-lhr6a). "
        f"The native-libs-* pom profiles select each platform's own libraries."
    )
