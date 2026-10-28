# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor's "PDF extraction (mineru)" row.

Every PDF is extracted by MinerU (unless the user chose the text-only extractor), and a MinerU
pass that dies on an import is an error instead of a quiet fallback. This row reports the same
condition before an index run does. Seen 2026-10-11: a venv listing opencv-python-headless with
no ``cv2/`` directory (two distributions owned it, one was uninstalled).
"""
from __future__ import annotations

from types import SimpleNamespace

from nexus import health


def _spec_missing(*names: str):
    """A resolver for which every pipeline module is present except *names*."""

    def find_spec(name: str):
        return None if name in names else SimpleNamespace(name=name)

    return find_spec


def test_the_row_passes_when_every_pipeline_module_resolves() -> None:
    [row] = health._check_mineru_importable(_spec_missing())
    assert row.label == "PDF extraction (mineru)" and row.ok is True
    assert not row.fix_suggestions
    assert "4 pipeline modules present" in row.detail


def test_a_missing_module_is_a_hard_failure_that_names_it_and_the_remedy() -> None:
    [row] = health._check_mineru_importable(_spec_missing("cv2"))
    assert row.ok is False and row.warn is False, "a broken PDF path is a failure, not a soft warning"
    assert "cv2" in row.detail and "torch" not in row.detail
    assert any("reinstall" in s.lower() for s in row.fix_suggestions)
    assert any("--extractor pymupdf" in s for s in row.fix_suggestions), "the text-only way out is named"


def test_a_lookup_that_raises_counts_as_missing() -> None:
    def find_spec(name: str):
        if name == "mineru":
            raise ModuleNotFoundError(name)
        return SimpleNamespace(name=name)

    [row] = health._check_mineru_importable(find_spec)
    assert row.ok is False and "mineru" in row.detail


def test_with_no_resolver_given_it_asks_the_real_import_system() -> None:
    """This suite's own environment has the PDF stack installed, so the default path must pass here."""
    [row] = health._check_mineru_importable()
    assert row.ok is True, row.detail


def test_the_row_is_part_of_the_default_doctor_run() -> None:
    import inspect

    # nexus-2k66t.4.2: the default run asks the "PDF support" row, which returns
    # the pipeline-module row too once the stack is present (and says
    # "not installed" instead when it is not, so a slim install is not red).
    assert "_check_pdf_support()" in inspect.getsource(health.run_health_checks)
    assert "_check_mineru_importable()" in inspect.getsource(health._check_pdf_support)


def test_the_module_list_is_mineru_cv2_torch_and_torchvision() -> None:
    """The field failure was `import cv2` with the package directory gone; cv2 is the only
    ``find_spec`` check for it. torch and torchvision are also in ``pdf_stack.STACK_MODULES``."""
    assert health._MINERU_PIPELINE_MODULES == ("mineru", "cv2", "torch", "torchvision")


def test_the_removed_extractor_is_not_a_pipeline_module() -> None:
    assert not any(name.startswith("docling") for name in health._MINERU_PIPELINE_MODULES)
