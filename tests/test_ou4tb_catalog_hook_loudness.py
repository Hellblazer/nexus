# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ou4tb (a): catalog post-store hook failures must be LOUD and counted.

Rug audit 2026-07-15, items 2/3/10. Four catalog-registration hooks swallowed
their failures to ``_log.debug`` and nothing else, so a document could land in
T3 and never be registered in the catalog — no doc_id, no manifest, no links —
while the index run reported success. Only a rebuild recovers it, and nobody
was told to run one.

These pin the two halves of the fix: the log level, and the audit row that
makes ``nx doctor`` able to say how many documents are affected.
"""
from __future__ import annotations

import sqlite3

import pytest

from nexus.commands.doctor import _catalog_hook_failure_lines


def _hook_failures_db(rows: list[tuple[str, str]]) -> sqlite3.Connection:
    """In-memory T2 shaped like the hook_failures audit table."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE hook_failures ("
        " doc_id TEXT, collection TEXT, hook_name TEXT, error TEXT,"
        " chain TEXT, occurred_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO hook_failures (doc_id, collection, hook_name, error,"
        " chain, occurred_at) VALUES ('d', 'c', ?, 'boom', 'document', ?)",
        rows,
    )
    conn.commit()
    return conn


class TestDoctorSurfacesUncatalogedDocuments:
    def test_clean_store_reports_ok(self) -> None:
        conn = _hook_failures_db([])
        (line,) = _catalog_hook_failure_lines(conn, {"hook_failures"})
        assert "Catalog registration" in line
        assert "no un-cataloged documents" in line

    def test_failures_report_count_oldest_and_the_recovery_command(self) -> None:
        conn = _hook_failures_db([
            ("catalog_index_hook", "2026-07-19T10:00:00Z"),
            ("catalog_store_hook", "2026-07-20T11:00:00Z"),
            ("catalog_pdf_hook", "2026-07-20T12:00:00Z"),
        ])
        (line,) = _catalog_hook_failure_lines(conn, {"hook_failures"})
        assert "3 document(s) indexed but NOT cataloged" in line
        assert "2026-07-19T10:00:00Z" in line, "names the OLDEST occurrence"
        assert "nx catalog rebuild" in line, "names the recovery command"

    def test_unrelated_hook_failures_are_not_counted(self) -> None:
        """The count must mean what it says — catalog registration only."""
        conn = _hook_failures_db([
            ("aspect_enqueue", "2026-07-20T10:00:00Z"),
            ("taxonomy_assign", "2026-07-20T10:00:00Z"),
        ])
        (line,) = _catalog_hook_failure_lines(conn, {"hook_failures"})
        assert "no un-cataloged documents" in line

    def test_absent_table_is_silent_not_alarming(self) -> None:
        """A not-yet-migrated T2 must not render a scary unknown."""
        conn = sqlite3.connect(":memory:")
        assert _catalog_hook_failure_lines(conn, set()) == []


class TestTheFourSitesAreNoLongerSilent:
    """Grep-level pins: DEBUG here means a silent non-registration."""

    @pytest.mark.parametrize(
        ("module_path", "event"),
        [
            ("src/nexus/indexer.py", "catalog_hook_failed"),
            ("src/nexus/indexer.py", "catalog_link_generation_failed"),
            ("src/nexus/catalog/store_hook.py", "catalog_store_hook_failed"),
            ("src/nexus/pipeline_stages.py", "catalog_pdf_hook_failed"),
        ],
    )
    def test_site_logs_at_warning_and_records_an_audit_row(
        self, module_path: str, event: str
    ) -> None:
        from pathlib import Path

        src = Path(module_path).read_text()
        assert f'_log.debug("{event}"' not in src, (
            f"{event} is back at DEBUG — a silent non-registration"
        )
        assert f'_log.warning("{event}"' in src

        # The audit row is what doctor counts; a WARNING alone is still only
        # visible to whoever is reading logs at the time.
        idx = src.index(f'_log.warning("{event}"')
        following = src[idx: idx + 600]
        assert "record_catalog_hook_failure(" in following, (
            f"{event} logs loudly but records no audit row, so nx doctor "
            f"still cannot tell the user anything"
        )
