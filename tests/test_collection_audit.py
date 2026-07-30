# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx collection audit <name>`` — RDR-087 Phase 4.2.

Four sections in one report: distance histogram, top-5 cross-projections,
orphan chunks, hub-topic assignments. Section 1 (distance histogram)
ships telemetry-only in this bead; the live-probe fallback is deferred
to follow-up bead ``nexus-fx2d``.

CATALOG SUBSTRATE (nexus-i711w terminal deletion). Seven of the eleven tests
— the live-distance-probe section and the chash-coverage section — never
touch a catalog at all and are unaffected.

The remaining FOUR are PORT-BLOCKED on nexus-e9ru2 and are annotated at
their own sites: ``TestOrphanChunks`` (both) plus the two
``TestCollectionAuditCli`` tests that monkeypatch ``_open_catalog_conn``.
They pass a raw ``sqlite3.Connection`` into ``compute_orphan_chunks``, whose
production ``_open_catalog_conn`` seam now returns ``None`` (degraded by
design) but survives precisely as this fixture's injection point. The
fixture carries its own two-table DDL (``_ORPHAN_AUDIT_DDL``) since
``nexus/catalog/catalog_db.py`` is deleted. They are the sole coverage of
``compute_orphan_chunks`` repo-wide, so they stay rather than being dropped.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner
from tests.conftest import make_vector_test_client


def _clean_ephemeral_client():
    """Return a chromadb.EphemeralClient with all collections cleared.

    make_vector_test_client() instances share an in-memory backend
    across the process, so a hardcoded ``create_collection(name=...)``
    in test N collides with a leftover from test N-1 when both ran in
    the same suite. Clearing collections at entry guarantees per-test
    isolation. See project memory: chromadb_ephemeral_shared_state.
    """
    client = make_vector_test_client()
    for col in list(client.list_collections()):
        try:
            client.delete_collection(col.name)
        except Exception:
            pass
    return client


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed_t2(path: Path) -> None:
    """Build a T2 DB with topics, topic_assignments, and search_telemetry
    seeded for a ``code__main`` collection under audit plus a few others
    so cross-projection + hub queries have data."""
    from nexus.db.storage_mode import has_raw_access
    from nexus.db.t2 import T2Database

    db = T2Database(path)
    if not has_raw_access(db.taxonomy):
        # Engine substrate (RDR-155 P4b P0a'): the audit's T2 diagnostic
        # sections are service-mode-degraded by design (nexus-9613q.4), so
        # there is nothing meaningful to seed — callers that assert on
        # SQLite-populated sections carry a dies-roster skip instead.
        db.close()
        return
    c = db.taxonomy.conn
    c.executemany(
        "INSERT OR IGNORE INTO topics "
        "(id, label, collection, created_at) VALUES (?, ?, ?, ?)",
        [
            (1, "auth",    "code__main",    "2026-04-01"),
            (2, "search",  "code__main",    "2026-04-01"),
            (3, "db",      "docs__alpha",   "2026-04-01"),
            (4, "misc",    "code__other",   "2026-04-01"),
            (5, "hub-A",   "code__main",    "2026-04-01"),  # high-src hub
            (6, "hub-B",   "code__main",    "2026-04-01"),
        ],
    )
    # topic_assignments:
    # - topic 3 (docs__alpha) gets multiple chunks from code__main → cross-projection pair.
    # - topic 4 (code__other) gets 1 chunk from code__main → another pair.
    # - topics 5 and 6 get chunks from many source collections → they're cross-coll hubs.
    c.executemany(
        "INSERT INTO topic_assignments "
        "(doc_id, topic_id, assigned_by, similarity, assigned_at, source_collection) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            # code__main → docs__alpha/db (3 shared docs, avg sim 0.7)
            ("cm1", 3, "projection", 0.8, "2026-04-01", "code__main"),
            ("cm2", 3, "projection", 0.7, "2026-04-01", "code__main"),
            ("cm3", 3, "projection", 0.6, "2026-04-01", "code__main"),
            # code__main → code__other/misc (1 shared doc, avg sim 0.5)
            ("cm4", 4, "projection", 0.5, "2026-04-01", "code__main"),
            # Hub-A (topic 5) gets chunks from 3 source collections → hub
            ("cm5", 5, "projection", 0.9, "2026-04-01", "code__main"),
            ("hx",  5, "projection", 0.9, "2026-04-01", "docs__alpha"),
            ("hy",  5, "projection", 0.9, "2026-04-01", "code__other"),
            # Hub-B (topic 6) gets chunks from 2 source collections
            ("cm6", 6, "projection", 0.85, "2026-04-01", "code__main"),
            ("hy2", 6, "projection", 0.85, "2026-04-01", "code__other"),
        ],
    )
    c.commit()
    # search_telemetry: seed 15 rows for code__main in the last 30d
    # with top_distance values spread across buckets.
    now = datetime.now(UTC)
    tel_rows = []
    dists = [0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 1.05,
             1.25, 0.15, 0.25, 0.35, 0.45]  # 15 samples
    for i, d in enumerate(dists):
        ts = (now - timedelta(days=1)).isoformat()
        tel_rows.append(
            (ts, f"hash{i:04d}", "code__main", 5, 3, d, 0.45),
        )
    db.telemetry.log_search_batch(tel_rows)
    db.close()


