# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``scripts/check_native_embedded_resources.py`` (nexus-vwfc0).

The checker reads GraalVM's embedded-resources report plus the Maven log of
the native build. Each failure mode has a fixture that must fail with the
right message, and a clean fixture must pass; the vacuity cases (empty
report, no native library, no log stages) must fail, never pass.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import check_native_embedded_resources as chk

LINUX = chk.PLATFORMS["linux-amd64"]
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
    swapped = dataclasses.replace(LINUX, onnxruntime_dir="linux-x86_64", djl_dir="linux-x64")
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
    assert any("required native library not embedded: ai/onnxruntime/native/linux-x64/libonnxruntime.so" in p
               for p in problems)


@pytest.mark.parametrize("platform_name", sorted(chk.PLATFORMS))
def test_losing_any_one_required_library_fails_on_every_platform(platform_name: str):
    """The required list is the non-vacuity: dropping ANY one library the
    platform carries (libonnxruntime4j_jni, libtokenizers, a Windows MinGW
    runtime DLL) must fail even though every forbidden-resource check is green."""
    platform = chk.PLATFORMS[platform_name]
    full = [_res(path) for path in platform.required]
    assert chk.check_report(full, platform) == [], "the required list itself must be a clean report"
    for missing in platform.required:
        report = [r for r in full if r["name"] != missing]
        problems = chk.check_report(report, platform)
        assert any(f"required native library not embedded: {missing}" in p for p in problems), missing


def test_required_lists_are_distinct_per_platform_and_non_empty():
    seen: set[str] = set()
    for name, platform in chk.PLATFORMS.items():
        assert len(platform.required) >= 3, f"{name}: too few required libraries; the list was gutted"
        for path in platform.required:
            assert path not in seen, f"{path} required by two platforms: a foreign-platform path leaked in"
            seen.add(path)


def test_losing_jni_or_tokenizers_alone_fails_on_linux():
    no_jni = [r for r in CLEAN if not r["name"].endswith("libonnxruntime4j_jni.so")]
    no_tok = [r for r in CLEAN if not r["name"].endswith("libtokenizers.so")]
    assert any("libonnxruntime4j_jni.so" in p for p in chk.check_report(no_jni, LINUX))
    assert any("libtokenizers.so" in p for p in chk.check_report(no_tok, LINUX))


# ── foreign native-library suffix anywhere outside com/sun/jna/ ─────────────


@pytest.mark.parametrize(
    "name",
    ["libcudart.dylib", "vendor/native/foo.dll", "lib/libfoo.jnilib", "x/y/z/onnxruntime_providers_cuda.dll"],
)
def test_foreign_suffix_outside_platform_paths_fails_on_linux(name: str):
    """A single-origin foreign library at a path neither per-library prefix covers
    used to pass (nexus-zz2w7): the suffix alone decides."""
    problems = chk.check_report(CLEAN + [_res(name)], LINUX)
    assert len(problems) == 1 and "foreign-platform native library" in problems[0] and name in problems[0]


@pytest.mark.parametrize(
    ("platform_name", "name"),
    [
        ("linux-amd64", "vendor/x.dll"),
        ("linux-arm64", "vendor/x.dylib"),
        ("mac-arm64", "vendor/libx.so"),
        ("mac-arm64", "vendor/libx.so.1"),
        ("mac-arm64", "vendor/x.dll"),
        ("windows-x64", "vendor/libx.so"),
        ("windows-x64", "vendor/libx.dylib"),
        ("windows-x64", "vendor/x.jnilib"),
    ],
)
def test_foreign_suffix_fails_per_platform(platform_name: str, name: str):
    platform = chk.PLATFORMS[platform_name]
    clean = [_res(path) for path in platform.required]
    problems = chk.check_report(clean + [_res(name)], platform)
    assert any("foreign-platform native library" in p and name in p for p in problems)


