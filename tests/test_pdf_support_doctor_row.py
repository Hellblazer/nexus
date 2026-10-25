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

from nexus import _vcrt, health, pdf_stack
from nexus.pdf_stack import InstallPlan, StackState
from tests._module_seam import setattr_in

_PLAN = InstallPlan("generation", True, pdf_stack.INSTALL_COMMAND)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(pdf_stack, "install_plan", lambda: _PLAN)
    # Hermetic: the OpenCV rows below set their own installed distributions.
    _opencv(monkeypatch)


def _opencv(monkeypatch, *dists: str) -> None:
    """Make exactly *dists* the installed OpenCV distributions, through the real
    importlib.metadata lookup the doctor row uses (no cv2 import)."""
    import importlib.metadata as md

    real = md.version

    def _version(name: str) -> str:
        if name in ("opencv-python", "opencv-python-headless"):
            if name in dists:
                return "4.13.0"
            raise md.PackageNotFoundError(name)
        return real(name)

    monkeypatch.setattr(md, "version", _version)


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


# ── a carry-forward that is pending or failed (nexus-2k66t.4.9) ───────────────


def _pending(monkeypatch, value: bool = True) -> None:
    monkeypatch.setattr(pdf_stack, "carry_forward_pending", lambda: value)


def test_a_pending_carry_forward_is_a_warning_that_names_the_command(monkeypatch) -> None:
    """An install built before the split that has not carried the stack forward yet: the box HAD PDF
    support in every earlier generation, so "not installed" alone would hide that an upgrade owes it."""
    _stack(monkeypatch, "absent")
    _pending(monkeypatch)
    (row,) = health._check_pdf_support()
    assert row.label == "PDF support" and not row.ok and row.warn and not row.fatal
    assert "built before PDF support became an extra" in row.detail
    assert "nx upgrade" in row.detail
    assert row.fix_suggestions and pdf_stack.INSTALL_COMMAND in row.fix_suggestions[0]


def test_a_failed_carry_forward_says_so_and_when(monkeypatch) -> None:
    _stack(monkeypatch, "absent")
    _pending(monkeypatch)
    sentinel = pdf_stack._carry_failed_sentinel()
    assert sentinel is not None
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("2026-10-23T12:00:00+0000\n", encoding="utf-8")
    (row,) = health._check_pdf_support()
    assert not row.ok and row.warn
    assert "could not carry PDF support forward" in row.detail and "2026-10-23T12:00:00" in row.detail
    assert pdf_stack.INSTALL_COMMAND in row.detail
    assert row.fix_suggestions and pdf_stack.INSTALL_COMMAND in row.fix_suggestions[0]


def test_a_failed_note_outranks_a_pending_one(monkeypatch) -> None:
    _stack(monkeypatch, "absent")
    _pending(monkeypatch)
    sentinel = pdf_stack._carry_failed_sentinel()
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("2026-10-23T12:00:00+0000\n", encoding="utf-8")
    note = pdf_stack.carry_forward_note()
    assert note is not None and "could not carry" in note and "built before" not in note


def test_an_old_failed_marker_is_silent_on_an_install_that_owes_no_carry(monkeypatch) -> None:
    """A deliberately slim box (or one that carried since) with a stale failed marker must not warn forever."""
    _stack(monkeypatch, "absent")
    _pending(monkeypatch, False)
    sentinel = pdf_stack._carry_failed_sentinel()
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("2026-10-23T12:00:00+0000\n", encoding="utf-8")
    assert pdf_stack.carry_forward_note() is None
    (row,) = health._check_pdf_support()
    assert row.ok and not row.warn


def test_no_note_when_nothing_is_pending_and_nothing_failed(monkeypatch) -> None:
    """The control: a fresh slim install stays the informational pass the earlier tests pin."""
    _stack(monkeypatch, "absent")
    _pending(monkeypatch, False)
    assert pdf_stack.carry_forward_note() is None
    (row,) = health._check_pdf_support()
    assert row.ok and not row.warn


def test_a_broken_stack_still_reports_red_and_adds_the_failed_note(monkeypatch) -> None:
    _stack(monkeypatch, "broken", "mineru: 9.9.9, the pin is 3.1.11")
    _pending(monkeypatch)
    sentinel = pdf_stack._carry_failed_sentinel()
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("2026-10-23T12:00:00+0000\n", encoding="utf-8")
    (row,) = health._check_pdf_support()
    assert not row.ok and not row.warn, "broken stays a hard failure"
    assert "mineru: 9.9.9" in row.detail and "could not carry PDF support forward" in row.detail


