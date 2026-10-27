# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 (nexus-jypmj.3.6, gate critique S7): the published ``nx_answer`` contract sites say what the code does.

Three sites carry the answer-shape and run-summary contract outside the code that produces it: the ``nx_answer`` tool
description (the only text an MCP client reads), ``docs/cli-reference.md`` (the ``answer_shape`` paragraph, the RDR-232
envelope keys, the ``nx answer-runs`` section) and the ``nx answer-runs`` help. Each test reads the vocabulary from the
code (``AnswerShape``, ``PersistenceState``) and fails when a value the code can produce is missing from the text, so a
new shape or persistence state that skips the docs fails here rather than shipping undocumented.

The wire snapshot (``tests/test_mcp_wire_snapshot.py``) strips ``description`` and ``title`` and nx_answer's parameters
did not change, so it cannot see this change; these tests are what pins the description.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re
import typing

import pytest
from click.testing import CliRunner

from nexus.commands.answer_runs import answer_runs_cmd
from nexus.mcp import core
from nexus.plans.answer_shape import AnswerShape
from nexus.plans.run_summary import PersistenceState, RunSummaryRecorder

_REPO = pathlib.Path(__file__).resolve().parent.parent
_CORE = _REPO / "src" / "nexus" / "mcp" / "core.py"
_CLI_REFERENCE = _REPO / "docs" / "cli-reference.md"

#: The raw source text of the description (every line's indentation kept, as Python 3.12 keeps it in ``__doc__``),
#: not the dedented text 3.13 serves. The lint caps ``tool.description`` at 2048 on CI's 3.12, so the raw length is
#: the one that must stay clear of that cap; this is the headroom this bead keeps.
_RAW_DESCRIPTION_CEILING = 1950

_PERSISTENCE_STATES = typing.get_args(PersistenceState)


def _raw_description() -> str:
    tree = ast.parse(_CORE.read_text(encoding="utf-8"))
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "nx_answer"]
    assert len(fns) == 1, "non-vacuity: exactly one nx_answer definition"
    doc = fns[0].body[0]
    assert isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant) and isinstance(doc.value.value, str)
    return doc.value.value  # ast.parse does not dedent: this is the text 3.12 serves as __doc__


def _section(text: str, start: str, end: str) -> str:
    """The text from *start* to the next *end*, failing loud when either anchor is gone (a vacuous slice)."""
    assert start in text, f"anchor not found: {start!r}"
    tail = text.split(start, 1)[1]
    assert end in tail, f"end anchor not found after {start!r}: {end!r}"
    out = tail.split(end, 1)[0]
    assert len(out) > 200, f"slice after {start!r} is implausibly short"
    return out


@pytest.fixture(scope="module")
def reference() -> str:
    return _CLI_REFERENCE.read_text(encoding="utf-8")


# ── the tool description ─────────────────────────────────────────────────────────────────────────────────────


class TestToolDescription:
    def test_the_vocabulary_this_checks_is_not_empty(self) -> None:
        assert AnswerShape.INSUFFICIENT_EVIDENCE.value in {s.value for s in AnswerShape}
        assert set(_PERSISTENCE_STATES) == {"persisted", "dropped", "unsupported", "unverified", "not_recorded"}

    def test_it_no_longer_says_every_non_answered_shape_is_a_one_line_notice(self) -> None:
        raw = _raw_description()
        assert 'other than "answered"' not in raw, "insufficient_evidence is prose behind a marker, not a notice"

    def test_it_names_the_insufficient_evidence_marker_and_that_prose_follows(self) -> None:
        raw = _raw_description()
        assert "insufficient_evidence" in raw
        assert "[insufficient-evidence: <n> unresolved]" in raw
        assert re.search(r"insufficient_evidence.{0,300}(prose|answer)", raw, re.S), "must say an answer follows the marker"

    def test_it_names_the_uncovered_shape(self) -> None:
        assert "uncovered" in _raw_description()

    def test_it_names_the_run_summary_and_every_persistence_state(self) -> None:
        raw = _raw_description()
        assert "run_summary" in raw
        missing = [s for s in _PERSISTENCE_STATES if s not in raw]
        assert not missing, f"persistence states absent from the description: {missing}"

    def test_a_text_mode_degraded_answer_can_open_with_the_partial_marker(self) -> None:
        raw = _raw_description()
        text_bullet = raw[raw.rindex("ext mode"):]
        assert "[insufficient-evidence:" in text_bullet

    def test_the_description_lists_the_coverage_key_in_the_envelope(self) -> None:
        raw = " ".join(_raw_description().split())
        assert "answer_shape, coverage, run_summary" in raw

    def test_the_raw_description_leaves_headroom_under_the_harness_cut(self) -> None:
        raw = _raw_description()
        assert len(raw) <= _RAW_DESCRIPTION_CEILING, (
            f"raw description is {len(raw)} chars (3.12 keeps indentation); the harness cut is 2048. "
            "Move detail to docs/cli-reference.md."
        )

    def test_the_registered_description_is_the_docstring(self) -> None:
        registered = core.mcp._tool_manager._tools["nx_answer"].description or ""
        assert "insufficient_evidence" in registered, "the tool registry serves the docstring this file reads"


# ── docs/cli-reference.md ────────────────────────────────────────────────────────────────────────────────────


