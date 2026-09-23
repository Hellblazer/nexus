# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/pins-preflight.sh's lint-bucket verdict (nexus-aut8g).

Root cause: the old detector grepped the WHOLE captured pytest output for
`[0-9]+ (errors?|failed)( |,)`, so any transient line elsewhere in the run
-- a substrate sweep warning, a captured sub-log -- containing text like
"3 errors " reds the sweep even when the run's own summary line is clean.
Measured 2026-09-08 on 1ce54078d: preflight.log showed a clean summary
('1103 passed, 113 skipped, 18631 deselected, 3 warnings') immediately
followed by 'RED: lint bucket'; an identical immediate rerun in the same
tree exited 0, and PR #1516's CI lint job passed on the same commit --
the false RED did not reproduce, because whatever transient line tripped
the whole-output grep was gone the second time and the offending line was
never preserved (the temp file was deleted unconditionally).

`_lint_bucket_verdict()` (pins-preflight.sh) is self-contained -- pure
string classification of a file path, no cwd/pipefail/tee dependency --
so it is sourced directly rather than driving a real pytest run (which
would need the engine substrate and minutes per case).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "pins-preflight.sh"


def _extract_function() -> str:
    text = SCRIPT.read_text()
    start = text.index("_lint_bucket_verdict() {")
    end = text.index("\n}\n", start) + len("\n}")
    body = text[start : end + 1]
    assert "_lint_bucket_verdict() {" in body, (
        "pins-preflight.sh's _lint_bucket_verdict() was not found at the expected span"
    )
    return body


def _verdict(tmp_path: Path, content: str) -> str:
    outfile = tmp_path / "lint_out.txt"
    outfile.write_text(content)
    script = _extract_function() + f'\n_lint_bucket_verdict {outfile}\n'
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


class TestFalsePositiveFromATransientLine:
    """The exact bug: a clean run's own summary line is not RED, no matter
    what an unrelated earlier line in the same output says."""

    def test_a_transient_errors_line_ahead_of_a_clean_summary_is_not_red(
        self, tmp_path: Path
    ) -> None:
        content = (
            "WARNING substrate sweep found 3 errors in orphaned clusters, ignoring\n"
            "................................................ [100%]\n"
            "1103 passed, 113 skipped, 18631 deselected, 3 warnings in 45.32s\n"
        )
        verdict = _verdict(tmp_path, content)
        assert verdict.startswith("PASS"), verdict

    def test_a_transient_failed_word_ahead_of_a_clean_summary_is_not_red(
        self, tmp_path: Path
    ) -> None:
        content = (
            "captured sub-log: connection failed 1 time, retried and succeeded\n"
            "1103 passed, 113 skipped, 18631 deselected, 3 warnings in 45.32s\n"
        )
        verdict = _verdict(tmp_path, content)
        assert verdict.startswith("PASS"), verdict


class TestGenuineFailureIsStillRed:
    def test_a_real_failed_count_in_the_summary_line_is_red(self, tmp_path: Path) -> None:
        content = (
            "................F....................... [100%]\n"
            "2 failed, 1101 passed, 113 skipped in 44.90s\n"
        )
        verdict = _verdict(tmp_path, content)
        assert verdict.startswith("RED"), verdict
        assert "2 failed" in verdict

    def test_a_real_setup_error_count_in_the_summary_line_is_red(self, tmp_path: Path) -> None:
        content = (
            "gate jar cache MISS -- rebuild the jar: scripts/build-gate-jar.sh\n"
            "20673 errors in 12.10s\n"
        )
        verdict = _verdict(tmp_path, content)
        assert verdict.startswith("RED"), verdict

    def test_no_summary_line_at_all_is_red(self, tmp_path: Path) -> None:
        verdict = _verdict(tmp_path, "some unexpected crash with no pytest footer\n")
        assert verdict.startswith("RED"), verdict


class TestVacuity:
    def test_fewer_than_100_passed_is_vacuous(self, tmp_path: Path) -> None:
        content = "3 passed, 1 skipped in 0.40s\n"
        verdict = _verdict(tmp_path, content)
        assert verdict.startswith("VACUOUS"), verdict
