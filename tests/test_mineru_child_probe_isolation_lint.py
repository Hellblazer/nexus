# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lint: a test that runs ``nx doctor --check-mineru`` does not spawn the real MinerU child probe.

``_run_check_mineru`` imports MinerU's native stack in a fresh child (nexus-vyy65), a cold torch
import per call. Only ``tests/test_mineru_child_dll_dirs.py`` runs it for real; every other test file
that reaches ``_run_check_mineru`` (directly or through ``doctor --check-mineru``) takes the
``no_mineru_child_probe`` fixture (``tests/conftest.py``) or patches ``_probe_mineru_child_import``
itself, so a new caller cannot silently add a child import to the fast suite.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_TESTS = Path(__file__).parent
#: A call of the check, or a doctor_cmd invocation that selects it.
_RUNS_THE_CHECK = re.compile(r"""\b_run_check_mineru\(\)|["']--check-mineru["']""")
_ISOLATED = re.compile(r"\bno_mineru_child_probe\b|\b_probe_mineru_child_import\b")

#: Files that run the check on purpose, with why none needs the fixture.
_ALLOWED = {
    "test_mineru_child_dll_dirs.py": "owns the child probe and runs it against real and fake MinerU packages",
    "test_mineru_child_probe_isolation_lint.py": "this file names the patterns it scans for",
    "test_pdf_stack_no_silent_fallback.py": "runs the CLI with the stack absent, which returns before the child probe",
    "test_doctor_cmd.py": "names the flag in the opt-in summary text and in a --json refusal; neither runs the check",
}


def _offenders(files: dict[str, str]) -> list[str]:
    return sorted(
        name for name, text in files.items()
        if name not in _ALLOWED and _RUNS_THE_CHECK.search(text) and not _ISOLATED.search(text)
    )


def test_no_test_file_runs_the_check_without_isolating_the_child_probe() -> None:
    files = {p.name: p.read_text(encoding="utf-8") for p in sorted(_TESTS.glob("test_*.py"))}
    callers = [n for n, t in files.items() if _RUNS_THE_CHECK.search(t)]
    assert len(callers) >= 6, f"expected the known callers of the check, found {callers}"
    stale = [n for n in _ALLOWED if n not in callers]
    assert stale == [], f"allowlist entries that no longer run the check (remove them): {stale}"
    assert _offenders(files) == [], (
        "these files run `_run_check_mineru` / `doctor --check-mineru` and spawn the real MinerU child "
        "probe; add the `no_mineru_child_probe` fixture (tests/conftest.py): " + ", ".join(_offenders(files))
    )


def test_the_lint_flags_an_unisolated_caller_and_passes_an_isolated_one() -> None:
    bare = "def test_x():\n    doctor._run_check_mineru()\n"
    assert _offenders({"test_a.py": bare}) == ["test_a.py"]
    assert _offenders({"test_a.py": "def test_y():\n    runner.invoke(doctor_cmd, ['--check-mineru'])\n"}) == ["test_a.py"]
    assert _offenders({"test_a.py": "@pytest.mark.usefixtures('no_mineru_child_probe')\n" + bare}) == []
    assert _offenders({"test_a.py": "monkeypatch.setattr(doctor, '_probe_mineru_child_import', f)\n" + bare}) == []
    assert _offenders({"test_a.py": "def test_z():\n    pass\n"}) == []
