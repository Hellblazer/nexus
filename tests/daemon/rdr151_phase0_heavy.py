# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-151 Phase 0 escalation B: REAL T2Daemon under HEAVY real-op churn.

Escalation A (rdr151_phase0_realdaemon.py) ran the real daemon under
hello()-only client churn and did NOT spin (steady ~2150 select/s). This
variant broadens the churn to real WRITE ops (memory.put -> real SQLite write
path -> WAL writer-lock contention with the heartbeat/reclaim) AND a high-rate
abrupt-RST-mid-write pattern: a raw socket sends a valid memory.put frame, then
RSTs (SO_LINGER 0) WITHOUT reading the response, so the daemon is mid-write
(inside asyncio.to_thread) when the peer vanishes. The live leak showed accepted
sockets with no peer process (clients dying mid-op), so this targets that path
against the real write machinery.

Run (backgroundable):  uv run python tests/daemon/rdr151_phase0_heavy.py
"""
from __future__ import annotations

import asyncio
import json
import os
import selectors
import socket
import struct
import threading
import time
from pathlib import Path
from tempfile import mkdtemp

SOAK_SECONDS = int(os.environ.get("RDR151_SOAK_SECONDS", "780"))
DUMP_EVERY = int(os.environ.get("RDR151_DUMP_EVERY", "30"))


class SpySelector(selectors.DefaultSelector):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.zero_to_ready = 0
        self.ready_fd_hits: dict[int, int] = {}

    def select(self, timeout=None):  # type: ignore[override]
        self.calls += 1
        res = super().select(timeout)
        if res and timeout is not None and timeout <= 0.0005:
            self.zero_to_ready += 1
            for key, _ in res:
                self.ready_fd_hits[key.fd] = self.ready_fd_hits.get(key.fd, 0) + 1
        return res


def _frame(op: str, args: list) -> bytes:
    payload = json.dumps({"op": op, "args": args, "kwargs": {}, "request_id": 1}).encode()
    return struct.pack(">I", len(payload)) + payload + b"\n"


def _real_write_churn(cfgdir: Path, stop: threading.Event, n: int) -> None:
    """Graceful real T2Client traffic: memory.put (write) + get + search."""
    from nexus.daemon.t2_client import make_t2_client

    os.environ["NEXUS_CONFIG_DIR"] = str(cfgdir)
    i = 0
    while not stop.is_set():
        i += 1
        try:
            c = make_t2_client(config_dir=cfgdir)
            try:
                c.call("memory.put", f"proj{n}", f"title-{i}", "x" * 200, "tag", 30)
                c.call("memory.get", f"proj{n}", f"title-{i}")
                if i % 5 == 0:
                    c.call("memory.search", "title", f"proj{n}", 5)
            finally:
                c.close()
        except Exception:
            pass
        time.sleep(0.005)


def _abrupt_midwrite_churn(uds: Path, stop: threading.Event) -> None:
    """Raw socket: send a valid memory.put frame, then RST without reading the
    response -> the daemon is mid-write (in to_thread) when the peer dies."""
    linger = struct.pack("ii", 1, 0)
    i = 0
    while not stop.is_set():
        i += 1
        try:
            if uds.exists():
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(uds))
                s.sendall(_frame("memory.put", [f"rst", f"r-{i}", "y" * 200, "", 30]))
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
                s.close()  # RST, mid-write, response never read
        except OSError:
            pass
        time.sleep(0.004)


async def _dump_loop(sel: SpySelector, deadline: float) -> None:
    prev_c, prev_z = sel.calls, sel.zero_to_ready
    while True:
        await asyncio.sleep(DUMP_EVERY)
        c = sel.calls - prev_c
        z = sel.zero_to_ready - prev_z
        prev_c, prev_z = sel.calls, sel.zero_to_ready
        rate = c / DUMP_EVERY
        spin = "SPIN!" if rate > 5000 else "ok"
        top = sorted(sel.ready_fd_hits.items(), key=lambda kv: -kv[1])[:5]
        print(f"[t+{int(time.monotonic()):>6}] select/s={rate:>10.0f} "
              f"zero-ready/s={z / DUMP_EVERY:>9.0f} [{spin}] top_ready_fds={top}", flush=True)
        if time.monotonic() >= deadline:
            return


async def _amain() -> None:
    from nexus.daemon.t2_daemon import T2Daemon

    cfgdir = Path(mkdtemp(prefix="rdr151heavy-"))
    (cfgdir / "sockets").mkdir(exist_ok=True)
    db_path = cfgdir / "memory.db"
    uds = cfgdir / "sockets" / "t2.sock"
    print(f"config_dir={cfgdir}", flush=True)

    sel: SpySelector = asyncio.get_running_loop()._selector  # type: ignore[attr-defined]
    daemon = T2Daemon(config_dir=cfgdir, db_path=db_path)
    await daemon.start()
    print("real T2Daemon started; HEAVY churn (real memory.put writes + abrupt-RST-mid-write)", flush=True)

    deadline = time.monotonic() + SOAK_SECONDS
    stop = threading.Event()
    threads = [
        threading.Thread(target=_real_write_churn, args=(cfgdir, stop, 0), daemon=True),
        threading.Thread(target=_real_write_churn, args=(cfgdir, stop, 1), daemon=True),
        threading.Thread(target=_abrupt_midwrite_churn, args=(uds, stop), daemon=True),
        threading.Thread(target=_abrupt_midwrite_churn, args=(uds, stop), daemon=True),
    ]
    for t in threads:
        t.start()
    print(f"4 churn threads (2 real-write, 2 abrupt-mid-write); soaking {SOAK_SECONDS}s", flush=True)

    await _dump_loop(sel, deadline)
    stop.set()
    print(f"FINAL top ready-fd hits: "
          f"{sorted(sel.ready_fd_hits.items(), key=lambda kv: -kv[1])[:8]}", flush=True)
    try:
        await daemon.stop()
    except Exception as exc:  # noqa: BLE001
        print(f"stop raised: {exc!r}", flush=True)


def main() -> None:
    sel = SpySelector()
    loop = asyncio.SelectorEventLoop(sel)
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_amain())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
