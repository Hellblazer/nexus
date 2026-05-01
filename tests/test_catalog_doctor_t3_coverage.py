# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the RDR-101 Phase 2 PR δ ``--t3-doc-id-coverage`` doctor flag.

Coverage:
- Verb fails with usage error when no flag is passed.
- ``--t3-doc-id-coverage`` PASSes when every chunk carries the right doc_id.
- FAILs when chunks lack doc_id metadata.
- FAILs when chunks carry the wrong doc_id.
- FAILs when the event log claims chunks T3 doesn't have.
- Orphan chunks (synthesized_orphan=True) without doc_id do not fail.
- ``--json`` payload contains per-collection counts.
- Combined with ``--replay-equality``: both checks run, JSON has both keys.
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
from nexus.commands.catalog import doctor_cmd


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


def _seed_log(catalog_dir: Path, events: list[ev.Event]) -> None:
    catalog_dir.mkdir(parents=True, exist_ok=True)
    Catalog.init(catalog_dir)
    log = EventLog(catalog_dir)
    log.append_many(events)


def _chunk(
    chunk_id: str, doc_id: str, coll_id: str,
    *, orphan: bool = False,
) -> ev.Event:
    return ev.Event(
        type=ev.TYPE_CHUNK_INDEXED, v=0,
        payload=ev.ChunkIndexedPayload(
            chunk_id=chunk_id, chash="h", doc_id=doc_id,
            coll_id=coll_id, position=0,
            synthesized_orphan=orphan,
        ),
        ts="2026-04-30T00:00:00Z",
    )


# ── Usage ────────────────────────────────────────────────────────────────


class TestUsage:
    def test_no_flag_is_usage_error(self, isolated_nexus, runner):
        result = runner.invoke(doctor_cmd, [])
        assert result.exit_code != 0
        assert "Pass a check flag" in (result.output + (result.stderr or ""))


# ── Pass paths ───────────────────────────────────────────────────────────


class TestCoveragePasses:
    def test_full_coverage_passes(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events = [_chunk("ch1", "uuid7-A", "code__test")]
        _seed_log(isolated_nexus, events)
        _seed(chroma_client, "code__test", [
            {
                "id": "ch1", "content": "x",
                "metadata": {"doc_id": "uuid7-A"},
            },
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--t3-doc-id-coverage", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)["t3_doc_id_coverage"]
        assert payload["pass"] is True
        assert payload["tables"]["code__test"]["coverage"] == 1.0

    def test_orphan_without_doc_id_does_not_fail(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events = [
            _chunk("orphan", "", "code__test", orphan=True),
            _chunk("ch1", "uuid7-A", "code__test"),
        ]
        _seed_log(isolated_nexus, events)
        _seed(chroma_client, "code__test", [
            {"id": "orphan", "content": "x", "metadata": {"_": "_"}},
            {"id": "ch1", "content": "y", "metadata": {"doc_id": "uuid7-A"}},
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--t3-doc-id-coverage", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)["t3_doc_id_coverage"]
        assert payload["pass"] is True
        coll = payload["tables"]["code__test"]
        assert coll["expected_orphans"] == 1
        assert coll["with_doc_id"] == 1
        assert coll["total_chunks"] == 2


# ── Fail paths ───────────────────────────────────────────────────────────


class TestCoverageFails:
    def test_missing_doc_id_fails(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events = [_chunk("ch1", "uuid7-A", "code__test")]
        _seed_log(isolated_nexus, events)
        _seed(chroma_client, "code__test", [
            {"id": "ch1", "content": "x", "metadata": {"_": "_"}},  # no doc_id
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--t3-doc-id-coverage", "--json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)["t3_doc_id_coverage"]
        assert payload["pass"] is False
        coll = payload["tables"]["code__test"]
        assert "ch1" in coll["missing_doc_id_sample"]

    def test_mismatched_doc_id_fails(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events = [_chunk("ch1", "uuid7-A", "code__test")]
        _seed_log(isolated_nexus, events)
        _seed(chroma_client, "code__test", [
            {
                "id": "ch1", "content": "x",
                "metadata": {"doc_id": "uuid7-WRONG"},
            },
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--t3-doc-id-coverage", "--json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)["t3_doc_id_coverage"]
        coll = payload["tables"]["code__test"]
        assert coll["mismatched_doc_id_count"] == 1
        m = coll["mismatched_doc_id_sample"][0]
        assert m["actual"] == "uuid7-WRONG"
        assert m["expected"] == "uuid7-A"

    def test_chunk_in_log_but_not_in_t3_fails(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events = [_chunk("missing-from-t3", "uuid7-A", "code__test")]
        _seed_log(isolated_nexus, events)
        _seed(chroma_client, "code__test", [
            # Some other chunk; not the one the log references.
            {
                "id": "different-chunk", "content": "x",
                "metadata": {"doc_id": "uuid7-A"},
            },
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--t3-doc-id-coverage", "--json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)["t3_doc_id_coverage"]
        coll = payload["tables"]["code__test"]
        assert coll["not_in_t3_count"] == 1
        assert "missing-from-t3" in coll["not_in_t3_sample"]


# ── Combined check ───────────────────────────────────────────────────────


class TestCombined:
    def test_both_flags_run_both_checks(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Build a real catalog so --replay-equality can run.
        Catalog.init(isolated_nexus)
        cat = Catalog(isolated_nexus, isolated_nexus / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(owner, "doc.md", content_type="prose", file_path="doc.md")
        cat._db.close()

        # Add a ChunkIndexed event so coverage check has something to check.
        log = EventLog(isolated_nexus)
        log.append_many([_chunk("ch1", "uuid7-A", "code__test")])
        _seed(chroma_client, "code__test", [
            {
                "id": "ch1", "content": "x",
                "metadata": {"doc_id": "uuid7-A"},
            },
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd,
            ["--replay-equality", "--t3-doc-id-coverage", "--json"],
        )
        # Replay equality may or may not pass depending on whether the
        # synthesized log matches the live SQLite — what we care about
        # here is that both checks ran and got a payload.
        payload = json.loads(result.output)
        assert "replay_equality" in payload
        assert "t3_doc_id_coverage" in payload
        assert payload["t3_doc_id_coverage"]["pass"] is True
