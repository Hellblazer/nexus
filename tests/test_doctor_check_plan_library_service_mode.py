"""nx doctor --check-plan-library service-mode honesty.

In service mode the plan library lives in Postgres; the local-SQLite
dimensional census is N/A, not a failure. Previously the check exited
non-zero with "T2 database not found" on every fresh service-mode install —
the release-sandbox smoke's only red check (caught during 6.2.0 prep).
Mirrors the --check-schema treatment (nexus-p0clh).
"""

from __future__ import annotations

import click
import pytest

from nexus.commands.doctor import _run_check_plan_library
from nexus.db.storage_mode import StorageBackend


def test_check_plan_library_reports_na_in_service_mode(monkeypatch, capsys):
    monkeypatch.setattr(
        "nexus.db.storage_mode.storage_backend_for",
        lambda store: StorageBackend.SERVICE,
    )
    _run_check_plan_library()
    out = capsys.readouterr().out
    assert "service-backed" in out
    assert "N/A in service mode" in out
    assert "T2 database not found" not in out


