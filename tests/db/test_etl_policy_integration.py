# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-153 Phase 2 (bead nexus-cr3oo, TDD-red): per-store ETL policy
integration — the data-quality policy table applied inside the RDR-152
T2 ETLs, recording into the Phase-1 ``IssueCollector``.

Policy (RDR §Decision 2): orphan_parent → skip-and-record (skipped);
identity_mismatch → schema_corrected; format_anomaly parseable →
normalize (handled), unparseable → failed; soft_dangler →
import-but-flag (flagged); unexpected → fail-and-record (failed, NEVER
a silent drop).

Real tmp SQLite sources (integration over mocks — the source side is the
thing under test); the Postgres side is a duck-typed capture store, as
in the existing per-ETL suites (the service's INSERT…ON CONFLICT
idempotency is its own locked contract there).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus.db.t2.taxonomy_etl import migrate_taxonomy_rows
from nexus.db.t2.telemetry_etl import migrate_telemetry_rows
from nexus.migration.migration_report import IssueCollector, build_report

# ── Source fixtures ──────────────────────────────────────────────────────────

_TAXONOMY_SCHEMA = """
CREATE TABLE topics (
    id            INTEGER PRIMARY KEY,
    label         TEXT NOT NULL,
    parent_id     INTEGER,
    collection    TEXT NOT NULL,
    centroid_hash TEXT,
    doc_count     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'pending',
    terms         TEXT
);
CREATE TABLE topic_assignments (
    doc_id            TEXT NOT NULL,
    topic_id          INTEGER NOT NULL,
    assigned_by       TEXT NOT NULL DEFAULT 'hdbscan',
    similarity        REAL,
    assigned_at       TEXT,
    source_collection TEXT,
    PRIMARY KEY (doc_id, topic_id)
);
CREATE TABLE topic_links (
    from_topic_id INTEGER NOT NULL,
    to_topic_id   INTEGER NOT NULL,
    link_type     TEXT NOT NULL DEFAULT 'related',
    weight        REAL,
    created_at    TEXT
);
CREATE TABLE taxonomy_meta (
    collection              TEXT PRIMARY KEY,
    last_discover_doc_count INTEGER NOT NULL DEFAULT 0,
    last_discover_at        TEXT
);
"""

_TELEMETRY_SCHEMA = """
CREATE TABLE hook_failures (
    id            INTEGER PRIMARY KEY,
    doc_id        TEXT,
    collection    TEXT,
    hook_name     TEXT NOT NULL,
    error         TEXT,
    occurred_at   TEXT NOT NULL,
    batch_doc_ids TEXT,
    is_batch      INTEGER DEFAULT 0,
    chain         TEXT
);
CREATE TABLE nx_answer_runs (
    id                 INTEGER PRIMARY KEY,
    question           TEXT NOT NULL,
    plan_id            INTEGER,
    matched_confidence REAL,
    step_count         INTEGER DEFAULT 0,
    final_text         TEXT,
    cost_usd           REAL,
    duration_ms        INTEGER DEFAULT 0,
    created_at         TEXT NOT NULL
);
CREATE TABLE plans (
    id         INTEGER PRIMARY KEY,
    query      TEXT,
    created_at TEXT
);
"""


def _make_db(tmp_path: Path, schema: str, name: str = "src.db") -> Path:
    db = tmp_path / name
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    conn.commit()
    conn.close()
    return db


def _insert(db: Path, sql: str, rows: list[tuple]) -> None:
    conn = sqlite3.connect(db)
    conn.executemany(sql, rows)
    conn.commit()
    conn.close()


