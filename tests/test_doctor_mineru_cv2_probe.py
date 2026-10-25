# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor --check-mineru imports cv2 (nexus-11x9o).

MinerU imports cv2 lazily (mineru/model/utils/tools/infer/pytorchocr_utility.py,
reached from mineru.cli.common's parse path), so `from mineru.cli.common import
do_parse` succeeds on a box whose cv2 cannot load. Shakedown 2026-10-25 of
7.86.0 on Ubuntu 26.04: the non-headless opencv-python won the shared cv2/
directory, `import cv2` died on libGL.so.1, MinerU answered 409 on every formula
PDF, and the doctor row stayed green. The row now imports cv2 itself and names
the repair.

The cv2 here is a real importable module in a tmp dir placed first on sys.path
(one that raises the real ImportError text, one that loads), not a stubbed
function, so the probe's own import runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nexus import pdf_stack
from nexus.commands import doctor
from nexus.pdf_stack import StackState

LIBGL_ERROR = "libGL.so.1: cannot open shared object file: No such file or directory"


@pytest.fixture(autouse=True)
def _present_stack_no_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PDF stack present, model weights absent: the parse probe is skipped,
    so only the import rows are under test."""
    monkeypatch.setattr(pdf_stack, "state", lambda: StackState("present", "test"))
    monkeypatch.setattr(doctor, "_mineru_pipeline_model_dir_configured", lambda: False)


@pytest.fixture()
def fake_cv2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Write a cv2.py ahead of the real one on sys.path; restore sys.modules after."""
    import importlib

    saved = sys.modules.pop("cv2", None)
    monkeypatch.syspath_prepend(str(tmp_path))

    def _write(source: str) -> None:
        (tmp_path / "cv2.py").write_text(source, encoding="utf-8")
        importlib.invalidate_caches()

    yield _write
    sys.modules.pop("cv2", None)
    if saved is not None:
        sys.modules["cv2"] = saved


def test_a_cv2_that_cannot_load_is_red_and_names_the_repair(fake_cv2, capsys) -> None:
    fake_cv2(f"raise ImportError({LIBGL_ERROR!r})\n")
    doctor._run_check_mineru()
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if "cv2" in ln and "↳" not in ln)
    assert "✗" in row and LIBGL_ERROR in row
    assert "headless" in out
    assert pdf_stack.INSTALL_COMMAND in out
    # the do_parse row is still green: that is exactly the gap this row closes
    assert any("MinerU import" in ln and "✓" in ln for ln in out.splitlines())


def test_a_cv2_that_loads_is_green(fake_cv2, capsys) -> None:
    fake_cv2('__version__ = "9.9.9"\n')
    doctor._run_check_mineru()
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if "cv2" in ln and "↳" not in ln)
    assert "✓" in row and "9.9.9" in row
    assert "✗" not in out


def test_a_broken_cv2_skips_the_real_parse_probe(fake_cv2, monkeypatch, capsys) -> None:
    """The parse would fail on the same import after minutes of model load; one
    red row says it."""
    monkeypatch.setattr(doctor, "_mineru_pipeline_model_dir_configured", lambda: True)

    def _boom(*_a, **_kw):
        raise AssertionError("the real-parse probe ran with a cv2 that cannot load")

    monkeypatch.setattr(doctor, "_mineru_parse_fixture_once", _boom)
    fake_cv2(f"raise ImportError({LIBGL_ERROR!r})\n")
    doctor._run_check_mineru()
    out = capsys.readouterr().out
    assert "MinerU parse" not in out
