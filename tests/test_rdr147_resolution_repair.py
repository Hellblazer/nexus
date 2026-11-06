# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-147 Phase 1 Step 4 (nexus-qpimy): the exact-key resolution branch of RDR-232's one coverage repair.

Tenant-free, built on the boundary seams of ``tests/test_rdr232_coverage_repair.py``: ``plan_run`` is driven with a
stub dispatcher, and the model call (``operator_check``) is stubbed at the dispatcher, the boundary the runner uses.
The ``resolve`` tool answers with the REAL :func:`nexus.plans.resolve_step.resolve_keys` over a small in-memory
catalog (its two methods), so the branch is tested against the step's real output shape, ``by_token`` included.

The branch reaches the catalog through ``view.retrieve("resolve", ...)`` and ``view.fetch`` (the runner's accounted
``sql`` steps), so these tests drive it through the dispatcher the way production does.

Every pin below was run with the branch removed and fails there (the vacuity reds are in the bead's hand-back).
"""
from __future__ import annotations

import hashlib
import ast
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from nexus import config as _config
from nexus.entity_keys import EntityConfigError
from nexus.plans import coverage_ledger, switches
from nexus.plans.coverage_ledger import BoundaryView, CoverageBoundary
from nexus.plans.coverage_repair import (
    JOINED_DROP_REASONS,
    MAX_REPAIR_ITEMS,
    RESOLUTION_REASONS,
    RESOLVED_RECORD_MAX_CHUNKS,
    WIRE_KEY_LIST_CAP,
    RepairReport,
)
from nexus.plans.evidence import ScopeConstraint
from nexus.plans.repair_stream import SupplementItem
from nexus.plans.reserved_stream import strip_source_note
from nexus.plans.resolve_step import resolve_keys
from nexus.plans.runner import PlanResult
from tests.test_rdr232_coverage_ledger import RDR_COLL, _cov, _request, _structured
from tests.test_rdr232_coverage_repair import (
    REPAIR_TEXT,
    RepairDispatcher,
    _h,
    _run,
    by_topic,
)
from tests.test_rdr232_coverage_wiring import _drive

RECORD_COLL = "knowledge__custs__bge-base-en-v15-768__v1"
PATTERN = r"CUST-\d{4}"
#: f1 and f2 are supported by any anchor ("decision"); f3 only by a joined item ("Resolved"), so f3 is the
#: unsupported facet the repair targets, and only the branch's items can cover it.
CHECK = by_topic(topic={"f1": "decision", "f2": "decision", "f3": "Resolved"})
JOINED_SEP = "\n\nResolved "


def _tok(n: int) -> str:
    return f"CUST-{n:04d}"


def _record_text(token: str) -> str:
    return f"Customer {token} is Acme-{token[-4:]}"


def _chunk_text(token: str, k: int) -> str:
    """Chunk *k* of the record named *token*: chunk 0 is the one-line record, a later chunk carries its own text."""
    return _record_text(token) if k == 0 else f"{_record_text(token)} part {k}"


def _anchor_text(n: int, *tokens: str) -> str:
    return f"decision message {n} raised by {' and '.join(tokens) or 'nobody'} in the channel"


class FakeCatalog:
    """``find_by_title_exact`` and ``get_manifests``: the two methods ``resolve_keys`` calls."""

    def __init__(self, tokens: list[str], *, ambiguous: tuple[str, ...] = (), record_collection: str = RECORD_COLL,
                 chunks: int | dict[str, int] = 1, tumblers: dict[str, str] | None = None) -> None:
        self.docs: dict[str, list[Any]] = {}
        self.manifests: dict[str, list[Any]] = {}
        self.chunk_texts: dict[str, str] = {}
        for i, token in enumerate(tokens):
            tumbler = (tumblers or {}).get(token, f"1.9.{i + 1}")
            n = chunks.get(token, 1) if isinstance(chunks, dict) else chunks
            doc = SimpleNamespace(tumbler=tumbler, physical_collection=record_collection)
            self.docs[token] = [doc, SimpleNamespace(tumbler=f"{tumbler}9", physical_collection=record_collection)] if token in ambiguous else [doc]
            self.manifests[tumbler] = [
                SimpleNamespace(position=k, chash=_h(_chunk_text(token, k)), collection=record_collection) for k in range(n)]
            self.chunk_texts.update({_h(_chunk_text(token, k)): _chunk_text(token, k) for k in range(n)})

    def find_by_title_exact(self, title: str, *, content_type: str | None = None, owner: Any = None) -> list[Any]:  # noqa: ARG002
        return list(self.docs.get(title, []))

    def get_manifests(self, doc_ids: list[str]) -> dict[str, list[Any]]:
        return {d: self.manifests.get(d, []) for d in doc_ids}

    def record_texts(self) -> dict[str, str]:
        return dict(self.chunk_texts)


class ResolveDispatcher(RepairDispatcher):
    """The repair tests' stub, with anchors that carry key tokens, a ``resolve`` tool over the real ``resolve_keys``
    and a fetch that returns the record chunks. ``restore_cap`` lifts a patched assessment cap at the moment the
    repair starts (its first ``resolve`` call), so a test can leave an anchor unread in round one only."""

    def __init__(self, anchor_texts: list[str], catalog: FakeCatalog, check=CHECK, *, fail_resolve: bool = False,
                 restore_cap: int | None = None, blank_records: bool = False, **kw: Any) -> None:
        super().__init__(check, **kw)
        self.blank_records = blank_records  # the record chunks come back with no text (a fetch that finds nothing readable)
        self.anchor_texts = anchor_texts
        self.catalog = catalog
        self.fail_resolve = fail_resolve
        self.restore_cap = restore_cap
        self.by_hash = {_h(strip_source_note(t)): t for t in anchor_texts} | catalog.record_texts()  # a chunk hash is the bare text's

    async def __call__(self, tool: str, args: dict) -> dict:
        if tool == "resolve":
            self.calls.append((tool, dict(args)))
            if self.restore_cap is not None:
                coverage_ledger.ASSESSMENT_MAX_ITEM_CHARS = self.restore_cap
            if self.fail_resolve:
                raise RuntimeError("catalog down")
            return resolve_keys(self.catalog, args["tokens"])
        if tool == "search" and not self.is_repair_search(args):
            self.calls.append((tool, dict(args)))
            return _structured([strip_source_note(t) for t in self.anchor_texts], RDR_COLL)
        if tool == "store_get_many" and args.get("ids") and {str(i) for i in args["ids"]} <= set(self.by_hash):
            self.calls.append((tool, dict(args)))
            if self.blank_records and {str(i) for i in args["ids"]} <= set(self.catalog.record_texts()):
                return {"contents": ["" for _ in args["ids"]]}
            return {"contents": [self.by_hash[str(i)] for i in args["ids"]]}
        return await super().__call__(tool, args)

    def resolve_calls(self) -> list[list[str]]:
        return [a["tokens"] for t, a in self.calls if t == "resolve"]


@pytest.fixture(autouse=True)
def _no_catalog_lookup():
    """The runner's non-evidentiary hygiene asks the catalog; these tests are tenant-free, so it answers 'nothing excluded'."""
    with patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()):
        yield


