# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-w8d30 (RDR-250): the pool filter's per-call log (``pool_filter_log`` and the ``nx_answer_pool_filter_call`` event), the
validation of a reply's records against the call's own item and facet ids, the runner's usage key, and the reply classes
the filter names. The fixtures and stubs are ``tests/test_rdr250_pool_key_scan.py``'s."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from nexus.operators.dispatch import OperatorError, OperatorOutputError, OperatorTimeoutError
from nexus.plans import coverage_ledger, coverage_pool
from nexus.plans.coverage_ledger import AssessmentReport
from nexus.plans.coverage_repair import RepairReport
from tests.test_rdr147_resolution_repair import CHECK, _tok
from tests.test_rdr232_coverage_repair import _run
from tests.test_rdr232_coverage_ledger import _cov
from tests.test_rdr250_pool_key_scan import (  # noqa: F401 — the autouse fixtures must be in this module's namespace
    KEYLESS,
    POOL_PLAN,
    PoolDispatcher,
    _anchor,
    _cited,
    _entity_config,
    _filter_log,
    _fixture,
    _no_catalog_lookup,
    _no_records,
    _on_pool,
    _raise,
    _uncited,
    info_logs,
    pool_off,
    pool_on,
)

pytestmark = pytest.mark.usefixtures("rowless_collection_cache")  # RDR-229 P2.9: a tenant-free test takes item 1's NoRow outcome without a listing


# ── nexus-w8d30: the filter logs what it sent and got back ─────────────────────────────────────────────────


def _pool_ids(items: str) -> list[str]:
    return [i["id"] for i in json.loads(items)]


def _supports_7(items: str, instruction: str) -> dict:
    """The pool call cites the CUST-0007 chunk for f1. It also puts a ``usage`` of its own in the reply, which is never read."""
    payload = json.loads(items)
    if not any(i["id"].startswith("p") for i in payload):
        return CHECK(items, instruction)
    seven = next(i["id"] for i in payload if _tok(7) in i["text"])
    return {"ok": True, "evidence": [{"item_id": seven, "quote": "[f1] it supports", "role": "supports"}], "usage": {"output_tokens": 41}}


@pytest.mark.asyncio
async def test_with_the_switch_on_each_filter_call_is_one_event_and_one_log_entry_of_hashes_ids_and_a_parse_outcome(info_logs: list) -> None:
    import hashlib  # noqa: PLC0415

    d = _fixture(check=_on_pool(_supports_7))
    r = _cov(await _run(d, pool_on(), plan=POOL_PLAN)).repair
    (entry,) = _filter_log(r)
    filter_call = next(a for a in d.args_of("operator_check") if any(i["id"].startswith("p") for i in json.loads(a["items"])))
    sent = f"{filter_call['items']}\n{filter_call['check_instruction']}"
    evidence = json.dumps(_supports_7(filter_call["items"], filter_call["check_instruction"])["evidence"], sort_keys=True, ensure_ascii=False)
    assert entry["payload_sha256"] == hashlib.sha256(sent.encode()).hexdigest() and "prompt_sha256" not in entry
    assert entry["reply_chars"] == len(evidence) and entry["reply_sha256"] == hashlib.sha256(evidence.encode()).hexdigest()
    assert entry["outcome"] == "supports" and entry["call"] == 1 and entry["error"] is None
    assert entry["output_tokens"] is None, "a usage the model put in its reply is never read"
    assert len(entry["supports"]) == 1 and entry["supports"][0]["facet_id"] == "f1" and entry["supports"][0]["item_id"].startswith("p")
    assert (entry["supports_truncated"], entry["unmatched"], entry["host_rejected"]) == (0, 0, 0)
    events = [e for e in info_logs if e["event"] == "nx_answer_pool_filter_call"]
    assert len(events) == 1 and {k: v for k, v in events[0].items() if k not in ("event", "log_level")} == entry
    text = json.dumps(entry)  # never chunk text: no pool chunk's words reach the entry
    assert not any(t in text for t in (_cited(7, _tok(7)), _uncited(8, _tok(8)), "decision message"))
    wire = r.to_wire()
    assert wire["pool_filter_log"] == [entry] and wire["pool_filter_log_truncated"] == 0 and wire["pool_filter_empty_calls"] == 0


@pytest.mark.asyncio
async def test_a_reply_whose_call_reported_no_usage_has_null_token_counts() -> None:
    r = _cov(await _run(_fixture(), pool_on(), plan=POOL_PLAN)).repair
    (entry,) = _filter_log(r)
    assert entry["reply_chars"] > 0 and entry["supports"], "the stub's check reports no usage"
    assert (entry["output_tokens"], entry["input_tokens"], entry["model"], entry["cache_read_input_tokens"]) == (None, None, None, None)


