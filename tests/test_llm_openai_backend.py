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
    monkeypatch.setattr(llm_api, "_FORCED_REJECTED", set(), raising=False)  # per-process memo of a rejected forced tool_choice
    from nexus.operators import llm_openai

    monkeypatch.setattr(llm_openai, "_COMPLETION_TOKENS_MODELS", set(), raising=False)  # per-process memo: server wants max_completion_tokens
    monkeypatch.setattr(llm_openai, "_WARNED_INSECURE", set(), raising=False)
    for var in ("NX_LLM_MAX_TOKENS", "NX_LLM_MAX_CONCURRENCY", "NX_LLM_BACKEND"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def server():
    with FakeLLMServer() as srv:
        yield srv


class Harness:
    def __init__(self, server: FakeLLMServer, *, api_key: str | None = KEY, base_url: str | None = None,
                 model: str = "llama3.1", **backend_kw) -> None:
        self.server = server
        self.sleeps: list[float] = []

        async def sleep(x: float) -> None:
            self.sleeps.append(x)

        self.backend = OpenAICompatBackend(
            base_url=base_url or server.url, model=model, api_key=api_key,
            sleep=sleep, rng=random.Random(3), trust_env=False, **backend_kw,
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


# ── nexus-2k66t.3 P2 fix round ──────────────────────────────────────────────

PLAIN_KEY = "zzkey-lowercase-0123456789abcdef"


def _everything(logs, exc) -> str:
    return json.dumps(logs, default=str) + str(exc) + repr(exc) + repr(exc.__cause__) + repr(exc.__context__)


class TestABudgetCeilingNeedsAPriceOrAnExplicitOptOut:
    def test_a_ceiling_with_no_price_is_refused_before_any_request(self, server) -> None:
        server.add(_ok())
        with pytest.raises(OperatorError, match="price_in_per_mtok") as ei:
            Harness(server).run(max_budget_usd=0.01)
        assert "unpriced_ok" in str(ei.value) and server.requests == []

    def test_no_ceiling_needs_no_price(self, server) -> None:
        server.add(_ok())
        assert Harness(server).run().structured == GOOD

    def test_unpriced_ok_accepts_the_ceiling_unenforced_and_says_so(self, server) -> None:
        server.add(_ok())
        with structlog.testing.capture_logs() as logs:
            assert Harness(server, unpriced_ok=True).run(max_budget_usd=0.01).structured == GOOD
        assert any(e["event"] == "llm_budget_not_enforceable" for e in logs)

    def test_a_configured_price_enforces_the_ceiling_and_prices_the_call(self, server) -> None:
        server.add(_ok())
        sink: list[DispatchUsage] = []
        result = Harness(server, price=(1.0, 2.0)).run(max_budget_usd=5.0, usage_sink=sink)
        assert result.structured == GOOD
        assert sink[0].cost_usd == pytest.approx((40 * 1.0 + 20 * 2.0) / 1_000_000)  # 40 in, 20 out tokens

    def test_a_priced_ceiling_clamps_max_tokens(self, server) -> None:
        server.add(_ok())
        # $0.0005 at $2.0/Mtok output buys at most ~250 output tokens beyond the input.
        Harness(server, price=(1.0, 2.0)).run(max_budget_usd=0.0005)
        assert 0 < server.requests[0].body["max_tokens"] < 16000

    def test_a_priced_ceiling_smaller_than_the_input_refuses_before_any_request(self, server) -> None:
        from nexus.operators.llm_types import OperatorBudgetExceededError

        with pytest.raises(OperatorBudgetExceededError):
            Harness(server, price=(1000.0, 2000.0)).run(prompt="x" * 3000, max_budget_usd=0.001)
        assert server.requests == []


class TestTheKeyIsScrubbedFromEveryThirdPartyString:
    def test_an_in_stream_error_chunk_that_echoes_the_key(self, server) -> None:
        chunk = (None, json.dumps({"error": {"code": 500, "message": f"bad credential {PLAIN_KEY}"}}))
        server.add(*[Step(events=[chunk]) for _ in range(3)])
        with structlog.testing.capture_logs() as logs, pytest.raises(OperatorAPIError) as ei:
            Harness(server, api_key=PLAIN_KEY).run()
        assert "the stream carried an error" in str(ei.value) and "[REDACTED]" in str(ei.value)
        assert PLAIN_KEY not in _everything(logs, ei.value)

    def test_a_bare_string_error_chunk_that_echoes_the_key(self, server) -> None:
        chunk = (None, json.dumps({"error": f"upstream rejected {PLAIN_KEY}"}))
        server.add(*[Step(events=[chunk]) for _ in range(3)])
        with structlog.testing.capture_logs() as logs, pytest.raises(OperatorAPIError) as ei:
            Harness(server, api_key=PLAIN_KEY).run()
        assert "[REDACTED]" in str(ei.value) and PLAIN_KEY not in _everything(logs, ei.value)

    def test_a_connection_failure_that_echoes_the_key(self, server) -> None:
        with structlog.testing.capture_logs() as logs, pytest.raises(OperatorAPIError) as ei:
            Harness(server, api_key=PLAIN_KEY, base_url=f"{PLAIN_KEY}://x").run()
        assert "connection failed" in str(ei.value) and "[REDACTED]" in str(ei.value)
        assert PLAIN_KEY not in _everything(logs, ei.value)

    def test_partial_output_that_contains_the_key_is_scrubbed_in_the_error_and_the_log_file(
        self, server, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        events = openai_stream(tool_arg_chunks=['{"answer": "my key is ', PLAIN_KEY, ' and more'])
        server.add(Step(events=events, stall_after=5))
        with structlog.testing.capture_logs() as logs, pytest.raises(OperatorTimeoutError) as ei:
            Harness(server, api_key=PLAIN_KEY).run(timeout=0.6)
        err = ei.value
        assert any(PLAIN_KEY in data for _name, data in events)  # non-vacuity: the stream did carry it
        assert "my key is" in err.partial_text
        assert "[REDACTED]" in err.partial_text and "[REDACTED]" in err.log_path.read_text()
        assert PLAIN_KEY not in _everything(logs, err) + err.partial_text + err.log_path.read_text()


class TestUsageNamesTheModelTheCallWasSentTo:
    def test_a_server_that_reports_no_model_gets_the_configured_one(self, server) -> None:
        server.add(Step(events=openai_stream(tool_input=GOOD, model="")))
        sink: list[DispatchUsage] = []
        Harness(server, model="gpu-box-model").run(usage_sink=sink)
        assert sink[0].model == "gpu-box-model"

    def test_the_model_the_server_reports_wins(self, server) -> None:
        server.add(Step(events=openai_stream(tool_input=GOOD, model="served-variant")))
        sink: list[DispatchUsage] = []
        Harness(server, model="router/auto").run(usage_sink=sink)
        assert sink[0].model == "served-variant"


class TestAMemoOfARejectedForcedChoice:
    def test_each_model_pays_the_wasted_400_once_per_process(self, server) -> None:
        server.add(openai_error(400, "tool_choice is not supported by this model"), _ok(), _ok())
        h = Harness(server)
        h.run()
        h.run()
        kinds = [r.body["tool_choice"] if isinstance(r.body["tool_choice"], str) else "forced" for r in server.requests]
        assert kinds == ["forced", "auto", "auto"]

    def test_another_model_is_still_tried_forced(self, server) -> None:
        server.add(openai_error(400, "tool_choice is not supported by this model"), _ok(), _ok())
        Harness(server, model="model-a").run()
        Harness(server, model="model-b").run()
        kinds = [r.body["tool_choice"] if isinstance(r.body["tool_choice"], str) else "forced" for r in server.requests]
        assert kinds == ["forced", "auto", "forced"]


class TestMaxCompletionTokens:
    UNSUPPORTED = ("Unsupported parameter: 'max_tokens' is not supported with this model. "
                   "Use 'max_completion_tokens' instead.")

    def test_max_tokens_is_the_default(self, server) -> None:
        server.add(_ok())
        Harness(server).run()
        body = server.requests[0].body
        assert "max_tokens" in body and "max_completion_tokens" not in body

    def test_a_server_that_names_max_completion_tokens_is_retried_with_it_and_remembered(self, server) -> None:
        server.add(openai_error(400, self.UNSUPPORTED), _ok(), _ok())
        h = Harness(server)
        assert h.run().structured == GOOD
        assert h.run().structured == GOOD
        first, second, third = (r.body for r in server.requests)
        assert "max_tokens" in first
        assert second["max_completion_tokens"] == 16000 and "max_tokens" not in second
        assert third["max_completion_tokens"] == 16000 and "max_tokens" not in third  # remembered: no second wasted 400

    def test_an_unrelated_400_naming_neither_is_not_retried(self, server) -> None:
        server.add(openai_error(400, "model not found"), _ok())
        with pytest.raises(OperatorAPIError):
            Harness(server).run()
        assert len(server.requests) == 1


class TestAKeyOverCleartextHttp:
    @pytest.mark.parametrize("url", ["http://gpu-box.example:8000", "http://10.1.2.3:8000"])
    def test_a_key_sent_over_http_to_another_host_warns_once_naming_the_host_only(self, url) -> None:
        backend = OpenAICompatBackend(base_url=url, model="m", api_key=KEY)
        with structlog.testing.capture_logs() as logs:
            backend._check_ready()
            backend._check_ready()
        (event,) = [e for e in logs if e["event"] == "llm_openai_compat_key_over_cleartext"]
        assert event["host"] in url and KEY not in json.dumps(logs, default=str)

    @pytest.mark.parametrize("url,key", [
        ("http://localhost:11434", KEY), ("http://127.0.0.1:11434", KEY), ("http://[::1]:11434", KEY),
        ("https://openrouter.ai/api/v1", KEY), ("http://gpu-box.example:8000", None),
    ])
    def test_loopback_https_and_keyless_do_not_warn(self, url, key) -> None:
        with structlog.testing.capture_logs() as logs:
            OpenAICompatBackend(base_url=url, model="m", api_key=key)._check_ready()
        assert not any(e["event"] == "llm_openai_compat_key_over_cleartext" for e in logs)


class TestARepairNeverEchoesAnEmptyAssistantMessage:
    def test_an_empty_answer_is_repaired_without_a_null_content_assistant_turn(self, server) -> None:
        server.add(Step(events=openai_stream()), _ok())  # no content, no tool call
        assert Harness(server).run().structured == GOOD
        messages = server.requests[1].body["messages"]
        assert not any(m["role"] == "assistant" and m.get("content") is None and not m.get("tool_calls") for m in messages)
        assert messages[-1]["role"] == "user" and "StructuredOutput" in messages[-1]["content"]

    def test_a_text_answer_is_still_echoed(self, server) -> None:
        server.add(Step(events=openai_stream(content="The answer is 42")), _ok())
        Harness(server).run()
        messages = server.requests[1].body["messages"]
        assert messages[1] == {"role": "assistant", "content": "The answer is 42"}


class TestAResponseThatIsNotTheStreamWeAsked:
    def test_a_redirect_is_a_distinct_error_and_is_not_followed_or_retried(self, server) -> None:
        server.add(Step(status=301, headers={"location": "https://elsewhere.invalid/v1/chat/completions"}), _ok())
        with pytest.raises(OperatorAPIError) as ei:
            Harness(server).run()
        assert ei.value.status == 301 and not ei.value.transient and "redirect" in str(ei.value)
        assert len(server.requests) == 1

    def test_a_json_200_is_a_distinct_error_and_is_not_retried(self, server) -> None:
        # A server that ignores stream:true answers one JSON document.
        server.add(Step(status=200, content_type="application/json", body=b'{"choices": []}'), _ok())
        with pytest.raises(OperatorAPIError) as ei:
            Harness(server).run()
        assert "text/event-stream" in str(ei.value) and "stream ended" not in str(ei.value)
        assert not ei.value.transient and len(server.requests) == 1
