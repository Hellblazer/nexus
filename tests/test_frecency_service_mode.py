# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-67ljl: TDD tests for frecency split-brain guard.

Verifies that _run_index_frecency_only does NOT call make_t3() / does NOT write
daemon-Chroma in service mode (NX_STORAGE_BACKEND_VECTORS=service).

In service mode:
1. make_t3() must NOT be called (split-brain guard).
2. A structured log event "frecency_skipped_service_mode" must be emitted.
3. The function returns early without raising.

In non-service mode (default):
4. make_t3() IS called (normal path exercised).

Patch targets use fully-qualified module paths because the imports inside
_run_index_frecency_only are local (function-level) imports.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch


import pytest


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_registry(code_col: str = "code__repo__voyage-code-3__v1") -> MagicMock:
    """Return a fake registry object whose .get() returns repo info."""
    reg = MagicMock()
    reg.get.return_value = {
        "collection": code_col,
        "code_collection": code_col,
        "docs_collection": None,
    }
    return reg


# ── Tests: service mode guard ──────────────────────────────────────────────────


class TestFrecencyServiceModeGuard:
    """In service mode, _run_index_frecency_only must skip the Chroma write."""

    def test_make_t3_not_called_in_service_mode(self, tmp_path: Path) -> None:
        """make_t3 must NOT be called when NX_STORAGE_BACKEND_VECTORS=service."""
        with (
            patch.dict(os.environ, {"NX_STORAGE_BACKEND_VECTORS": "service"}),
            patch("nexus.db.make_t3") as mock_make_t3,
            patch("nexus.frecency.batch_frecency", return_value={}),
            patch("nexus.indexer.check_local_path_writable"),
            patch("nexus.indexer._build_frecency_doc_id_map", return_value={}),
            patch("nexus.config.is_local_mode", return_value=True),
        ):
            from nexus.indexer import _run_index_frecency_only
            _run_index_frecency_only(tmp_path, _make_registry())

        mock_make_t3.assert_not_called()

    def test_structured_log_emitted_in_service_mode(self, tmp_path: Path) -> None:
        """A 'frecency_skipped_service_mode' log event must be emitted.

        structlog by default writes to its own bound logger chain.  We verify
        the log by patching the module-level ``_log`` object's ``.info()`` method
        and asserting the expected event key was passed.
        """
        with (
            patch.dict(os.environ, {"NX_STORAGE_BACKEND_VECTORS": "service"}),
            patch("nexus.db.make_t3"),
            patch("nexus.frecency.batch_frecency", return_value={}),
            patch("nexus.indexer.check_local_path_writable"),
            patch("nexus.indexer._build_frecency_doc_id_map", return_value={}),
            patch("nexus.config.is_local_mode", return_value=True),
            patch("nexus.indexer._log") as mock_log,
        ):
            from nexus.indexer import _run_index_frecency_only
            _run_index_frecency_only(tmp_path, _make_registry())

        # Assert that _log.info was called with the expected event key
        mock_log.info.assert_called_once()
        call_args = mock_log.info.call_args
        # First positional argument is the event name
        assert call_args[0][0] == "frecency_skipped_service_mode"

    def test_returns_early_without_error_in_service_mode(self, tmp_path: Path) -> None:
        """Function must return cleanly (no exception) in service mode."""
        with (
            patch.dict(os.environ, {"NX_STORAGE_BACKEND_VECTORS": "service"}),
            patch("nexus.db.make_t3", side_effect=RuntimeError("must not be called")),
            patch("nexus.frecency.batch_frecency", return_value={}),
            patch("nexus.indexer.check_local_path_writable"),
            patch("nexus.indexer._build_frecency_doc_id_map", return_value={}),
            patch("nexus.config.is_local_mode", return_value=True),
        ):
            from nexus.indexer import _run_index_frecency_only
            # Must not raise even though make_t3 would raise RuntimeError
            _run_index_frecency_only(tmp_path, _make_registry())

    def test_no_write_when_frecency_map_nonempty_service_mode(self, tmp_path: Path) -> None:
        """Even with items in frecency_map, service mode must not call make_t3."""
        frecency_data = {tmp_path / "file.py": 0.75}

        with (
            patch.dict(os.environ, {"NX_STORAGE_BACKEND_VECTORS": "service"}),
            patch("nexus.db.make_t3") as mock_make_t3,
            patch("nexus.frecency.batch_frecency", return_value=frecency_data),
            patch("nexus.indexer.check_local_path_writable"),
            patch("nexus.indexer._build_frecency_doc_id_map", return_value={}),
            patch("nexus.config.is_local_mode", return_value=True),
        ):
            from nexus.indexer import _run_index_frecency_only
            _run_index_frecency_only(tmp_path, _make_registry())

        mock_make_t3.assert_not_called()


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
            patch.dict(os.environ, {"NX_STORAGE_BACKEND_VECTORS": ""}, clear=False),
            patch("nexus.db.make_t3", return_value=fake_db) as mock_make_t3,
            patch("nexus.frecency.batch_frecency", return_value={}),
            patch("nexus.indexer.check_local_path_writable"),
            patch("nexus.indexer._build_frecency_doc_id_map", return_value={}),
            patch("nexus.config.is_local_mode", return_value=True),
        ):
            from nexus.indexer import _run_index_frecency_only
            _run_index_frecency_only(tmp_path, _make_registry())

        mock_make_t3.assert_called_once()

    def test_registry_none_returns_early(self, tmp_path: Path) -> None:
        """When registry.get returns None, return early (no make_t3 call)."""
        registry = MagicMock()
        registry.get.return_value = None

        with (
            patch.dict(os.environ, {"NX_STORAGE_BACKEND_VECTORS": ""}, clear=False),
            patch("nexus.db.make_t3") as mock_make_t3,
        ):
            from nexus.indexer import _run_index_frecency_only
            _run_index_frecency_only(tmp_path, registry)

        mock_make_t3.assert_not_called()
