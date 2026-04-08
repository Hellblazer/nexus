# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for HNSW ef tuning (RDR-056 Phase 1a)."""
from __future__ import annotations

import threading
import uuid
from unittest.mock import MagicMock, patch

import chromadb
import pytest

from nexus.db.t3 import T3Database, apply_hnsw_ef
from nexus.db.chroma_quotas import QuotaValidator


# EphemeralClient is a process-wide singleton — collections persist across
# test instances.  Use unique suffixes so tests don't collide.

def _unique(prefix: str = "code__test") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _local_db() -> T3Database:
    """Create a local-mode T3Database backed by the shared EphemeralClient."""
    return T3Database(
        _client=chromadb.EphemeralClient(),
        local_mode=True,
        local_path="/tmp/test",
    )


def _local_db_with_ef() -> T3Database:
    """Local-mode T3Database with DefaultEmbeddingFunction override (no Voyage)."""
    db = _local_db()
    db._ef_override = chromadb.utils.embedding_functions.DefaultEmbeddingFunction()
    return db


def _cloud_db_with_ef() -> T3Database:
    """Simulate a cloud-mode T3Database with EF override (avoids Voyage API calls)."""
    db = T3Database.__new__(T3Database)
    db._local_mode = False
    db._voyage_api_key = ""
    db._ef_override = chromadb.utils.embedding_functions.DefaultEmbeddingFunction()
    db._ef_cache = {}
    db._ef_lock = threading.Lock()
    db._write_sems = {}
    db._read_sems = {}
    db._sems_lock = threading.Lock()
    db._quota_validator = QuotaValidator()
    db._client = chromadb.EphemeralClient()
    db._voyage_client = None
    return db


class TestGetOrCreateCollectionHnswMetadata:
    def test_local_mode_passes_hnsw_search_ef_metadata(self) -> None:
        """get_or_create_collection passes hnsw:search_ef metadata in local mode."""
        db = _local_db_with_ef()
        col = db.get_or_create_collection(_unique("code__ef"))
        meta = col.metadata or {}
        assert "hnsw:search_ef" in meta
        assert meta["hnsw:search_ef"] == 256

    def test_local_mode_hnsw_ef_matches_config(self) -> None:
        """hnsw:search_ef value matches config search.hnsw_ef default (256)."""
        db = _local_db_with_ef()
        col = db.get_or_create_collection(_unique("knowledge__ef"))
        assert col.metadata["hnsw:search_ef"] == 256

    def test_cloud_mode_does_not_pass_hnsw_metadata(self) -> None:
        """get_or_create_collection does NOT pass hnsw metadata in cloud mode."""
        db = _cloud_db_with_ef()
        col = db.get_or_create_collection(_unique("code__cloud"))
        meta = col.metadata or {}
        assert "hnsw:search_ef" not in meta


class TestApplyHnswEf:
    def test_apply_hnsw_ef_local_mode_modifies_collections(self) -> None:
        """apply_hnsw_ef() modifies collections and returns correct count."""
        # Create collections directly via EphemeralClient (no T3 metadata)
        client = chromadb.EphemeralClient()
        name1 = _unique("code__apply1")
        name2 = _unique("docs__apply2")
        col1 = client.get_or_create_collection(name1)
        col2 = client.get_or_create_collection(name2)

        # Capture modify calls via mocks
        original_modify1 = col1.modify
        original_modify2 = col2.modify
        modify_calls: list[dict] = []

        def tracking_modify(**kwargs):
            modify_calls.append(kwargs)

        # Patch list_collections to return only our two collections
        mock_col1 = MagicMock()
        mock_col2 = MagicMock()

        db = T3Database(_client=client, local_mode=True, local_path="/tmp/test")

        with patch.object(client, "list_collections", return_value=[mock_col1, mock_col2]):
            mock_col1.modify = MagicMock()
            mock_col2.modify = MagicMock()
            count = apply_hnsw_ef(db)

        assert count == 2
        mock_col1.modify.assert_called_once_with(metadata={"hnsw:search_ef": 256})
        mock_col2.modify.assert_called_once_with(metadata={"hnsw:search_ef": 256})

    def test_apply_hnsw_ef_cloud_mode_no_op(self) -> None:
        """apply_hnsw_ef() returns 0 and no-ops in cloud mode."""
        db = _cloud_db_with_ef()
        count = apply_hnsw_ef(db)
        assert count == 0

    def test_apply_hnsw_ef_empty_local_returns_zero(self) -> None:
        """apply_hnsw_ef() returns 0 when no collections exist in local mode."""
        db = _local_db()
        with patch.object(db._client, "list_collections", return_value=[]):
            count = apply_hnsw_ef(db)
        assert count == 0

    def test_apply_hnsw_ef_uses_config_value(self) -> None:
        """apply_hnsw_ef() reads hnsw_ef from config (default 256)."""
        db = _local_db()
        mock_col = MagicMock()
        with patch.object(db._client, "list_collections", return_value=[mock_col]):
            count = apply_hnsw_ef(db)
        assert count == 1
        mock_col.modify.assert_called_once_with(metadata={"hnsw:search_ef": 256})


