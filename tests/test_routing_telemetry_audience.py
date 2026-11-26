# SPDX-License-Identifier: AGPL-3.0-or-later
"""``_lib.log_routing_event`` records only when the user's ``telemetry`` setting is true (nexus-sby49.3 ledger row "Telemetry";
reworked in nexus-sby49.4: opt-in, off by default, a project cannot turn it on).

A routing_events row carries the user's command fragment to their engine (the managed service in cloud mode), so it is sent only
on the user's own say-so, in ``~/.config/nexus/conexus.json``. The guards still decide the same way for everyone; only the row
is gone.

* in-process: the recorder itself, with the endpoint call stubbed;
* end to end: the real credential guard through ``tests/hooks/audience_harness.py`` (so the account is named), against a
  stdlib HTTP server on port 0, on a plain account, an account with telemetry on and a project that asks for it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.hooks.audience_harness import plain_home, plain_repo, strict_repo, write_user_settings

ROOT = pathlib.Path(__file__).parent.parent
SCRIPTS = ROOT / "conexus" / "hooks" / "scripts"
LIB_PATH = SCRIPTS / "routing" / "_lib.py"
HARNESS = ROOT / "tests" / "hooks" / "audience_harness.py"
GUARD = SCRIPTS / "routing" / "credential_print_guard.py"


def _load_lib():
    spec = importlib.util.spec_from_file_location("nx_routing_lib_audience", LIB_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    sent: list[dict] = []
    module._send_routing_event = lambda record: sent.append(record)
    return module, sent


# --- the recorder -------------------------------------------------------------------------------


def test_the_default_records_nothing(plugin_account, tmp_path) -> None:
    lib, sent = _load_lib()
    lib._payload = {"cwd": plain_repo(tmp_path / "users-repo"), "session_id": "s"}
    lib.log_routing_event(rule="r", outcome="deny", tool_name="Bash", command_fragment="cat secret")
    assert sent == []


def test_no_payload_and_no_setting_records_nothing(plugin_account) -> None:
    lib, sent = _load_lib()
    lib.log_routing_event(rule="r", outcome="allow")
    assert sent == []


def test_the_user_setting_records_from_any_repo(plugin_account, tmp_path) -> None:
    plugin_account(telemetry=True)
    lib, sent = _load_lib()
    lib._payload = {"cwd": plain_repo(tmp_path / "other-repo")}
    lib.log_routing_event(rule="r", outcome="deny", command_fragment="x")
    assert [r["rule"] for r in sent] == ["r"]


def test_a_project_file_cannot_turn_the_recording_on(plugin_account, tmp_path) -> None:
    """Tighten-only: a tracked ``.nexus/project.json`` that says ``telemetry: true`` is ignored."""
    lib, sent = _load_lib()
    lib._payload = {"cwd": strict_repo(tmp_path / "project", telemetry=True)}
    lib.log_routing_event(rule="r", outcome="deny")
    assert sent == []


def test_a_strict_project_records_nothing_without_the_user_setting(plugin_account, tmp_path) -> None:
    lib, sent = _load_lib()
    lib._payload = {"cwd": strict_repo(tmp_path / "nexus")}
    lib.log_routing_event(rule="r", outcome="deny")
    assert sent == []


def test_run_hook_hands_the_payload_to_the_recorder(plugin_account, tmp_path, monkeypatch) -> None:
    plugin_account(telemetry=True)
    lib, sent = _load_lib()
    payload = {"cwd": plain_repo(tmp_path / "repo"), "session_id": "sid-1"}
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(payload)))

    def body(p):
        lib.log_routing_event(rule="r", outcome="deny")
        lib.pass_through()

    with pytest.raises(SystemExit):
        lib.run_hook(body, rule_name="r")
    assert [(r["rule"], r["session_id"]) for r in sent] == [("r", "sid-1")]


def test_a_reader_that_raises_records_nothing(plugin_account, tmp_path, monkeypatch) -> None:
    """The recorder sends data, so a failing reader answers False: a broken helper records nothing."""
    plugin_account(telemetry=True)
    audience = sys.modules["_audience"]

    def boom(*_a, **_k):
        raise RuntimeError("helper broke")

    monkeypatch.setattr(audience, "account_home", boom)
    lib, sent = _load_lib()
    lib._payload = {"cwd": plain_repo(tmp_path / "users-repo")}
    lib.log_routing_event(rule="r", outcome="deny")
    assert sent == []


def test_a_helper_that_cannot_import_records_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(sys.modules, "_audience", None)  # an import of it now raises ImportError
    lib, sent = _load_lib()
    assert lib._telemetry_enabled() is False
    lib.log_routing_event(rule="r", outcome="deny")
    assert sent == []


# --- end to end ---------------------------------------------------------------------------------


@pytest.fixture()
def engine(tmp_path):
    bodies: list[dict] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 -- http.server's naming
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            if self.path == "/v1/telemetry/routing_events/record":
                bodies.append(json.loads(raw))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format, *args):  # noqa: A002 -- base signature
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = os.environ.copy()
    env.update({
        "NX_HOOK_PYTHON": sys.executable,
        "NEXUS_CONFIG_DIR": str(tmp_path / "isolated-nexus-config"),
        "NX_DROPPED_WRITES_LOG_PATH": str(tmp_path / "dropped.jsonl"),
        "NX_SERVICE_HOST": "127.0.0.1",
        "NX_SERVICE_PORT": str(server.server_address[1]),
        "NX_SERVICE_TOKEN": "test-token",
    })
    env.pop("NX_SERVICE_URL", None)
    try:
        yield bodies, env, tmp_path / "dropped.jsonl"
    finally:
        server.shutdown()
        server.server_close()


def _credential_read(cwd: str, home: str, env: dict) -> dict:
    payload = {"tool_name": "Bash", "tool_input": {"command": "cat /proc/1234/environ"}, "session_id": "s-audience", "cwd": cwd}
    proc = subprocess.run(
        [sys.executable, str(HARNESS), home, str(GUARD)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]


def _settled(bodies: list, dropped: pathlib.Path, *, expect: bool) -> bool:
    """The post is a detached child's: wait for it when one is expected; when none is, give a child the time it would take."""
    deadline = time.monotonic() + (15.0 if expect else 3.0)
    while time.monotonic() < deadline:
        if bodies or (dropped.exists() and dropped.read_text().strip()):
            return True
        time.sleep(0.05)
    return False


