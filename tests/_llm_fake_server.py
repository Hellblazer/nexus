# SPDX-License-Identifier: AGPL-3.0-or-later
"""A scripted LLM HTTP server on 127.0.0.1:0 for the API-backend tests
(nexus-2k66t.3 P2). Stdlib only.

Each request consumes the next ``Step`` of the script and the server records
what it was sent, so a test asserts on the real request bytes and replays real
server-sent-event streams over a real socket (no mock of httpx or asyncio).
The streams are built from the documented event grammar by ``anthropic_stream``
and ``openai_stream`` below; the live check in ``tests/integration`` is what
compares that grammar against the real API.

A stalled step waits on the server's stop event, so ``stop()`` always releases
every handler thread: no test can leave a thread sleeping on a dead socket.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Step:
    """One scripted response."""

    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    #: SSE events as ``(event_name | None, data_string)``; sent for a 200.
    events: list[tuple[str | None, str]] = field(default_factory=list)
    #: A non-streaming body (an error JSON) for a non-200.
    body: bytes = b""
    #: Seconds to wait before sending the status line (a stall before any event).
    delay_before: float = 0.0
    #: Send this many events, then stall until the server stops.
    stall_after: int | None = None
    #: Send this many events, then close the connection (a cut stream).
    drop_after: int | None = None
    #: Seconds to hold the connection before the first event is sent (concurrency tests).
    hold: float = 0.0
    #: Seconds to wait between events (a slow but steady stream).
    event_delay: float = 0.0


@dataclass
class Recorded:
    path: str
    headers: dict[str, str]
    body: dict[str, Any]
    raw: bytes


class _Handler(BaseHTTPRequestHandler):
    server: "FakeLLMServer"  # type: ignore[assignment]

    def log_message(self, *_a: Any) -> None:  # silence the stderr access log
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        srv = self.server
        with srv.lock:
            srv.requests.append(Recorded(self.path, {k.lower(): v for k, v in self.headers.items()}, body, raw))
            srv.inflight += 1
            srv.max_inflight = max(srv.max_inflight, srv.inflight)
            if not srv.script:
                step = Step(status=500, body=b'{"type":"error","error":{"type":"api_error","message":"script exhausted"}}')
            else:
                step = srv.script.pop(0)
        try:
            self._serve(step)
        finally:
            with srv.lock:
                srv.inflight -= 1

    def _serve(self, step: Step) -> None:
        stop = self.server.stop_event
        if step.delay_before and stop.wait(step.delay_before):
            return
        self.send_response(step.status)
        for k, v in step.headers.items():
            self.send_header(k, v)
        if step.status != 200:
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(step.body)))
            self.end_headers()
            self.wfile.write(step.body)
            return
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        self.wfile.flush()
        if step.hold and stop.wait(step.hold):
            return
        for n, (name, data) in enumerate(step.events):
            if step.stall_after is not None and n >= step.stall_after:
                stop.wait(60)
                return
            if step.drop_after is not None and n >= step.drop_after:
                self.wfile.flush()
                self.close_connection = True
                try:
                    self.connection.shutdown(2)
                except OSError:
                    pass
                return
            if n and step.event_delay and stop.wait(step.event_delay):
                return
            chunk = (f"event: {name}\n" if name else "") + f"data: {data}\n\n"
            try:
                self.wfile.write(chunk.encode())
                self.wfile.flush()
            except OSError:
                return
        if step.stall_after is not None and step.stall_after >= len(step.events):
            stop.wait(60)


class FakeLLMServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, script: list[Step] | None = None) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.script: list[Step] = list(script or [])
        self.requests: list[Recorded] = []
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.inflight = 0
        self.max_inflight = 0
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def __enter__(self) -> "FakeLLMServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop_event.set()
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=5)

    def add(self, *steps: Step) -> None:
        with self.lock:
            self.script.extend(steps)


# ── stream builders ─────────────────────────────────────────────────────────


def _ev(name: str, payload: dict[str, Any]) -> tuple[str, str]:
    return name, json.dumps(payload)


def anthropic_stream(
    *,
    tool_input: Any = None,
    tool_json_chunks: list[str] | None = None,
    text: str | None = None,
    thinking: tuple[str, str] | None = None,
    model: str = "claude-haiku-5-5",
    stop_reason: str | None = None,
    stop_details: dict[str, Any] | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation: int = 0,
    cache_read: int = 0,
    tool_id: str = "toolu_01",
) -> list[tuple[str | None, str]]:
    """A Messages API stream: optional thinking block, optional text block,
    optional ``StructuredOutput`` tool_use block (its input streamed as
    ``input_json_delta`` chunks). ``message_delta.usage`` is cumulative, as the
    API sends it."""
    events: list[tuple[str | None, str]] = [
        _ev("message_start", {"type": "message_start", "message": {
            "id": "msg_01", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "cache_creation_input_tokens": cache_creation,
                      "cache_read_input_tokens": cache_read, "output_tokens": 1},
        }}),
        _ev("ping", {"type": "ping"}),
    ]
    index = 0
    if thinking is not None:
        events += [
            _ev("content_block_start", {"type": "content_block_start", "index": index,
                                        "content_block": {"type": "thinking", "thinking": ""}}),
            _ev("content_block_delta", {"type": "content_block_delta", "index": index,
                                        "delta": {"type": "thinking_delta", "thinking": thinking[0]}}),
            _ev("content_block_delta", {"type": "content_block_delta", "index": index,
                                        "delta": {"type": "signature_delta", "signature": thinking[1]}}),
            _ev("content_block_stop", {"type": "content_block_stop", "index": index}),
        ]
        index += 1
    if text is not None:
        events += [
            _ev("content_block_start", {"type": "content_block_start", "index": index,
                                        "content_block": {"type": "text", "text": ""}}),
            _ev("content_block_delta", {"type": "content_block_delta", "index": index,
                                        "delta": {"type": "text_delta", "text": text}}),
            _ev("content_block_stop", {"type": "content_block_stop", "index": index}),
        ]
        index += 1
    if tool_input is not None or tool_json_chunks is not None:
        chunks = tool_json_chunks if tool_json_chunks is not None else _split(json.dumps(tool_input))
        events.append(_ev("content_block_start", {"type": "content_block_start", "index": index, "content_block": {
            "type": "tool_use", "id": tool_id, "name": "StructuredOutput", "input": {}}}))
        events.append(_ev("content_block_delta", {"type": "content_block_delta", "index": index,
                                                  "delta": {"type": "input_json_delta", "partial_json": ""}}))
        for chunk in chunks:
            events.append(_ev("content_block_delta", {"type": "content_block_delta", "index": index,
                                                      "delta": {"type": "input_json_delta", "partial_json": chunk}}))
        events.append(_ev("content_block_stop", {"type": "content_block_stop", "index": index}))
        stop_reason = stop_reason or "tool_use"
    delta: dict[str, Any] = {"stop_reason": stop_reason or "end_turn", "stop_sequence": None}
    if stop_details is not None:
        delta["stop_details"] = stop_details
    events.append(_ev("message_delta", {"type": "message_delta", "delta": delta, "usage": {
        "input_tokens": input_tokens, "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read, "output_tokens": output_tokens}}))
    events.append(_ev("message_stop", {"type": "message_stop"}))
    return events


def _split(text: str, n: int = 3) -> list[str]:
    """*text* in *n* roughly equal pieces, so a test sees a real multi-delta stream."""
    size = max(1, -(-len(text) // n))
    return [text[i:i + size] for i in range(0, len(text), size)]


def anthropic_error(status: int, etype: str, message: str, *, headers: dict[str, str] | None = None) -> Step:
    body = json.dumps({"type": "error", "error": {"type": etype, "message": message}, "request_id": "req_test"})
    return Step(status=status, body=body.encode(), headers={"request-id": "req_test", **(headers or {})})


def openai_stream(
    *,
    tool_input: Any = None,
    tool_arg_chunks: list[str] | None = None,
    content: str | None = None,
    finish_reason: str = "stop",
    model: str = "llama3.1",
    prompt_tokens: int | None = 40,
    completion_tokens: int | None = 20,
    call_id: str = "call_1",
    name: str = "StructuredOutput",
    refusal: str | None = None,
) -> list[tuple[str | None, str]]:
    """A Chat Completions stream: content deltas and/or one function tool call
    whose arguments stream in chunks, a finish chunk, then (when counts are given)
    the ``include_usage`` chunk with empty ``choices``, then ``[DONE]``."""
    def chunk(delta: dict[str, Any], finish: str | None = None) -> tuple[None, str]:
        return None, json.dumps({"id": "chatcmpl-1", "object": "chat.completion.chunk", "model": model,
                                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

    events: list[tuple[str | None, str]] = [chunk({"role": "assistant", "content": ""})]
    if content:
        events.append(chunk({"content": content}))
    if refusal:
        events.append(chunk({"refusal": refusal}))
    if tool_input is not None or tool_arg_chunks is not None:
        pieces = tool_arg_chunks if tool_arg_chunks is not None else _split(json.dumps(tool_input))
        events.append(chunk({"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                             "function": {"name": name, "arguments": ""}}]}))
        for piece in pieces:
            events.append(chunk({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]}))
        finish_reason = "tool_calls" if finish_reason == "stop" else finish_reason
    events.append(chunk({}, finish_reason))
    if prompt_tokens is not None:
        events.append((None, json.dumps({"id": "chatcmpl-1", "model": model, "choices": [], "usage": {
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + (completion_tokens or 0)}})))
    events.append((None, "[DONE]"))
    return events


def openai_error(status: int, message: str, *, headers: dict[str, str] | None = None, etype: str = "invalid_request_error") -> Step:
    body = json.dumps({"error": {"message": message, "type": etype, "code": None}})
    return Step(status=status, body=body.encode(), headers=headers or {})


def wait_until(predicate: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False
