# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-151 nexus-xmohw (issue #1137): ALL daemon writes serialise through the
single write lock, so the T2 daemon is a genuine single serialised writer and
cannot self-contend into a SQLITE_BUSY retry spin / 100% CPU peg.

2.1a serialised only ``catalog_write.*`` + ``taxonomy.*``. #1137 showed a
``memory.delete`` burst plus the daemon's own 30s reclaim loop racing the WAL
writer lock — both un-serialised. These tests assert (1) memory writes now
serialise, (2) the reclaim writer serialises against serve-path writes, and
(3) a coverage forcing-function so no future mutating op silently bypasses.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import threading
import time
from pathlib import Path
from tempfile import mkdtemp


async def _max_overlap_for_op(op: str) -> int:
    """Drive two concurrent dispatches of *op* with a slow threaded body and
    return the max observed overlap (2 = concurrent/un-serialised, 1 = serial)."""
    from nexus.daemon.t2_daemon import T2Daemon

    cfgdir = Path(mkdtemp(prefix="xmohw-"))
    (cfgdir / "sockets").mkdir(exist_ok=True)
    daemon = T2Daemon(config_dir=cfgdir, db_path=cfgdir / "memory.db")
    await daemon.start()

    state = {"cur": 0, "max": 0}
    lock = threading.Lock()

    def _slow_write(*_a, **_k) -> int:
        with lock:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.15)
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


def test_memory_delete_is_serialized() -> None:
    """#1137 trigger: a burst of memory.delete must serialise (overlap 1)."""
    assert asyncio.run(_max_overlap_for_op("memory.delete")) == 1


def test_memory_put_is_serialized() -> None:
    assert asyncio.run(_max_overlap_for_op("memory.put")) == 1


def test_reclaim_serializes_against_serve_write() -> None:
    """#1137 mechanism 2: the internal reclaim writer must not run concurrently
    with a serve-path write — both hold the single write lock."""
    from nexus.daemon.t2_daemon import T2Daemon

    async def _drive() -> int:
        cfgdir = Path(mkdtemp(prefix="xmohw-rec-"))
        (cfgdir / "sockets").mkdir(exist_ok=True)
        daemon = T2Daemon(config_dir=cfgdir, db_path=cfgdir / "memory.db")
        await daemon.start()

        state = {"cur": 0, "max": 0}
        guard = threading.Lock()

        def _bump_hold_drop() -> int:
            with guard:
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
            time.sleep(0.15)
            with guard:
                state["cur"] -= 1
            return 0

        # A serve-path write (serialised) ...
        daemon._dispatch_table["memory.put"] = _bump_hold_drop  # type: ignore[index]
        # ... and the reclaim writer pointed at the same instrumented body.
        fake_t2 = type("T", (), {"aspect_queue": type("Q", (), {
            "reclaim_stale": staticmethod(lambda _t: _bump_hold_drop()),
        })()})()
        frame = {"op": "memory.put", "args": [], "kwargs": {}, "request_id": 1}
        try:
            await asyncio.gather(
                daemon._dispatch(dict(frame), is_uds=True),
                daemon._reclaim_stale_once(fake_t2),
            )
        finally:
            try:
                await asyncio.wait_for(daemon.stop(), timeout=10.0)
            except Exception:  # noqa: BLE001
                pass
        return state["max"]

    assert asyncio.run(_drive()) == 1, "reclaim must not overlap a serve write"


def test_write_op_coverage() -> None:
    """Forcing function (silent-recurrence guard): every dispatchable mutating
    store method MUST be serialised — present in _WRITE_OPS or matched by a
    serialised prefix. A future writer added without serialisation fails here."""
    from nexus.db.t2 import T2Database
    from nexus.daemon.t2_daemon import (
        _RPC_DENY_OPS,
        _T2_STORE_ATTRS,
        _WRITE_OPS,
        _WRITE_SERIALIZED_PREFIXES,
        _build_dispatch_table,
    )

    write_verb = re.compile(
        r"^(put|delete|merge|update|insert|upsert|claim|complete|mark|enqueue|"
        r"dequeue|set_|clear|prune|purge|flag|promote|reclaim|save|store|persist|"
        r"increment|add_|expire|trim|log_|rename|rebuild|assign|commit|vacuum|"
        r"archive|sweep|drop|apply|write|record|remove|migrate|backfill)"
    )

    cfgdir = Path(mkdtemp(prefix="xmohw-cov-"))
    with T2Database(cfgdir / "memory.db", run_migrations=True) as db:
        table = _build_dispatch_table(db)
        missing: list[str] = []
        for store_name in _T2_STORE_ATTRS:
            store = getattr(db, store_name, None)
            if store is None:
                continue
            for attr in dir(store):
                if attr.startswith("_"):
                    continue
                op = f"{store_name}.{attr}"
                if op in _RPC_DENY_OPS or op not in table:
                    continue  # not dispatchable
                if not callable(getattr(store, attr)):
                    continue
                if not write_verb.match(attr):
                    continue  # not a mutating verb
                serialized = (
                    op in _WRITE_OPS or op.startswith(_WRITE_SERIALIZED_PREFIXES)
                )
                if not serialized:
                    missing.append(op)

    assert not missing, (
        "mutating dispatch ops not serialised (add to _WRITE_OPS): "
        + ", ".join(sorted(missing))
    )


def test_write_ops_have_no_stale_entries() -> None:
    """Every _WRITE_OPS entry must name a real dispatchable op (no typos / drift)."""
    from nexus.db.t2 import T2Database
    from nexus.daemon.t2_daemon import _WRITE_OPS, _build_dispatch_table

    cfgdir = Path(mkdtemp(prefix="xmohw-stale-"))
    with T2Database(cfgdir / "memory.db", run_migrations=True) as db:
        table = _build_dispatch_table(db)
        # database.* methods are added under the pseudo-store; include them.
        valid = set(table)
        stale = [op for op in _WRITE_OPS if op not in valid]
    assert not stale, f"_WRITE_OPS names non-dispatchable ops: {stale}"
