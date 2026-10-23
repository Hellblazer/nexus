# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared machinery of the direct-API LLM backends (nexus-2k66t.3 P2).

``anthropic`` (``llm_anthropic``) and ``openai_compat`` (``llm_openai``) differ
in their request and event shapes and nothing else. Everything that is the same
lives here, once:

* the HTTP transport on ``httpx`` (a core dependency): one streaming POST per
  attempt, a server-sent-events reader, **two-phase timeouts** inside the
  caller's deadline (time to the first event, then time to the end of the
  stream), retries on 429 / 529 / 5xx / a lost connection with ``retry-after``
  honoured and capped by the deadline and jittered, seeded backoff;
* a per-process concurrency gate (``NX_LLM_MAX_CONCURRENCY``, default 4);
* the structured-output loop: the request's JSON schema is offered as one tool,
  the tool input is validated **locally** with ``jsonschema``, and a failure
  gets exactly one repair turn;
* the ``max_budget_usd`` pre-flight check, usage summing across turns, cost
  from the price table (``None`` plus a warning for an unpriced model, never
  ``0.0``), and one ``record_usage`` call per call.

The API key is never logged, never written to ``os.environ`` and never put in
an exception message: provider and transport error text passes through
:meth:`ApiBackend._scrub`, and no request header or body is logged.

Design of record: T2 ``nexus/design-direct-llm-provider-2026-10-23``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import random
import re
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx
import structlog

from nexus.operators.llm_backend import BackendStatus, DispatchRequest, DispatchResult
from nexus.operators.llm_models import ModelPrice
from nexus.operators.llm_types import (
    DispatchUsage,
    ModelUsage,
    OperatorAPIError,
    OperatorBudgetExceededError,
    OperatorError,
    OperatorOutputError,
    OperatorTimeoutError,
    record_usage,
)

_log = structlog.get_logger(__name__)

NX_LLM_MAX_CONCURRENCY_ENV: str = "NX_LLM_MAX_CONCURRENCY"
NX_LLM_MAX_TOKENS_ENV: str = "NX_LLM_MAX_TOKENS"
DEFAULT_MAX_CONCURRENCY: int = 4
DEFAULT_MAX_TOKENS: int = 16000
#: Retries after the first attempt, for 429 / 529 / 5xx / connection failures.
DEFAULT_TRANSPORT_RETRIES: int = 2
#: A ``retry-after`` longer than this is not waited out even with no deadline.
MAX_RETRY_AFTER_SECONDS: float = 60.0
_BACKOFF_BASE_SECONDS: float = 1.0
_BACKOFF_CAP_SECONDS: float = 30.0
_JITTER_FRACTION: float = 0.25
#: Bound on the cleanup after a timeout cancels the stream task.
_CANCEL_GRACE_SECONDS: float = 5.0
#: Largest error body read from a failed response.
_ERROR_BODY_CAP: int = 65536
#: Conservative characters-per-token for the pre-flight input estimate: the
#: newer tokenizers produce about 30% more tokens than the older ~4 chars/token.
_CHARS_PER_TOKEN: float = 3.0

#: Name of the one tool that carries the structured answer.
SCHEMA_TOOL_NAME: str = "StructuredOutput"
SCHEMA_TOOL_DESCRIPTION: str = (
    "Return the final answer. Call this tool exactly once, with an input that "
    "conforms to the schema."
)
CALL_THE_TOOL_INSTRUCTION: str = (
    f"Respond by calling the {SCHEMA_TOOL_NAME} tool exactly once with your "
    "complete answer. Do not reply in plain text."
)

#: Ceiling on the validator text sent back in the repair turn.
_FEEDBACK_CAP: int = 600


# ── configuration ───────────────────────────────────────────────────────────


