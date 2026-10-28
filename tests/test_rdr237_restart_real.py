# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.2): a server that timed out is replaced by the REAL restart path.

``tests/test_rdr237_bounds.py`` pins the restart rule against a spy. This file runs what the rule
calls: ``_restart_broken_mineru_server`` into the real ``_restart_mineru_server`` with its real
election, its real pid-file read, the real pid-recycle guard (``classify_server_pid``), the real
``stop_server_group`` and the real health wait, against REAL processes: a stand-in ``mineru-api``
(a small HTTP server in its own session that answers ``/health`` and ``/file_parse``). The old one
hangs on one page's request, so the client's own ``httpx`` timeout fires for real.

Only two things are faked: the spawn core (the real one needs the ``mineru-api`` binary; the stand-in
writes the pid file as it does) and the per-page worker that rung 2 runs on (the subprocess path).

Non-vacuity, asserted rather than assumed: the old pid is alive and classified a mineru server before
the run, and the new one differs from it. A stand-in that never started fails the test loudly.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pymupdf
import pytest

from nexus import _mineru_spawn
from nexus._mineru_health import claim_broken_restart
from nexus._mineru_pid import _pid_file_path, is_process_alive, read_pid_file
from nexus.pdf_extractor import PDFExtractor
from tests._child_process import kill_group, popen_in_group
from tests._module_seam import patch_in

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="process groups and shebang scripts are POSIX")