class _CaptureStore:
    """Duck-typed Postgres store: records every import call; raises for
    keys registered via ``fail_on`` (models a service-side rejection)."""

    def __init__(self) -> None:
        self.calls: dict[str, list[dict]] = {}
        self.fail_on: dict[str, int] = {}  # method -> 1-based call index

    def __getattr__(self, name: str):
        # RDR-176 P3: the batched ETLs call build_import_row (returns a row dict)
        # then an import_*_batch method (returns the batch size). A batch method's
        # rows arg is the LAST positional list; we record one entry per row (so
        # per-row .calls assertions still hold) and honour fail_on by failing the
        # WHOLE batch (a server-side rejection rejects the batch, not one row).
        if name == "build_import_row":
            return lambda **kwargs: dict(kwargs)
        if not name.startswith("import_"):
            raise AttributeError(name)

        def _record(*args, **kwargs):
            rows = next((a for a in reversed(args) if isinstance(a, list)), None)
            if rows is not None:  # batched call: import_rows_batch(table/kind, rows)
                # key per-row records by the table/kind str arg when present (so
                # tests still read calls["hook_failures"], calls["topic"], …),
                # else by method name (memory/plans batches have no kind arg).
                key = next((a for a in args if isinstance(a, str)), name)
                self.calls.setdefault(key, []).extend(rows)
                if key in self.fail_on or name in self.fail_on:
                    raise RuntimeError(f"injected failure on {name} (batch of {len(rows)})")
                return len(rows)
            seq = self.calls.setdefault(name, [])
            seq.append(kwargs or (args[0] if args else {}))
            if self.fail_on.get(name) == len(seq):
                raise RuntimeError(f"injected failure on {name} #{len(seq)}")
            return None

        return _record


# ── Taxonomy policy ──────────────────────────────────────────────────────────


class TestTaxonomyPolicy:
    def _seeded_db(self, tmp_path: Path) -> Path:
        db = _make_db(tmp_path, _TAXONOMY_SCHEMA)
        _insert(db, "INSERT INTO topics (id, label, collection, created_at) VALUES (?,?,?,?)", [
            (1, "alpha", "knowledge__x", "2026-01-01T00:00:00"),
            (2, "beta", "knowledge__x", "2026-01-01T00:00:00"),
        ])
        _insert(db, "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?,?)", [
            ("chash-a", 1),        # valid
            ("chash-b", 2),        # valid
            ("chash-c", 99),       # orphan: topic 99 deleted
            ("chash-d", 99),       # orphan
        ])
        _insert(db, "INSERT INTO topic_links (from_topic_id, to_topic_id) VALUES (?,?)", [
            (1, 2),                # valid
            (1, 99),               # orphan to-side
            (99, 2),               # orphan from-side
        ])
        return db

    def test_orphan_assignments_skipped_and_recorded(self, tmp_path: Path) -> None:
        db = self._seeded_db(tmp_path)
        store = _CaptureStore()
        collector = IssueCollector()

        migrate_taxonomy_rows(db, store, collector=collector)

        # Valid rows imported; orphans NEVER reach the store.
        imported = {c["doc_id"] for c in store.calls["assignment"]}
        assert imported == {"chash-a", "chash-b"}
        (issue,) = [
            i for i in collector.issues_for("taxonomy", "topic_assignments")
            if i.issue_class == "orphan_parent"
        ]
        assert issue.action == "skipped"
        assert issue.count == 2
        # Composite-key sample convention: <doc_id>:<topic_id>.
        assert set(issue.sample_ids) == {"chash-c:99", "chash-d:99"}

    def test_orphan_links_skipped_both_sides(self, tmp_path: Path) -> None:
        db = self._seeded_db(tmp_path)
        store = _CaptureStore()
        collector = IssueCollector()

        migrate_taxonomy_rows(db, store, collector=collector)

        links = store.calls["link"]
        assert len(links) == 1
        assert (links[0]["from_topic_id"], links[0]["to_topic_id"]) == (1, 2)
        (issue,) = [
            i for i in collector.issues_for("taxonomy", "topic_links")
            if i.issue_class == "orphan_parent"
        ]
        assert issue.action == "skipped"
        assert issue.count == 2
        assert set(issue.sample_ids) == {"1:99", "99:2"}

    def test_doc_id_is_chash_schema_correction_recorded_once(
        self, tmp_path: Path,
    ) -> None:
        db = self._seeded_db(tmp_path)
        collector = IssueCollector()
        migrate_taxonomy_rows(db, _CaptureStore(), collector=collector)
        issues = [
            i for i in collector.issues_for("taxonomy", "topic_assignments")
            if i.issue_class == "identity_mismatch"
        ]
        assert len(issues) == 1
        assert issues[0].action == "schema_corrected"
        assert issues[0].count == 1          # ONCE per run, not per row
        assert issues[0].sample_ids == []    # one-time event, no row sample

    def test_counts_recorded_per_table(self, tmp_path: Path) -> None:
        db = self._seeded_db(tmp_path)
        collector = IssueCollector()
        migrate_taxonomy_rows(db, _CaptureStore(), collector=collector)
        assert collector.table_counts("taxonomy", "topic_assignments") == {
            "read": 4, "written": 2,
        }
        assert collector.table_counts("taxonomy", "topic_links") == {
            "read": 3, "written": 1,
        }
        assert collector.table_counts("taxonomy", "topics") == {
            "read": 2, "written": 2,
        }

    def test_store_rejection_is_failed_issue_never_silent(
        self, tmp_path: Path,
    ) -> None:
        """Catch-all: a service-side rejection on one row records a failed
        issue and the run continues (no abort, no silent drop)."""
        db = self._seeded_db(tmp_path)
        store = _CaptureStore()
        store.fail_on["topic"] = True   # the topic batch write blows up
        collector = IssueCollector()

        migrate_taxonomy_rows(db, store, collector=collector)

        (issue,) = [
            i for i in collector.issues_for("taxonomy", "topics")
            if i.action == "failed"
        ]
        assert issue.issue_class == "unexpected"
        # Whole-batch failure: both topics in the batch are recorded failed
        # (re-run is idempotent). Matches the chash batch-failure contract.
        assert issue.count == 2
        assert collector.table_counts("taxonomy", "topics")["written"] == 0
        # The run continued: assignments still migrated.
        assert len(store.calls["assignment"]) == 2

    def test_rerun_is_idempotent_at_etl_level(self, tmp_path: Path) -> None:
        """Re-run after parent repair must not raise (server-side
        INSERT…ON CONFLICT DO NOTHING absorbs already-migrated rows; the
        ETL must not fail on identical inputs either)."""
        db = self._seeded_db(tmp_path)
        store = _CaptureStore()
        migrate_taxonomy_rows(db, store, collector=IssueCollector())
        collector2 = IssueCollector()
        migrate_taxonomy_rows(db, store, collector=collector2)
        # Second run reads identically; orphans recorded identically.
        assert collector2.table_counts("taxonomy", "topic_assignments") == {
            "read": 4, "written": 2,
        }

    def test_collector_optional_back_compat(self, tmp_path: Path) -> None:
        # The RDR-152 callers pass no collector; the ETL must work unchanged.
        db = self._seeded_db(tmp_path)
        result = migrate_taxonomy_rows(db, _CaptureStore())
        assert result["topics"]["written"] == 2


