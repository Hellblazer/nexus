# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-180 land-then-transform P2.1 (nexus-jxizy.10.6): the landing client.

The pre-land census is the client half of Hal's every-column directive:
schema-derived enumeration over the REAL source connections, fail-loud on
unclaimed chash-bearing columns, non-vacuous by rediscovering the known
inventory. The landing legs' wire shapes are pinned against the engine's
StagingHandler.STORES contract; the topics JOIN projection carries the
cross-store (label, collection) identity (critic-p1 Critical).
"""
from __future__ import annotations

import sqlite3

import pytest

from nexus.migration.staging_land import (
    CENSUS_EXCLUSIONS,
    HttpStagingStore,
    LANDING_MANIFEST,
    StagedTimestampError,
    StagingCensusError,
    chunk_rows,
    pointer_store_rows,
    source_census,
    topic_assignment_rows,
    validate_timestamp_fields,
)

LEGACY32 = "0123456789abcdef0123456789abcdef"


def _memory_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE chash_index (chash TEXT NOT NULL, physical_collection TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY (chash, physical_collection));
        CREATE TABLE topics (id INTEGER PRIMARY KEY, label TEXT NOT NULL,
            collection TEXT NOT NULL DEFAULT '');
        CREATE TABLE topic_assignments (doc_id TEXT NOT NULL, topic_id INTEGER NOT NULL,
            PRIMARY KEY (doc_id, topic_id));
        CREATE TABLE frecency (chunk_id TEXT PRIMARY KEY, embedded_at TEXT NOT NULL DEFAULT '',
            ttl_days INTEGER NOT NULL DEFAULT 0, frecency_score REAL NOT NULL DEFAULT 0,
            miss_count INTEGER NOT NULL DEFAULT 0, last_hit_at TEXT NOT NULL DEFAULT '');
        CREATE TABLE relevance_log (id INTEGER PRIMARY KEY, query TEXT NOT NULL,
            chunk_id TEXT NOT NULL, collection TEXT, action TEXT NOT NULL,
            session_id TEXT, timestamp TEXT NOT NULL);
        """
    )
    return c


def _seed_chash_index(c: sqlite3.Connection, n: int = 5) -> None:
    for i in range(n):
        c.execute("INSERT INTO chash_index VALUES (?, 'col', '2026-07-01T00:00:00Z')",
                  (f"{i:032x}",))


class TestSourceCensus:
    def test_clean_claimed_store_passes_and_rediscovers_inventory(self):
        mem = _memory_db()
        _seed_chash_index(mem)
        report = source_census({"memory": mem})
        assert any(f.table == "chash_index" and f.column == "chash"
                   for f in report.findings), "the census must rediscover the known inventory"
        assert report.unclaimed == []

    def test_unclaimed_chash_bearing_column_fails_loud(self):
        # THE missed-leg killer: a novel table no manifest names, holding
        # chash-shaped values, must refuse the landing BEFORE it starts.
        mem = _memory_db()
        _seed_chash_index(mem)
        mem.execute("CREATE TABLE mystery_store (some_ref TEXT)")
        for i in range(5):
            mem.execute("INSERT INTO mystery_store VALUES (?)", (f"{i + 7:032x}",))
        with pytest.raises(StagingCensusError, match="mystery_store.some_ref"):
            source_census({"memory": mem})

    def test_prevalence_threshold_ignores_incidental_hashes(self):
        # A free-text column with ONE hash-looking value must not classify.
        mem = _memory_db()
        _seed_chash_index(mem)
        mem.execute("CREATE TABLE notes (body TEXT)")
        mem.execute("INSERT INTO notes VALUES (?)", (LEGACY32,))
        for i in range(10):
            mem.execute("INSERT INTO notes VALUES (?)", (f"prose body {i}",))
        report = source_census({"memory": mem})
        assert not any(f.table == "notes" for f in report.findings)

    def test_manifest_and_exclusions_are_wellformed(self):
        # RDR-187 (nexus-piwya.8): the router landing leg is retired — the
        # entry MOVED to CENSUS_EXCLUSIONS (never bare-deleted: every
        # pre-RDR-187 source still HAS the table, and an unclaimed
        # chash-bearing column trips StagingCensusError on real upgrades).
        assert ("memory", "chash_index", "chash") not in LANDING_MANIFEST
        assert any(e[:3] == ("memory", "chash_index", "chash")
                   for e in CENSUS_EXCLUSIONS), (
            "chash_index must be a JUSTIFIED exclusion, not silently dropped")
        assert all(len(e) == 4 for e in CENSUS_EXCLUSIONS)

    def test_new_client_no_longer_lands_chash_index(self):
        # RDR-187 (nexus-piwya.8): the paired-release client stops
        # participating in the chash staging pipeline — landing is driven by
        # driver._POINTER_STORES (NOT the census manifest; .7 critique
        # correction), so the retirement must happen THERE. Old clients
        # still land; the engine keeps the dead-sink acceptance one release.
        from nexus.migration.driver import _POINTER_STORES
        assert "chash_index" not in _POINTER_STORES

    def test_census_still_rediscovers_excluded_chash_index(self):
        # The exclusion must not blind the census: the column is still
        # FOUND (chash-bearing), just justified — unclaimed stays empty.
        mem = _memory_db()
        _seed_chash_index(mem)
        report = source_census({"memory": mem})
        assert any(f.table == "chash_index" for f in report.findings)
        assert report.unclaimed == []


