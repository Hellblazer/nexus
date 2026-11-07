# SPDX-License-Identifier: AGPL-3.0-or-later
"""A MinerU pid file whose pid is not visible here but whose port answers is not stale (nexus-dyfsw).

A server started from another PID namespace that shares the config directory has a pid this
process cannot see; unlinking its pid file let a second server start.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from click.testing import CliRunner

import nexus._mineru_pid as mp
from nexus.commands.mineru import mineru_group

_DEAD_PID = 999_999_002


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — http.server's name
        body = b'{"processing_tasks": 0, "queued_tasks": 0}'
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a) -> None:
        pass


@pytest.fixture
def answering_port() -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(mp, "is_process_alive", lambda pid: False)
    return tmp_path / "mineru.pid"


def _write(pid_file: Path, port: int) -> None:
    pid_file.write_text(json.dumps({"pid": _DEAD_PID, "port": port}))


def test_an_answering_port_keeps_a_server_with_an_invisible_pid_alive(pid_file: Path, answering_port: int) -> None:
    _write(pid_file, answering_port)
    assert mp.recorded_server_alive(json.loads(pid_file.read_text()))


def test_a_dead_pid_with_a_silent_port_is_dead(pid_file: Path, answering_port: int) -> None:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        silent = s.getsockname()[1]  # bound, never listening
        _write(pid_file, silent)
        assert not mp.recorded_server_alive(json.loads(pid_file.read_text()))


def test_stop_and_status_keep_the_pid_file_of_an_answering_server(pid_file: Path, answering_port: int) -> None:
    _write(pid_file, answering_port)
    stop = CliRunner().invoke(mineru_group, ["stop"])
    assert stop.exit_code == 1
    assert "not visible from here" in stop.output
    assert pid_file.exists()
    CliRunner().invoke(mineru_group, ["status"])
    assert pid_file.exists()


def test_stop_still_removes_a_stale_pid_file(pid_file: Path) -> None:
    _write(pid_file, 1)  # nothing answers on port 1
    result = CliRunner().invoke(mineru_group, ["stop"])
    assert "stale PID" in result.output
    assert not pid_file.exists()