class TestVerifyCollectionDeepHnswSpace:
    """Tests for the hnsw:space reading logic in verify_collection_deep.

    db.search() is mocked to isolate the metric-extraction code path and avoid
    ChromaDB embedding-function conflicts in unit tests.
    """

    def _fake_search(self, probe_id: str):
        """Return a search() replacement that pretends probe_id was found."""
        def _inner(query, collection_names, n_results=10, where=None):
            return [{"id": probe_id, "content": "alpha beta", "distance": 0.1}]
        return _inner

    def test_local_mode_reads_hnsw_space_from_metadata(self) -> None:
        """verify_collection_deep reads hnsw:space from collection metadata in local mode."""
        from nexus.db.t3 import verify_collection_deep

        db = _local_db_with_ef()
        col_name = _unique("code__spaceloc")
        client = db._client
        client.get_or_create_collection(col_name, metadata={"hnsw:space": "cosine"})
        col = client.get_collection(col_name)
        probe_id = "doc__space_1"
        col.add(
            ids=[probe_id, "doc__space_2"],
            documents=["alpha beta gamma delta epsilon", "foo bar baz qux quux"],
            metadatas=[{"src": "x"}, {"src": "y"}],
        )
        with patch.object(db, "search", side_effect=self._fake_search(probe_id)):
            result = verify_collection_deep(db, col_name)
        assert result.metric == "cosine"

    def test_cloud_mode_returns_cosine_without_metadata(self) -> None:
        """verify_collection_deep returns 'cosine' in cloud mode regardless of hnsw:space."""
        from nexus.db.t3 import verify_collection_deep

        db = _cloud_db_with_ef()
        col_name = _unique("code__spacecld")
        client = db._client
        client.get_or_create_collection(col_name)
        col = client.get_collection(col_name)
        probe_id = "doc__cloud_1"
        col.add(
            ids=[probe_id, "doc__cloud_2"],
            documents=["alpha beta gamma delta epsilon", "foo bar baz qux quux"],
            metadatas=[{"src": "a"}, {"src": "b"}],
        )
        with patch.object(db, "search", side_effect=self._fake_search(probe_id)):
            result = verify_collection_deep(db, col_name)
        assert result.metric == "cosine"

    def test_local_mode_defaults_l2_when_no_hnsw_space(self) -> None:
        """verify_collection_deep defaults to 'l2' when hnsw:space absent in local mode."""
        from nexus.db.t3 import verify_collection_deep

        db = _local_db_with_ef()
        col_name = _unique("code__spacel2")
        client = db._client
        client.get_or_create_collection(col_name)  # no hnsw:space metadata
        col = client.get_collection(col_name)
        probe_id = "doc__l2_1"
        col.add(
            ids=[probe_id, "doc__l2_2"],
            documents=["alpha beta gamma delta epsilon", "foo bar baz qux quux"],
            metadatas=[{"src": "p"}, {"src": "q"}],
        )
        with patch.object(db, "search", side_effect=self._fake_search(probe_id)):
            result = verify_collection_deep(db, col_name)
        assert result.metric == "l2"


class TestConfigDefault:
    def test_hnsw_ef_default_is_256(self) -> None:
        """Config default for search.hnsw_ef is 256."""
        from nexus.config import load_config
        cfg = load_config()
        assert cfg.get("search", {}).get("hnsw_ef") == 256


class TestDoctorFixFlag:
    def test_doctor_fix_local_mode_outputs_updated_count(self) -> None:
        """doctor --fix in local mode shows updated collection count."""
        from click.testing import CliRunner
        from nexus.commands.doctor import doctor_cmd

        runner = CliRunner()
        with (
            patch("nexus.config.is_local_mode", return_value=True),
            patch("nexus.db.t3.apply_hnsw_ef", return_value=3),
            patch("nexus.db.t3.T3Database") as MockT3,
        ):
            MockT3.return_value = MagicMock()
            result = runner.invoke(doctor_cmd, ["--fix"])

        assert result.exit_code == 0
        assert "3" in result.output

    def test_doctor_fix_cloud_mode_skips_hnsw(self) -> None:
        """doctor --fix in cloud mode prints skip message and returns."""
        from click.testing import CliRunner
        from nexus.commands.doctor import doctor_cmd

        runner = CliRunner()
        with patch("nexus.config.is_local_mode", return_value=False):
            result = runner.invoke(doctor_cmd, ["--fix"])

        assert result.exit_code == 0
        assert "cloud" in result.output.lower() or "SPANN" in result.output
