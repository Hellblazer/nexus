# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-134 Step 8 (nexus-7fku2.2.2): the generic-document question set loads, is labelled, and is reported.

``bench/queries/generic_docs_v1.yaml`` holds questions whose grade-3 answer is a generic document
(a changelog, a CLI reference, a docs page). Step 9 reads it through ``scripts/bench/taxonomy_ab2.py``
and, for the benchmark's Path B, through ``scripts/bench/schema.py``. These tests pin that both loaders
accept it, that no question is dropped on the way, and that every label names a file that exists.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

import bench.taxonomy_ab2 as ab2
from bench.schema import load_queries as schema_load_queries

ROOT = Path(__file__).resolve().parent.parent
QUERIES = ROOT / "bench" / "queries" / "generic_docs_v1.yaml"
MIN_GENERIC = 10


def _raw() -> list[dict]:
    return yaml.safe_load(QUERIES.read_text())["queries"]


def _grade3_keys(q: dict) -> list[str]:
    return [k for k, g in q["ground_truth"].items() if g == 3]


def test_file_exists_and_has_labelling_method_header() -> None:
    text = QUERIES.read_text()
    assert "Labelling method" in text
    assert "2026-11-19" in text
    assert "before any routed" in text


def test_schema_loader_accepts_the_file() -> None:
    qs = schema_load_queries(QUERIES)
    assert len(qs) >= MIN_GENERIC
    assert len({q.qid for q in qs}) == len(qs)
    for q in qs:
        assert q.text.strip()
        assert q.ground_truth, q.qid


def test_taxonomy_ab2_drops_no_question_and_marks_them_generic() -> None:
    raw = _raw()
    loaded = ab2.load_queries([QUERIES])
    assert len(loaded) == len(raw), "taxonomy_ab2.load_queries silently dropped a question"
    assert {q["kind"] for q in loaded} == {"generic"}
    assert len(loaded) >= MIN_GENERIC


def test_every_question_has_a_source_and_a_grade_3() -> None:
    for q in _raw():
        gt = q["ground_truth"]
        assert gt, q["qid"]
        assert all(g in (0, 1, 2, 3) for g in gt.values()), q["qid"]
        assert _grade3_keys(q), f"{q['qid']} has no grade-3 source"


def test_keys_are_basenames_that_the_grader_matches_and_that_exist() -> None:
    """The grader matches a normalised key against the normalised file BASENAME; a path never matches."""
    for q in _raw():
        for key, grade in q["ground_truth"].items():
            assert "/" not in key, f"{q['qid']}: {key!r} is a path; the grader sees only the basename"
            assert ab2._grade(key, {ab2._norm(key): grade}) == grade
        for key in _grade3_keys(q):
            candidates = [ROOT / key, ROOT / "docs" / key]
            assert any(c.is_file() for c in candidates), f"{q['qid']}: grade-3 source {key!r} not found"


def test_spread_over_documents() -> None:
    per_doc = Counter(k for q in _raw() for k in _grade3_keys(q))
    assert len(per_doc) >= 5, per_doc
    assert max(per_doc.values()) <= 3, per_doc


def test_qids_and_texts_are_unique() -> None:
    raw = _raw()
    assert len({q["qid"] for q in raw}) == len(raw)
    assert len({q["text"] for q in raw}) == len(raw)


def test_generic_is_a_summary_bucket_and_unknown_kind_is_refused(tmp_path: Path) -> None:
    assert "generic" in ab2.SUMMARY_KINDS
    assert ab2.SUMMARY_KINDS[-1] == "all"
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"queries": [
        {"qid": "X", "category": "factual", "text": "t", "kind": "bogus", "ground_truth": {"a.md": 3}}]}))
    with pytest.raises(ValueError, match="kind"):
        ab2.load_queries([bad])


def test_existing_kinds_are_unchanged(tmp_path: Path) -> None:
    f = tmp_path / "old.yaml"
    f.write_text(yaml.safe_dump({"queries": [
        {"qid": "A", "category": "factual", "text": "a", "ground_truth": {"rdr-049-": 3}},
        {"qid": "B", "category": "factual", "text": "b", "required_sources": ["Some Paper Title"]}]}))
    assert [q["kind"] for q in ab2.load_queries([f])] == ["rdr", "title"]
