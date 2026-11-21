# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-229 P2.5 (nexus-p2m1x.10), client half: the one helper the twelve
``_BYPASS_SCHEMA_PREFIXES`` sites share. A name with no row keeps today's prefix
outcome; a row's ``lifecycle_state`` decides when there is one."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.db.rows.lifecycle import (
    QUARANTINE_NAME_PREFIX,
    TAXONOMY_BYPASS_PREFIXES,
    bypasses_canonical_schema,
    is_quarantine_collection,
    registry_rows_by_name,
)
from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES


@pytest.mark.parametrize(
    ("name", "row", "expected"),
    [
        # no row: the prefix decides, as before
        ("quarantine-code__1-1__bge-base-en-v15-768__v1", None, True),
        ("code__1-1__bge-base-en-v15-768__v1", None, False),
        ("quarantine-x", {}, True),
        # a row with no lifecycle_state is no row
        ("quarantine-x", {"name": "quarantine-x"}, True),
        ("quarantine-x", {"lifecycle_state": None}, True),
        ("quarantine-x", {"lifecycle_state": ""}, True),
        # the row's state wins over the name
        ("any-label", {"lifecycle_state": "quarantine"}, True),
        ("quarantine-x", {"lifecycle_state": "live"}, False),
        ("quarantine-x", {"lifecycle_state": "dormant"}, False),
        ("code__1-1__bge-base-en-v15-768__v1", {"lifecycle_state": "live"}, False),
        # a name-only listing entry (a test double) carries no state
        ("quarantine-x", "quarantine-x", True),
    ],
)
def test_is_quarantine_collection(name, row, expected):
    assert is_quarantine_collection(name, row) is expected


def test_taxonomy_half_stays_on_the_name_until_p29():
    assert bypasses_canonical_schema("taxonomy__centroids", None) is True
    assert bypasses_canonical_schema("taxonomy__centroids", {"lifecycle_state": "live"}) is True
    assert bypasses_canonical_schema("code__1-1__bge-base-en-v15-768__v1", {"lifecycle_state": "live"}) is False


def test_bypass_follows_the_quarantine_row():
    assert bypasses_canonical_schema("any-label", {"lifecycle_state": "quarantine"}) is True
    assert bypasses_canonical_schema("quarantine-x", {"lifecycle_state": "live"}) is False
    assert bypasses_canonical_schema("quarantine-x") is True


def test_the_two_halves_make_up_the_pinned_prefix_tuple():
    assert _BYPASS_SCHEMA_PREFIXES == (*TAXONOMY_BYPASS_PREFIXES, QUARANTINE_NAME_PREFIX)


def test_registry_rows_by_name_is_fail_soft():
    class Cat:
        def list_collections(self):
            return [{"name": "a", "lifecycle_state": "live"}, {"lifecycle_state": "x"}]

    assert registry_rows_by_name(Cat()) == {"a": {"name": "a", "lifecycle_state": "live"}}

    class Down:
        def list_collections(self):
            raise RuntimeError("down")

    assert registry_rows_by_name(Down()) == {}
    # a handle whose list is not a list (a MagicMock double) is no registry either
    assert registry_rows_by_name(MagicMock()) == {}
