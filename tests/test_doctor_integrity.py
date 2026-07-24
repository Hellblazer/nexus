# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ENGINE_SUBSTRATE = os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine"

# Tests below that seed a REAL on-disk SQLite T2 file via T2Database and then
# probe it with doctor's SQLite integrity check (PRAGMA integrity_check, FTS5,
# WAL writer-slot contention). On the engine substrate T2Database routes to the
# Http* stores and never materialises the SQLite file, so the probe sees "not
# created yet". The probe's SUBJECT is the SQLite substrate itself.
_sqlite_probe_dies_at_flip = pytest.mark.skipif(
    _ENGINE_SUBSTRATE,
    reason="dies-roster: doctor's SQLite T2 integrity probe (on-disk memory.db "
    "seeded via the SQLite twin) dies at the RDR-155 P4b flip",
)

from nexus.health import (
    _check_orphan_t1,
    _check_t2_integrity,
    _check_t2_dropped_writes,
    _check_t2_daemon_singleton,
    _check_orphan_checkpoints,
    HealthResult,
)
from nexus.db.t2 import T2Database


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_session_file(sessions_dir: Path, name: str, pid: int) -> Path:
    record = {
        "session_id": "test-session", "server_host": "127.0.0.1",
        "server_port": 12345, "server_pid": pid, "created_at": 9999999999.0,
    }
    path = sessions_dir / name
    path.write_text(json.dumps(record))
    return path


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _run_orphan_t1(sessions_dir: Path) -> tuple[bool, list[HealthResult]]:
    with patch("nexus.session.SESSIONS_DIR", sessions_dir):
        results = _check_orphan_t1()
    ok = all(r.ok for r in results)
    return ok, results


# ── Step 5: Orphan T1 ───────────────────────────────────────────────────────




# ── Step 6: T2 integrity ────────────────────────────────────────────────────

