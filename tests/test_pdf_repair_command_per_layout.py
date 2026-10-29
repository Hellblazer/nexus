# SPDX-License-Identifier: AGPL-3.0-or-later
"""The PDF repair a message names has to work on the install that reads it.

7.86.1 shakeout, Linux leg: on a bare ``uv tool install 'conexus[pdf]'`` both OpenCV
builds land, and the doctor row and ``nx doctor --check-mineru`` told the user to run
``nx self install --extras pdf``. That exits 1 outside a generation install ("--extras
applies to a generation install, and this nx is not running from one"); plain
``nx self install`` is what converges a legacy uv tool tree, carries ``pdf`` and applies
the overrides.

``pdf_stack.install_plan().command`` already says the right command per layout. These
tests run each user-facing site on a LEGACY layout through the real ``install_plan``
(only the two layout predicates it asks are set, as tests/test_pdf_stack.py does) and
require the plan's command, never the generation-only constant.
"""
from __future__ import annotations

import importlib
import importlib.metadata as md
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus import health, pdf_stack
from nexus.commands import doctor, self_cmd
from nexus.indexer import _hold_pdfs_without_stack
from nexus.pdf_extractor import PDFExtractor
from nexus.pdf_stack import StackState

pytestmark = pytest.mark.usefixtures("no_mineru_child_probe")  # nexus-vyy65: no real MinerU child import here
from nexus.upgrade_finish import SkewReport, StaleProcess, restart_stale

GENERATION_ONLY = pdf_stack.INSTALL_COMMAND
LEGACY_COMMAND = "nx self install"
LIBGL_ERROR = "libGL.so.1: cannot open shared object file: No such file or directory"


@pytest.fixture()
def legacy_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """This process runs from a uv tool tree, not a generation."""
    monkeypatch.setattr(self_cmd, "running_generation", lambda: Path("/uv/tools/conexus"))
    monkeypatch.setattr(self_cmd, "_running_from_legacy_tool_install", lambda: True)


def _both_opencv_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    real = md.version

    def _version(name: str) -> str:
        if name in ("opencv-python", "opencv-python-headless"):
            return "5.0.0.93"
        return real(name)

    monkeypatch.setattr(md, "version", _version)


def test_the_plan_for_a_legacy_tree_is_plain_self_install(legacy_layout) -> None:
    """The control every test below leans on: the generation-only command is wrong here."""
    plan = pdf_stack.install_plan()
    assert plan.kind == "legacy" and plan.command == LEGACY_COMMAND != GENERATION_ONLY
    assert pdf_stack.repair_command() == LEGACY_COMMAND


def test_the_doctor_pdf_row_with_both_opencv_builds_names_the_working_repair(legacy_layout, monkeypatch) -> None:
    _both_opencv_builds(monkeypatch)
    row = health._pdf_support_row_with_opencv("5 distributions on their pins")
    assert not row.ok, "both builds installed is the state this row exists to flag"
    (fix,) = row.fix_suggestions
    assert fix.endswith(f": {LEGACY_COMMAND}"), fix
    assert "--extras" not in fix


@pytest.fixture()
def fake_cv2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A cv2.py ahead of the real one on the probe child's PYTHONPATH (the probe imports in a child)."""
    saved = sys.modules.pop("cv2", None)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setattr(pdf_stack, "state", lambda: StackState("present", "test"))
    monkeypatch.setattr(doctor, "_mineru_pipeline_model_dir_configured", lambda: False)

    def _write(source: str) -> None:
        (tmp_path / "cv2.py").write_text(source, encoding="utf-8")
        importlib.invalidate_caches()

    yield _write
    sys.modules.pop("cv2", None)
    if saved is not None:
        sys.modules["cv2"] = saved


def test_check_mineru_cv2_remedy_names_the_working_repair(legacy_layout, fake_cv2, capsys) -> None:
    fake_cv2(f"raise ImportError({LIBGL_ERROR!r})\n")
    assert doctor._run_check_mineru() is True
    out = capsys.readouterr().out
    assert f"`{LEGACY_COMMAND}`" in out
    assert GENERATION_ONLY not in out


def test_check_mineru_exits_nonzero_when_its_cv2_row_is_red(fake_cv2) -> None:
    """The default sweep exits non-zero on a hard red row; ``--check-mineru`` printed the
    same red cv2 row and exited 0, so a script gating on it read a broken MinerU as healthy."""
    fake_cv2(f"raise ImportError({LIBGL_ERROR!r})\n")
    result = CliRunner().invoke(doctor.doctor_cmd, ["--check-mineru"])
    assert "✗ MinerU cv2 import" in result.output, result.output
    assert result.exit_code == 1, result.output


def test_check_mineru_exits_zero_when_every_row_is_green(fake_cv2) -> None:
    """Control for the test above: the exit code follows the rows, it is not just always 1."""
    fake_cv2('__version__ = "9.9.9"\n')
    result = CliRunner().invoke(doctor.doctor_cmd, ["--check-mineru"])
    assert "✗" not in result.output, result.output
    assert result.exit_code == 0, result.output


