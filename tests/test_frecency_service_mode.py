# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-enehl: TDD tests for frecency service-mode routing (replaces nexus-67ljl skip guard).

Verifies that _run_index_frecency_only ROUTES through the Java service
(HttpVectorClient) in service mode rather than calling make_t3() / daemon-Chroma.

In service mode (NX_STORAGE_BACKEND_VECTORS=service):
1. make_t3() must NOT be called (no split-brain with daemon-Chroma).
2. get_http_vector_client() IS called — service client is used.
3. The function does NOT return early; it invokes the real frecency logic.
4. update_chunks() is called on the HttpVectorClient (proves the service path).

In non-service mode (default):
5. make_t3() IS called (normal path exercised).
6. get_http_vector_client() is NOT called.

Patch targets use fully-qualified module paths because the imports inside
_run_index_frecency_only are local (function-level) imports.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_registry(
    code_col: str = "code__repo__voyage-code-3__v1",
    docs_col: str | None = None,
) -> MagicMock:
    """Return a fake registry object whose .get() returns repo info."""
    reg = MagicMock()
    reg.get.return_value = {
        "collection": code_col,
        "code_collection": code_col,
        "docs_collection": docs_col,
    }
    return reg


def _make_svc_client(
    *,
    collection_exists: bool = True,
    chunk_ids: list[str] | None = None,
    chunk_metas: list[dict] | None = None,
) -> MagicMock:
    """Build a MagicMock HttpVectorClient for service-mode assertions."""
    from chromadb.errors import NotFoundError as _ChromaNotFoundError

    svc = MagicMock()
    if collection_exists:
        # get_collection returns a stub-like mock
        col_stub = MagicMock()
        ids = chunk_ids or []
        metas = chunk_metas or [{} for _ in ids]
        col_stub.get.return_value = {"ids": ids, "documents": [], "metadatas": metas}
        svc.get_collection.return_value = col_stub
    else:
        svc.get_collection.side_effect = _ChromaNotFoundError("collection not found")
    return svc


# ── Tests: service mode routing ────────────────────────────────────────────────


