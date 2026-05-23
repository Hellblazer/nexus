#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scenario 4 — schema-version handshake mismatch under load (nexus-57pwo).

Models the version-skew failure mode: an operator runs an old client
against a new daemon (or vice versa) and the substrate must refuse
to operate rather than silently writing against an unexpected
schema.

Sequence:
1. Start daemon at the current schema version (auto-bootstrapped).
2. Spawn 3 worker subprocesses running steady put load for 5 seconds.
3. Stop the daemon. Edit ``_nexus_version`` directly in the SQLite
   to a "future" version. Restart the daemon.
4. New clients connect; the framed-JSON ``database.hello`` op
   returns the future version. A client coded against the actual
   build version should raise ``T2SchemaVersionMismatchError`` and
   refuse to proceed.

This scenario uses the framed-JSON protocol directly + the
``T2SchemaVersionMismatchError`` check we ship in ``T2Client``.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
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


N_WORKERS = 3
PROJECT = "nexus_stress_schema"
FAKE_FUTURE_VERSION = "999.999.999"


def worker_loop_script(*, addr: str, worker_id: int, duration: float) -> str:
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
ok, err = 0, 0
start = time.monotonic()
i = 0
while time.monotonic() - start < {duration}:
    i += 1
    try:
        send(sock, {{
            "op": "memory.put", "args": [],
            "kwargs": {{"project": {PROJECT!r},
                       "title": f"w{{WID}}-{{i}}",
                       "content": f"pre-mismatch worker {{WID}} op {{i}}",
                       "ttl": 1}},
            "request_id": i,
        }})
        resp = recv_frame(sock)
        if resp.get("ok"):
            ok += 1
        else:
            err += 1
    except Exception:
        err += 1
        break

sock.close()
print(json.dumps({{"worker_id": WID, "ok": ok, "err": err}}))
'''


def main() -> None:
    started = time.monotonic()
    with isolated_daemon() as (config_dir, tcp_port):
        addr = f"127.0.0.1:{tcp_port}"

        # Phase 1: steady load for 5 seconds against the current
        # daemon. Confirms normal operation.
        t0 = time.monotonic()
        procs = []
        for wid in range(N_WORKERS):
            script = worker_loop_script(
                addr=addr, worker_id=wid, duration=5.0,
            )
            p = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            procs.append(p)

        pre_results = []
        for p in procs:
            out, err = p.communicate(timeout=30)
            try:
                pre_results.append(json.loads(out.strip().splitlines()[-1]))
            except (json.JSONDecodeError, IndexError):
                pre_results.append({"parse_error": True})
        pre_ok = sum(r.get("ok", 0) for r in pre_results)
        pre_err = sum(r.get("err", 0) for r in pre_results)
        pre_phase_elapsed = time.monotonic() - t0

        # Phase 2: stop the daemon, edit _nexus_version to a future
        # value, restart, then probe with a fresh client.
        env = os.environ.copy()
        env["NEXUS_CONFIG_DIR"] = str(config_dir)
        env["NX_T2_AUTO_MIGRATE"] = "1"
        repo_root = Path(__file__).resolve().parents[3]
        subprocess.run(
            ["uv", "run", "nx", "daemon", "t2", "stop"],
            env=env, capture_output=True, timeout=10, cwd=repo_root,
        )
        # Wait for the discovery file to vanish.
        uid = os.getuid()
        addr_path = config_dir / f"t2_addr.{uid}"
        deadline = time.time() + 10.0
        while addr_path.exists() and time.time() < deadline:
            time.sleep(0.1)

        # Edit _nexus_version directly.
        db_path = config_dir / "memory.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE _nexus_version SET value = ? WHERE key = 'cli_version'",
                (FAKE_FUTURE_VERSION,),
            )
            conn.commit()
        finally:
            conn.close()

        # Restart the daemon with the modified version row.
        restart_proc = subprocess.Popen(
            ["uv", "run", "nx", "daemon", "t2", "start"],
            env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=repo_root,
        )
        deadline = time.monotonic() + 30.0
        new_port = None
        while time.monotonic() < deadline:
            if addr_path.exists():
                try:
                    p = json.loads(addr_path.read_text())
                    new_port = int(p.get("tcp_port") or 0)
                    if new_port > 0:
                        break
                except (json.JSONDecodeError, OSError):
                    pass
            time.sleep(0.1)

        # Phase 3: hello against the modified daemon. Verify the
        # daemon's reported version is the future value AND a real
        # T2Client raises T2SchemaVersionMismatchError.
        daemon_reports_future = False
        client_raises_mismatch = False
        client_actual_version: str | None = None

        if new_port:
            # Raw framed-JSON probe — confirm the daemon really
            # reports the future version.
            try:
                sock = socket.create_connection(
                    ("127.0.0.1", new_port), timeout=5.0,
                )
                try:
                    hello = call(
                        sock, "database.hello", 1,
                        client_schema_version="raw-probe",
                    )
                    daemon_reports_future = (
                        hello.get("daemon_schema_version") == FAKE_FUTURE_VERSION
                    )
                finally:
                    sock.close()
            except Exception:
                pass

            # Real T2Client should raise T2SchemaVersionMismatchError
            # on first call (the lazy handshake fires there).
            try:
                from nexus.daemon.t2_client import (
                    T2Client,
                    T2SchemaVersionMismatchError,
                )
                from nexus.db.migrations import expected_t2_schema_version
                client_actual_version = expected_t2_schema_version()

                # Construct the T2Client and force a call; this
                # triggers the lazy handshake.
                import os as _os
                _os.environ["NX_T2_ADDR"] = f"127.0.0.1:{new_port}"
                client = T2Client(config_dir=config_dir)
                try:
                    client.memory.put(
                        content="should-not-land",
                        project=PROJECT,
                        title="post-mismatch-canary",
                        ttl=1,
                    )
                    # Should not reach here.
                except T2SchemaVersionMismatchError:
                    client_raises_mismatch = True
                except Exception:
                    pass
                finally:
                    client.close()
                _os.environ.pop("NX_T2_ADDR", None)
            except Exception:
                pass

        subprocess.run(
            ["uv", "run", "nx", "daemon", "t2", "stop"],
            env=env, capture_output=True, timeout=10, cwd=repo_root,
        )

        passed = (
            pre_ok > 0
            and pre_err == 0
            and daemon_reports_future
            and client_raises_mismatch
            and client_actual_version != FAKE_FUTURE_VERSION
        )

        emit_scenario_result("scenario_4_schema_mismatch", {
            "pass": passed,
            "n_workers": N_WORKERS,
            "pre_phase_elapsed_seconds": round(pre_phase_elapsed, 2),
            "pre_ok": pre_ok,
            "pre_err": pre_err,
            "fake_future_version": FAKE_FUTURE_VERSION,
            "daemon_reports_future_version": daemon_reports_future,
            "client_actual_version": client_actual_version,
            "client_raises_T2SchemaVersionMismatchError": client_raises_mismatch,
        })


if __name__ == "__main__":
    main()
