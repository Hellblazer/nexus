# SPDX-License-Identifier: AGPL-3.0-or-later
"""The aspect extractor on a direct API backend (nexus-2k66t.3 P2).

``aspect_extractor`` keeps its own supervised ``claude -p`` ``Popen`` for the CLI
path. When the selection names an API backend it routes through
``llm_backend.run_text_sync`` instead, keeps its own 3-attempt retry, and turns
the backend's transient errors into its transient class WITHOUT letting the
backend retry underneath (3 attempts, not 3 x 3). This lands with the
availability flip: with a key and no ``claude`` the daemon's probe now says
"available", so the extractor must be able to run there.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import nexus.aspect_extractor as ae
from tests._module_seam import setattr_in
from nexus.operators.llm_anthropic import AnthropicBackend
from nexus.operators.llm_backend import DispatchResult
from nexus.operators.llm_types import (
    DispatchUsage,
    OperatorAPIError,
    OperatorError,
    OperatorOutputError,
    OperatorTimeoutError,
)
from tests._llm_fake_server import FakeLLMServer, Step, anthropic_error, anthropic_stream
from tests.test_aspect_extractor import _PAPER_SHAPED_CONTENT, _make_completed, _ok_stdout

KEY = "sk-ant-api03-ASPECTSECRET0123456789abcdef"
INNER = json.loads(_ok_stdout())["result"]  # the model's response text, as the extractor expects it


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    sleeps: list[int] = []
    monkeypatch.setattr(ae, "_sleep_with_jitter", lambda attempt: sleeps.append(attempt))
    for var in ("NX_LLM_BACKEND", "NX_LLM_MAX_CONCURRENCY"):
        monkeypatch.delenv(var, raising=False)
    return sleeps


@pytest.fixture
def server():
    with FakeLLMServer() as srv:
        yield srv


@pytest.fixture
def api_backend(server, monkeypatch):
    """No claude on PATH, a key set: the extractor must pick the API backend,
    and the backend talks to the scripted server instead of api.anthropic.com."""
    setattr_in(monkeypatch, "nexus.operators.llm_backend", "shutil.which", lambda name: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)

    def build(name):
        assert name == "anthropic"
        return AnthropicBackend(api_key=KEY, base_url=server.url, trust_env=False)

    with patch("nexus.operators.llm_backend.build_api_backend", side_effect=build):
        yield server


def _no_cli():
    return patch.object(ae, "_run_claude_isolated", side_effect=AssertionError("the CLI path must not run"))


def _extract():
    return ae.extract_aspects(content=_PAPER_SHAPED_CONTENT, source_path="/p1.pdf", collection="knowledge__delos")


class TestRouting:
    def test_extraction_runs_on_the_api_backend_and_never_spawns_claude(self, api_backend) -> None:
        api_backend.add(Step(events=anthropic_stream(text=INNER, model="claude-haiku-5-5")))
        with _no_cli():
            record = _extract()
        assert record is not None and record.problem_formulation == "Sharded WAL bottleneck."
        (req,) = api_backend.requests
        body = req.body
        assert body["model"] == "claude-haiku-5-5"  # the config's pinned id, passed through
        assert "tools" not in body and "tool_choice" not in body  # text mode: the extractor parses its own JSON
        assert "Abstract" in body["messages"][0]["content"]  # the paper content is in the prompt
        assert req.headers["x-api-key"] == KEY

    def test_a_code_fenced_response_is_still_parsed(self, api_backend) -> None:
        api_backend.add(Step(events=anthropic_stream(text=f"```json\n{INNER}\n```", model="claude-haiku-5-5")))
        with _no_cli():
            assert _extract().proposed_method.startswith("Hybrid Paxos")

    def test_with_claude_on_path_the_cli_path_is_used_even_with_a_key(self, monkeypatch) -> None:
        setattr_in(monkeypatch, "nexus.operators.llm_backend", "shutil.which", lambda name: "/usr/bin/claude")
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        calls = []

        def fake_run(prompt, timeout, **kw):
            calls.append(kw)
            return _make_completed(_ok_stdout())

        with patch.object(ae, "_run_claude_isolated", fake_run), \
                patch("nexus.operators.llm_backend.run_text_sync", side_effect=AssertionError("API path must not run")):
            record = _extract()
        assert record is not None and len(calls) == 1 and calls[0]["model"] == "claude-haiku-5-5"

    def test_with_nothing_available_the_cli_path_fails_as_it_always_did(self, monkeypatch) -> None:
        setattr_in(monkeypatch, "nexus.operators.llm_backend", "shutil.which", lambda name: None)
        calls = []

        def fake_run(prompt, timeout, **kw):
            calls.append(1)
            raise FileNotFoundError("claude")

        with patch.object(ae, "_run_claude_isolated", fake_run):
            record = _extract()  # a hard failure: the null-fields row, after ONE attempt
        assert calls == [1] and record is not None and record.problem_formulation is None

    def test_an_explicit_anthropic_setting_without_a_key_is_a_hard_failure_with_no_request(self, monkeypatch, server) -> None:
        monkeypatch.setenv("NX_LLM_BACKEND", "anthropic")
        setattr_in(monkeypatch, "nexus.operators.llm_backend", "shutil.which", lambda name: "/usr/bin/claude")
        with _no_cli():
            record = _extract()
        assert record.problem_formulation is None and server.requests == []

    def test_batch_extraction_uses_the_api_backend(self, api_backend) -> None:
        batch = {"papers": [{"source_path": "/p1.pdf", **json.loads(INNER)}]}
        api_backend.add(Step(events=anthropic_stream(text=json.dumps(batch), model="claude-haiku-5-5")))
        with _no_cli():
            out = ae.extract_aspects_batch([("knowledge__delos", "/p1.pdf", _PAPER_SHAPED_CONTENT)])
        assert len(out) == 1 and out[0] is not None and out[0].problem_formulation == "Sharded WAL bottleneck."
        assert len(api_backend.requests) == 1


class TestRetryIsNotMultiplied:
    def test_three_429s_cost_three_requests_not_nine(self, api_backend, _env) -> None:
        api_backend.add(*[anthropic_error(429, "rate_limit_error", "slow down") for _ in range(9)])
        with _no_cli():
            record = _extract()
        assert len(api_backend.requests) == 3  # the extractor's 3 attempts; the backend added none
        assert _env == [0, 1]  # slept between attempts with the extractor's own backoff schedule
        assert record is not None and record.problem_formulation is None  # exhausted: the null-fields row

    def test_two_429s_then_success(self, api_backend) -> None:
        api_backend.add(
            anthropic_error(429, "rate_limit_error", "slow"),
            anthropic_error(529, "overloaded_error", "busy"),
            Step(events=anthropic_stream(text=INNER, model="claude-haiku-5-5")),
        )
        with _no_cli():
            assert _extract().problem_formulation == "Sharded WAL bottleneck."
        assert len(api_backend.requests) == 3

    def test_a_401_is_hard_and_not_retried(self, api_backend, _env) -> None:
        api_backend.add(anthropic_error(401, "authentication_error", "invalid x-api-key"), Step(events=anthropic_stream(text=INNER)))
        with _no_cli():
            record = _extract()
        assert len(api_backend.requests) == 1 and _env == []
        assert record.problem_formulation is None

    def test_unparseable_text_is_transient_as_on_the_cli_path(self, api_backend) -> None:
        api_backend.add(
            Step(events=anthropic_stream(text="not json at all", model="claude-haiku-5-5")),
            Step(events=anthropic_stream(text=INNER, model="claude-haiku-5-5")),
        )
        with _no_cli():
            assert _extract().problem_formulation == "Sharded WAL bottleneck."
        assert len(api_backend.requests) == 2


class TestErrorMapping:
    """``_api_inner_text`` classifies the backend's exceptions."""

    @staticmethod
    def _call(exc: Exception | None, text: str = "hi"):
        def fake(backend, prompt, *, model, timeout, operator):
            if exc is not None:
                raise exc
            return DispatchResult(text=text)

        with patch("nexus.operators.llm_backend.run_text_sync", side_effect=fake):
            return ae._api_inner_text("p", "anthropic", timeout=5, model="haiku")

    def test_success_returns_the_text(self) -> None:
        assert self._call(None, "the text") == "the text"

    @pytest.mark.parametrize("exc", [
        OperatorTimeoutError("t"),
        OperatorAPIError("x", status=429),
        OperatorAPIError("x", status=529),
        OperatorAPIError("x", status=503),
        OperatorAPIError("x", status=None),
    ])
    def test_transient_failures(self, exc) -> None:
        with pytest.raises(ae._TransientFailure):
            self._call(exc)

    @pytest.mark.parametrize("exc", [
        OperatorAPIError("x", status=401),
        OperatorAPIError("x", status=400),
        OperatorOutputError("cut off"),
        OperatorError("refused"),
    ])
    def test_hard_failures(self, exc) -> None:
        with pytest.raises(ae._HardFailure):
            self._call(exc)

    def test_a_token_in_an_error_message_is_redacted(self) -> None:
        with pytest.raises(ae._HardFailure) as ei:
            self._call(OperatorAPIError(f"bad key {KEY}", status=401))
        assert KEY not in str(ei.value) and "[REDACTED]" in str(ei.value)

    def test_the_backend_is_called_with_transport_retries_off_and_the_aspect_operator(self) -> None:
        seen = {}

        def fake_run(backend, prompt, *, model, timeout, operator):
            seen.update(backend=backend, model=model, timeout=timeout, operator=operator)
            return DispatchResult(text="x")

        with patch("nexus.operators.llm_backend.run_text_sync", side_effect=fake_run):
            ae._api_inner_text("p", "openai_compat", timeout=77, model="claude-haiku-5-5")
        assert seen == {"backend": "openai_compat", "model": "claude-haiku-5-5", "timeout": 77, "operator": "aspect_extractor"}

    def test_usage_is_logged_with_the_backend_name(self, monkeypatch) -> None:
        events: list[tuple[str, dict]] = []
        stub = type("L", (), {"info": staticmethod(lambda ev, **kw: events.append((ev, kw)))})()
        monkeypatch.setattr(ae, "_log", stub)
        usage = DispatchUsage(model="claude-haiku-4-5", cost_usd=0.002, input_tokens=1, output_tokens=2,
                              cache_creation_input_tokens=None, cache_read_input_tokens=None,
                              duration_ms=5, duration_api_ms=4, num_turns=1)
        with patch("nexus.operators.llm_backend.run_text_sync", return_value=DispatchResult(text="x", usage=usage)):
            ae._api_inner_text("p", "anthropic", timeout=5, model="haiku")
        ((name, fields),) = events
        assert name == "aspect_extractor_usage"
        assert fields["backend"] == "anthropic" and fields["cost_usd"] == 0.002 and fields["models"] == ["claude-haiku-4-5"]


