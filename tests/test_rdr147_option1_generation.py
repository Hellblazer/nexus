# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-147 Step 4 sub-step 4 (nexus-irndi), option 1: generation after an entered exact-key join.

RDR-232 reads a final ledger that covers no facet (``unavailable`` / ``uncovered``) as a non-answer and skips
generation. RDR-147 adds one exception under ``NX_ANSWER_RESOLUTION_REPAIR``: when the resolution branch entered and
MERGED at least one joined item, the run generates anyway, the report says ``generated_on_join``, the outcome is an
answer that never grows a plan, and the text opens with one exact-key-join notice. The ledger is not rewritten.

Each block pins one half of the rule and its controls: every case that must stay a non-answer is run next to the
case that generates, over the same fixtures, so a rule that fired everywhere (or nowhere) fails here.

Built on the harness of ``tests/test_rdr147_resolution_repair.py`` (a stub dispatcher, the real ``resolve_keys`` over
an in-memory catalog); the post-generation half is driven through the real ``nx_answer`` as
``tests/test_rdr232_coverage_wiring.py`` does.
"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from nexus.plans import switches
from nexus.plans.answer_shape import NON_ANSWER_SHAPES, AnswerShape
from nexus.plans.coverage_ledger import BoundaryView, CoverageBoundary, CoverageLedger, CoverageResult, FacetFinding
from nexus.plans.coverage_outcome import (
    NO_USABLE_ANSWER_REASON,
    decide_outcome,
    has_merged_join,
    render_exact_key_join_notice,
    render_insufficient_evidence,
)
from nexus.plans.coverage_repair import RepairReport
from nexus.plans.repair_stream import SupplementItem
from tests.test_rdr147_resolution_repair import (  # noqa: F401 — the autouse fixtures are the harness
    FakeCatalog,
    ResolveDispatcher,
    _anchor_text,
    _entity_config,
    _hooked_view,
    _no_catalog_lookup,
    _record_text,
    _simple,
    _tok,
    _wire,
    on,
)
from tests.test_rdr232_coverage_ledger import _cov, _request
from tests.test_rdr232_coverage_repair import _run, by_topic
from tests.test_rdr232_coverage_wiring import _drive, _match
from tests.test_rdr232_coverage_wiring import _coverage_opted_in  # noqa: F401 — the wiring tests' autouse opt-in

#: No facet's topic word appears in any anchor or joined item: every facet stays unsupported after the re-check, so
#: the FINAL ledger is ``unavailable`` / ``uncovered`` (state_for: every facet known, none usable).
NOTHING = by_topic(topic={"f1": "zz-no-such-word-1", "f2": "zz-no-such-word-2", "f3": "zz-no-such-word-3"})


def _uncovered_simple(n: int = 3, **kw: Any) -> ResolveDispatcher:
    tokens = [_tok(i) for i in range(1, n + 1)]
    return ResolveDispatcher([_anchor_text(i, t) for i, t in enumerate(tokens, 1)], FakeCatalog(tokens), check=NOTHING, **kw)


def _generations(d: ResolveDispatcher) -> int:
    return d.tools.count("generate")


# ── the boundary: an entered join with an uncovered final ledger generates ────────────────────────────────────


@pytest.mark.asyncio
async def test_an_entered_join_with_an_uncovered_final_ledger_generates_and_the_ledger_is_left_as_the_recheck_found_it() -> None:
    d = _uncovered_simple(3)
    cov = _cov(await _run(d, on()))
    r = cov.repair
    assert (r.resolution_entered, r.joined_added, r.outcome) == (True, 3, "completed")
    assert (cov.state, cov.reason) == ("unavailable", "uncovered"), "the final coverage state stays as the re-check left it"
    assert [f.state for f in cov.ledger.findings] == ["unsupported"] * 3, "no facet state is rewritten"
    assert cov.skip_generation is False, "(1) generation is not skipped"
    assert _generations(d) == 1, "the terminal reduction ran"
    assert r.generated_on_join is True, "(2) the report says so"
    assert r.joined_keys == (_tok(1), _tok(2), _tok(3))
    assert cov.counts()["skip_generation"] is False


