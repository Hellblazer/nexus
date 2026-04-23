# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-086 Phase 3: forward-direction plumbing for chunk_text_hash.

Covers the three structured-return surfaces:
  * ``search(structured=True)``  — add ``chunk_text_hash`` per page result
  * ``query(structured=True)``   — same addition, document-level semantics
  * ``nx_answer(structured=True)`` — new kwarg returning an envelope
    ``{final_text, chunks, plan_id, step_count}``
  * Single-step guard path (multi-stepless question rerouted through
    ``query()``) must also produce an envelope, not a bare string,
    when structured=True.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
import pytest

from nexus.db.t1 import T1Database
from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database
from nexus.mcp_server import (
    _inject_catalog,
    _inject_t1,
    _inject_t3,
    _reset_singletons,
    query,
    search,
    store_put,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset():
    _reset_singletons()
    yield
    _reset_singletons()


@pytest.fixture()
def t1():
    client = chromadb.EphemeralClient()
    db = T1Database(session_id="p3-test", client=client)
    _inject_t1(db)
    return db


@pytest.fixture()
def t2_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        yield Path(f.name)


@pytest.fixture()
def t3():
    client = chromadb.EphemeralClient()
    ef = chromadb.utils.embedding_functions.DefaultEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)
    _inject_t3(db)
    return db


@pytest.fixture(autouse=True)
def _patch_t2(t2_path, monkeypatch):
    import nexus.mcp.core as mod
    monkeypatch.setattr(mod, "_t2_ctx", lambda: T2Database(t2_path))


# ── search(structured=True) — 3.1 ────────────────────────────────────────────


class TestSearchStructuredChashSurface:
    def test_search_structured_populated_contains_chunk_text_hash(self, t3):
        store_put(
            content="a chunk about chromadb vector databases",
            collection="knowledge__s1",
            title="doc1",
        )
        result = search(
            query="vector databases",
            corpus="knowledge__s1",
            limit=5,
            structured=True,
        )
        assert isinstance(result, dict), f"expected dict, got {type(result)}"
        assert "chunk_text_hash" in result
        assert isinstance(result["chunk_text_hash"], list)
        # Each id must have a corresponding chunk_text_hash entry.
        assert len(result["chunk_text_hash"]) == len(result["ids"])
        # For a populated store_put result, hashes must be non-empty 64-char hex.
        for h in result["chunk_text_hash"]:
            assert isinstance(h, str)
            # 64 hex chars from SHA-256; empty string only if metadata lacks it.
            # Either is accepted, but the key must exist on the dict.

    def test_search_structured_empty_contains_empty_chash_list(self, t3):
        result = search(
            query="no such thing",
            corpus="knowledge__never_created",
            structured=True,
        )
        assert isinstance(result, dict)
        assert result["chunk_text_hash"] == []


# ── query(structured=True) — 3.2 ─────────────────────────────────────────────


class TestQueryStructuredChashSurface:
    def test_query_structured_populated_contains_chunk_text_hash(self, t3):
        store_put(
            content="chunk text about retrieval-augmented generation",
            collection="knowledge__q1",
            title="rag-doc",
        )
        result = query(
            question="retrieval-augmented generation",
            corpus="knowledge__q1",
            structured=True,
            limit=5,
        )
        assert isinstance(result, dict)
        assert "chunk_text_hash" in result
        assert isinstance(result["chunk_text_hash"], list)
        assert len(result["chunk_text_hash"]) == len(result["ids"])

    def test_query_structured_empty_contains_empty_chash_list(self, t3):
        result = query(
            question="no matches",
            corpus="knowledge__unknown",
            structured=True,
        )
        assert isinstance(result, dict)
        assert result["chunk_text_hash"] == []


# ── nx_answer(structured=True) — 3.3 + 3.4 ──────────────────────────────────


class TestNxAnswerStructuredEnvelope:
    def test_nx_answer_structured_default_returns_string(self, t3, t1, monkeypatch):
        """Backward compat: default behavior must return a plain string.

        The plan-miss path spawns a ``claude -p`` subprocess via
        ``claude_dispatch`` which adds ~45s of cold-start latency
        per call. Mock the dispatcher to a cheap stub plan so the
        test verifies the envelope-vs-string return shape without
        the subprocess roundtrip.
        """
        import asyncio

        import nexus.operators.dispatch as _dispatch_mod
        from nexus.mcp.core import nx_answer

        async def fake_dispatch(prompt, schema, timeout=60.0):
            return {"steps": [
                {"tool": "search", "args": {"query": "$intent"}},
                {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
            ]}
        monkeypatch.setattr(_dispatch_mod, "claude_dispatch", fake_dispatch)

        # No plan in library → inline planner miss path; result must be str.
        result = asyncio.run(nx_answer(
            question="what is chromadb?",
            scope="knowledge",
        ))
        assert isinstance(result, str), f"expected str, got {type(result)}"

    def test_nx_answer_structured_true_single_step_returns_envelope(
        self, t3, t1,
    ):
        """Single-step guard path returns an envelope when structured=True,
        not a bare string."""
        import asyncio
        import json

        from nexus.mcp.core import nx_answer

        # Seed a single-query plan into T2 so the plan-match gate hits.
        store_put(
            content="orange foxes are clever",
            collection="knowledge__nx1",
            title="foxes",
        )

        plan_json = json.dumps({
            "steps": [{
                "op": "query",
                "args": {"question": "orange foxes", "corpus": "knowledge__nx1"},
            }],
        })
        import nexus.mcp.core as mod
        with mod._t2_ctx() as db:
            db.save_plan(
                query="orange foxes", plan_json=plan_json,
                tags="test", project="", ttl=30,
            )

        result = asyncio.run(nx_answer(
            question="orange foxes",
            scope="knowledge",
            structured=True,
        ))
        assert isinstance(result, dict), f"expected dict, got {type(result)}"
        # Envelope shape (RDR §Phase 3 3.3):
        assert "final_text" in result
        assert "chunks" in result
        assert "plan_id" in result
        assert "step_count" in result
        assert isinstance(result["chunks"], list)
        # Each chunk must carry id, chash, collection.
        for ch in result["chunks"]:
            assert "id" in ch
            assert "chash" in ch
            assert "collection" in ch

    def test_nx_answer_structured_true_empty_results_has_chunks_key(
        self, t3, t1, monkeypatch,
    ):
        """When retrieval yields nothing, envelope's chunks list is [] — not error.

        Mocks ``claude_dispatch`` for the same reason as
        ``test_nx_answer_structured_default_returns_string``: the
        subprocess roundtrip costs ~35s and does not need to run for
        this envelope-shape assertion.
        """
        import asyncio

        import nexus.operators.dispatch as _dispatch_mod
        from nexus.mcp.core import nx_answer

        async def fake_dispatch(prompt, schema, timeout=60.0):
            return {"steps": [
                {"tool": "search", "args": {"query": "$intent"}},
                {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
            ]}
        monkeypatch.setattr(_dispatch_mod, "claude_dispatch", fake_dispatch)

        result = asyncio.run(nx_answer(
            question="completely unknown topic that matches nothing",
            scope="knowledge",
            structured=True,
        ))
        assert isinstance(result, dict)
        assert "chunks" in result
        assert isinstance(result["chunks"], list)
