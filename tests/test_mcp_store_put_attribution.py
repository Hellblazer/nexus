# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-4ftd7: MCP ``store_put`` must accept ``agent``/``session`` actor
attribution params, mirroring ``memory_put``'s shape, and plumb them into
the T3 chunk's ``source_agent``/``session_id`` metadata.

Pre-fix: ``store_put`` had no actor parameter at all and never passed
``source_agent``/``session_id`` to ``T3Database.put``, so every MCP-written
chunk landed with T3Database.put's own bare ``""`` default (NOT the
``make_chunk_metadata`` factory's "nexus-indexer" default, which
``T3Database.put`` always overrides with its own explicit kwarg) — either
way, a value that can never satisfy ``search_engine._flag_contradictions``'s
``agent_a and agent_b and agent_a != agent_b`` precondition (RDR-057
Phase 3a), making it dead code on the entire MCP-written population.

Mirrors ``tests/test_mcp_store_put_doc_id.py``'s fixture shape
(``inject_local_t3`` / ``catalog_env`` / real chunk-metadata inspection).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from tests._catalog_fixture_ops import ActiveCatalog
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from nexus.db.t3 import T3Database
from tests.conftest import make_vector_test_client


@pytest.fixture
def local_t3() -> T3Database:
    return T3Database(
        _client=make_vector_test_client(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    return catalog_dir


def _no_op(*args, **kwargs):
    pass


def _seed_for_store_put(content: str, collection: str = "knowledge") -> None:
    """See ``tests/test_mcp_store_put_doc_id.py``'s helper of the same
    name — pre-seeds the real ``nexus.chunks`` FK row the manifest write
    requires."""
    import hashlib

    from nexus.corpus import t3_collection_name
    from tests._catalog_fixture_ops import seed_manifest_chunks

    col_name = t3_collection_name(collection)
    chash = hashlib.sha256(content.encode()).hexdigest()
    seed_manifest_chunks(col_name, [chash])


@pytest.fixture
def inject_local_t3(local_t3: T3Database):
    from nexus.mcp_infra import inject_t3
    inject_t3(local_t3)
    yield local_t3
    inject_t3(None)


def _stored_metadata(local_t3: T3Database, stored_col_name: str, title: str) -> dict:
    stored_col = local_t3._client.get_collection(stored_col_name)
    chunk_result = stored_col.get(include=["metadatas"])
    matching = [m for m in chunk_result["metadatas"] if m.get("title") == title]
    assert matching, f"expected a chunk with title={title!r}"
    return matching[0]


def _store(
    *,
    content: str,
    title: str,
    agent: str = "",
    session: str = "",
    collection: str = "knowledge",
) -> str:
    from nexus.mcp.core import store_put

    return store_put(
        content=content,
        collection=collection,
        title=title,
        agent=agent,
        session=session,
    )


def _run_store_put(local_t3: T3Database, **kwargs) -> str:
    with patch("nexus.mcp.core._get_t3", return_value=local_t3), \
         patch("nexus.mcp.core._hooks.fire_single", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_batch", side_effect=_no_op), \
         patch("nexus.mcp.core._hooks.fire_document", side_effect=_no_op), \
         patch("nexus.mcp.core._catalog_auto_link", return_value=0):
        return _store(**kwargs)


# ── Explicit agent/session land in T3 chunk metadata ────────────────────────


def test_explicit_agent_lands_in_chunk_metadata(
    inject_local_t3: T3Database, catalog_env: Path,
) -> None:
    local_t3 = inject_local_t3
    content = "# nexus-4ftd7 explicit agent\n\nBody."
    _seed_for_store_put(content)
    result = _run_store_put(
        local_t3, content=content, title="attrib-explicit-agent", agent="developer",
    )
    assert "Stored" in result, result
    stored_col_name = result.split("->")[-1].strip()
    meta = _stored_metadata(local_t3, stored_col_name, "attrib-explicit-agent")
    assert meta.get("source_agent") == "developer"


def test_explicit_session_lands_in_chunk_metadata(
    inject_local_t3: T3Database, catalog_env: Path,
) -> None:
    local_t3 = inject_local_t3
    content = "# nexus-4ftd7 explicit session\n\nBody."
    _seed_for_store_put(content)
    result = _run_store_put(
        local_t3, content=content, title="attrib-explicit-session", session="sess-xyz",
    )
    assert "Stored" in result, result
    stored_col_name = result.split("->")[-1].strip()
    meta = _stored_metadata(local_t3, stored_col_name, "attrib-explicit-session")
    assert meta.get("session_id") == "sess-xyz"


def test_agent_falls_back_to_nx_agent_env(
    inject_local_t3: T3Database, catalog_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NX_AGENT", "architect-planner")
    local_t3 = inject_local_t3
    content = "# nexus-4ftd7 env agent\n\nBody."
    _seed_for_store_put(content)
    result = _run_store_put(local_t3, content=content, title="attrib-env-agent")
    assert "Stored" in result, result
    stored_col_name = result.split("->")[-1].strip()
    meta = _stored_metadata(local_t3, stored_col_name, "attrib-env-agent")
    assert meta.get("source_agent") == "architect-planner"


def test_session_falls_back_to_nx_session_id_env(
    inject_local_t3: T3Database, catalog_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NX_SESSION_ID", "session-from-env")
    local_t3 = inject_local_t3
    content = "# nexus-4ftd7 env session\n\nBody."
    _seed_for_store_put(content)
    result = _run_store_put(local_t3, content=content, title="attrib-env-session")
    assert "Stored" in result, result
    stored_col_name = result.split("->")[-1].strip()
    meta = _stored_metadata(local_t3, stored_col_name, "attrib-env-session")
    assert meta.get("session_id") == "session-from-env"


def test_explicit_agent_overrides_nx_agent_env(
    inject_local_t3: T3Database, catalog_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicitly-passed agent always wins over ambient NX_AGENT."""
    monkeypatch.setenv("NX_AGENT", "env-agent")
    local_t3 = inject_local_t3
    content = "# nexus-4ftd7 override\n\nBody."
    _seed_for_store_put(content)
    result = _run_store_put(
        local_t3, content=content, title="attrib-override", agent="explicit-agent",
    )
    assert "Stored" in result, result
    stored_col_name = result.split("->")[-1].strip()
    meta = _stored_metadata(local_t3, stored_col_name, "attrib-override")
    assert meta.get("source_agent") == "explicit-agent"


def test_no_agent_and_no_env_gets_distinguishable_mcp_marker(
    inject_local_t3: T3Database, catalog_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-4ftd7 core fix: an unmarked MCP write must land a value that
    distinguishes it from BOTH the empty-string pre-fix default AND the
    T3 indexer path's "nexus-indexer" constant — never either one."""
    monkeypatch.delenv("NX_AGENT", raising=False)
    local_t3 = inject_local_t3
    content = "# nexus-4ftd7 no agent\n\nBody."
    _seed_for_store_put(content)
    result = _run_store_put(local_t3, content=content, title="attrib-no-agent")
    assert "Stored" in result, result
    stored_col_name = result.split("->")[-1].strip()
    meta = _stored_metadata(local_t3, stored_col_name, "attrib-no-agent")
    source_agent = meta.get("source_agent")
    assert source_agent, "MCP write must not land an empty source_agent"
    assert source_agent != "nexus-indexer", (
        "an unmarked MCP write must not collapse onto the indexer's "
        "constant — that is exactly what makes _flag_contradictions dead"
    )


# ── _flag_contradictions precondition now satisfiable on MCP writers ───────


def test_two_mcp_writers_with_different_agents_satisfy_contradiction_precondition(
    inject_local_t3: T3Database, catalog_env: Path,
) -> None:
    """RF-10 motivating case (nexus-4ftd7): two MCP-authored chunks with
    different explicit agents must satisfy
    ``search_engine._flag_contradictions``'s ``agent_a != agent_b``
    precondition — pre-fix this could never hold since every MCP write
    shared the same (empty) source_agent."""
    from nexus.search_engine import _flag_contradictions
    from nexus.types import SearchResult

    local_t3 = inject_local_t3
    content_a = "# Design note A\n\ncaching uses Redis."
    content_b = "# Design note B\n\ncaching uses Memcached."
    _seed_for_store_put(content_a)
    _seed_for_store_put(content_b)

    result_a = _run_store_put(
        local_t3, content=content_a, title="rf10-agent-a", agent="agent-alpha",
    )
    result_b = _run_store_put(
        local_t3, content=content_b, title="rf10-agent-b", agent="agent-beta",
    )
    assert "Stored" in result_a and "Stored" in result_b

    col_a = result_a.split("->")[-1].strip()
    col_b = result_b.split("->")[-1].strip()
    meta_a = _stored_metadata(local_t3, col_a, "rf10-agent-a")
    meta_b = _stored_metadata(local_t3, col_b, "rf10-agent-b")

    assert meta_a["source_agent"] != meta_b["source_agent"]
    assert meta_a["source_agent"] and meta_b["source_agent"]

    # Drive the actual precondition check with near-identical embeddings
    # (as if the two notes were semantically close) — must flag.
    embs = np.array([[1.0, 0.0, 0.0], [0.99, 0.01, 0.0]], dtype=np.float32)
    results = [
        SearchResult(id="a", content=content_a, distance=0.1, collection=col_a, metadata=meta_a),
        SearchResult(id="b", content=content_b, distance=0.1, collection=col_a, metadata=meta_b),
    ]
    out = _flag_contradictions(results, embs)
    assert out[0].metadata.get("_contradiction_flag") is True
    assert out[1].metadata.get("_contradiction_flag") is True
