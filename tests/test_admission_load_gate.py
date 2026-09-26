# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit coverage for ``tests/e2e/lib/admission_load.py`` (nexus-u2mlh.9).

Three layers, deliberately separated:

* The PURE core (counter extraction/comparison, per-step pass evaluation,
  ramp-stop decision, idle-precheck logic, nonce/subject/document
  generation, response summarization, transport-error validity) is
  exercised directly here, in-process, no network.
* The network-facing functions (``fetch_status``, ``post_upsert``,
  ``fire_step``, ``build_client``) are exercised against
  ``httpx.MockTransport`` — a real ``httpx.Client``, a real request/response
  round-trip, zero real sockets.
* ``run_gate`` itself is exercised end to end with every module-level
  network function monkeypatched (they are plain module globals ``run_gate``
  resolves by name at call time, so this works without any import-time
  indirection) plus a ``client_factory`` backed by ``httpx.MockTransport``
  for the two calls that go through the shared client (status reads, the
  ramp's own upserts).

The wrapper's mode detection (``tests/e2e/admission-load-gate.sh`` refuses
on a local-mode box) is exercised as a real subprocess with an isolated,
credential-free environment — still no network, since a local-mode
refusal is the FIRST thing the script does, before any driver invocation.

Never runs the real ramp: that would drive concurrent embed load against
the operator's live engine, which is exactly what this gate is for and
exactly what a test suite must never do on its own initiative.
"""
from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json as _json
import os
import subprocess
import sys
import time as _time
from pathlib import Path
from typing import Any

import httpx
import pytest

import nexus.db as nexus_db_module
import nexus.db.data_token as data_token_module
from nexus.db import service_endpoint as service_endpoint_module

REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULE_PATH = REPO_ROOT / "tests" / "e2e" / "lib" / "admission_load.py"
_SCRIPT_PATH = REPO_ROOT / "tests" / "e2e" / "admission-load-gate.sh"


def _load_module():
    spec = importlib.util.spec_from_file_location("admission_load", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses' own machinery looks the defining module up via
    # sys.modules[cls.__module__] -- registering BEFORE exec_module is
    # required, not optional, or every @dataclass in the file raises at
    # import time (confirmed: AttributeError on a bare module_from_spec).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


admission_load = _load_module()


def _status(*, active: bool = False, queue_depth: int = 0, admission: int = 0, deadline: int = 0) -> dict[str, Any]:
    return {
        "embedding_mode": "voyage",
        "embedder_activity": {
            admission_load.DEFAULT_EMBEDDER: {
                "active": active,
                "queue_depth": queue_depth,
                "admission_refusals_total": admission,
                "deadline_aborts_total": deadline,
                "chunks_done_total": 0,
                "thread_width": 12,
            }
        },
    }


IDLE_STATUS = _status()


# ── Counters + idle pre-check ────────────────────────────────────────────


def test_snapshot_counters_extracts_the_named_embedder_including_active() -> None:
    counters = admission_load.snapshot_counters(_status(active=True, queue_depth=5, admission=3, deadline=1), admission_load.DEFAULT_EMBEDDER)
    assert counters.admission_refusals_total == 3
    assert counters.deadline_aborts_total == 1
    assert counters.queue_depth == 5
    assert counters.active is True


@pytest.mark.parametrize(
    "status",
    [None, {}, {"embedder_activity": None}, {"embedder_activity": {}}, {"embedder_activity": {"voyage-context-3": None}}],
)
def test_snapshot_counters_is_fail_closed_to_zero_and_idle(status) -> None:
    assert admission_load.snapshot_counters(status, admission_load.DEFAULT_EMBEDDER) == admission_load.EmbedderCounters()


def test_is_idle_true_only_when_inactive_and_queue_drained() -> None:
    assert admission_load.is_idle(admission_load.EmbedderCounters(active=False, queue_depth=0)) is True
    assert admission_load.is_idle(admission_load.EmbedderCounters(active=False, queue_depth=-1)) is True  # tolerant of a negative sentinel
    assert admission_load.is_idle(admission_load.EmbedderCounters(active=True, queue_depth=0)) is False
    assert admission_load.is_idle(admission_load.EmbedderCounters(active=False, queue_depth=1)) is False


def test_resolve_idle_precheck_passes_when_two_reads_agree_idle() -> None:
    reads = iter([IDLE_STATUS, IDLE_STATUS])
    slept: list[float] = []
    reason, counters = admission_load.resolve_idle_precheck(
        lambda: next(reads), admission_load.DEFAULT_EMBEDDER, gap_s=3.0, sleep=slept.append,
    )
    assert reason is None
    assert admission_load.is_idle(counters)
    assert slept == [3.0]


def test_resolve_idle_precheck_fails_status_unreadable_on_first_read() -> None:
    reason, counters = admission_load.resolve_idle_precheck(lambda: None, admission_load.DEFAULT_EMBEDDER, sleep=lambda s: None)
    assert reason is not None and "status unreadable" in reason
    assert counters is None


def test_resolve_idle_precheck_fails_status_unreadable_on_second_read() -> None:
    reads = iter([IDLE_STATUS, None])
    reason, counters = admission_load.resolve_idle_precheck(lambda: next(reads), admission_load.DEFAULT_EMBEDDER, sleep=lambda s: None)
    assert reason is not None and "status unreadable" in reason
    assert counters is None


def test_resolve_idle_precheck_fails_engine_not_idle_when_active() -> None:
    reads = iter([_status(active=True), IDLE_STATUS])
    reason, counters = admission_load.resolve_idle_precheck(lambda: next(reads), admission_load.DEFAULT_EMBEDDER, sleep=lambda s: None)
    assert reason is not None and "engine not idle" in reason
    assert counters is not None  # the second read is still returned as evidence


def test_resolve_idle_precheck_fails_engine_not_idle_when_queue_nonzero() -> None:
    reads = iter([IDLE_STATUS, _status(queue_depth=2)])
    reason, _ = admission_load.resolve_idle_precheck(lambda: next(reads), admission_load.DEFAULT_EMBEDDER, sleep=lambda s: None)
    assert reason is not None and "engine not idle" in reason


def test_resolve_idle_precheck_never_sleeps_on_a_first_read_failure() -> None:
    calls: list[float] = []
    admission_load.resolve_idle_precheck(lambda: None, admission_load.DEFAULT_EMBEDDER, sleep=calls.append)
    assert calls == [], "no need to wait out the gap once the first read already failed"


# ── Per-step evaluation + ramp stop logic ───────────────────────────────


def _rr(status_code: int, deadline_outcome: str | None = None, error: str | None = None) -> "admission_load.RawResponse":
    return admission_load.RawResponse(status_code=status_code, retry_after=None, deadline_outcome=deadline_outcome, elapsed_s=0.01, error=error)


def test_evaluate_step_passes_on_admission_move_plus_observed_refused() -> None:
    before = admission_load.EmbedderCounters(admission_refusals_total=0)
    after = admission_load.EmbedderCounters(admission_refusals_total=1)
    verdict = admission_load.evaluate_step(before, after, [_rr(200), _rr(503, "refused")])
    assert verdict.admission_moved is True
    assert verdict.observed_refused is True
    assert verdict.passes is True
    assert verdict.unattributable is False


def test_evaluate_step_is_unattributable_when_admission_moves_with_no_refused_observed() -> None:
    before = admission_load.EmbedderCounters(admission_refusals_total=0)
    after = admission_load.EmbedderCounters(admission_refusals_total=1)
    verdict = admission_load.evaluate_step(before, after, [_rr(200), _rr(200)])
    assert verdict.admission_moved is True
    assert verdict.observed_refused is False
    assert verdict.passes is False
    assert verdict.unattributable is True


def test_evaluate_step_deadline_abort_alone_never_passes() -> None:
    before = admission_load.EmbedderCounters(admission_refusals_total=0, deadline_aborts_total=0)
    after = admission_load.EmbedderCounters(admission_refusals_total=0, deadline_aborts_total=1)
    verdict = admission_load.evaluate_step(before, after, [_rr(503, "aborted"), _rr(503, "aborted")])
    assert verdict.admission_moved is False
    assert verdict.deadline_moved is True
    assert verdict.passes is False
    assert verdict.unattributable is False


def test_evaluate_step_neither_moved() -> None:
    before = admission_load.EmbedderCounters(admission_refusals_total=2, deadline_aborts_total=1)
    after = admission_load.EmbedderCounters(admission_refusals_total=2, deadline_aborts_total=1)
    verdict = admission_load.evaluate_step(before, after, [_rr(200)])
    assert verdict.passes is False
    assert verdict.unattributable is False
    assert verdict.deadline_moved is False


def test_is_step_valid_below_threshold() -> None:
    responses = [_rr(200)] * 9 + [_rr(503, "refused")] * 5 + [_rr(0, error="boom")]  # 1/15 transport errors
    assert admission_load.is_step_valid(responses) is True


def test_is_step_valid_above_threshold_is_invalid() -> None:
    responses = [_rr(0, error="boom")] * 3 + [_rr(200)]  # 3/4 transport errors
    assert admission_load.is_step_valid(responses) is False


def test_is_step_valid_empty_is_invalid() -> None:
    assert admission_load.is_step_valid([]) is False


def test_decide_ramp_outcome_stops_at_first_pass() -> None:
    passing = admission_load.StepVerdict(True, False, True, True, False)
    neither = admission_load.StepVerdict(False, False, False, False, False)
    outcome = admission_load.decide_ramp_outcome([32, 64, 128], [neither, passing, passing])
    assert outcome.stopped_at_step == 64
    assert outcome.stopped_at_index == 1
    assert outcome.unattributable_at_step is None


def test_decide_ramp_outcome_stops_at_first_unattributable_even_if_a_later_step_would_pass() -> None:
    unattributable = admission_load.StepVerdict(True, False, False, False, True)
    passing = admission_load.StepVerdict(True, False, True, True, False)
    outcome = admission_load.decide_ramp_outcome([32, 64], [unattributable, passing])
    assert outcome.unattributable_at_step == 32
    assert outcome.stopped_at_step is None


def test_decide_ramp_outcome_collects_deadline_only_steps() -> None:
    deadline_only = admission_load.StepVerdict(False, True, False, False, False)
    outcome = admission_load.decide_ramp_outcome([32, 64], [deadline_only, deadline_only])
    assert outcome.deadline_only_steps == (32, 64)
    assert outcome.stopped_at_step is None
    assert outcome.unattributable_at_step is None


def test_decide_ramp_outcome_refuses_a_length_mismatch() -> None:
    with pytest.raises(ValueError):
        admission_load.decide_ramp_outcome([32, 64], [admission_load.INVALID_STEP_VERDICT])


def test_require_pass_is_silent_on_a_passing_outcome() -> None:
    passing = admission_load.StepVerdict(True, False, True, True, False)
    outcome = admission_load.decide_ramp_outcome([32], [passing])
    admission_load.require_pass(outcome)  # must not raise


def test_require_pass_raises_unattributable() -> None:
    unattributable = admission_load.StepVerdict(True, False, False, False, True)
    outcome = admission_load.decide_ramp_outcome([32], [unattributable])
    with pytest.raises(admission_load.AdmissionLoadUnattributableError, match="not attributable"):
        admission_load.require_pass(outcome)


def test_require_pass_raises_vacuous_and_names_deadline_only_steps() -> None:
    deadline_only = admission_load.StepVerdict(False, True, False, False, False)
    neither = admission_load.StepVerdict(False, False, False, False, False)
    outcome = admission_load.decide_ramp_outcome([32, 64], [deadline_only, neither])
    with pytest.raises(admission_load.AdmissionLoadVacuousError) as excinfo:
        admission_load.require_pass(outcome)
    message = str(excinfo.value)
    assert "32" in message and "64" in message
    assert "deadline_aborts_total" in message and "nexus-u2mlh.3" in message


# ── Nonce, subject, document generation, ramp-step parsing ─────────────


def test_parse_ramp_steps_accepts_a_strictly_increasing_list() -> None:
    assert admission_load.parse_ramp_steps("32,64,128,256") == [32, 64, 128, 256]


def test_parse_ramp_steps_tolerates_whitespace() -> None:
    assert admission_load.parse_ramp_steps(" 32, 64 ,128 ") == [32, 64, 128]


@pytest.mark.parametrize("raw", ["", "  ", "0,-1", "64,32", "32,32,64"])
def test_parse_ramp_steps_refuses_non_increasing_or_empty(raw: str) -> None:
    with pytest.raises(ValueError):
        admission_load.parse_ramp_steps(raw)


def test_make_run_nonce_default_is_lowercase_alnum() -> None:
    nonce = admission_load.make_run_nonce()
    assert nonce == nonce.lower()
    assert nonce.isalnum()
    assert len(nonce) > 0


def test_make_run_nonce_two_default_calls_differ() -> None:
    assert admission_load.make_run_nonce() != admission_load.make_run_nonce()


def test_make_run_nonce_accepts_an_injected_source() -> None:
    assert admission_load.make_run_nonce(lambda: "abc123") == "abc123"


def test_make_run_nonce_refuses_a_non_alnum_source() -> None:
    with pytest.raises(ValueError):
        admission_load.make_run_nonce(lambda: "not-alnum!")


def test_load_collection_subject_is_conformant_with_docs_collections_rule_3() -> None:
    subject = admission_load.load_collection_subject("abc123")
    assert subject == "u2mlh-load-abc123"
    assert subject == subject.lower()
    assert admission_load._SUBJECT_RE.match(subject)


def test_load_collection_subject_rejects_a_shape_the_grammar_refuses() -> None:
    with pytest.raises(ValueError):
        admission_load.load_collection_subject("has-a-hyphen-in-it")


def test_generate_document_is_unique_per_nonce_and_tag() -> None:
    a = admission_load.generate_document("nonce1", "0-0")
    b = admission_load.generate_document("nonce1", "0-1")
    c = admission_load.generate_document("nonce2", "0-0")
    assert len({a, b, c}) == 3


def test_generate_document_is_deterministic_for_the_same_inputs() -> None:
    assert admission_load.generate_document("n", "t") == admission_load.generate_document("n", "t")


def test_generate_document_respects_the_target_byte_floor() -> None:
    text = admission_load.generate_document("n", "t", target_bytes=admission_load.MIN_DOCUMENT_BYTES)
    assert len(text.encode("utf-8")) >= admission_load.MIN_DOCUMENT_BYTES


@pytest.mark.parametrize(
    "target_bytes",
    [admission_load.MIN_DOCUMENT_BYTES - 1, admission_load.MAX_DOCUMENT_BYTES + 1],
)
def test_generate_document_refuses_out_of_range_sizes(target_bytes: int) -> None:
    with pytest.raises(ValueError):
        admission_load.generate_document("n", "t", target_bytes=target_bytes)


def test_load_document_id_is_the_sha256_hex_of_the_generated_document() -> None:
    text = admission_load.generate_document("n", "t")
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert admission_load.load_document_id("n", "t") == expected
    assert len(expected) == 64


# ── Raw-503 evidence rollup ──────────────────────────────────────────────


def test_summarize_responses_counts_by_status_and_outcome() -> None:
    responses = [_rr(200), _rr(503, "refused"), _rr(503, "aborted"), _rr(0, error="boom")]
    summary = admission_load.summarize_responses(responses)
    assert summary["count"] == 4
    assert summary["by_status"] == {"200": 1, "503": 2, "0": 1}
    assert summary["by_deadline_outcome"] == {"refused": 1, "aborted": 1}
    assert summary["transport_errors"] == 1


def test_summarize_responses_of_an_empty_step_is_not_vacuous_silently() -> None:
    summary = admission_load.summarize_responses([])
    assert summary["count"] == 0
    assert summary["by_status"] == {}


# ── Network functions against httpx.MockTransport (no real sockets) ────


def test_build_client_sizes_the_connection_pool_to_the_ramp_ceiling() -> None:
    client = admission_load.build_client("https://fake.example", {"Authorization": "Bearer x"}, (32, 64, 128, 256))
    try:
        # httpx.Client has no public `.limits` accessor; the pool lives on
        # its default transport. Introspecting httpcore internals here is
        # the only way to prove the sizing actually reached the wire.
        pool = client._transport._pool  # noqa: SLF001 — deliberate: proving the real config landed, not re-deriving it
        assert pool._max_connections == 256 + admission_load.CONNECTION_POOL_HEADROOM
        assert pool._max_keepalive_connections == 256 + admission_load.CONNECTION_POOL_HEADROOM
        assert client.timeout.read == admission_load.DEFAULT_REQUEST_TIMEOUT_S
        assert client.timeout.pool == admission_load.POOL_ACQUIRE_TIMEOUT_S
    finally:
        client.close()


def test_fetch_status_returns_the_parsed_body_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/status"
        return httpx.Response(200, json=IDLE_STATUS)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fake.example") as client:
        assert admission_load.fetch_status(client) == IDLE_STATUS


@pytest.mark.parametrize("handler_status", [500, 503])
def test_fetch_status_is_fail_closed_to_none_on_an_error_status(handler_status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(handler_status)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fake.example") as client:
        assert admission_load.fetch_status(client) is None


def test_fetch_status_is_fail_closed_to_none_on_a_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fake.example") as client:
        assert admission_load.fetch_status(client) is None


def test_post_upsert_sends_the_documented_body_and_deadline_header() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["deadline_header"] = request.headers.get("X-Nexus-Request-Deadline-Ms")
        captured["body"] = request.content
        return httpx.Response(200, json={"upserted": 1})

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fake.example") as client:
        resp = admission_load.post_upsert(client, "knowledge__x__voyage-context-3__v1", "nonce1", "0-0")

    assert resp.status_code == 200
    assert captured["path"] == "/v1/vectors/upsert-chunks"
    assert captured["deadline_header"] == str(admission_load.DEFAULT_REQUEST_DEADLINE_MS)
    body = _json.loads(captured["body"])
    assert body["collection"] == "knowledge__x__voyage-context-3__v1"
    assert body["ids"] == [admission_load.load_document_id("nonce1", "0-0")]
    assert body["documents"] == [admission_load.generate_document("nonce1", "0-0")]


def test_post_upsert_captures_refused_503_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "5", "X-Nexus-Deadline-Outcome": "refused"})

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fake.example") as client:
        resp = admission_load.post_upsert(client, "coll", "n", "t")
    assert resp.status_code == 503
    assert resp.retry_after == 5.0
    assert resp.deadline_outcome == "refused"


def test_post_upsert_captures_a_transport_error_as_status_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fake.example") as client:
        resp = admission_load.post_upsert(client, "coll", "n", "t")
    assert resp.status_code == 0
    assert resp.error is not None


def test_fire_step_collects_every_concurrent_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "1", "X-Nexus-Deadline-Outcome": "refused"})

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fake.example") as client:
        responses, hit_deadline = admission_load.fire_step(client, "coll", "n", 0, 8)
    assert len(responses) == 8
    assert hit_deadline is False
    assert all(r.status_code == 503 and r.deadline_outcome == "refused" for r in responses)


def test_fire_step_hits_the_wall_clock_deadline_and_marks_incomplete_responses() -> None:
    def slow_handler(request: httpx.Request) -> httpx.Response:
        _time.sleep(0.3)
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(slow_handler), base_url="https://fake.example") as client:
        responses, hit_deadline = admission_load.fire_step(client, "coll", "n", 0, 4, deadline_s=0.02)
    assert hit_deadline is True
    assert len(responses) == 4
    assert all(r.status_code == 0 for r in responses)
    assert admission_load.is_step_valid(responses) is False


# ── Direct boundary tests: resolve_bearer, find_orphan_collections, ────
# ── guard_write — the REAL implementations, faked only at their own ────
# ── nexus.* boundary (fold 2, T2 [27139] Important 1) ───────────────────


class _FakeDataTokenManager:
    def __init__(self, token: str | None) -> None:
        self._token = token

    def bearer_for(self, base_url: str, tenant: str) -> str | None:
        return self._token


def test_resolve_bearer_uses_the_self_minted_token_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_token_module, "get_data_token_manager", lambda: _FakeDataTokenManager("minted-xyz"))
    assert admission_load.resolve_bearer("https://fake.example", "default", "static-token") == "minted-xyz"


def test_resolve_bearer_falls_back_to_the_static_token_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_token_module, "get_data_token_manager", lambda: _FakeDataTokenManager(None))
    assert admission_load.resolve_bearer("https://fake.example", "default", "static-token") == "static-token"


class _FakeT3ForOrphanScan:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list_collections(self) -> list[dict[str, str]]:
        return [{"name": n} for n in self._names]

    def delete_collection(self, name: str) -> None:
        raise AssertionError(f"find_orphan_collections must never delete anything (attempted delete of {name!r})")


def test_find_orphan_collections_lists_only_the_gate_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    names = [
        "knowledge__u2mlh-load-abc123__voyage-context-3__v1",
        "knowledge__u2mlh-load-def456__voyage-context-3__v1",
        "knowledge__unrelated-subject__voyage-context-3__v1",
        "docs__some-repo__voyage-context-3__v1",
    ]
    monkeypatch.setattr(nexus_db_module, "make_t3", lambda: _FakeT3ForOrphanScan(names))
    result = admission_load.find_orphan_collections()
    assert result == sorted(names[:2])


def test_find_orphan_collections_excludes_the_current_runs_own_name(monkeypatch: pytest.MonkeyPatch) -> None:
    names = ["knowledge__u2mlh-load-abc123__voyage-context-3__v1", "knowledge__u2mlh-load-def456__voyage-context-3__v1"]
    monkeypatch.setattr(nexus_db_module, "make_t3", lambda: _FakeT3ForOrphanScan(names))
    result = admission_load.find_orphan_collections(exclude=names[0])
    assert result == [names[1]]


def test_find_orphan_collections_performs_no_delete_when_orphans_are_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake's delete_collection raises unconditionally; a clean pass
    here IS the proof that no delete/purge path is reachable from
    find_orphan_collections -- if it were reachable, this test would fail
    with the fake's own AssertionError instead of passing."""
    names = ["knowledge__u2mlh-load-abc123__voyage-context-3__v1"]
    monkeypatch.setattr(nexus_db_module, "make_t3", lambda: _FakeT3ForOrphanScan(names))
    result = admission_load.find_orphan_collections()
    assert result == names


