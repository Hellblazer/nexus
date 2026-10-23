# SPDX-License-Identifier: AGPL-3.0-or-later
"""Types shared by every LLM backend: the operator exceptions, ``DispatchUsage``,
the ambient usage sink and the one usage-recording helper (nexus-2k66t.3 P2).

This module sits BELOW ``nexus.operators.dispatch`` (the ``cli`` backend and the
``claude_dispatch`` front) and below ``llm_backend`` and the API backends, and
imports nothing from ``nexus``: it was lifted out of ``dispatch.py`` so the
backends can raise these exceptions and fill these records without importing
``dispatch`` (which imports them). ``dispatch`` re-exports every name, so
``from nexus.operators.dispatch import OperatorError`` and the patches tests
make on ``dispatch`` globals keep working.
"""
from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger("nexus.operators.dispatch")

#: RDR-196 .p1b Gap 1 (nexus-nyry9.8 coordinator addendum, 2026-08-20): an
#: AMBIENT usage sink, parallel to ``_ROLLED_UP`` above. The explicit
#: ``usage_sink`` kwarg on ``claude_dispatch`` only helps a caller that
#: invokes ``claude_dispatch`` directly (``plans/bundle.py``'s
#: ``dispatch_bundle`` is the only one inside this package) — it does
#: NOT reach a caller further up the stack, such as ``plans/runner.py``'s
#: isolated-step dispatch, which goes through the ``ToolDispatcher``
#: protocol into one of 10 separate ``operator_*`` MCP-tool functions in
#: ``mcp/core.py`` (extract/rank/compare/summarize/generate/filter/
#: check/verify/groupby/aggregate), each of which owns ITS OWN internal
#: ``claude_dispatch`` call. Threading an explicit ``usage_sink`` kwarg
#: through all 10 signatures would be a much larger, separately-scoped
#: change; the ambient sink closes that gap without touching any of
#: them. ``None`` (the default) is a complete no-op — byte-identical to
#: every dispatch before this addendum. asyncio Tasks copy the current
#: ``contextvars.Context`` at creation time, so a sink set here survives
#: any ``asyncio.create_task`` an operator issues internally (proven by
#: ``test_ambient_usage_sink_survives_child_task`` in
#: tests/test_operator_dispatch.py) — no special handling needed.
_ambient_usage_sink: contextvars.ContextVar[list[DispatchUsage] | None] = (
    contextvars.ContextVar("dispatch_ambient_usage_sink", default=None)
)


@contextmanager
def ambient_usage_sink(sink: list[DispatchUsage]) -> Iterator[None]:
    """Scope *sink* as the ambient usage-capture target for every
    ``claude_dispatch`` call made within this context — IN ADDITION to
    whatever explicit ``usage_sink`` (if any) an individual call also
    passes; both receive the SAME ``DispatchUsage`` instance (parsed
    once), never two independently-parsed copies.

    ``plans/runner.py`` wraps each isolated / bundle-fallback step
    dispatch in this context manager so a ``StepRecord`` can be built
    from real usage even though the dispatch reaches ``claude_dispatch``
    through an ``operator_*`` MCP-tool function this module never calls
    directly. Nested/overlapping scopes are NOT a supported use case —
    the inner scope's ``reset`` restores exactly the value the token
    captured, same discipline as ``rolled_up_dispatch_failures`` above.
    """
    token = _ambient_usage_sink.set(sink)
    try:
        yield
    finally:
        _ambient_usage_sink.reset(token)


class OperatorError(Exception):
    """Raised when claude -p exits non-zero."""


