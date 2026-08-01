# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


from nexus.health import (
    _check_orphan_t1,
    _check_t2_integrity,
    _check_t2_dropped_writes,
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

# NO TestCheckT2DaemonSingleton: RDR-129 A3's doctor census counted T2 daemons
# per memory.db and failed FATAL on more than one. Both the check and its
# subject retired with the daemon (nexus-i711w Stage 2 sub-stage B) — with no
# daemon the count can only be zero, and the fix it suggested named
# `nx daemon t2 stop` / `ensure-running`. The single-writer invariant it guarded
# is now Postgres's, not a pid count's. The soft live-contention signal it was
# complementary to (_check_t2_integrity) is unaffected and still covered above.


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