# ── Telemetry policy ─────────────────────────────────────────────────────────


class TestTelemetryPolicy:
    def _seeded_db(self, tmp_path: Path) -> Path:
        db = _make_db(tmp_path, _TELEMETRY_SCHEMA)
        _insert(db, "INSERT INTO plans (id, query) VALUES (?,?)", [(7, "q")])
        _insert(
            db,
            "INSERT INTO hook_failures (hook_name, error, occurred_at) VALUES (?,?,?)",
            [
                ("auto_link", "boom", "2026-04-23 10:47:54"),   # space form
                ("auto_link", "boom", "2026-04-24T09:00:00"),   # already ISO
                ("auto_link", "boom", "not-a-timestamp"),       # unparseable
            ],
        )
        _insert(
            db,
            "INSERT INTO nx_answer_runs (question, plan_id, created_at) VALUES (?,?,?)",
            [
                ("q-ok", 7, "2026-05-01T00:00:00"),     # plan exists
                ("q-dangler", 99, "2026-05-01T00:00:00"),  # plan deleted
                ("q-none", None, "2026-05-01T00:00:00"),   # NULL is fine
            ],
        )
        return db

    def test_space_form_timestamp_normalized_and_handled(
        self, tmp_path: Path,
    ) -> None:
        db = self._seeded_db(tmp_path)
        store = _CaptureStore()
        collector = IssueCollector()

        migrate_telemetry_rows(db, store, collector=collector)

        by_ts = {c["occurred_at"] for c in store.calls["hook_failures"]}
        # Canonical form is OFFSET-QUALIFIED: the Java strict import
        # parser (OffsetDateTime.parse) rejects naive timestamps — the
        # actual nexus-9sjn3 root cause (0/234 imported). Naive == UTC.
        assert "2026-04-23T10:47:54+00:00" in by_ts   # space form normalized
        assert "2026-04-24T09:00:00+00:00" in by_ts  # naive T form gains offset
        assert "2026-04-23 10:47:54" not in by_ts # space form never written
        (issue,) = [
            i for i in collector.issues_for("telemetry", "hook_failures")
            if i.action == "handled"
        ]
        assert issue.issue_class == "format_anomaly"
        assert issue.count == 2   # both naive rows were normalized

    def test_unparseable_timestamp_failed_never_silent(
        self, tmp_path: Path,
    ) -> None:
        db = self._seeded_db(tmp_path)
        store = _CaptureStore()
        collector = IssueCollector()

        migrate_telemetry_rows(db, store, collector=collector)

        (issue,) = [
            i for i in collector.issues_for("telemetry", "hook_failures")
            if i.action == "failed"
        ]
        assert issue.issue_class == "format_anomaly"
        assert issue.count == 1
        # The unparseable row was NOT written.
        assert len(store.calls["hook_failures"]) == 2
        assert collector.table_counts("telemetry", "hook_failures") == {
            "read": 3, "written": 2,
        }

    def test_plan_danglers_imported_and_flagged(self, tmp_path: Path) -> None:
        """Soft dangler policy: the row IMPORTS (no FK enforced) and an
        advisory records the dangling reference."""
        db = self._seeded_db(tmp_path)
        store = _CaptureStore()
        collector = IssueCollector()

        migrate_telemetry_rows(db, store, collector=collector)

        # All three runs imported — danglers are not dropped.
        assert len(store.calls["nx_answer_runs"]) == 3
        (issue,) = [
            i for i in collector.issues_for("telemetry", "nx_answer_runs")
            if i.action == "flagged"
        ]
        assert issue.issue_class == "soft_dangler"
        assert issue.count == 1               # only plan_id=99; NULL is not a dangler
        assert collector.table_counts("telemetry", "nx_answer_runs") == {
            "read": 3, "written": 3,
        }

    def test_collector_optional_back_compat(self, tmp_path: Path) -> None:
        db = self._seeded_db(tmp_path)
        result = migrate_telemetry_rows(db, _CaptureStore())
        assert result["nx_answer_runs"]["written"] == 3


