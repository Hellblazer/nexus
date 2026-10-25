# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A PDF without the stack never falls back quietly. nexus-2k66t.4.2.

nexus-2fyb is the reason this file exists: an opt-in extra produced silent
formula loss on fresh installs for weeks, because the extractor degraded to a
plainer one and the result looked like a paper with no math. With the stack an
extra, the same hazard is back unless every path through extraction says so.

Two layers are exercised:

* the library: ``PDFExtractor.extract`` refuses by name, in every extractor
  mode, before any backend runs (the in-process fakes below);
* the real CLI in a real interpreter that cannot see docling, MinerU or torch
  (a meta-path finder hides them), which is the absent path the dev
  environment, with the stack installed, cannot otherwise show.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from nexus import pdf_stack
from nexus.pdf_extractor import PDFExtractor
from nexus.pdf_stack import PdfStackMissingError, StackState


@pytest.fixture(autouse=True)
def _reset() -> None:
    pdf_stack.reset_cache()


@pytest.fixture
def absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_stack, "state", lambda: StackState("absent", "docling, MinerU and torch are not installed"))
    monkeypatch.setattr(
        pdf_stack, "install_plan",
        lambda: pdf_stack.InstallPlan("generation", True, pdf_stack.INSTALL_COMMAND),
    )


@pytest.fixture
def dummy_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "paper.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    return p


@pytest.mark.parametrize("mode", ["auto", "docling", "mineru"])
def test_extract_refuses_before_any_backend_runs(absent, dummy_pdf, monkeypatch, mode) -> None:
    ex = PDFExtractor()
    ran: list[str] = []
    for name in ("_extract_with_docling", "_extract_with_mineru", "_extract_normalized", "_route_to_mineru"):
        monkeypatch.setattr(ex, name, lambda *a, _n=name, **k: ran.append(_n))
    monkeypatch.setattr("nexus.pdf_extractor._has_formulas_quick", lambda p: ran.append("screen") or 0)
    with pytest.raises(PdfStackMissingError) as err:
        ex.extract(dummy_pdf, extractor=mode)
    assert ran == [], f"a backend ran before the refusal: {ran}"
    assert pdf_stack.INSTALL_COMMAND in str(err.value) and "paper.pdf" in str(err.value)
    assert err.value.exit_code == 69


def test_a_formula_pdf_is_not_routed_to_pymupdf_when_the_stack_is_absent(absent, dummy_pdf, monkeypatch) -> None:
    """The nexus-2fyb shape: formulas detected, MinerU unavailable. The old
    failure was a quiet pymupdf_normalized result."""
    ex = PDFExtractor()
    monkeypatch.setattr("nexus.pdf_extractor._has_formulas_quick", lambda p: 50)
    normalized = []
    monkeypatch.setattr(ex, "_extract_normalized", lambda *a, **k: normalized.append(1))
    with pytest.raises(PdfStackMissingError):
        ex.extract(dummy_pdf)
    assert normalized == []


def test_an_import_error_deep_in_extraction_says_not_installed_when_it_is_not(absent, dummy_pdf) -> None:
    err = PDFExtractor._docling_not_importable(dummy_pdf, ModuleNotFoundError("No module named 'docling'"))
    msg = str(err)
    assert "docling could not be loaded" in msg
    assert "PDF support is not installed" in msg and pdf_stack.INSTALL_COMMAND in msg
    assert "install is broken" not in msg and "Reinstall with" not in msg


def test_an_import_error_with_the_stack_present_keeps_the_corrupt_install_message(dummy_pdf, monkeypatch) -> None:
    monkeypatch.setattr(pdf_stack, "state", lambda: StackState("present", ""))
    msg = str(PDFExtractor._docling_not_importable(dummy_pdf, ModuleNotFoundError("No module named 'cv2'")))
    assert "Reinstall with `" in msg and "nx doctor" in msg


def test_the_mineru_import_branch_refuses_instead_of_advising_formula_stripped_output(absent, dummy_pdf, monkeypatch) -> None:
    ex = PDFExtractor()

    def _boom(*a, **k):
        raise ImportError("No module named 'mineru'")

    monkeypatch.setattr(ex, "_extract_with_mineru", _boom)
    with pytest.raises(RuntimeError) as err:
        ex._route_to_mineru(dummy_pdf, 12, on_page=None, on_formula_oom="fail")
    msg = str(err.value)
    assert "PDF support is not installed" in msg and pdf_stack.INSTALL_COMMAND in msg
    assert "--extractor docling" not in msg, "never suggest the formula-stripping opt-out for a missing stack"


# ── the real CLI in an interpreter that cannot see the stack ─────────────────

_HIDE = textwrap.dedent(
    """
    import importlib.abc, sys

    class _Hide(importlib.abc.MetaPathFinder):
        _names = {"docling", "mineru", "torch", "torchvision", "pdftext"}

        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in self._names:
                raise ModuleNotFoundError(f"No module named {name!r} (hidden by the test)", name=name)
            return None

    sys.meta_path.insert(0, _Hide())
    from nexus.cli import main
    main()
    """
)


def _run_cli(tmp_path: Path, *argv: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("NX_PDF_STACK")},
        "HOME": str(home), "NEXUS_CONFIG_DIR": str(home / "cfg"), "NX_NO_TELEMETRY": "1",
        **(env_extra or {}),
    }
    return subprocess.run(
        [sys.executable, "-c", _HIDE, *argv], capture_output=True, text=True, env=env, check=False, timeout=120,
    )


@pytest.mark.parametrize("policy", ["refuse", "install"])
def test_the_real_cli_refuses_with_69_names_the_command_and_writes_nothing(tmp_path, policy) -> None:
    """policy=install too: this interpreter is a source checkout's venv, not a
    generation, so the auto path has nothing it may rebuild and refuses with
    the layout's own command. Either way, nothing was written."""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    proc = _run_cli(tmp_path, "index", "pdf", str(pdf), env_extra={pdf_stack.POLICY_ENV: policy})
    assert proc.returncode == 69, (proc.returncode, proc.stderr[-800:])
    assert "PDF support is not installed" in proc.stderr
    assert "nothing was written" in proc.stderr
    assert "Run: uv sync" in proc.stderr, "a source checkout's command, whatever the policy"
    assert ("refused by NX_PDF_STACK=refuse" in proc.stderr) == (policy == "refuse")
    written = [p for p in (tmp_path / "home").rglob("*") if p.is_file() and "logs" not in p.parts]
    assert written == [], f"a refused PDF index wrote {written}"


def test_the_real_cli_still_serves_everything_that_is_not_a_pdf(tmp_path) -> None:
    proc = _run_cli(tmp_path, "--version")
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "version" in proc.stdout.lower()


def test_the_real_cli_doctor_check_mineru_is_not_applicable(tmp_path) -> None:
    proc = _run_cli(tmp_path, "doctor", "--check-mineru")
    assert "MinerU import: N/A" in proc.stdout, (proc.stdout[-600:], proc.stderr[-600:])
