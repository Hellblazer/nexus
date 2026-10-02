"""nexus-eex5m: service/native-smoke.sh must not write fixed /tmp paths.

On a shared box (ghrunner release job + a human's validation run) fixed
``/tmp/native-smoke-svc.log`` / ``/tmp/ns-*.out`` names collide: /tmp is
sticky, so the last user owns the files and the next user's redirect fails
with "Permission denied", which the script reports as a bogus FAIL against a
healthy binary. Every scratch file must live in the per-run ``mktemp -d`` dir
(``$SMOKE_TMP``), removed by the EXIT trap.

Single-file scan, O(1): not a repo scan, so no ``lint`` marker.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SMOKE = Path(__file__).resolve().parent.parent / "service" / "native-smoke.sh"

# The one legitimate /tmp mention: the fallback inside the mktemp template.
_TEMPLATE = '"${TMPDIR:-/tmp}/native-smoke.XXXXXX"'


def _code_lines() -> list[tuple[int, str]]:
    return [
        (n, line)
        for n, line in enumerate(SMOKE.read_text().splitlines(), 1)
        if not line.lstrip().startswith("#")
    ]


def _fixed_tmp_literals(lines: list[tuple[int, str]]) -> list[str]:
    hits = []
    for n, line in lines:
        stripped = line.replace(_TEMPLATE, "")
        stripped = stripped.replace("${TMPDIR:-/tmp}", "")
        if re.search(r"/tmp\b", stripped):
            hits.append(f"{SMOKE.name}:{n}: {line.strip()}")
    return hits


def test_no_fixed_tmp_scratch_paths():
    assert _fixed_tmp_literals(_code_lines()) == []


def test_detector_flags_the_historical_shape():
    """A check that cannot fail is not a check: prove the scan trips."""
    bad = [(1, '"$BIN" > /tmp/native-smoke-svc.log 2>&1 &'), (2, "curl -o /tmp/ns.out x")]
    assert len(_fixed_tmp_literals(bad)) == 2
    ok = [(1, f'SMOKE_TMP=$(mktemp -d {_TEMPLATE})')]
    assert _fixed_tmp_literals(ok) == []


def test_uses_mktemp_dir_and_trap_removes_it():
    text = SMOKE.read_text()
    assert _TEMPLATE in text
    assert 'rm -rf "$SMOKE_TMP"' in text
    assert "trap cleanup EXIT" in text


def test_syntax_ok():
    bash = shutil.which("bash")
    assert bash
    r = subprocess.run([bash, "-n", str(SMOKE)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_trap_removes_dir_on_success_and_keeps_it_on_failure(tmp_path):
    """Run the real mktemp + cleanup() text extracted from the script."""
    text = SMOKE.read_text()
    m_setup = re.search(r"^SMOKE_TMP=\$\(mktemp.*?^export SMOKE_TMP\n", text, re.S | re.M)
    m_clean = re.search(r"^cleanup\(\) \{.*?^\}\n", text, re.S | re.M)
    assert m_setup and m_clean
    bash = shutil.which("bash")
    assert bash
    outcomes = {}
    for label, code in (("ok", 0), ("fail", 3)):
        scratch = tmp_path / label
        scratch.mkdir()
        prog = (
            "set -uo pipefail\nOWN_PG=0\n"
            + m_setup.group(0)
            + m_clean.group(0)
            + "trap cleanup EXIT\n"
            + 'echo "$SMOKE_TMP"\n'
            + f"exit {code}\n"
        )
        r = subprocess.run(
            [bash, "-c", prog],
            env={"TMPDIR": str(scratch), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )
        assert r.returncode == code, r.stderr
        d = Path(r.stdout.strip())
        assert d.parent == scratch and d.name.startswith("native-smoke.")
        outcomes[label] = d.exists()
    assert outcomes == {"ok": False, "fail": True}