# ── Report round-trip over a real two-store run ──────────────────────────────


class TestEndToEndReport:
    def test_gate_predicate_over_real_etl_run(self, tmp_path: Path) -> None:
        """The production-shaped pass: orphans skipped, timestamps handled,
        danglers flagged, one unparseable row failed — the report reflects
        every action and total_failed counts exactly the failed rows."""
        tax_db = TestTaxonomyPolicy()._seeded_db(tmp_path)
        tel_db = TestTelemetryPolicy()._seeded_db(
            (tmp_path / "tel").resolve() if (tmp_path / "tel").mkdir() is None else tmp_path / "tel",
        )
        collector = IssueCollector()
        migrate_taxonomy_rows(tax_db, _CaptureStore(), collector=collector)
        migrate_telemetry_rows(tel_db, _CaptureStore(), collector=collector)

        report = build_report(collector, source={"sqlite": str(tax_db)}, target={})
        summary = report["summary"]
        assert summary["by_action"]["skipped"] == 4      # 2 assignments + 2 links
        assert summary["by_action"]["handled"] == 2  # both naive ts rows
        assert summary["by_action"]["flagged"] == 1
        assert summary["by_action"]["schema_corrected"] == 1
        assert summary["total_failed"] == 1              # the unparseable ts
        assert summary["max_severity"] == 4


# ── Catalog policy (P2.3) ────────────────────────────────────────────────────

_CATALOG_SCHEMA = """
CREATE TABLE owners (
    tumbler_prefix TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    owner_type     TEXT NOT NULL,
    repo_hash      TEXT,
    description    TEXT,
    repo_root      TEXT DEFAULT '',
    head_hash      TEXT
);
CREATE TABLE documents (
    tumbler              TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    author               TEXT,
    year                 INTEGER,
    content_type         TEXT,
    file_path            TEXT,
    corpus               TEXT,
    physical_collection  TEXT,
    chunk_count          INTEGER,
    head_hash            TEXT,
    indexed_at           TEXT,
    metadata             TEXT,
    source_mtime         REAL DEFAULT 0.0,
    alias_of             TEXT DEFAULT '',
    source_uri           TEXT DEFAULT ''
);
CREATE TABLE links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_tumbler TEXT NOT NULL,
    to_tumbler   TEXT NOT NULL,
    link_type    TEXT NOT NULL,
    from_span    TEXT DEFAULT '',
    to_span      TEXT DEFAULT '',
    created_by   TEXT DEFAULT 'user',
    created_at   TEXT DEFAULT '',
    metadata     TEXT
);
CREATE TABLE collections (
    name         TEXT PRIMARY KEY,
    content_type TEXT DEFAULT ''
);
CREATE TABLE document_chunks (
    doc_id   TEXT NOT NULL,
    position INTEGER NOT NULL,
    chash    TEXT NOT NULL
);
CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT);
"""