@pytest.fixture(autouse=True)
def _entity_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The anchor collection (``rdr__*``) carries the key pattern: the repair looks it up by each anchor's collection."""

    real = _config.load_config

    def with_entity(*a: Any, **k: Any) -> dict:
        return {**real(*a, **k), "entity": {"collections": {"rdr__*": PATTERN}}}

    monkeypatch.setattr(_config, "load_config", with_entity)


def on(**kw: Any) -> CoverageBoundary:
    return CoverageBoundary(request=_request(3), resolution_repair=True, **kw)


def _wire(repair: RepairReport) -> dict:
    return {k: v for k, v in repair.to_wire().items() if k != "latency_ms"}


def _simple(n: int = 3, **kw: Any) -> ResolveDispatcher:
    tokens = [_tok(i) for i in range(1, n + 1)]
    return ResolveDispatcher([_anchor_text(i, t) for i, t in enumerate(tokens, 1)], FakeCatalog(tokens), **kw)


# ── the switch ────────────────────────────────────────────────────────────────────────────────────────────


def test_the_switch_is_on_by_default_and_a_bare_boundary_does_not_resolve_unless_told() -> None:
    # RDR-147 Phase 2 (Sam, 2026-11-06, nexus-bwct4.2): the shipped default is on; the boundary's own field stays False
    # so only the runner, reading the switch, turns the branch on.
    assert switches.ANSWER_RESOLUTION_REPAIR_ENABLED is True
    assert switches.resolution_repair_enabled({}) is True
    assert CoverageBoundary(request=None).resolution_repair is False


@pytest.mark.parametrize(("raw", "expected"), [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("", True), ("  ", True), ("0", False), ("off", False), ("NO", False), ("garbage", False),
])
def test_the_switch_reads_unset_and_on_values_as_on_and_off_or_unrecognised_as_off(raw: str, expected: bool) -> None:
    switches.reset_warned()
    assert switches.resolution_repair_enabled({switches.ANSWER_RESOLUTION_REPAIR_ENV: raw}) is expected


def test_the_suite_clears_the_switch_for_every_test() -> None:
    """``tests/conftest.py`` must clear the switch before every test, so a developer's export cannot leak in. The
    runtime check (the variable is absent here) cannot fail on CI, where nothing exports it; this one reads the
    conftest and fails when the ``delenv`` of the switch is gone."""
    tree = ast.parse((Path(__file__).parent / "conftest.py").read_text(encoding="utf-8"))
    cleared = {
        call.args[0].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "delenv"
        and call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
    }
    assert switches.ANSWER_RESOLUTION_REPAIR_ENV in cleared, "the conftest no longer clears the switch for every test"
    assert switches.ANSWER_RESOLUTION_REPAIR_ENV not in os.environ


@pytest.mark.asyncio
async def test_with_the_switch_off_the_repair_is_the_semantic_one_and_the_wire_is_unchanged() -> None:
    d = _simple()
    result = await _run(d, CoverageBoundary(request=_request(3)))
    cov = _cov(result)
    assert d.resolve_calls() == [], "no resolve call, no scan"
    assert len(d.repair_searches()) == 1 and cov.repair.outcome == "completed"
    assert cov.repair.resolution_entered is False and cov.repair.resolution_reason == "" and cov.repair.anchor_key_ids == ()
    assert set(cov.repair.to_wire()) == {
        "outcome", "facet_ids", "queries", "items_found", "items_merged", "latency_ms", "detail", "noted_facets", "reason",
        "unread_facet_ids"}, "off is the wire of today: the resolution keys appear only when the branch was considered"
    assert REPAIR_TEXT in d.args_of("generate")[0]["context"]


@pytest.mark.asyncio
async def test_with_the_switch_off_the_whole_run_equals_the_run_before_the_field_existed() -> None:
    """The same input through ``CoverageBoundary()`` and through ``resolution_repair=False`` is one run."""
    d1, d2 = _simple(), _simple()
    a = _cov(await _run(d1, CoverageBoundary(request=_request(3))))
    b = _cov(await _run(d2, CoverageBoundary(request=_request(3), resolution_repair=False)))
    assert a.to_wire()["repair"].keys() == b.to_wire()["repair"].keys()
    assert _wire(a.repair) == _wire(b.repair) and d1.tools == d2.tools
    assert [x.to_wire() for x in a.ledger.anchors] == [x.to_wire() for x in b.ledger.anchors]


