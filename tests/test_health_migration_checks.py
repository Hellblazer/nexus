# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-178 Gap 1 + Gap 2 (beads nexus-aigpt, nexus-14ndm): doctor checks over
migration-report artifacts.

- ``_check_migration_reports`` (nexus-aigpt): reads the NEWEST
  ``<config>/migration-reports/migration-*.json`` report and fails loud
  (fatal) when ``summary.total_failed > 0`` or ``verification != "verified"``.
  Reference incident: report migration-9141ebaf sat unread for a month with
  total_failed=120, verification=indeterminate, and nothing ever surfaced it.

- ``_check_migration_divergence`` (nexus-14ndm): once the newest report
  records a cloud target, warns (non-fatal) when local SQLite ``memory.db``
  received writes after the report's ``completed_at``.

Both use real tmp-path fixtures (actual JSON report files, actual SQLite
databases) — no filesystem mocks.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from nexus.db.t2.memory_store import _MEMORY_SCHEMA_SQL
import inspect

from nexus.health import _check_migration_divergence, _check_migration_reports
from nexus.migration.orchestrator import verify_counts

# ── Fixtures ─────────────────────────────────────────────────────────────────


def _write_report(
    reports_dir: Path,
    *,
    migration_id: str = "abc12345",
    total_failed: int = 0,
    verification: str | None = "verified",
    service_url: str = "https://api.conexus-nexus.com",
    completed_at: str | None = None,
    stores: list[dict] | None = None,
    mtime_offset: float = 0.0,
) -> Path:
    """Write a schema_version=1 migration report and return its path.

    ``mtime_offset`` lets tests control relative file mtimes (seconds added
    to the current time) so "newest report" selection can be exercised.
    """
    reports_dir.mkdir(parents=True, exist_ok=True)
    completed_at = completed_at or datetime.now(UTC).isoformat()
    report: dict = {
        "schema_version": "1",
        "migration_id": migration_id,
        "started_at": completed_at,
        "completed_at": completed_at,
        "source": {"sqlite": "/tmp/memory.db"},
        "target": {"service_url": service_url},
        "stores": stores or [],
        "summary": {
            "total_read": 0,
            "total_written": 0,
            "total_skipped": 0,
            "total_flagged": 0,
            "total_failed": total_failed,
            "max_severity": 0,
            "by_action": {},
        },
    }
    if verification is not None:
        report["verification"] = verification
    path = reports_dir / f"migration-{migration_id}.json"
    path.write_text(json.dumps(report))
    if mtime_offset:
        st = path.stat()
        os.utime(path, (st.st_atime + mtime_offset, st.st_mtime + mtime_offset))
    return path


