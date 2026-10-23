# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The "PDF support" doctor row and the MinerU rows without the stack.
nexus-2k66t.4.2.

A fresh install has no PDF stack by design, so the row is INFORMATIONAL there
(never red) and the MinerU rows are not applicable. A broken stack (part of it,
a version off its pin, a receipt that lists `pdf` over an empty tree) is red
with the repair command. A refusing background run's left-behind PDFs are
counted, so the hook's silence is not the last word.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus import health, pdf_stack
from nexus.pdf_stack import InstallPlan, StackState

_PLAN = InstallPlan("generation", True, pdf_stack.INSTALL_COMMAND)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(pdf_stack, "install_plan", lambda: _PLAN)


def _stack(monkeypatch, status: str, detail: str = "") -> None:
    monkeypatch.setattr(pdf_stack, "state", lambda: StackState(status, detail))


def test_present_is_a_pass_followed_by_the_docling_module_row(monkeypatch) -> None:
    _stack(monkeypatch, "present", "5 distributions on their pins")
    rows = health._check_pdf_support()
    assert [r.label for r in rows] == ["PDF support", "PDF extraction (docling)"]
    assert rows[0].ok and rows[0].detail == "5 distributions on their pins"


def test_absent_is_informational_never_red_and_names_the_command(monkeypatch) -> None:
    _stack(monkeypatch, "absent", "docling, MinerU and torch are not installed")
    (row,) = health._check_pdf_support()
    assert row.label == "PDF support"
    assert row.ok and not row.warn and not row.fatal, "a fresh install is not a failure"
    assert "not installed" in row.detail and "installs it on first use" in row.detail
    assert pdf_stack.INSTALL_COMMAND in row.detail


def test_absent_counts_the_pdfs_a_refusing_run_left_behind(monkeypatch, tmp_path) -> None:
    _stack(monkeypatch, "absent")
    pdf_stack.record_skipped([tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "c.pdf"], repo=tmp_path)
    (row,) = health._check_pdf_support()
    assert row.ok and "3 PDF(s)" in row.detail


def test_broken_is_red_with_the_repair_command(monkeypatch) -> None:
    _stack(monkeypatch, "broken", "mineru: 9.9.9, the pin is 3.1.11")
    (row,) = health._check_pdf_support()
    assert not row.ok and not row.warn
    assert "mineru: 9.9.9" in row.detail
    assert row.fix_suggestions and pdf_stack.INSTALL_COMMAND in row.fix_suggestions[0]


def test_a_layout_the_auto_path_does_not_touch_names_its_own_command(monkeypatch) -> None:
    _stack(monkeypatch, "absent")
    monkeypatch.setattr(pdf_stack, "install_plan", lambda: InstallPlan("dev", False, "uv sync", "a source checkout"))
    (row,) = health._check_pdf_support()
    assert "uv sync" in row.detail


def test_the_default_run_asks_the_row() -> None:
    import inspect

    assert "_check_pdf_support()" in inspect.getsource(health.run_health_checks)


# ── the MinerU rows ──────────────────────────────────────────────────────────


def test_the_mineru_server_row_is_not_applicable_without_the_stack(monkeypatch) -> None:
    _stack(monkeypatch, "absent")

    def _boom(*a, **k):
        raise AssertionError("the server probe ran without a stack")

    monkeypatch.setattr("nexus.config.mineru_server_provisioned", _boom)
    monkeypatch.setattr("nexus.config.get_mineru_server_url", _boom)
    assert health._check_mineru_server() == []


def test_check_mineru_reports_na_not_a_failure_without_the_stack(monkeypatch, capsys) -> None:
    from nexus.commands import doctor

    _stack(monkeypatch, "absent")
    doctor._run_check_mineru()
    out = capsys.readouterr().out
    assert "MinerU import: N/A" in out
    assert "required since nexus-2fyb" not in out
    assert "⚠" not in out and "✗" not in out, "no warning or failure glyph on a slim install"
    assert pdf_stack.INSTALL_COMMAND in out
