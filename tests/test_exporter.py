# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Generator

import chromadb
import msgpack
import numpy as np
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.t3 import T3Database
from nexus.errors import (
    EmbeddingDimensionMismatch,
    EmbeddingModelMismatch,
    FormatVersionError,
    NexusError,
)
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
    # RDR-103 Phase 5: exporter tests legitimately operate on legacy
    # 2-segment names (the round-trip surface includes pre-conformant
    # backups). Pre-create with ``strict=False`` so the test fixture
    # can keep using the historical ``code__test`` shape.
    col = ephemeral_db.get_or_create_collection("code__test", strict=False)
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


def _write_nxexp_records(
    path: Path,
    collection_name: str,
    records: list[dict],
    embedding_model: str = "voyage-code-3",
    dim: int = 1024,
    seed: int = 11,
) -> None:
    """Hand-craft a ``.nxexp`` file from raw record dicts (id/document/
    metadata; ``embedding`` auto-filled with random vectors unless the
    record already supplies one). Used by tests that need to control the
    source id/model/dims directly rather than going through a real
    ``export_collection`` round trip (e.g. simulating a legacy
    pre-migration backup)."""
    header = {
        "format_version": FORMAT_VERSION,
        "collection_name": collection_name,
        "database_type": collection_name.split("__")[0],
        "embedding_model": embedding_model,
        "record_count": len(records),
        "embedding_dim": dim,
        "exported_at": "2025-01-01T00:00:00+00:00",
        "pipeline_version": "nexus-1",
    }
    rng = np.random.default_rng(seed=seed)
    with open(path, "wb") as f:
        f.write(json.dumps(header).encode() + b"\n")
        with gzip.GzipFile(fileobj=f, mode="wb") as gz:
            for r in records:
                r = dict(r)
                r.setdefault(
                    "embedding",
                    rng.standard_normal(dim).astype(np.float32).tobytes(),
                )
                gz.write(msgpack.packb(r, use_bin_type=True))


