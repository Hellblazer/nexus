# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 §A8 / nexus-6y2a9: ``nx aspects backfill-source-uri`` and
``nx aspects gc-pre-rdr096`` verbs.

These two verbs carry forward the substantive behaviour of three
migrations whose bodies were demoted to no-ops under RDR-120's
substrate-vs-consumer boundary:

  - ``migrate_document_aspects_source_uri`` (4.16.0)  →  DDL only
  - ``migrate_document_aspects_source_uri_backfill_empty`` (4.26.2) →  no-op
  - ``migrate_drop_null_aspect_rows`` (4.16.0)        →  no-op

The verb-tests below port the same scenarios the migration tests in
``tests/test_migrations_rdr096.py`` used to exercise, plus add CLI-
level coverage (dry-run, --apply, missing DB, missing column).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands.aspects import aspects_group

from tests._catalog_fixture_ops import register_real_doc_id


# hygiene-001-1 (nexus-tk070.p6a follow-on): aspect_extraction_queue.doc_id
# now carries a REAL FK to catalog_documents(tenant_id, tumbler) via the
# engine's POST /v1/aspects/queue/enqueue 400 -- see the identical cache in
# tests/test_document_aspects_store.py for the full rationale. One real
# registration per test, memoized on the test's freshly-minted
# NX_SERVICE_TOKEN.
_doc_id_cache: dict[str, str] = {}


