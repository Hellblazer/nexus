#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scenario 3 — daemon-failure-recovery under load (nexus-57pwo).

Spawns 5 worker subprocesses doing steady ``memory.put`` load.
At t=10s the test SIGKILLs the daemon (`kill -9`). Workers in the
middle of an operation see their socket close mid-frame and the
client raises. At t=15s the test starts a NEW daemon at the same
config dir. Surviving workers don't auto-reconnect (each worker
uses one connection); their remaining ops fail. A fresh post-restart
client confirms the daemon is healthy and the SQLite is intact.

Asserts:
- During-kill ops fail loud (the worker's socket sees peer-closed,
  Python raises, the worker records err).
- Post-restart: a fresh client can hello + put + get cleanly.
- Pre-kill writes are durable across the restart (they're committed
  to the SQLite file, not lost on daemon crash).
- PRAGMA integrity_check on the post-restart database is "ok"
  (verified via memory.list_entries succeeding, which would error
  on a torn database).
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (  # noqa: E402
    call,
    emit_scenario_result,
    isolated_daemon,
)


N_WORKERS = 5
PROJECT = "nexus_stress_kill9"


def worker_script(*, addr: str, worker_id: int, duration: float) -> str:
    return f'''
import json, socket, struct, sys, time

def send(sock, payload):
    body = json.dumps(payload).encode("utf-8")
    sock.sendall(struct.pack(">I", len(body)) + body + b"\\n")

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

WID = {worker_id}
host, port = {addr!r}.rsplit(":", 1)
sock = socket.create_connection((host, int(port)), timeout=5.0)
sock.settimeout(5.0)

ok, err, first_err_ts = 0, 0, None
start = time.monotonic()
i = 0
while time.monotonic() - start < {duration}:
    i += 1
    try:
        send(sock, {{
            "op": "memory.put", "args": [],
            "kwargs": {{
                "project": {PROJECT!r},
                "title": f"w{{WID}}-op-{{i}}",
                "content": f"worker {{WID}} op {{i}} ts {{time.time()}}",
                "ttl": 1,
            }},
            "request_id": i,
        }})
        resp = recv_frame(sock)
        if resp.get("ok"):
            ok += 1
        else:
            err += 1
            if first_err_ts is None:
                first_err_ts = time.monotonic() - start
    except Exception as exc:
        err += 1
        if first_err_ts is None:
            first_err_ts = time.monotonic() - start
        break  # connection broken; stop trying

sock.close()
print(json.dumps({{
    "worker_id": WID, "ok": ok, "err": err,
    "first_err_seconds": first_err_ts, "total_attempts": i,
}}))
'''


