# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx command-context`` preamble blocks: ``nx doctor``'s own bound and liveness probe, and a ``bd`` with no database (GH #1689).

Split out of ``tests/test_command_context_command.py``, which is at its file-size ceiling. Fakes replace only the runner
(``run_bounded``) or the engine address; the engine probe and the child-process ``bd`` tests use real I/O.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# GH #1689: nx doctor's own bound, and a bd with no database
# ---------------------------------------------------------------------------


def _fake_bounded(monkeypatch, cc, *, returncode: int = 0, stdout: str = "", stderr: str = "", raises: Exception | None = None):
    """Replace the preamble's ``run_bounded``; returns the list of ``(argv, kwargs)`` it was called with."""
    import types

    calls: list[tuple[list[str], dict]] = []

    def fake(argv, **kw):  # noqa: ANN001, ANN003
        calls.append((list(argv), kw))
        if raises is not None:
            raise raises
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(cc, "run_bounded", fake)
    return calls


@pytest.fixture
def no_local_engine(monkeypatch) -> None:
    """No local engine is discoverable: the doctor block skips its liveness probe and runs doctor."""
    from nexus import health

    monkeypatch.setattr(health, "_resolve_service_endpoint", lambda _config_dir: None)


def test_nx_doctor_gets_its_own_bound_longer_than_a_healthy_run(monkeypatch, no_local_engine) -> None:
    """A healthy ``nx doctor`` took 44.9 s (105 steps) on Windows; the 15 s tool bound made check 2 impossible to pass."""
    from nexus.commands import command_context as cc

    calls = _fake_bounded(monkeypatch, cc, stdout="doctor: ok\n")
    lines = cc.nx_doctor_block()
    (_, kw), = calls
    assert kw["timeout"] == cc._DOCTOR_TIMEOUT
    assert cc._DOCTOR_TIMEOUT >= 90 > cc._PREAMBLE_TIMEOUT
    assert "Status: PASS" in lines


def test_nx_doctor_timeout_says_it_timed_out_and_is_not_a_failure(monkeypatch, no_local_engine) -> None:
    import subprocess

    from nexus.commands import command_context as cc

    _fake_bounded(monkeypatch, cc, raises=subprocess.TimeoutExpired(["nx", "doctor"], cc._DOCTOR_TIMEOUT))
    text = "\n".join(cc.nx_doctor_block())
    assert f"exceeded {cc._DOCTOR_TIMEOUT}s" in text
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


def test_a_local_engine_that_does_not_answer_is_reported_at_once_and_doctor_is_not_run(monkeypatch) -> None:
    """A wedged engine must not stall the synchronous preamble for the whole doctor bound: probe /health first."""
    import socket

    from nexus import health
    from nexus.commands import command_context as cc

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]  # bound then released: nothing listens, connection is refused
    monkeypatch.setattr(health, "_resolve_service_endpoint", lambda _config_dir: ("127.0.0.1", closed_port))
    calls = _fake_bounded(monkeypatch, cc, stdout="doctor: ok\n")
    text = "\n".join(cc.nx_doctor_block())
    assert calls == [], "doctor must not run when the engine does not answer"
    assert "Status: SKIP - engine not answering" in text and f"127.0.0.1:{closed_port}" in text
    assert "run 'nx doctor' directly" in text and "unavailable" not in text


@pytest.mark.parametrize("status", [200, 503], ids=["healthy", "db-down-still-answers"])
def test_a_local_engine_that_answers_gets_doctor_under_the_long_bound(monkeypatch, status: int) -> None:
    from nexus import health
    from nexus.commands import command_context as cc

    server, port = _answering_engine(status)
    try:
        monkeypatch.setattr(health, "_resolve_service_endpoint", lambda _config_dir: ("127.0.0.1", port))
        calls = _fake_bounded(monkeypatch, cc, stdout="doctor: ok\n")
        lines = cc.nx_doctor_block()
    finally:
        server.shutdown()
        server.server_close()
    (_, kw), = calls
    assert kw["timeout"] == cc._DOCTOR_TIMEOUT and "Status: PASS" in lines


def test_the_other_preamble_calls_keep_the_short_bound(monkeypatch, tmp_path: Path) -> None:
    from nexus.commands import command_context as cc

    calls = _fake_bounded(monkeypatch, cc, stdout="x\n")
    cc.beads_block(tmp_path, args=[], heading="### Beads")
    assert [kw["timeout"] for _, kw in calls] == [cc._PREAMBLE_TIMEOUT]


_NO_DB_STDERR = "Error: no beads database found\nHint: run 'bd init' to create a database in the current directory\n"