# ── the branch ────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unsupported_facet_with_key_tokens_in_the_anchors_merges_one_joined_item_per_pair_and_rechecks_once() -> None:
    d = _simple(3)
    result = await _run(d, on())
    cov = _cov(result)
    r = cov.repair
    assert (r.outcome, r.resolution_entered, r.resolution_reason) == ("completed", True, "")
    assert r.anchor_key_ids == (_tok(1), _tok(2), _tok(3)) and r.resolution_tokens == 3
    assert (r.resolved, r.missing, r.ambiguous, r.joined_added, r.joined_dropped) == ((_tok(1), _tok(2), _tok(3)), (), (), 3, ())
    assert (r.queries, r.items_found, r.items_merged) == (0, 3, 3), "no semantic query ran; three joined items merged"
    assert d.repair_searches() == [], "the branch runs in place of the semantic facet queries"
    assert d.resolve_calls() == [[_tok(1), _tok(2), _tok(3)]]
    # the joined text: the anchor's text, a blank line, "Resolved <token>: <record text>"; its chash is sha256 of that text
    anchors = {a.content: a for a in cov.ledger.anchors}
    for i in (1, 2, 3):
        joined = _anchor_text(i, _tok(i)) + JOINED_SEP + f"{_tok(i)}: {_record_text(_tok(i))}"
        assert joined in anchors, "one joined item per resolved (anchor, token) pair, in the pinned text format"
        assert anchors[joined].chash == hashlib.sha256(joined.encode()).hexdigest()
        assert anchors[joined].collection == RECORD_COLL and anchors[joined].stream == "vector"
    # one re-check, prior=first: the delta pass over the joined items only, and f3 is covered by them
    checks = d.args_of("operator_check")
    assert len(checks) == 2, "the first round's one vector call and exactly one re-check"
    assert cov.ledger.rounds == 2 and cov.ledger.finding("f3").state == "covered"
    assert cov.state == "complete" and [x.item_id for x in cov.ledger.anchors if x.content == _anchor_text(1, _tok(1))] == ["e1"], "first-round ids do not move"
    last = checks[-1]
    assert all(JOINED_SEP in i["text"] for i in json.loads(last["items"]))
    context = d.args_of("generate")[0]["context"]
    assert JOINED_SEP in context and "Customer CUST-0001" in context, "generation got the joined evidence"


@pytest.mark.asyncio
async def test_the_resolve_and_the_record_fetch_are_accounted_as_known_zero_sql_steps() -> None:
    d = _simple(2)
    result = await _run(d, on())
    repair = [r for r in result.step_records if r.step_index >= result.total_planned_steps and r.operator != "operator_check"]
    assert [r.operator for r in repair] == ["resolve", "store_get_many"]
    assert all(r.source == "sql" and r.ok and r.cost_usd == 0.0 for r in repair), "resolve makes no model call"


@pytest.mark.asyncio
async def test_missing_and_ambiguous_tokens_are_reported_and_make_no_joined_item() -> None:
    present = [_tok(1), _tok(2), _tok(3)]
    d = ResolveDispatcher(
        [_anchor_text(1, _tok(1)), _anchor_text(2, _tok(2)), _anchor_text(3, _tok(3)), _anchor_text(4, "CUST-0099")],
        FakeCatalog(present, ambiguous=(_tok(2),)))
    cov = _cov(await _run(d, on()))
    r = cov.repair
    assert r.resolution_entered and r.joined_added == 2 and r.resolved == (_tok(1), _tok(3))
    assert r.ambiguous == (_tok(2),) and r.missing == ("CUST-0099",)
    assert not any(a.content.startswith(_anchor_text(2, _tok(2)) + JOINED_SEP) for a in cov.ledger.anchors), "an ambiguous key names no record"
    assert r.anchor_key_ids == (_tok(1), _tok(2), _tok(3), "CUST-0099"), "the completeness domain is every id the anchors named"


@pytest.mark.asyncio
async def test_when_every_token_is_unresolvable_the_branch_ran_and_the_outcome_is_no_new_evidence() -> None:
    d = ResolveDispatcher([_anchor_text(1, "CUST-0098"), _anchor_text(2, "CUST-0099")], FakeCatalog([]))
    cov = _cov(await _run(d, on()))
    r = cov.repair
    assert (r.outcome, r.resolution_entered, r.joined_added, r.missing) == ("no_new_evidence", True, 0, ("CUST-0098", "CUST-0099"))
    assert d.repair_searches() == [], "no fallback to the semantic queries (running both is unmeasured)"
    assert d.tools.count("operator_check") == 1 and cov.ledger.rounds == 1 and d.tools.count("generate") == 1


@pytest.mark.asyncio
async def test_the_branch_runs_when_the_semantic_queries_would_find_nothing_new() -> None:
    d = _simple(2, repair_hits=[])
    cov = _cov(await _run(d, on()))
    assert cov.repair.resolution_entered and cov.repair.outcome == "completed" and cov.repair.joined_added == 2
    assert d.repair_searches() == [] and cov.ledger.finding("f3").state == "covered"
    off = _cov(await _run(_simple(2, repair_hits=[]), CoverageBoundary(request=_request(3))))
    assert off.repair.outcome == "no_new_evidence", "the same input without the branch ends the repair before any re-check"


@pytest.mark.asyncio
async def test_the_branch_runs_before_the_no_target_return_when_there_is_no_scope_to_ask() -> None:
    off = await CoverageBoundary(request=_request(3)).run(await _hooked_view(_simple(2), hooks=True))
    assert off.repair.outcome == "no_target", "no primary collection and no plan corpus: the semantic repair has nowhere to ask"
    d = _simple(2)
    cov = await on().run(await _hooked_view(d, hooks=True))
    assert cov.repair.outcome == "completed" and cov.repair.resolution_entered and cov.repair.joined_added == 2
    assert d.resolve_calls() == [[_tok(1), _tok(2)]]


# ── the cap ───────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ten_distinct_ids_over_twenty_pairs_make_twelve_items_one_per_token_first_and_list_the_eight_dropped() -> None:
    tokens = [_tok(i) for i in range(1, 11)]
    anchors = [_anchor_text(i, tokens[(i - 1) % 10]) for i in range(1, 21)]  # anchor i names token (i-1) % 10: each token twice
    d = ResolveDispatcher(anchors, FakeCatalog(tokens))
    cov = _cov(await _run(d, on()))
    r = cov.repair
    assert MAX_REPAIR_ITEMS == 12 and r.joined_added == 12 and r.items_merged == 12
    assert len(r.joined_dropped) == 8 and all(re.fullmatch(r"CUST-\d{4}@e\d+:cap", x) for x in r.joined_dropped), "each dropped pair is named token@anchor id:reason"
    assert r.tokens_dropped == ()
    joined = [a for a in cov.ledger.anchors if JOINED_SEP in a.content]
    represented = {a.content.split(JOINED_SEP)[1].split(":")[0] for a in joined}
    assert represented == set(tokens), "all ten tokens are represented"
    assert [a.content.split(JOINED_SEP)[1].split(":")[0] for a in joined[:10]] == tokens, "the first ten slots are one per token, in token order"
    # the extra anchors fill round robin: the two extra slots go to the first two tokens, not to the first token twice
    extra = [t for t in tokens if sum(1 for a in joined if f"Resolved {t}:" in a.content) == 2]
    assert extra == tokens[:2]


