# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``scripts/check_native_embedded_resources.py`` (nexus-vwfc0).

The checker reads GraalVM's embedded-resources report plus the Maven log of
the native build. Each failure mode has a fixture that must fail with the
right message, and a clean fixture must pass; the vacuity cases (empty
report, no native library, no log stages) must fail, never pass.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import check_native_embedded_resources as chk

LINUX = {"onnxruntime": "linux-x64", "djl": "linux-x86_64"}
ORIGIN = "/home/runner/.m2/repository/x.jar"


def _res(name: str, *origins: str) -> dict:
    return {"name": name, "entries": [{"origin": o or ORIGIN, "size": 1} for o in (origins or ("",))]}


CLEAN = [
    _res("ai/onnxruntime/native/linux-x64/libonnxruntime.so"),
    _res("ai/onnxruntime/native/linux-x64/libonnxruntime4j_jni.so"),
    _res("native/lib/linux-x86_64/cpu/libtokenizers.so"),
    _res("native/lib/tokenizers.properties"),
    _res("db/changelog/aspects-001-baseline.xml"),
    # JNA ships every platform itself and is explicitly out of scope.
    _res("com/sun/jna/darwin-aarch64/libjnidispatch.jnilib"),
    _res("com/sun/jna/win32-x86-64/jnidispatch.dll", "a.jar", "b.jar"),
]

GOOD_LOG = """\
[INFO] --- resources:3.3.1:resources (default-resources) @ nexus-service ---
[INFO] --- jar:3.4.1:jar (default-jar) @ nexus-service ---
[INFO] --- native:1.1.2:compile-no-fork (build-native) @ nexus-service ---
[INFO] --- shade:3.6.0:shade (default) @ nexus-service ---
"""


def test_clean_report_passes():
    assert chk.check_report(CLEAN, LINUX) == []


def test_native_library_with_two_entries_fails():
    report = CLEAN + [_res("ai/onnxruntime/native/linux-x64/libextra.so", "dep.jar", "uber.jar")]
    problems = chk.check_report(report, LINUX)
    assert len(problems) == 1 and "embedded 2 times" in problems[0]
    assert "libextra.so" in problems[0]


@pytest.mark.parametrize("suffix", ["so", "so.1", "so.1.20.0", "dylib", "jnilib", "dll"])
def test_every_native_suffix_is_duplicate_checked(suffix: str):
    name = f"vendor/native/libfoo.{suffix}"
    problems = chk.check_report(CLEAN + [_res(name, "a.jar", "b.jar")], LINUX)
    assert any("embedded 2 times" in p and name in p for p in problems)


def test_foreign_onnxruntime_platform_fails():
    report = CLEAN + [_res("ai/onnxruntime/native/osx-aarch64/libonnxruntime.dylib")]
    problems = chk.check_report(report, LINUX)
    assert len(problems) == 1
    assert "foreign-platform" in problems[0] and "osx-aarch64" in problems[0]


def test_foreign_djl_platform_fails():
    report = CLEAN + [_res("native/lib/win-x86_64/cpu/tokenizers.dll")]
    problems = chk.check_report(report, LINUX)
    assert len(problems) == 1 and "win-x86_64" in problems[0]


def test_platform_dirs_are_per_library():
    """onnxruntime says linux-x64, djl says linux-x86_64: swapping them is a
    foreign-platform resource for each, not an accepted alias."""
    swapped = {"onnxruntime": "linux-x86_64", "djl": "linux-x64"}
    problems = chk.check_report(CLEAN, swapped)
    assert any("libonnxruntime.so" in p for p in problems)
    assert any("libtokenizers.so" in p for p in problems)


@pytest.mark.parametrize(
    "name",
    [
        "ai/onnxruntime/native/linux-x64/onnxruntime.pdb",
        "ai/onnxruntime/native/linux-x64/libonnxruntime.dylib.dSYM/Contents/Info.plist",
        "something/else/debug.PDB",
    ],
)
def test_debug_symbols_fail(name: str):
    problems = chk.check_report(CLEAN + [_res(name)], LINUX)
    assert any("debug symbols" in p and name in p for p in problems)


def test_jna_is_ignored_even_when_foreign_duplicated_or_debug():
    report = CLEAN + [
        _res("com/sun/jna/linux-aarch64/libjnidispatch.so", "a.jar", "b.jar"),
        _res("com/sun/jna/win32-x86-64/jnidispatch.pdb"),
    ]
    assert chk.check_report(report, LINUX) == []


@pytest.mark.parametrize("report", [[], {}, None, "nope"])
def test_empty_or_wrong_shaped_report_fails(report):
    problems = chk.check_report(report, LINUX)
    assert problems and "nothing was checked" in problems[0]


def test_report_with_no_native_library_fails_non_vacuity():
    only_jna_and_data = [
        _res("db/changelog/aspects-001-baseline.xml"),
        _res("com/sun/jna/darwin-aarch64/libjnidispatch.jnilib"),
    ]
    problems = chk.check_report(only_jna_and_data, LINUX)
    assert any("no native library outside com/sun/jna/" in p for p in problems)


