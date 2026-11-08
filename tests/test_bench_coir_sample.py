# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-244 Track A sample draw (nexus-2k66t.2; residuals .6 and .12)."""
from __future__ import annotations

import random
import pytest

from scripts.bench.coir_sample import draw, loadable


def _corpus(n: int, text: str = "x") -> list[dict]:
    return [{"_id": f"c{i}", "text": text} for i in range(n)]


def _qrels(n: int) -> list[dict]:
    return [{"query_id": f"q{i}", "corpus_id": f"c{i}", "score": 1} for i in range(n)]


def test_draw_is_deterministic_and_keeps_relevant_documents() -> None:
    a = draw("ds", _corpus(200), _qrels(100), queries=10, cap=40)
    b = draw("ds", _corpus(200), _qrels(100), queries=10, cap=40)
    assert a == b and a.manifest()["sha256"] == b.manifest()["sha256"]
    assert len(a.query_ids) == 10 and len(a.corpus_ids) == 40
    assert all(c in a.corpus_ids for q in a.query_ids for c in a.qrels[q])


def test_small_corpus_loads_whole() -> None:
    s = draw("ds", _corpus(30), _qrels(30), queries=50, cap=5000)
    assert len(s.query_ids) == 30 and len(s.corpus_ids) == 30


def test_changed_seed_changes_the_manifest_hash() -> None:
    """Test Plan: a changed query set shows as a different hash."""
    a = draw("ds", _corpus(200), _qrels(100), queries=10, cap=40)
    b = draw("ds", _corpus(200), _qrels(100), queries=10, cap=40, seed=7)
    assert a.manifest()["sha256"] != b.manifest()["sha256"]


def test_unloadable_document_and_its_query_are_excluded_and_counted() -> None:
    corpus = _corpus(5) + [{"_id": "c5", "text": "  "}, {"_id": "c6", "text": "y" * 20000}]
    s = draw("ds", corpus, _qrels(7), queries=7, cap=100)
    assert s.excluded_docs == ["c5", "c6"]
    assert s.excluded_queries == ["q5", "q6"]
    assert "c5" not in s.corpus_ids and "q5" not in s.query_ids


def test_qrels_naming_a_missing_document_refuses() -> None:
    with pytest.raises(ValueError, match="not in the corpus"):
        draw("ds", _corpus(2), _qrels(3), queries=3)


def test_loadable_counts_utf8_bytes() -> None:
    assert loadable("é" * 8192) and not loadable("é" * 8193)


def test_identical_texts_form_one_class_named_by_smallest_id() -> None:
    corpus = [{"_id": "c0", "text": "a"}, {"_id": "c1", "text": "same"}, {"_id": "c2", "text": "same"},
              {"_id": "c3", "text": "same"}, {"_id": "c4", "text": "b"}]
    s = draw("ds", corpus, _qrels(5), queries=5, cap=100)
    assert s.duplicate_classes == {"c1": ["c1", "c2", "c3"]}
    assert [s.class_of(c) for c in ("c0", "c2", "c3")] == ["c0", "c1", "c1"]
    assert "duplicate_classes" in s.manifest()


def test_changed_text_changes_the_manifest_hash() -> None:
    """Test Plan: a changed query-set file is a different set, also when only a text changed."""
    base = draw("ds", _corpus(20), _qrels(10), queries=5, cap=20, query_text={f"q{i}": "a" for i in range(10)})
    corpus = _corpus(20)
    corpus[3]["text"] = "changed"
    doc = draw("ds", corpus, _qrels(10), queries=5, cap=20, query_text={f"q{i}": "a" for i in range(10)})
    qtext = {f"q{i}": "a" for i in range(10)} | {base.query_ids[0]: "b"}
    query = draw("ds", _corpus(20), _qrels(10), queries=5, cap=20, query_text=qtext)
    assert len({base.manifest()["sha256"], doc.manifest()["sha256"], query.manifest()["sha256"]}) == 3


def test_draw_matches_the_preregistered_rule_exactly() -> None:
    """Golden: Random(2026).sample over sorted ids, for queries and for the corpus fill."""
    s = draw("ds", _corpus(50), _qrels(30), queries=5, cap=12)
    qids = sorted(f"q{i}" for i in range(30))
    want_q = sorted(random.Random(2026).sample(qids, 5))
    assert s.query_ids == want_q
    must = sorted("c" + q[1:] for q in want_q)
    rest = sorted(f"c{i}" for i in range(50) if f"c{i}" not in must)
    assert s.corpus_ids == sorted(set(must) | set(random.Random(2026).sample(rest, 7)))


def test_score_zero_judgements_are_dropped_not_loaded() -> None:
    qrels = _qrels(3) + [{"query_id": "q0", "corpus_id": "absent", "score": 0}]
    s = draw("ds", _corpus(3), qrels, queries=3)
    assert "absent" not in s.corpus_ids and s.qrels["q0"] == {"c0": 1} and s.grades == [1]
