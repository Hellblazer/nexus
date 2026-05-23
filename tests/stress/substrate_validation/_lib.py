# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared infrastructure for the RDR-120 substrate-validation
stress matrix (nexus-57pwo).

Each scenario script spawns N worker subprocesses (separate Python
interpreters — true cross-process isolation, what the daemon's
substrate contract is for) and exercises the daemon under load.
Receipts are written as JSON files under
``tests/stress/substrate_validation/receipts/`` for the coordinator
to aggregate into a final T2 entry.

NOT Docker-based. Docker adds container-namespace isolation but
no new failure modes vs separate Python interpreters; the daemon's
arbitration contract is process-agnostic. The container TCP path
is separately validated by nexus-wkyt7 / nexus-3d1ph / nexus-cm0az
receipts; here we exercise the substrate's concurrency contract
at high op-count under controlled conditions.

Run a single scenario directly:
    uv run python tests/stress/substrate_validation/scenario_1_fan_in.py

Run the full matrix:
    bash tests/stress/substrate_validation/run_all.sh
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


HERE = Path(__file__).parent
RECEIPTS_DIR = HERE / "receipts"
REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Daemon lifecycle (isolated per scenario)
# ---------------------------------------------------------------------------


@contextmanager
def isolated_daemon() -> Iterator[tuple[Path, int]]:
    """Spawn a T2 daemon in an isolated config dir; yield
    ``(config_dir, tcp_port)``; tear down on exit.

    Uses a fresh tempdir per scenario so scenarios cannot poison
    each other's state. The daemon binds a dynamic TCP port.
    """
    tmp_root = Path(tempfile.mkdtemp(prefix="substrate-validation-"))
    config_dir = tmp_root / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "sockets").mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["NEXUS_CONFIG_DIR"] = str(config_dir)
    env["NX_T2_AUTO_MIGRATE"] = "1"  # tests inherit the conftest opt-in
    # Strip any prior daemon-discovery env so the daemon writes
    # its OWN addr file under our isolated config dir.
    for k in ("NX_T2_ADDR", "NX_T2_SOCK", "NX_T3_ADDR"):
        env.pop(k, None)

    log_file = open(tmp_root / "daemon.log", "w")
    proc = subprocess.Popen(
        ["uv", "run", "nx", "daemon", "t2", "start"],
        env=env,
        stdout=log_file,
        stderr=log_file,
        cwd=REPO_ROOT,
    )

    # Wait for the discovery file to appear (daemon ready).
    uid = os.getuid()
    addr_path = config_dir / f"t2_addr.{uid}"
    deadline = time.time() + 30.0
    while time.time() < deadline:
        if addr_path.exists():
            try:
                payload = json.loads(addr_path.read_text())
                tcp_port = int(payload.get("tcp_port") or 0)
                if tcp_port > 0:
                    break
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.1)
    else:
        proc.terminate()
        proc.wait(timeout=5)
        log_file.close()
        raise RuntimeError(
            f"daemon failed to start within 30s; log at {tmp_root / 'daemon.log'}",
        )

    try:
        yield config_dir, tcp_port
    finally:
        # Best-effort SIGTERM + cleanup.
        subprocess.run(
            ["uv", "run", "nx", "daemon", "t2", "stop"],
            env=env,
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=10,
        )
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log_file.close()
        shutil.rmtree(tmp_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# T2Client framed-JSON protocol (stdlib-only; matches mvv_client.py)
# ---------------------------------------------------------------------------


def send_frame(sock: socket.socket, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    header = struct.pack(">I", len(body))
    sock.sendall(header + body + b"\n")


def recv_exactly(sock: socket.socket, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise RuntimeError("peer closed mid-frame")
        out.extend(chunk)
    return bytes(out)


def recv_frame(sock: socket.socket) -> dict[str, Any]:
    header = recv_exactly(sock, 4)
    length = struct.unpack(">I", header)[0]
    body = recv_exactly(sock, length + 1)
    return json.loads(body[:-1].decode("utf-8"))


def call(sock: socket.socket, op: str, request_id: int, **kwargs: Any) -> Any:
    send_frame(sock, {
        "op": op, "args": [], "kwargs": kwargs, "request_id": request_id,
    })
    response = recv_frame(sock)
    if response.get("request_id") != request_id:
        raise RuntimeError(
            f"request_id mismatch: sent {request_id}, "
            f"got {response.get('request_id')!r}",
        )
    if not response.get("ok", False):
        err = response.get("error") or {}
        raise RuntimeError(
            f"daemon error on {op}: {err.get('type')}: {err.get('message')}",
        )
    return response.get("result")


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


def write_receipt(scenario: str, verdict: dict[str, Any]) -> Path:
    """Write a JSON receipt for the scenario. Coordinator reads
    these and aggregates into a single T2 entry.
    """
    RECEIPTS_DIR.mkdir(exist_ok=True)
    path = RECEIPTS_DIR / f"{scenario}.json"
    verdict_with_meta = {
        "scenario": scenario,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "host_platform": sys.platform,
        **verdict,
    }
    path.write_text(json.dumps(verdict_with_meta, indent=2) + "\n")
    return path


# ---------------------------------------------------------------------------
# Worker subprocess helpers
# ---------------------------------------------------------------------------


def worker_script_writer(
    *,
    addr: str,
    count: int,
    project: str,
    worker_id: int,
) -> str:
    """Return a Python program (as a string) that writes ``count``
    memory rows to the daemon at ``addr`` using framed JSON, then
    prints a JSON summary of (ops_ok, ops_err, ids).
    """
    return f'''
import json, socket, struct, sys, time

def send(sock, payload):
    body = json.dumps(payload).encode("utf-8")
    header = struct.pack(">I", len(body))
    sock.sendall(header + body + b"\\n")

def recv_exactly(sock, n):
    out = bytearray()
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise RuntimeError("peer closed")
        out.extend(chunk)
    return bytes(out)

def recv_frame(sock):
    header = recv_exactly(sock, 4)
    length = struct.unpack(">I", header)[0]
    return json.loads(recv_exactly(sock, length + 1)[:-1].decode("utf-8"))

host, port = {addr!r}.rsplit(":", 1)
sock = socket.create_connection((host, int(port)), timeout=30.0)
sock.settimeout(60.0)

ok, err, ids = 0, 0, []
for i in range({count}):
    rid = i + 1
    try:
        send(sock, {{
            "op": "memory.put",
            "args": [],
            "kwargs": {{
                "project": {project!r},
                "title": f"worker-{worker_id}-op-{{i}}",
                "content": f"worker {worker_id} op {{i}} ts {{time.time()}}",
                "ttl": 1,
            }},
            "request_id": rid,
        }})
        resp = recv_frame(sock)
        if resp.get("ok"):
            ok += 1
            ids.append(resp.get("result"))
        else:
            err += 1
    except Exception as e:
        err += 1
        sys.stderr.write(f"worker {worker_id} op {{i}}: {{type(e).__name__}}: {{e}}\\n")

sock.close()
print(json.dumps({{"worker_id": {worker_id}, "ok": ok, "err": err, "ids": ids}}))
'''


def spawn_workers(
    *,
    addr: str,
    n_workers: int,
    count_per_worker: int,
    project: str,
    timeout: float = 600.0,
) -> list[dict[str, Any]]:
    """Spawn N worker subprocesses concurrently; collect their JSON
    summaries. Each worker runs in its own Python interpreter —
    true cross-process isolation."""
    procs = []
    for wid in range(n_workers):
        script = worker_script_writer(
            addr=addr, count=count_per_worker, project=project, worker_id=wid,
        )
        p = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        procs.append(p)

    results = []
    for p in procs:
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            results.append({"timeout": True, "stderr": err})
            continue
        try:
            results.append(json.loads(out.strip().splitlines()[-1]))
        except (json.JSONDecodeError, IndexError):
            results.append({"parse_error": True, "stdout": out, "stderr": err})
    return results


# ---------------------------------------------------------------------------
# Scenario runner protocol
# ---------------------------------------------------------------------------


def emit_scenario_result(scenario: str, result: dict[str, Any]) -> None:
    """Print the receipt path + a one-line verdict to stdout so the
    coordinator can scrape pass/fail."""
    receipt_path = write_receipt(scenario, result)
    verdict = "PASS" if result.get("pass") else "FAIL"
    print(f"\n[{scenario}] {verdict}  receipt: {receipt_path}")
    if not result.get("pass"):
        print(f"[{scenario}] details: {json.dumps(result, indent=2)}")
        sys.exit(1)
