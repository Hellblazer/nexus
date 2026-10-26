# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jypmj.4: every evidence arg the planner is told to use reaches a reserved-stream merge site.

nexus-jypmj.1.20 happened because nothing tied ``_PLANNER_TOOL_REFERENCE`` (what the planner is told
to write) to ``runner._hydrate_operator_args`` (what the runner merges the reserved primary stream
into): the documented ``summarize(content=$stepN.contents)`` reached no merge site, and a plan-miss
run read the named source and dropped it. This census parses the operator signatures in the
reference and drives each evidence arg through the hydration seam with a stream in scope.

Delete a merge site in ``_hydrate_operator_args`` (the ``_INPUTS_TARGET`` pass-through, the extract
``inputs`` merge, or the Pattern A ``content`` / ``context`` / ``items`` site) and the matching rows
fail. Tenant-free: the seam is a pure function over an in-memory stream.
"""

from __future__ import annotations

import re

import pytest

from nexus.mcp.core import _PLANNER_TOOL_REFERENCE
from nexus.plans.evidence_diagnostics import ReducerInputRecorder, reducer_input_scope
from nexus.plans.primary_evidence import PrimaryEvidence, SelectedChunk
from nexus.plans.reserved_stream import (
    ReservedStream,
    reserved_scope,
    set_reserved_step,
)
from nexus.plans.runner import (
    _INPUTS_TARGET,
    _OPERATOR_TOOL_MAP,
    _hydrate_operator_args,
)

#: The args that carry retrieved text into an operator. Every other parsed arg must be listed in
#: ``INSTRUCTION_ARGS``: a newly documented arg that is in neither set fails the census until
#: someone says which kind it is (and, if it carries evidence, which merge site takes it).
EVIDENCE_ARGS = frozenset({"inputs", "items", "content", "context"})
INSTRUCTION_ARGS = frozenset({"fields", "criterion", "focus", "template", "cited"})

_SIGNATURE = re.compile(r"^  (\w+)\(([^)]*)\)", re.MULTILINE)


def _operator_signatures() -> dict[str, list[str]]:
    """``{operator: [arg, ...]}`` from the reference's Operators section."""
    section = _PLANNER_TOOL_REFERENCE.split("=== Operators (LLM-backed) ===", 1)[
        1
    ].split("=== Correct chain", 1)[0]
    return {
        name: [a.split("=")[0].strip() for a in params.split(",") if a.strip()]
        for name, params in _SIGNATURE.findall(section)
    }


SIGNATURES = _operator_signatures()
EVIDENCE_ROWS = sorted(
    (op, arg) for op, args in SIGNATURES.items() for arg in args if arg in EVIDENCE_ARGS
)


def _stream() -> ReservedStream:
    chunks = tuple(
        SelectedChunk(
            source="RDR-155",
            doc_id="1.2.3",
            position=i,
            chash=f"{i + 1:02x}" * 32,
            collection="rdr__x",
            label=f"Section {i}",
            label_source="content",
            text=f"PRIMARY TEXT {i}",
        )
        for i in range(2)
    )
    stream = ReservedStream.from_evidence(PrimaryEvidence(items=chunks))
    assert stream is not None
    return stream


def _reducer_input(op: str, args: dict) -> tuple[list[str], dict]:
    """Hydrate *args* for *op* with a stream in scope; return the recorded reducer input and the stream report."""
    stream, rec = _stream(), ReducerInputRecorder()
    with reducer_input_scope(rec), reserved_scope(stream):
        rec.set_step(3)
        set_reserved_step(3)
        _hydrate_operator_args(op, args)
    return list(rec.items or ()), stream.report()


def test_the_census_reads_the_reference_and_is_not_vacuous() -> None:
    assert set(SIGNATURES) >= {"extract", "rank", "compare", "summarize", "generate"}, (
        SIGNATURES
    )
    assert len(EVIDENCE_ROWS) >= 5, (
        f"the census examined {len(EVIDENCE_ROWS)} evidence args: {EVIDENCE_ROWS}"
    )
    for op in SIGNATURES:
        assert op in _OPERATOR_TOOL_MAP, (
            f"the reference documents {op!r}, which the runner cannot dispatch"
        )
        assert any(a in EVIDENCE_ARGS for a in SIGNATURES[op]), (
            f"{op}: no evidence arg parsed from {SIGNATURES[op]}"
        )


@pytest.mark.parametrize("op", sorted(SIGNATURES))
def test_every_documented_arg_is_classified(op: str) -> None:
    unclassified = [
        a for a in SIGNATURES[op] if a not in EVIDENCE_ARGS | INSTRUCTION_ARGS
    ]
    assert unclassified == [], (
        f"{op}: {unclassified} is documented to the planner but the census does not know whether it "
        "carries evidence; add it to EVIDENCE_ARGS (and make sure a merge site takes it) or INSTRUCTION_ARGS"
    )


@pytest.mark.parametrize(("op", "arg"), EVIDENCE_ROWS)
def test_an_evidence_arg_holding_fetched_texts_reaches_a_merge_site(
    op: str, arg: str
) -> None:
    """The documented shape: the arg resolves from ``$stepN.contents`` to a list of fetched texts."""
    texts = ["secondary 0", "secondary 1"]
    items, report = _reducer_input(op, {arg: list(texts)})
    assert report["merged"] is True and report["merged_at_step"] == 3, (
        f"{op}({arg}=[...]) never merged the stream"
    )
    assert any("PRIMARY TEXT 0" in i for i in items), items
    assert items[-2:] == texts, (
        f"{op}({arg}=[...]): the reducer input was not recorded after the merge"
    )


@pytest.mark.parametrize(
    "op", sorted(op for op in SIGNATURES if _OPERATOR_TOOL_MAP[op] in _INPUTS_TARGET)
)
def test_the_inputs_alias_of_a_target_operator_reaches_the_pass_through_merge(
    op: str,
) -> None:
    """A pre-hydrated pipe passes ``inputs=``; ``_INPUTS_TARGET`` renames it to the operator's own arg."""
    texts = ["secondary 0", "secondary 1"]
    items, report = _reducer_input(op, {"inputs": list(texts)})
    assert report["merged"] is True, f"{op}(inputs=[...]) never merged the stream"
    assert items[-2:] == texts and any("PRIMARY TEXT 0" in i for i in items)


def test_the_extract_inputs_arg_is_not_in_the_target_table_and_has_its_own_site() -> (
    None
):
    """Why extract needs its own row: it takes ``inputs`` as it is, so only its dedicated merge reaches it."""
    assert _OPERATOR_TOOL_MAP["extract"] not in _INPUTS_TARGET
    assert ("extract", "inputs") in EVIDENCE_ROWS
    items, report = _reducer_input(
        "extract", {"inputs": ["secondary 0"], "fields": "decision"}
    )
    assert report["merged"] is True and any("PRIMARY TEXT 0" in i for i in items)
