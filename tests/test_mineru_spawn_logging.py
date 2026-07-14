# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-148 Gap 4: route the long-lived mineru-api server spawns through
``open_child_log_or_devnull`` instead of DEVNULL.

DEVNULL discarded the server's startup banner and crash tracebacks — the
only record of WHY it died (the nexus-ovbr7 silent-death class). The two
long-lived server spawns (``nx mineru start`` and
``PDFExtractor._restart_mineru_server``) now route stdout+stderr to a
rotated child log. The short-lived, returncode-covered per-batch worker
(``_mineru_run_subprocess``) is a deliberate, documented carve-out that
keeps DEVNULL.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner


# ── nx mineru start: behavioral — Popen receives the child-log handle ──


def test_start_routes_server_output_to_child_log(
    tmp_path: Path, monkeypatch,
) -> None:
    from nexus.commands.mineru import start

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))  # election flock dir

    # A non-int sentinel standing in for the opened child-log handle; the
    # spawn site must close it (isinstance(..., int) is False -> close()).
    log_handle = MagicMock(name="child_log_handle")
    proc = MagicMock(pid=1234)
    proc.poll.return_value = None  # process stays alive through health poll
    healthy_resp = MagicMock(status_code=200)

    with patch(
        "nexus._mineru_spawn._resolve_mineru_api_bin",
        return_value="/fake/bin/mineru-api",
    ), patch(
        "nexus.commands.mineru._read_pid_file", return_value=None,
    ), patch(
        "nexus.commands.mineru._pid_file_path",
        return_value=tmp_path / "mineru.pid",
    ), patch(
        "nexus._mineru_pid._pid_file_path",
        return_value=tmp_path / "mineru.pid",
    ), patch(
        "nexus._mineru_spawn._mineru_output_root", return_value=tmp_path,
    ), patch(
        # Patch the SOURCE module, not nexus.commands.mineru: the import is
        # deferred inside start() and runs under this patch context, so the
        # name is fetched from nexus.logging_setup at call time.
        "nexus.logging_setup.open_child_log_or_devnull",
        return_value=log_handle,
    ) as mock_open_log, patch(
        "nexus._mineru_spawn.subprocess.Popen", return_value=proc,
    ) as mock_popen, patch(
        "nexus.commands.mineru.httpx.get", return_value=healthy_resp,
    ):
        result = CliRunner().invoke(start, ["--port", "8011"])

    assert result.exit_code == 0, result.output
    # The child log was opened under the canonical "mineru_server" name.
    mock_open_log.assert_called_once_with("mineru_server")
    # ...and handed to the spawn as both stdout and stderr (NOT DEVNULL).
    _, kwargs = mock_popen.call_args
    assert kwargs["stdout"] is log_handle
    assert kwargs["stderr"] is log_handle
    # The parent's copy of the handle is closed after the spawn.
    log_handle.close.assert_called_once()


# ── _restart_mineru_server: behavioral — same routing on the restart path ──


def test_restart_server_routes_output_to_child_log(
    tmp_path: Path, monkeypatch,
) -> None:
    from nexus.pdf_extractor import PDFExtractor

    # nexus-c7odl: the restart path is now policy-gated (conftest pins
    # autostart OFF) and election-guarded (flock in the config dir).
    monkeypatch.setenv("NX_MINERU_AUTOSTART", "1")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

    log_handle = MagicMock(name="child_log_handle")
    proc = MagicMock(pid=4321)
    proc.poll.return_value = None  # stays alive through the health poll
    healthy_resp = MagicMock(status_code=200)

    ext = PDFExtractor()
    with patch(
        "nexus._mineru_pid.read_pid_file", return_value=None,
    ), patch(
        "nexus._mineru_pid._pid_file_path",
        return_value=tmp_path / "mineru.pid",
    ), patch(
        "nexus._mineru_spawn._resolve_mineru_api_bin",
        return_value="/fake/bin/mineru-api",
    ), patch(
        "nexus._mineru_spawn._find_free_port", return_value=8099,
    ), patch(
        "nexus._mineru_spawn._mineru_output_root", return_value=tmp_path,
    ), patch(
        "nexus.logging_setup.open_child_log_or_devnull",
        return_value=log_handle,
    ) as mock_open_log, patch(
        "subprocess.Popen", return_value=proc,
    ) as mock_popen, patch(
        "nexus.pdf_extractor.httpx.get", return_value=healthy_resp,
    ):
        ok = ext._restart_mineru_server()

    assert ok is True
    mock_open_log.assert_called_once_with("mineru_server")
    _, kwargs = mock_popen.call_args
    assert kwargs["stdout"] is log_handle
    assert kwargs["stderr"] is log_handle
    log_handle.close.assert_called_once()


def test_worker_subprocess_keeps_devnull_carveout() -> None:
    import nexus.pdf_extractor as ext_mod

    src = Path(ext_mod.__file__).read_text()
    # The short-lived per-batch worker is a deliberate DEVNULL carve-out.
    assert "Gap 4 carve-out" in src, (
        "the per-batch worker DEVNULL choice must stay documented as a "
        "judged carve-out, not an oversight."
    )
    assert "_MINERU_WORKER_SCRIPT" in src


# ── RDR-148 Gap 3: fresh-interpreter worker (macOS spawn-guard moot) ──


def test_worker_uses_fresh_interpreter_subprocess_form() -> None:
    """RDR-148 Gap 3 (spike: closed-as-moot). The worker must stay a
    fresh-interpreter ``subprocess.Popen([sys.executable, "-c", ...])``,
    NOT a multiprocessing-spawn child. That structural property is what
    makes the originally-diagnosed macOS spawn-guard hazard categorically
    inapplicable at the nexus->worker boundary; a regression to a
    multiprocessing worker would re-open it. This guard pins the form so
    such a revert fails loudly here rather than silently on macOS."""
    import nexus.pdf_extractor as ext_mod

    src = Path(ext_mod.__file__).read_text()
    # The worker is spawned as a fresh interpreter running the inline script.
    assert "sys.executable, \"-c\", _MINERU_WORKER_SCRIPT" in src, (
        "the MinerU worker must remain a fresh-interpreter `-c` subprocess "
        "(RDR-148 Gap 3 moot-by-refactor invariant)."
    )
    # And the spike outcome stays documented next to it.
    assert "RDR-148 Gap 3" in src and "VERIFY-FIRST spike outcome" in src