#: Minimal DDL for the two tables ``compute_orphan_chunks`` anti-joins.
#: nexus-i711w terminal deletion: this used to import ``_SCHEMA_SQL`` from
#: ``nexus.catalog.catalog_db``, which is deleted. The production seam these
#: tests exercise SURVIVES on purpose (``_open_catalog_conn`` stays a
#: monkeypatchable function precisely for this fixture — see its docstring),
#: so the fixture carries its own schema for the columns the query reads.
_ORPHAN_AUDIT_DDL = """
CREATE TABLE documents (
    tumbler TEXT PRIMARY KEY,
    title TEXT,
    author TEXT,
    year INTEGER,
    content_type TEXT,
    file_path TEXT,
    corpus TEXT,
    physical_collection TEXT,
    chunk_count INTEGER,
    head_hash TEXT,
    indexed_at TEXT,
    metadata TEXT
);
CREATE TABLE links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_tumbler TEXT,
    to_tumbler TEXT,
    link_type TEXT,
    created_by TEXT
);
"""


def _seed_catalog_conn(db_path: Path) -> "sqlite3.Connection":
    """Build a minimal catalog-shaped SQLite DB for the orphan-audit seam.

    nexus-e9ru2 / nexus-i711w: ``compute_orphan_chunks`` still takes a raw
    ``sqlite3.Connection`` (its only remaining audience is a pre-migration
    SQLite catalog handed in via the ``_open_catalog_conn`` monkeypatch
    seam); its four callers here remain its sole repo-wide coverage until
    e9ru2 provides a service-side orphan query.
    """
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.executescript(_ORPHAN_AUDIT_DDL)
    old_ts = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    new_ts = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    conn.executemany(
        "INSERT INTO documents "
        "(tumbler, title, author, year, content_type, file_path, corpus, "
        " physical_collection, chunk_count, head_hash, indexed_at, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("1.1", "linked doc (old)", "t", 2026, "code", "src/linked.py",
             "code", "code__main", 5, "h1", old_ts, "{}"),
            ("1.2", "orphan old", "t", 2026, "code", "src/orphan.py",
             "code", "code__main", 3, "h2", old_ts, "{}"),
            ("1.3", "orphan recent", "t", 2026, "code", "src/recent.py",
             "code", "code__main", 1, "h3", new_ts, "{}"),
        ],
    )
    # Incoming link onto 1.1 — makes it non-orphan.
    conn.execute(
        "INSERT INTO links (from_tumbler, to_tumbler, link_type, created_by) "
        "VALUES (?, ?, ?, ?)",
        ("2.1", "1.1", "cites", "test"),
    )
    conn.commit()
    return conn


# ── Section 3: orphan chunks ────────────────────────────────────────────────