def _seed_collection(db, name, docs, ids, metadatas):
    # See ``populated_db`` for why ``strict=False``.
    col = db.get_or_create_collection(name, strict=False)
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
        """RDR-102 D2: source_path is dropped by normalize() at the
        canonical write boundary, so the import path strips it. Verify
        round-trip via ``title`` (which is in ALLOWED_TOP_LEVEL and
        unaffected). The fixture seeds ``title="File N"`` per chunk."""
        _export_import(populated_db, "code__test", tmp_path, target="code__meta_check")
        col = populated_db._client_for("code__meta_check").get_collection("code__meta_check")
        titles = {m.get("title", "") for m in col.get(include=["metadatas"])["metadatas"]}
        assert "File 0" in titles and "File 4" in titles

    def test_round_trip_preserves_embeddings(self, populated_db: T3Database, tmp_path: Path):
        # nexus GH #1370 D1: non-conformant (non-32-char) source ids like
        # "id-000" are re-hashed to content-derived ids on import, so
        # identity is no longer id-based post-fix -- look up by document
        # text instead of assuming the id round-trips verbatim.
        orig_col = populated_db._client_for("code__test").get_collection("code__test")
        orig_result = orig_col.get(include=["documents", "embeddings"])
        orig_by_doc = dict(zip(orig_result["documents"], orig_result["embeddings"]))
        orig_emb = orig_by_doc["document 0"]

        _export_import(populated_db, "code__test", tmp_path, target="code__emb_check")
        restored_col = populated_db._client_for("code__emb_check").get_collection("code__emb_check")
        restored_result = restored_col.get(include=["documents", "embeddings"])
        restored_by_doc = dict(zip(restored_result["documents"], restored_result["embeddings"]))
        restored_emb = restored_by_doc["document 0"]
        np.testing.assert_allclose(orig_emb, restored_emb, rtol=1e-6)

    def test_round_trip_preserves_taxonomy_metadata(
        self, ephemeral_db: T3Database, tmp_path: Path,
    ):
        """nexus-o6aa.9.16: programmatic vector-only collections
        (``taxonomy__*``) carry collection-specific metadata
        (``topic_id``, ``label``, ``doc_count``, ``collection``) that
        is not part of the canonical chunk schema. Pre-fix, the
        canonical normalize/validate funnel in T3Database stripped
        every taxonomy-specific key on import — silently turning the
        .nxexp round-trip from a faithful backup into a lossy one.

        This regression test seeds a ``taxonomy__centroids``-shape
        collection, exports it, imports into a fresh collection,
        and asserts every taxonomy key round-trips intact.
        """
        col_name = "taxonomy__centroids"
        ids = ["taxonomy__centroids:1", "taxonomy__centroids:2"]
        # Vector-only entries: no documents, just embeddings + metadata.
        # _seed_collection requires non-empty docs to compute embeddings,
        # so we seed with placeholder docs but then read back the
        # metadata only.
        docs = ["", ""]
        # Hand-craft metadatas that mirror catalog_taxonomy._batched_upsert.
        metadatas = [
            {
                "topic_id": 1,
                "label": "neural laminar circuits",
                "collection": "papers__grossberg",
                "doc_count": 12,
            },
            {
                "topic_id": 2,
                "label": "phoneme blocking dynamics",
                "collection": "papers__grossberg",
                "doc_count": 8,
            },
        ]
        embeddings = [_EF(["x"])[0], _EF(["y"])[0]]
        # Mirror catalog_taxonomy._create_centroid_collection: use the
        # underlying chromadb client directly with hnsw:space=cosine.
        # NOT ephemeral_db.get_or_create_collection — that injects an
        # embedding_function and defaults to L2, which pollutes
        # process-wide chromadb state and breaks downstream
        # test_projection_quality similarity assertions when test
        # ordering co-locates these tests in the same process.
        col = ephemeral_db._client.get_or_create_collection(
            col_name,
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )
        col.upsert(
            ids=ids, documents=docs, embeddings=embeddings, metadatas=metadatas,
        )

        _export_import(
            ephemeral_db, col_name, tmp_path, target="taxonomy__restored",
        )

        restored = ephemeral_db._client_for(
            "taxonomy__restored",
        ).get_collection("taxonomy__restored")
        result = restored.get(ids=ids, include=["metadatas"])
        meta_by_id = dict(zip(result["ids"], result["metadatas"]))
        # WITH TEETH — every taxonomy key must survive the round trip.
        for original_id, original_meta in zip(ids, metadatas):
            roundtripped = meta_by_id[original_id]
            for key, expected in original_meta.items():
                assert roundtripped.get(key) == expected, (
                    f"taxonomy key {key!r} did not round-trip on "
                    f"{original_id!r}: expected {expected!r}, "
                    f"got {roundtripped!r}. If this fails the canonical "
                    f"schema funnel stripped the key — re-check the "
                    f"_bypass_canonical_schema guard in t3.py."
                )

    def test_round_trip_preserves_taxonomy_distance_metric(
        self, ephemeral_db: T3Database, tmp_path: Path,
    ):
        """nexus-18wz: ``taxonomy__*`` round-trip must preserve the
        ``hnsw:space=cosine`` distance metric.

        Pre-fix, the production import path (``upsert_chunks_with_embeddings``
        → ``get_or_create_collection``) injected an embedding_function and
        defaulted to L2, so a faithful-looking metadata round-trip silently
        recreated the collection with the wrong metric. Cosine queries
        against the imported collection then returned out-of-range
        distances and broke any downstream similarity assignment.

        The fix extends ``get_or_create_collection`` so bypass-prefix
        collections (per ``_BYPASS_SCHEMA_PREFIXES``) are created with
        ``embedding_function=None`` and ``metadata={'hnsw:space':
        'cosine'}`` — mirroring ``catalog_taxonomy._create_centroid_collection``.
        """
        col_name = "taxonomy__centroids_metric"
        target = "taxonomy__restored_metric"
        ids = ["c:1", "c:2"]
        docs = ["", ""]
        # Use orthonormal-ish vectors so cosine and L2 disagree clearly:
        # cosine_distance(e1, e2) = 1.0; L2_distance(e1, e2) = sqrt(2).
        embeddings = [_EF(["alpha"])[0], _EF(["beta"])[0]]
        metadatas = [
            {"topic_id": 1, "label": "alpha", "collection": "x", "doc_count": 1},
            {"topic_id": 2, "label": "beta", "collection": "x", "doc_count": 1},
        ]
        col = ephemeral_db._client.get_or_create_collection(
            col_name,
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )
        col.upsert(
            ids=ids, documents=docs, embeddings=embeddings, metadatas=metadatas,
        )

        _export_import(
            ephemeral_db, col_name, tmp_path, target=target,
        )

        restored = ephemeral_db._client_for(target).get_collection(target)

        # Direct metadata assertion as the load-bearing claim — pre-fix
        # the import path passes no metadata kwarg in non-local mode, so
        # ``restored.metadata`` is ``None`` and ChromaDB falls back to
        # the L2 default. After the fix, bypass-prefix collections are
        # created with ``metadata={'hnsw:space': 'cosine'}`` regardless
        # of local_mode.
        assert restored.metadata is not None, (
            "imported collection metadata is None — the import path is "
            "creating bypass-prefix collections without explicit "
            "hnsw:space=cosine. Check get_or_create_collection's "
            "bypass-prefix branch in src/nexus/db/t3.py (nexus-18wz)."
        )
        assert restored.metadata.get("hnsw:space") == "cosine", (
            f"imported collection metadata: {restored.metadata!r} — "
            f"expected hnsw:space=cosine (nexus-18wz)"
        )

        # Smoke check: a query against the imported collection returns
        # distances within cosine bounds [0, 2]. A passing metadata
        # assertion above guarantees this; the secondary check guards
        # against future ChromaDB versions where ``metadata['hnsw:space']``
        # might decouple from the actual index space.
        # Tolerance accounts for float underflow when querying a vector
        # against itself (distance ~0 can dip to a tiny negative on
        # some CPUs / ChromaDB builds — Linux CI runner hits this).
        query_result = restored.query(
            query_embeddings=[embeddings[0]],
            n_results=2,
            include=["distances"],
        )
        eps = 1e-6
        for d in query_result["distances"][0]:
            assert -eps <= d <= 2.0 + eps, (
                f"distance {d} is outside cosine bounds [0, 2] "
                f"(tolerance {eps})"
            )


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
        # See ``populated_db`` for why ``strict=False``.
        col = db.get_or_create_collection(col_name, strict=False)
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

    # Both pagination tests below seed >300 records to cross the
    # _PAGE/MAX_QUERY_RESULTS boundary. Real coverage of the 300-cap
    # contract, but the boundary doesn't drift between releases — paying
    # ~53s/each on every CI push is overhead, not signal. Marked ``slow``
    # so default ``pytest`` deselects them; run with
    # ``uv run pytest -m slow`` or as part of the release shakedown.
    @pytest.mark.slow
    def test_export_pagination(self, ephemeral_db: T3Database, tmp_path: Path):
        n = self._seed_large(ephemeral_db, "code__large", "id")
        _, result = _export(ephemeral_db, "code__large", tmp_path)
        assert result["exported_count"] == n

    @pytest.mark.slow
    def test_import_pagination(self, ephemeral_db: T3Database, tmp_path: Path):
        n = self._seed_large(ephemeral_db, "code__big_src", "big")
        _, _, import_result = _export_import(
            ephemeral_db, "code__big_src", tmp_path, target="code__big_dst",
        )
        assert import_result["imported_count"] == n
        dst = ephemeral_db._client_for("code__big_dst").get_collection("code__big_dst")
        assert dst.count() == n


