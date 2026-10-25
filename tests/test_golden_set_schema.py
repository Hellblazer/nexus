# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.1): golden-set label schema and the committed set.

The golden set extends the RDR-090 labeled query format (``bench/queries/spike_5q.yaml``) with
the labels the RDR-232 measures need: ``answer_kind``, ``required_sources``, ``required_facets``,
``allowed_corpora``, ``unacceptable_plans``, plus a top-level ``golden_set_version``. These tests pin
three things:

* the strict loader rejects a set with a missing field, an unknown kind, or a wrong version;
* the existing lenient loader still reads ``spike_5q.yaml`` unchanged;
* the committed ``bench/queries/golden_v1.yaml`` is a real set: every request kind is present,
  the ART RDR-084 request is verbatim, and every ``unacceptable_plans`` entry names a plan that
  exists in the builtin library (a typo there would make the wrong-shape measure read zero).

Pure file checks: no engine, no model call.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "bench" / "queries" / "golden_v1.yaml"
BUILTIN_PLANS = REPO / "conexus" / "plans" / "builtin"

#: The ART RDR-084 request as recorded in the 2026-10-23 incident (RDR-232 Problem Statement;
#: full text as stored in nx_answer_runs run 722, 2026-10-23T22:25:33Z).
ART_RDR_084_REQUEST = (
    "Synthesize RDR-084, LIST PARSE Canonical Consolidation. State the locked architecture "
    "and numerical conventions, the evidence and gate history supporting them, the "
    "implementation work recorded as complete or pending, and what remains open by design. "
    "Distinguish facts at the RDR's 2026-07-30 acceptance from later records, cite source "
    "documents, and do not infer current code status beyond the retrieved evidence."
)

NEW_FIELDS = (
    "answer_kind",
    "required_sources",
    "required_facets",
    "allowed_corpora",
    "unacceptable_plans",
)


def _valid_query(**over) -> dict:
    q = {
        "qid": "G-ok",
        "category": "factual",
        "text": "Which RDR introduced catalog tumblers?",
        "answer_kind": "lookup",
        "required_sources": ["RDR-049"],
        "required_facets": ["introducing-rdr"],
        "allowed_corpora": ["rdr"],
        "unacceptable_plans": ["query:abstract-themes"],
    }
    q.update(over)
    return q


def _write(tmp_path: Path, queries: list[dict], *, version: object = 1) -> Path:
    doc: dict = {"queries": queries}
    if version is not None:
        doc["golden_set_version"] = version
    path = tmp_path / "g.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


class TestStrictLoader:
    def test_valid_set_loads_with_every_label(self, tmp_path: Path) -> None:
        from bench.schema import load_golden_set

        qs = load_golden_set(_write(tmp_path, [_valid_query()]))
        assert len(qs) == 1
        q = qs[0]
        assert q.answer_kind == "lookup"
        assert q.required_sources == ["RDR-049"]
        assert q.required_facets == ["introducing-rdr"]
        assert q.allowed_corpora == ["rdr"]
        assert q.unacceptable_plans == ["query:abstract-themes"]

    @pytest.mark.parametrize("field", sorted(NEW_FIELDS))
    def test_missing_label_field_is_rejected(self, tmp_path: Path, field: str) -> None:
        from bench.schema import load_golden_set

        q = _valid_query()
        del q[field]
        with pytest.raises(ValueError, match=field):
            load_golden_set(_write(tmp_path, [q]))

    def test_unknown_answer_kind_is_rejected(self, tmp_path: Path) -> None:
        from bench.schema import load_golden_set

        with pytest.raises(ValueError, match="answer_kind"):
            load_golden_set(_write(tmp_path, [_valid_query(answer_kind="essay")]))

    def test_unknown_allowed_corpus_is_rejected(self, tmp_path: Path) -> None:
        from bench.schema import load_golden_set

        with pytest.raises(ValueError, match="allowed_corpora"):
            load_golden_set(_write(tmp_path, [_valid_query(allowed_corpora=["papers"])]))

    @pytest.mark.parametrize("field", ["required_sources", "required_facets", "unacceptable_plans"])
    def test_a_label_list_must_be_a_list_of_strings(self, tmp_path: Path, field: str) -> None:
        from bench.schema import load_golden_set

        with pytest.raises(ValueError, match=field):
            load_golden_set(_write(tmp_path, [_valid_query(**{field: "RDR-049"})]))

    def test_missing_version_is_rejected(self, tmp_path: Path) -> None:
        from bench.schema import load_golden_set

        with pytest.raises(ValueError, match="golden_set_version"):
            load_golden_set(_write(tmp_path, [_valid_query()], version=None))

    def test_unsupported_version_is_rejected(self, tmp_path: Path) -> None:
        from bench.schema import load_golden_set

        with pytest.raises(ValueError, match="golden_set_version"):
            load_golden_set(_write(tmp_path, [_valid_query()], version=2))

    def test_duplicate_qid_is_rejected(self, tmp_path: Path) -> None:
        from bench.schema import load_golden_set

        with pytest.raises(ValueError, match="duplicate"):
            load_golden_set(_write(tmp_path, [_valid_query(), _valid_query()]))

    def test_the_pre_existing_required_fields_still_apply(self, tmp_path: Path) -> None:
        from bench.schema import load_golden_set

        q = _valid_query()
        del q["category"]
        with pytest.raises(ValueError, match="category"):
            load_golden_set(_write(tmp_path, [q]))