class _UsageDispatcher(PoolDispatcher):
    """Reports a real-shaped ``DispatchUsage`` to the ambient sink on each assessment call, as ``claude_dispatch`` does."""

    tokens = 37

    async def __call__(self, tool: str, args: dict) -> dict:
        if tool == "operator_check":
            from nexus.operators.dispatch import DispatchUsage, _ambient_usage_sink  # noqa: PLC0415

            pool = any(i["id"].startswith("p") for i in json.loads(args["items"]))
            _ambient_usage_sink.get().append(DispatchUsage(
                model="m-1", cost_usd=0.01, input_tokens=912 if pool else 5, output_tokens=self.tokens if pool else 3,
                cache_creation_input_tokens=7, cache_read_input_tokens=11, duration_ms=1, duration_api_ms=1, num_turns=1))
        return await super().__call__(tool, args)


def _usage_fixture(check=CHECK, tokens: int = 37) -> _UsageDispatcher:
    d = _UsageDispatcher(
        [_anchor(1, _tok(1)), _anchor(2, _tok(2))], [KEYLESS, _cited(7, _tok(7)), _uncited(8, _tok(8)), _cited(9, _tok(1))], check)
    d.tokens = tokens
    return d


@pytest.mark.asyncio
async def test_the_calls_usage_reaches_the_filter_log_and_no_other_reader_sees_a_difference() -> None:
    """nexus-w8d30: the runner's check puts the call's usage beside the reply under its own key, so the counts are real."""
    cov = _cov(await _run(_usage_fixture(), pool_on(), plan=POOL_PLAN))
    (entry,) = _filter_log(cov.repair)
    assert (entry["output_tokens"], entry["input_tokens"], entry["model"]) == (37, 912, "m-1")
    assert (entry["cache_read_input_tokens"], entry["cache_creation_input_tokens"], entry["outcome"]) == (11, 7, "supports")
    plain = _cov(await _run(_fixture(), pool_on(), plan=POOL_PLAN))
    assert (cov.repair.pool_keys_kept, cov.repair.pool_keys_rejected, cov.state) == (
        plain.repair.pool_keys_kept, plain.repair.pool_keys_rejected, plain.state), "usage changes no verdict"


@pytest.mark.asyncio
async def test_a_model_supplied_usage_or_runner_key_is_replaced_by_the_runners_own() -> None:
    def forged(items: str, instruction: str) -> dict:
        reply = CHECK(items, instruction)
        return {**reply, "usage": {"output_tokens": 99999}, coverage_pool.DISPATCH_USAGE_KEY: {"output_tokens": 88888}}

    forged_on_a_run_with_usage = _cov(await _run(_usage_fixture(forged), pool_on(), plan=POOL_PLAN)).repair
    assert _filter_log(forged_on_a_run_with_usage)[0]["output_tokens"] == 37
    forged_on_a_run_without = _cov(await _run(_fixture(check=forged), pool_on(), plan=POOL_PLAN)).repair
    assert _filter_log(forged_on_a_run_without)[0]["output_tokens"] is None, "the model's key is stripped, not trusted"


@pytest.mark.asyncio
async def test_with_the_switch_off_the_runner_attaches_nothing_to_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []
    real = coverage_ledger._records

    def spy(reply: Any) -> Any:
        seen.append(dict(reply))
        return real(reply)

    monkeypatch.setattr(coverage_ledger, "_records", spy)
    _cov(await _run(_usage_fixture(), pool_off(), plan=POOL_PLAN))
    assert seen and not [r for r in seen if coverage_pool.DISPATCH_USAGE_KEY in r], "off: no key on any reply"
    seen.clear()
    _cov(await _run(_usage_fixture(), pool_on(), plan=POOL_PLAN))
    assert [r for r in seen if coverage_pool.DISPATCH_USAGE_KEY in r], "non-vacuity: on, the key is there"


@pytest.mark.asyncio
async def test_identical_evidence_hashes_the_same_whatever_the_call_cost_or_ok() -> None:
    def hashes(r: Any) -> list[tuple[str, int]]:
        return [(c["reply_sha256"], c["reply_chars"]) for c in _filter_log(r)]

    def with_ok(ok: bool):
        def check(items: str, instruction: str) -> dict:
            return {**_supports_7(items, instruction), "ok": ok}
        return check

    cheap = _cov(await _run(_usage_fixture(_on_pool(with_ok(True)), tokens=10), pool_on(), plan=POOL_PLAN)).repair
    dear = _cov(await _run(_usage_fixture(_on_pool(with_ok(False)), tokens=5000), pool_on(), plan=POOL_PLAN)).repair
    assert hashes(cheap) == hashes(dear) and _filter_log(cheap)[0]["output_tokens"] != _filter_log(dear)[0]["output_tokens"]


