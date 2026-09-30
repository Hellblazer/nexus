#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trip-wire on the native binary's ACTUAL embedded resource set (nexus-vwfc0).

nexus-lhr6a fixed three causes of oversized native binaries (every native
library embedded twice, another platform's onnxruntime embedded through a
Mac-traced glob, a 290 MB .pdb). The pom-level tests pin the CAUSES they know.
This reads the RESULT: GraalVM's ``-H:+GenerateEmbeddedResourcesFile`` report,
so a fourth cause (a new dependency bundling natives, a metadata re-trace, a
build-order regression the declaration-order test cannot see) fails the PR
instead of shipping.

Fails on:
  * any native-library resource (.so, .so.N, .dylib, .jnilib, .dll) with more
    than one entry (one library embedded from two origins);
  * any ``ai/onnxruntime/native/<dir>/`` or ``native/lib/<dir>/`` path whose
    <dir> is not the build platform's (per library, see --platform-dir);
  * any ``.pdb`` or ``.dSYM`` path (debug symbols);
  * a Maven log whose execution order is not jar:jar -> native
    compile-no-fork -> shade (native must see the thin jar, nexus-lhr6a);
  * a report that is empty or names no native library at all (non-vacuity:
    a format change or a missing report must not read as a clean pass).

The Maven log lines it matches are printed at INFO, which ``-q`` suppresses:
the CI step therefore must not pass ``-q``.

Report format: a JSON list of
``{"name": <resource path>, "entries": [{"origin": ..., "size": ...}]}``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# JNA ships every platform's libjnidispatch itself, through its own
# reachability metadata (com/sun/jna/<platform>/...). That is JNA's design and
# outside this gate's scope: exempt from every check below, and from the
# non-vacuity count.
IGNORED_PREFIXES = ("com/sun/jna/",)

# The two libraries name platforms differently (onnxruntime linux-x64, djl
# linux-x86_64), so the build platform is a per-library mapping.
LIBRARY_PATH_PREFIXES = {
    "onnxruntime": "ai/onnxruntime/native/",
    "djl": "native/lib/",
}

NATIVE_LIB_SUFFIX = re.compile(r"\.(?:so(?:\.\d+)*|dylib|jnilib|dll)$", re.IGNORECASE)
DEBUG_SYMBOLS = re.compile(r"(?:\.pdb$|\.dSYM(?:/|$))", re.IGNORECASE)

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


def check_report(report: object, platform_dirs: dict[str, str]) -> list[str]:
    """Problems found in the embedded-resources report; empty means clean."""
    if not isinstance(report, list) or not report:
        return ["embedded-resources report is empty or not a JSON list: nothing was checked"]
    problems: list[str] = []
    native_libs = 0
    native_libs_by_library: dict[str, int] = {}
    for item in report:
        name = str(item.get("name", "")).lstrip("/") if isinstance(item, dict) else ""
        if not name:
            problems.append(f"malformed report item (no name): {str(item)[:120]}")
            continue
        if name.startswith(IGNORED_PREFIXES):
            continue
        entries = item.get("entries") or []
        if DEBUG_SYMBOLS.search(name):
            problems.append(f"debug symbols embedded: {name}")
        is_lib = bool(NATIVE_LIB_SUFFIX.search(name))
        if is_lib:
            native_libs += 1
            if len(entries) > 1:
                origins = [str(e.get("origin", "?")) for e in entries]
                problems.append(f"native library embedded {len(entries)} times: {name} from {origins}")
        located = _library_of(name)
        if located is None:
            continue
        library, found_dir = located
        if is_lib:
            native_libs_by_library[library] = native_libs_by_library.get(library, 0) + 1
        want = platform_dirs.get(library)
        if want is not None and found_dir != want:
            problems.append(
                f"foreign-platform resource for {library}: {name} "
                f"(build platform dir is {want!r}, found {found_dir!r})"
            )
    if native_libs == 0:
        problems.append(
            "report names no native library outside com/sun/jna/: the report format "
            "changed or the include patterns matched nothing, so this check would pass vacuously"
        )
    if "onnxruntime" in platform_dirs and not native_libs_by_library.get("onnxruntime"):
        problems.append("no onnxruntime native library embedded: the ORT include pattern matched nothing")
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


def parse_platform_dirs(values: list[str]) -> dict[str, str]:
    """`--platform-dir onnxruntime=linux-x64 --platform-dir djl=linux-x86_64`.
    A bare value applies to every library."""
    mapping: dict[str, str] = {}
    for value in values:
        if "=" in value:
            library, _, directory = value.partition("=")
            if library not in LIBRARY_PATH_PREFIXES:
                raise ValueError(f"unknown library {library!r}; expected one of {sorted(LIBRARY_PATH_PREFIXES)}")
            mapping[library] = directory
        else:
            for library in LIBRARY_PATH_PREFIXES:
                mapping.setdefault(library, value)
    return mapping


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--report", required=True, type=Path, help="embedded-resources.json")
    parser.add_argument("--build-log", required=True, type=Path, help="Maven log of the native build (no -q)")
    parser.add_argument(
        "--platform-dir",
        action="append",
        required=True,
        help="build platform dir, per library (onnxruntime=linux-x64) or bare for all; repeatable",
    )
    args = parser.parse_args(argv)
    try:
        platform_dirs = parse_platform_dirs(args.platform_dir)
    except ValueError as exc:
        parser.error(str(exc))
    problems: list[str] = []
    if not args.report.is_file():
        problems.append(f"embedded-resources report not found: {args.report}")
    else:
        try:
            problems += check_report(json.loads(args.report.read_text()), platform_dirs)
        except json.JSONDecodeError as exc:
            problems.append(f"embedded-resources report is not valid JSON: {exc}")
    if not args.build_log.is_file():
        problems.append(f"build log not found: {args.build_log}")
    else:
        problems += check_build_log(args.build_log.read_text(errors="replace"))
    if problems:
        print("NATIVE EMBEDDED RESOURCES CHECK FAILED", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("NATIVE EMBEDDED RESOURCES CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