_STANDIN = '''#!{python}
import json, re, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
name = args[args.index("--name") + 1]
hang_page = int(args[args.index("--hang-page") + 1])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(b'{{"status": "healthy"}}')

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        start = int(re.search(rb'name="start_page_id"\\r\\n\\r\\n(\\d+)', raw).group(1))
        if start == hang_page:
            time.sleep(120)  # an abandoned parse: runs on, nothing cancels it
        md = f"{{name}} server parsed page {{start + 1}}"
        self._send(json.dumps({{"results": {{"doc": {{
            "md_content": md, "content_list": "[]",
            "middle_json": json.dumps({{"pdf_info": [{{"page_idx": 0, "para_blocks": []}}]}}),
        }}}}}}).encode())


ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


class StandIn:
    """One stand-in ``mineru-api`` process, in its own session, reaped as soon as it dies."""

    def __init__(self, script: Path, port: int, name: str, hang_page: int) -> None:
        self.port = port
        self.name = name
        self.proc = popen_in_group(  # its own process group, as the real server's start_new_session gives it
            [str(script), "--host", "127.0.0.1", "--port", str(port), "--name", name, "--hang-page", str(hang_page)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.reaped = threading.Event()
        threading.Thread(target=self._reap, daemon=True).start()

    def _reap(self) -> None:
        self.proc.wait()
        self.reaped.set()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def wait_healthy(self, seconds: float = 30.0) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{self.port}/health", timeout=2).status_code == 200:
                    return
            except httpx.TransportError:
                time.sleep(0.1)
        raise AssertionError(f"the {self.name} stand-in server never answered /health")

    def register(self) -> None:
        _mineru_spawn._write_pid_file(_pid_file_path(), {
            "pid": self.pid, "port": self.port, "output_root": "/var/rdr237/out",
            "python": sys.executable, "executable": "mineru-api",
        })

    def kill(self) -> None:
        kill_group(self.proc)


def _free_port() -> int:
    return _mineru_spawn._find_free_port()


def _wait_classified_mineru(pid: int) -> None:
    """The pid-recycle guard reads the live command line; wait for the stand-in's exec (the gap
    between fork and exec reads empty, which classifies "unreadable")."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if _mineru_spawn.classify_server_pid(pid) == "mineru":
            return
        time.sleep(0.1)
    raise AssertionError(f"pid {pid} never classified as a mineru server: {_mineru_spawn.classify_server_pid(pid)}")


def _make_pdf(path: Path, pages: int) -> Path:
    doc = pymupdf.open()
    for n in range(pages):
        doc.new_page().insert_text((72, 72), f"text layer of page {n + 1}")
    doc.save(str(path))
    doc.close()
    return path


def _fake_worker(calls: list):
    """Rung 2's subprocess: a worker that succeeds at once, recording the range it was given."""
    def popen(args, **kwargs):
        pdf, result_dir, start, end_arg, formula = args[-5:]
        calls.append((int(start), formula))
        proc = MagicMock(pid=4321)

        def wait(timeout=None):
            if timeout is None:
                return -9
            base = Path(result_dir) / Path(pdf).name / "auto"
            base.mkdir(parents=True)
            name = Path(pdf).name
            (base / f"{name}.md").write_text(f"worker parsed page {int(start) + 1}", encoding="utf-8")
            (base / f"{name}_content_list.json").write_text("[]", encoding="utf-8")
            (base / f"{name}_middle.json").write_text(
                json.dumps({"pdf_info": [{"page_idx": 0, "para_blocks": []}]}), encoding="utf-8",
            )
            return 0

        proc.wait.side_effect = wait
        return proc

    return popen


@pytest.fixture
def standins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "nexus-config"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("NEXUS_PDF_PROGRESS_QUIET", "1")
    monkeypatch.setenv("NX_MINERU_AUTOSTART", "1")
    monkeypatch.delenv("NX_MINERU_FAULT_PAGES", raising=False)
    monkeypatch.delenv("MINERU_API_MAX_CONCURRENT_REQUESTS", raising=False)
    script = tmp_path / "mineru-api"
    script.write_text(_STANDIN.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    started: list[StandIn] = []
    try:
        yield script, started
    finally:
        for server in started:
            server.kill()


def test_a_one_slot_server_that_times_out_is_replaced_by_the_real_restart_and_later_pages_use_the_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, standins,
) -> None:
    script, started = standins
    old = StandIn(script, _free_port(), "old", hang_page=1)  # page 2's request hangs
    started.append(old)
    old.wait_healthy()
    old.register()
    _wait_classified_mineru(old.pid)
    old_pid = old.pid
    assert is_process_alive(old_pid) and old.proc.poll() is None, "non-vacuity: the old server is alive"

    spawned: list[StandIn] = []

    def spawn(port: int):
        new = StandIn(script, port, "new", hang_page=-1)
        started.append(new)
        spawned.append(new)
        new.register()  # as spawn_server_process does, right after the Popen
        return new.proc

    pdf = _make_pdf(tmp_path / "doc.pdf", 4)
    worker_calls: list = []
    delivered: dict[int, str] = {}
    ext = PDFExtractor()
    with patch("nexus.config.load_config", return_value={"pdf": {"mineru_page_timeout_s": 2}}), \
            patch("nexus.pdf_stack.require"), \
            patch("nexus.pdf_extractor.do_parse", object()), \
            patch("nexus._mineru_spawn.spawn_server_process", side_effect=spawn), \
            patch_in("nexus.pdf_extractor", "sys.platform", "darwin"), \
            patch_in("nexus.pdf_extractor", "subprocess.Popen", _fake_worker(worker_calls)), \
            patch("nexus.util.process_group.safe_killpg_group"):
        result = ext.extract(
            pdf, extractor="mineru", allow_degraded=True,
            on_page=lambda i, t, m: delivered.__setitem__(i, t),
        )

    # The old server is gone, and it was the registered pid that the restart replaced.
    assert old.reaped.wait(30), "the old server process is still running"
    assert old.proc.returncode is not None
    assert len(spawned) == 1, "one restart"
    (new,) = spawned
    assert new.pid != old_pid, "a different server"
    assert is_process_alive(new.pid)
    assert read_pid_file()["pid"] == new.pid, "the pid file names the new server"
    assert httpx.get(f"http://127.0.0.1:{new.port}/health", timeout=5).status_code == 200, "and it answers"
    assert ext._mineru_server_restarts == 1

    # Page 1 came from the old server; page 2 timed out there and settled on rung 2 through the
    # worker; pages 3 and 4 were posted to the NEW server.
    assert "old server parsed page 1" in delivered[0]
    assert "worker parsed page 2" in delivered[1]
    assert worker_calls == [(1, "0")], "page 2's next rung ran on the subprocess path, formulas off"
    assert "new server parsed page 3" in delivered[2]
    assert "new server parsed page 4" in delivered[3]
    assert result.metadata["extraction_pages"] == {"mineru-no-formula": [2]}


def test_a_restart_the_machine_cooldown_refuses_leaves_the_old_server_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, standins,
) -> None:
    """Same run, but another process replaced a server a moment ago: the real cooldown stamp is held."""
    script, started = standins
    old = StandIn(script, _free_port(), "old", hang_page=1)
    started.append(old)
    old.wait_healthy()
    old.register()
    _wait_classified_mineru(old.pid)

    assert claim_broken_restart() is True, "another process took the cooldown just now"

    pdf = _make_pdf(tmp_path / "doc.pdf", 4)
    worker_calls: list = []
    delivered: dict[int, str] = {}
    ext = PDFExtractor()
    with patch("nexus.config.load_config", return_value={"pdf": {"mineru_page_timeout_s": 2}}), \
            patch("nexus.pdf_stack.require"), \
            patch("nexus.pdf_extractor.do_parse", object()), \
            patch("nexus._mineru_spawn.spawn_server_process") as spawn, \
            patch_in("nexus.pdf_extractor", "sys.platform", "darwin"), \
            patch_in("nexus.pdf_extractor", "subprocess.Popen", _fake_worker(worker_calls)), \
            patch("nexus.util.process_group.safe_killpg_group"):
        ext.extract(
            pdf, extractor="mineru", allow_degraded=True,
            on_page=lambda i, t, m: delivered.__setitem__(i, t),
        )

    spawn.assert_not_called()
    assert old.proc.poll() is None and is_process_alive(old.pid), "the old server was not touched"
    assert ext._mineru_server_up is False, "marked down"
    assert "worker parsed page 3" in delivered[2] and "worker parsed page 4" in delivered[3], (
        "later pages used the subprocess path"
    )
