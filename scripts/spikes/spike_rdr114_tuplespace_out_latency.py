"""RDR-114 spike: measure tuplespace.out RPC p99 latency under load.

Builds a real T2 daemon on tmp config, seeds the production registry,
opens a T2Client over UDS, and times 1000 sequential ``tuplespace.out``
RPC calls. Reports min/p50/p95/p99/max.

The daemon runs on a background thread (its own event loop); the client
runs synchronously on the main thread. This avoids the deadlock you'd
get from sync-client-on-same-loop-as-daemon.

Pass criterion: p99 < 250 ms under sequential load.
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path


def _spawn_daemon(daemon) -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """Run daemon.start() on a background loop and return (loop, thread)."""
    started = threading.Event()
    loop = asyncio.new_event_loop()

    def _thread() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(daemon.start())
        started.set()
        loop.run_forever()

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    started.wait(timeout=30.0)
    return loop, t


def main() -> int:
    import chromadb
    from nexus.daemon.subspace_registry import RegistryStore
    from nexus.daemon.t2_client import T2Client
    from nexus.daemon.t2_daemon import T2Daemon
    from nexus.daemon.tuplespace_service import TuplespaceService
    from nexus.db.t2 import T2Database

    with tempfile.TemporaryDirectory() as tmpd:
        config_dir = Path(tmpd) / "nexus"
        config_dir.mkdir(parents=True)
        memory_db = config_dir / "memory.db"
        tuples_db = config_dir / "tuples.db"
        chroma_dir = config_dir / "chroma"

        t2db = T2Database(memory_db)
        registry_store = RegistryStore(tuples_db_path=tuples_db)
        chroma_client = chromadb.PersistentClient(path=str(chroma_dir))
        service = TuplespaceService(
            tuples_db_path=tuples_db,
            chroma_client=chroma_client,
        )
        daemon = T2Daemon(
            config_dir=config_dir,
            t2db=t2db,
            tuples_db_path=tuples_db,
            registry_store=registry_store,
            tuplespace_service=service,
        )
        loop, t = _spawn_daemon(daemon)

        # Daemon is now serving on its background loop. Use sync client.
        try:
            client = T2Client(uds_path=daemon.uds_path)
            for i in range(5):
                client.tuplespace.out(
                    subspace="tasks/nexus",
                    content=f"warmup-{i}",
                    dimensions={
                        "status": "open",
                        "priority": "P3",
                        "created_by": "rdr-114-spike",
                    },
                )

            N = 1000
            samples_ms: list[float] = []
            for i in range(N):
                t0 = time.perf_counter()
                client.tuplespace.out(
                    subspace="tasks/nexus",
                    content=f"spike-{i}",
                    dimensions={
                        "status": "open",
                        "priority": "P3",
                        "created_by": "rdr-114-spike",
                    },
                )
                samples_ms.append((time.perf_counter() - t0) * 1000.0)

            samples_ms.sort()
            p50 = samples_ms[len(samples_ms) // 2]
            p95 = samples_ms[int(len(samples_ms) * 0.95)]
            p99 = samples_ms[int(len(samples_ms) * 0.99)]
            mean = statistics.mean(samples_ms)
            stdev = statistics.stdev(samples_ms)

            print(f"N={N} sequential tuplespace.out calls (warmup=5)")
            print(f"  min:  {samples_ms[0]:7.2f} ms")
            print(f"  p50:  {p50:7.2f} ms")
            print(f"  p95:  {p95:7.2f} ms")
            print(f"  p99:  {p99:7.2f} ms")
            print(f"  max:  {samples_ms[-1]:7.2f} ms")
            print(f"  mean: {mean:7.2f} ms  stdev: {stdev:6.2f}")
            print()
            target_p99_ms = 250.0
            if p99 < target_p99_ms:
                print(f"PASS: p99 {p99:.2f} ms < {target_p99_ms:.0f} ms target")
                rc = 0
            else:
                print(f"FAIL: p99 {p99:.2f} ms >= {target_p99_ms:.0f} ms target")
                rc = 1

            client.close()
        finally:
            future = asyncio.run_coroutine_threadsafe(daemon.stop(), loop)
            try:
                future.result(timeout=10.0)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=5.0)
            t2db.close()
            service.close()

    return rc


if __name__ == "__main__":
    sys.exit(main())
