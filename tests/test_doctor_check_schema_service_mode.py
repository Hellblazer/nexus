"""nx doctor --check-schema service-mode honesty (nexus-p0clh).

In service mode the T2 schema lives in Postgres (Liquibase-managed by the
nexus-service), not the local SQLite migrations. The check previously printed
"T2 database not found — nothing to check", which reads as an error. It must
report N/A for service mode instead.
"""

from __future__ import annotations

from nexus.commands.doctor import _run_check_schema
from nexus.db.storage_mode import StorageBackend


def test_check_schema_reports_na_in_service_mode(monkeypatch, capsys):
    monkeypatch.setattr(
        "nexus.db.storage_mode.storage_backend_for",
        lambda store: StorageBackend.SERVICE,
    )
    _run_check_schema()
    out = capsys.readouterr().out
    assert "service-backed" in out
    assert "N/A in service mode" in out
    # The misleading "not found" message must NOT appear in service mode.
    assert "T2 database not found" not in out


def test_check_schema_sqlite_mode_unaffected(monkeypatch, tmp_path, capsys):
    """In SQLite mode with no DB, the existing not-found path still applies."""
    monkeypatch.setattr(
        "nexus.db.storage_mode.storage_backend_for",
        lambda store: StorageBackend.SQLITE,
    )
    monkeypatch.setattr(
        "nexus.commands._helpers.default_db_path", lambda: tmp_path / "absent.db"
    )
    _run_check_schema()
    out = capsys.readouterr().out
    assert "T2 database not found" in out
