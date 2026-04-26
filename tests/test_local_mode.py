# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest

from nexus.config import is_local_mode, _default_local_path
from nexus.db.local_ef import LocalEmbeddingFunction
from nexus.db.t3 import T3Database


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def local_ef() -> LocalEmbeddingFunction:
    return LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")


@pytest.fixture()
def local_db(tmp_path: Path, local_ef: LocalEmbeddingFunction) -> T3Database:
    return T3Database(local_mode=True, local_path=str(tmp_path / "chroma"), _ef_override=local_ef)


# ── config.py: is_local_mode ─────────────────────────────────────────────────


class TestIsLocalMode:
    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            pytest.param({"NX_LOCAL": "1", "CHROMA_API_KEY": "k", "VOYAGE_API_KEY": "k"}, True, id="nx_local_1_overrides"),
            pytest.param({"NX_LOCAL": "0"}, False, id="nx_local_0_overrides"),
            pytest.param({}, True, id="no_credentials"),
            pytest.param({"CHROMA_API_KEY": "k", "VOYAGE_API_KEY": "k"}, False, id="both_keys"),
            pytest.param({"CHROMA_API_KEY": "k"}, True, id="chroma_only"),
            pytest.param({"VOYAGE_API_KEY": "k"}, True, id="voyage_only"),
        ],
    )
    def test_is_local_mode(
        self, env: dict[str, str], expected: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        for var in ("NX_LOCAL", "CHROMA_API_KEY", "VOYAGE_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert is_local_mode() is expected


class TestDefaultLocalPath:
    @pytest.mark.parametrize(
        ("env", "expected_suffix"),
        [
            pytest.param({}, ".local/share/nexus/chroma", id="default"),
            pytest.param({"XDG_DATA_HOME": "/custom/data"}, None, id="xdg"),
            pytest.param({"NX_LOCAL_CHROMA_PATH": "/my/chroma"}, None, id="env_override"),
        ],
    )
    def test_default_local_path(
        self, env: dict[str, str], expected_suffix: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("NX_LOCAL_CHROMA_PATH", raising=False)
        monkeypatch.setenv("HOME", "/home/testuser")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        result = _default_local_path()
        if "NX_LOCAL_CHROMA_PATH" in env:
            assert result == Path(env["NX_LOCAL_CHROMA_PATH"])
        elif "XDG_DATA_HOME" in env:
            assert result == Path("/custom/data/nexus/chroma")
        else:
            assert result == Path(f"/home/testuser/{expected_suffix}")


# ── db/local_ef.py: LocalEmbeddingFunction ────────────────────────────────────


class TestLocalEmbeddingFunction:
    def test_tier0_no_fastembed(self) -> None:
        with patch.dict("sys.modules", {"fastembed": None}):
            ef = LocalEmbeddingFunction()
            assert ef.model_name == "all-MiniLM-L6-v2"
            assert ef.dimensions == 384

    @pytest.mark.parametrize("n_texts", [1, 3], ids=["single", "batch"])
    def test_tier0_embeds(self, local_ef: LocalEmbeddingFunction, n_texts: int) -> None:
        result = local_ef(["hello"] * n_texts)
        assert len(result) == n_texts
        for vec in result:
            assert len(vec) == 384

    def test_explicit_model_override(self) -> None:
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        assert ef.model_name == "all-MiniLM-L6-v2"
        assert ef.dimensions == 384


# ── db/t3.py: T3Database local_mode ──────────────────────────────────────────


class TestT3DatabaseLocalMode:
    def test_local_mode_init(self, local_db: T3Database) -> None:
        assert local_db._local_mode is True
        assert isinstance(local_db._client, chromadb.ClientAPI)
        assert local_db._voyage_client is None

    def test_local_mode_no_cloud_probe(self, tmp_path: Path, local_ef: LocalEmbeddingFunction) -> None:
        with patch("nexus.db.t3.chromadb.CloudClient") as mock_cloud:
            T3Database(local_mode=True, local_path=str(tmp_path / "chroma"), _ef_override=local_ef)
            mock_cloud.assert_not_called()

    def test_local_mode_put_and_search(self, local_db: T3Database) -> None:
        doc_id = local_db.put(
            collection="knowledge__test",
            content="Python is a programming language",
            title="python-fact",
        )
        assert doc_id
        results = local_db.search("programming language", collection_names=["knowledge__test"])
        assert len(results) >= 1
        assert any("Python" in r.get("content", "") for r in results)

    def test_local_mode_search_skips_cce(self, local_db: T3Database) -> None:
        assert local_db._voyage_client is None
        local_db.put(collection="knowledge__test", content="test content", title="t1")
        results = local_db.search("test", collection_names=["knowledge__test"])
        assert isinstance(results, list)

    def test_local_mode_skips_max_query_results_clamping(self, local_db: T3Database) -> None:
        for i in range(5):
            local_db.put(collection="knowledge__test", content=f"document {i}", title=f"doc-{i}")
        results = local_db.search("document", collection_names=["knowledge__test"], n_results=500)
        assert isinstance(results, list)

    def test_cloud_mode_still_works(self) -> None:
        mock_client = MagicMock()
        db = T3Database(_client=mock_client, _ef_override=MagicMock())
        assert db._local_mode is False

    def test_local_mode_creates_path(self, tmp_path: Path, local_ef: LocalEmbeddingFunction) -> None:
        chroma_dir = tmp_path / "nonexistent" / "chroma"
        T3Database(local_mode=True, local_path=str(chroma_dir), _ef_override=local_ef)
        assert chroma_dir.exists()


# ── db/__init__.py: make_t3 local path ────────────────────────────────────────


class TestMakeT3Local:
    def test_make_t3_local_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma"))
        monkeypatch.setenv("HOME", str(tmp_path))
        from nexus.db import make_t3
        assert make_t3()._local_mode is True

    def test_make_t3_cloud_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_LOCAL", "0")
        from nexus.db import make_t3
        assert make_t3(_client=MagicMock(), _ef_override=MagicMock())._local_mode is False


# ── retry.py: sqlite3 OperationalError ────────────────────────────────────────


class TestRetryableSqliteError:
    @pytest.mark.parametrize(
        ("msg", "expected"),
        [("database is locked", True), ("no such table: foo", False)],
        ids=["locked", "other"],
    )
    def test_retryable_classification(self, msg: str, expected: bool) -> None:
        from nexus.retry import _is_retryable_chroma_error
        assert _is_retryable_chroma_error(sqlite3.OperationalError(msg)) is expected

    def test_chroma_with_retry_retries_locked(self) -> None:
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


# ── Staleness round-trip ──────────────────────────────────────────────────────


class TestLocalStaleness:
    @pytest.mark.parametrize(
        ("query_hash", "query_model", "expected"),
        [
            ("abc123", "all-MiniLM-L6-v2", True),
            ("def456", "all-MiniLM-L6-v2", False),
            ("abc123", "voyage-code-3", False),
        ],
        ids=["same-skip", "changed-hash", "changed-model"],
    )
    def test_staleness(
        self, local_db: T3Database, query_hash: str, query_model: str, expected: bool
    ) -> None:
        from nexus.indexer_utils import check_staleness

        col = local_db.get_or_create_collection("code__test")
        local_db.upsert_chunks(
            "code__test",
            ids=["chunk1"],
            documents=["def hello(): pass"],
            metadatas=[{
                "source_path": "/repo/hello.py",
                "content_hash": "abc123",
                "embedding_model": "all-MiniLM-L6-v2",
            }],
        )
        assert check_staleness(col, "/repo/hello.py", query_hash, query_model) is expected


# ── Collection lifecycle ──────────────────────────────────────────────────────


class TestLocalCollectionLifecycle:
    def test_collection_lifecycle(self, local_db: T3Database) -> None:
        doc_id = local_db.put(
            collection="knowledge__lifecycle",
            content="Rust is a systems programming language",
            title="rust-fact",
            tags="rust,systems",
        )
        assert doc_id

        results = local_db.search("systems programming", collection_names=["knowledge__lifecycle"])
        assert len(results) >= 1
        assert any("Rust" in r.get("content", "") for r in results)

        names = [c["name"] for c in local_db.list_collections()]
        assert "knowledge__lifecycle" in names
        assert len(local_db.list_store("knowledge__lifecycle")) >= 1
        assert local_db.delete_by_id("knowledge__lifecycle", doc_id) is True

    def test_expire_ttl_entries(self, local_db: T3Database) -> None:
        """``expire()`` deletes entries whose ``indexed_at + ttl_days``
        is in the past; permanent entries (``ttl_days=0``) are kept.
        ``expires_at`` was removed from the schema; expiry is derived
        Python-side via ``metadata_schema.is_expired``."""
        from datetime import UTC, datetime, timedelta
        from nexus.metadata_schema import make_chunk_metadata
        import hashlib

        # Backdate the indexed_at by 100 days with ttl_days=1 → expired.
        old = (datetime.now(UTC) - timedelta(days=100)).isoformat()
        col = local_db.get_or_create_collection("knowledge__expire_test")
        h_temp = hashlib.sha256(b"temporary data").hexdigest()
        col.upsert(
            ids=["temp-id"],
            documents=["temporary data"],
            metadatas=[make_chunk_metadata(
                content_type="prose", source_path="",
                chunk_index=0, chunk_count=1,
                chunk_text_hash=h_temp, content_hash=h_temp,
                chunk_end_char=14,
                indexed_at=old, ttl_days=1,
                embedding_model="local-onnx-minilm-l6-v2",
                store_type="knowledge", title="temp",
            )],
        )
        # Permanent entry — still alive after expire().
        local_db.put(
            collection="knowledge__expire_test",
            content="permanent data",
            title="perm",
            ttl_days=0,
        )
        assert local_db.expire() >= 1
        assert len(local_db.search("permanent", collection_names=["knowledge__expire_test"])) >= 1


# ── Corpus model consistency ──────────────────────────────────────────────────


class TestCorpusLocalModels:
    def test_local_ef_model_name_consistent(self, local_ef: LocalEmbeddingFunction) -> None:
        assert local_ef.model_name
        assert isinstance(local_ef.model_name, str)

    @pytest.mark.parametrize(
        ("collection", "expected_model"),
        [("code__test", "voyage-code-3"), ("docs__test", "voyage-context-3")],
    )
    def test_corpus_functions_return_cloud_names(self, collection: str, expected_model: str) -> None:
        from nexus.corpus import index_model_for_collection, embedding_model_for_collection
        assert index_model_for_collection(collection) == expected_model
        assert embedding_model_for_collection(collection) == expected_model


# ── Frecency-only local mode ─────────────────────────────────────────────────


class TestFrecencyOnlyLocalMode:
    def test_frecency_only_no_crash(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma"))
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        monkeypatch.delenv("CHROMA_API_KEY", raising=False)

        from nexus.indexer import _run_index_frecency_only

        registry = MagicMock()
        registry.get.return_value = {
            "collection": "code__repo",
            "code_collection": "code__repo",
            "docs_collection": "docs__repo",
        }
        with patch("nexus.frecency.batch_frecency", return_value={}):
            _run_index_frecency_only(tmp_path, registry)


# ── Check local path writable ────────────────────────────────────────────────


class TestCheckLocalPathWritable:
    def test_writable_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma"))
        from nexus.indexer_utils import check_local_path_writable
        check_local_path_writable()

    def test_unwritable_path_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", "/proc/nonexistent/chroma")
        from nexus.indexer_utils import check_local_path_writable
        from nexus.errors import CredentialsMissingError
        with pytest.raises(CredentialsMissingError, match="not writable"):
            check_local_path_writable()