class OperatorTimeoutError(OperatorError):
    """Raised when claude -p exceeds the timeout.

    nexus-h33x8.6 critic (T2 substantive-critique-h33x8.6-a3-a1-2026-08-19,
    a4 precondition): carries the RECONSTRUCTED partial content as
    structured attributes, not merely baked into the message string --
    a4 (hard budget + partial results) needs to consume the partial
    programmatically, without re-parsing the message text or re-reading
    the timeout log file back off disk. All three default to empty/None
    so existing bare ``OperatorTimeoutError("message")`` construction
    (test doubles, callers that synthesize a timeout without going
    through the real dispatch path) keeps working unchanged.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_text: str = "",
        event_count: int = 0,
        log_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        #: Assistant/structured-output text reconstructed from whatever
        #: stream-json NDJSON events arrived before the kill (see
        #: ``_parse_stream_json_output``). Empty when nothing parsed.
        self.partial_text = partial_text
        #: Number of NDJSON lines that parsed as JSON before the kill.
        self.event_count = event_count
        #: Path to the persisted timeout log, or ``None`` when the log
        #: write itself failed (``_persist_timeout_log``'s own best-effort
        #: fallback) or this exception was constructed outside the real
        #: timeout path.
        self.log_path = log_path


class OperatorBudgetExceededError(OperatorError):
    """Raised when a per-dispatch ``--max-budget-usd`` ceiling aborts
    ``claude -p`` mid-turn (nexus-2g8y7).

    Only ever raised when the caller (or ``NX_DISPATCH_MAX_BUDGET_USD``)
    actually set a ceiling for THIS dispatch -- opt-in follows the flag,
    never fires for a dispatch that never asked for one. Detection is
    keyword-based on the captured stdout/stderr (see
    ``_looks_like_budget_exceeded_signal``): the CLI docs do not define a
    distinct wire signal for a budget abort beyond a bare non-zero exit,
    and the ambient monthly spend limit currently blocks every
    ``claude -p`` invocation in dev environments, so the exact shape
    could not be empirically probed before this bead shipped (see
    ``scripts/probe_budget_abort.sh``).

    Carries the same partial-output fields as ``OperatorTimeoutError``
    for the same reason: a budget abort, like a timeout kill, loses
    whatever tail the subprocess had not yet flushed, so the caller
    needs the reconstructed partial content programmatically rather
    than re-parsing the message text or re-reading the log file.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_text: str = "",
        event_count: int = 0,
        log_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.partial_text = partial_text
        self.event_count = event_count
        self.log_path = log_path


class OperatorOutputError(OperatorError):
    """Raised when stdout cannot be parsed as JSON."""