def test_guard_write_refuses_without_the_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    # tests/conftest.py's blanket pytest exemption sets _test_only_opt_in_reason
    # for the WHOLE suite (see service_endpoint.py's own docstring); clear it
    # here so this test exercises the real env-var-absent refusal path
    # rather than the ambient test bypass every other test in this suite
    # silently rides.
    monkeypatch.setattr(service_endpoint_module, "_test_only_opt_in_reason", None)
    monkeypatch.delenv("NX_ALLOW_PROD_WRITE", raising=False)
    with pytest.raises(service_endpoint_module.ProductionWriteGuardError):
        admission_load.guard_write("https://fake.example")


def test_guard_write_accepts_with_the_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_endpoint_module, "_test_only_opt_in_reason", None)
    monkeypatch.setenv("NX_ALLOW_PROD_WRITE", "test probe: exercising the real opt-in acceptance path")
    admission_load.guard_write("https://fake.example")  # must not raise


# ── run_gate end to end, every network function monkeypatched ──────────


def _patch_lifecycle(monkeypatch: pytest.MonkeyPatch, *, register_calls: list[str] | None = None, delete_calls: list[str] | None = None) -> None:
    monkeypatch.setattr(admission_load, "resolve_cloud_endpoint", lambda: ("https://fake.example", "static-token"))
    monkeypatch.setattr(admission_load, "guard_write", lambda base_url: None)
    monkeypatch.setattr(admission_load, "resolve_bearer", lambda base_url, tenant, token: token)
    monkeypatch.setattr(admission_load, "find_orphan_collections", lambda exclude=None: [])
    monkeypatch.setattr(admission_load, "resolve_collection_name", lambda subject: f"knowledge__{subject}__voyage-context-3__v1")

    def _register(name: str) -> None:
        if register_calls is not None:
            register_calls.append(name)

    def _delete(name: str) -> dict[str, Any]:
        if delete_calls is not None:
            delete_calls.append(name)
        return {"t3_absent": False, "failures": []}

    monkeypatch.setattr(admission_load, "register_load_collection", _register)
    monkeypatch.setattr(admission_load, "delete_load_collection", _delete)