class _CaptureCatalogClient:
    """Duck-typed HttpCatalogClient: records _post calls per path."""

    def __init__(self) -> None:
        self.posts: dict[str, list[dict]] = {}

    def _post(self, path: str, payload: dict) -> dict:
        self.posts.setdefault(path, []).append(payload)
        return {"imported": 1}


class TestCatalogPolicy:
    def _seeded_db(self, tmp_path: Path) -> Path:
        db = _make_db(tmp_path, _CATALOG_SCHEMA, name="catalog.db")
        _insert(db, "INSERT INTO owners (tumbler_prefix, name, owner_type) VALUES (?,?,?)", [
            ("1.1", "nexus", "repo"),
        ])
        _insert(db, "INSERT INTO documents (tumbler, title) VALUES (?,?)", [
            ("1.1.1", "doc-a"),
            ("1.1.2", "doc-b"),
        ])
        _insert(db, "INSERT INTO links (from_tumbler, to_tumbler, link_type) VALUES (?,?,?)", [
            ("1.1.1", "1.1.2", "cites"),     # valid
            ("1.1.1", "9.9.9", "cites"),     # dangling endpoint (deleted doc)
            ("9.9.8", "1.1.2", "relates"),   # dangling from-side
        ])
        return db

    def test_dangling_links_imported_and_flagged(self, tmp_path: Path) -> None:
        """Soft-dangler policy: catalog links have no enforced endpoint FK —
        they IMPORT (273/1,719 in the audit) and the advisory records each."""
        from nexus.db.t2.catalog_etl import migrate_catalog

        db = self._seeded_db(tmp_path)
        client = _CaptureCatalogClient()
        collector = IssueCollector()

        migrate_catalog(db, client, collector=collector)

        # ALL three links imported (in one array POST) — flag, never drop.
        assert sum(len(p["rows"]) for p in client.posts["/import/link"]) == 3
        (issue,) = [
            i for i in collector.issues_for("catalog", "links")
            if i.action == "flagged"
        ]
        assert issue.issue_class == "soft_dangler"
        assert issue.count == 2
        assert set(issue.sample_ids) == {"1.1.1:9.9.9", "9.9.8:1.1.2"}
        assert collector.table_counts("catalog", "links") == {
            "read": 3, "written": 3,
        }

    def test_collector_optional_back_compat(self, tmp_path: Path) -> None:
        from nexus.db.t2.catalog_etl import migrate_catalog

        db = self._seeded_db(tmp_path)
        result = migrate_catalog(db, _CaptureCatalogClient())
        assert result["links"]["written"] == 3


# ── Aspects policy (P2.3) ────────────────────────────────────────────────────

_ASPECTS_SCHEMA = """
CREATE TABLE document_aspects (
    collection            TEXT,
    source_path           TEXT,
    doc_id                TEXT,
    problem_formulation   TEXT,
    proposed_method       TEXT,
    experimental_datasets TEXT,
    experimental_baselines TEXT,
    experimental_results  TEXT,
    extras                TEXT,
    confidence            REAL,
    extracted_at          TEXT,
    model_version         TEXT,
    extractor_name        TEXT
);
CREATE TABLE aspect_extraction_queue (
    id              INTEGER PRIMARY KEY,
    doc_id          TEXT,
    collection      TEXT,
    source_path     TEXT,
    content_hash    TEXT,
    status          TEXT,
    retry_count     INTEGER DEFAULT 0,
    enqueued_at     TEXT,
    last_attempt_at TEXT,
    last_error      TEXT
);
"""


