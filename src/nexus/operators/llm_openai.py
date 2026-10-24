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
models unless the operator configures one (``llm.openai_compat.price_in_per_mtok``
and ``.price_out_per_mtok``): without it ``cost_usd`` is ``None`` (one warning per
model) and a ``max_budget_usd`` ceiling is REFUSED, because it could not be
enforced. ``llm.openai_compat.unpriced_ok: true`` accepts the ceiling unenforced
(a warning says so), for a free local server.
"""
from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx
import structlog

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
from nexus.operators.llm_models import ModelPrice
from nexus.operators.llm_types import OperatorAPIError, OperatorError

_log = structlog.get_logger(__name__)

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


#: ``(base url, model)`` of every server model that answered ``max_tokens`` with a
#: 400 naming ``max_completion_tokens`` in this process: OpenAI's newer models and
#: some proxies accept only that spelling. They are sent it from then on.
_COMPLETION_TOKENS_MODELS: set[tuple[str, str]] = set()

#: Hosts a key has already been reported going to over cleartext http.
_WARNED_INSECURE: set[str] = set()


def _is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OpenAICompatBackend(ApiBackend):
    """Chat Completions backend. *base_url* is the server root (``http://localhost:11434``)
    or an OpenAI-style root ending in ``/v1`` (``https://openrouter.ai/api/v1``);
    *model* is sent on every call; *api_key* is optional (local servers take none).

    *price* is ``(usd_in_per_mtok, usd_out_per_mtok)`` from the operator's config;
    without one the call has no ``cost_usd`` and a ``max_budget_usd`` ceiling is
    refused unless *unpriced_ok* (a free local server) says to accept it unenforced.

    The server and model must support tool calling: the schema rides a function
    tool. A server that truncates prompts to its context window does so silently
    (Ollama's ``num_ctx`` default is the usual case); size it for the longest
    prompt the operators send."""

    name: ClassVar[str] = "openai_compat"
    drift_check: ClassVar[bool] = False

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        price: tuple[float, float] | None = None,
        unpriced_ok: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, **kwargs)
        self._model = model
        self._price_row = (
            # Cached prompt tokens are already inside prompt_tokens at the full input
            # rate, so the cache-read rate is 0 here: they are not billed twice.
            ModelPrice(input=price[0], output=price[1], cache_write=price[0], cache_read=0.0)
            if price is not None else None
        )
        self._unpriced_ok = unpriced_ok

    def _price(self, model: str) -> ModelPrice | None:
        return self._price_row

    def _unpriced_ceiling_remedy(self) -> str:
        return (
            "set llm.openai_compat.price_in_per_mtok and price_out_per_mtok in the global config.yml, "
            "or llm.openai_compat.unpriced_ok: true for a free local server, or unset the ceiling"
        )

    def _adapt_to_rejection(self, exc: OperatorAPIError, model: str) -> bool:
        key = (self._base_url, model)
        if exc.status == 400 and "max_completion_tokens" in str(exc).lower() and key not in _COMPLETION_TOKENS_MODELS:
            _COMPLETION_TOKENS_MODELS.add(key)
            return True
        return False

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
        parts = urlsplit(self._base_url)
        host = parts.hostname
        if self._api_key and parts.scheme == "http" and host and not _is_loopback(host) and host not in _WARNED_INSECURE:
            _WARNED_INSECURE.add(host)
            _log.warning(
                "llm_openai_compat_key_over_cleartext",
                host=host,
                effect="the openai_compat key is sent in cleartext to a host that is not this machine",
                remedy="use an https base_url",
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
        }
        # ``max_tokens`` is what Ollama, vLLM and most proxies take; OpenAI's newer
        # models refuse it for ``max_completion_tokens``. A server that says so once
        # is sent the other spelling from then on (_adapt_to_rejection).
        body["max_completion_tokens" if (self._base_url, model) in _COMPLETION_TOKENS_MODELS else "max_tokens"] = max_tokens
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
        echo = outcome.assistant
        # An answer with no content and no tool call has nothing to echo, and an
        # assistant message with null content and no tool_calls is rejected by many
        # servers: send the repair instruction alone.
        if isinstance(echo, dict) and (echo.get("content") or echo.get("tool_calls")):
            repaired.append(echo)
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