@pytest.mark.asyncio
async def test_the_wire_carries_generated_on_join_true_and_the_merged_keys() -> None:
    cov = _cov(await _run(_uncovered_simple(2), on()))
    wire = cov.to_wire()["repair"]
    assert wire["generated_on_join"] is True
    assert wire["joined_keys"] == [_tok(1), _tok(2)] and wire["joined_keys_truncated"] == 0
    assert cov.to_wire()["skip_generation"] is False


@pytest.mark.asyncio
async def test_a_covered_answer_from_an_entered_join_carries_no_generated_on_join() -> None:
    """Control: the same run with a check the joined items DO cover (CHECK: f3 by "Resolved") is complete; the wire has
    no ``generated_on_join`` key and the report no flag, and no notice is rendered for it."""
    d = _simple(3)
    cov = _cov(await _run(d, on()))
    assert cov.state == "complete" and cov.repair.joined_added == 3
    assert cov.repair.generated_on_join is False
    assert "generated_on_join" not in cov.to_wire()["repair"] and "generated_on_join" not in _wire(cov.repair)


# ── the cases that stay a non-answer, each next to the run that generates ─────────────────────────────────────


@pytest.mark.asyncio
async def test_the_branch_did_not_enter_so_an_uncovered_ledger_still_skips_generation() -> None:
    """Anchors that name no key: ``no_key_tokens``, the branch does not enter, RDR-232's rule stands."""
    d = ResolveDispatcher([f"decision message {i} with no customer id" for i in (1, 2, 3)], FakeCatalog([]), check=NOTHING)
    cov = _cov(await _run(d, on()))
    assert cov.repair.resolution_entered is False and cov.repair.resolution_reason == "no_key_tokens"
    assert (cov.state, cov.reason, cov.skip_generation) == ("unavailable", "uncovered", True)
    assert _generations(d) == 0 and cov.repair.generated_on_join is False
    assert "generated_on_join" not in cov.to_wire()["repair"]


@pytest.mark.asyncio
async def test_a_confined_run_still_skips_generation_on_an_uncovered_ledger() -> None:
    confinement = SimpleNamespace(contains=lambda key: True, sends_filter=False, allow_list=lambda: [])
    d = _uncovered_simple(2)
    cov = _cov(await _run(d, on(confinement=confinement)))
    assert cov.repair.resolution_reason == "confined_run" and cov.repair.resolution_entered is False
    assert (cov.state, cov.skip_generation) == ("unavailable", True) and _generations(d) == 0
    assert cov.repair.generated_on_join is False


@pytest.mark.asyncio
async def test_a_branch_that_entered_but_merged_nothing_still_skips_generation() -> None:
    """Every key is missing from the catalog: the branch entered, no joined item exists to merge."""
    d = ResolveDispatcher([_anchor_text(i, _tok(i)) for i in (1, 2)], FakeCatalog([]), check=NOTHING)
    cov = _cov(await _run(d, on()))
    assert cov.repair.resolution_entered is True and cov.repair.joined_added == 0
    assert (cov.state, cov.skip_generation) == ("unavailable", True) and _generations(d) == 0
    assert cov.repair.generated_on_join is False


@pytest.mark.asyncio
async def test_a_join_the_reduction_input_did_not_take_is_not_a_merge_and_still_skips_generation() -> None:
    """Built two items, the runner's input held none (``not_merged``): ``joined_added`` is 0, nothing to generate on."""
    d = _uncovered_simple(2)
    cov = await on().run(await _hooked_view(d, hooks=True, merges=False))
    assert cov.repair.outcome == "not_merged" and cov.repair.joined_added == 0
    assert cov.skip_generation is True and cov.repair.generated_on_join is False


