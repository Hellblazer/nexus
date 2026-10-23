# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``openai_compat`` backend against a scripted stdlib HTTP server
(nexus-2k66t.3 P2): the same scenarios as the ``anthropic`` backend, over the
same transport, in Chat Completions shapes (Ollama, vLLM, OpenRouter)."""
from __future__ import annotations

import asyncio
import json
import random

import pytest
import structlog

from nexus.operators.llm_backend import DispatchRequest
from nexus.operators.llm_openai import OpenAICompatBackend
from nexus.operators.llm_types import (
    DispatchUsage,
    OperatorAPIError,
    OperatorError,
    OperatorOutputError,
    OperatorTimeoutError,
)
from tests._llm_fake_server import FakeLLMServer, Step, openai_error, openai_stream

KEY = "or-v1-ZZTOPSECRETOPENROUTERKEY0123456789"
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
GOOD = {"answer": "42"}


@pytest.fixture(autouse=True)
def _llm_env(monkeypatch) -> None:
    # The unpriced-model warning is once per process per model; a test that asserts it
    # must not depend on which test ran first in its worker.
    from nexus.operators import llm_api

    monkeypatch.setattr(llm_api, "_WARNED_UNPRICED", set())
    for var in ("NX_LLM_MAX_TOKENS", "NX_LLM_MAX_CONCURRENCY", "NX_LLM_BACKEND"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def server():
    with FakeLLMServer() as srv:
        yield srv


class Harness:
    def __init__(self, server: FakeLLMServer, *, api_key: str | None = KEY, base_url: str | None = None,
                 model: str = "llama3.1") -> None:
        self.server = server
        self.sleeps: list[float] = []

        async def sleep(x: float) -> None:
            self.sleeps.append(x)

        self.backend = OpenAICompatBackend(
            base_url=base_url or server.url, model=model, api_key=api_key,
            sleep=sleep, rng=random.Random(3), trust_env=False,
        )

    def run(self, **kw):
        kw.setdefault("json_schema", SCHEMA)
        kw.setdefault("prompt", "what is the answer?")
        kw.setdefault("timeout", 10.0)
        return asyncio.run(self.backend.run(DispatchRequest(**kw)))


def _ok(**kw) -> Step:
    return Step(events=openai_stream(tool_input=kw.pop("tool_input", GOOD), **kw))


class TestSuccess:
    def test_request_shape_and_result(self, server) -> None:
        server.add(_ok())
        sink: list[DispatchUsage] = []
        result = Harness(server).run(usage_sink=sink, model="haiku", operator="operator_rank")
        assert result.structured == GOOD
        (rec,) = server.requests
        assert rec.path == "/v1/chat/completions"
        assert rec.headers["authorization"] == f"Bearer {KEY}"
        body = rec.body
        assert body["model"] == "llama3.1"  # the configured model, never the Claude alias
        assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
        assert body["max_tokens"] == 16000
        assert body["messages"] == [{"role": "user", "content": "what is the answer?"}]
        (tool,) = body["tools"]
        assert tool == {"type": "function", "function": {
            "name": "StructuredOutput", "description": tool["function"]["description"], "parameters": SCHEMA}}
        assert body["tool_choice"] == {"type": "function", "function": {"name": "StructuredOutput"}}
        (usage,) = sink
        assert usage is result.usage
        assert (usage.model, usage.input_tokens, usage.output_tokens) == ("llama3.1", 40, 20)
        assert usage.cost_usd is None  # no price for a local model, and never 0.0

    def test_no_key_sends_no_authorization_header(self, server) -> None:
        server.add(_ok())
        Harness(server, api_key=None).run()
        assert "authorization" not in server.requests[0].headers

    @pytest.mark.parametrize("base,path", [
        ("{url}", "/v1/chat/completions"),
        ("{url}/", "/v1/chat/completions"),
        ("{url}/v1", "/v1/chat/completions"),
        ("{url}/api/v1", "/api/v1/chat/completions"),
    ])
    def test_base_url_forms(self, server, base, path) -> None:
        server.add(_ok())
        Harness(server, base_url=base.format(url=server.url)).run()
        assert server.requests[0].path == path

    def test_server_that_sends_no_usage_chunk_gives_none_not_zero(self, server) -> None:
        server.add(_ok(prompt_tokens=None))
        sink: list[DispatchUsage] = []
        Harness(server).run(usage_sink=sink)
        assert sink[0].input_tokens is None and sink[0].output_tokens is None and sink[0].cost_usd is None

    def test_unpriced_warning_is_logged(self, server) -> None:
        server.add(_ok(model="some-new-local-model"))
        with structlog.testing.capture_logs() as logs:
            Harness(server, model="some-new-local-model").run()
        assert any(e["event"] == "llm_price_unknown" and e["backend"] == "openai_compat" for e in logs)

    def test_drift_check_does_not_fire_for_a_local_model(self, server) -> None:
        server.add(_ok())
        with structlog.testing.capture_logs() as logs:
            Harness(server).run(model="haiku")
        assert not any(e["event"] == "model_family_drift" for e in logs)

    def test_json_in_plain_text_is_accepted_when_it_validates(self, server) -> None:
        server.add(Step(events=openai_stream(content='```json\n{"answer": "42"}\n```')))
        assert Harness(server).run().structured == GOOD
        assert len(server.requests) == 1

    def test_plain_text_that_is_not_the_schema_is_repaired(self, server) -> None:
        server.add(Step(events=openai_stream(content="The answer is 42")), _ok())
        assert Harness(server).run().structured == GOOD
        msgs = server.requests[1].body["messages"]
        assert msgs[1]["role"] == "assistant" and msgs[2]["role"] == "user"

    def test_text_mode(self, server) -> None:
        server.add(Step(events=openai_stream(content="hello there")))
        result = Harness(server).run(json_schema=None)
        assert result.text == "hello there"
        assert "tools" not in server.requests[0].body

    def test_forced_choice_rejected_falls_back_to_auto(self, server) -> None:
        server.add(openai_error(400, "tool_choice is not supported by this model"), _ok())
        assert Harness(server).run().structured == GOOD
        first, second = (r.body for r in server.requests)
        assert first["tool_choice"]["type"] == "function" and second["tool_choice"] == "auto"
        assert second["messages"][0]["role"] == "system" and "StructuredOutput" in second["messages"][0]["content"]


class TestRepair:
    def test_schema_failure_then_repair(self, server) -> None:
        server.add(_ok(tool_input={"nope": 1}, call_id="call_bad"), _ok())
        sink: list[DispatchUsage] = []
        assert Harness(server).run(usage_sink=sink).structured == GOOD
        second = server.requests[1].body["messages"]
        assert second[1]["role"] == "assistant" and second[1]["tool_calls"][0]["id"] == "call_bad"
        assert second[2]["role"] == "tool" and second[2]["tool_call_id"] == "call_bad"
        assert "answer" in second[2]["content"]
        assert sink[0].num_turns == 2 and sink[0].input_tokens == 80

    def test_two_failures_raise_after_recording(self, server) -> None:
        server.add(_ok(tool_input={"nope": 1}), _ok(tool_input={"answer": 7}))
        sink: list[DispatchUsage] = []
        with pytest.raises(OperatorOutputError, match="after one repair turn"):
            Harness(server).run(usage_sink=sink)
        assert len(sink) == 1 and len(server.requests) == 2

    def test_length_finish_reason_names_the_cap(self, server, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_MAX_TOKENS", "55")
        server.add(Step(events=openai_stream(tool_arg_chunks=['{"answer": "x'], finish_reason="length")))
        with pytest.raises(OperatorOutputError, match="max_tokens=55"):
            Harness(server).run()

    def test_refusal(self, server) -> None:
        server.add(Step(events=openai_stream(refusal="I cannot help with that")))
        with pytest.raises(OperatorError, match="refused") as ei:
            Harness(server).run()
        assert not isinstance(ei.value, OperatorOutputError) and "cannot help" in str(ei.value)

    def test_content_filter_is_a_refusal(self, server) -> None:
        server.add(Step(events=openai_stream(finish_reason="content_filter")))
        with pytest.raises(OperatorError, match="refused"):
            Harness(server).run()


class TestTransport:
    def test_429_retry_after_then_success(self, server) -> None:
        server.add(openai_error(429, "rate limited", headers={"retry-after": "3"}, etype="rate_limit_error"), _ok())
        h = Harness(server)
        assert h.run().structured == GOOD
        assert h.sleeps == [3.0]

    def test_401_not_retried_and_hints_the_remedy(self, server) -> None:
        server.add(openai_error(401, "bad key", etype="authentication_error"), _ok())
        with pytest.raises(OperatorAPIError, match="openai_compat_api_key") as ei:
            Harness(server).run()
        assert ei.value.status == 401 and len(server.requests) == 1

    def test_503_exhausts_retries(self, server) -> None:
        server.add(*[openai_error(503, "down", etype="server_error") for _ in range(4)])
        with pytest.raises(OperatorAPIError) as ei:
            Harness(server).run()
        assert ei.value.status == 503 and len(server.requests) == 3

    def test_error_chunk_in_stream_carries_its_status(self, server) -> None:
        err = (None, json.dumps({"error": {"code": 429, "message": "upstream rate limit"}}))
        server.add(Step(events=[err]), _ok())
        h = Harness(server)
        assert h.run().structured == GOOD and len(server.requests) == 2

    def test_a_cut_stream_is_retried(self, server) -> None:
        server.add(Step(events=openai_stream(tool_input=GOOD), drop_after=3), _ok())
        assert Harness(server).run().structured == GOOD

    def test_stall_before_first_event(self, server) -> None:
        server.add(Step(delay_before=30))
        with pytest.raises(OperatorTimeoutError) as ei:
            Harness(server).run(timeout=0.4)
        assert ei.value.partial_text == ""

    def test_stall_mid_stream_keeps_partial(self, server, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        events = openai_stream(tool_arg_chunks=['{"answer": ', '"par', 'tial'])
        server.add(Step(events=events, stall_after=5))
        with pytest.raises(OperatorTimeoutError) as ei:
            Harness(server).run(timeout=0.6)
        assert ei.value.partial_text.startswith('{"answer": ') and ei.value.log_path.exists()

    def test_a_budget_ceiling_is_not_enforced_but_says_so(self, server) -> None:
        server.add(_ok())
        with structlog.testing.capture_logs() as logs:
            assert Harness(server).run(max_budget_usd=0.01).structured == GOOD
        assert any(e["event"] == "llm_budget_not_enforceable" for e in logs)

    def test_concurrency_cap(self, server, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_MAX_CONCURRENCY", "2")
        server.add(*[Step(events=openai_stream(tool_input=GOOD), hold=0.2) for _ in range(5)])
        h = Harness(server)

        async def many():
            req = DispatchRequest(prompt="p", json_schema=SCHEMA, timeout=20.0)
            return await asyncio.gather(*[h.backend.run(req) for _ in range(5)])

        asyncio.run(many())
        assert server.max_inflight == 2


class TestKeyNeverLeaks:
    def test_provider_message_echoing_the_key_is_scrubbed(self, server) -> None:
        server.add(openai_error(401, f"Incorrect API key provided: {KEY}", etype="authentication_error"))
        with structlog.testing.capture_logs() as logs, pytest.raises(OperatorAPIError) as ei:
            Harness(server).run()
        text = json.dumps(logs, default=str) + str(ei.value) + repr(ei.value)
        assert KEY not in text and "[REDACTED]" in str(ei.value)

    def test_key_absent_from_logs_on_retry_and_success(self, server) -> None:
        server.add(openai_error(503, "down", etype="server_error"), _ok())
        with structlog.testing.capture_logs() as logs:
            Harness(server).run()
        assert KEY not in json.dumps(logs, default=str)

    def test_unconfigured_backend_raises_before_any_request(self, server) -> None:
        backend = OpenAICompatBackend(base_url="", model="", api_key=None, trust_env=False)
        with pytest.raises(OperatorError, match="base_url and a model"):
            asyncio.run(backend.run(DispatchRequest(prompt="p", json_schema=SCHEMA)))
        assert backend.available().available is False
