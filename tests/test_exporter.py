# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Generator

import chromadb
import numpy as np
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.db.t3 import T3Database
from nexus.errors import EmbeddingModelMismatch, FormatVersionError, NexusError
from nexus.exporter import (
    FORMAT_VERSION,
    MAX_SUPPORTED_FORMAT_VERSION,
    _apply_filter,
    _apply_remap,
    export_collection,
    import_collection,
)

_EF = DefaultEmbeddingFunction()


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def ephemeral_db() -> Generator[T3Database, None, None]:
    yield T3Database(_client=chromadb.EphemeralClient(), _ef_override=_EF)


@pytest.fixture
def populated_db(ephemeral_db: T3Database):
    col = ephemeral_db.get_or_create_collection("code__test")
    docs = [f"document {i}" for i in range(5)]
    ids = [f"id-{i:03d}" for i in range(5)]
    metadatas = [
        {"source_path": f"/repo/file_{i}.py", "title": f"File {i}",
         "indexed_at": "2026-03-01T00:00:00+00:00"}
        for i in range(5)
    ]
    embeddings = [_EF([doc])[0] for doc in docs]
    col.upsert(ids=ids, documents=docs, embeddings=embeddings, metadatas=metadatas)
    return ephemeral_db


def _export(db, col_name, tmp_path, fname="out.nxexp", **kwargs):
    out = tmp_path / fname
    result = export_collection(db=db, collection_name=col_name, output_path=out, **kwargs)
    return out, result


def _export_import(db, src_col, tmp_path, target=None, fname="rt.nxexp", **kwargs):
    out, export_result = _export(db, src_col, tmp_path, fname)
    import_result = import_collection(db=db, input_path=out, target_collection=target, **kwargs)
    return out, export_result, import_result


def _seed_collection(db, name, docs, ids, metadatas):
    col = db.get_or_create_collection(name)
    embeddings = [_EF([d])[0] for d in docs]
    col.upsert(ids=ids, documents=docs, embeddings=embeddings, metadatas=metadatas)
    return col


# ── Unit: filter helpers ──────────────────────────────────────────────────────


class TestApplyFilter:
    @pytest.mark.parametrize("path,includes,excludes,expected", [
        ("/repo/file.py", (), (), True),
        ("/repo/main.py", ("*.py",), (), True),
        ("/repo/main.go", ("*.py",), (), False),
        ("/repo/test_foo.py", (), ("*/test_*",), False),
        ("/repo/main.py", (), ("*/test_*",), True),
        (None, ("*.py",), ("*/test*",), True),
        ("/repo/test_main.py", ("*.py",), ("*/test_*",), False),
        (None, ("*.py",), (), True),
    ])
    def test_filter(self, path, includes, excludes, expected):
        assert _apply_filter(path, includes, excludes) is expected

    @pytest.mark.parametrize("path,includes,excludes,expected", [
        ("/repo/main.py", ("*.py", "*.go"), (), True),
        ("/repo/main.go", ("*.py", "*.go"), (), True),
        ("/repo/main.rs", ("*.py", "*.go"), (), False),
    ])
    def test_include_or_logic(self, path, includes, excludes, expected):
        assert _apply_filter(path, includes, excludes) is expected


class TestApplyRemap:
    @pytest.mark.parametrize("path,remaps,expected", [
        ("/old/path/file.py", [("/old/path", "/new/path")], "/new/path/file.py"),
        ("/other/file.py", [("/old/path", "/new/path")], "/other/file.py"),
        ("/prefix/file.py", [("/prefix", "/first"), ("/prefix", "/second")], "/first/file.py"),
        ("/some/path", [], "/some/path"),
    ])
    def test_remap(self, path, remaps, expected):
        assert _apply_remap(path, remaps) == expected


# ── Unit: round-trip ─────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_basic_round_trip(self, populated_db: T3Database, tmp_path: Path):
        out, export_result, import_result = _export_import(
            populated_db, "code__test", tmp_path, target="code__restored",
        )
        assert export_result["exported_count"] == 5
        assert out.exists() and out.stat().st_size > 0
        assert import_result["imported_count"] == 5
        assert import_result["collection_name"] == "code__restored"
        restored = populated_db._client_for("code__restored").get_collection("code__restored")
        assert restored.count() == 5

    def test_round_trip_preserves_metadata(self, populated_db: T3Database, tmp_path: Path):
        _export_import(populated_db, "code__test", tmp_path, target="code__meta_check")
        col = populated_db._client_for("code__meta_check").get_collection("code__meta_check")
        paths = {m["source_path"] for m in col.get(include=["metadatas"])["metadatas"]}
        assert "/repo/file_0.py" in paths and "/repo/file_4.py" in paths

    def test_round_trip_preserves_embeddings(self, populated_db: T3Database, tmp_path: Path):
        orig_col = populated_db._client_for("code__test").get_collection("code__test")
        orig_emb = orig_col.get(ids=["id-000"], include=["embeddings"])["embeddings"][0]

        _export_import(populated_db, "code__test", tmp_path, target="code__emb_check")
        restored_col = populated_db._client_for("code__emb_check").get_collection("code__emb_check")
        restored_emb = restored_col.get(ids=["id-000"], include=["embeddings"])["embeddings"][0]
        np.testing.assert_allclose(orig_emb, restored_emb, rtol=1e-6)


