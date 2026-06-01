# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for local-mode client-side embedding fixes.

ROOT CAUSE: In LOCAL mode the T3 daemon (chroma run via HttpClient) has no
registration for nexus's embedding function.  Any op that passes query_texts
or upserts without embeddings triggers the server's DefaultEmbeddingFunction
(384-dim), while collections are 768-dim (bge) or 384-dim (minilm) -- the
mismatch produces InvalidArgumentError that is SILENTLY SKIPPED.

Four fixes validated here:
  Fix 1 -- search: local mode must pass query_embeddings, NOT query_texts.
  Fix 2 -- write: local mode must pass embeddings= on upsert, NOT documents-only.
  Fix 3 -- defense-in-depth: all-collections-skipped fires ERROR, not just warnings.
  Fix 4 -- display: conformant 4-segment names show parsed model token.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, call, patch

import chromadb
from chromadb.errors import InvalidArgumentError

from nexus.db.t3 import T3Database


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_fake_ef(dim: int = 384) -> MagicMock:
    """Return a callable mock that returns dim-length float vectors."""
    ef = MagicMock(name="fake_ef")
    ef.return_value = [[float(i) / dim for i in range(dim)]]
    return ef


def _spy_collection(count: int = 1) -> MagicMock:
    """Return a mock ChromaDB collection that records .query() / .upsert() kwargs."""
    col = MagicMock(name="spy_col")
    col.count.return_value = count
    # query returns the minimal structure the search loop expects
    col.query.return_value = {
        "ids": [[]],
        "documents": [[]],
        "metadatas": [[]],
        "distances": [[]],
    }
    return col


def _local_db_with_spy_col(fake_ef: MagicMock, spy_col: MagicMock) -> T3Database:
    """Build a local-mode T3Database whose collection operations hit spy_col."""
    mock_client = MagicMock(name="chroma_client")
    mock_client.get_collection.return_value = spy_col
    mock_client.get_or_create_collection.return_value = spy_col
    mock_client.count = MagicMock(return_value=spy_col.count.return_value)
    db = T3Database(
        _client=mock_client,
        _ef_override=fake_ef,
        local_mode=True,
    )
    return db


# ─────────────────────────────────────────────────────────────────────────────
# Fix 1: search -- local mode must use query_embeddings, NOT query_texts
# ─────────────────────────────────────────────────────────────────────────────