def test_check_mineru_exits_nonzero_when_mineru_itself_cannot_be_imported(monkeypatch) -> None:
    """The older branch beside the cv2 row: a MinerU import that fails is red too."""
    monkeypatch.setattr(pdf_stack, "state", lambda: StackState("present", "test"))
    monkeypatch.setitem(sys.modules, "mineru.cli.common", None)  # `from mineru.cli.common import ...` raises ImportError
    result = CliRunner().invoke(doctor.doctor_cmd, ["--check-mineru"])
    assert "✗ MinerU import" in result.output, result.output
    assert result.exit_code == 1, result.output


def test_a_refused_index_run_names_the_working_repair(legacy_layout, monkeypatch, tmp_path) -> None:
    """The post-commit hook refuses the install (NX_PDF_STACK=refuse); on a legacy tree the
    line it prints for the held PDFs must name the command that works there."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv(pdf_stack.POLICY_ENV, "refuse")
    monkeypatch.setattr(pdf_stack, "state", lambda: StackState("absent", "MinerU and torch are not installed"))
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    phases: list[str] = []
    held = _hold_pdfs_without_stack([pdf], tmp_path, on_phase=phases.append)
    assert held == [pdf]
    (line,) = [p for p in phases if "NOT indexed" in p]
    assert line.endswith(f"Run: {LEGACY_COMMAND}"), line


def test_a_corrupt_mineru_install_names_the_working_repair(legacy_layout, monkeypatch, tmp_path) -> None:
    """The stack reports itself installed but MinerU will not import: the message offers the
    reinstall and a second way; the second way must not be the generation-only command."""
    monkeypatch.setattr(pdf_stack, "state", lambda: StackState("present", "test"))
    extractor = PDFExtractor()

    def _no_do_parse(*_a, **_kw):
        raise ImportError("cannot import name 'do_parse'")

    monkeypatch.setattr(extractor, "_extract_with_mineru", _no_do_parse)
    with pytest.raises(RuntimeError) as err:
        extractor._extract_with_mineru_guarded(tmp_path / "p.pdf", on_page=None)
    message = str(err.value)
    assert "this conexus install is corrupt" in message
    assert GENERATION_ONLY not in message, message
    assert "--extras pdf" not in message


def test_a_stale_mineru_restart_names_the_working_repair(legacy_layout, monkeypatch) -> None:
    """``nx daemon restart-stale`` can run from a legacy tree beside a ``current`` generation
    that lacks the stack; both of its PDF lines must name a command that works there."""
    report = SkewReport(installed_version="7.86.1")
    report.stale = [StaleProcess(pid=300, kind="mineru", command="mineru-api", age_s=99)]
    monkeypatch.setattr(pdf_stack, "state", lambda: StackState("absent", ""))
    monkeypatch.setattr(pdf_stack, "current_generation_has_stack", lambda: False)
    with patch("nexus.daemon.mineru_lifecycle.spawn_policy_allows", return_value=True), \
            patch("nexus.upgrade_finish.process_command", return_value="mineru-api --host 127.0.0.1"), \
            patch("nexus.upgrade_finish.run_bounded") as sp:
        left_running = restart_stale(report)
    sp.assert_not_called()
    assert any(f"`{LEGACY_COMMAND}` installs it" in a for a in left_running), left_running
    assert not any(GENERATION_ONLY in a for a in left_running), left_running

    monkeypatch.setattr(pdf_stack, "current_generation_has_stack", lambda: True)
    results = iter([MagicMock(returncode=0), MagicMock(returncode=69)])
    with patch("nexus.daemon.mineru_lifecycle.spawn_policy_allows", return_value=True), \
            patch("nexus.upgrade_finish.process_command", return_value="mineru-api --host 127.0.0.1"), \
            patch("nexus.upgrade_finish.run_bounded", side_effect=lambda *a, **k: next(results)):
        stopped = restart_stale(report)
    assert any("not restarted" in a and f"`{LEGACY_COMMAND}`" in a for a in stopped), stopped
    assert not any(GENERATION_ONLY in a for a in stopped), stopped


# ── the sweep: which sites may still name the generation-only constant ───────

#: Files that may name ``INSTALL_COMMAND`` / the literal command, and why each is safe.
#: pdf_stack.py: every use there is reachable only from a generation install (``ensure``
#: past its ``plan.auto`` gate and the re-run guard that follows an install it made;
#: ``HOLD_HINT`` and the network/installer failure text on the same path; the carry-forward
#: notes, which ``carry_forward_pending`` gates on ``install_plan().kind == "generation"``)
#: and the plan for a generation itself.
_GENERATION_ONLY_SITES = {"pdf_stack.py"}


def test_no_message_outside_the_generation_only_paths_names_the_generation_only_command() -> None:
    src = Path(pdf_stack.__file__).resolve().parent
    pattern = re.compile(r"INSTALL_COMMAND|self install --extras pdf")
    offenders: dict[str, list[int]] = {}
    scanned = 0
    for path in sorted(src.rglob("*.py")):
        scanned += 1
        if path.name in _GENERATION_ONLY_SITES:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and not line.lstrip().startswith("#"):
                offenders.setdefault(str(path.relative_to(src)), []).append(number)
    assert scanned > 100, f"the sweep examined {scanned} files; it is looking in the wrong place"
    assert not offenders, (
        "a user-facing message names the generation-only PDF command where a legacy, directory, "
        f"mcpb or dev install can read it; use pdf_stack.repair_command(): {offenders}"
    )