# ── Unit: gzip compression ───────────────────────────────────────────────────


class TestGzipCompression:
    def test_file_format(self, populated_db: T3Database, tmp_path: Path):
        out, _ = _export(populated_db, "code__test", tmp_path)
        with open(out, "rb") as f:
            header_line = f.readline()
            body_start = f.read(2)
        # Header is valid JSON
        header = json.loads(header_line.decode())
        assert header["format_version"] == FORMAT_VERSION
        assert header["collection_name"] == "code__test"
        assert header["record_count"] == 5
        assert "embedding_model" in header
        assert "exported_at" in header
        # Body is gzip
        assert body_start == b"\x1f\x8b"


# ── Unit: pagination ─────────────────────────────────────────────────────────


class TestPagination:
    def _seed_large(self, db, col_name, prefix):
        from nexus.db.chroma_quotas import QUOTAS
        n = QUOTAS.MAX_RECORDS_PER_WRITE + 50
        col = db.get_or_create_collection(col_name)
        docs = [f"doc {i}" for i in range(n)]
        ids = [f"{prefix}-{i:04d}" for i in range(n)]
        metadatas = [{"source_path": f"/f{i}.py"} for i in range(n)]
        embeddings = [_EF([d])[0] for d in docs]
        mid = n // 2
        col.upsert(ids=ids[:mid], documents=docs[:mid],
                    embeddings=embeddings[:mid], metadatas=metadatas[:mid])
        col.upsert(ids=ids[mid:], documents=docs[mid:],
                    embeddings=embeddings[mid:], metadatas=metadatas[mid:])
        return n

    def test_export_pagination(self, ephemeral_db: T3Database, tmp_path: Path):
        n = self._seed_large(ephemeral_db, "code__large", "id")
        _, result = _export(ephemeral_db, "code__large", tmp_path)
        assert result["exported_count"] == n

    def test_import_pagination(self, ephemeral_db: T3Database, tmp_path: Path):
        n = self._seed_large(ephemeral_db, "code__big_src", "big")
        _, _, import_result = _export_import(
            ephemeral_db, "code__big_src", tmp_path, target="code__big_dst",
        )
        assert import_result["imported_count"] == n
        dst = ephemeral_db._client_for("code__big_dst").get_collection("code__big_dst")
        assert dst.count() == n


# ── Unit: path remapping ─────────────────────────────────────────────────────


class TestPathRemapping:
    @pytest.mark.parametrize("remaps,prefix_check", [
        ([("/repo", "/new_root")], "/new_root/"),
        ([("/nonexistent", "/other")], "/repo/"),
    ])
    def test_remap_on_import(self, populated_db: T3Database, tmp_path: Path, remaps, prefix_check):
        target = f"code__remap_{prefix_check.strip('/')}"
        out, _ = _export(populated_db, "code__test", tmp_path, fname=f"{target}.nxexp")
        import_collection(db=populated_db, input_path=out, target_collection=target, remaps=remaps)
        col = populated_db._client_for(target).get_collection(target)
        paths = {m["source_path"] for m in col.get(include=["metadatas"])["metadatas"]}
        assert all(p.startswith(prefix_check) for p in paths)


# ── Unit: embedding model validation ─────────────────────────────────────────


class TestEmbeddingModelValidation:
    def test_same_model_succeeds(self, populated_db: T3Database, tmp_path: Path):
        _, _, result = _export_import(
            populated_db, "code__test", tmp_path, target="code__compat",
        )
        assert result["imported_count"] == 5

    @pytest.mark.parametrize("target", [
        "docs__corpus", "knowledge__myknowledge", "rdr__decisions",
    ])
    def test_code_into_incompatible_raises(self, populated_db: T3Database, tmp_path: Path, target):
        out, _ = _export(populated_db, "code__test", tmp_path, fname=f"{target}.nxexp")
        with pytest.raises(EmbeddingModelMismatch):
            import_collection(db=populated_db, input_path=out, target_collection=target)

    def test_code_into_docs_error_detail(self, populated_db: T3Database, tmp_path: Path):
        out, _ = _export(populated_db, "code__test", tmp_path)
        with pytest.raises(EmbeddingModelMismatch) as exc_info:
            import_collection(db=populated_db, input_path=out, target_collection="docs__corpus")
        msg = str(exc_info.value)
        assert "voyage-code-3" in msg and "voyage-context-3" in msg and "docs__corpus" in msg

    def test_knowledge_to_knowledge_succeeds(self, ephemeral_db: T3Database, tmp_path: Path):
        _seed_collection(ephemeral_db, "knowledge__src",
                         ["doc a", "doc b"], ["ka-001", "ka-002"],
                         [{"title": "A"}, {"title": "B"}])
        _, _, result = _export_import(
            ephemeral_db, "knowledge__src", tmp_path, target="knowledge__dst",
        )
        assert result["imported_count"] == 2


