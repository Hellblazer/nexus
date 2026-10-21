# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hgzx4 (RDR-210 Phase 2 Step 2): pins on service/native-boot-shapes-smoke.sh.

The script boots the native binary at the presence probe's edges (the cloud shape,
neither embedder family, a truncated model) and is invoked from the end of
service/native-smoke.sh. These pins keep it from being quietly gutted: all three
shapes by name, each asserting what the RDR requires, no fixed /tmp scratch paths
(the nexus-eex5m rule), no hardcoded ports, and a loud skip path.

Single-file scans, O(1): not a repo scan, so no ``lint`` marker (same as the sibling
native-smoke pins); they run in the default suite.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parent.parent / "service"
SHAPES = SERVICE / "native-boot-shapes-smoke.sh"
SMOKE = SERVICE / "native-smoke.sh"

_TEMPLATE = '"${TMPDIR:-/tmp}/native-boot-shapes.XXXXXX"'


def _code_lines(path: Path) -> list[tuple[int, str]]:
    return [
        (n, line)
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if not line.lstrip().startswith("#")
    ]


def _fixed_tmp_literals(lines: list[tuple[int, str]]) -> list[str]:
    hits = []
    for n, line in lines:
        stripped = line.replace(_TEMPLATE, "").replace("${TMPDIR:-/tmp}", "")
        if re.search(r"/tmp\b", stripped):
            hits.append(f"{n}: {line.strip()}")
    return hits


def _hardcoded_ports(lines: list[tuple[int, str]]) -> list[str]:
    hits = []
    for n, line in lines:
        if re.search(r"NX_SERVICE_PORT=\d+|localhost:\d{2,5}\b|127\.0\.0\.1:\d{2,5}\b|-p\s+\d+:\d+", line):
            hits.append(f"{n}: {line.strip()}")
    return hits


def test_the_script_exists_and_is_executable():
    assert SHAPES.is_file()
    assert SHAPES.stat().st_mode & 0o111, "native-boot-shapes-smoke.sh must be executable"


