# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-151 Phase 1 backstops: idle-connection reap (P1.2, nexus-5haam) and
shutdown-marker-first ordering (P1.3, nexus-yd6fy).

These complement the load-bearing peg oracle in
``test_rdr151_p1_selector_spin.py`` (P1.1). Both are deterministic and fast.
"""
from __future__ import annotations

import asyncio
import socket
import struct
import time
from pathlib import Path
from tempfile import mkdtemp

import nexus.daemon.t2_daemon as t2d


async def _idle_connection_is_reaped(cfgdir: Path) -> bool:
    """Open a raw connection, send nothing, and confirm the daemon closes it
    within the (patched-short) idle deadline rather than holding it open."""
    (cfgdir / "sockets").mkdir(exist_ok=True)
    uds = cfgdir / "sockets" / "t2.sock"

    daemon = t2d.T2Daemon(config_dir=cfgdir, db_path=cfgdir / "memory.db")
    await daemon.start()
    try:
        # A blocking client on a thread so its fd never lands in the daemon loop.
        result: dict[str, bool] = {}

        def _client() -> None:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(uds))
            # Longer than the test's observation window so that PRE-fix (no idle
            # deadline) the client stays blocked in recv and "closed" is never
            # set; only the daemon's post-fix reap can close us inside the window.
            s.settimeout(30.0)
            try:
                # Never send a frame. Pre-fix the daemon blocks in read_frame
                # forever; post-fix it closes us after _IDLE_READ_TIMEOUT.
                data = s.recv(1)
                result["closed"] = data == b""  # clean EOF from the daemon
            except OSError:
                result["closed"] = True  # connection torn down
            finally:
                s.close()

        import threading

        t = threading.Thread(target=_client, daemon=True)
        t.start()
        deadline = time.monotonic() + 4.0
        while t.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        t.join(timeout=1.0)
        return result.get("closed", False)
    finally:
        try:
            await asyncio.wait_for(daemon.stop(), timeout=10.0)
        except Exception:  # noqa: BLE001
            pass


def test_idle_connection_reaped_after_deadline(monkeypatch) -> None:
    monkeypatch.setattr(t2d, "_IDLE_READ_TIMEOUT", 0.4)
    cfgdir = Path(mkdtemp(prefix="rdr151p2-"))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        reaped = loop.run_until_complete(_idle_connection_is_reaped(cfgdir))
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    assert reaped, (
        "an idle accepted connection was not reaped within the idle deadline; "
        "a silent peer can hold the accepted socket/fd open indefinitely "
        "(RDR-151 P1.2)."
    )


async def _shutdown_marker_published_first(cfgdir: Path) -> list[str]:
    (cfgdir / "sockets").mkdir(exist_ok=True)
    daemon = t2d.T2Daemon(config_dir=cfgdir, db_path=cfgdir / "memory.db")
    await daemon.start()

    calls: list[str] = []
    registry = daemon._registry  # type: ignore[attr-defined]
    assert registry is not None
    real_mark = registry.mark_shutting_down
    real_relinquish = registry.relinquish

    def _spy_mark(record):  # type: ignore[no-untyped-def]
        calls.append("mark_shutting_down")
        return real_mark(record)

    def _spy_relinquish(record):  # type: ignore[no-untyped-def]
        calls.append("relinquish")
        return real_relinquish(record)

    registry.mark_shutting_down = _spy_mark  # type: ignore[method-assign]
    registry.relinquish = _spy_relinquish  # type: ignore[method-assign]

    await asyncio.wait_for(daemon.stop(), timeout=10.0)
    return calls


def test_shutdown_marker_published_before_relinquish(tmp_path) -> None:
    cfgdir = Path(mkdtemp(prefix="rdr151p3-"))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        calls = loop.run_until_complete(_shutdown_marker_published_first(cfgdir))
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    # A clean shutdown must publish the shutting_down marker so discoverers stop
    # resolving us immediately, and it must do so BEFORE the record is
    # relinquished/unlinked at the end of teardown (RDR-151 P1.3).
    assert "mark_shutting_down" in calls, (
        "stop() did not publish the shutdown marker (RDR-151 P1.3)."
    )
    assert calls.index("mark_shutting_down") < calls.index("relinquish"), (
        f"shutdown marker must precede relinquish; got order {calls}."
    )
