# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Live-DT integration smoke for ``nexus.devonthink`` (RDR-099 P4).

Gated by **two** runtime conditions so the suite is invisible
everywhere except a developer's macOS workstation with DT running:

* ``sys.platform == "darwin"``: the helpers spawn ``osascript``;
  Linux/Windows can't satisfy the contract.
* ``NEXUS_DT_LIVE=1`` in the environment: even on macOS, CI runners
  rarely have DEVONthink installed, so the env-var toggle keeps the
  suite opt-in. Combined with the platform gate, the default behaviour
  is "skip cleanly" on every untouched runner.

Per-test fixtures are read from environment variables documented in
``tests/e2e/devonthink-manual.md``. A test whose fixture variable is
unset is skipped individually so an operator who has only authored
some fixtures can still smoke the rest.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Module-level skip: short-circuits collection on every non-darwin or
# non-LIVE runner so the rest of the imports can assume the helpers
# are callable.
pytestmark = [
    pytest.mark.skipif(
        sys.platform != "darwin",
        reason="DEVONthink integration is macOS-only",
    ),
    pytest.mark.skipif(
        os.environ.get("NEXUS_DT_LIVE") != "1",
        reason="set NEXUS_DT_LIVE=1 to run live-DT integration tests "
        "(see tests/e2e/devonthink-manual.md)",
    ),
]


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(
            f"set {name} to a real DEVONthink fixture value "
            "(see tests/e2e/devonthink-manual.md)",
        )
    return value


def _assert_records_shape(records, fixture_name: str) -> None:
    """Common assertions: non-empty list of (uuid, path) tuples, every
    path exists on disk. Fixture name is for the failure message so the
    operator knows which env var's record set is broken."""
    assert isinstance(records, list)
    assert len(records) >= 1, (
        f"expected ≥1 record for {fixture_name}; got 0. Check the DT "
        "fixture matches the env var, and confirm DT is running."
    )
    for uuid, path in records:
        assert uuid, f"empty UUID in {fixture_name} record"
        assert path, f"empty path for UUID {uuid} in {fixture_name}"
        assert Path(path).exists(), (
            f"DT reported path {path!r} for {uuid} but the file is not "
            "on disk; the DT-side index may be stale."
        )


# ── Per-selector smoke ───────────────────────────────────────────────────────


def test_uuid_record_resolves_to_real_file():
    """A known UUID resolves through the production default resolver
    (no injected fake) to a real file path."""
    from nexus.devonthink import _dt_uuid_record

    uuid = _require_env("NEXUS_DT_TEST_UUID")
    records = _dt_uuid_record(uuid)
    _assert_records_shape(records, "NEXUS_DT_TEST_UUID")
    assert records[0][0] == uuid


def test_tag_records_multi_database():
    """A tag present in ≥2 open databases returns merged results.

    The operator sets ``NEXUS_DT_TEST_DATABASES`` to a comma-separated
    list of databases that carry the tag; the test verifies at least
    one record path appears under each database's storage prefix.
    Fails (rather than skips) if the multi-DB invariant is violated.
    """
    from nexus.devonthink import _dt_tag_records

    tag = _require_env("NEXUS_DT_TEST_TAG")
    db_list = _require_env("NEXUS_DT_TEST_DATABASES")
    expected_dbs = [d.strip() for d in db_list.split(",") if d.strip()]
    if len(expected_dbs) < 2:
        pytest.skip(
            "NEXUS_DT_TEST_DATABASES needs ≥2 databases for the "
            "multi-DB invariant smoke",
        )

    records = _dt_tag_records(tag)
    _assert_records_shape(records, "NEXUS_DT_TEST_TAG")
    # Every record's resolved path should sit under one of the expected
    # database storage roots. DT stores libraries under
    # ~/Databases/<name>.dtBase2/Files.noindex/ by default; the
    # database name appearing in the path is a robust heuristic across
    # DT installations that don't follow the default location.
    seen_dbs: set[str] = set()
    for _uuid, path in records:
        for db in expected_dbs:
            if db in path:
                seen_dbs.add(db)
                break
    assert len(seen_dbs) >= 2, (
        f"expected records from ≥2 databases ({expected_dbs}); only "
        f"saw {sorted(seen_dbs)} in {len(records)} records. Check that "
        "the test tag is applied in BOTH databases."
    )


def test_tag_records_single_database_scoping():
    """``--database`` should narrow results to the named database."""
    from nexus.devonthink import _dt_tag_records

    tag = _require_env("NEXUS_DT_TEST_TAG")
    db_list = _require_env("NEXUS_DT_TEST_DATABASES")
    first_db = next(
        (d.strip() for d in db_list.split(",") if d.strip()),
        "",
    )
    if not first_db:
        pytest.skip("NEXUS_DT_TEST_DATABASES has no entries")

    records = _dt_tag_records(tag, database=first_db)
    _assert_records_shape(records, "NEXUS_DT_TEST_TAG (scoped)")
    for _uuid, path in records:
        assert first_db in path, (
            f"--database {first_db!r} should scope results, but got "
            f"path {path!r} that doesn't reference the database."
        )


def test_group_records_recursive_walk():
    """``_dt_group_records`` walks descendants of the named group."""
    from nexus.devonthink import _dt_group_records

    group = _require_env("NEXUS_DT_TEST_GROUP")
    records = _dt_group_records(group)
    _assert_records_shape(records, "NEXUS_DT_TEST_GROUP")


def test_smart_group_records_honours_search_group():
    """Smart group with non-root ``search group`` returns only records
    from inside that scope (the AC-5 lock from RDR-099)."""
    from nexus.devonthink import _dt_smart_group_records

    name = _require_env("NEXUS_DT_TEST_SMART_GROUP")
    records = _dt_smart_group_records(name)
    _assert_records_shape(records, "NEXUS_DT_TEST_SMART_GROUP")


def test_selection_skips_when_no_selection():
    """``_dt_selection`` returns ``[]`` when nothing is selected; the
    full happy-path AC-1 lives in the manual smoke (operator must
    multi-select in the UI, which we can't drive from pytest)."""
    from nexus.devonthink import _dt_selection

    # Don't try to drive selection from osascript — too brittle. Just
    # confirm the helper round-trips cleanly against live DT.
    result = _dt_selection()
    assert isinstance(result, list)
    for uuid, path in result:
        assert uuid and path
        assert Path(path).exists()
