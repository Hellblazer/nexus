# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.13 against the real engine: ``store_get`` and ``store_list`` show each owning note's own
tags, TTL and index date although another note with the same text wrote the shared chunk row last.

Two notes with identical text share one chunk row (RDR-108); the row's ``tags``, ``ttl_days`` and
``indexed_at`` are the last writer's (here B's). The catalog entries hold each note's own values
(nexus-6pfgb.5). The stub-reader twin of the rules is ``tests/test_6pfgb13_note_display.py``.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.corpus import t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient
from nexus.mcp.core import store_get, store_list, store_put

pytestmark = pytest.mark.integration

_BODY = "6pfgb13 identical text under two notes with different tags and ttl"


def test_each_note_shows_its_own_tags_and_expiry_when_the_other_wrote_last(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "6pfgb13-display"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content=_BODY, collection=subject, title="note-a", tags="alpha", ttl="7d")
        store_put(content=_BODY, collection=subject, title="note-b", tags="beta", ttl="30d")  # B writes the row last
        collection = t3_collection_name(subject, t3=client)

        a = store_get(doc_id="note-a", collection=collection)
        b = store_get(doc_id="note-b", collection=collection)
        listing = store_list(collection=collection)

    assert "Tags:       alpha" in a and "beta" not in a, a
    assert "Tags:       beta" in b and "alpha" not in b, b
    a_ttl = next(ln for ln in a.splitlines() if ln.startswith("TTL:"))
    b_ttl = next(ln for ln in b.splitlines() if ln.startswith("TTL:"))
    assert a_ttl != b_ttl, f"7 days and 30 days from two puts must not read alike: {a_ttl!r} vs {b_ttl!r}"
    # the one shared chunk lists once, naming both owners, with the tags both record and no expiry it
    # would have to take from B's row
    row = next(ln for ln in listing.splitlines() if "note-a" in ln)
    assert "note-b" in row and "[alpha, beta]" in row, listing
