"""nexus-90gyo / nexus-zy0kj: the answer-shape classifier.

Anchored to the live shapes the 2026-09-06 probe recorded: run 706 /
plan 488 (six retrieval steps, ``store_get_many`` payload as the answer)
and plan 487 (extract-terminal compare plan, bare extractions).
"""

from __future__ import annotations

import json

import pytest

from nexus.plans.answer_shape import (
    NON_ANSWER_MAX_CHUNK_REFS,
    NON_ANSWER_NOTICE_PREFIX,
    NON_ANSWER_SHAPES,
    AnswerShape,
    classify_answer_shape,
    render_non_answer_notice,
)

class TestPayloadShapes:
    def test_store_get_many_payload_is_hydration_dump(self) -> None:
        text = json.dumps({"contents": ["a", "b"], "missing": [], "section_types": ["class", "method"]})
        assert classify_answer_shape(text) is AnswerShape.HYDRATION_DUMP

    def test_hydration_dump_wins_even_when_plan_has_an_operator(self) -> None:
        """The text rule outranks the structural rule: what the caller
        received is a payload, whatever the plan promised."""
        text = json.dumps({"contents": ["a"], "missing": []})
        assert classify_answer_shape(text) is AnswerShape.HYDRATION_DUMP

    def test_extract_payload_is_extractions_only(self) -> None:
        text = json.dumps({"extractions": [{"item_index": 1, "rdr_id": "RDR-176"}]})
        assert classify_answer_shape(text) is AnswerShape.EXTRACTIONS_ONLY

    def test_rank_payload_is_ranking_only(self) -> None:
        text = json.dumps({"ranked": ["a", "b"]})
        assert classify_answer_shape(text) is AnswerShape.RANKING_ONLY

    def test_payload_with_extra_keys_is_not_a_payload(self) -> None:
        """A dict carrying keys outside the payload schema is not that
        payload — the subset rule, not a substring rule."""
        text = json.dumps({"extractions": [], "answer": "The two RDRs differ in ..."})
        assert classify_answer_shape(text) is AnswerShape.ANSWERED

    def test_prose_mentioning_contents_is_answered(self) -> None:
        text = 'The manifest "contents" field lists every chash, and {"extractions"} is the operator key.'
        assert classify_answer_shape(text) is AnswerShape.ANSWERED

    def test_empty_dict_is_not_a_payload(self) -> None:
        assert classify_answer_shape("{}") is AnswerShape.ANSWERED

    def test_leading_whitespace_tolerated(self) -> None:
        text = "\n  " + json.dumps({"contents": ["a"]})
        assert classify_answer_shape(text) is AnswerShape.HYDRATION_DUMP


class TestStructuralAndTextRules:
    def test_search_result_payload_is_retrieval_only(self) -> None:
        text = json.dumps({"ids": ["c1"], "tumblers": [""], "distances": [0.1],
                           "collections": ["rdr__1-1"], "chunk_collections": ["rdr__1-1"],
                           "chunk_text_hash": ["c1"]})
        assert classify_answer_shape(text) is AnswerShape.RETRIEVAL_ONLY

    def test_traverse_result_payload_is_retrieval_only(self) -> None:
        text = json.dumps({"tumblers": ["1.1"], "ids": [], "collections": ["rdr__1-1"]})
        assert classify_answer_shape(text) is AnswerShape.RETRIEVAL_ONLY

    def test_prose_from_a_fetch_only_plan_is_still_answered(self) -> None:
        """Text-only by design: the classifier judges what the caller
        received, never what the plan promised."""
        assert classify_answer_shape("some rendered search line") is AnswerShape.ANSWERED

    def test_listing_header(self) -> None:
        text = "Found 3 documents (from 12 chunks across 2 collections)\n\n1. [0.1] T\n"
        assert classify_answer_shape(text) is AnswerShape.LISTING

    def test_listing_header_not_on_first_line_is_not_listing(self) -> None:
        text = "[Catalog routing]\nFound 3 documents (from 9 across 3 collections)\n"
        assert classify_answer_shape(text) is AnswerShape.ANSWERED

    @pytest.mark.parametrize("text", ["", "   ", "\n\t", None])
    def test_empty(self, text) -> None:
        assert classify_answer_shape(text) is AnswerShape.EMPTY

    def test_generate_terminal_prose_is_answered(self) -> None:
        text = "**Limitation 1.** RkNN results can lie far from q ..."
        assert classify_answer_shape(text) is AnswerShape.ANSWERED


class TestVocabulary:
    def test_answered_is_the_only_answer(self) -> None:
        assert set(AnswerShape) - NON_ANSWER_SHAPES == {AnswerShape.ANSWERED}

    def test_notice_carries_prefix_shape_and_plan_id(self) -> None:
        notice = render_non_answer_notice(AnswerShape.HYDRATION_DUMP, plan_id=488, step_count=6)
        assert notice.startswith(NON_ANSWER_NOTICE_PREFIX + " hydration_dump]")
        assert "plan_id=488" in notice and "6 steps" in notice
        assert "search/query directly" in notice
        # The notice must not itself classify as a payload or listing.
        assert classify_answer_shape(notice) is AnswerShape.ANSWERED

    def test_notice_lists_chunk_refs_capped(self) -> None:
        """A text-mode caller never sees the envelope's chunks, so the
        notice carries the references itself (critique, 2026-09-06)."""
        refs = [f"rdr__1-1 chash:{i:064x}" for i in range(NON_ANSWER_MAX_CHUNK_REFS + 3)]
        notice = render_non_answer_notice(AnswerShape.RETRIEVAL_ONLY, plan_id=488, step_count=6, chunk_refs=refs)
        assert refs[0] in notice and refs[NON_ANSWER_MAX_CHUNK_REFS - 1] in notice
        assert refs[NON_ANSWER_MAX_CHUNK_REFS] not in notice
        assert "and 3 more" in notice
        assert f"Retrieved {len(refs)} chunk(s)" in notice
        assert classify_answer_shape(notice) is AnswerShape.ANSWERED

    @pytest.mark.parametrize("payload", [
        {"items": [{"id": "a"}], "rationale": [{"id": "a", "reason": "r"}]},
        {"ok": True, "evidence": []},
        {"verified": False, "reason": "no", "citations": []},
        {"groups": [{"key_value": "x", "items": []}]},
    ])
    def test_other_operator_payloads_are_non_answers(self, payload) -> None:
        assert classify_answer_shape(json.dumps(payload)) is AnswerShape.OPERATOR_PAYLOAD