def configured_max_tokens() -> int:
    """``NX_LLM_MAX_TOKENS`` (default 16000). A value that is not a positive
    integer fails loud, as ``NX_DISPATCH_MAX_BUDGET_USD`` does."""
    raw = os.environ.get(NX_LLM_MAX_TOKENS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_TOKENS
    try:
        n = int(raw)
    except ValueError:
        n = 0
    if n < 1:
        raise ValueError(f"{NX_LLM_MAX_TOKENS_ENV}={raw!r} is not a positive integer -- unset it or fix the value")
    return n


def configured_concurrency() -> int:
    raw = os.environ.get(NX_LLM_MAX_CONCURRENCY_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_CONCURRENCY
    try:
        n = int(raw)
    except ValueError:
        n = 0
    if n < 1:
        raise ValueError(
            f"{NX_LLM_MAX_CONCURRENCY_ENV}={raw!r} is not a positive integer -- unset it or fix the value"
        )
    return n


# ── per-process concurrency gate ────────────────────────────────────────────

# A threading semaphore, not an asyncio one: the aspect extractor's sync entry
# runs each call on a fresh event loop, and an asyncio.Semaphore binds to the
# loop that first contends on it. "Per process" has to hold across loops.
_GATE_LOCK = threading.Lock()
_GATE: tuple[int, threading.Semaphore] | None = None


def _semaphore() -> threading.Semaphore:
    global _GATE
    limit = configured_concurrency()
    with _GATE_LOCK:
        if _GATE is None or _GATE[0] != limit:
            _GATE = (limit, threading.Semaphore(limit))
        return _GATE[1]


@contextlib.asynccontextmanager
async def concurrency_permit() -> AsyncIterator[None]:
    """Hold one of the process's ``NX_LLM_MAX_CONCURRENCY`` slots for the
    duration of one HTTP attempt (not across a retry sleep)."""
    sem = _semaphore()
    wait = 0.005
    while not sem.acquire(blocking=False):
        await asyncio.sleep(wait)
        wait = min(wait * 2, 0.05)
    try:
        yield
    finally:
        sem.release()


# ── server-sent events ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class SSEEvent:
    event: str | None
    data: str


async def iter_sse(lines: AsyncIterator[str]) -> AsyncIterator[SSEEvent]:
    """Group SSE lines into events. Comment lines (``:``) are skipped, several
    ``data:`` lines join with newlines, and an unterminated final event at EOF
    is still delivered."""
    event: str | None = None
    data: list[str] = []
    async for line in lines:
        if line == "":
            if data:
                yield SSEEvent(event, "\n".join(data))
            event, data = None, []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
    if data:
        yield SSEEvent(event, "\n".join(data))


# ── retry arithmetic ────────────────────────────────────────────────────────


def parse_retry_after(value: str | None, *, now: Callable[[], datetime] | None = None) -> float | None:
    """Seconds from a ``retry-after`` header (delta-seconds or an HTTP date), or
    ``None`` when absent or unparseable. Never negative."""
    if not value:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - (now or (lambda: datetime.now(timezone.utc)))()).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def backoff_delay(attempt: int, rng: random.Random) -> float:
    """Exponential backoff for the *attempt*-th retry (0-based), +-25% jitter
    from *rng* (seeded in tests)."""
    base = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** attempt))
    return base * (1.0 + rng.uniform(-_JITTER_FRACTION, _JITTER_FRACTION))


class StreamTimeout(Exception):
    """A phase of the stream ran out of time. *phase* is ``first_event`` or
    ``stream``. Internal: the backend turns it into ``OperatorTimeoutError``
    carrying the partial output."""

    def __init__(self, phase: str) -> None:
        super().__init__(phase)
        self.phase = phase


# ── per-turn result and stream-state protocol ───────────────────────────────


