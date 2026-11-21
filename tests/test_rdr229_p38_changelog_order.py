# SPDX-License-Identifier: AGPL-3.0-or-later
"""collections-001 (the live-tuple index) must follow the changesets it depends on in the master changelog (RDR-229 P3.8).

Two dependencies, both unmerged at the time of writing:

* D2's ``vectors-065`` (``catalog_collections_owner_read``). collections-001-1's counter is a SECURITY INVOKER function
  that reads every tenant's rows of ``nexus.catalog_collections`` through that policy. The table is FORCE ROW LEVEL
  SECURITY, so before the policy exists the counter reads no row and answers 0, and the index build (collections-001-2)
  would be attempted over a real duplicate and refused.
* marlowe's ``catalog-051``, which makes catalog-044's owner-rewrite AFTER trigger skip, with a NOTICE, an owner rewrite
  that would land a document's collection on a live tuple another collection holds. The index must not exist before it:
  without it the trigger turns a document write into an unnamed unique violation.

Each dependency has two tests. The PRESENCE test is a strict xfail: the moment the changeset is included it XPASSes, which
fails the run and forces the mark off. The ORDER test is live and unmarked: when the changeset is present, the index
changeset comes after it. It passes vacuously while the changeset is absent; the presence test is what keeps that visible.

A changeset is found by its id, in the file that holds it, not by that file's name: a renamed file must not hide the order.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.test_changelog_rls_lint import CHANGELOG_DIR

_NS = "{http://www.liquibase.org/xml/ns/dbchangelog}"
_MASTER = CHANGELOG_DIR / "db.changelog-master.xml"
_RESOURCES = CHANGELOG_DIR.parent.parent  # service/src/main/resources
_INDEX_CHANGESET = "collections-001-2"
_OWNER_READ_PREFIX = "vectors-065"
_TRIGGER_SKIP_PREFIX = "catalog-051"


def _includes() -> list[str]:
    return [e.get("file", "") for e in ET.parse(_MASTER).getroot().iter(f"{_NS}include")]


def _changeset_ids(include: str) -> list[str]:
    path = Path(_RESOURCES / include)
    if not path.is_file():
        return []
    return [c.get("id", "") for c in ET.parse(path).getroot().iter(f"{_NS}changeSet")]


def _position(changeset_prefix: str) -> int | None:
    """Index in the master include list of the file holding a changeset whose id starts with *changeset_prefix*."""
    for i, include in enumerate(_includes()):
        if any(cid.startswith(changeset_prefix) for cid in _changeset_ids(include)):
            return i
    return None


def test_the_live_tuple_index_changeset_is_in_exactly_one_included_file() -> None:
    holders = [i for i in _includes() if _INDEX_CHANGESET in _changeset_ids(i)]
    assert len(holders) == 1, f"changeset {_INDEX_CHANGESET} must be in exactly one file of {_MASTER.name} (found {holders})"


def test_a_changeset_is_found_by_id_in_the_file_that_holds_it() -> None:
    # the finder is what the order tests stand on: it must find a changeset that is here, wherever its file is named
    assert _position(_INDEX_CHANGESET) is not None
    assert _position("no-such-changeset-prefix-p38") is None


@pytest.mark.xfail(
    strict=True,
    reason="D2's vectors-065 (catalog_collections_owner_read) is not on develop yet; remove this mark when it lands, "
    "RDR-229 P3.8 nexus-p2m1x.28",
)
def test_the_owner_read_policy_changeset_is_included() -> None:
    assert _position(_OWNER_READ_PREFIX) is not None, f"no included file holds a {_OWNER_READ_PREFIX}* changeset"


def test_when_the_owner_read_policy_is_included_the_live_tuple_index_follows_it() -> None:
    policy = _position(_OWNER_READ_PREFIX)
    if policy is None:
        return  # absent: test_the_owner_read_policy_changeset_is_included is the strict xfail that says so
    index = _position(_INDEX_CHANGESET)
    assert index is not None
    assert index > policy, (
        f"{_INDEX_CHANGESET} must be included after the {_OWNER_READ_PREFIX}* changeset: the counter reads every tenant "
        "through D2's policy"
    )


@pytest.mark.xfail(
    strict=True,
    reason="marlowe's catalog-051 (catalog-044's AFTER trigger skips an owner rewrite onto a held live tuple, with a NOTICE) "
    "is not on develop yet; remove this mark when it lands, RDR-229 P3.8 nexus-p2m1x.28",
)
def test_the_catalog_044_trigger_skip_changeset_is_included() -> None:
    assert _position(_TRIGGER_SKIP_PREFIX) is not None, f"no included file holds a {_TRIGGER_SKIP_PREFIX}* changeset"


def test_when_the_catalog_044_trigger_skip_is_included_the_live_tuple_index_follows_it() -> None:
    skip = _position(_TRIGGER_SKIP_PREFIX)
    if skip is None:
        return  # absent: test_the_catalog_044_trigger_skip_changeset_is_included is the strict xfail that says so
    index = _position(_INDEX_CHANGESET)
    assert index is not None
    assert index > skip, (
        f"{_INDEX_CHANGESET} must be included after the {_TRIGGER_SKIP_PREFIX}* changeset: with the index present and "
        "the trigger unchanged, an owner rewrite onto a held tuple is an unnamed unique violation inside a document write"
    )