def main() -> None:
    started = time.monotonic()
    with isolated_daemon() as (config_dir, tcp_port):
        addr = f"127.0.0.1:{tcp_port}"

        # Read the daemon PID from the discovery file.
        uid = os.getuid()
        addr_path = config_dir / f"t2_addr.{uid}"
        payload = json.loads(addr_path.read_text())
        daemon_pid_pre = int(payload.get("pid") or 0)

        # Spawn workers; they run for 30 seconds.
        WORKER_DURATION = 30.0
        KILL_AT = 8.0
        t0 = time.monotonic()
        procs = []
        for wid in range(N_WORKERS):
            script = worker_script(
                addr=addr, worker_id=wid, duration=WORKER_DURATION,
            )
            p = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            procs.append(p)

        # Wait, then SIGKILL the daemon.
        time.sleep(KILL_AT)
        os.kill(daemon_pid_pre, signal.SIGKILL)
        kill_wall = time.monotonic() - t0

        # Wait for workers to finish (they should exit on socket
        # error within seconds of the kill).
        results = []
        for p in procs:
            try:
                out, err = p.communicate(timeout=WORKER_DURATION + 10)
            except subprocess.TimeoutExpired:
                p.kill()
                out, err = p.communicate()
                results.append({"timeout": True})
                continue
            try:
                results.append(json.loads(out.strip().splitlines()[-1]))
            except (json.JSONDecodeError, IndexError):
                results.append({"parse_error": True, "stdout": out, "stderr": err})

        total_ok = sum(r.get("ok", 0) for r in results)
        total_err = sum(r.get("err", 0) for r in results)

        # Daemon should be dead now (the kill -9 took it down and
        # nothing respawned automatically).
        try:
            os.kill(daemon_pid_pre, 0)  # signal 0 = check existence
            daemon_dead_after_kill = False
        except OSError:
            daemon_dead_after_kill = True

        # Restart the daemon by hand. Same config dir, same db file.
        env = os.environ.copy()
        env["NEXUS_CONFIG_DIR"] = str(config_dir)
        env["NX_T2_AUTO_MIGRATE"] = "1"
        # Remove the stale discovery file so the new daemon's start
        # is unambiguous.
        try:
            addr_path.unlink()
        except FileNotFoundError:
            pass
        repo_root = Path(__file__).resolve().parents[3]
        restart_proc = subprocess.Popen(
            ["uv", "run", "nx", "daemon", "t2", "start"],
            env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=repo_root,
        )

        # Wait for the new daemon's discovery file.
        deadline = time.monotonic() + 30.0
        new_port = None
        while time.monotonic() < deadline:
            if addr_path.exists():
                try:
                    new_payload = json.loads(addr_path.read_text())
                    new_port = int(new_payload.get("tcp_port") or 0)
                    if new_port > 0:
                        break
                except (json.JSONDecodeError, OSError):
                    pass
            time.sleep(0.1)

        post_restart_ok = False
        rows_after_restart = -1
        if new_port:
            # Verify hello + post-restart write + read.
            try:
                sock = socket.create_connection(
                    ("127.0.0.1", new_port), timeout=5.0,
                )
                try:
                    hello = call(
                        sock, "database.hello", 1,
                        client_schema_version="probe",
                    )
                    # Confirm pre-kill writes are durable: query
                    # the project's rows.
                    rows = call(
                        sock, "memory.list_entries", 2, project=PROJECT,
                    )
                    rows_after_restart = (
                        len(rows) if isinstance(rows, list) else -1
                    )
                    # Post-restart write.
                    new_id = call(
                        sock, "memory.put", 3,
                        project=PROJECT,
                        title="post-restart-canary",
                        content="written after kill-9 + restart",
                        ttl=1,
                    )
                    got = call(
                        sock, "memory.get", 4,
                        project=PROJECT, title="post-restart-canary",
                    )
                    post_restart_ok = (
                        hello is not None
                        and isinstance(new_id, int) and new_id > 0
                        and got is not None
                        and "written after kill-9" in (got.get("content") or "")
                    )
                finally:
                    sock.close()
            except Exception:
                post_restart_ok = False

        # Stop the manually-spawned restart daemon. The
        # ``isolated_daemon`` context manager's stop also fires on
        # exit; that's harmless because the daemon is keyed by
        # config dir.
        subprocess.run(
            ["uv", "run", "nx", "daemon", "t2", "stop"],
            env=env, capture_output=True, timeout=10, cwd=repo_root,
        )

        passed = (
            total_ok > 0  # some writes succeeded pre-kill
            and total_err > 0  # some failed loud post-kill
            and daemon_dead_after_kill
            and post_restart_ok
            and rows_after_restart >= total_ok  # pre-kill rows durable
        )

        emit_scenario_result("scenario_3_kill9_recovery", {
            "pass": passed,
            "n_workers": N_WORKERS,
            "kill_at_seconds": KILL_AT,
            "kill_wall_seconds": round(kill_wall, 2),
            "total_ok_pre_kill": total_ok,
            "total_err_post_kill": total_err,
            "daemon_dead_after_kill": daemon_dead_after_kill,
            "post_restart_handshake_and_write_ok": post_restart_ok,
            "rows_visible_after_restart": rows_after_restart,
            "pre_kill_writes_durable": rows_after_restart >= total_ok,
        })


if __name__ == "__main__":
    main()