@pytest.mark.asyncio
async def test_a_raised_call_is_logged_with_its_error_class_and_no_reply_hash() -> None:
    raised = _cov(await _run(_fixture(check=_on_pool(_raise)), pool_on(), plan=POOL_PLAN)).repair
    (a,) = _filter_log(raised)
    assert a["outcome"] == "raised" and a["error"] == "RuntimeError" and a["reply_chars"] is None and a["reply_sha256"] is None
    assert len(a["payload_sha256"]) == 64


def _error(exc: BaseException):
    def check(items: str, instruction: str) -> Any:  # noqa: ARG001
        return exc
    return check


@pytest.mark.asyncio
@pytest.mark.parametrize(("exc", "outcome", "reason"), [
    (OperatorError("claude -p refused the request"), "raised", "filter_failed"),
    (OperatorOutputError("claude -p returned null structured_output"), "raised", "filter_failed"),
    (OperatorTimeoutError("claude -p exceeded the timeout"), "timeout", "filter_timeout"),
])
async def test_a_refused_unparsed_or_timed_out_call_names_its_cause_in_the_log_and_the_reason(exc: BaseException, outcome: str, reason: str) -> None:
    """The usage the runner recorded for the step rides on the exception, so a failed call still shows what it cost."""
    cov = _cov(await _run(_usage_fixture(_on_pool(_error(exc))), pool_on(), plan=POOL_PLAN))
    r = cov.repair
    (entry,) = _filter_log(r)
    assert (entry["outcome"], entry["error"]) == (outcome, type(exc).__name__)
    assert (entry["output_tokens"], entry["input_tokens"], entry["model"]) == (37, 912, "m-1"), "the failed step's usage is carried"
    assert r.pool_reason == reason and r.pool_keys_kept == () and r.pool_filter_empty_calls == 0


def _on_pool_reply(*records: dict, rejected: list | None = None):
    """A pool-call reply of the given records. Each record's ``item_id`` of ``"@first"`` means the pack's first item id."""
    def check(items: str, instruction: str) -> dict:  # noqa: ARG001
        first = _pool_ids(items)[0]
        evidence = [{**r, "item_id": first if r["item_id"] == "@first" else r["item_id"]} for r in records]
        return {"ok": False, "evidence": evidence, **({"rejected": rejected} if rejected is not None else {})}
    return check


def _rec(item_id: str, quote: str = "[f1] it says so", role: str = "supports") -> dict:
    return {"item_id": item_id, "quote": quote, "role": role}


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply", "outcome", "reason", "unmatched", "host", "empty"), [
    pytest.param(_on_pool_reply(_rec("zz9")), "all_rejected", "filter_failed", 1, 0, 0, id="out-of-range-id"),
    pytest.param(_on_pool_reply(_rec("@first", "[f99] no such facet")), "all_rejected", "filter_failed", 1, 0, 0, id="bad-facet-tag"),
    pytest.param(_on_pool_reply(_rec("@first", "no tag at all")), "all_rejected", "filter_failed", 1, 0, 0, id="no-facet-tag"),
    pytest.param(_on_pool_reply(_rec("@first", role="proves")), "all_rejected", "filter_failed", 1, 0, 0, id="bad-role"),
    pytest.param(_on_pool_reply(_rec("x" * 360 + " " + _cited(7, _tok(7)))), "all_rejected", "filter_failed", 1, 0, 0, id="text-bearing-id"),
    pytest.param(_on_pool_reply(rejected=["zz9"]), "all_rejected", "filter_failed", 0, 1, 0, id="host-dropped-the-only-record"),
    pytest.param(_on_pool_reply(), "empty", "filter_empty_reply", 0, 0, 1, id="no-record-and-none-dropped"),
    pytest.param(_on_pool_reply(_rec("@first", role="neutral"), _rec("zz9")), "no_support", "", 1, 0, 0, id="neutral-beside-a-ghost"),
])
async def test_a_record_the_ledger_would_not_accept_is_counted_never_logged_by_its_id_and_never_a_reject_or_an_empty(
    reply, outcome: str, reason: str, unmatched: int, host: int, empty: int,
) -> None:
    r = _cov(await _run(_fixture(check=_on_pool(reply)), pool_on(), plan=POOL_PLAN)).repair
    (entry,) = _filter_log(r)
    assert (entry["outcome"], entry["unmatched"], entry["host_rejected"]) == (outcome, unmatched, host)
    assert r.pool_reason == reason and r.pool_filter_empty_calls == empty and r.pool_keys_kept == ()
    assert entry["supports"] == [], "no unmatched id is a supports record"
    assert "xxxxxxxx" not in json.dumps(entry) and len(json.dumps(entry)) < 1500, "a model-written string never reaches the log"
    assert "xxxxxxxx" not in json.dumps(r.to_wire()["pool_filter_log"])