@pytest.mark.asyncio
async def test_with_the_switch_off_an_uncovered_ledger_is_a_non_answer_and_the_wire_has_no_new_key() -> None:
    d = _uncovered_simple(3)
    cov = _cov(await _run(d, CoverageBoundary(request=_request(3))))
    assert cov.repair.resolution_entered is False
    assert (cov.state, cov.skip_generation) == ("unavailable", True) and _generations(d) == 0
    wire = cov.to_wire()["repair"]
    assert "generated_on_join" not in wire and "joined_keys" not in wire
    assert wire == {k: v for k, v in wire.items() if not k.startswith("resolution") and not k.startswith("joined")}, "the switch off keeps the wire form"


@pytest.mark.asyncio
async def test_the_unread_tail_only_repair_does_not_generate_on_join() -> None:
    """A repair that never entered the resolution branch (``resolution_entered`` False) cannot carry the flag even
    with a merged-looking ``joined_added``: the flag needs the entered branch."""
    d = _uncovered_simple(2)
    cov = _cov(await _run(d, on()))
    forged = replace(cov, skip_generation=True, repair=replace(cov.repair, resolution_entered=False, generated_on_join=False))
    assert forged.repair.generated_on_join is False and forged.skip_generation is True


# ── which keys the notice names: the merged items', not every resolved key ────────────────────────────────────


