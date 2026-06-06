# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-151 P2.1a (nexus-gcu07): taxonomy write ops must be serialised through the
daemon's ``_catalog_write_lock``.

Root cause of the 100% CPU peg (captured live 2026-06-05): ``taxonomy.*`` write
ops dispatched as ``taxonomy.<method>`` were NOT covered by the daemon's
``_catalog_write_lock`` guard (which only matched ``catalog_write.*``), so N
concurrent ``taxonomy.persist_assignments`` RPCs each launched a parallel
``asyncio.to_thread`` writer, all racing for the single SQLite write lock and
wedging executor threads on the contended lock. Serialising taxonomy writes
through the existing asyncio lock makes them cooperative (one at a time, yielding
the event loop between them) instead of a thread pile-up.

This test drives two concurrent dispatches of a taxonomy write op and asserts the
threaded bodies never overlap. Pre-fix they run concurrently (max overlap 2);
post-fix the asyncio lock serialises them (max overlap 1).
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from tempfile import mkdtemp


async def _max_overlap_for_op(op: str) -> int:
    from nexus.daemon.t2_daemon import T2Daemon

    cfgdir = Path(mkdtemp(prefix="rdr151p2-"))
    (cfgdir / "sockets").mkdir(exist_ok=True)
    daemon = T2Daemon(config_dir=cfgdir, db_path=cfgdir / "memory.db")
    await daemon.start()

    state = {"cur": 0, "max": 0}
    lock = threading.Lock()

    def _slow_write(*_a, **_k) -> int:
        with lock:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.15)  # hold the "write" so a concurrent one would overlap
        with lock:
            state["cur"] -= 1
        return 0

    daemon._dispatch_table[op] = _slow_write  # type: ignore[index]

    frame = {"op": op, "args": [], "kwargs": {}, "request_id": 1}
    try:
        await asyncio.gather(
            daemon._dispatch(dict(frame), is_uds=True),
            daemon._dispatch(dict(frame), is_uds=True),
        )
    finally:
        try:
            await asyncio.wait_for(daemon.stop(), timeout=10.0)
        except Exception:  # noqa: BLE001
            pass
    return state["max"]


def test_taxonomy_writes_are_serialised() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        overlap = loop.run_until_complete(
            _max_overlap_for_op("taxonomy.persist_assignments")
        )
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    assert overlap == 1, (
        f"taxonomy write ops ran concurrently (max overlap {overlap}); they must "
        f"be serialised through _catalog_write_lock so N concurrent writers do "
        f"not pile up threads on the SQLite write lock (RDR-151 P2.1a)."
    )


def test_catalog_writes_still_serialised() -> None:
    """Regression guard: the pre-existing catalog_write.* serialisation (RDR-146)
    is not broken by extending the guard to taxonomy.*."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        overlap = loop.run_until_complete(
            _max_overlap_for_op("catalog_write.register_owner")
        )
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    assert overlap == 1, f"catalog_write.* serialisation regressed (overlap {overlap})"