# ── Unit: service-mode export (GH #1373) ─────────────────────────────────────


class TestServiceModeExport:
    """GH #1373: ``make_t3()`` returns ``HttpVectorClient`` in production
    (both local and cloud mode, RDR-155 P4a.2) -- ``export_collection``
    crashed reaching for the Chroma-only ``db._client_for()`` private
    method. These tests exercise the fix through the same
    ``nexus.db.http_vector_client._post`` / ``_get`` module-function-patch
    pattern used by ``tests/test_bridge_address_fields.py`` (no real HTTP
    transport, no httpx.MockTransport needed -- the client's networking
    funnels through those two module functions)."""

    @staticmethod
    def _make_client() -> HttpVectorClient:
        client = HttpVectorClient.__new__(HttpVectorClient)
        client._tenant = "test-tenant"
        return client

    def _fake_service(
        self, collection_name: str, ids: list[str],
        documents: list[str], metadatas: list[dict], embeddings: np.ndarray,
    ):
        """Return (fake_get, fake_post) closures backing a single collection
        with *ids*/*documents*/*metadatas*/*embeddings* (all same length),
        routing on the endpoint paths export_collection actually calls."""

        def fake_get(path: str, **_kwargs):
            if path == "/v1/vectors/stats":
                return [{"name": collection_name, "count": len(ids)}]
            if path.startswith("/v1/vectors/count"):
                return {"count": len(ids)}
            raise AssertionError(f"unexpected GET {path}")

        def fake_post(path: str, body: dict, **_kwargs):
            if path == "/v1/vectors/get":
                assert body["collection"] == collection_name
                offset = body.get("offset", 0)
                limit = body.get("limit", len(ids))
                page = slice(offset, offset + limit)
                return {
                    "ids": ids[page],
                    "documents": documents[page],
                    "metadatas": metadatas[page],
                }
            if path == "/v1/vectors/get-embeddings":
                assert body["collection"] == collection_name
                by_id = dict(zip(ids, embeddings))
                return {"embeddings": [by_id[i].tolist() for i in body["ids"]]}
            raise AssertionError(f"unexpected POST {path}")

        return fake_get, fake_post

    def test_export_via_http_vector_client_builds_valid_file(self, tmp_path: Path):
        from unittest.mock import patch
        collection_name = "code__svc__voyage-code-3__v1"
        ids = [f"{i:032x}" for i in range(3)]
        documents = [f"document {i}" for i in range(3)]
        metadatas = [{"source_path": f"/repo/file_{i}.py"} for i in range(3)]
        rng = np.random.default_rng(seed=7)
        embeddings = rng.standard_normal((3, 1024)).astype(np.float32)

        fake_get, fake_post = self._fake_service(
            collection_name, ids, documents, metadatas, embeddings,
        )
        client = self._make_client()
        out_path = tmp_path / "svc.nxexp"

        with patch("nexus.db.http_vector_client._get", side_effect=fake_get), \
             patch("nexus.db.http_vector_client._post", side_effect=fake_post):
            result = export_collection(
                db=client, collection_name=collection_name, output_path=out_path,
            )

        assert result["exported_count"] == 3
        assert out_path.exists()

        with open(out_path, "rb") as f:
            header = json.loads(f.readline().decode())
            with gzip.GzipFile(fileobj=f) as gz:
                records = list(msgpack.Unpacker(gz, raw=False))

        assert header["collection_name"] == collection_name
        assert header["embedding_model"] == "voyage-code-3"
        assert len(records) == 3
        assert {r["id"] for r in records} == set(ids)
        by_id_meta = {r["id"]: r["metadata"] for r in records}
        for i, doc_id in enumerate(ids):
            assert by_id_meta[doc_id]["source_path"] == f"/repo/file_{i}.py"
        by_id_emb = {r["id"]: r["embedding"] for r in records}
        for i, doc_id in enumerate(ids):
            got = np.frombuffer(by_id_emb[doc_id], dtype=np.float32)
            np.testing.assert_allclose(got, embeddings[i])

    def test_export_via_http_vector_client_round_trips_into_local_import(
        self, tmp_path: Path, ephemeral_db: T3Database,
    ):
        """The .nxexp a service-mode export produces is byte-compatible
        with the local-mode import path -- proves the file shape doesn't
        silently diverge between backends."""
        from unittest.mock import patch
        collection_name = "code__svc__voyage-code-3__v1"
        ids = [f"{i:032x}" for i in range(2)]
        documents = [f"document {i}" for i in range(2)]
        metadatas = [{"source_path": f"/repo/file_{i}.py"} for i in range(2)]
        rng = np.random.default_rng(seed=11)
        embeddings = rng.standard_normal((2, 1024)).astype(np.float32)

        fake_get, fake_post = self._fake_service(
            collection_name, ids, documents, metadatas, embeddings,
        )
        client = self._make_client()
        out_path = tmp_path / "svc.nxexp"

        with patch("nexus.db.http_vector_client._get", side_effect=fake_get), \
             patch("nexus.db.http_vector_client._post", side_effect=fake_post):
            export_collection(db=client, collection_name=collection_name, output_path=out_path)

        import_result = import_collection(db=ephemeral_db, input_path=out_path)
        assert import_result["imported_count"] == 2

        dst = ephemeral_db.get_collection(collection_name)
        assert dst.count() == 2
        stored = dst.get(ids=ids, include=["documents", "metadatas", "embeddings"])
        assert set(stored["ids"]) == set(ids)
        by_id_doc = dict(zip(stored["ids"], stored["documents"]))
        for i, doc_id in enumerate(ids):
            assert by_id_doc[doc_id] == documents[i]