# ── Unit: format version validation ──────────────────────────────────────────


class TestFormatVersionValidation:
    def test_current_version_accepted(self, populated_db: T3Database, tmp_path: Path):
        out, _ = _export(populated_db, "code__test", tmp_path)
        with open(out, "rb") as f:
            header = json.loads(f.readline().decode())
        assert header["format_version"] == FORMAT_VERSION
        _, _, result = _export_import(populated_db, "code__test", tmp_path,
                                      target="code__v1_dst", fname="v1.nxexp")
        assert result["imported_count"] == 5

    def test_future_version_raises(self, populated_db: T3Database, tmp_path: Path):
        out, _ = _export(populated_db, "code__test", tmp_path)
        with open(out, "rb") as f:
            header_line = f.readline()
            rest = f.read()
        header = json.loads(header_line.decode())
        header["format_version"] = MAX_SUPPORTED_FORMAT_VERSION + 1
        future_file = tmp_path / "future.nxexp"
        with open(future_file, "wb") as f:
            f.write(json.dumps(header).encode() + b"\n")
            f.write(rest)
        with pytest.raises(FormatVersionError) as exc_info:
            import_collection(db=populated_db, input_path=future_file,
                              target_collection="code__future_dst")
        msg = str(exc_info.value).lower()
        assert "format_version" in msg and "upgrade" in msg


# ── Unit: include/exclude filters ────────────────────────────────────────────


class TestIncludeExcludeFilters:
    @pytest.fixture
    def filter_db(self, ephemeral_db: T3Database):
        _seed_collection(
            ephemeral_db, "code__filter_test",
            ["python file content", "go file content", "test python file", "no-path entry"],
            ["py-001", "go-001", "test-001", "nopath-001"],
            [{"source_path": "/repo/main.py"}, {"source_path": "/repo/main.go"},
             {"source_path": "/repo/test_main.py"}, {"title": "store put entry"}],
        )
        return ephemeral_db

    @pytest.mark.parametrize("includes,excludes,expected_count", [
        (("*.py",), (), 3),           # main.py, test_main.py, no-path
        ((), ("*/test_*",), 3),        # main.py, main.go, no-path
        (("*.go",), ("*/main*",), 1),  # only no-path survives
        (("*.py",), ("*/test_*",), 2), # main.py + no-path
    ])
    def test_filter_export(self, filter_db, tmp_path, includes, excludes, expected_count):
        _, result = _export(filter_db, "code__filter_test", tmp_path,
                            fname=f"f_{expected_count}.nxexp",
                            includes=includes, excludes=excludes)
        assert result["exported_count"] == expected_count


# ── Unit: --all semantics ────────────────────────────────────────────────────


class TestExportAll:
    def test_export_all_produces_one_file_per_collection(
        self, ephemeral_db: T3Database, tmp_path: Path,
    ):
        from datetime import date
        for name in ("code__alpha", "knowledge__beta"):
            _seed_collection(ephemeral_db, name, ["doc a", "doc b"],
                             [f"{name}-001", f"{name}-002"],
                             [{"title": "A"}, {"title": "B"}])
        today = date.today().isoformat()
        files = []
        for col_name in ("code__alpha", "knowledge__beta"):
            out_path = tmp_path / f"{col_name}-{today}.nxexp"
            export_collection(db=ephemeral_db, collection_name=col_name, output_path=out_path)
            files.append(out_path)

        assert len(files) == 2
        for f in files:
            assert f.exists()
            with open(f, "rb") as fh:
                header = json.loads(fh.readline().decode())
            expected_col = f.stem[:-11]  # strip "-YYYY-MM-DD"
            assert header["collection_name"] == expected_col
        names = [f.name for f in files]
        assert f"code__alpha-{today}.nxexp" in names
        assert f"knowledge__beta-{today}.nxexp" in names


# ── Unit: CLI layer tests ────────────────────────────────────────────────────


