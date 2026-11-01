# SPDX-License-Identifier: AGPL-3.0-or-later
"""Witness Schema v1 host binding (RDR-088 amendment 2026-11-01, nexus-z1d0l.1.2).

``nexus.operators.witness.bind`` is pure: a model's raw ``operator_check`` /
``operator_verify`` payload plus the evidence records the host showed the model in,
one versioned payload out. These tests pin the binding rules, the quote
normalization and the span map, and ``requires_review``.
"""
from __future__ import annotations

import copy
import json

import pytest

from nexus.operators import witness
from nexus.operators.witness import EvidenceRecord, bind

H1 = "a" * 64
H2 = "b" * 64


def _rec(item_id="e1", text="The model reaches 91.2 percent top-1 accuracy.", **kw):
    return EvidenceRecord(item_id=item_id, text=text, **kw)


def _item(item_id="e1", quote="91.2 percent top-1", role="supports", **extra):
    return {"item_id": item_id, "quote": quote, "role": role, **extra}


def _check(*items, ok=True):
    return {"ok": ok, "evidence": list(items)}


def _verify(*items, verified=True, reason="r"):
    return {"verified": verified, "reason": reason, "evidence": list(items)}


IDENT = dict(tumbler="1.2.3", chash=H1, collection="docs__1-1__voyage-context-3__v1")


class TestPayloadShape:
    def test_check_payload_keys_and_version(self):
        out = bind(_check(_item()), [_rec(**IDENT)], "check")
        assert list(out) == ["schema_version", "ok", "evidence", "rejected", "uncovered", "requires_review"]
        assert out["schema_version"] == 1

    def test_verify_payload_keys_and_version(self):
        out = bind(_verify(_item()), [_rec(**IDENT)], "verify")
        assert list(out) == [
            "schema_version", "verified", "reason", "evidence", "rejected", "requires_review", "citations",
        ]
        assert out["schema_version"] == 1

    def test_host_returned_item_keys(self):
        out = bind(_check(_item()), [_rec(**IDENT)], "check")
        assert list(out["evidence"][0]) == [
            "item_id", "quote", "role", "state", "span", "tumbler", "chash", "collection",
        ]

    def test_input_is_not_mutated(self):
        raw = _check(_item())
        before = copy.deepcopy(raw)
        bind(raw, [_rec(**IDENT)], "check")
        assert raw == before

    def test_a_pre_v1_citations_only_verify_reply_is_returned_unbound(self):
        """The one legacy shape: a verify reply that carries ``citations`` and no ``evidence`` key is what the
        pre-v1 model schema produced. It is read as it was; the host fabricates nothing for it."""
        raw = {"verified": True, "reason": "r", "citations": ["p.3"]}
        assert bind(raw, [_rec()], "verify") == raw

    def test_a_v1_reply_with_no_evidence_list_is_malformed_not_legacy(self):
        """Anything else without an evidence list went through a v1 dispatch and is malformed: schema_version 1,
        no evidence, review required, and the verdict untouched."""
        out = bind({"ok": True}, [_rec("e1"), _rec("e2")], "check")
        assert out["schema_version"] == 1 and out["ok"] is True
        assert out["evidence"] == [] and out["uncovered"] == ["e1", "e2"] and out["requires_review"] is True
        out = bind({"verified": True, "reason": "r"}, [_rec()], "verify")
        assert out["schema_version"] == 1 and out["evidence"] == [] and out["citations"] == []
        assert out["requires_review"] is True
        out = bind({"ok": True, "evidence": "not a list"}, [_rec()], "check")
        assert out["schema_version"] == 1 and out["evidence"] == [] and out["requires_review"] is True

    def test_a_non_mapping_reply_is_returned_as_it_came(self):
        assert bind("prose", [_rec()], "check") == "prose"


