# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the RDR-101 Phase 2 PR γ ``nx catalog t3-backfill-doc-id`` verb.

Coverage:
- Verb fails loudly when catalog is not initialized or events.jsonl is empty.
- ``--dry-run`` reports counts without calling ChromaDB.update.
- Default behavior writes doc_id metadata into T3 chunks.
- Idempotency: re-running on already-backfilled chunks is a no-op.
- ``--collection`` filter scopes the backfill.
- Orphan chunks (synthesized_orphan=True) are skipped and counted.
- ``--json`` emits a structured report.
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
    """Reset the EphemeralClient at fixture build."""
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
    """Skip Catalog construction; just write events.jsonl directly."""
    catalog_dir.mkdir(parents=True, exist_ok=True)
    Catalog.init(catalog_dir)
    log = EventLog(catalog_dir)
    log.append_many(events)


def _make_chunk_event(
    chunk_id: str, doc_id: str, coll_id: str,
    *, synthesized_orphan: bool = False, chash: str = "h",
) -> ev.Event:
    return ev.Event(
        type=ev.TYPE_CHUNK_INDEXED, v=0,
        payload=ev.ChunkIndexedPayload(
            chunk_id=chunk_id, chash=chash, doc_id=doc_id,
            coll_id=coll_id, position=0,
            synthesized_orphan=synthesized_orphan,
        ),
        ts="2026-04-30T00:00:00Z",
    )


# ── Usage ────────────────────────────────────────────────────────────────


class TestUsage:
    def test_missing_catalog(self, isolated_nexus, runner):
        result = runner.invoke(t3_backfill_doc_id_cmd, [])
        assert result.exit_code != 0
        assert "not initialized" in result.output.lower()

    def test_empty_event_log(self, isolated_nexus, runner):
        Catalog.init(isolated_nexus)
        # events.jsonl exists but is empty after Catalog.init.
        result = runner.invoke(t3_backfill_doc_id_cmd, [])
        assert result.exit_code != 0
        assert "empty" in result.output.lower()


# ── Dry-run + filter ─────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_reports_without_writing(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events = [
            _make_chunk_event("ch1", "uuid7-A", "code__test"),
            _make_chunk_event("ch2", "uuid7-A", "code__test"),
        ]
        _seed_event_log(isolated_nexus, events)
        _seed(chroma_client, "code__test", [
            {"id": "ch1", "content": "x", "metadata": {"chunk_text_hash": "h1"}},
            {"id": "ch2", "content": "y", "metadata": {"chunk_text_hash": "h2"}},
        ])

        result = runner.invoke(
            t3_backfill_doc_id_cmd, ["--dry-run", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["dry_run"] is True
        assert payload["chunks_eligible"] == 2
        assert payload["chunks_updated"] == 2
        # No actual update, no doc_id in metadata yet.
        col = chroma_client.get_collection("code__test")
        meta = col.get(ids=["ch1"], include=["metadatas"])["metadatas"][0]
        assert "doc_id" not in meta


# ── Happy path: writes doc_id into T3 ────────────────────────────────────


class TestBackfillUpdates:
    def test_writes_doc_id_into_chunks(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events = [
            _make_chunk_event("ch1", "uuid7-A", "code__test"),
            _make_chunk_event("ch2", "uuid7-B", "code__test"),
        ]
        _seed_event_log(isolated_nexus, events)
        _seed(chroma_client, "code__test", [
            {"id": "ch1", "content": "x", "metadata": {"chunk_text_hash": "h1"}},
            {"id": "ch2", "content": "y", "metadata": {"chunk_text_hash": "h2"}},
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        result = runner.invoke(t3_backfill_doc_id_cmd, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["chunks_updated"] == 2
        assert payload["chunks_already_correct"] == 0

        col = chroma_client.get_collection("code__test")
        for cid, expected in [("ch1", "uuid7-A"), ("ch2", "uuid7-B")]:
            meta = col.get(ids=[cid], include=["metadatas"])["metadatas"][0]
            assert meta["doc_id"] == expected
            # Existing keys preserved.
            assert "chunk_text_hash" in meta

    def test_idempotent_second_run_is_noop(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events = [_make_chunk_event("ch1", "uuid7-A", "code__test")]
        _seed_event_log(isolated_nexus, events)
        _seed(chroma_client, "code__test", [
            {"id": "ch1", "content": "x", "metadata": {"chunk_text_hash": "h1"}},
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        first = runner.invoke(t3_backfill_doc_id_cmd, ["--json"])
        assert first.exit_code == 0
        first_payload = json.loads(first.output)
        assert first_payload["chunks_updated"] == 1

        second = runner.invoke(t3_backfill_doc_id_cmd, ["--json"])
        assert second.exit_code == 0
        second_payload = json.loads(second.output)
        assert second_payload["chunks_updated"] == 0
        assert second_payload["chunks_already_correct"] == 1


# ── Filter + orphan handling ─────────────────────────────────────────────


class TestCollectionFilter:
    def test_filter_scopes_backfill(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events = [
            _make_chunk_event("ch1", "uuid7-A", "code__a"),
            _make_chunk_event("ch2", "uuid7-B", "code__b"),
        ]
        _seed_event_log(isolated_nexus, events)
        _seed(chroma_client, "code__a", [
            {"id": "ch1", "content": "x", "metadata": {"chunk_text_hash": "h1"}},
        ])
        _seed(chroma_client, "code__b", [
            {"id": "ch2", "content": "y", "metadata": {"chunk_text_hash": "h2"}},
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        result = runner.invoke(
            t3_backfill_doc_id_cmd,
            ["--collection", "code__a", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["collections_processed"] == 1
        assert payload["chunks_updated"] == 1

        # code__b chunk untouched.
        col_b = chroma_client.get_collection("code__b")
        meta = col_b.get(ids=["ch2"], include=["metadatas"])["metadatas"][0]
        assert "doc_id" not in meta


class TestOrphanSkip:
    def test_orphans_not_updated(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events = [
            _make_chunk_event("ch1", "uuid7-A", "code__test"),
            _make_chunk_event(
                "orphan", "", "code__test",
                synthesized_orphan=True,
            ),
        ]
        _seed_event_log(isolated_nexus, events)
        _seed(chroma_client, "code__test", [
            {"id": "ch1", "content": "x", "metadata": {"chunk_text_hash": "h1"}},
            {"id": "orphan", "content": "y", "metadata": {"chunk_text_hash": "h2"}},
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        result = runner.invoke(t3_backfill_doc_id_cmd, ["--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["chunks_updated"] == 1
        assert payload["orphans_skipped"] == 1

        col = chroma_client.get_collection("code__test")
        # Orphan chunk has no doc_id metadata.
        orphan_meta = col.get(ids=["orphan"], include=["metadatas"])["metadatas"][0]
        assert "doc_id" not in orphan_meta
        # Non-orphan does.
        other_meta = col.get(ids=["ch1"], include=["metadatas"])["metadatas"][0]
        assert other_meta["doc_id"] == "uuid7-A"