class _CaptureAspectsStore:
    """RDR-176 P3: the aspects ETLs batch the transport; record one entry per
    row in the batch and return the batch size."""

    def __init__(self) -> None:
        self.imported: list[dict] = []
        self.queued: list[dict] = []
        self.highlights: list[dict] = []
        self.promotions: list[dict] = []

    def import_aspect(self, body: dict) -> int:
        self.imported.append(body)
        return 1

    def import_aspects_batch(self, rows: list[dict]) -> int:
        self.imported.extend(rows)
        return len(rows)

    def import_queue_row(self, body: dict) -> int:
        self.queued.append(body)
        return 1

    def import_queue_batch(self, rows: list[dict]) -> int:
        self.queued.extend(rows)
        return len(rows)

    def import_highlights_batch(self, rows: list[dict]) -> int:
        self.highlights.extend(rows)
        return len(rows)

    def import_promotion_batch(self, rows: list[dict]) -> int:
        self.promotions.extend(rows)
        return len(rows)


class TestAspectsPolicy:
    def _seeded(self, tmp_path: Path) -> tuple[Path, Path]:
        aspects_db = _make_db(tmp_path, _ASPECTS_SCHEMA, name="memory.db")
        catalog_db = _make_db(tmp_path, _CATALOG_SCHEMA, name="catalog.db")
        _insert(catalog_db, "INSERT INTO documents (tumbler, title) VALUES (?,?)", [
            ("1.1.1", "live-doc"),
        ])
        _insert(
            aspects_db,
            "INSERT INTO document_aspects "
            "(collection, source_path, doc_id, extracted_at, model_version, extractor_name) "
            "VALUES (?,?,?,?,?,?)",
            [
                ("knowledge__x", "/a", "1.1.1", "2026-01-01", "v2", "claude"),  # valid doc
                ("knowledge__x", "/b", "9.9.9", "2026-01-01", "v2", "claude"),  # stale doc_id
            ],
        )
        _insert(
            aspects_db,
            "INSERT INTO aspect_extraction_queue "
            "(doc_id, collection, source_path, status, enqueued_at) VALUES (?,?,?,?,?)",
            [
                ("1.1.1", "knowledge__x", "/a", "pending", "2026-01-01"),  # valid
                ("9.9.9", "knowledge__x", "/b", "pending", "2026-01-01"),  # orphan
            ],
        )
        return aspects_db, catalog_db

    def test_orphan_doc_id_skipped_and_recorded(self, tmp_path: Path) -> None:
        from nexus.db.t2.aspects_etl import migrate_aspects

        aspects_db, catalog_db = self._seeded(tmp_path)
        store = _CaptureAspectsStore()
        collector = IssueCollector()

        migrate_aspects(
            aspects_db, store, collector=collector, catalog_db_path=catalog_db,
        )

        assert len(store.imported) == 1                  # only the live doc
        assert store.imported[0]["doc_id"] == "1.1.1"
        (issue,) = [
            i for i in collector.issues_for("aspects", "document_aspects")
            if i.action == "skipped"
        ]
        assert issue.issue_class == "orphan_parent"
        assert issue.count == 1
        assert collector.table_counts("aspects", "document_aspects") == {
            "read": 2, "written": 1,
        }

    def test_without_catalog_db_no_orphan_check_back_compat(
        self, tmp_path: Path,
    ) -> None:
        from nexus.db.t2.aspects_etl import migrate_aspects

        aspects_db, _ = self._seeded(tmp_path)
        store = _CaptureAspectsStore()
        result = migrate_aspects(aspects_db, store)
        assert result["imported"] == 2  # current behavior preserved

    def test_queue_orphans_skipped_valid_migrate(self, tmp_path: Path) -> None:
        """The audit's 3/7 queue-orphan shape: orphans skip-and-record, the
        valid rows migrate."""
        from nexus.db.t2.aspects_etl import migrate_queue

        aspects_db, catalog_db = self._seeded(tmp_path)
        store = _CaptureAspectsStore()
        collector = IssueCollector()

        migrate_queue(
            aspects_db, store, collector=collector, catalog_db_path=catalog_db,
        )

        assert len(store.queued) == 1
        assert store.queued[0]["doc_id"] == "1.1.1"
        (issue,) = [
            i for i in collector.issues_for("aspects", "aspect_extraction_queue")
            if i.action == "skipped"
        ]
        assert issue.issue_class == "orphan_parent"
        assert issue.count == 1
        assert collector.table_counts("aspects", "aspect_extraction_queue") == {
            "read": 2, "written": 1,
        }