#: Sentinels for _client_factory_for's status_sequence: an entry that is
#: one of these produces a failed /v1/status read (a 5xx, or a raised
#: transport error) instead of a 200 body, so a fold-2 test can simulate a
#: mid-ramp status-read failure at an exact call index.
STATUS_FAIL_500 = object()
STATUS_FAIL_TRANSPORT = object()


def _client_factory_for(status_sequence: list, upsert_responder):
    calls = {"status": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/status":
            idx = min(calls["status"], len(status_sequence) - 1)
            calls["status"] += 1
            entry = status_sequence[idx]
            if entry is STATUS_FAIL_500:
                return httpx.Response(500)
            if entry is STATUS_FAIL_TRANSPORT:
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(200, json=entry)
        if request.url.path == "/v1/vectors/upsert-chunks":
            return upsert_responder(request)
        return httpx.Response(404)

    def factory(base_url: str, headers: dict[str, str]) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), base_url=base_url, headers=headers)

    return factory


def test_run_gate_dry_run_touches_no_network_and_reports_a_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate from this box's real ~/.config/nexus/config.yml: resolve_collection_name
    # (called inside the dry-run branch) is network-free but reads local
    # config for the embed-model intent, and a real box's config could be
    # voyage-shaped with no key reachable from this test process.
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    result = admission_load.run_gate(dry_run=True, nonce_source=lambda: "dryrunnonce")
    assert result["passed"] is True
    assert result["dry_run"] is True
    plan = result["plan"]
    assert plan["nonce"] == "dryrunnonce"
    assert plan["subject"] == "u2mlh-load-dryrunnonce"
    assert plan["ramp_steps"] == list(admission_load.DEFAULT_RAMP_STEPS)
    assert plan["wall_clock_cap_s"] == admission_load.DEFAULT_WALL_CLOCK_CAP_S
    assert plan["collection_name"].startswith("knowledge__u2mlh-load-dryrunnonce__")


