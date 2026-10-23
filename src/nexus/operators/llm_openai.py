# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``openai_compat`` LLM backend: Chat Completions over raw ``httpx``
(nexus-2k66t.3 P2/P3), for Ollama, vLLM, OpenRouter and anything else that
speaks ``POST {base_url}/v1/chat/completions``.

It rides the same transport as the ``anthropic`` backend (``llm_api``): the
request's JSON schema is offered as one function tool, ``StructuredOutput``,
forced with ``tool_choice``; the arguments are validated locally and a failure
gets one repair turn. Where the server answers the forced choice with a 400, the
call is retried once with ``auto`` and a system line telling the model to call
the tool. A model that answers in plain text with JSON that satisfies the schema
is accepted, since small local models often do.

Where the endpoint and model come from is decided in ``llm_backend`` and is the
GLOBAL config and the environment only, never a per-repo ``.nexus.yml``: a
cloned repository must not be able to point prompts at a server of its choosing.
The configured model is used for every call; the Claude aliases dispatch passes
(``haiku``, ``opus``) mean nothing to such a server. No price is known for these
models, so ``cost_usd`` is ``None`` (with one warning per model) and a
``max_budget_usd`` ceiling is not enforced (a warning says so).
"""
from __future__ import annotations

import json
import re
from typing import Any, ClassVar

import httpx

from nexus.operators.llm_api import (
    CALL_THE_TOOL_INSTRUCTION,
    NO_VALUE,
    SCHEMA_TOOL_DESCRIPTION,
    SCHEMA_TOOL_NAME,
    ApiBackend,
    SSEEvent,
    StreamState,
    TurnOutcome,
    parse_retry_after,
)
from nexus.operators.llm_backend import BackendStatus, DispatchRequest
from nexus.operators.llm_types import OperatorAPIError, OperatorError

_FENCE = re.compile(r"^```(?:json)?\s*\n(.*?)\n?```\s*$", re.DOTALL)


class _OpenAIStream(StreamState):
    def __init__(self, scrub: Any) -> None:
        self._scrub = scrub
        self.reset()

    def reset(self) -> None:
        self.request_id: str | None = None
        self.event_count = 0
        self.finished = False
        self.model: str | None = None
        self.finish_reason: str | None = None
        self.text: list[str] = []
        self.refusal: list[str] = []
        self.calls: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, Any] = {}

    def feed(self, ev: SSEEvent) -> None:
        raw = ev.data.strip()
        if raw == "[DONE]":
            self.finished = True
            return
        try:
            data = json.loads(raw)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        self.event_count += 1
        err = data.get("error")
        if err:
            if isinstance(err, dict):
                code = err.get("code")
                message = self._scrub(str(err.get("message") or ""))[:300]
                etype = str(err.get("type")) if err.get("type") else None
            else:
                code, message, etype = None, self._scrub(str(err))[:300], None
            status = code if isinstance(code, int) and 400 <= code <= 599 else 500
            raise OperatorAPIError(
                f"openai_compat: the stream carried an error: {message}",
                status=status, error_type=etype, request_id=self.request_id,
            )
        self.model = data.get("model") or self.model
        usage = data.get("usage")
        if isinstance(usage, dict):
            self.usage = usage
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str):
                self.text.append(content)
            refusal = delta.get("refusal")
            if isinstance(refusal, str):
                self.refusal.append(refusal)
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                slot = self.calls.setdefault(int(call.get("index", 0)), {"id": "", "name": "", "args": []})
                if call.get("id"):
                    slot["id"] = str(call["id"])
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["name"] = slot["name"] or str(fn["name"])
                if isinstance(fn.get("arguments"), str):
                    slot["args"].append(fn["arguments"])
            if choice.get("finish_reason"):
                self.finish_reason = str(choice["finish_reason"])
                self.finished = True

    def partial_text(self) -> str:
        return "".join(self.text) + "".join("".join(c["args"]) for _, c in sorted(self.calls.items()))

    def outcome(self) -> TurnOutcome:
        refusal = "".join(self.refusal).strip()
        stop = "complete"
        detail = ""
        if self.finish_reason == "length":
            stop = "max_tokens"
        elif self.finish_reason == "content_filter" or refusal:
            stop, detail = "refusal", refusal
        calls = [c for _, c in sorted(self.calls.items())]
        tool = next((c for c in calls if c["name"] == SCHEMA_TOOL_NAME), calls[0] if len(calls) == 1 else None)
        text = "".join(self.text)
        echo: dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            echo["tool_calls"] = [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": "".join(c["args"])}}
                for c in calls
            ]
        usage = self.usage
        details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        out = TurnOutcome(
            model=self.model,
            stop=stop,
            detail=detail,
            text=text,
            input_tokens=_int(usage.get("prompt_tokens")),
            output_tokens=_int(usage.get("completion_tokens")),
            cache_read_tokens=_int(details.get("cached_tokens")),
            assistant=echo,
            tool_call_ids=[c["id"] for c in calls],
            request_id=self.request_id,
        )
        if tool is not None:
            out.has_tool_call = True
            args = "".join(tool["args"])
            if not args.strip():
                out.tool_input = {}
            else:
                try:
                    out.tool_input = json.loads(args)
                except ValueError as exc:
                    out.tool_input_error = str(exc)[:120]
        return out


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class OpenAICompatBackend(ApiBackend):
    """Chat Completions backend. *base_url* is the server root (``http://localhost:11434``)
    or an OpenAI-style root ending in ``/v1`` (``https://openrouter.ai/api/v1``);
    *model* is sent on every call; *api_key* is optional (local servers take none)."""

    name: ClassVar[str] = "openai_compat"
    budget_enforceable_without_price: ClassVar[bool] = True
    drift_check: ClassVar[bool] = False

    def __init__(self, *, base_url: str, model: str, api_key: str | None = None, **kwargs: Any) -> None:
        super().__init__(base_url=base_url, api_key=api_key, **kwargs)
        self._model = model

    def available(self) -> BackendStatus:
        if self._base_url and self._model:
            return BackendStatus("openai_compat", True, f"base_url and model are configured (model {self._model})")
        return BackendStatus("openai_compat", False, "llm.openai_compat.base_url and .model are not both set")

    def _check_ready(self) -> None:
        if not (self._base_url and self._model):
            raise OperatorError(
                "the openai_compat backend needs a base_url and a model "
                "(llm.openai_compat.base_url / .model in the global config.yml, or "
                "NX_LLM_OPENAI_BASE_URL / NX_LLM_OPENAI_MODEL)"
            )

    def _url(self) -> str:
        if self._base_url.endswith("/v1"):
            return f"{self._base_url}/chat/completions"
        return f"{self._base_url}/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json", "accept": "text/event-stream"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        return headers

    def _resolve_model(self, req: DispatchRequest) -> str:
        return self._model

    def _initial_messages(self, req: DispatchRequest, *, auto_mode: bool) -> list[Any]:
        return [{"role": "user", "content": req.prompt}]

    def _body(
        self,
        messages: list[Any],
        *,
        model: str,
        max_tokens: int,
        tool_schema: dict[str, Any] | None,
        forced: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": max_tokens,
        }
        if tool_schema is not None:
            body["tools"] = [{
                "type": "function",
                "function": {
                    "name": SCHEMA_TOOL_NAME,
                    "description": SCHEMA_TOOL_DESCRIPTION,
                    "parameters": tool_schema,
                },
            }]
            if forced:
                body["tool_choice"] = {"type": "function", "function": {"name": SCHEMA_TOOL_NAME}}
            else:
                body["tool_choice"] = "auto"
                body["messages"] = [{"role": "system", "content": CALL_THE_TOOL_INSTRUCTION}, *messages]
        return body

    def _repair(self, messages: list[Any], outcome: TurnOutcome, feedback: str) -> list[Any]:
        repaired = list(messages)
        repaired.append(outcome.assistant)
        ids = [i for i in outcome.tool_call_ids if i]
        if ids:
            for n, call_id in enumerate(ids):
                content = (
                    f"{feedback}. Call {SCHEMA_TOOL_NAME} again with a corrected input." if n == 0
                    else "Not executed."
                )
                repaired.append({"role": "tool", "tool_call_id": call_id, "content": content})
        else:
            repaired.append({"role": "user", "content": f"{feedback}. {CALL_THE_TOOL_INSTRUCTION}"})
        return repaired

    def _salvage_text(self, outcome: TurnOutcome) -> Any:
        text = outcome.text.strip()
        match = _FENCE.match(text)
        if match:
            text = match.group(1).strip()
        if not text.startswith(("{", "[")):
            return NO_VALUE
        try:
            return json.loads(text)
        except ValueError:
            return NO_VALUE

    def _new_stream(self) -> StreamState:
        return _OpenAIStream(self._scrub)

    def _http_error(self, status: int, headers: httpx.Headers, body: bytes) -> OperatorAPIError:
        etype: str | None = None
        message = ""
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            err = parsed.get("error", parsed.get("detail"))
            if isinstance(err, dict):
                etype = str(err.get("type")) if err.get("type") else None
                message = str(err.get("message") or "")
            elif err:
                message = str(err)
        message = self._scrub(message)[:300]
        text = f"openai_compat API error {status}"
        if etype:
            text += f" ({etype})"
        if message:
            text += f": {message}"
        if status in (401, 403):
            text += " -- check the openai_compat key (nx config set openai_compat_api_key <key>)"
        return OperatorAPIError(
            text,
            status=status,
            error_type=etype,
            request_id=headers.get("x-request-id"),
            retry_after=parse_retry_after(headers.get("retry-after")),
        )

    def _forced_choice_rejected(self, exc: OperatorAPIError) -> bool:
        return exc.status == 400 and "tool_choice" in str(exc).lower()
