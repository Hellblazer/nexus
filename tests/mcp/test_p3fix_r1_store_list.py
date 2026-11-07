# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 fix round r1 (nexus-rfvyy.8), item 1: the ``store_list`` flat view fails closed.

Review r1 B1 / critic S3: ``HttpVectorClient.list_store`` dropped the engine's ``sensitives`` list, and when the
catalog could not be read (``live_owner_entries`` -> None) the flat view fell back to each row's own title. The docs
view already treated an unreadable catalog as all-sensitive; the flat view now does too, and the engine's flag
reaches each entry.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

import nexus.db.http_vector_client as hvc
from nexus.mcp.core import store_list
from nexus.mcp_server import _inject_t3, _reset_singletons

CANARY = f"CANARY-p3fix-sl-{uuid4()}"
SECRET_TITLE = f"{CANARY} board minutes"
COLLECTION = "knowledge__test"
CHASH_S = "a" * 64
CHASH_C = "b" * 64


@pytest.fixture(autouse=True)
def _world(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "nexus-config"
    cfg_dir.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("NX_SENSITIVITY_POSTURE", raising=False)
    _reset_singletons()
    yield
    _reset_singletons()


def _t3(rows: list[dict]) -> MagicMock:
    t3 = MagicMock()
    t3.list_collections.return_value = [
        {"name": COLLECTION, "count": len(rows), "content_type": "knowledge", "owner_id": "1-1",
         "embedding_model": "test-model", "lifecycle_state": "live"},
    ]
    t3.collection_info.return_value = {"count": len(rows)}
    t3.list_store.return_value = rows
    _inject_t3(t3)
    return t3


def _rows() -> list[dict]:
    return [
        {"id": CHASH_C, "title": "Gardening", "indexed_at": "2026-11-01"},
        {"id": CHASH_S, "title": SECRET_TITLE, "indexed_at": "2026-11-01"},
    ]


def test_flat_view_withholds_every_row_when_the_catalog_cannot_be_read():
    _t3(_rows())
    with patch("nexus.catalog.store_hook.live_owner_entries", return_value=None):
        out = store_list(collection=COLLECTION)
    assert CANARY not in out and "Gardening" not in out
    assert out.count("content withheld") == 2
    assert "2 sensitive hits withheld" in out


def test_flat_view_with_a_readable_catalog_and_no_flag_is_unchanged():
    _t3(_rows()[:1])
    owner = SimpleNamespace(tumbler="1.3.1", title="Gardening", sensitive=False, meta={}, indexed_at="2026-11-01",
                            alias_of="")
    with patch("nexus.catalog.store_hook.live_owner_entries", return_value={CHASH_C: [owner]}):
        out = store_list(collection=COLLECTION)
    assert "Gardening" in out and "withheld" not in out


def test_flat_view_honours_the_engines_row_flag_when_the_catalog_says_nothing():
    rows = [{"id": CHASH_S, "title": SECRET_TITLE, "indexed_at": "2026-11-01", "sensitive": True},
            {"id": CHASH_C, "title": "Gardening", "indexed_at": "2026-11-01", "sensitive": False}]
    _t3(rows)
    owner = SimpleNamespace(tumbler="1.3.1", title="Gardening", sensitive=False, meta={}, indexed_at="2026-11-01",
                            alias_of="")
    with patch("nexus.catalog.store_hook.live_owner_entries", return_value={CHASH_C: [owner], CHASH_S: []}):
        out = store_list(collection=COLLECTION)
    assert CANARY not in out and "Gardening" in out
    assert "1 sensitive hit withheld" in out


def test_list_store_carries_the_engines_sensitives_into_each_entry(monkeypatch):
    monkeypatch.setattr(hvc.HttpVectorClient, "__init__", lambda self: None)
    client = hvc.HttpVectorClient()
    client._tenant = "t"
    env = {"ids": ["a", "b", "c"], "metadatas": [{"title": "A", "sensitive": True}, {"title": "B"}, None],
           "sensitives": [False, True]}
    monkeypatch.setattr(hvc, "_post", lambda path, body, tenant=None, **kw: dict(env))
    rows = client.list_store("c")
    assert [r.get("sensitive") for r in rows] == [False, True, None], (
        "the engine's flag is set after the metadata, so a chunk row cannot speak for it; a row past the list gets none"
    )
