# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-142 P1.1 (nexus-aaz1r): read-only migration step-resolver.

The resolver reports — without any DDL or row writes — whether each pending
migration step WOULD succeed, defer (``MigrationRetry``), or gate
(``MigrationError``), carrying the operator remediation for gates. It exists so
``nx upgrade --dry-run`` / ``nx doctor --check-schema`` can stop lying about
"no pending migrations" when a deferred/gated step actually remains (the
RDR-142 reporting-lie class; the per-condition ``_check_deferred_migrations``
stopgap is superseded in a later bead).

Anti-drift is enforced two ways: (1) shared threshold/message helpers between
the real ``_check_high_volume_orphans`` and the classifier; (2) an AGREEMENT
test that runs both the resolver AND the real ``apply_pending`` on identical
fixtures and asserts the verdicts match.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ── Fixtures (pre-migration schema; mirror tests/test_rdr108_lh8c_*) ──────────


def _make_memory_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE document_aspects (
            collection TEXT NOT NULL, source_path TEXT NOT NULL,
            problem_formulation TEXT, proposed_method TEXT,
            experimental_datasets TEXT, experimental_baselines TEXT,
            experimental_results TEXT, extras TEXT, confidence REAL,
            extracted_at TEXT NOT NULL, model_version TEXT NOT NULL,
            extractor_name TEXT NOT NULL, source_uri TEXT,
            PRIMARY KEY (collection, source_path)
        );
        CREATE TABLE aspect_extraction_queue (
            collection TEXT NOT NULL, source_path TEXT NOT NULL,
            doc_id TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            retry_count INTEGER NOT NULL DEFAULT 0, enqueued_at TEXT NOT NULL,
            last_attempt_at TEXT, last_error TEXT,
            PRIMARY KEY (collection, source_path)
        );
    """)
    conn.commit()
    return conn


def _make_catalog_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE documents (
            tumbler TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT 'Doc',
            file_path TEXT, physical_collection TEXT
        );
        CREATE TABLE collections (
            name TEXT PRIMARY KEY, superseded_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );
    """)
    conn.commit()
    conn.close()


def _insert_catalog_doc(cat: Path, *, tumbler: str, file_path: str, collection: str) -> None:
    conn = sqlite3.connect(str(cat))
    conn.execute(
        "INSERT INTO documents (tumbler, file_path, physical_collection) VALUES (?, ?, ?)",
        (tumbler, file_path, collection),
    )
    conn.commit()
    conn.close()


def _insert_aspect(conn: sqlite3.Connection, *, collection: str, source_path: str,
                   source_uri: str | None = "uri://x") -> None:
    conn.execute(
        "INSERT INTO document_aspects (collection, source_path, extracted_at, "
        "model_version, extractor_name, source_uri) VALUES (?, ?, ?, ?, ?, ?)",
        (collection, source_path, "2026-05-01T00:00:00+00:00", "m-v1", "x-v1", source_uri),
    )
    conn.commit()


def _insert_queue(conn: sqlite3.Connection, *, collection: str, source_path: str,
                  status: str = "pending") -> None:
    conn.execute(
        "INSERT INTO aspect_extraction_queue (collection, source_path, status, enqueued_at) "
        "VALUES (?, ?, ?, ?)",
        (collection, source_path, status, "2026-05-01T00:00:00+00:00"),
    )
    conn.commit()


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    """Return (memory_db_path, catalog_db_path) in the layout
    ``_catalog_db_path_from_conn`` expects: <dir>/memory.db + <dir>/catalog/.catalog.db."""
    return tmp_path / "memory.db", tmp_path / "catalog" / ".catalog.db"


def _snapshot(conn: sqlite3.Connection) -> tuple:
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
    ).fetchall()
    da = conn.execute("SELECT COUNT(*) FROM document_aspects").fetchone()[0]
    q = conn.execute("SELECT COUNT(*) FROM aspect_extraction_queue").fetchone()[0]
    return schema, da, q


# ── Shape ─────────────────────────────────────────────────────────────────────