class TestTimestampGuard:
    def test_iso_and_empty_pass(self):
        validate_timestamp_fields("frecency", [
            {"chunk_id": "x", "embedded_at": "2026-07-01T00:00:00Z", "last_hit_at": ""}])

    def test_garbage_fails_naming_row_and_field(self):
        with pytest.raises(StagedTimestampError, match="row 1.*last_hit_at"):
            validate_timestamp_fields("frecency", [
                {"chunk_id": "a", "last_hit_at": ""},
                {"chunk_id": "b", "last_hit_at": "not-a-date"}])


class TestHttpStagingStoreLoad:
    def _store_with_capture(self, monkeypatch):
        store = HttpStagingStore.__new__(HttpStagingStore)
        calls: list[tuple[str, dict]] = []

        def fake_post(path, payload):
            calls.append((path, payload))
            return {"landed": len(payload["rows"])}

        monkeypatch.setattr(store, "_post", fake_post, raising=False)
        return store, calls

    def test_load_batches_at_the_wire_cap(self, monkeypatch):
        store, calls = self._store_with_capture(monkeypatch)
        rows = [{"chunk_id": f"{i:064x}"} for i in range(650)]
        landed = store.load("frecency", rows)
        assert landed == 650
        assert [len(p["rows"]) for _, p in calls] == [300, 300, 50]
        assert all(path == "/v1/staging/load/frecency" for path, _ in calls)

    def test_load_validates_timestamps_before_any_wire_call(self, monkeypatch):
        store, calls = self._store_with_capture(monkeypatch)
        with pytest.raises(StagedTimestampError):
            store.load("frecency", [{"chunk_id": "a", "embedded_at": "garbage"}])
        assert calls == []