def test_run_gate_passes_when_admission_moves_and_refused_is_observed(monkeypatch: pytest.MonkeyPatch) -> None:
    register_calls: list[str] = []
    delete_calls: list[str] = []
    _patch_lifecycle(monkeypatch, register_calls=register_calls, delete_calls=delete_calls)

    before_status = _status(admission=0)
    after_status = _status(admission=1)
    factory = _client_factory_for(
        [IDLE_STATUS, IDLE_STATUS, before_status, after_status],
        lambda request: httpx.Response(503, headers={"Retry-After": "1", "X-Nexus-Deadline-Outcome": "refused"}),
    )

    result = admission_load.run_gate(
        dry_run=False, ramp_steps=(2,), client_factory=factory, idle_sleep=lambda s: None,
        nonce_source=lambda: "passnonce",
    )
    assert result["passed"] is True, result["reason"]
    assert result["stopped_at_step"] == 2
    assert register_calls == ["knowledge__u2mlh-load-passnonce__voyage-context-3__v1"]
    assert delete_calls == ["knowledge__u2mlh-load-passnonce__voyage-context-3__v1"]
    assert result["cleanup"]["ok"] is True


def test_run_gate_fails_unattributable_when_counter_moves_with_no_refused_observed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_lifecycle(monkeypatch)
    before_status = _status(admission=0)
    after_status = _status(admission=1)  # moved, but every response below is a plain 200
    factory = _client_factory_for(
        [IDLE_STATUS, IDLE_STATUS, before_status, after_status],
        lambda request: httpx.Response(200, json={"upserted": 1}),
    )

    result = admission_load.run_gate(dry_run=False, ramp_steps=(2,), client_factory=factory, idle_sleep=lambda s: None)
    assert result["passed"] is False
    assert "not attributable" in result["reason"]