@pytest.mark.parametrize(
    ("platform_name", "name"),
    [
        ("linux-amd64", "vendor/libx.so.1.2"),
        ("mac-arm64", "vendor/libx.jnilib"),
        ("mac-arm64", "vendor/libx.dylib"),
        ("windows-x64", "vendor/X.DLL"),
    ],
)
def test_own_suffix_elsewhere_passes(platform_name: str, name: str):
    platform = chk.PLATFORMS[platform_name]
    clean = [_res(path) for path in platform.required]
    assert chk.check_report(clean + [_res(name)], platform) == []


def test_foreign_suffix_under_jna_is_still_exempt():
    report = CLEAN + [_res("com/sun/jna/darwin-aarch64/libjnidispatch.jnilib"), _res("com/sun/jna/x/y.dll")]
    assert chk.check_report(report, LINUX) == []


def test_foreign_path_and_suffix_report_once():
    report = CLEAN + [_res("ai/onnxruntime/native/osx-aarch64/libonnxruntime.dylib")]
    assert len(chk.check_report(report, LINUX)) == 1


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


def test_platform_keys_are_the_release_matrix_arches():
    assert sorted(chk.PLATFORMS) == ["linux-amd64", "linux-arm64", "mac-arm64", "windows-x64"]


def test_unknown_platform_is_rejected_by_the_cli(tmp_path):
    report_path = tmp_path / "r.json"
    report_path.write_text("[]")
    with pytest.raises(SystemExit):
        chk.main(["--report", str(report_path), "--build-log", str(report_path), "--platform", "freebsd-x64"])


def _cli(tmp_path: Path, report: object, log: str, platform: str = "linux-amd64") -> int:
    report_path = tmp_path / "embedded-resources.json"
    report_path.write_text(json.dumps(report))
    log_path = tmp_path / "build.log"
    log_path.write_text(log)
    return chk.main(
        ["--report", str(report_path), "--build-log", str(log_path), "--platform", platform]
    )


def test_cli_passes_on_clean_inputs(tmp_path, capsys):
    assert _cli(tmp_path, CLEAN, GOOD_LOG) == 0
    assert "PASSED" in capsys.readouterr().out


@pytest.mark.parametrize("platform_name", sorted(chk.PLATFORMS))
def test_cli_passes_each_platforms_own_required_set(tmp_path, platform_name: str):
    report = [_res(path) for path in chk.PLATFORMS[platform_name].required]
    assert _cli(tmp_path, report, GOOD_LOG, platform_name) == 0


def test_cli_fails_when_the_leg_is_given_another_platforms_report(tmp_path, capsys):
    """A mac report on the linux leg: required libraries missing AND foreign."""
    report = [_res(path) for path in chk.PLATFORMS["mac-arm64"].required]
    assert _cli(tmp_path, report, GOOD_LOG, "linux-amd64") == 1
    err = capsys.readouterr().err
    assert "required native library not embedded" in err and "foreign-platform" in err


def test_cli_fails_and_lists_every_problem(tmp_path, capsys):
    report = CLEAN + [_res("ai/onnxruntime/native/win-x64/onnxruntime.pdb")]
    assert _cli(tmp_path, report, "") == 1
    err = capsys.readouterr().err
    assert "FAILED" in err and "debug symbols" in err and "foreign-platform" in err and "no execution line" in err


def test_cli_missing_report_fails(tmp_path, capsys):
    log_path = tmp_path / "build.log"
    log_path.write_text(GOOD_LOG)
    rc = chk.main(["--report", str(tmp_path / "absent.json"), "--build-log", str(log_path),
                   "--platform", "linux-amd64"])
    assert rc == 1 and "not found" in capsys.readouterr().err


def test_cli_invalid_json_fails(tmp_path, capsys):
    report_path = tmp_path / "r.json"
    report_path.write_text("{not json")
    log_path = tmp_path / "build.log"
    log_path.write_text(GOOD_LOG)
    rc = chk.main(["--report", str(report_path), "--build-log", str(log_path), "--platform", "linux-amd64"])
    assert rc == 1 and "not valid JSON" in capsys.readouterr().err
