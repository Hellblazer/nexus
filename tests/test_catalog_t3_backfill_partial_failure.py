# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-101 Phase 3 follow-up E (nexus-o6aa.9.11): partial-failure
injection for ``nx catalog t3-backfill-doc-id``.

The recovery story documented in ``docs/migration/rdr-101.md`` says:

  > **`t3-backfill-doc-id` partial** — some chunks missing `doc_id`
  > metadata; `doctor --t3-doc-id-coverage` flags them. Re-run
  > `nx catalog t3-backfill-doc-id`. Idempotent — already-backfilled
  > chunks are no-ops.

This file validates that claim end-to-end. The .9.10 sandbox harness
covers the happy path. Here we inject faults into ChromaDB's
``col.update`` to simulate the network-blip / quota-error failure
modes the migration doc names, and confirm:

* The verb exits non-zero (so operator scripts can detect partial).
* Successful chunks are persisted with ``doc_id`` set.
* Failed chunks remain in their pre-update state.
* Re-running the verb (without the fault injector) recovers cleanly:
  exit 0, all chunks now carry ``doc_id``.
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


class _FaultInjectingClient:
    """Wraps a ChromaDB client; the FIRST ``col.update(...)`` call on
    each collection raises ``RuntimeError("simulated 503")``. Subsequent
    calls pass through. This simulates the canonical network-blip
    failure mode the migration doc warns about.

    Why "first call per collection, then pass": the verb batches in
    300-id windows; with 4 chunks total in our sandbox the second
    batch is empty, so a once-per-collection fault is enough to
    exercise the partial-write recovery path without making the
    fixture too clever.
    """

    def __init__(self, real_client):
        self._real = real_client
        self._tripped: set[str] = set()

    def get_collection(self, name: str):
        col = self._real.get_collection(name=name)
        outer = self

        class _FaultCol:
            _underlying = col

            def get(self, *args, **kwargs):
                return col.get(*args, **kwargs)

            def update(self, *args, **kwargs):
                if name not in outer._tripped:
                    outer._tripped.add(name)
                    raise RuntimeError(
                        f"simulated 503 on first update to {name}"
                    )
                return col.update(*args, **kwargs)

            def add(self, *args, **kwargs):
                return col.add(*args, **kwargs)

        return _FaultCol()


# ─────────────────────────────────────────────────────────────────────
# Partial-failure path
# ─────────────────────────────────────────────────────────────────────


