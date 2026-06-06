# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-151 Phase 0 escalation: run the REAL T2Daemon under an instrumented
selector and real client churn to pin the busy-loop fd (RF-1).

The simplified harnesses refuted the read-loop, to_thread churn, signal-wakeup
fd, and uncaught-ConnectionResetError mechanisms. This one runs the actual
``T2Daemon`` (real SQLite + WAL, heartbeat flock, _invoke_with_lock_retry, the
real framing protocol, lease/discovery I/O) on a SpySelector event loop, driving
real ``T2Client`` connect/RPC/disconnect churn, and dumps the selector spin
signature every 30s. The live lone-daemon peg appeared after ~4-12 min of
ordinary nx traffic (RF-5), so this soaks for ~13 min.

Run (backgroundable):  uv run python tests/daemon/rdr151_phase0_realdaemon.py
"""
from __future__ import annotations

import asyncio
import os
import selectors
import socket
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


def _churn(cfgdir: Path, stop: threading.Event, label: str) -> None:
    """Real T2Client connect/RPC/disconnect churn, mixed graceful + abrupt."""
    from nexus.daemon.t2_client import make_t2_client

    os.environ["NEXUS_CONFIG_DIR"] = str(cfgdir)
    i = 0
    while not stop.is_set():
        i += 1
        try:
            if i % 4 == 0:
                # abrupt: raw socket to the UDS, send a partial/garbage frame, RST
                disc = cfgdir / "sockets" / "t2.sock"
                if disc.exists():
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(str(disc))
                    s.sendall(b"\x00\x00\x00\x05hel")  # partial frame
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                 __import__("struct").pack("ii", 1, 0))
                    s.close()
            else:
                c = make_t2_client(config_dir=cfgdir)
                try:
                    c.call("database.hello")
                finally:
                    if i % 3 == 0:
                        # abandon without close -> GC closes (sometimes abruptly)
                        del c
                    else:
                        c.close()
        except Exception:
            pass
        time.sleep(0.01)


async def _dump_loop(sel: SpySelector, deadline: float) -> None:
    prev_calls = sel.calls
    prev_zero = sel.zero_to_ready
    start = None
    while True:
        await asyncio.sleep(DUMP_EVERY)
        c = sel.calls - prev_calls
        z = sel.zero_to_ready - prev_zero
        prev_calls = sel.calls
        prev_zero = sel.zero_to_ready
        rate = c / DUMP_EVERY
        zrate = z / DUMP_EVERY
        spin = "SPIN!" if rate > 5000 else "ok"
        top = sorted(sel.ready_fd_hits.items(), key=lambda kv: -kv[1])[:5]
        print(f"[t+{int(time.monotonic()):>6}] select/s={rate:>10.0f} "
              f"zero-ready/s={zrate:>9.0f} [{spin}] top_ready_fds={top}", flush=True)
        if time.monotonic() >= deadline:
            return


async def _amain() -> None:
    from nexus.daemon.t2_daemon import T2Daemon

    cfgdir = Path(mkdtemp(prefix="rdr151real-"))
    (cfgdir / "sockets").mkdir(exist_ok=True)
    db_path = cfgdir / "memory.db"
    print(f"config_dir={cfgdir}", flush=True)

    sel: SpySelector = asyncio.get_running_loop()._selector  # type: ignore[attr-defined]
    daemon = T2Daemon(config_dir=cfgdir, db_path=db_path)
    await daemon.start()
    print("real T2Daemon started on SpySelector loop", flush=True)

    deadline = time.monotonic() + SOAK_SECONDS
    stop = threading.Event()
    threads = [threading.Thread(target=_churn, args=(cfgdir, stop, f"c{n}"), daemon=True)
               for n in range(3)]
    for t in threads:
        t.start()
    print(f"3 churn threads started; soaking {SOAK_SECONDS}s, dump every {DUMP_EVERY}s", flush=True)

    await _dump_loop(sel, deadline)
    stop.set()
    print("soak complete", flush=True)
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
