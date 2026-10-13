# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor's "PDF extraction (docling)" row.

Every PDF without formulas is extracted by docling, and a docling pass that dies on an import
is now an error instead of a quiet PyMuPDF fallback. This row reports the same condition
before an index run does. Seen 2026-10-11: a venv listing opencv-python-headless with no
``cv2/`` directory (two distributions owned it, one was uninstalled).
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
    [row] = health._check_docling_importable(_spec_missing())
    assert row.label == "PDF extraction (docling)" and row.ok is True
    assert not row.fix_suggestions


def test_a_missing_module_is_a_hard_failure_that_names_it_and_the_remedy() -> None:
    [row] = health._check_docling_importable(_spec_missing("cv2"))
    assert row.ok is False and row.warn is False, "a broken PDF path is a failure, not a soft warning"
    assert "cv2" in row.detail and "torch" not in row.detail
    assert any("reinstall" in s.lower() for s in row.fix_suggestions)


def test_a_lookup_that_raises_counts_as_missing() -> None:
    def find_spec(name: str):
        if name == "docling_ibm_models":
            raise ModuleNotFoundError(name)
        return SimpleNamespace(name=name)

    [row] = health._check_docling_importable(find_spec)
    assert row.ok is False and "docling_ibm_models" in row.detail


def test_with_no_resolver_given_it_asks_the_real_import_system() -> None:
    """This suite's own environment has docling installed, so the default path must pass here."""
    [row] = health._check_docling_importable()
    assert row.ok is True, row.detail


def test_the_row_is_part_of_the_default_doctor_run() -> None:
    import inspect

    assert "_check_docling_importable()" in inspect.getsource(health.run_health_checks)


def test_the_module_list_covers_what_the_table_model_imports() -> None:
    """The field failure was `import cv2` inside docling_ibm_models' TableFormer predictor."""
    assert {"cv2", "docling_ibm_models", "docling"} <= set(health._DOCLING_PIPELINE_MODULES)