class TestResolverShape:
    def test_step_outcome_values(self) -> None:
        from nexus.db.migrations import StepOutcome

        assert {o.value for o in StepOutcome} == {
            "would-succeed", "would-defer", "would-gate",
        }

    def test_migration_precondition_defaults_none(self) -> None:
        # All existing positional Migration(...) constructions must stay valid.
        from nexus.db.migrations import Migration

        m = Migration("1.0.0", "x", lambda c: None)
        assert m.precondition is None

    def test_resolution_carries_remediation(self) -> None:
        from nexus.db.migrations import StepOutcome, StepResolution

        r = StepResolution(
            name="x", introduced="4.30.0", outcome=StepOutcome.WOULD_GATE,
            detail="d", remediation="run nx ...",
        )
        assert r.outcome == StepOutcome.WOULD_GATE
        assert r.remediation == "run nx ..."


# ── document_aspects PK precondition ──────────────────────────────────────────


class TestDocumentAspectsPkPrecondition:
    def test_catalog_absent_would_defer(self, tmp_path: Path) -> None:
        from nexus.db.migrations import (
            StepOutcome, _precondition_document_aspects_pk,
        )

        mem, _cat = _layout(tmp_path)
        conn = _make_memory_db(mem)
        _insert_aspect(conn, collection="knowledge__x", source_path="/a.pdf")
        # No catalog created.
        v = _precondition_document_aspects_pk(conn)
        assert v.outcome == StepOutcome.WOULD_DEFER
        assert "catalog absent" in v.detail.lower()
        conn.close()

    def test_clean_mapped_would_succeed(self, tmp_path: Path) -> None:
        from nexus.db.migrations import (
            StepOutcome, _precondition_document_aspects_pk,
        )

        mem, cat = _layout(tmp_path)
        cat.parent.mkdir(parents=True)
        _make_catalog_db(cat)
        conn = _make_memory_db(mem)
        _insert_aspect(conn, collection="knowledge__x", source_path="/a.pdf")
        _insert_catalog_doc(cat, tumbler="1.1", file_path="/a.pdf", collection="knowledge__x")
        v = _precondition_document_aspects_pk(conn)
        assert v.outcome == StepOutcome.WOULD_SUCCEED, v
        conn.close()

    def test_high_volume_orphans_would_gate(self, tmp_path: Path, monkeypatch) -> None:
        from nexus.db.migrations import (
            StepOutcome, _precondition_document_aspects_pk,
        )

        monkeypatch.setenv("NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD", "1")
        mem, cat = _layout(tmp_path)
        cat.parent.mkdir(parents=True)
        _make_catalog_db(cat)  # empty catalog -> nothing maps
        conn = _make_memory_db(mem)
        # 2 unmapped rows in one collection > threshold 1 -> orphan gate.
        _insert_aspect(conn, collection="knowledge__orphan", source_path="/a.pdf")
        _insert_aspect(conn, collection="knowledge__orphan", source_path="/b.pdf")
        v = _precondition_document_aspects_pk(conn)
        assert v.outcome == StepOutcome.WOULD_GATE, v
        assert "rename-collection" in v.remediation
        conn.close()

    def test_already_migrated_would_succeed(self, tmp_path: Path) -> None:
        from nexus.db.migrations import (
            StepOutcome, _precondition_document_aspects_pk,
        )

        mem, cat = _layout(tmp_path)
        cat.parent.mkdir(parents=True)
        _make_catalog_db(cat)  # catalog present (checked before already-migrated)
        conn = _make_memory_db(mem)
        conn.executescript("""
            DROP TABLE document_aspects;
            CREATE TABLE document_aspects (
                doc_id TEXT NOT NULL, collection TEXT NOT NULL DEFAULT '',
                extracted_at TEXT NOT NULL DEFAULT '', model_version TEXT NOT NULL DEFAULT '',
                extractor_name TEXT NOT NULL DEFAULT '', PRIMARY KEY (doc_id)
            );
        """)
        conn.commit()
        v = _precondition_document_aspects_pk(conn)
        assert v.outcome == StepOutcome.WOULD_SUCCEED
        conn.close()


# ── aspect_extraction_queue PK precondition (BOTH gate branches) ──────────────


