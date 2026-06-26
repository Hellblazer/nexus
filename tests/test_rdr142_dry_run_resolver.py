# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-142 P2.1: nx upgrade --dry-run reports from the read-only step-resolver.

The pure version-range filter + the GH-1061 E2 ``_check_deferred_migrations``
stopgap are replaced by ``migrations.resolve_blocking_steps`` — the
version-eligible steps PLUS any precondition-bearing step whose table is in an
incomplete state even though the version row advanced past it. This file proves:

* deferred / gated steps are surfaced WITH their remediation hints,
* the migrate_drop_source_path_column conditions the stopgap MISSED are now
  reported (regression-forward),
* the undrained-queue gate remediation is preserved,
* ``_check_deferred_migrations`` is fully gone,
and unit-tests the new ``resolve_blocking_steps`` + ``last_seen`` override.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _no_real_daemon():
    with (
        patch("nexus.commands.upgrade._quiesce_daemon"),
        patch("nexus.commands.upgrade._cycle_supervised_daemons_to_current"),
    ):
        yield


@pytest.fixture(autouse=True)
def _clear_upgrade_done():
    from nexus.db.migrations import _upgrade_done
    _upgrade_done.clear()


def _db_at_current_legacy_aspects(tmp_path: Path) -> tuple[Path, str]:
    """memory.db where cli_version == current but document_aspects keeps the
    legacy (collection, source_path) PK — the version row advanced past the
    4.30.0 PK step yet the table is unmigrated (the stopgap's table-state case)."""
    from nexus.commands.upgrade import _current_version
    from nexus.db.migrations import bootstrap_version

    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    bootstrap_version(conn)
    current = _current_version()
    conn.execute("UPDATE _nexus_version SET value=? WHERE key='cli_version'", (current,))
    # legacy-PK document_aspects WITH a source_uri column (realistic 4.31.0-era schema)
    conn.execute(
        "CREATE TABLE document_aspects ("
        "  collection TEXT NOT NULL, source_path TEXT NOT NULL,"
        "  doc_id TEXT NOT NULL DEFAULT '', source_uri TEXT,"
        "  PRIMARY KEY (collection, source_path))"
    )
    conn.commit()
    conn.close()
    return db_path, current


def _realistic_catalog(tmp_path: Path) -> Path:
    cat = tmp_path / "catalog" / ".catalog.db"
    cat.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cat))
    conn.executescript("""
        CREATE TABLE documents (tumbler TEXT PRIMARY KEY, title TEXT DEFAULT 'd',
            file_path TEXT, physical_collection TEXT);
        CREATE TABLE collections (name TEXT PRIMARY KEY, superseded_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '');
    """)
    conn.commit()
    conn.close()
    return cat


