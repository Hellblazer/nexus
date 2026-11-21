# SPDX-License-Identifier: AGPL-3.0-or-later
"""collections-001 (the live-tuple index) must follow D2's vectors-065 in the master changelog (RDR-229 P3.8).

collections-001-1's counter is a SECURITY INVOKER function that reads every tenant's rows of
``nexus.catalog_collections`` through D2's ``catalog_collections_owner_read`` policy (changeset
``vectors-065-catalog-collections-owner-read``). The table is FORCE ROW LEVEL SECURITY, so before that policy exists the
counter reads no row and answers 0, and the index build (collections-001-2) would then be attempted over a real duplicate
and refused. The include order is therefore load-bearing: vectors-065 first.

D2's file is not on this branch yet, so the order test is an expected failure until it lands. It is strict: the moment
vectors-065 is included, the test XPASSes, which fails the run and forces the mark off. The test REQUIRES vectors-065 in
the include list (it fails when absent, never passes vacuously); the unmarked companion keeps the other half live.
"""

from __future__ import annotations

import pytest

from tests.test_changelog_rls_lint import CHANGELOG_DIR

_MASTER = CHANGELOG_DIR / "db.changelog-master.xml"
_INDEX = "db/changelog/collections-001-tuple-unique-index.xml"
_OWNER_READ = "db/changelog/vectors-065-catalog-collections-owner-read.xml"


def _includes() -> list[str]:
    import xml.etree.ElementTree as ET

    ns = "{http://www.liquibase.org/xml/ns/dbchangelog}"
    root = ET.parse(_MASTER).getroot()
    return [e.get("file", "") for e in root.iter(f"{ns}include")]


def test_the_live_tuple_index_changelog_is_included_once() -> None:
    inc = _includes()
    assert inc.count(_INDEX) == 1, f"{_INDEX} must be in {_MASTER.name} exactly once (found {inc.count(_INDEX)})"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "D2's vectors-065-catalog-collections-owner-read is not on develop yet; remove this mark when it lands, "
        "RDR-229 P3.8 nexus-p2m1x.28"
    ),
)
def test_the_live_tuple_index_follows_the_owner_read_policy() -> None:
    inc = _includes()
    assert _OWNER_READ in inc, f"{_OWNER_READ} is not in {_MASTER.name}'s include list"
    assert _INDEX in inc
    assert inc.index(_INDEX) > inc.index(_OWNER_READ), (
        f"{_INDEX} must be included after {_OWNER_READ}: the counter reads every tenant through D2's policy"
    )