def test_first_update_per_collection_fails_then_recovers(
    isolated_nexus, runner, chroma_client, monkeypatch,
):
    """Inject a fault on the first ``col.update`` call. Verb exits 1
    (errors reported); chunks remain pre-update. Re-run without the
    injector recovers cleanly — verb exits 0, all chunks now carry
    ``doc_id``.
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

    # First run: inject fault.
    fault_client = _FaultInjectingClient(chroma_client)

    class _FaultT3:
        _client = fault_client

    monkeypatch.setattr("nexus.db.make_t3", lambda: _FaultT3())
    result_fault = runner.invoke(t3_backfill_doc_id_cmd, ["--json"])

    # Verb exits 1 because errors were collected.
    assert result_fault.exit_code != 0, (
        "verb must exit non-zero on partial failure for operator-script "
        "detectability"
    )

    # Strip any non-JSON prefix.
    out = result_fault.output
    payload = json.loads(out[out.find("{"):])
    assert payload["chunks_updated"] == 0, (
        "fault tripped on the only batch; no chunks should land"
    )
    assert len(payload["errors"]) == 1, (
        f"expected one batch-update error; got {payload['errors']}"
    )
    assert payload["errors"][0]["stage"] == "update"
    assert "simulated 503" in payload["errors"][0]["error"]

    # Verify chunks did NOT receive doc_id metadata (the failed update
    # never landed).
    col = chroma_client.get_collection("code__test")
    for cid in ("ch1", "ch2"):
        meta = col.get(ids=[cid], include=["metadatas"])["metadatas"][0]
        assert "doc_id" not in meta, (
            f"chunk {cid} unexpectedly carries doc_id after failed batch"
        )

    # Second run: NO fault injector. Confirm idempotent recovery.
    class _CleanT3:
        _client = chroma_client

    monkeypatch.setattr("nexus.db.make_t3", lambda: _CleanT3())
    result_clean = runner.invoke(t3_backfill_doc_id_cmd, ["--json"])

    assert result_clean.exit_code == 0, result_clean.output
    out = result_clean.output
    payload2 = json.loads(out[out.find("{"):])
    assert payload2["chunks_updated"] == 2, (
        f"recovery run should backfill the 2 chunks the fault dropped; "
        f"got {payload2['chunks_updated']}"
    )
    assert payload2["chunks_already_correct"] == 0
    assert payload2["errors"] == []

    # Final verification: chunks now carry doc_id.
    col = chroma_client.get_collection("code__test")
    expected = {"ch1": "uuid7-A", "ch2": "uuid7-B"}
    for cid, want in expected.items():
        meta = col.get(ids=[cid], include=["metadatas"])["metadatas"][0]
        assert meta["doc_id"] == want, (
            f"chunk {cid} doc_id mismatch after recovery: got {meta!r}"
        )


def test_partial_completion_some_collections_succeed(
    isolated_nexus, runner, chroma_client, monkeypatch,
):
    """When the fault injector trips on collection A's first update
    but collection B has already completed, the verb reports the
    partial state correctly: chunks_updated > 0, errors > 0,
    exit non-zero. Re-running brings the failed collection up.
    """
    events = [
        _chunk_event("ch1", "uuid7-A", "code__alpha"),
        _chunk_event("ch2", "uuid7-B", "code__beta"),
    ]
    _seed_event_log(isolated_nexus, events)
    _seed(chroma_client, "code__alpha", [
        {"id": "ch1", "content": "x", "metadata": {"chunk_text_hash": "ha"}},
    ])
    _seed(chroma_client, "code__beta", [
        {"id": "ch2", "content": "y", "metadata": {"chunk_text_hash": "hb"}},
    ])

    # The fault injector trips once per collection. With 1 chunk per
    # collection there's only one batch each; both fail. To get one
    # success and one failure, drop the fault for the second collection
    # before its update lands.
    class _FaultOnceClient:
        """Trips on the FIRST update across all collections, then
        passes for everything subsequent."""

        def __init__(self, real_client):
            self._real = real_client
            self._tripped = False

        def get_collection(self, name: str):
            col = self._real.get_collection(name=name)
            outer = self

            class _FaultCol:
                _underlying = col

                def get(self, *args, **kwargs):
                    return col.get(*args, **kwargs)

                def update(self, *args, **kwargs):
                    if not outer._tripped:
                        outer._tripped = True
                        raise RuntimeError("simulated 503 on first update")
                    return col.update(*args, **kwargs)

                def add(self, *args, **kwargs):
                    return col.add(*args, **kwargs)

            return _FaultCol()

    fault_client = _FaultOnceClient(chroma_client)

    class _FaultT3:
        _client = fault_client

    monkeypatch.setattr("nexus.db.make_t3", lambda: _FaultT3())
    result = runner.invoke(t3_backfill_doc_id_cmd, ["--json"])  # noqa: E501

    assert result.exit_code != 0
    out = result.output
    payload = json.loads(out[out.find("{"):])

    # One collection failed (errored), one succeeded (updated).
    assert payload["chunks_updated"] == 1, (
        f"expected 1 collection to land cleanly; got "
        f"{payload['chunks_updated']}"
    )
    assert len(payload["errors"]) == 1

    # Re-run recovers the failed collection.
    class _CleanT3:
        _client = chroma_client

    monkeypatch.setattr("nexus.db.make_t3", lambda: _CleanT3())
    recover = runner.invoke(t3_backfill_doc_id_cmd, ["--json"])
    assert recover.exit_code == 0, recover.output
    out = recover.output
    payload2 = json.loads(out[out.find("{"):])
    # Idempotent: the already-good chunk is "already_correct"; the
    # newly-good chunk is "updated".
    assert payload2["chunks_updated"] == 1
    assert payload2["chunks_already_correct"] == 1
    assert payload2["errors"] == []
