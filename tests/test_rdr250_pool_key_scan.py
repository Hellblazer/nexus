# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-250 Phase 1 Steps 2 and 3 (nexus-io7tw): the pool key scan and its precision filter, behind ``NX_ANSWER_POOL_KEY_SCAN``.

Tenant-free, in the fixture style of ``tests/test_rdr147_resolution_repair.py``: ``plan_run`` is driven with a stub
dispatcher, the model call (``operator_check``) is stubbed at the dispatcher, and ``resolve`` answers with the real
``resolve_keys`` over a small in-memory catalog. The plan's own ``store_get_many`` carries ``limit_per_source``, so the
generator reads the first ``ANCHORS`` ids of the search step and the rest of its ids are the pool (plan 10's shape, in
miniature: it reads 20 of 40).

The Test Plan of RDR-250 is the six scenarios below. The non-vacuity pins are the cited-versus-uncited pair: the same
pool through a check that cites its chunk and through one that does not, which must differ.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from nexus import config as _config
from nexus.plans import coverage_ledger, coverage_pool, switches
from nexus.plans.coverage_ledger import AssessmentReport, BoundaryView, CoverageBoundary
from nexus.operators.dispatch import OperatorError, OperatorOutputError, OperatorTimeoutError
from nexus.plans.coverage_repair import WIRE_KEY_LIST_CAP, RepairReport
from nexus.plans.repair_stream import SupplementItem
from nexus.plans.reserved_stream import strip_source_note
from tests.test_rdr147_resolution_repair import (
    CHECK,
    JOINED_SEP,
    PATTERN,
    RECORD_COLL,
    FakeCatalog,
    ResolveDispatcher,
    _record_text,
    _tok,
)
from tests.test_rdr232_coverage_ledger import RDR_COLL, _cov, _request, _structured
from tests.test_rdr232_coverage_repair import _h, _run, by_topic

pytestmark = pytest.mark.usefixtures("rowless_collection_cache")  # RDR-229 P2.9: a tenant-free test takes NoRow without a listing

#: Anchors the generator reads: the first two ids of the search step. Everything after them is the pool.
ANCHORS = 2
POOL_PLAN = {"steps": [
    {"tool": "search", "args": {"query": "q", "corpus": "rdr", "limit": 40}},
    {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "rdr", "limit_per_source": ANCHORS}},
    {"tool": "generate", "args": {"context": "$step2.contents", "question": "q"}},
]}
#: The query-tool twin of the plan above: no ``search`` step, so no pool.
NO_SEARCH_PLAN = {"steps": [
    {"tool": "query", "args": {"question": "q", "corpus": "rdr"}},
    {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "rdr", "limit_per_source": ANCHORS}},
    {"tool": "generate", "args": {"context": "$step2.contents", "question": "q"}},
]}


def _anchor(n: int, token: str) -> str:
    return f"decision message {n} raised by {token} in the channel"


def _cited(n: int, token: str) -> str:
    """A pool chunk the stub's check cites: it holds the topic word of f1 and f2."""
    return f"decision message {n} also raised by {token} in the channel"


def _uncited(n: int, token: str) -> str:
    """A pool chunk with a key and no topic word: the stub's check answers it with a neutral record."""
    return f"lunch order {n} for {token} and the usual table"


KEYLESS = "a decision note with no customer in it at all"


class PoolDispatcher(ResolveDispatcher):
    """The resolution tests' stub, with a search step of ``ANCHORS`` anchor texts followed by the pool texts, a plan
    ``store_get_many`` that honours ``limit_per_source`` (as the real one does), and a pool fetch that can fail."""

    def __init__(self, anchor_texts: list[str], pool_texts: list[str], check=CHECK, *, fail_pool_fetch: bool = False, **kw: Any) -> None:
        tokens = sorted({t for text in (*anchor_texts, *pool_texts) for t in _tokens_of(text)})
        super().__init__(anchor_texts, FakeCatalog(tokens), check, **kw)
        self.pool_texts = pool_texts
        self.pool_hashes = {_h(strip_source_note(t)) for t in pool_texts}
        self.by_hash |= {_h(strip_source_note(t)): t for t in pool_texts}
        self.fail_pool_fetch = fail_pool_fetch

    async def __call__(self, tool: str, args: dict) -> dict:
        if tool in ("search", "query") and not self.is_repair_search(args):
            self.calls.append((tool, dict(args)))
            return _structured([strip_source_note(t) for t in (*self.anchor_texts, *self.pool_texts)], RDR_COLL)
        if tool == "store_get_many" and args.get("ids") and {str(i) for i in args["ids"]} <= set(self.by_hash):
            self.calls.append((tool, dict(args)))
            ids = [str(i) for i in args["ids"]]
            if "limit_per_source" in args:
                ids = ids[: int(args["limit_per_source"])]
            elif self.fail_pool_fetch and set(ids) & self.pool_hashes:
                raise RuntimeError("pool fetch backend down")
            return {"contents": [self.by_hash[i] for i in ids]}
        return await super().__call__(tool, args)

    def pool_fetches(self) -> list[dict]:
        return [a for t, a in self.calls if t == "store_get_many" and "limit_per_source" not in a
                and {str(i) for i in a["ids"]} & self.pool_hashes]


