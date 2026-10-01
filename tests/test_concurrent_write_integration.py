# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-9p0c: integration tests for backfill-hash under concurrent indexer
writes (its reidentify twin was deleted with ``nx t3 reidentify``, RDR-223
P3.3, nexus-z0o2p.25).

The verb uses a two-pass walk (pass 1 collects ids, pass 2 fetches by
exact id). The two-pass design is best-effort under concurrent writes:
new chunks added during pass 1 may be missed by this iteration, but
the verb is idempotent so a re-run picks them up. This contract was
informally documented in code comments after RDR-108 nexus-2exh's
review caveat #4; this test file locks it.

Test pattern:
  1. Seed a real chromadb.EphemeralClient collection with N>300 chunks
     (forces multi-page pass 1).
  2. Spawn a background thread that adds K more chunks during the
     verb's run.
  3. Run the verb.
  4. Assert: all pre-existing chunks were processed.
  5. Re-run; assert idempotent recovery (any concurrent-write chunks
     missed in run 1 are picked up in run 2).

The pre-existing chunks vs concurrent-write chunks split is the key
invariant. The contract is "pre-existing always processed; concurrent
may be missed; idempotent re-run catches the misses."
"""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import UTC, datetime

import pytest
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction

from nexus.db.t3 import T3Database
from tests.conftest import make_vector_test_client


_PAGE = 300  # chromadb Cloud per-call limit; mirror in tests for clarity


@pytest.fixture()
def t3_db() -> T3Database:
    """Real T3Database backed by an ephemeral local chroma."""
    return T3Database(
        _client=make_vector_test_client(),
        _ef_override=DefaultEmbeddingFunction(),
    )


def _seed_chunks_no_hash(
    t3_db: T3Database, *, collection: str, count: int, prefix: str,
) -> list[str]:
    """Seed *count* chunks in *collection* WITHOUT chunk_text_hash.

    Returns the list of seeded chunk ids in insertion order.
    """
    col = t3_db._client.get_or_create_collection(collection)
    ids = [f"{prefix}-{i:04d}" for i in range(count)]
    docs = [f"chunk content {prefix} {i}" for i in range(count)]
    metas = [{"indexed_at": "2024-01-01T00:00:00+00:00"} for _ in range(count)]
    # Add in 300-batches to respect Cloud-style chunking.
    for start in range(0, count, _PAGE):
        end = start + _PAGE
        col.add(
            ids=ids[start:end],
            documents=docs[start:end],
            metadatas=metas[start:end],
        )
    return ids


def _writer_thread(
    t3_db: T3Database,
    *,
    collection: str,
    count: int,
    prefix: str,
    stop_event: threading.Event,
    seed_with_hash: bool,
) -> threading.Thread:
    """Spawn a background thread that adds *count* chunks one-at-a-time
    until ``stop_event`` is set OR all *count* chunks are written.

    The thread writes one chunk every ~5ms so it overlaps the verb's
    pass-1 walk without flooding chromadb.
    """
    def _run():
        col = t3_db._client.get_or_create_collection(collection)
        for i in range(count):
            if stop_event.is_set():
                return
            doc = f"concurrent {prefix} {i}"
            meta: dict = {"indexed_at": datetime.now(UTC).isoformat()}
            if seed_with_hash:
                meta["chunk_text_hash"] = hashlib.sha256(doc.encode()).hexdigest()
            try:
                col.add(
                    ids=[f"{prefix}-conc-{i:04d}"],
                    documents=[doc],
                    metadatas=[meta],
                )
            except Exception:
                # ChromaDB may reject during concurrent ops; that's
                # acceptable for the test, just record by exiting.
                return
            time.sleep(0.005)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ── backfill-hash ─────────────────────────────────────────────────────────


class TestBackfillHashConcurrentWrites:
    """Under concurrent writes, backfill-hash must process every
    pre-existing chunk and be idempotent on re-run."""

    def test_preexisting_processed_concurrent_may_be_missed_idempotent_recovery(
        self, t3_db: T3Database,
    ) -> None:
        from nexus.commands.collection import _backfill_chunk_text_hash

        coll_name = "code__concurrent_backfill"
        # 350 pre-existing chunks; > _PAGE forces pass-1 multi-page.
        pre_ids = _seed_chunks_no_hash(
            t3_db, collection=coll_name, count=350, prefix="pre",
        )

        col = t3_db._client.get_or_create_collection(coll_name)
        stop = threading.Event()
        writer = _writer_thread(
            t3_db, collection=coll_name, count=50, prefix="pre",
            stop_event=stop, seed_with_hash=False,
        )

        # Run backfill while writer is still adding chunks.
        try:
            _backfill_chunk_text_hash(col)
        finally:
            stop.set()
            writer.join(timeout=10)

        # Invariant 1: every pre-existing chunk now has chunk_text_hash.
        present = col.get(ids=pre_ids, include=["metadatas"])
        for cid, meta in zip(present["ids"], present["metadatas"]):
            assert meta and meta.get("chunk_text_hash"), (
                f"pre-existing chunk {cid} was missed by backfill; "
                f"meta={meta}"
            )

        # Invariant 2: idempotent re-run picks up any concurrent-write
        # chunks the first run missed AND leaves already-hashed rows
        # unchanged.
        _backfill_chunk_text_hash(col)

        # Verify: now EVERY chunk (pre-existing + concurrent) has the
        # hash. The re-run sweeps the collection completely.
        offset = 0
        missing = 0
        while True:
            page = col.get(limit=_PAGE, offset=offset, include=["metadatas"])
            ids = page.get("ids") or []
            if not ids:
                break
            for cid, meta in zip(ids, page.get("metadatas") or []):
                if not (meta or {}).get("chunk_text_hash"):
                    missing += 1
            if len(ids) < _PAGE:
                break
            offset += _PAGE
        assert missing == 0, (
            f"after idempotent re-run, {missing} chunks still lack "
            f"chunk_text_hash; the verb is not converging on the full "
            f"corpus across re-runs"
        )
