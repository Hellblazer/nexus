# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the surviving Chroma READ client (RDR-155 P4a.2).

P4a.G test-validator gap closure (Medium): ``migration/chroma_read.py`` is
the Phase-5 ETL's read substrate — the locked P4a.1 contract only asserts
the module exists and names both constructors; these tests pin its
behaviour. Hermetic: EphemeralClient for pagination, tmp paths for the
fail-loud legs, no network.
"""
from __future__ import annotations

from pathlib import Path

import chromadb
import pytest

from nexus.db.chroma_quotas import QUOTAS
from nexus.migration.chroma_read import (
    iter_collection_chunks,
    list_collection_names,
    open_cloud_read_client,
    open_local_read_client,
)


class TestOpenLocalReadClient:
    def test_missing_store_fails_loud(self, tmp_path: Path) -> None:
        """A non-existent local store is FileNotFoundError, never a silent
        empty store (no-silent-fallback rule)."""
        with pytest.raises(FileNotFoundError, match="nothing to migrate"):
            open_local_read_client(tmp_path / "does_not_exist")


class TestOpenCloudReadClient:
    def test_half_configured_cloud_fails_loud(self, monkeypatch) -> None:
        """Missing database/api_key refuses — a half-configured cloud read
        would silently migrate nothing."""
        monkeypatch.setattr(
            "nexus.config.get_credential", lambda key: "", raising=True,
        )
        with pytest.raises(RuntimeError, match="half-configured"):
            open_cloud_read_client()


class TestIterCollectionChunks:
    @pytest.fixture()
    def client(self):
        c = chromadb.EphemeralClient()
        # EphemeralClient shares one in-process backend — clear leftovers.
        for col in c.list_collections():
            c.delete_collection(col.name)
        return c

    def _seed(self, client, name: str, n: int) -> None:
        col = client.get_or_create_collection(name)
        col.add(
            ids=[f"id-{i:04d}" for i in range(n)],
            documents=[f"chunk text {i}" for i in range(n)],
            metadatas=[{"position": i} for i in range(n)],
            embeddings=[[float(i), 1.0, 0.0] for i in range(n)],
        )

    def test_yields_every_chunk_across_page_boundaries(self, client) -> None:
        # 7 chunks with page_size=3 → pages of 3+3+1: exercises full-page
        # continuation AND partial-page exit.
        self._seed(client, "knowledge__etl_page_test", 7)
        rows = list(
            iter_collection_chunks(client, "knowledge__etl_page_test", page_size=3)
        )
        assert len(rows) == 7
        assert sorted(r["id"] for r in rows) == [f"id-{i:04d}" for i in range(7)]
        by_id = {r["id"]: r for r in rows}
        assert by_id["id-0003"]["document"] == "chunk text 3"
        assert by_id["id-0003"]["metadata"] == {"position": 3}

    def test_exact_page_multiple_terminates(self, client) -> None:
        # 6 chunks with page_size=3 → the loop must stop after the second
        # page (empty third page), not spin.
        self._seed(client, "knowledge__etl_exact_test", 6)
        rows = list(
            iter_collection_chunks(client, "knowledge__etl_exact_test", page_size=3)
        )
        assert len(rows) == 6

    def test_empty_collection_yields_nothing(self, client) -> None:
        client.get_or_create_collection("knowledge__etl_empty_test")
        assert list(iter_collection_chunks(client, "knowledge__etl_empty_test")) == []

    def test_embeddings_deliberately_not_fetched(self, client) -> None:
        """The read leg transfers TEXT, not vectors — the pgvector side
        re-embeds server-side (RDR-109 cross-model-contamination guard;
        decision recorded in the module docstring and on nexus-unp61)."""
        self._seed(client, "knowledge__etl_noemb_test", 2)
        rows = list(iter_collection_chunks(client, "knowledge__etl_noemb_test"))
        assert rows and all("embedding" not in r for r in rows)
        assert all(set(r) == {"id", "document", "metadata"} for r in rows)

    def test_page_size_over_quota_fails_loud(self, client) -> None:
        """chroma_quotas still governs this leg — the ONE reason it
        survives Phase 4a."""
        self._seed(client, "knowledge__etl_quota_test", 1)
        with pytest.raises(ValueError, match="chroma_quotas governs"):
            list(
                iter_collection_chunks(
                    client,
                    "knowledge__etl_quota_test",
                    page_size=QUOTAS.MAX_QUERY_RESULTS + 1,
                )
            )


class TestListCollectionNames:
    def test_sorted_names(self) -> None:
        client = chromadb.EphemeralClient()
        for col in client.list_collections():
            client.delete_collection(col.name)
        client.get_or_create_collection("knowledge__etl_names_b")
        client.get_or_create_collection("knowledge__etl_names_a")
        names = list_collection_names(client)
        assert names == sorted(names)
        assert {"knowledge__etl_names_a", "knowledge__etl_names_b"} <= set(names)
