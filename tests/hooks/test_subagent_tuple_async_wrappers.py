# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-205 Phase 2 Step 3 (bead nexus-em75s.11): the two async projection
entries beside subagent-start.sh/subagent-start-stamp.sh (SubagentStart)
and subagent-stop.sh (SubagentStop).

CA 4 ("the hook-path write can be projected to the engine without ever
blocking a dispatch") is pinned here at the SCRIPT level, independent of
whether the installed harness honors ``async: true`` on a hooks.json
entry: research 5 measured a detached child with all three fds on
``/dev/null`` at 18ms regardless, and these wrapper scripts are built to
that exact shape, so even a harness that silently treated this hooks.json
entry as an ordinary BLOCKING hook would still see it return in tens of
milliseconds, never the seconds (or the curl timeout ceiling) a slow or
down engine could otherwise cost.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
START_ASYNC = SCRIPTS_DIR / "subagent-start-tuple-async.sh"
STOP_ASYNC = SCRIPTS_DIR / "subagent-stop-tuple-async.sh"
PROJECT_PY = SCRIPTS_DIR / "tuple_ledger_project.py"

SESSION_ID = "sess-async-wrap"
AGENT_ID = "aworkerasyncwrap"
AGENT_TYPE = "developer"


def _payload() -> str:
    return json.dumps({
        "session_id": SESSION_ID,
        "hook_event_name": "SubagentStart",
        "agent_id": AGENT_ID,
        "agent_type": AGENT_TYPE,
    })


def _log_path(state_dir: Path) -> Path:
    return state_dir / "nexus" / "orchestration" / f"{SESSION_ID}.tuple-projection.log"


def _run_wrapper(
    script: Path, tmp_path: Path, *, base_url: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], float]:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    env = {k: v for k, v in os.environ.items() if not k.startswith("NX_SERVICE_")}
    env["NEXUS_CONFIG_DIR"] = str(config_dir)
    env["XDG_STATE_HOME"] = str(state_dir)
    if base_url is not None:
        env["NX_SERVICE_URL"] = base_url
    start = time.monotonic()
    proc = subprocess.run(
        ["bash", str(script)],
        input=_payload(),
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    elapsed = time.monotonic() - start
    return proc, elapsed


def test_scripts_exist_and_are_executable() -> None:
    for script in (START_ASYNC, STOP_ASYNC):
        assert script.exists(), f"missing: {script}"
        assert os.access(script, os.X_OK), f"not executable: {script}"


def test_start_wrapper_returns_immediately_with_no_endpoint_resolvable(tmp_path: Path) -> None:
    """No storage lease, no env: the projector itself will SKIP (nothing
    to POST to), but the WRAPPER must not wait around to find that out --
    it backgrounds the work and returns."""
    proc, elapsed = _run_wrapper(START_ASYNC, tmp_path)
    assert proc.returncode == 0
    assert elapsed < 5.0, f"wrapper took {elapsed:.2f}s -- must return near-instantly"


def test_stop_wrapper_returns_immediately(tmp_path: Path) -> None:
    proc, elapsed = _run_wrapper(STOP_ASYNC, tmp_path)
    assert proc.returncode == 0
    assert elapsed < 5.0, f"wrapper took {elapsed:.2f}s -- must return near-instantly"


def test_wrapper_returns_immediately_even_against_an_unreachable_engine(tmp_path: Path) -> None:
    """CA 4's actual measured claim: even when the background curl POST
    would itself take real wall time against a down engine (bounded by
    tuple_ledger_project.py's own curl timeout, seconds-scale), the
    WRAPPER's own exit is unaffected -- its fds are redirected away
    before backgrounding, so Claude Code (or a harness that does not
    honor `async: true`) never waits on them.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    base_url = f"http://127.0.0.1:{port}"

    # A fresh (but useless, since nothing listens) data-token lease so the
    # projector gets past resolution and actually attempts the curl call.
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    import hashlib
    from urllib.parse import urlsplit

    host = urlsplit(base_url).netloc
    digest = hashlib.sha256(f"{host}\x00default".encode("utf-8")).hexdigest()
    record = {
        "format_version": 1,
        "token": "whatever",
        "tenant": "default",
        "base_url_digest": digest,
        "expires_at": time.time() + 3600.0,
        "ttl_seconds": 3600.0,
        "minted_by_pid": os.getpid(),
    }
    (config_dir / f"data_token_lease.{digest}").write_text(json.dumps(record))

    proc, elapsed = _run_wrapper(START_ASYNC, tmp_path, base_url=base_url)
    assert proc.returncode == 0
    # The wrapper itself must be fast; the curl attempt (bounded at ~7s by
    # tuple_ledger_project.py's own timeout) runs detached in the background.
    assert elapsed < 2.0, (
        f"wrapper took {elapsed:.2f}s against an unreachable engine -- "
        "the backgrounding must decouple this from the curl timeout"
    )


def test_wrapper_fds_are_redirected_before_backgrounding() -> None:
    """Source-level pin on the inert-safe shape research 5 measured: the
    backgrounded subshell's stdin/stdout/stderr are all redirected to
    /dev/null BEFORE the `&`, and the wrapper never becomes a child that
    inherits the hook's own fds."""
    for script in (START_ASYNC, STOP_ASYNC):
        src = script.read_text()
        assert "</dev/null >/dev/null 2>&1 &" in src, (
            f"{script.name}: missing the fds-to-/dev/null-then-background shape"
        )
        assert "disown" in src


def test_start_wrapper_invokes_tuple_ledger_project_with_kind_start() -> None:
    src = START_ASYNC.read_text()
    assert "tuple_ledger_project.py" in src
    assert '"$HERE/tuple_ledger_project.py" start' in src


def test_stop_wrapper_invokes_tuple_ledger_project_with_kind_report() -> None:
    src = STOP_ASYNC.read_text()
    assert "tuple_ledger_project.py" in src
    assert '"$HERE/tuple_ledger_project.py" report' in src


def test_eventually_the_backgrounded_write_actually_lands(tmp_path: Path) -> None:
    """The wrapper returns fast, but the detached child must still do the
    real work -- confirmed by polling the log/engine after the wrapper
    itself has already exited."""
    import hashlib
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit

    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            try:
                received.append(json.loads(body.decode("utf-8")))
            except json.JSONDecodeError:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        base_url = f"http://{host}:{port}"

        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(f"{urlsplit(base_url).netloc}\x00default".encode("utf-8")).hexdigest()
        record = {
            "format_version": 1,
            "token": "async-e2e-token",
            "tenant": "default",
            "base_url_digest": digest,
            "expires_at": time.time() + 3600.0,
            "ttl_seconds": 3600.0,
            "minted_by_pid": os.getpid(),
        }
        (config_dir / f"data_token_lease.{digest}").write_text(json.dumps(record))

        proc, elapsed = _run_wrapper(START_ASYNC, tmp_path, base_url=base_url)
        assert proc.returncode == 0

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not received:
            time.sleep(0.05)

        assert received, "the backgrounded write never reached the engine within 5s"
        assert received[0]["subspace"] == f"ledger/{SESSION_ID}"
        assert received[0]["keys"] == {"agent_id": AGENT_ID, "kind": "start"}
    finally:
        server.shutdown()
        server.server_close()
