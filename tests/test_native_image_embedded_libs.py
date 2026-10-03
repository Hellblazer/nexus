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
   -H:ExcludeResources=.*\\.pdb had no effect; nexus-vwfc0 measured why: on
   Windows a backslash in a pom <buildArg> broke the exclude (mechanism unknown)
   (`.*[.]pdb` excluded both .pdb files, exe 143 MB; `.*\\.pdb`, one arg
   changed, did not, 435 MB). The include is a positive pattern naming the
   loadable library types, and no buildArg may contain a backslash.

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


# A glob names a platform's native library when it has a native-library
# suffix, or a directory segment that is a platform name (darwin, osx, macos,
# linux, win, win32, win64, windows, with -aarch64 / -x86-64 / -x64 style
# variants), or sits under the per-library platform paths.
_NATIVE_SUFFIX = re.compile(r"\.(?:so(?:\.\d+)*|dylib|jnilib|dll)$", re.IGNORECASE)
_PLATFORM_SEGMENT = re.compile(
    r"^(?:darwin|osx|macos|linux|win|win32|win64|windows)(?:[-_][A-Za-z0-9_]+)*$", re.IGNORECASE
)


def _names_platform_native_library(glob: str) -> bool:
    if _NATIVE_SUFFIX.search(glob):
        return True
    if any(_PLATFORM_SEGMENT.match(segment) for segment in glob.split("/")[:-1]):
        return True
    return bool(re.match(r"^(ai/onnxruntime/native/|native/lib/)[^/]+/", glob))


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
    every time (code review of nexus-lhr6a; a JVM package and then a -Pnative package
    can run on the same tree)."""
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


def _plugin_executions(root: ET.Element, artifact_id: str) -> list[tuple[str, ET.Element]]:
    """(profile id or "main", <execution>) for every execution of the plugin
    anywhere in the pom: main build, pluginManagement, every profile."""
    found: list[tuple[str, ET.Element]] = []
    scopes = [("main", root)] + [
        (profile.findtext("m:id", namespaces=NS) or "?", profile)
        for profile in root.findall("./m:profiles/m:profile", NS)
    ]
    for scope, element in scopes:
        for plugin in element.iter(f"{{{NS['m']}}}plugin"):
            if plugin.findtext("m:artifactId", namespaces=NS) != artifact_id:
                continue
            for execution in plugin.findall("./m:executions/m:execution", NS):
                found.append((scope, execution))
    return found


def test_native_and_shade_executions_bind_to_package() -> None:
    """test_native_build_runs_before_shade pins DECLARATION order, which only
    decides the order of executions bound to the SAME phase. Moving either
    execution to another phase (or a profile re-binding build-native) passes
    that test and silently re-doubles every native library, so the phases
    themselves are pinned (nexus-vwfc0)."""
    root = ET.parse(POM_PATH).getroot()
    native = _plugin_executions(root, "native-maven-plugin")
    build_native = [(scope, ex) for scope, ex in native if ex.findtext("m:id", namespaces=NS) == "build-native"]
    assert any(scope == "native" for scope, _ in build_native), (
        "the `native` profile must declare the build-native execution"
    )
    for scope, ex in build_native:
        phase = ex.findtext("m:phase", namespaces=NS)
        # A profile execution that combines into build-native and names no
        # phase inherits it; the one that binds it must say package.
        assert phase in (None, "package"), (
            f"build-native in {scope!r} binds to phase {phase!r}; it must be `package`, "
            f"the phase maven-shade-plugin runs in, or declaration order stops meaning anything"
        )
    binding = [ex for scope, ex in build_native if scope == "native"][0]
    assert binding.findtext("m:phase", namespaces=NS) == "package"
    shade = _plugin_executions(root, "maven-shade-plugin")
    assert shade, "maven-shade-plugin has no execution"
    for scope, ex in shade:
        assert ex.findtext("m:phase", namespaces=NS) == "package", (
            f"maven-shade-plugin execution in {scope!r} is not bound to `package`"
        )


def test_jar_force_creation_is_never_overridden_off() -> None:
    """test_jar_is_rebuilt_on_every_package checks the main build's value. A
    profile or pluginManagement <configuration> (plugin level or execution
    level) setting forceCreation=false would win at combine time and pass that
    test while the incremental build sees the previous uber jar again. Every
    forceCreation anywhere in the pom must be `true` (nexus-vwfc0)."""
    root = ET.parse(POM_PATH).getroot()
    values = [
        (el.text or "").strip()
        for plugin in root.iter(f"{{{NS['m']}}}plugin")
        if plugin.findtext("m:artifactId", namespaces=NS) == "maven-jar-plugin"
        for el in plugin.iter(f"{{{NS['m']}}}forceCreation")
    ]
    assert values, "no forceCreation found for maven-jar-plugin; the pom shape changed"
    assert all(v == "true" for v in values), f"maven-jar-plugin forceCreation values: {values}"


def test_no_build_arg_contains_a_backslash() -> None:
    """Measured on the Windows native build (nexus-vwfc0): with only one
    argument changed, -H:ExcludeResources=.*[.]pdb excluded both .pdb files
    (exe 143 MB) while -H:ExcludeResources=.*\\.pdb did not (435 MB). A
    backslash in a <buildArg> broke an exclude on Windows (measured; mechanism unknown), so
    the pattern silently matches nothing. Write a literal dot as [.] (a
    character class) and never put a backslash in any buildArg, in any
    profile."""
    root = ET.parse(POM_PATH).getroot()
    offenders = [
        (el.text or "").strip()
        for el in root.iter(f"{{{NS['m']}}}buildArg")
        if "\\" in (el.text or "")
    ]
    assert not offenders, (
        f"buildArg(s) containing a backslash: {offenders}. A backslash broke an "
        f"exclude on Windows (measured: .*[.]pdb excluded the .pdb "
        f"files, .*\\.pdb did not); use the [.] character-class form instead"
    )
    assert len(list(root.iter(f"{{{NS['m']}}}buildArg"))) > 10, (
        "found almost no buildArg elements; the pom shape changed and this check would pass vacuously"
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
    offenders = sorted(glob for glob in globs if _names_platform_native_library(glob))
    assert not offenders, (
        f"{TRACED_METADATA.name} names per-platform native libraries {offenders}; "
        f"it is traced on one host, so these get embedded into every other "
        f"platform's binary (the mac onnxruntime in linux and windows, nexus-lhr6a). "
        f"The native-libs-* pom profiles select each platform's own libraries, and "
        f"JNA embeds its own through its own metadata."
    )


@pytest.mark.parametrize(
    "glob",
    [
        "ai/onnxruntime/native/osx-aarch64/libonnxruntime.dylib",
        "native/lib/linux-x86_64/cpu/libtokenizers.so",
        "com/sun/jna/darwin-aarch64/libjnidispatch.jnilib",
        "darwin-aarch64/libcudart.dylib",
        "darwin/libcudart.dylib",
        "libcudart.dylib",
        "libfoo.so",
        "lib/libfoo.so.1.2",
        "win32-x86-64/jnidispatch.dll",
        "com/sun/jna/win32-x86-64/x.txt",
        "vendor/linux-x64/data.bin",
        "vendor/osx/data.bin",
        "vendor/windows/data.bin",
    ],
)
def test_platform_glob_detector_catches(glob: str) -> None:
    """The detector is what keeps the traced-metadata check honest; pin what it catches."""
    assert _names_platform_native_library(glob)


@pytest.mark.parametrize(
    "glob",
    [
        "db/changelog/aspects-001-baseline.xml",
        "META-INF/services/org.slf4j.spi.SLF4JServiceProvider",
        "native/lib/tokenizers.properties",
        "liquibase.build.properties",
        "org/example/Winners.class",
        "windows-notes.txt",
    ],
)
def test_platform_glob_detector_passes_ordinary_resources(glob: str) -> None:
    assert not _names_platform_native_library(glob)
