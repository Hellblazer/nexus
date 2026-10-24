# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``anthropic`` LLM backend: the Messages API over raw ``httpx``
(nexus-2k66t.3 P2).

``POST {base}/v1/messages`` with ``stream: true``. The request's JSON schema is
offered as one tool, ``StructuredOutput`` (the same tool the ``claude -p
--json-schema`` path forces, see ``tests/fixtures/claude_dispatch_stream_json_sample.ndjson``),
and the tool input is validated locally; ``llm_api`` holds the loop, the
transport, the retries and the budget.

Two facts from the API reference (Context7 ``/websites/platform_claude_en``,
2026-10-23) shape the request:

* Claude Opus 5.5, Sonnet 5.5 and Fable 5.1 **reject** a forced
  ``tool_choice`` with a 400. For those the tool is offered with
  ``tool_choice: auto`` and the system prompt tells the model to call it;
  ``llm_models.supports_forced_tool_choice`` names them, and a 400 that blames
  ``tool_choice`` on any other model is retried once with ``auto``.
* Strict tool use needs ``additionalProperties: false`` on every object, which
  would empty the open objects some operator schemas carry, so ``strict`` is not
  set; local ``jsonschema`` validation plus one repair turn replace it.

Streaming details: ``message_delta.usage`` is cumulative, so later values
replace earlier ones; a ``thinking`` block (always on for some models) is
accumulated with its ``signature`` and echoed back unchanged in a repair turn,
as the API requires; an ``error`` event mid-stream is an ``OperatorAPIError``
whose status is the one that error type has over plain HTTP.
"""
from __future__ import annotations

import json
from typing import Any, ClassVar

import httpx

from nexus.operators.llm_api import (
    CALL_THE_TOOL_INSTRUCTION,
    SCHEMA_TOOL_DESCRIPTION,
    SCHEMA_TOOL_NAME,
    ApiBackend,
    SSEEvent,
    StreamState,
    TurnOutcome,
    parse_retry_after,
)
from nexus.operators.llm_backend import BackendStatus, DispatchRequest
from nexus.operators.llm_models import ModelPrice, price_for, supports_forced_tool_choice
from nexus.operators.llm_types import OperatorAPIError, OperatorError
from nexus.operators.model_tiers import resolve_api_model

ANTHROPIC_API_URL: str = "https://api.anthropic.com"
ANTHROPIC_VERSION: str = "2023-06-01"

#: HTTP status an in-stream ``error`` event of each type stands for.
_ERROR_EVENT_STATUS: dict[str, int] = {
    "overloaded_error": 529,
    "api_error": 500,
    "rate_limit_error": 429,
    "timeout_error": 504,
}
_KEY_HINT = "check anthropic_api_key (nx config set anthropic_api_key <key>, or export ANTHROPIC_API_KEY)"
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def resolved_model_id(requested: str | None) -> str:
    """The API model id a dispatch ``model`` value (an alias, an id, or ``None``)
    is sent as: what a row written from this call should name."""
    return resolve_api_model(requested)


class _AnthropicStream(StreamState):
    def __init__(self, scrub: Any) -> None:
        self._scrub = scrub
        self.reset()

    def reset(self) -> None:
        self.request_id: str | None = None
        self.event_count = 0
        self.finished = False
        self.model: str | None = None
        self.stop_reason: str | None = None
        self.stop_details: dict[str, Any] | None = None
        self.usage: dict[str, int] = {}
        self.blocks: dict[int, dict[str, Any]] = {}

    def _merge_usage(self, usage: Any) -> None:
        if isinstance(usage, dict):
            for key in _USAGE_KEYS:
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    self.usage[key] = value  # cumulative: the latest value wins

    def feed(self, ev: SSEEvent) -> None:
        try:
            data = json.loads(ev.data)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        self.event_count += 1
        kind = data.get("type")
        if kind == "message_start":
            message = data.get("message") or {}
            self.model = message.get("model") or self.model
            self._merge_usage(message.get("usage"))
        elif kind == "content_block_start":
            block = dict(data.get("content_block") or {})
            block["_json"] = []
            self.blocks[int(data.get("index", len(self.blocks)))] = block
        elif kind == "content_block_delta":
            block = self.blocks.get(int(data.get("index", -1)))
            delta = data.get("delta") or {}
            if block is None:
                return
            dtype = delta.get("type")
            if dtype == "text_delta":
                block["text"] = block.get("text", "") + str(delta.get("text", ""))
            elif dtype == "input_json_delta":
                block["_json"].append(str(delta.get("partial_json", "")))
            elif dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + str(delta.get("thinking", ""))
            elif dtype == "signature_delta":
                block["signature"] = block.get("signature", "") + str(delta.get("signature", ""))
        elif kind == "content_block_stop":
            block = self.blocks.get(int(data.get("index", -1)))
            if block is not None and block.get("type") == "tool_use":
                raw = "".join(block["_json"])
                if raw.strip():
                    try:
                        block["input"] = json.loads(raw)
                    except ValueError as exc:
                        block["input"] = None
                        block["_input_error"] = str(exc)[:120]
        elif kind == "message_delta":
            delta = data.get("delta") or {}
            self.stop_reason = delta.get("stop_reason") or self.stop_reason
            details = delta.get("stop_details")
            if isinstance(details, dict):
                self.stop_details = details
            self._merge_usage(data.get("usage"))
        elif kind == "message_stop":
            self.finished = True
        elif kind == "error":
            err = data.get("error") or {}
            etype = str(err.get("type") or "error")
            message = self._scrub(str(err.get("message") or ""))[:300]
            raise OperatorAPIError(
                f"anthropic: the stream carried an {etype} event: {message}",
                status=_ERROR_EVENT_STATUS.get(etype, 400),
                error_type=etype,
                request_id=self.request_id,
            )

    def partial_text(self) -> str:
        parts: list[str] = []
        for index in sorted(self.blocks):
            block = self.blocks[index]
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                parts.append("".join(block["_json"]))
        return "".join(parts)

    def outcome(self) -> TurnOutcome:
        stop = {
            "max_tokens": "max_tokens",
            "refusal": "refusal",
            "model_context_window_exceeded": "context",
            "pause_turn": "paused",
        }.get(self.stop_reason or "", "complete")
        detail = ""
        if self.stop_details:
            detail = ": ".join(
                str(self.stop_details[k]) for k in ("category", "explanation") if self.stop_details.get(k)
            )
        echo: list[dict[str, Any]] = []
        text_parts: list[str] = []
        ids: list[str] = []
        tool: dict[str, Any] | None = None
        for index in sorted(self.blocks):
            block = self.blocks[index]
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text", ""))
                text_parts.append(text)
                if text:
                    echo.append({"type": "text", "text": text})
            elif btype == "thinking":
                echo.append({
                    "type": "thinking",
                    "thinking": block.get("thinking", ""),
                    "signature": block.get("signature", ""),
                })
            elif btype == "redacted_thinking":
                echo.append({"type": "redacted_thinking", "data": block.get("data", "")})
            elif btype == "tool_use":
                ids.append(str(block.get("id", "")))
                echo.append({
                    "type": "tool_use",
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": block.get("input") if block.get("input") is not None else {},
                })
                if tool is None and block.get("name") == SCHEMA_TOOL_NAME:
                    tool = block
        out = TurnOutcome(
            model=self.model,
            stop=stop,
            detail=detail,
            text="".join(text_parts),
            input_tokens=self.usage.get("input_tokens"),
            output_tokens=self.usage.get("output_tokens"),
            cache_creation_tokens=self.usage.get("cache_creation_input_tokens"),
            cache_read_tokens=self.usage.get("cache_read_input_tokens"),
            assistant=echo,
            tool_call_ids=ids,
            request_id=self.request_id,
        )
        if tool is not None:
            out.has_tool_call = True
            out.tool_input_error = tool.get("_input_error")
            out.tool_input = tool.get("input") if "input" in tool else {}
        return out


class AnthropicBackend(ApiBackend):
    """Anthropic Messages API backend. *api_key* is the resolved key (the
    caller reads it with ``config.get_credential``); it is held privately,
    sent only in the ``x-api-key`` header and scrubbed from error text."""

    name: ClassVar[str] = "anthropic"

    def __init__(self, *, api_key: str | None, base_url: str = ANTHROPIC_API_URL, **kwargs: Any) -> None:
        super().__init__(base_url=base_url, api_key=api_key, **kwargs)

    def available(self) -> BackendStatus:
        if self._api_key:
            return BackendStatus("anthropic", True, "an anthropic_api_key is configured")
        return BackendStatus("anthropic", False, f"no anthropic_api_key: {_KEY_HINT}")

    def _check_ready(self) -> None:
        if not self._api_key:
            raise OperatorError(f"the anthropic backend has no API key: {_KEY_HINT}")

    def _url(self) -> str:
        return f"{self._base_url}/v1/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }

    def _resolve_model(self, req: DispatchRequest) -> str:
        return resolve_api_model(req.model)

    def _price(self, model: str) -> ModelPrice | None:
        return price_for(model)

    def _supports_forced(self, model: str) -> bool:
        return supports_forced_tool_choice(model)

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
        body: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "stream": True, "messages": messages}
        if tool_schema is not None:
            body["tools"] = [{
                "name": SCHEMA_TOOL_NAME,
                "description": SCHEMA_TOOL_DESCRIPTION,
                "input_schema": tool_schema,
            }]
            if forced:
                body["tool_choice"] = {"type": "tool", "name": SCHEMA_TOOL_NAME}
            else:
                body["tool_choice"] = {"type": "auto"}
                body["system"] = CALL_THE_TOOL_INSTRUCTION
        return body

    def _repair(self, messages: list[Any], outcome: TurnOutcome, feedback: str) -> list[Any]:
        repaired = list(messages)
        if outcome.assistant:
            repaired.append({"role": "assistant", "content": outcome.assistant})
        ids = outcome.tool_call_ids
        if ids:
            results: list[dict[str, Any]] = [{
                "type": "tool_result",
                "tool_use_id": ids[0],
                "is_error": True,
                "content": f"{feedback}. Call {SCHEMA_TOOL_NAME} again with a corrected input.",
            }]
            results += [
                {"type": "tool_result", "tool_use_id": i, "is_error": True, "content": "Not executed."}
                for i in ids[1:]
            ]
            repaired.append({"role": "user", "content": results})
        else:
            repaired.append({"role": "user", "content": f"{feedback}. {CALL_THE_TOOL_INSTRUCTION}"})
        return repaired

    def _new_stream(self) -> StreamState:
        return _AnthropicStream(self._scrub)

    def _http_error(self, status: int, headers: httpx.Headers, body: bytes) -> OperatorAPIError:
        etype: str | None = None
        message = ""
        request_id = headers.get("request-id")
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                etype = str(err.get("type")) if err.get("type") else None
                message = self._scrub(str(err.get("message") or ""))[:300]
            request_id = request_id or parsed.get("request_id")
        text = f"anthropic API error {status}"
        if etype:
            text += f" ({etype})"
        if message:
            text += f": {message}"
        if status in (401, 403):
            text += f" -- {_KEY_HINT}"
        if request_id:
            text += f" [request-id {request_id}]"
        should_retry = headers.get("x-should-retry")
        return OperatorAPIError(
            text,
            status=status,
            error_type=etype,
            request_id=request_id,
            retry_after=parse_retry_after(headers.get("retry-after")),
            retryable=False if (should_retry or "").strip().lower() == "false" else None,
        )

    def _forced_choice_rejected(self, exc: OperatorAPIError) -> bool:
        return exc.status == 400 and "tool_choice" in str(exc).lower()