@pytest.mark.asyncio
async def test_joined_keys_are_those_of_the_merged_items_not_of_every_resolved_key() -> None:
    d = _uncovered_simple(3)
    first = await _run(d, CoverageBoundary(request=_request(3), max_repair_queries=0))
    texts = [a.content for a in _cov(first).ledger.anchors]
    installed: list[Any] = []

    async def check(args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        return await d("operator_check", args)

    async def retrieve(tool: str, args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        return await d(tool, args)

    async def fetch(hits: list[Any], *, timeout: float) -> list[Any]:  # noqa: ARG001
        return [SupplementItem(text=d.by_hash[h.id], chash=h.id, collection=h.collection, tumbler=h.tumbler) for h in hits]

    def rehydrate() -> tuple[list[str], bool]:
        # the runner's input takes the FIRST joined item only
        return [*texts, *[i.text for i in installed[:1]]], True

    view = BoundaryView(
        evidence_texts=texts, observed=True, step_outputs=list(first.steps),
        tools=["search", "traverse", "store_get_many", "generate"], scope_diagnostics=[], resolved_args=[],
        check=check, remaining_usd=lambda: None, remaining_seconds=lambda: None, retrieve=retrieve, fetch=fetch,
        install_supplement=lambda items: installed.__setitem__(slice(None), list(items)), rehydrate=rehydrate,
    )
    cov = await on().run(view)
    r = cov.repair
    assert r.resolved == (_tok(1), _tok(2), _tok(3)) and r.items_found == 3
    assert r.joined_added == 1 and r.joined_keys == (_tok(1),), "one item merged: its key only, not the three resolved"
    assert r.generated_on_join is True and cov.skip_generation is False


@pytest.mark.asyncio
async def test_two_merged_items_of_one_key_name_the_key_once() -> None:
    tokens = [_tok(1)]
    d = ResolveDispatcher([_anchor_text(1, tokens[0]), _anchor_text(2, tokens[0])], FakeCatalog(tokens), check=NOTHING)
    cov = _cov(await _run(d, on()))
    assert cov.repair.joined_added == 2 and cov.repair.joined_keys == (_tok(1),)


# ── the wire form, unit level ─────────────────────────────────────────────────────────────────────────────────


def test_to_wire_carries_generated_on_join_only_when_true() -> None:
    quiet = RepairReport("completed", ("f3",), resolution_entered=True, joined_added=2, joined_keys=("CUST-0001",))
    assert "generated_on_join" not in quiet.to_wire(), "an entered join that covered carries no flag"
    flagged = replace(quiet, generated_on_join=True).to_wire()
    assert flagged["generated_on_join"] is True and flagged["joined_keys"] == ["CUST-0001"]
    assert "generated_on_join" not in RepairReport("completed", ("f3",)).to_wire()
    assert "joined_keys" not in RepairReport("completed", ("f3",)).to_wire()


# ── the run summary ───────────────────────────────────────────────────────────────────────────────────────────


def test_the_run_summarys_resolution_counts_include_generated_on_join_and_no_key() -> None:
    from tests.test_rdr232_run_summary import _build, _coverage  # noqa: PLC0415 — the summary tests' builders

    base = _coverage("unavailable", rounds=2)
    report = RepairReport(
        "completed", ("f2",), 0, 3, 2, 10, "", resolution_entered=True, resolved=("CUST-0001",), joined_added=2,
        joined_keys=("CUST-0001",), generated_on_join=True)
    block = _build(coverage=replace(base, repair=report))["repair"]
    assert block["resolution_generated_on_join"] is True and block["resolution_items_merged"] == 2
    assert "CUST-0001" not in json.dumps(block), "counts only, no key"
    plain = _build(coverage=replace(base, repair=replace(report, generated_on_join=False)))["repair"]
    assert plain["resolution_generated_on_join"] is False
    assert "resolution_generated_on_join" not in _build(coverage=base)["repair"], "the switch off keeps today's summary"


# ── decide_outcome ────────────────────────────────────────────────────────────────────────────────────────────


def _uncovered(*, join: bool, entered: bool = True, merged: int = 2) -> CoverageResult:
    findings = (FacetFinding("f1", "unsupported", (), "no_supporting_evidence"), FacetFinding("f2", "unsupported", (), "no_supporting_evidence"))
    repair = RepairReport(
        "completed", ("f1", "f2"), resolution_entered=entered, joined_added=merged,
        joined_keys=("CUST-0001", "CUST-0002") if join else (), generated_on_join=join)
    return CoverageResult(
        state="unavailable", reason="uncovered", skip_generation=not join,
        ledger=CoverageLedger(findings=findings), repair=repair)


def test_an_uncovered_ledger_with_generated_on_join_and_answered_prose_is_an_answer_that_never_grows() -> None:
    cov = _uncovered(join=True)
    v = decide_outcome(cov, AnswerShape.ANSWERED)
    assert v.shape is AnswerShape.ANSWERED and v.shape not in NON_ANSWER_SHAPES
    assert v.success is True, "(3) counts as an answer"
    assert v.no_grow is True, "an answer the coverage check did not confirm never grows a plan"
    assert v.coverage is cov, "(5) the final coverage state and facet states are what the re-check left"
    assert (v.coverage.state, v.coverage.reason) == ("unavailable", "uncovered")
    assert [f.state for f in v.coverage.ledger.findings] == ["unsupported", "unsupported"]


@pytest.mark.parametrize("shape", [s for s in AnswerShape if s is not AnswerShape.ANSWERED and s in NON_ANSWER_SHAPES])
def test_generated_on_join_whose_prose_is_not_an_answer_gets_the_existing_no_usable_answer_downgrade(shape: AnswerShape) -> None:
    """Generation ran, and returned an empty string, a refusal or a payload: the rule every other row has. The classified
    shape stands, the counter is a failure, the plan never grows, and the coverage becomes unavailable / no_usable_answer."""
    v = decide_outcome(_uncovered(join=True), shape)
    assert v.shape is shape and v.success is False and v.no_grow is True
    assert v.coverage is not None and (v.coverage.state, v.coverage.reason) == ("unavailable", NO_USABLE_ANSWER_REASON)
    control = decide_outcome(replace(_uncovered(join=True), state="partial", reason="unresolved_facets"), shape)
    assert (control.shape, control.success, control.no_grow) == (v.shape, v.success, v.no_grow), "the same verdict a partial ledger gets"


def test_an_uncovered_ledger_without_generated_on_join_is_the_non_answer_it_was() -> None:
    v = decide_outcome(_uncovered(join=False), AnswerShape.ANSWERED)
    assert v.shape is AnswerShape.UNCOVERED and v.success is False and v.no_grow is True
    assert (v.coverage.state, v.coverage.reason) == ("unavailable", "uncovered")  # type: ignore[union-attr]


def test_a_no_evidence_ledger_never_becomes_an_answer_whatever_the_flag() -> None:
    cov = replace(_uncovered(join=True), reason="no_evidence")
    v = decide_outcome(cov, AnswerShape.ANSWERED)
    assert v.shape is AnswerShape.NO_EVIDENCE and v.success is False


def test_a_complete_ledger_is_answered_and_grows_as_before() -> None:
    cov = CoverageResult(
        state="complete", reason="all_covered", skip_generation=False,
        ledger=CoverageLedger(findings=(FacetFinding("f1", "covered", ("e1",)),)),
        repair=RepairReport("completed", resolution_entered=True, joined_added=1, joined_keys=("CUST-0001",)))
    v = decide_outcome(cov, AnswerShape.ANSWERED)
    assert (v.shape, v.success, v.no_grow) == (AnswerShape.ANSWERED, True, False)


# ── the notice ────────────────────────────────────────────────────────────────────────────────────────────────


def test_the_notice_is_one_line_with_the_merged_keys_then_a_blank_line_then_the_prose() -> None:
    text = render_exact_key_join_notice(_uncovered(join=True), "The record says X.")
    first, blank, rest = text.split("\n", 2)
    assert first == "[exact-key join: 2 record(s) joined by exact key (CUST-0001, CUST-0002); the coverage check did not confirm this answer]"
    assert blank == "" and rest == "The record says X."
    assert text.count("[exact-key join:") == 1 and "insufficient" not in text.lower() and "unresolved" not in text


def test_the_notice_of_a_covered_or_partial_run_has_no_unconfirmed_clause() -> None:
    """The clause is for an uncovered ledger that generated because of option 1 (``generated_on_join``) and for nothing else."""
    cov = _uncovered(join=True)
    plain = replace(cov, state="complete", reason="all_covered", repair=replace(cov.repair, generated_on_join=False))
    first, blank, rest = render_exact_key_join_notice(plain, "The record says X.").split("\n", 2)
    assert first == "[exact-key join: 2 record(s) joined by exact key (CUST-0001, CUST-0002)]"
    assert blank == "" and rest == "The record says X."
    assert "did not confirm" in render_exact_key_join_notice(cov, "p")


def test_has_merged_join_needs_an_entered_branch_and_a_merged_item() -> None:
    assert has_merged_join(_uncovered(join=True)) is True
    assert has_merged_join(replace(_uncovered(join=True), repair=replace(_uncovered(join=True).repair, joined_added=0, joined_keys=()))) is False
    assert has_merged_join(_uncovered(join=False, merged=0)) is False
    assert has_merged_join(replace(_uncovered(join=True), repair=RepairReport("completed", ("f1",)))) is False
    assert has_merged_join(None) is False


def test_the_notice_names_only_the_merged_keys_and_counts_them() -> None:
    cov = _uncovered(join=True)
    cov = replace(cov, repair=replace(cov.repair, resolved=("CUST-0001", "CUST-0002", "CUST-0003"), joined_keys=("CUST-0002",), joined_added=1))
    first = render_exact_key_join_notice(cov, "p").split("\n", 1)[0]
    assert first == "[exact-key join: 1 record(s) joined by exact key (CUST-0002); the coverage check did not confirm this answer]"


def test_the_notice_counts_distinct_records_not_joined_items() -> None:
    """One record named by two anchors is two joined items and one record: ``<n>`` is the records, as ``(<keys>)`` lists them."""
    cov = _uncovered(join=True, merged=2)
    cov = replace(cov, repair=replace(cov.repair, joined_keys=("CUST-0001",)))
    assert cov.repair.joined_added == 2
    assert render_exact_key_join_notice(cov, "p").split("\n", 1)[0] == (
        "[exact-key join: 1 record(s) joined by exact key (CUST-0001); the coverage check did not confirm this answer]")


@pytest.mark.asyncio
async def test_one_record_named_by_two_anchors_is_one_record_in_the_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    _env_on(monkeypatch)
    tokens = [_tok(1)]
    d = ResolveDispatcher([_anchor_text(1, tokens[0]), _anchor_text(2, tokens[0])], FakeCatalog(tokens), check=NOTHING, answer=PROSE)
    run = await _drive(plan_id=7, dispatcher=d, request=_request(3))
    assert run.summary["repair"]["resolution_items_merged"] == 2
    assert _core(run.out["final_text"]).split("\n", 1)[0] == (
        "[exact-key join: 1 record(s) joined by exact key (CUST-0001); the coverage check did not confirm this answer]")


def test_the_notice_does_not_reuse_the_insufficient_evidence_renderer() -> None:
    cov = _uncovered(join=True)
    assert render_exact_key_join_notice(cov, "p") != render_insufficient_evidence(cov, {}, "p")


# ── through the real nx_answer ────────────────────────────────────────────────────────────────────────────────


PROSE = "Customer CUST-0001 is Acme-0001 and raised the decision."
NOTICE = "[exact-key join: 3 record(s) joined by exact key (CUST-0001, CUST-0002, CUST-0003); the coverage check did not confirm this answer]"


NOTICE_JOINED = "[exact-key join: 3 record(s) joined by exact key (CUST-0001, CUST-0002, CUST-0003)]"


def _env_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(switches.ANSWER_RESOLUTION_REPAIR_ENV, "1")


def _core(text: str) -> str:
    """*text* without the harness's trailing ``[budget warning ...]`` lines (the stub steps report no cost), which follow
    a leading notice by the tool contract and are not what these tests pin."""
    return "\n".join(line for line in text.split("\n") if not line.startswith("[budget warning")).rstrip("\n")


@pytest.mark.asyncio
async def test_nx_answer_returns_the_answer_with_the_notice_and_does_not_grow_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _env_on(monkeypatch)
    d = _uncovered_simple(3, answer=PROSE)
    run = await _drive(plan_id=0, dispatcher=d, request=_request(3))
    out = run.out
    assert out["answer_shape"] == "answered"
    assert _core(out["final_text"]) == f"{NOTICE}\n\n{PROSE}", "(4) exactly one notice line, then the prose"
    assert out["final_text"].startswith(NOTICE), "the notice leads; any warning follows it"
    assert out["final_text"].count("[exact-key join:") == 1 and "[insufficient-evidence" not in out["final_text"]
    assert _generations(d) == 1
    assert run.write["success"] is True, "(3) the plan counter records an answer"
    assert not run.grew, "(3) no_grow: an answer the coverage check did not confirm never grows a plan"
    assert run.summary["coverage_state"] == "unavailable" and run.summary["coverage"]["reason"] == "uncovered", "(5)"
    assert run.summary["repair"]["resolution_generated_on_join"] is True
    assert run.summary["repair"]["resolution_items_merged"] == 3
    assert out["coverage"]["repair"]["generated_on_join"] is True and out["coverage"]["skip_generation"] is False, "(2) on the wire"


@pytest.mark.asyncio
async def test_nx_answer_text_mode_opens_with_the_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    _env_on(monkeypatch)
    run = await _drive(plan_id=7, dispatcher=_uncovered_simple(3, answer=PROSE), request=_request(3), structured=False)
    assert isinstance(run.out, str) and run.out.startswith(NOTICE + "\n\n"), "the notice leads in text mode too; warnings follow"


@pytest.mark.asyncio
async def test_nx_answer_with_the_switch_off_is_the_uncovered_non_answer_it_was(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(switches.ANSWER_RESOLUTION_REPAIR_ENV, raising=False)
    d = _uncovered_simple(3, answer=PROSE)
    run = await _drive(plan_id=7, dispatcher=d, request=_request(3))
    assert run.out["answer_shape"] == "uncovered" and run.write["success"] is False
    assert _generations(d) == 0 and "[exact-key join:" not in str(run.out["final_text"])


@pytest.mark.asyncio
async def test_nx_answer_a_covered_entered_join_carries_the_notice_without_the_unconfirmed_clause_and_grows_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every answer from a run that merged a join says so; the clause is for an uncovered ledger only."""
    _env_on(monkeypatch)
    run = await _drive(plan_id=0, dispatcher=_simple(3, answer=PROSE), request=_request(3))
    assert run.out["answer_shape"] == "answered"
    assert _core(run.out["final_text"]) == f"{NOTICE_JOINED}\n\n{PROSE}"
    assert "did not confirm" not in run.out["final_text"]
    assert run.grew and run.write["success"] is True, "a covered answer grows as before"
    assert "generated_on_join" not in run.out["coverage"]["repair"]
    assert run.out["coverage"]["repair"]["joined_keys"] == [_tok(1), _tok(2), _tok(3)]


#: f1 and f2 have no topic word anywhere; only a joined item ("Resolved") supports f3: the final ledger is partial.
PARTIAL_CHECK = by_topic(topic={"f1": "zz-no-such-word-1", "f2": "zz-no-such-word-2", "f3": "Resolved"})


@pytest.mark.asyncio
async def test_nx_answer_a_partial_answer_from_a_merged_join_opens_with_the_join_line_then_the_insufficient_evidence_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    _env_on(monkeypatch)
    tokens = [_tok(i) for i in (1, 2, 3)]
    d = ResolveDispatcher([_anchor_text(i, t) for i, t in enumerate(tokens, 1)], FakeCatalog(tokens), check=PARTIAL_CHECK, answer=PROSE)
    run = await _drive(plan_id=0, dispatcher=d, request=_request(3))
    text = run.out["final_text"]
    assert run.out["answer_shape"] == "insufficient_evidence" and run.write["success"] is True and not run.grew
    assert text.startswith(NOTICE_JOINED + "\n\n[insufficient-evidence: 2 unresolved]\n"), "the join line first, then the existing notice"
    assert text.count("[exact-key join:") == 1 and "did not confirm" not in text
    assert _core(text).endswith("\n\n" + PROSE)
    assert run.summary["coverage_state"] == "partial" and "generated_on_join" not in run.out["coverage"]["repair"]
    # the answer-runs reader still buckets the text-only row as insufficient-evidence (no run summary to read)
    from nexus.commands.answer_runs import _row_is_insufficient_evidence  # noqa: PLC0415

    assert _row_is_insufficient_evidence({"final_text": text}) is True
    assert _row_is_insufficient_evidence({"final_text": NOTICE_JOINED + "\n\n" + PROSE}) is False, "control: a join line alone is no marker"


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["no_key_tokens", "all_missing", "switch_off"])
async def test_nx_answer_a_run_with_no_merged_join_carries_no_notice(monkeypatch: pytest.MonkeyPatch, why: str) -> None:
    """The notice needs a MERGED join: a branch that did not enter, one that entered and merged nothing, and the switch off."""
    if why == "switch_off":
        monkeypatch.delenv(switches.ANSWER_RESOLUTION_REPAIR_ENV, raising=False)
        d = _simple(3, answer=PROSE)
    else:
        _env_on(monkeypatch)
        if why == "no_key_tokens":
            d = ResolveDispatcher([f"decision message {i} with no customer id" for i in (1, 2, 3)], FakeCatalog([]), answer=PROSE)
        else:
            d = ResolveDispatcher([_anchor_text(i, _tok(i)) for i in (1, 2, 3)], FakeCatalog([]), answer=PROSE)
    run = await _drive(plan_id=7, dispatcher=d, request=_request(3))
    assert "[exact-key join:" not in str(run.out["final_text"])
    assert "generated_on_join" not in run.out["coverage"]["repair"]


# ── the redacted row ──────────────────────────────────────────────────────────────────────────────────────────


def test_a_redacted_row_of_a_generated_on_join_answer_is_not_classed_a_non_answer() -> None:
    from nexus.plans.answer_text_class import heuristic_class  # noqa: PLC0415

    def row(join: bool) -> dict[str, Any]:
        repair = {"resolution_entered": True, "resolution_generated_on_join": join}
        return {
            "final_text": "[redacted]", "question": "q", "step_count": 5,
            "run_summary": {"kind": "nx_answer", "coverage_state": "unavailable", "coverage": {"reason": "uncovered"}, "repair": repair},
        }

    assert heuristic_class(row(False)) == "non_answer/uncovered", "control: the same row without the flag"
    assert heuristic_class(row(True)) == "redacted"
    # generation returned a non-answer after the join: the reason is no_usable_answer and the row is a non-answer
    cut = row(True)
    cut["run_summary"]["coverage"] = {"reason": "no_usable_answer"}
    assert heuristic_class(cut) == "non_answer/no_usable_answer"


@pytest.mark.asyncio
async def test_a_trace_false_option1_answer_is_recorded_as_an_answer_not_a_non_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The row ``trace=False`` writes has the text redacted, so ``answer_text_class`` reads its run summary: the summary
    says ``unavailable`` (the ledger is not rewritten) and ``resolution_generated_on_join``. The recorded row must not be
    classed ``non_answer/uncovered``; the control row (same ledger, switch off) is."""
    from nexus.plans.answer_text_class import heuristic_class  # noqa: PLC0415

    _env_on(monkeypatch)
    run = await _drive(plan_id=7, dispatcher=_uncovered_simple(3, answer=PROSE), request=_request(3), trace=False)
    assert run.out["answer_shape"] == "answered" and run.write["success"] is True
    assert run.write["final_text"] == "[redacted]"
    assert run.summary["coverage_state"] == "unavailable" and run.summary["repair"]["resolution_generated_on_join"] is True
    row = {"final_text": run.write["final_text"], "question": "q", "step_count": 5, "run_summary": run.summary}
    assert heuristic_class(row) == "redacted"
    monkeypatch.delenv(switches.ANSWER_RESOLUTION_REPAIR_ENV, raising=False)
    off = await _drive(plan_id=7, dispatcher=_uncovered_simple(3, answer=PROSE), request=_request(3), trace=False)
    assert heuristic_class({"final_text": "[redacted]", "question": "q", "step_count": 5, "run_summary": off.summary}) == "non_answer/uncovered"


@pytest.mark.asyncio
async def test_a_traced_option1_answer_row_is_recorded_with_the_notice_and_classed_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.plans.answer_text_class import heuristic_class  # noqa: PLC0415

    _env_on(monkeypatch)
    run = await _drive(plan_id=7, dispatcher=_uncovered_simple(3, answer=PROSE), request=_request(3), trace=True)
    assert _core(run.write["final_text"]) == f"{NOTICE}\n\n{PROSE}" and run.write["success"] is True
    cls = heuristic_class({"final_text": run.write["final_text"], "question": "q", "step_count": 5, "run_summary": run.summary})
    assert not cls.startswith("non_answer/") and cls not in ("degenerate", "error"), cls


@pytest.mark.asyncio
async def test_nx_answer_option1_whose_generation_returns_nothing_is_a_failure_with_no_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    _env_on(monkeypatch)
    d = _uncovered_simple(3, answer="")
    run = await _drive(plan_id=0, dispatcher=d, request=_request(3))
    assert _generations(d) == 1, "generation ran"
    assert run.out["answer_shape"] == "empty" and run.write["success"] is False and not run.grew
    assert "[exact-key join:" not in str(run.out["final_text"])
    assert run.summary["coverage"]["reason"] == NO_USABLE_ANSWER_REASON and run.summary["coverage_state"] == "unavailable"


@pytest.mark.asyncio
async def test_an_option1_run_is_not_a_skipped_generation_for_the_continuation_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """core.py hands nothing off only when ``skip_generation`` is true; an option-1 run generated, so its coverage says so."""
    _env_on(monkeypatch)
    run = await _drive(plan_id=7, dispatcher=_uncovered_simple(3, answer=PROSE), request=_request(3))
    assert run.summary["coverage"]["skip_generation"] is False
    monkeypatch.delenv(switches.ANSWER_RESOLUTION_REPAIR_ENV, raising=False)
    off = await _drive(plan_id=7, dispatcher=_uncovered_simple(3, answer=PROSE), request=_request(3))
    assert off.summary["coverage"]["skip_generation"] is True, "control: the switch off skips"
