# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4w07i: a rename's retired name is never a search-routing target.

The engine keeps the old name's catalog row as a tombstone (superseded_by =
the new name) with lifecycle_state still 'live', so the routing view treated
it as live once it held chunks. The client half: a stats row that names a
successor is not routing-eligible. The engine half (engine-service-v0.1.139)
starts sending superseded_by on the stats row and adds the substrate
consequence test; until then an engine omits the key and this check is a
no-op.
"""
from __future__ import annotations

from nexus.db.http_vector_client import is_live_collection_row

_OLD = "docs__4w07i-old__bge-base-en-v15-768__v1"
_NEW = "docs__4w07i-new__bge-base-en-v15-768__v1"


def test_a_row_naming_a_successor_is_not_routing_eligible():
    assert not is_live_collection_row({"name": _OLD, "lifecycle_state": "live", "superseded_by": _NEW})


def test_rows_without_the_key_behave_as_before():
    """v0.1.138 and older never send superseded_by: nothing changes for them."""
    assert is_live_collection_row({"name": _NEW, "lifecycle_state": "live"})
    assert is_live_collection_row({"name": _NEW, "lifecycle_state": "live", "superseded_by": ""})
    assert not is_live_collection_row({"name": _NEW, "lifecycle_state": "quarantine"})
    assert is_live_collection_row({"name": _NEW})
