# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-151 Phase 0 reproducer: pin the asyncio selector busy-loop (RF-1).

Stands up a minimal asyncio UNIX-domain server that mirrors the shape of
``T2Daemon._handle_connection`` (readexactly-framed loop, break on
``IncompleteReadError``, ``finally: writer.close()``, and crucially NO
``ConnectionResetError`` catch). It then drives clients through four exit modes
and watches an instrumented event-loop selector for the ``select(timeout~0)``
spin signature, naming the fd that is reported ready every iteration.

Run directly:  uv run python tests/daemon/rdr151_phase0_repro.py

This is a diagnostic harness first; once it reproduces the spin it is the basis
for the Gap-1 regression test. It deliberately does NOT import the real daemon
(RF-1 isolation: minimal server first; escalate to to_thread dispatch only if
the minimal server does not reproduce).
"""
from __future__ import annotations

import asyncio
import selectors
import socket
import struct
import threading
import time
from pathlib import Path
from tempfile import mkdtemp


# ---- instrumented selector ------------------------------------------------

class SpySelector(selectors.DefaultSelector):  # KqueueSelector on macOS
    """Wraps select() to count calls and detect the busy-loop signature."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.zero_to_ready = 0          # select(timeout<=~0) that returned >=1 ready
        self.last_ready_fds: list[int] = []
        self.ready_fd_hits: dict[int, int] = {}

    def select(self, timeout=None):  # type: ignore[override]
        self.calls += 1
        res = super().select(timeout)
        if res and timeout is not None and timeout <= 0.0005:
            self.zero_to_ready += 1
            self.last_ready_fds = [key.fd for key, _ in res]
            for key, _ in res:
                self.ready_fd_hits[key.fd] = self.ready_fd_hits.get(key.fd, 0) + 1
        return res


FRAME = struct.Struct(">I")


def _blocking_work(body: bytes) -> bytes:
    """Mirror a serve-path RPC: real work runs in the thread pool."""
    time.sleep(0.02)
    return body


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Mirror of T2Daemon._handle_connection: dispatch via to_thread (as the real
    daemon does for every RPC through _invoke_with_lock_retry), no
    ConnectionResetError catch."""
    try:
        while True:
            try:
                hdr = await reader.readexactly(4)
            except asyncio.IncompleteReadError:
                break  # client closed (graceful FIN)
            (length,) = FRAME.unpack(hdr)
            body = await reader.readexactly(length)
            # Every RPC dispatched off the loop thread -> self-pipe wakeup on
            # completion (RF-1 candidate a). The client may RST while we are here.
            result = await asyncio.to_thread(_blocking_work, body)
            writer.write(FRAME.pack(len(result)) + result)
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def _background_to_thread_traffic(stop: asyncio.Event) -> None:
    """Mirror the daemon's heavy off-loop dispatch (reclaim/reassert/heartbeat
    all hit to_thread or the loop thread on a cadence)."""
    while not stop.is_set():
        await asyncio.to_thread(time.sleep, 0.0)
        await asyncio.sleep(0.01)


class Server:
    def __init__(self) -> None:
        self.sock_path = str(Path(mkdtemp(prefix="rdr151-")) / "t.sock")
        self.sel = SpySelector()
        self.loop = asyncio.SelectorEventLoop(self.sel)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)

        self._stop = asyncio.Event()

        async def _boot() -> None:
            self._server = await asyncio.start_unix_server(_handle, path=self.sock_path)
            self.loop.create_task(_background_to_thread_traffic(self._stop))
            self._ready.set()

        self.loop.run_until_complete(_boot())
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(5)

    def snapshot(self) -> tuple[int, int]:
        return self.sel.calls, self.sel.zero_to_ready

    def rate(self, seconds: float = 1.0) -> tuple[float, float, list[int]]:
        c0, z0 = self.snapshot()
        time.sleep(seconds)
        c1, z1 = self.snapshot()
        return (c1 - c0) / seconds, (z1 - z0) / seconds, list(self.sel.last_ready_fds)


def _connect(path: str) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path)
    return s


def _send_frame(s: socket.socket, body: bytes) -> None:
    s.sendall(FRAME.pack(len(body)) + body)


def _drain_resp(s: socket.socket) -> None:
    s.settimeout(1.0)
    try:
        s.recv(4096)
    except OSError:
        pass


def main() -> None:
    srv = Server()
    srv.start()
    print(f"server up: {srv.sock_path}")

    base_calls, base_zero, _ = srv.rate(1.0)
    print(f"BASELINE (bg traffic):   select/s={base_calls:>10.0f}  zero-to-ready/s={base_zero:>10.0f}")

    # CHURN: the live peg only appeared after minutes of connect/RPC/disconnect
    # churn (RF-5). Drive many cycles, including disconnect-mid-dispatch (client
    # RSTs while the server is in to_thread), then measure the IDLE spin after.
    def churn(n: int) -> None:
        for i in range(n):
            s = _connect(srv.sock_path)
            if i % 3 == 0:
                # disconnect mid-dispatch: send, RST immediately, never read resp
                s.sendall(FRAME.pack(5) + b"hello")
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                s.close()
            elif i % 3 == 1:
                _send_frame(s, b"hello"); _drain_resp(s); s.close()  # clean
            else:
                _send_frame(s, b"hello")  # GC-close without drain
                s.close()
    print("driving 600 connect/RPC/disconnect cycles (mixed clean / RST-mid-dispatch)...")
    churn(600)

    def probe(name: str, fn) -> None:
        fn()
        calls, zero, fds = srv.rate(1.5)
        spin = "SPIN" if calls > 5000 else "ok"
        print(f"{name:<28} select/s={calls:>10.0f}  zero-to-ready/s={zero:>10.0f}  ready_fds={fds}  [{spin}]")

    probe("POST-CHURN idle", lambda: None)

    # Mode A: abrupt RST (SO_LINGER 0 then close)
    def mode_rst() -> None:
        s = _connect(srv.sock_path)
        _send_frame(s, b"hello"); _drain_resp(s)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
    probe("A: abrupt RST (linger0)", mode_rst)

    # Mode B: half-close (shutdown SHUT_WR, keep socket object)
    held: list[socket.socket] = []
    def mode_halfclose() -> None:
        s = _connect(srv.sock_path)
        _send_frame(s, b"hello"); _drain_resp(s)
        s.shutdown(socket.SHUT_WR)
        held.append(s)  # keep ref so it is not GC-closed
    probe("B: half-close (SHUT_WR)", mode_halfclose)

    # Mode C: mid-frame disconnect (claim length 100, send 10, RST)
    def mode_midframe() -> None:
        s = _connect(srv.sock_path)
        s.sendall(FRAME.pack(100) + b"0123456789")
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
    probe("C: mid-frame RST", mode_midframe)

    # Mode D: idle after a complete frame (client stays connected, silent)
    def mode_idle() -> None:
        s = _connect(srv.sock_path)
        _send_frame(s, b"hello"); _drain_resp(s)
        held.append(s)  # keep open & idle
    probe("D: idle-after-frame", mode_idle)

    print(f"\ntop ready-fd hit counts: "
          f"{sorted(srv.sel.ready_fd_hits.items(), key=lambda kv: -kv[1])[:6]}")
    print("interpretation: a mode with select/s in the thousands reproduces the "
          "spin; ready_fds names the perpetually-ready fd.")


if __name__ == "__main__":
    main()