@pytest.mark.asyncio
async def test_a_supports_record_beside_a_ghost_keeps_only_the_matched_pair_and_counts_the_ghost() -> None:
    d = _fixture(check=_on_pool(_on_pool_reply(_rec("@first"), _rec("zz9"), _rec("y" * 360))))
    r = _cov(await _run(d, pool_on(), plan=POOL_PLAN)).repair
    (entry,) = _filter_log(r)
    assert entry["outcome"] == "supports" and entry["unmatched"] == 2 and len(entry["supports"]) == 1
    assert entry["supports"][0]["facet_id"] == "f1" and entry["supports"][0]["item_id"].startswith("p")
    assert r.pool_reason == "" and len(r.pool_keys_kept) == 1, "the matched record is what the ledger kept too"


@pytest.mark.asyncio
async def test_with_the_switch_off_there_is_no_filter_event_log_or_summary_field(info_logs: list) -> None:
    from nexus.plans.run_summary import _resolution_counts  # noqa: PLC0415

    cov = _cov(await _run(_fixture(check=_on_pool(_supports_7)), pool_off(), plan=POOL_PLAN))
    assert not [e for e in info_logs if e["event"].startswith("nx_answer_pool_")]
    assert cov.repair.pool_filter_log == () and not [k for k in cov.repair.to_wire() if k.startswith("pool_")]
    assert not [k for k in _resolution_counts(cov) if k.startswith("pool_")]
    on = _resolution_counts(_cov(await _run(_fixture(check=_on_pool(_supports_7)), pool_on(), plan=POOL_PLAN)))
    assert [e for e in info_logs if e["event"] == "nx_answer_pool_filter_call"], "non-vacuity: the same capture sees the event with the switch on"
    assert on["pool_filter_empty_calls"] == 0 and len(on["pool_filter_log"]) == 1 and on["pool_filter_log_truncated"] == 0
    assert set(on["pool_filter_log"][0]) == {"call", "outcome", "error", "output_tokens", "reply_chars", "supports", "unmatched", "host_rejected"}
    assert on["pool_filter_log"][0]["outcome"] == "supports" and on["pool_filter_log"][0]["supports"] == 1


@pytest.mark.asyncio
async def test_the_hashes_are_stable_for_identical_input_and_move_when_the_input_does() -> None:
    def hashes(r: Any) -> list[tuple[str, str]]:
        return [(c["payload_sha256"], c["reply_sha256"]) for c in _filter_log(r)]

    one = _cov(await _run(_fixture(check=_on_pool(_supports_7)), pool_on(), plan=POOL_PLAN)).repair
    two = _cov(await _run(_fixture(check=_on_pool(_supports_7)), pool_on(), plan=POOL_PLAN)).repair
    assert hashes(one) == hashes(two) and len(hashes(one)) == 1
    other = PoolDispatcher(
        [_anchor(1, _tok(1)), _anchor(2, _tok(2))], [KEYLESS, _cited(7, _tok(7)), _uncited(8, _tok(8)) + " and more", _cited(9, _tok(1))],
        _on_pool(_supports_7))
    three = _cov(await _run(other, pool_on(), plan=POOL_PLAN)).repair
    assert hashes(three)[0][0] != hashes(one)[0][0], "a different chunk is a different payload hash"


def test_the_filter_log_is_cut_at_the_wire_cap_and_says_how_many_it_left_out() -> None:
    from nexus.plans.coverage_repair import WIRE_FILTER_LOG_CAP  # noqa: PLC0415

    calls = tuple({"call": n} for n in range(1, WIRE_FILTER_LOG_CAP + 4))
    wire = RepairReport(resolution_entered=True, pool_key_scan=True, pool_filter_log=calls).to_wire()
    assert len(wire["pool_filter_log"]) == WIRE_FILTER_LOG_CAP and wire["pool_filter_log_truncated"] == 3