# ── Clean stores: counts + catch-all only (P2.3) ─────────────────────────────

_MEMORY_SCHEMA = """
CREATE TABLE memory (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project       TEXT NOT NULL,
    title         TEXT NOT NULL,
    content       TEXT NOT NULL,
    tags          TEXT,
    timestamp     TEXT,
    ttl_days      INTEGER DEFAULT 0,
    access_count  INTEGER DEFAULT 0,
    last_accessed TEXT,
    session_id    TEXT,
    source_agent  TEXT,
    UNIQUE (project, title)
);
"""


class TestCleanStoreCountsAndCatchAll:
    def test_memory_counts_and_failed_catch_all(self, tmp_path: Path) -> None:
        from nexus.db.t2.memory_etl import migrate_memory_rows

        db = _make_db(tmp_path, _MEMORY_SCHEMA)
        _insert(
            db,
            "INSERT INTO memory (project, title, content) VALUES (?,?,?)",
            [("p", "t1", "c1"), ("p", "t2", "c2"), ("p", "t3", "c3")],
        )
        store = _CaptureStore()
        store.fail_on["import_entries_batch"] = True
        collector = IssueCollector()

        migrate_memory_rows(db, store, collector=collector)

        assert collector.table_counts("memory", "memory") == {
            "read": 3, "written": 0,
        }
        (issue,) = [
            i for i in collector.issues_for("memory", "memory")
            if i.action == "failed"
        ]
        assert issue.issue_class == "unexpected"
        # Whole-batch failure records every row in the rejected batch.
        assert issue.count == 3

    def test_plans_counts_and_failed_catch_all(self, tmp_path: Path) -> None:
        from nexus.db.t2.plan_etl import migrate_plan_rows

        db = tmp_path / "plans.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE plans (id INTEGER PRIMARY KEY, project TEXT, "
            "query TEXT NOT NULL, plan_json TEXT NOT NULL DEFAULT '{}', "
            "outcome TEXT DEFAULT 'success', tags TEXT DEFAULT '', "
            "created_at TEXT DEFAULT '');"
        )
        conn.executemany(
            "INSERT INTO plans (project, query) VALUES (?,?)",
            [("p", "q1"), ("p", "q2")],
        )
        conn.commit()
        conn.close()
        store = _CaptureStore()
        store.fail_on["import_plans_batch"] = True
        collector = IssueCollector()

        migrate_plan_rows(db, store, collector=collector)

        assert collector.table_counts("plans", "plans") == {
            "read": 2, "written": 0,
        }
        (issue,) = [
            i for i in collector.issues_for("plans", "plans")
            if i.action == "failed"
        ]
        assert issue.count == 2

    def test_chash_batch_error_records_failed_per_row(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_etl import migrate_chash_rows

        db = tmp_path / "chash.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE chash_index (chash TEXT PRIMARY KEY, "
            "physical_collection TEXT, created_at TEXT);"
        )
        conn.executemany(
            "INSERT INTO chash_index VALUES (?,?,?)",
            [("c1", "k", ""), ("c2", "k", "")],
        )
        conn.commit()
        conn.close()

        class _OkChash:
            def import_batch(self, *a, **k):
                return len(a[0]) if a else 0

            def __getattr__(self, name):
                def _f(*a, **k):
                    payload = a[0] if a else k.get("rows", [])
                    return {"imported": len(payload)} if isinstance(payload, list) else 1
                return _f

        collector = IssueCollector()
        migrate_chash_rows(db, _OkChash(), collector=collector)
        counts = collector.table_counts("chash", "chash_index")
        assert counts["read"] == 2
        # _OkChash's __getattr__ returns a bare function for ._client, so
        # the batch post raises — this test OWNS the error path (CRE P2
        # finding): nothing written, and total_failed counts BOTH rows.
        assert counts["written"] == 0
        (issue,) = [
            i for i in collector.issues_for("chash", "chash_index")
            if i.action == "failed"
        ]
        assert issue.count == 2


