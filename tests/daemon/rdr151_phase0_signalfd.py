# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-151 Phase 0 escalation: test the main-thread signal-wakeup-fd hypothesis.

The thread-based reproducer (rdr151_phase0_repro.py) refuted the read-loop,
to_thread churn, and uncaught-ConnectionResetError mechanisms. The one thing it
cannot exercise is main-thread-only machinery: ``loop.add_signal_handler`` (the
real daemon's ``run_until_signal`` installs SIGTERM/SIGINT handlers, which on
Unix register a ``set_wakeup_fd`` socketpair in the selector — the fd 5/6 pair
seen in the live ``lsof``). This harness runs the server ON the main thread with
those handlers installed, drives client churn from a worker thread, and watches
the instrumented selector for the spin.

Run:  uv run python tests/daemon/rdr151_phase0_signalfd.py
"""
from __future__ import annotations

import asyncio
import selectors
import signal
import socket
import struct
import threading
import time
from pathlib import Path
from tempfile import mkdtemp


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


FRAME = struct.Struct(">I")


def _blocking_work(body: bytes) -> bytes:
    time.sleep(0.02)
    return body


async def _handle(reader, writer) -> None:
    try:
        while True:
            try:
                hdr = await reader.readexactly(4)
            except asyncio.IncompleteReadError:
                break
            (length,) = FRAME.unpack(hdr)
            body = await reader.readexactly(length)
            result = await asyncio.to_thread(_blocking_work, body)
            writer.write(FRAME.pack(len(result)) + result)
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


def _client_churn(sock_path: str, n: int) -> None:
    for i in range(n):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            if i % 3 == 0:
                s.sendall(FRAME.pack(5) + b"hello")
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                s.close()
            elif i % 3 == 1:
                s.sendall(FRAME.pack(5) + b"hello")
                s.settimeout(1.0)
                try: s.recv(4096)
                except OSError: pass
                s.close()
            else:
                s.sendall(FRAME.pack(5) + b"hello")
                s.close()
        except OSError:
            pass
        time.sleep(0.003)


async def main() -> None:
    loop = asyncio.get_running_loop()
    sel = loop._selector  # the SpySelector we installed via the policy
    sock_path = str(Path(mkdtemp(prefix="rdr151sig-")) / "t.sock")

    # Mirror the daemon: install signal handlers on the running loop (this is the
    # main-thread-only machinery the thread harness could not exercise).
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1):
        try:
            loop.add_signal_handler(sig, lambda: None)
        except (NotImplementedError, RuntimeError):
            pass

    server = await asyncio.start_unix_server(_handle, path=sock_path)
    print(f"server up (MAIN thread, signal handlers installed): {sock_path}")

    def snap():
        return sel.calls, sel.zero_to_ready

    async def rate(seconds: float):
        c0, z0 = snap()
        await asyncio.sleep(seconds)
        c1, z1 = snap()
        return (c1 - c0) / seconds, (z1 - z0) / seconds

    c, z = await rate(1.0)
    print(f"BASELINE:               select/s={c:>10.0f}  zero-to-ready/s={z:>10.0f}")

    # churn from a worker thread (clients are blocking sockets)
    print("driving 800 connect/RPC/disconnect cycles from a worker thread...")
    t = threading.Thread(target=_client_churn, args=(sock_path, 800), daemon=True)
    t.start()
    while t.is_alive():
        await asyncio.sleep(0.5)
    await asyncio.sleep(0.5)

    c, z = await rate(2.0)
    spin = "SPIN" if c > 5000 else "ok"
    print(f"POST-CHURN idle:        select/s={c:>10.0f}  zero-to-ready/s={z:>10.0f}  [{spin}]")
    print(f"top ready-fd hits: {sorted(sel.ready_fd_hits.items(), key=lambda kv: -kv[1])[:6]}")
    server.close()


if __name__ == "__main__":
    sel = SpySelector()
    loop = asyncio.SelectorEventLoop(sel)
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
