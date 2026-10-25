# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lint: an e2e script never runs ``nx doctor --check-mineru`` bare.

Since 7.86.1 a red doctor row exits 1. The e2e harnesses run under ``set -e``, so a bare
``_nx doctor --check-mineru >log`` aborted the script at that line, before it printed the
log or reached ``_fail`` (the 7.86.1 shakeout: ``fresh-install-mvv.sh``). Every call must
capture the status (``|| rc=$?``), sit in an ``if`` / ``!`` test, or discard it on purpose
(``|| true`` where the log line, not the rc, is the signal).
"""
from __future__ import annotations

import re
from pathlib import Path

_E2E = Path(__file__).parent / "e2e"
_CALL = re.compile(r"^\s*(?:if\s+|!\s+)?(?:_?\w*nx|uv run nx)\s+doctor\b[^#\n]*--check-mineru")
_HANDLED = re.compile(r"\|\|\s*\S|^\s*(?:if|!|while|until)\s")


def _bare_calls(text: str) -> tuple[list[str], int]:
    """(unhandled call lines, total call lines) in one script."""
    bare: list[str] = []
    total = 0
    for line in text.splitlines():
        if line.lstrip().startswith("#") or not _CALL.match(line):
            continue
        total += 1
        if not _HANDLED.search(line):
            bare.append(line.strip())
    return bare, total


def test_no_e2e_script_runs_doctor_check_mineru_bare() -> None:
    scripts = sorted(_E2E.glob("*.sh"))
    assert scripts, f"no e2e scripts found under {_E2E}; this lint would pass vacuously"
    calls = 0
    offenders: dict[str, list[str]] = {}
    for script in scripts:
        bare, total = _bare_calls(script.read_text(encoding="utf-8"))
        calls += total
        if bare:
            offenders[script.name] = bare
    assert calls >= 2, f"expected the known --check-mineru call sites, scanned {calls}"
    assert offenders == {}, f"bare `doctor --check-mineru` under set -e: {offenders}"


def test_the_lint_sees_the_bare_shape_and_passes_the_handled_ones() -> None:
    bare = '        _nx doctor --check-mineru >"$LOGS/x.log" 2>&1\n'
    assert _bare_calls(bare) == ([bare.strip()], 1)
    for ok in (
        '_nx doctor --check-mineru >"$L" 2>&1 || rc=$?\n',
        '_nx doctor --check-mineru >"$L" 2>&1 || true   # the line is the signal\n',
        'if _nx doctor --check-mineru >"$L" 2>&1; then\n',
    ):
        assert _bare_calls(ok) == ([], 1), ok
    assert _bare_calls("# _nx doctor --check-mineru was bare once\n") == ([], 0)