class TestExportImportCLI:
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

    @pytest.mark.parametrize("args", [
        ["store", "export"],
        ["store", "export", "code__test", "--all"],
    ])
    def test_export_mutual_exclusion(self, runner, env_creds, args):
        from unittest.mock import MagicMock, patch
        from nexus.cli import main
        with patch("nexus.commands.store._t3", return_value=MagicMock()):
            result = runner.invoke(main, args)
        assert result.exit_code != 0

    @pytest.mark.parametrize("remap_val,check_word", [
        ("no_colon_here", "remap"),
        (":/new/path", "remap"),
    ])
    def test_import_remap_bad_format(self, runner, env_creds, tmp_path, remap_val, check_word):
        from unittest.mock import MagicMock, patch
        from nexus.cli import main
        dummy = tmp_path / "dummy.nxexp"
        dummy.write_bytes(b"not a real file")
        with patch("nexus.commands.store._t3", return_value=MagicMock()):
            result = runner.invoke(main, ["store", "import", str(dummy), "--remap", remap_val])
        assert result.exit_code != 0
        output = result.output.lower()
        assert check_word in output or "colon" in output or "empty" in output

    def test_export_single_collection_success(
        self, runner, env_creds, tmp_path, populated_db: T3Database,
    ):
        from unittest.mock import patch
        from nexus.cli import main
        out_file = tmp_path / "out.nxexp"
        with patch("nexus.commands.store._t3", return_value=populated_db):
            result = runner.invoke(main, ["store", "export", "code__test", "-o", str(out_file)])
        assert result.exit_code == 0
        assert out_file.exists() and "5" in result.output

    def test_import_embedding_mismatch_shows_error(
        self, runner, env_creds, tmp_path, populated_db: T3Database,
    ):
        from unittest.mock import patch
        from nexus.cli import main
        out, _ = _export(populated_db, "code__test", tmp_path)
        with patch("nexus.commands.store._t3", return_value=populated_db):
            result = runner.invoke(main, ["store", "import", str(out), "--collection", "docs__corpus"])
        assert result.exit_code != 0
        assert "mismatch" in result.output.lower() or "error" in result.output.lower()

    def test_export_all_produces_files(
        self, runner, env_creds, tmp_path, ephemeral_db: T3Database,
    ):
        from unittest.mock import patch
        from nexus.cli import main
        for name in ("code__repo1", "knowledge__notes"):
            _seed_collection(ephemeral_db, name, ["hello"], [f"{name}-001"], [{"title": "t"}])
        ephemeral_db.list_collections = lambda: [
            {"name": "code__repo1", "count": 1},
            {"name": "knowledge__notes", "count": 1},
        ]
        out_dir = tmp_path / "exports"
        out_dir.mkdir()
        with patch("nexus.commands.store._t3", return_value=ephemeral_db):
            result = runner.invoke(main, ["store", "export", "--all", "-o", str(out_dir)])
        assert result.exit_code == 0
        assert len(list(out_dir.glob("*.nxexp"))) == 2


# ── Unit: error types ────────────────────────────────────────────────────────


class TestErrorTypes:
    @pytest.mark.parametrize("cls", [EmbeddingModelMismatch, FormatVersionError])
    def test_importable_and_is_nexus_error(self, cls):
        assert issubclass(cls, NexusError)

    @pytest.mark.parametrize("cls,msg", [
        (EmbeddingModelMismatch, "test message"),
        (FormatVersionError, "version error message"),
    ])
    def test_error_message(self, cls, msg):
        assert msg in str(cls(msg))


# ── Empty collection round-trip ──────────────────────────────────────────────


class TestEmptyCollectionRoundTrip:
    def test_empty_export_and_import(self, ephemeral_db: T3Database, tmp_path: Path):
        ephemeral_db.get_or_create_collection("knowledge__empty")
        out, stats = _export(ephemeral_db, "knowledge__empty", tmp_path)
        assert stats["exported_count"] == 0 and out.exists()
        with open(out, "rb") as f:
            header = json.loads(f.readline().decode())
        assert header["collection_name"] == "knowledge__empty"
        result = import_collection(ephemeral_db, out)
        assert result["imported_count"] == 0


# ── Corrupt msgpack body ─────────────────────────────────────────────────────


class TestCorruptMsgpackBody:
    def test_import_corrupt_msgpack_raises(self, ephemeral_db: T3Database, tmp_path: Path):
        header = {
            "format_version": FORMAT_VERSION,
            "collection_name": "knowledge__corrupt",
            "database_type": "knowledge",
            "embedding_model": "voyage-context-3",
            "record_count": 1,
            "embedding_dim": 128,
            "exported_at": "2026-01-01T00:00:00+00:00",
            "pipeline_version": "nexus-1",
        }
        out = tmp_path / "corrupt.nxexp"
        with open(out, "wb") as f:
            f.write(json.dumps(header).encode() + b"\n")
            with gzip.GzipFile(fileobj=f, mode="wb") as gz:
                gz.write(b"this is not valid msgpack data at all!!")
        ephemeral_db.get_or_create_collection("knowledge__corrupt")
        with pytest.raises(Exception):
            import_collection(ephemeral_db, out)
