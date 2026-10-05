#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trip-wire on the native binary's ACTUAL embedded resource set (nexus-vwfc0, nexus-zz2w7).

nexus-lhr6a fixed three causes of oversized native binaries (every native
library embedded twice, another platform's onnxruntime embedded through a
Mac-traced glob, a 290 MB .pdb). The pom-level tests pin the CAUSES they know.
This reads the RESULT: GraalVM's ``-H:+GenerateEmbeddedResourcesFile`` report,
so a fourth cause (a new dependency bundling natives, a metadata re-trace, a
build-order regression the declaration-order test cannot see) fails the build
instead of shipping.

It runs on every native build that ships (the PR trip-wire in ci.yml and each
native release leg in engine-service-release.yml), once per platform, selected
with ``--platform`` from ``PLATFORMS`` below. Supporting a new platform is one
``PLATFORMS`` entry; tests/test_native_image_embedded_libs.py pins every entry
against service/pom.xml and the committed jar listing.

Fails on:
  * any native-library resource (.so, .so.N, .dylib, .jnilib, .dll) with more
    than one entry (one library embedded from two origins);
  * any native-library resource outside com/sun/jna/ whose suffix belongs to
    another platform (a .dll in a linux binary, a .so in a windows one),
    wherever it sits in the tree;
  * any ``ai/onnxruntime/native/<dir>/`` or ``native/lib/<dir>/`` path whose
    <dir> is not the build platform's (per library);
  * any ``.pdb`` or ``.dSYM`` path (debug symbols);
  * a library the platform must carry that the report does not name
    (``Platform.required``: losing libonnxruntime4j_jni or libtokenizers
    ships a binary that cannot embed text, with every forbidden-resource
    check still green);
  * a Maven log whose execution order is not jar:jar -> native
    compile-no-fork -> shade (native must see the thin jar, nexus-lhr6a);
  * a report that is empty or names no native library at all (non-vacuity:
    a format change or a missing report must not read as a clean pass).

The Maven log lines it matches are printed at INFO, which ``-q`` suppresses:
the CI step therefore must not pass ``-q``.

Report format: a JSON list of
``{"name": <resource path>, "entries": [{"origin": ..., "size": ...}]}``.

Stdlib only and Python 3.9 compatible: the release legs run it with whatever
``python3`` the runner has.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# JNA ships every platform's libjnidispatch itself, through its own
# reachability metadata (com/sun/jna/<platform>/...). That is JNA's design and
# outside this gate's scope: exempt from every check below, and from the
# non-vacuity count.
IGNORED_PREFIXES = ("com/sun/jna/",)

# Per-library resource prefixes. The two libraries name platforms differently
# (onnxruntime linux-x64, djl linux-x86_64), so each Platform carries one
# directory per library.
LIBRARY_PATH_PREFIXES = {
    "onnxruntime": "ai/onnxruntime/native/",
    "djl": "native/lib/",
}

# Native-library suffix families, by the OS that loads them. A suffix outside
# a platform's own family is foreign to it.
SUFFIX_FAMILIES = {
    "elf": re.compile(r"\.so(?:\.\d+)*$", re.IGNORECASE),
    "macho": re.compile(r"\.(?:dylib|jnilib)$", re.IGNORECASE),
    "pe": re.compile(r"\.dll$", re.IGNORECASE),
}

NATIVE_LIB_SUFFIX = re.compile(r"\.(?:so(?:\.\d+)*|dylib|jnilib|dll)$", re.IGNORECASE)
DEBUG_SYMBOLS = re.compile(r"(?:\.pdb$|\.dSYM(?:/|$))", re.IGNORECASE)


@dataclass(frozen=True)
class Platform:
    """One release leg's expected embedded native set.

    ``required`` lists full resource paths. Sources: the onnxruntime and DJL
    tokenizers jar contents (tests/fixtures/native_jar_listings.txt, generated
    by scripts/native_jar_listing.py); a test pins each list to exactly the
    loadable libraries those jars ship for the platform's directories.
    """

    onnxruntime_dir: str
    djl_dir: str
    suffix_family: str
    required: tuple[str, ...]

    def dir_for(self, library: str) -> str:
        return {"onnxruntime": self.onnxruntime_dir, "djl": self.djl_dir}[library]


def _ort(directory: str, *names: str) -> tuple[str, ...]:
    return tuple(f"ai/onnxruntime/native/{directory}/{name}" for name in names)


def _djl(directory: str, *names: str) -> tuple[str, ...]:
    return tuple(f"native/lib/{directory}/cpu/{name}" for name in names)


