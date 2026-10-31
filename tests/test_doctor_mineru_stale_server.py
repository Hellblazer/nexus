# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor --check-mineru`` does not call a leftover server healthy.

7.86.1 shakeout, Linux leg: a bare ``uv tool install`` broke cv2; the index autostarted
``mineru-api`` from that broken tool environment. ``nx self install`` then repaired the box
onto a new generation, but the server kept running, ``--check-mineru`` showed it green
("reachable", interpreter under uv/tools/conexus), and every page of the next index got a 409
from it and fell back to the per-page worker (12 pages, 518 s).

The server here is real: an HTTP server on port 0 answering ``/health``, a live process whose
command line is a ``mineru-api`` entry point, and the pid file ``nx mineru start`` writes. The one
seam is ``upgrade_finish._current_generation`` (which generation ``current`` names).
"""
from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus import pdf_stack, upgrade_finish
from nexus.commands import doctor
from nexus.pdf_stack import StackState

pytestmark = pytest.mark.usefixtures("no_mineru_child_probe")  # nexus-vyy65: no real MinerU child import here


class _Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — http.server's name
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args) -> None:
        return


@pytest.fixture()
def box(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A stack that imports and a cv2 that loads, a live mineru-api, its pid file, and a `current`."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(pdf_stack, "state", lambda: StackState("present", "test"))
    monkeypatch.setattr(doctor, "_mineru_pipeline_model_dir_configured", lambda: False)
    (tmp_path / "cv2.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    entry_point = tmp_path / "bin" / "mineru-api"
    entry_point.parent.mkdir()
    entry_point.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(entry_point)])  # noqa: S603 — fixed argv
    current = tmp_path / "tools" / "gen-20261025T152250Z"
    (current / "bin").mkdir(parents=True)
    monkeypatch.setattr(upgrade_finish, "_current_generation", lambda: current)

    def register(python: Path, port: int | None = None) -> None:
        (cfg / "mineru.pid").write_text(json.dumps({
            "pid": proc.pid, "port": port or server.server_address[1], "python": str(python),
            "started_at": "2026-10-25T15:00:00",
        }), encoding="utf-8")

    deadline = time.time() + 10
    while time.time() < deadline and "mineru-api" not in upgrade_finish.process_command(proc.pid):
        time.sleep(0.05)
    yield current, register
    proc.kill()
    proc.wait(timeout=10)
    server.shutdown()
    server.server_close()


def test_a_server_from_the_replaced_environment_is_flagged_stale_with_the_remedy(box) -> None:
    current, register = box
    register(current.parent.parent / "uv" / "tools" / "conexus" / "bin" / "python")
    result = CliRunner().invoke(doctor.doctor_cmd, ["--check-mineru"])
    out = result.output
    row = next(ln for ln in out.splitlines() if "MinerU server" in ln)
    assert "⚠" in row and "STALE" in row and "uv/tools/conexus" in row, out
    assert "✓ MinerU server" not in out
    assert "`nx mineru stop`, then `nx mineru start`" in out, out
    assert result.exit_code == 0, "a warning does not move the exit code"


def test_a_server_from_the_current_install_is_green(box) -> None:
    """The control: same live server, its interpreter inside `current`."""
    current, register = box
    register(current / "bin" / "python")
    result = CliRunner().invoke(doctor.doctor_cmd, ["--check-mineru"])
    row = next(ln for ln in result.output.splitlines() if "MinerU server" in ln)
    assert "✓" in row and "STALE" not in row, result.output
    assert result.exit_code == 0, result.output


def test_a_server_that_does_not_answer_is_red_and_exits_nonzero(box) -> None:
    """A registered server nothing answers on: the server row counts toward the exit code too."""
    import socket

    current, register = box
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    register(current / "bin" / "python", port=dead_port)
    result = CliRunner().invoke(doctor.doctor_cmd, ["--check-mineru"])
    assert "✗ MinerU server" in result.output, result.output
    assert result.exit_code == 1, result.output
