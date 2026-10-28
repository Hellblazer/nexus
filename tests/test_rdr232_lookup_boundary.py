# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 section 1 lookup boundary (nexus-jypmj.13): the extraction prompt, the deterministic rule and the
labelled compound-lookup set say what the RDR says.

Sam decided on 2026-10-27 that several connected facts from one passage of one source are a lookup, and that a
synthesis gathers aspects across sections, sources or history. Until this bead the extraction prompt defined
synthesis as "a unified account of several aspects" and asked for one facet per aspect, so a compound
single-passage question was read as a lookup only when the model departed from its prompt. No model call here:
the prompt text is checked against the RDR's own table, the deterministic rule is exercised on the labelled
requests, and the measurement script is dry-run.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bench import rdr232_extract_measure as em
from bench import rdr232_replay as replay
from bench.schema import load_golden_set
from nexus.plans import request_coverage as rc
from nexus.plans.request_coverage import (
    _EXTRACT_PROMPT,
    _weak_lookup,
    derive_request_coverage,
    needs_structured_extract,
)

REPO = Path(__file__).resolve().parents[1]
RDR = REPO / "docs" / "rdr" / "rdr-232-nx-answer-shape-eligibility-and-evidence-sufficiency.md"
COMPOUND = REPO / "bench" / "queries" / "golden_compound_lookup_v1.yaml"

# The two boundary phrases of RDR-232 section 1's shape table. The prompt must carry them, and so must the RDR.
LOOKUP_PHRASE = "several connected facts from one passage of one source"
SYNTHESIS_PHRASE = "across sections, sources or history"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


def _table_row(kind: str) -> str:
    # section 1 has two tables keyed by kind; the shape table is the one with three columns (kind, meaning, evidence)
    rows = [ln for ln in RDR.read_text(encoding="utf-8").splitlines()
            if ln.startswith(f"| `{kind}` |") and ln.count("|") == 4]
    assert len(rows) == 1, f"RDR-232 section 1 shape table has no single row for {kind}"
    return _norm(rows[0])


# ── the prompt ──────────────────────────────────────────────────────────────────────────────────────────


def test_the_rdr_table_still_carries_the_boundary_phrases() -> None:
    """The anchor: if Sam's wording in the RDR changes, this goes red and the prompt is re-read against it."""
    assert LOOKUP_PHRASE in _table_row("lookup")
    assert SYNTHESIS_PHRASE in _table_row("synthesis")


def test_the_extraction_prompt_states_the_rdr_boundary() -> None:
    prompt = _norm(_EXTRACT_PROMPT)
    assert LOOKUP_PHRASE in prompt, "the prompt does not say that connected facts from one passage are a lookup"
    assert SYNTHESIS_PHRASE in prompt, "the prompt does not say that a synthesis spans sections, sources or history"


def test_the_prompt_no_longer_defines_synthesis_as_any_several_aspects() -> None:
    prompt = _norm(_EXTRACT_PROMPT)
    assert "a unified account of several aspects" not in prompt
    # asking for several facts is not what makes a synthesis
    assert "the number of facets does not decide the kind" in prompt


def test_the_prompt_still_lists_every_kind_and_keeps_the_question_marker() -> None:
    prompt = _EXTRACT_PROMPT
    for kind in rc.REQUEST_KINDS:
        assert kind in prompt, kind
    assert prompt.endswith("Question:\n")


# ── the deterministic rule ──────────────────────────────────────────────────────────────────────────────


def test_weak_lookup_leaves_a_multi_facet_question_to_the_extraction() -> None:
    """The boundary is in the docstring and pinned here: the first pass cannot tell one passage from several
    sections, so a deterministic ``lookup`` for two facets would override a model reading that can. It stays
    ``unknown`` and the request is sent."""
    assert _weak_lookup("What is the default port?", 1, True) is True
    assert _weak_lookup("What is the default port and what is the bind host?", 2, True) is False
    assert _weak_lookup("What is the default port and what is the bind host?", 1, True) is False  # `and <wh>`: a second question
    doc = _norm(_weak_lookup.__doc__ or "")
    assert "one passage of one source" in doc and "extraction" in doc, "_weak_lookup does not state the boundary"


# ── the labelled compound-lookup set ────────────────────────────────────────────────────────────────────


def test_the_compound_set_is_a_loadable_golden_set_of_compound_lookups() -> None:
    queries = load_golden_set(COMPOUND)
    assert 5 <= len(queries) <= 6
    assert {q.answer_kind for q in queries} == {"lookup"}
    assert all(len(q.required_facets) >= 2 for q in queries), "a compound lookup asks for at least two facts"
    assert all(len(q.required_sources) == 1 for q in queries), "one passage of one source: one named source"
    # distinct from the other two labelled sets, so the probe adds requests rather than repeating them
    others = {
        replay.question_key(q.text)
        for name in ("golden_v1.yaml", "golden_heldout_v1.yaml")
        for q in load_golden_set(REPO / "bench" / "queries" / name)
    }
    assert not others & {replay.question_key(q.text) for q in queries}
    for q in queries:
        assert list((REPO / "docs" / "rdr").glob(f"rdr-{q.required_sources[0].split('-')[1]}-*.md")), q.qid


def test_every_compound_request_is_sent_to_the_extraction_not_settled_by_the_first_pass() -> None:
    """Non-vacuity: the set exercises the prompt. A request the first pass settled would never reach it."""
    queries = load_golden_set(COMPOUND)
    sent = []
    for q in queries:
        cov = derive_request_coverage(q.text)
        assert cov.answer_kind in ("lookup", "unknown"), (q.qid, cov.answer_kind)
        if needs_structured_extract(cov):
            sent.append(q.qid)
    assert len(sent) >= 4, f"only {sent} reach the extraction; the set no longer probes the prompt"


# ── the measurement reads the set ───────────────────────────────────────────────────────────────────────


def test_the_measurement_knows_the_compound_set_and_marks_it_in_sample(tmp_path: Path) -> None:
    assert em.SET_PATHS["compound"] == COMPOUND
    assert em.labelled_report("compound", [], {}, {})["in_sample"] is True
    assert em.labelled_report("golden", [], {}, {})["in_sample"] is True
    assert em.labelled_report("heldout", [], {}, {})["in_sample"] is False
    out = tmp_path / "compound.json"
    assert em.main(["--set", "compound", "--dry-run", "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["set"] == "compound" and report["questions"] == len(load_golden_set(COMPOUND))
    assert report["extraction"]["calls_made"] == 0 and report["dry_run"] is True
    assert "In RDR" not in out.read_text(encoding="utf-8"), "the report carries no question text"


def test_the_measurement_still_refuses_an_unknown_set() -> None:
    with pytest.raises(SystemExit):
        em.main(["--set", "nonsense", "--dry-run"])