class TestNeverSilentSweep:
    """RDR-153 P2 critic Criticals: EVERY ETL surface records import
    rejections — a silent path lets the Phase-4 gate pass falsely."""

    def test_telemetry_relevance_log_failure_recorded(self, tmp_path: Path) -> None:
        from nexus.db.t2.telemetry_etl import migrate_telemetry_rows

        db = _make_db(tmp_path, _TELEMETRY_SCHEMA + """
CREATE TABLE relevance_log (
    id INTEGER PRIMARY KEY, query TEXT, doc_id TEXT, rank INTEGER,
    clicked INTEGER DEFAULT 0, timestamp TEXT
);""")
        _insert(db, "INSERT INTO relevance_log (query, doc_id, rank) VALUES (?,?,?)", [
            ("q", "d1", 1), ("q", "d2", 2),
        ])
        store = _CaptureStore()
        store.fail_on["relevance_log"] = 1
        collector = IssueCollector()

        migrate_telemetry_rows(db, store, collector=collector)

        failed = [
            i for i in collector.issues_for("telemetry", "relevance_log")
            if i.action == "failed"
        ]
        assert len(failed) == 1
        # Whole-batch failure records both relevance rows in the rejected batch.
        assert failed[0].count == 2

    def test_catalog_import_failure_recorded(self, tmp_path: Path) -> None:
        from nexus.db.t2.catalog_etl import migrate_catalog

        db = TestCatalogPolicy()._seeded_db(tmp_path)

        class _FailingDocBatch(_CaptureCatalogClient):
            def _post(self, path, payload):
                super()._post(path, payload)
                if path == "/import/document":
                    raise RuntimeError("injected")
                return {"imported": 1}

        collector = IssueCollector()
        migrate_catalog(db, _FailingDocBatch(), collector=collector)

        (issue,) = [
            i for i in collector.issues_for("catalog", "documents")
            if i.action == "failed"
        ]
        # Both seeded docs ship in one array POST; rejecting it records both
        # (re-run idempotent) and writes none.
        assert issue.count == 2
        assert collector.table_counts("catalog", "documents")["written"] == 0

    def test_queue_import_rejection_recorded(self, tmp_path: Path) -> None:
        from nexus.db.t2.aspects_etl import migrate_queue

        aspects_db, catalog_db = TestAspectsPolicy()._seeded(tmp_path)

        class _RejectingQueue(_CaptureAspectsStore):
            def import_queue_batch(self, rows):
                raise RuntimeError("rejected")

        collector = IssueCollector()
        migrate_queue(
            aspects_db, _RejectingQueue(), collector=collector,
            catalog_db_path=catalog_db,
        )
        failed = [
            i for i in collector.issues_for("aspects", "aspect_extraction_queue")
            if i.action == "failed"
        ]
        assert len(failed) == 1
        assert failed[0].count == 1  # the valid row; the orphan was skipped

    def test_memory_transform_failure_recorded_not_aborting(
        self, tmp_path: Path,
    ) -> None:
        """CRE Low-2: a corrupt row whose TRANSFORM raises must record a
        failed issue and the loop must continue (never-silent, no abort)."""
        from nexus.db.t2.memory_etl import migrate_memory_rows

        db = _make_db(tmp_path, _MEMORY_SCHEMA)
        conn = sqlite3.connect(db)
        # NULL content slips past SQLite (no NOT NULL via executescript path?
        # enforce corruption explicitly: drop NOT NULL by inserting via a
        # direct pragma-free path is impossible — instead simulate transform
        # failure with a row whose content is NULL through a relaxed table).
        conn.executescript(
            "CREATE TABLE m2 AS SELECT * FROM memory; DROP TABLE memory;"
            "CREATE TABLE memory (id INTEGER PRIMARY KEY, project TEXT, "
            "title TEXT, content TEXT, tags TEXT, timestamp TEXT, "
            "ttl_days INTEGER, access_count INTEGER, last_accessed TEXT, "
            "session_id TEXT, source_agent TEXT);"
        )
        conn.executemany(
            "INSERT INTO memory (project, title, content) VALUES (?,?,?)",
            [("p", "good", "c"), (None, None, None), ("p", "good2", "c")],
        )
        conn.commit()
        conn.close()
        store = _CaptureStore()
        collector = IssueCollector()

        migrate_memory_rows(db, store, collector=collector)

        counts = collector.table_counts("memory", "memory")
        assert counts["read"] == 3
        # The corrupt row either transforms-and-imports or records failed —
        # never silently vanishes:
        assert counts["written"] + sum(
            i.count for i in collector.issues_for("memory", "memory")
            if i.action == "failed"
        ) == 3
