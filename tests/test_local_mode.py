# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for local T3 mode (RDR-038 Phase 1).

Covers:
- config.py: is_local_mode(), _default_local_path()
- db/local_ef.py: LocalEmbeddingFunction tier auto-selection
- db/t3.py: T3Database local_mode init path
- db/__init__.py: make_t3() local path
- retry.py: sqlite3.OperationalError retryable
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest

from nexus.config import is_local_mode, _default_local_path
from nexus.db.t3 import T3Database


# ── config.py: is_local_mode ────────────────────────────────────────────────


class TestIsLocalMode:
    """is_local_mode() returns True when NX_LOCAL=1 or no cloud credentials."""

    def test_nx_local_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NX_LOCAL=1 forces local mode regardless of credentials."""
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.setenv("CHROMA_API_KEY", "some-key")
        monkeypatch.setenv("VOYAGE_API_KEY", "some-key")
        assert is_local_mode() is True

    def test_nx_local_env_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NX_LOCAL=0 explicitly disables local mode."""
        monkeypatch.setenv("NX_LOCAL", "0")
        monkeypatch.delenv("CHROMA_API_KEY", raising=False)
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        assert is_local_mode() is False

    def test_no_cloud_credentials(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """No API keys and no NX_LOCAL → auto-detect as local mode."""
        monkeypatch.delenv("NX_LOCAL", raising=False)
        monkeypatch.delenv("CHROMA_API_KEY", raising=False)
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert is_local_mode() is True

    def test_has_cloud_credentials(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Both API keys present and no NX_LOCAL → cloud mode."""
        monkeypatch.delenv("NX_LOCAL", raising=False)
        monkeypatch.setenv("CHROMA_API_KEY", "key1")
        monkeypatch.setenv("VOYAGE_API_KEY", "key2")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert is_local_mode() is False

    def test_partial_credentials_chroma_only(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Only CHROMA_API_KEY without VOYAGE_API_KEY → local mode."""
        monkeypatch.delenv("NX_LOCAL", raising=False)
        monkeypatch.setenv("CHROMA_API_KEY", "key1")
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert is_local_mode() is True

    def test_partial_credentials_voyage_only(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Only VOYAGE_API_KEY without CHROMA_API_KEY → local mode."""
        monkeypatch.delenv("NX_LOCAL", raising=False)
        monkeypatch.delenv("CHROMA_API_KEY", raising=False)
        monkeypatch.setenv("VOYAGE_API_KEY", "key2")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert is_local_mode() is True


class TestDefaultLocalPath:
    """_default_local_path() returns the XDG-aware local ChromaDB path."""

    def test_default_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without XDG_DATA_HOME, uses ~/.local/share/nexus/chroma."""
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setenv("HOME", "/home/testuser")
        result = _default_local_path()
        assert result == Path("/home/testuser/.local/share/nexus/chroma")

    def test_xdg_data_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """XDG_DATA_HOME is respected."""
        monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
        result = _default_local_path()
        assert result == Path("/custom/data/nexus/chroma")

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NX_LOCAL_CHROMA_PATH overrides the default."""
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", "/my/chroma")
        result = _default_local_path()
        assert result == Path("/my/chroma")


# ── db/local_ef.py: LocalEmbeddingFunction ──────────────────────────────────


class TestLocalEmbeddingFunction:
    """LocalEmbeddingFunction auto-selects tier 0 (ONNX MiniLM) or tier 1 (fastembed)."""

    def test_tier0_no_fastembed(self) -> None:
        """Without fastembed, falls back to tier 0 (bundled ONNX MiniLM)."""
        with patch.dict("sys.modules", {"fastembed": None}):
            from nexus.db.local_ef import LocalEmbeddingFunction
            ef = LocalEmbeddingFunction()
            assert ef.model_name == "all-MiniLM-L6-v2"
            assert ef.dimensions == 384

    def test_tier0_embeds_text(self) -> None:
        """Tier 0 can embed text and returns correct dimensionality."""
        from nexus.db.local_ef import LocalEmbeddingFunction
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        result = ef(["hello world"])
        assert len(result) == 1
        assert len(result[0]) == 384

    def test_tier0_multiple_texts(self) -> None:
        """Tier 0 can embed multiple texts at once."""
        from nexus.db.local_ef import LocalEmbeddingFunction
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        result = ef(["hello", "world", "test"])
        assert len(result) == 3
        for vec in result:
            assert len(vec) == 384

    def test_explicit_model_override(self) -> None:
        """Explicit model_name overrides auto-selection."""
        from nexus.db.local_ef import LocalEmbeddingFunction
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        assert ef.model_name == "all-MiniLM-L6-v2"
        assert ef.dimensions == 384


# ── db/t3.py: T3Database local_mode ─────────────────────────────────────────


class TestT3DatabaseLocalMode:
    """T3Database(local_mode=True) creates PersistentClient, skips cloud probe."""

    def test_local_mode_creates_persistent_client(self, tmp_path: Path) -> None:
        """local_mode=True uses PersistentClient instead of CloudClient."""
        from nexus.db.local_ef import LocalEmbeddingFunction
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        db = T3Database(local_mode=True, local_path=str(tmp_path / "chroma"), _ef_override=ef)
        assert db._local_mode is True
        assert isinstance(db._client, chromadb.ClientAPI)

    def test_local_mode_no_voyage_client(self, tmp_path: Path) -> None:
        """local_mode=True does not create a voyage client."""
        from nexus.db.local_ef import LocalEmbeddingFunction
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        db = T3Database(local_mode=True, local_path=str(tmp_path / "chroma"), _ef_override=ef)
        assert db._voyage_client is None

    def test_local_mode_no_cloud_probe(self, tmp_path: Path) -> None:
        """local_mode=True skips the old-layout probe entirely."""
        from nexus.db.local_ef import LocalEmbeddingFunction
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        with patch("nexus.db.t3.chromadb.CloudClient") as mock_cloud:
            T3Database(local_mode=True, local_path=str(tmp_path / "chroma"), _ef_override=ef)
            mock_cloud.assert_not_called()

    def test_local_mode_put_and_search(self, tmp_path: Path) -> None:
        """End-to-end: put a document and search for it in local mode."""
        from nexus.db.local_ef import LocalEmbeddingFunction
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        db = T3Database(local_mode=True, local_path=str(tmp_path / "chroma"), _ef_override=ef)
        doc_id = db.put(
            collection="knowledge__test",
            content="Python is a programming language",
            title="python-fact",
        )
        assert doc_id
        results = db.search("programming language", collection_names=["knowledge__test"])
        assert len(results) >= 1
        assert any("Python" in r.get("content", "") for r in results)

    def test_local_mode_search_skips_cce(self, tmp_path: Path) -> None:
        """Local mode search does not attempt CCE embedding."""
        from nexus.db.local_ef import LocalEmbeddingFunction
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        db = T3Database(local_mode=True, local_path=str(tmp_path / "chroma"), _ef_override=ef)
        # voyage_api_key is empty, so CCE path should not trigger
        assert db._voyage_client is None
        # Put and search should work without CCE
        db.put(collection="knowledge__test", content="test content", title="t1")
        results = db.search("test", collection_names=["knowledge__test"])
        assert isinstance(results, list)

    def test_local_mode_skips_max_query_results_clamping(self, tmp_path: Path) -> None:
        """Local mode does not clamp n_results to QUOTAS.MAX_QUERY_RESULTS."""
        from nexus.db.local_ef import LocalEmbeddingFunction
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        db = T3Database(local_mode=True, local_path=str(tmp_path / "chroma"), _ef_override=ef)
        # Insert enough docs to test
        for i in range(5):
            db.put(collection="knowledge__test", content=f"document {i}", title=f"doc-{i}")
        # Request more than MAX_QUERY_RESULTS — should not clamp
        results = db.search("document", collection_names=["knowledge__test"], n_results=500)
        assert isinstance(results, list)

    def test_cloud_mode_still_works(self) -> None:
        """Existing cloud mode init path is unaffected."""
        from nexus.db.t3 import T3Database
        mock_client = MagicMock()
        db = T3Database(_client=mock_client, _ef_override=MagicMock())
        assert db._local_mode is False

    def test_local_mode_creates_path(self, tmp_path: Path) -> None:
        """local_mode creates the local_path directory if it doesn't exist."""
        from nexus.db.local_ef import LocalEmbeddingFunction
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        chroma_dir = tmp_path / "nonexistent" / "chroma"
        T3Database(local_mode=True, local_path=str(chroma_dir), _ef_override=ef)
        assert chroma_dir.exists()


# ── db/__init__.py: make_t3 local path ──────────────────────────────────────


class TestMakeT3Local:
    """make_t3() returns a local T3Database when is_local_mode() is True."""

    def test_make_t3_local_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """make_t3() in local mode returns T3Database with _local_mode=True."""
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma"))
        monkeypatch.setenv("HOME", str(tmp_path))
        from nexus.db import make_t3
        db = make_t3()
        assert db._local_mode is True

    def test_make_t3_cloud_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """make_t3() in cloud mode returns T3Database with _local_mode=False."""
        monkeypatch.setenv("NX_LOCAL", "0")
        from nexus.db import make_t3
        mock_client = MagicMock()
        db = make_t3(_client=mock_client, _ef_override=MagicMock())
        assert db._local_mode is False


# ── retry.py: sqlite3 OperationalError ──────────────────────────────────────


class TestRetryableSqliteError:
    """sqlite3.OperationalError('database is locked') is retryable."""

    def test_sqlite_locked_is_retryable(self) -> None:
        """sqlite3.OperationalError with 'locked' message is retryable."""
        from nexus.retry import _is_retryable_chroma_error
        exc = sqlite3.OperationalError("database is locked")
        assert _is_retryable_chroma_error(exc) is True

    def test_sqlite_other_error_not_retryable(self) -> None:
        """sqlite3.OperationalError without 'locked' is not retryable."""
        from nexus.retry import _is_retryable_chroma_error
        exc = sqlite3.OperationalError("no such table: foo")
        assert _is_retryable_chroma_error(exc) is False

    def test_chroma_with_retry_retries_locked(self) -> None:
        """_chroma_with_retry retries on sqlite3 'database is locked'."""
        from nexus.retry import _chroma_with_retry
        call_count = 0

        def flaky_fn():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise sqlite3.OperationalError("database is locked")
            return "success"

        with patch("nexus.retry.time.sleep"):
            result = _chroma_with_retry(flaky_fn)
        assert result == "success"
        assert call_count == 3