async def _view_and_first(d: PoolDispatcher):
    """A boundary view over a finished run of ``POOL_PLAN`` (so its search step carries the pool) with the repair hooks
    the runner would hand it, and the first ledger: what ``_resolve_anchors`` is called with, for the tests that set the
    allowance themselves."""
    first_result = await _run(d, CoverageBoundary(request=_request(3), max_repair_queries=0), plan=POOL_PLAN)
    first = _cov(first_result)

    async def check(args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        return await d("operator_check", args)

    async def retrieve(tool: str, args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        return await d(tool, args)

    async def fetch(hits: list[Any], *, timeout: float) -> list[Any]:  # noqa: ARG001
        return [SupplementItem(text=d.by_hash[h.id], chash=h.id, collection=h.collection, tumbler=h.tumbler) for h in hits]

    view = BoundaryView(
        evidence_texts=[a.content for a in first.ledger.anchors], observed=True, step_outputs=list(first_result.steps),
        tools=["search", "store_get_many", "generate"], scope_diagnostics=[], resolved_args=[],
        check=check, remaining_usd=lambda: None, remaining_seconds=lambda: None, retrieve=retrieve, fetch=fetch,
        install_supplement=lambda items: None, rehydrate=lambda: ([a.content for a in first.ledger.anchors], True),
    )
    return view, first


def _soon(seconds: float = 30.0) -> float:
    return time.monotonic() + seconds


def _tokens_of(text: str) -> list[str]:
    return re.findall(PATTERN, text)


@pytest.fixture(autouse=True)
def _no_catalog_lookup():
    with patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()):
        yield


@pytest.fixture(autouse=True)
def _entity_config(monkeypatch: pytest.MonkeyPatch) -> None:
    real = _config.load_config

    def with_entity(*a: Any, **k: Any) -> dict:
        return {**real(*a, **k), "entity": {"collections": {"rdr__*": PATTERN}}}

    monkeypatch.setattr(_config, "load_config", with_entity)


def pool_on(**kw: Any) -> CoverageBoundary:
    return CoverageBoundary(request=_request(3), resolution_repair=True, pool_key_scan=True, **kw)


def pool_off(**kw: Any) -> CoverageBoundary:
    return CoverageBoundary(request=_request(3), resolution_repair=True, **kw)


def _fixture(*, check=CHECK, **kw: Any) -> PoolDispatcher:
    """Anchors name CUST-0001 and CUST-0002. The pool: a keyless chunk, a chunk naming CUST-0007 that the check
    cites, a chunk naming CUST-0008 that it does not, and a chunk naming CUST-0001 again (an anchor key, so no candidate)."""
    return PoolDispatcher(
        [_anchor(1, _tok(1)), _anchor(2, _tok(2))],
        [KEYLESS, _cited(7, _tok(7)), _uncited(8, _tok(8)), _cited(9, _tok(1))],
        check, **kw)


# ── the switch ────────────────────────────────────────────────────────────────────────────────────────────


def test_the_switch_is_off_by_default_and_a_bare_boundary_does_not_scan_the_pool() -> None:
    assert switches.ANSWER_POOL_KEY_SCAN_ENABLED is False
    assert switches.pool_key_scan_enabled({}) is False
    assert CoverageBoundary(request=None).pool_key_scan is False


@pytest.mark.parametrize(("raw", "expected"), [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("", False), ("  ", False), ("0", False), ("off", False), ("NO", False), ("garbage", False),
])
def test_the_switch_reads_an_on_value_as_on_and_unset_off_or_unrecognised_as_off(raw: str, expected: bool) -> None:
    switches.reset_warned()
    assert switches.pool_key_scan_enabled({switches.ANSWER_POOL_KEY_SCAN_ENV: raw}) is expected


def test_the_suite_clears_the_switch_for_every_test() -> None:
    """The conftest must ``delenv`` the switch before every test, so a developer's export cannot leak in."""
    tree = ast.parse((Path(__file__).parent / "conftest.py").read_text(encoding="utf-8"))
    cleared = {
        call.args[0].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "delenv"
        and call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
    }
    assert switches.ANSWER_POOL_KEY_SCAN_ENV in cleared, "the conftest no longer clears the switch for every test"
    assert switches.ANSWER_POOL_KEY_SCAN_ENV not in os.environ


# ── scenario 1: the switch off ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_1_with_the_switch_off_the_pool_is_not_read_and_the_report_has_no_pool_field() -> None:
    d = _fixture()
    cov = _cov(await _run(d, pool_off(), plan=POOL_PLAN))
    r = cov.repair
    assert r.resolution_entered and r.anchor_key_ids == (_tok(1), _tok(2))
    assert d.resolve_calls() == [[_tok(1), _tok(2)]], "the anchors' keys only"
    assert d.pool_fetches() == [], "the code path is not entered: the pool chunks are never fetched"
    wire = r.to_wire()
    assert not [k for k in wire if k.startswith("pool_")], "off is the wire of today"
    assert r.pool_key_scan is False and r.pool_scanned == 0 and r.pool_reason == ""
    # the same run with the switch spelled out False is the same wire
    d2 = _fixture()
    again = _cov(await _run(d2, pool_off(pool_key_scan=False), plan=POOL_PLAN)).repair
    assert {k: v for k, v in again.to_wire().items() if k != "latency_ms"} == {k: v for k, v in wire.items() if k != "latency_ms"}
    assert d.tools == d2.tools


# ── scenario 2 and 3: a cited key is kept, an uncited key is rejected ─────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_2_a_pool_key_the_assessment_cites_is_resolved_and_joined_to_its_pool_chunk() -> None:
    d = _fixture()
    cov = _cov(await _run(d, pool_on(), plan=POOL_PLAN))
    r = cov.repair
    assert r.pool_key_scan is True and r.pool_reason == ""
    assert r.pool_scanned == 4, "the non-anchor ids of the search step, fetched once"
    assert len(d.pool_fetches()) == 1 and len(d.pool_fetches()[0]["ids"]) == 4
    assert r.pool_candidate_keys == (_tok(7), _tok(8)), "keys found only in the pool; the repeat of an anchor key is no candidate"
    assert r.pool_keys_kept == (_tok(7),) and r.pool_keys_rejected == (_tok(8),) and r.pool_filter_calls == 1
    assert r.anchors_scanned == ANCHORS, "anchors_scanned keeps its meaning: the anchors only"
    assert d.resolve_calls() == [[_tok(1), _tok(2)], [_tok(7)]], "the anchors' keys first, in their own call, then the kept pool key"
    assert r.anchor_key_ids == (_tok(1), _tok(2)), "anchor_key_ids stays anchors-only: pool keys are reported in pool_keys_kept"
    assert r.resolved == (_tok(1), _tok(2), _tok(7)) and r.joined_added == 3 and r.resolution_tokens == 3
    # the joined item for the pool key carries the pool chunk's own text as its anchor text
    joined = _cited(7, _tok(7)) + JOINED_SEP + f"{_tok(7)}: {_record_text(_tok(7))}"
    assert joined in [a.content for a in cov.ledger.anchors]
    assert cov.ledger.finding("f3").state == "covered" and cov.state == "complete"
    # the filter is the boundary's own assessment: its instruction names the request's facets, over the pool chunks only
    filter_call = next(a for a in d.args_of("operator_check") if any(i["id"].startswith("p") for i in json.loads(a["items"])))
    assert {i["text"] for i in json.loads(filter_call["items"])} == {_cited(7, _tok(7)), _uncited(8, _tok(8))}
    assert all(f"[f{n}]" in filter_call["check_instruction"] for n in (1, 2, 3))
    wire = r.to_wire()
    assert wire["pool_keys_kept"] == [_tok(7)] and wire["pool_keys_rejected"] == [_tok(8)] and wire["pool_filter_calls"] == 1


@pytest.mark.asyncio
async def test_scenario_3_a_pool_key_the_assessment_does_not_cite_is_rejected_by_name_and_never_resolved() -> None:
    """Non-vacuity: the filter must be able to reject. The same pool, with a check that cites nothing, keeps nothing."""
    d = _fixture(check=by_topic(never=("f1", "f2", "f3")))
    cov = _cov(await _run(d, pool_on(), plan=POOL_PLAN))
    r = cov.repair
    assert r.pool_candidate_keys == (_tok(7), _tok(8)) and r.pool_keys_kept == ()
    assert r.pool_keys_rejected == (_tok(7), _tok(8)), "named, and a rejection is not a failure"
    assert r.pool_reason == "" and r.pool_filter_calls == 1
    assert d.resolve_calls() == [[_tok(1), _tok(2)]], "neither pool key reached resolve"
    # and the cited run of the same pool differs: the filter, not the scan, decides
    kept = _cov(await _run(_fixture(), pool_on(), plan=POOL_PLAN)).repair
    assert kept.pool_keys_kept == (_tok(7),) and kept.pool_keys_rejected == (_tok(8),)


def _contradicts_cited_supports_uncited(items: str, instruction: str) -> Any:
    """On the pool's call: the CUST-0008 chunk supports f1 (so f1 is covered) and the CUST-0007 chunk only contradicts it."""
    payload = json.loads(items)
    if not any(i["id"].startswith("p") for i in payload):
        return CHECK(items, instruction)
    by_key = {t: i["id"] for i in payload for t in (_tok(7), _tok(8)) if t in i["text"]}
    return {"ok": True, "evidence": [
        {"item_id": by_key[_tok(8)], "quote": "[f1] supports it", "role": "supports"},
        {"item_id": by_key[_tok(7)], "quote": "[f1] says the opposite", "role": "contradicts"},
    ]}


@pytest.mark.asyncio
async def test_a_pool_chunk_cited_only_as_contradicting_is_rejected_though_its_facet_is_covered() -> None:
    """The precision hole: a covered facet's ``evidence_ids`` holds its contradicting ids too, so reading them would keep a
    key whose chunk argues against the facet. Only a ``supports`` record keeps a key."""
    d = _fixture(check=_contradicts_cited_supports_uncited)
    r = _cov(await _run(d, pool_on(), plan=POOL_PLAN)).repair
    assert r.pool_reason == "" and r.pool_filter_calls == 1
    assert r.pool_candidate_keys == (_tok(7), _tok(8))
    assert r.pool_keys_kept == (_tok(8),) and r.pool_keys_rejected == (_tok(7),), "the supporter is kept, the contradicter is not"
    assert d.resolve_calls() == [[_tok(1), _tok(2)], [_tok(8)]]


@pytest.mark.asyncio
async def test_a_pool_with_no_key_makes_no_filter_call() -> None:
    d = PoolDispatcher([_anchor(1, _tok(1)), _anchor(2, _tok(2))], [KEYLESS, _cited(9, _tok(2))])
    r = _cov(await _run(d, pool_on(), plan=POOL_PLAN)).repair
    assert (r.pool_scanned, r.pool_candidate_keys, r.pool_filter_calls, r.pool_reason) == (2, (), 0, "")
    assert d.resolve_calls() == [[_tok(1), _tok(2)]]
    assert len(d.args_of("operator_check")) == 2, "the first round's one vector call and the re-check; no filter"


# ── scenario 4: the filter fails closed ───────────────────────────────────────────────────────────────────


def _on_pool(inner):
    """A check that misbehaves on the pool's call only (its items are ``p<n>``), and is *inner* on every other."""
    def check(items: str, instruction: str) -> Any:
        if any(i["id"].startswith("p") for i in json.loads(items)):
            return inner(items, instruction)
        return CHECK(items, instruction)
    return check


def _raise(items: str, instruction: str) -> Any:  # noqa: ARG001
    return RuntimeError("check backend down")


def _malformed(items: str, instruction: str) -> dict:  # noqa: ARG001
    return {"ok": True}  # no evidence list: not the check shape


def _no_records(items: str, instruction: str) -> dict:  # noqa: ARG001
    return {"ok": False, "evidence": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(("inner", "reason"), [
    (_raise, "filter_failed"), (_malformed, "filter_failed"),
])
async def test_scenario_4_a_filter_that_fails_or_replies_unusably_keeps_no_pool_key_and_the_anchors_still_resolve(inner, reason: str) -> None:
    d = _fixture(check=_on_pool(inner))
    cov = _cov(await _run(d, pool_on(), plan=POOL_PLAN))
    r = cov.repair
    assert r.pool_reason == reason and r.pool_keys_kept == ()
    assert r.pool_filter_calls == 1, "a call that raised or replied unusably still started"
    assert r.pool_keys_rejected == (_tok(7), _tok(8)), "fail closed: every candidate is named as not kept"
    assert d.resolve_calls() == [[_tok(1), _tok(2)]]
    assert r.resolution_entered and r.resolved == (_tok(1), _tok(2)) and r.outcome == "completed", "the anchors' keys resolve as today"


@pytest.mark.asyncio
async def test_scenario_4_a_failed_pool_fetch_keeps_no_pool_key_and_the_anchors_still_resolve() -> None:
    d = _fixture(fail_pool_fetch=True)
    r = _cov(await _run(d, pool_on(), plan=POOL_PLAN)).repair
    assert r.pool_reason == "fetch_failed" and r.pool_scanned == 0 and r.pool_candidate_keys == () and r.pool_filter_calls == 0
    assert d.resolve_calls() == [[_tok(1), _tok(2)]] and r.resolution_entered and r.resolved == (_tok(1), _tok(2))


class _SlowFilter(PoolDispatcher):
    """The filter's call hangs (the pool's items are ``p<n>``); everything else answers at once."""

    async def __call__(self, tool: str, args: dict) -> dict:
        if tool == "operator_check" and any(i["id"].startswith("p") for i in json.loads(args["items"])):
            await asyncio.sleep(30)
        return await super().__call__(tool, args)


def _anchors_resolved(res: Any) -> None:
    """Phase A ran: the anchors' keys went to ``resolve`` and the branch did not fail, whatever the pool did."""
    assert res.failure == "" and res.entered and res.fields["resolved"][:2] == (_tok(1), _tok(2)), "the anchors' keys resolved"
    assert len(res.items) >= 2


@pytest.mark.asyncio
async def test_scenario_4_a_filter_that_outlives_its_slice_keeps_no_pool_key_and_never_starves_the_anchors_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The review blocker (probe 1): a hung filter used to take the time the anchors' ``resolve`` needed. Phase A now runs
    first, so the anchors' keys resolve whatever phase B does, and the filter is cut at its slice and counted."""
    monkeypatch.setattr(coverage_pool, "_TAIL_S", 0.5)
    d = _SlowFilter([_anchor(1, _tok(1)), _anchor(2, _tok(2))], [_cited(7, _tok(7))])
    view, first = await _view_and_first(d)
    started = time.monotonic()
    res = await replace(pool_on(), min_call_s=0.5)._resolve_anchors(view, first, resolve_until=started + 2.0, fetch_until=started + 2.0)
    _anchors_resolved(res)
    assert res.fields["pool_reason"] == "filter_timeout" and res.fields["pool_keys_kept"] == ()
    assert res.fields["pool_filter_calls"] >= 1, "a call that started and was cut still counts"
    assert res.fields["pool_keys_rejected"] == (_tok(7),)
    assert time.monotonic() - started < 5, "the filter was cut at its slice, not left to run"


@pytest.mark.asyncio
async def test_scenario_4_a_window_too_short_for_one_assessment_call_skips_the_pool_before_starting_it() -> None:
    """Probe 1's own window (two seconds): one call needs the boundary's 5 s minimum, so nothing is started, the filter is
    never sent and the anchors' keys still resolve."""
    d = _SlowFilter([_anchor(1, _tok(1)), _anchor(2, _tok(2))], [_cited(7, _tok(7))])
    view, first = await _view_and_first(d)
    started = time.monotonic()
    res = await pool_on()._resolve_anchors(view, first, resolve_until=started + 2.0, fetch_until=started + 2.0)
    _anchors_resolved(res)
    assert res.fields["pool_reason"] == "filter_skipped_deadline" and res.fields["pool_filter_calls"] == 0
    assert res.fields["pool_scanned"] == 0 and d.pool_fetches() == [], "not even the pool fetch was started"
    assert time.monotonic() - started < 1.5


@pytest.mark.asyncio
async def test_scenario_4_a_pool_fetch_that_times_out_never_starves_the_anchors_resolve() -> None:
    """Probe 2: a hung pool fetch. The anchors' record fetch (other ids) is untouched and resolved first."""
    d = PoolDispatcher([_anchor(1, _tok(1)), _anchor(2, _tok(2))], [_cited(7, _tok(7))])
    view, first = await _view_and_first(d)
    real = view.fetch

    async def slow_pool_fetch(hits: list[Any], *, timeout: float) -> list[Any]:
        if {h.id for h in hits} & d.pool_hashes:
            await asyncio.sleep(0.2)
            raise TimeoutError
        return await real(hits, timeout=timeout)

    started = time.monotonic()
    res = await pool_on()._resolve_anchors(
        replace(view, fetch=slow_pool_fetch), first, resolve_until=started + 30.0, fetch_until=started + 30.0)
    _anchors_resolved(res)
    assert res.fields["pool_reason"] == "fetch_failed" and res.fields["pool_scanned"] == 0 and res.fields["pool_filter_calls"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("usd", "seconds", "estimate", "reserve", "reason", "calls"), [
    (0.001, None, 0.01, None, "filter_skipped_budget", 0),    # what is left is below one call's mean cost
    (0.0, None, None, None, "filter_skipped_budget", 0),      # spent
    (0.05, None, 0.01, 0.045, "filter_skipped_budget", 0),    # the reassessment's reserve leaves less than one call
    (0.5, None, 0.01, None, "", 1),                           # affordable: the filter runs
    (0.5, None, 0.01, 0.4, "", 1),                            # affordable beside the reserve
    (None, None, 0.01, 0.4, "", 1),                           # no USD limit
    (0.001, None, None, None, "", 1),                         # cost unknown: proceed
    (None, 1.0, None, None, "filter_skipped_deadline", 0),    # the answer has under one call's time left
])
async def test_scenario_4_a_short_allowance_keeps_no_pool_key_and_an_unknown_cost_proceeds(usd, seconds, estimate, reserve, reason, calls) -> None:
    d = _fixture()
    view, first = await _view_and_first(d)
    view = replace(view, remaining_usd=lambda: usd, remaining_seconds=lambda: seconds, assessment_cost_estimate=lambda: estimate)
    res = await pool_on()._resolve_anchors(view, first, resolve_until=_soon(), fetch_until=_soon(), usd_reserve=reserve)
    _anchors_resolved(res)
    assert res.fields["pool_reason"] == reason and res.fields["pool_filter_calls"] == calls
    if reason:
        assert res.fields["pool_keys_kept"] == () and res.fields["pool_scanned"] == 0, "nothing was started"
        assert d.pool_fetches() == []
    else:
        assert res.fields["pool_keys_kept"] == (_tok(7),)


@pytest.mark.asyncio
async def test_scenario_4_a_request_with_no_facet_has_nothing_to_filter_by() -> None:
    d = _fixture()
    view, first = await _view_and_first(d)
    res = await replace(pool_on(), request=None)._resolve_anchors(view, first, resolve_until=_soon(), fetch_until=_soon())
    _anchors_resolved(res)
    assert res.fields["pool_reason"] == "no_facets" and res.fields["pool_keys_kept"] == () and res.fields["pool_filter_calls"] == 0
    assert res.fields["pool_keys_rejected"] == (_tok(7), _tok(8))


def _truncated(items: str, instruction: str) -> dict:  # noqa: ARG001
    return {"ok": False, "evidence": ['{"item_id": "p1", "quo']}  # a cut-off record: no mapping survived


def _neutral_only(items: str, instruction: str) -> dict:  # noqa: ARG001
    """A genuine reject: the assessor read the pack, judged its chunks, and cited none as support."""
    first = json.loads(items)[0]["id"]
    return {"ok": False, "evidence": [{"item_id": first, "quote": "[f1] only mentions the topic", "role": "neutral"}]}


@pytest.fixture
def info_logs():
    """structlog's filtering wrapper reads WARNING in the suite; the per-call event is info, so lower it and put it back."""
    import logging  # noqa: PLC0415

    import structlog  # noqa: PLC0415
    from structlog.testing import capture_logs  # noqa: PLC0415

    before = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    try:
        with capture_logs() as logs:
            yield logs
    finally:
        structlog.configure(wrapper_class=before)


def _filter_log(r: Any) -> list[dict]:
    return [dict(c) for c in r.pool_filter_log]


@pytest.mark.asyncio
@pytest.mark.parametrize(("inner", "reason", "outcome", "empty"), [
    (_no_records, "filter_empty_reply", "empty", 1),
    (_truncated, "filter_empty_reply", "empty", 1),
    (_malformed, "filter_failed", "malformed", 0),
    (_raise, "filter_failed", "raised", 0),
    (_neutral_only, "", "no_support", 0),
])
async def test_each_reply_class_of_the_filter_is_named_and_only_a_judged_pack_is_a_reject_verdict(inner, reason: str, outcome: str, empty: int) -> None:
    """nexus-w8d30: an empty reply is a failed assessment (the instruction says so), never the reject verdict it was read as
    in review round 1. A reply that held a record and cited none as support is the reject: no reason."""
    d = _fixture(check=_on_pool(inner))
    r = _cov(await _run(d, pool_on(), plan=POOL_PLAN)).repair
    assert r.pool_reason == reason and r.pool_filter_calls == 1
    assert r.pool_keys_kept == () and r.pool_keys_rejected == (_tok(7), _tok(8)), "every class fails closed"
    assert r.pool_filter_empty_calls == empty
    assert [c["outcome"] for c in _filter_log(r)] == [outcome]
    assert d.resolve_calls() == [[_tok(1), _tok(2)]]


@pytest.mark.asyncio
async def test_an_empty_reply_is_warned_about_and_a_judged_reject_is_not() -> None:
    from structlog.testing import capture_logs  # noqa: PLC0415

    with capture_logs() as empty_logs:
        _cov(await _run(_fixture(check=_on_pool(_no_records)), pool_on(), plan=POOL_PLAN))
    warned = [e for e in empty_logs if e["event"] == "nx_answer_pool_filter_empty_reply"]
    assert len(warned) == 1 and warned[0]["log_level"] == "warning" and warned[0]["reason"] == "filter_empty_reply"
    with capture_logs() as reject_logs:
        _cov(await _run(_fixture(check=_on_pool(_neutral_only)), pool_on(), plan=POOL_PLAN))
    assert not [e for e in reject_logs if e["event"] == "nx_answer_pool_filter_empty_reply"]


@pytest.mark.asyncio
async def test_a_two_pack_filter_with_one_failed_pack_keeps_what_the_completed_pack_cited() -> None:
    """One chunk a pack (``max_anchors_per_call=1``): the pack holding CUST-0008 raises, the one holding CUST-0007 cites it."""
    def check(items: str, instruction: str) -> Any:
        payload = json.loads(items)
        if payload[0]["id"].startswith("p") and _tok(8) in payload[0]["text"]:
            return RuntimeError("one pack's call failed")
        return CHECK(items, instruction)

    d = _fixture(check=check)
    r = _cov(await _run(d, pool_on(max_anchors_per_call=1), plan=POOL_PLAN)).repair
    assert r.pool_filter_calls == 2 and r.pool_reason == "filter_partial_failure"
    assert r.pool_keys_kept == (_tok(7),) and r.pool_keys_rejected == (_tok(8),), "the failed pack's candidate is rejected, by name"
    assert d.resolve_calls() == [[_tok(1), _tok(2)], [_tok(7)]]


@pytest.mark.asyncio
async def test_a_request_of_two_facet_groups_keeps_a_key_either_group_cites() -> None:
    """Seven facets make two groups (six and one) of the one pack. Group one says nothing (an empty reply, a verdict), group
    two cites the CUST-0007 chunk for f7: the key is kept, and the empty reply is a failed call, so the filter is partial."""
    def check(items: str, instruction: str) -> Any:
        payload = json.loads(items)
        if not payload[0]["id"].startswith("p"):
            return CHECK(items, instruction)
        if "[f7]" not in instruction:
            return {"ok": False, "evidence": []}
        seven = next(i["id"] for i in payload if _tok(7) in i["text"])
        return {"ok": True, "evidence": [{"item_id": seven, "quote": "[f7] it supports", "role": "supports"}]}

    d = _fixture(check=check)
    boundary = CoverageBoundary(request=_request(7), resolution_repair=True, pool_key_scan=True)
    r = _cov(await _run(d, boundary, plan=POOL_PLAN)).repair
    assert r.pool_filter_calls == 2 and r.pool_reason == "filter_partial_failure" and r.pool_filter_empty_calls == 1
    assert r.pool_keys_kept == (_tok(7),) and r.pool_keys_rejected == (_tok(8),)


@pytest.mark.asyncio
async def test_a_candidate_chunk_the_size_cap_left_unread_is_rejected_with_a_named_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    d = _fixture()
    view, first = await _view_and_first(d)
    monkeypatch.setattr(coverage_ledger, "ASSESSMENT_MAX_ITEM_CHARS", len(_cited(7, _tok(7))) + 5)  # room for one chunk only
    res = await pool_on()._resolve_anchors(view, first, resolve_until=_soon(), fetch_until=_soon())
    _anchors_resolved(res)
    assert res.fields["pool_keys_kept"] == (_tok(7),) and res.fields["pool_keys_rejected"] == (_tok(8),)
    assert res.fields["pool_reason"] == "filter_unread", "an unread candidate is not left to read as a judged one"


@pytest.mark.asyncio
async def test_the_pool_phase_runs_after_phase_a_and_does_not_run_when_phase_a_failed() -> None:
    d = _fixture(fail_resolve=True)
    r = _cov(await _run(d, pool_on(), plan=POOL_PLAN)).repair
    assert r.outcome == "retrieval_failed" and r.pool_reason == "resolution_failed"
    assert d.pool_fetches() == [] and r.pool_filter_calls == 0


@pytest.mark.asyncio
async def test_an_unexpected_exception_in_the_pool_phase_leaves_phase_a_standing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> float:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(coverage_pool, "_need_s", boom)
    d = _fixture()
    view, first = await _view_and_first(d)
    res = await pool_on()._resolve_anchors(view, first, resolve_until=_soon(), fetch_until=_soon())
    _anchors_resolved(res)
    assert res.fields["pool_reason"] == "pool_error" and res.fields["pool_keys_kept"] == ()


@pytest.mark.asyncio
async def test_a_boundary_with_no_fetch_hook_cannot_scan_the_pool() -> None:
    d = _fixture()
    view, first = await _view_and_first(d)
    res = await pool_on()._resolve_anchors(replace(view, fetch=None), first, resolve_until=_soon(), fetch_until=_soon())
    assert res.failure == "" and res.fields["pool_reason"] == "no_fetch" and res.fields["pool_scanned"] == 0


def test_a_filter_that_was_skipped_whole_maps_to_the_reason_it_was_skipped_for() -> None:
    tally = coverage_pool._Tally()
    verdict = coverage_pool._verdict
    assert verdict(AssessmentReport(outcome="skipped_budget", calls=0, skipped_budget_calls=1), tally) == "filter_skipped_budget"
    assert verdict(AssessmentReport(outcome="skipped_deadline", calls=0, skipped_deadline_calls=1), tally) == "filter_skipped_deadline"
    assert verdict(AssessmentReport(outcome="partial_failure", calls=1, skipped_budget_calls=1), tally) == "filter_partial_failure"
    assert verdict(AssessmentReport(outcome="ok", calls=1), tally) == ""


@pytest.mark.asyncio
async def test_the_filter_calls_and_their_cost_are_reported_and_a_cut_call_still_counts() -> None:
    """``usage_cost`` makes each assessment call report 0.01 USD to the run; the filter's share is its calls' sum."""
    d = _fixture(usage_cost=0.01)
    r = _cov(await _run(d, pool_on(), plan=POOL_PLAN)).repair
    assert r.pool_filter_calls == 1 and r.pool_filter_cost_usd == pytest.approx(0.01)
    assert r.to_wire()["pool_filter_cost_usd"] == pytest.approx(0.01)
    off = _cov(await _run(_fixture(usage_cost=0.01), pool_off(), plan=POOL_PLAN)).repair
    assert off.pool_filter_cost_usd is None and "pool_filter_cost_usd" not in off.to_wire()


@pytest.mark.asyncio
async def test_the_run_summary_carries_the_pool_counts_only_with_the_switch_on() -> None:
    from nexus.plans.run_summary import _resolution_counts  # noqa: PLC0415 — the summary's own builder

    on = _resolution_counts(_cov(await _run(_fixture(usage_cost=0.01), pool_on(), plan=POOL_PLAN)))
    assert (on["pool_scanned"], on["pool_candidate_keys"], on["pool_keys_kept"], on["pool_keys_rejected"]) == (4, 2, 1, 1)
    assert (on["pool_filter_calls"], on["pool_reason"]) == (1, "") and on["pool_filter_cost_usd"] == pytest.approx(0.01)
    assert on["resolution_entered"] is True, "the resolution counts are still there"
    off = _resolution_counts(_cov(await _run(_fixture(), pool_off(), plan=POOL_PLAN)))
    assert off and not [k for k in off if k.startswith("pool_")], "off is the summary it always was"


class _SecondResolveFails(PoolDispatcher):
    """``resolve`` answers phase A's keys and raises for the pool's (the call that names only CUST-0007)."""

    async def __call__(self, tool: str, args: dict) -> dict:
        if tool == "resolve" and args["tokens"] == [_tok(7)]:
            self.calls.append((tool, dict(args)))
            raise RuntimeError("catalog down for the pool keys")
        return await super().__call__(tool, args)


@pytest.mark.asyncio
async def test_a_failed_second_resolve_moves_the_kept_keys_to_rejected_and_overrides_an_earlier_reason() -> None:
    """Two packs, the one holding CUST-0008 raises (``filter_partial_failure``), the one that cites CUST-0007 completes; then
    the pool's own resolve fails. Nothing is kept, both candidates are rejected, the reason is the later one, and the anchors'
    keys stand."""
    def check(items: str, instruction: str) -> Any:
        payload = json.loads(items)
        if payload[0]["id"].startswith("p") and _tok(8) in payload[0]["text"]:
            return RuntimeError("one pack's call failed")
        return CHECK(items, instruction)

    d = _SecondResolveFails([_anchor(1, _tok(1)), _anchor(2, _tok(2))], [_cited(7, _tok(7)), _uncited(8, _tok(8))], check)
    r = _cov(await _run(d, pool_on(max_anchors_per_call=1), plan=POOL_PLAN)).repair
    assert r.pool_reason == "pool_resolve_failed"
    assert r.pool_keys_kept == () and r.pool_keys_rejected == (_tok(7), _tok(8)) and r.pool_candidate_keys == (_tok(7), _tok(8))
    assert r.pool_filter_calls == 2, "the filter's calls are still counted"
    assert r.resolution_entered and r.resolved == (_tok(1), _tok(2)) and r.joined_added == 2 and r.outcome == "completed"


@pytest.mark.asyncio
async def test_the_pools_resolve_stops_two_seconds_before_the_end_of_the_retrieval_window(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[float, float]] = []
    real = CoverageBoundary._join_keys

    async def spy(self: CoverageBoundary, view: Any, pairs: Any, scan: Any, *, cap: int, resolve_until: float, fetch_until: float) -> Any:
        seen.append((resolve_until, fetch_until))
        return await real(self, view, pairs, scan, cap=cap, resolve_until=resolve_until, fetch_until=fetch_until)

    d = _fixture()
    view, first = await _view_and_first(d)
    monkeypatch.setattr(CoverageBoundary, "_join_keys", spy)
    until = time.monotonic() + 30
    await pool_on()._resolve_anchors(view, first, resolve_until=until, fetch_until=until)
    assert len(seen) == 2 and seen[0] == (until, until), "phase A keeps the stamps it was given"
    assert seen[1] == (until - 2.0, until), "the pool's resolve ends two seconds early, leaving its record fetch the rest"


@pytest.mark.asyncio
async def test_a_pool_skipped_for_time_reports_the_seconds_one_call_needs_and_the_seconds_it_had(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deadline of 100 s gives the repair 30 s and its retrieval window 12 s; a tail of 8 s leaves the filter about 4 s, under the
    5 s one call needs. The wire and the run summary carry both numbers, and only with the switch on."""
    from nexus.plans.run_summary import _resolution_counts  # noqa: PLC0415 — the summary's own builder

    monkeypatch.setattr(coverage_pool, "_TAIL_S", 8.0)
    d = _fixture()
    cov = _cov(await _run(d, pool_on(), plan=POOL_PLAN, deadline=time.monotonic() + 100))
    r = cov.repair
    assert r.pool_reason == "filter_skipped_deadline" and r.pool_scanned == 0 and d.pool_fetches() == []
    assert r.pool_need_s == pytest.approx(5.0) and r.pool_room_s is not None and 0 < r.pool_room_s < 5.0
    assert r.to_wire()["pool_need_s"] == r.pool_need_s and r.to_wire()["pool_room_s"] == r.pool_room_s
    counts = _resolution_counts(cov)
    assert counts["pool_need_s"] == r.pool_need_s and counts["pool_room_s"] == r.pool_room_s
    ran = _cov(await _run(_fixture(), pool_on(), plan=POOL_PLAN)).repair
    assert ran.pool_need_s is None and ran.pool_room_s is None, "a pool that was not skipped names neither"


@pytest.mark.asyncio
async def test_a_run_whose_assessment_calls_report_no_cost_reports_the_filter_cost_as_unknown() -> None:
    r = _cov(await _run(_fixture(), pool_on(), plan=POOL_PLAN)).repair
    assert r.pool_filter_calls == 1 and r.pool_filter_cost_usd is None
    assert r.to_wire()["pool_filter_cost_usd"] is None


@pytest.mark.asyncio
async def test_a_filter_cut_by_the_outer_timeout_has_no_report_and_the_count_comes_from_the_tally(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_assess`` never returns (it started one call through the counting check, then hung), so the outer ``wait_for`` cuts it
    and there is no assessment report to read the calls from."""
    async def hang(self: CoverageBoundary, facets: Any, anchors: Any, view: Any, reserve: Any = None, support_ids: Any = None) -> Any:  # noqa: ARG001
        await view.check({"items": "[]", "check_instruction": ""}, timeout=1.0)
        await asyncio.sleep(30)

    monkeypatch.setattr(coverage_pool, "_TAIL_S", 0.5)
    monkeypatch.setattr(coverage_pool, "MIN_REPAIR_QUERY_S", 0.1)
    d = _fixture()
    view, first = await _view_and_first(d)  # the first round assesses normally; only the pool's filter hangs
    monkeypatch.setattr(CoverageBoundary, "_assess", hang)
    started = time.monotonic()
    res = await replace(pool_on(), min_call_s=0.5)._resolve_anchors(view, first, resolve_until=started + 5.0, fetch_until=started + 2.5)
    _anchors_resolved(res)
    assert res.fields["pool_reason"] == "filter_timeout" and res.fields["pool_filter_calls"] == 1
    assert res.fields["pool_keys_kept"] == () and res.fields["pool_keys_rejected"] == (_tok(7), _tok(8))
    assert time.monotonic() - started < 6


# ── the runner caller's wiring ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "expected"), [(None, False), ("", False), ("0", False), ("1", True), ("on", True), ("garbage", False)])
async def test_nx_answer_hands_the_boundary_the_pool_switch_it_reads_from_the_environment(monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool) -> None:
    from tests.test_rdr232_coverage_wiring import _drive  # noqa: PLC0415 — the one test that needs the whole nx_answer harness
    from nexus.plans.runner import PlanResult  # noqa: PLC0415

    monkeypatch.setenv(switches.ANSWER_COVERAGE_ENV, "1")
    if value is None:
        monkeypatch.delenv(switches.ANSWER_POOL_KEY_SCAN_ENV, raising=False)
    else:
        monkeypatch.setenv(switches.ANSWER_POOL_KEY_SCAN_ENV, value)
    run = await _drive(plan_result=PlanResult(steps=[{"text": "ok"}]), request=_request(2))
    [kw] = run.plan_run_calls
    assert kw["coverage_boundary"].pool_key_scan is expected


# ── scenario 5: no search step ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_5_a_plan_with_no_search_step_has_no_pool_and_says_so() -> None:
    d = _fixture()
    r = _cov(await _run(d, pool_on(), plan=NO_SEARCH_PLAN)).repair
    assert r.pool_key_scan is True and r.pool_reason == "no_search_step"
    assert (r.pool_scanned, r.pool_candidate_keys, r.pool_keys_kept, r.pool_filter_calls) == (0, (), (), 0)
    assert d.pool_fetches() == [] and d.resolve_calls() == [[_tok(1), _tok(2)]], "the branch behaves exactly as today"


# ── scenario 6: the cap ───────────────────────────────────────────────────────────────────────────────────


def _crowded() -> PoolDispatcher:
    return PoolDispatcher(
        [_anchor(1, _tok(1)), _anchor(2, _tok(2))],
        [_cited(7, _tok(7)), _cited(8, _tok(8)), _cited(9, _tok(9))])


@pytest.mark.asyncio
async def test_scenario_6_anchor_keys_fill_the_cap_first_and_the_overflow_is_named_in_tokens_dropped() -> None:
    d = _crowded()
    r = _cov(await _run(d, pool_on(max_repair_items=3), plan=POOL_PLAN)).repair
    assert r.pool_keys_kept == (_tok(7), _tok(8), _tok(9)), "all three passed the filter"
    assert d.resolve_calls() == [[_tok(1), _tok(2)], [_tok(7)]], "the two anchor keys, then the one pool key the cap leaves room for"
    assert r.tokens_dropped == (_tok(8), _tok(9)) and r.resolution_tokens == 3
    tight = _crowded()
    r2 = _cov(await _run(tight, pool_on(max_repair_items=2), plan=POOL_PLAN)).repair
    assert tight.resolve_calls() == [[_tok(1), _tok(2)]], "a cap the anchors fill leaves the pool keys no slot"
    assert r2.pool_reason == "cap_full" and r2.pool_filter_calls == 0 and r2.pool_scanned == 0
    assert tight.pool_fetches() == [] and len(tight.args_of("operator_check")) == 2, "no pool fetch and no filter call: nothing can change the answer"
    assert r2.tokens_dropped == () and r2.resolved == (_tok(1), _tok(2))


def test_the_wire_cuts_a_long_pool_list_at_the_shared_cap_and_counts_what_it_left_out() -> None:
    names = tuple(f"CUST-{n:04d}" for n in range(WIRE_KEY_LIST_CAP + 3))
    wire = RepairReport(
        resolution_entered=True, pool_key_scan=True, pool_candidate_keys=names, pool_keys_rejected=names).to_wire()
    assert len(wire["pool_candidate_keys"]) == WIRE_KEY_LIST_CAP and wire["pool_candidate_keys_truncated"] == 3
    assert wire["pool_keys_kept"] == [] and wire["pool_keys_kept_truncated"] == 0


def test_the_pool_reasons_are_a_closed_list() -> None:
    assert "no_search_step" in coverage_pool.POOL_REASONS and "filter_failed" in coverage_pool.POOL_REASONS
    assert "filter_empty_reply" in coverage_pool.POOL_REASONS
    assert len(set(coverage_pool.POOL_REASONS)) == len(coverage_pool.POOL_REASONS)
    assert RECORD_COLL  # the catalog fixture's collection is the one the resolved records live in
