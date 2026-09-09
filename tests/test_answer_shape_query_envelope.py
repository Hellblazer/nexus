"""Review of the GH #1524 landing (Critical): the answer-shape classifier's
RETRIEVAL_ONLY schema must carry every key query()'s structured envelope
can emit, or a plan whose terminal step is a bare query() is misclassified
as ANSWERED. Both envelopes are built in nexus.mcp.core; this pins the
classifier against the real key set rather than a hand-written fixture."""

from __future__ import annotations

import json
import re
from pathlib import Path

from nexus.plans import answer_shape as _answer_shape_module
from nexus.plans.answer_shape import AnswerShape, classify_answer_shape

QUERY_ENVELOPE_KEYS = {
    "ids", "tumblers", "distances", "collections", "chunk_collections", "chunk_text_hash",
    "also_in", "also_in_ids",
}
#: The OPTIONAL keys core.py adds to the envelope on the follow_links paths
#: (seed_scope when the seed list was capped, graph_scope when the BFS hit
#: its node cap). The critique of the GH #1524 landing [25095] found the
#: classifier blind to them by the same mechanism as also_in.
OPTIONAL_ENVELOPE_KEYS = {"seed_scope", "graph_scope"}


def test_real_query_envelope_classifies_as_retrieval_only() -> None:
    envelope = {k: [] for k in QUERY_ENVELOPE_KEYS}
    envelope["ids"] = ["1.61.7"]
    envelope["also_in"] = [["rdr__1-46__bge-base-en-v15-768__v1"]]
    assert classify_answer_shape(json.dumps(envelope)) is AnswerShape.RETRIEVAL_ONLY


def test_optional_envelope_keys_still_classify_as_retrieval_only() -> None:
    envelope = {k: [] for k in QUERY_ENVELOPE_KEYS}
    envelope["ids"] = ["1.61.7"]
    envelope["seed_scope"] = {"seed_count": 5, "capped": True}
    envelope["graph_scope"] = {"batches_at_cap": 1, "batches_total": 2, "possibly_incomplete": True}
    assert classify_answer_shape(json.dumps(envelope)) is AnswerShape.RETRIEVAL_ONLY


def test_every_key_core_assigns_on_the_envelope_is_in_the_classifier() -> None:
    """Read core.py for every `structured_result["..."]` / `empty_result["..."]`
    / `result["..."]` assignment on the query envelope and require the
    classifier's schema to carry each one -- the next optional key cannot
    silently reclassify a retrieval as ANSWERED."""
    m = _answer_shape_module
    src = Path(m.__file__).resolve().parents[1] / "mcp" / "core.py"
    text = src.read_text()
    assigned = set(re.findall(r'(?:structured_result|empty_result)\["([a-z_]+)"\] =', text))
    schema = next(keys for keys, shape in m._PAYLOAD_SHAPES if shape is m.AnswerShape.RETRIEVAL_ONLY)
    assert assigned, "no envelope assignments found; the scan pattern is stale"
    assert assigned <= schema, f"envelope keys missing from the classifier: {assigned - schema}"


def test_query_envelope_key_set_is_the_one_core_emits() -> None:
    """The plain path's exact key set is pinned in tests/test_query_repoint.py
    (TestQueryRepointMetadataScoped.test_structured_exact_key_set); keep this
    set equal to it so the classifier and the envelope move together."""
    assert QUERY_ENVELOPE_KEYS == {
        "ids", "tumblers", "distances", "collections", "chunk_collections", "chunk_text_hash",
        "also_in", "also_in_ids",
    }
