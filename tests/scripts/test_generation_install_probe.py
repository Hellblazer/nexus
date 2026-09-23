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