class TestDryRunReportsGatedWithRemediation:
    def test_orphan_gate_reports_remediation_hints(self, runner, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD", "1")
        db_path, _ = _db_at_current_legacy_aspects(tmp_path)
        cat = _realistic_catalog(tmp_path)  # well-formed but empty -> all orphans
        conn = sqlite3.connect(str(db_path))
        conn.executemany(
            "INSERT INTO document_aspects (collection, source_path, source_uri) VALUES (?,?,?)",
            [("knowledge__orphan", f"/d{i}.md", "uri://x") for i in range(3)],
        )
        conn.commit()
        conn.close()

        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.db.migrations._catalog_db_path_from_conn", return_value=cat),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])

        out = result.output
        assert "No pending migrations" not in out, out
        # Remediation hints the stopgap emitted must survive (RDR-142 coverage-not-worse).
        assert "rename-collection" in out
        assert "NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD" in out
        assert "gate" in out.lower() or "BLOCKED" in out


class TestDryRunReportsDropSourcePath:
    """The site the 5.6.2 stopgap MISSED — migrate_drop_source_path_column.
    A document_aspects table with a NULL source_uri row would gate it; --dry-run
    must now report that (regression-forward)."""

    def test_drop_source_path_bad_uri_gate_reported(self, runner, tmp_path) -> None:
        db_path, _ = _db_at_current_legacy_aspects(tmp_path)
        cat = _realistic_catalog(tmp_path)
        conn = sqlite3.connect(str(db_path))
        # Map the row so the PK-step orphan gate does NOT fire, isolating the
        # drop_source_path bad-source_uri gate. NULL source_uri -> drop gate.
        conn.execute(
            "INSERT INTO document_aspects (collection, source_path, source_uri) VALUES (?,?,?)",
            ("knowledge__x", "/a.md", None),
        )
        conn.execute("UPDATE document_aspects SET doc_id='1.1'")  # mapped -> no orphan
        conn.commit()
        conn.close()

        cat_conn = sqlite3.connect(str(cat))
        cat_conn.execute(
            "INSERT INTO documents (tumbler, file_path, physical_collection) VALUES (?,?,?)",
            ("1.1", "/a.md", "knowledge__x"),
        )
        cat_conn.commit()
        cat_conn.close()

        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.db.migrations._catalog_db_path_from_conn", return_value=cat),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])

        out = result.output
        assert "No pending migrations" not in out, out
        assert "source_path" in out  # the migration the stopgap never probed
        assert "backfill-source-uri" in out  # its remediation


class TestDryRunLabelAccuracy:
    """RDR-142 P2.1 review (substantive-critic SIG-1/SIG-2): supplementary
    (version-gate-passed) steps must NOT be labelled 'would gate on next start'
    (apply_pending won't run them), and the undrained-queue gate must be
    informational, not a hard BLOCKED."""

    def test_supplementary_gate_not_labelled_next_start_crash(self, runner, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD", "1")
        db_path, _ = _db_at_current_legacy_aspects(tmp_path)
        cat = _realistic_catalog(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.executemany(
            "INSERT INTO document_aspects (collection, source_path, source_uri) VALUES (?,?,?)",
            [("knowledge__orphan", f"/d{i}.md", "uri://x") for i in range(3)],
        )
        conn.commit()
        conn.close()
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.db.migrations._catalog_db_path_from_conn", return_value=cat),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])
        out = result.output
        # The version gate has passed (version==current); the daemon will NOT
        # crash on next start. Must be framed as a table-state/runtime issue.
        assert "would gate on next start" not in out, out
        assert "Table-state checks" in out
        assert "TABLE STATE INCOMPLETE" in out or "runtime" in out.lower()

    def test_undrained_queue_is_informational_not_blocked(self) -> None:
        from nexus.db.migrations import StepOutcome, _precondition_aspect_queue_pk

        cat = None  # built inline
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            from pathlib import Path as _P
            base = _P(d)
            mem = base / "memory.db"
            cat = base / "catalog" / ".catalog.db"
            cat.parent.mkdir(parents=True)
            sqlite3.connect(str(cat)).close()
            conn = sqlite3.connect(str(mem))
            conn.executescript("""
                CREATE TABLE aspect_extraction_queue (
                    collection TEXT NOT NULL, source_path TEXT NOT NULL,
                    doc_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
                    enqueued_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (collection, source_path));
                INSERT INTO aspect_extraction_queue (collection, source_path, status, enqueued_at)
                    VALUES ('knowledge__x', '/a', 'pending', 't');
            """)
            conn.commit()
            with patch("nexus.db.migrations._catalog_db_path_from_conn", return_value=cat):
                v = _precondition_aspect_queue_pk(conn)
            conn.close()
        assert v.outcome == StepOutcome.WOULD_GATE
        assert v.informational is True  # soft gate — apply_pending drains first


class TestForceDryRun:
    def test_force_dry_run_previews_full_remigration(self, runner, tmp_path) -> None:
        """--force --dry-run resets last_seen to 0.0.0 and previews ALL steps as
        eligible (HIGH-2). On a clean realistic DB they are plain (would-succeed)."""
        from nexus.catalog.catalog import Catalog
        from nexus.commands.upgrade import _current_version
        from nexus.db.migrations import apply_pending

        Catalog.init(tmp_path / "catalog")
        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        apply_pending(conn, _current_version())  # fully migrate
        conn.close()
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade", "--force", "--dry-run"])
        out = result.output
        assert result.exit_code == 0, out
        # force => last_seen 0.0.0 => every registered step previewed as pending.
        assert "last seen: v0.0.0" in out
        assert "pending migrations" in out


class TestOrphanFallbackAndDedup:
    def test_orphan_predict_fallback_on_malformed_catalog(self, tmp_path, monkeypatch) -> None:
        """P2.4 gap: a catalog missing documents/collections tables makes the
        accurate JOIN raise OperationalError; the predictor falls back to the
        simple unmapped-row count instead of crashing (still WOULD_GATE)."""
        from nexus.db.migrations import StepOutcome, _precondition_document_aspects_pk

        monkeypatch.setenv("NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD", "1")
        mem = tmp_path / "memory.db"
        cat = tmp_path / "catalog" / ".catalog.db"
        cat.parent.mkdir(parents=True)
        sqlite3.connect(str(cat)).close()  # empty catalog — no documents/collections
        conn = sqlite3.connect(str(mem))
        conn.executescript("""
            CREATE TABLE document_aspects (collection TEXT NOT NULL, source_path TEXT NOT NULL,
                doc_id TEXT NOT NULL DEFAULT '', source_uri TEXT,
                PRIMARY KEY (collection, source_path));
            INSERT INTO document_aspects (collection, source_path) VALUES ('k__o', '/a'), ('k__o', '/b');
        """)
        conn.commit()
        with patch("nexus.db.migrations._catalog_db_path_from_conn", return_value=cat):
            v = _precondition_document_aspects_pk(conn)
        conn.close()
        assert v.outcome == StepOutcome.WOULD_GATE  # fallback count > threshold

    def test_resolve_blocking_steps_no_double_report(self, tmp_path, monkeypatch) -> None:
        """P2.4 gap: a precondition-bearing step in the eligible set must NOT also
        appear in the supplementary section (the `seen` dedup guard)."""
        from nexus.db import migrations
        from nexus.db.migrations import Migration, resolve_blocking_steps

        def _gate(c):
            from nexus.db.migrations import PreconditionVerdict, StepOutcome
            return PreconditionVerdict(StepOutcome.WOULD_GATE, detail="d", remediation="r")

        sentinel = Migration("9.9.9", "dedup-sentinel", lambda c: None, precondition=_gate)
        orig = migrations.MIGRATIONS
        conn = sqlite3.connect(":memory:")
        try:
            migrations.MIGRATIONS = [sentinel]
            # last_seen 0.0.0 -> sentinel is eligible (9.9.9 > 0.0.0).
            steps = resolve_blocking_steps(conn, "9.9.9", last_seen="0.0.0")
        finally:
            migrations.MIGRATIONS = orig
        conn.close()
        names = [s.name for s in steps]
        assert names.count("dedup-sentinel") == 1, names
        assert steps[0].eligible is True


class TestStopgapGone:
    def test_no_check_deferred_migrations_residue(self) -> None:
        import nexus.commands.upgrade as up

        assert not hasattr(up, "_check_deferred_migrations")
        src = Path(up.__file__).read_text()
        # only an explanatory comment may mention it, never a def/call
        assert "def _check_deferred_migrations" not in src
        assert "_check_deferred_migrations(conn)" not in src


class TestResolveBlockingSteps:
    def test_includes_state_blocking_below_version(self, tmp_path) -> None:
        """A precondition-bearing step BELOW last_seen whose table is incomplete
        is reported (the stopgap's table-state coverage)."""
        from nexus.commands.upgrade import _current_version
        from nexus.db.migrations import StepOutcome, resolve_blocking_steps

        db_path, current = _db_at_current_legacy_aspects(tmp_path)
        conn = sqlite3.connect(str(db_path))
        # catalog absent -> the PK steps would DEFER even though version==current.
        with patch(
            "nexus.db.migrations._catalog_db_path_from_conn",
            return_value=tmp_path / "nope" / ".catalog.db",
        ):
            steps = resolve_blocking_steps(conn, current)
        conn.close()
        deferred = [s for s in steps if s.outcome == StepOutcome.WOULD_DEFER]
        assert any("document_aspects" in s.name for s in deferred), [s.name for s in steps]

    def test_last_seen_override(self, tmp_path) -> None:
        """last_seen override changes the eligible set (used by --force --dry-run)."""
        from nexus.db import migrations
        from nexus.db.migrations import Migration, StepOutcome, resolve_pending_steps

        conn = sqlite3.connect(":memory:")
        sentinel = Migration("3.0.0", "ls-sentinel", lambda c: None)
        orig = migrations.MIGRATIONS
        try:
            migrations.MIGRATIONS = [sentinel]
            # last_seen='0.0.0' -> eligible; last_seen='5.0.0' -> not eligible.
            elig = resolve_pending_steps(conn, "9.9.9", last_seen="0.0.0")
            assert any(s.name == "ls-sentinel" for s in elig)
            none = resolve_pending_steps(conn, "9.9.9", last_seen="5.0.0")
            assert not any(s.name == "ls-sentinel" for s in none)
            assert StepOutcome.WOULD_SUCCEED  # enum import sanity
        finally:
            migrations.MIGRATIONS = orig
        conn.close()