@pytest.mark.asyncio
async def test_fourteen_distinct_ids_resolve_twelve_tokens_and_list_the_other_two_as_dropped() -> None:
    tokens = [_tok(i) for i in range(1, 15)]
    d = ResolveDispatcher([_anchor_text(i, t) for i, t in enumerate(tokens, 1)], FakeCatalog(tokens))
    cov = _cov(await _run(d, on()))
    r = cov.repair
    assert d.resolve_calls() == [tokens[:12]], "at most twelve tokens are resolved, in anchor order"
    assert r.resolution_tokens == 12 and len(r.resolved) == 12 and r.joined_added == 12
    assert r.tokens_dropped == (_tok(13), _tok(14)), "a token never resolved is listed by name, not silently"
    assert r.joined_dropped == (), "and it is not a dropped pair: no pair of it was ever built"
    assert r.anchor_key_ids == tuple(tokens), "the completeness domain still lists all fourteen"


@pytest.mark.asyncio
async def test_a_joined_text_already_held_is_not_added_twice() -> None:
    tokens = [_tok(1)]
    joined = _anchor_text(1, _tok(1)) + JOINED_SEP + f"{_tok(1)}: {_record_text(_tok(1))}"
    d = ResolveDispatcher([_anchor_text(1, _tok(1)), joined], FakeCatalog(tokens), by_topic(topic={"f1": "decision", "f2": "decision", "f3": "nothing says this"}))
    cov = _cov(await _run(d, on()))
    assert cov.repair.resolution_entered
    assert sum(1 for a in cov.ledger.anchors if a.content == joined) == 1, "the join the input already holds is not added again"
    # the held join itself names CUST-0001, so it pairs with the record once more: that (new) join is the one item added;
    # the join of the first anchor is the one the input holds, and it is named, not skipped silently
    assert cov.repair.joined_added == 1 and cov.repair.joined_dropped == (f"{_tok(1)}@e1:held",)


# ── when the branch does not enter ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_collection_with_no_pattern_does_not_enter_and_the_semantic_repair_runs_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:

    real = _config.load_config
    monkeypatch.setattr(_config, "load_config", lambda *a, **k: {**real(*a, **k), "entity": {"collections": {"knowledge__other*": PATTERN}}})
    d = _simple(2)
    cov = _cov(await _run(d, on()))
    r = cov.repair
    assert (r.resolution_entered, r.resolution_reason, r.anchor_key_ids) == (False, "no_pattern_for_anchor_collections", ())
    assert r.resolution_reason in RESOLUTION_REASONS
    assert d.resolve_calls() == [] and len(d.repair_searches()) == 1 and r.outcome == "completed" and REPAIR_TEXT in d.args_of("generate")[0]["context"]
    assert r.to_wire()["resolution_entered"] is False and r.to_wire()["resolution_reason"] == "no_pattern_for_anchor_collections"


@pytest.mark.asyncio
async def test_anchors_with_no_tokens_do_not_enter_and_the_semantic_repair_runs_unchanged() -> None:
    d = ResolveDispatcher([_anchor_text(1), _anchor_text(2)], FakeCatalog([]))
    cov = _cov(await _run(d, on()))
    assert (cov.repair.resolution_entered, cov.repair.resolution_reason) == (False, "no_key_tokens")
    assert cov.repair.resolution_reason in RESOLUTION_REASONS
    assert d.resolve_calls() == [] and len(d.repair_searches()) == 1 and cov.repair.outcome == "completed"


@pytest.mark.asyncio
async def test_a_key_only_in_the_source_note_is_not_a_key_the_text_names() -> None:
    """The ``[source: ...]`` line names the chunk's own document (a record's title is its key), not what the chunk says."""
    d = ResolveDispatcher(
        [f"[source: {_tok(5)}]\n{_anchor_text(1)}", f"[source: {_tok(6)}]\n{_anchor_text(2)}"], FakeCatalog([_tok(5), _tok(6)]))
    cov = _cov(await _run(d, on()))
    assert (cov.repair.resolution_entered, cov.repair.resolution_reason, cov.repair.anchor_key_ids) == (False, "no_key_tokens", ())
    assert cov.repair.resolution_reason in RESOLUTION_REASONS
    assert d.resolve_calls() == []


@pytest.mark.asyncio
async def test_a_covered_first_ledger_has_no_repair_and_no_resolution() -> None:
    d = ResolveDispatcher([_anchor_text(i, _tok(i)) for i in (1, 2)], FakeCatalog([_tok(1), _tok(2)]), by_topic(topic={"f1": "decision", "f2": "decision", "f3": "decision"}))
    cov = _cov(await _run(d, on()))
    assert cov.repair.outcome == "not_attempted" and cov.repair.resolution_entered is False and d.resolve_calls() == []
    assert "resolution_entered" not in cov.repair.to_wire()


@pytest.mark.asyncio
async def test_a_malformed_entity_config_is_not_entered_and_never_escapes_the_boundary(monkeypatch: pytest.MonkeyPatch) -> None:

    def broken(*a: Any, **k: Any) -> dict:
        raise EntityConfigError("entity.collections[x]: not a regex")

    monkeypatch.setattr(_config, "load_config", broken)
    d = _simple(2)
    cov = _cov(await _run(d, on()))
    assert (cov.repair.resolution_entered, cov.repair.resolution_reason) == (False, "entity_config_error")
    assert cov.repair.resolution_reason in RESOLUTION_REASONS
    assert d.resolve_calls() == [] and cov.repair.outcome == "completed" and len(d.repair_searches()) == 1