# ── Unit: path remapping ─────────────────────────────────────────────────────


class TestPathRemapping:
    """nexus-8g79.29: pre-fix the legacy ``test_remap_on_import``
    asserted the remap's effect on T3 chunk metadata, but RDR-102
    Phase B dropped ``source_path`` from ``ALLOWED_TOP_LEVEL`` so the
    ``normalize()`` funnel strips it on import — the assertion was
    permanently unreachable and parked under ``xfail(strict=True)``,
    which is brittle (any incidental fix would flip it to XPASS and
    break the suite).

    Replaced with a direct unit test of ``_apply_remap`` — the
    function still operates on the export stream (back-compat for
    legacy ``.nxexp`` files) but its end-to-end effect on chunks is
    obsoleted by the schema removal. Testing the helper directly
    locks the back-compat contract without depending on chunk
    metadata that no longer exists.
    """

    @pytest.mark.parametrize("remaps,source_path,expected", [
        # First matching prefix wins.
        ([("/repo", "/new_root")], "/repo/src/foo.py", "/new_root/src/foo.py"),
        # No match → passthrough.
        ([("/nonexistent", "/other")], "/repo/src/bar.py", "/repo/src/bar.py"),
        # Earlier match wins over later.
        ([("/a", "/A"), ("/a/sub", "/AB")], "/a/sub/x", "/A/sub/x"),
        # Empty remaps → passthrough.
        ([], "/repo/file.py", "/repo/file.py"),
    ])
    def test_apply_remap_first_match_wins(
        self, remaps, source_path, expected,
    ):
        from nexus.exporter import _apply_remap
        assert _apply_remap(source_path, remaps) == expected


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
        # env_creds sets cloud creds -> real _t3() would be HttpVectorClient.
        with patch("nexus.commands.store._t3", return_value=MagicMock(spec=HttpVectorClient)):
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
        with patch("nexus.commands.store._t3", return_value=MagicMock(spec=HttpVectorClient)):
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
        ephemeral_db.get_or_create_collection("knowledge__empty", strict=False)
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
        ephemeral_db.get_or_create_collection("knowledge__corrupt", strict=False)
        with pytest.raises(Exception):
            import_collection(ephemeral_db, out)


# ── Vector-only entries (document=None) ──────────────────────────────────────


