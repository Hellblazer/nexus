# SPDX-License-Identifier: AGPL-3.0-or-later
"""`nx index failures` — the read surface for nexus-nukn3's durable per-file
index-failure record.

Mocks HttpTelemetryStore; no substrate needed (mirrors the doctor
`--check-index-failures` tests in tests/test_false_clean_diagnostics_service_mode.py).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
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


# ── code-review fold-in (T2 code-review-nexus-nukn3-410720f6a [24573],
# Important): construction/list must not surface a raw traceback on an
# unreachable service or a 404 from an old engine, contradicting the
# wire-ledger entry's claim of a clean degrade. ─────────────────────────────


def test_unreachable_service_is_a_click_exception_not_a_traceback() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.list_index_failures.side_effect = httpx.ConnectError("refused")
        result = CliRunner().invoke(main, ["index", "failures"])

    assert result.exit_code != 0
    assert not isinstance(result.exception, httpx.ConnectError), (
        f"a raw httpx exception escaped instead of a clean ClickException: {result.exception!r}"
    )
    assert "unreachable" in result.output.lower()


def test_route_absent_on_old_engine_is_a_click_exception_not_a_traceback() -> None:
    response = MagicMock(status_code=404)
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.list_index_failures.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=response,
        )
        result = CliRunner().invoke(main, ["index", "failures"])

    assert result.exit_code != 0
    assert not isinstance(result.exception, httpx.HTTPStatusError), (
        f"a raw httpx exception escaped instead of a clean ClickException: {result.exception!r}"
    )
    assert "route not found" in result.output.lower()
    assert "upgrade the engine" in result.output.lower()


# ── nx index failures --clear (fold-in: the critic's Critical remedy) ───────


def test_clear_without_scope_is_a_usage_error() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore"):
        result = CliRunner().invoke(main, ["index", "failures", "--clear"])

    assert result.exit_code != 0
    assert "run_id" in result.output or "days" in result.output


def test_clear_by_run_id_deletes_and_reports_the_count() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.trim_index_failures.return_value = 3
        result = CliRunner().invoke(
            main, ["index", "failures", "--clear", "--run-id", "run-xyz"],
        )

    assert result.exit_code == 0, result.output
    store.return_value.trim_index_failures.assert_called_once_with(
        run_id="run-xyz", days=0, dry_run=False,
    )
    assert "Cleared 3" in result.output


def test_clear_by_older_than_days_deletes_and_reports_the_count() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.trim_index_failures.return_value = 7
        result = CliRunner().invoke(
            main, ["index", "failures", "--clear", "--older-than-days", "90"],
        )

    assert result.exit_code == 0, result.output
    store.return_value.trim_index_failures.assert_called_once_with(
        run_id="", days=90, dry_run=False,
    )
    assert "Cleared 7" in result.output


def test_clear_route_absent_on_old_engine_is_a_click_exception() -> None:
    response = MagicMock(status_code=404)
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.trim_index_failures.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=response,
        )
        result = CliRunner().invoke(
            main, ["index", "failures", "--clear", "--run-id", "run-xyz"],
        )

    assert result.exit_code != 0
    assert not isinstance(result.exception, httpx.HTTPStatusError)
    assert "route not found" in result.output.lower()


# ── nx index failures --acknowledge (fold-in round 2: the critic's Critical
# finding that --clear alone is undone by the next re-index) ────────────────


def test_acknowledge_requires_file_or_error_class() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore"):
        result = CliRunner().invoke(main, ["index", "failures", "--acknowledge"])

    assert result.exit_code != 0
    assert "--file" in result.output and "--error-class" in result.output


def test_acknowledge_by_explicit_error_class_skips_lookup() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        result = CliRunner().invoke(main, [
            "index", "failures", "--acknowledge",
            "--error-class", "UnextractableContentError",
            "--reason", "known limitation",
        ])

    assert result.exit_code == 0, result.output
    store.return_value.list_index_failures.assert_not_called()
    store.return_value.acknowledge_index_failure.assert_called_once_with(
        error_class="UnextractableContentError", file_path="", reason="known limitation",
    )
    assert "Acknowledged" in result.output


def test_acknowledge_by_file_resolves_error_class_from_the_backlog() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.list_index_failures.return_value = {
            "rows": [{
                "run_id": "run-1", "file_path": "/repo/broken.pdf",
                "error_class": "UnextractableContentError",
                "error": "encrypted", "occurred_at": "2026-09-05T00:00:00Z",
                "acknowledged": False,
            }],
            "total": 1, "oldest_occurred_at": "2026-09-05T00:00:00Z",
        }
        result = CliRunner().invoke(main, [
            "index", "failures", "--acknowledge", "--file", "/repo/broken.pdf",
        ])

    assert result.exit_code == 0, result.output
    # Round-4 fold-in (code review [24624] item 3): the auto-resolve lookup
    # must be a server-side exact file_path filter, never a wide
    # client-side-filtered page.
    store.return_value.list_index_failures.assert_called_once_with(
        limit=1, file_path="/repo/broken.pdf",
    )
    store.return_value.acknowledge_index_failure.assert_called_once_with(
        error_class="UnextractableContentError", file_path="/repo/broken.pdf", reason="",
    )


def test_acknowledge_by_file_with_no_recorded_failure_is_a_click_exception() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.list_index_failures.return_value = {
            "rows": [], "total": 0, "oldest_occurred_at": "",
        }
        result = CliRunner().invoke(main, [
            "index", "failures", "--acknowledge", "--file", "/repo/never-failed.pdf",
        ])

    assert result.exit_code != 0
    assert "no recorded failure" in result.output.lower()
    store.return_value.acknowledge_index_failure.assert_not_called()


def test_clear_and_acknowledge_are_mutually_exclusive() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore"):
        result = CliRunner().invoke(main, [
            "index", "failures", "--clear", "--acknowledge",
            "--error-class", "X", "--run-id", "r1",
        ])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_acknowledged_rows_are_marked_in_the_list_view() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.list_index_failures.return_value = {
            "rows": [{
                "run_id": "run-1", "file_path": "/repo/broken.pdf",
                "error_class": "UnextractableContentError",
                "error": "encrypted", "occurred_at": "2026-09-05T00:00:00Z",
                "acknowledged": True,
            }],
            "total": 1, "oldest_occurred_at": "2026-09-05T00:00:00Z",
        }
        result = CliRunner().invoke(main, ["index", "failures"])

    assert result.exit_code == 0, result.output
    assert "[ACKNOWLEDGED]" in result.output


def test_acknowledge_route_absent_on_old_engine_is_a_click_exception() -> None:
    response = MagicMock(status_code=404)
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.acknowledge_index_failure.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=response,
        )
        result = CliRunner().invoke(main, [
            "index", "failures", "--acknowledge",
            "--error-class", "UnextractableContentError",
        ])

    assert result.exit_code != 0
    assert not isinstance(result.exception, httpx.HTTPStatusError)
    assert "route not found" in result.output.lower()


# ── nx index failures --acks / --unacknowledge (round-4 fold-in: critique
# [24621] item 1 -- ack rows are write-only without a list/revoke surface) ──


def test_acks_lists_active_acknowledgments() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.list_index_failure_acknowledgments.return_value = {
            "rows": [
                {
                    "file_path": "/repo/broken.pdf",
                    "error_class": "UnextractableContentError",
                    "reason": "encrypted",
                    "created_at": "2026-09-05T00:00:00Z",
                },
                {
                    "file_path": "",
                    "error_class": "ScannedPdfNoOcrError",
                    "reason": "known systemic issue",
                    "created_at": "2026-09-05T00:01:00Z",
                },
            ],
            "total": 2,
        }
        result = CliRunner().invoke(main, ["index", "failures", "--acks"])

    assert result.exit_code == 0, result.output
    store.return_value.list_index_failure_acknowledgments.assert_called_once_with()
    assert "2 active acknowledgment(s)" in result.output
    assert "/repo/broken.pdf" in result.output
    assert "UnextractableContentError" in result.output
    assert "ScannedPdfNoOcrError" in result.output
    assert "encrypted" in result.output


def test_acks_empty_reports_none_active() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.list_index_failure_acknowledgments.return_value = {
            "rows": [], "total": 0,
        }
        result = CliRunner().invoke(main, ["index", "failures", "--acks"])

    assert result.exit_code == 0, result.output
    assert "no active acknowledgments" in result.output.lower()


def test_acks_route_absent_on_old_engine_is_a_click_exception() -> None:
    response = MagicMock(status_code=404)
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.list_index_failure_acknowledgments.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=response,
        )
        result = CliRunner().invoke(main, ["index", "failures", "--acks"])

    assert result.exit_code != 0
    assert not isinstance(result.exception, httpx.HTTPStatusError)
    assert "route not found" in result.output.lower()


def test_unacknowledge_requires_file_or_error_class() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore"):
        result = CliRunner().invoke(main, ["index", "failures", "--unacknowledge"])

    assert result.exit_code != 0
    assert "--file" in result.output and "--error-class" in result.output


def test_unacknowledge_by_explicit_error_class_skips_lookup() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.unacknowledge_index_failure.return_value = 1
        result = CliRunner().invoke(main, [
            "index", "failures", "--unacknowledge",
            "--error-class", "ScannedPdfNoOcrError",
        ])

    assert result.exit_code == 0, result.output
    store.return_value.list_index_failure_acknowledgments.assert_not_called()
    store.return_value.unacknowledge_index_failure.assert_called_once_with(
        error_class="ScannedPdfNoOcrError", file_path="",
    )
    assert "Revoked" in result.output


def test_unacknowledge_by_file_resolves_error_class_from_the_acks_list() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.list_index_failure_acknowledgments.return_value = {
            "rows": [{
                "file_path": "/repo/broken.pdf",
                "error_class": "UnextractableContentError",
                "reason": "encrypted",
                "created_at": "2026-09-05T00:00:00Z",
            }],
            "total": 1,
        }
        store.return_value.unacknowledge_index_failure.return_value = 1
        result = CliRunner().invoke(main, [
            "index", "failures", "--unacknowledge", "--file", "/repo/broken.pdf",
        ])

    assert result.exit_code == 0, result.output
    # Round-5 fold-in (code-review [24635] item 2): the auto-resolve lookup
    # must be a server-side exact file_path filter, never a wide
    # tenant-wide page filtered client-side.
    store.return_value.list_index_failure_acknowledgments.assert_called_once_with(
        file_path="/repo/broken.pdf",
    )
    store.return_value.unacknowledge_index_failure.assert_called_once_with(
        error_class="UnextractableContentError", file_path="/repo/broken.pdf",
    )


def test_unacknowledge_by_file_with_no_ack_is_a_click_exception() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.list_index_failure_acknowledgments.return_value = {
            "rows": [], "total": 0,
        }
        result = CliRunner().invoke(main, [
            "index", "failures", "--unacknowledge", "--file", "/repo/never-acked.pdf",
        ])

    assert result.exit_code != 0
    assert "no acknowledgment found" in result.output.lower()
    store.return_value.unacknowledge_index_failure.assert_not_called()


def test_unacknowledge_by_file_ambiguous_across_classes_is_a_usage_error() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.list_index_failure_acknowledgments.return_value = {
            "rows": [
                {
                    "file_path": "/repo/broken.pdf", "error_class": "ClassA",
                    "reason": "", "created_at": "2026-09-05T00:00:00Z",
                },
                {
                    "file_path": "/repo/broken.pdf", "error_class": "ClassB",
                    "reason": "", "created_at": "2026-09-05T00:01:00Z",
                },
            ],
            "total": 2,
        }
        result = CliRunner().invoke(main, [
            "index", "failures", "--unacknowledge", "--file", "/repo/broken.pdf",
        ])

    assert result.exit_code != 0
    assert "disambiguate" in result.output.lower()
    store.return_value.unacknowledge_index_failure.assert_not_called()


def test_unacknowledge_with_nothing_deleted_reports_not_found() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.unacknowledge_index_failure.return_value = 0
        result = CliRunner().invoke(main, [
            "index", "failures", "--unacknowledge",
            "--error-class", "NeverAcked",
        ])

    assert result.exit_code == 0, result.output
    assert "no acknowledgment found for" in result.output.lower()


def test_unacknowledge_route_absent_on_old_engine_is_a_click_exception() -> None:
    response = MagicMock(status_code=404)
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.unacknowledge_index_failure.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=response,
        )
        result = CliRunner().invoke(main, [
            "index", "failures", "--unacknowledge",
            "--error-class", "UnextractableContentError",
        ])

    assert result.exit_code != 0
    assert not isinstance(result.exception, httpx.HTTPStatusError)
    assert "route not found" in result.output.lower()


def test_all_four_modes_are_pairwise_mutually_exclusive() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore"):
        result = CliRunner().invoke(main, [
            "index", "failures", "--acks", "--unacknowledge", "--error-class", "X",
        ])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


# ── nx index failures --clear --dry-run (code review [24624] item 2) ────────


def test_dry_run_without_clear_is_a_usage_error() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore"):
        result = CliRunner().invoke(main, [
            "index", "failures", "--dry-run", "--run-id", "run-xyz",
        ])

    assert result.exit_code != 0
    assert "--dry-run" in result.output and "--clear" in result.output


def test_dry_run_with_acknowledge_is_a_usage_error() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore"):
        result = CliRunner().invoke(main, [
            "index", "failures", "--dry-run", "--acknowledge",
            "--error-class", "X",
        ])

    assert result.exit_code != 0
    assert "--dry-run" in result.output and "--clear" in result.output


def test_clear_dry_run_previews_without_deleting() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.trim_index_failures.return_value = 5
        result = CliRunner().invoke(main, [
            "index", "failures", "--clear", "--run-id", "run-xyz", "--dry-run",
        ])

    assert result.exit_code == 0, result.output
    store.return_value.trim_index_failures.assert_called_once_with(
        run_id="run-xyz", days=0, dry_run=True,
    )
    assert "Would clear 5" in result.output


# ── --older-than-days floor (round-4 fold-in: critique [24621] item 3,
# round-2 leftover) ──────────────────────────────────────────────────────────


def test_older_than_days_rejects_zero() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore"):
        result = CliRunner().invoke(main, [
            "index", "failures", "--clear", "--older-than-days", "0",
        ])

    assert result.exit_code != 0
    assert "older-than-days" in result.output.lower()


def test_older_than_days_rejects_negative() -> None:
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore"):
        result = CliRunner().invoke(main, [
            "index", "failures", "--clear", "--older-than-days", "-5",
        ])

    assert result.exit_code != 0
    assert "older-than-days" in result.output.lower()


def test_older_than_days_omitted_still_means_no_bound() -> None:
    # Omission must keep working after the IntRange(min=1) floor was added
    # (a naive `type=IntRange(min=1)` + a concrete `default=0` would break
    # this -- Click validates the default too).
    with patch("nexus.db.t2.http_telemetry_store.HttpTelemetryStore") as store:
        store.return_value.trim_index_failures.return_value = 2
        result = CliRunner().invoke(
            main, ["index", "failures", "--clear", "--run-id", "run-xyz"],
        )

    assert result.exit_code == 0, result.output
    store.return_value.trim_index_failures.assert_called_once_with(
        run_id="run-xyz", days=0, dry_run=False,
    )
