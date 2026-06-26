# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-142 adjacent (nexus-3lbhb): T2 daemon bootstrap surfaces a gated migration
LOUDLY, but stays fail-closed.

``apply_pending`` raises ``MigrationError`` when a migration is GATED (high-volume
orphans, undrained queue, NULL source_uri). At daemon bootstrap that reached the
process as a bare uncaught traceback. ``_open_t2db_or_loud_gate_crash`` now logs a
structured error carrying the gate's remediation + a pointer to
``nx upgrade --dry-run``, then RE-RAISES — the daemon crashes and the supervisor
restarts it (recoverable), rather than serving a degraded, version-mismatched
daemon that would refuse every client while looking healthy.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest


def _empty_catalog(cat: Path) -> None:
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


def _gated_db(tmp_path: Path) -> Path:
    """memory.db whose RDR-108 PK migration is eligible and would GATE on
    high-volume orphans (catalog present but maps nothing)."""
    from nexus.db.migrations import bootstrap_version

    mem = tmp_path / "memory.db"
    _empty_catalog(tmp_path / "catalog" / ".catalog.db")
    conn = sqlite3.connect(str(mem))
    conn.execute("PRAGMA journal_mode=WAL")
    bootstrap_version(conn)
    conn.execute("UPDATE _nexus_version SET value='4.1.2' WHERE key='cli_version'")
    conn.execute(
        "CREATE TABLE document_aspects (collection TEXT NOT NULL, source_path TEXT NOT NULL,"
        "  doc_id TEXT NOT NULL DEFAULT '', source_uri TEXT, extracted_at TEXT,"
        "  model_version TEXT, extractor_name TEXT, PRIMARY KEY (collection, source_path))"
    )
    conn.executemany(
        "INSERT INTO document_aspects (collection, source_path) VALUES (?,?)",
        [("knowledge__orphan", "/a"), ("knowledge__orphan", "/b")],
    )
    conn.commit()
    conn.close()
    return mem


class TestLoudGateCrash:
    def test_gate_logs_loudly_and_reraises(self, tmp_path: Path, monkeypatch) -> None:
        from nexus.daemon import t2_daemon
        from nexus.daemon.t2_daemon import _open_t2db_or_loud_gate_crash
        from nexus.db.migrations import MigrationError

        monkeypatch.setenv("NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD", "1")
        mem = _gated_db(tmp_path)

        with patch.object(t2_daemon._log, "error") as mock_err:
            with pytest.raises(MigrationError):  # fail-closed: still crashes
                _open_t2db_or_loud_gate_crash(mem)

        # ...but loudly: a structured error naming the gate + remediation.
        assert mock_err.called
        event = mock_err.call_args.args[0]
        kwargs = mock_err.call_args.kwargs
        assert event == "t2_daemon_bootstrap_migration_gated"
        assert "rename-collection" in kwargs["error"] or "orphan" in kwargs["error"].lower()
        assert "nx upgrade --dry-run" in kwargs["remediation"]

    def test_gate_crash_takes_no_data_action(self, tmp_path: Path, monkeypatch) -> None:
        """The crash applies nothing: orphans + legacy PK + version row survive."""
        from nexus.daemon.t2_daemon import _open_t2db_or_loud_gate_crash
        from nexus.db.migrations import MigrationError

        monkeypatch.setenv("NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD", "1")
        mem = _gated_db(tmp_path)
        with pytest.raises(MigrationError):
            _open_t2db_or_loud_gate_crash(mem)

        chk = sqlite3.connect(str(mem))
        n = chk.execute("SELECT COUNT(*) FROM document_aspects WHERE doc_id=''").fetchone()[0]
        pk = {r[1] for r in chk.execute("PRAGMA table_info(document_aspects)").fetchall() if r[5] > 0}
        ver = chk.execute("SELECT value FROM _nexus_version WHERE key='cli_version'").fetchone()[0]
        chk.close()
        assert n == 2
        assert pk == {"collection", "source_path"}
        assert ver == "4.1.2"  # version row never advanced past the gate

    def test_clean_db_opens_normally(self, tmp_path: Path) -> None:
        from nexus.catalog.catalog import Catalog
        from nexus.daemon.t2_daemon import _open_t2db_or_loud_gate_crash

        Catalog.init(tmp_path / "catalog")
        t2db = _open_t2db_or_loud_gate_crash(tmp_path / "memory.db")
        try:
            assert t2db is not None
        finally:
            t2db.close()

    def test_sqlite_error_not_intercepted(self, tmp_path: Path) -> None:
        """A non-gate failure (not MigrationError) must NOT be swallowed — only the
        gate is intercepted for the loud log."""
        from nexus.daemon.t2_daemon import _open_t2db_or_loud_gate_crash

        # Patch the SOURCE namespace (nexus.db.t2): the helper does a deferred
        # `from nexus.db.t2 import T2Database` at call time, so the name resolves
        # there, not on the consuming module.
        with patch("nexus.db.t2.T2Database", side_effect=sqlite3.OperationalError("disk I/O error")):
            with pytest.raises(sqlite3.OperationalError):
                _open_t2db_or_loud_gate_crash(tmp_path / "memory.db")
