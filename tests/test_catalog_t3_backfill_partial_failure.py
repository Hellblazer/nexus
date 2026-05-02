# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-101 Phase 3 follow-up E (nexus-o6aa.9.11) + .9.18 retry path:
``nx catalog t3-backfill-doc-id`` partial-failure handling.

Two evolutions of the recovery story documented in
``docs/migration/rdr-101.md``:

1. **Per-chunk retry on batch failure** (.9.18). ChromaDB's ``col.update``
   is all-or-nothing: any chunk in the batch that violates a quota
   rejects the whole batch. Pre-fix, this swept ~22k clean chunks
   into the deferred list because they shared a batch with an
   over-cap chunk. Post-fix, batch failure triggers per-chunk
   updates so clean chunks land cleanly and only genuinely-failing
   chunks are reported.

2. **Deferred-class quota differentiation** (.9.18). The
   ``NumMetadataKeys`` quota class is expected during the Phase 4
   transition (chunks at 35-36 keys can't accept ``doc_id`` until
   Phase 4's prune-deprecated-keys verb ships). Errors carrying
   that class land in ``chunks_deferred`` (an operator-readable
   cleanup list) rather than ``errors``, and the verb exits 0.
   Genuine failures (network, auth, schema) still exit 1.

Validates the recovery story end-to-end against synthetic faults
that mimic the live failure mode Hal's first migration hit.
"""
from __future__ import annotations

import json
from pathlib import Path

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.catalog import events as ev
from nexus.catalog.catalog import Catalog
from nexus.catalog.event_log import EventLog
from nexus.commands.catalog import t3_backfill_doc_id_cmd


@pytest.fixture()
def isolated_nexus(tmp_path: Path) -> Path:
    return tmp_path / "test-catalog"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def chroma_client():
    client = chromadb.EphemeralClient()
    for col in list(client.list_collections()):
        try:
            client.delete_collection(col.name)
        except Exception:
            pass
    return client


def _seed(client, name: str, chunks: list[dict]) -> None:
    col = client.get_or_create_collection(
        name=name, embedding_function=DefaultEmbeddingFunction(),
    )
    col.add(
        ids=[c["id"] for c in chunks],
        documents=[c["content"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
    )


def _seed_event_log(catalog_dir: Path, events: list[ev.Event]) -> None:
    catalog_dir.mkdir(parents=True, exist_ok=True)
    Catalog.init(catalog_dir)
    log = EventLog(catalog_dir)
    log.append_many(events)


def _chunk_event(chunk_id: str, doc_id: str, coll_id: str) -> ev.Event:
    return ev.Event(
        type=ev.TYPE_CHUNK_INDEXED, v=0,
        payload=ev.ChunkIndexedPayload(
            chunk_id=chunk_id, chash="h", doc_id=doc_id,
            coll_id=coll_id, position=0,
        ),
        ts="2026-05-01T00:00:00Z",
    )


def _parse_json_payload(output: str) -> dict:
    return json.loads(output[output.find("{"):])


# ─────────────────────────────────────────────────────────────────────
# Fault-injection client wrappers
# ─────────────────────────────────────────────────────────────────────


class _BatchOnlyFaultClient:
    """First multi-id ``col.update`` per collection raises a transient
    error; per-chunk (single-id) updates pass through. Simulates the
    canonical ChromaDB Cloud quota response: batch all-or-nothing
    rejection, but the same chunks update fine when retried
    individually with the over-cap one isolated.

    For the .9.18 per-chunk retry test: the verb retries the batch
    chunk-by-chunk; each individual update succeeds.
    """

    def __init__(self, real_client):
        self._real = real_client
        self._tripped: set[str] = set()

    def get_collection(self, name: str):
        col = self._real.get_collection(name=name)
        outer = self

        class _FaultCol:
            _underlying = col

            def get(self, *a, **kw):
                return col.get(*a, **kw)

            def update(self, ids, metadatas, *a, **kw):
                if name not in outer._tripped and len(ids) > 1:
                    outer._tripped.add(name)
                    raise RuntimeError(
                        "simulated transient failure on batch update"
                    )
                return col.update(ids=ids, metadatas=metadatas, *a, **kw)

            def add(self, *a, **kw):
                return col.add(*a, **kw)

        return _FaultCol()


class _OverCapFaultClient:
    """Reject every ``col.update`` whose payload contains a chunk
    with ``len(metadata) > MAX_KEYS``, raising a ChromaDB-shaped
    error message. Multi-id calls reject if ANY chunk is over-cap
    (matching ChromaDB Cloud's batch-all-or-nothing behaviour);
    single-id calls reject only the offending chunk.

    The error message includes the canonical
    ``Number of metadata dictionary keys`` and ``NumMetadataKeys``
    substrings so the verb's deferred-class detector trips on it.
    """

    MAX_KEYS = 32

    def __init__(self, real_client):
        self._real = real_client

    def get_collection(self, name: str):
        col = self._real.get_collection(name=name)
        outer = self

        class _OverCapCol:
            _underlying = col

            def get(self, *a, **kw):
                return col.get(*a, **kw)

            def update(self, ids, metadatas, *a, **kw):
                for cid, m in zip(ids, metadatas):
                    if len(m) > outer.MAX_KEYS:
                        raise RuntimeError(
                            "Quota exceeded: 'Number of metadata "
                            "dictionary keys' exceeded quota limit "
                            f"for action 'Update': current usage of "
                            f"{len(m)} exceeds limit of {outer.MAX_KEYS}. "
                            f"NumMetadataKeys"
                        )
                return col.update(ids=ids, metadatas=metadatas, *a, **kw)

            def add(self, *a, **kw):
                return col.add(*a, **kw)

        return _OverCapCol()


class _ChunkSpecificFaultClient:
    """Reject ``col.update`` whose payload contains a specific
    chunk_id. Models a chunk-specific transient (network blip,
    permissions, schema validation) that is NOT the deferred-class
    quota — should land in ``errors`` and produce exit 1.
    """

    def __init__(self, real_client, doomed_chunk_id: str):
        self._real = real_client
        self._doomed = doomed_chunk_id

    def get_collection(self, name: str):
        col = self._real.get_collection(name=name)
        outer = self

        class _DoomCol:
            _underlying = col

            def get(self, *a, **kw):
                return col.get(*a, **kw)

            def update(self, ids, metadatas, *a, **kw):
                if outer._doomed in ids:
                    raise RuntimeError(
                        "schema validation failed: invalid metadata "
                        "value type"
                    )
                return col.update(ids=ids, metadatas=metadatas, *a, **kw)

            def add(self, *a, **kw):
                return col.add(*a, **kw)

        return _DoomCol()


# ─────────────────────────────────────────────────────────────────────
# Per-chunk retry on batch failure (.9.18)
# ─────────────────────────────────────────────────────────────────────


def test_batch_fault_recovers_via_per_chunk_retry(
    isolated_nexus, runner, chroma_client, monkeypatch,
):
    """RDR-101 Phase 3 follow-up .9.18: when ``col.update`` rejects
    a batch but the per-chunk updates would succeed, the verb falls
    back to per-chunk retry within the same run. Pre-fix the batch
    rejection swept all chunks into ``errors`` and required an
    operator re-run; post-fix recovery is automatic.
    """
    events = [
        _chunk_event("ch1", "uuid7-A", "code__test"),
        _chunk_event("ch2", "uuid7-B", "code__test"),
        _chunk_event("ch3", "uuid7-C", "code__test"),
    ]
    _seed_event_log(isolated_nexus, events)
    _seed(chroma_client, "code__test", [
        {"id": "ch1", "content": "x", "metadata": {"chunk_text_hash": "h1"}},
        {"id": "ch2", "content": "y", "metadata": {"chunk_text_hash": "h2"}},
        {"id": "ch3", "content": "z", "metadata": {"chunk_text_hash": "h3"}},
    ])

    fault_client = _BatchOnlyFaultClient(chroma_client)

    class _FaultT3:
        _client = fault_client

    monkeypatch.setattr("nexus.db.make_t3", lambda: _FaultT3())
    result = runner.invoke(t3_backfill_doc_id_cmd, ["--json"])

    assert result.exit_code == 0, (
        "per-chunk retry should recover from batch fault and exit 0; "
        f"output:\n{result.output}"
    )
    payload = _parse_json_payload(result.output)
    assert payload["chunks_updated"] == 3, payload
    assert payload["errors"] == [], payload
    assert payload["chunks_deferred"] == [], payload

    # Final verification: every chunk now carries doc_id.
    col = chroma_client.get_collection("code__test")
    for cid, want in [("ch1", "uuid7-A"), ("ch2", "uuid7-B"), ("ch3", "uuid7-C")]:
        meta = col.get(ids=[cid], include=["metadatas"])["metadatas"][0]
        assert meta["doc_id"] == want, (
            f"chunk {cid} did not get doc_id post-retry: {meta!r}"
        )


# ─────────────────────────────────────────────────────────────────────
# Deferred-class quota differentiation (.9.18)
# ─────────────────────────────────────────────────────────────────────


def test_overcap_chunk_lands_in_deferred_not_errors(
    isolated_nexus, runner, chroma_client, monkeypatch,
):
    """A batch with one over-cap chunk (33+ keys) and two clean
    chunks: pre-fix the whole batch was rejected and ALL three
    chunks landed in ``errors``. Post-fix:

    * Batch update fails on the over-cap chunk.
    * Per-chunk retry runs:
      - Clean chunks land cleanly (chunks_updated += 2).
      - Over-cap chunk fails with the NumMetadataKeys error → recognized
        as deferred-class → lands in ``chunks_deferred``, NOT ``errors``.
    * Verb exits 0 because there are no genuine errors.
    """
    events = [
        _chunk_event("ch1", "uuid7-A", "code__test"),
        _chunk_event("ch2", "uuid7-B", "code__test"),
        _chunk_event("ch3", "uuid7-C", "code__test"),
    ]
    _seed_event_log(isolated_nexus, events)
    # ch2 has 35 keys; ch1, ch3 have 5. Adding doc_id pushes ch2 to 36.
    fat_meta = {f"k{i}": f"v{i}" for i in range(35)}
    _seed(chroma_client, "code__test", [
        {"id": "ch1", "content": "x", "metadata": {"chunk_text_hash": "h1"}},
        {"id": "ch2", "content": "y", "metadata": fat_meta},
        {"id": "ch3", "content": "z", "metadata": {"chunk_text_hash": "h3"}},
    ])

    fault_client = _OverCapFaultClient(chroma_client)

    class _OvercapT3:
        _client = fault_client

    monkeypatch.setattr("nexus.db.make_t3", lambda: _OvercapT3())
    result = runner.invoke(t3_backfill_doc_id_cmd, ["--json"])

    assert result.exit_code == 0, (
        "deferred-class failures (NumMetadataKeys quota) must NOT "
        "fail the verb; only genuine errors do. "
        f"output:\n{result.output}"
    )
    payload = _parse_json_payload(result.output)

    # Clean chunks landed.
    assert payload["chunks_updated"] == 2, (
        f"clean chunks ch1 and ch3 should land via per-chunk retry; "
        f"got {payload['chunks_updated']} updated"
    )
    # Over-cap chunk in deferred list.
    assert payload["chunks_deferred_count"] == 1, payload
    assert len(payload["chunks_deferred"]) == 1
    deferred_record = payload["chunks_deferred"][0]
    assert deferred_record["chunk_id"] == "ch2"
    assert deferred_record["collection"] == "code__test"
    assert "NumMetadataKeys" in deferred_record["error"] or \
           "Number of metadata dictionary keys" in deferred_record["error"]
    # No genuine errors.
    assert payload["errors"] == [], payload


def test_genuine_per_chunk_error_still_fails_verb(
    isolated_nexus, runner, chroma_client, monkeypatch,
):
    """Per-chunk retry that hits a non-deferred-class error
    (e.g. schema validation) lands in ``errors`` and the verb
    exits 1. Operators can detect genuine failures vs deferred-class
    via the report shape and exit code.
    """
    events = [
        _chunk_event("ch1", "uuid7-A", "code__test"),
        _chunk_event("ch2", "uuid7-B", "code__test"),
    ]
    _seed_event_log(isolated_nexus, events)
    _seed(chroma_client, "code__test", [
        {"id": "ch1", "content": "x", "metadata": {"chunk_text_hash": "h1"}},
        {"id": "ch2", "content": "y", "metadata": {"chunk_text_hash": "h2"}},
    ])

    fault_client = _ChunkSpecificFaultClient(chroma_client, doomed_chunk_id="ch2")

    class _DoomT3:
        _client = fault_client

    monkeypatch.setattr("nexus.db.make_t3", lambda: _DoomT3())
    result = runner.invoke(t3_backfill_doc_id_cmd, ["--json"])

    assert result.exit_code != 0, (
        "non-deferred-class error must fail the verb"
    )
    payload = _parse_json_payload(result.output)
    # ch1 lands cleanly via per-chunk retry; ch2 fails persistently.
    assert payload["chunks_updated"] == 1, payload
    assert payload["chunks_deferred"] == [], payload
    assert len(payload["errors"]) == 1
    err = payload["errors"][0]
    assert err["chunk_id"] == "ch2"
    assert err["stage"] == "update_per_chunk"
    assert "schema validation" in err["error"]