def test_run_gate_fails_vacuous_when_only_deadline_aborts_move(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_lifecycle(monkeypatch)
    step1_before, step1_after = _status(deadline=0), _status(deadline=1)
    step2_before, step2_after = _status(deadline=1), _status(deadline=2)
    factory = _client_factory_for(
        [IDLE_STATUS, IDLE_STATUS, step1_before, step1_after, step2_before, step2_after],
        lambda request: httpx.Response(503, headers={"Retry-After": "1", "X-Nexus-Deadline-Outcome": "aborted"}),
    )

    result = admission_load.run_gate(dry_run=False, ramp_steps=(2, 4), client_factory=factory, idle_sleep=lambda s: None)
    assert result["passed"] is False
    assert "deadline_aborts_total" in result["reason"]
    assert "nexus-u2mlh.3" in result["reason"]


def test_run_gate_fails_and_names_invalid_steps_on_heavy_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_lifecycle(monkeypatch)
    counter = itertools.count()

    def upsert_responder(request: httpx.Request) -> httpx.Response:
        n = next(counter)
        if n < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(503, headers={"Retry-After": "1", "X-Nexus-Deadline-Outcome": "refused"})

    factory = _client_factory_for([IDLE_STATUS, IDLE_STATUS, _status(), _status()], upsert_responder)

    result = admission_load.run_gate(dry_run=False, ramp_steps=(4,), client_factory=factory, idle_sleep=lambda s: None)
    assert result["passed"] is False
    assert "invalid" in result["reason"]
    assert result["steps"][0]["valid"] is False


def test_run_gate_marks_a_step_invalid_when_the_before_status_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """fold 2 (T2 [27139] Important 2): a failed mid-ramp status read must
    never silently become a zeroed counter via snapshot_counters' own
    fail-closed default -- it must invalidate the step outright, never
    contribute a pass or an unattributable verdict, and name the real
    cause ("status read failed"), not misattribute it to "concurrent
    activity on the shared tenant"."""
    _patch_lifecycle(monkeypatch)
    # idle precheck consumes 2 reads (both idle); the step's OWN "before"
    # read is the 3rd call and fails (500); the "after" read (4th call)
    # would show a real admission_refusals_total=1 if it were ever trusted.
    factory = _client_factory_for(
        [IDLE_STATUS, IDLE_STATUS, STATUS_FAIL_500, _status(admission=1)],
        lambda request: httpx.Response(503, headers={"Retry-After": "1", "X-Nexus-Deadline-Outcome": "refused"}),
    )
    result = admission_load.run_gate(dry_run=False, ramp_steps=(2,), client_factory=factory, idle_sleep=lambda s: None)
    assert result["passed"] is False
    assert "status read failed" in result["reason"]
    assert result["steps"][0]["valid"] is False
    assert result["steps"][0]["status_read_failed"] is True
    assert result["steps"][0]["admission_moved"] is False, "an invalid step must never report a moved counter"


def test_run_gate_marks_a_step_invalid_when_the_after_status_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_lifecycle(monkeypatch)
    # The step's "before" read (3rd call) succeeds with a real baseline;
    # the "after" read (4th call) raises a transport error instead of a 5xx,
    # covering the other named failure mode.
    factory = _client_factory_for(
        [IDLE_STATUS, IDLE_STATUS, _status(admission=0), STATUS_FAIL_TRANSPORT],
        lambda request: httpx.Response(503, headers={"Retry-After": "1", "X-Nexus-Deadline-Outcome": "refused"}),
    )
    result = admission_load.run_gate(dry_run=False, ramp_steps=(2,), client_factory=factory, idle_sleep=lambda s: None)
    assert result["passed"] is False
    assert "status read failed" in result["reason"]
    assert result["steps"][0]["valid"] is False
    assert result["steps"][0]["status_read_failed"] is True


def test_run_gate_stops_early_and_reports_the_wall_clock_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_lifecycle(monkeypatch)

    def slow_upsert(request: httpx.Request) -> httpx.Response:
        _time.sleep(0.3)
        return httpx.Response(200)

    factory = _client_factory_for([IDLE_STATUS, IDLE_STATUS, _status(), _status()], slow_upsert)

    result = admission_load.run_gate(
        dry_run=False, ramp_steps=(4, 8), wall_clock_cap_s=0.05, client_factory=factory, idle_sleep=lambda s: None,
    )
    assert result["passed"] is False
    assert "wall-clock cap" in result["reason"]
    assert len(result["steps"]) == 1, "must not have started the second step past the cap"


def test_run_gate_refuses_to_write_when_the_engine_is_not_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    register_calls: list[str] = []
    _patch_lifecycle(monkeypatch, register_calls=register_calls)
    factory = _client_factory_for([_status(active=True), IDLE_STATUS], lambda request: httpx.Response(200))

    result = admission_load.run_gate(dry_run=False, ramp_steps=(2,), client_factory=factory, idle_sleep=lambda s: None)
    assert result["passed"] is False
    assert "engine not idle" in result["reason"]
    assert register_calls == [], "must never write once the idle pre-check fails"


def test_run_gate_refuses_to_write_when_status_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    register_calls: list[str] = []
    _patch_lifecycle(monkeypatch, register_calls=register_calls)

    def factory(base_url: str, headers: dict[str, str]) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)), base_url=base_url, headers=headers)

    result = admission_load.run_gate(dry_run=False, ramp_steps=(2,), client_factory=factory, idle_sleep=lambda s: None)
    assert result["passed"] is False
    assert "status unreadable" in result["reason"]
    assert register_calls == []