def test_report_without_onnxruntime_fails():
    report = [_res("native/lib/linux-x86_64/cpu/libtokenizers.so")]
    problems = chk.check_report(report, LINUX)
    assert any("no onnxruntime native library" in p for p in problems)


def test_item_without_name_is_reported():
    problems = chk.check_report(CLEAN + [{"entries": []}], LINUX)
    assert any("malformed report item" in p for p in problems)


def test_leading_slash_names_are_normalised():
    report = [{**r, "name": "/" + r["name"]} for r in CLEAN]
    assert chk.check_report(report, LINUX) == []


# ── build log order ────────────────────────────────────────────────────────


def test_good_log_order_passes():
    assert chk.check_build_log(GOOD_LOG) == []


def test_ansi_colour_and_old_plugin_names_pass():
    log = (
        "\x1b[1;34mINFO\x1b[m --- maven-jar-plugin:3.4.1:jar (default-jar) @ x ---\n"
        "[INFO] --- native-maven-plugin:1.1.2:compile-no-fork (build-native) @ x ---\n"
        "[INFO] --- maven-shade-plugin:3.6.0:shade (default) @ x ---\n"
    )
    assert chk.check_build_log(log) == []


def test_shade_before_native_fails():
    log = GOOD_LOG.replace(
        "[INFO] --- native:1.1.2:compile-no-fork (build-native) @ nexus-service ---\n", ""
    ) + "[INFO] --- native:1.1.2:compile-no-fork (build-native) @ nexus-service ---\n"
    problems = chk.check_build_log(log)
    assert len(problems) == 1 and "expected" in problems[0]


def test_native_before_jar_fails():
    lines = GOOD_LOG.splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    assert chk.check_build_log("\n".join(lines))


def test_test_jar_goal_does_not_count_as_the_jar_stage():
    log = (
        "[INFO] --- jar:3.4.1:test-jar (default) @ x ---\n"
        "[INFO] --- native:1.1.2:compile-no-fork (build-native) @ x ---\n"
        "[INFO] --- shade:3.6.0:shade (default) @ x ---\n"
    )
    problems = chk.check_build_log(log)
    assert problems and "jar:jar" in problems[0]


@pytest.mark.parametrize("log", ["", "[INFO] BUILD SUCCESS\n"])
def test_log_without_stage_lines_fails(log: str):
    problems = chk.check_build_log(log)
    assert problems and "-q" in problems[0]


# ── platform mapping and CLI ───────────────────────────────────────────────


def test_parse_platform_dirs_per_library_and_bare():
    assert chk.parse_platform_dirs(["onnxruntime=linux-x64", "djl=linux-x86_64"]) == LINUX
    assert chk.parse_platform_dirs(["osx-aarch64"]) == {"onnxruntime": "osx-aarch64", "djl": "osx-aarch64"}
    with pytest.raises(ValueError):
        chk.parse_platform_dirs(["bogus=linux-x64"])


def _cli(tmp_path: Path, report: object, log: str, *extra: str) -> int:
    report_path = tmp_path / "embedded-resources.json"
    report_path.write_text(json.dumps(report))
    log_path = tmp_path / "build.log"
    log_path.write_text(log)
    return chk.main(
        ["--report", str(report_path), "--build-log", str(log_path),
         "--platform-dir", "onnxruntime=linux-x64", "--platform-dir", "djl=linux-x86_64", *extra]
    )


def test_cli_passes_on_clean_inputs(tmp_path, capsys):
    assert _cli(tmp_path, CLEAN, GOOD_LOG) == 0
    assert "PASSED" in capsys.readouterr().out


def test_cli_fails_and_lists_every_problem(tmp_path, capsys):
    report = CLEAN + [_res("ai/onnxruntime/native/win-x64/onnxruntime.pdb")]
    assert _cli(tmp_path, report, "") == 1
    err = capsys.readouterr().err
    assert "FAILED" in err and "debug symbols" in err and "foreign-platform" in err and "no execution line" in err


def test_cli_missing_report_fails(tmp_path, capsys):
    log_path = tmp_path / "build.log"
    log_path.write_text(GOOD_LOG)
    rc = chk.main(["--report", str(tmp_path / "absent.json"), "--build-log", str(log_path),
                   "--platform-dir", "linux-x64"])
    assert rc == 1 and "not found" in capsys.readouterr().err


def test_cli_invalid_json_fails(tmp_path, capsys):
    report_path = tmp_path / "r.json"
    report_path.write_text("{not json")
    log_path = tmp_path / "build.log"
    log_path.write_text(GOOD_LOG)
    rc = chk.main(["--report", str(report_path), "--build-log", str(log_path), "--platform-dir", "linux-x64"])
    assert rc == 1 and "not valid JSON" in capsys.readouterr().err