class TestVectorOnlyImport:
    """Regression for nexus-fxc1: ``taxonomy__centroids`` and similar
    vector-only collections store ``document=None`` and round-trip that
    via ``.nxexp``.  The import path used to crash with
    ``'NoneType' object has no attribute 'encode'`` because the
    byte-length check in ``_write_batch`` called ``doc.encode()``
    unconditionally.
    """

    def _write_nxexp_with_none_docs(
        self, path: Path, collection_name: str, n_records: int = 3, dim: int = 16
    ) -> None:
        import msgpack

        header = {
            "format_version": FORMAT_VERSION,
            "collection_name": collection_name,
            "database_type": collection_name.split("__")[0],
            "embedding_model": "voyage-code-3",
            "record_count": n_records,
            "embedding_dim": dim,
            "exported_at": "2026-05-01T00:00:00+00:00",
            "pipeline_version": "nexus-1",
        }
        with open(path, "wb") as f:
            f.write(json.dumps(header).encode() + b"\n")
            with gzip.GzipFile(fileobj=f, mode="wb") as gz:
                rng = np.random.default_rng(seed=42)
                for i in range(n_records):
                    emb = rng.standard_normal(dim).astype(np.float32).tobytes()
                    gz.write(msgpack.packb(
                        {
                            "id": f"vec-only-{i:03d}",
                            "document": None,  # vector-only entry
                            "metadata": {"topic_id": i, "label": f"cluster-{i}"},
                            "embedding": emb,
                        },
                        use_bin_type=True,
                    ))

    def test_import_succeeds_when_documents_are_none(
        self, ephemeral_db: T3Database, tmp_path: Path
    ):
        out = tmp_path / "vec_only.nxexp"
        col_name = "taxonomy__test_centroids"
        self._write_nxexp_with_none_docs(out, col_name, n_records=3)

        # Pre-create the collection so the EF override applies — matches
        # the production flow where the collection already exists.
        ephemeral_db.get_or_create_collection(col_name)

        result = import_collection(ephemeral_db, out)
        assert result["imported_count"] == 3
        assert result["collection_name"] == col_name

        col = ephemeral_db._client_for(col_name).get_collection(col_name)
        assert col.count() == 3
        # Vector-only entries should have empty (or None) documents and
        # the original embeddings preserved.
        got = col.get(include=["documents", "embeddings"])
        assert all((d == "" or d is None) for d in got["documents"])
        assert len(got["embeddings"]) == 3
        assert len(got["embeddings"][0]) == 16

    def test_import_mixed_some_none_some_text(
        self, ephemeral_db: T3Database, tmp_path: Path
    ):
        """Mixed batch: half ``document=None``, half real text. Both must import."""
        import msgpack

        col_name = "taxonomy__mixed"
        out = tmp_path / "mixed.nxexp"
        header = {
            "format_version": FORMAT_VERSION,
            "collection_name": col_name,
            "database_type": "taxonomy",
            "embedding_model": "voyage-code-3",
            "record_count": 4,
            "embedding_dim": 16,
            "exported_at": "2026-05-01T00:00:00+00:00",
            "pipeline_version": "nexus-1",
        }
        rng = np.random.default_rng(seed=7)
        records = [
            {"id": "a", "document": None,        "metadata": {"k": "v1"}},
            {"id": "b", "document": "real text", "metadata": {"k": "v2"}},
            {"id": "c", "document": None,        "metadata": {"k": "v3"}},
            {"id": "d", "document": "more text", "metadata": {"k": "v4"}},
        ]
        with open(out, "wb") as f:
            f.write(json.dumps(header).encode() + b"\n")
            with gzip.GzipFile(fileobj=f, mode="wb") as gz:
                for r in records:
                    r["embedding"] = rng.standard_normal(16).astype(np.float32).tobytes()
                    gz.write(msgpack.packb(r, use_bin_type=True))

        ephemeral_db.get_or_create_collection(col_name)
        result = import_collection(ephemeral_db, out)
        assert result["imported_count"] == 4

        col = ephemeral_db._client_for(col_name).get_collection(col_name)
        assert col.count() == 4
        got = col.get(ids=["a", "b"], include=["documents"])
        # ``a`` was None, normalized to ""; ``b`` keeps its text.
        a_doc = got["documents"][got["ids"].index("a")]
        b_doc = got["documents"][got["ids"].index("b")]
        assert a_doc in ("", None)
        assert b_doc == "real text"


# ── GH #1370 D1: non-conformant chunk id re-hash ────────────────────────────


