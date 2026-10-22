"""nexus-m4lej: a routing_events row carries the hook payload's session_id.

The credential-print guard and the subagent git-write guard called
``_lib.log_routing_event`` without ``session_id`` at all seven sites, so
every row they posted had an empty session. ``run_hook`` now records the
payload's session and ``log_routing_event`` defaults to it. These cases run
the real scripts against a stdlib HTTP server on port 0 and read the body
each one POSTed.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROUTING = pathlib.Path(__file__).parent.parent / "conexus" / "hooks" / "scripts" / "routing"
SESSION = "m4lej-session-0001"


@pytest.fixture()
def engine(tmp_path):
    """A fake engine that records every routing_events POST body."""
    bodies: list[dict] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 -- http.server's naming
            length = int(self.headers.get("Content-Length") or 0)
            if self.path == "/v1/telemetry/routing_events/record":
                bodies.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format, *args):  # noqa: A002 -- base signature
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = os.environ.copy()
    env.update({
        "NX_HOOK_PYTHON": sys.executable,
        "NEXUS_CONFIG_DIR": str(tmp_path / "isolated-nexus-config"),
        "NX_ROUTING_LOG_PATH": str(tmp_path / "routing_log.jsonl"),
        "NX_DROPPED_WRITES_LOG_PATH": str(tmp_path / "dropped_writes.jsonl"),
        "NX_SERVICE_HOST": "127.0.0.1",
        "NX_SERVICE_PORT": str(server.server_address[1]),
        "NX_SERVICE_TOKEN": "test-token",
    })
    env.pop("NX_SERVICE_URL", None)
    try:
        yield bodies, env
    finally:
        server.shutdown()
        server.server_close()


def _run(script: str, payload: dict, env: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(ROUTING / script)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=20, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]


def test_credential_print_guard_deny_row_carries_session(engine):
    bodies, env = engine
    out = _run(
        "credential_print_guard.py",
        {"tool_name": "Bash", "tool_input": {"command": "cat /proc/1234/environ"},
         "session_id": SESSION},
        env,
    )
    assert out["permissionDecision"] == "deny"
    assert [b["outcome"] for b in bodies] == ["deny"], bodies
    assert bodies[0]["session_id"] == SESSION


def test_subagent_git_write_deny_row_carries_session(engine, tmp_path):
    bodies, env = engine
    repo = tmp_path / "shared"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    out = _run(
        "subagent_git_write_requires_orchestrator.py",
        {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"},
         "agent_id": "aworker-x-6f59dab8bbb14864", "agent_type": "worker-x",
         "cwd": str(repo), "session_id": SESSION},
        env,
    )
    assert out["permissionDecision"] == "deny"
    assert [b["outcome"] for b in bodies] == ["deny"], bodies
    assert bodies[0]["session_id"] == SESSION


def test_explicit_session_id_wins_over_payload(engine):
    """A caller that passes session_id keeps it; the payload is only the default."""
    bodies, env = engine
    stub = (
        f"import sys; sys.path.insert(0, {str(ROUTING)!r}); import _lib\n"
        "def body(p):\n"
        "    _lib.log_routing_event(rule='r', outcome='deny', session_id='explicit')\n"
        "    _lib.log_routing_event(rule='r', outcome='allow')\n"
        "    _lib.pass_through()\n"
        "_lib.run_hook(body, rule_name='r')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", stub], input=json.dumps({"session_id": SESSION}),
        capture_output=True, text=True, timeout=20, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert [b["session_id"] for b in bodies] == ["explicit", SESSION], bodies