class TestFix1SearchClientSideEmbed:
    """GUARD test (load-bearing): local-mode search must pass query_embeddings."""

    def test_local_mode_search_passes_query_embeddings_not_query_texts(self) -> None:
        """Fix 1 GUARD: query_embeddings present, query_texts absent in local mode."""
        fake_ef = _make_fake_ef(dim=384)
        spy_col = _spy_collection(count=1)
        db = _local_db_with_spy_col(fake_ef, spy_col)

        db.search("hello world", collection_names=["knowledge__test__minilm-l6-v2-384__v1"])

        spy_col.query.assert_called_once()
        kwargs = spy_col.query.call_args.kwargs
        assert "query_embeddings" in kwargs, (
            "Fix 1 MISSING: local-mode search must pass query_embeddings to avoid "
            "server-side 384-dim DefaultEmbeddingFunction mismatch"
        )
        assert "query_texts" not in kwargs, (
            "Fix 1 REGRESSION: local-mode search must NOT pass query_texts"
        )

    def test_local_mode_search_embedding_fn_is_invoked(self) -> None:
        """Fix 1: the EF is actually called during search in local mode."""
        fake_ef = _make_fake_ef(dim=768)
        spy_col = _spy_collection(count=1)
        db = _local_db_with_spy_col(fake_ef, spy_col)

        db.search("test query", collection_names=["code__owner__bge-base-en-v15-768__v1"])

        fake_ef.assert_called()

    def test_local_mode_search_embedding_vector_sent(self) -> None:
        """Fix 1: the embedding vector from the EF is the one sent in query_embeddings."""
        expected_vec = [0.1, 0.2, 0.3]
        fake_ef = MagicMock(name="ef")
        fake_ef.return_value = [expected_vec]
        spy_col = _spy_collection(count=1)
        db = _local_db_with_spy_col(fake_ef, spy_col)

        db.search("test", collection_names=["knowledge__owner__minilm-l6-v2-384__v1"])

        kwargs = spy_col.query.call_args.kwargs
        assert kwargs["query_embeddings"] == [expected_vec]

    def test_cloud_mode_search_still_passes_query_texts(self) -> None:
        """Fix 1 inverse: cloud mode (non-CCE) must still pass query_texts."""
        mock_client = MagicMock(name="cloud_client")
        spy_col = _spy_collection(count=1)
        mock_client.get_collection.return_value = spy_col
        mock_ef = MagicMock(name="cloud_ef")

        # Cloud mode: local_mode=False (default), no voyage key so CCE is off
        db = T3Database(_client=mock_client, _ef_override=mock_ef)
        assert db._local_mode is False

        db.search("hello", collection_names=["code__owner__voyage-code-3__v1"])

        kwargs = spy_col.query.call_args.kwargs
        assert "query_texts" in kwargs, (
            "Cloud-mode (non-CCE) search must still pass query_texts"
        )
        assert "query_embeddings" not in kwargs, (
            "Cloud-mode (non-CCE) search must NOT pass query_embeddings"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Fix 2: write -- local mode upsert must pass embeddings=, NOT documents-only
# ─────────────────────────────────────────────────────────────────────────────


class TestFix2WriteClientSideEmbed:
    """GUARD test: local-mode upsert must pass embeddings= to avoid server-side mismatch."""

    def test_local_mode_upsert_chunks_passes_embeddings(self) -> None:
        """Fix 2 GUARD: upsert_chunks in local mode must supply embeddings."""
        fake_ef = _make_fake_ef(dim=384)
        # ef is called with a list of texts, returns list of vecs
        fake_ef.side_effect = lambda texts: [[float(i) / 384 for i in range(384)] for _ in texts]
        spy_col = _spy_collection()
        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = spy_col

        db = T3Database(_client=mock_client, _ef_override=fake_ef, local_mode=True)

        db.upsert_chunks(
            "knowledge__test__minilm-l6-v2-384__v1",
            ids=["id1"],
            documents=["hello world"],
            metadatas=[{"source_path": "test.txt", "content_type": "knowledge"}],
        )

        spy_col.upsert.assert_called()
        upsert_kwargs = spy_col.upsert.call_args.kwargs
        assert "embeddings" in upsert_kwargs, (
            "Fix 2 MISSING: local-mode upsert_chunks must pass embeddings= to avoid "
            "server-side 384-dim DefaultEmbeddingFunction mismatch"
        )
        assert upsert_kwargs["embeddings"] is not None
        assert len(upsert_kwargs["embeddings"]) == 1

    def test_local_mode_ef_called_during_upsert_chunks(self) -> None:
        """Fix 2: the EF is invoked for the documents during upsert_chunks."""
        fake_ef = MagicMock(name="ef")
        fake_ef.side_effect = lambda texts: [[0.0] * 384 for _ in texts]
        spy_col = _spy_collection()
        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = spy_col

        db = T3Database(_client=mock_client, _ef_override=fake_ef, local_mode=True)
        db.upsert_chunks(
            "knowledge__test__minilm-l6-v2-384__v1",
            ids=["a"],
            documents=["doc"],
            metadatas=[{"content_type": "knowledge"}],
        )

        fake_ef.assert_called()

    def test_cloud_mode_upsert_chunks_does_not_pass_embeddings(self) -> None:
        """Fix 2 inverse: cloud mode upsert must NOT pre-compute embeddings (server does it)."""
        mock_ef = MagicMock(name="cloud_ef")
        spy_col = _spy_collection()
        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = spy_col

        db = T3Database(_client=mock_client, _ef_override=mock_ef)
        assert db._local_mode is False

        db.upsert_chunks(
            "code__owner__voyage-code-3__v1",
            ids=["x"],
            documents=["cloud doc"],
            metadatas=[{"content_type": "code"}],
        )

        spy_col.upsert.assert_called()
        upsert_kwargs = spy_col.upsert.call_args.kwargs
        # Cloud mode: embeddings key should be absent (server-side embedding)
        assert "embeddings" not in upsert_kwargs, (
            "Cloud mode upsert_chunks must NOT pass embeddings= (server embeds)"
        )
        # Mock EF should NOT be invoked for document embedding in cloud mode
        mock_ef.assert_not_called()

    def test_local_mode_upsert_respects_batch_size(self) -> None:
        """Fix 2: batching still works -- >300 docs must be split across multiple upsert calls."""
        n = 310
        fake_ef = MagicMock(name="ef")
        fake_ef.side_effect = lambda texts: [[0.0] * 3 for _ in texts]
        spy_col = _spy_collection()
        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = spy_col

        db = T3Database(_client=mock_client, _ef_override=fake_ef, local_mode=True)
        db.upsert_chunks(
            "knowledge__test__minilm-l6-v2-384__v1",
            ids=[f"id{i}" for i in range(n)],
            documents=[f"doc {i}" for i in range(n)],
            metadatas=[{"content_type": "knowledge"} for _ in range(n)],
        )

        # Should be called twice: 300 + 10
        assert spy_col.upsert.call_count == 2

    def test_local_mode_put_passes_embeddings_via_chokepoint(self) -> None:
        """Fix 2 GUARD (put path): put() in local mode must supply embeddings= on upsert.

        put() -> _write_batch (no-embeddings branch) -> _maybe_client_embed -> upsert(embeddings=).
        This test pins the transitive guard so a future refactor that breaks the
        _write_batch chokepoint routing is caught directly rather than only via
        integration tests.
        """
        fake_ef = MagicMock(name="ef")
        expected_vec = [0.1, 0.2, 0.3]
        fake_ef.side_effect = lambda texts: [expected_vec for _ in texts]
        spy_col = _spy_collection()
        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = spy_col

        db = T3Database(_client=mock_client, _ef_override=fake_ef, local_mode=True)
        # put() is the MCP single-document write path (non-CCE branch in local mode)
        db.put(
            collection="code__owner__bge-base-en-v15-768__v1",
            content="local mode put test content",
            title="put-guard-test",
        )

        spy_col.upsert.assert_called()
        upsert_kwargs = spy_col.upsert.call_args.kwargs
        assert "embeddings" in upsert_kwargs, (
            "Fix 2 GUARD (put path): local-mode put() must pass embeddings= via "
            "_write_batch chokepoint to avoid server-side dimension mismatch"
        )
        assert upsert_kwargs["embeddings"] is not None
        assert len(upsert_kwargs["embeddings"]) == 1
        assert upsert_kwargs["embeddings"][0] == expected_vec


# ─────────────────────────────────────────────────────────────────────────────
# Fix 3: defense-in-depth -- all-skipped error event
# ─────────────────────────────────────────────────────────────────────────────


class TestFix3AllSkippedErrorEvent:
    """Fix 3: when ALL queried collections are dimension-skipped, fire ERROR."""

    def _db_with_dim_error_cols(self, collection_names: list[str]) -> tuple[T3Database, MagicMock]:
        """Return a cloud-mode DB and a spy client where every collection raises dim error."""
        mock_client = MagicMock()

        def _get_collection_raising(name, **kwargs):
            col = MagicMock()
            col.count.return_value = 5
            col.query.side_effect = InvalidArgumentError(
                "dimension mismatch: expected 768 got 384"
            )
            return col

        mock_client.get_collection.side_effect = _get_collection_raising
        db = T3Database(_client=mock_client, _ef_override=MagicMock())
        return db, mock_client

    def test_all_collections_dimension_skipped_fires_error(self, caplog) -> None:
        """Fix 3: if all collections are dimension-skipped, an ERROR-level distinct event fires."""
        import logging
        db, _ = self._db_with_dim_error_cols(["col1", "col2"])

        with patch.object(db, "_local_mode", False):
            with patch("nexus.db.t3._log") as mock_log:
                results = db.search(
                    "test",
                    collection_names=["knowledge__a__voyage-context-3__v1",
                                      "knowledge__b__voyage-context-3__v1"],
                )

        assert results == [], "should return empty, not raise"

        # The ERROR-level all-skipped event must fire
        error_calls = [c for c in mock_log.error.call_args_list
                       if c.args and "search_all_collections_dimension_skipped" in str(c.args[0])]
        assert error_calls, (
            "Fix 3 MISSING: search_all_collections_dimension_skipped ERROR event "
            "must fire when every collection is dimension-skipped"
        )

    def test_single_non_skipped_empty_col_does_not_trigger_all_skipped_error(self) -> None:
        """Fix 3: one non-error empty collection must NOT trigger the all-skipped ERROR."""
        mock_client = MagicMock()
        # One collection returns empty (no error)
        empty_col = MagicMock()
        empty_col.count.return_value = 0
        mock_client.get_collection.return_value = empty_col

        db = T3Database(_client=mock_client, _ef_override=MagicMock())

        with patch("nexus.db.t3._log") as mock_log:
            results = db.search("test", collection_names=["code__owner__voyage-code-3__v1"])

        assert results == []
        error_calls = [c for c in mock_log.error.call_args_list
                       if c.args and "search_all_collections_dimension_skipped" in str(c.args[0])]
        assert not error_calls, (
            "Fix 3: an empty (but not erroring) collection must NOT trigger all-skipped ERROR"
        )

    def test_partial_dimension_skip_does_not_trigger_all_skipped_error(self) -> None:
        """Fix 3: one skipped + one successful collection must NOT trigger all-skipped ERROR."""
        mock_client = MagicMock()
        call_count = [0]

        def _get_collection(name, **kwargs):
            col = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                # First collection: dimension error
                col.count.return_value = 5
                col.query.side_effect = InvalidArgumentError("dimension mismatch: got 384 want 768")
            else:
                # Second collection: success
                col.count.return_value = 1
                col.query.return_value = {
                    "ids": [["id1"]],
                    "documents": [["content"]],
                    "metadatas": [[""]],
                    "distances": [[0.1]],
                }
            return col

        mock_client.get_collection.side_effect = _get_collection
        db = T3Database(_client=mock_client, _ef_override=MagicMock())

        with patch("nexus.db.t3._log") as mock_log:
            db.search("test", collection_names=[
                "code__a__voyage-code-3__v1",
                "code__b__voyage-code-3__v1",
            ])

        error_calls = [c for c in mock_log.error.call_args_list
                       if c.args and "search_all_collections_dimension_skipped" in str(c.args[0])]
        assert not error_calls, (
            "Fix 3: partial skip (one success) must NOT trigger all-skipped ERROR"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Fix 4: display -- conformant 4-segment names show parsed model token
# ─────────────────────────────────────────────────────────────────────────────


class TestFix4DisplayAlias:
    """Fix 4: embedding_model_for_collection / index_model_for_collection
    must return the model token from the name, not voyage fallback."""

    def test_conformant_bge_name_displays_bge_token(self) -> None:
        """Fix 4: conformant bge collection shows bge token, not voyage-code-3."""
        from nexus.corpus import embedding_model_for_collection
        result = embedding_model_for_collection(
            "code__owner__bge-base-en-v15-768__v1"
        )
        assert result == "bge-base-en-v15-768", (
            f"Fix 4 MISSING: expected 'bge-base-en-v15-768' got {result!r}. "
            "embedding_model_for_collection must parse the 4-segment conformant "
            "name and return the embedded model token."
        )

    def test_conformant_minilm_name_displays_minilm_token(self) -> None:
        """Fix 4: conformant minilm collection shows minilm token."""
        from nexus.corpus import embedding_model_for_collection
        result = embedding_model_for_collection(
            "knowledge__test__minilm-l6-v2-384__v1"
        )
        assert result == "minilm-l6-v2-384", (
            f"Fix 4 MISSING: expected 'minilm-l6-v2-384' got {result!r}."
        )

    def test_conformant_voyage_name_still_returns_voyage(self) -> None:
        """Fix 4: conformant voyage collection returns voyage token (unchanged)."""
        from nexus.corpus import embedding_model_for_collection
        result = embedding_model_for_collection(
            "code__owner__voyage-code-3__v1"
        )
        assert result == "voyage-code-3"

    def test_legacy_two_segment_code_collection_falls_back_to_voyage(self) -> None:
        """Fix 4: legacy 2-segment code name still falls back to voyage-code-3."""
        from nexus.corpus import embedding_model_for_collection
        result = embedding_model_for_collection("code__myrepo")
        assert result == "voyage-code-3"

    def test_legacy_two_segment_knowledge_collection_falls_back_to_voyage(self) -> None:
        """Fix 4: legacy 2-segment knowledge name still falls back to voyage-context-3."""
        from nexus.corpus import embedding_model_for_collection
        result = embedding_model_for_collection("knowledge__security")
        assert result == "voyage-context-3"

    def test_index_model_for_collection_conformant_bge(self) -> None:
        """Fix 4: index_model_for_collection (alias) also returns bge token."""
        from nexus.corpus import index_model_for_collection
        result = index_model_for_collection(
            "code__owner__bge-base-en-v15-768__v1"
        )
        assert result == "bge-base-en-v15-768"

    def test_index_model_for_collection_legacy_code(self) -> None:
        """Fix 4 inverse: index_model_for_collection legacy 2-seg → voyage-code-3."""
        from nexus.corpus import index_model_for_collection
        result = index_model_for_collection("code__myrepo")
        assert result == "voyage-code-3"