class TestChashRehash:
    """Pre-migration ``.nxexp`` exports carry non-32-char chunk ids
    (Chroma-era ids were 16-char hex). The service backend's
    ``chunks_<dim>`` tables enforce ``CHECK (length(chash) = 32)``, so
    importing such a record 409s with an opaque integrity-constraint
    error. ``import_collection`` now re-derives the id the same way
    fresh indexing does (``chunk_text_hash[:32]``) whenever the source
    id isn't already 32 chars, except for bypass-schema collections
    (``taxonomy__*``) whose ids are intentional programmatic keys, not
    content hashes.
    """

    _write_legacy_nxexp = staticmethod(_write_nxexp_records)

    def test_16char_hex_id_rehashed_to_32char_content_hash(
        self, ephemeral_db: T3Database, tmp_path: Path,
    ):
        out = tmp_path / "legacy16.nxexp"
        legacy_id = "abcdef0123456789"  # 16-char hex: Chroma-era shape
        doc_text = "legacy chunk content"
        self._write_legacy_nxexp(out, "code__legacy", [
            {"id": legacy_id, "document": doc_text,
             "metadata": {"chunk_text_hash": "0" * 64, "title": "t"}},
        ])

        result = import_collection(ephemeral_db, out)
        assert result["imported_count"] == 1
        assert result["rehashed_count"] == 1

        col = ephemeral_db._client_for("code__legacy").get_collection("code__legacy")
        got = col.get(include=["documents", "metadatas"])
        assert len(got["ids"]) == 1
        new_id = got["ids"][0]
        expected_id = hashlib.sha256(doc_text.encode()).hexdigest()[:32]
        assert new_id == expected_id
        assert len(new_id) == 32
        assert new_id != legacy_id
        assert got["documents"][0] == doc_text
        # chunk_text_hash metadata stays consistent with the new id
        # (its first 32 chars) rather than the stale pre-rehash value.
        assert got["metadatas"][0]["chunk_text_hash"] == hashlib.sha256(doc_text.encode()).hexdigest()

    def test_empty_document_rehashes_from_old_id(
        self, ephemeral_db: T3Database, tmp_path: Path,
    ):
        """Vector-only entries (``document=None``/``""``) have no content
        to hash meaningfully -- the old id is hashed instead, giving a
        deterministic, stable new id across repeated re-imports."""
        out = tmp_path / "legacy_empty_doc.nxexp"
        legacy_id = "vecid-0001"  # non-32-char, non-hex
        self._write_legacy_nxexp(out, "code__vec_legacy", [
            {"id": legacy_id, "document": None, "metadata": {}},
        ])

        result = import_collection(ephemeral_db, out)
        assert result["imported_count"] == 1
        assert result["rehashed_count"] == 1

        col = ephemeral_db._client_for("code__vec_legacy").get_collection("code__vec_legacy")
        got = col.get(include=[])
        expected_id = hashlib.sha256(legacy_id.encode()).hexdigest()[:32]
        assert got["ids"] == [expected_id]

    def test_taxonomy_collections_ids_not_rehashed(
        self, ephemeral_db: T3Database, tmp_path: Path,
    ):
        """Bypass-schema (``taxonomy__*``) collections use intentional
        programmatic ids (e.g. ``topic_id``-keyed) -- rehashing them
        would break their identity semantics, so they're left alone."""
        out = tmp_path / "legacy_taxonomy.nxexp"
        legacy_id = "taxonomy__centroids:1"
        self._write_legacy_nxexp(out, "taxonomy__legacy", [
            {"id": legacy_id, "document": "",
             "metadata": {"topic_id": 1, "label": "x", "collection": "y", "doc_count": 1}},
        ])
        ephemeral_db._client.get_or_create_collection(
            "taxonomy__legacy", embedding_function=None, metadata={"hnsw:space": "cosine"},
        )

        result = import_collection(ephemeral_db, out)
        assert result["rehashed_count"] == 0

        col = ephemeral_db._client_for("taxonomy__legacy").get_collection("taxonomy__legacy")
        got = col.get(include=[])
        assert got["ids"] == [legacy_id]

    def test_conformant_32char_id_left_unchanged(
        self, ephemeral_db: T3Database, tmp_path: Path,
    ):
        """An id that's already 32 chars (the common case, post-RDR-108
        exports) is never rehashed -- it round-trips verbatim."""
        out = tmp_path / "conformant.nxexp"
        doc_text = "already conformant chunk"
        conformant_id = hashlib.sha256(doc_text.encode()).hexdigest()[:32]
        self._write_legacy_nxexp(out, "code__conformant", [
            {"id": conformant_id, "document": doc_text, "metadata": {}},
        ])

        result = import_collection(ephemeral_db, out)
        assert result["rehashed_count"] == 0

        col = ephemeral_db._client_for("code__conformant").get_collection("code__conformant")
        got = col.get(include=[])
        assert got["ids"] == [conformant_id]


# ── GH #1370 D2: embedding-model mislabel + dims sanity check ───────────────


