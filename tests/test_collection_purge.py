# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-144 nexus-prgf4: the shared collection-delete cascade.

purge_collection_cascade deletes a T3 collection and best-effort-purges all
derived state. A failed derived-state step must NOT block the physical delete,
but MUST be recorded in ``failures`` so callers can surface it (the silence
regression both P4-follow-up reviewers flagged).
"""
from __future__ import annotations

import chromadb
import pytest

from nexus.db.collection_purge import CascadeCounts, purge_collection_cascade
from nexus.db.t3 import T3Database


@pytest.fixture()
def t3() -> T3Database:
    client = chromadb.EphemeralClient()
    return T3Database(_client=client)


def _seed(t3: T3Database, name: str) -> None:
    try:
        t3._client.delete_collection(name)
    except Exception:
        pass
    col = t3._client.get_or_create_collection(name)
    col.add(ids=["x"], embeddings=[[0.1] * 384], documents=["hi"],
            metadatas=[{"source_path": "a.md"}])


def test_t2_failure_recorded_but_t3_still_deleted(
    t3: T3Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "docs__purge__minilm-l6-v2-384__v1"
    _seed(t3, name)

    def _boom(_cascade):
        raise RuntimeError("daemon down")

    monkeypatch.setattr("nexus.mcp_infra.t2_index_write", _boom)

    counts = purge_collection_cascade(t3, name)

    assert not t3.collection_exists(name)  # physical delete still happened
    assert any("taxonomy/chash cascade failed" in f for f in counts.failures)
    assert "daemon down" in " ".join(counts.failures)


def test_clean_run_has_no_failures(
    t3: T3Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "docs__purge2__minilm-l6-v2-384__v1"
    _seed(t3, name)
    # Stub the derived-state steps to succeed quietly.
    monkeypatch.setattr(
        "nexus.mcp_infra.t2_index_write",
        lambda _c: ({"topics": 0, "assignments": 0, "links": 0, "meta": 0}, 0),
    )

    counts = purge_collection_cascade(t3, name)

    assert not t3.collection_exists(name)
    # catalog/pipeline may legitimately be absent in the test env; the T2 step
    # we stubbed must not have failed.
    assert all("taxonomy/chash" not in f for f in counts.failures)
    assert isinstance(counts, CascadeCounts)