def test_the_note_reads_the_marker_the_carry_forward_writes(monkeypatch, tmp_path) -> None:
    """The producer and the reader agree on one file: run the real failing carry-forward, then ask."""
    monkeypatch.setattr(pdf_stack, "_install_generation", lambda **kw: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr("nexus.install_layout.read_receipt", lambda _g: __import__("nexus").install_layout.Receipt(
        version="", spec="conexus", source_kind="registry", source="conexus", python="3.12",
        base_interpreter="/usr/bin", created_at="2026-10-01T00:00:00Z", extras=[], installer_schema=1,
    ))
    monkeypatch.setattr("nexus.commands.self_cmd.running_generation", lambda: tmp_path / "gen-x")
    assert pdf_stack.carry_forward() is not None
    note = pdf_stack.carry_forward_note()
    assert note is not None and "could not carry PDF support forward" in note


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


# nexus-vyy65: a Windows box with the stack but without msvcp140_atomic_wait.dll
# cannot import torch, so the row must not say healthy.


def test_present_but_the_windows_runtime_lacks_atomic_wait_is_red_with_the_redistributable(monkeypatch) -> None:
    _stack(monkeypatch, "present", "5 distributions on their pins")
    monkeypatch.setattr(pdf_stack, "_windows_runtime_gap", lambda *a, **k: ("base_only", r"C:\cfg\service"))
    rows = health._check_pdf_support()
    assert [r.label for r in rows] == ["PDF support"], "no docling-importable row on a stack that cannot load"
    (row,) = rows
    assert not row.ok
    assert "msvcp140_atomic_wait.dll" in row.detail
    assert any(pdf_stack.VCRT_URL in f for f in row.fix_suggestions)


def test_present_with_no_windows_runtime_gap_stays_healthy(monkeypatch) -> None:
    _stack(monkeypatch, "present", "5 distributions on their pins")
    monkeypatch.setattr(pdf_stack, "_windows_runtime_gap", lambda *a, **k: None)
    rows = health._check_pdf_support()
    assert rows[0].ok and len(rows) == 2


def test_the_red_row_runs_the_real_gap_probe_and_does_not_tell_an_installed_stack_to_reinstall(
    monkeypatch, tmp_path,
) -> None:
    """No lambda for the probe: real tmp dirs, only the platform test and System32 patched."""
    _stack(monkeypatch, "present", "5 distributions on their pins")
    sysdir = tmp_path / "System32"
    sysdir.mkdir()
    cfg_service = tmp_path / "cfg" / "service"
    cfg_service.mkdir(parents=True)
    for n in _vcrt.VC_RUNTIME_DLLS:
        (cfg_service / n).write_bytes(b"MZ")
    monkeypatch.setattr(_vcrt, "is_windows", lambda platform=None: True)
    monkeypatch.setattr(_vcrt, "system_dir", lambda: str(sysdir))
    (row,) = health._check_pdf_support()
    assert not row.ok
    assert "msvcp140_atomic_wait.dll" in row.detail and str(cfg_service) in row.detail
    assert "then rerun nx doctor" in row.detail
    assert pdf_stack.INSTALL_COMMAND not in row.detail, "the stack is installed: do not send the user to reinstall"
    assert any(pdf_stack.VCRT_URL in f for f in row.fix_suggestions)
    # the redistributable lands in System32: the same probe now passes
    for n in _vcrt.PDF_STACK_DLLS:
        (sysdir / n).write_bytes(b"MZ")
    assert health._check_pdf_support()[0].ok


# nexus-vyy65: an ABSENT stack on a Windows box that cannot load it. "First use installs it"
# and "to install now: <command>" are both false there (the first PDF index refuses, and the
# command builds slim again), so the row says the gap and what places the runtime instead.


def test_absent_on_a_gap_box_says_the_gap_and_the_remedy_not_the_install_command(monkeypatch, tmp_path) -> None:
    _stack(monkeypatch, "absent", "docling, MinerU and torch are not installed")
    monkeypatch.setattr(pdf_stack, "_windows_runtime_gap", lambda *a, **k: ("base_only", str(tmp_path / "svc")))
    (row,) = health._check_pdf_support()
    assert row.label == "PDF support" and not row.ok and row.warn, "a warning, not a plain pass"
    _renders_as_soft_warning(row)
    assert "msvcp140_atomic_wait.dll" in row.detail and str(tmp_path / "svc") in row.detail
    assert "cannot be installed yet" in row.detail
    assert "nx upgrade" in row.detail and pdf_stack.VCRT_URL in row.detail
    assert "installs it on first use" not in row.detail, "false on a gap box"
    assert "to install now" not in row.detail and pdf_stack.INSTALL_COMMAND not in row.detail
    assert any(pdf_stack.VCRT_URL in f for f in row.fix_suggestions)


def test_absent_with_no_runtime_at_all_says_so(monkeypatch) -> None:
    _stack(monkeypatch, "absent")
    monkeypatch.setattr(pdf_stack, "_windows_runtime_gap", lambda *a, **k: ("none", None))
    (row,) = health._check_pdf_support()
    assert "no Microsoft Visual C++ runtime" in row.detail and "to install now" not in row.detail


def test_absent_on_a_box_without_a_gap_keeps_the_first_use_wording(monkeypatch) -> None:
    _stack(monkeypatch, "absent")
    monkeypatch.setattr(pdf_stack, "_windows_runtime_gap", lambda *a, **k: None)
    (row,) = health._check_pdf_support()
    assert row.ok and not row.warn
    assert "installs it on first use" in row.detail and pdf_stack.INSTALL_COMMAND in row.detail


def _renders_as_soft_warning(row) -> None:
    """A warning row prints ⚠ and leaves the exit code alone. The 7.86.1 shakeout found
    rows built ok=True, warn=True: ok wins in the renderer, so they printed a green ✓."""
    out, _ = health.format_health_for_cli([row], local_mode=True)
    assert f"{health._SOFT_WARN} {row.label}" in out, out
    assert health.health_exit_code([row]) == health.EXIT_HEALTHY


# ── two OpenCV builds in one environment (nexus-11x9o) ────────────────────────
# opencv-python and opencv-python-headless both write site-packages/cv2/. On a
# Linux box without libGL.so.1 the non-headless one wins and `import cv2` dies,
# so every PDF fails. find_spec cannot see it (the package directory is there).


def test_both_opencv_builds_make_the_row_red_on_linux_and_name_the_repair(monkeypatch) -> None:
    _stack(monkeypatch, "present", "5 distributions on their pins")
    _opencv(monkeypatch, "opencv-python", "opencv-python-headless")
    setattr_in(monkeypatch, health, "sys.platform", "linux")
    row = health._check_pdf_support()[0]
    assert row.label == "PDF support" and not row.ok
    assert "opencv-python" in row.detail and "opencv-python-headless" in row.detail
    assert "libGL" in row.detail
    assert row.fix_suggestions and pdf_stack.INSTALL_COMMAND in row.fix_suggestions[0]


def test_both_opencv_builds_off_linux_is_a_warning_not_red(monkeypatch) -> None:
    _stack(monkeypatch, "present", "5 distributions on their pins")
    _opencv(monkeypatch, "opencv-python", "opencv-python-headless")
    setattr_in(monkeypatch, health, "sys.platform", "darwin")
    row = health._check_pdf_support()[0]
    assert not row.ok and row.warn
    _renders_as_soft_warning(row)
    assert pdf_stack.INSTALL_COMMAND in row.fix_suggestions[0]


def test_headless_only_is_the_healthy_shape(monkeypatch) -> None:
    _stack(monkeypatch, "present", "5 distributions on their pins")
    _opencv(monkeypatch, "opencv-python-headless")
    rows = health._check_pdf_support()
    assert rows[0].ok and not rows[0].warn
    assert rows[0].detail == "5 distributions on their pins"


def test_a_slim_install_does_not_report_opencv(monkeypatch) -> None:
    """No PDF stack, so the OpenCV pair is not this box's business: the row stays the informational pass."""
    _stack(monkeypatch, "absent", "docling, MinerU and torch are not installed")
    _opencv(monkeypatch, "opencv-python", "opencv-python-headless")
    setattr_in(monkeypatch, health, "sys.platform", "linux")
    (row,) = health._check_pdf_support()
    assert row.ok and not row.warn and "opencv" not in row.detail.lower()