class TestEmbeddingDimensionMismatch:
    """Pre-migration exports can carry a WRONG ``embedding_model``
    header label: legacy two-segment collection names route through
    ``voyage_model_for_collection``'s prefix-based guess, which silently
    mislabels local-mode (bge/minilm) exports as Voyage models.
    ``import_collection`` now sanity-checks the declared model's expected
    dimensionality against the actual vectors, and ``--assume-model``
    lets the caller correct a wrong label.
    """

    def _write_nxexp(
        self, path: Path, collection_name: str, embedding_model: str,
        dim: int, doc: str = "hello world",
    ) -> None:
        header = {
            "format_version": FORMAT_VERSION,
            "collection_name": collection_name,
            "database_type": collection_name.split("__")[0],
            "embedding_model": embedding_model,
            "record_count": 1,
            "embedding_dim": dim,
            "exported_at": "2025-01-01T00:00:00+00:00",
            "pipeline_version": "nexus-1",
        }
        rng = np.random.default_rng(seed=13)
        with open(path, "wb") as f:
            f.write(json.dumps(header).encode() + b"\n")
            with gzip.GzipFile(fileobj=f, mode="wb") as gz:
                gz.write(msgpack.packb({
                    "id": hashlib.sha256(doc.encode()).hexdigest()[:32],
                    "document": doc,
                    "metadata": {},
                    "embedding": rng.standard_normal(dim).astype(np.float32).tobytes(),
                }, use_bin_type=True))

    def test_dims_mismatch_raises_clear_error(
        self, ephemeral_db: T3Database, tmp_path: Path,
    ):
        out = tmp_path / "mislabeled.nxexp"
        # Dims check only fires for a conformant (self-declaring) target
        # name or an explicit --assume-model -- a legacy two-segment
        # name's "expected model" is just a guess (voyage_model_for_
        # collection) and is exempt (see enforce_dims_check).
        target = "code__myrepo__voyage-code-3__v1"
        # Header claims voyage-code-3 (1024-dim) but vectors are 768-dim
        # (bge) -- the GH #1370 D2 mislabel defect.
        self._write_nxexp(out, target, "voyage-code-3", dim=768)

        with pytest.raises(EmbeddingDimensionMismatch) as exc_info:
            import_collection(ephemeral_db, out)
        msg = str(exc_info.value)
        assert "voyage-code-3" in msg
        assert "1024" in msg
        assert "768" in msg

    def test_assume_model_corrects_wrong_label(
        self, ephemeral_db: T3Database, tmp_path: Path,
    ):
        out = tmp_path / "mislabeled2.nxexp"
        self._write_nxexp(out, "code__mislabeled2", "voyage-code-3", dim=768)
        target = "code__myrepo__bge-base-en-v15-768__v1"

        result = import_collection(
            ephemeral_db, out, target_collection=target,
            assume_model="bge-base-en-v15-768",
        )
        assert result["imported_count"] == 1
        assert result["collection_name"] == target

    def test_assume_model_wrong_override_fails_loud(
        self, ephemeral_db: T3Database, tmp_path: Path,
    ):
        """A wrong --assume-model override must still be validated
        against the actual vector dims -- it corrects a bad label, it
        doesn't disable the safety check."""
        out = tmp_path / "mislabeled3.nxexp"
        self._write_nxexp(out, "code__mislabeled3", "voyage-code-3", dim=768)
        target = "code__myrepo__minilm-l6-v2-384__v1"

        with pytest.raises(EmbeddingDimensionMismatch) as exc_info:
            import_collection(
                ephemeral_db, out, target_collection=target,
                assume_model="minilm-l6-v2-384",
            )
        msg = str(exc_info.value)
        assert "minilm-l6-v2-384" in msg
        assert "384" in msg
        assert "768" in msg

    def test_legacy_two_segment_target_exempt_from_dims_check(
        self, ephemeral_db: T3Database, tmp_path: Path,
    ):
        """A legacy (non-conformant) target name's expected model is only
        ever a prefix-based guess (``voyage_model_for_collection``) --
        local-mode installs legitimately restore bge/minilm vectors
        under such names, so the dims check does not block this
        pre-existing, unaffected workflow."""
        out = tmp_path / "legacy_mismatched_dims.nxexp"
        self._write_nxexp(out, "code__legacy_dims", "voyage-code-3", dim=384)

        result = import_collection(ephemeral_db, out)
        assert result["imported_count"] == 1

    def test_assume_model_still_enforces_model_mismatch_gate(
        self, ephemeral_db: T3Database, tmp_path: Path,
    ):
        """--assume-model corrects the label used in the mismatch gate;
        it doesn't bypass the gate for a genuinely incompatible target."""
        out = tmp_path / "mislabeled4.nxexp"
        self._write_nxexp(out, "code__mislabeled4", "voyage-code-3", dim=1024)

        with pytest.raises(EmbeddingModelMismatch):
            import_collection(
                ephemeral_db, out, target_collection="docs__corpus",
                assume_model="voyage-code-3",
            )


# ── GH #1370 D3: --skip-existing ────────────────────────────────────────────