class TestBindingRules:
    def test_contained_quote_with_a_tumbler_is_bound_with_host_fields_and_span(self):
        rec = _rec(**IDENT)
        out = bind(_check(_item(quote="91.2 percent")), [rec], "check")
        it = out["evidence"][0]
        assert it["state"] == "bound"
        assert (it["tumbler"], it["chash"], it["collection"]) == (IDENT["tumbler"], H1, IDENT["collection"])
        s = it["span"]
        assert rec.text[s["start"]:s["end"]] == "91.2 percent"

    def test_model_supplied_identity_is_ignored(self):
        rec = _rec(**IDENT)
        evil = _item(tumbler="9.9.9", chash="c" * 64, collection="x", state="bound", span={"start": 0, "end": 1})
        out = bind(_check(evil), [rec], "check")
        it = out["evidence"][0]
        assert (it["tumbler"], it["chash"], it["collection"]) == (IDENT["tumbler"], H1, IDENT["collection"])
        assert it["span"] != {"start": 0, "end": 1}

    def test_model_supplied_identity_cannot_bind_an_identityless_record(self):
        out = bind(_check(_item(tumbler="9.9.9", chash=H2, collection="c")), [_rec()], "check")
        it = out["evidence"][0]
        assert it["state"] == "unattributed"
        assert (it["tumbler"], it["chash"], it["collection"]) == (None, None, None)

    def test_item_id_with_no_record_is_rejected_and_removed(self):
        out = bind(_check(_item(), _item(item_id="zz")), [_rec(**IDENT)], "check")
        assert [i["item_id"] for i in out["evidence"]] == ["e1"]
        assert out["rejected"] == ["zz"]
        assert out["requires_review"] is True

    def test_contained_quote_without_identity_is_unattributed(self):
        it = bind(_check(_item()), [_rec()], "check")["evidence"][0]
        assert it["state"] == "unattributed"
        assert it["span"] is not None
        assert (it["tumbler"], it["chash"], it["collection"]) == (None, None, None)

    def test_chash_only_record_is_ambiguous_and_keeps_its_own_fields_and_span(self):
        rec = _rec(chash=H1, collection="docs__c")
        it = bind(_check(_item()), [rec], "check")["evidence"][0]
        assert it["state"] == "ambiguous"
        assert it["span"] is not None
        assert (it["tumbler"], it["chash"], it["collection"]) == (None, H1, "docs__c")

    def test_wrong_document_quote_is_ambiguous_with_null_span_and_null_source(self):
        a = _rec("e1", "Alpha reports 12 GPU-hours.", tumbler="1.1.1", chash=H1, collection="c")
        b = _rec("e2", "Beta reports 99 GPU-hours.", tumbler="1.1.2", chash=H2, collection="c")
        # the model cites e1 but quotes e2's text
        it = bind(_check(_item("e1", "99 GPU-hours")), [a, b], "check")["evidence"][0]
        assert it["state"] == "ambiguous"
        assert it["span"] is None
        assert (it["tumbler"], it["chash"], it["collection"]) == (None, None, None)

    def test_fabricated_quote_is_unresolved_with_null_span_and_source(self):
        it = bind(_check(_item(quote="the moon is made of cheese")), [_rec(**IDENT)], "check")["evidence"][0]
        assert it["state"] == "unresolved"
        assert it["span"] is None
        assert (it["tumbler"], it["chash"], it["collection"]) == (None, None, None)

    def test_empty_quote_is_unresolved(self):
        for q in ("", "   ", None, 7):
            it = bind(_check(_item(quote=q)), [_rec(**IDENT)], "check")["evidence"][0]
            assert it["state"] == "unresolved"

    def test_case_is_not_folded(self):
        it = bind(_check(_item(quote="THE MODEL REACHES")), [_rec(**IDENT)], "check")["evidence"][0]
        assert it["state"] == "unresolved"

    def test_a_non_mapping_evidence_entry_is_dropped(self):
        out = bind(_check("junk", _item()), [_rec(**IDENT)], "check")
        assert len(out["evidence"]) == 1