class TestOrphanChunks:
    """PORT-BLOCKED on nexus-e9ru2 — DO NOT convert, weaken, or delete.

    ``compute_orphan_chunks`` takes a raw ``sqlite3.Connection`` and runs a
    local-catalog ``documents``/``links`` anti-join. In service mode the audit
    is degraded BY DESIGN (``src/nexus/collection_audit.py``:376-382) — there
    is no service-side orphan query to point these at, so a "port" could only
    assert that nothing is computed. These two are the ONLY coverage of
    ``compute_orphan_chunks`` repo-wide and become its regression tests when
    e9ru2 provides a service-side equivalent.
    """

    def test_flags_old_unlinked_documents(self, tmp_path: Path) -> None:
        from nexus.collection_audit import compute_orphan_chunks

        conn = _seed_catalog_conn(tmp_path / "catalog.db")
        try:
            orphans = compute_orphan_chunks(
                conn, "code__main", age_days=30, limit=20,
            )
        finally:
            conn.close()
        # 1.1 has incoming link → not orphan.
        # 1.2 is old + unlinked → orphan.
        # 1.3 is unlinked but young (5d) → not orphan.
        tumblers = {o.tumbler for o in orphans}
        assert "1.2" in tumblers
        assert "1.1" not in tumblers
        assert "1.3" not in tumblers

    def test_empty_when_collection_clean(self, tmp_path: Path) -> None:
        from nexus.collection_audit import compute_orphan_chunks

        conn = _seed_catalog_conn(tmp_path / "catalog.db")
        try:
            orphans = compute_orphan_chunks(
                conn, "code__notreal", age_days=30, limit=20,
            )
        finally:
            conn.close()
        assert orphans == []


# ── Section 1: live-probe fallback (nexus-fx2d) ─────────────────────────────


class TestLiveDistanceProbe:
    """Live probe reuses stored embeddings — no Voyage API, no network
    when backed by an EphemeralClient."""

    def _ephemeral_collection_with_embeddings(self, name: str):
        """Seed a Chroma EphemeralClient collection with N deterministic
        embeddings so ``col.query`` returns repeatable distances."""
        from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction

        client = _clean_ephemeral_client()
        ef = DefaultEmbeddingFunction()
        col = client.create_collection(name=name, embedding_function=ef)
        col.add(
            ids=[f"d{i}" for i in range(6)],
            documents=[
                "authentication handler for OIDC flows",
                "database migrations via Alembic",
                "caching layer with Redis backing",
                "async task queue over RabbitMQ",
                "observability via OpenTelemetry spans",
                "GraphQL schema with federation directives",
            ],
            metadatas=[{"idx": i} for i in range(6)],
        )
        return col

    def test_sample_live_distances_returns_values_for_each_seed(self) -> None:
        from nexus.collection_audit import sample_live_distances

        col = self._ephemeral_collection_with_embeddings("code__live_probe")

        class FakeT3:
            def get_or_create_collection(self_, name):
                assert name == "code__live_probe"
                return col

        distances = sample_live_distances("code__live_probe", FakeT3(), n=6)
        # Each seed → one nearest-other distance. Self should have been
        # excluded (position [0] is self; we take [1]).
        assert len(distances) == 6
        # MiniLM self → nearest-other distance must be >0 (else we
        # accidentally returned self).
        assert all(d > 0 for d in distances)

    def test_compute_live_histogram_marks_source_live(self) -> None:
        from nexus.collection_audit import compute_live_distance_histogram

        col = self._ephemeral_collection_with_embeddings("code__live_mark")

        class FakeT3:
            def get_or_create_collection(self_, name):
                return col

        hist = compute_live_distance_histogram("code__live_mark", FakeT3(), n=6)
        assert hist.source == "live"
        assert hist.sample_size == 6
        assert sum(hist.buckets) == 6
        assert len(hist.buckets) == 10

    def test_live_histogram_empty_when_collection_empty(self) -> None:
        from nexus.collection_audit import compute_live_distance_histogram
        from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction

        client = _clean_ephemeral_client()
        ef = DefaultEmbeddingFunction()
        empty = client.create_collection(name="code__empty", embedding_function=ef)

        class FakeT3:
            def get_or_create_collection(self_, name):
                return empty

        hist = compute_live_distance_histogram("code__empty", FakeT3(), n=5)
        assert hist.source == "empty"
        assert hist.sample_size == 0


    def test_run_audit_falls_back_to_live_when_telemetry_empty(
        self, tmp_path: Path,
    ) -> None:
        from nexus.collection_audit import run_collection_audit
        from unittest.mock import patch

        _seed_t2(tmp_path / "memory.db")
        col = self._ephemeral_collection_with_embeddings("code__fallback_live")

        class FakeT3:
            def get_or_create_collection(self_, name):
                return col

        with patch("nexus.config.default_db_path", return_value=tmp_path / "memory.db"):
            report = run_collection_audit(
                "code__fallback_live", live=True, t3=FakeT3(), live_n=6,
            )
        assert report.distance_histogram.source == "live"
        assert report.distance_histogram.sample_size == 6