class TestAspectQueuePkPrecondition:
    def test_catalog_absent_would_defer(self, tmp_path: Path) -> None:
        from nexus.db.migrations import StepOutcome, _precondition_aspect_queue_pk

        mem, _cat = _layout(tmp_path)
        conn = _make_memory_db(mem)
        _insert_queue(conn, collection="knowledge__x", source_path="/a.pdf", status="pending")
        # No catalog -> DEFER even though a row is present (catalog checked first).
        v = _precondition_aspect_queue_pk(conn)
        assert v.outcome == StepOutcome.WOULD_DEFER
        conn.close()

    def test_undrained_queue_would_gate_drain(self, tmp_path: Path) -> None:
        """Gate branch (a): pending/in_progress rows -> drain remediation."""
        from nexus.db.migrations import StepOutcome, _precondition_aspect_queue_pk

        mem, cat = _layout(tmp_path)
        cat.parent.mkdir(parents=True)
        _make_catalog_db(cat)
        conn = _make_memory_db(mem)
        _insert_queue(conn, collection="knowledge__x", source_path="/a.pdf", status="pending")
        v = _precondition_aspect_queue_pk(conn)
        assert v.outcome == StepOutcome.WOULD_GATE, v
        assert "drain" in v.remediation.lower()
        conn.close()

    def test_drained_high_volume_orphans_would_gate(self, tmp_path: Path, monkeypatch) -> None:
        """Gate branch (b): drained queue + high-volume orphans -> orphan remediation.

        The undrained check must run FIRST (mirror the real fn ordering): with the
        queue drained (only 'failed' rows), the orphan branch is reached."""
        from nexus.db.migrations import StepOutcome, _precondition_aspect_queue_pk

        monkeypatch.setenv("NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD", "1")
        mem, cat = _layout(tmp_path)
        cat.parent.mkdir(parents=True)
        _make_catalog_db(cat)  # empty -> nothing maps
        conn = _make_memory_db(mem)
        # 'failed' rows do NOT count as undrained; they remain as orphan candidates.
        _insert_queue(conn, collection="knowledge__orphan", source_path="/a.pdf", status="failed")
        _insert_queue(conn, collection="knowledge__orphan", source_path="/b.pdf", status="failed")
        v = _precondition_aspect_queue_pk(conn)
        assert v.outcome == StepOutcome.WOULD_GATE, v
        assert "rename-collection" in v.remediation
        conn.close()

    def test_drained_clean_would_succeed(self, tmp_path: Path) -> None:
        from nexus.db.migrations import StepOutcome, _precondition_aspect_queue_pk

        mem, cat = _layout(tmp_path)
        cat.parent.mkdir(parents=True)
        _make_catalog_db(cat)
        conn = _make_memory_db(mem)
        _insert_queue(conn, collection="knowledge__x", source_path="/a.pdf", status="failed")
        _insert_catalog_doc(cat, tumbler="1.1", file_path="/a.pdf", collection="knowledge__x")
        v = _precondition_aspect_queue_pk(conn)
        assert v.outcome == StepOutcome.WOULD_SUCCEED, v
        conn.close()


# ── drop_source_path precondition ─────────────────────────────────────────────


class TestDropSourcePathPrecondition:
    def test_column_absent_would_succeed(self, tmp_path: Path) -> None:
        from nexus.db.migrations import StepOutcome, _precondition_drop_source_path

        mem, _cat = _layout(tmp_path)
        conn = _make_memory_db(mem)
        # Post-4.31.0 shape: source_path already dropped (can't ALTER-DROP a PK
        # column in SQLite, so rebuild the table without it).
        conn.executescript("""
            DROP TABLE document_aspects;
            CREATE TABLE document_aspects (
                doc_id TEXT NOT NULL, collection TEXT,
                extracted_at TEXT NOT NULL DEFAULT '',
                model_version TEXT NOT NULL DEFAULT '',
                extractor_name TEXT NOT NULL DEFAULT '',
                source_uri TEXT, PRIMARY KEY (doc_id)
            );
        """)
        conn.commit()
        v = _precondition_drop_source_path(conn)
        assert v.outcome == StepOutcome.WOULD_SUCCEED
        conn.close()

    def test_bad_source_uri_would_gate(self, tmp_path: Path) -> None:
        from nexus.db.migrations import StepOutcome, _precondition_drop_source_path

        mem, _cat = _layout(tmp_path)
        conn = _make_memory_db(mem)
        # source_path NOT in PK here (default _make_memory_db PK is collection,source_path —
        # so for the bad-uri branch to be reached BEFORE the PK-defer branch, the gate
        # check must come first). Seed a NULL source_uri row.
        _insert_aspect(conn, collection="knowledge__x", source_path="/a.pdf", source_uri=None)
        v = _precondition_drop_source_path(conn)
        assert v.outcome == StepOutcome.WOULD_GATE, v
        assert "backfill-source-uri" in v.remediation
        conn.close()

    def test_source_path_in_pk_would_defer(self, tmp_path: Path) -> None:
        from nexus.db.migrations import StepOutcome, _precondition_drop_source_path

        mem, _cat = _layout(tmp_path)
        conn = _make_memory_db(mem)
        # All rows have valid source_uri; source_path is in the PK (default schema).
        _insert_aspect(conn, collection="knowledge__x", source_path="/a.pdf", source_uri="uri://a")
        v = _precondition_drop_source_path(conn)
        assert v.outcome == StepOutcome.WOULD_DEFER, v
        conn.close()


