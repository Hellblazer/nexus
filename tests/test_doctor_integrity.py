# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest

from nexus.health import (
    _check_orphan_t1,
    _check_t2_integrity,
    _check_chroma_pagination,
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

class TestCheckOrphanT1:
    @pytest.mark.parametrize("setup,expect_ok,expect_text", [
        ("no_dir", True, "no sessions directory"),
        ("empty_dir", True, "no session files"),
    ])
    def test_missing_or_empty(self, tmp_path, setup, expect_ok, expect_text):
        d = tmp_path / "sessions"
        if setup == "empty_dir":
            d.mkdir()
        ok, results = _run_orphan_t1(d)
        assert ok is expect_ok
        assert expect_text in results[0].detail

    def test_live_process_reports_ok(self, tmp_path):
        d = tmp_path / "sessions"
        d.mkdir()
        _make_session_file(d, "99999.session", os.getpid())
        ok, results = _run_orphan_t1(d)
        assert ok is True and "no orphans detected" in results[0].detail

    def test_dead_pid_detected_as_orphan(self, tmp_path):
        d = tmp_path / "sessions"
        d.mkdir()
        pid = _dead_pid()
        _make_session_file(d, f"{pid}.session", pid)
        ok, results = _run_orphan_t1(d)
        assert ok is False and "1 orphaned" in results[0].detail
        assert any("rm" in s for s in results[0].fix_suggestions)

    @pytest.mark.parametrize("content", [
        "not-json{{{",
        json.dumps({"session_id": "abc", "server_host": "127.0.0.1", "server_port": 1234}),
    ])
    def test_corrupt_or_missing_pid_skipped(self, tmp_path, content):
        d = tmp_path / "sessions"
        d.mkdir()
        (d / "bad.session").write_text(content)
        ok, _ = _run_orphan_t1(d)
        assert ok is True

    def test_multiple_orphans_count(self, tmp_path):
        d = tmp_path / "sessions"
        d.mkdir()
        for _ in range(2):
            pid = _dead_pid()
            _make_session_file(d, f"{pid}.session", pid)
        ok, results = _run_orphan_t1(d)
        assert ok is False and "2 orphaned" in results[0].detail


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

    @pytest.mark.parametrize("populate", [True, False], ids=["with_data", "empty"])
    def test_valid_database_passes(self, tmp_path, populate):
        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            if populate:
                db.put(project="test", title="item1", content="hello world", ttl=30)
        ok, results = self._run(db_path)
        assert ok is True and "PRAGMA ok" in results[0].detail

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
        assert ok is False and results[0].ok is False


# ── Step 7: ChromaDB pagination ─────────────────────────────────────────────

@pytest.fixture()
def ephemeral_client():
    client = chromadb.EphemeralClient()
    for col in client.list_collections():
        client.delete_collection(col.name)
    return client


class TestCheckChromaPagination:
    def _run(self, client, db="test_db") -> tuple[bool, list[HealthResult]]:
        results = _check_chroma_pagination(client, db)
        ok = all(r.ok for r in results)
        return ok, results

    @pytest.mark.parametrize("n_docs,setup", [
        (0, "no_col"), (0, "empty_col"), (10, "small"), (350, "large"),
    ])
    def test_valid_collections_pass(self, ephemeral_client, n_docs, setup):
        if setup == "empty_col":
            ephemeral_client.create_collection("empty_col")
        elif setup in ("small", "large"):
            col = ephemeral_client.create_collection("col")
            col.add(ids=[f"id{i}" for i in range(n_docs)],
                    documents=[f"doc {i}" for i in range(n_docs)])
        ok, results = self._run(ephemeral_client)
        assert ok is True and results[0].ok is True
        if n_docs > 0:
            assert f"count={n_docs}" in results[0].detail
            assert f"paginated={n_docs}" in results[0].detail

    def test_count_mismatch_fails(self, ephemeral_client):
        col = ephemeral_client.create_collection("mismatch_col")
        col.add(ids=[f"id{i}" for i in range(5)], documents=[f"doc {i}" for i in range(5)])
        mock_col = MagicMock(wraps=col)
        mock_col.name = col.name
        mock_col.count.return_value = 105
        mock_client = MagicMock()
        mock_client.list_collections.return_value = [mock_col]
        ok, results = self._run(mock_client)
        assert ok is False and results[0].ok is False

    def test_list_collections_exception(self):
        bad_client = MagicMock()
        bad_client.list_collections.side_effect = RuntimeError("network error")
        ok, results = self._run(bad_client, "bad_db")
        assert ok is False and "list failed" in results[0].detail

    def test_only_one_collection_audited(self, ephemeral_client):
        for i in range(3):
            col = ephemeral_client.create_collection(f"col_{i}")
            col.add(ids=[f"id{i}"], documents=[f"doc {i}"])
        _, results = self._run(ephemeral_client)
        assert len(results) == 1


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