class TestCheckT2Integrity:
    def _run(self, db_path: Path) -> tuple[bool, list[HealthResult]]:
        with patch("nexus.health.default_db_path", return_value=db_path):
            results = _check_t2_integrity()
        ok = all(r.ok for r in results)
        return ok, results

    def test_db_not_exists(self, tmp_path):
        ok, results = self._run(tmp_path / "nonexistent.db")
        assert ok is True and "not created yet" in results[0].detail

    @_sqlite_probe_dies_at_flip
    @pytest.mark.parametrize("populate", [True, False], ids=["with_data", "empty"])
    def test_valid_database_passes(self, tmp_path, populate):
        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            if populate:
                db.put(project="test", title="item1", content="hello world", ttl=30)
        ok, results = self._run(db_path)
        assert ok is True and "PRAGMA ok" in results[0].detail

    @_sqlite_probe_dies_at_flip
    @pytest.mark.parametrize("corrupt_fn", [
        lambda p: open(str(p), "r+b").truncate(512) or None,
        lambda p: p.write_bytes(b"this is not sqlite" * 100),
    ], ids=["truncated", "not_sqlite"])
    def test_corrupt_database_fails(self, tmp_path, corrupt_fn):
        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            db.put(project="p", title="t", content="data", ttl=1)
        corrupt_fn(db_path)
        ok, results = self._run(db_path)
        # Genuine corruption is a HARD failure, never a soft WARN: warn stays
        # False so the operator sees a red X, not a yellow ⚠ (RDR-129 B4).
        assert ok is False
        assert results[0].ok is False
        assert results[0].warn is False

    @_sqlite_probe_dies_at_flip
    def test_transient_write_lock_is_soft_warn(self, tmp_path, monkeypatch):
        """RDR-129 B4 (nexus-uq8a4): a held WAL writer slot makes the FTS5
        integrity probe a soft WARN, not a hard red X. The DB is healthy,
        just busy with a legitimate concurrent writer (e.g. nx index repo)."""
        import sqlite3 as _sqlite3

        from nexus import health

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            db.put(project="p", title="t", content="data", ttl=1)

        # Fail fast so the test never waits the production timeout.
        monkeypatch.setattr(health, "_INTEGRITY_BUSY_TIMEOUT_MS", 50)
        monkeypatch.setattr(health, "_INTEGRITY_RETRY_SLEEPS_BETWEEN", (0.0,))

        # Hold the single WAL writer slot for the whole probe.
        holder = _sqlite3.connect(str(db_path), timeout=0)
        try:
            holder.execute("PRAGMA busy_timeout = 0")
            holder.execute("BEGIN IMMEDIATE")
            ok, results = self._run(db_path)
        finally:
            holder.rollback()
            holder.close()

        assert ok is False
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert r.fatal is False
        assert "busy" in r.detail.lower()

    def test_non_lock_fts_error_stays_hard(self, tmp_path, monkeypatch):
        """A non-lock OperationalError on the FTS5 probe (genuine FTS
        corruption) is a HARD failure, distinct from transient contention."""
        import sqlite3 as _sqlite3

        from nexus import health

        db_path = tmp_path / "memory.db"
        db_path.write_text("placeholder")  # only .exists() matters here

        class _Cursor:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        class _FakeConn:
            def execute(self, sql, *args):
                if sql.startswith("PRAGMA busy_timeout"):
                    return _Cursor([])
                if sql == "PRAGMA integrity_check":
                    return _Cursor([("ok",)])
                if "INSERT INTO memory_fts" in sql:
                    raise _sqlite3.OperationalError("malformed database schema")
                return _Cursor([])

            def rollback(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr(health.sqlite3, "connect", lambda *a, **k: _FakeConn())
        ok, results = self._run(db_path)
        r = results[0]
        assert ok is False
        assert r.ok is False
        assert r.warn is False
        assert "FTS5" in r.detail

    def test_lock_fts_error_via_fake_conn_is_soft(self, tmp_path, monkeypatch):
        """The discriminator at the FTS5 layer: a lock OperationalError on the
        probe → soft WARN even when surfaced through a stand-in connection."""
        import sqlite3 as _sqlite3

        from nexus import health

        db_path = tmp_path / "memory.db"
        db_path.write_text("placeholder")

        monkeypatch.setattr(health, "_INTEGRITY_RETRY_SLEEPS_BETWEEN", (0.0,))

        class _Cursor:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        class _FakeConn:
            def execute(self, sql, *args):
                if sql == "PRAGMA integrity_check":
                    return _Cursor([("ok",)])
                if "INSERT INTO memory_fts" in sql:
                    raise _sqlite3.OperationalError("database is locked")
                return _Cursor([])

            def rollback(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr(health.sqlite3, "connect", lambda *a, **k: _FakeConn())
        ok, results = self._run(db_path)
        r = results[0]
        assert ok is False
        assert r.ok is False
        assert r.warn is True
        assert "busy" in r.detail.lower()


# ── T2 best-effort write drops (RDR-129 B4, nexus-uq8a4) ────────────────────

class TestCheckT2DroppedWrites:
    def test_no_drops_is_ok(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "NX_DROPPED_WRITES_LOG_PATH", str(tmp_path / "drops.jsonl")
        )
        results = _check_t2_dropped_writes()
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert r.warn is False
        assert "no drops" in r.detail.lower()

    def test_recorded_drops_are_historical_informational(self, tmp_path, monkeypatch):
        """RDR-187 (nexus-piwya.4): the meter's only-ever producer (the chash
        dual-write hook) is retired, so existing drop records are HISTORICAL
        — reported ok=True with the count and the retirement visible, never
        a frozen soft-WARN whose last_ts can never advance."""
        from nexus import dropped_writes

        monkeypatch.setenv(
            "NX_DROPPED_WRITES_LOG_PATH", str(tmp_path / "drops.jsonl")
        )
        dropped_writes.record_drop(
            hook="chash_dual_write_batch_hook",
            collection="code__nexus",
            rows=3,
            error="database is locked",
        )
        results = _check_t2_dropped_writes()
        r = results[0]
        assert r.ok is True
        assert r.warn is False
        assert "1" in r.detail           # the count stays visible
        assert "historical" in r.detail.lower()
        assert "retired" in r.detail.lower()


# ── T2 daemon singleton / multiplicity (RDR-129 A3, nexus-exa2p) ────────────

class TestCheckT2DaemonSingleton:
    def _run(self, db_path: Path) -> tuple[bool, list[HealthResult]]:
        with patch("nexus.health.default_db_path", return_value=db_path):
            results = _check_t2_daemon_singleton()
        ok = all(r.ok for r in results)
        return ok, results

    def test_no_db_is_ok(self, tmp_path):
        ok, results = self._run(tmp_path / "absent.db")
        assert ok is True
        assert results[0].ok is True

    @pytest.mark.parametrize("pids", [[], [4242]], ids=["zero", "one"])
    def test_zero_or_one_daemon_ok(self, tmp_path, monkeypatch, pids):
        db = tmp_path / "memory.db"
        db.write_text("x")
        monkeypatch.setattr(
            "nexus.daemon.t2_daemon._enumerate_t2_daemon_pids_for_db",
            lambda p: list(pids),
        )
        ok, results = self._run(db)
        assert ok is True
        assert results[0].ok is True
        assert results[0].fatal is False

    def test_multiple_daemons_hard_error_names_pids(self, tmp_path, monkeypatch):
        db = tmp_path / "memory.db"
        db.write_text("x")
        monkeypatch.setattr(
            "nexus.daemon.t2_daemon._enumerate_t2_daemon_pids_for_db",
            lambda p: [4242, 5353],
        )
        ok, results = self._run(db)
        r = results[0]
        assert ok is False
        assert r.ok is False
        assert r.fatal is True  # multiplicity is a HARD failure (A3), unlike B4
        assert r.warn is False
        assert "4242" in r.detail and "5353" in r.detail

    def test_probe_failure_degrades_to_ok(self, tmp_path, monkeypatch):
        """A probe that itself errors must not flip doctor red — absence of
        evidence is not evidence of multiplicity."""
        db = tmp_path / "memory.db"
        db.write_text("x")

        def _boom(p):
            raise RuntimeError("lsof exploded")

        monkeypatch.setattr(
            "nexus.daemon.t2_daemon._enumerate_t2_daemon_pids_for_db", _boom
        )
        ok, results = self._run(db)
        assert ok is True


# ── Orphan checkpoints ──────────────────────────────────────────────────────

class TestCheckOrphanCheckpoints:
    @pytest.fixture()
    def ckpt_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "checkpoints"
        d.mkdir()
        monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", d)
        return d

    def _write_ckpt(self, ckpt_dir, pdf, content_hash, collection="knowledge__art"):
        from nexus.checkpoint import CheckpointData, write_checkpoint
        write_checkpoint(CheckpointData(
            pdf=pdf, collection=collection, content_hash=content_hash,
            chunks_upserted=10, total_chunks=100, embedding_model="voyage-context-3",
        ))

    @pytest.mark.parametrize("setup", ["no_dir", "empty_dir"])
    def test_missing_or_empty_reports_ok(self, tmp_path, monkeypatch, setup):
        d = tmp_path / "checkpoints"
        if setup == "empty_dir":
            d.mkdir()
        monkeypatch.setattr("nexus.checkpoint.CHECKPOINT_DIR", d)
        results = _check_orphan_checkpoints()
        assert results[0].ok is True

    def test_live_pdf_reports_ok(self, ckpt_dir, tmp_path):
        pdf = tmp_path / "present.pdf"
        pdf.write_bytes(b"%PDF")
        self._write_ckpt(ckpt_dir, str(pdf), "live123")
        results = _check_orphan_checkpoints()
        assert results[0].ok is True

    def test_dead_pdf_reports_failure(self, ckpt_dir, tmp_path):
        self._write_ckpt(ckpt_dir, str(tmp_path / "gone.pdf"), "dead123")
        results = _check_orphan_checkpoints()
        assert results[0].ok is False

    def test_mixed_reports_failure(self, ckpt_dir, tmp_path):
        pdf = tmp_path / "here.pdf"
        pdf.write_bytes(b"%PDF")
        self._write_ckpt(ckpt_dir, str(pdf), "live_mixed")
        self._write_ckpt(ckpt_dir, str(tmp_path / "nope.pdf"), "dead_mixed")
        results = _check_orphan_checkpoints()
        assert results[0].ok is False
