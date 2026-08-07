# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


from nexus.health import (
    _check_orphan_t1_lease,
    _check_orphan_t1_handoff,
    _check_t2_dropped_writes,
    _check_t2_schema_applied,
    _check_orphan_checkpoints,
    _LEGACY_T2_SOURCE_LABEL,
    _T2_SCHEMA_LABEL,
    HealthResult,
    T2SchemaFingerprint,
)
from nexus.db.t2 import T2Database

# NO _make_session_file / _run_orphan_t1 (nexus-8zfwv, 2026-08-07): both were
# already-dead scaffolding (unused anywhere in this file) for the retired
# ``t1_addr.*`` / ``nexus.session.SESSIONS_DIR`` session-record format --
# ``T1LeasePublisher``, the only thing that ever published that format, is
# retired (deleted at ff744321), and ``SESSIONS_DIR`` itself is vestigial.
# See TestCheckOrphanT1Lease below for the live check's real tests, built on
# ``nexus.db.t1``'s real path/publish constructors. ``_dead_pid`` below is
# NOT part of that retirement -- it is a live, actively-used fixture helper
# for TestCheckOrphanT1Handoff (nexus-9l147), unrelated to the t1_addr.*
# format.


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


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


class TestCheckOrphanT1Lease:
    """nexus-8zfwv (2026-08-07): port of the orphan-T1 health check off the
    retired ``t1_addr.*`` ``ServiceRegistry`` lease format onto the live
    ``t1_session_lease.*`` file (``nexus.db.t1.publish_t1_session_lease``).
    Unlike its predecessor this check REAPS expired lease files, not merely
    reports them -- nothing else sweeps them after an ungraceful owner
    death. Every fixture uses the real publisher/path constructors, never a
    hand-built filename.
    """

    @staticmethod
    def _publish(config_dir: Path, session_id: str, *, ttl_seconds: float) -> Path:
        from nexus.db.t1 import _t1_session_lease_path, publish_t1_session_lease

        publish_t1_session_lease(session_id, "tok", config_dir, ttl_seconds=ttl_seconds)
        return _t1_session_lease_path(session_id, config_dir)

    def _run(self, config_dir: Path, monkeypatch):
        # nexus_config_dir() is imported LOCALLY inside _check_orphan_t1_lease
        # (deferred, to avoid a circular import), so patching the attribute
        # on nexus.health would miss -- redirect via the real env-var
        # precedence nexus.config.nexus_config_dir() itself honours.
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
        return _check_orphan_t1_lease()

    def test_no_config_dir_reports_ok(self, tmp_path, monkeypatch):
        results = self._run(tmp_path / "does-not-exist", monkeypatch)
        assert results[0].ok is True
        assert "no nexus config dir" in results[0].detail

    def test_empty_config_dir_reports_ok(self, tmp_path, monkeypatch):
        results = self._run(tmp_path, monkeypatch)
        assert results[0].ok is True
        assert "no live t1 sessions" in results[0].detail.lower()

    def test_fresh_lease_reported_and_not_reaped(self, tmp_path, monkeypatch):
        path = self._publish(tmp_path, "sess-fresh", ttl_seconds=3600.0)
        results = self._run(tmp_path, monkeypatch)
        assert results[0].ok is True
        assert "sess-fresh" in results[0].detail
        assert path.exists(), "a fresh lease must not be reaped"

    def test_expired_lease_is_reaped(self, tmp_path, monkeypatch):
        path = self._publish(tmp_path, "sess-expired", ttl_seconds=-100.0)
        results = self._run(tmp_path, monkeypatch)
        assert results[0].ok is True
        assert "reaped" in results[0].detail.lower()
        assert "sess-expired" in results[0].detail
        assert not path.exists(), "an expired lease must be reaped"

    def test_malformed_lease_old_enough_is_reaped(self, tmp_path, monkeypatch):
        """A file that fails to parse as the lease JSON shape (pre-ngcpo
        bare-token format, or corruption) is fail-safe, not fail-open: it is
        reaped only once it is old enough that no in-flight publish could
        explain it."""
        import os
        import time

        from nexus.db.t1 import _t1_session_lease_path

        path = _t1_session_lease_path("sess-corrupt", tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json-at-all")
        old = time.time() - 7200.0  # 2h old, well past the 1h fallback window
        os.utime(path, (old, old))

        results = self._run(tmp_path, monkeypatch)
        assert results[0].ok is True
        assert "reaped" in results[0].detail.lower()
        assert not path.exists()

    def test_malformed_lease_too_young_is_left_alone(self, tmp_path, monkeypatch):
        """A just-written unparseable file (e.g. mid torn-write, or a
        recent format change) is NOT reaped -- only age proves abandonment."""
        from nexus.db.t1 import _t1_session_lease_path

        path = _t1_session_lease_path("sess-recent-corrupt", tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json-at-all")

        results = self._run(tmp_path, monkeypatch)
        assert results[0].ok is True
        assert "sess-recent-corrupt" in results[0].detail
        assert path.exists(), "a young unparseable file must not be reaped"

    def test_mixed_fresh_and_expired(self, tmp_path, monkeypatch):
        self._publish(tmp_path, "sess-a", ttl_seconds=3600.0)
        expired_path = self._publish(tmp_path, "sess-b", ttl_seconds=-100.0)

        results = self._run(tmp_path, monkeypatch)
        assert results[0].ok is True
        assert "sess-a" in results[0].detail
        assert "sess-b" in results[0].detail
        assert not expired_path.exists()


# ── Orphan T1 handoff markers (nexus-9l147) ─────────────────────────────────

class TestCheckOrphanT1Handoff:
    """nexus-d76vc's SessionStart hook writes ``t1_handoff.<mcp_pid>``; the
    MCP lifespan's handoff watcher claims it (renaming to
    ``t1_handoff.claimed.<mcp_pid>``) then consumes it within one tick. If
    the target mcp_pid dies between write and tick (either variant), the
    marker is never cleaned up. This is the reaper: a marker is orphaned
    when its filename's mcp_pid names no live process; only orphans are
    reaped, and a live pid's marker is left completely untouched."""

    def _run(self, config_dir: Path) -> tuple[bool, list[HealthResult]]:
        with patch("nexus.config.nexus_config_dir", return_value=config_dir):
            results = _check_orphan_t1_handoff()
        ok = all(r.ok for r in results)
        return ok, results

    def test_no_config_dir_reports_ok(self, tmp_path):
        ok, results = self._run(tmp_path / "nonexistent")
        assert ok is True

    def test_no_markers_reports_ok(self, tmp_path):
        ok, results = self._run(tmp_path)
        assert ok is True
        assert "no handoff markers" in results[0].detail.lower()

    def test_orphaned_live_variant_is_reaped(self, tmp_path):
        from nexus.daemon.t1_handoff import handoff_marker_path, write_handoff_marker

        dead = _dead_pid()
        write_handoff_marker(
            dead, new_session_id="sess-A", claude_pid=99999, config_dir=tmp_path,
        )
        marker = handoff_marker_path(dead, tmp_path)
        assert marker.exists()

        ok, results = self._run(tmp_path)
        assert ok is True
        assert not marker.exists(), "orphaned live-variant marker was not reaped"
        assert "reaped 1" in results[0].detail.lower()

    def test_orphaned_claimed_variant_is_reaped(self, tmp_path):
        from nexus.daemon.t1_handoff import (
            claimed_marker_path,
            handoff_marker_path,
            write_handoff_marker,
        )

        dead = _dead_pid()
        write_handoff_marker(
            dead, new_session_id="sess-A", claude_pid=99999, config_dir=tmp_path,
        )
        # Simulate the watcher having claimed the marker (atomic rename to
        # the tick-private claimed path, nexus-d76vc fix-round 2) and then
        # dying before consume_claimed_marker runs.
        claimed = claimed_marker_path(dead, tmp_path)
        handoff_marker_path(dead, tmp_path).rename(claimed)
        assert claimed.exists()

        ok, results = self._run(tmp_path)
        assert ok is True
        assert not claimed.exists(), "orphaned claimed-variant marker was not reaped"
        assert "reaped 1" in results[0].detail.lower()

    def test_live_pid_marker_is_untouched(self, tmp_path):
        from nexus.daemon.t1_handoff import handoff_marker_path, write_handoff_marker

        live = os.getpid()
        write_handoff_marker(
            live, new_session_id="sess-A", claude_pid=99999, config_dir=tmp_path,
        )
        marker = handoff_marker_path(live, tmp_path)
        assert marker.exists()

        ok, results = self._run(tmp_path)
        assert ok is True
        assert marker.exists(), "a marker for a live pid must never be reaped"
        assert "1 live" in results[0].detail.lower()

    def test_malformed_marker_name_handled_fail_safe(self, tmp_path):
        # A marker filename whose suffix is not a plain integer must never be
        # guessed at or deleted -- fail-safe, surfaced instead.
        bogus = tmp_path / "t1_handoff.not-a-pid"
        bogus.write_text("{}")

        ok, results = self._run(tmp_path)
        assert bogus.exists(), "an unparseable marker must never be deleted"
        assert ok is False
        assert any("not-a-pid" in r.detail for r in results)
