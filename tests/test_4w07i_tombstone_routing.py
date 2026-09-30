# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4w07i: a rename's retired name is never a search-routing target.

The engine keeps the old name's catalog row as a tombstone (superseded_by =
the new name) with lifecycle_state still 'live', so the routing view treated
it as live once it held chunks. The client half: a stats row that names a
successor is not routing-eligible. The engine half (engine-service-v0.1.139)
sends superseded_by on the stats row and leaves tombstones out of its live
view; against an older engine the client check is a no-op.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.collection_errors import SupersededCollectionWriteError
from nexus.db.http_vector_client import HttpVectorClient, is_live_collection_row, live_collection_rows

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


@pytest.mark.integration
def test_a_retired_name_holding_chunks_is_not_routed_to(t2_service_env, tmp_path):
    """Engine substrate end to end: index A, rename A to B, write to A again,
    and the routing listing leaves A out while the inventory still names it
    with its successor.

    A current client refuses the stale write (nexus-wwuzp revalidates an aged
    registration with a read), so that refusal is asserted first. The engine
    still accepts a write to the retired name from a client that predates
    wwuzp, which is how a retired name comes to hold chunks; that client is
    modelled by skipping the revalidation read for the second write."""
    from nexus.doc_indexer import index_markdown  # noqa: PLC0415 — test-local import

    client = HttpVectorClient(tenant=t2_service_env)
    first = tmp_path / "first.md"
    first.write_text("# first\n\n" + " ".join(f"4w07i first sentence {j}." for j in range(60)))
    assert index_markdown(first, corpus="4w07i", t3=client, collection_name=_OLD)
    with patch("nexus.commands.collection._t3", return_value=client):
        renamed = CliRunner().invoke(main, ["collection", "rename", _OLD, _NEW])
    assert renamed.exit_code == 0, renamed.output

    stale = tmp_path / "stale.md"
    stale.write_text("# stale\n\n" + " ".join(f"4w07i stale sentence {j}." for j in range(60)))
    with pytest.raises(SupersededCollectionWriteError):
        index_markdown(stale, corpus="4w07i", t3=client, collection_name=_OLD)
    with patch("nexus.corpus._revalidate_cached_registration", return_value=True):
        assert index_markdown(stale, corpus="4w07i", t3=client, collection_name=_OLD)

    inventory = {r["name"]: r for r in client.list_collections()}
    assert _OLD in inventory, "the retired name must hold chunks for this test to mean anything"
    assert inventory[_OLD].get("superseded_by") == _NEW

    routed = {r["name"] for r in live_collection_rows(client)}
    assert _NEW in routed
    assert _OLD not in routed