class TestPointerStoreRows:
    def test_topics_join_projection_carries_identity_and_skips_orphans(self):
        mem = _memory_db()
        mem.execute("INSERT INTO topics VALUES (7, 'quokkas', 'knowledge__k__m__v1')")
        mem.execute("INSERT INTO topic_assignments VALUES (?, 7)", (LEGACY32,))
        mem.execute("INSERT INTO topic_assignments VALUES ('orphan-doc', 99)")
        rows = topic_assignment_rows(mem)
        assert rows == [{
            "doc_id": LEGACY32, "topic_id": 7,
            "topic_label": "quokkas", "topic_collection": "knowledge__k__m__v1"}]

    def test_document_aspects_survives_the_rdr096_source_path_drop(self):
        # dev-driver-rewire catch: migrated T2 stores DROP source_path
        # (migrate_drop_source_path_column, 4.31.0) — the landing derives
        # the wire value from source_uri, else doc_id, never ''.
        mem = _memory_db()
        mem.executescript(
            "CREATE TABLE document_aspects (doc_id TEXT NOT NULL DEFAULT '', "
            "collection TEXT NOT NULL DEFAULT '', problem_formulation TEXT, "
            "proposed_method TEXT, experimental_datasets TEXT, "
            "experimental_baselines TEXT, experimental_results TEXT, extras TEXT, "
            "confidence REAL, extracted_at TEXT NOT NULL DEFAULT '', "
            "model_version TEXT NOT NULL DEFAULT '', extractor_name TEXT NOT NULL DEFAULT '', "
            "source_uri TEXT);")
        mem.execute("INSERT INTO document_aspects (doc_id, collection, source_uri, "
                    "extracted_at, model_version, extractor_name) "
                    "VALUES ('1.2.3', 'col', 'file:///p.pdf', '2026-07-01T00:00:00Z', 'm', 'x')")
        mem.execute("INSERT INTO document_aspects (doc_id, collection, source_uri, "
                    "extracted_at, model_version, extractor_name) "
                    "VALUES ('1.2.4', 'col', '', '2026-07-01T00:00:00Z', 'm', 'x')")
        rows = pointer_store_rows("document_aspects", None, mem)
        assert rows[0]["source_path"] == "file:///p.pdf"
        assert rows[1]["source_path"] == "1.2.4", "doc_id fallback — never an empty PK leg"

    def test_document_aspects_survives_the_pre_rdr096_fresh_schema(self):
        # --guided gate run 2 catch (nexus-jxizy.10.10): document_aspects has
        # TWO schema eras. A FRESH current-schema T2 (T2Database
        # run_migrations=True, catalog-absent) has source_path but NO doc_id
        # — doc_id only arrives with the RDR-096 P5.2 doc-id PK switch. The
        # landing SELECT must probe BOTH columns; an unconditional doc_id
        # read crashes every fresh-era source with 'no such column: doc_id'.
        mem = _memory_db()
        mem.executescript(
            "CREATE TABLE document_aspects (collection TEXT NOT NULL DEFAULT '', "
            "source_path TEXT NOT NULL DEFAULT '', problem_formulation TEXT, "
            "proposed_method TEXT, experimental_datasets TEXT, "
            "experimental_baselines TEXT, experimental_results TEXT, extras TEXT, "
            "confidence REAL, extracted_at TEXT NOT NULL DEFAULT '', "
            "model_version TEXT NOT NULL DEFAULT '', extractor_name TEXT NOT NULL DEFAULT '', "
            "source_uri TEXT, salient_sentences TEXT);")
        mem.execute("INSERT INTO document_aspects (collection, source_path, source_uri, "
                    "extracted_at, model_version, extractor_name) "
                    "VALUES ('col', '/p.pdf', 'file:///p.pdf', "
                    "'2026-07-01T00:00:00Z', 'm', 'x')")
        rows = pointer_store_rows("document_aspects", None, mem)
        assert rows[0]["source_path"] == "/p.pdf"
        assert rows[0]["doc_id"] == "", "fresh era has no doc_id column — wire an empty leg"
        assert rows[0]["source_uri"] == "file:///p.pdf"

    def test_relevance_log_maps_rowid_and_ts(self):
        mem = _memory_db()
        mem.execute("INSERT INTO relevance_log (query, chunk_id, action, timestamp) "
                    "VALUES ('q', ?, 'hit', '2026-07-01T00:00:00Z')", (LEGACY32,))
        rows = pointer_store_rows("relevance_log", None, mem)
        assert rows[0]["id"] == 1
        assert rows[0]["ts"] == "2026-07-01T00:00:00Z"
        assert rows[0]["chunk_id"] == LEGACY32


class _FakeCollection:
    """Two pages: 300 + 50, ids legacy-shaped, one NULL embedding."""

    def __init__(self, n: int = 350, with_embeddings: bool = True) -> None:
        self.n = n
        self.with_embeddings = with_embeddings

    def get(self, *, limit, offset, include):
        ids = [f"{i:032x}" for i in range(offset, min(offset + limit, self.n))]
        docs = [f"text {i}" for i in range(offset, offset + len(ids))]
        embs = None
        if self.with_embeddings:
            embs = [[0.1, 0.2] for _ in ids]
            if offset == 0 and embs:
                embs[0] = None  # one reuse-miss even in a reuse-legal source
        return {"ids": ids, "documents": docs, "embeddings": embs,
                "metadatas": [{"chunk_text_hash": "a" * 64} for _ in ids]}


class TestChunkRows:
    def test_reuse_legal_stages_vectors_and_pages(self):
        batches = list(chunk_rows(
            _FakeCollection(), target_name="knowledge__k__bge-base-en-v15-768__v1",
            target_model="bge-base-en-v15-768", target_dim=768,
            source_model="bge-base-en-v15-768"))
        assert [len(b) for b in batches] == [300, 50]
        first = batches[0][0]
        assert "embedding" not in first, "a NULL source vector lands vector-less"
        assert "embedding" in batches[0][1]
        assert first["collection"] == "knowledge__k__bge-base-en-v15-768__v1"
        assert first["dim"] == 768
        assert first["legacy_ref"] == f"{0:032x}"

    def test_cross_model_source_lands_vectorless(self):
        batches = list(chunk_rows(
            _FakeCollection(n=10), target_name="knowledge__k__bge-base-en-v15-768__v1",
            target_model="bge-base-en-v15-768", target_dim=768,
            source_model="minilm-l6-v2-384"))
        assert all("embedding" not in row for b in batches for row in b), (
            "reuse is illegal across models — embed_fill covers these server-side")
