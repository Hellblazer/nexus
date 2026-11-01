# SPDX-License-Identifier: AGPL-3.0-or-later
"""The plan runner's evidence-record channel for Witness Schema v1 (RDR-088 amendment 2026-11-01,
nexus-z1d0l.1.2).

A check step fed by a retrieval step builds one ``EvidenceRecord`` per item the model is shown, from the
retrieval step's own output, zipped with the hydrated contents by position BEFORE the empty-content filter,
the supplement / reserved merges and the cap. The records ride beside the args (``_evidence_records``,
like ``_truncation_metadata``), never into the prompt, and the runner binds the operator's reply once.

Storage-free: T3 and the catalog are stubbed (the fixtures of ``test_6pfgb50_runner_chash_reuses_rows``);
the model is stubbed at the ``claude_dispatch`` boundary.
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest

import nexus.operators.dispatch as dmod
from nexus.mcp.core import operator_verify
from nexus.operators.witness import EvidenceRecord
from nexus.plans import bundle as bundle_mod
from nexus.plans import runner
from nexus.plans.match import Match
from nexus.plans.repair_stream import RepairSupplement, SupplementItem, supplement_scope
from tests.test_6pfgb11_provenance_from_catalog import COLL, X, _entry, _position, _t3
from tests.test_6pfgb50_runner_chash_reuses_rows import Y, _CountingCatalog, _plain

Z = "c" * 64


def _source(ids, *, tumblers=None, collections=None, hashes=None):
    return {
        "ids": list(ids),
        "tumblers": list(tumblers) if tumblers is not None else [""] * len(ids),
        "chunk_collections": list(collections) if collections is not None else [COLL] * len(ids),
        "chunk_text_hash": list(hashes) if hashes is not None else list(ids),
    }


def _hydrate_check(catalog, ids, rows, *, source=None, extra=None):
    with patch("nexus.mcp_infra.get_catalog", return_value=catalog), patch(
        "nexus.mcp.core._get_catalog", return_value=catalog,
    ), patch("nexus.mcp.core._get_t3", return_value=_t3(rows)), patch(
        "nexus.mcp.core._read_scope", return_value=[COLL],
    ):
        args = {"ids": ids, "collections": COLL, "check_instruction": "c", **(extra or {})}
        if source is not None:
            args["_evidence_source"] = source
        return runner._hydrate_operator_args("check", args)


def _shown(args):
    return json.loads(args["items"])


class TestChashPathRecords:
    def test_each_item_gets_a_record_with_host_identity_and_body_text(self) -> None:
        catalog = _CountingCatalog(
            [_plain("1.1.1"), _plain("1.1.2")], [_position(X, "1.1.1"), _position(Y, "1.1.2")],
        )
        _, args = _hydrate_check(
            catalog, [X, Y], {X: {"content": "first body"}, Y: {"content": "second body"}},
            source=_source([X, Y], tumblers=["1.1.1", "1.1.2"]),
        )
        recs = args["_evidence_records"]
        assert [(r.item_id, r.text, r.tumbler, r.chash, r.collection) for r in recs] == [
            ("e1", "first body", "1.1.1", X, COLL),
            ("e2", "second body", "1.1.2", Y, COLL),
        ]
        shown = _shown(args)
        assert [s["id"] for s in shown] == ["e1", "e2"]
        # the model reads the source note the hydration adds; the record's body never carries it
        assert all(s["text"].startswith("[source: ") and s["text"].endswith(r.text) for s, r in zip(shown, recs))
        assert all("[source:" not in r.text for r in recs)

    def test_a_chash_shared_by_two_documents_is_chash_only_never_a_guessed_tumbler(self) -> None:
        catalog = _CountingCatalog(
            [_plain("1.1.1"), _plain("1.1.2")], [_position(X, "1.1.1"), _position(X, "1.1.2")],
        )
        # search picked 1.1.1 for the shared chunk (an inferred owner); it must not become identity
        _, args = _hydrate_check(
            catalog, [X], {X: {"content": "shared body"}}, source=_source([X], tumblers=["1.1.1"]),
        )
        (rec,) = args["_evidence_records"]
        assert (rec.tumbler, rec.chash, rec.collection) == (None, X, COLL)

    def test_a_retrieval_tumbler_without_an_owner_set_is_not_identity(self) -> None:
        catalog = _CountingCatalog([_plain("1.1.1")], [_position(X, "1.1.1")], positions_fail=True)
        _, args = _hydrate_check(
            catalog, [X], {X: {"content": "body"}}, source=_source([X], tumblers=["1.1.1"]),
        )
        (rec,) = args["_evidence_records"]
        assert rec.tumbler is None and rec.chash == X

    def test_alignment_survives_the_empty_content_filter(self) -> None:
        catalog = _CountingCatalog(
            [_plain("1.1.1"), _plain("1.1.2")], [_position(X, "1.1.1"), _position(Y, "1.1.2")],
        )
        # X is not in T3: its content is empty and it is filtered; Y must keep Y's identity at e1
        _, args = _hydrate_check(
            catalog, [X, Y], {Y: {"content": "second body"}},
            source=_source([X, Y], tumblers=["1.1.1", "1.1.2"], collections=["knowledge__other", COLL]),
        )
        (rec,) = args["_evidence_records"]
        assert (rec.item_id, rec.tumbler, rec.chash, rec.collection) == ("e1", "1.1.2", Y, COLL)

    def test_no_source_output_still_records_chash_and_the_single_collection(self) -> None:
        catalog = _CountingCatalog([_plain("1.1.1")], [_position(X, "1.1.1")])
        _, args = _hydrate_check(catalog, [X], {X: {"content": "body"}})
        (rec,) = args["_evidence_records"]
        assert (rec.chash, rec.collection) == (X, COLL)
        assert rec.tumbler == "1.1.1"  # the owner set shows one owner

    def test_a_source_that_does_not_line_up_with_the_ids_is_ignored(self) -> None:
        catalog = _CountingCatalog([_plain("1.1.1")], [_position(X, "1.1.1")], positions_fail=True)
        _, args = _hydrate_check(
            catalog, [X], {X: {"content": "body"}},
            source=_source([Y], tumblers=["1.1.9"], collections=["knowledge__other"]),
        )
        (rec,) = args["_evidence_records"]
        assert (rec.tumbler, rec.collection) == (None, COLL)

    def test_the_private_keys_never_reach_the_dispatched_prompt_args(self) -> None:
        catalog = _CountingCatalog([_plain("1.1.1")], [_position(X, "1.1.1")])
        _, args = _hydrate_check(catalog, [X], {X: {"content": "body"}}, source=_source([X]))
        assert "_evidence_source" not in args
        assert "_evidence_records" in args  # popped by the dispatcher / bundle builder, not leaked into them


class TestMergedItems:
    def test_a_supplement_item_gets_a_record_from_its_provenance_and_ids_follow_record_order(self) -> None:
        catalog = _CountingCatalog([_plain("1.1.1")], [_position(X, "1.1.1")])
        supp = RepairSupplement(items=(SupplementItem(text="[source: s]\nrepair body", chash=Z, collection=COLL, tumbler="1.1.7"),))
        with supplement_scope(supp):
            _, args = _hydrate_check(catalog, [X], {X: {"content": "body"}}, source=_source([X], tumblers=["1.1.1"]))
        recs = args["_evidence_records"]
        assert [r.item_id for r in recs] == ["e1", "e2"]
        assert (recs[0].text, recs[0].tumbler, recs[0].chash) == ("repair body", "1.1.7", Z)
        assert (recs[1].text, recs[1].tumbler, recs[1].chash) == ("body", "1.1.1", X)

    def test_the_coverage_note_and_unknown_items_get_identity_less_records(self) -> None:
        catalog = _CountingCatalog([_plain("1.1.1")], [_position(X, "1.1.1")])
        supp = RepairSupplement(items=(), note="NOTE: unresolved facets")
        with supplement_scope(supp):
            _, args = _hydrate_check(catalog, [X], {X: {"content": "body"}}, source=_source([X], tumblers=["1.1.1"]))
        note_rec = args["_evidence_records"][0]
        assert note_rec == EvidenceRecord("e1", "NOTE: unresolved facets")


class TestExplicitItemLists:
    def test_a_list_of_texts_gets_identityless_records_and_the_id_text_rendering(self) -> None:
        _, args = runner._hydrate_operator_args("check", {"items": ["alpha text", "beta text"], "check_instruction": "c"})
        assert [(r.item_id, r.text, r.tumbler, r.chash, r.collection) for r in args["_evidence_records"]] == [
            ("e1", "alpha text", None, None, None), ("e2", "beta text", None, None, None),
        ]
        assert _shown(args) == [{"id": "e1", "text": "alpha text"}, {"id": "e2", "text": "beta text"}]

    def test_a_deferred_bundle_ref_in_the_items_means_no_records(self) -> None:
        ref = {runner._DEFERRED_REF_KEY: True, "step_index": 0, "field": "extractions"}
        _, args = runner._hydrate_operator_args("check", {"items": [ref], "check_instruction": "c"})
        assert "_evidence_records" not in args

    def test_other_operators_are_untouched(self) -> None:
        _, args = runner._hydrate_operator_args("rank", {"items": ["a", "b"], "criterion": "c"})
        assert json.loads(args["items"]) == ["a", "b"]
        assert "_evidence_records" not in args


class TestEvidenceSourcePlumbing:
    def test_a_step_ids_reference_carries_the_source_arrays_for_check_only(self) -> None:
        out = {"ids": [X], "tumblers": ["1.1.1"], "chunk_collections": [COLL], "chunk_text_hash": [X], "collections": [COLL]}
        raw = {"ids": "$step1.ids", "collections": "$step1.collections", "check_instruction": "c"}
        resolved = runner._resolve_args(raw, bindings={}, step_outputs=[out])
        got = runner._with_evidence_source("check", raw, resolved, [out])
        assert got["_evidence_source"] == {"ids": [X], "tumblers": ["1.1.1"], "chunk_collections": [COLL], "chunk_text_hash": [X]}
        assert "_evidence_source" not in runner._with_evidence_source("summarize", raw, resolved, [out])
        assert "_evidence_source" not in runner._with_evidence_source("check", {"ids": [X]}, {"ids": [X]}, [out])

    def test_a_list_of_id_references_concatenates_sources_in_order(self) -> None:
        o1 = {"ids": [X], "tumblers": ["1.1.1"], "chunk_collections": [COLL], "chunk_text_hash": [X]}
        o2 = {"ids": [Y], "tumblers": ["1.1.2"], "chunk_collections": ["knowledge__b"], "chunk_text_hash": [Y]}
        raw = {"ids": ["$step1.ids", "$step2.ids"]}
        resolved = runner._resolve_args(raw, bindings={}, step_outputs=[o1, o2])
        got = runner._with_evidence_source("operator_check", raw, resolved, [o1, o2])["_evidence_source"]
        assert got["ids"] == [X, Y] and got["tumblers"] == ["1.1.1", "1.1.2"] and got["chunk_collections"] == [COLL, "knowledge__b"]


# ── the dispatcher: bind once, with the runner's records ───────────────────────────────────────────────


def _rows_catalog():
    return _CountingCatalog([_plain("1.1.1")], [_position(X, "1.1.1")])


@pytest.mark.asyncio
async def test_default_dispatcher_binds_the_check_reply_once_with_the_runner_records() -> None:
    seen_prompts: list[str] = []

    async def fake_dispatch(prompt, schema, **kw):
        seen_prompts.append(prompt)
        return {"ok": True, "evidence": [
            {"item_id": "e1", "quote": "alpha beta", "role": "supports", "tumbler": "9.9.9"},
            {"item_id": "e9", "quote": "alpha", "role": "supports"},
        ]}

    catalog = _rows_catalog()
    with patch.object(dmod, "claude_dispatch", fake_dispatch), patch("nexus.mcp_infra.get_catalog", return_value=catalog), patch(
        "nexus.mcp.core._get_catalog", return_value=catalog,
    ), patch("nexus.mcp.core._get_t3", return_value=_t3({X: {"content": "the alpha beta text"}})), patch(
        "nexus.mcp.core._read_scope", return_value=[COLL],
    ):
        result = await runner._default_dispatcher("check", {
            "ids": [X], "collections": COLL, "check_instruction": "c", "_evidence_source": _source([X], tumblers=["1.1.1"]),
        })
    assert result["schema_version"] == 1
    (item,) = result["evidence"]
    assert (item["state"], item["tumbler"], item["chash"], item["collection"]) == ("bound", "1.1.1", X, COLL)
    assert result["rejected"] == ["e9"]
    assert result["requires_review"] is True
    # the model saw ids and body text, and none of the private keys
    assert '"id": "e1"' in seen_prompts[0] and "_evidence" not in seen_prompts[0]


@pytest.mark.asyncio
async def test_default_dispatcher_merges_truncation_metadata_onto_the_bound_payload() -> None:
    async def fake_dispatch(prompt, schema, **kw):
        return {"ok": True, "evidence": []}

    n = runner._OPERATOR_MAX_INPUTS + 2
    ids = [f"{i:064x}" for i in range(n)]
    rows = {i: {"content": f"body {k}"} for k, i in enumerate(ids)}
    catalog = _CountingCatalog([], [])
    with patch.object(dmod, "claude_dispatch", fake_dispatch), patch("nexus.mcp_infra.get_catalog", return_value=catalog), patch(
        "nexus.mcp.core._get_catalog", return_value=catalog,
    ), patch("nexus.mcp.core._get_t3", return_value=_t3(rows)), patch("nexus.mcp.core._read_scope", return_value=[COLL]):
        result = await runner._default_dispatcher("check", {"ids": ids, "collections": COLL, "check_instruction": "c"})
    assert result["schema_version"] == 1 and result["truncated"] is True and result["kept_count"] == runner._OPERATOR_MAX_INPUTS
    # the cap dropped the tail; the records the model saw are the kept ones
    assert result["uncovered"][0] == "e1" and len(result["uncovered"]) == runner._OPERATOR_MAX_INPUTS


# ── plan_run end to end: isolated and bundled return the same schema as a direct call ──────────────────

BODY_X = "The model reaches 91.2 percent top-1 accuracy on the held-out set."
BODY_Y = "Latency drops to 12 ms per request under batching."
MODEL_REPLY = {"ok": True, "evidence": [
    {"item_id": "e1", "quote": "91.2 percent top-1", "role": "supports"},
    {"item_id": "e2", "quote": "12 ms per request", "role": "supports"},
]}


def _search_stub(inferred: bool = False):
    async def search(**kw):
        return {
            "ids": [X, Y], "tumblers": ["1.1.1", "1.1.2"], "distances": [0.1, 0.2], "collections": [COLL],
            "chunk_collections": [COLL, COLL], "chunk_text_hash": [X, Y],
        }
    return search


def _plan_match(steps):
    return Match(
        plan_id=1, name="witness", description="d", confidence=0.9, dimensions={"verb": "check"}, tags="",
        plan_json=json.dumps({"steps": steps}), required_bindings=[], optional_bindings=[],
        default_bindings={}, parent_dims=None,
    )


CHECK_PLAN = [
    {"tool": "search", "args": {"query": "q", "corpus": "knowledge"}},
    {"tool": "check", "args": {"ids": "$step1.ids", "collections": "$step1.collections", "check_instruction": "do they hold"}},
]


def _env(catalog, rows):
    stack = ExitStack()
    stack.enter_context(patch("nexus.mcp_infra.get_catalog", return_value=catalog))
    stack.enter_context(patch("nexus.mcp.core._get_catalog", return_value=catalog))
    stack.enter_context(patch("nexus.mcp.core._get_t3", return_value=_t3(rows)))
    stack.enter_context(patch("nexus.mcp.core._read_scope", return_value=[COLL]))
    stack.enter_context(patch("nexus.mcp.core.search", _search_stub()))
    return stack


def _owners():
    return _CountingCatalog([_plain("1.1.1"), _plain("1.1.2")], [_position(X, "1.1.1"), _position(Y, "1.1.2")])


@pytest.mark.asyncio
async def test_isolated_plan_step_returns_bound_witnesses_with_host_identity() -> None:
    async def fake_dispatch(prompt, schema, **kw):
        return json.loads(json.dumps(MODEL_REPLY))

    with _env(_owners(), {X: {"content": BODY_X}, Y: {"content": BODY_Y}}), patch.object(dmod, "claude_dispatch", fake_dispatch):
        result = await runner.plan_run(_plan_match(CHECK_PLAN), {}, bundle_operators=False)
    out = result.steps[-1]
    assert out["schema_version"] == 1
    assert [(i["item_id"], i["state"], i["tumbler"], i["chash"], i["collection"]) for i in out["evidence"]] == [
        ("e1", "bound", "1.1.1", X, COLL), ("e2", "bound", "1.1.2", Y, COLL),
    ]
    assert out["requires_review"] is False and out["rejected"] == [] and out["uncovered"] == []


@pytest.mark.asyncio
async def test_a_wrong_document_quote_on_the_runner_path_is_ambiguous_with_null_source() -> None:
    reply = {"ok": True, "evidence": [
        {"item_id": "e1", "quote": "12 ms per request", "role": "supports"},   # e2's text cited under e1
        {"item_id": "e2", "quote": "12 ms per request", "role": "supports"},
    ]}

    async def fake_dispatch(prompt, schema, **kw):
        return json.loads(json.dumps(reply))

    with _env(_owners(), {X: {"content": BODY_X}, Y: {"content": BODY_Y}}), patch.object(dmod, "claude_dispatch", fake_dispatch):
        result = await runner.plan_run(_plan_match(CHECK_PLAN), {}, bundle_operators=False)
    first, second = result.steps[-1]["evidence"]
    assert (first["state"], first["span"], first["tumbler"], first["chash"]) == ("ambiguous", None, None, None)
    assert second["state"] == "bound"
    assert result.steps[-1]["requires_review"] is True


@pytest.mark.asyncio
async def test_bundled_terminal_check_is_bound_like_the_isolated_one_when_its_evidence_is_host_hydrated() -> None:
    """A two-operator bundle whose terminal check hydrates its own ids: the same schema as the isolated step."""
    plan = [
        *CHECK_PLAN[:1],
        {"tool": "rank", "args": {"ids": "$step1.ids", "collections": "$step1.collections", "criterion": "relevance"}},
        CHECK_PLAN[1],
    ]
    prompts: list[str] = []

    async def fake_bundle(bundle, **kw):
        prompts.append(bundle_mod.compose_bundle_prompt(bundle)[0])
        return json.loads(json.dumps(MODEL_REPLY))

    async def fail_dispatch(*a, **kw):
        raise AssertionError("the bundle must be one dispatch")

    with _env(_owners(), {X: {"content": BODY_X}, Y: {"content": BODY_Y}}), \
         patch("nexus.plans.bundle.dispatch_bundle", fake_bundle), patch.object(dmod, "claude_dispatch", fail_dispatch):
        result = await runner.plan_run(_plan_match(plan), {}, bundle_operators=True)
    out = result.steps[-1]
    assert out["schema_version"] == 1
    assert [(i["state"], i["tumbler"], i["chash"], i["collection"]) for i in out["evidence"]] == [
        ("bound", "1.1.1", X, COLL), ("bound", "1.1.2", Y, COLL),
    ]
    assert "_evidence_records" not in prompts[0] and '"id": "e1"' in prompts[0]


@pytest.mark.asyncio
async def test_bundled_terminal_check_over_an_earlier_steps_output_is_unresolved_and_needs_review() -> None:
    plan = [
        CHECK_PLAN[0],
        {"tool": "extract", "args": {"ids": "$step1.ids", "collections": "$step1.collections", "fields": "claim"}},
        {"tool": "check", "args": {"items": "$step2.extractions", "check_instruction": "do they hold"}},
    ]

    async def fake_bundle(bundle, **kw):
        return json.loads(json.dumps(MODEL_REPLY))

    with _env(_owners(), {X: {"content": BODY_X}, Y: {"content": BODY_Y}}), patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
        result = await runner.plan_run(_plan_match(plan), {}, bundle_operators=True)
    out = result.steps[-1]
    assert [i["state"] for i in out["evidence"]] == ["unresolved", "unresolved"]
    assert all(i["tumbler"] is None and i["chash"] is None and i["span"] is None for i in out["evidence"])
    assert out["requires_review"] is True and out["rejected"] == []


@pytest.mark.asyncio
async def test_bundled_verify_has_the_direct_call_key_set_and_is_unresolved() -> None:
    """Parity on one fixture: a bundled terminal verify returns the same v1 key set a direct call does; with
    no host records behind it every item is unresolved and the payload needs review."""
    verify_reply = {"verified": True, "reason": "r", "evidence": [{"item_id": "e1", "quote": "12 ms", "role": "supports"}]}

    async def fake_direct(prompt, schema, **kw):
        return json.loads(json.dumps(verify_reply))

    with patch.object(dmod, "claude_dispatch", fake_direct):
        direct = await operator_verify(claim="c", evidence=BODY_Y)

    plan = [
        CHECK_PLAN[0],
        {"tool": "extract", "args": {"ids": "$step1.ids", "collections": "$step1.collections", "fields": "claim"}},
        {"tool": "verify", "args": {"claim": "latency drops", "evidence": "$step2.extractions"}},
    ]

    async def fake_bundle(bundle, **kw):
        return json.loads(json.dumps(verify_reply))

    with _env(_owners(), {X: {"content": BODY_X}, Y: {"content": BODY_Y}}), patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
        result = await runner.plan_run(_plan_match(plan), {}, bundle_operators=True)
    bundled = result.steps[-1]
    assert list(bundled) == list(direct)
    assert direct["evidence"][0]["state"] == "unattributed"
    assert bundled["evidence"][0]["state"] == "unresolved" and bundled["requires_review"] is True
    assert bundled["citations"] == []