class TestCliReferenceNxAnswerContract:
    def _shape_paragraph(self, reference: str) -> str:
        return _section(reference, "The envelope also carries `answer_shape`", "Grown plans are additionally gated")

    def test_the_answer_shape_paragraph_names_every_shape_the_code_can_produce(self, reference: str) -> None:
        paragraph = self._shape_paragraph(reference)
        missing = [s.value for s in AnswerShape if s.value not in paragraph]
        assert not missing, f"answer_shape values absent from the cli-reference paragraph: {missing}"

    def test_the_paragraph_says_prose_follows_the_marker_and_no_longer_claims_a_notice_for_all(self, reference: str) -> None:
        paragraph = self._shape_paragraph(reference)
        assert "[insufficient-evidence: <n> unresolved]" in paragraph
        assert "On a non-answer `final_text` is a one-line `[non-answer: <shape>]` notice" not in paragraph, (
            "insufficient_evidence is not a non-answer; the sentence must name which shapes it covers"
        )

    def test_the_paragraph_distinguishes_uncovered_from_no_evidence(self, reference: str) -> None:
        paragraph = self._shape_paragraph(reference)
        assert re.search(r"uncovered.{0,400}no_evidence|no_evidence.{0,400}uncovered", paragraph, re.S)

    def test_the_envelope_key_count_includes_run_summary(self, reference: str) -> None:
        assert "The envelope has twelve RDR-232 keys" in reference
        assert "The envelope has eleven RDR-232 keys" not in reference

    def test_run_summary_is_documented_with_its_keys_and_every_persistence_state(self, reference: str) -> None:
        block = _section(reference, "- `run_summary` (nexus-jypmj.3.5", "- `traverse` under `nx_answer`")
        for key in ("version", "persistence", "detail", "returned_coverage_state", "persisted_coverage_state", "digest"):
            assert re.search(rf"\b{key}\b", block), key
        missing = [s for s in _PERSISTENCE_STATES if f"`{s}`" not in block]
        assert not missing, f"persistence states absent from the run_summary entry: {missing}"
        assert "HTTP 200" in block, "must say why a status code is not evidence of persistence"

    def test_the_coverage_object_is_documented_for_the_continuation_envelope(self, reference: str) -> None:
        block = _section(reference, "- `continuation.coverage` (nexus-jypmj.3.2", "- `traverse` under `nx_answer`")
        for key in ("state", "reason", "skip_generation", "facets_required", "findings", "anchors", "sources", "scope",
                    "assessment", "repair"):
            assert re.search(rf"\b{key}\b", block), key
        for state in ("complete", "partial", "unavailable", "unassessed"):
            assert f"`{state}`" in block, state
        assert "`trace=False`" in block, "must say what redaction removes from the coverage object"

    def test_the_top_level_coverage_key_is_in_the_envelope_literal_and_documented(self, reference: str) -> None:
        """RDR-232 section 4 names a top-level ``coverage`` object. Pin the dict literal that builds the envelope
        to carry it and the reference to document it, so dropping either fails here."""
        tree = ast.parse(_CORE.read_text(encoding="utf-8"))
        envelopes = [
            {k.value for k in n.keys if isinstance(k, ast.Constant)}
            for n in ast.walk(tree)
            if isinstance(n, ast.Dict) and {"answer_shape", "run_summary", "plan_eligibility"} <= {
                k.value for k in n.keys if isinstance(k, ast.Constant)
            }
        ]
        assert len(envelopes) == 1, "non-vacuity: exactly one envelope literal carries these keys"
        assert "coverage" in envelopes[0]
        assert "no top-level `coverage` key" not in reference and "no\ntop-level `coverage` key" not in reference

    def test_the_coverage_key_is_documented_with_its_fields_and_every_unassessed_reason(self, reference: str) -> None:
        block = _section(reference, "- `coverage` (nexus-jypmj.3.6", "- `continuation.coverage` (nexus-jypmj.3.2")
        for key in ("requested_facet_ids", "covered_facet_ids", "residual_gaps", "primary_source", "corpus_counts", "repair"):
            assert f"`{key}`" in block, key
        for reason in ("switch_off", "no_facets", "terminal_reduction_not_reached", "boundary_not_run"):
            assert f"`{reason}`" in block, reason
        assert "`trace=False`" in block and "no evidence text" in block
        assert "insufficient_evidence" in block, "must say a partial state is never answered"

    def test_the_documented_unassessed_reasons_are_the_ones_the_code_emits(self) -> None:
        recorder_src = inspect.getsource(RunSummaryRecorder.coverage_wire)
        emitted = set(re.findall(r'"([a-z_]+)"', recorder_src.split("reason = (", 1)[1].split(")\n", 1)[0]))
        assert emitted == {"switch_off", "no_facets", "terminal_reduction_not_reached", "boundary_not_run"}


class TestCliReferenceAnswerRuns:
    def _section(self, reference: str) -> str:
        return _section(reference, "## nx answer-runs", "### `--derive-budget`")

    def test_the_insufficient_evidence_bucket_is_documented(self, reference: str) -> None:
        text = self._section(reference)
        assert "insufficient-evidence" in text
        assert "insufficient_evidence_count" in text
        assert "`coverage_partial`" in text, "must say the bucket is read from the run summary"

    def test_the_trace_false_without_a_summary_fallback_is_documented(self, reference: str) -> None:
        text = self._section(reference)
        assert re.search(r"`trace=False`[^.]*without a (run )?summary", text), "the no-summary trace=False fallback"
        assert "stays redacted" in text or "not recognizable" in text

    def test_the_coverage_non_answers_are_named_as_degenerate_classes(self, reference: str) -> None:
        text = self._section(reference)
        assert "`uncovered`" in text and "`no_evidence`" in text


class TestAnswerRunsHelp:
    def test_the_help_names_the_bucket_its_json_key_and_the_fallback(self) -> None:
        result = CliRunner().invoke(answer_runs_cmd, ["--help"])
        assert result.exit_code == 0
        flat = " ".join(result.output.split())
        assert "insufficient-evidence" in flat
        assert "insufficient_evidence_count" in flat
        assert "redacted" in flat
        assert "uncovered" in flat and "no_evidence" in flat
