"""Spike: stress chromadb.PersistentClient with concurrent processes.

Reproduces the access pattern of ``src/nexus/cockpit/hook_bridge.py``:
multiple independent OS processes each open ``PersistentClient(path=<dir>)``
against the *same* persist directory, then bang on a shared collection with
``add`` / ``upsert`` / ``query`` calls. We measure:

* completion (did every worker finish without exception?),
* corruption (does the final ``coll.count()`` equal the sum of intended
  writes from each worker?),
* timing (per-worker wallclock to spot serialization / lock contention),
* read consistency (does a final ``query`` from a fresh client see the
  expected number of records?).

This is a spike, not a test. It produces a JSON summary on stdout. Run::

    uv run python tests/spike/chroma_multiprocess_spike.py \
        --workers 5 --records-per-worker 50

Findings live in
``docs/field-reports/2026-05-15-chroma-multiprocess-spike.md``.

The harness does NOT touch the real ``~/.config/nexus/chroma`` directory; it
allocates a tmp dir per run and cleans it up afterward.

Issue: nexus-23lr / epic nexus-hp9f / RDR-111.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path


def _worker(
    worker_id: int,
    chroma_dir: str,
    coll_name: str,
    records: int,
    start_barrier_path: str,
) -> dict:
    """Run one worker process: open client, slam collection, return stats."""
    # Wait for all workers to be alive before banging on chroma. We use a
    # simple file-presence barrier so all processes start writing within the
    # same ~10ms window. multiprocessing.Barrier across fresh-spawned
    # interpreters is awkward; a file is robust.
    deadline = time.monotonic() + 30.0
    while not Path(start_barrier_path).exists():
        if time.monotonic() > deadline:
            return {"worker_id": worker_id, "error": "barrier timeout"}
        time.sleep(0.01)

    t0 = time.monotonic()
    result: dict = {"worker_id": worker_id, "records_attempted": records}
    try:
        import chromadb

        client = chromadb.PersistentClient(path=chroma_dir)
        coll = client.get_or_create_collection(name=coll_name)

        # Phase 1: adds.
        ids = [f"w{worker_id}-r{i}" for i in range(records)]
        docs = [f"worker {worker_id} record {i}" for i in range(records)]
        embeddings = [[float(worker_id), float(i), 0.0] for i in range(records)]
        metadatas = [{"worker_id": worker_id, "i": i} for i in range(records)]

        # Mix add + upsert (the bridge uses upsert).
        coll.upsert(ids=ids, documents=docs, embeddings=embeddings, metadatas=metadatas)
        t_after_write = time.monotonic()

        # Phase 2: read-back.
        got = coll.get(ids=ids[: min(records, 10)])
        readback = len(got.get("ids") or [])

        # Phase 3: a small query.
        q = coll.query(
            query_embeddings=[[float(worker_id), 0.0, 0.0]],
            n_results=min(5, records),
        )
        q_hits = len((q.get("ids") or [[]])[0])

        # Final count visible to this worker after its writes.
        final_count = coll.count()

        result.update(
            {
                "ok": True,
                "wallclock_total_s": round(time.monotonic() - t0, 3),
                "wallclock_write_s": round(t_after_write - t0, 3),
                "readback_n": readback,
                "query_hits": q_hits,
                "self_visible_count": final_count,
            }
        )
    except Exception as e:  # noqa: BLE001 — spike, capture and report
        result.update(
            {
                "ok": False,
                "error_type": type(e).__name__,
                "error": str(e)[:500],
                "traceback": traceback.format_exc()[-2000:],
                "wallclock_total_s": round(time.monotonic() - t0, 3),
            }
        )
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--records-per-worker", type=int, default=50)
    ap.add_argument("--persist-dir", type=str, default=None)
    ap.add_argument("--keep", action="store_true", help="don't delete tmp dir")
    args = ap.parse_args()

    persist_dir = args.persist_dir or tempfile.mkdtemp(prefix="chroma-spike-")
    barrier = os.path.join(persist_dir, "..", f".barrier-{os.getpid()}")
    coll_name = "spike_coll"

    print(f"# persist_dir: {persist_dir}", file=sys.stderr)
    print(f"# workers: {args.workers}", file=sys.stderr)
    print(f"# records_per_worker: {args.records_per_worker}", file=sys.stderr)

    # Spawn workers. Use spawn context so each worker gets a clean interpreter
    # (the bridge subprocesses are fresh `python -m` invocations).
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=args.workers) as pool:
        async_results = [
            pool.apply_async(
                _worker,
                kwds={
                    "worker_id": i,
                    "chroma_dir": persist_dir,
                    "coll_name": coll_name,
                    "records": args.records_per_worker,
                    "start_barrier_path": barrier,
                },
            )
            for i in range(args.workers)
        ]
        # Give pool a moment to fork + import chromadb in each child.
        time.sleep(2.0)
        Path(barrier).touch()
        t_run = time.monotonic()
        results = [r.get(timeout=300) for r in async_results]
        wallclock = round(time.monotonic() - t_run, 3)

    # Post-mortem: open a fresh client and count.
    try:
        import chromadb

        client = chromadb.PersistentClient(path=persist_dir)
        coll = client.get_or_create_collection(name=coll_name)
        final_count = coll.count()
    except Exception as e:  # noqa: BLE001
        final_count = f"error: {type(e).__name__}: {e}"

    expected = args.workers * args.records_per_worker
    summary = {
        "persist_dir": persist_dir,
        "workers": args.workers,
        "records_per_worker": args.records_per_worker,
        "expected_total": expected,
        "final_count": final_count,
        "wallclock_run_s": wallclock,
        "ok_workers": sum(1 for r in results if r.get("ok")),
        "failed_workers": sum(1 for r in results if not r.get("ok")),
        "results": results,
        "verdict": (
            "CLEAN"
            if final_count == expected
            and all(r.get("ok") for r in results)
            else (
                "CORRUPTION"
                if isinstance(final_count, int)
                and final_count != expected
                and all(r.get("ok") for r in results)
                else "RACE_OR_BLOCK"
            )
        ),
    }
    print(json.dumps(summary, indent=2))

    try:
        Path(barrier).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    if not args.keep and not args.persist_dir:
        shutil.rmtree(persist_dir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
