# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-189 P1 (nexus-z1d0l.1.4) fix round: pins the review found missing.

* an explicit ``verify(evidence=$stepN.x)`` is seen by the reserved-stream marking and the boundary evidence;
* the witness dispatch scope (the two contextvars) is closed after a dispatch that raised inside it;
* the hand-kept lists of "the witness operators" agree.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

import nexus.operators.dispatch as dmod
from nexus.operators import witness
from nexus.plans import bundle as bundle_mod
from nexus.plans import runner
from nexus.plans.continuation_envelope import WITNESS_BARE_VERBS
from nexus.plans.primary_evidence import PrimaryEvidence, SelectedChunk
from nexus.plans.reserved_stream import EVIDENCE_KEYS, ReservedStream, active_reserved_stream, reserved_scope
from tests.test_6pfgb11_provenance_from_catalog import COLL, X
from tests.test_6pfgb50_runner_chash_reuses_rows import Y
from tests.test_witness_runner_records import BODY_X, BODY_Y, _env, _owners, _source


def _stream(n: int = 3) -> ReservedStream:
    chunks = tuple(
        SelectedChunk(
            source="RDR-084", doc_id="1.2.3", position=i, chash=f"{i:02x}" * 32, collection="rdr__x",
            label=f"Section {i}", label_source="content", text=f"PRIMARY TEXT {i}",
        )
        for i in range(n)
    )
    stream = ReservedStream.from_evidence(PrimaryEvidence(items=chunks))
    assert stream is not None
    return stream


# ── "evidence" is an evidence key ───────────────────────────────────────────────────────────────────────


def test_evidence_is_a_reserved_stream_evidence_key() -> None:
    assert "evidence" in EVIDENCE_KEYS


def test_the_reserved_stream_marking_records_the_producers_of_a_verifys_explicit_evidence() -> None:
    steps = [
        {"tool": "rank", "args": {"items": ["a"], "criterion": "c"}},
        {"tool": "verify", "args": {"claim": "c", "evidence": "$step1.ranked"}},
    ]
    stream = _stream()
    with reserved_scope(stream):
        runner._mark_reserved_step(1, steps)
        assert active_reserved_stream() is stream
        assert stream.current_step == 1 and stream.current_producers == ("rank",)


def test_a_reserved_item_a_rank_dropped_before_an_explicit_evidence_verify_is_counted() -> None:
    steps = [
        {"tool": "rank", "args": {"items": ["a"], "criterion": "c"}},
        {"tool": "verify", "args": {"claim": "c", "evidence": "$step1.ranked"}},
    ]
    stream = _stream(3)
    with reserved_scope(stream):
        runner._mark_reserved_step(0, steps)
        _, first = runner._hydrate_operator_args("rank", {"inputs": ["s"], "criterion": "c"})
        survivors = json.loads(first["items"])[:1]  # the rank kept one reserved item and dropped two
        runner._mark_reserved_step(1, steps)
        runner._hydrate_operator_args("verify", {"claim": "c", "evidence": json.dumps(survivors)})
    assert stream.report()["dropped_before_terminal"] == {"step": 1, "missing": 2, "of": 3}


def test_a_verify_that_reads_a_fetch_step_through_evidence_reads_retrieved_evidence() -> None:
    steps = [
        {"tool": "search", "args": {"query": "q"}},
        {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "$step1.collections"}},
        {"tool": "verify", "args": {"claim": "c", "evidence": "$step2.contents"}},
    ]
    assert runner._reads_retrieved_evidence(2, steps)


def test_the_boundary_reads_the_texts_of_an_explicit_verify_evidence_list() -> None:
    shown = json.dumps([{"id": "e1", "text": "first body"}, {"id": "e2", "text": "second body"}])
    assert runner._boundary_evidence({"claim": "c", "evidence": shown}, []) == (["first body", "second body"], True)


# ── the witness dispatch scope closes when the dispatch raises ──────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["check", "verify"])
async def test_the_witness_scope_is_closed_after_a_dispatch_that_raised_inside_it(verb: str) -> None:
    inside: list[tuple[bool, bool]] = []

    async def failing_dispatch(prompt: str, schema: Any, **kw: Any) -> Any:
        inside.append((witness.binding_deferred(), witness.evidence_prerendered()))
        raise RuntimeError("model unavailable")

    args = {"ids": [X, Y], "collections": COLL, "_evidence_source": _source([X, Y], tumblers=["1.1.1", "1.1.2"])}
    args.update({"claim": "latency drops"} if verb == "verify" else {"check_instruction": "c"})
    assert (witness.binding_deferred(), witness.evidence_prerendered()) == (False, False)
    with _env(_owners(), {X: {"content": BODY_X}, Y: {"content": BODY_Y}}), patch.object(dmod, "claude_dispatch", failing_dispatch):
        with pytest.raises(Exception):  # noqa: B017, PT011 — whatever the dispatcher wraps the model failure in
            await runner._default_dispatcher(verb, args)
    # non-vacuous: the dispatch ran inside the scope (binding held by the runner; a verify's evidence prerendered)
    assert inside == [(True, verb == "verify")]
    assert (witness.binding_deferred(), witness.evidence_prerendered()) == (False, False)


# ── the hand-kept witness lists agree ───────────────────────────────────────────────────────────────────


def test_every_list_of_the_witness_operators_names_the_same_two_verbs() -> None:
    """``bundle.WITNESS_OPERATORS``, ``continuation_envelope.WITNESS_BARE_VERBS``, ``runner._WITNESS_TOOLS`` and the
    verbs ``witness.bind`` binds are kept by hand. A third witness operator added to one of them goes red here."""
    bare = {v.removeprefix("operator_") for v in bundle_mod.WITNESS_OPERATORS}
    assert bare == {"check", "verify"}
    assert bundle_mod.WITNESS_OPERATORS == bare | {f"operator_{v}" for v in bare}, "both spellings, nothing else"
    assert set(WITNESS_BARE_VERBS) == bare
    assert set(runner._WITNESS_TOOLS) == {f"operator_{v}" for v in bare}
    assert all(bundle_mod.is_witness_tool(t) for t in runner._WITNESS_TOOLS)
    # witness.bind: each of the verbs yields its own v1 payload shape (check: uncovered; verify: citations)
    shapes = {
        verb: {k for k in witness.bind({"ok": True, "verified": True, "evidence": []}, None, verb) if k in ("uncovered", "citations")}
        for verb in sorted(bare)
    }
    assert shapes == {"check": {"uncovered"}, "verify": {"citations"}}
    for tool in runner._WITNESS_TOOLS:  # the runner binds under the operator_ spelling too
        assert witness.bind({"evidence": []}, None, tool)["schema_version"] == witness.SCHEMA_VERSION
