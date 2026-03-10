"""Tests for nexus.exporter — collection export/import (RDR-031).

All tests use chromadb.EphemeralClient + DefaultEmbeddingFunction to avoid
requiring any real API keys or network access.
"""
from __future__ import annotations

import json
import gzip
from pathlib import Path
from typing import Generator

import chromadb
import msgpack
import numpy as np
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.db.t3 import T3Database
from nexus.errors import EmbeddingModelMismatch, FormatVersionError
from nexus.exporter import (
    FORMAT_VERSION,
    MAX_SUPPORTED_FORMAT_VERSION,
    _apply_filter,
    _apply_remap,
    export_collection,
    import_collection,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def ephemeral_db() -> Generator[T3Database, None, None]:
    """T3Database backed by an in-memory EphemeralClient — no API keys needed."""
    client = chromadb.EphemeralClient()
    ef = DefaultEmbeddingFunction()
    yield T3Database(_client=client, _ef_override=ef)


@pytest.fixture
def populated_db(ephemeral_db: T3Database, tmp_path: Path):
    """T3Database pre-populated with 5 records in code__test collection."""
    col = ephemeral_db.get_or_create_collection("code__test")
    ef = DefaultEmbeddingFunction()
    docs = [f"document {i}" for i in range(5)]
    ids = [f"id-{i:03d}" for i in range(5)]
    metadatas = [
        {
            "source_path": f"/repo/file_{i}.py",
            "title": f"File {i}",
            "indexed_at": "2026-03-01T00:00:00+00:00",
        }
        for i in range(5)
    ]
    embeddings = [ef([doc])[0] for doc in docs]
    col.upsert(ids=ids, documents=docs, embeddings=embeddings, metadatas=metadatas)
    return ephemeral_db


# ── Unit: filter helpers ───────────────────────────────────────────────────────


class TestApplyFilter:
    def test_no_filters_all_pass(self):
        assert _apply_filter("/repo/file.py", (), ()) is True

    def test_include_match(self):
        assert _apply_filter("/repo/main.py", ("*.py",), ()) is True

    def test_include_no_match(self):
        assert _apply_filter("/repo/main.go", ("*.py",), ()) is False

    def test_include_or_logic(self):
        """Multiple --include patterns use OR logic."""
        assert _apply_filter("/repo/main.py", ("*.py", "*.go"), ()) is True
        assert _apply_filter("/repo/main.go", ("*.py", "*.go"), ()) is True
        assert _apply_filter("/repo/main.rs", ("*.py", "*.go"), ()) is False

    def test_exclude_match(self):
        assert _apply_filter("/repo/test_foo.py", (), ("*/test_*",)) is False

    def test_exclude_no_match(self):
        assert _apply_filter("/repo/main.py", (), ("*/test_*",)) is True

    def test_none_source_path_passes_unconditionally(self):
        """Entries without source_path always pass (nx store put entries)."""
        assert _apply_filter(None, ("*.py",), ("*/test*",)) is True

    def test_include_and_exclude_combined(self):
        """Entry matching include but also exclude is excluded."""
        assert _apply_filter("/repo/test_main.py", ("*.py",), ("*/test_*",)) is False

    def test_include_only_entry_without_path_passes(self):
        """No source_path bypasses include filter."""
        assert _apply_filter(None, ("*.py",), ()) is True


class TestApplyRemap:
    def test_matching_prefix(self):
        result = _apply_remap("/old/path/file.py", [("/old/path", "/new/path")])
        assert result == "/new/path/file.py"

    def test_no_match(self):
        result = _apply_remap("/other/file.py", [("/old/path", "/new/path")])
        assert result == "/other/file.py"

    def test_first_match_wins(self):
        result = _apply_remap(
            "/prefix/file.py",
            [("/prefix", "/first"), ("/prefix", "/second")],
        )
        assert result == "/first/file.py"

    def test_empty_remaps(self):
        assert _apply_remap("/some/path", []) == "/some/path"


# ── Unit: round-trip ──────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_basic_round_trip(self, populated_db: T3Database, tmp_path: Path):
        """Export then import preserves documents, metadata, and embedding count."""
        out_file = tmp_path / "test.nxexp"

        export_result = export_collection(
            db=populated_db,
            collection_name="code__test",
            output_path=out_file,
        )
        assert export_result["exported_count"] == 5
        assert out_file.exists()
        assert out_file.stat().st_size > 0

        # Import into a new collection name.
        import_result = import_collection(
            db=populated_db,
            input_path=out_file,
            target_collection="code__restored",
        )
        assert import_result["imported_count"] == 5
        assert import_result["collection_name"] == "code__restored"

        # Verify the data made it in.
        restored_col = populated_db._client_for("code__restored").get_collection(
            "code__restored"
        )
        assert restored_col.count() == 5

    def test_round_trip_preserves_metadata(self, populated_db: T3Database, tmp_path: Path):
        """Import preserves all metadata fields from the export."""
        out_file = tmp_path / "meta_rt.nxexp"
        export_collection(
            db=populated_db,
            collection_name="code__test",
            output_path=out_file,
        )
        import_collection(
            db=populated_db,
            input_path=out_file,
            target_collection="code__meta_check",
        )
        col = populated_db._client_for("code__meta_check").get_collection("code__meta_check")
        result = col.get(include=["documents", "metadatas"])
        # Check a metadata field
        source_paths = {m["source_path"] for m in result["metadatas"]}
        assert "/repo/file_0.py" in source_paths
        assert "/repo/file_4.py" in source_paths

    def test_round_trip_preserves_embeddings(self, populated_db: T3Database, tmp_path: Path):
        """Embeddings are preserved byte-for-byte through export/import."""
        out_file = tmp_path / "emb_rt.nxexp"

        # Get original embeddings.
        orig_col = populated_db._client_for("code__test").get_collection("code__test")
        orig = orig_col.get(ids=["id-000"], include=["embeddings"])
        orig_emb = orig["embeddings"][0]

        export_collection(
            db=populated_db,
            collection_name="code__test",
            output_path=out_file,
        )
        import_collection(
            db=populated_db,
            input_path=out_file,
            target_collection="code__emb_check",
        )

        restored_col = populated_db._client_for("code__emb_check").get_collection(
            "code__emb_check"
        )
        restored = restored_col.get(ids=["id-000"], include=["embeddings"])
        restored_emb = restored["embeddings"][0]

        # Embeddings should match within float32 precision.
        np.testing.assert_allclose(orig_emb, restored_emb, rtol=1e-6)


# ── Unit: gzip compression ─────────────────────────────────────────────────────


class TestGzipCompression:
    def test_body_is_gzip_compressed(self, populated_db: T3Database, tmp_path: Path):
        """The body (after the JSON header line) is gzip-compressed."""
        out_file = tmp_path / "gz_test.nxexp"
        export_collection(
            db=populated_db,
            collection_name="code__test",
            output_path=out_file,
        )
        with open(out_file, "rb") as f:
            header_line = f.readline()
            body_start = f.read(2)
        # gzip magic bytes: 0x1f 0x8b
        assert body_start == b"\x1f\x8b", "Body should start with gzip magic bytes"

    def test_header_is_valid_json(self, populated_db: T3Database, tmp_path: Path):
        """The first line is valid JSON."""
        out_file = tmp_path / "hdr_test.nxexp"
        export_collection(
            db=populated_db,
            collection_name="code__test",
            output_path=out_file,
        )
        with open(out_file, "rb") as f:
            header_line = f.readline()
        header = json.loads(header_line.decode())
        assert header["format_version"] == FORMAT_VERSION
        assert header["collection_name"] == "code__test"
        assert header["record_count"] == 5
        assert "embedding_model" in header
        assert "exported_at" in header


# ── Unit: pagination ──────────────────────────────────────────────────────────


class TestPagination:
    def test_export_pagination_handles_large_collection(
        self, ephemeral_db: T3Database, tmp_path: Path
    ):
        """Export paginate correctly for collections larger than one page (300 records)."""
        from nexus.db.chroma_quotas import QUOTAS

        n = QUOTAS.MAX_RECORDS_PER_WRITE + 50  # 350 records
        col = ephemeral_db.get_or_create_collection("code__large")
        ef = DefaultEmbeddingFunction()
        docs = [f"doc {i}" for i in range(n)]
        ids = [f"id-{i:04d}" for i in range(n)]
        metadatas = [{"source_path": f"/f{i}.py"} for i in range(n)]
        embeddings = [ef([d])[0] for d in docs]
        # Insert in two batches to avoid ChromaDB limit.
        mid = n // 2
        col.upsert(
            ids=ids[:mid],
            documents=docs[:mid],
            embeddings=embeddings[:mid],
            metadatas=metadatas[:mid],
        )
        col.upsert(
            ids=ids[mid:],
            documents=docs[mid:],
            embeddings=embeddings[mid:],
            metadatas=metadatas[mid:],
        )

        out_file = tmp_path / "large.nxexp"
        result = export_collection(
            db=ephemeral_db,
            collection_name="code__large",
            output_path=out_file,
        )
        assert result["exported_count"] == n

    def test_import_pagination_handles_large_export(
        self, ephemeral_db: T3Database, tmp_path: Path
    ):
        """Import paginates correctly for exports with > 300 records."""
        from nexus.db.chroma_quotas import QUOTAS

        n = QUOTAS.MAX_RECORDS_PER_WRITE + 50  # 350 records
        col = ephemeral_db.get_or_create_collection("code__big_src")
        ef = DefaultEmbeddingFunction()
        docs = [f"doc {i}" for i in range(n)]
        ids = [f"big-{i:04d}" for i in range(n)]
        metadatas = [{"source_path": f"/big{i}.py"} for i in range(n)]
        embeddings = [ef([d])[0] for d in docs]
        mid = n // 2
        col.upsert(
            ids=ids[:mid],
            documents=docs[:mid],
            embeddings=embeddings[:mid],
            metadatas=metadatas[:mid],
        )
        col.upsert(
            ids=ids[mid:],
            documents=docs[mid:],
            embeddings=embeddings[mid:],
            metadatas=metadatas[mid:],
        )

        out_file = tmp_path / "big.nxexp"
        export_collection(
            db=ephemeral_db,
            collection_name="code__big_src",
            output_path=out_file,
        )
        result = import_collection(
            db=ephemeral_db,
            input_path=out_file,
            target_collection="code__big_dst",
        )
        assert result["imported_count"] == n

        dst_col = ephemeral_db._client_for("code__big_dst").get_collection("code__big_dst")
        assert dst_col.count() == n


# ── Unit: path remapping ───────────────────────────────────────────────────────


class TestPathRemapping:
    def test_remap_transforms_source_path_on_import(
        self, populated_db: T3Database, tmp_path: Path
    ):
        """--remap applies the prefix substitution to source_path metadata."""
        out_file = tmp_path / "remap.nxexp"
        export_collection(
            db=populated_db,
            collection_name="code__test",
            output_path=out_file,
        )
        import_collection(
            db=populated_db,
            input_path=out_file,
            target_collection="code__remapped",
            remaps=[("/repo", "/new_root")],
        )
        col = populated_db._client_for("code__remapped").get_collection("code__remapped")
        result = col.get(include=["metadatas"])
        paths = {m["source_path"] for m in result["metadatas"]}
        assert all(p.startswith("/new_root/") for p in paths), (
            f"Expected all paths to start with /new_root/, got: {paths}"
        )

    def test_remap_no_match_leaves_path_unchanged(
        self, populated_db: T3Database, tmp_path: Path
    ):
        """A remap that doesn't match leaves source_path unchanged."""
        out_file = tmp_path / "no_remap.nxexp"
        export_collection(
            db=populated_db,
            collection_name="code__test",
            output_path=out_file,
        )
        import_collection(
            db=populated_db,
            input_path=out_file,
            target_collection="code__no_remap",
            remaps=[("/nonexistent", "/other")],
        )
        col = populated_db._client_for("code__no_remap").get_collection("code__no_remap")
        result = col.get(include=["metadatas"])
        paths = {m["source_path"] for m in result["metadatas"]}
        assert all(p.startswith("/repo/") for p in paths)


# ── Unit: embedding model validation ─────────────────────────────────────────


class TestEmbeddingModelValidation:
    def test_code_to_code_same_model_succeeds(
        self, populated_db: T3Database, tmp_path: Path
    ):
        """Importing a code__ export into a code__ collection succeeds."""
        out_file = tmp_path / "same_model.nxexp"
        export_collection(
            db=populated_db,
            collection_name="code__test",
            output_path=out_file,
        )
        # Should not raise.
        result = import_collection(
            db=populated_db,
            input_path=out_file,
            target_collection="code__compat",
        )
        assert result["imported_count"] == 5

    def test_code_export_into_docs_collection_raises(
        self, populated_db: T3Database, tmp_path: Path
    ):
        """Importing a code__ (voyage-code-3) export into docs__ (voyage-context-3) MUST fail."""
        out_file = tmp_path / "mismatch.nxexp"
        export_collection(
            db=populated_db,
            collection_name="code__test",
            output_path=out_file,
        )
        with pytest.raises(EmbeddingModelMismatch) as exc_info:
            import_collection(
                db=populated_db,
                input_path=out_file,
                target_collection="docs__corpus",
            )
        assert "voyage-code-3" in str(exc_info.value)
        assert "voyage-context-3" in str(exc_info.value)
        assert "docs__corpus" in str(exc_info.value)

    def test_code_export_into_knowledge_collection_raises(
        self, populated_db: T3Database, tmp_path: Path
    ):
        """Importing a code__ export into knowledge__ MUST fail."""
        out_file = tmp_path / "mismatch_knowledge.nxexp"
        export_collection(
            db=populated_db,
            collection_name="code__test",
            output_path=out_file,
        )
        with pytest.raises(EmbeddingModelMismatch):
            import_collection(
                db=populated_db,
                input_path=out_file,
                target_collection="knowledge__myknowledge",
            )

    def test_code_export_into_rdr_collection_raises(
        self, populated_db: T3Database, tmp_path: Path
    ):
        """Importing a code__ export into rdr__ MUST fail."""
        out_file = tmp_path / "mismatch_rdr.nxexp"
        export_collection(
            db=populated_db,
            collection_name="code__test",
            output_path=out_file,
        )
        with pytest.raises(EmbeddingModelMismatch):
            import_collection(
                db=populated_db,
                input_path=out_file,
                target_collection="rdr__decisions",
            )

    def test_knowledge_export_into_knowledge_succeeds(
        self, ephemeral_db: T3Database, tmp_path: Path
    ):
        """knowledge__ to knowledge__ import succeeds (same model: voyage-context-3)."""
        ef = DefaultEmbeddingFunction()
        col = ephemeral_db.get_or_create_collection("knowledge__src")
        docs = ["doc a", "doc b"]
        ids = ["ka-001", "ka-002"]
        metas = [{"title": "A"}, {"title": "B"}]
        embeddings = [ef([d])[0] for d in docs]
        col.upsert(ids=ids, documents=docs, embeddings=embeddings, metadatas=metas)

        out_file = tmp_path / "k_to_k.nxexp"
        export_collection(
            db=ephemeral_db,
            collection_name="knowledge__src",
            output_path=out_file,
        )
        result = import_collection(
            db=ephemeral_db,
            input_path=out_file,
            target_collection="knowledge__dst",
        )
        assert result["imported_count"] == 2


# ── Unit: format version validation ──────────────────────────────────────────


class TestFormatVersionValidation:
    def test_current_version_accepted(self, populated_db: T3Database, tmp_path: Path):
        """Current format version (1) is accepted without error."""
        out_file = tmp_path / "v1.nxexp"
        export_collection(
            db=populated_db,
            collection_name="code__test",
            output_path=out_file,
        )
        # Verify the header has format_version = FORMAT_VERSION.
        with open(out_file, "rb") as f:
            header = json.loads(f.readline().decode())
        assert header["format_version"] == FORMAT_VERSION

        # Import should succeed.
        result = import_collection(
            db=populated_db,
            input_path=out_file,
            target_collection="code__v1_dst",
        )
        assert result["imported_count"] == 5

    def test_future_version_raises(self, populated_db: T3Database, tmp_path: Path):
        """A file with format_version > MAX_SUPPORTED_FORMAT_VERSION MUST abort."""
        out_file = tmp_path / "future.nxexp"
        export_collection(
            db=populated_db,
            collection_name="code__test",
            output_path=out_file,
        )

        # Tamper with the header to simulate a future format version.
        with open(out_file, "rb") as f:
            header_line = f.readline()
            rest = f.read()

        header = json.loads(header_line.decode())
        header["format_version"] = MAX_SUPPORTED_FORMAT_VERSION + 1
        new_header_line = json.dumps(header).encode() + b"\n"

        future_file = tmp_path / "future_tampered.nxexp"
        with open(future_file, "wb") as f:
            f.write(new_header_line)
            f.write(rest)

        with pytest.raises(FormatVersionError) as exc_info:
            import_collection(
                db=populated_db,
                input_path=future_file,
                target_collection="code__future_dst",
            )
        assert "format_version" in str(exc_info.value).lower()
        assert "upgrade" in str(exc_info.value).lower()


# ── Unit: include/exclude filters ────────────────────────────────────────────


class TestIncludeExcludeFilters:
    def _make_db_with_mixed_paths(self, ephemeral_db: T3Database) -> T3Database:
        """Populate a collection with files at varying paths."""
        ef = DefaultEmbeddingFunction()
        col = ephemeral_db.get_or_create_collection("code__filter_test")
        docs = [
            "python file content",
            "go file content",
            "test python file",
            "no-path entry",
        ]
        ids = ["py-001", "go-001", "test-001", "nopath-001"]
        metadatas = [
            {"source_path": "/repo/main.py"},
            {"source_path": "/repo/main.go"},
            {"source_path": "/repo/test_main.py"},
            {"title": "store put entry"},  # no source_path
        ]
        embeddings = [ef([d])[0] for d in docs]
        col.upsert(ids=ids, documents=docs, embeddings=embeddings, metadatas=metadatas)
        return ephemeral_db

    def test_include_filters_by_source_path(
        self, ephemeral_db: T3Database, tmp_path: Path
    ):
        """--include *.py exports only .py files (plus entries without source_path)."""
        self._make_db_with_mixed_paths(ephemeral_db)
        out_file = tmp_path / "py_only.nxexp"
        result = export_collection(
            db=ephemeral_db,
            collection_name="code__filter_test",
            output_path=out_file,
            includes=("*.py",),
        )
        # 3 .py files + 1 no-path entry = 4 expected... but test_main.py also matches *.py
        # py-001 (/repo/main.py), test-001 (/repo/test_main.py), nopath-001 (no path)
        assert result["exported_count"] == 3  # main.py, test_main.py, no-path

    def test_exclude_filters_out_matching_paths(
        self, ephemeral_db: T3Database, tmp_path: Path
    ):
        """--exclude */test_* excludes test files but keeps entries without source_path."""
        self._make_db_with_mixed_paths(ephemeral_db)
        out_file = tmp_path / "no_tests.nxexp"
        result = export_collection(
            db=ephemeral_db,
            collection_name="code__filter_test",
            output_path=out_file,
            excludes=("*/test_*",),
        )
        # main.py, main.go, no-path = 3
        assert result["exported_count"] == 3

    def test_no_source_path_always_included(
        self, ephemeral_db: T3Database, tmp_path: Path
    ):
        """Entries without source_path pass through both include and exclude filters."""
        self._make_db_with_mixed_paths(ephemeral_db)
        out_file = tmp_path / "strict.nxexp"
        result = export_collection(
            db=ephemeral_db,
            collection_name="code__filter_test",
            output_path=out_file,
            includes=("*.go",),       # only .go files + no-path
            excludes=("*/main*",),   # but main.go is excluded
        )
        # Only no-path entry survives (main.go matched include but also excluded)
        assert result["exported_count"] == 1

    def test_include_and_exclude_combined(
        self, ephemeral_db: T3Database, tmp_path: Path
    ):
        """Combined include + exclude: include *.py, exclude */test_*."""
        self._make_db_with_mixed_paths(ephemeral_db)
        out_file = tmp_path / "py_no_tests.nxexp"
        result = export_collection(
            db=ephemeral_db,
            collection_name="code__filter_test",
            output_path=out_file,
            includes=("*.py",),
            excludes=("*/test_*",),
        )
        # main.py (passes include, not excluded) + no-path = 2
        assert result["exported_count"] == 2


# ── Unit: --all semantics (via CLI layer) ─────────────────────────────────────


class TestExportAll:
    def test_export_all_produces_one_file_per_collection(
        self, ephemeral_db: T3Database, tmp_path: Path
    ):
        """export_collection can be called per-collection to simulate --all."""
        from datetime import date

        ef = DefaultEmbeddingFunction()
        for prefix in ("code__alpha", "knowledge__beta"):
            col = ephemeral_db.get_or_create_collection(prefix)
            docs = ["doc a", "doc b"]
            ids = [f"{prefix}-001", f"{prefix}-002"]
            metas = [{"title": "A"}, {"title": "B"}]
            embs = [ef([d])[0] for d in docs]
            col.upsert(ids=ids, documents=docs, embeddings=embs, metadatas=metas)

        today = date.today().isoformat()
        collections = ["code__alpha", "knowledge__beta"]
        files: list[Path] = []
        for col_name in collections:
            fname = f"{col_name}-{today}.nxexp"
            out_path = tmp_path / fname
            export_collection(
                db=ephemeral_db,
                collection_name=col_name,
                output_path=out_path,
            )
            files.append(out_path)

        assert len(files) == 2
        for f in files:
            assert f.exists()
            # Verify each file has the correct collection in header.
            with open(f, "rb") as fh:
                header = json.loads(fh.readline().decode())
            # The filename is {collection_name}-{YYYY-MM-DD}.nxexp
            # Strip the .nxexp suffix, then strip the trailing -{YYYY-MM-DD}
            stem = f.stem  # e.g. "code__alpha-2026-03-09"
            # Date is the last 10 chars of the stem (YYYY-MM-DD)
            expected_col = stem[:-11]  # remove "-YYYY-MM-DD" (11 chars)
            assert header["collection_name"] == expected_col

        # Verify naming convention: {collection_name}-{date}.nxexp
        names = [f.name for f in files]
        assert f"code__alpha-{today}.nxexp" in names
        assert f"knowledge__beta-{today}.nxexp" in names


# ── Unit: CLI layer tests ─────────────────────────────────────────────────────


class TestExportImportCLI:
    """CLI-layer tests using CliRunner and patched _t3."""

    @pytest.fixture
    def runner(self):
        from click.testing import CliRunner
        return CliRunner()

    @pytest.fixture
    def env_creds(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CHROMA_API_KEY", "test-chroma-key")
        monkeypatch.setenv("VOYAGE_API_KEY", "test-voyage-key")
        monkeypatch.setenv("CHROMA_TENANT", "test-tenant")
        monkeypatch.setenv("CHROMA_DATABASE", "test-db")

    def test_export_missing_collection_and_all(
        self, runner, env_creds, tmp_path
    ):
        """Providing neither COLLECTION nor --all fails with UsageError."""
        from unittest.mock import MagicMock, patch
        from nexus.cli import main

        mock_db = MagicMock()
        with patch("nexus.commands.store._t3", return_value=mock_db):
            result = runner.invoke(main, ["store", "export"])
        assert result.exit_code != 0

    def test_export_collection_and_all_exclusive(
        self, runner, env_creds, tmp_path
    ):
        """Providing both COLLECTION and --all fails with UsageError."""
        from unittest.mock import MagicMock, patch
        from nexus.cli import main

        mock_db = MagicMock()
        with patch("nexus.commands.store._t3", return_value=mock_db):
            result = runner.invoke(
                main,
                ["store", "export", "code__test", "--all"],
            )
        assert result.exit_code != 0

    def test_import_remap_bad_format(self, runner, env_creds, tmp_path):
        """--remap without colon separator fails with UsageError."""
        from unittest.mock import MagicMock, patch
        from nexus.cli import main

        # Create a dummy .nxexp file so the click.Path(exists=True) check passes.
        dummy = tmp_path / "dummy.nxexp"
        dummy.write_bytes(b"not a real file")

        mock_db = MagicMock()
        with patch("nexus.commands.store._t3", return_value=mock_db):
            result = runner.invoke(
                main,
                ["store", "import", str(dummy), "--remap", "no_colon_here"],
            )
        assert result.exit_code != 0
        assert "remap" in result.output.lower() or "colon" in result.output.lower() or result.exit_code != 0

    def test_export_single_collection_success(
        self, runner, env_creds, tmp_path, populated_db: T3Database
    ):
        """CLI export command succeeds for a single collection."""
        from unittest.mock import patch
        from nexus.cli import main

        out_file = tmp_path / "out.nxexp"
        with patch("nexus.commands.store._t3", return_value=populated_db):
            result = runner.invoke(
                main,
                ["store", "export", "code__test", "-o", str(out_file)],
            )
        assert result.exit_code == 0, result.output
        assert out_file.exists()
        assert "5" in result.output

    def test_import_embedding_mismatch_shows_error(
        self, runner, env_creds, tmp_path, populated_db: T3Database
    ):
        """CLI import shows error on EmbeddingModelMismatch."""
        from unittest.mock import patch
        from nexus.cli import main

        # First export the code__ collection.
        out_file = tmp_path / "code_export.nxexp"
        export_collection(
            db=populated_db,
            collection_name="code__test",
            output_path=out_file,
        )

        # Now try to import into docs__ via CLI — must fail.
        with patch("nexus.commands.store._t3", return_value=populated_db):
            result = runner.invoke(
                main,
                [
                    "store",
                    "import",
                    str(out_file),
                    "--collection",
                    "docs__corpus",
                ],
            )
        assert result.exit_code != 0
        output = result.output.lower()
        assert "mismatch" in output or "error" in output

    def test_export_all_produces_files(
        self, runner, env_creds, tmp_path, ephemeral_db: T3Database
    ):
        """--all exports one file per collection into the output directory."""
        from unittest.mock import patch
        from nexus.cli import main

        # Seed two collections.
        ef = DefaultEmbeddingFunction()
        for name in ("code__repo1", "knowledge__notes"):
            col = ephemeral_db.get_or_create_collection(name)
            embs = [ef(["hello"])[0]]
            col.upsert(ids=[f"{name}-001"], documents=["hello"], embeddings=embs, metadatas=[{"title": "t"}])

        ephemeral_db.list_collections = lambda: [
            {"name": "code__repo1", "count": 1},
            {"name": "knowledge__notes", "count": 1},
        ]

        out_dir = tmp_path / "exports"
        out_dir.mkdir()
        with patch("nexus.commands.store._t3", return_value=ephemeral_db):
            result = runner.invoke(
                main,
                ["store", "export", "--all", "-o", str(out_dir)],
            )
        assert result.exit_code == 0, result.output
        nxexp_files = list(out_dir.glob("*.nxexp"))
        assert len(nxexp_files) == 2


# ── Unit: error types ─────────────────────────────────────────────────────────


class TestErrorTypes:
    def test_embedding_model_mismatch_importable(self):
        from nexus.errors import EmbeddingModelMismatch  # noqa: F401

    def test_format_version_error_importable(self):
        from nexus.errors import FormatVersionError  # noqa: F401

    def test_both_are_nexus_error_subclasses(self):
        from nexus.errors import EmbeddingModelMismatch, FormatVersionError, NexusError
        assert issubclass(EmbeddingModelMismatch, NexusError)
        assert issubclass(FormatVersionError, NexusError)

    def test_embedding_model_mismatch_message(self):
        exc = EmbeddingModelMismatch("test message")
        assert "test message" in str(exc)

    def test_format_version_error_message(self):
        exc = FormatVersionError("version error message")
        assert "version error message" in str(exc)