def _make_memory_db(db_path: Path, rows: list[tuple[str, str]]) -> None:
    """Create a real ``memory`` table (production schema) with *rows* of
    ``(title, timestamp)``."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_MEMORY_SCHEMA_SQL)
        for i, (title, ts) in enumerate(rows):
            conn.execute(
                "INSERT INTO memory (id, project, title, content, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (i + 1, "proj", title, "content", ts),
            )
        conn.commit()
    finally:
        conn.close()


# ── _check_migration_reports (nexus-aigpt) ──────────────────────────────────


class TestCheckMigrationReports:
    def test_no_reports_dir(self, tmp_path):
        results = _check_migration_reports(reports_dir=tmp_path / "migration-reports")
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert r.fatal is False
        assert "no migrations recorded" in r.detail

    def test_empty_reports_dir(self, tmp_path):
        reports_dir = tmp_path / "migration-reports"
        reports_dir.mkdir()
        results = _check_migration_reports(reports_dir=reports_dir)
        assert results[0].ok is True
        assert "no migrations recorded" in results[0].detail

    def test_clean_report_passes(self, tmp_path):
        reports_dir = tmp_path / "migration-reports"
        _write_report(reports_dir, total_failed=0, verification="verified")
        results = _check_migration_reports(reports_dir=reports_dir)
        assert len(results) == 1
        r = results[0]
        assert r.ok is True
        assert r.fatal is False

    def test_total_failed_fails_loud(self, tmp_path):
        reports_dir = tmp_path / "migration-reports"
        path = _write_report(
            reports_dir,
            migration_id="9141ebaf",
            total_failed=120,
            verification="indeterminate",
            stores=[
                {
                    "store": "memory",
                    "tables": [{"table": "memory", "read": 10, "written": 0, "failed": 80}],
                },
                {
                    "store": "plans",
                    "tables": [{"table": "plans", "read": 5, "written": 0, "failed": 40}],
                },
            ],
        )
        results = _check_migration_reports(reports_dir=reports_dir)
        assert len(results) == 1
        r = results[0]
        assert r.ok is False
        assert r.fatal is True
        assert str(path) in r.detail
        # Per-store failure counts in the detail.
        assert "memory=80" in r.detail
        assert "plans=40" in r.detail
        # fix_suggestions carry the re-run-the-failed-stores hint.
        joined = " ".join(r.fix_suggestions)
        assert "nx storage migrate memory" in joined
        assert "nx storage migrate plans" in joined

    def test_verification_indeterminate_fails_even_with_zero_failed(self, tmp_path):
        """nexus-r0esi precedent: indeterminate is treated as failure, never silence."""
        reports_dir = tmp_path / "migration-reports"
        _write_report(reports_dir, total_failed=0, verification="indeterminate")
        results = _check_migration_reports(reports_dir=reports_dir)
        r = results[0]
        assert r.ok is False
        assert r.fatal is True
        assert "indeterminate" in r.detail

    def test_verification_absent_clean_warns_not_fatal(self, tmp_path):
        """The legacy-artifact split (2026-07-02): a ZERO-failure report whose
        verification KEY is absent was written by pre-6.2 tooling that never
        recorded verdicts — a benign, knowable artifact. It must surface as a
        non-fatal WARN with the one-time --verify-fill suggestion, never a
        fatal crying-wolf alarm — but it is still NOT ok/silently passed."""
        reports_dir = tmp_path / "migration-reports"
        _write_report(reports_dir, total_failed=0, verification=None)
        results = _check_migration_reports(reports_dir=reports_dir)
        r = results[0]
        assert r.ok is False       # never silently treated as passed
        assert r.warn is True
        assert r.fatal is False
        assert "predates verification recording" in r.detail
        assert any("--verify-fill" in s for s in r.fix_suggestions)

    def test_verification_absent_with_failures_stays_fatal(self, tmp_path):
        """The legacy split never softens a report that RECORDED failures."""
        reports_dir = tmp_path / "migration-reports"
        _write_report(reports_dir, total_failed=7, verification=None)
        results = _check_migration_reports(reports_dir=reports_dir)
        r = results[0]
        assert r.ok is False
        assert r.fatal is True

    def test_verification_mismatch_fails(self, tmp_path):
        reports_dir = tmp_path / "migration-reports"
        _write_report(reports_dir, total_failed=0, verification="mismatch")
        results = _check_migration_reports(reports_dir=reports_dir)
        r = results[0]
        assert r.ok is False
        assert r.fatal is True

    def test_verification_passed_is_not_the_vocabulary(self, tmp_path):
        """Cross-module contract regression (wave-1 critic finding): the
        writer — orchestrator.verify_counts() — emits exactly
        "verified" | "mismatch" | "indeterminate". "passed" is NOT a value
        any report ever carries; the reader accepting it (as it originally
        did, `!= "passed"`) made a clean verified run fail doctor forever.
        A literal "passed" must be treated as unverified."""
        reports_dir = tmp_path / "migration-reports"
        _write_report(reports_dir, total_failed=0, verification="passed")
        results = _check_migration_reports(reports_dir=reports_dir)
        r = results[0]
        assert r.ok is False
        assert r.fatal is True

    def test_reader_accepts_the_writers_success_literal(self):
        """Pin the writer side of the handshake: verify_counts()'s success
        return is the literal "verified" — the exact string doctor accepts."""
        src = inspect.getsource(verify_counts)
        assert 'return "verified"' in src
        assert '"passed"' not in src

    def test_reads_newest_report_only(self, tmp_path):
        """Two reports present: an older FAILED one and a newer CLEAN one —
        only the newest is read, and it passes."""
        reports_dir = tmp_path / "migration-reports"
        _write_report(
            reports_dir, migration_id="older", total_failed=50,
            verification="indeterminate", mtime_offset=-100.0,
        )
        _write_report(
            reports_dir, migration_id="newer", total_failed=0,
            verification="verified", mtime_offset=0.0,
        )
        results = _check_migration_reports(reports_dir=reports_dir)
        r = results[0]
        assert r.ok is True

    def test_reads_newest_report_when_newest_is_bad(self, tmp_path):
        """Inverse of the above: older is clean, newer failed — must flag."""
        reports_dir = tmp_path / "migration-reports"
        _write_report(
            reports_dir, migration_id="older", total_failed=0,
            verification="verified", mtime_offset=-100.0,
        )
        _write_report(
            reports_dir, migration_id="newer", total_failed=3,
            verification="indeterminate", mtime_offset=0.0,
        )
        results = _check_migration_reports(reports_dir=reports_dir)
        r = results[0]
        assert r.ok is False
        assert r.fatal is True
        assert "newer" in r.detail

    def test_corrupt_report_json_fails_loud(self, tmp_path):
        """A report that exists but cannot be parsed must never be silently
        treated as 'no migrations recorded' — that would re-create the
        month-of-silence class with a different failure mode."""
        reports_dir = tmp_path / "migration-reports"
        reports_dir.mkdir()
        bad = reports_dir / "migration-badjson.json"
        bad.write_text("{not valid json")
        results = _check_migration_reports(reports_dir=reports_dir)
        r = results[0]
        assert r.ok is False
        assert r.fatal is True

    def test_ignores_non_matching_files(self, tmp_path):
        reports_dir = tmp_path / "migration-reports"
        reports_dir.mkdir()
        (reports_dir / "README.txt").write_text("not a report")
        results = _check_migration_reports(reports_dir=reports_dir)
        assert results[0].ok is True
        assert "no migrations recorded" in results[0].detail


# ── _check_migration_divergence (nexus-14ndm) ───────────────────────────────


class TestCheckMigrationDivergence:
    def test_no_reports(self, tmp_path):
        results = _check_migration_divergence(
            reports_dir=tmp_path / "migration-reports",
            memory_db_path=tmp_path / "memory.db",
        )
        r = results[0]
        assert r.ok is True
        assert "no migrations recorded" in r.detail

    def test_local_target_skips_check(self, tmp_path):
        reports_dir = tmp_path / "migration-reports"
        _write_report(
            reports_dir,
            service_url="http://127.0.0.1:8080",
            completed_at="2020-01-01T00:00:00+00:00",
        )
        memory_db = tmp_path / "memory.db"
        # Writes far in the future — would trip divergence if the target
        # were cloud, must be skipped because target is local.
        _make_memory_db(memory_db, [("t1", "2030-01-01T00:00:00Z")])
        results = _check_migration_divergence(reports_dir=reports_dir, memory_db_path=memory_db)
        r = results[0]
        assert r.ok is True

    def test_lease_placeholder_target_is_local(self, tmp_path):
        reports_dir = tmp_path / "migration-reports"
        _write_report(
            reports_dir, service_url="(lease)",
            completed_at="2020-01-01T00:00:00+00:00",
        )
        memory_db = tmp_path / "memory.db"
        _make_memory_db(memory_db, [("t1", "2030-01-01T00:00:00Z")])
        results = _check_migration_divergence(reports_dir=reports_dir, memory_db_path=memory_db)
        assert results[0].ok is True

    def test_memory_db_absent_skips(self, tmp_path):
        reports_dir = tmp_path / "migration-reports"
        _write_report(
            reports_dir, service_url="https://api.conexus-nexus.com",
            completed_at="2020-01-01T00:00:00+00:00",
        )
        results = _check_migration_divergence(
            reports_dir=reports_dir, memory_db_path=tmp_path / "memory.db",
        )
        r = results[0]
        assert r.ok is True

    def test_no_divergence_when_writes_predate_migration(self, tmp_path):
        reports_dir = tmp_path / "migration-reports"
        _write_report(
            reports_dir, service_url="https://api.conexus-nexus.com",
            completed_at="2026-06-30T12:00:00+00:00",
        )
        memory_db = tmp_path / "memory.db"
        _make_memory_db(memory_db, [
            ("t1", "2026-06-30T10:00:00Z"),
            ("t2", "2026-06-30T11:59:00Z"),
        ])
        results = _check_migration_divergence(reports_dir=reports_dir, memory_db_path=memory_db)
        r = results[0]
        assert r.ok is True

    def test_divergence_detected_warns_not_fatal(self, tmp_path):
        """68-row incident shape: local writes landed after the cloud
        migration completed — must warn loudly but stay non-fatal."""
        reports_dir = tmp_path / "migration-reports"
        _write_report(
            reports_dir, service_url="https://api.conexus-nexus.com",
            completed_at="2026-06-30T12:00:00+00:00",
        )
        memory_db = tmp_path / "memory.db"
        _make_memory_db(memory_db, [
            ("before", "2026-06-30T10:00:00Z"),
            ("after1", "2026-06-30T13:00:00Z"),
            ("after2", "2026-07-01T09:00:00Z"),
        ])
        results = _check_migration_divergence(reports_dir=reports_dir, memory_db_path=memory_db)
        r = results[0]
        assert r.ok is False
        assert r.warn is True
        assert r.fatal is False
        assert "2" in r.detail  # row-count of divergent writes
        assert any("nx storage migrate memory" in s for s in r.fix_suggestions)

    def test_completed_at_missing_skips(self, tmp_path):
        reports_dir = tmp_path / "migration-reports"
        path = _write_report(
            reports_dir, service_url="https://api.conexus-nexus.com",
            completed_at="2026-06-30T12:00:00+00:00",
        )
        data = json.loads(path.read_text())
        del data["completed_at"]
        path.write_text(json.dumps(data))
        memory_db = tmp_path / "memory.db"
        _make_memory_db(memory_db, [("t1", "2030-01-01T00:00:00Z")])
        results = _check_migration_divergence(reports_dir=reports_dir, memory_db_path=memory_db)
        assert results[0].ok is True


class TestRunPsqlBundleLibEnv:
    """GH #1414 era-hop regression (2026-07-21): the health probe's psql
    invocation must carry the same nexus-iytd3 bundle-lib loader guard that
    pg_provision's own psql calls get. Without it, a bundle whose psql has
    no RPATH (the published bundles) exits 127 (libpq.so.5 unresolvable) on
    a minimal Linux base — and post-fc24123c that broken probe reads as
    UNKNOWN to the tri-state chash-poison gate, permanently DEFERRING
    engine convergence on exactly the era boxes the unattended `nx upgrade`
    walk exists for (the RDR-185 era-hop MVV caught it: 0.1.11 -> 0.1.51
    held back, /v1/remap 404s, 8 checks failed)."""

    def _bundle_psql(self, tmp_path: Path) -> Path:
        (tmp_path / "bundle" / "bin").mkdir(parents=True)
        (tmp_path / "bundle" / "lib").mkdir(parents=True)
        psql = tmp_path / "bundle" / "bin" / "psql"
        psql.write_text("")
        return psql

    def test_real_subprocess_branch_carries_bundle_lib_env(self, tmp_path, monkeypatch):
        import subprocess as _subprocess

        from nexus.health import _run_psql

        psql = self._bundle_psql(tmp_path)
        captured: dict = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return _subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")

        monkeypatch.setattr("nexus.health.subprocess.run", _fake_run)
        proc = _run_psql(psql, "127.0.0.1", 5432, "nexus", "u", "secret", "SELECT 1;")

        assert proc.returncode == 0
        env = captured["env"]
        assert env is not None
        assert env.get("PGPASSWORD") == "secret"
        lib = str(tmp_path / "bundle" / "lib")
        assert env.get("LD_LIBRARY_PATH", "").split(os.pathsep)[0] == lib, (
            "probe psql must get the bundle's sibling lib/ on the loader path "
            "(same _bundle_lib_env guard as pg_provision's own calls)"
        )

    def test_non_bundle_layout_still_gets_pgpassword(self, tmp_path, monkeypatch):
        import subprocess as _subprocess

        from nexus.health import _run_psql

        (tmp_path / "bin").mkdir()
        psql = tmp_path / "bin" / "psql"  # no sibling lib/ — system layout
        psql.write_text("")
        captured: dict = {}

        def _fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return _subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")

        monkeypatch.setattr("nexus.health.subprocess.run", _fake_run)
        _run_psql(psql, "127.0.0.1", 5432, "nexus", "u", "pw", "SELECT 1;")

        env = captured["env"]
        assert env.get("PGPASSWORD") == "pw"
        assert "LD_LIBRARY_PATH" not in env or env["LD_LIBRARY_PATH"] == os.environ.get("LD_LIBRARY_PATH")
