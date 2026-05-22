#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scenario 5 — catalog rebuild under concurrent T2 traffic (nexus-57pwo).

Cross-domain isolation check: ``Catalog.rebuild()`` DELETEs four
tables (owners, documents, links, collections), disables FK
enforcement, and replays from JSONL. Under RDR-120 P5.A's
collapse, the catalog and T2 share the same daemon process; this
scenario asserts the catalog rebuild does NOT corrupt T2 memory
writes that are concurrent with it.

Sequence:
1. Set up an isolated catalog directory with a tiny manifest
   (some owners + documents + links written via JSONL).
2. Start a daemon. Drive 4 workers doing steady T2 memory writes
   to a project unrelated to the catalog.
3. On the host, invoke ``Catalog.rebuild()`` directly (the
   replay-equality machinery) while writes are in flight.
4. Stop the workers. Verify:
   - All T2 writes succeeded (no errors from catalog-rebuild
     contention).
   - The catalog projection matches the JSONL source after
     rebuild.
   - The T2 row count is exactly what the workers wrote.
"""
from __future__ import annotations

import json
import os
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


N_WORKERS = 4
PROJECT = "nexus_stress_catalog"


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
sock.settimeout(10.0)
ok, err = 0, 0
start = time.monotonic()
i = 0
while time.monotonic() - start < {duration}:
    i += 1
    try:
        send(sock, {{
            "op": "memory.put", "args": [],
            "kwargs": {{
                "project": {PROJECT!r},
                "title": f"w{{WID}}-{{i}}",
                "content": f"during-rebuild worker {{WID}} op {{i}}",
                "ttl": 1,
            }},
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


def seed_catalog(catalog_dir: Path) -> tuple[int, int, int]:
    """Materialise a small JSONL-backed catalog. Returns
    (n_owners, n_docs, n_links)."""
    catalog_dir.mkdir(parents=True, exist_ok=True)
    # Init the catalog: write a tiny JSONL set.
    owners = [
        {"owner": "nexus-stress-r", "name": "stress-r", "owner_type": "repo",
         "repo_root": "/tmp/x", "repo_hash": "deadbeef", "description": ""},
    ]
    docs = [
        {"tumbler": f"1.1.{i}", "title": f"doc-{i}.md", "author": "",
         "year": 0, "content_type": "code", "file_path": f"doc-{i}.md",
         "corpus": "", "physical_collection": "code__test",
         "chunk_count": 1, "head_hash": "", "indexed_at": "",
         "metadata": "{}", "source_mtime": 0.0, "alias_of": "",
         "source_uri": ""}
        for i in range(1, 11)
    ]
    links = [
        {"from_t": "1.1.1", "to_t": "1.1.2", "link_type": "cites",
         "from_span": "", "to_span": "",
         "created_by": "test", "created_at": "", "metadata": "{}"},
        {"from_t": "1.1.2", "to_t": "1.1.3", "link_type": "cites",
         "from_span": "", "to_span": "",
         "created_by": "test", "created_at": "", "metadata": "{}"},
    ]
    (catalog_dir / "owners.jsonl").write_text(
        "\n".join(json.dumps(o) for o in owners) + "\n",
    )
    (catalog_dir / "documents.jsonl").write_text(
        "\n".join(json.dumps(d) for d in docs) + "\n",
    )
    (catalog_dir / "links.jsonl").write_text(
        "\n".join(json.dumps(l) for l in links) + "\n",  # noqa: E741
    )
    return len(owners), len(docs), len(links)


def main() -> None:
    started = time.monotonic()
    with isolated_daemon() as (config_dir, tcp_port):
        addr = f"127.0.0.1:{tcp_port}"

        # Seed the catalog. Match the path nexus.config.catalog_path()
        # resolves under our isolated config dir.
        catalog_dir = config_dir / "catalog"
        n_owners, n_docs, n_links = seed_catalog(catalog_dir)

        # Spawn workers doing steady T2 writes (unrelated project).
        WORKER_DURATION = 6.0
        t0 = time.monotonic()
        procs = []
        for wid in range(N_WORKERS):
            script = worker_loop_script(
                addr=addr, worker_id=wid, duration=WORKER_DURATION,
            )
            p = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            procs.append(p)

        # While the workers are running, drive a Catalog.rebuild()
        # via a subprocess so we use the same isolated config dir.
        time.sleep(1.5)  # let workers ramp up
        rebuild_script = f'''
import os, sys
os.environ["NEXUS_CONFIG_DIR"] = {str(config_dir)!r}
os.environ["NX_T2_AUTO_MIGRATE"] = "1"
sys.path.insert(0, {str(Path(__file__).resolve().parents[3] / "src")!r})

from pathlib import Path
from nexus.catalog.catalog import Catalog

cat_dir = Path({str(catalog_dir)!r})
cat = Catalog(cat_dir, cat_dir / ".catalog.db")
# Trigger the rebuild path explicitly.
cat.rebuild()
# Verify projection.
db = cat._db
docs = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
owners = db.execute("SELECT COUNT(*) FROM owners").fetchone()[0]
links = db.execute("SELECT COUNT(*) FROM links").fetchone()[0]
import json
print(json.dumps({{"docs": docs, "owners": owners, "links": links}}))
'''
        rebuild_proc = subprocess.run(
            [sys.executable, "-c", rebuild_script],
            capture_output=True, text=True, timeout=60,
        )

        # Wait for the T2 workers.
        worker_results = []
        for p in procs:
            try:
                out, err = p.communicate(timeout=WORKER_DURATION + 30)
            except subprocess.TimeoutExpired:
                p.kill()
                out, err = p.communicate()
                worker_results.append({"timeout": True})
                continue
            try:
                worker_results.append(json.loads(out.strip().splitlines()[-1]))
            except (json.JSONDecodeError, IndexError):
                worker_results.append({"parse_error": True, "stdout": out})
        worker_elapsed = time.monotonic() - t0

        # Parse the catalog rebuild result.
        rebuild_ok = rebuild_proc.returncode == 0
        rebuild_counts = {}
        if rebuild_ok:
            try:
                rebuild_counts = json.loads(
                    rebuild_proc.stdout.strip().splitlines()[-1]
                )
            except (json.JSONDecodeError, IndexError):
                rebuild_ok = False

        # T2 worker aggregate.
        total_ok = sum(r.get("ok", 0) for r in worker_results)
        total_err = sum(r.get("err", 0) for r in worker_results)

        # Verify the T2 row count survives the catalog rebuild.
        sock = socket.create_connection(("127.0.0.1", tcp_port), timeout=5.0)
        try:
            db_rows = call(sock, "memory.list_entries", 1, project=PROJECT)
            stored_rows = len(db_rows) if isinstance(db_rows, list) else -1
        finally:
            sock.close()

        catalog_projection_matches = (
            rebuild_ok
            and rebuild_counts.get("owners") == n_owners
            and rebuild_counts.get("docs") == n_docs
            and rebuild_counts.get("links") == n_links
        )

        passed = (
            total_ok > 0
            and total_err == 0
            and stored_rows == total_ok
            and rebuild_ok
            and catalog_projection_matches
            and not any(r.get("timeout") or r.get("parse_error") for r in worker_results)
        )

        emit_scenario_result("scenario_5_catalog_rebuild", {
            "pass": passed,
            "n_workers": N_WORKERS,
            "worker_duration_seconds": WORKER_DURATION,
            "total_t2_writes_ok": total_ok,
            "total_t2_writes_err": total_err,
            "t2_stored_rows": stored_rows,
            "t2_durable_during_rebuild": stored_rows == total_ok,
            "expected_catalog_counts": {
                "owners": n_owners, "docs": n_docs, "links": n_links,
            },
            "rebuilt_catalog_counts": rebuild_counts,
            "catalog_rebuild_ok": rebuild_ok,
            "catalog_projection_matches_source": catalog_projection_matches,
            "rebuild_stderr_tail": (
                rebuild_proc.stderr[-500:] if rebuild_proc.stderr else None
            ),
            "worker_elapsed_seconds": round(worker_elapsed, 2),
        })


if __name__ == "__main__":
    main()
