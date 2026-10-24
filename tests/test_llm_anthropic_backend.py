# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``anthropic`` backend against a scripted stdlib HTTP server (nexus-2k66t.3 P2).

Real sockets, real httpx, real SSE framing; no mock of the transport. Every
scenario the design lists: success, schema-fail then repair, max_tokens, refusal,
429 then success, 401, a stall before the first event, a stall mid-stream with
partial output, and the key never appearing in a log line or an exception.
"""
from __future__ import annotations

import asyncio
import json
import os
import random

import pytest
import structlog

from nexus.operators.llm_api import backoff_delay, parse_retry_after
from nexus.operators.llm_anthropic import AnthropicBackend
from nexus.operators.llm_backend import DispatchRequest
from nexus.operators.llm_models import price_for
from nexus.operators.llm_types import (
    DispatchUsage,
    OperatorAPIError,
    OperatorBudgetExceededError,
    OperatorError,
    OperatorOutputError,
    OperatorTimeoutError,
    ambient_usage_sink,
)
from tests._llm_fake_server import FakeLLMServer, Step, anthropic_error, anthropic_stream

SECRET = "sk-ant-api03-ZZTOPSECRETKEY0123456789abcdef"
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}, "n": {"type": "integer"}},
          "required": ["answer"]}
GOOD = {"answer": "42", "n": 1}


@pytest.fixture(autouse=True)
def _llm_env(monkeypatch) -> None:
    # The unpriced-model warning is once per process per model; a test that asserts it
    # must not depend on which test ran first in its worker.
    from nexus.operators import llm_api

    monkeypatch.setattr(llm_api, "_WARNED_UNPRICED", set())
    monkeypatch.setattr(llm_api, "_FORCED_REJECTED", set(), raising=False)  # the per-process memo of a rejected forced tool_choice
    for var in ("NX_LLM_MAX_TOKENS", "NX_LLM_MAX_CONCURRENCY", "NX_LLM_BACKEND", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


class Harness:
    def __init__(self, server: FakeLLMServer, *, api_key: str = SECRET, base_url: str | None = None, **kw) -> None:
        self.server = server
        self.sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.backend = AnthropicBackend(
            api_key=api_key, base_url=base_url or server.url, sleep=sleep, rng=random.Random(7), trust_env=False, **kw,
        )

    def run(self, **req_kw):
        req_kw.setdefault("json_schema", SCHEMA)
        req_kw.setdefault("prompt", "what is the answer?")
        req_kw.setdefault("timeout", 10.0)
        return asyncio.run(self.backend.run(DispatchRequest(**req_kw)))


@pytest.fixture
def server():
    with FakeLLMServer() as srv:
        yield srv


def _ok(**kw) -> Step:
    return Step(events=anthropic_stream(tool_input=kw.pop("tool_input", GOOD), **kw))


# ── success and the request that was sent ───────────────────────────────────


class TestSuccess:
    def test_forced_tool_request_shape_and_result(self, server) -> None:
        server.add(_ok(input_tokens=100, output_tokens=50))
        h = Harness(server)
        result = h.run(model="haiku")
        assert result.structured == GOOD
        (rec,) = server.requests
        assert rec.path == "/v1/messages"
        assert rec.headers["x-api-key"] == SECRET
        assert rec.headers["anthropic-version"] == "2023-06-01"
        body = rec.body
        assert body["model"] == "claude-haiku-5-5"  # alias -> id
        assert body["stream"] is True and body["max_tokens"] == 16000
        assert body["messages"] == [{"role": "user", "content": "what is the answer?"}]
        (tool,) = body["tools"]
        assert tool["name"] == "StructuredOutput" and tool["input_schema"] == SCHEMA
        assert body["tool_choice"] == {"type": "tool", "name": "StructuredOutput"}
        assert "system" not in body  # forced: no instruction needed

    def test_usage_and_cost_from_the_price_table(self, server) -> None:
        server.add(_ok(input_tokens=1000, output_tokens=500, cache_creation=200, cache_read=300))
        sink: list[DispatchUsage] = []
        result = Harness(server).run(model="haiku", usage_sink=sink)
        p = price_for("claude-haiku-5-5")
        expected = (1000 * p.input + 500 * p.output + 200 * p.cache_write + 300 * p.cache_read) / 1_000_000
        (usage,) = sink
        assert usage is result.usage
        assert usage.model == "claude-haiku-5-5"
        assert (usage.input_tokens, usage.output_tokens) == (1000, 500)
        assert (usage.cache_creation_input_tokens, usage.cache_read_input_tokens) == (200, 300)
        assert usage.cost_usd == pytest.approx(expected)
        assert usage.cost_usd > 0
        assert usage.num_turns == 1
        assert usage.model_usage["claude-haiku-5-5"].cost_usd == pytest.approx(expected)

    def test_both_sinks_receive_one_instance(self, server) -> None:
        server.add(_ok())
        explicit: list[DispatchUsage] = []
        ambient: list[DispatchUsage] = []
        with ambient_usage_sink(ambient):
            Harness(server).run(model="haiku", usage_sink=explicit)
        assert len(explicit) == len(ambient) == 1 and explicit[0] is ambient[0]

    def test_no_sink_is_a_noop(self, server) -> None:
        server.add(_ok())
        assert Harness(server).run(model="haiku").usage is not None  # still returned on the result

    def test_default_model_is_the_strong_default(self, server) -> None:
        server.add(_ok(model="claude-opus-5-5"))
        Harness(server).run()
        assert server.requests[0].body["model"] == "claude-opus-5-5"

    def test_full_model_ids_pass_through(self, server) -> None:
        server.add(_ok(model="claude-haiku-4-5-20251001"))
        Harness(server).run(model="claude-haiku-4-5-20251001")
        assert server.requests[0].body["model"] == "claude-haiku-4-5-20251001"

    def test_dated_snapshot_id_is_priced_as_its_base(self, server) -> None:
        server.add(_ok(model="claude-haiku-4-5-20251001", input_tokens=1000, output_tokens=1000))
        usage = Harness(server).run(model="claude-haiku-4-5-20251001").usage
        assert usage.cost_usd == pytest.approx((1000 * 1.0 + 1000 * 5.0) / 1_000_000)

    def test_max_tokens_from_the_environment(self, server, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_MAX_TOKENS", "321")
        server.add(_ok())
        Harness(server).run(model="haiku")
        assert server.requests[0].body["max_tokens"] == 321

    @pytest.mark.parametrize("bad", ["0", "-5", "lots"])
    def test_bad_max_tokens_env_fails_loud(self, server, monkeypatch, bad) -> None:
        monkeypatch.setenv("NX_LLM_MAX_TOKENS", bad)
        with pytest.raises(ValueError, match="NX_LLM_MAX_TOKENS"):
            Harness(server).run(model="haiku")
        assert server.requests == []

    def test_text_mode_returns_the_text(self, server) -> None:
        server.add(Step(events=anthropic_stream(text='{"a": 1}')))
        result = Harness(server).run(json_schema=None, model="haiku")
        assert result.text == '{"a": 1}' and result.structured is None
        body = server.requests[0].body
        assert "tools" not in body and "tool_choice" not in body

    def test_non_object_schema_is_wrapped_and_unwrapped(self, server) -> None:
        array_schema = {"type": "array", "items": {"type": "string"}}
        server.add(_ok(tool_input={"result": ["a", "b"]}))
        result = Harness(server).run(json_schema=array_schema, model="haiku")
        assert result.structured == ["a", "b"]
        sent = server.requests[0].body["tools"][0]["input_schema"]
        assert sent["type"] == "object" and sent["properties"]["result"] == array_schema

    def test_a_tool_input_streamed_in_many_deltas(self, server) -> None:
        text = json.dumps(GOOD)
        server.add(_ok(tool_input=None, tool_json_chunks=list(text)))  # one char per delta
        assert Harness(server).run(model="haiku").structured == GOOD


# ── forced tool choice: the models that reject it ───────────────────────────


class TestForcedToolChoice:
    @pytest.mark.parametrize("alias,model_id", [("sonnet", "claude-sonnet-5-5"), ("opus", "claude-opus-5-5")])
    def test_models_that_reject_forcing_get_auto_plus_an_instruction(self, server, alias, model_id) -> None:
        server.add(_ok(model=model_id))
        Harness(server).run(model=alias)
        body = server.requests[0].body
        assert body["model"] == model_id
        assert body["tool_choice"] == {"type": "auto"}
        assert "StructuredOutput" in body["system"]

    def test_a_400_blaming_tool_choice_is_retried_once_with_auto(self, server) -> None:
        server.add(
            anthropic_error(400, "invalid_request_error", 'tool_choice: type "tool" and "any" are not supported for this model.'),
            _ok(model="claude-future-9"),
        )
        result = Harness(server).run(model="claude-future-9")
        assert result.structured == GOOD
        first, second = (r.body for r in server.requests)
        assert first["tool_choice"]["type"] == "tool" and second["tool_choice"] == {"type": "auto"}

    def test_the_retry_with_auto_is_not_counted_as_a_transport_retry(self, server) -> None:
        server.add(
            anthropic_error(400, "invalid_request_error", "tool_choice: not supported for this model"),
            anthropic_error(429, "rate_limit_error", "slow down"),
            anthropic_error(429, "rate_limit_error", "slow down"),
            _ok(model="claude-future-9"),
        )
        assert Harness(server).run(model="claude-future-9").structured == GOOD
        assert len(server.requests) == 4

    def test_an_unrelated_400_is_not_retried(self, server) -> None:
        server.add(anthropic_error(400, "invalid_request_error", "max_tokens: too large"))
        with pytest.raises(OperatorAPIError) as ei:
            Harness(server).run(model="claude-future-9")
        assert ei.value.status == 400 and len(server.requests) == 1


# ── structured output: validation and the repair turn ───────────────────────


class TestRepair:
    def test_schema_failure_then_repair_succeeds(self, server) -> None:
        server.add(_ok(tool_input={"n": 3}, tool_id="toolu_bad"), _ok(tool_input=GOOD))
        sink: list[DispatchUsage] = []
        result = Harness(server).run(model="haiku", usage_sink=sink)
        assert result.structured == GOOD
        first, second = (r.body for r in server.requests)
        assert len(second["messages"]) == 3
        assert second["messages"][1] == {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_bad", "name": "StructuredOutput", "input": {"n": 3}}]}
        (tool_result,) = second["messages"][2]["content"]
        assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_bad"
        assert tool_result["is_error"] is True
        assert "answer" in tool_result["content"]  # the validator's own message
        assert second["tool_choice"] == first["tool_choice"]  # forced again
        (usage,) = sink  # one record for the whole call, both turns summed
        assert usage.num_turns == 2 and usage.input_tokens == 200 and usage.output_tokens == 100

    def test_two_failures_raise_output_error_after_recording_usage(self, server) -> None:
        server.add(_ok(tool_input={"n": 3}), _ok(tool_input={"answer": 5}))
        sink: list[DispatchUsage] = []
        with pytest.raises(OperatorOutputError, match="after one repair turn"):
            Harness(server).run(model="haiku", usage_sink=sink)
        assert len(sink) == 1 and sink[0].num_turns == 2  # appended BEFORE the raise
        assert len(server.requests) == 2  # exactly one repair turn, never a third

    def test_a_tool_input_that_is_not_json_is_repairable(self, server) -> None:
        server.add(_ok(tool_input=None, tool_json_chunks=['{"answer": "4']), _ok(tool_input=GOOD))
        assert Harness(server).run(model="haiku").structured == GOOD
        (tool_result,) = server.requests[1].body["messages"][2]["content"]
        assert "not valid JSON" in tool_result["content"]

    def test_no_tool_call_is_repaired_with_an_instruction(self, server) -> None:
        server.add(Step(events=anthropic_stream(text="The answer is 42.")), _ok(tool_input=GOOD))
        assert Harness(server).run(model="haiku").structured == GOOD
        msgs = server.requests[1].body["messages"]
        assert msgs[1] == {"role": "assistant", "content": [{"type": "text", "text": "The answer is 42."}]}
        assert msgs[2]["role"] == "user" and "StructuredOutput" in msgs[2]["content"]

    def test_no_tool_call_twice_raises_output_error(self, server) -> None:
        server.add(Step(events=anthropic_stream(text="x")), Step(events=anthropic_stream(text="y")))
        sink: list[DispatchUsage] = []
        with pytest.raises(OperatorOutputError):
            Harness(server).run(model="haiku", usage_sink=sink)
        assert len(sink) == 1

    def test_thinking_blocks_are_echoed_unchanged_in_the_repair_turn(self, server) -> None:
        server.add(
            Step(events=anthropic_stream(tool_input={"n": 1}, thinking=("let me think", "SIG123"), model="claude-sonnet-5-5")),
            _ok(tool_input=GOOD, model="claude-sonnet-5-5"),
        )
        Harness(server).run(model="sonnet")
        echoed = server.requests[1].body["messages"][1]["content"]
        assert echoed[0] == {"type": "thinking", "thinking": "let me think", "signature": "SIG123"}
        assert echoed[1]["type"] == "tool_use"

    def test_a_schema_the_validator_rejects_is_left_to_the_provider(self, server) -> None:
        server.add(_ok(tool_input={"anything": 1}))
        bad_schema = {"type": "object", "properties": {"x": {"type": "not-a-type"}}}
        assert Harness(server).run(json_schema=bad_schema, model="haiku").structured == {"anything": 1}


# ── stop reasons ────────────────────────────────────────────────────────────


class TestStopReasons:
    def test_max_tokens_raises_output_error_naming_the_cap(self, server, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_MAX_TOKENS", "77")
        server.add(Step(events=anthropic_stream(tool_json_chunks=['{"answer": "tr'], stop_reason="max_tokens")))
        sink: list[DispatchUsage] = []
        with pytest.raises(OperatorOutputError, match=r"max_tokens=77"):
            Harness(server).run(model="haiku", usage_sink=sink)
        assert len(sink) == 1
        assert len(server.requests) == 1  # a cut-off answer is not repaired

    def test_refusal_raises_operator_error_with_the_category(self, server) -> None:
        server.add(Step(events=anthropic_stream(
            stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"})))
        sink: list[DispatchUsage] = []
        with pytest.raises(OperatorError, match="refused") as ei:
            Harness(server).run(model="haiku", usage_sink=sink)
        assert not isinstance(ei.value, OperatorOutputError)
        assert "cyber" in str(ei.value) and len(sink) == 1

    def test_context_window_exceeded(self, server) -> None:
        server.add(Step(events=anthropic_stream(stop_reason="model_context_window_exceeded")))
        with pytest.raises(OperatorOutputError, match="context"):
            Harness(server).run(model="haiku")


# ── transport: retries, errors ──────────────────────────────────────────────


class TestTransport:
    def test_429_with_retry_after_then_success(self, server) -> None:
        server.add(anthropic_error(429, "rate_limit_error", "slow", headers={"retry-after": "2"}), _ok())
        h = Harness(server)
        assert h.run(model="haiku").structured == GOOD
        assert h.sleeps == [2.0] and len(server.requests) == 2

    def test_backoff_without_retry_after_is_seeded_jitter(self, server) -> None:
        server.add(anthropic_error(529, "overloaded_error", "busy"), anthropic_error(500, "api_error", "oops"), _ok())
        h = Harness(server)
        h.run(model="haiku")
        rng = random.Random(7)
        assert h.sleeps == [backoff_delay(0, rng), backoff_delay(1, rng)]
        assert 0.75 <= h.sleeps[0] <= 1.25 and 1.5 <= h.sleeps[1] <= 2.5

    def test_gives_up_after_two_retries(self, server) -> None:
        server.add(*[anthropic_error(429, "rate_limit_error", "slow") for _ in range(5)])
        with pytest.raises(OperatorAPIError) as ei:
            Harness(server).run(model="haiku")
        assert ei.value.status == 429 and ei.value.transient and len(server.requests) == 3

    def test_transport_retries_can_be_turned_off(self, server) -> None:
        server.add(anthropic_error(429, "rate_limit_error", "slow"), _ok())
        with pytest.raises(OperatorAPIError):
            Harness(server).run(model="haiku", transport_retries=0)
        assert len(server.requests) == 1

    @pytest.mark.parametrize("status,etype", [(400, "invalid_request_error"), (401, "authentication_error"),
                                              (403, "permission_error"), (404, "not_found_error")])
    def test_client_errors_are_not_retried(self, server, status, etype) -> None:
        server.add(anthropic_error(status, etype, "nope"), _ok())
        with pytest.raises(OperatorAPIError) as ei:
            Harness(server).run(model="haiku")
        assert ei.value.status == status and ei.value.error_type == etype and not ei.value.transient
        assert len(server.requests) == 1

    def test_401_message_names_the_remedy_and_the_request_id(self, server) -> None:
        server.add(anthropic_error(401, "authentication_error", "invalid x-api-key"))
        with pytest.raises(OperatorAPIError, match=r"nx config set anthropic_api_key") as ei:
            Harness(server).run(model="haiku")
        assert "req_test" in str(ei.value)

    def test_retry_after_longer_than_the_deadline_raises_without_sleeping(self, server) -> None:
        import time
        server.add(anthropic_error(429, "rate_limit_error", "slow", headers={"retry-after": "30"}), _ok())
        h = Harness(server)
        with pytest.raises(OperatorAPIError) as ei:
            h.run(model="haiku", deadline=time.monotonic() + 5.0)
        assert ei.value.status == 429 and h.sleeps == [] and len(server.requests) == 1

    def test_x_should_retry_false_is_honoured(self, server) -> None:
        server.add(anthropic_error(500, "api_error", "deterministic", headers={"x-should-retry": "false"}), _ok())
        with pytest.raises(OperatorAPIError):
            Harness(server).run(model="haiku")
        assert len(server.requests) == 1

    def test_error_event_in_the_stream_is_retried(self, server) -> None:
        err = ("error", json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))
        server.add(Step(events=anthropic_stream(tool_input=GOOD)[:3] + [err]), _ok())
        assert Harness(server).run(model="haiku").structured == GOOD
        assert len(server.requests) == 2

    def test_a_cut_stream_is_retried(self, server) -> None:
        server.add(Step(events=anthropic_stream(tool_input=GOOD), drop_after=6), _ok())
        assert Harness(server).run(model="haiku").structured == GOOD
        assert len(server.requests) == 2

    def test_connection_refused_is_a_transient_api_error(self) -> None:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        slept: list[float] = []

        async def sleep(x: float) -> None:
            slept.append(x)

        backend = AnthropicBackend(api_key=SECRET, base_url=f"http://127.0.0.1:{port}", sleep=sleep,
                                   rng=random.Random(1), trust_env=False)
        with pytest.raises(OperatorAPIError) as ei:
            asyncio.run(backend.run(DispatchRequest(prompt="p", json_schema=SCHEMA, model="haiku", timeout=5.0)))
        assert ei.value.status is None and ei.value.transient and len(slept) == 2

    def test_parse_retry_after_forms(self) -> None:
        assert parse_retry_after("7") == 7.0 and parse_retry_after("0.5") == 0.5
        assert parse_retry_after(None) is None and parse_retry_after("soon") is None
        assert parse_retry_after("-3") == 0.0
        from datetime import datetime, timezone
        now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
        assert parse_retry_after("Fri, 23 Oct 2026 12:00:30 GMT", now=lambda: now) == 30.0


# ── timeouts ────────────────────────────────────────────────────────────────


class TestTimeouts:
    def test_stall_before_the_first_event(self, server) -> None:
        server.add(Step(delay_before=30))
        with pytest.raises(OperatorTimeoutError) as ei:
            Harness(server).run(model="haiku", timeout=0.4)
        err = ei.value
        assert err.partial_text == "" and err.event_count == 0
        assert "before the first event" in str(err)
        assert len(server.requests) == 1  # a timeout is not retried

    def test_stall_mid_stream_keeps_the_partial_output(self, server, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        events = anthropic_stream(tool_json_chunks=['{"answer": ', '"par', 'tial'])
        server.add(Step(events=events, stall_after=len(events) - 4))  # inside the tool input, before content_block_stop
        with pytest.raises(OperatorTimeoutError) as ei:
            Harness(server).run(model="haiku", timeout=0.6)
        err = ei.value
        assert err.partial_text.startswith('{"answer": ')
        assert err.event_count > 0
        assert err.log_path is not None and err.log_path.exists()
        logged = err.log_path.read_text()
        assert err.partial_text in logged and "mid-stream" in str(err)

    def test_the_deadline_caps_each_phase(self, server) -> None:
        import time
        server.add(Step(delay_before=30))
        t0 = time.monotonic()
        with pytest.raises(OperatorTimeoutError):
            Harness(server).run(model="haiku", timeout=60.0, deadline=time.monotonic() + 0.4)
        assert time.monotonic() - t0 < 5.0

    def test_phase_two_gets_a_fresh_budget_after_the_first_event(self, server) -> None:
        # Phase 1 (to the first event) takes ~0.5 s of the 0.8 s budget and the stream
        # then takes another ~0.5 s: 1.0 s in all, over the budget, but each phase is
        # inside its own. Only a single budget spanning both would time out here.
        events = anthropic_stream(tool_input=GOOD)
        server.add(Step(events=events, hold=0.5, event_delay=0.5 / (len(events) - 1)))
        assert Harness(server).run(model="haiku", timeout=0.8).structured == GOOD


# ── budget ──────────────────────────────────────────────────────────────────


class TestBudget:
    def test_input_that_cannot_fit_refuses_before_any_request(self, server) -> None:
        with pytest.raises(OperatorBudgetExceededError, match="max_budget_usd"):
            Harness(server).run(model="opus", prompt="x" * 30000, max_budget_usd=0.0001)
        assert server.requests == []

    def test_max_tokens_is_clamped_to_what_the_ceiling_buys(self, server) -> None:
        server.add(_ok(model="claude-haiku-5-5"))
        Harness(server).run(model="haiku", prompt="hi", max_budget_usd=0.001)
        p = price_for("claude-haiku-5-5")
        sent = server.requests[0].body["max_tokens"]
        assert 1 <= sent < 16000
        assert sent <= int(0.001 / p.output * 1_000_000)

    def test_a_clamped_cap_is_named_in_the_max_tokens_error(self, server) -> None:
        server.add(Step(events=anthropic_stream(tool_json_chunks=['{"answer": "x'], stop_reason="max_tokens")))
        with pytest.raises(OperatorOutputError, match="max_budget_usd"):
            Harness(server).run(model="haiku", prompt="hi", max_budget_usd=0.001)

    def test_the_repair_turn_spends_from_the_same_ceiling(self, server) -> None:
        # Turn 1 costs (100*0.10 + 50*0.50)/1e6 = $0.000035 on haiku-5-5 and fits a
        # $0.00005 ceiling (its input estimate is ~$0.00003). The repair turn then has
        # $0.000015 left against an input estimate of ~$0.00004: refused, not sent.
        server.add(_ok(tool_input={"n": 1}, input_tokens=100, output_tokens=50), _ok())
        sink: list[DispatchUsage] = []
        with pytest.raises(OperatorBudgetExceededError):
            Harness(server).run(model="haiku", prompt="x" * 600, max_budget_usd=0.00005, usage_sink=sink)
        assert len(server.requests) == 1
        assert len(sink) == 1 and sink[0].num_turns == 1  # turn 1's spend is still reported

    def test_unpriced_model_with_a_ceiling_is_refused(self, server) -> None:
        with pytest.raises(OperatorError, match="no price is known"):
            Harness(server).run(model="claude-future-9", max_budget_usd=1.0)
        assert server.requests == []

    def test_unpriced_model_gives_cost_none_and_a_warning(self, server) -> None:
        server.add(_ok(model="claude-future-9"))
        sink: list[DispatchUsage] = []
        with structlog.testing.capture_logs() as logs:
            Harness(server).run(model="claude-future-9", usage_sink=sink)
        assert sink[0].cost_usd is None  # never 0.0
        assert any(e["event"] == "llm_price_unknown" and e["model"] == "claude-future-9" for e in logs)


# ── concurrency ─────────────────────────────────────────────────────────────


class TestConcurrency:
    def test_in_flight_calls_are_capped_per_process(self, server, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_MAX_CONCURRENCY", "2")
        server.add(*[Step(events=anthropic_stream(tool_input=GOOD), hold=0.25) for _ in range(6)])
        h = Harness(server)

        async def many():
            req = DispatchRequest(prompt="p", json_schema=SCHEMA, model="haiku", timeout=20.0)
            return await asyncio.gather(*[h.backend.run(req) for _ in range(6)])

        results = asyncio.run(many())
        assert all(r.structured == GOOD for r in results)
        assert server.max_inflight == 2  # the cap is reached and never exceeded

    def test_the_cap_holds_across_event_loops(self, server, monkeypatch) -> None:
        # The aspect extractor's sync entry runs each call on its own loop.
        import threading
        monkeypatch.setenv("NX_LLM_MAX_CONCURRENCY", "1")
        server.add(*[Step(events=anthropic_stream(tool_input=GOOD), hold=0.2) for _ in range(3)])
        h = Harness(server)
        out: list = []

        def worker():
            out.append(h.run(model="haiku"))

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert len(out) == 3 and server.max_inflight == 1

    @pytest.mark.parametrize("bad", ["0", "x", "-2"])
    def test_bad_concurrency_env_fails_loud(self, server, monkeypatch, bad) -> None:
        monkeypatch.setenv("NX_LLM_MAX_CONCURRENCY", bad)
        with pytest.raises(ValueError, match="NX_LLM_MAX_CONCURRENCY"):
            Harness(server).run(model="haiku")


# ── the key never leaks ─────────────────────────────────────────────────────


class TestKeyNeverLeaks:
    def _everything_text(self, logs, exc) -> str:
        return json.dumps(logs, default=str) + str(exc) + repr(exc) + repr(exc.__cause__) + repr(exc.__context__)

    def test_a_provider_error_that_echoes_the_key_is_scrubbed(self, server) -> None:
        server.add(anthropic_error(401, "authentication_error", f"invalid x-api-key: {SECRET}"))
        with structlog.testing.capture_logs() as logs, pytest.raises(OperatorAPIError) as ei:
            Harness(server).run(model="haiku")
        assert SECRET not in self._everything_text(logs, ei.value)
        assert "[REDACTED]" in str(ei.value)

    def test_a_key_shaped_token_in_a_provider_message_is_scrubbed_too(self, server) -> None:
        server.add(anthropic_error(400, "invalid_request_error", "bad key sk-ant-api03-OTHERKEYOTHERKEY123"))
        with pytest.raises(OperatorAPIError) as ei:
            Harness(server).run(model="haiku")
        assert "OTHERKEY" not in str(ei.value)

    def test_success_failure_and_retry_paths_log_no_key(self, server, caplog) -> None:
        import logging
        caplog.set_level(logging.DEBUG)
        server.add(anthropic_error(429, "rate_limit_error", "slow"), _ok(tool_input={"n": 1}), _ok(tool_input={"n": 2}))
        with structlog.testing.capture_logs() as logs, pytest.raises(OperatorOutputError) as ei:
            Harness(server).run(model="haiku")
        assert SECRET not in self._everything_text(logs, ei.value) + caplog.text

    def test_the_key_is_never_written_to_the_environment(self, server) -> None:
        server.add(_ok())
        before = dict(os.environ)
        Harness(server).run(model="haiku")
        assert dict(os.environ) == before and SECRET not in "".join(os.environ.values())

    # The timeout log's no-key pin lives in TestTheKeyIsScrubbedFromEveryThirdPartyString: a stall
    # before any event writes an empty partial, which holds no key whatever the code does.

    def test_no_key_raises_before_any_request(self, server) -> None:
        backend = AnthropicBackend(api_key=None, base_url=server.url, trust_env=False)
        with pytest.raises(OperatorError, match="no API key"):
            asyncio.run(backend.run(DispatchRequest(prompt="p", json_schema=SCHEMA, model="haiku")))
        assert server.requests == [] and backend.available().available is False

    def test_availability_reason_names_no_key(self) -> None:
        status = AnthropicBackend(api_key=SECRET, base_url="http://x", trust_env=False).available()
        assert status.available and SECRET not in status.reason


# ── nexus-2k66t.3 P2 fix round ──────────────────────────────────────────────

#: Lowercase and not ``sk-`` shaped: only the exact-key scrub can remove it (the
#: ``sk-...`` pattern scrub would hide a missing exact-key scrub for SECRET), and
#: valid as a URL scheme, so a bad ``base_url`` makes httpx quote it.
PLAIN_KEY = "zzkey-lowercase-0123456789abcdef"


def _everything(logs, exc) -> str:
    return json.dumps(logs, default=str) + str(exc) + repr(exc) + repr(exc.__cause__) + repr(exc.__context__)


class TestTheKeyIsScrubbedFromEveryThirdPartyString:
    def test_an_in_stream_error_event_that_echoes_the_key(self, server) -> None:
        err = ("error", json.dumps({"type": "error", "error": {"type": "api_error", "message": f"bad credential {PLAIN_KEY}"}}))
        server.add(*[Step(events=anthropic_stream(tool_input=GOOD)[:3] + [err]) for _ in range(3)])
        with structlog.testing.capture_logs() as logs, pytest.raises(OperatorAPIError) as ei:
            Harness(server, api_key=PLAIN_KEY).run(model="haiku")
        assert "the stream carried" in str(ei.value) and "[REDACTED]" in str(ei.value)  # non-vacuity: this path ran
        assert PLAIN_KEY not in _everything(logs, ei.value)

    def test_a_connection_failure_that_echoes_the_key(self, server) -> None:
        import httpx

        # Non-vacuity: httpx itself puts the "scheme" (here, the key) into the exception text.
        with pytest.raises(httpx.UnsupportedProtocol, match=PLAIN_KEY):
            httpx.Client(trust_env=False).post(f"{PLAIN_KEY}://x/v1/messages")
        with structlog.testing.capture_logs() as logs, pytest.raises(OperatorAPIError) as ei:
            Harness(server, api_key=PLAIN_KEY, base_url=f"{PLAIN_KEY}://x").run(model="haiku")
        assert "connection failed" in str(ei.value) and "[REDACTED]" in str(ei.value)
        assert PLAIN_KEY not in _everything(logs, ei.value)

    def test_partial_output_that_contains_the_key_is_scrubbed_in_the_error_and_the_log_file(
        self, server, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        events = anthropic_stream(tool_json_chunks=['{"answer": "my key is ', PLAIN_KEY, ' and more'])
        server.add(Step(events=events, stall_after=len(events) - 4))  # inside the tool input, before content_block_stop
        with structlog.testing.capture_logs() as logs, pytest.raises(OperatorTimeoutError) as ei:
            Harness(server, api_key=PLAIN_KEY).run(model="haiku", timeout=0.6)
        err = ei.value
        assert any(PLAIN_KEY in json.dumps(e) for e in events)  # non-vacuity: the stream did carry it
        assert err.partial_text.startswith('{"answer": "my key is ') and "[REDACTED]" in err.partial_text
        assert err.log_path is not None and "[REDACTED]" in err.log_path.read_text()
        assert PLAIN_KEY not in _everything(logs, err) + err.partial_text + err.log_path.read_text()


class TestAMemoOfARejectedForcedChoice:
    def test_each_model_pays_the_wasted_400_once_per_process(self, server) -> None:
        server.add(
            anthropic_error(400, "invalid_request_error", "tool_choice: type tool is not supported for this model"),
            _ok(model="claude-future-9"),
            _ok(model="claude-future-9"),
        )
        h = Harness(server)
        assert h.run(model="claude-future-9").structured == GOOD
        assert h.run(model="claude-future-9").structured == GOOD
        kinds = [r.body["tool_choice"]["type"] for r in server.requests]
        assert kinds == ["tool", "auto", "auto"]  # the second call went straight to auto

    def test_another_model_is_still_tried_forced(self, server) -> None:
        server.add(
            anthropic_error(400, "invalid_request_error", "tool_choice: not supported for this model"),
            _ok(model="claude-future-9"),
            _ok(model="claude-haiku-5-5"),
        )
        h = Harness(server)
        h.run(model="claude-future-9")
        h.run(model="haiku")
        assert [r.body["tool_choice"]["type"] for r in server.requests] == ["tool", "auto", "tool"]


class TestAResponseThatIsNotTheStreamWeAsked:
    def test_a_redirect_is_a_distinct_error_and_is_not_followed_or_retried(self, server) -> None:
        server.add(Step(status=302, headers={"location": "http://elsewhere.invalid/"}), _ok())
        with pytest.raises(OperatorAPIError) as ei:
            Harness(server).run(model="haiku")
        assert ei.value.status == 302 and not ei.value.transient
        assert "redirect" in str(ei.value) and "stream ended" not in str(ei.value)
        assert len(server.requests) == 1

    def test_a_200_that_is_not_an_event_stream_is_a_distinct_error_and_is_not_retried(self, server) -> None:
        server.add(Step(status=200, content_type="text/html", body=b"<html>welcome to nginx</html>"), _ok())
        with pytest.raises(OperatorAPIError) as ei:
            Harness(server).run(model="haiku")
        assert ei.value.status == 200 and not ei.value.transient
        assert "text/event-stream" in str(ei.value) and "stream ended" not in str(ei.value)
        assert len(server.requests) == 1


class TestThePermitWaitIsBoundedByTheDeadline:
    def test_a_call_queued_behind_a_slow_one_times_out_at_its_deadline(self, server, monkeypatch) -> None:
        import time

        monkeypatch.setenv("NX_LLM_MAX_CONCURRENCY", "1")
        server.add(Step(events=anthropic_stream(tool_input=GOOD), hold=2.0))
        h = Harness(server)

        async def both():
            slow = asyncio.ensure_future(
                h.backend.run(DispatchRequest(prompt="p", json_schema=SCHEMA, model="haiku", timeout=20.0)))
            await asyncio.sleep(0.3)  # let the slow call take the only permit
            t0 = time.monotonic()
            req = DispatchRequest(prompt="p", json_schema=SCHEMA, model="haiku", timeout=20.0,
                                  deadline=time.monotonic() + 0.4)
            try:
                await h.backend.run(req)
            except Exception as exc:  # noqa: BLE001 - the assertion below inspects it
                return exc, time.monotonic() - t0, await slow
            raise AssertionError("the queued call should not have run")

        exc, waited, slow_result = asyncio.run(both())
        assert isinstance(exc, OperatorTimeoutError) and "concurrency" in str(exc)
        assert waited < 1.5  # not the 2 s the holder keeps the permit
        assert slow_result.structured == GOOD and len(server.requests) == 1


class TestObservabilityOfTheTables:
    def test_the_unpriced_warning_carries_the_price_table_date(self, server) -> None:
        from nexus.operators.llm_models import PRICES_AS_OF

        server.add(_ok(model="claude-future-9"))
        with structlog.testing.capture_logs() as logs:
            Harness(server).run(model="claude-future-9")
        (event,) = [e for e in logs if e["event"] == "llm_price_unknown"]
        assert event["prices_as_of"] == PRICES_AS_OF and PRICES_AS_OF[:2] == "20"

    def test_a_repair_turn_logs_the_model_output_at_debug_never_info(self, server, monkeypatch) -> None:
        # The suite runs structlog at WARNING, so capture_logs never sees INFO or DEBUG:
        # record the module logger's calls by level instead.
        from nexus.operators import llm_api

        calls: list[tuple[str, str, dict]] = []

        class Recorder:
            def __getattr__(self, level):
                return lambda event, **kw: calls.append((level, event, kw))

        monkeypatch.setattr(llm_api, "_log", Recorder())
        server.add(_ok(tool_input={"answer": 31337}), _ok())
        Harness(server).run(model="haiku")
        at_info = [(e, kw) for level, e, kw in calls if level == "info"]
        assert not any("31337" in json.dumps(kw, default=str) for _e, kw in at_info)  # the model's value stays out of INFO
        (repair,) = [kw for e, kw in at_info if e == "llm_structured_output_repair"]
        assert repair["backend"] == "anthropic" and "reason" not in repair
        (detail,) = [kw for level, e, kw in calls if level == "debug" and e == "llm_structured_output_repair_detail"]
        assert "31337" in detail["reason"]