def test_the_credential_deny_is_unchanged_and_sends_no_row_by_default(engine, tmp_path) -> None:
    bodies, env, dropped = engine
    out = _credential_read(plain_repo(tmp_path / "users-repo"), plain_home(tmp_path / "acct"), env)
    assert out["permissionDecision"] == "deny", "the credential guard is every user's (U7a)"
    assert not _settled(bodies, dropped, expect=False), (bodies, dropped.exists() and dropped.read_text())


def test_a_project_that_asks_for_telemetry_sends_no_row(engine, tmp_path) -> None:
    bodies, env, dropped = engine
    out = _credential_read(strict_repo(tmp_path / "nexus", telemetry=True), plain_home(tmp_path / "acct"), env)
    assert out["permissionDecision"] == "deny"
    assert not _settled(bodies, dropped, expect=False), (bodies, dropped.exists() and dropped.read_text())


def test_a_user_with_telemetry_on_sends_the_row_from_any_repo(engine, tmp_path) -> None:
    bodies, env, dropped = engine
    out = _credential_read(plain_repo(tmp_path / "other"), write_user_settings(tmp_path / "acct", telemetry=True), env)
    assert out["permissionDecision"] == "deny"
    assert _settled(bodies, dropped, expect=True) and [b["outcome"] for b in bodies] == ["deny"]