class TestNormalization:
    def test_fullwidth_quote_binds_with_an_exact_span(self):
        rec = _rec(text="Accuracy: ABC 123 reported.", **IDENT)
        it = bind(_check(_item(quote="ＡＢＣ １２３")), [rec], "check")["evidence"][0]
        assert it["state"] == "bound"
        assert rec.text[it["span"]["start"]:it["span"]["end"]] == "ABC 123"

    def test_ligature_in_the_record_binds_to_the_plain_quote(self):
        rec = _rec(text="The ﬁnal result is clear.", **IDENT)
        it = bind(_check(_item(quote="final result")), [rec], "check")["evidence"][0]
        assert it["state"] == "bound"
        # the span covers whole original code points: 'ﬁ' is one code point at index 4
        assert rec.text[it["span"]["start"]:it["span"]["end"]] == "ﬁnal result"

    def test_whitespace_runs_collapse_on_both_sides(self):
        rec = _rec(text="line one\n\n   line   two\tends", **IDENT)
        it = bind(_check(_item(quote="one  line two")), [rec], "check")["evidence"][0]
        assert it["state"] == "bound"
        assert rec.text[it["span"]["start"]:it["span"]["end"]] == "one\n\n   line   two"

    def test_nbsp_and_ideographic_space_count_as_whitespace(self):
        rec = _rec(text="alpha beta　gamma", **IDENT)
        assert bind(_check(_item(quote="alpha beta gamma")), [rec], "check")["evidence"][0]["state"] == "bound"

    def test_leading_and_trailing_whitespace_of_the_quote_is_stripped(self):
        it = bind(_check(_item(quote="  \n91.2 percent  ")), [_rec(**IDENT)], "check")["evidence"][0]
        assert it["state"] == "bound"

    def test_cross_character_composition_does_not_match(self):
        rec = _rec(text="café au lait", **IDENT)
        it = bind(_check(_item(quote="café")), [rec], "check")["evidence"][0]
        assert it["state"] == "unresolved"

    def test_span_is_the_first_occurrence(self):
        rec = _rec(text="x cat y cat z", **IDENT)
        it = bind(_check(_item(quote="cat")), [rec], "check")["evidence"][0]
        assert it["span"] == {"start": 2, "end": 5}

    def test_normalize_map_is_exact(self):
        norm, mapping = witness.normalize_with_map("a  ﬁ\tb")
        assert norm == "a fi b"
        assert [ "a  ﬁ\tb"[i] for i in mapping] == ["a", " ", "ﬁ", "ﬁ", "\t", "b"]


class TestFacetTag:
    def test_facet_tag_is_stripped_before_the_test_and_kept_in_the_returned_quote(self):
        out = bind(_check(_item(quote="[f2] 91.2 percent top-1")), [_rec(**IDENT)], "check")
        it = out["evidence"][0]
        assert it["state"] == "bound"
        assert it["quote"] == "[f2] 91.2 percent top-1"

    def test_tag_is_case_insensitive_and_only_one_is_stripped(self):
        assert bind(_check(_item(quote="[F10]91.2 percent")), [_rec(**IDENT)], "check")["evidence"][0]["state"] == "bound"
        it = bind(_check(_item(quote="[f1] [f2] 91.2 percent")), [_rec(**IDENT)], "check")["evidence"][0]
        assert it["state"] == "unresolved"

    def test_a_tag_only_quote_is_unresolved(self):
        assert bind(_check(_item(quote="[f1]")), [_rec(**IDENT)], "check")["evidence"][0]["state"] == "unresolved"

    def test_a_tag_that_is_in_the_record_text_is_not_needed(self):
        rec = _rec(text="[f1] literally in the text", **IDENT)
        assert bind(_check(_item(quote="[f1] literally in the text")), [rec], "check")["evidence"][0]["state"] == "bound"


