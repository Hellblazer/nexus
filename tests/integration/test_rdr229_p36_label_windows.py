# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.6 (nexus-p2m1x.26), the Windows label test (Cross-Cutting, Windows): one label that holds a space,
``%``, ``:`` and a non-ASCII character is registered, listed, searched and renamed against the real engine.

The label is not a key, an identifier or a file name any more (item 7), so none of those four characters may break
a path: not the URL the client builds, not the engine's route, not a temp file, not a PostgreSQL identifier.
``:`` is the character a Windows file name cannot hold and ``%`` the one a URL decodes, which is why they are here.
On POSIX this proves the substrate takes the label end to end; the same file runs on native Windows, by hand,
before the push: ``uv run scripts/windows_hand_test.py --session <name> -- tests/integration/test_rdr229_p36_label_windows.py``
(the rule is the push filter's, AGENTS.md rule 14; the engine half of this test is
``CollectionLabelRuleRouteTest.aLabelHoldingASpacePercentColonAndNonAscii_registersListsAndRenames``).
"""
from __future__ import annotations

import hashlib

import pytest

from nexus.catalog.factory import make_catalog_reader
from nexus.collection_rename import rename_collection_data_plane
from nexus.db.http_vector_client import HttpVectorClient
from tests._catalog_fixture_ops import give_chunks_a_live_owner
from tests._chunk_seed import seed_chunks_direct

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]

_LABEL = "notes 50%: café"
_RENAMED = "notes 60%: café"
_TEXT = "the label holds a space, a percent sign, a colon and an accented letter"


def _names(reader) -> set[str]:
    return {c.get("name") for c in reader.list_collections()}


def _hits(client: HttpVectorClient, label: str) -> list[dict]:
    return client.search("accented letter", [label], n_results=5, sensitivity="include")


def test_a_label_with_a_space_percent_colon_and_non_ascii_registers_lists_searches_and_renames(
    t2_service_env,
) -> None:
    client = HttpVectorClient(tenant=t2_service_env)
    reader = make_catalog_reader()
    assert reader is not None
    chash = hashlib.sha256(_TEXT.encode()).hexdigest()

    # registered: the label is taken as it is, with no ChromaDB-shape refusal on the way
    seed_chunks_direct(_LABEL, ids=[chash], documents=[_TEXT], embed=True)
    give_chunks_a_live_owner(_LABEL, [chash], content_type="knowledge", owner_name="label-windows")
    # listed
    assert _LABEL in _names(reader)
    # searched
    assert any(_TEXT in str(hit.get("content", hit)) for hit in _hits(client, _LABEL)), _hits(client, _LABEL)
    # renamed, to another label with the same four kinds of character
    rename_collection_data_plane(_LABEL, _RENAMED, t3_db=client, catalog=reader)
    names = _names(reader)
    assert _RENAMED in names
    live = {c.get("name") for c in reader.list_collections() if not c.get("superseded_by")}
    assert _LABEL not in live and _RENAMED in live, live
    assert any(_TEXT in str(hit.get("content", hit)) for hit in _hits(client, _RENAMED))
