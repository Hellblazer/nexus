# SPDX-License-Identifier: AGPL-3.0-or-later
"""The client's engine-log readers against lines rendered through the engine's RDR-167 sink (nexus-dngtz.1.11).

The sink writes every line break inside a message as a literal ``\\x0a``, so a PostgreSQL message with its
``Where:`` line, a Liquibase ``Reason:`` block or a client value with a newline stays on ONE physical line. The
other reader tests replay logs from before the sink. These replay the same RDR-231 halt, and an uncaught
exception, as the sink renders them (tests/fixtures/rdr231_halt/README.txt), so a reader that shows the text after
an escaped line break, or loses the SQL one-liner, fails here.
"""

from __future__ import annotations

from pathlib import Path

from nexus.daemon import storage_service_daemon as ssd
from nexus.daemon.readiness import MigrationLogScanner
from nexus.health import _last_boot_failure_detail
from tests._rdr231_halt import FIXTURES, RAISE_START, RAISE_URL

FAILED_BOOT: Path = FIXTURES / "engine_native_log_failed_boot_post_sink.txt"
UNCAUGHT: Path = FIXTURES / "engine_native_log_uncaught_post_sink.txt"


def test_the_fixtures_are_post_sink() -> None:
    """Non-vacuity: each fixture carries escaped line breaks, and the failed boot's RAISE has its Where: line."""
    text = FAILED_BOOT.read_text(encoding="utf-8")
    assert text.count("\\x0a") >= 5
    assert "halts-startup\\x0a  Where: PL/pgSQL" in text
    assert "\\x0aline two: event=forged" in UNCAUGHT.read_text(encoding="utf-8")


def test_the_root_cause_ends_at_the_runbook_url() -> None:
    cause = ssd.last_engine_root_cause(FAILED_BOOT, 0)
    assert cause is not None and cause.startswith(RAISE_START) and cause.endswith(RAISE_URL), cause


def test_the_engine_error_line_shows_no_text_after_an_escaped_line_break() -> None:
    for reason in (ssd.last_engine_error_line(FAILED_BOOT, 0), ssd.engine_boot_failure_reason(FAILED_BOOT)):
        assert reason is not None and "event=schema_migration_failed" in reason, reason
        assert reason.endswith(RAISE_URL) and "\\x0a" not in reason and "Where:" not in reason, reason


def test_an_uncaught_exception_gives_its_reason_and_not_its_forged_second_line() -> None:
    for reason in (ssd.last_engine_error_line(UNCAUGHT, 0), ssd.engine_boot_failure_reason(UNCAUGHT)):
        assert reason is not None and "event=uncaught_exception" in reason, reason
        assert reason.endswith("java.lang.IllegalStateException: config invalid"), reason
        assert "event=forged" not in reason, reason


def test_the_doctor_detail_keeps_the_sql_one_liner_and_stops_at_the_line_break() -> None:
    detail = _last_boot_failure_detail(FAILED_BOOT)
    assert detail is not None and detail.startswith("Liquibase changeset note-uri-001-1: ERROR: " + RAISE_START), detail
    assert "\\x0a" not in detail and "Where:" not in detail, detail


def test_the_scanner_reads_the_failed_boot_and_nothing_from_the_uncaught_exception() -> None:
    scanner = MigrationLogScanner()
    scanner.feed(FAILED_BOOT.read_text(encoding="utf-8").splitlines())
    assert scanner.failed is True
    assert scanner.failure_line is not None and "dev.nexus.service.Main" in scanner.failure_line
    other = MigrationLogScanner()
    other.feed(UNCAUGHT.read_text(encoding="utf-8").splitlines())
    assert (other.started, other.complete, other.failed) == (False, False, False)


def test_the_liquibase_ui_line_alone_gives_a_reason_that_ends_at_the_url(tmp_path: Path) -> None:
    """A boot that wrote only Liquibase's UI-channel line (LineSafeUIService) still reads, cut at the escape."""
    ui = FAILED_BOOT.read_text(encoding="utf-8").splitlines()[0]
    assert ui.startswith("ERROR: Exception Primary Reason:")
    log = tmp_path / "storage_service_native.log"
    log.write_text(ui + "\n", encoding="utf-8")
    reason = ssd.last_engine_error_line(log, 0)
    assert reason is not None and reason.endswith(RAISE_URL) and "Where:" not in reason, reason


def test_a_reason_that_starts_with_an_escaped_line_break_is_not_empty(tmp_path: Path) -> None:
    log = tmp_path / "storage_service_native.log"
    log.write_text(
        "2026-11-18 07:01:05,171 ERROR [main] dev.nexus.service.Main [cid=- tenant=- session=-] - "
        'event=schema_migration_failed error="Liquibase migration failed"\n'
        "Caused by: org.postgresql.util.PSQLException: \\x0a  ERROR: the real message\\x0aWhere: x\n",
        encoding="utf-8",
    )
    assert ssd.last_engine_root_cause(log, 0) == "ERROR: the real message"


def test_escaped_and_empty_thread_fields_still_parse() -> None:
    """%threadSafe escapes the thread name like an MDC value; a name holding '] - event=' cannot end the bracket."""
    escaped = (
        "2026-11-18 07:01:05,171 INFO  [w\\x5d\\x20-\\x20event\\x3dschema_migration_complete] "
        "dev.nexus.service.db.SchemaMigrator [cid=- tenant=- session=-] - event=schema_migration_start"
    )
    empty = "2026-11-18 07:01:05,171 INFO  [] dev.nexus.service.db.SchemaMigrator [cid=- tenant=- session=-] - event=schema_migration_complete"
    hikari = (
        "2026-11-18 07:01:05,171 ERROR [HikariPool-1\\x20housekeeper] dev.nexus.service.Main "
        "[cid=- tenant=- session=-] - event=schema_migration_failed error=x"
    )
    scanner = MigrationLogScanner()
    scanner.feed([escaped])
    assert (scanner.started, scanner.complete) == (True, False)
    scanner.feed([empty])
    assert scanner.complete is True
    scanner.feed([hikari])
    assert scanner.failed is True