# Keys are the release matrix's `arch` values (engine-service-release.yml), so a
# leg passes `--platform ${{ matrix.target.arch }}`.
PLATFORMS = {
    "linux-amd64": Platform(
        "linux-x64",
        "linux-x86_64",
        "elf",
        _ort("linux-x64", "libonnxruntime.so", "libonnxruntime4j_jni.so")
        + _djl("linux-x86_64", "libtokenizers.so"),
    ),
    "linux-arm64": Platform(
        "linux-aarch64",
        "linux-aarch64",
        "elf",
        _ort("linux-aarch64", "libonnxruntime.so", "libonnxruntime4j_jni.so")
        + _djl("linux-aarch64", "libtokenizers.so"),
    ),
    "mac-arm64": Platform(
        "osx-aarch64",
        "osx-aarch64",
        "macho",
        _ort("osx-aarch64", "libonnxruntime.dylib", "libonnxruntime4j_jni.dylib")
        + _djl("osx-aarch64", "libtokenizers.dylib"),
    ),
    # tokenizers.dll links the three MinGW runtime DLLs that ship beside it in
    # the DJL jar, so losing one breaks the load as surely as losing the dll.
    "windows-x64": Platform(
        "win-x64",
        "win-x86_64",
        "pe",
        _ort("win-x64", "onnxruntime.dll", "onnxruntime4j_jni.dll")
        + _djl("win-x86_64", "tokenizers.dll", "libwinpthread-1.dll", "libstdc++-6.dll", "libgcc_s_seh-1.dll"),
    ),
}

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Maven prints `[INFO] --- jar:3.4.1:jar (default-jar) @ nexus-service ---`
# (older Maven: maven-jar-plugin:3.4.1:jar). Anchoring on the goal name keeps
# jar:test-jar out.
LOG_STAGES = (
    ("jar:jar", re.compile(r"---\s+(?:maven-)?jar(?:-plugin)?:[^:\s]+:jar\b")),
    ("native:compile-no-fork", re.compile(r"---\s+(?:native|native-maven-plugin):[^:\s]+:compile-no-fork\b")),
    ("shade:shade", re.compile(r"---\s+(?:maven-)?shade(?:-plugin)?:[^:\s]+:shade\b")),
)


def _library_of(name: str) -> tuple[str, str] | None:
    """(library, platform dir) when `name` is under a per-platform path."""
    for library, prefix in LIBRARY_PATH_PREFIXES.items():
        if name.startswith(prefix):
            rest = name[len(prefix):]
            if "/" in rest:
                return library, rest.split("/", 1)[0]
    return None


def check_report(report: object, platform: Platform) -> list[str]:
    """Problems found in the embedded-resources report; empty means clean."""
    if not isinstance(report, list) or not report:
        return ["embedded-resources report is empty or not a JSON list: nothing was checked"]
    own_suffix = SUFFIX_FAMILIES[platform.suffix_family]
    problems: list[str] = []
    names: set[str] = set()
    native_libs = 0
    for item in report:
        name = str(item.get("name", "")).lstrip("/") if isinstance(item, dict) else ""
        if not name:
            problems.append(f"malformed report item (no name): {str(item)[:120]}")
            continue
        if name.startswith(IGNORED_PREFIXES):
            continue
        names.add(name)
        entries = item.get("entries") or []
        if DEBUG_SYMBOLS.search(name):
            problems.append(f"debug symbols embedded: {name}")
        is_lib = bool(NATIVE_LIB_SUFFIX.search(name))
        if is_lib:
            native_libs += 1
            if len(entries) > 1:
                origins = [str(e.get("origin", "?")) for e in entries]
                problems.append(f"native library embedded {len(entries)} times: {name} from {origins}")
        foreign_path = False
        located = _library_of(name)
        if located is not None:
            library, found_dir = located
            want = platform.dir_for(library)
            if found_dir != want:
                foreign_path = True
                problems.append(
                    f"foreign-platform resource for {library}: {name} "
                    f"(build platform dir is {want!r}, found {found_dir!r})"
                )
        if is_lib and not foreign_path and not own_suffix.search(name):
            problems.append(
                f"foreign-platform native library: {name} "
                f"(this leg loads {platform.suffix_family} libraries only)"
            )
    if native_libs == 0:
        problems.append(
            "report names no native library outside com/sun/jna/: the report format "
            "changed or the include patterns matched nothing, so this check would pass vacuously"
        )
    for required in platform.required:
        if required not in names:
            problems.append(f"required native library not embedded: {required}")
    return problems


def check_build_log(log_text: str) -> list[str]:
    """Problems with the Maven execution order; empty means jar -> native -> shade."""
    lines = _ANSI.sub("", log_text).splitlines()
    first: dict[str, int] = {}
    for label, pattern in LOG_STAGES:
        for number, line in enumerate(lines):
            if pattern.search(line):
                first[label] = number
                break
    missing = [label for label, _ in LOG_STAGES if label not in first]
    if missing:
        return [
            f"build log has no execution line for {missing}: the log is empty, was "
            f"produced with -q, or the plugin goals changed"
        ]
    ordered = [label for label, _ in LOG_STAGES]
    if not first[ordered[0]] < first[ordered[1]] < first[ordered[2]]:
        by_line = sorted(first, key=first.__getitem__)
        return [
            f"build executed in the order {by_line}, expected {ordered}: the native "
            f"build must see the thin jar, or every native library is embedded twice (nexus-lhr6a)"
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--report", required=True, type=Path, help="embedded-resources.json")
    parser.add_argument("--build-log", required=True, type=Path, help="Maven log of the native build (no -q)")
    parser.add_argument(
        "--platform",
        required=True,
        choices=sorted(PLATFORMS),
        help="the release leg's platform (the matrix `arch` value)",
    )
    args = parser.parse_args(argv)
    platform = PLATFORMS[args.platform]
    problems: list[str] = []
    if not args.report.is_file():
        problems.append(f"embedded-resources report not found: {args.report}")
    else:
        try:
            problems += check_report(json.loads(args.report.read_text()), platform)
        except json.JSONDecodeError as exc:
            problems.append(f"embedded-resources report is not valid JSON: {exc}")
    if not args.build_log.is_file():
        problems.append(f"build log not found: {args.build_log}")
    else:
        problems += check_build_log(args.build_log.read_text(errors="replace"))
    if problems:
        print(f"NATIVE EMBEDDED RESOURCES CHECK FAILED ({args.platform})", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"NATIVE EMBEDDED RESOURCES CHECK PASSED ({args.platform})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