class TestRowsNameTheModelThatProducedThem:
    """nexus-oc98c: the model_version on a row is the model that produced it. The
    config pins a Claude id; under openai_compat the call runs on the configured
    server model, so stamping the Claude id would be a false provenance."""

    @pytest.fixture
    def openai_backend(self, server, monkeypatch):
        from nexus.operators.llm_openai import OpenAICompatBackend

        setattr_in(monkeypatch, "nexus.operators.llm_backend", "shutil.which", lambda name: None)
        monkeypatch.setenv("NX_LLM_BACKEND", "openai_compat")
        monkeypatch.setenv("NX_LLM_OPENAI_BASE_URL", server.url)
        monkeypatch.setenv("NX_LLM_OPENAI_MODEL", "router/auto")

        def build(name):
            assert name == "openai_compat"
            return OpenAICompatBackend(base_url=server.url, model="router/auto", api_key=None, trust_env=False)

        with patch("nexus.operators.llm_backend.build_api_backend", side_effect=build):
            yield server

    def test_a_single_extraction_is_stamped_with_the_model_the_server_reports(self, openai_backend) -> None:
        from tests._llm_fake_server import openai_stream

        openai_backend.add(Step(events=openai_stream(content=INNER, model="qwen2.5-14b-instruct")))
        with _no_cli():
            record = _extract()
        assert record.problem_formulation == "Sharded WAL bottleneck."
        assert record.model_version == "qwen2.5-14b-instruct"

    def test_a_batch_extraction_is_stamped_the_same_way(self, openai_backend) -> None:
        from tests._llm_fake_server import openai_stream

        batch = {"papers": [{"source_path": "/p1.pdf", **json.loads(INNER)}]}
        openai_backend.add(Step(events=openai_stream(content=json.dumps(batch), model="qwen2.5-14b-instruct")))
        with _no_cli():
            (record,) = ae.extract_aspects_batch([("knowledge__delos", "/p1.pdf", _PAPER_SHAPED_CONTENT)])
        assert record.problem_formulation == "Sharded WAL bottleneck."
        assert record.model_version == "qwen2.5-14b-instruct"

    def test_a_server_that_reports_no_model_is_stamped_with_the_configured_one(self, openai_backend) -> None:
        from tests._llm_fake_server import openai_stream

        openai_backend.add(Step(events=openai_stream(content=INNER, model="")))
        with _no_cli():
            record = _extract()
        assert record.problem_formulation == "Sharded WAL bottleneck."
        assert record.model_version == "router/auto"

    def test_a_failed_extraction_row_does_not_claim_the_claude_id_either(self, openai_backend) -> None:
        from tests._llm_fake_server import openai_error

        openai_backend.add(openai_error(401, "bad key", etype="authentication_error"))
        with _no_cli():
            record = _extract()
        assert record.problem_formulation is None
        assert record.model_version == "router/auto"  # the model the call was aimed at

    def test_the_anthropic_path_keeps_the_pinned_id(self, api_backend) -> None:
        api_backend.add(Step(events=anthropic_stream(text=INNER, model="claude-haiku-5-5")))
        with _no_cli():
            assert _extract().model_version == "claude-haiku-5-5"
        # The row is stamped from the response, so the request is what shows
        # the pinned id was the one asked for.
        (req,) = api_backend.requests
        assert req.body["model"] == ae._SCHOLARLY_PAPER_CONFIG.model_version == "claude-haiku-5-5"

    def test_the_cli_path_is_unchanged_and_clears_a_stale_api_model(self, monkeypatch) -> None:
        # An earlier API extraction on this thread must not stamp a later CLI row.
        ae._API_MODEL.model = "stale-model-from-an-earlier-call"
        setattr_in(monkeypatch, "nexus.operators.llm_backend", "shutil.which", lambda name: "/usr/bin/claude")
        with patch.object(ae, "_run_claude_isolated", lambda prompt, timeout, **kw: _make_completed(_ok_stdout())):
            record = _extract()
        assert record.model_version == "claude-haiku-5-5"
