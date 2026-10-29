# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-489n1): the "PDF settings" doctor row.

A ``[pdf]`` integer setting that holds no integer, or a MinerU time limit above the 240 s bound,
refuses every PDF run. The MinerU server row swallows the error that reads the same section
(``get_mineru_server_url`` raises it), so without this row ``nx doctor`` said nothing about it.
Not applicable (no row) when the settings can be used, which is every box that never set one.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import yaml

from nexus import health, pdf_stack


@pytest.fixture
def pdf_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A private config directory (the settings and the held-PDF record both live in it); the
    returned function writes the ``pdf`` section."""
    cfg = tmp_path / "nxcfg"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))

    def write(**pdf) -> None:
        (cfg / "config.yml").write_text(yaml.safe_dump({"pdf": pdf}))

    return write


def test_a_box_with_no_pdf_settings_gets_no_row(pdf_config) -> None:
    """A virgin box: no config file at all."""
    assert health._check_pdf_config() == []


def test_usable_settings_get_no_row(pdf_config) -> None:
    pdf_config(mineru_page_timeout_s="60", mineru_page_batch=4, mineru_memory_ceiling_mb=2048, ladder_breaker=5)
    assert health._check_pdf_config() == []


@pytest.mark.parametrize("key,bad", [
    ("mineru_page_timeout_s", "soon"),
    ("mineru_page_batch", "many"),
    ("mineru_memory_ceiling_mb", "lots"),
    ("mineru_page_timeout_s", 2.5),
    ("mineru_page_batch", True),
])
def test_a_non_integer_setting_is_a_red_row_that_names_the_key_and_the_remedy(pdf_config, key, bad) -> None:
    pdf_config(**{key: bad})

    (row,) = health._check_pdf_config()

    assert row.label == "PDF settings"
    assert row.ok is False and not row.warn
    assert f"pdf.{key} must be an integer" in row.detail
    assert any(f"nx config set pdf.{key}=<integer>" in s for s in row.fix_suggestions)


def test_a_product_above_the_bound_is_a_red_row_naming_both_settings(pdf_config) -> None:
    pdf_config(mineru_page_timeout_s=100, mineru_page_batch=4)

    (row,) = health._check_pdf_config()

    assert row.ok is False
    assert "pdf.mineru_page_timeout_s (100 s) x pdf.mineru_page_batch (4) = 400 s" in row.detail
    assert row.fix_suggestions


def test_the_text_only_extractor_does_not_care_about_the_mineru_product(pdf_config) -> None:
    """MinerU is never reached, so a product above the bound refuses nothing."""
    pdf_config(extractor="pymupdf", mineru_page_timeout_s=100, mineru_page_batch=4)
    assert health._check_pdf_config() == []


def test_a_non_integer_is_a_row_whatever_the_extractor_says(pdf_config) -> None:
    """The loader reads every integer key whichever extractor is set, and refuses the whole
    section, so the row follows."""
    pdf_config(extractor="pymupdf", mineru_page_timeout_s="soon")
    (row,) = health._check_pdf_config()
    assert "pdf.mineru_page_timeout_s must be an integer" in row.detail


def test_the_default_run_asks_the_row() -> None:
    assert "_check_pdf_config()" in inspect.getsource(health.run_health_checks)


# ── PDFs a run held on the limit (nexus-tr1rj.26) ────────────────────────────


def test_the_row_counts_the_pdfs_a_run_held_on_the_limit(pdf_config, tmp_path) -> None:
    pdf_config(mineru_page_timeout_s=100, mineru_page_batch=4)
    pdf_stack.record_skipped([tmp_path / "a.pdf", tmp_path / "b.pdf"], repo=tmp_path, cause=pdf_stack.SKIP_CAUSE_BOUNDS)

    (row,) = health._check_pdf_config()

    assert "2 PDF(s)" in row.detail and "held" in row.detail


def test_pdfs_held_for_the_stack_are_not_this_rows_to_count(pdf_config, tmp_path) -> None:
    pdf_config(mineru_page_timeout_s=100, mineru_page_batch=4)
    pdf_stack.record_skipped([tmp_path / "a.pdf"], repo=tmp_path)  # the stack's hold

    (row,) = health._check_pdf_config()

    assert "PDF(s)" not in row.detail


def test_a_hold_that_outlived_its_cause_is_a_warning_that_names_the_rerun(pdf_config, tmp_path) -> None:
    """The setting is fixed but no run has cleared the record yet: the PDFs are still not indexed."""
    pdf_config(mineru_page_timeout_s=60, mineru_page_batch=2)
    pdf_stack.record_skipped([tmp_path / "a.pdf"], repo=tmp_path, cause=pdf_stack.SKIP_CAUSE_BOUNDS)

    (row,) = health._check_pdf_config()

    assert row.ok is False and row.warn
    assert "1 PDF(s)" in row.detail
    assert any("nx index repo" in s for s in row.fix_suggestions)


def test_no_hold_and_usable_settings_stay_silent(pdf_config, tmp_path) -> None:
    pdf_config(mineru_page_timeout_s=60, mineru_page_batch=2)
    pdf_stack.record_skipped([tmp_path / "a.pdf"], repo=tmp_path)  # a stack hold, not ours
    assert health._check_pdf_config() == []