def test_syntax_ok():
    bash = shutil.which("bash")
    assert bash
    r = subprocess.run([bash, "-n", str(SHAPES)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_native_smoke_invokes_it_and_propagates_failure():
    text = SMOKE.read_text()
    assert "native-boot-shapes-smoke.sh" in text
    invocations = [line for line in _code_lines(SMOKE) if "native-boot-shapes-smoke.sh" in line[1]]
    assert invocations, "native-smoke.sh no longer invokes native-boot-shapes-smoke.sh"
    assert all("|| fail=1" in line for _, line in invocations), invocations
    # It runs after the voyage-mode phase and before the verdict line.
    assert text.index("native-boot-shapes-smoke.sh") > text.index("end voyage-mode phase")
    assert text.index("native-boot-shapes-smoke.sh") < text.rindex("NATIVE SMOKE PASS")


def test_no_fixed_tmp_scratch_paths():
    assert _fixed_tmp_literals(_code_lines(SHAPES)) == []
    text = SHAPES.read_text()
    assert _TEMPLATE in text, "scratch dir must come from mktemp -d"
    assert 'rm -rf "$SHAPES_TMP"' in text and "trap cleanup EXIT" in text


def test_detector_flags_the_historical_shapes():
    """A check that cannot fail is not a check: prove both scans trip."""
    assert len(_fixed_tmp_literals([(1, '"$BIN" > /tmp/boot.log 2>&1 &')])) == 1
    assert _fixed_tmp_literals([(1, f"X=$(mktemp -d {_TEMPLATE})")]) == []
    assert len(_hardcoded_ports([(1, "NX_SERVICE_PORT=8080 foo"), (2, "curl localhost:5432/x")])) == 2
    assert _hardcoded_ports([(1, 'NX_SERVICE_PORT="$BOOT_PORT"')]) == []


def test_no_hardcoded_ports_and_ports_are_allocated_dynamically():
    assert _hardcoded_ports(_code_lines(SHAPES)) == []
    assert "s.bind(('127.0.0.1',0))" in SHAPES.read_text(), "ports must be allocated with bind(0)"


def test_every_boot_is_isolated_with_an_empty_ort_dir_and_a_clean_env():
    text = SHAPES.read_text()
    assert "env -i" in text, "boots must run in a clean env so ambient keys cannot change the shape"
    assert 'NX_ORT_NATIVE_DIR="$BOOT_ORT"' in text
    assert 'NX_ONNX_MODEL_DIR="$BOOT_MODELS"' in text
    assert 'mkdir -p "$BOOT_DIR/jtmp" "$BOOT_DIR/home" "$BOOT_ORT" "$BOOT_MODELS"' in text


def _function_body(name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}\n", SHAPES.read_text(), re.S | re.M)
    assert m, f"{name}() is gone from native-boot-shapes-smoke.sh"
    return m.group(0)


def test_cloud_shape_asserts_what_the_rdr_requires():
    body = _function_body("run_cloud_shape")
    assert "boot_start spike-dummy" in body, "cloud shape runs with a dummy key"
    assert "reaches service_ready" in body
    assert "/version" in body and "version_is_voyage_only" in body
    assert "no_log 'event=ort_native_dir'" in body
    assert "ort_dir_empty" in body
    assert "exit_is_143" in body and 'WINLIKE" = "1"' in body, "SIGTERM 143, skipped on Windows-like hosts"
    assert "has_log \"event=embedder_presence bge_present=false voyage_key_present=true\"" in body
    voyage_check = _function_body("version_is_voyage_only")
    assert "'voyage'" in voyage_check and "startswith('voyage-')" in voyage_check


def test_neither_family_asserts_both_remedies_and_a_bounded_failure():
    body = _function_body("run_neither_family")
    assert 'boot_start ""' in body, "neither-family runs keyless"
    assert "wait_exit 90" in body and "exited_in_bound" in body and "exited_with_failure" in body
    assert "no_log 'event=service_ready'" in body
    assert "has_log 'NX_VOYAGE_API_KEY'" in body
    assert "has_log 'nx init'" in body


def test_truncated_model_runs_keyless_and_keyed_and_names_the_size_floor():
    body = _function_body("run_truncated_model")
    assert "keyless keyed" in body
    assert 'key="spike-dummy"' in body
    assert "tokenizer.json" in body and "model.onnx" in body
    assert "size_floor_named" in body
    assert "no_log 'event=service_ready'" in body
    assert "no_log 'event=no_embedder_available'" in body, "a truncated model is never reported as absent"
    assert "bge_present=true" in body, "presence is not validity: the probe must report it PRESENT"
    floor = _function_body("size_floor_named")
    assert "far below the standard fp32 bge export" in floor
    # The fake model must be far below the engine's 200 MB floor.
    m = re.search(r"printf '([^']*)' > .*model\.onnx", body)
    assert m and len(m.group(1)) < 1024


def test_all_waits_are_bounded_and_only_our_pids_are_killed():
    text = SHAPES.read_text()
    assert "seq 1 90" in text  # wait_ready
    assert re.search(r"wait_exit\(\)", text)
    # No unbounded wait on the engine and no name-based kill.
    assert not re.search(r"\b(pkill|killall)\b", text)
    assert not re.search(r"^\s*wait\s*$", text, re.M)
    assert 'kill -9 "$BOOT_PID"' in text


def test_the_external_db_path_skips_loudly_rather_than_passing_silently():
    text = SHAPES.read_text()
    assert "SKIP boot shapes" in text and "NOT covered" in text


def test_missing_binary_fails_with_exit_2(tmp_path):
    r = subprocess.run(
        [str(SHAPES)],
        env={"PATH": "/usr/bin:/bin", "BIN": str(tmp_path / "no-such-binary"), "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert "native binary not found" in r.stdout


def test_external_db_url_without_a_container_skips_loudly_and_leaves_no_scratch(tmp_path):
    fake = tmp_path / "fake-bin"
    fake.write_text("#!/bin/sh\nexit 99\n")
    fake.chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    r = subprocess.run(
        [str(SHAPES)],
        env={
            "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
            "BIN": str(fake),
            "TMPDIR": str(scratch),
            "NX_DB_URL": "jdbc:postgresql://localhost:1/none",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SKIP boot shapes" in r.stdout and "NOT covered" in r.stdout
    assert list(scratch.iterdir()) == [], "the EXIT trap must remove the per-run scratch dir"


def test_the_cloud_shapes_negative_onnx_checks_have_positive_controls() -> None:
    """The cloud shape asserts NOTHING was extracted; that only means something if a boot that
    DOES reach applyAtBoot is seen to log the event and fill the directory (review of the
    P2.2 commit, 2026-10-19)."""
    text = SHAPES.read_text(encoding="utf-8")
    assert "positive control: applyAtBoot logged the extraction" in text
    assert "event=ort_native_dir source=extracted" in text
    assert "positive control: NX_ORT_NATIVE_DIR was filled" in text
    assert text.count("ort_dir_nonempty") >= 2


def test_a_skipped_shapes_block_can_be_made_a_failure() -> None:
    text = SHAPES.read_text(encoding="utf-8")
    assert "NATIVE_SMOKE_REQUIRE_SHAPES" in text
    assert "exit 3" in text


def test_the_throwaway_postgres_binds_loopback_only() -> None:
    text = SHAPES.read_text(encoding="utf-8")
    assert '127.0.0.1:${PGP}:5432' in text
