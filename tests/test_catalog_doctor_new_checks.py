# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6dan: 3 new catalog doctor checks.

- ``--chunk-size-distribution``: per-collection p50/p95/p99/max +
  micro-chunk WARN + over-quota FAIL.
- ``--chunk-text-dedup``: within-collection dupe ratio WARN +
  cross-collection dupe count WARN.
- ``--t3-vs-catalog``: T3 orphan collections + zombie collections +
  catalog docs pointing at missing T3.

Tests use ``chromadb.EphemeralClient`` per the project's
integration-over-mocks rule and seed real chunks/docs to exercise
each pass / WARN / FAIL boundary.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.commands.catalog_cmds.doctor import (
    _percentile,
    doctor_cmd,
)
from tests.conftest import make_vector_test_client


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _pin_local_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin sqlite/local backend: every test here seeds a LOCAL tmp
    catalog, so under the ``NX_TEST_T2_SUBSTRATE=engine`` flip (global
    ``NX_STORAGE_BACKEND=service``) the doctor would read the engine
    tenant's empty catalog instead of the seeded one (nexus-b6enc:
    fixed the 2 pre-existing engine-substrate failures in this file)."""
    monkeypatch.setenv("NX_STORAGE_BACKEND", "sqlite")


@pytest.fixture()
def isolated_nexus(tmp_path: Path) -> Path:
    return tmp_path / "test-catalog"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def chroma_client():
    """Fresh EphemeralClient with collections cleared (chromadb's
    in-memory backend is process-shared per project memory).
    """
    client = make_vector_test_client()
    for col in list(client.list_collections()):
        try:
            client.delete_collection(col.name)
        except Exception:
            pass
    return client


def _seed(client, name: str, chunks: list[dict]) -> None:
    col = client.get_or_create_collection(
        name=name, embedding_function=DefaultEmbeddingFunction(),
    )
    col.add(
        ids=[c["id"] for c in chunks],
        documents=[c["content"] for c in chunks],
        metadatas=[c.get("metadata", {"_": "_"}) for c in chunks],
    )


# ── _percentile helper ─────────────────────────────────────────────────────


class TestPercentile:
    def test_empty_returns_zero(self) -> None:
        assert _percentile([], 0.5) == 0

    def test_single_value(self) -> None:
        assert _percentile([42], 0.5) == 42

    def test_p50_of_three(self) -> None:
        assert _percentile([1, 2, 3], 0.5) == 2

    def test_p99_returns_near_max(self) -> None:
        # 100 values 1..100; p99 should be close to 99.
        result = _percentile(list(range(1, 101)), 0.99)
        assert 98 <= result <= 100


# ── --chunk-size-distribution ────────────────────────────────────────────


class TestChunkSizeDistribution:
    def test_pass_when_all_chunks_in_range(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """All chunks well above micro-floor and well below over-
        quota → PASS, no WARN, no FAIL.
        """
        text = "x" * 500  # 500 bytes; comfortably mid-range
        _seed(chroma_client, "code__ok", [
            {"id": f"c{i}", "content": text} for i in range(10)
        ])

        class _FakeT3:
            _client = chroma_client

            def get_collection(self, name):
                return chroma_client.get_collection(name)

            def list_collections(self):
                return [{"name": "code__ok"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--chunk-size-distribution", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)["chunk_size_distribution"]
        assert payload["pass"] is True
        t = payload["tables"]["code__ok"]
        assert t["total_chunks"] == 10
        assert t["over_quota_count"] == 0
        assert t["micro_count"] == 0

    def test_micro_chunks_warn_above_5_percent(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """When > 5% of chunks are < 100 bytes the per-collection
        ``warn`` flag flips True. Lock the boundary so a future
        threshold shift surfaces in tests.
        """
        # 10 chunks, 2 micros = 20% → above the 5% warn threshold.
        chunks = [
            {"id": f"c{i}", "content": "x" * 50} for i in range(2)
        ] + [
            {"id": f"c{i + 2}", "content": "y" * 500} for i in range(8)
        ]
        _seed(chroma_client, "code__micros", chunks)

        class _FakeT3:
            _client = chroma_client

            def get_collection(self, name):
                return chroma_client.get_collection(name)

            def list_collections(self):
                return [{"name": "code__micros"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--chunk-size-distribution", "--json"],
        )
        # Micros are WARN, not FAIL.
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)["chunk_size_distribution"]
        t = payload["tables"]["code__micros"]
        assert t["micro_count"] == 2
        assert t["warn"] is True
        assert payload["pass"] is True  # WARN doesn't break overall

    def test_over_quota_chunk_fails(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A chunk over MAX_DOCUMENT_BYTES is a hard FAIL — Voyage
        rejects these, so they must surface as a release blocker.
        """
        from nexus.db.chroma_quotas import QUOTAS
        big = "x" * (QUOTAS.MAX_DOCUMENT_BYTES + 100)
        _seed(chroma_client, "code__big", [
            {"id": "c1", "content": "tiny"},
            {"id": "c2", "content": big},
        ])

        class _FakeT3:
            _client = chroma_client

            def get_collection(self, name):
                return chroma_client.get_collection(name)

            def list_collections(self):
                return [{"name": "code__big"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--chunk-size-distribution", "--json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.stdout)["chunk_size_distribution"]
        assert payload["pass"] is False
        t = payload["tables"]["code__big"]
        assert t["over_quota_count"] == 1
        assert t["pass"] is False

    def test_taxonomy_collection_skipped(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """``taxonomy__*`` collections carry centroid embeddings,
        not chunked text; they must be skipped from the size audit.
        """
        _seed(chroma_client, "taxonomy__centroids", [
            {"id": "topic-1", "content": "x"},
        ])

        class _FakeT3:
            _client = chroma_client

            def get_collection(self, name):
                return chroma_client.get_collection(name)

            def list_collections(self):
                return [{"name": "taxonomy__centroids"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--chunk-size-distribution", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)["chunk_size_distribution"]
        assert "taxonomy__centroids" not in payload["tables"]


# ── --chunk-text-dedup ────────────────────────────────────────────────────


class TestChunkTextDedup:
    def test_no_dupes_passes(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _seed(chroma_client, "code__cleanup", [
            {"id": "c1", "content": "a", "metadata": {"chunk_text_hash": "h1"}},
            {"id": "c2", "content": "b", "metadata": {"chunk_text_hash": "h2"}},
        ])

        class _FakeT3:
            _client = chroma_client

            def get_collection(self, name):
                return chroma_client.get_collection(name)

            def list_collections(self):
                return [{"name": "code__cleanup"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--chunk-text-dedup", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)["chunk_text_dedup"]
        assert payload["pass"] is True
        assert payload["within"]["code__cleanup"]["dupe_chunks"] == 0
        assert payload["cross_dupe_chunk_count"] == 0

    def test_within_collection_high_dupe_ratio_warns(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """When > 5% of chunks share a chunk_text_hash within one
        collection, the per-collection ``warn`` flag fires. The
        scenario simulates a chunker bug producing duplicate chunks
        from distinct positions.
        """
        # 4 chunks, 2 share a hash → 50% dupe ratio.
        _seed(chroma_client, "code__bug", [
            {"id": "c1", "content": "a", "metadata": {"chunk_text_hash": "h1"}},
            {"id": "c2", "content": "b", "metadata": {"chunk_text_hash": "h1"}},
            {"id": "c3", "content": "c", "metadata": {"chunk_text_hash": "h3"}},
            {"id": "c4", "content": "d", "metadata": {"chunk_text_hash": "h4"}},
        ])

        class _FakeT3:
            _client = chroma_client

            def get_collection(self, name):
                return chroma_client.get_collection(name)

            def list_collections(self):
                return [{"name": "code__bug"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--chunk-text-dedup", "--json"],
        )
        # WARN, not FAIL; overall pass is True unless an exception fires.
        assert result.exit_code == 0
        payload = json.loads(result.stdout)["chunk_text_dedup"]
        t = payload["within"]["code__bug"]
        assert t["dupe_chunks"] == 2
        assert t["warn"] is True

    def test_cross_collection_dupes_surfaced(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A chash appearing in 2+ collections is a cross-ingest
        signal (fixture re-import, multi-corpus leak). Lock that
        the report includes the chash + the involved collections.
        """
        _seed(chroma_client, "code__a", [
            {"id": "c1", "content": "x", "metadata": {"chunk_text_hash": "shared-1234"}},
        ])
        _seed(chroma_client, "code__b", [
            {"id": "c2", "content": "x", "metadata": {"chunk_text_hash": "shared-1234"}},
        ])

        class _FakeT3:
            _client = chroma_client

            def get_collection(self, name):
                return chroma_client.get_collection(name)

            def list_collections(self):
                return [{"name": "code__a"}, {"name": "code__b"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--chunk-text-dedup", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)["chunk_text_dedup"]
        assert payload["cross_dupe_chunk_count"] == 1
        sample = payload["cross_sample"][0]
        assert sample["chash"].startswith("shared-1234")
        assert sample["collections"] == ["code__a", "code__b"]


# ── --t3-vs-catalog ───────────────────────────────────────────────────────


class TestT3VsCatalog:
    def test_clean_state_passes(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """T3 collection has chunks AND catalog has docs referencing
        it. No drift in either direction.
        """
        from nexus.catalog.catalog import Catalog
        Catalog.init(isolated_nexus)
        cat = Catalog(isolated_nexus, isolated_nexus / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(
            owner, "doc.md", content_type="prose",
            file_path="doc.md", physical_collection="docs__clean",
        )
        cat._db.close()

        _seed(chroma_client, "docs__clean", [
            {"id": "c1", "content": "x"},
        ])

        class _FakeT3:
            _client = chroma_client

            def get_collection(self, name):
                return chroma_client.get_collection(name)

            def list_collections(self):
                return [{"name": "docs__clean"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--t3-vs-catalog", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)["t3_vs_catalog"]
        assert payload["pass"] is True
        assert payload["t3_orphans"] == []
        assert payload["zombies"] == []
        assert payload["docs_pointing_at_missing_t3"] == []

    def test_t3_orphan_detected(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A T3 collection with chunks but zero catalog docs is an
        orphan; report it and FAIL.
        """
        from nexus.catalog.catalog import Catalog
        Catalog.init(isolated_nexus)

        _seed(chroma_client, "code__orphan", [
            {"id": "c1", "content": "x"},
        ])

        class _FakeT3:
            _client = chroma_client

            def get_collection(self, name):
                return chroma_client.get_collection(name)

            def list_collections(self):
                return [{"name": "code__orphan"}]

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--t3-vs-catalog", "--json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.stdout)["t3_vs_catalog"]
        assert payload["pass"] is False
        assert len(payload["t3_orphans"]) == 1
        assert payload["t3_orphans"][0]["name"] == "code__orphan"
        assert payload["t3_orphans"][0]["chunk_count"] == 1

    def test_doc_pointing_at_missing_t3_detected(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A catalog document whose ``physical_collection`` is gone
        from T3 (operator deleted it without going through
        supersede) lands in ``docs_pointing_at_missing_t3``.
        """
        from nexus.catalog.catalog import Catalog
        Catalog.init(isolated_nexus)
        cat = Catalog(isolated_nexus, isolated_nexus / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(
            owner, "ghost.md", content_type="prose",
            file_path="ghost.md",
            physical_collection="docs__ghost_t3",
        )
        cat._db.close()

        class _FakeT3:
            _client = chroma_client

            def get_collection(self, name):
                return chroma_client.get_collection(name)

            def list_collections(self):
                return []  # no T3 collections at all

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--t3-vs-catalog", "--json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.stdout)["t3_vs_catalog"]
        assert payload["pass"] is False
        missing = payload["docs_pointing_at_missing_t3"]
        assert len(missing) == 1
        assert missing[0]["physical_collection"] == "docs__ghost_t3"


# ── nexus-b6enc: --store-put-integrity ────────────────────────────────────


class TestStorePutIntegrity:
    """store_put-origin integrity: drift (chunk_count vs manifest) and
    ghosts (row + zero manifest + zero chunks), fatal on both, with
    title+tumbler reported for ghosts so content can be re-created."""

    @staticmethod
    def _seed_doc(
        isolated_nexus: Path, title: str, *, chash: str,
        chunk_count: int = 0, with_manifest: bool = False,
    ) -> str:
        from nexus.catalog.catalog import Catalog
        cat = Catalog(isolated_nexus, isolated_nexus / ".catalog.db")
        owner_t = cat.curator_owner_tumbler_by_name("knowledge")
        owner = owner_t or cat.register_owner("knowledge", "curator")
        t = cat.register(
            owner, title, content_type="knowledge",
            physical_collection="knowledge__seeded",
            chunk_count=chunk_count,
            meta={"doc_id": chash},
        )
        if with_manifest:
            cat.atomic_manifest_replace(str(t), [{
                "chash": chash, "position": 0, "chunk_index": 0,
                "line_start": None, "line_end": None,
                "char_start": None, "char_end": None,
            }])
        cat._db.close()
        return str(t)

    class _FakeT3:
        def __init__(self, present_ids: set[str] | None = None):
            self._present = present_ids or set()

        def get_by_id(self, collection, doc_id):
            return {"id": doc_id} if doc_id in self._present else None

    def test_clean_seeded_doc_passes_non_vacuous(
        self, isolated_nexus, runner, monkeypatch: pytest.MonkeyPatch,
    ):
        """A healthy store_put doc (chunk_count==manifest==1, chunk in
        T3) passes — and ``checked`` proves the scan was non-vacuous."""
        from nexus.catalog.catalog import Catalog
        Catalog.init(isolated_nexus)
        chash = "a" * 64
        self._seed_doc(
            isolated_nexus, "healthy-note", chash=chash, with_manifest=True,
        )
        monkeypatch.setattr(
            "nexus.db.make_t3", lambda: self._FakeT3({chash}),
        )
        result = runner.invoke(doctor_cmd, ["--store-put-integrity", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)["store_put_integrity"]
        assert payload["pass"] is True
        assert payload["checked"] == 1, (
            "non-vacuity: the check must have scanned the seeded doc"
        )
        assert payload["drift"] == []
        assert payload["ghosts"] == []

    def test_clean_install_passes(
        self, isolated_nexus, runner, monkeypatch: pytest.MonkeyPatch,
    ):
        """Zero store_put-origin docs: PASS with checked=0 reported
        honestly (no false drift/ghost on a clean install)."""
        from nexus.catalog.catalog import Catalog
        Catalog.init(isolated_nexus)
        monkeypatch.setattr("nexus.db.make_t3", lambda: self._FakeT3())
        result = runner.invoke(doctor_cmd, ["--store-put-integrity", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)["store_put_integrity"]
        assert payload["pass"] is True
        assert payload["checked"] == 0

    def test_drift_detected(
        self, isolated_nexus, runner, monkeypatch: pytest.MonkeyPatch,
    ):
        """chunk_count=1 with zero manifest rows (the C3 swallow damage
        class / the migration verbatim-import class) FAILS."""
        from nexus.catalog.catalog import Catalog
        Catalog.init(isolated_nexus)
        chash = "b" * 64
        self._seed_doc(
            isolated_nexus, "drifted-note", chash=chash, chunk_count=1,
        )
        monkeypatch.setattr(
            "nexus.db.make_t3", lambda: self._FakeT3({chash}),
        )
        result = runner.invoke(doctor_cmd, ["--store-put-integrity", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout)["store_put_integrity"]
        assert payload["pass"] is False
        assert len(payload["drift"]) == 1
        d = payload["drift"][0]
        assert d["title"] == "drifted-note"
        assert d["chunk_count"] == 1
        assert d["manifest_count"] == 0

    def test_ghost_detected_fatal_with_title_and_tumbler(
        self, isolated_nexus, runner, monkeypatch: pytest.MonkeyPatch,
    ):
        """Row + zero manifest + zero chunks = ghost: FATAL, reported by
        TITLE and TUMBLER so the content can be re-created while it is
        still remembered (nexus-b6enc record section c)."""
        from nexus.catalog.catalog import Catalog
        Catalog.init(isolated_nexus)
        chash = "c" * 64
        tumbler = self._seed_doc(
            isolated_nexus, "ghost-note", chash=chash, chunk_count=0,
        )
        # T3 has NO chunk for this id.
        monkeypatch.setattr("nexus.db.make_t3", lambda: self._FakeT3())
        result = runner.invoke(doctor_cmd, ["--store-put-integrity", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout)["store_put_integrity"]
        assert payload["pass"] is False
        assert payload["ghosts"] == [
            {"tumbler": tumbler, "title": "ghost-note"}
        ]

    def test_orphan_chunk_without_manifest_is_not_a_ghost(
        self, isolated_nexus, runner, monkeypatch: pytest.MonkeyPatch,
    ):
        """chunk_count==0 and zero manifest but the chunk EXISTS in T3
        (the C2 catalog-hook-swallow direction — content recoverable):
        not a ghost; counts agree (0==0) so no drift either."""
        from nexus.catalog.catalog import Catalog
        Catalog.init(isolated_nexus)
        chash = "d" * 64
        self._seed_doc(
            isolated_nexus, "recoverable-note", chash=chash, chunk_count=0,
        )
        monkeypatch.setattr(
            "nexus.db.make_t3", lambda: self._FakeT3({chash}),
        )
        result = runner.invoke(doctor_cmd, ["--store-put-integrity", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)["store_put_integrity"]
        assert payload["ghosts"] == []

    def test_t3_unavailable_routes_to_unverifiable_not_ghost(
        self, isolated_nexus, runner, monkeypatch: pytest.MonkeyPatch,
    ):
        """make_t3() failing (critic Sig 1): a ghost CANDIDATE must land
        in the non-fatal ``unverifiable`` bucket — never a "content is
        GONE" verdict the check could not verify."""
        from nexus.catalog.catalog import Catalog
        Catalog.init(isolated_nexus)
        chash = "f" * 64
        tumbler = self._seed_doc(
            isolated_nexus, "maybe-ghost", chash=chash, chunk_count=0,
        )
        monkeypatch.setattr(
            "nexus.db.make_t3",
            lambda: (_ for _ in ()).throw(RuntimeError("T3 down")),
        )
        result = runner.invoke(doctor_cmd, ["--store-put-integrity", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)["store_put_integrity"]
        assert payload["pass"] is True
        assert payload["ghosts"] == [], (
            "an unverified candidate must never be classified as a ghost"
        )
        assert len(payload["unverifiable"]) == 1
        u = payload["unverifiable"][0]
        assert u["tumbler"] == tumbler and u["title"] == "maybe-ghost"
        assert "T3 unavailable" in u["reason"]

        # Text mode: WARN with the named doc, and NEVER the GONE claim.
        text = runner.invoke(doctor_cmd, ["--store-put-integrity"])
        assert text.exit_code == 0, text.output
        assert "GONE" not in text.output, (
            "'content is GONE' must never be printed for unverified docs"
        )
        assert "unverifiable" in text.output
        assert "maybe-ghost" in text.output

    def test_per_doc_transient_error_routes_to_unverifiable_not_ghost(
        self, isolated_nexus, runner, monkeypatch: pytest.MonkeyPatch,
    ):
        """CRE Imp 2: get_by_id already maps a missing collection to
        ``None``, so a RAISE from the per-doc lookup is transient
        (timeout/auth/network) — unverifiable, never a false GONE."""
        from nexus.catalog.catalog import Catalog
        Catalog.init(isolated_nexus)
        chash = "9" * 64
        tumbler = self._seed_doc(
            isolated_nexus, "flaky-lookup", chash=chash, chunk_count=0,
        )

        class _FlakyT3:
            def get_by_id(self, collection, doc_id):
                raise TimeoutError("engine read timed out")

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FlakyT3())
        result = runner.invoke(doctor_cmd, ["--store-put-integrity", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)["store_put_integrity"]
        assert payload["ghosts"] == []
        assert len(payload["unverifiable"]) == 1
        u = payload["unverifiable"][0]
        assert u["tumbler"] == tumbler and u["title"] == "flaky-lookup"
        assert "chunk lookup failed" in u["reason"]

        text = runner.invoke(doctor_cmd, ["--store-put-integrity"])
        assert "GONE" not in text.output

    def test_file_backed_docs_out_of_scope(
        self, isolated_nexus, runner, monkeypatch: pytest.MonkeyPatch,
    ):
        """Indexer-origin docs (file_path set) are not store_put-origin
        and must not be scanned by this check."""
        from nexus.catalog.catalog import Catalog
        Catalog.init(isolated_nexus)
        cat = Catalog(isolated_nexus, isolated_nexus / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(
            owner, "indexed.md", content_type="knowledge",
            file_path="notes/indexed.md",
            physical_collection="knowledge__seeded",
            chunk_count=5,  # drift-shaped, but out of scope
            meta={"doc_id": "e" * 64},
        )
        cat._db.close()
        monkeypatch.setattr("nexus.db.make_t3", lambda: self._FakeT3())
        result = runner.invoke(doctor_cmd, ["--store-put-integrity", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)["store_put_integrity"]
        assert payload["checked"] == 0


# ── Usage ─────────────────────────────────────────────────────────────────


class TestUsage:
    def test_no_flag_lists_all_six_checks(self, runner) -> None:
        """The usage error must enumerate every flag so an operator
        running ``nx catalog doctor`` blind sees every option, not
        just the original three.
        """
        result = runner.invoke(doctor_cmd, [])
        assert result.exit_code != 0
        out = result.output + (result.stderr or "")
        for flag in (
            "--replay-equality", "--t3-doc-id-coverage",
            "--collections-drift", "--chunk-size-distribution",
            "--chunk-text-dedup", "--t3-vs-catalog",
        ):
            assert flag in out, f"usage error must list {flag}"
