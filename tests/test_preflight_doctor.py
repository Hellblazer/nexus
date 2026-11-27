# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``nx doctor`` block of the preamble: its own bound and the engine liveness probe (GH #1689).

Fakes replace only the runner (``run_bounded``) and the engine address; the probe itself talks to real sockets.
"""
from __future__ import annotations

import pytest

from nexus.commands import preflight_doctor as pd


def _fake_bounded(monkeypatch, *, returncode: int = 0, stdout: str = "", stderr: str = "", raises: Exception | None = None):
    """Replace the doctor block's ``run_bounded``; returns the list of ``(argv, kwargs)`` it was called with."""
    import types

    calls: list[tuple[list[str], dict]] = []

    def fake(argv, **kw):  # noqa: ANN001, ANN003
        calls.append((list(argv), kw))
        if raises is not None:
            raise raises
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(pd, "run_bounded", fake)
    return calls


@pytest.fixture
def no_local_engine(monkeypatch) -> None:
    """No local engine is discoverable: the doctor block skips its liveness probe and runs doctor."""
    from nexus import health

    monkeypatch.setattr(health, "_resolve_service_endpoint", lambda _config_dir: None)


def test_nx_doctor_gets_its_own_bound_longer_than_a_healthy_run(monkeypatch, no_local_engine) -> None:
    """A healthy ``nx doctor`` took 44.9 s (105 steps) on Windows; the 15 s tool bound made check 2 impossible to pass."""
    calls = _fake_bounded(monkeypatch, stdout="doctor: ok\n")
    lines = pd.nx_doctor_block()
    (_, kw), = calls
    assert kw["timeout"] == pd._DOCTOR_TIMEOUT
    assert pd._DOCTOR_TIMEOUT >= 90
    assert "Status: PASS" in lines


def test_nx_doctor_timeout_says_it_timed_out_and_is_not_a_failure(monkeypatch, no_local_engine) -> None:
    import subprocess

    _fake_bounded(monkeypatch, raises=subprocess.TimeoutExpired(["nx", "doctor"], pd._DOCTOR_TIMEOUT))
    text = "\n".join(pd.nx_doctor_block())
    assert f"exceeded {pd._DOCTOR_TIMEOUT}s" in text
    assert "not a failure" in text and "run 'nx doctor' directly" in text
    assert "unavailable" not in text


def _answering_engine(status: int):
    """A real HTTP server on port 0 whose /health answers *status*; returns ``(server, port)`` (caller shuts it down)."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(status)
            self.end_headers()

        def log_message(self, *_a):  # noqa: ANN002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _closed_port() -> int:
    """A port nothing listens on: bound, then released, so a connect is refused."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("status", [200, 401, 404, 500, 502, 503], ids=["ok", "401", "404", "500", "proxy-502", "db-down-503"])
def test_any_http_answer_means_the_engine_answers_and_doctor_runs(monkeypatch, status: int) -> None:
    """The first probe mapped every HTTP error but 503 to "did not answer" and skipped doctor on an engine that had."""
    from nexus import health

    server, port = _answering_engine(status)
    try:
        monkeypatch.setattr(health, "_resolve_service_endpoint", lambda _config_dir: ("127.0.0.1", port))
        calls = _fake_bounded(monkeypatch, stdout="doctor: ok\n")
        lines = pd.nx_doctor_block()
    finally:
        server.shutdown()
        server.server_close()
    (_, kw), = calls
    assert kw["timeout"] == pd._DOCTOR_TIMEOUT and "Status: PASS" in lines


def test_a_refused_connection_runs_doctor_because_it_is_fast_there_and_says_what_to_do(monkeypatch) -> None:
    """Nothing listening is the engine being down: doctor fails fast and carries the fix steps and its other checks."""
    from nexus import health

    monkeypatch.setattr(health, "_resolve_service_endpoint", lambda _config_dir: ("127.0.0.1", _closed_port()))
    calls = _fake_bounded(monkeypatch, returncode=1, stdout="Storage service unreachable: start it with nx daemon service start\n")
    text = "\n".join(pd.nx_doctor_block())
    assert len(calls) == 1, "doctor must run when the connection is refused"
    assert "nx daemon service start" in text and "Status: FAIL" in text


def test_a_listener_that_accepts_and_never_replies_is_a_warn_naming_the_engine_and_doctor_is_not_run(monkeypatch) -> None:
    """The one case that skips doctor: a wedged engine, where doctor would wait on the same engine for its whole bound."""
    import socket

    from nexus import health

    wedged = socket.socket()
    wedged.bind(("127.0.0.1", 0))
    wedged.listen(8)  # the kernel completes the handshake; nothing ever reads or answers
    port = wedged.getsockname()[1]
    try:
        monkeypatch.setattr(health, "_resolve_service_endpoint", lambda _config_dir: ("127.0.0.1", port))
        monkeypatch.setattr(pd, "_ENGINE_PROBE_TIMEOUT", 0.3)
        calls = _fake_bounded(monkeypatch, stdout="doctor: ok\n")
        text = "\n".join(pd.nx_doctor_block())
    finally:
        wedged.close()
    assert calls == [], "doctor must not run behind a wedged engine"
    assert "Status: WARN" in text and f"127.0.0.1:{port}" in text and "engine" in text
    assert "run 'nx doctor' directly" in text and "unavailable" not in text and "SKIP" not in text


def test_a_probe_that_cannot_be_made_never_costs_the_doctor_run(monkeypatch) -> None:
    from nexus import health

    def boom(_config_dir):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(health, "_resolve_service_endpoint", boom)
    calls = _fake_bounded(monkeypatch, stdout="doctor: ok\n")
    assert "Status: PASS" in pd.nx_doctor_block() and len(calls) == 1