class TestRequiresReviewAndCoverage:
    def test_all_bound_supported_check_needs_no_review(self):
        recs = [_rec("e1", "alpha text here", **IDENT), _rec("e2", "beta text here", tumbler="1.2.4", chash=H2, collection="c")]
        out = bind(_check(_item("e1", "alpha text"), _item("e2", "beta text")), recs, "check")
        assert out["requires_review"] is False
        assert out["uncovered"] == [] and out["rejected"] == []

    def test_check_ok_true_with_one_of_three_items_named_needs_review(self):
        recs = [_rec(f"e{i}", f"text number {i}", tumbler=f"1.1.{i}", chash=H1, collection="c") for i in (1, 2, 3)]
        out = bind(_check(_item("e1", "text number 1")), recs, "check")
        assert out["uncovered"] == ["e2", "e3"]
        assert out["requires_review"] is True

    def test_neutral_item_does_not_cover(self):
        recs = [_rec("e1", "alpha text", **IDENT)]
        out = bind(_check(_item("e1", "alpha", role="neutral")), recs, "check")
        assert out["uncovered"] == ["e1"]
        assert out["requires_review"] is True

    def test_ok_false_with_all_bound_and_covered_needs_no_review(self):
        recs = [_rec("e1", "alpha text", **IDENT)]
        out = bind(_check(_item("e1", "alpha", role="contradicts"), ok=False), recs, "check")
        assert out["requires_review"] is False

    def test_ok_true_with_only_unbound_support_needs_review(self):
        out = bind(_check(_item()), [_rec()], "check")  # unattributed
        assert out["requires_review"] is True

    def test_verify_true_without_a_bound_supports_needs_review(self):
        out = bind(_verify(_item(role="contradicts")), [_rec(**IDENT)], "verify")
        assert out["requires_review"] is True

    def test_verify_true_with_a_bound_support_needs_no_review(self):
        out = bind(_verify(_item()), [_rec(**IDENT)], "verify")
        assert out["requires_review"] is False

    def test_verify_false_with_a_bound_contradiction_needs_no_review(self):
        out = bind(_verify(_item(role="contradicts"), verified=False), [_rec(**IDENT)], "verify")
        assert out["requires_review"] is False

    def test_verify_false_with_no_evidence_has_nothing_unbound(self):
        out = bind(_verify(verified=False), [_rec(**IDENT)], "verify")
        assert out["evidence"] == [] and out["requires_review"] is False

    def test_any_unbound_item_needs_review(self):
        out = bind(_verify(_item(), _item(quote="invented"), verified=False), [_rec(**IDENT)], "verify")
        assert out["requires_review"] is True

    def test_the_host_never_changes_the_verdict(self):
        assert bind(_check(_item(quote="invented")), [_rec(**IDENT)], "check")["ok"] is True
        assert bind(_verify(_item(quote="invented")), [_rec(**IDENT)], "verify")["verified"] is True

    def test_truthy_non_boolean_verdict_counts_as_a_true_verdict(self):
        """No evidence at all, so only the verdict rule can ask for review."""
        out = bind(_verify(verified="yes"), [_rec(**IDENT)], "verify")
        assert out["verified"] == "yes"  # untouched
        assert out["requires_review"] is True
        out = bind(_check(ok=1), [_rec(**IDENT)], "check")
        assert out["ok"] == 1 and out["requires_review"] is True

    def test_a_falsy_verdict_with_no_evidence_needs_no_review(self):
        assert bind(_verify(verified=False), [_rec(**IDENT)], "verify")["requires_review"] is False

    def test_a_merged_coverage_note_is_not_an_input_to_cover(self):
        note = EvidenceRecord("e1", "NOTE: unresolved facets", is_evidence=False)
        real = _rec("e2", "real text here", **IDENT)
        out = bind(_check(_item("e2", "real text")), [note, real], "check")
        assert out["uncovered"] == [] and out["requires_review"] is False


class TestCitations:
    def test_citations_are_quotes_of_bound_and_unattributed_items_in_order(self):
        recs = [_rec("e1", "alpha text", **IDENT), _rec("e2", "beta text"), _rec("e3", "gamma text", chash=H2, collection="c")]
        out = bind(
            _verify(_item("e1", "alpha"), _item("e2", "beta"), _item("e3", "gamma"), _item("e1", "nope")),
            recs, "verify",
        )
        assert out["citations"] == ["alpha", "beta"]

    def test_citations_can_be_empty_on_a_failing_verdict(self):
        assert bind(_verify(verified=False), [_rec()], "verify")["citations"] == []


class TestUnrecordedBundleInput:
    def test_every_item_is_unresolved_and_requires_review_when_no_records_exist(self):
        out = bind(_check(_item(), _item(item_id="zz")), None, "check")
        assert [i["state"] for i in out["evidence"]] == ["unresolved", "unresolved"]
        assert all(i["span"] is None and i["tumbler"] is None for i in out["evidence"])
        assert out["rejected"] == []
        assert out["requires_review"] is True

    def test_verify_with_no_records_is_unresolved_too(self):
        out = bind(_verify(_item(), verified=False), None, "verify")
        assert out["evidence"][0]["state"] == "unresolved"
        assert out["requires_review"] is True
        assert out["citations"] == []