def test_the_run_summary_carries_scalars_only_and_stays_under_the_size_cap_with_a_full_log() -> None:
    from nexus.plans.coverage_repair import WIRE_FILTER_LOG_CAP  # noqa: PLC0415
    from nexus.plans.run_summary import RUN_SUMMARY_MAX_BYTES, canonical_json  # noqa: PLC0415
    from tests.test_rdr232_run_summary import _build, _coverage  # noqa: PLC0415

    entry = {
        "call": 1, "payload_sha256": "a" * 64, "reply_chars": 123456, "reply_sha256": "b" * 64, "input_tokens": 1_000_000,
        "output_tokens": 1_000_000, "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2, "model": "m" * 80,
        "supports": [{"item_id": f"p{n}", "facet_id": "f1"} for n in range(50)], "supports_truncated": 9, "unmatched": 40,
        "host_rejected": 40, "outcome": "all_rejected", "error": "E" * 80,
    }
    log = tuple({**entry, "call": n} for n in range(1, WIRE_FILTER_LOG_CAP + 11))
    cov = _coverage("partial")
    cov = replace(cov, repair=RepairReport(
        resolution_entered=True, pool_key_scan=True, pool_filter_calls=len(log), pool_filter_log=log, pool_reason="filter_failed"))
    summary = _build(coverage=cov)
    block = summary["repair"]["pool_filter_log"]
    assert len(block) == WIRE_FILTER_LOG_CAP and summary["repair"]["pool_filter_log_truncated"] == 10
    assert block[0]["supports"] == 59 and set(block[0]) == {"call", "outcome", "error", "output_tokens", "reply_chars", "supports", "unmatched", "host_rejected"}
    raw = canonical_json(summary)
    assert "payload_sha256" not in raw and "item_id" not in raw and "truncated" not in summary
    assert len(raw.encode("utf-8")) <= RUN_SUMMARY_MAX_BYTES
    assert len(canonical_json(block).encode("utf-8")) < 8192, "the per-call scalars are small"


@pytest.mark.asyncio
async def test_a_call_that_cannot_be_described_leaves_the_verdict_where_it_was(monkeypatch: pytest.MonkeyPatch, info_logs: list) -> None:
    """Logging never moves a verdict: with the describer broken, the empty reply is still counted, still named."""
    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("describer broke")

    monkeypatch.setattr(coverage_pool, "_call_record", boom)
    r = _cov(await _run(_fixture(check=_on_pool(_no_records)), pool_on(), plan=POOL_PLAN)).repair
    assert r.pool_reason == "filter_empty_reply" and r.pool_filter_empty_calls == 1 and r.pool_keys_kept == ()
    assert [dict(c)["outcome"] for c in r.pool_filter_log] == ["unlogged"]
    assert [e for e in info_logs if e["event"] == "nx_answer_pool_filter_log_failed"]
    kept = _cov(await _run(_fixture(check=_on_pool(_supports_7)), pool_on(), plan=POOL_PLAN)).repair
    assert kept.pool_keys_kept == (_tok(7),), "a broken describer keeps what the filter cited"


def test_a_filter_that_only_replied_empty_maps_to_filter_empty_reply_and_the_mixed_cases_to_their_causes() -> None:
    verdict = coverage_pool._verdict

    def tally(*, empty: int = 0, timed_out: bool = False) -> Any:
        t = coverage_pool._Tally()
        t.empty, t.timed_out = empty, timed_out
        return t

    assert verdict(AssessmentReport(outcome="failed", calls=1, unusable_calls=1), tally(empty=1)) == "filter_empty_reply"
    assert verdict(AssessmentReport(outcome="failed", calls=2, unusable_calls=1, raised_calls=1), tally(empty=1)) == "filter_failed"
    assert verdict(AssessmentReport(outcome="failed", calls=2, unusable_calls=2), tally(empty=1)) == "filter_failed", "one unusable reply was not empty"
    assert verdict(AssessmentReport(outcome="partial_failure", calls=2, unusable_calls=1), tally(empty=1)) == "filter_partial_failure"
    # empty beside a skipped call: the calls that ran were empty, so that is the cause named
    assert verdict(AssessmentReport(outcome="partial_failure", calls=1, unusable_calls=1, skipped_budget_calls=1), tally(empty=1)) == "filter_empty_reply"
    assert verdict(AssessmentReport(outcome="partial_failure", calls=1, unusable_calls=1, skipped_deadline_calls=1), tally(empty=1)) == "filter_empty_reply"
    # empty beside a call cut at its timeout: the cut wins
    assert verdict(AssessmentReport(outcome="failed", calls=2, unusable_calls=1, raised_calls=1), tally(empty=1, timed_out=True)) == "filter_timeout"
    # a budget cut with no call started
    assert verdict(AssessmentReport(outcome="skipped_budget", calls=0, skipped_budget_calls=1), tally()) == "filter_skipped_budget"
