# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


from nexus.health import (
    _check_orphan_t1,
    _check_t2_dropped_writes,
    _check_t2_schema_applied,
    _check_orphan_checkpoints,
    _LEGACY_T2_SOURCE_LABEL,
    _T2_SCHEMA_LABEL,
    HealthResult,
    T2SchemaFingerprint,
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


# ── T2 schema applied (nexus-ay18d PORT off SQLite PRAGMA integrity) ────────

class _FakeResponse:
    """Minimal ``httpx.Response`` stand-in for
    :func:`nexus.health.probe_t2_schema_fingerprint`'s injectable
    ``http_get`` — always a 200; non-200 behavior is exercised via the
    ``http_get`` callable raising directly (see
    ``test_unreachable_engine_is_soft_warn_never_vacuous_ok``)."""

    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._body


class TestCheckT2SchemaApplied:
    def _run(self, db_path: Path, http_get=None) -> tuple[bool, list[HealthResult]]:
        from contextlib import ExitStack

        from nexus import health

        real_probe = health.probe_t2_schema_fingerprint
        with ExitStack() as stack:
            stack.enter_context(patch("nexus.health.default_db_path", return_value=db_path))
            if http_get is not None:
                stack.enter_context(patch(
                    "nexus.health.probe_t2_schema_fingerprint",
                    lambda: real_probe(base_url="http://127.0.0.1:1", http_get=http_get),
                ))
            results = _check_t2_schema_applied()
        ok = all(r.ok for r in results)
        return ok, results

    def _schema_result(self, results: list[HealthResult]) -> HealthResult:
        matches = [r for r in results if r.label == _T2_SCHEMA_LABEL]
        assert matches, f"no {_T2_SCHEMA_LABEL!r} result in {results!r}"
        return matches[0]

    def test_healthy_engine_reports_ok(self, tmp_path):
        """Real PG install: /version reports a real changeset count, no
        error — the honest, non-vacuous PASS."""
        http_get = lambda url, timeout: _FakeResponse(
            {"schema_latest_id": "vectors-014", "schema_changeset_count": 209}
        )
        ok, results = self._run(tmp_path / "nonexistent.db", http_get=http_get)
        r = self._schema_result(results)
        assert ok is True
        assert r.ok is True
        assert "209 changeset" in r.detail
        assert "vectors-014" in r.detail
        # No legacy-source advisory when the file is absent (fresh box).
        assert not [x for x in results if x.label == _LEGACY_T2_SOURCE_LABEL]

    def test_engine_reports_schema_error_is_hard_fail(self, tmp_path):
        """The engine itself could not read its changelog — a genuine,
        engine-sourced failure signal, not a client-side guess."""
        http_get = lambda url, timeout: _FakeResponse(
            {"schema_latest_id": None, "schema_changeset_count": None, "schema_error": "connection refused"}
        )
        ok, results = self._run(tmp_path / "nonexistent.db", http_get=http_get)
        r = self._schema_result(results)
        assert ok is False
        assert r.ok is False
        assert r.warn is False
        assert "connection refused" in r.detail

    def test_zero_changesets_is_hard_fail_non_vacuity(self, tmp_path):
        """A reachable engine reporting zero applied changesets is NOT a
        healthy schema — the non-vacuity guard (nexus-kmo9h class)."""
        http_get = lambda url, timeout: _FakeResponse(
            {"schema_latest_id": None, "schema_changeset_count": 0}
        )
        ok, results = self._run(tmp_path / "nonexistent.db", http_get=http_get)
        r = self._schema_result(results)
        assert ok is False
        assert "applied nothing" in r.detail

    def test_managed_endpoint_omits_fields_is_honest_na(self, tmp_path):
        """The managed/cloud endpoint withholds the fingerprint BY DESIGN
        (nexus.db.managed_endpoint docstring) — absent keys, not null
        values. Reported ok=True but explicitly labelled "not exposed",
        never conflated with "checked and healthy"."""
        http_get = lambda url, timeout: _FakeResponse(
            {"app_version": "1.0-SNAPSHOT", "release_version": "0.1.65"}
        )
        ok, results = self._run(tmp_path / "nonexistent.db", http_get=http_get)
        r = self._schema_result(results)
        assert ok is True
        assert "not exposed" in r.detail

    def test_unreachable_engine_is_soft_warn_never_vacuous_ok(self, tmp_path):
        """Engine unreachable — the fresh-box false-clean this bead exists
        to close. MUST be warn=True, never a silent ok=True."""
        def _boom(url, timeout):
            raise ConnectionError("refused")

        ok, results = self._run(tmp_path / "nonexistent.db", http_get=_boom)
        r = self._schema_result(results)
        assert ok is False
        assert r.ok is False
        assert r.warn is True
        assert "unreachable" in r.detail.lower()

    def test_fresh_box_no_file_no_legacy_advisory(self, tmp_path, monkeypatch):
        """A fresh PG-only install (WAVE-2 finding): no SQLite file ever
        existed. Only the schema-applied result renders — no legacy-source
        noise for an install shape that never had SQLite."""
        monkeypatch.setattr(
            "nexus.health.probe_t2_schema_fingerprint",
            lambda: T2SchemaFingerprint(reachable=False, reported=False, unreachable_detail="no service"),
        )
        with patch("nexus.health.default_db_path", return_value=tmp_path / "nonexistent.db"):
            results = _check_t2_schema_applied()
        assert len(results) == 1
        assert results[0].label == _T2_SCHEMA_LABEL

    def test_legacy_file_present_is_informational_advisory_only(self, tmp_path, monkeypatch):
        """A migration-era install with the frozen SQLite file still on
        disk gets a SEPARATE, purely informational advisory — decoupled
        from (and never gating) the schema-applied verdict."""
        monkeypatch.setattr(
            "nexus.health.probe_t2_schema_fingerprint",
            lambda: T2SchemaFingerprint(
                reachable=True, reported=True, latest_id="vectors-014", changeset_count=209,
            ),
        )
        db_path = tmp_path / "memory.db"
        db_path.write_text("placeholder")
        with patch("nexus.health.default_db_path", return_value=db_path):
            results = _check_t2_schema_applied()
        labels = {r.label: r for r in results}
        assert labels[_T2_SCHEMA_LABEL].ok is True
        advisory = labels[_LEGACY_T2_SOURCE_LABEL]
        assert advisory.ok is True  # purely informational — never gates
        assert "rollback artifact" in advisory.detail
        assert str(db_path) in advisory.detail

    def test_legacy_file_present_does_not_flip_a_failing_verdict(self, tmp_path, monkeypatch):
        """The advisory is decoupled: a legacy file's presence must not
        mask (or be masked by) a genuine schema_error on the live engine."""
        monkeypatch.setattr(
            "nexus.health.probe_t2_schema_fingerprint",
            lambda: T2SchemaFingerprint(reachable=True, reported=True, schema_error="boom"),
        )
        db_path = tmp_path / "memory.db"
        db_path.write_text("placeholder")
        with patch("nexus.health.default_db_path", return_value=db_path):
            results = _check_t2_schema_applied()
        labels = {r.label: r for r in results}
        assert labels[_T2_SCHEMA_LABEL].ok is False
        assert labels[_LEGACY_T2_SOURCE_LABEL].ok is True


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
# complementary to (the SQLite FTS5-busy WARN, formerly `_check_t2_integrity`)
# was itself retired at nexus-ay18d — the replacement,
# `_check_t2_schema_applied`, asks the engine directly and has no
# write-lock-contention case to be soft about (see TestCheckT2SchemaApplied
# above).


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