class OperatorAPIError(OperatorError):
    """A direct-API backend (``anthropic``, ``openai_compat``) got an HTTP error
    or lost the connection (nexus-2k66t.3 P2).

    ``status`` is the HTTP status, ``None`` for a connection failure.
    ``error_type`` is the provider's ``error.type`` when the body had one.
    ``request_id`` is the provider's request id when sent. The message is built
    from these and the provider's own error message, never from request
    headers, so it cannot carry the API key.

    :attr:`transient` is the one place that says which failures a retry can
    cure (429, 529, 5xx, a lost connection): the backend's own transport retry
    reads it, and the aspect extractor's retry loop reads it to decide between
    its transient and hard failure classes.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error_type: str | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.request_id = request_id
        #: Seconds the provider asked us to wait (``retry-after``), if it did.
        self.retry_after = retry_after
        self._retryable = retryable

    @property
    def transient(self) -> bool:
        """A retry might cure it. *retryable* (the provider's own
        ``x-should-retry``) overrides the status rule when it was sent."""
        if self._retryable is not None:
            return self._retryable
        s = self.status
        return s is None or s in (429, 529) or s >= 500


@dataclass(frozen=True)
class ModelUsage:
    """Per-model usage/cost breakdown from a stream-json result event's
    ``modelUsage`` map (RDR-196 196-R1). The wire payload's field names
    are camelCase (``inputTokens``, ``costUSD``, ``canonicalModel``, ...);
    attributes here are the snake_case translation.
    """

    canonical_model: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    cost_usd: float | None


@dataclass(frozen=True)
class DispatchUsage:
    """Cost/usage telemetry parsed from a ``claude -p --output-format
    stream-json`` terminal result event (RDR-196 Gap 1 / 196-R1,
    nexus-nyry9.7).

    Field names are FIXTURE-VERIFIED against
    ``tests/fixtures/claude_dispatch_stream_json_sample.ndjson`` -- not
    the RDR-196 Technical Design section's illustrative sketch, which
    used ``elapsed_ms`` for what the wire payload actually calls
    ``duration_ms``. Correction recorded in
    ``docs/rdr/rdr-196-cost-aware-nx-answer.md``'s Technical Design section
    (2026-08-20, nexus-nyry9.7). A field is
    ``None`` -- never ``0.0`` -- when the result event does not carry it:
    ``0.0`` reads as "this call was free", which is exactly the
    measurement bug RDR-196 exists to fix (see ``_parse_dispatch_usage``).
    """

    model: str | None
    """Canonical model id. Taken from the sole ``modelUsage`` entry's
    ``canonicalModel`` field when there is exactly one; when there are
    two or more, taken from the entry with the HIGHEST ``costUSD`` (ties
    broken by the highest ``outputTokens``, then by the first key in
    ``modelUsage``'s own iteration order) -- see ``_parse_dispatch_
    usage``. ``None`` when ``modelUsage`` is absent/empty, OR when it is
    non-empty but every entry in it fails the dict-shape check (a
    malformed payload; nothing survives to rank -- code-review-expert
    finding on the nexus-xepsr fix round, which corrected an earlier
    version of this code that called ``max()`` on an empty dict there and
    raised, breaking this module's documented never-raise-on-a-malformed-
    payload contract).

    An earlier rule -- pinned only in this dataclass's own docstring and
    its accompanying unit test, never written into RDR-196's body text --
    said: leave this ``None`` whenever ``modelUsage`` carried more than
    one entry, never make a silent pick. That rule is REVERSED here per
    Sam's decision (nexus-xepsr, 2026-09-14; recorded in RDR-196's
    Technical Design section as a CORRECTION block dated 2026-09-14,
    placed beside the 2026-08-20/nexus-nyry9.7 block that first pinned
    this field's wire semantics -- NOT "196-R3", which is the RDR's
    ``--strict-mcp-config`` spike and citing it for this rule was itself a
    mistake in an earlier version of this docstring). Every ``claude -p``
    dispatch now reports a second, near-zero-cost ``modelUsage`` entry for
    a side call the CLI makes itself (measured: claude-haiku-4-5 alongside
    the answering model, ~$0.001), so ">1 entry" stopped meaning
    "ambiguous bundle" and started meaning "the common case". The
    answering call's own cost is ordinarily far higher -- it carries the
    CLI's cached system prompt, measured at ~7,000 cache-creation
    tokens -- so it reliably outranks the side call, EXCEPT when the
    answering model is itself haiku-priced; in that case both entries
    already belong to the requested family, so the nexus-ek8tr drift
    tripwire below reaches the same (no-drift) verdict regardless of which
    one wins the pick. Leaving ``model`` permanently ``None`` broke the
    live-usage cost test (``TestClaudeDispatchLiveUsage::test_live_
    dispatch_records_nonzero_cost``) and made that tripwire vacuous (an
    absent canonical id trivially "matches" any request, per
    ``model_family_matches``'s own vacuous-true rule). Callers needing
    full per-model detail still read ``model_usage`` directly, which this
    reversal leaves untouched -- every entry survives."""

    cost_usd: float | None  # total_cost_usd
    input_tokens: int | None  # usage.input_tokens
    output_tokens: int | None  # usage.output_tokens
    cache_creation_input_tokens: int | None  # usage.cache_creation_input_tokens
    cache_read_input_tokens: int | None  # usage.cache_read_input_tokens
    duration_ms: int | None
    duration_api_ms: int | None
    num_turns: int | None
    model_usage: dict[str, ModelUsage] = field(default_factory=dict)
    """Keyed by the ``modelUsage`` map's own key(s) -- usually the
    canonical model id, but the key itself is not assumed canonical
    (``ModelUsage.canonical_model`` is the verified field)."""

    # ``frozen=True`` with the default ``eq=True`` would otherwise
    # auto-generate a ``__hash__`` that raises ``TypeError: unhashable
    # type: 'dict'`` lazily, only when something actually calls
    # ``hash()`` on an instance (``model_usage`` is a dict). No current
    # caller hashes a DispatchUsage, but leaving that landmine armed is
    # worse than declaring the type honestly unhashable up front.
    __hash__ = None  # type: ignore[assignment]



def model_family_matches(requested: str | None, canonical: str | None) -> bool:
    """nexus-ek8tr drift tripwire: does the RECORDED canonical id belong to
    the REQUESTED model's family? ``requested`` may be an alias ("fable",
    "haiku") or a full id ("claude-haiku-4-5"); the check is that some
    alphabetic token of the request appears in the canonical id. Vacuously
    true when either side is unknown (no basis to warn)."""
    if not requested or not canonical:
        return True
    tokens = [t for t in requested.lower().replace("claude-", "").split("-") if t.isalpha()]
    canon = canonical.lower()
    return any(t in canon for t in tokens) if tokens else True


def warn_on_family_drift(
    requested: str | None,
    usage: "DispatchUsage | None",
    operator: str | None,
    log: Any = None,
) -> None:
    """Emit the nexus-ek8tr drift warning when the recorded canonical id falls
    outside the requested model's family. Never raises. *log* defaults to this
    module's logger; ``dispatch._warn_on_family_drift`` passes its own so a
    test patching ``dispatch._log`` still sees the event."""
    canonical = usage.model if usage is not None else None
    if not model_family_matches(requested, canonical):
        (log or _log).warning(
            "model_family_drift",
            requested_model=requested,
            canonical_model=canonical,
            operator=operator,
        )


def empty_usage() -> DispatchUsage:
    """A ``DispatchUsage`` with every field unknown (``None``), for a call that
    reached a parse step but produced no usage payload."""
    return DispatchUsage(
        model=None, cost_usd=None, input_tokens=None, output_tokens=None,
        cache_creation_input_tokens=None, cache_read_input_tokens=None,
        duration_ms=None, duration_api_ms=None, num_turns=None,
    )


def record_usage(
    usage: DispatchUsage | None,
    *,
    usage_sink: "list[DispatchUsage] | None",
    requested_model: str | None,
    operator: str | None,
    absent: "Callable[[], DispatchUsage] | None" = None,
    warn_drift: "Callable[[str | None, DispatchUsage | None, str | None], None] | None" = None,
) -> DispatchUsage | None:
    """The one place a backend reports what a call cost (nexus-2k66t.3 P2).

    Every backend calls this once per call that reached a parsed result, BEFORE
    any post-parse raise, so a caller catching the exception can still read
    what the failed turn cost (the append-before-raise contract of
    ``claude_dispatch``'s ``usage_sink``). It runs the model-family drift check,
    then appends ONE ``DispatchUsage`` instance to *usage_sink* and to the
    ambient sink (:func:`ambient_usage_sink`) when either is active: both
    receive the same object, never two copies. Neither active is a no-op.

    *usage* may be ``None`` (the CLI found no result event). When a sink is
    active the recorded value is then ``absent()`` (the CLI passes its
    parser, which logs ``dispatch_usage_no_result_event``) or an all-``None``
    record. The return value is *usage* as given, so
    ``DispatchResult.usage`` stays ``None`` in that case.

    *warn_drift* defaults to :func:`warn_on_family_drift`; the CLI backend
    passes ``dispatch._warn_on_family_drift`` so patches on ``dispatch._log``
    keep seeing the event.
    """
    ambient = _ambient_usage_sink.get()
    if requested_model is not None and usage is not None:
        (warn_drift or warn_on_family_drift)(requested_model, usage, operator)
    if usage_sink is not None or ambient is not None:
        recorded = usage if usage is not None else (absent() if absent is not None else empty_usage())
        if usage_sink is not None:
            usage_sink.append(recorded)
        if ambient is not None:
            ambient.append(recorded)
    return usage