@pytest.mark.parametrize("confined", [False, True])
@pytest.mark.asyncio
async def test_with_no_entity_config_at_all_the_switch_on_repair_is_the_switch_off_repair(
    monkeypatch: pytest.MonkeyPatch, confined: bool,
) -> None:
    """RDR-147 Phase 2 turned the switch on by default (nexus-bwct4.2). An install with no ``entity.collections`` must
    see the repair and its wire exactly as before RDR-147: no resolution keys, no reason (not even ``confined_run``),
    no catalog call."""
    real = _config.load_config
    monkeypatch.setattr(_config, "load_config", lambda *a, **k: {x: v for x, v in real(*a, **k).items() if x != "entity"})
    kw: dict[str, Any] = {}
    if confined:
        kw["confinement"] = SimpleNamespace(contains=lambda key: True, sends_filter=False, allow_list=lambda: [])
    d_on, d_off = _simple(2), _simple(2)
    cov_on = _cov(await _run(d_on, on(**kw)))
    cov_off = _cov(await _run(d_off, CoverageBoundary(request=_request(3), resolution_repair=False, **kw)))
    assert (cov_on.repair.resolution_entered, cov_on.repair.resolution_reason) == (False, "")
    assert _wire(cov_on.repair) == _wire(cov_off.repair)
    assert not any(k.startswith("resolution_") or k.startswith("anchors_") for k in cov_on.repair.to_wire())
    assert d_on.resolve_calls() == []


@pytest.mark.asyncio
async def test_a_run_confined_to_one_document_does_not_enter_and_says_why() -> None:
    """A joined item carries another document's record text under a new chash, so it fails the confinement's own
    membership test by construction; ``resolve`` is in ``CONFINED_TOOLS`` for the same reason (Step 3)."""
    confinement = SimpleNamespace(contains=lambda key: True, sends_filter=False, allow_list=lambda: [])
    d = _simple(2)
    cov = _cov(await _run(d, on(confinement=confinement)))
    r = cov.repair
    assert (r.resolution_entered, r.resolution_reason, r.anchor_key_ids) == (False, "confined_run", ())
    assert r.resolution_reason in RESOLUTION_REASONS
    assert d.resolve_calls() == [], "no catalog call from a confined run"
    assert len(d.repair_searches()) == 1, "the confined semantic repair runs as it did"


@pytest.mark.asyncio
async def test_a_record_outside_the_callers_scope_is_dropped_and_one_inside_joins() -> None:
    """A caller-scoped run keeps the repair inside the scope: the branch enters, and a pair whose record lies outside
    the scope is listed as dropped (the record's collection is judged like a repair hit's)."""
    tokens = [_tok(1), _tok(2)]
    d = ResolveDispatcher([_anchor_text(i, t) for i, t in enumerate(tokens, 1)], FakeCatalog(tokens))
    outside = _cov(await _run(d, on(scope=ScopeConstraint("corpus_prefix", "rdr"))))
    assert outside.repair.resolution_entered and outside.repair.joined_added == 0
    assert outside.repair.joined_dropped == (f"{_tok(1)}@e1:out_of_scope", f"{_tok(2)}@e2:out_of_scope")
    d2 = ResolveDispatcher([_anchor_text(i, t) for i, t in enumerate(tokens, 1)], FakeCatalog(tokens))
    inside = _cov(await _run(d2, on(scope=ScopeConstraint("corpus_prefix", "knowledge__custs"))))
    assert inside.repair.resolution_entered is True
    assert inside.repair.joined_added == 2 and inside.repair.joined_dropped == ()


@pytest.mark.asyncio
async def test_no_repair_queries_allowed_switches_the_branch_off_too() -> None:
    d = _simple(2)
    cov = _cov(await _run(d, on(max_repair_queries=0)))
    assert cov.repair.outcome == "not_attempted" and cov.repair.resolution_entered is False and d.resolve_calls() == []


@pytest.mark.asyncio
async def test_a_view_with_no_retrieval_hooks_is_unavailable_and_the_branch_does_not_enter() -> None:
    d = _simple(2)
    view = await _hooked_view(d, hooks=False)
    result = await on().run(view)
    assert result.repair.outcome == "unavailable" and result.repair.resolution_entered is False
    assert d.resolve_calls() == []