class TestFrecencyServiceModeRouting:
    """In service mode, _run_index_frecency_only must route through HttpVectorClient."""

    def test_make_t3_not_called_in_service_mode(self, tmp_path: Path) -> None:
        """make_t3 must NOT be called when NX_STORAGE_BACKEND_VECTORS=service."""
        svc = _make_svc_client()
        with (
            patch.dict(os.environ, {"NX_STORAGE_BACKEND_VECTORS": "service"}),
            patch("nexus.db.make_t3") as mock_make_t3,
            patch("nexus.db.http_vector_client.get_http_vector_client", return_value=svc),
            patch("nexus.frecency.batch_frecency", return_value={}),
            patch("nexus.indexer._build_frecency_doc_id_map", return_value={}),
        ):
            from nexus.indexer import _run_index_frecency_only
            _run_index_frecency_only(tmp_path, _make_registry())

        mock_make_t3.assert_not_called()

    def test_service_client_used_in_service_mode(self, tmp_path: Path) -> None:
        """get_http_vector_client IS called; the returned client is used as db."""
        svc = _make_svc_client()
        with (
            patch.dict(os.environ, {"NX_STORAGE_BACKEND_VECTORS": "service"}),
            patch("nexus.db.make_t3"),
            patch(
                "nexus.db.http_vector_client.get_http_vector_client",
                return_value=svc,
            ) as mock_get_svc,
            patch("nexus.frecency.batch_frecency", return_value={}),
            patch("nexus.indexer._build_frecency_doc_id_map", return_value={}),
        ):
            from nexus.indexer import _run_index_frecency_only
            _run_index_frecency_only(tmp_path, _make_registry())

        mock_get_svc.assert_called_once()

    def test_does_not_return_early_in_service_mode(self, tmp_path: Path) -> None:
        """In service mode, function proceeds through the frecency loop (not a skip)."""
        # Collection with one chunk — proves the inner loop executes
        svc = _make_svc_client(
            collection_exists=True,
            chunk_ids=["chunk-aaa"],
            chunk_metas=[{"frecency_score": 0.0}],
        )
        frecency_data = {tmp_path / "file.py": 0.75}
        with (
            patch.dict(os.environ, {"NX_STORAGE_BACKEND_VECTORS": "service"}),
            patch("nexus.db.make_t3"),
            patch("nexus.db.http_vector_client.get_http_vector_client", return_value=svc),
            patch("nexus.frecency.batch_frecency", return_value=frecency_data),
            patch("nexus.indexer._build_frecency_doc_id_map", return_value={}),
            patch("nexus.catalog.factory.make_catalog_reader", side_effect=Exception("no catalog")),
        ):
            from nexus.indexer import _run_index_frecency_only
            _run_index_frecency_only(tmp_path, _make_registry())

        # update_chunks must have been called at least once on the service client
        # (may be called for code + docs collections)
        assert svc.update_chunks.call_count >= 1, (
            "update_chunks must be called when service client is used — "
            "function must not skip/early-return in service mode"
        )

    def test_update_chunks_called_with_frecency_score(self, tmp_path: Path) -> None:
        """update_chunks on the service client receives the updated frecency_score metadata."""
        chunk_id = "chunk-bbb"
        original_meta = {"source_path": "file.py", "frecency_score": 0.0}
        svc = _make_svc_client(
            collection_exists=True,
            chunk_ids=[chunk_id],
            chunk_metas=[original_meta],
        )
        # Only set docs_collection=None explicitly to avoid it generating a fallback
        frecency_data = {tmp_path / "file.py": 0.88}
        with (
            patch.dict(os.environ, {"NX_STORAGE_BACKEND_VECTORS": "service"}),
            patch("nexus.db.make_t3"),
            patch("nexus.db.http_vector_client.get_http_vector_client", return_value=svc),
            patch("nexus.frecency.batch_frecency", return_value=frecency_data),
            patch("nexus.indexer._build_frecency_doc_id_map", return_value={}),
            patch("nexus.catalog.factory.make_catalog_reader", side_effect=Exception("no catalog")),
        ):
            from nexus.indexer import _run_index_frecency_only
            _run_index_frecency_only(tmp_path, _make_registry())

        # At least one update_chunks call should have occurred
        assert svc.update_chunks.call_count >= 1, (
            "update_chunks must be called on service client in service mode"
        )
        # All calls must have frecency_score=0.88
        for c in svc.update_chunks.call_args_list:
            updated_metas = c.kwargs.get("metadatas") or (c.args[2] if len(c.args) > 2 else [])
            assert all(
                m.get("frecency_score") == pytest.approx(0.88) for m in updated_metas
            ), (
                f"Expected frecency_score=0.88 in updated metadata, got: {updated_metas}"
            )

    def test_structured_log_emitted_in_service_mode(self, tmp_path: Path) -> None:
        """A 'frecency_service_mode' log event must be emitted (replaces old skip log)."""
        svc = _make_svc_client()
        with (
            patch.dict(os.environ, {"NX_STORAGE_BACKEND_VECTORS": "service"}),
            patch("nexus.db.make_t3"),
            patch("nexus.db.http_vector_client.get_http_vector_client", return_value=svc),
            patch("nexus.frecency.batch_frecency", return_value={}),
            patch("nexus.indexer._build_frecency_doc_id_map", return_value={}),
            patch("nexus.indexer._log") as mock_log,
        ):
            from nexus.indexer import _run_index_frecency_only
            _run_index_frecency_only(tmp_path, _make_registry())

        mock_log.info.assert_called_once()
        call_args = mock_log.info.call_args
        assert call_args[0][0] == "frecency_service_mode"

    def test_collection_not_found_skipped_gracefully(self, tmp_path: Path) -> None:
        """When get_collection raises ChromaNotFoundError, the collection is skipped."""
        svc = _make_svc_client(collection_exists=False)
        frecency_data = {tmp_path / "file.py": 0.5}
        with (
            patch.dict(os.environ, {"NX_STORAGE_BACKEND_VECTORS": "service"}),
            patch("nexus.db.make_t3"),
            patch("nexus.db.http_vector_client.get_http_vector_client", return_value=svc),
            patch("nexus.frecency.batch_frecency", return_value=frecency_data),
            patch("nexus.indexer._build_frecency_doc_id_map", return_value={}),
        ):
            from nexus.indexer import _run_index_frecency_only
            # Must not raise
            _run_index_frecency_only(tmp_path, _make_registry())

        # update_chunks must NOT have been called (collection skipped)
        svc.update_chunks.assert_not_called()

    def test_no_write_when_frecency_map_empty_service_mode(self, tmp_path: Path) -> None:
        """Empty frecency_map means no update_chunks call."""
        svc = _make_svc_client(collection_exists=True, chunk_ids=[], chunk_metas=[])
        with (
            patch.dict(os.environ, {"NX_STORAGE_BACKEND_VECTORS": "service"}),
            patch("nexus.db.make_t3"),
            patch("nexus.db.http_vector_client.get_http_vector_client", return_value=svc),
            patch("nexus.frecency.batch_frecency", return_value={}),
            patch("nexus.indexer._build_frecency_doc_id_map", return_value={}),
        ):
            from nexus.indexer import _run_index_frecency_only
            _run_index_frecency_only(tmp_path, _make_registry())

        svc.update_chunks.assert_not_called()