def test_run_gate_runs_cleanup_on_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    delete_calls: list[str] = []
    _patch_lifecycle(monkeypatch, delete_calls=delete_calls)
    factory = _client_factory_for([IDLE_STATUS, IDLE_STATUS], lambda request: httpx.Response(200))

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyboardInterrupt()

    monkeypatch.setattr(admission_load, "fire_step", boom)

    result = admission_load.run_gate(dry_run=False, ramp_steps=(4,), client_factory=factory, idle_sleep=lambda s: None)
    assert result["passed"] is False
    assert "KeyboardInterrupt" in result["reason"]
    assert delete_calls == ["knowledge__u2mlh-load-" + result["plan"]["nonce"] + "__voyage-context-3__v1"]
    assert result["cleanup"]["attempted"] is True


def test_run_gate_skips_cleanup_cleanly_when_no_collection_was_ever_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    delete_calls: list[str] = []
    _patch_lifecycle(monkeypatch, delete_calls=delete_calls)

    def boom_endpoint() -> tuple[str, str]:
        raise RuntimeError("endpoint unresolvable")

    monkeypatch.setattr(admission_load, "resolve_cloud_endpoint", boom_endpoint)

    result = admission_load.run_gate(dry_run=False, ramp_steps=(2,), idle_sleep=lambda s: None)
    assert result["passed"] is False
    assert delete_calls == []
    assert result["cleanup"]["attempted"] is False
    assert result["cleanup"]["ok"] is True