@pytest.mark.parametrize(
    "call",
    [
        lambda cc, root: cc.beads_block(root, args=["--status=in_progress"], heading="### Beads"),
        lambda cc, root: cc._ready_beads_block(root),
    ],
    ids=["beads_block", "ready_beads_block"],
)
def test_a_bd_with_no_database_is_reported_as_such_not_as_unavailable(monkeypatch, tmp_path: Path, call) -> None:
    """bd is installed and says so on stderr (exit 1); 'unavailable or no results' was false (GH #1689)."""
    from nexus.commands import command_context as cc

    _fake_bounded(monkeypatch, cc, returncode=1, stderr=_NO_DB_STDERR)
    text = "\n".join(call(cc, tmp_path))
    assert "no beads database" in text
    assert "unavailable" not in text and "no results" not in text


@pytest.mark.parametrize(
    "call",
    [
        lambda cc, root: cc.beads_block(root, args=[], heading=None),
        lambda cc, root: cc._ready_beads_block(root),
    ],
    ids=["beads_block", "ready_beads_block"],
)
def test_a_bd_that_fails_for_another_reason_says_what_it_said(monkeypatch, tmp_path: Path, call) -> None:
    from nexus.commands import command_context as cc

    _fake_bounded(monkeypatch, cc, returncode=2, stderr="Error: dolt server unreachable\n")
    text = "\n".join(call(cc, tmp_path))
    assert "exit 2" in text and "dolt server unreachable" in text
    assert "no beads database" not in text


@pytest.mark.parametrize(
    "call",
    [
        lambda cc, root: cc.beads_block(root, args=[], heading=None),
        lambda cc, root: cc._ready_beads_block(root),
    ],
    ids=["beads_block", "ready_beads_block"],
)
def test_a_missing_or_slow_bd_is_named_for_what_it_is(monkeypatch, tmp_path: Path, call) -> None:
    import subprocess

    from nexus.commands import command_context as cc

    _fake_bounded(monkeypatch, cc, raises=FileNotFoundError("bd"))
    assert "not installed" in "\n".join(call(cc, tmp_path))
    _fake_bounded(monkeypatch, cc, raises=subprocess.TimeoutExpired(["bd"], cc._PREAMBLE_TIMEOUT))
    assert "timed out" in "\n".join(call(cc, tmp_path))


def test_bd_with_a_database_and_no_rows_still_says_none(monkeypatch, tmp_path: Path) -> None:
    from nexus.commands import command_context as cc

    _fake_bounded(monkeypatch, cc, returncode=0, stdout="\n")
    assert cc.beads_block(tmp_path, args=[], heading=None) == ["- (no beads found)"]
    assert cc._ready_beads_block(tmp_path) == ["### Ready beads (top 10)", "- (none)"]


# The measured bd v1.0.4 behaviour with no database (reporter's Windows run, GH #1689): exit 1, EMPTY stdout, text on
# stderr only. The block builders used to drop stderr and the exit code, so a detector fed from them never matched; these
# tests run a real child process instead of faking the runner, so a regression that drops stderr again goes red.
_BD_NO_DB_STDERR = (
    "Error: no beads database found\n"
    "Hint: run 'bd where' to inspect the resolved workspace, or 'bd init' to create a new database\n"
    "      or set BEADS_DIR to point to your .beads directory\n"
)


@pytest.fixture
def bd_without_database(tmp_path: Path, monkeypatch) -> Path:
    """A real ``bd`` on PATH that behaves as bd v1.0.4 does where there is no beads database."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    bd = bindir / "bd"
    bd.write_text("#!/bin/sh\nprintf '%s' \"$BD_FAKE_STDERR\" >&2\nexit 1\n")
    bd.chmod(0o755)
    monkeypatch.setenv("BD_FAKE_STDERR", _BD_NO_DB_STDERR.replace("\n", "\n"))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("BEADS_DIR", raising=False)
    work = tmp_path / "repo"
    work.mkdir()
    return work


_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="the fake bd is a shell script; Windows refuses a .cmd shim for bd by design")


@_POSIX_ONLY
def test_real_bd_with_no_database_through_the_preamble_blocks(bd_without_database: Path) -> None:
    from nexus.commands import command_context as cc

    for lines in (cc.beads_block(bd_without_database, args=[], heading=None), cc._ready_beads_block(bd_without_database)):
        text = "\n".join(lines)
        assert "no beads database" in text, text
        assert "unavailable" not in text and "no results" not in text, text


@_POSIX_ONLY
def test_real_bd_with_no_database_through_the_brief_and_the_hook_render(bd_without_database: Path) -> None:
    from nexus import brief
    from nexus.hooks import session_context

    rendered = "\n".join(session_context._render_beads(bd_without_database))
    assert "no beads database" in rendered, rendered
    text = brief.render("beads", cwd=str(bd_without_database)).text
    assert "no beads database" in text and "printed nothing" not in text, text
