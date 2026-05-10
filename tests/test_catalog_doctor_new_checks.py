# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6dan: 3 new catalog doctor checks.

- ``--chunk-size-distribution``: per-collection p50/p95/p99/max +
  micro-chunk WARN + over-quota FAIL.
- ``--chunk-text-dedup``: within-collection dupe ratio WARN +
  cross-collection dupe count WARN.
- ``--t3-vs-catalog``: T3 orphan collections + zombie collections +
  catalog docs pointing at missing T3.

Tests use ``chromadb.EphemeralClient`` per the project's
integration-over-mocks rule and seed real chunks/docs to exercise
each pass / WARN / FAIL boundary.
"""
from __future__ import annotations

import json
from pathlib import Path

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.commands.catalog import (
    _MICRO_CHUNK_BYTES,
    _percentile,
    doctor_cmd,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def isolated_nexus(tmp_path: Path) -> Path:
    return tmp_path / "test-catalog"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def chroma_client():
    """Fresh EphemeralClient with collections cleared (chromadb's
    in-memory backend is process-shared per project memory).
    """
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
        metadatas=[c.get("metadata", {"_": "_"}) for c in chunks],
    )


# ── _percentile helper ─────────────────────────────────────────────────────


class TestPercentile:
    def test_empty_returns_zero(self) -> None:
        assert _percentile([], 0.5) == 0

    def test_single_value(self) -> None:
        assert _percentile([42], 0.5) == 42

    def test_p50_of_three(self) -> None:
        assert _percentile([1, 2, 3], 0.5) == 2

    def test_p99_returns_near_max(self) -> None:
        # 100 values 1..100; p99 should be close to 99.
        result = _percentile(list(range(1, 101)), 0.99)
        assert 98 <= result <= 100


# ── --chunk-size-distribution ────────────────────────────────────────────


class TestChunkSizeDistribution:
    def test_pass_when_all_chunks_in_range(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """All chunks well above micro-floor and well below over-
        quota → PASS, no WARN, no FAIL.
        """
        text = "x" * 500  # 500 bytes; comfortably mid-range
        _seed(chroma_client, "code__ok", [
            {"id": f"c{i}", "content": text} for i in range(10)
        ])

        class _FakeT3:
            _client = chroma_client

            def list_collections(self):
                return [{"name": "code__ok"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--chunk-size-distribution", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)["chunk_size_distribution"]
        assert payload["pass"] is True
        t = payload["tables"]["code__ok"]
        assert t["total_chunks"] == 10
        assert t["over_quota_count"] == 0
        assert t["micro_count"] == 0

    def test_micro_chunks_warn_above_5_percent(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """When > 5% of chunks are < 100 bytes the per-collection
        ``warn`` flag flips True. Lock the boundary so a future
        threshold shift surfaces in tests.
        """
        # 10 chunks, 2 micros = 20% → above the 5% warn threshold.
        chunks = [
            {"id": f"c{i}", "content": "x" * 50} for i in range(2)
        ] + [
            {"id": f"c{i + 2}", "content": "y" * 500} for i in range(8)
        ]
        _seed(chroma_client, "code__micros", chunks)

        class _FakeT3:
            _client = chroma_client

            def list_collections(self):
                return [{"name": "code__micros"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--chunk-size-distribution", "--json"],
        )
        # Micros are WARN, not FAIL.
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)["chunk_size_distribution"]
        t = payload["tables"]["code__micros"]
        assert t["micro_count"] == 2
        assert t["warn"] is True
        assert payload["pass"] is True  # WARN doesn't break overall

    def test_over_quota_chunk_fails(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A chunk over MAX_DOCUMENT_BYTES is a hard FAIL — Voyage
        rejects these, so they must surface as a release blocker.
        """
        from nexus.db.chroma_quotas import QUOTAS
        big = "x" * (QUOTAS.MAX_DOCUMENT_BYTES + 100)
        _seed(chroma_client, "code__big", [
            {"id": "c1", "content": "tiny"},
            {"id": "c2", "content": big},
        ])

        class _FakeT3:
            _client = chroma_client

            def list_collections(self):
                return [{"name": "code__big"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--chunk-size-distribution", "--json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)["chunk_size_distribution"]
        assert payload["pass"] is False
        t = payload["tables"]["code__big"]
        assert t["over_quota_count"] == 1
        assert t["pass"] is False

    def test_taxonomy_collection_skipped(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """``taxonomy__*`` collections carry centroid embeddings,
        not chunked text; they must be skipped from the size audit.
        """
        _seed(chroma_client, "taxonomy__centroids", [
            {"id": "topic-1", "content": "x"},
        ])

        class _FakeT3:
            _client = chroma_client

            def list_collections(self):
                return [{"name": "taxonomy__centroids"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--chunk-size-distribution", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)["chunk_size_distribution"]
        assert "taxonomy__centroids" not in payload["tables"]


# ── --chunk-text-dedup ────────────────────────────────────────────────────


class TestChunkTextDedup:
    def test_no_dupes_passes(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _seed(chroma_client, "code__cleanup", [
            {"id": "c1", "content": "a", "metadata": {"chunk_text_hash": "h1"}},
            {"id": "c2", "content": "b", "metadata": {"chunk_text_hash": "h2"}},
        ])

        class _FakeT3:
            _client = chroma_client

            def list_collections(self):
                return [{"name": "code__cleanup"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--chunk-text-dedup", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)["chunk_text_dedup"]
        assert payload["pass"] is True
        assert payload["within"]["code__cleanup"]["dupe_chunks"] == 0
        assert payload["cross_dupe_chunk_count"] == 0

    def test_within_collection_high_dupe_ratio_warns(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """When > 5% of chunks share a chunk_text_hash within one
        collection, the per-collection ``warn`` flag fires. The
        scenario simulates a chunker bug producing duplicate chunks
        from distinct positions.
        """
        # 4 chunks, 2 share a hash → 50% dupe ratio.
        _seed(chroma_client, "code__bug", [
            {"id": "c1", "content": "a", "metadata": {"chunk_text_hash": "h1"}},
            {"id": "c2", "content": "b", "metadata": {"chunk_text_hash": "h1"}},
            {"id": "c3", "content": "c", "metadata": {"chunk_text_hash": "h3"}},
            {"id": "c4", "content": "d", "metadata": {"chunk_text_hash": "h4"}},
        ])

        class _FakeT3:
            _client = chroma_client

            def list_collections(self):
                return [{"name": "code__bug"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--chunk-text-dedup", "--json"],
        )
        # WARN, not FAIL; overall pass is True unless an exception fires.
        assert result.exit_code == 0
        payload = json.loads(result.output)["chunk_text_dedup"]
        t = payload["within"]["code__bug"]
        assert t["dupe_chunks"] == 2
        assert t["warn"] is True

    def test_cross_collection_dupes_surfaced(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A chash appearing in 2+ collections is a cross-ingest
        signal (fixture re-import, multi-corpus leak). Lock that
        the report includes the chash + the involved collections.
        """
        _seed(chroma_client, "code__a", [
            {"id": "c1", "content": "x", "metadata": {"chunk_text_hash": "shared-1234"}},
        ])
        _seed(chroma_client, "code__b", [
            {"id": "c2", "content": "x", "metadata": {"chunk_text_hash": "shared-1234"}},
        ])

        class _FakeT3:
            _client = chroma_client

            def list_collections(self):
                return [{"name": "code__a"}, {"name": "code__b"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--chunk-text-dedup", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)["chunk_text_dedup"]
        assert payload["cross_dupe_chunk_count"] == 1
        sample = payload["cross_sample"][0]
        assert sample["chash"].startswith("shared-1234")
        assert sample["collections"] == ["code__a", "code__b"]


# ── --t3-vs-catalog ───────────────────────────────────────────────────────


class TestT3VsCatalog:
    def test_clean_state_passes(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """T3 collection has chunks AND catalog has docs referencing
        it. No drift in either direction.
        """
        from nexus.catalog.catalog import Catalog
        Catalog.init(isolated_nexus)
        cat = Catalog(isolated_nexus, isolated_nexus / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(
            owner, "doc.md", content_type="prose",
            file_path="doc.md", physical_collection="docs__clean",
        )
        cat._db.close()

        _seed(chroma_client, "docs__clean", [
            {"id": "c1", "content": "x"},
        ])

        class _FakeT3:
            _client = chroma_client

            def list_collections(self):
                return [{"name": "docs__clean"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--t3-vs-catalog", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)["t3_vs_catalog"]
        assert payload["pass"] is True
        assert payload["t3_orphans"] == []
        assert payload["zombies"] == []
        assert payload["docs_pointing_at_missing_t3"] == []

    def test_t3_orphan_detected(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A T3 collection with chunks but zero catalog docs is an
        orphan; report it and FAIL.
        """
        from nexus.catalog.catalog import Catalog
        Catalog.init(isolated_nexus)

        _seed(chroma_client, "code__orphan", [
            {"id": "c1", "content": "x"},
        ])

        class _FakeT3:
            _client = chroma_client

            def list_collections(self):
                return [{"name": "code__orphan"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--t3-vs-catalog", "--json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)["t3_vs_catalog"]
        assert payload["pass"] is False
        assert len(payload["t3_orphans"]) == 1
        assert payload["t3_orphans"][0]["name"] == "code__orphan"
        assert payload["t3_orphans"][0]["chunk_count"] == 1

    def test_doc_pointing_at_missing_t3_detected(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A catalog document whose ``physical_collection`` is gone
        from T3 (operator deleted it without going through
        supersede) lands in ``docs_pointing_at_missing_t3``.
        """
        from nexus.catalog.catalog import Catalog
        Catalog.init(isolated_nexus)
        cat = Catalog(isolated_nexus, isolated_nexus / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(
            owner, "ghost.md", content_type="prose",
            file_path="ghost.md",
            physical_collection="docs__ghost_t3",
        )
        cat._db.close()

        class _FakeT3:
            _client = chroma_client

            def list_collections(self):
                return []  # no T3 collections at all

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--t3-vs-catalog", "--json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)["t3_vs_catalog"]
        assert payload["pass"] is False
        missing = payload["docs_pointing_at_missing_t3"]
        assert len(missing) == 1
        assert missing[0]["physical_collection"] == "docs__ghost_t3"


# ── Usage ─────────────────────────────────────────────────────────────────


class TestUsage:
    def test_no_flag_lists_all_six_checks(self, runner) -> None:
        """The usage error must enumerate every flag so an operator
        running ``nx catalog doctor`` blind sees every option, not
        just the original three.
        """
        result = runner.invoke(doctor_cmd, [])
        assert result.exit_code != 0
        out = result.output + (result.stderr or "")
        for flag in (
            "--replay-equality", "--t3-doc-id-coverage",
            "--collections-drift", "--chunk-size-distribution",
            "--chunk-text-dedup", "--t3-vs-catalog",
        ):
            assert flag in out, f"usage error must list {flag}"
