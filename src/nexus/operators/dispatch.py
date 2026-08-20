# SPDX-License-Identifier: AGPL-3.0-or-later
"""Async claude -p subprocess dispatch for operator tools.

Single responsibility: spawn `claude -p` as a truly async subprocess,
deliver a prompt via stdin, parse JSON output, surface typed errors.

No worker pool. No auth check. No session management.
claude -p inherits Claude Code auth; if it fails, the subprocess error
surfaces naturally.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import structlog

_log = structlog.get_logger()

#: nexus-l1qpj: fail-open BATCH callers (taxonomy discover/review) roll
#: dispatch failures up into one summary line themselves — inside their
#: scope the per-failure ``operator_dispatch_failed`` event demotes from
#: WARNING to INFO so a bad run does not wall the terminal with one
#: WARNING per failed batch. INFO still reaches any attached file handler
#: (``open_run_log`` unlocks INFO for its file while pinning stderr
#: quiet), so the per-failure record survives; only the stderr noise goes.
_ROLLED_UP: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "dispatch_failures_rolled_up", default=False,
)


@contextmanager
def rolled_up_dispatch_failures() -> Iterator[None]:
    """Demote per-failure dispatch WARNINGs to INFO within this scope.

    ONLY for callers that (a) are fail-open by contract AND (b) emit their
    own end-of-batch rollup summary naming the failure count and where the
    per-failure records went. Everyone else keeps the WARNING default —
    the durable-record posture of nexus-q6830 is the point of the choke
    point.
    """
    token = _ROLLED_UP.set(True)
    try:
        yield
    finally:
        _ROLLED_UP.reset(token)


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

#: Per-stream cap on what the failure log records, in characters.
#: Sized against the handler's real budget, not picked for feel: the log
#: rotates at 10 MB x 5 backups (:mod:`nexus.logging_setup`), so a 16 KB
#: two-stream worst case is ~625 max-size entries per rotation — room for
#: a pathological failure loop without letting it evict the retained
#: window. Deliberately NOT the exception message's 300-char cap, which is
#: sized for terminal readability: a JSON error payload with a stack
#: summary clears 300 easily, and a durable record that inherits a
#: readability cap loses the diagnostic all over again.
_LOG_STREAM_CAP: int = 8000

#: Appended when a stream is cut at the cap. Without it a field of exactly
#: _LOG_STREAM_CAP chars is indistinguishable from a diagnostic that
#: happened to be exactly that long — the same ambiguity the "no output on
#: stdout or stderr" sentinel exists to prevent, one field over.
_TRUNCATION_MARKER: str = "...[truncated]"

__all__ = [
    "claude_dispatch",
    "ambient_usage_sink",
    "rolled_up_dispatch_failures",
    "OperatorError",
    "OperatorOutputError",
    "OperatorTimeoutError",
]


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


class OperatorOutputError(OperatorError):
    """Raised when stdout cannot be parsed as JSON."""


def _build_dispatch_env(
    *,
    share_t1: bool = False,
    ephemeral: bool = False,
    parent_session_id: str | None = None,
    grants_tool_access: bool = False,
) -> dict[str, str]:
    """Build the env dict for a dispatched ``claude -p`` subprocess.

    RDR-105 P4 (nexus-jnx7). Three modes (RDR-155 P4b: the chroma T1
    server is retired — ``share_t1=True`` now raises, since there is no
    parent-owned chroma address to share; cross-process findings go to
    T2, and service-mode T1 sharing rides the session-lease mechanism):

    Shared T1 (``share_t1=True``)
        RETIRED. Raises ``RuntimeError`` unconditionally.

    Ephemeral (``ephemeral=True``)
        nexus-4lkmz (decision 1, LOCKED 2026-08-07; blast-radius fix
        nexus-bjltu, 2026-08-07 review round): the isolated in-process
        leg (``NX_T1_ISOLATED=1`` -> ``InMemoryVectorClient``) is deleted
        outright — T1 is PG-only, no null-store branches. This mode
        MINTS A FRESH, OWN T1 SESSION (a new uuid4, distinct from the
        parent's) via :func:`nexus.db.t1.mint_t1_session_token` and
        injects it directly as ``NX_T1_SESSION`` / ``NX_T1_SESSION_ID`` —
        the subprocess inherits an already-live, PG-backed
        ``HttpScratchStore`` session (``T1RoutingAction.USE_INHERITED``)
        with no lease lookup or mint of its own — **but ONLY when
        ``grants_tool_access=True``**: the vast majority of dispatches
        (~15/17 call sites) pass ``mcp_servers=None`` / ``allowed_tools=
        None``, the stateless tool-free default per ``claude_dispatch``'s
        own docstring, whose spawned subprocess has NO MCP tool access
        and therefore CANNOT reach T1 at all — minting for it is dead
        weight and, per nexus-bjltu, a real single point of failure (the
        storage service being transiently down must never kill a
        tool-free dispatch that never touches T1). Strips inherited
        ``NX_T1_HOST`` / ``NX_T1_PORT`` legacy vars and any stale
        ``NX_T1_ISOLATED`` regardless of ``grants_tool_access``. Mutually
        exclusive with ``share_t1``.

        A mint failure (only reachable when ``grants_tool_access=True``)
        does NOT propagate: it is logged at WARNING (carrying the
        exception) and ``NX_T1_SESSION`` / ``NX_T1_SESSION_ID`` are left
        UNSET. This is deferred fail-loud, not a null-store branch — no
        store object is fabricated here.

        WHAT HAPPENS NEXT in the subprocess's own nested MCP (if it starts
        one) is NOT simply "raises T1UnavailableThisProcessError" —
        nexus-ylof9 (P3, corrected here after an inaccurate first-round
        claim): ``NX_SESSION_ID`` is forwarded below regardless of mint
        outcome (see ``parent_session_id`` handling), so the child usually
        DOES resolve a real (the parent's) session id, not none at all.
        Concretely:

        * If the parent is a live MCP session with a FRESH published
          lease for that id (nexus-c8yvj) — the common case — the child's
          Branch 0 BORROWS it (``T1RoutingAction.USE_LEASED``) and T1
          works fine via the parent's session; this dispatch-level mint
          failure has NO visible effect on the child at all.
        * Otherwise the child's Branch 0 attempts its OWN mint for that
          forwarded id; if the underlying cause is the same storage-
          service outage, that mint also fails and defers via the
          existing nexus-brw1s hook (a per-call ``RuntimeError``, not
          ``T1UnavailableThisProcessError``).
        * ``T1UnavailableThisProcessError`` (nexus-4lkmz decision 2) fires
          ONLY when the child cannot resolve ANY session id at all (no
          forwarded id, no lease, no fallback) — the genuinely
          unresolvable-session case, not "any mint failure upstream".

        The NX_SESSION_ID forwarding behavior itself is unchanged by this
        correction (tracked separately: nexus-ylof9) — only this
        docstring's prior overstatement is fixed. What remains true
        regardless of which of the above paths fires: no store object is
        ever fabricated here, and this dispatch-level mint failure never
        kills the CURRENT dispatch (the point of this whole fix).

    Owned (default, neither flag set)
        Strips ``NX_T1_HOST`` / ``NX_T1_PORT`` / ``NX_T1_ISOLATED`` so the
        subprocess resolves its own T1 via its own nested MCP session
        (T1 is PG-only, no in-process opt-out — nexus-4lkmz). The
        subprocess gets a sealed-from-parent T1 session of its own.

    nexus-5daww (defense in depth): both ``ephemeral`` and ``owned`` also
    strip ``NX_T1_SESSION`` / ``NX_T1_SESSION_ID`` -- the SERVICE-backed T1
    session-token pair minted by the top-level MCP's
    ``_t1_lifespan`` Branch 0. Pre-fix, ``base = dict(os.environ)``
    carried the parent's already-minted, LIVE token straight through to a
    nested ``nx-mcp`` (spawned by a subsequent tool-granting dispatch, e.g.
    ``nx_plan_audit`` / ``nx_enrich_beads``), whose own Branch 0 would
    resolve the SAME session id via the still-passed-through
    ``NX_SESSION_ID`` and either reuse or re-mint against it. Stripping the
    token pair here means the child never even sees the parent's secret
    directly in its env (reduced exposure surface); it is not sufficient
    on its own to prevent a same-session re-mint since ``NX_SESSION_ID``
    is deliberately still forwarded below for attribution -- the
    session-level fix (a lease-file consult before mint) lives in
    ``mcp.core._t1_lifespan`` Branch 0 (nexus-5daww) and is the
    layer that actually prevents rotation.
    """
    if share_t1 and ephemeral:
        raise ValueError(
            "share_t1 and ephemeral are mutually exclusive: a subprocess "
            "cannot both inherit the parent's T1 and skip T1 entirely."
        )

    base = dict(os.environ)

    if share_t1:
        raise RuntimeError(
            "share_t1=True is retired (RDR-155 P4b): the parent-owned "
            "chroma T1 server no longer exists. Use T2 (memory_put) for "
            "cross-process findings, or service-mode T1 session leases."
        )
    if ephemeral:
        # nexus-4lkmz decision 1 / nexus-bjltu blast-radius fix: strip any
        # inherited T1 signals unconditionally (never the parent's token,
        # never the retired isolated leg) -- minting only happens below,
        # and only when the subprocess could possibly reach T1.
        base.pop("NX_T1_HOST", None)
        base.pop("NX_T1_PORT", None)
        base.pop("NX_T1_ISOLATED", None)
        base.pop("NX_T1_SESSION", None)
        base.pop("NX_T1_SESSION_ID", None)

        if grants_tool_access:
            from nexus.db.t1 import mint_t1_session_token  # noqa: PLC0415 — deferred to avoid circular import at module load

            dispatch_session_id = str(uuid4())
            try:
                minted = mint_t1_session_token(
                    dispatch_session_id, context="operator dispatch mint"
                )
            except Exception as exc:  # noqa: BLE001 — nexus-bjltu: a mint failure must never kill the whole dispatch; the child's own Branch 0 resolves what happens next (borrow the parent's lease, its own deferred mint, or T1UnavailableThisProcessError — see this function's docstring, nexus-ylof9)
                _log.warning(
                    "operator_dispatch_t1_mint_failed",
                    session_id=dispatch_session_id,
                    error=str(exc),
                )
            else:
                base["NX_T1_SESSION"] = minted["session_token"]
                base["NX_T1_SESSION_ID"] = dispatch_session_id
        # else: tool-free dispatch -- nothing in the subprocess can reach
        # T1, so no mint is attempted at all (nexus-bjltu).
    else:
        # Owned: subprocess spawns its own T1. Strip any parent T1
        # signals so the lifespan's Branch 3 fires.
        base.pop("NX_T1_HOST", None)
        base.pop("NX_T1_PORT", None)
        base.pop("NX_T1_ISOLATED", None)
        # nexus-5daww: never forward the parent's live SERVICE-backed T1
        # session-token pair to a nested MCP subprocess.
        base.pop("NX_T1_SESSION", None)
        base.pop("NX_T1_SESSION_ID", None)

    if parent_session_id:
        base["NX_SESSION_ID"] = parent_session_id
    return base


async def _drain_pipe(pipe: asyncio.StreamReader | None) -> bytes:
    """Read whatever bytes are currently buffered in *pipe*.

    Used by the timeout path (nexus-1at5) AFTER the subprocess has
    been killed and reaped. The writer is dead, so ``read()`` returns
    EOF immediately for whatever was buffered without blocking.
    Returns an empty ``bytes`` on any error so the caller can still
    raise the timeout exception cleanly.
    """
    if pipe is None:
        return b""
    try:
        return await pipe.read()
    except Exception:  # noqa: BLE001 - subprocess pipe failure; logged DEBUG with exc_info, returns empty bytes
        # nexus-8g79.8: empty bytes is the right return shape (caller
        # treats it as "no output"), but the silent swallow hides
        # subprocess pipe failures (OOM kill, fd exhaustion, broken
        # pipe). DEBUG-with-exc_info preserves the API contract while
        # making the cause discoverable.
        import structlog  # noqa: PLC0415 - deferred to call time
        structlog.get_logger(__name__).debug(
            "operator_pipe_read_failed", exc_info=True,
        )
        return b""


def _capped_text(text: str) -> str:
    """Cap an already-decoded string for the failure log, marking any cut."""
    if len(text) <= _LOG_STREAM_CAP:
        return text
    return text[:_LOG_STREAM_CAP] + _TRUNCATION_MARKER


def _capped(raw: bytes) -> str:
    """Decode a subprocess stream for the failure log, marking any cut."""
    return _capped_text(raw.decode(errors="replace").strip())


def _persist_timeout_log(
    timeout: float, partial_text: str, stderr: bytes, event_count: int,
) -> str:
    """Persist partial subprocess output to a timestamped log file.

    nexus-h33x8.6 a3: *partial_text* is the assistant/structured-output
    text RECONSTRUCTED by :func:`_parse_stream_json_output` from whatever
    ``--output-format stream-json`` NDJSON events arrived before the kill
    -- not a raw byte dump of the wire format (that would mostly be
    ``stream_event``/``system`` JSON envelope, not the content a debugging
    session actually wants). *event_count* is the number of NDJSON lines
    that parsed, surfaced so a reader can tell "the process barely
    started" (event_count near 0) from "it was deep into generation"
    without decoding the prompt/log by hand.

    Returns the file path as a string for inclusion in the timeout
    exception message. Failures to write the log are swallowed so
    that the timeout exception (the load-bearing signal) always
    surfaces; the absent log is a soft loss.
    """
    from datetime import datetime, timezone  # noqa: PLC0415 - branch-local; deferred to call time
    from nexus.config import nexus_config_dir  # noqa: PLC0415 - deferred to avoid circular import at module load

    try:
        logs_dir = nexus_config_dir() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = logs_dir / f"operator-timeout-{ts}.log"
        path.write_bytes(
            f"[operator-timeout {timeout}s] {ts}Z\n".encode()
            + f"--- stdout ({event_count} stream-json events; reconstructed text) ---\n".encode()
            + partial_text.encode(errors="replace") + b"\n"
            + b"--- stderr ---\n" + stderr + b"\n"
        )
        return str(path)
    except Exception as exc:  # noqa: BLE001 - best-effort timeout-log write; logged via log.warning
        _log.warning("operator_timeout_log_failed", error=str(exc))
        return "(log write failed)"


def _parse_stream_json_output(raw: str) -> tuple[dict[str, Any] | None, str, int]:
    """Parse ``claude -p --output-format stream-json`` NDJSON output.

    nexus-h33x8.6 a3. Returns ``(final_result, partial_text, event_count)``:

      * ``final_result`` -- the parsed top-level ``{"type": "result", ...}``
        event. This is the SAME wrapper shape ``--output-format json``
        returns as its single stdout object (``is_error`` /
        ``structured_output`` / ``result`` / ...), verified against a
        captured fixture pair (tests/fixtures/claude_dispatch_json_mode_
        sample.json vs claude_dispatch_stream_json_sample.ndjson): the
        NDJSON stream is a strict superset -- intermediate events plus
        the identical terminal object. ``None`` when no such line
        appears (subprocess killed before finishing, or *raw* is a
        legacy bare-JSON blob from a caller/test that never spoke
        stream-json — the caller falls back to whole-blob ``json.loads``
        in that case).
      * ``partial_text`` -- content reconstructed from
        ``content_block_delta`` stream events (``text_delta`` for plain
        text, ``input_json_delta``/``partial_json`` for the
        StructuredOutput tool call every ``json_schema``-constrained
        dispatch actually takes -- which is every real caller, since
        ``claude_dispatch`` always passes ``json_schema``) plus, as a
        fallback, complete ``assistant`` message text blocks. This is
        what de-vacuates the nexus-1at5 timeout drain: with
        ``--output-format json`` the subprocess buffers its entire
        response and writes nothing until it finishes, so a killed
        subprocess had written zero bytes BY CONSTRUCTION (all 68 prior
        operator-timeout logs are 74-75 bytes, zero on both streams).
        stream-json puts each event on the wire as it happens.
      * ``event_count`` -- number of NDJSON lines that parsed as a JSON
        object (informational; surfaced in the timeout log and
        exception message). A line truncated mid-write by SIGKILL is
        silently skipped, not counted and not fatal to the rest of the
        parse -- reconstructing partial output must be resilient to a
        cut stream by definition.
    """
    final_result: dict[str, Any] | None = None
    partial_text_parts: list[str] = []
    event_count = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        event_count += 1
        obj_type = obj.get("type")
        if obj_type == "result":
            final_result = obj
        elif obj_type == "stream_event":
            event = obj.get("event")
            if isinstance(event, dict) and event.get("type") == "content_block_delta":
                delta = event.get("delta")
                if isinstance(delta, dict):
                    text = delta.get("text")
                    if text is None:
                        # StructuredOutput tool calls (every json_schema
                        # dispatch) stream their input as input_json_delta
                        # chunks, not text_delta -- this is the dominant
                        # partial-content shape in practice.
                        text = delta.get("partial_json")
                    if isinstance(text, str):
                        partial_text_parts.append(text)
        elif obj_type == "assistant":
            message = obj.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            partial_text_parts.append(text)
    return final_result, "".join(partial_text_parts), event_count


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
    """Canonical model id (196-R3), taken from the single ``modelUsage``
    entry's ``canonicalModel`` field. ``None`` when ``modelUsage`` is
    absent/empty, or when it carries more than one model (ambiguous for
    this single-value convenience field -- callers needing per-model
    detail read ``model_usage`` directly)."""

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


def _parse_dispatch_usage(final_result: dict[str, Any] | None) -> DispatchUsage:
    """Parse cost/usage telemetry from *final_result* -- the terminal
    ``{"type": "result", ...}`` stream-json event from
    ``_parse_stream_json_output``, or ``None`` when no such event was
    found (subprocess killed before emitting one, or a legacy bare-JSON
    test double that never spoke stream-json).

    Every affected field is ``None`` -- never ``0.0`` -- when the source
    payload doesn't carry it, with a ``dispatch_usage_fields_missing``
    (or, for a wholly absent result event, ``dispatch_usage_no_result_
    event``) structlog warning naming what was missing. This is a pure
    function over the parsed envelope; it does not raise on a malformed
    or partial payload -- a telemetry gap must never fail a dispatch that
    otherwise succeeded.
    """
    if final_result is None:
        _log.warning("dispatch_usage_no_result_event")
        return DispatchUsage(
            model=None,
            cost_usd=None,
            input_tokens=None,
            output_tokens=None,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
            duration_ms=None,
            duration_api_ms=None,
            num_turns=None,
            model_usage={},
        )

    missing: list[str] = []

    cost_usd = final_result.get("total_cost_usd")
    if cost_usd is None:
        missing.append("total_cost_usd")

    usage = final_result.get("usage")
    if isinstance(usage, dict):
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        cache_creation_input_tokens = usage.get("cache_creation_input_tokens")
        cache_read_input_tokens = usage.get("cache_read_input_tokens")
    else:
        missing.append("usage")
        input_tokens = output_tokens = None
        cache_creation_input_tokens = cache_read_input_tokens = None

    duration_ms = final_result.get("duration_ms")
    duration_api_ms = final_result.get("duration_api_ms")
    num_turns = final_result.get("num_turns")

    model_usage_raw = final_result.get("modelUsage")
    model_usage: dict[str, ModelUsage] = {}
    model: str | None = None
    if isinstance(model_usage_raw, dict) and model_usage_raw:
        for key, entry in model_usage_raw.items():
            if not isinstance(entry, dict):
                continue
            canonical = entry.get("canonicalModel") or key
            model_usage[key] = ModelUsage(
                canonical_model=canonical,
                input_tokens=entry.get("inputTokens"),
                output_tokens=entry.get("outputTokens"),
                cache_read_input_tokens=entry.get("cacheReadInputTokens"),
                cache_creation_input_tokens=entry.get("cacheCreationInputTokens"),
                cost_usd=entry.get("costUSD"),
            )
        if len(model_usage) == 1:
            model = next(iter(model_usage.values())).canonical_model
        # len(model_usage) > 1: leave `model` None -- ambiguous for this
        # single-value convenience field. Per-model attribution across a
        # bundled multi-model dispatch is what `model_usage` is for
        # (consumed by .p1b's bundle StepRecords).
    else:
        missing.append("modelUsage")

    if missing:
        _log.warning("dispatch_usage_fields_missing", missing=missing)

    return DispatchUsage(
        model=model,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        duration_ms=duration_ms,
        duration_api_ms=duration_api_ms,
        num_turns=num_turns,
        model_usage=model_usage,
    )


async def _feed_stdin(proc: "asyncio.subprocess.Process", prompt: str) -> None:
    """Write *prompt* to stdin and close it.

    Mirrors ``asyncio.subprocess.Process._feed_stdin``'s tolerance of the
    child closing its end early (e.g. an immediate arg-parse/auth
    failure) -- BrokenPipeError/ConnectionResetError on drain are
    expected there too, not fatal.
    """
    proc.stdin.write(prompt.encode())
    try:
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    proc.stdin.close()


async def _drain_stream(pipe: "asyncio.StreamReader", sink: list[bytes]) -> None:
    """Read *pipe* in bounded chunks, appending each chunk to *sink* as it
    arrives, until EOF.

    nexus-h33x8.6 a3. Two deliberate departures from the obvious approach:

    1. NOT ``asyncio.subprocess.Process.communicate()``. Verified
       empirically: ``communicate()``'s internal unbounded
       ``StreamReader.read(-1)`` loop (CPython ``asyncio/streams.py``)
       accumulates into a list LOCAL to that nested coroutine, popping
       bytes out of the stream's own buffer as it goes. When the
       enclosing ``asyncio.wait_for(..., timeout=...)`` times out and
       cancels ``communicate()``, everything already read is discarded
       with the cancelled frame -- and it is no longer sitting in the
       stream's buffer either, since it was already popped out. So the
       pre-a3 ``_drain_pipe``-after-kill path always found nothing,
       independent of ``--output-format``: switching only the output
       format left the drain at 0 bytes in a controlled repro (identical
       prompt, SIGKILL after timeout: manual accumulator captured
       ~65KB, ``communicate()`` + post-kill ``_drain_pipe`` captured 0).
       Accumulating into *sink*, a list owned by the CALLER's frame
       (``claude_dispatch``), means a cancellation here still leaves
       whatever was appended intact.
    2. NOT ``readline()``. asyncio's ``StreamReader`` enforces a
       line-length limit (default 64 KiB) and raises
       ``LimitOverrunError`` if a single NDJSON line -- e.g. the
       terminal ``result`` event carrying a large ``structured_output``
       -- exceeds it, a risk the old single unbounded ``read(-1)``
       never had. ``read(n)`` has no per-record limit; NDJSON line
       splitting happens once, after the read loop, in
       ``_parse_stream_json_output``.
    """
    while True:
        chunk = await pipe.read(65536)
        if not chunk:
            return
        sink.append(chunk)


def _close_dispatch_session(session_id: str | None, session_token: str | None) -> None:
    """Best-effort close of a dispatch-minted T1 session (nexus-bjltu
    Significant #1, TWO-STEP teardown per round-2 code-review finding).

    Called from ``claude_dispatch``'s ``finally`` after the subprocess has
    exited (success, harness failure, timeout-kill, or even a subprocess-
    creation failure) — the session is no longer needed once the one-shot
    subprocess that owned it is gone.

    TWO INDEPENDENT calls, mirroring ``mcp.core``'s Branch-0 teardown
    EXACTLY (``_t1_session_shutdown``: scratch close first, then
    ``HttpTokenStore().close_session()``) — round-1 of this fix closed only
    the first half:

    1. ``HttpScratchStore(...).close_session()`` — deletes the SCRATCH ROWS
       for this session. Backstopped by the passive TTL sweep.
    2. ``HttpTokenStore().close_session(session_id)`` — revokes the minted
       SESSION TOKEN itself (``POST /v1/sessions/close``), the row
       ``mint_t1_session_token``'s ``POST /v1/sessions/start`` created.
       **NOT backstopped by any sweep** (``session_tokens`` has no
       scheduled sweep, unlike the scratch rows — round-2 finding,
       engine-side sweep tracked separately as nexus-t23zk, not this bead):
       an unclosed token is a PERMANENT row, not a 24h-bounded one, which
       is why this call is not optional the way the scratch close's TTL
       backstop makes it merely "nice to have".

    Each step is independently wrapped: a failure in step 1 must not skip
    step 2, and vice versa. Both failures are logged, never raised — a
    dispatch that already succeeded (or already failed for its own reason)
    must not additionally fail on cleanup.

    A no-op (skips both steps) when *session_id* is ``None`` (tool-free
    dispatch, or a mint that never happened/failed — nothing to close
    either way).
    """
    if not session_id:
        return

    # Step 1: scratch rows (HttpScratchStore), constructed EXPLICITLY from
    # the minted id/token rather than reading ambient env (this process's
    # own env was never mutated — only the subprocess's copy was).
    try:
        from nexus.db.http_scratch_store import HttpScratchStore  # noqa: PLC0415 — deferred to avoid circular import at module load

        store = HttpScratchStore(session_id=session_id, _session_token=session_token)
        try:
            store.close_session()
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup; the scratch-row TTL sweep is the backstop, never fail an already-completed dispatch on close
        _log.warning(
            "operator_dispatch_t1_scratch_close_failed",
            session_id=session_id,
            error=str(exc),
        )

    # Step 2: the session TOKEN itself (HttpTokenStore) -- a SEPARATE call
    # against a SEPARATE endpoint, using the process's own SERVICE bearer
    # (ambient NX_SERVICE_TOKEN, never touched by _build_dispatch_env),
    # never the per-session T1 token from step 1. Independent try/except:
    # this must run even if step 1 raised, and its own failure must not
    # propagate either.
    try:
        from nexus.db.t2.http_token_store import HttpTokenStore  # noqa: PLC0415 — deferred to avoid circular import at module load

        with HttpTokenStore() as token_store:
            token_store.close_session(session_id)
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup; NO sweep backstops this row (nexus-t23zk tracks an engine-side one, not implemented here), but a dispatch that already completed must not additionally fail on cleanup
        _log.warning(
            "operator_dispatch_t1_token_close_failed",
            session_id=session_id,
            error=str(exc),
        )


async def claude_dispatch(
    prompt: str,
    json_schema: dict[str, Any],
    timeout: float = 300.0,
    *,
    allowed_tools: list[str] | None = None,
    mcp_servers: dict[str, Any] | None = None,
    usage_sink: list[DispatchUsage] | None = None,
    model: str | None = None,
    operator: str | None = None,
) -> dict[str, Any]:
    """Dispatch a single operator call to claude -p, fully async.

    Args:
        prompt: The full prompt text, delivered via stdin.
        json_schema: JSON Schema the model output must conform to.
            Passed via --output-format json and --json-schema flag.
        timeout: Seconds before the subprocess is killed. Default 300s
            (5 min) — the analytical workloads these tools run
            (audit, enrich, summarise, extract) can legitimately
            take minutes. Callers that know their input is short
            should override lower; callers running heavy audits
            override up (``nx_plan_audit`` / ``nx_tidy`` use 600s).
            The prior 60s default produced a lot of false timeouts
            on real workloads.
        allowed_tools: Opt-in tool allowlist (nexus-mawqw). When set,
            ``--allowedTools <comma-joined>`` is passed so the child
            ``claude -p`` may call those tools (built-ins like ``Read`` /
            ``Grep`` / ``Glob`` and/or MCP tools like ``mcp__nexus``).
            ``None`` (default) keeps the stateless-operator contract:
            no tool access. DO NOT pass this for stateless operators
            (extract/filter/rank/etc.) — they take all input from the
            prompt and must stay tool-free.
        mcp_servers: Opt-in MCP server map (nexus-mawqw), shape
            ``{server_key: {"command": ..., "args": [...], ...}}``. When
            set, ``--mcp-config '{"mcpServers": {...}}'`` is passed inline.
            Servers provided via the flag are *explicitly* supplied, so
            they clear the post-CC-2.1.162 pending-approval gate that
            denies tool calls to unapproved ``.mcp.json`` servers. Pair
            with ``allowed_tools`` containing ``mcp__<server_key>`` (or a
            specific ``mcp__<server_key>__<tool>``) to actually permit the
            calls. ``None`` (default) injects no MCP servers.
        usage_sink: Opt-in out-param (RDR-196 .p1a, nexus-nyry9.7). When
            not ``None``, a ``DispatchUsage`` -- parsed from the terminal
            stream-json result event -- is appended to it for every
            dispatch that reaches the post-empty-stdout parse step,
            **including several that then go on to raise**. The append
            happens once ``_parse_stream_json_output`` has run on *raw*
            stdout, which is BEFORE three of the six possible raises
            below -- those three still carry a parsed envelope (often
            real, non-zero spend) at the point they fire, so the append
            deliberately runs first: a caller catching one of these can
            still read what the failed turn cost.

            Appended, then STILL RAISES:
              * ``OperatorOutputError`` -- "not valid JSON" (no stream-json
                result event found, and the raw blob also failed
                ``json.loads``). Appends the all-``None`` DispatchUsage
                (+ warning) produced when no result event was found.
              * ``OperatorError`` -- the parsed result's ``is_error`` was
                true.
              * ``OperatorOutputError`` -- the parsed result's
                ``structured_output`` was null.

            NEVER reaches the append (raises before any parse of stdout):
              * ``OperatorTimeoutError`` -- subprocess killed mid-turn.
              * ``OperatorError`` -- subprocess exited non-zero.
              * ``OperatorOutputError`` -- "empty stdout".

            ``None`` (default) is a complete no-op: this preserves
            ``claude_dispatch``'s existing return contract (a bare dict)
            for all current call sites, none of which unpack a tuple.
            Independent of, and additive with, :func:`ambient_usage_sink`
            (RDR-196 .p1b Gap-1 addendum, nexus-nyry9.8): when an ambient
            sink is ALSO active, both receive the same parsed
            ``DispatchUsage`` instance for this call.
        model: Opt-in ``--model`` override (RDR-196 .p2b, nexus-nyry9.15).
            ``None`` (default) appends NO ``--model`` flag -- argv is
            byte-identical to every pre-.p2b call site. When set, the
            value is passed to the CLI verbatim (a tier alias such as
            ``"haiku"``/``"sonnet"`` -- see
            ``nexus.operators.model_tiers.resolve_model_for_tier`` --
            or a pinned model id); ``claude_dispatch`` itself never
            consults the tier table, so resolving a tier to a model
            string is entirely the caller's decision. Whatever the CLI
            actually resolves the alias to is recorded separately, in
            ``DispatchUsage.model`` (sourced from the stream-json
            envelope's own ``canonicalModel``, not this argument), so a
            future alias re-point stays observable in telemetry.
        operator: Opt-in diagnostic label (RDR-196 .p2b) identifying which
            operator/caller this dispatch is for (e.g. ``"operator_rank"``).
            Purely cosmetic -- carried into the dispatch-harness-failure
            error message (only when *model* is also set) so a rejected
            ``--model`` value names both what was rejected and who asked
            for it. Never affects argv or control flow.

    Returns:
        Parsed JSON dict from stdout.

    Raises:
        OperatorTimeoutError: subprocess exceeded *timeout*.
        OperatorError: subprocess exited non-zero.
        OperatorOutputError: stdout was not valid JSON.
    """
    schema_json = json.dumps(json_schema)
    # RDR-196 .p2b review fix (nexus-nyry9.15, code-review-expert [23032]
    # Important #2): normalize model="" / whitespace-only to None ONCE
    # here, so the argv-append (`if model:`) and the error-clause gate
    # (`if model is not None:`) below can never disagree about whether a
    # model was "set" -- a lingering empty string previously would skip
    # --model in argv but still append a `[model='' ...]` clause to a
    # harness-failure error, falsely implying an override reached the
    # CLI when none did.
    if model is not None and not model.strip():
        _log.warning("claude_dispatch_empty_model_normalized", operator=operator)
        model = None
    # Search review I-6: start in a new process group so we can reach
    # any child processes ``claude -p`` spawns (nested claude calls, tool
    # subprocesses). Same killpg idiom as T1 chroma + MinerU cleanup
    # (PR #198). Without this, ``proc.kill()`` on timeout only kills the
    # claude leader and orphans the children.
    #
    # nexus-4lkmz decision 1 / nexus-bjltu blast-radius fix: the subprocess
    # inherits a freshly minted, OWN T1 session (NX_T1_SESSION /
    # NX_T1_SESSION_ID) rather than the retired NX_T1_ISOLATED=1 in-process
    # leg -- but ONLY when this call actually grants the subprocess tool
    # access (mcp_servers or allowed_tools set) that could reach T1. The
    # stateless tool-free default (the common case -- extract/filter/rank/
    # summarize/etc.) spawns a subprocess with NO MCP tool access, so it
    # cannot reach T1 at all; minting for it would be dead weight and a
    # storage-service hiccup must never kill a dispatch that never touches
    # T1 (see ``_build_dispatch_env``'s ephemeral-mode docstring).
    #
    # NX_SESSION_ID=<parent-uuid> tells the subprocess's SessionStart hook
    # that it is a NESTED session — its own conversation UUID arrives via
    # the stdin payload, but it should preserve the parent's
    # ``current_session`` flat-file pointer instead of stomping it. Without
    # this, the subprocess's hook would write its own UUID into
    # ``current_session``, the file would point at no on-disk record (skip-
    # T1 wrote none), and the parent's shell-side ``nx scratch`` would fall
    # back to EphemeralClient for the rest of the parent conversation.
    # ``read_claude_session_id`` reads the parent's UUID at dispatch time —
    # the parent's SessionStart populated it before any operator runs.
    from nexus.session import read_claude_session_id  # noqa: PLC0415 - deferred to avoid circular import at module load
    parent_session_id = read_claude_session_id()
    # RDR-105 P2.5 (nexus-4gby): build the subprocess env via the
    # three-mode helper. The operator-dispatch caller is the
    # canonical stateless one-shot, so default to ``ephemeral=True``.
    # nexus-4lkmz / nexus-bjltu: the helper mints the subprocess its own
    # PG-backed T1 session (NX_T1_SESSION / NX_T1_SESSION_ID) rather than
    # the retired NX_T1_ISOLATED=1 in-process leg -- gated on
    # ``grants_tool_access`` so a tool-free dispatch never pays for (or
    # depends on) a mint it cannot use.
    env = _build_dispatch_env(
        ephemeral=True,
        parent_session_id=parent_session_id,
        grants_tool_access=bool(mcp_servers or allowed_tools),
    )
    # nexus-bjltu Significant #1: track whether THIS dispatch minted its
    # own T1 session so it can be closed after the subprocess exits,
    # rather than relying entirely on the passive 24h TTL sweep backstop
    # (operator dispatch is the high-volume default path -- real
    # session-row accretion otherwise). Only ever set when
    # ``_build_dispatch_env`` actually minted (grants_tool_access=True AND
    # the mint succeeded); absent for tool-free dispatches and for a
    # failed mint (nothing to close in either case).
    _minted_session_id = env.get("NX_T1_SESSION_ID")
    _minted_session_token = env.get("NX_T1_SESSION")
    # Base argv is the stateless, tool-free default. Opt-in tool access
    # (nexus-mawqw) appends --mcp-config / --allowedTools only when the
    # caller explicitly requests it, preserving the stateless-operator
    # contract for extract/filter/rank/etc.
    argv: list[str] = [
        "claude", "-p",
        # nexus-h33x8.6 a3: stream-json (NOT plain json) so the subprocess
        # writes each event to stdout as it happens, rather than buffering
        # the whole response and writing nothing until the turn completes.
        # --verbose is REQUIRED by the claude CLI for stream-json (it
        # refuses to start otherwise: "requires --verbose").
        # --include-partial-messages adds content_block_delta events
        # (text_delta / input_json_delta) so a killed-mid-turn subprocess
        # leaves reconstructable partial content, not just envelope noise
        # -- see _parse_stream_json_output.
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--json-schema", schema_json,
        "--no-session-persistence",
        # RDR-196 Gap 4 (measured by session nexus-0d, 2026-08-19): without
        # this flag the child loads the user's ENTIRE ambient MCP server set
        # (.mcp.json / settings) even on the tool-free default path that has
        # no tools to call them with -- ~92K context tokens and ~2x the
        # dollars per operator call vs ~45K with a strict empty config.
        # With --strict-mcp-config and no --mcp-config the child loads zero
        # servers; with an opt-in --mcp-config below it loads ONLY those.
        "--strict-mcp-config",
    ]
    # RDR-196 .p2b (nexus-nyry9.15): ability only -- appended ONLY when the
    # caller passes an explicit model. Tier resolution to this string (if
    # any) happens in the CALLER, e.g. via
    # ``nexus.operators.model_tiers.resolve_model_for_tier`` -- claude_dispatch
    # never imports or consults the tier table itself. ``model=None`` (the
    # default, and every one of the 18 pre-.p2b call sites) leaves argv
    # byte-identical to before this bead.
    if model:
        argv += ["--model", model]
    if mcp_servers:
        argv += ["--mcp-config", json.dumps({"mcpServers": mcp_servers})]
    if allowed_tools:
        argv += ["--allowedTools", ",".join(allowed_tools)]
    # nexus-bjltu (round 2, code-reviewer + critic independently): the
    # minted-session close (below) must run regardless of how the dispatch
    # finishes -- successful subprocess completion, harness failure,
    # timeout-kill, or even subprocess CREATION itself raising -- so the
    # try/finally starts BEFORE ``create_subprocess_exec``, not after it.
    # A mint immediately followed by a spawn failure (fork/exec error,
    # resource exhaustion) previously leaked both the scratch rows and the
    # session token with nothing to close.
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=env,
        )

        # nexus-h33x8.6 a3: NOT proc.communicate(). See _drain_stream's
        # docstring for why -- communicate()'s internal read(-1) loop
        # discards already-read bytes when asyncio.wait_for cancels it on
        # timeout, which made the nexus-1at5 partial-output drain below
        # structurally empty regardless of output format. stdout_chunks /
        # stderr_chunks are owned by THIS frame, so a cancellation here
        # leaves whatever was appended intact.
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def _run_io() -> None:
            # nexus-h33x8.6 review round 2 (code-review on dca12e1e3):
            # proc.wait() MUST be inside this wait_for-guarded coroutine,
            # not after it -- mirrors CPython's own Process.communicate()
            # shape, which calls self.wait() from INSIDE the coroutine
            # tree it awaits, not after returning from it. Both streams
            # reaching EOF does not guarantee the child has actually
            # exited: a process can close its stdout/stderr fds before
            # its own exit completes. A proc.wait() left outside the
            # timeout guard would then hang forever with no kill ever
            # firing -- a real deadlock, not merely a slow path (verified:
            # a fake proc whose streams EOF immediately but whose wait()
            # never returns hung the caller indefinitely pre-fix).
            await asyncio.gather(
                _feed_stdin(proc, prompt),
                _drain_stream(proc.stdout, stdout_chunks),
                _drain_stream(proc.stderr, stderr_chunks),
            )
            await proc.wait()

        try:
            await asyncio.wait_for(_run_io(), timeout=timeout)
        except asyncio.TimeoutError:
            # Search review I-6: reach the whole process group so any claude
            # children (nested planners, tool subprocesses) get reaped too.
            # safe_killpg guards on isinstance(proc.pid, int) so mocked-
            # subprocess tests deterministically fall through to proc.kill()
            # — the pgid=1 deadlock on GitHub ubuntu-latest is covered by
            # tests/test_process_group_safety.py.
            from nexus.util.process_group import safe_killpg  # noqa: PLC0415 - deferred to avoid circular import at module load
            import signal  # noqa: PLC0415 - branch-local; deferred to call time

            if not safe_killpg(proc, signal.SIGKILL):
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001 - best-effort process reap during cleanup; non-fatal
                    pass
            # Reap the leader so the asyncio transport closes cleanly.
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001 - best-effort cancel cleanup before drain-and-raise; non-fatal
                pass
            # nexus-1at5 / nexus-h33x8.6 a3: stdout_chunks/stderr_chunks
            # already hold every chunk _drain_stream consumed before
            # cancellation (unlike the old communicate()-based drain,
            # cancellation does not discard them). One more read mops up
            # a final chunk that may have still been sitting in the
            # StreamReader's internal buffer (arrived but not yet
            # delivered to _drain_stream) when the writer died -- after
            # kill+wait the writer is dead, so this drains cleanly
            # without blocking.
            stdout_chunks.append(await _drain_pipe(proc.stdout))
            stderr_chunks.append(await _drain_pipe(proc.stderr))
            partial_stdout = b"".join(stdout_chunks)
            partial_stderr = b"".join(stderr_chunks)
            # Reconstruct readable partial content from the NDJSON so far,
            # rather than persisting the raw wire bytes (mostly envelope/
            # system JSON, not what a debugging session wants) -- otherwise
            # a 5-minute timeout discards 5 minutes of analytical output
            # and the next debugging session starts from zero.
            _final_result, partial_text, event_count = _parse_stream_json_output(
                partial_stdout.decode(errors="replace")
            )
            log_path_str = _persist_timeout_log(timeout, partial_text, partial_stderr, event_count)
            raise OperatorTimeoutError(
                f"claude -p timed out after {timeout}s; partial output "
                f"({len(partial_text)} chars reconstructed from {event_count} "
                f"stream-json events, {len(partial_stdout)}B raw stdout, "
                f"{len(partial_stderr)}B stderr) logged to {log_path_str}",
                partial_text=partial_text,
                event_count=event_count,
                # _persist_timeout_log's own best-effort write can fail (its
                # sentinel string, not an exception -- the timeout signal
                # must surface regardless); a4 gets None rather than a path
                # that doesn't exist on disk.
                log_path=Path(log_path_str) if log_path_str != "(log write failed)" else None,
            )

        stdout = b"".join(stdout_chunks)
        stderr = b"".join(stderr_chunks)

        if proc.returncode != 0:
            # GH #1414: `claude -p --output-format json` reports its errors on
            # STDOUT, so a stderr-only message rendered as the bare, useless
            # "claude -p exited 1:" — twice, for nx_plan_audit, with nothing in
            # mcp.log either. Report whichever stream spoke.
            #
            # nexus-h33x8.6 review round 2 (code-review on dca12e1e3): a3
            # switched stdout to NDJSON (stream-json), but this branch kept
            # reading the RAW joined bytes for the snippet/detail/durable-log
            # fields -- mostly system/assistant envelope JSON now, not
            # claude's own error text, which is exactly the opacity #1414
            # fixed, reintroduced one layer down. Parse first; prefer the
            # terminal result event's own `result` field (labelling whether
            # it carried `is_error`); fall back to the raw bytes only when no
            # result line parsed at all (process died before ever emitting
            # one -- crash before the first event, non-JSON output, etc).
            final_result, _partial_text, _event_count = _parse_stream_json_output(
                stdout.decode(errors="replace")
            )
            if final_result is not None and isinstance(final_result.get("result"), str):
                stdout_text = final_result["result"].strip()
                out_label = (
                    "stdout(claude-error)" if final_result.get("is_error")
                    else "stdout(claude-result)"
                )
            else:
                stdout_text = stdout.decode(errors="replace").strip()
                out_label = "stdout"
            err_snippet = stderr.decode(errors="replace").strip()[:300]
            out_snippet = stdout_text[:300]
            parts = [
                f"{label}: {text}"
                for label, text in (("stderr", err_snippet), (out_label, out_snippet))
                if text
            ]
            # Silence must READ as silence: a bare trailing colon is
            # indistinguishable from "we dropped the output", which is the
            # ambiguity that cost GH #1414 a hand investigation.
            detail = " | ".join(parts) if parts else "no output on stdout or stderr"
            # The DURABLE half. The exception above is visible for exactly one
            # turn, in whatever renders the tool error; nothing writes it down.
            # GH #1414 searched a May-July mcp.log and found nothing, because
            # FastMCP's handler returns str(e) to the client without logging —
            # and of the 17 call sites, 13 propagate bare on at least one real
            # invocation path, three of them (nx_plan_audit, nx_tidy,
            # nx_enrich_beads) with no covered path at all. This is the one
            # choke point every caller passes through, including call site 18
            # that nobody has written yet, so the record belongs here rather
            # than at N call sites that must each remember to opt in.
            #
            # Deliberately NOT capped at the exception's 300 chars: that cap
            # buys a readable message, and a durable record that inherits it
            # loses the same diagnostic tail all over again (nexus-1at5's
            # actual lesson was durability independent of the exception text,
            # which the first cut of this fix claimed but did not deliver).
            #
            # SCOPE, precisely: this fires for all 17 call sites, but DURABILITY
            # is a property of the calling process's logging mode, not of this
            # choke point. mode="mcp" gets the rotating file handler, so the 15
            # server-side sites get a record on disk. `nx taxonomy discover` and
            # `review --auto` (taxonomy_cmd.py:1537,:1653) run under
            # mode="cli", which logging_setup returns from before any file
            # handler is attached — stderr only. Those two go from 100% silent
            # to one stderr line per failure during the run, which is an
            # improvement and is NOT "something to grep afterward".
            emit = _log.info if _ROLLED_UP.get() else _log.warning
            emit(
                "operator_dispatch_failed",
                returncode=proc.returncode,
                stdout=_capped_text(stdout_text),
                stderr=_capped(stderr),
                # RDR-196 .p2b review fix (nexus-nyry9.15, code-review-expert
                # [23032] Important #1): the DURABLE record must carry the
                # same model/operator label the transient exception text
                # gets a few lines below -- the exception is visible for
                # exactly one turn; this structlog emit is what a later
                # investigation actually greps.
                model=model,
                operator=operator,
            )
            # nexus-ri56e: (a) origin unambiguity — a populated message now
            # reads like an ordinary application error, but this is the
            # DISPATCH HARNESS failing (the claude -p CLI exited non-zero);
            # whoever hits it must not mistake the relayed error text for a
            # model-level answer. (b) addressability — the timeout branch has
            # always named its artifact in the exception; name ours too, or
            # honestly say there is none (plain CLI mode).
            from nexus.logging_setup import active_log_file  # noqa: PLC0415 — deferred: logging_setup is heavier than this hot-free error path needs at import time

            log_file = active_log_file()
            where = (
                f"durable record: operator_dispatch_failed in {log_file}"
                if log_file is not None
                else "no log file attached (plain CLI mode) — this message is "
                     "the only record"
            )
            # RDR-196 .p2b DO 4: a rejected --model must name both the
            # model and the operator that asked for it -- only when
            # *model* was actually set, so the default (model=None)
            # error text is untouched.
            model_operator_clause = (
                f" [model={model!r} operator={operator!r}]"
                if model is not None else ""
            )
            raise OperatorError(
                f"claude -p exited {proc.returncode} (dispatch-harness "
                f"failure, not a model answer): {detail} [{where}]"
                f"{model_operator_clause}"
            )

        raw = stdout.decode(errors="replace").strip()
        if not raw:
            raise OperatorOutputError("claude -p produced empty stdout")

        # nexus-h33x8.6 a3: stdout is now NDJSON (stream-json). The
        # terminal "result" event is byte-identical in shape to what
        # --output-format json used to return as the whole payload, so
        # extract that line first. Fall back to parsing the whole blob as
        # a single JSON object when no such line is found -- covers a
        # subprocess/test double that emits a bare JSON blob rather than
        # real stream-json (the format this repo's own test doubles use).
        final_result, _partial_text, event_count = _parse_stream_json_output(raw)
        _ambient_sink = _ambient_usage_sink.get()
        if usage_sink is not None or _ambient_sink is not None:
            # RDR-196 .p1a (nexus-nyry9.7) + .p1b Gap-1 addendum
            # (nexus-nyry9.8, 2026-08-20): capture cost/usage telemetry
            # for a dispatch that reached a parsed result -- the raw JSON
            # fallback below (final_result is None) still parses to an
            # all-None DispatchUsage + warning via _parse_dispatch_usage,
            # never a silently-omitted record. This append is BEFORE the
            # fallback's own possible raise (see the docstring's "Appended,
            # then STILL RAISES" list) -- the diagnostic enrichment below
            # (nexus-nyry9.4 review-fix, taxonomy id=99) only changes the
            # exception's MESSAGE, never this ordering or the exception
            # type, so that contract and its test
            # (TestUsageSinkAppendsBeforeSubsequentRaises::
            # test_invalid_json_fallback_still_appends_all_none_usage_
            # then_raises) both stay true unchanged. Parsed ONCE; the
            # SAME DispatchUsage instance is appended to both sinks when
            # both are active, never two independently-parsed copies.
            _usage = _parse_dispatch_usage(final_result)
            if usage_sink is not None:
                usage_sink.append(_usage)
            if _ambient_sink is not None:
                _ambient_sink.append(_usage)
        if final_result is not None:
            parsed = final_result
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                # nexus-nyry9.4 review-fix (RDR-196 .r4 residual, taxonomy
                # id=99): the old ``raw[:200]`` preview showed only the
                # OPENING bytes of the whole blob, which for a real
                # multi-line NDJSON stream is rarely where the parse
                # actually failed -- useless when the interesting content
                # is past byte 200. Locate the specific line ``json.loads``
                # choked on via the exception's own ``lineno`` and preview
                # THAT, plus how many lines parsed as objects at all (0
                # means the child never spoke NDJSON; >0 means a real
                # stream was seen but never reached a terminal "result"
                # event) -- "no terminal 'result' event seen" is always
                # true in this branch (we are here BECAUSE final_result is
                # None) but stated explicitly rather than left implicit.
                _raw_lines = raw.splitlines()
                _offending_line = (
                    _raw_lines[exc.lineno - 1]
                    if 0 < exc.lineno <= len(_raw_lines)
                    else raw
                )
                raise OperatorOutputError(
                    f"claude -p output is not valid JSON: {exc} — "
                    f"{event_count} NDJSON line(s) parsed as objects, "
                    f"no terminal 'result' event seen — "
                    f"offending line (first 200 chars): {_offending_line[:200]!r}"
                ) from exc

        # `claude -p --output-format json` returns a wrapper:
        # {"type":"result", "is_error":bool, "result":str, "structured_output":dict, ...}
        # Callers supplied a `json_schema`, so they expect the schema-conforming
        # dict, not the wrapper.  Surface errors explicitly, unwrap otherwise.
        if isinstance(parsed, dict) and "structured_output" in parsed:
            if parsed.get("is_error"):
                raise OperatorError(
                    f"claude -p reported error: {parsed.get('result', '')[:300]}"
                )
            structured = parsed.get("structured_output")
            if structured is None:
                raise OperatorOutputError(
                    f"claude -p returned null structured_output; "
                    f"result={parsed.get('result', '')[:200]!r}"
                )
            return structured
        return parsed
    finally:
        # nexus-bjltu Significant #1: close the minted session (if any)
        # now that the subprocess that owned it is gone. No-op when this
        # dispatch never minted (tool-free, or a failed mint).
        _close_dispatch_session(_minted_session_id, _minted_session_token)
