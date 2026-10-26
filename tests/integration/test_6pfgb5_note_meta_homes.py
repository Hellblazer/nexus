# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.5 against the real engine: a note keeps its own tags, category, ttl_days and
source_agent in its catalog entry's ``meta``.

Two notes with identical text share one chunk row (RDR-108), and that row carries the last writer's
keys. Each catalog entry must hold its own four values after both puts. The storage-free twin, with
the retraction and restore cases, is ``tests/test_6pfgb5_note_meta_homes.py``.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.aspect_readers import uri_for
from nexus.catalog.factory import make_catalog_reader
from nexus.corpus import t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient
from nexus.mcp.core import store_put

pytestmark = pytest.mark.integration

_KEYS = ("tags", "category", "ttl_days", "source_agent")


def _entry_keys(collection: str, title: str) -> dict:
    reader = make_catalog_reader()
    assert reader is not None
    doc = reader.by_source_uri(uri_for(collection, title))
    assert doc is not None, f"{title!r} was never registered"
    return {k: (doc.meta or {}).get(k) for k in _KEYS}


def test_two_notes_with_identical_text_each_catalog_entry_keeps_its_own_keys(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "6pfgb5-shared"
    body = "6pfgb5 identical body stored under two titles with different keys"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content=body, collection=subject, title="keys-A", tags="alpha,one",
                  category="design", ttl="7d", agent="agent-a")
        store_put(content=body, collection=subject, title="keys-B", tags="beta,two",
                  category="finding", ttl="30d", agent="agent-b")

    collection = t3_collection_name(subject, t3=client)
    assert _entry_keys(collection, "keys-A") == {
        "tags": "alpha,one", "category": "design", "ttl_days": 7, "source_agent": "agent-a"}
    assert _entry_keys(collection, "keys-B") == {
        "tags": "beta,two", "category": "finding", "ttl_days": 30, "source_agent": "agent-b"}


def test_a_reput_updates_the_entry_and_a_permanent_reput_clears_the_ttl(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "6pfgb5-reput"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content="6pfgb5 reput body one", collection=subject, title="reput", tags="old",
                  category="design", ttl="7d", agent="agent-a")
        store_put(content="6pfgb5 reput body two", collection=subject, title="reput", tags="new",
                  category="design", agent="agent-a")

    collection = t3_collection_name(subject, t3=client)
    assert _entry_keys(collection, "reput") == {
        "tags": "new", "category": "design", "ttl_days": 0, "source_agent": "agent-a"}