# ── Section 5: chash_index coverage (RDR-087 Phase 4.6 / nexus-c2op) ────────


class _FakeChashIndex:
    """Stand-in for ``HttpChashIndex`` — the only chash index since
    nexus-i711w Stage 2 sub-stage A deleted the SQLite ``ChashIndex``.
    ``compute_chash_coverage`` constructs it via the function-local
    ``nexus.db.t2.http_chash_index.HttpChashIndex`` seam, so tests
    monkeypatch that attribute with a factory returning this fake."""

    def __init__(self, count: int = 0, chashes: set[str] | None = None) -> None:
        self._count = count
        self._chashes = chashes or set()
        self.closed = False

    def count_for_collection(self, collection: str) -> int:
        return self._count

    def registered_chashes_for_collection(self, collection: str) -> set[str]:
        return set(self._chashes)

    def close(self) -> None:
        self.closed = True


class TestChashCoverageSection:
    """Audit section 5 ratio + missing_sample shape.

    The production ``compute_chash_coverage`` hits T3 for the total
    chunk count and the HTTP chash index for the indexed-row count. Both
    are stubbed (``_FakeChashIndex`` + a fake T3) so the test is
    deterministic without network. (Ported off the deleted SQLite
    ``ChashIndex`` seed in nexus-i711w Stage 2 sub-stage A2.)
    """

    def test_empty_t3_collection_returns_none_ratio(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from nexus.collection_audit import compute_chash_coverage

        idx = _FakeChashIndex(count=0)
        monkeypatch.setattr(
            "nexus.db.t2.http_chash_index.HttpChashIndex", lambda: idx,
        )

        class _FakeCol:
            def count(self): return 0
        class _FakeT3:
            def get_or_create_collection(self, _n): return _FakeCol()
            # nexus-8lbe: compute_chash_coverage now uses get_collection
            def get_collection(self, _n): return _FakeCol()

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        cov = compute_chash_coverage("code__empty")
        assert cov is not None
        assert cov.total_chunks == 0
        assert cov.indexed_rows == 0
        assert cov.ratio is None
        assert idx.closed, "coverage probe must close the chash index"


    def test_missing_t3_collection_does_not_create_zombie(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """nexus-8lbe (RDR-108 Phase 4 review CR-M3): the audit must
        NOT speculatively create a T3 collection while computing
        chash coverage. Pre-fix it called
        ``get_or_create_collection`` and minted an empty zombie any
        time the audit ran on a collection name that didn't yet
        exist in T3 — the same leak ks40 fixed in three indexer
        paths but missed in collection_audit.

        Post-fix: ``get_collection`` raises NotFoundError; the
        audit catches and returns total_chunks=None.
        ``get_or_create_collection`` MUST NOT be called.
        """
        from nexus.errors import CollectionNotFoundError as _ChromaNotFoundError

        from nexus.collection_audit import compute_chash_coverage

        monkeypatch.setattr(
            "nexus.db.t2.http_chash_index.HttpChashIndex",
            lambda: _FakeChashIndex(count=0),
        )

        get_or_create_calls: list[str] = []

        class _FakeT3:
            def get_or_create_collection(self, name):
                get_or_create_calls.append(name)
                class _C:
                    def count(self_inner): return 0
                return _C()

            def get_collection(self, name):
                raise _ChromaNotFoundError(f"Collection {name} not found")

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        cov = compute_chash_coverage("code__never_created")

        assert get_or_create_calls == [], (
            "audit must NOT speculatively create T3 collection; "
            f"get_or_create_collection was called with {get_or_create_calls!r}"
        )
        assert cov is not None
        assert cov.total_chunks is None
        assert cov.ratio is None
        assert cov.missing_sample == []


# ── CLI integration ─────────────────────────────────────────────────────────


class TestCollectionAuditCli:
    """The first two tests here are PORT-BLOCKED on nexus-e9ru2 alongside
    ``TestOrphanChunks``: both monkeypatch
    ``nexus.collection_audit._open_catalog_conn`` to hand the verb a raw
    local-catalog ``sqlite3.Connection`` seeded by ``_seed_catalog_conn``, and
    both assert the ORPHAN section is present in the report. In service mode
    that section is degraded by design, so the assertion has no service-mode
    form yet. The third (``--live``) test needs no catalog and is unaffected.
    """

    def test_default_output_covers_four_sections(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        """PORT-BLOCKED — nexus-e9ru2. Asserts the ``orphan`` section renders;
        see the class docstring.
        """
        from nexus.cli import main

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        catalog_db_path = tmp_path / "catalog.db"
        _seed_catalog_conn(catalog_db_path).close()

        monkeypatch.setattr(
            "nexus.config.default_db_path", lambda: db_path,
        )
        monkeypatch.setattr(
            "nexus.collection_audit._open_catalog_conn",
            lambda: __import__("sqlite3").connect(str(catalog_db_path)),
        )

        result = runner.invoke(
            main, ["collection", "audit", "code__main"],
        )
        assert result.exit_code == 0, result.output
        out = result.output.lower()
        for section_hint in ["distance histogram", "cross-projection", "orphan", "hub"]:
            assert section_hint in out

    def test_json_flag_emits_parseable_payload(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        """PORT-BLOCKED — nexus-e9ru2. Asserts ``payload["orphans"]``; see the
        class docstring.
        """
        import json
        from nexus.cli import main

        db_path = tmp_path / "memory.db"
        _seed_t2(db_path)
        catalog_db_path = tmp_path / "catalog.db"
        _seed_catalog_conn(catalog_db_path).close()

        monkeypatch.setattr(
            "nexus.config.default_db_path", lambda: db_path,
        )
        monkeypatch.setattr(
            "nexus.collection_audit._open_catalog_conn",
            lambda: __import__("sqlite3").connect(str(catalog_db_path)),
        )

        result = runner.invoke(
            main, ["collection", "audit", "code__main", "--format", "json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["collection"] == "code__main"
        assert "distance_histogram" in payload
        assert "cross_projections" in payload
        assert "orphans" in payload
        assert "hub_assignments" in payload

    def test_live_flag_populates_histogram_when_telemetry_cold(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        """nexus-fx2d — ``--live`` promotes source="empty" → source="live"
        by probing ChromaDB for chunks that have never been searched."""
        import json
        from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
        from unittest.mock import patch

        from nexus.cli import main
        from nexus.db.t2 import T2Database

        # T2 with zero search_telemetry rows for code__live_cli.
        db_path = tmp_path / "memory.db"
        with T2Database(db_path):
            pass
        monkeypatch.setattr(
            "nexus.config.default_db_path", lambda: db_path,
        )

        # Seed an EphemeralClient collection the --live probe can sample.
        client = _clean_ephemeral_client()
        ef = DefaultEmbeddingFunction()
        col = client.create_collection(name="code__live_cli", embedding_function=ef)
        col.add(
            ids=[f"d{i}" for i in range(4)],
            documents=["auth", "cache", "queue", "graphql"],
            metadatas=[{"i": i} for i in range(4)],
        )

        class FakeT3:
            def get_or_create_collection(self_, name):
                assert name == "code__live_cli"
                return col

        with patch("nexus.db.make_t3", return_value=FakeT3()):
            result = runner.invoke(
                main,
                [
                    "collection", "audit", "code__live_cli",
                    "--live", "--live-n", "4", "--format", "json",
                ],
            )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["distance_histogram"]["source"] == "live"
        assert payload["distance_histogram"]["sample_size"] == 4