def _shared_doc_id() -> str:
    key = os.environ.get("NX_SERVICE_TOKEN", "")
    if key not in _doc_id_cache:
        _doc_id_cache[key] = register_real_doc_id()
    return _doc_id_cache[key]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _seed_aspects_schema(conn: sqlite3.Connection) -> None:
    """Set up document_aspects at the post-RDR-096 schema (with source_uri).

    Mirrors what the migrations on real installs produce after they have
    run: the DDL is in place, but rows can carry NULL or empty
    ``source_uri`` for the backfill verb to act on.
    """
    # RDR-158 P4 Stage 4 (nexus-i711w): frozen DDL snapshot of what
    # migrate_document_aspects_table + migrate_document_aspects_source_uri
    # produced — the migration chain died with nexus/db/migrations.py.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS document_aspects (
            collection             TEXT NOT NULL,
            source_path            TEXT NOT NULL,
            problem_formulation    TEXT,
            proposed_method        TEXT,
            experimental_datasets  TEXT,
            experimental_baselines TEXT,
            experimental_results   TEXT,
            extras                 TEXT,
            confidence             REAL,
            extracted_at           TEXT NOT NULL,
            model_version          TEXT NOT NULL,
            extractor_name         TEXT NOT NULL,
            source_uri             TEXT,
            PRIMARY KEY (collection, source_path)
        );
        CREATE INDEX IF NOT EXISTS idx_document_aspects_extractor
            ON document_aspects(extractor_name, model_version);
    """)


def _insert_aspect(
    conn: sqlite3.Connection,
    *,
    collection: str,
    source_path: str,
    source_uri: str | None = None,
    extractor: str = "scholarly-paper-v1",
    problem_formulation: str | None = None,
    proposed_method: str | None = None,
    experimental_datasets: str = "[]",
    experimental_baselines: str = "[]",
    experimental_results: str | None = None,
    extras: str | None = "{}",
    confidence: float | None = None,
) -> None:
    if source_uri is None:
        conn.execute(
            "INSERT INTO document_aspects "
            "(collection, source_path, problem_formulation, proposed_method, "
            " experimental_datasets, experimental_baselines, experimental_results, "
            " extras, confidence, extracted_at, model_version, extractor_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (collection, source_path, problem_formulation, proposed_method,
             experimental_datasets, experimental_baselines, experimental_results,
             extras, confidence,
             "2026-04-27T00:00:00+00:00", "claude-haiku-4-5-20251001",
             extractor),
        )
    else:
        conn.execute(
            "INSERT INTO document_aspects "
            "(collection, source_path, source_uri, problem_formulation, proposed_method, "
            " experimental_datasets, experimental_baselines, experimental_results, "
            " extras, confidence, extracted_at, model_version, extractor_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (collection, source_path, source_uri,
             problem_formulation, proposed_method,
             experimental_datasets, experimental_baselines, experimental_results,
             extras, confidence,
             "2026-04-27T00:00:00+00:00", "claude-haiku-4-5-20251001",
             extractor),
        )
    conn.commit()


@pytest.fixture
def t2_path(tmp_path: Path, monkeypatch) -> Path:
    """Patch default_db_path to a tmp memory.db. The verbs open
    T2Database under the hood; ensure the file exists with the
    post-RDR-096 aspect schema."""
    mem_path = tmp_path / "memory.db"
    monkeypatch.setattr(
        "nexus.commands._helpers.default_db_path",
        lambda: mem_path,
    )
    # Hand-seed the schema (T2Database.__init__ would also do this
    # via its store init paths, but spelling it out keeps each test
    # honest about what it expects).
    conn = sqlite3.connect(str(mem_path))
    _seed_aspects_schema(conn)
    conn.close()
    return mem_path


# ── backfill-source-uri verb ──────────────────────────────────────────────────


class TestRetiredRepairVerbsRefuse:
    """RDR-158 P4 Stage 4 (nexus-i711w, critique Critical): the two
    pre-migration repair verbs — ``backfill-source-uri`` and
    ``gc-pre-rdr096`` — carried the last unguarded raw-SQLite WRITE paths
    into the frozen migration source (RDR-176 Gap 2). Both are now
    unconditional guided refusals; the behavioural suites that drove
    their raw UPDATE/DELETE arms died with the arms (repair, if ever
    needed, happens on the last migration-capable 6.x release)."""

    @pytest.mark.parametrize("argv", [
        ["backfill-source-uri"],
        ["backfill-source-uri", "--apply"],
        ["gc-pre-rdr096"],
        ["gc-pre-rdr096", "--apply"],
    ])
    def test_refuses_loud_without_touching_the_frozen_source(
        self, argv: list[str], t2_path: Path,
    ) -> None:
        before = t2_path.read_bytes() if t2_path.exists() else None
        result = CliRunner().invoke(aspects_group, argv)
        assert result.exit_code == 2, result.output
        assert "retired" in result.output
        assert "frozen migration source" in result.output
        assert "6.x" in result.output
        assert "Traceback" not in result.output
        after = t2_path.read_bytes() if t2_path.exists() else None
        assert before == after, "refusal must not write the frozen source"

    @pytest.mark.parametrize("verb", ["backfill-source-uri", "gc-pre-rdr096"])
    def test_verb_registered(self, verb: str) -> None:
        # The refusal IS the contract; the verb must stay registered so old
        # scripts fail with the explanation rather than a bare usage error.
        assert verb in aspects_group.commands


@pytest.mark.t2_tenant
class TestRequeueFailed:
    """`nx aspects requeue-failed` bulk-revives dead ``aspects`` tasks.

    Substrate: the hermetic engine substrate (fresh tenant per test). Tasks
    are seeded through the producer and the tuple store, and status is read
    from the tuple store's ``rd`` (claim_state ``dead`` or available).
    """

    @pytest.fixture
    def _queue_t2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Route the CLI's read (default_db_path) + write (t2_index_write) at
        a tmp-pathed T2Database. The queue is engine-side; deliberately NO
        ``touch()`` of the local file — the verb's old local-file existence
        gate silently no-opped requeue-failed on any box without the legacy
        memory.db (porter-b defect, fixed in this commit), and running
        against an absent file pins that fix."""
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "t2.db"
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )

        import nexus.mcp_infra as infra

        def _direct_index_write(write_fn):  # noqa: ANN001
            with T2Database(db_path) as db:
                return write_fn(db)

        monkeypatch.setattr(infra, "t2_index_write", _direct_index_write)
        return db_path

    def _seed_dead(self, db_path: Path) -> None:
        """Three dead tasks (x and y in knowledge__a, z in knowledge__b) and one
        available task (ok.pdf in knowledge__a). One document per task, since a
        task's identity is its doc_id."""
        import json

        from nexus.db.t2 import T2Database
        from nexus.db.t2.http_tuple_store import HttpTupleStore
        from tests._catalog_fixture_ops import register_real_doc_id

        seeded = [("knowledge__a", "x.pdf"), ("knowledge__a", "y.pdf"), ("knowledge__b", "z.pdf")]
        with T2Database(db_path) as db:
            for coll, sp in seeded:
                db.aspect_queue.enqueue(coll, sp, content="c", doc_id=register_real_doc_id())
        # Claim oldest first and kill each one, so the three are dead in order.
        store = HttpTupleStore()
        try:
            for _coll, sp in seeded:
                got = store.inp("aspects", {"queue": "aspects"}, claimant="t-seed", lease_s=60)
                assert got is not None and json.loads(got[0].body)["source_path"] == sp
                store.nack(got[1], "t-seed", dead=True)
        finally:
            store.close()
        with T2Database(db_path) as db:
            db.aspect_queue.enqueue("knowledge__a", "ok.pdf", content="c", doc_id=_shared_doc_id())

    def _paths_in(self, db_path: Path, claim_state: str | None) -> list[str]:
        """Source paths of the ``aspects`` tasks in ``claim_state`` (None: available)."""
        import json

        from nexus.db.t2 import T2Database

        with T2Database(db_path) as db:
            return sorted(
                json.loads(t.body)["source_path"]
                for t in db.tuples.rd("aspects", {"queue": "aspects"}, n=300)
                if t.claim_state == claim_state
            )

    def _status(self, db_path: Path, source_path: str) -> str:
        """``dead`` or ``available`` for the task with this source path, else ``<absent>``."""
        import json

        from nexus.db.t2 import T2Database

        with T2Database(db_path) as db:
            for t in db.tuples.rd("aspects", {"queue": "aspects"}, n=300):
                if json.loads(t.body)["source_path"] == source_path:
                    return "dead" if t.claim_state == "dead" else "available"
        return "<absent>"

    def test_dry_run_reports_without_writing(self, _queue_t2: Path) -> None:
        self._seed_dead(_queue_t2)
        res = CliRunner().invoke(aspects_group, ["requeue-failed", "--dry-run"])
        assert res.exit_code == 0, res.output
        assert "Would revive 3 dead task(s)" in res.output
        # Dry-run writes nothing: the dead tasks stay dead.
        assert self._paths_in(_queue_t2, "dead") == ["x.pdf", "y.pdf", "z.pdf"]

    def test_requeue_revives_dead_tasks_to_available(self, _queue_t2: Path) -> None:
        self._seed_dead(_queue_t2)
        res = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert res.exit_code == 0, res.output
        assert "Revived 3 dead task(s) to available." in res.output
        # A revived task is available again; the available task beside them is not touched.
        for sp in ("x.pdf", "y.pdf", "z.pdf", "ok.pdf"):
            assert self._status(_queue_t2, sp) == "available"
        assert self._paths_in(_queue_t2, "dead") == []

    def test_collection_scope(self, _queue_t2: Path) -> None:
        self._seed_dead(_queue_t2)
        res = CliRunner().invoke(
            aspects_group, ["requeue-failed", "--collection", "knowledge__a"],
        )
        assert res.exit_code == 0, res.output
        assert "Revived 2 dead task(s) in knowledge__a to available." in res.output
        assert self._status(_queue_t2, "x.pdf") == "available"  # knowledge__a: revived
        assert self._status(_queue_t2, "y.pdf") == "available"
        assert self._status(_queue_t2, "z.pdf") == "dead"       # knowledge__b untouched

    def test_limit_caps_requeue_count(self, _queue_t2: Path) -> None:
        self._seed_dead(_queue_t2)  # 3 dead tasks
        res = CliRunner().invoke(aspects_group, ["requeue-failed", "--limit", "2"])
        assert res.exit_code == 0, res.output
        assert "Revived 2 dead task(s) to available." in res.output
        # Exactly 2 of the 3 are revived; 1 stays dead.
        statuses = [self._status(_queue_t2, sp) for sp in ("x.pdf", "y.pdf", "z.pdf")]
        assert statuses.count("available") == 2
        assert statuses.count("dead") == 1

    def test_limit_rejects_nonpositive(self, _queue_t2: Path) -> None:
        self._seed_dead(_queue_t2)
        res = CliRunner().invoke(aspects_group, ["requeue-failed", "--limit", "0"])
        assert res.exit_code == 1
        assert "--limit must be a positive integer" in res.output
        assert self._status(_queue_t2, "x.pdf") == "dead"  # nothing written

    def test_no_dead_tasks_message(self, _queue_t2: Path) -> None:
        from nexus.db.t2 import T2Database
        with T2Database(_queue_t2) as db:
            db.aspect_queue.enqueue("knowledge__a", "only-available.pdf", doc_id=_shared_doc_id())
        res = CliRunner().invoke(aspects_group, ["requeue-failed"])
        assert res.exit_code == 0, res.output
        assert "aspects queue: no dead tasks." in res.output