class TestLenientLoaderUnchanged:
    def test_spike_5q_still_loads_with_empty_golden_labels(self) -> None:
        from bench.schema import load_queries

        qs = load_queries(REPO / "bench" / "queries" / "spike_5q.yaml")
        assert len(qs) == 5
        assert all(q.answer_kind == "" and q.required_sources == [] for q in qs)

    def test_lenient_loader_reads_the_labels_when_a_file_carries_them(self, tmp_path: Path) -> None:
        from bench.schema import load_queries

        qs = load_queries(_write(tmp_path, [_valid_query()]))
        assert qs[0].answer_kind == "lookup"


def _builtin_plan_keys() -> set[str]:
    keys: set[str] = set()
    for path in sorted(BUILTIN_PLANS.glob("*.yml")):
        dims = yaml.safe_load(path.read_text(encoding="utf-8"))["dimensions"]
        keys.add(f"{dims['verb']}:{dims['strategy']}")
    return keys


class TestCommittedGoldenSet:
    def test_the_committed_set_loads_strictly(self) -> None:
        from bench.schema import GOLDEN_SET_VERSION, load_golden_set

        qs = load_golden_set(GOLDEN)
        assert GOLDEN_SET_VERSION == 1
        # Non-vacuity: a set this small cannot back a measure.
        assert len(qs) >= 20, f"golden set has {len(qs)} requests"

    def test_every_request_kind_is_covered(self) -> None:
        from bench.schema import ANSWER_KINDS, load_golden_set

        qs = load_golden_set(GOLDEN)
        seen = {q.answer_kind for q in qs}
        missing = sorted(ANSWER_KINDS - seen)
        assert not missing, f"golden set has no request of kind(s) {missing}"

    def test_the_art_rdr_084_request_is_verbatim(self) -> None:
        from bench.schema import load_golden_set

        qs = [q for q in load_golden_set(GOLDEN) if q.text == ART_RDR_084_REQUEST]
        assert len(qs) == 1, "the ART RDR-084 request must appear exactly once, verbatim"
        q = qs[0]
        assert q.answer_kind == "synthesis"
        assert "hybrid-factual-lookup" in " ".join(q.unacceptable_plans)
        assert q.allowed_corpora == ["rdr"]
        assert len(q.required_facets) >= 5

    def test_unacceptable_plans_name_real_builtin_plans(self) -> None:
        from bench.schema import load_golden_set

        keys = _builtin_plan_keys()
        assert len(keys) >= 10, f"builtin plan census came back with {len(keys)} plans"
        for q in load_golden_set(GOLDEN):
            unknown = sorted(set(q.unacceptable_plans) - keys)
            assert not unknown, f"{q.qid}: unacceptable_plans names no builtin plan: {unknown}"

    def test_a_lookup_request_never_lists_the_lookup_plan_as_unacceptable(self) -> None:
        # The labels are the request's, not the plan's: a single-fact lookup is exactly what
        # hybrid-factual-lookup answers, so listing it unacceptable would be a labeling error.
        from bench.schema import load_golden_set

        for q in load_golden_set(GOLDEN):
            if q.answer_kind == "lookup":
                assert "lookup:hybrid-factual-lookup" not in q.unacceptable_plans, q.qid
