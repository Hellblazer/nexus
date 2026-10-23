# SPDX-License-Identifier: AGPL-3.0-or-later
"""One scenario list, run over every backend (nexus-2k66t.3 P2).

``cli`` (a fake ``claude`` subprocess speaking stream-json), ``anthropic`` and
``openai_compat`` (the scripted stdlib server) must hand ``claude_dispatch`` the
same dict, raise the same exception types, report the same partial output on a
timeout and record usage at the same point. A backend that drifts from the
others fails here by name, which is the point of having three that must be
interchangeable behind one function.
"""
from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.operators.dispatch import (
    CliBackend,
    DispatchUsage,
    OperatorError,
    OperatorOutputError,
    OperatorTimeoutError,
    ambient_usage_sink,
    claude_dispatch,
)
from nexus.operators.llm_anthropic import AnthropicBackend
from nexus.operators.llm_openai import OpenAICompatBackend
from tests._llm_fake_server import (
    FakeLLMServer,
    Step,
    anthropic_error,
    anthropic_stream,
    openai_error,
    openai_stream,
)
from tests._module_seam import patch_in

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
GOOD = {"answer": "42"}
PARTIAL_JSON = ['{"answer": ', '"par', 'tial']

BACKENDS = ["cli", "anthropic", "openai_compat"]


@pytest.fixture(autouse=True)
def _env(monkeypatch) -> None:
    for var in ("NX_LLM_BACKEND", "NX_LLM_MAX_TOKENS", "NX_LLM_MAX_CONCURRENCY", "NX_DISPATCH_MAX_BUDGET_USD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "nexus.db.t1.mint_t1_session_token", lambda session_id, *, context: {"session_token": "t"},
    )


# ── drivers ─────────────────────────────────────────────────────────────────