class TestRecordsFromCallerArguments:
    def test_check_items_array_of_id_text_objects_gives_a_record_each_with_the_callers_ids(self):
        recs = witness.records_from_check_items('[{"id": "p1", "text": "one"}, {"id": "p2", "text": "two"}]')
        assert [(r.item_id, r.text) for r in recs] == [("p1", "one"), ("p2", "two")]
        assert all(r.tumbler is None and r.chash is None and r.collection is None for r in recs)

    @pytest.mark.parametrize("items", [
        "plain text, not JSON",
        '["a", "b"]',
        '[{"id": "a"}]',                                   # no text
        '[{"text": "a"}]',                                 # no id
        '[{"id": "a", "text": "x"}, {"id": "a", "text": "y"}]',   # duplicate ids
        '[{"id": "a", "text": "x"}, "b"]',                 # mixed
        '{"id": "a", "text": "x"}',                        # not an array
        "[]",
    ])
    def test_anything_else_is_one_record_e1_with_the_whole_string(self, items):
        recs = witness.records_from_check_items(items)
        assert len(recs) == 1 and recs[0].item_id == "e1" and recs[0].text == items

    def test_verify_evidence_is_always_one_record_e1_and_is_never_sniffed(self):
        ev = '[{"id": "a", "text": "x"}]'
        recs = witness.records_from_verify_evidence(ev)
        assert [(r.item_id, r.text) for r in recs] == [("e1", ev)]

    def test_shown_items_render_the_records_as_id_text_objects(self):
        recs = [EvidenceRecord("e1", "café"), EvidenceRecord("e2", "two")]
        shown = witness.shown_items(recs)
        assert json.loads(shown) == [{"id": "e1", "text": "café"}, {"id": "e2", "text": "two"}]
        assert "café" in shown  # not \u-escaped

    def test_check_items_shown_unchanged_when_the_callers_objects_are_the_records(self):
        items = '[{"id": "p1", "text": "one"}]'
        assert witness.shown_check_items(items) == items

    def test_check_items_plain_text_is_shown_as_the_e1_record(self):
        assert json.loads(witness.shown_check_items("plain")) == [{"id": "e1", "text": "plain"}]


class TestDeferral:
    def test_binding_is_not_deferred_by_default(self):
        assert witness.binding_deferred() is False

    def test_defer_binding_scopes_the_flag(self):
        with witness.defer_binding():
            assert witness.binding_deferred() is True
        assert witness.binding_deferred() is False


class TestAdversarialInputs:
    def test_non_string_item_ids_do_not_raise_on_the_unrecorded_path(self):
        for bad in ([1, 2], {"a": 1}, None, 5):
            out = bind(_check(_item(item_id=bad)), None, "check")
            assert out["evidence"][0]["state"] == "unresolved" and out["requires_review"] is True

    def test_non_string_item_ids_are_rejected_on_the_recorded_path(self):
        out = bind(_check(_item(item_id=["e1"])), [_rec(**IDENT)], "check")
        assert out["evidence"] == [] and out["rejected"] == ["['e1']"]

    def test_a_quote_may_not_end_inside_a_combining_sequence(self):
        rec = _rec(text="cafe\u0301 au lait", **IDENT)  # e + COMBINING ACUTE
        assert bind(_check(_item(quote="cafe")), [rec], "check")["evidence"][0]["state"] == "unresolved"
        assert bind(_check(_item(quote="cafe\u0301")), [rec], "check")["evidence"][0]["state"] == "bound"

    def test_a_quote_may_not_start_inside_a_combining_sequence(self):
        rec = _rec(text="cafe\u0301 au lait", **IDENT)
        assert bind(_check(_item(quote="\u0301 au")), [rec], "check")["evidence"][0]["state"] == "unresolved"

    def test_a_later_occurrence_that_respects_the_boundary_still_matches(self):
        rec = _rec(text="cafe\u0301 and then cafe again", **IDENT)
        it = bind(_check(_item(quote="cafe")), [rec], "check")["evidence"][0]
        assert it["state"] == "bound"
        assert rec.text[it["span"]["start"]:it["span"]["end"]] == "cafe"
        assert it["span"]["start"] > 5

    def test_role_is_normalised_and_an_unknown_role_is_null_and_never_covers(self):
        out = bind(_check(_item(role="partial"), _item(item_id="e1", role=None)), [_rec(**IDENT)], "check")
        assert [i["role"] for i in out["evidence"]] == [None, None]
        assert out["uncovered"] == ["e1"] and out["requires_review"] is True
        assert bind(_check(_item(role="supports")), [_rec(**IDENT)], "check")["evidence"][0]["role"] == "supports"

    def test_each_record_text_is_normalised_once_per_bind(self):
        recs = [_rec("e1", "alpha text " * 50, **IDENT), _rec("e2", "beta text " * 50, tumbler="1.2.4", chash=H2, collection="c")]
        texts = {r.text for r in recs}
        real = witness.normalize_with_map
        seen: list[str] = []

        def counting(text):
            if text in texts:
                seen.append(text)
            return real(text)

        quotes = [_item("e1", f"fabricated {i}") for i in range(6)]
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(witness, "normalize_with_map", counting)
            bind(_check(*quotes), recs, "check")
        assert sorted(seen) == sorted(texts)  # once each, not per item and per elsewhere-scan
