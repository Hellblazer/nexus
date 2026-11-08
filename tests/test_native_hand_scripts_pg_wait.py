# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-joa2d (1): the hand-run native scripts' pgvector wait gives up after 90 attempts and exits 2.

``service/linux-native-verify.sh`` takes the real service build lease and ``service/trace-native.sh`` needs a
GraalVM ``JAVA_HOME``, so neither runs end to end in a test. Their wait is three lines: a bounded ``for`` loop of 90
``docker exec ... pg_isready`` attempts, one more probe, and ``FAIL ... exit 2``. This test runs those exact lines,
cut out of each script with the script's own ``_bounded`` helper, against a stub ``docker`` that is never ready and a
stub ``sleep`` that returns at once, and pins the count and the exit. A change that drops the bound, the final probe
or the exit fails here, which the until-over-docker lint alone does not catch.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = ["service/linux-native-verify.sh", "service/trace-native.sh"]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash scripts run on POSIX hosts only")


def _wait_block(text: str) -> str:
    """The script's ``_bounded`` function and its pgvector wait (the for-loop line through the FAIL line)."""
    bounded = re.search(r"^_bounded\(\) \{\n.*?^\}\n", text, re.M | re.S)
    loop = re.search(r"^for _ in \$\(seq 1 90\); do _bounded 5 docker exec .*?pg_isready.*\n"
                     r"(?:.*\n)*?.*FAIL: pgvector not ready after 90 attempts.*\n", text, re.M)
    assert bounded and loop, "the script's _bounded helper or its 90-attempt pg wait moved; update this test"
    return bounded.group(0) + loop.group(0)


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_pg_wait_gives_up_after_90_attempts_and_exits_2(script: str, tmp_path: Path) -> None:
    block = _wait_block((REPO / script).read_text(encoding="utf-8"))
    stubs = tmp_path / "bin"
    stubs.mkdir()
    log = tmp_path / "docker.calls"
    (stubs / "docker").write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexit 1\n')
    (stubs / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for f in stubs.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}", "PG": "pg-under-test"}
    proc = subprocess.run(["bash", "-c", block], env=env, capture_output=True, text=True, timeout=120)
    calls = log.read_text().splitlines() if log.exists() else []
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "FAIL: pgvector not ready after 90 attempts" in proc.stdout
    probes = [c for c in calls if "pg_isready" in c]
    assert len(probes) == 91, f"90 loop attempts plus the final probe, got {len(probes)}"


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_pg_wait_stops_at_the_first_ready_probe(script: str, tmp_path: Path) -> None:
    """Non-vacuity: a docker that answers ready on the third probe ends the wait there, with no FAIL."""
    block = _wait_block((REPO / script).read_text(encoding="utf-8"))
    stubs = tmp_path / "bin"
    stubs.mkdir()
    log = tmp_path / "docker.calls"
    (stubs / "docker").write_text(
        f'#!/bin/sh\necho "$*" >> "{log}"\n[ "$(wc -l < "{log}")" -ge 3 ] && exit 0\nexit 1\n')
    (stubs / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for f in stubs.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}", "PG": "pg-under-test"}
    proc = subprocess.run(["bash", "-c", block], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0 and "FAIL" not in proc.stdout, (proc.returncode, proc.stdout, proc.stderr)
    assert len(log.read_text().splitlines()) == 4, "three loop probes, the third ready, then the final probe"