# ── Tests: non-service mode (default) ─────────────────────────────────────────


class TestFrecencyNonServiceMode:
    """In non-service mode, make_t3() IS called (normal path)."""

    def test_make_t3_called_in_local_mode(self, tmp_path: Path) -> None:
        """make_t3 must be called in local (non-service) mode."""
        fake_db = MagicMock()
        # get_collection raises so the inner loop exits immediately — but make_t3 WAS called
        from chromadb.errors import NotFoundError as _ChromaNotFoundError
        fake_db.get_collection.side_effect = _ChromaNotFoundError("collection not found")

        with (
            # nexus-tawx0: service mode is now the DEFAULT; non-service
            # requires the explicit opt-out value.
            patch.dict(os.environ, {"NX_STORAGE_BACKEND_VECTORS": "chroma"}, clear=False),
            patch("nexus.db.make_t3", return_value=fake_db) as mock_make_t3,
            patch("nexus.db.http_vector_client.get_http_vector_client") as mock_get_svc,
            patch("nexus.frecency.batch_frecency", return_value={}),
            patch("nexus.indexer.check_local_path_writable"),
            patch("nexus.indexer._build_frecency_doc_id_map", return_value={}),
            patch("nexus.config.is_local_mode", return_value=True),
        ):
            from nexus.indexer import _run_index_frecency_only
            _run_index_frecency_only(tmp_path, _make_registry())

        mock_make_t3.assert_called_once()
        mock_get_svc.assert_not_called()

    def test_registry_none_returns_early(self, tmp_path: Path) -> None:
        """When registry.get returns None, return early (no make_t3 call)."""
        registry = MagicMock()
        registry.get.return_value = None

        with (
            # nexus-tawx0: service mode is now the DEFAULT; non-service
            # requires the explicit opt-out value.
            patch.dict(os.environ, {"NX_STORAGE_BACKEND_VECTORS": "chroma"}, clear=False),
            patch("nexus.db.make_t3") as mock_make_t3,
        ):
            from nexus.indexer import _run_index_frecency_only
            _run_index_frecency_only(tmp_path, registry)

        mock_make_t3.assert_not_called()