@dataclass
class TurnOutcome:
    """One API round trip, normalised across providers."""

    model: str | None
    #: ``complete`` | ``max_tokens`` | ``refusal`` | ``context`` | ``paused``
    stop: str
    detail: str = ""
    #: Structured tool call: ``has_tool_call`` says the model made one;
    #: ``tool_input`` is its parsed input; ``tool_input_error`` is set instead
    #: when the argument JSON would not parse.
    has_tool_call: bool = False
    tool_input: Any = None
    tool_input_error: str | None = None
    tool_call_ids: list[str] = field(default_factory=list)
    text: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    #: Provider-native assistant message to echo back in a repair turn.
    assistant: Any = None
    request_id: str | None = None
    api_ms: int = 0


class StreamState:
    """Per-attempt accumulator a provider implements. ``feed`` takes events in
    order; ``reset`` clears everything for a retry; ``partial_text`` is what a
    timeout log records; ``finished`` is true once the provider's terminal event
    (``message_stop`` / ``[DONE]``) was seen."""

    request_id: str | None = None
    event_count: int = 0
    finished: bool = False

    def feed(self, ev: SSEEvent) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def reset(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def partial_text(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def outcome(self) -> TurnOutcome:  # pragma: no cover - interface
        raise NotImplementedError


# ── budget ──────────────────────────────────────────────────────────────────


class _Budget:
    """The ``max_budget_usd`` pre-flight ceiling across the turns of one call."""

    def __init__(self, ceiling: float | None, price: ModelPrice | None, *, enforceable: bool) -> None:
        self.ceiling = ceiling
        self.price = price
        self.enforceable = enforceable
        self.spent = 0.0

    def clamp(self, max_tokens: int, request_chars: int, operator: str | None) -> tuple[int, bool]:
        """Return ``(max_tokens, clamped)`` for the next turn, or raise
        ``OperatorBudgetExceededError`` when even the input does not fit."""
        if self.ceiling is None or not self.enforceable or self.price is None:
            return max_tokens, False
        est_in = math.ceil(request_chars / _CHARS_PER_TOKEN)
        row = self.price.for_prompt(est_in)
        in_cost = est_in * row.input / 1_000_000
        remaining = self.ceiling - self.spent
        if in_cost >= remaining:
            raise OperatorBudgetExceededError(
                f"max_budget_usd {self.ceiling:f} would be exceeded before the model answers: the input alone "
                f"is estimated at ${in_cost:.6f} (~{est_in} tokens) with ${max(remaining, 0.0):.6f} left"
                f"{_for_operator(operator)}"
            )
        affordable = int((remaining - in_cost) / row.output * 1_000_000)
        if affordable < 1:
            raise OperatorBudgetExceededError(
                f"max_budget_usd {self.ceiling:f} leaves no room for output after an estimated "
                f"${in_cost:.6f} of input{_for_operator(operator)}"
            )
        if affordable < max_tokens:
            return affordable, True
        return max_tokens, False


def _for_operator(operator: str | None) -> str:
    return f" [operator={operator!r}]" if operator else ""


# ── the backend base class ──────────────────────────────────────────────────

_WARNED_UNPRICED: set[str] = set()


class ApiBackend:
    """Base of the API backends. Subclasses supply the provider's request
    shape (:meth:`_body`, :meth:`_repair`), event parsing (:meth:`_new_stream`)
    and error parsing (:meth:`_http_error`); this class runs the call."""

    name: ClassVar[str] = "api"
    #: Whether a ``max_budget_usd`` ceiling can be enforced (needs a price).
    budget_enforceable_without_price: ClassVar[bool] = False
    #: Whether the model-family drift tripwire (requested alias vs the model the
    #: provider reports) means anything: it does for Anthropic aliases, not for
    #: a local model that ignores them.
    drift_check: ClassVar[bool] = True

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rng: random.Random | None = None,
        trust_env: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or None
        self._sleep = sleep or asyncio.sleep
        self._rng = rng or random.Random()
        self._trust_env = trust_env

    # -- provider hooks ----------------------------------------------------

    def available(self) -> BackendStatus:  # pragma: no cover - interface
        raise NotImplementedError

    def _check_ready(self) -> None:
        """Raise ``OperatorError`` when the backend lacks what a call needs
        (a key, a base URL, a model)."""

    def _url(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:  # pragma: no cover - interface
        raise NotImplementedError

    def _resolve_model(self, req: DispatchRequest) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def _price(self, model: str) -> ModelPrice | None:
        return None

    def _supports_forced(self, model: str) -> bool:
        return True

    def _initial_messages(self, req: DispatchRequest, *, auto_mode: bool) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def _body(
        self,
        messages: list[Any],
        *,
        model: str,
        max_tokens: int,
        tool_schema: dict[str, Any] | None,
        forced: bool,
    ) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    def _repair(self, messages: list[Any], outcome: TurnOutcome, feedback: str) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def _new_stream(self) -> StreamState:  # pragma: no cover - interface
        raise NotImplementedError

    def _http_error(self, status: int, headers: httpx.Headers, body: bytes) -> OperatorAPIError:  # pragma: no cover
        raise NotImplementedError

    def _forced_choice_rejected(self, exc: OperatorAPIError) -> bool:
        return False

    def _salvage_text(self, outcome: TurnOutcome) -> Any:
        """A provider hook: a structured value found in plain text when the
        model made no tool call (``NO_VALUE`` when none)."""
        return NO_VALUE

    # -- secrets -----------------------------------------------------------

    def _scrub(self, text: str) -> str:
        """*text* with the API key removed. Applied to every piece of
        third-party text (transport exceptions, provider error messages) that
        reaches a log line or an exception."""
        if self._api_key:
            text = text.replace(self._api_key, "[REDACTED]")
        return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)

    # -- the call ------------------------------------------------------------

    async def run(self, req: DispatchRequest) -> DispatchResult:
        started = time.monotonic()
        self._check_ready()
        model = self._resolve_model(req)
        price = self._price(model)
        if price is None:
            self._warn_unpriced(model)
        if req.max_budget_usd is not None and price is None and not self.budget_enforceable_without_price:
            raise OperatorError(
                f"cannot enforce max_budget_usd={req.max_budget_usd}: no price is known for model {model!r} "
                f"(add it to llm_models.PRICES, or unset the ceiling)"
            )
        if req.max_budget_usd is not None and price is None:
            _log.warning("llm_budget_not_enforceable", backend=self.name, model=model)
        schema = req.json_schema
        wrap = schema is not None and schema.get("type") != "object"
        tool_schema: dict[str, Any] | None = None
        if schema is not None:
            tool_schema = (
                {"type": "object", "properties": {"result": schema}, "required": ["result"]} if wrap else schema
            )
        validator = _build_validator(schema)
        budget = _Budget(req.max_budget_usd, price, enforceable=price is not None)
        max_tokens_cfg = configured_max_tokens()
        forced = schema is not None and self._supports_forced(model)
        messages = self._initial_messages(req, auto_mode=schema is not None and not forced)
        turns: list[TurnOutcome] = []
        settled: list[DispatchUsage | None] = []

        def settle() -> DispatchUsage | None:
            if settled:
                return settled[0]
            usage = self._usage(turns, price, started) if turns else None
            if usage is not None:
                record_usage(
                    usage,
                    usage_sink=req.usage_sink,
                    requested_model=req.model if self.drift_check else None,
                    operator=req.operator,
                )
            settled.append(usage)
            return usage

        try:
            for turn_no in (1, 2):
                chars = len(json.dumps(messages, default=str)) + len(json.dumps(tool_schema or {}))
                max_tokens, clamped = budget.clamp(max_tokens_cfg, chars, req.operator)
                outcome, forced = await self._turn(
                    req, model, messages, max_tokens, tool_schema, forced
                )
                turns.append(outcome)
                if price is not None:
                    budget.spent += _turn_cost(price, outcome) or 0.0

                if outcome.stop == "refusal":
                    settle()
                    raise OperatorError(
                        f"{self.name}: the model refused the request{(' (' + outcome.detail + ')') if outcome.detail else ''}"
                        f"{_for_operator(req.operator)}"
                    )
                if outcome.stop == "max_tokens":
                    settle()
                    note = (
                        f" (clamped from {max_tokens_cfg} by max_budget_usd={req.max_budget_usd})" if clamped
                        else f"; raise {NX_LLM_MAX_TOKENS_ENV} (now {max_tokens_cfg})"
                    )
                    raise OperatorOutputError(
                        f"{self.name}: the response was cut off at max_tokens={max_tokens}{note}"
                        f"{_for_operator(req.operator)}"
                    )
                if outcome.stop in ("context", "paused"):
                    settle()
                    raise OperatorOutputError(
                        f"{self.name}: the response stopped early ({outcome.stop}"
                        f"{': ' + outcome.detail if outcome.detail else ''}){_for_operator(req.operator)}"
                    )

                if schema is None:
                    text = outcome.text.strip()
                    if not text:
                        settle()
                        raise OperatorOutputError(f"{self.name}: the model returned no text{_for_operator(req.operator)}")
                    return DispatchResult(text=text, usage=settle())

                value, feedback = self._structured_value(outcome, schema, wrap, validator)
                if feedback is None:
                    return DispatchResult(structured=value, usage=settle())
                if turn_no == 1:
                    _log.info(
                        "llm_structured_output_repair",
                        backend=self.name, model=model, operator=req.operator, reason=feedback[:200],
                    )
                    messages = self._repair(messages, outcome, feedback)
                    continue
                settle()
                raise OperatorOutputError(
                    f"{self.name}: no schema-valid {SCHEMA_TOOL_NAME} after one repair turn: {feedback}"
                    f"{_for_operator(req.operator)}"
                )
            raise AssertionError("unreachable: the loop returns or raises on its last turn")  # pragma: no cover
        finally:
            # A turn that completed cost money even when a later one timed out
            # or failed: report it, once, before the exception leaves.
            settle()

    # -- structured value --------------------------------------------------

    def _structured_value(
        self,
        outcome: TurnOutcome,
        schema: dict[str, Any],
        wrap: bool,
        validator: Any,
    ) -> tuple[Any, str | None]:
        """``(value, None)`` for a schema-valid answer, else ``(None, feedback)``
        where *feedback* is the text the repair turn sends back."""
        if outcome.has_tool_call:
            if outcome.tool_input_error is not None:
                return None, f"the tool input was not valid JSON ({outcome.tool_input_error})"
            raw = outcome.tool_input
        else:
            raw = self._salvage_text(outcome)
            if raw is NO_VALUE:
                return None, f"you did not call the {SCHEMA_TOOL_NAME} tool"
        if wrap:
            if not isinstance(raw, dict) or "result" not in raw:
                return None, "the tool input must be an object with a 'result' field"
            raw = raw["result"]
        if validator is not None:
            err = _first_validation_error(validator, raw)
            if err is not None:
                return None, err
        return raw, None

    # -- one turn ------------------------------------------------------------

    async def _turn(
        self,
        req: DispatchRequest,
        model: str,
        messages: list[Any],
        max_tokens: int,
        tool_schema: dict[str, Any] | None,
        forced: bool,
    ) -> tuple[TurnOutcome, bool]:
        """One round trip. Returns the outcome and the ``forced`` setting that
        worked (a model that rejects a forced tool choice is retried once with
        ``auto``, and stays on it for the repair turn)."""
        for attempt_forced in ([forced, False] if forced else [False]):
            body = self._body(
                messages, model=model, max_tokens=max_tokens, tool_schema=tool_schema, forced=attempt_forced,
            )
            stream = self._new_stream()
            t0 = time.monotonic()
            try:
                await self._stream_with_retries(body, stream, req)
            except StreamTimeout as st:
                raise self._timeout_error(req, stream, st.phase) from None
            except OperatorAPIError as exc:
                if attempt_forced and self._forced_choice_rejected(exc):
                    _log.info("llm_forced_tool_choice_rejected", backend=self.name, model=model)
                    continue
                raise
            outcome = stream.outcome()
            outcome.api_ms = int((time.monotonic() - t0) * 1000)
            outcome.request_id = stream.request_id
            _log.debug(
                "llm_call",
                backend=self.name, model=outcome.model or model, operator=req.operator,
                request_id=stream.request_id, stop=outcome.stop,
                input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
            )
            return outcome, attempt_forced
        raise AssertionError("unreachable")  # pragma: no cover

    def _timeout_error(self, req: DispatchRequest, stream: StreamState, phase: str) -> OperatorTimeoutError:
        from nexus.operators.dispatch import _persist_timeout_log  # noqa: PLC0415 - dispatch imports llm_backend at load; deferred

        partial = stream.partial_text()
        n = stream.event_count
        log_path = _persist_timeout_log(req.timeout, partial, b"", n)
        where = "before the first event" if phase == "first_event" else "mid-stream"
        return OperatorTimeoutError(
            f"{self.name} API call timed out after {req.timeout}s {where}; partial output "
            f"({len(partial)} chars reconstructed from {n} stream events) logged to {log_path}",
            partial_text=partial,
            event_count=n,
            log_path=Path(log_path) if log_path != "(log write failed)" else None,
        )

    # -- transport -------------------------------------------------------------

    async def _stream_with_retries(self, body: dict[str, Any], stream: StreamState, req: DispatchRequest) -> None:
        retries = DEFAULT_TRANSPORT_RETRIES if req.transport_retries is None else max(0, req.transport_retries)
        attempt = 0
        while True:
            try:
                async with concurrency_permit():
                    await self._one_attempt(body, stream, req)
                return
            except OperatorAPIError as exc:
                if not exc.transient or attempt >= retries:
                    raise
                delay = self._retry_delay(exc, attempt)
                if req.deadline is not None and delay >= req.deadline - time.monotonic():
                    raise
                _log.info(
                    "llm_transport_retry",
                    backend=self.name, status=exc.status, error_type=exc.error_type,
                    attempt=attempt + 1, delay_s=round(delay, 3), request_id=exc.request_id,
                )
                stream.reset()
                await self._sleep(delay)
                attempt += 1

    def _retry_delay(self, exc: OperatorAPIError, attempt: int) -> float:
        if exc.retry_after is not None:
            return min(exc.retry_after, MAX_RETRY_AFTER_SECONDS)
        return backoff_delay(attempt, self._rng)

    async def _one_attempt(self, body: dict[str, Any], stream: StreamState, req: DispatchRequest) -> None:
        from nexus.operators.dispatch import _effective_phase_timeout  # noqa: PLC0415 - dispatch imports llm_backend at load; deferred

        first_event = asyncio.Event()
        url = self._url()
        headers = self._headers()

        async def consume() -> None:
            timeout = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=None)
            async with httpx.AsyncClient(timeout=timeout, trust_env=self._trust_env, follow_redirects=False) as client:
                try:
                    async with client.stream("POST", url, headers=headers, json=body) as resp:
                        if resp.status_code >= 400:
                            raw = b""
                            async for chunk in resp.aiter_bytes():
                                raw += chunk
                                if len(raw) >= _ERROR_BODY_CAP:
                                    break
                            raise self._http_error(resp.status_code, resp.headers, raw[:_ERROR_BODY_CAP])
                        stream.request_id = resp.headers.get("request-id") or resp.headers.get("x-request-id")
                        async for ev in iter_sse(resp.aiter_lines()):
                            first_event.set()
                            stream.feed(ev)
                except httpx.HTTPError as exc:
                    raise OperatorAPIError(
                        f"{self.name}: connection failed ({type(exc).__name__}: {self._scrub(str(exc))[:200]})"
                    ) from None
                if not stream.finished:
                    raise OperatorAPIError(f"{self.name}: the stream ended before the response was complete")

        task = asyncio.ensure_future(consume())
        waiter = asyncio.ensure_future(first_event.wait())
        try:
            done, _ = await asyncio.wait(
                {task, waiter}, timeout=_effective_phase_timeout(req.timeout, req.deadline),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise StreamTimeout("first_event")
            if task not in done:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=_effective_phase_timeout(req.timeout, req.deadline))
                except asyncio.TimeoutError:
                    raise StreamTimeout("stream") from None
            task.result()
        finally:
            waiter.cancel()
            if not task.done():
                task.cancel()
            await asyncio.wait({task, waiter}, timeout=_CANCEL_GRACE_SECONDS)
            for t in (task, waiter):
                if t.done() and not t.cancelled():
                    t.exception()  # consumed: a cancelled attempt's error is not ours to raise

    # -- usage -------------------------------------------------------------------

    def _warn_unpriced(self, model: str) -> None:
        if model in _WARNED_UNPRICED:
            return
        _WARNED_UNPRICED.add(model)
        _log.warning("llm_price_unknown", backend=self.name, model=model, effect="cost_usd is None for this model")

    def _usage(self, turns: list[TurnOutcome], price: ModelPrice | None, started: float) -> DispatchUsage:
        def total(attr: str) -> int | None:
            vals = [getattr(t, attr) for t in turns if getattr(t, attr) is not None]
            return sum(vals) if vals else None

        costs = [_turn_cost(price, t) for t in turns] if price is not None else [None]
        cost = None if any(c is None for c in costs) else float(sum(costs))  # type: ignore[arg-type]
        model = next((t.model for t in reversed(turns) if t.model), None)
        in_t, out_t = total("input_tokens"), total("output_tokens")
        cc_t, cr_t = total("cache_creation_tokens"), total("cache_read_tokens")
        return DispatchUsage(
            model=model,
            cost_usd=cost,
            input_tokens=in_t,
            output_tokens=out_t,
            cache_creation_input_tokens=cc_t,
            cache_read_input_tokens=cr_t,
            duration_ms=int((time.monotonic() - started) * 1000),
            duration_api_ms=sum(t.api_ms for t in turns),
            num_turns=len(turns),
            model_usage=(
                {model: ModelUsage(model, in_t, out_t, cr_t, cc_t, cost)} if model else {}
            ),
        )


NO_VALUE = object()


def _turn_cost(price: ModelPrice | None, t: TurnOutcome) -> float | None:
    """Cost of one turn, ``None`` when the price is unknown or the provider did
    not report the token counts that cost needs."""
    if price is None or t.input_tokens is None or t.output_tokens is None:
        return None
    return price.cost(
        input_tokens=t.input_tokens,
        output_tokens=t.output_tokens,
        cache_creation_tokens=t.cache_creation_tokens or 0,
        cache_read_tokens=t.cache_read_tokens or 0,
    )


def _build_validator(schema: dict[str, Any] | None) -> Any:
    """A ``jsonschema`` validator for *schema*, or ``None`` when there is no
    schema or the schema is not itself valid (the provider then decides)."""
    if schema is None:
        return None
    import jsonschema  # noqa: PLC0415 - deferred: only structured calls on an API backend need it

    try:
        cls = jsonschema.validators.validator_for(schema)
        cls.check_schema(schema)
        return cls(schema)
    except jsonschema.SchemaError as exc:
        _log.warning("llm_schema_not_checkable", error=str(exc.message)[:200])
        return None


def _first_validation_error(validator: Any, instance: Any) -> str | None:
    import jsonschema  # noqa: PLC0415 - deferred, as above

    err = jsonschema.exceptions.best_match(validator.iter_errors(instance))
    if err is None:
        return None
    where = "/".join(str(p) for p in err.absolute_path) or "<root>"
    return f"{where}: {err.message}"[:_FEEDBACK_CAP]