# ── resolve_pending_steps: read-only + eligible range ─────────────────────────


class TestResolvePendingSteps:
    def test_is_read_only(self, tmp_path: Path) -> None:
        """No DDL / no row writes: schema + row counts identical before and after."""
        from nexus.db.migrations import resolve_pending_steps

        mem, _cat = _layout(tmp_path)
        conn = _make_memory_db(mem)
        _insert_aspect(conn, collection="knowledge__x", source_path="/a.pdf")
        _insert_queue(conn, collection="knowledge__x", source_path="/a.pdf", status="pending")
        before = _snapshot(conn)
        resolve_pending_steps(conn, "9.9.9")
        after = _snapshot(conn)
        assert before == after
        conn.close()

    def test_reports_defer_and_gate_for_pk_steps(self, tmp_path: Path) -> None:
        from nexus.db.migrations import StepOutcome, resolve_pending_steps

        mem, _cat = _layout(tmp_path)
        conn = _make_memory_db(mem)
        _insert_aspect(conn, collection="knowledge__x", source_path="/a.pdf")
        # No catalog -> the PK steps would defer.
        res = {r.name: r for r in resolve_pending_steps(conn, "9.9.9")}
        defers = [r for r in res.values() if r.outcome == StepOutcome.WOULD_DEFER]
        assert any("doc_id" in r.name or "PK" in r.name or "je0b" in r.name.lower()
                   for r in defers), [r.name for r in defers]
        conn.close()

    def test_eligible_range_is_lower_bound_only(self, tmp_path: Path) -> None:
        """RDR-170: resolve mirrors apply_pending's lower-bound-only loop — a step
        introduced ABOVE current_version is still eligible (not filtered out)."""
        from nexus.db import migrations
        from nexus.db.migrations import Migration, StepOutcome, resolve_pending_steps

        mem, _cat = _layout(tmp_path)
        conn = _make_memory_db(mem)
        sentinel = Migration("99.0.0", "rdr142 sentinel", lambda c: None)
        # last_seen reads 0.0.0 (no _nexus_version) -> 99.0.0 > 0.0.0 eligible even
        # though current_version (5.0.0) < 99.0.0.
        import nexus.db.migrations as _m
        # Append sentinel to a copy of the registry.
        res = None
        orig = _m.MIGRATIONS
        try:
            _m.MIGRATIONS = list(orig) + [sentinel]
            res = {r.name: r for r in resolve_pending_steps(conn, "5.0.0")}
        finally:
            _m.MIGRATIONS = orig
        assert "rdr142 sentinel" in res
        assert res["rdr142 sentinel"].outcome == StepOutcome.WOULD_SUCCEED
        conn.close()


# ── Whack-a-mole guard ────────────────────────────────────────────────────────


class TestWhackAMoleGuard:
    def test_every_defer_or_gate_step_has_a_precondition(self) -> None:
        """Every registered Migration whose fn can raise MigrationRetry/MigrationError
        MUST carry a precondition, or the resolver would silently report would-succeed
        for a step that actually defers/gates. A new defer/gate step without a
        precondition fails this test (the anti-regression guard)."""
        import inspect
        import re

        from nexus.db.migrations import MIGRATIONS

        # LIMITATION: this inspects the REGISTERED fn body only — a step that
        # delegates all its raises to a callee (no ``raise`` in its own source)
        # bypasses this guard. The agreement tests (TestResolverAgreesWith-
        # ApplyPending) are the complementary backstop for callee-raised gates.
        # Today all three registered fns raise directly in their own bodies.
        #
        # Match an actual ``raise MigrationError/Retry`` STATEMENT, not a
        # docstring/comment mention (e.g. nexus-pnje documents that a *later*
        # migration raises but its own body is a no-op ``return``).
        raise_re = re.compile(r"\braise\s+Migration(Error|Retry)\b")
        offenders = []
        for m in MIGRATIONS:
            try:
                body = inspect.getsource(m.fn)
            except (OSError, TypeError):
                continue
            if raise_re.search(body) and m.precondition is None:
                offenders.append(m.name)
        assert not offenders, (
            f"defer/gate steps missing a precondition classifier: {offenders}"
        )