def _proc(stdout_reader, returncode: int = 0, stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = MagicMock()
    proc.stdout.read = stdout_reader
    proc.stderr = MagicMock()
    errs = {"sent": False}

    async def err_read(n: int = -1) -> bytes:
        if errs["sent"]:
            return b""
        errs["sent"] = True
        return stderr

    proc.stderr.read = err_read
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    return proc


def _once(data: bytes):
    state = {"sent": False}

    async def read(n: int = -1) -> bytes:
        if state["sent"]:
            return b""
        state["sent"] = True
        return data

    return read


def _stream_event(delta: dict) -> str:
    return json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": delta}})


def _result_event(structured) -> str:
    return json.dumps({
        "type": "result", "is_error": False, "result": "", "structured_output": structured,
        "total_cost_usd": 0.01, "num_turns": 2, "usage": {"input_tokens": 3, "output_tokens": 4},
        "modelUsage": {"claude-haiku-5-5": {"inputTokens": 3, "outputTokens": 4, "costUSD": 0.01,
                                              "canonicalModel": "claude-haiku-5-5"}},
    })


class Driver:
    """Runs ``claude_dispatch`` on one backend with a scripted scenario."""

    name: str

    @contextmanager
    def scenario(self, kind: str) -> Iterator["Run"]:  # pragma: no cover - interface
        raise NotImplementedError


class Run:
    def __init__(self, backend, requests=lambda: []) -> None:
        self.backend = backend
        self.requests = requests

    def dispatch(self, *, timeout: float = 10.0, **kw):
        return asyncio.run(claude_dispatch("what is the answer?", SCHEMA, timeout, backend=self.backend, **kw))


class CliDriver(Driver):
    name = "cli"

    @contextmanager
    def scenario(self, kind: str) -> Iterator[Run]:
        if kind == "success":
            lines = [_stream_event({"type": "input_json_delta", "partial_json": json.dumps(GOOD)}), _result_event(GOOD)]
            proc = _proc(_once(("\n".join(lines) + "\n").encode()))
        elif kind == "no_structured_output":
            proc = _proc(_once((_result_event(None) + "\n").encode()))
        elif kind == "provider_error":
            proc = _proc(_once(b""), returncode=1, stderr=b"authentication failed")
        elif kind in ("stall_before_first_event", "stall_mid_stream"):
            first = ("\n".join(_stream_event({"type": "input_json_delta", "partial_json": p}) for p in PARTIAL_JSON) + "\n").encode()
            calls = {"n": 0}

            async def read(n: int = -1) -> bytes:
                calls["n"] += 1
                if kind == "stall_mid_stream" and calls["n"] == 1:
                    return first
                if calls["n"] <= (2 if kind == "stall_mid_stream" else 1):
                    await asyncio.sleep(999)  # cancelled by the timeout; the post-kill mop-up read returns b""
                return b""

            proc = _proc(read)
        else:  # pragma: no cover
            raise AssertionError(kind)
        with patch_in("nexus.operators.dispatch", "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            yield Run(CliBackend())


class _ApiDriver(Driver):
    def _backend(self, server: FakeLLMServer):  # pragma: no cover - interface
        raise NotImplementedError

    def _stream(self, **kw):  # pragma: no cover - interface
        raise NotImplementedError

    def _error(self, status: int) -> Step:  # pragma: no cover - interface
        raise NotImplementedError

    def _no_tool(self) -> Step:  # pragma: no cover - interface
        raise NotImplementedError

    @contextmanager
    def scenario(self, kind: str) -> Iterator[Run]:
        with FakeLLMServer() as server:
            if kind == "success":
                server.add(Step(events=self._stream(tool_input=GOOD)))
            elif kind == "no_structured_output":
                server.add(self._no_tool(), self._no_tool())
            elif kind == "provider_error":
                server.add(self._error(401))
            elif kind == "stall_before_first_event":
                server.add(Step(delay_before=60))
            elif kind == "stall_mid_stream":
                events = self._stream(tool_json_chunks=PARTIAL_JSON)
                server.add(Step(events=events, stall_after=self._stall_after(events)))
            else:  # pragma: no cover
                raise AssertionError(kind)
            yield Run(self._backend(server), lambda: server.requests)

    def _stall_after(self, events) -> int:  # pragma: no cover - interface
        raise NotImplementedError


class AnthropicDriver(_ApiDriver):
    name = "anthropic"

    def _backend(self, server):
        return AnthropicBackend(api_key="k-test", base_url=server.url, rng=random.Random(1), trust_env=False)

    def _stream(self, **kw):
        return anthropic_stream(**kw)

    def _error(self, status):
        return anthropic_error(status, "authentication_error", "invalid x-api-key")

    def _no_tool(self):
        return Step(events=anthropic_stream(text="no tool call"))

    def _stall_after(self, events):
        return len(events) - 4


class OpenAIDriver(_ApiDriver):
    name = "openai_compat"

    def _backend(self, server):
        return OpenAICompatBackend(base_url=server.url, model="m", api_key="k-test", rng=random.Random(1), trust_env=False)

    def _stream(self, **kw):
        if "tool_json_chunks" in kw:
            kw["tool_arg_chunks"] = kw.pop("tool_json_chunks")
        return openai_stream(**kw)

    def _error(self, status):
        return openai_error(status, "Incorrect API key", etype="authentication_error")

    def _no_tool(self):
        return Step(events=openai_stream(content="no tool call"))

    def _stall_after(self, events):
        return 5


DRIVERS: dict[str, Driver] = {"cli": CliDriver(), "anthropic": AnthropicDriver(), "openai_compat": OpenAIDriver()}


# ── scenarios ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("backend", BACKENDS)
class TestContract:
    def test_success_returns_the_same_dict_and_one_shared_usage_record(self, backend) -> None:
        sink: list[DispatchUsage] = []
        ambient: list[DispatchUsage] = []
        with DRIVERS[backend].scenario("success") as run, ambient_usage_sink(ambient):
            got = run.dispatch(usage_sink=sink)
        assert got == GOOD
        assert len(sink) == 1 and len(ambient) == 1 and sink[0] is ambient[0]
        assert isinstance(sink[0], DispatchUsage) and sink[0].num_turns is not None

    def test_the_backend_result_carries_the_usage_it_recorded(self, backend) -> None:
        # DispatchResult.usage is the same instance the sinks received (the facade hides it).
        from nexus.operators.llm_backend import DispatchRequest

        sink: list[DispatchUsage] = []
        with DRIVERS[backend].scenario("success") as run:
            result = asyncio.run(run.backend.run(
                DispatchRequest(prompt="p", json_schema=SCHEMA, timeout=10.0, usage_sink=sink)))
        assert result.structured == GOOD and result.usage is not None and sink == [result.usage]
        assert sink[0] is result.usage

    def test_no_usable_structured_output_is_an_output_error_with_usage_recorded_first(self, backend) -> None:
        sink: list[DispatchUsage] = []
        with DRIVERS[backend].scenario("no_structured_output") as run:
            with pytest.raises(OperatorOutputError):
                run.dispatch(usage_sink=sink)
        assert len(sink) == 1  # appended before the raise, on every backend

    def test_a_provider_failure_is_an_operator_error_that_is_not_an_output_or_timeout_error(self, backend) -> None:
        sink: list[DispatchUsage] = []
        with DRIVERS[backend].scenario("provider_error") as run:
            with pytest.raises(OperatorError) as ei:
                run.dispatch(usage_sink=sink)
        assert not isinstance(ei.value, (OperatorOutputError, OperatorTimeoutError))
        assert sink == []  # never reached a parse: nothing to record

    def test_a_stall_before_the_first_event_is_a_timeout_with_nothing_partial(self, backend) -> None:
        with DRIVERS[backend].scenario("stall_before_first_event") as run:
            with pytest.raises(OperatorTimeoutError) as ei:
                run.dispatch(timeout=0.4)
        assert ei.value.partial_text == ""
        assert ei.value.event_count == 0

    def test_a_stall_mid_stream_is_a_timeout_that_keeps_the_partial_output(self, backend) -> None:
        with DRIVERS[backend].scenario("stall_mid_stream") as run:
            with pytest.raises(OperatorTimeoutError) as ei:
                run.dispatch(timeout=0.6)
        err = ei.value
        assert err.partial_text.startswith('{"answer": '), err.partial_text
        assert err.event_count > 0
        assert err.log_path is not None and err.log_path.exists()
        assert err.partial_text in err.log_path.read_text()

    def test_every_error_is_an_operator_error(self, backend) -> None:
        # The facade's callers catch OperatorError; nothing a backend raises may escape that net.
        for kind, expected in (("no_structured_output", OperatorOutputError), ("provider_error", OperatorError)):
            with DRIVERS[backend].scenario(kind) as run:
                with pytest.raises(OperatorError) as ei:
                    run.dispatch()
            assert isinstance(ei.value, expected)


def test_the_scenario_matrix_covers_every_backend() -> None:
    # Non-vacuity: the parametrisation really spans the three, and each driver is distinct.
    assert set(BACKENDS) == set(DRIVERS) == {"cli", "anthropic", "openai_compat"}
    assert len({type(d) for d in DRIVERS.values()}) == 3


def test_a_dispatch_through_the_facade_selects_and_runs_an_api_backend_end_to_end(monkeypatch) -> None:
    """The whole path with nothing injected: setting -> key -> backend -> wire."""
    with FakeLLMServer() as server:
        server.add(Step(events=anthropic_stream(tool_input=GOOD)))
        monkeypatch.setenv("NX_LLM_BACKEND", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k-end-to-end")
        real = AnthropicBackend

        def build(name):
            assert name == "anthropic"
            return real(api_key="k-end-to-end", base_url=server.url, trust_env=False)

        with patch("nexus.operators.dispatch.build_api_backend", side_effect=build):
            got = asyncio.run(claude_dispatch("p", SCHEMA, 10.0, model="haiku", operator="operator_rank"))
        assert got == GOOD and server.requests[0].headers["x-api-key"] == "k-end-to-end"
