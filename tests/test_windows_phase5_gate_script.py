# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shape pins for the RDR-224 Phase 5 gate (nexus-ijue9.16).

The gate is hand-run inside a Windows guest, so CI cannot execute it. These
pins hold the properties a reviewer would otherwise re-derive by reading: the
published versions are required input, the assertion count is declared and
enforced, a skip can only fail, and the two Phase 3 critique items (both init
paths, the delayed re-check after the stop) are present. The red runs that
prove the count is enforced are recorded in T2, not here.
"""

from __future__ import annotations

import re
from pathlib import Path

GATE: Path = Path(__file__).resolve().parent / "e2e" / "windows-phase5-gate.ps1"


def _text() -> str:
    return GATE.read_text(encoding="utf-8")


def test_versions_are_mandatory_with_no_default() -> None:
    text = _text()
    for name in ("ExpectedVersion", "ExpectedEngine"):
        m = re.search(
            r"\[Parameter\(Mandatory\s*=\s*\$true\)\]\s*\[string\]\s*\$" + name + r"\s*[,)]",
            text,
        )
        assert m, f"${name} must be a mandatory string parameter with no default"


def test_declares_five_assertions_and_fails_below_count() -> None:
    text = _text()
    assert re.search(r"\$DECLARED\s*=\s*5\b", text)
    assert "executed=$executed declared=$DECLARED" in text
    assert re.search(r"if\s*\(\s*\$executed\s+-ne\s+\$DECLARED", text)


def test_a_skip_is_recorded_as_not_executed() -> None:
    text = _text()
    assert "[string]$ForceSkip" in text
    assert "$skipIds -contains $id" in text
    # The skip path records SKIPPED; only an executed (PASS or FAIL)
    # assertion counts toward $executed.
    assert "'SKIPPED'" in text
    assert re.search(r"\(K \$_ 'status'\)\s+-in\s+@\('PASS',\s*'FAIL'\)", text)


def test_verdict_lines_and_exit_codes() -> None:
    text = _text()
    assert "WINDOWS PHASE 5 GATE PASSED" in text
    assert "WINDOWS PHASE 5 GATE FAILED" in text
    assert "exit 1" in text


def test_both_init_paths_and_delayed_recheck() -> None:
    text = _text()
    assert "'--service', '--yes'" in text
    assert re.search(r"''\s*\|\s*&\s*\$nx\s+init\s+--service", text), (
        "leg B must run init with stdin redirected (no TTY) and no --yes"
    )
    m = re.search(r"\$RECHECK_SECONDS\s*=\s*(\d+)", text)
    assert m and int(m.group(1)) >= 35
    assert "'--with-pg'" in text


def test_refuses_off_windows() -> None:
    assert "$env:OS -ne 'Windows_NT'" in _text()
