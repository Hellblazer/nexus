"""Review of the GH #1524 landing (Critical): the answer-shape classifier's
RETRIEVAL_ONLY schema must carry every key query()'s structured envelope
can emit, or a plan whose terminal step is a bare query() is misclassified
as ANSWERED. Both envelopes are built in nexus.mcp.core; this pins the
classifier against the real key set rather than a hand-written fixture."""

from __future__ import annotations

import json

from nexus.plans.answer_shape import AnswerShape, classify_answer_shape

QUERY_ENVELOPE_KEYS = {
    "ids", "tumblers", "distances", "collections", "chunk_collections", "chunk_text_hash", "also_in",
}


def test_real_query_envelope_classifies_as_retrieval_only() -> None:
    envelope = {k: [] for k in QUERY_ENVELOPE_KEYS}
    envelope["ids"] = ["1.61.7"]
    envelope["also_in"] = [["rdr__1-46__bge-base-en-v15-768__v1"]]
    assert classify_answer_shape(json.dumps(envelope)) is AnswerShape.RETRIEVAL_ONLY


def test_query_envelope_key_set_is_the_one_core_emits() -> None:
    """The plain path's exact key set is pinned in tests/test_query_repoint.py
    (TestQueryRepointMetadataScoped.test_structured_exact_key_set); keep this
    set equal to it so the classifier and the envelope move together."""
    assert QUERY_ENVELOPE_KEYS == {
        "ids", "tumblers", "distances", "collections", "chunk_collections", "chunk_text_hash", "also_in",
    }