async def _hooked_view(d: ResolveDispatcher, *, hooks: bool, merges: bool = True) -> BoundaryView:
    """A boundary view over a finished plain run, with the repair hooks the runner would hand it (or none), and NO plan
    retrieval scope (``resolved_args`` empty, no primary source): the semantic repair has nowhere to ask."""
    first = await _run(d, CoverageBoundary(request=_request(3), max_repair_queries=0))
    texts = [a.content for a in _cov(first).ledger.anchors]
    installed: list[Any] = []

    async def check(args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        return await d("operator_check", args)

    async def retrieve(tool: str, args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        return await d(tool, args)

    async def fetch(hits: list[Any], *, timeout: float) -> list[Any]:  # noqa: ARG001
        return [SupplementItem(text=d.by_hash[h.id], chash=h.id, collection=h.collection, tumbler=h.tumbler) for h in hits]

    return BoundaryView(
        evidence_texts=texts, observed=True, step_outputs=list(first.steps),
        tools=["search", "traverse", "store_get_many", "generate"], scope_diagnostics=[], resolved_args=[],
        check=check, remaining_usd=lambda: None, remaining_seconds=lambda: None,
        retrieve=retrieve if hooks else None, fetch=fetch if hooks else None,
        install_supplement=(lambda items: installed.__setitem__(slice(None), list(items))) if hooks else None,
        rehydrate=(lambda: ([*texts, *[i.text for i in installed]] if merges else list(texts), True)) if hooks else None,
    )


# ── failures ──────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_resolve_leaves_the_first_ledger_in_force() -> None:
    d = _simple(2, fail_resolve=True)
    result = await _run(d, on())
    cov = _cov(result)
    assert cov.repair.outcome == "retrieval_failed" and cov.repair.resolution_entered is True
    assert "RuntimeError" in cov.repair.detail
    assert cov.state == "partial" and cov.unresolved_facet_ids == ("f3",) and cov.ledger.rounds == 1
    assert d.repair_searches() == [] and d.tools.count("operator_check") == 1 and d.tools.count("generate") == 1
    failed = [r for r in result.step_records if r.operator == "resolve" and not r.ok]
    assert len(failed) == 1 and failed[0].source == "sql"


@pytest.mark.asyncio
async def test_a_failed_record_fetch_leaves_the_first_ledger_in_force() -> None:
    class D(ResolveDispatcher):
        async def __call__(self, tool: str, args: dict) -> dict:
            if tool == "store_get_many" and args.get("ids") and {str(i) for i in args["ids"]} <= set(self.catalog.record_texts()):
                raise RuntimeError("fetch backend down")
            return await super().__call__(tool, args)

    tokens = [_tok(1), _tok(2)]
    d = D([_anchor_text(i, t) for i, t in enumerate(tokens, 1)], FakeCatalog(tokens))
    cov = _cov(await _run(d, on()))
    assert cov.repair.outcome == "retrieval_failed" and "the chunk fetch failed" in cov.repair.detail
    assert cov.repair.resolution_entered is True and cov.repair.joined_added == 0 and cov.ledger.rounds == 1


# ── the re-check is the existing one ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_recheck_still_reads_the_anchor_the_first_round_left_unread() -> None:
    """The first round's size cap leaves anchor e2 unread; the branch's re-check reassesses it with the joined items,
    exactly as the semantic repair does (nexus-jypmj.23). The cap is lifted when the repair starts (its first
    ``resolve`` call), so the re-check can carry what the first round could not."""
    tokens = [_tok(1), _tok(2)]
    d = ResolveDispatcher([_anchor_text(1, tokens[0]), _anchor_text(2, tokens[1])], FakeCatalog(tokens), restore_cap=120_000)
    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", 80):  # one anchor (52 chars) fits a call, two do not
        cov = _cov(await _run(d, on()))
    assert cov.ledger.rounds == 2 and cov.repair.resolution_entered and cov.repair.joined_added == 2
    e2 = next(a.item_id for a in cov.ledger.anchors if a.content == _anchor_text(2, tokens[1]))
    last = d.args_of("operator_check")[-1]
    ids = [i["id"] for i in json.loads(last["items"])]
    assert e2 in ids, "the unread anchor rides the re-check beside the joined items"
    assert cov.ledger.assessment.skipped_items == 0


# ── the wire ──────────────────────────────────────────────────────────────────────────────────────────────


def test_to_wire_carries_the_resolution_fields_when_the_branch_was_considered_and_only_then() -> None:
    quiet = RepairReport("completed", ("f3",))
    assert "resolution_entered" not in quiet.to_wire() and "anchor_key_ids" not in quiet.to_wire()
    r = RepairReport(
        "completed", ("f3",), resolution_entered=True, resolution_tokens=3, resolved=("CUST-0001",), missing=("CUST-0002",),
        ambiguous=("CUST-0003",), joined_added=1, joined_dropped=("CUST-0001@e2:cap",), anchor_key_ids=("CUST-0001", "CUST-0002", "CUST-0003"))
    wire = r.to_wire()
    assert wire["resolution_entered"] is True and wire["resolution_tokens"] == 3 and wire["resolved"] == ["CUST-0001"]
    assert wire["missing"] == ["CUST-0002"] and wire["ambiguous"] == ["CUST-0003"] and wire["joined_added"] == 1
    assert wire["joined_dropped"] == ["CUST-0001@e2:cap"] and wire["anchor_key_ids"] == ["CUST-0001", "CUST-0002", "CUST-0003"]
    assert {"outcome", "facet_ids", "queries", "items_found", "items_merged", "latency_ms", "detail", "noted_facets", "reason", "unread_facet_ids"} <= set(wire)
    reason_only = RepairReport("completed", resolution_reason="confined_run").to_wire()
    assert reason_only["resolution_entered"] is False and reason_only["resolution_reason"] == "confined_run"


@pytest.mark.asyncio
async def test_the_envelope_coverage_object_carries_the_repair_fields_end_to_end() -> None:
    cov = _cov(await _run(_simple(2), on()))
    repair = cov.to_wire()["repair"]
    assert repair["resolution_entered"] is True and repair["joined_added"] == 2 and repair["anchor_key_ids"] == [_tok(1), _tok(2)]


# ── the runner caller's wiring ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "expected"), [(None, True), ("", True), ("0", False), ("1", True), ("off", False)])
async def test_nx_answer_hands_the_boundary_the_switch_it_reads_from_the_environment(monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool) -> None:

    monkeypatch.setenv(switches.ANSWER_COVERAGE_ENV, "1")
    if value is None:
        monkeypatch.delenv(switches.ANSWER_RESOLUTION_REPAIR_ENV, raising=False)
    else:
        monkeypatch.setenv(switches.ANSWER_RESOLUTION_REPAIR_ENV, value)
    run = await _drive(plan_result=PlanResult(steps=[{"text": "ok"}]), request=_request(2))
    [kw] = run.plan_run_calls
    assert kw["coverage_boundary"].resolution_repair is expected


# ── fix round 1 (review r1 of 9e281b3e4) ────────────────────────────────────────────────────────────────────


def _joined(cov: Any) -> list[Any]:
    return [a for a in cov.ledger.anchors if JOINED_SEP in a.content]


def _key_of(anchor: Any) -> str:
    return anchor.content.split(JOINED_SEP)[1].split(":")[0]


@pytest.mark.asyncio
async def test_ten_records_of_three_chunks_each_get_their_item_none_is_starved_by_an_earlier_record() -> None:
    """B1: the chunk fetch was one flat list cut at twice the item cap across all tokens, so a record with several chunks
    took the slots of the records after it (probe: 10 tokens of 3 chunks, 24 fetched, CUST-0009 and CUST-0010 dropped)."""
    tokens = [_tok(i) for i in range(1, 11)]
    d = ResolveDispatcher([_anchor_text(i, t) for i, t in enumerate(tokens, 1)], FakeCatalog(tokens, chunks=3))
    cov = _cov(await _run(d, on()))
    r = cov.repair
    assert (r.resolved, r.joined_added, r.joined_dropped) == (tuple(tokens), 10, ())
    assert [_key_of(a) for a in _joined(cov)] == tokens, "every resolved key has its item"
    for a in _joined(cov):
        key = _key_of(a)
        assert all(_chunk_text(key, k) in a.content for k in range(3)), "the item carries the whole record (three chunks)"
    fetch = [a["ids"] for t, a in d.calls if t == "store_get_many" and set(a["ids"]) <= set(d.catalog.record_texts())]
    assert [len(ids) for ids in fetch] == [30], "one fetch, every record's chunks: not cut at twice the item cap"


@pytest.mark.asyncio
async def test_a_record_longer_than_the_chunk_bound_is_cut_to_its_first_chunks_and_the_cut_is_named() -> None:
    """B1: the per-record bound keeps one joined item (anchor + record body) and the fetch (items x bound) finite."""
    bound = RESOLVED_RECORD_MAX_CHUNKS
    assert bound == 3
    tokens = [_tok(1), _tok(2)]
    d = ResolveDispatcher([_anchor_text(i, t) for i, t in enumerate(tokens, 1)], FakeCatalog(tokens, chunks={_tok(1): bound + 2, _tok(2): 1}))
    cov = _cov(await _run(d, on()))
    long = next(a for a in _joined(cov) if _key_of(a) == _tok(1))
    assert _chunk_text(_tok(1), bound - 1) in long.content and _chunk_text(_tok(1), bound) not in long.content
    assert f"1 record cut to its first {bound} chunks" in cov.repair.detail
    fetch = [a["ids"] for t, a in d.calls if t == "store_get_many" and set(a["ids"]) <= set(d.catalog.record_texts())]
    assert [len(ids) for ids in fetch] == [bound + 1]
    assert cov.repair.joined_added == 2 and cov.repair.joined_dropped == ()


@pytest.mark.asyncio
async def test_a_record_the_fetch_returns_no_text_for_is_dropped_as_no_text() -> None:
    """B1: a pair dropped for a missing record text says so (it used to carry no reason, and the comment named the scope)."""
    d = ResolveDispatcher([_anchor_text(1, _tok(1))], FakeCatalog([_tok(1)]), blank_records=True)
    cov = _cov(await _run(d, on()))
    assert cov.repair.resolution_entered and cov.repair.joined_added == 0
    assert cov.repair.joined_dropped == (f"{_tok(1)}@e1:no_text",)


def test_every_drop_reason_the_ledger_writes_is_in_the_closed_set() -> None:
    assert JOINED_DROP_REASONS == ("cap", "no_text", "out_of_scope", "held", "self")
    assert RESOLUTION_REASONS == ("confined_run", "entity_config_error", "no_pattern_for_anchor_collections", "no_key_tokens")


# F2: an anchor that IS the record it resolves to is no join


@pytest.mark.asyncio
async def test_an_anchor_that_is_the_resolved_record_is_skipped_as_self_and_the_real_pair_still_joins() -> None:
    """The record's own chunk is an anchor (same chash) and its body names its own key: it is not a join, and it must
    not take the key's first slot (a self-pair ahead of the message pushed the message past the cap)."""
    tokens = [_tok(1)]
    d = ResolveDispatcher([_record_text(_tok(1)), _anchor_text(2, _tok(1))], FakeCatalog(tokens))
    cov = _cov(await _run(d, on()))
    r = cov.repair
    assert r.joined_dropped == (f"{_tok(1)}@e1:self",) and r.joined_added == 1
    assert [a.content for a in _joined(cov)] == [_anchor_text(2, _tok(1)) + JOINED_SEP + f"{_tok(1)}: {_record_text(_tok(1))}"]
    assert r.anchor_key_ids == (_tok(1),), "another anchor names the key, so it stays in the completeness domain"


@pytest.mark.asyncio
async def test_an_anchor_in_the_resolved_documents_tumbler_is_also_self() -> None:
    """The anchor is another chunk of the record's document: same tumbler, different chash."""
    tokens = [_tok(1)]
    d = ResolveDispatcher([_anchor_text(1, _tok(1)), _anchor_text(2, _tok(1))], FakeCatalog(tokens, tumblers={_tok(1): "1.1.1"}))
    cov = _cov(await _run(d, on()))
    assert cov.repair.joined_dropped == (f"{_tok(1)}@e2:self",) and cov.repair.joined_added == 1


@pytest.mark.asyncio
async def test_a_key_only_its_own_record_names_is_not_in_the_completeness_domain() -> None:
    tokens = [_tok(1), _tok(2)]
    d = ResolveDispatcher([_record_text(_tok(1)), _anchor_text(2, _tok(2))], FakeCatalog(tokens))
    cov = _cov(await _run(d, on()))
    r = cov.repair
    assert r.resolution_tokens == 2 and r.resolved == (_tok(1), _tok(2)) and r.joined_added == 1
    assert r.joined_dropped == (f"{_tok(1)}@e1:self",)
    assert r.anchor_key_ids == (_tok(2),), "the record that names itself is not evidence that a message tied it to the issue"


# F3: the entered early returns still reassess an unread anchor


async def _unread_run(d: ResolveDispatcher) -> Any:
    with patch.object(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", 80):  # one anchor (52 chars) fits a call, two do not
        return _cov(await _run(d, on()))


@pytest.mark.asyncio
async def test_when_every_key_is_missing_the_anchor_the_first_round_left_unread_is_still_reread() -> None:
    d = ResolveDispatcher([_anchor_text(1, "CUST-0098"), _anchor_text(2, "CUST-0099")], FakeCatalog([]), restore_cap=120_000)
    cov = await _unread_run(d)
    assert cov.repair.outcome == "no_new_evidence" and cov.repair.resolution_entered and cov.repair.joined_added == 0
    assert cov.ledger.rounds == 2, "the repair did not end at the no-item return: the unread anchor was reassessed"
    e2 = next(a.item_id for a in cov.ledger.anchors if a.content == _anchor_text(2, "CUST-0099"))
    assert e2 in [i["id"] for i in json.loads(d.args_of("operator_check")[-1]["items"])]


@pytest.mark.asyncio
async def test_when_the_resolve_fails_the_anchor_the_first_round_left_unread_is_still_reread() -> None:
    d = ResolveDispatcher([_anchor_text(1, _tok(1)), _anchor_text(2, _tok(2))], FakeCatalog([_tok(1), _tok(2)]), fail_resolve=True, restore_cap=120_000)
    cov = await _unread_run(d)
    assert cov.repair.outcome == "retrieval_failed" and cov.repair.resolution_entered
    assert cov.ledger.rounds == 2
    e2 = next(a.item_id for a in cov.ledger.anchors if a.content == _anchor_text(2, _tok(2)))
    assert e2 in [i["id"] for i in json.loads(d.args_of("operator_check")[-1]["items"])]


# F4: joined_added is what reached the reduction


@pytest.mark.asyncio
async def test_joined_added_counts_the_items_merged_not_the_items_built() -> None:
    d = _simple(2)
    cov = await on().run(await _hooked_view(d, hooks=True, merges=False))
    r = cov.repair
    assert r.outcome == "not_merged" and r.resolution_entered
    assert (r.items_found, r.items_merged, r.joined_added) == (2, 0, 0), "built 2, merged none: joined_added says none"
    ok = await on().run(await _hooked_view(_simple(2), hooks=True))
    assert (ok.repair.items_found, ok.repair.items_merged, ok.repair.joined_added) == (2, 2, 2)


# F5: an anchor collection with no configured pattern is not silent


@pytest.mark.asyncio
async def test_the_report_counts_the_anchors_scanned_and_those_whose_collection_has_no_pattern() -> None:
    d = _simple(2)
    cov = _cov(await _run(d, on()))
    scanned = len(cov.ledger.anchors) - sum(1 for a in cov.ledger.anchors if JOINED_SEP in a.content)
    assert scanned >= 2
    assert (cov.repair.anchors_scanned, cov.repair.anchors_without_pattern) == (scanned, 0)
    assert cov.to_wire()["repair"]["anchors_scanned"] == scanned and cov.to_wire()["repair"]["anchors_without_pattern"] == 0


@pytest.mark.asyncio
async def test_no_anchor_collection_with_a_pattern_is_its_own_reason_and_the_counts_say_how_many(monkeypatch: pytest.MonkeyPatch) -> None:
    """The records collection is configured and the anchors' collection is not (the research-11 mistake): that is not
    'the evidence names no ids'."""
    real = _config.load_config
    monkeypatch.setattr(_config, "load_config", lambda *a, **k: {**real(*a, **k), "entity": {"collections": {RECORD_COLL: PATTERN}}})
    cov = _cov(await _run(_simple(2), on()))
    r = cov.repair
    assert r.resolution_reason == "no_pattern_for_anchor_collections" and r.resolution_entered is False
    assert r.anchors_scanned >= 2 and r.anchors_without_pattern == r.anchors_scanned
    assert cov.to_wire()["repair"]["anchors_without_pattern"] == r.anchors_without_pattern


@pytest.mark.asyncio
async def test_a_configured_collection_whose_anchors_name_no_key_is_still_no_key_tokens() -> None:
    cov = _cov(await _run(ResolveDispatcher([_anchor_text(1), _anchor_text(2)], FakeCatalog([])), on()))
    assert cov.repair.resolution_reason == "no_key_tokens" and cov.repair.anchors_without_pattern == 0 and cov.repair.anchors_scanned >= 2


# F6: the key lists on the wire are capped


def test_the_wire_caps_each_key_list_and_counts_what_it_left_out() -> None:
    assert WIRE_KEY_LIST_CAP == 50
    keys = tuple(f"CUST-{i:04d}" for i in range(60))
    r = RepairReport("completed", resolution_entered=True, anchor_key_ids=keys, joined_dropped=keys[:51], tokens_dropped=keys[:3], resolved=keys[:12])
    wire = r.to_wire()
    assert wire["anchor_key_ids"] == list(keys[:50]) and wire["anchor_key_ids_truncated"] == 10
    assert wire["joined_dropped"] == list(keys[:50]) and wire["joined_dropped_truncated"] == 1
    assert wire["tokens_dropped"] == list(keys[:3]) and wire["tokens_dropped_truncated"] == 0
    assert wire["resolved_truncated"] == 0 and wire["missing_truncated"] == 0 and wire["ambiguous_truncated"] == 0
    assert r.anchor_key_ids == keys, "the report itself is whole; only the wire is cut"


# F7: the persisted run summary says the branch ran


def test_the_run_summary_repair_block_carries_the_resolution_counts_when_the_branch_was_considered() -> None:
    from tests.test_rdr232_run_summary import _build, _coverage  # noqa: PLC0415 — the summary tests' builders

    base = _coverage("partial", rounds=2)
    off = _build(coverage=base)["repair"]
    assert "resolution_entered" not in off, "off is the summary of today"
    report = RepairReport(
        "completed", ("f2",), 0, 3, 2, 10, "", resolution_entered=True, resolved=("CUST-0001", "CUST-0002"), missing=("CUST-0003",),
        ambiguous=("CUST-0004", "CUST-0005", "CUST-0006"), joined_added=2, anchor_key_ids=("CUST-0001",))
    block = _build(coverage=replace(base, repair=report))["repair"]
    assert (block["resolution_entered"], block["resolution_reason"]) == (True, "")
    assert (block["resolution_resolved"], block["resolution_missing"], block["resolution_ambiguous"], block["resolution_items_merged"]) == (2, 1, 3, 2)
    assert "CUST-0001" not in json.dumps(block), "counts only, no key"
    said = _build(coverage=replace(base, repair=RepairReport("completed", resolution_reason="confined_run")))["repair"]
    assert said["resolution_entered"] is False and said["resolution_reason"] == "confined_run"


# F9: the joined chash is the anchor convention's


@pytest.mark.asyncio
async def test_the_joined_chash_is_the_hash_of_the_text_without_the_source_line() -> None:
    """An anchor's chash is the sha256 of its chunk text WITHOUT the ``[source: ...]`` line (``EvidenceAnchor``); the
    joined item follows it, so a lookup by recomputing the hash from the bare text finds it."""
    text = f"[source: Slack #escalations]\n{_anchor_text(1, _tok(1))}"
    d = ResolveDispatcher([text], FakeCatalog([_tok(1)]))
    cov = _cov(await _run(d, on()))
    [item] = _joined(cov)
    assert item.content.startswith("[source: Slack #escalations]\n")
    bare = strip_source_note(item.content)
    assert item.chash == hashlib.sha256(bare.encode()).hexdigest() != hashlib.sha256(item.content.encode()).hexdigest()
