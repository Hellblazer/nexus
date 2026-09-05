# SPDX-License-Identifier: AGPL-3.0-or-later
"""`nx index failures` — the read surface for nexus-nukn3's durable per-file
index-failure record.

Mocks HttpTelemetryStore; no substrate needed (mirrors the doctor
`--check-index-failures` tests in tests/test_false_clean_diagnostics_service_mode.py).
"""
from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from nexus.cli import main


def test_no_failures_reports_clean() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.list_index_failures.return_value = {
            "rows": [], "total": 0, "oldest_occurred_at": "",
        }
        result = CliRunner().invoke(main, ["index", "failures"])

    assert result.exit_code == 0, result.output
    assert "no recorded failures" in result.output


def test_lists_rows_and_the_exact_total() -> None:
    rows = [
        {
            "run_id": "run-1", "file_path": "/repo/a.pdf",
            "error_class": "UnextractableContentError",
            "error": "produced empty output",
            "occurred_at": "2026-09-05T00:00:00Z",
        },
        {
            "run_id": "run-1", "file_path": "/repo/b.pdf",
            "error_class": "UnextractableContentError",
            "error": "scanned image",
            "occurred_at": "2026-09-05T00:01:00Z",
        },
    ]
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.list_index_failures.return_value = {
            "rows": rows, "total": 2, "oldest_occurred_at": "2026-09-05T00:00:00Z",
        }
        result = CliRunner().invoke(main, ["index", "failures"])

    assert result.exit_code == 0, result.output
    assert "2 row(s)" in result.output
    assert "/repo/a.pdf" in result.output
    assert "/repo/b.pdf" in result.output
    assert "UnextractableContentError" in result.output


def test_total_exceeding_the_page_is_stated_not_hidden() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.list_index_failures.return_value = {
            "rows": [{
                "run_id": "run-1", "file_path": "/repo/a.pdf",
                "error_class": "UnextractableContentError",
                "error": "boom", "occurred_at": "2026-09-05T00:00:00Z",
            }],
            "total": 50,
            "oldest_occurred_at": "2026-09-05T00:00:00Z",
        }
        result = CliRunner().invoke(main, ["index", "failures", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "50 row(s)" in result.output
    assert "49 more not shown" in result.output


def test_run_id_option_scopes_the_query() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.list_index_failures.return_value = {
            "rows": [], "total": 0, "oldest_occurred_at": "",
        }
        result = CliRunner().invoke(main, ["index", "failures", "--run-id", "run-xyz"])

    assert result.exit_code == 0, result.output
    store.return_value.list_index_failures.assert_called_once_with(
        run_id="run-xyz", days=0, limit=100,
    )
    assert "for run run-xyz" in result.output