# ── CLI ──────────────────────────────────────────────────────────────────


def test_main_refuses_a_non_positive_wall_clock_cap() -> None:
    rc = admission_load.main(["run", "--dry-run", "--wall-clock-cap-s", "0"])
    assert rc == 1


# ── Wrapper mode detection (real subprocess, still no network) ────────────


def test_wrapper_refuses_on_a_local_mode_box(tmp_path: Path) -> None:
    """The FIRST thing the wrapper does is mode detection; a box with no
    configured service_url must refuse (exit 2) before anything resembling
    the driver ever runs -- so this subprocess never reaches the network
    regardless of what credentials happen to be live in this box's own
    environment."""
    env = dict(os.environ)
    for var in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN", "NX_SERVICE_HOST", "NX_SERVICE_PORT"):
        env.pop(var, None)
    env["NEXUS_CONFIG_DIR"] = str(tmp_path)  # empty scratch dir: no config.yml, no lease to discover

    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "not applicable" in result.stdout


def test_wrapper_refuses_on_a_local_mode_box_even_with_ramp_flags(tmp_path: Path) -> None:
    """Flag forwarding must not move mode detection later in the script."""
    env = dict(os.environ)
    for var in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN", "NX_SERVICE_HOST", "NX_SERVICE_PORT"):
        env.pop(var, None)
    env["NEXUS_CONFIG_DIR"] = str(tmp_path)

    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH), "--ramp-steps", "8,16", "--wall-clock-cap-s", "30"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_wrapper_forwards_ramp_steps_and_wall_clock_cap_to_the_driver() -> None:
    """Static check that the flags are actually threaded through, without
    needing a cloud-mode box to exercise the live path: the wrapper's own
    source must reference both flag names on the driver invocation line."""
    text = _SCRIPT_PATH.read_text()
    assert "--ramp-steps" in text
    assert "--wall-clock-cap-s" in text
