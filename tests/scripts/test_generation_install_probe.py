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

from generation_install_probe import (  # noqa: E402
    PDF_STACK_MODULES,
    installer_args,
    is_propagation_miss,
    pdftext_bound_ok,
    propagation_spec,
    requested_pdf_extra,
    stack_mode,
)
from lock_check import LOCK_MATCHED_FLOOR, LOCK_PDF_WITNESSES, LOCK_WITNESSES, lock_verdict  # noqa: E402


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


class TestSlimGeneration:
    """nexus-2k66t.4.2: a plain `conexus` is slim, so the release probe must not ask a slim
    generation for pdftext, and must not let a slim generation pass for a pdf one."""

    def test_the_probe_names_the_same_modules_as_the_package(self) -> None:
        from nexus.pdf_stack import STACK_MODULES

        assert PDF_STACK_MODULES == STACK_MODULES

    def test_a_plain_spec_is_slim_and_a_pdf_extra_spec_is_not(self) -> None:
        assert not requested_pdf_extra("conexus")
        assert not requested_pdf_extra("conexus==7.86.0")
        assert not requested_pdf_extra("conexus[local]")
        assert requested_pdf_extra("conexus[pdf]")
        assert requested_pdf_extra("conexus[local, pdf]")
        assert requested_pdf_extra("/tmp/conexus-7.86.0-py3-none-any.whl[pdf]")

    def test_stack_mode_distinguishes_slim_pdf_and_a_broken_mix(self) -> None:
        assert stack_mode({m: False for m in PDF_STACK_MODULES}) == "slim"
        assert stack_mode({m: True for m in PDF_STACK_MODULES}) == "pdf"
        assert stack_mode({m: m == "torch" for m in PDF_STACK_MODULES}) == "partial"

    def test_a_slim_install_passes_the_lock_witnesses_without_the_pdf_only_one(self) -> None:
        report = _report(witness={name: ["1", "==1"] for name in LOCK_WITNESSES})
        assert lock_verdict(report) == []
        assert any("opencv-python-headless" in p for p in lock_verdict(report, LOCK_WITNESSES + LOCK_PDF_WITNESSES)), (
            "an install that carries the stack and lacks opencv is NOT proved by the slim witnesses"
        )

    def test_the_pdf_only_witness_is_not_in_the_default_closure(self) -> None:
        """If opencv-python-headless ever moves into the default dependencies the slim witnesses should grow."""
        import tomllib

        lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
        pk = {p["name"]: p for p in lock["package"]}
        seen: set[str] = set()
        todo = [d["name"] for d in pk["conexus"]["dependencies"]]
        while todo:
            name = todo.pop()
            if name not in seen:
                seen.add(name)
                todo.extend(d["name"] for d in pk.get(name, {}).get("dependencies", []))
        for w in LOCK_WITNESSES:
            assert w in seen, f"{w} is a slim witness but is not in the default dependency closure"
        for w in LOCK_PDF_WITNESSES:
            assert w not in seen, f"{w} is now a default dependency; move it to LOCK_WITNESSES"


class TestTheDefaultInstallCarriesPdf:
    """nexus-2k66t.4.9 (Sam, 2026-10-23: install everything by default). The release gate builds its
    generation the way the installers do (``--extras pdf``), so the probe's pdf branches (the opencv
    witness, the pdftext/pypdfium2 bound on the generation) run in the gate; a slim leg stays."""

    def test_extras_are_asked_of_the_installer_not_written_into_the_source(self) -> None:
        """``conexus==X[pdf]`` is not a valid spec and ``conexus[pdf]==X`` as a SOURCE makes a receipt whose
        spec and extras disagree: the real route is ``--source conexus --extras pdf --version X``."""
        assert installer_args("conexus==7.86.0", "pdf") == [
            "--source", "conexus", "--extras", "pdf", "--version", "7.86.0",
        ]
        assert installer_args("/tmp/conexus-7.86.0-py3-none-any.whl", "pdf") == [
            "--source", "/tmp/conexus-7.86.0-py3-none-any.whl", "--extras", "pdf",
        ]

    def test_no_extras_is_the_slim_argv_as_before(self) -> None:
        assert installer_args("conexus==7.86.0", "") == ["--source", "conexus==7.86.0"]
        assert installer_args("/tmp/x.whl", "") == ["--source", "/tmp/x.whl"]

    def test_the_propagation_wait_watches_the_spec_uv_will_see(self) -> None:
        """With the extra, uv's miss message names ``conexus[pdf]==X``; a watch on ``conexus==X`` would
        read a late release as a hard failure."""
        spec = propagation_spec("conexus==7.86.0", "pdf")
        assert spec == "conexus[pdf]==7.86.0"
        miss = "error: no version of conexus[pdf]==7.86.0 found in the registry"
        assert is_propagation_miss(miss, spec)
        assert not is_propagation_miss(miss, "conexus==7.86.0")
        assert propagation_spec("conexus==7.86.0", "") == "conexus==7.86.0"
        assert propagation_spec("/tmp/x.whl", "pdf") == "/tmp/x.whl"

    def test_the_extras_argument_alone_asks_for_the_stack(self) -> None:
        assert requested_pdf_extra("conexus==7.86.0", "pdf")
        assert requested_pdf_extra("conexus==7.86.0", "local,pdf")
        assert not requested_pdf_extra("conexus==7.86.0", "local")
        assert not requested_pdf_extra("conexus==7.86.0", "")

    def test_the_mvv_script_builds_the_default_generation_with_pdf_and_keeps_a_slim_leg(self) -> None:
        text = (REPO_ROOT / "tests" / "e2e" / "fresh-install-mvv.sh").read_text(encoding="utf-8")
        assert 'GEN_EXTRAS="pdf"' in text, "the default generation leg must ask for the pdf extra"
        assert "NX_MVV_SKIP_SLIM_LEG" in text and 'GEN_EXTRAS=""' in text, "a slim leg must stay"
        assert "generation-install-slim.log" in text, "the slim leg is re-checked by the non-vacuity leg"
        assert '"$HOME_DIR" "$GEN_SOURCE" "$GEN_EXTRAS"' in text, "the extras must reach the probe"


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