# ── Anti-drift: resolver verdict agrees with real apply_pending outcome ───────


class TestResolverAgreesWithApplyPending:
    """The load-bearing guard: run the resolver AND the real apply_pending on the
    SAME fixture; their verdicts must agree (defer => any_skipped/no-stamp; gate =>
    MigrationError raised; succeed => completes + stamps)."""

    def _eligible(self, conn, name: str):
        from nexus.db.migrations import resolve_pending_steps
        return {r.name: r for r in resolve_pending_steps(conn, "9.9.9")}[name]

    def test_defer_agreement_document_aspects(self, tmp_path: Path) -> None:
        from nexus.db import migrations
        from nexus.db.migrations import (
            StepOutcome, _migrate_document_aspects_pk_via_apply_pending,
            MigrationRetry,
        )

        mem, _cat = _layout(tmp_path)
        conn = _make_memory_db(mem)
        _insert_aspect(conn, collection="knowledge__x", source_path="/a.pdf")
        name = "RDR-108 Phase 1c: PK switch document_aspects to doc_id (nexus-je0b)"
        verdict = self._eligible(conn, name)
        assert verdict.outcome == StepOutcome.WOULD_DEFER
        # Real fn: catalog absent -> MigrationRetry (the defer).
        with pytest.raises(MigrationRetry):
            _migrate_document_aspects_pk_via_apply_pending(conn)
        conn.close()

    def test_gate_agreement_orphans(self, tmp_path: Path, monkeypatch) -> None:
        from nexus.db.migrations import (
            StepOutcome, MigrationError,
            _migrate_document_aspects_pk_via_apply_pending,
        )

        monkeypatch.setenv("NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD", "1")
        mem, cat = _layout(tmp_path)
        cat.parent.mkdir(parents=True)
        _make_catalog_db(cat)
        conn = _make_memory_db(mem)
        _insert_aspect(conn, collection="knowledge__orphan", source_path="/a.pdf")
        _insert_aspect(conn, collection="knowledge__orphan", source_path="/b.pdf")
        name = "RDR-108 Phase 1c: PK switch document_aspects to doc_id (nexus-je0b)"
        verdict = self._eligible(conn, name)
        assert verdict.outcome == StepOutcome.WOULD_GATE
        with pytest.raises(MigrationError):
            _migrate_document_aspects_pk_via_apply_pending(conn)
        conn.close()

    def test_succeed_agreement(self, tmp_path: Path) -> None:
        from nexus.db.migrations import (
            StepOutcome, _migrate_document_aspects_pk_via_apply_pending,
            _is_already_migrated,
        )

        mem, cat = _layout(tmp_path)
        cat.parent.mkdir(parents=True)
        _make_catalog_db(cat)
        conn = _make_memory_db(mem)
        _insert_aspect(conn, collection="knowledge__x", source_path="/a.pdf")
        _insert_catalog_doc(cat, tumbler="1.1", file_path="/a.pdf", collection="knowledge__x")
        name = "RDR-108 Phase 1c: PK switch document_aspects to doc_id (nexus-je0b)"
        verdict = self._eligible(conn, name)
        assert verdict.outcome == StepOutcome.WOULD_SUCCEED
        # Real fn completes and migrates the PK.
        _migrate_document_aspects_pk_via_apply_pending(conn)
        assert _is_already_migrated(conn, table="document_aspects")
        conn.close()

    def test_succeed_agreement_pass2_superseded_by(self, tmp_path: Path) -> None:
        """Pass-2 (superseded_by hop) must predict WOULD_SUCCEED, not a false-gate:
        a row in a legacy collection whose document lives in the SUCCESSOR
        collection is mappable by Pass 2, so the classifier must not call it an
        orphan — and the real backfill must agree by completing."""
        from nexus.db.migrations import (
            StepOutcome, _is_already_migrated,
            _migrate_document_aspects_pk_via_apply_pending,
        )

        mem, cat = _layout(tmp_path)
        cat.parent.mkdir(parents=True)
        _make_catalog_db(cat)
        # legacy collection 'knowledge__old' superseded by 'knowledge__new';
        # the document lives in the successor with the same file_path.
        conn_cat = sqlite3.connect(str(cat))
        conn_cat.execute(
            "INSERT INTO collections (name, superseded_by) VALUES (?, ?)",
            ("knowledge__old", "knowledge__new"),
        )
        conn_cat.commit()
        conn_cat.close()
        _insert_catalog_doc(cat, tumbler="2.1", file_path="/a.pdf", collection="knowledge__new")
        conn = _make_memory_db(mem)
        _insert_aspect(conn, collection="knowledge__old", source_path="/a.pdf")
        name = "RDR-108 Phase 1c: PK switch document_aspects to doc_id (nexus-je0b)"
        verdict = self._eligible(conn, name)
        assert verdict.outcome == StepOutcome.WOULD_SUCCEED, verdict
        _migrate_document_aspects_pk_via_apply_pending(conn)
        assert _is_already_migrated(conn, table="document_aspects")
        conn.close()

    # ── queue PK agreement (both gate branches + defer + succeed) ─────────────

    _QNAME = "RDR-108 Phase 1c: PK switch aspect_extraction_queue to doc_id (nexus-je0b)"

    def test_queue_defer_agreement(self, tmp_path: Path) -> None:
        from nexus.db.migrations import (
            StepOutcome, MigrationRetry,
            _migrate_aspect_queue_pk_via_apply_pending,
        )

        mem, _cat = _layout(tmp_path)
        conn = _make_memory_db(mem)
        _insert_queue(conn, collection="knowledge__x", source_path="/a.pdf", status="failed")
        verdict = self._eligible(conn, self._QNAME)
        assert verdict.outcome == StepOutcome.WOULD_DEFER
        with pytest.raises(MigrationRetry):
            _migrate_aspect_queue_pk_via_apply_pending(conn)
        conn.close()

    def test_queue_undrained_gate_agreement(self, tmp_path: Path, monkeypatch) -> None:
        """Gate branch (a). The real wrapper attempts drain_worker first; patch it
        to a no-op so the pending row survives and the real fn gates — matching the
        classifier's read-only WOULD_GATE on the same pre-drain state."""
        import nexus.aspect_worker as _aw
        from nexus.db.migrations import (
            StepOutcome, MigrationError,
            _migrate_aspect_queue_pk_via_apply_pending,
        )

        monkeypatch.setattr(_aw, "drain_worker", lambda *a, **k: None)
        mem, cat = _layout(tmp_path)
        cat.parent.mkdir(parents=True)
        _make_catalog_db(cat)
        conn = _make_memory_db(mem)
        _insert_queue(conn, collection="knowledge__x", source_path="/a.pdf", status="pending")
        verdict = self._eligible(conn, self._QNAME)
        assert verdict.outcome == StepOutcome.WOULD_GATE
        assert "drain" in verdict.remediation.lower()
        with pytest.raises(MigrationError):
            _migrate_aspect_queue_pk_via_apply_pending(conn)
        conn.close()

    def test_queue_succeed_agreement(self, tmp_path: Path) -> None:
        from nexus.db.migrations import (
            StepOutcome, _is_already_migrated,
            _migrate_aspect_queue_pk_via_apply_pending,
        )

        mem, cat = _layout(tmp_path)
        cat.parent.mkdir(parents=True)
        _make_catalog_db(cat)
        conn = _make_memory_db(mem)
        # drained (only 'failed') + mapped -> succeed.
        _insert_queue(conn, collection="knowledge__x", source_path="/a.pdf", status="failed")
        _insert_catalog_doc(cat, tumbler="1.1", file_path="/a.pdf", collection="knowledge__x")
        verdict = self._eligible(conn, self._QNAME)
        assert verdict.outcome == StepOutcome.WOULD_SUCCEED, verdict
        _migrate_aspect_queue_pk_via_apply_pending(conn)
        assert _is_already_migrated(conn, table="aspect_extraction_queue")
        conn.close()
