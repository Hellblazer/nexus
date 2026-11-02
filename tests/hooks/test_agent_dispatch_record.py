# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The SubagentStop nudge records each dispatch's T1 put count through a
detached child (nexus-mtheb.4).

The engine side is a stub HTTP server on port 0 standing in for the
``/v1/telemetry/agent_dispatches/record`` route; the child is a real
``python -m nexus.hooks.agent_dispatch_record`` process pointed at it by
``NX_SERVICE_URL``, so the spawn, the module entry and the one bounded
request all run for real.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from nexus.hooks import agent_dispatch_record as rec
from nexus.hooks import subagent_stop_t1_nudge as nudge
from tests._child_encoding import PYTHON_CHILD_ENCODING
from tests._hook_wiring import REPO_ROOT
from tests._module_seam import setattr_in

ROUTE = "/v1/telemetry/agent_dispatches/record"


class _Engine:
    """A stub engine: records each POST body, answers with *status* after *delay*."""

    def __init__(self, status: int = 200, delay: float = 0.0) -> None:
        self.bodies: list[tuple[str, dict]] = []
        self.auth: list[str] = []
        self.arrived = threading.Event()
        engine = self

        class _H(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server API
                n = int(self.headers.get("Content-Length", "0"))
                engine.auth.append(self.headers.get("Authorization", ""))
                engine.bodies.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
                engine.arrived.set()
                time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b"{}")
                except OSError:
                    pass

            def log_message(self, *_a: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def engine_env(monkeypatch: pytest.MonkeyPatch):
    """Point both this process and any child at a fresh stub engine."""
    made: list[_Engine] = []

    def make(status: int = 200, delay: float = 0.0) -> _Engine:
        eng = _Engine(status, delay)
        made.append(eng)
        monkeypatch.setenv("NX_SERVICE_URL", eng.url)
        monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")
        monkeypatch.delenv(rec.OFF_ENV, raising=False)
        return eng

    yield make
    for eng in made:
        eng.close()


def _child_env() -> dict[str, str]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    env.pop(rec.OFF_ENV, None)
    return env


# --- the child -------------------------------------------------------------


def test_the_child_posts_one_row_with_the_payload(engine_env) -> None:
    eng = engine_env()
    payload = {"session_id": "s1", "agent_id": "a1", "agent_type": "developer", "t1_puts": 2}
    proc = subprocess.run(
        [sys.executable, "-m", "nexus.hooks.agent_dispatch_record", json.dumps(payload)],
        capture_output=True, text=True, encoding=PYTHON_CHILD_ENCODING,
        env=_child_env(), timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert len(eng.bodies) == 1
    path, body = eng.bodies[0]
    assert path == ROUTE
    assert body["session_id"] == "s1"
    assert body["agent_id"] == "a1"
    assert body["agent_type"] == "developer"
    assert body["t1_puts"] == 2


@pytest.mark.parametrize("status", [404, 503])
def test_an_engine_that_refuses_leaves_the_child_silent_and_zero(engine_env, status: int) -> None:
    eng = engine_env(status=status)
    assert rec.main([json.dumps({"session_id": "s1", "agent_id": "a1", "t1_puts": 0})]) == 0
    assert len(eng.bodies) == 1, "one request, no retry"


def test_a_hung_engine_is_cut_off_by_the_request_timeout(engine_env) -> None:
    engine_env(delay=10.0)
    start = time.monotonic()
    assert rec.main([json.dumps({"session_id": "s1", "agent_id": "a1", "t1_puts": 0})]) == 0
    assert time.monotonic() - start < rec._REQUEST_TIMEOUT_S + 2.0


@pytest.mark.parametrize("arg", ["not json", "[]", json.dumps({"agent_id": "a1"})])
def test_a_bad_payload_is_silent_and_zero(engine_env, arg: str) -> None:
    eng = engine_env()
    assert rec.main([arg]) == 0
    assert eng.bodies == []


# --- the spawn -------------------------------------------------------------


def test_spawn_starts_a_detached_child_that_records_and_returns_at_once(engine_env) -> None:
    # The engine answers after 3 s, so a spawn that waited on its child
    # could not come back inside the 1 s bound.
    eng = engine_env(delay=3.0)
    start = time.monotonic()
    assert rec.spawn("s9", "a9", "developer", 3) is True
    assert time.monotonic() - start < 1.0, "the hook never waits on the child"
    assert eng.arrived.wait(30), "the detached child never reached the engine"
    _path, body = eng.bodies[0]
    assert (body["session_id"], body["agent_id"], body["t1_puts"]) == ("s9", "a9", 3)


@pytest.mark.parametrize("value", ["off", "OFF", "0"])
def test_the_off_switch_spawns_nothing(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(rec.OFF_ENV, value)
    started: list[object] = []
    setattr_in(monkeypatch, rec, "subprocess.Popen", lambda *a, **k: started.append(a))
    assert rec.spawn("s1", "a1", "developer", 0) is False
    assert started == []


@pytest.mark.parametrize(("sid", "aid"), [("", "a1"), ("s1", "")])
def test_no_identity_spawns_nothing(monkeypatch: pytest.MonkeyPatch, sid: str, aid: str) -> None:
    monkeypatch.delenv(rec.OFF_ENV, raising=False)
    started: list[object] = []
    setattr_in(monkeypatch, rec, "subprocess.Popen", lambda *a, **k: started.append(a))
    assert rec.spawn(sid, aid, "developer", 0) is False
    assert started == []


def test_a_failing_popen_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(rec.OFF_ENV, raising=False)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("no fork")

    setattr_in(monkeypatch, rec, "subprocess.Popen", boom)
    assert rec.spawn("s1", "a1", "developer", 0) is False


def test_the_posix_spawn_leaves_the_hook_session_and_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "nt":
        pytest.skip("POSIX spawn flags")
    monkeypatch.delenv(rec.OFF_ENV, raising=False)
    seen: list[tuple[list[str], dict]] = []

    class _P:
        def __init__(self, argv: list[str], **kw: object) -> None:
            seen.append((argv, kw))

    setattr_in(monkeypatch, rec, "subprocess.Popen", _P)
    assert rec.spawn("s1", "a1", "developer", 4) is True
    [(argv, kw)] = seen
    assert argv[:3] == [sys.executable, "-m", "nexus.hooks.agent_dispatch_record"]
    assert json.loads(argv[3]) == {"session_id": "s1", "agent_id": "a1", "agent_type": "developer", "t1_puts": 4}
    assert kw["start_new_session"] is True
    for stream in ("stdin", "stdout", "stderr"):
        assert kw[stream] == subprocess.DEVNULL


# --- the nudge calls it ------------------------------------------------------


def test_the_nudge_counter_seam_spawns_the_recorder(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(rec, "spawn", lambda *a: calls.append(a) or True)
    nudge.record_dispatch("s1", "a1", "developer", 2)
    assert calls == [("s1", "a1", "developer", 2)]


def test_importing_the_recorder_pulls_in_only_stdlib_and_nexus_modules() -> None:
    """What the recorder module adds on top of its package. ``nexus`` and
    ``nexus.hooks`` are imported first because the nudge already has them
    loaded, and ``nexus/__init__`` injects truststore on Windows, which loads
    certifi and urllib3 (the Windows set went red on that, not on the recorder)."""
    probe = (
        "import sys\n"
        "import nexus, nexus.hooks\n"
        "before = set(sys.modules)\n"
        "import nexus.hooks.agent_dispatch_record\n"
        "new = set(sys.modules) - before\n"
        "bad = sorted(m for m in new if m.split('.')[0] not in sys.stdlib_module_names"
        " and not m.startswith('nexus') and not m.startswith('_'))\n"
        "print(','.join(bad))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True,
        encoding=PYTHON_CHILD_ENCODING, env=_child_env(), check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


# --- review r1: the token, the pin, the watchdog, silence ---------------------


def test_a_minted_data_token_is_the_bearer(engine_env, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = engine_env()
    import nexus.db.data_token as dt  # noqa: PLC0415 — the manager is replaced where the child reads it

    class _Mgr:
        def bearer_for(self, _base_url: str, _tenant: str) -> str:
            return "minted-data-token"

    monkeypatch.setattr(dt, "get_data_token_manager", lambda: _Mgr())
    assert rec.main([json.dumps({"session_id": "s1", "agent_id": "a1", "t1_puts": 1})]) == 0
    assert eng.auth == ["Bearer minted-data-token"]


def test_the_endpoint_is_pinned_so_the_stores_own_resolution_never_runs(
    engine_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = engine_env()
    import nexus.db.service_endpoint as se  # noqa: PLC0415 — the gated resolver is replaced where the store reads it

    def gated(*_a: object, **_k: object) -> tuple[str, str]:
        raise AssertionError("the store resolved its own endpoint (12 s lease wait on a restart)")

    monkeypatch.setattr(se, "resolve_service_endpoint_with_evidence_gate", gated)
    assert rec.main([json.dumps({"session_id": "s1", "agent_id": "a1", "t1_puts": 1})]) == 0
    assert len(eng.bodies) == 1


def test_the_watchdog_ends_a_hung_child() -> None:
    code = (
        "import time, nexus.hooks.agent_dispatch_record as r\n"
        "r._arm_watchdog(1.0)\n"
        "time.sleep(60)\n"
    )
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        encoding=PYTHON_CHILD_ENCODING, env=_child_env(), timeout=30, check=False,
    )
    assert proc.returncode == 0
    assert time.monotonic() - start < 10.0


def test_the_module_entry_arms_the_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    armed: list[float] = []
    monkeypatch.setattr(rec, "_arm_watchdog", armed.append)
    monkeypatch.setattr(rec, "main", lambda argv=None: 0)
    assert rec.run_child([]) == 0
    assert armed == [rec._CHILD_LIFETIME_S]


def test_a_failing_child_writes_nothing(engine_env) -> None:
    engine_env(status=503)
    payload = json.dumps({"session_id": "s1", "agent_id": "a1", "t1_puts": 0})
    proc = subprocess.run(
        [sys.executable, "-m", "nexus.hooks.agent_dispatch_record", payload],
        capture_output=True, text=True, encoding=PYTHON_CHILD_ENCODING,
        env=_child_env(), timeout=60, check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_the_windows_spawn_tries_breakaway_then_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows branch, with a fake Popen: breakaway first; when the job
    refuses it, the same flags without breakaway (the _session_end_launcher
    pattern). Never DETACHED_PROCESS."""
    monkeypatch.delenv(rec.OFF_ENV, raising=False)
    setattr_in(monkeypatch, rec, "os.name", "nt")
    flags: list[int] = []

    class _P:
        def __init__(self, _argv: list[str], **kw: object) -> None:
            flags.append(int(kw["creationflags"]))  # type: ignore[call-overload]
            assert "start_new_session" not in kw
            if len(flags) == 1:
                raise PermissionError("job forbids breakaway")

    setattr_in(monkeypatch, rec, "subprocess.Popen", _P)
    assert rec.spawn("s1", "a1", "developer", 1) is True
    base = rec._CREATE_NO_WINDOW | rec._CREATE_NEW_PROCESS_GROUP
    assert flags == [base | rec._CREATE_BREAKAWAY_FROM_JOB, base]
    assert not any(f & 0x00000008 for f in flags), "DETACHED_PROCESS"


def test_the_module_entry_point_is_run_child_so_the_watchdog_is_armed() -> None:
    """``python -m`` runs the ``__main__`` block; it must go through run_child,
    or the child runs with no watchdog (confirmation r2 of the counter)."""
    import ast  # noqa: PLC0415 — test-local

    tree = ast.parse(Path(rec.__file__).read_text(encoding="utf-8"))
    mains = [
        node for node in tree.body
        if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test)
    ]
    assert len(mains) == 1
    called = {n.func.id for n in ast.walk(mains[0]) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "run_child" in called
    assert "main" not in called


def test_the_child_lifetime_stays_short() -> None:
    """Eight seconds: past the 2 s request and a warm token step, short of a
    cold mint, and no cap a fan-out of children could pile up behind."""
    assert rec._CHILD_LIFETIME_S == 8.0
