# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #554: PlanSessionCache.query must use query_embeddings, not query_texts.

Pre-fix the query went through chromadb 1.5.9's text-side path which
crashed in convert_np_embeddings_to_list when the embedding function
returned plain Python lists. This test verifies the call goes through
query_embeddings and returns results without crashing.
"""
from __future__ import annotations

import chromadb
import pytest

from nexus.plans.session_cache import PlanSessionCache


def test_query_uses_query_embeddings_not_query_texts() -> None:
    """GH #554: pre-fix the query routed via query_texts and crashed
    inside chromadb 1.5.9. Post-fix it pre-embeds the intent and
    passes query_embeddings, bypassing the text-side adapter.

    Verifies: a query against this session returns [] cleanly when
    no rows match (no exception caught + warning, which was the
    pre-fix symptom).
    """
    # MEMORY: chromadb EphemeralClient shares process state across
    # test instances; scope the where-filter to a unique session_id
    # so other tests' rows don't leak into this query.
    cache = PlanSessionCache(
        client=chromadb.EphemeralClient(),
        session_id="test-554-empty-session",
    )

    out = cache.query("how does X work", n=5)
    assert out == []


def test_query_does_not_warn_on_chromadb_text_path(caplog) -> None:
    """GH #554 belt-and-suspenders: ensure the warning event the
    pre-fix path emitted (``plan_session_cache_query_failed``) does
    NOT fire on a plain query against an empty collection.

    Pre-fix, every ``query()`` call emitted this warning because
    chromadb 1.5.9's text-side adapter crashed on plain-list
    embedding return values. Post-fix, the adapter is bypassed and
    the warning never fires.
    """
    import logging

    cache = PlanSessionCache(
        client=chromadb.EphemeralClient(),
        session_id="test-554-no-warn-session",
    )

    with caplog.at_level(logging.WARNING):
        out = cache.query("any prose query", n=5)

    assert out == []
    failure_records = [
        r for r in caplog.records
        if "plan_session_cache_query_failed" in r.getMessage()
    ]
    assert not failure_records, (
        f"unexpected warning emitted: {[r.getMessage() for r in failure_records]}"
    )