class TestSkipExisting:
    """A failing batch previously aborted the entire import with no way
    to resume. ``skip_existing=True`` drops records whose id already
    exists in the target collection instead of overwriting them, and
    reports how many were skipped."""

    def test_skip_existing_skips_already_imported_records(
        self, populated_db: T3Database, tmp_path: Path,
    ):
        out, _, first = _export_import(
            populated_db, "code__test", tmp_path, target="code__resume",
        )
        assert first["imported_count"] == 5

        second = import_collection(
            db=populated_db, input_path=out, target_collection="code__resume",
            skip_existing=True,
        )
        assert second["skipped_count"] == 5
        assert second["imported_count"] == 0

        col = populated_db._client_for("code__resume").get_collection("code__resume")
        assert col.count() == 5

    def test_without_skip_existing_reimport_overwrites_not_errors(
        self, populated_db: T3Database, tmp_path: Path,
    ):
        out, _, first = _export_import(
            populated_db, "code__test", tmp_path, target="code__reimport",
        )
        assert first["imported_count"] == 5

        second = import_collection(
            db=populated_db, input_path=out, target_collection="code__reimport",
        )
        assert second["imported_count"] == 5
        assert second["skipped_count"] == 0

        col = populated_db._client_for("code__reimport").get_collection("code__reimport")
        assert col.count() == 5

    def test_skip_existing_partial_overlap(
        self, ephemeral_db: T3Database, tmp_path: Path,
    ):
        """Only the ids that already exist are skipped; new ids in the
        same file still import.

        Content-derived ids are deterministic (GH #1370 D1): importing
        the same document text twice always rehashes to the same id, so
        seeding via a first ``import_collection`` call and re-importing
        a file containing both that record and a new one exercises the
        partial-overlap path without needing to hand-craft matching raw
        ids.
        """
        first_file = tmp_path / "partial_first.nxexp"
        _write_nxexp_records(first_file, "code__partial", [
            {"id": "legacy-id-0001", "document": "existing text", "metadata": {"title": "existing"}},
        ])
        first = import_collection(db=ephemeral_db, input_path=first_file)
        assert first["imported_count"] == 1

        second_file = tmp_path / "partial_second.nxexp"
        _write_nxexp_records(second_file, "code__partial", [
            {"id": "legacy-id-9999", "document": "existing text", "metadata": {"title": "existing"}},
            {"id": "legacy-id-0002", "document": "brand new text", "metadata": {"title": "new"}},
        ])
        result = import_collection(
            db=ephemeral_db, input_path=second_file, skip_existing=True,
        )
        assert result["skipped_count"] == 1
        assert result["imported_count"] == 1

        col = ephemeral_db._client_for("code__partial").get_collection("code__partial")
        assert col.count() == 2


# ── GH #1370 D3: constraint-violation hint wrapping ─────────────────────────


class TestUpsertHintWrapping:
    """A raw backend integrity-constraint error is opaque (GH #1370 D3
    cheap-win UX). ``_upsert_with_hint`` wraps errors that look like a
    chash/duplicate-key conflict with an actionable hint; unrelated
    errors pass through untouched."""

    def test_constraint_error_gets_hint(self):
        from unittest.mock import MagicMock

        from nexus.exporter import _upsert_with_hint

        db = MagicMock()
        db.upsert_chunks_with_embeddings.side_effect = RuntimeError(
            'duplicate key value violates unique constraint "chunks_1024_pkey"'
        )
        with pytest.raises(NexusError) as exc_info:
            _upsert_with_hint(db, "code__x", ["id1"], ["doc"], [[0.1]], [{}], MagicMock())
        msg = str(exc_info.value).lower()
        assert "skip-existing" in msg
        assert "duplicate key" in msg

    def test_unrelated_error_passes_through_unchanged(self):
        from unittest.mock import MagicMock

        from nexus.exporter import _upsert_with_hint

        db = MagicMock()
        db.upsert_chunks_with_embeddings.side_effect = ValueError("network timeout")
        with pytest.raises(ValueError, match="network timeout"):
            _upsert_with_hint(db, "code__x", ["id1"], ["doc"], [[0.1]], [{}], MagicMock())


# ── GH #1370: CLI flag wiring for --assume-model / --skip-existing ─────────


class TestImportFlagsCLI:
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

    def test_skip_existing_flag_reports_skipped_count(
        self, runner, env_creds, tmp_path, populated_db: T3Database,
    ):
        from unittest.mock import patch

        from nexus.cli import main
        out, _ = _export(populated_db, "code__test", tmp_path)
        with patch("nexus.commands.store._t3", return_value=populated_db):
            first = runner.invoke(
                main, ["store", "import", str(out), "--collection", "code__cli_skip"],
            )
            assert first.exit_code == 0
            second = runner.invoke(
                main,
                ["store", "import", str(out), "--collection", "code__cli_skip", "--skip-existing"],
            )
        assert second.exit_code == 0
        assert "Skipped 5" in second.output

    def test_assume_model_flag_wired_through(
        self, runner, env_creds, tmp_path, ephemeral_db: T3Database,
    ):
        from unittest.mock import patch

        from nexus.cli import main
        out = tmp_path / "mislabeled_cli.nxexp"
        header = {
            "format_version": FORMAT_VERSION,
            "collection_name": "code__cli_mislabeled",
            "database_type": "code",
            "embedding_model": "voyage-code-3",
            "record_count": 1,
            "embedding_dim": 384,
            "exported_at": "2025-01-01T00:00:00+00:00",
            "pipeline_version": "nexus-1",
        }
        rng = np.random.default_rng(seed=19)
        with open(out, "wb") as f:
            f.write(json.dumps(header).encode() + b"\n")
            with gzip.GzipFile(fileobj=f, mode="wb") as gz:
                gz.write(msgpack.packb({
                    "id": "0" * 32,
                    "document": "cli test doc",
                    "metadata": {},
                    "embedding": rng.standard_normal(384).astype(np.float32).tobytes(),
                }, use_bin_type=True))

        target = "code__myrepo__minilm-l6-v2-384__v1"
        with patch("nexus.commands.store._t3", return_value=ephemeral_db):
            result = runner.invoke(main, [
                "store", "import", str(out),
                "--collection", target,
                "--assume-model", "minilm-l6-v2-384",
            ])
        assert result.exit_code == 0
        assert "Imported 1" in result.output
