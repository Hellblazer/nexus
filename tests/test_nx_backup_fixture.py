# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine's committed backup fixture is a real ``.nxtenant`` file (nexus-csayy.7, RDR-248 Step 6).

``service/src/test/resources/fixtures/rdr248-tenant-backup.nxtenant`` is written by the engine tests'
``TenantBackupFiles`` and loaded by ``TenantImportFixtureTest`` on every later engine. This test is the other half:
the Python client's own verifier (``nx backup --check``) accepts it, so the Java writer cannot drift from
``nexus.backup.tenant_file`` and the fixture stays a file a user's ``nx backup restore`` could send. No engine and no
tenant: it reads the file.
"""

from __future__ import annotations

from pathlib import Path

from nexus.backup.tenant_file import index_file, verify_file

_FIXTURE = Path(__file__).resolve().parents[1] / "service/src/test/resources/fixtures/rdr248-tenant-backup.nxtenant"


def test_the_committed_engine_fixture_passes_nx_backup_check_and_indexes_for_a_restore() -> None:
    assert _FIXTURE.is_file(), f"the engine fixture is missing: {_FIXTURE}"

    report = verify_file(_FIXTURE)

    assert report.header["format_version"] == 1
    assert len(report.tables) >= 26, [t.name for t in report.tables]
    empty = [t.name for t in report.tables if t.rows == 0]
    assert not empty, f"tables with no row in the fixture: {empty}"
    assert {t.table_class for t in report.tables} == {"data", "derived"}
    index = index_file(_FIXTURE, part_bytes=8 * 1024 * 1024)
    assert set(index.tables) == {t.name for t in report.tables}
    assert index.header_sha256
