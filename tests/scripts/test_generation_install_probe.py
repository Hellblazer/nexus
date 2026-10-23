# SPDX-License-Identifier: AGPL-3.0-or-later
"""tests/e2e/lib/generation_install_probe.py's pdftext_bound_ok() (nexus-kard5,
GH #1533 follow-on).

Fix check on ede1dfdc7 (T2 nexus/fix-check-nexus-gqrg0-ede1dfdc7-2026-09-11):
the critic asked for the always-on pdftext/pypdfium2 resolution assert on
BOTH the uv-tool layer (fresh-install-mvv.sh leg 8c, already implemented)
and the GENERATION install layer (this probe, previously unasserted). No
live defect on either layer -- the generation path is already cured by the
[tool.uv] override reaching install_generation.sh's overrides.txt -- this
pins the pure version-comparison boundary so a REGRESSION on the generation
layer is caught by the MVV rather than shipping silently.

A pure function, not the subprocess-driving main() (which needs a real
generation built by install_generation.sh, out of scope for a unit test):
this is the same boundary leg 8c's inline python one-liner in
fresh-install-mvv.sh checks, re-exercised here as a plain function call.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "e2e" / "lib"))

from generation_install_probe import pdftext_bound_ok  # noqa: E402
from lock_check import LOCK_MATCHED_FLOOR, LOCK_WITNESSES, lock_verdict  # noqa: E402


class TestPdftextBoundOk:
    def test_below_the_ceiling_on_both_passes(self) -> None:
        assert pdftext_bound_ok("0.6.3", "4.30.0") is True

    def test_pdftext_at_or_above_the_ceiling_fails(self) -> None:
        # GH #1533: pdftext 0.7.x drops PageChars.__iter__.
        assert pdftext_bound_ok("0.7.0", "4.30.0") is False
        assert pdftext_bound_ok("0.7.1", "4.30.0") is False

    def test_pypdfium2_at_or_above_the_ceiling_fails(self) -> None:
        assert pdftext_bound_ok("0.6.3", "5.0.0") is False

    def test_just_below_each_ceiling_passes(self) -> None:
        assert pdftext_bound_ok("0.6.99", "4.99.99") is True


def _report(**over) -> dict:
    base = {
        "examined": 150, "matched": 150, "absent": [], "mismatched": [], "marker_skipped": 4,
        "witness": {name: ["1.0", "==1.0"] for name in LOCK_WITNESSES},
    }
    base.update(over)
    return base


class TestLockVerdict:
    """nexus-2k66t.4.1: the MVV's assertion that a generation follows the lock."""

    def test_a_matching_generation_has_no_problems(self) -> None:
        assert lock_verdict(_report()) == []

    def test_a_mismatched_pin_fails_and_is_named(self) -> None:
        problems = lock_verdict(_report(mismatched=["onnxruntime installed 1.31.0, locked ==1.24.2"]))
        assert problems and "onnxruntime installed 1.31.0" in problems[0]

    def test_a_missing_witness_fails(self) -> None:
        witness = {n: ["1", "==1"] for n in LOCK_WITNESSES if n != "pymupdf"}
        problems = lock_verdict(_report(witness=witness))
        assert any("pymupdf" in p for p in problems)

    def test_a_check_that_examined_too_little_fails(self) -> None:
        problems = lock_verdict(_report(matched=LOCK_MATCHED_FLOOR - 1))
        assert any("examined too little" in p for p in problems)


class TestLockCheckScript:
    """tests/e2e/lib/lock_check.py, which the MVVs run inside the install, run here against this
    interpreter's own installed packages."""

    def _run(self, tmp_path: Path, body: str, *witnesses: str) -> dict:
        import json
        import subprocess
        pins = tmp_path / "constraints.txt"
        pins.write_text(body)
        out = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tests" / "e2e" / "lib" / "lock_check.py"), str(pins), *witnesses],
            capture_output=True, text=True, check=False, timeout=60,
        )
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_it_counts_matches_mismatches_absences_and_marker_skips(self, tmp_path: Path) -> None:
        from importlib.metadata import version
        have = version("packaging")
        report = self._run(
            tmp_path,
            f"# comment\npackaging=={have}\n"
            "click==0.0.1\n"
            "no-such-dist-xyz==1.0\n"
            "pytest==1.0 ; sys_platform == 'never'\n",
            "packaging",
        )
        assert report["matched"] == 1
        assert len(report["mismatched"]) == 1 and report["mismatched"][0].startswith("click installed")
        assert report["absent"] == ["no-such-dist-xyz"]
        assert report["marker_skipped"] == 1
        assert report["witness"]["packaging"][0] == have

    def test_a_local_label_on_the_installed_version_still_matches_a_plain_pin(self, tmp_path: Path) -> None:
        """torch 2.8.0+cpu is installed against a pin of torch==2.8.0."""
        from packaging.requirements import Requirement
        assert Requirement("torch==2.8.0").specifier.contains("2.8.0+cpu", prereleases=True)
