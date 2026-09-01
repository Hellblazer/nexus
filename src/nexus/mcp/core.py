# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MCP core tools: search, store, memory, scratch, collections, plans.

36 registered tools + 3 demoted (plain functions, no @mcp.tool()). The
RDR-182 consent-gated ``forensics``/``remediate`` pair (nexus-ykzbj.10/.11)
was deleted at nexus-lgdel — the chash-rekey upgrade rung it steered
operators toward no longer exists.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

import structlog
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

try:  # nexus-vc5yb: UNEXPORTED SDK internal — absence must not kill startup
    from mcp.server.fastmcp.server import Settings as _FastMCPSettings
except ImportError:  # pragma: no cover — future SDK restructure
    _FastMCPSettings = None

from nexus.corpus import (
    embedding_model_for_collection,
    embedding_model_for_collection_name,
    index_model_for_collection,
    resolve_corpus,
    t3_collection_name,
)
from nexus.db.t3 import verify_collection_deep
from nexus.migration.banner import degrade_loud_when_migrating
from nexus.filters import parse_where_str as _parse_where_str
from nexus.config import load_config
from nexus.hook_registry import HookRegistry as _HookRegistry, install_default_hooks as _install_default_hooks
from nexus.mcp_infra import (
    catalog_auto_link as _catalog_auto_link,
    get_catalog as _get_catalog,
    get_collection_names as _get_collection_names,
    get_recent_search_traces as _get_recent_search_traces,
    get_t1 as _get_t1,
    get_t3 as _get_t3,
    inject_t1 as _inject_t1,
    inject_t3 as _inject_t3,
    record_search_trace as _record_search_trace,
    reset_singletons as _reset_singletons,
    t2_ctx as _t2_ctx,
    t2_index_write as _t2_index_write,
)
from nexus.ttl import parse_ttl

#: Module logger for MCP tool handlers (nexus-yttqr). Read-path handlers return a
#: string to the agent rather than raising; before returning an error they must
#: ALSO emit a structured server-side log so a dead backing service is diagnosable.
_log = structlog.get_logger(__name__)

#: Substrings that mark a backing-service-unreachable failure. When an MCP tool
#: error matches, the agent-facing return carries an actionable remediation hint
#: instead of a bare exception repr. Kept narrow (no bare "connect") to avoid
#: false-positive hints on unrelated errors that merely mention connecting
#: (review 2026-06-23): ``ConnectionError``/``TimeoutError`` instances are matched
#: by type below; these markers catch string-only wrappers (e.g. httpx).
_CONNECTION_ERROR_MARKERS = (
    "connection refused", "cannot connect", "broken pipe", "connection reset",
    "max retries", "timed out", "no route to host", "connection aborted",
)


def _mcp_tool_error(tool: str, e: Exception) -> str:
    """Log an MCP tool-handler exception structured, return an agent-facing string.

    nexus-yttqr: the ~22 handlers in this module historically ended with
    ``return f"Error: {e}"`` and emitted NO server-side log — a dead service gave
    the agent a bare repr and left nothing to diagnose. This helper logs the
    exception with ``exc_info`` (traceback stays server-side, never leaks into the
    return string) and enriches the returned message with a remediation hint when
    the failure looks like the backing service is unreachable.

    The unreachable-service hint is gated narrowly: ``ConnectionError`` /
    ``TimeoutError`` instances, or a marker substring. Bare ``OSError`` (which
    includes ``PermissionError`` / ``FileNotFoundError`` — realistic on a locked
    SQLite file, NOT a daemon-down condition) is deliberately NOT treated as a
    connection failure, to avoid a misleading "restart the daemon" hint.

    nexus-ngcpo Finding/(d): a ``SESSION_UNAUTHORIZED_MARKER`` (a T1 401 --
    the live MCP session's minted token is stale/revoked) previously fell
    through to the bare ``f"Error: {e}"`` return with no reconnect guidance,
    unlike ``commands/scratch.py``'s ``_clean_service_errors``, which already
    gives the CLI path an actionable "reconnect the conexus MCP/extension"
    hint for the exact same marker. Checked BEFORE the connection-error
    branch below since the two are mutually exclusive failure shapes (a 401
    is not a connection failure) and the marker text is specific enough
    (contains "unauthorized") that ordering does not matter in practice.
    """
    _log.error(f"mcp_{tool}_failed", error=str(e), exc_info=True)
    text = str(e)

    from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER  # noqa: PLC0415 — deferred import; only paid on the (rare) error path

    if SESSION_UNAUTHORIZED_MARKER in text:
        return (
            f"Error: {text}\n"
            "The MCP session's T1 (scratch) session token is no longer valid, "
            "AND the automatic self-heal already failed: on this 401 the store "
            "re-read the owner-republished session lease and found no fresh "
            "token to adopt (nexus-g5hzk — no lease, expired, or unchanged). "
            "That means the token's owner incarnation is gone or its refresh "
            "loop died — reconnect the conexus MCP/extension so a fresh "
            "session-scoped token is minted. A bare CLI self-heals on its "
            "next invocation; a live MCP session can adopt a rotated token "
            "from the lease but cannot MINT one mid-conversation."
        )
    if isinstance(e, (ConnectionError, TimeoutError)) or any(
        m in text.lower() for m in _CONNECTION_ERROR_MARKERS
    ):
        return (
            f"Error: {text}\n"
            "The nexus backing service may be unreachable — check the daemon is "
            "running with `nx doctor` (and `nx daemon service status`)."
        )
    return f"Error: {text}"


#: Generous backstop for an assembled MCP text result (nexus-2xjge audit).
#: Claude Code 2.1.91's harness-side ``_meta.maxResultSizeChars`` truncates
#: SILENTLY (no marker, house doctrine forbids that — see the bead), so this
#: module caps explicitly instead, with a visible trailing marker.
#:
#: Basis: none of this module's paginated text tools clamp the caller-
#: supplied ``limit`` (or, for ``store_list(docs=True)``/``memory_search``/
#: ``plan_search``, some fetch the FULL matching set before any pagination
#: at all) — and the audit found the backing pgvector engine does not
#: either: ``PgVectorRepository#searchWithTokens``'s own javadoc states "No
#: upper bound is applied to nResults by design: the result-size caps the
#: Chroma path enforces ... fall away with pgvector." So ``MAX_QUERY_
#: RESULTS`` (300) — the value CLAUDE.md and this module's own comments
#: elsewhere document as the paging ceiling — is a ChromaDB-era quota that
#: no longer reaches the live query path; a caller-supplied ``limit`` can
#: scale a response arbitrarily. Value: a 300-row conceptual page x ~500
#: chars/row (the largest observed per-row shape, ``query()``'s bib-
#: enriched document block) ~= 150_000 chars; rounded up with real headroom
#: to 250_000 so no realistic paged response is ever touched.
_TEXT_RESULT_CAP_CHARS = 250_000


def _cap_text_result(text: str, tool: str, cap: "int | None" = None) -> str:
    """Truncate an assembled MCP text result WITH a visible trailing marker.

    House doctrine forbids silent truncation (nexus-2xjge): never returns
    something that merely LOOKS complete. The marker is unconditionally the
    last thing in the returned string — never prepended — so it can never
    be mistaken for part of the result and never collides with a leading-
    marker contract elsewhere (e.g. ``nx_answer``'s budget-exhaustion
    prefix, which callers key on via ``str.startswith``).

    ``cap`` defaults to ``None`` and is resolved against the *module*
    global ``_TEXT_RESULT_CAP_CHARS`` INSIDE the function body (not as a
    bound default parameter) so a test can ``monkeypatch.setattr(core,
    "_TEXT_RESULT_CAP_CHARS", N)`` and have it take effect immediately.
    """
    if cap is None:
        cap = _TEXT_RESULT_CAP_CHARS
    if len(text) <= cap:
        return text
    dropped = len(text) - cap
    return (
        f"{text[:cap]}"
        f"\n\n[{tool}: result capped at {cap} chars; {dropped} chars "
        f"dropped — narrow the query, lower limit, or page with offset=...]"
    )


#: Process-local HookRegistry constructed at MCP-server startup.
#: The MCP server is a long-running entry point — the registry's
#: lifecycle matches the server process. ``install_default_hooks``
#: wires the load-bearing default consumers (chash, taxonomy, manifest,
#: aspect-extraction).
_hooks = _HookRegistry()
_install_default_hooks(_hooks)

# ── T1 session lifecycle (RDR-105 P4 → RDR-155 P4b) ─────────────────────────
#
# RDR-155 P4b: the chroma-backed T1 branches (env-inherit, isolated
# skip-spawn, owned spawn+publish — the former Branches 1-3) are retired
# with the chroma substrate. RDR-158 P3 (nexus-7bomn) then retired the
# non-service tail as well: Branch 0 — the SERVICE-backed T1 session path
# (mint / borrow / inherit a session token against the storage service) —
# is the ONLY path; a stranded =sqlite export hard-errors at the lifespan's
# validation call. NX_T1_ISOLATED is retired outright (nexus-4lkmz, T1 is
# PG-only) — setting it now hard-fails in nexus.db.t1, no opt-out.
#
# Cleanup is idempotent across three sites: the lifespan async finally
# (HTTP/SSE clean exit + clean stdin EOF on stdio), _sigterm_handler
# (stdio SIGTERM where anyio does not install a SIGTERM handler), and
# atexit (belt-and-braces). _SHUTDOWN_IN_FLIGHT prevents re-entry from
# a signal handler that interrupted an in-flight teardown.

import os as _os

#: nexus-5daww: module-scope state for the SERVICE-backed T1 session minted
#: by the lifespan Branch 0 (Postgres-service path). Populated with
#: ``{"session_id": ...}`` right after a successful mint, cleared by
#: :func:`_t1_session_shutdown`. Exists so the SIGTERM / atexit path
#: (which cannot resume the paused lifespan generator past its ``yield``)
#: can still revoke the minted token and clear its lease file instead of
#: leaking both.
_OWNED_T1_SESSION: dict[str, Any] = {}

#: Sticky flag set by :func:`_t1_shutdown` on first entry so a
#: signal arriving mid-cleanup (the production stdin-EOF + SIGTERM
#: race that produced spurious ``mcp_server_crashed`` events on every
#: clean shutdown post-4.12.0) can short-circuit instead of racing
#: the in-flight teardown. Once set, never cleared: shutdown is
#: one-shot per process.
_SHUTDOWN_IN_FLIGHT: bool = False

#: nexus-ngcpo Finding 1: the running Branch-0 (SERVICE-backed) T1 session
#: TOKEN refresh task -- Branch 0's counterpart to ``_T1_HEARTBEAT_TASK``
#: above. Created right after a successful mint (never for a
#: borrowed/inherited session -- see the borrow-path commentary in
#: ``_t1_lifespan`` for why only the minting OWNER ever refreshes),
#: cancelled in Branch 0's own ``finally`` BEFORE the session is closed
#: (mirrors the RDR-129 early-stop ordering already used for
#: ``_T1_HEARTBEAT_TASK``). ``None`` when no owned SERVICE session is live.
_T1_SESSION_REFRESH_TASK: Any = None

#: nexus-ngcpo: fraction of the minted token's ACTUAL TTL (from the mint
#: response's ``expires_in_seconds``, not an assumed constant) at which
#: Branch 0 proactively re-mints its own session token. Refreshing at half
#: the TTL gives a full half-TTL safety margin: even a single missed tick
#: (a transient service blip) still leaves an entire half-TTL window to
#: retry before the OLD token would actually expire.
_T1_SESSION_REFRESH_FRACTION: float = 0.5

#: Floor on the computed refresh interval so a very short token TTL (test
#: fixtures, or a future low-TTL server config) can never drive a
#: pathologically tight refresh loop. No ceiling is needed -- even the
#: server's 24h default TTL only yields a 12h interval.
_T1_SESSION_REFRESH_MIN_INTERVAL_S: float = 5.0

#: Defensive fallback TTL (seconds) used to size the refresh interval if a
#: mint response is somehow missing ``expires_in_seconds``. Mirrors the
#: service's ``SessionTokenHandler.DEFAULT_TTL_SECONDS`` (24h) and
#: ``nexus.db.t1._T1_SESSION_LEASE_DEFAULT_TTL_SECONDS``.
_T1_SESSION_DEFAULT_TTL_SECONDS: float = 86_400.0

#: nexus-brw1s (GH #1405, field report stevengharris): Branch 0's deferred-mint
#: state. Non-empty iff the storage service was unreachable when the lifespan
#: tried to mint the T1 session token — in which case the server STARTS ANYWAY
#: (the old behavior raised, the RuntimeError escaped the stdio TaskGroup, the
#: whole MCP server died, and Claude Code cached the dead connection for the
#: session's entire lifetime: every nexus tool gone because a SCRATCH
#: precondition failed). Keys: session_id, config_dir, loop (the lifespan's
#: event loop, for scheduling the refresh task from a tool worker thread).
#: Cleared on successful deferred mint or lifespan exit.
_DEFERRED_T1_MINT: dict[str, Any] = {}


def _start_t1_refresh_task(session_id: str, interval: float) -> None:
    """Create the refresh task on the CURRENT loop (module-level so the
    deferred-mint hook can schedule it via ``call_soon_threadsafe``)."""
    import asyncio  # noqa: PLC0415 — stdlib, branch-local

    global _T1_SESSION_REFRESH_TASK
    _T1_SESSION_REFRESH_TASK = asyncio.create_task(
        _t1_session_refresh_loop(session_id, interval)
    )


class T1UnavailableThisProcessError(RuntimeError):
    """Raised (via the T1 pre-init hook) when this MCP process has no
    resolvable session id to bind a T1 session to.

    nexus-4lkmz decision 2 (LOCKED 2026-08-07): replaces the pre-fix
    behaviour of forcing ``NX_T1_ISOLATED=1`` — which, pre-retirement,
    silently handed this process a private in-process T1Database. That
    escape hatch is gone (T1 is PG-only); the honest replacement is a
    FAIL-LOUD, per-call, named error — never a shared identity (the
    nexus-rn3wo.1 security property this whole branch exists to
    preserve: an unresolvable-session MCP process must never be pulled
    into the SAME shared CLI-dedicated identity as every bare `nx
    scratch` invocation) and never a silent private store.

    Scoped to T1 ONLY, same "a T1-only failure must cost T1 only"
    posture as the deferred-mint hook above: every other nexus tool
    (search, memory, store, catalog) is unaffected, and the whole MCP
    server does not crash — only T1-touching calls raise, and every
    later call retries the same check (nothing is cached).
    """


def _raise_t1_unavailable_this_process() -> None:
    """Pre-init hook body for the no-resolvable-session-id case.

    Registered by :func:`_t1_lifespan` Branch 0 in place of the retired
    ``NX_T1_ISOLATED=1`` force. Raises unconditionally — there is no
    mint to retry, since without a resolvable session id there is
    nothing to mint a token FOR (minting a shared "unknown" row would
    let concurrent unresolvable-session MCPs collide on the (tenant,
    session_id) UPSERT, each invalidating the other's token — the same
    hazard the surrounding code already documents).
    """
    raise T1UnavailableThisProcessError(
        "T1 unavailable this process: no resolvable session id for this "
        "MCP server instance (neither NX_SESSION_ID nor "
        "CLAUDE_CODE_SESSION_ID resolved one). This affects T1 scratch "
        "only — every other nexus tool (search, memory, store, catalog) "
        "is unaffected. A sub-agent that inherited a LIVE minted token "
        "from its parent is unaffected too (checked before this hook "
        "would ever be consulted). Remedy: reconnect the conexus "
        "MCP/extension from a Claude Code session that stamps a "
        "resolvable session id, or run `nx doctor --check-t1`."
    )


def _retry_deferred_t1_mint() -> None:
    """Complete a startup-deferred T1 session mint, on first T1 use.

    nexus-brw1s: registered with :func:`nexus.mcp_infra.set_t1_pre_init_hook`
    by the lifespan when the startup mint failed; runs in a tool worker thread
    under ``mcp_infra``'s T1 lock, BEFORE the T1 handle is first constructed.

    Success: the same flock-guarded mint-or-borrow the lifespan uses (so a
    concurrent recoverer's fresh lease is borrowed, never rotated — the
    nexus-jwqjm race discipline holds on this path too), the same env vars,
    the same ownership bookkeeping, and the refresh task scheduled onto the
    lifespan's loop from this thread. The hook then unregisters itself; the
    session behaves as if the mint had succeeded at startup.

    Failure: an actionable per-call error. Nothing is cached and the hook
    stays registered, so every subsequent T1-touching call retries — the walk
    to recovery is "start the service", not "restart your Claude session".
    Phase E require-minted is intact: there is still no bare-id fallback and
    no CLI-dedicated identity sharing; T1 is simply unavailable, loudly, per
    call, while every non-T1 tool keeps working.
    """
    state = dict(_DEFERRED_T1_MINT)
    if not state:
        return  # completed by a concurrent call while we waited on the lock
    _log = structlog.get_logger(__name__)
    from nexus.db.t1 import _lock_guarded_mint_or_borrow  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

    session_id = state["session_id"]
    try:
        token, minted_fresh, mint_ttl = _lock_guarded_mint_or_borrow(
            session_id, state["config_dir"]
        )
    except Exception as exc:
        _log.warning(
            "t1_deferred_mint_retry_failed", session_id=session_id, error=str(exc)
        )
        raise RuntimeError(
            "T1 scratch is unavailable: the storage service could not mint a "
            f"session token ({exc}). Start it (`nx daemon service start`) and "
            "retry — this failure affects T1 scratch only. (In cloud mode T3 "
            "has a separate probe-cache limitation: nexus-5t1jp.)"
        ) from exc

    if not _DEFERRED_T1_MINT:
        # Shutdown sentinel (code review LOW-1): the lifespan finally CLEARED
        # the deferred state while this mint was in flight — the session is
        # tearing down. Committing now would set ownership AFTER the revoke
        # already ran (the nexus-5daww token-leak class) and schedule a refresh
        # task past its cancel. Abort instead: raise so get_t1() caches
        # nothing and constructs nothing (constructing env-less here would
        # route into the shared CLI-dedicated identity). The just-minted token
        # is left to the service's 24h TTL sweep — the same best-effort
        # backstop every other leaked-token path already relies on.
        _log.warning(
            "t1_deferred_mint_completed_after_shutdown", session_id=session_id
        )
        raise RuntimeError(
            "T1 session mint completed during MCP shutdown; discarding — the "
            "session is ending."
        )
    _os.environ["NX_T1_SESSION"] = token
    _os.environ["NX_T1_SESSION_ID"] = session_id
    if minted_fresh:
        _OWNED_T1_SESSION["session_id"] = session_id
        ttl = mint_ttl if mint_ttl is not None else _T1_SESSION_DEFAULT_TTL_SECONDS
        interval = max(
            ttl * _T1_SESSION_REFRESH_FRACTION, _T1_SESSION_REFRESH_MIN_INTERVAL_S
        )
        loop = state.get("loop")
        try:
            loop.call_soon_threadsafe(_start_t1_refresh_task, session_id, interval)
        except Exception as exc:  # noqa: BLE001 — refresh loss degrades to TTL expiry + 401 self-heal, never fails a successful mint
            _log.warning(
                "t1_deferred_mint_refresh_not_scheduled",
                session_id=session_id,
                error=str(exc),
            )
        _log.info("t1_session_isolation_minted", session_id=session_id, deferred=True)
    else:
        _log.info("t1_session_leased_after_deferred_mint", session_id=session_id)

    _DEFERRED_T1_MINT.clear()
    from nexus import mcp_infra  # noqa: PLC0415 — deferred to avoid import cycle at module load

    mcp_infra.set_t1_pre_init_hook(None)


async def _t1_session_refresh_loop(session_id: str, interval: float) -> None:
    """Periodically re-mint Branch 0's OWN SERVICE-backed T1 session token.

    nexus-ngcpo Finding 1: Branch 0 previously minted a session token ONCE
    at MCP startup (``HttpTokenStore.start_session``) and never again. The
    server's default token TTL is 24h (``SessionTokenHandler.
    DEFAULT_TTL_SECONDS``); any session alive past that wall-clock boundary
    -- a long dev session, a scheduled/cron agent (the ``schedule`` skill),
    a laptop-sleep-preserved session -- would have every subsequent T1
    put/get/search/list start 401ing for the rest of the process, with no
    self-heal on this path (contrast the CLI-dedicated path's
    ``_CliDedicatedScratchStore``, which retries once on a 401). This loop
    is the fix: mirrors Branch 3's ``_t1_heartbeat_loop`` re-assert pattern
    (RDR-149 P4), adapted to a token mint instead of a lease re-stamp.

    Re-minting is safe here SPECIFICALLY because this loop only ever
    re-mints the session id THIS process itself minted and recorded into
    ``_OWNED_T1_SESSION`` (started immediately after that mint, in the
    caller) -- never a borrowed/inherited session id. Re-minting a session
    id this process does NOT own would rotate (``HttpTokenStore.
    start_session`` is ``ON CONFLICT DO UPDATE``) another owner's live
    token out from under it -- exactly the hazard the nexus-5daww
    commentary elsewhere in this module documents at length. A borrow-path
    reader (the lease self-check below) deliberately never starts this
    loop, both because it does not own the session and because two
    processes independently re-minting the SAME session id would race each
    other's mint.

    Also republishes the lease file with the fresh token + a fresh expiry
    (nexus-ngcpo Finding 2/3) so sibling/detached readers -- the SessionEnd
    hook, a nested MCP that resolves the same session id -- keep seeing a
    live, borrowable lease for as long as this owner keeps refreshing. The
    "recovery" case (Finding 3: a lease nobody is refreshing any more) is
    therefore only ever reached once refreshing has ACTUALLY stopped, i.e.
    this owner process has died -- see ``read_t1_session_lease``'s
    freshness check and the borrow-path commentary below for how a
    subsequent reader detects that and mints fresh instead of borrowing a
    lease no one is maintaining.

    A tick that raises (a transient mint failure / network blip) is logged
    at warning and the loop continues -- one missed refresh still leaves
    roughly half the TTL as a safety margin before the OLD token actually
    expires. Only cancellation (clean shutdown, see
    ``_cancel_t1_session_refresh_task``) stops the loop.
    """
    import asyncio  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site

    import structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
    _rf_log = structlog.get_logger("nexus.mcp.core")
    while True:
        await asyncio.sleep(interval)
        try:
            from nexus.db.t2.http_token_store import HttpTokenStore  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
            with HttpTokenStore(prefer_data_token=True) as _ts:  # nexus-maf9l: armed boxes mint sessions with the data token
                _minted = _ts.start_session(session_id)
            _os.environ["NX_T1_SESSION"] = _minted["session_token"]
            try:
                from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
                from nexus.db.t1 import publish_t1_session_lease  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
                publish_t1_session_lease(
                    session_id,
                    _minted["session_token"],
                    nexus_config_dir(),
                    ttl_seconds=float(
                        _minted.get("expires_in_seconds") or _T1_SESSION_DEFAULT_TTL_SECONDS
                    ),
                )
            except Exception as _exc:  # noqa: BLE001 — boundary catch; best-effort lease republish, must not crash the refresh loop
                _rf_log.warning(
                    "t1_session_refresh_lease_publish_failed",
                    session_id=session_id, error=str(_exc),
                )
            _rf_log.info("t1_session_token_refreshed", session_id=session_id)
        except Exception as exc:  # noqa: BLE001 — never let a transient refresh failure kill the loop
            _rf_log.warning("t1_session_refresh_failed", session_id=session_id, error=str(exc))


async def _cancel_t1_session_refresh_task() -> None:
    """Cancel and await the T1 session refresh task if one is running. Idempotent."""
    import asyncio  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site
    import contextlib  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site

    global _T1_SESSION_REFRESH_TASK
    task = _T1_SESSION_REFRESH_TASK
    _T1_SESSION_REFRESH_TASK = None
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


# ── T1 handoff-marker watcher (nexus-d76vc) ──────────────────────────────────
#
# The other half of the aj564 split-brain fix. The SessionStart hook
# (source=clear|resume, see nexus.hooks._write_t1_handoff_markers) writes
# ~/.config/nexus/t1_handoff.<mcp_pid> naming the transcript's NEW session
# id. This loop is a SEPARATE, independent poll -- deliberately not
# piggybacked on `_t1_session_refresh_loop` above, whose interval is half
# the token TTL (hours, by default) and which does not even run on the
# USE_INHERITED/USE_LEASED branches. A handoff needs to be noticed promptly
# regardless of which branch this MCP server's T1 routing took.

#: Poll interval for the handoff-marker watcher. Independent of (and much
#: tighter than) `_T1_SESSION_REFRESH_MIN_INTERVAL_S` above, which bounds a
#: token-TTL-driven interval measured in hours -- a user expects `/clear` or
#: `/resume` to take effect promptly, not after a multi-hour refresh tick.
_T1_HANDOFF_WATCH_INTERVAL_S: float = 5.0

#: This process's own start time, stamped at import (module import happens
#: once per MCP process, at or before `_t1_lifespan` first runs -- close
#: enough to "server start" for the freshness check below, and simpler than
#: threading an explicit timestamp through the lifespan's many branches).
#: A marker whose `written_at` predates this can only be a leftover from a
#: PRIOR process that happened to reuse this pid (or a clock going
#: backwards) -- never a legitimate handoff for THIS incarnation.
_T1_SERVER_START_TIME: float = time.time()

#: The running handoff-watch asyncio task, mirrors `_T1_SESSION_REFRESH_TASK`
#: above. Started unconditionally near the top of `_t1_lifespan` (every
#: routing branch gets a watcher); cancelled at every one of that function's
#: exit points.
_T1_HANDOFF_WATCH_TASK: Any = None

#: Wall-clock budget for the re-lease mint-or-borrow call (nexus-d76vc
#: fix-round IMPORTANT/event-loop-block finding, code-review-expert pass
#: 1). Bounds the flock-wait leg of `_lock_guarded_mint_or_borrow` (the
#: SAME `deadline=` parameter the by875-hardened CLI path already uses);
#: the mint HTTP call's own timeout, once started, is `mint_t1_session_
#: token`'s pre-existing behavior, unchanged by this bead either way.
_T1_HANDOFF_MINT_DEADLINE_S: float = 30.0


def _start_t1_handoff_watch_task() -> None:
    """Create the handoff-watch task on the CURRENT (running) loop.

    Called synchronously from inside `_t1_lifespan`'s own async-generator
    body, so `asyncio.get_running_loop()` context already exists -- no
    `call_soon_threadsafe` indirection needed here (contrast
    `_start_t1_refresh_task`, which IS scheduled that way because its other
    caller, `_retry_deferred_t1_mint`, runs in a worker thread).
    """
    import asyncio  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site

    global _T1_HANDOFF_WATCH_TASK
    _T1_HANDOFF_WATCH_TASK = asyncio.create_task(_t1_handoff_watch_loop())


async def _cancel_t1_handoff_watch_task() -> None:
    """Cancel and await the handoff-watch task if one is running. Idempotent."""
    import asyncio  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site
    import contextlib  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site

    global _T1_HANDOFF_WATCH_TASK
    task = _T1_HANDOFF_WATCH_TASK
    _T1_HANDOFF_WATCH_TASK = None
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def _t1_handoff_watch_loop() -> None:
    """Poll for a consumed T1 handoff marker every tick; re-lease on one.

    Runs for the server's entire lifetime once started (see
    `_start_t1_handoff_watch_task`'s call site in `_t1_lifespan`).  A tick
    that raises (marker corruption not already handled by
    `read_handoff_marker`'s own fail-safe parsing, a transient mint
    failure) is logged at warning and the loop continues — never let a
    single bad tick kill the watcher for the rest of the process
    lifetime (mirrors `_t1_session_refresh_loop`'s own error handling).
    """
    import asyncio  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site

    import structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
    _hw_log = structlog.get_logger("nexus.mcp.core")
    mcp_pid = _os.getpid()
    while True:
        await asyncio.sleep(_T1_HANDOFF_WATCH_INTERVAL_S)
        try:
            await _t1_handoff_tick(mcp_pid, _hw_log)
        except Exception as exc:  # noqa: BLE001 — never let a tick failure kill the watcher for the rest of the process lifetime
            _hw_log.warning("t1_handoff_watch_tick_failed", error=str(exc))


async def _t1_handoff_tick(mcp_pid: int, log: Any) -> None:
    """One handoff-watch step: claim, verify, and either re-lease or reject.

    `async` (nexus-d76vc fix-round IMPORTANT/event-loop-block finding,
    code-review-expert pass 1): the re-lease mint-or-borrow below is
    blocking (flock + synchronous HTTP) and is offloaded to a worker
    thread via `asyncio.to_thread` so a stuck mint/lock cannot freeze the
    event loop -- and therefore every OTHER tool call this MCP server is
    serving -- for its duration. See that call site for the bounded
    `deadline=` this is paired with.

    Claim-first (nexus-d76vc fix-round IMPORTANT/TOCTOU finding): the
    live marker is atomically RENAMED to a tick-private claimed path
    (`claim_handoff_marker`) before any parsing. The prior shape read the
    live marker, processed it (a blocking, possibly slow mint), then
    unconditionally deleted whatever was CURRENTLY at the live path --
    silently dropping a second `/clear` that landed a fresh marker there
    mid-tick. Claiming first means this tick only ever touches the
    specific marker it claimed; a fresh live-path write during processing
    survives untouched for the next tick.

    Rejection posture (nexus-d76vc TESTS note, resolved): an
    ancestry-mismatched, malformed, or stale marker is DELETED (the
    claimed copy), not left in place. Leaving it would retry the SAME
    rejected marker on every subsequent tick forever with no way to
    distinguish "still invalid" from "a fresh legitimate marker just got
    written under the same name" — deleting means a spoof/stale marker
    gets exactly one silent rejection (loudly logged) and cannot wedge
    the watcher into repeatedly re-warning about a marker nobody will
    ever fix, while a GENUINELY new marker for the same pid (a real
    subsequent /clear) simply gets written fresh at the LIVE path and
    picked up on its own next tick.

    Re-lease steps, mirroring `_t1_lifespan`'s own MINT branch: mint-or-
    borrow a token for the new session id (the same flock-guarded helper,
    so a concurrent sibling MCP re-leasing the SAME new session id
    converges to one mint), stop refreshing the OLD lease BEFORE adopting
    the new identity (so the cancelled task can never race a re-mint
    against the swap), swap the env vars, clear any stale pre-init hook
    state (see the CRITICAL-fix comment at that call site below), drop
    the T1 singleton (so the NEXT tool call reconstructs against the new
    scope -- no per-call re-resolve, the scope only moves on a consumed
    marker, per MUST-HOLD 3), and — only if THIS call won the mint (never
    on a borrow, exactly like the original mint branch) — take ownership
    and start a fresh refresh loop for the new session id.

    Deliberately does NOT touch the old session's T1 rows or explicitly
    revoke its token: they strand under the old session id and age out
    via the T1 TTL sweep (design decision, nexus-d76vc bead body) — migrating
    would silently merge two conversations the user explicitly separated
    with `/clear`.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
    from nexus.daemon.t1_handoff import (  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
        claim_handoff_marker,
        consume_claimed_marker,
        read_claimed_marker,
        write_handoff_marker_if_absent,
    )

    config_dir = nexus_config_dir()

    claimed_path = claim_handoff_marker(mcp_pid, config_dir)
    if claimed_path is None:
        return  # no live marker -- steady state, every tick, stay silent

    marker = read_claimed_marker(claimed_path)
    if marker is None:
        log.warning(
            "t1_handoff_marker_rejected", reason="malformed_marker", mcp_pid=mcp_pid,
        )
        consume_claimed_marker(claimed_path)
        return

    from nexus.session import find_immediate_claude_pid  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

    # Re-derive OUR OWN ancestry independently rather than trusting the
    # marker's claimed claude_pid alone (MUST-HOLD rn3wo.1 / the writer's
    # own ancestry check is defense-in-depth, not the only check — see
    # nexus.hooks._write_t1_handoff_markers's docstring).
    own_claude_pid = find_immediate_claude_pid(start_pid=mcp_pid)
    if own_claude_pid <= 0 or own_claude_pid != marker.claude_pid:
        log.warning(
            "t1_handoff_marker_rejected",
            reason="ancestry_mismatch",
            mcp_pid=mcp_pid,
            marker_claude_pid=marker.claude_pid,
            own_claude_pid=own_claude_pid,
        )
        consume_claimed_marker(claimed_path)
        return

    new_session_id = marker.new_session_id
    if not new_session_id or new_session_id == "unknown":
        log.warning(
            "t1_handoff_marker_rejected",
            reason="malformed_session_id",
            mcp_pid=mcp_pid,
        )
        consume_claimed_marker(claimed_path)
        return

    if marker.written_at < _T1_SERVER_START_TIME:
        log.warning(
            "t1_handoff_marker_rejected",
            reason="stale_marker",
            mcp_pid=mcp_pid,
            written_at=marker.written_at,
            server_start=_T1_SERVER_START_TIME,
        )
        consume_claimed_marker(claimed_path)
        return

    old_session_id = _os.environ.get("NX_T1_SESSION_ID", "").strip() or None
    if old_session_id == new_session_id:
        # Nothing to do (e.g. a re-tick that raced the previous one's own
        # consume, or a resume that lands back on the id already active).
        # Consume so the marker never lingers past the tick that saw it.
        consume_claimed_marker(claimed_path)
        return

    import asyncio  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site

    from nexus.db.t1 import _lock_guarded_mint_or_borrow  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

    try:
        token, minted_fresh, mint_ttl = await asyncio.to_thread(
            _lock_guarded_mint_or_borrow,
            new_session_id, config_dir,
            context="T1 handoff re-lease",
            deadline=time.monotonic() + _T1_HANDOFF_MINT_DEADLINE_S,
        )
    except Exception as exc:  # noqa: BLE001 — a re-lease failure must not crash the watcher; the marker is REINSTATED (not left claimed-and-orphaned) so a transient failure retries next tick
        log.warning(
            "t1_handoff_release_failed",
            mcp_pid=mcp_pid, new_session_id=new_session_id, error=str(exc),
        )
        # Re-instate at the LIVE path: having claimed the marker away
        # from the live name before attempting the mint, we must put it
        # back on failure or the handoff is lost forever (no future tick
        # ever re-examines the claimed-path name -- claim_handoff_marker
        # only ever looks at the LIVE path). fix-round 2 (code-review-
        # expert IMPORTANT): this must NOT be an unconditional overwrite —
        # ordinary write_handoff_marker's last-write-wins policy is correct
        # for a genuine WRITER (its own write is always the newest truth
        # at the moment it happens) but backwards here: the reinstated
        # content is OLDER truth arriving late (the failed mint took real
        # wall-clock time), so if a second /clear published a genuinely
        # newer marker at the live path WHILE this mint was in flight, an
        # unconditional overwrite would let the STALE reinstate clobber
        # it — the exact data-loss class the claim-first design exists to
        # prevent, just relocated to this call site.
        # write_handoff_marker_if_absent is the no-clobber primitive:
        # publishes ONLY if the live path is still empty, atomically (no
        # separate exists-check-then-write TOCTOU window — see its
        # docstring). If it returns False, a newer marker already won;
        # drop the stale one loudly rather than silently.
        reinstated = write_handoff_marker_if_absent(
            mcp_pid, new_session_id=marker.new_session_id,
            claude_pid=marker.claude_pid, config_dir=config_dir,
            clock=lambda: marker.written_at,
        )
        if not reinstated:
            log.warning(
                "t1_handoff_marker_reinstate_superseded",
                mcp_pid=mcp_pid,
                stale_new_session_id=marker.new_session_id,
            )
        consume_claimed_marker(claimed_path)
        return

    # Stop refreshing the OLD lease BEFORE adopting the new identity, so
    # the cancelled task can never race a re-mint against the swap below
    # (mirrors the ordering `_t1_lifespan`'s own teardown already uses:
    # cancel the refresh task before touching session state).
    global _T1_SESSION_REFRESH_TASK
    if _T1_SESSION_REFRESH_TASK is not None:
        _T1_SESSION_REFRESH_TASK.cancel()
        _T1_SESSION_REFRESH_TASK = None
    _OWNED_T1_SESSION.clear()

    from nexus import mcp_infra  # noqa: PLC0415 — deferred to avoid import cycle at module load

    # nexus-d76vc fix-round CRITICAL (code-review-expert pass 1): a
    # successful re-lease means THIS process now has a good, resolvable
    # session id and a fresh token -- any pre-init hook (or brw1s
    # deferred-mint state) registered by an EARLIER lifespan branch is
    # now stale and must never fire again:
    #   - a stale brw1s `_retry_deferred_t1_mint` hook (registered when
    #     the storage service was unreachable at MCP startup, for the
    #     OLD pre-handoff session) would re-mint/borrow for that OLD
    #     session id on the very next `get_t1()` call and silently
    #     overwrite NX_T1_SESSION_ID back to it -- reverting this
    #     handoff with no error, the exact split-brain this bead exists
    #     to close.
    #   - a stale 4lkmz `_raise_t1_unavailable_this_process` hook
    #     (registered when this process started with no resolvable
    #     session id at all) would keep raising
    #     `T1UnavailableThisProcessError` forever, even though the
    #     handoff just supplied a perfectly good session.
    # A successful re-lease can never itself warrant RE-registering a
    # hook (it always leaves NX_T1_SESSION/NX_T1_SESSION_ID set to a
    # freshly minted-or-borrowed, live pair), so clearing unconditionally
    # is correct in every case, not just the two traced above -- there is
    # nothing to assert beyond that unconditional clear.
    #
    # fix-round 2 (code-review-expert LOW, hardening): this clear now
    # happens BEFORE the env-var swap below, not after. Neither `get_t1()`
    # (mcp_infra.py) nor this tick holds `_t1_lock` across the WHOLE
    # sequence — `get_t1()` only takes it around its own check-hook/
    # construct step — so a concurrent OS-thread tool call landing in the
    # gap between two of this function's statements can still observe a
    # PARTIALLY-applied release. Clearing the hook first closes the
    # specific window this review flagged (a concurrent get_t1() between
    # the swap and the clear would have seen the NEW env vars but still
    # run the STALE hook, which -- for the brw1s case -- reads and acts on
    # the OLD `_DEFERRED_T1_MINT["session_id"]`, not the new env, so it
    # would have reverted the swap moments after it happened). A narrower
    # residual remains structurally (a concurrent get_t1() landing BEFORE
    # this clear even starts still races it, since nothing here is
    # lock-held end-to-end) — accepted as low severity per review: it
    # requires a stale hook to still be registered AND a concurrent
    # first-ever T1 touch to land in a sub-millisecond window, and the
    # worst case is one extra failed/reverted call that self-heals on the
    # very next touch once this clear completes, not a standing split-brain.
    _DEFERRED_T1_MINT.clear()
    mcp_infra.set_t1_pre_init_hook(None)

    _os.environ["NX_T1_SESSION"] = token
    _os.environ["NX_T1_SESSION_ID"] = new_session_id

    # Freeze the NEW scope for subsequent T1 calls (MUST-HOLD 3): drop the
    # cached singleton so the next `get_t1()` call reconstructs against the
    # env vars just swapped above, then stays frozen there until the next
    # consumed marker -- never a per-call re-resolve.
    mcp_infra.reset_t1_for_release()

    if minted_fresh:
        _OWNED_T1_SESSION["session_id"] = new_session_id
        ttl = mint_ttl if mint_ttl is not None else _T1_SESSION_DEFAULT_TTL_SECONDS
        interval = max(
            ttl * _T1_SESSION_REFRESH_FRACTION, _T1_SESSION_REFRESH_MIN_INTERVAL_S
        )
        _T1_SESSION_REFRESH_TASK = asyncio.create_task(
            _t1_session_refresh_loop(new_session_id, interval)
        )

    consume_claimed_marker(claimed_path)
    log.info(
        "t1_handoff_released",
        mcp_pid=mcp_pid,
        old_session_id=old_session_id,
        new_session_id=new_session_id,
        minted_fresh=minted_fresh,
    )


def _tcp_probe_alive(host: str, port: int, timeout: float = 0.5) -> bool:
    """Return True if a TCP connection to ``(host, port)`` succeeds.

    Retained from the pre-RDR-105 module for use by ``nx doctor`` and
    other diagnostic surfaces that probe a chroma address without
    constructing a full ``T1Database``.
    """
    import socket  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


from contextlib import asynccontextmanager
from pathlib import Path as _Path


@asynccontextmanager
async def _t1_lifespan(_app: Any):
    """T1 session lifespan (RDR-105 P4, reshaped at RDR-155 P4b / RDR-158 P3).

    Branch 0 (service) is the only branch: route T1 through
    HttpScratchStore — mint / borrow / inherit a per-session token
    against the storage service; close the session + revoke the token on
    exit. The chroma-backed Branches 1-3 died with the chroma substrate,
    and the non-service plain-yield tail died with the =sqlite opt-out
    (RDR-158 P3, nexus-7bomn — a stranded export hard-errors at the
    validation call below).

    Cleanup is idempotent across three sites:

    * The lifespan ``async finally`` (HTTP/SSE transports and the
      clean-stdin-EOF path on stdio).
    * :func:`_sigterm_handler` (stdio SIGTERM where anyio does not
      install a SIGTERM handler).
    * :mod:`atexit` (belt-and-braces for clean SystemExit paths).
    """
    # Branch 0 (RDR-152 bead nexus-gmiaf.13): Postgres service path.
    # NX_STORAGE_BACKEND_T1=service (or global NX_STORAGE_BACKEND=service)
    # routes T1 through HttpScratchStore. Chroma is NOT spawned; the session is
    # closed on exit via HttpScratchStore.close_session() so the UNLOGGED table
    # is reaped promptly rather than waiting for the 24-h TTL sweep backstop.
    # RDR-158 P3 (nexus-7bomn): validation only — the non-service tail of
    # this lifespan died with the =sqlite opt-out; a stale
    # NX_STORAGE_BACKEND[_T1]=sqlite export hard-errors here with the
    # stranded-install redirect instead of being silently ignored.
    from nexus.db.storage_mode import storage_backend_for  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
    storage_backend_for("t1")
    import structlog as _structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
    _svc_log = _structlog.get_logger(__name__)
    _svc_log.info("t1_service_path_active", backend="service")

    # nexus-d76vc: start the T1 handoff-marker watcher UNCONDITIONALLY,
    # before the routing decision below picks a branch. A handoff can
    # arrive at any point after this server starts -- including onto a
    # process that started with USE_INHERITED/USE_LEASED (no mint, no
    # refresh task of its own) or even the no-resolvable-session branch
    # further down -- so the watcher's lifetime must not depend on which
    # branch fires. See ``_t1_handoff_watch_loop``'s own docstring for the
    # full re-lease protocol; cancelled at every exit point below
    # (mirrors the multiple yield/return sites this lifespan already has).
    _start_t1_handoff_watch_task()

    # nexus-1si7z: tiers 1-2 (inherited-wins, then borrow-a-fresh-lease)
    # are the SAME decision get_t1_database() makes for the bare-CLI/
    # detached-process path (db/t1.py) -- both now call the ONE shared
    # implementation so they cannot silently diverge again. See
    # resolve_t1_routing_tiers's docstring for the full "why one
    # function, why not tier 3 too" reasoning.
    #
    # nexus-5daww (round-4 CRITICAL, USE_INHERITED case): an inherited,
    # ALREADY-LIVE token in NX_T1_SESSION -- e.g. a nested `nx-mcp`
    # subprocess spawned by operators.dispatch.claude_dispatch's
    # tool-granting env (_build_dispatch_env copies the PARENT's
    # os.environ verbatim except for a few explicitly-stripped keys;
    # NX_T1_SESSION/NX_T1_SESSION_ID were not among them) -- must be used
    # AS-IS: never re-minted (HttpTokenStore.start_session is ON
    # CONFLICT DO UPDATE and rotates, which would invalidate the owning
    # ancestor's live token out from under it) and never torn down by
    # this process on exit (it does not own the session).
    #
    # nexus-5daww defense-in-depth (USE_LEASED case): even without a
    # DIRECTLY-inherited NX_T1_SESSION (e.g.
    # operators.dispatch._build_dispatch_env's ephemeral/owned modes now
    # strip it -- see dispatch.py), the resolved session id may already
    # have a LIVE lease published by an ancestor's Branch 0 mint
    # (nexus-c8yvj's publish_t1_session_lease/read_t1_session_lease --
    # the SAME mechanism the SessionEnd hook and get_t1_database()'s
    # detached-process path already use to reach a live MCP session
    # without re-minting). Consult it BEFORE minting so a nested MCP
    # that resolves the SAME session id as a live ancestor (NX_SESSION_ID
    # is intentionally still passed through dispatch for attribution)
    # borrows the existing token instead of rotating it out from under
    # that ancestor.
    #
    # nexus-ngcpo Finding 2 (USE_LEASED / MINT split): resolve_t1_routing_
    # tiers's read_t1_session_lease call refuses to return a STALE lease
    # (past its stored expiry) -- MINT with a real session_id therefore
    # means either "no lease was ever published" OR "one was published
    # but nobody has refreshed it since it went stale" (i.e. its
    # original owner is presumably no longer alive/refreshing -- see
    # `_t1_session_refresh_loop`). Either way the mint branch below both
    # mints a fresh token for THIS process AND takes ownership
    # (`_OWNED_T1_SESSION`, refresh task, teardown) -- the Finding-3
    # "orphaned lease" recovery: it happens lazily, on the next BRANCH-0
    # process that resolves this session id and finds the lease no
    # longer trustworthy, not via any active monitoring.
    #
    # SCOPE OF THIS RECOVERY (do not overstate it): this is the
    # Branch-0-to-Branch-0 case only. `get_t1_database()`'s CLI/
    # detached-path tier-3 (its own MINT branch) does NOT retry-mint
    # against the resolved session id on a stale lease -- it falls
    # straight through to the disjoint, permanent CLI-dedicated identity
    # instead (by design -- see resolve_t1_routing_tiers's docstring).
    # So a detached SessionEnd-hook grandchild that finds a stale lease
    # does NOT reconnect to the original session's T1 state via this
    # mechanism; it silently degrades to the SAME best-effort "flush
    # skipped" outcome `hooks.session_end_flush` already documents as an
    # accepted, pre-existing race window. Only a fresh Branch-0 MCP
    # restart for the same session id gets the recovery described here.
    #
    # nexus-ngcpo Finding 3 (USE_LEASED specifically): when the lease IS
    # fresh we deliberately do NOT claim ownership here (no
    # `_OWNED_T1_SESSION`, no refresh task) -- a fresh lease means its
    # original owner is presumably still alive and actively refreshing
    # it (this is what makes it fresh), so this borrowing process
    # piggybacks on that owner's lifecycle rather than starting a
    # SECOND, competing refresh loop for the same session id (which
    # would race the real owner's own re-mint -- see
    # `_t1_session_refresh_loop`'s docstring). The formerly-"accepted"
    # residual here -- a long-lived borrower's one-shot token copy going
    # stale when the owner rotates -- is now HANDLED, reactively, one
    # layer down: HttpScratchStore._refresh_session_token_from_lease
    # re-reads this lease on a 401 and retries once (nexus-g5hzk). The
    # original acceptance rested on two premises the 2026-07-14 live
    # incident falsified: borrowers are NOT always short-lived (a
    # same-session RESUMED PEER MCP borrows and lives for hours), and
    # the refresh cadence is NOT >=12h (the deployed server TTL was 1h,
    # a 30-min rotation -- the borrower went dark 4 minutes after
    # borrowing). Re-READ on 401 (never re-mint) is exactly the remedy
    # this comment used to defer.
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
    from nexus.db.t1 import T1RoutingAction, resolve_t1_routing_tiers  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

    _decision = resolve_t1_routing_tiers(nexus_config_dir())

    if _decision.action == T1RoutingAction.USE_INHERITED:
        _svc_log.info(
            "t1_session_inherited_no_mint",
            session_id=_os.environ.get("NX_T1_SESSION_ID", "").strip(),
        )
        yield
        await _cancel_t1_handoff_watch_task()
        return

    if _decision.action == T1RoutingAction.USE_LEASED:
        _os.environ["NX_T1_SESSION"] = _decision.session_token
        _os.environ["NX_T1_SESSION_ID"] = _decision.session_id
        _svc_log.info("t1_session_leased_no_mint", session_id=_decision.session_id)
        yield
        await _cancel_t1_handoff_watch_task()
        return

    # T1RoutingAction.MINT. Phase D (bead nexus-gmiaf.32.4): mint a
    # per-session token at session start. Set NX_T1_SESSION to the
    # minted secret (the X-Nexus-T1-Session header value) and
    # NX_T1_SESSION_ID to the session id (body + flush-title).
    # Sub-agents inherit both via os.environ.
    #
    # Phase E (bead nexus-gmiaf.32.5): the server now REQUIRES a minted
    # session token (a present-but-non-live X-Nexus-T1-Session is a 401
    # — the transitional bootstrap session path is retired). So a mint
    # failure can no longer degrade to a bare session id (it would 401
    # on every scratch op). With a resolvable session id, mint failure
    # is FATAL: we fail loud rather than ship a broken or silently
    # session-unscoped T1 (no silent fallback for a security-boundary
    # input).
    _t1_session_id = _decision.session_id or ""

    # nexus-1si7z review (code-review-expert): no `or _t1_session_id ==
    # "unknown"` leg here -- resolve_t1_routing_tiers's own MINT branch
    # already collapses a resolved-but-"unknown" candidate_id to None
    # before returning (see its docstring/body), so `_decision.session_id`
    # can only ever be a real id or None by the time it reaches here;
    # checking for the literal string "unknown" again was dead code left
    # over from before the tiers-1-2 extraction, when this file resolved
    # the session id itself instead of consuming an already-normalized
    # T1RoutingDecision.
    if not _t1_session_id:
        # No resolvable session id — do NOT mint a shared "unknown" row (concurrent
        # MCPs would collide on the (tenant, session_id) UPSERT, each invalidating
        # the other's token). We leave NX_T1_SESSION untouched: a sub-agent that
        # inherited a LIVE minted token from its parent keeps working (require-minted
        # is satisfied by the inherited token).
        #
        # nexus-rn3wo.1 (code-review HIGH finding): with no inherited token, T1
        # scratch used to be "unavailable this process" because the pre-rn3wo.1
        # get_t1_database() had no fallback and HttpScratchStore() raised on first
        # use with neither env var set (fail-loud). Since rn3wo.1, get_t1_database()
        # treats "both env vars unset" as "bare CLI, mint my own CLI-dedicated
        # session" — which would silently pull an unresolvable-session MCP process
        # into the SAME shared CLI-dedicated identity as every bare `nx scratch`
        # invocation, a session-isolation regression across a boundary this code
        # explicitly treats as security-relevant.
        #
        # nexus-4lkmz decision 2 (LOCKED 2026-08-07): the pre-fix remedy forced
        # NX_T1_ISOLATED=1, redirecting into a private in-process T1Database.
        # That leg is deleted outright (T1 is PG-only). The honest replacement:
        # register a pre-init hook that FAILS LOUD, per call, with a named error
        # ("T1 unavailable this process") — never a shared identity, never a
        # silent private store. Mirrors the deferred-mint hook's "a T1-only
        # failure costs T1 only" posture: the MCP server starts and serves
        # every non-T1 tool normally; only T1-touching calls raise.
        _t1_session_id = ""
        from nexus import mcp_infra as _mcp_infra_unresolved  # noqa: PLC0415 — deferred to avoid import cycle at module load

        _mcp_infra_unresolved.set_t1_pre_init_hook(_raise_t1_unavailable_this_process)
        _svc_log.warning(
            "t1_session_unresolved", reason="no_resolvable_session",
            msg="no session id to mint; T1 scratch uses an inherited token if "
            "live, else T1 is unavailable this process (fail-loud on first "
            "use) — every other nexus tool is unaffected")
    else:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
        from nexus.db.t1 import _lock_guarded_mint_or_borrow  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

        _t1_config_dir = nexus_config_dir()

        # nexus-1si7z follow-up review (code-review-expert): the try below is
        # scoped to ONLY the mint-or-borrow call, deliberately -- an earlier
        # draft also wrapped the env-var writes / structlog info call /
        # ownership-dict update in the same except, so a (highly unlikely)
        # failure in one of THOSE would still have been mis-reported as a
        # mint failure via the RuntimeError's "T1 session token mint failed"
        # framing. Narrowing the try means _exc below is unambiguously
        # _lock_guarded_mint_or_borrow's own RuntimeError (propagated
        # unchanged from mint_t1_session_token), never anything else.
        #
        # nexus-jwqjm: routes through the flock-guarded mint-or-borrow
        # helper instead of calling mint_t1_session_token directly -- two
        # simultaneous stale-lease recoverers for the SAME session_id
        # previously both minted, each rotating the other's token via the
        # server's ON CONFLICT DO UPDATE, causing persistent 401 churn.
        # The helper serializes the mint-or-borrow critical section so
        # exactly one recoverer mints and every other borrows the
        # winner's published lease. See T2
        # nexus/design-jwqjm-t1-mint-race-flock.md.
        try:
            _t1_token, _t1_minted_fresh, _t1_mint_ttl = _lock_guarded_mint_or_borrow(
                _t1_session_id, _t1_config_dir
            )
        except Exception as _exc:  # noqa: BLE001 — boundary catch: ANY startup mint failure (unreachable, unresolvable endpoint, HTTP) defers rather than killing the whole MCP server
            # nexus-brw1s (GH #1405, field report stevengharris): DEFER the
            # mint — NEVER crash the server. This used to raise, the
            # RuntimeError escaped the stdio TaskGroup, the whole MCP
            # server died, and Claude Code cached the dead connection
            # (~/.claude/mcp-needs-auth-cache.json) for the session's
            # entire lifetime — EVERY nexus tool gone, including search/
            # memory/store/catalog which never touch T1, because a scratch
            # precondition could not reach a service that was merely not
            # up YET. That inverted the blast radius: a T1-only failure
            # must cost T1 only.
            #
            # Phase E require-minted is NOT weakened (the reason the old
            # code failed loud): there is still no bare-id fallback and no
            # CLI-dedicated identity sharing. The fail-loud moved from
            # process-fatal to PER-CALL: mcp_infra.get_t1() consults the
            # registered hook before first construction, which either
            # completes this mint (service came up — same flock/borrow
            # discipline, same ownership, refresh task scheduled onto this
            # loop) or raises an actionable error for that call alone,
            # retried on the next. Same altitude as the RDR-185
            # preconditions contract: never hard-block on a network probe
            # of a possibly-down process.
            import asyncio  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site

            _svc_log.warning(
                "t1_session_mint_deferred", session_id=_t1_session_id,
                error=str(_exc),
                msg="storage service unreachable at MCP start; the server "
                "starts anyway — T1 session mint retries on first scratch "
                "use, and this deferral affects T1 scratch only (in cloud "
                "mode T3 carries its own probe-cache limitation, "
                "nexus-5t1jp). Start the service with "
                "`nx daemon service start`.")
            _DEFERRED_T1_MINT.update(
                session_id=_t1_session_id,
                config_dir=_t1_config_dir,
                loop=asyncio.get_running_loop(),
            )
            from nexus import mcp_infra as _mcp_infra  # noqa: PLC0415 — deferred to avoid import cycle at module load

            _mcp_infra.set_t1_pre_init_hook(_retry_deferred_t1_mint)
            _t1_token, _t1_minted_fresh, _t1_mint_ttl = None, False, None

        if _DEFERRED_T1_MINT:
            # Deferred: fall through to the shared yield/teardown below.
            # The finally unregisters the hook; if the deferred mint
            # completed mid-session, the retry hook already recorded
            # ownership and started the refresh task, and the shared
            # teardown (cancel-refresh, close-session, shutdown) handles
            # both outcomes identically.
            pass
        elif not _t1_minted_fresh:
            # nexus-jwqjm: a concurrent recoverer already won the mint race
            # and published a fresh lease while we waited on the lock --
            # borrow it exactly like the USE_LEASED branch above: no
            # ownership, no refresh task, no teardown participation.
            _os.environ["NX_T1_SESSION"] = _t1_token
            _os.environ["NX_T1_SESSION_ID"] = _t1_session_id
            _svc_log.info(
                "t1_session_leased_after_mint_race", session_id=_t1_session_id
            )
            yield
            await _cancel_t1_handoff_watch_task()
            return

        else:
            _os.environ["NX_T1_SESSION"] = _t1_token
            _os.environ["NX_T1_SESSION_ID"] = _t1_session_id
            _svc_log.info("t1_session_isolation_minted", session_id=_t1_session_id)
            # nexus-5daww: track for `_t1_shutdown`'s Branch-0
            # handling so a raw SIGTERM / atexit (which cannot resume
            # this paused generator past `yield`) still revokes the
            # token and clears the lease file instead of leaking both.
            _OWNED_T1_SESSION["session_id"] = _t1_session_id

            # nexus-c8yvj / nexus-jwqjm: the lease publish (so a DETACHED
            # process with no inherited env -- most notably the SessionEnd
            # hook, nexus.hooks.session_end_flush -- can reach this SAME
            # session via nexus.db.t1.read_t1_session_lease instead of
            # falling into the disjoint CLI-dedicated identity) now happens
            # INSIDE _lock_guarded_mint_or_borrow, under the same lock that
            # guards the mint itself -- best-effort there too (a publish
            # failure never fails an already-successful mint).
            #
            # nexus-ngcpo Finding 1: start the periodic refresh task now that
            # we own this session id (`_OWNED_T1_SESSION` was set just above,
            # right after the mint succeeded). Refresh at half the token's
            # ACTUAL TTL (not an assumed constant) so a missed tick still
            # leaves a full half-TTL safety margin -- see
            # `_t1_session_refresh_loop`'s docstring for why re-minting is
            # safe here specifically (we only ever refresh our OWN id).
            # nexus-jwqjm: `_t1_mint_ttl` is the SAME value
            # `_lock_guarded_mint_or_borrow` used for its own publish call,
            # returned directly on the mint path -- never a post-hoc re-read
            # of the lease file (code-review-expert Medium finding, round 1:
            # a re-read could observe a stale/unrelated file if the publish
            # inside the helper silently failed). `_t1_mint_ttl` is only
            # ``None`` on the borrow path, which returns above before
            # reaching here, so it is always a float by this point.
            _mint_ttl = (
                _t1_mint_ttl if _t1_mint_ttl is not None else _T1_SESSION_DEFAULT_TTL_SECONDS
            )
            import asyncio  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site
            _refresh_interval = max(
                _mint_ttl * _T1_SESSION_REFRESH_FRACTION, _T1_SESSION_REFRESH_MIN_INTERVAL_S
            )
            global _T1_SESSION_REFRESH_TASK
            _T1_SESSION_REFRESH_TASK = asyncio.create_task(
                _t1_session_refresh_loop(_t1_session_id, _refresh_interval)
            )

    try:
        yield
    finally:
        # nexus-brw1s: clear any startup-deferred mint state + unregister
        # the retry hook so nothing dangles past this lifespan. No-op when
        # the deferred mint completed mid-session (the hook cleared both)
        # and when nothing was ever deferred. This teardown assumes
        # tool-call QUIESCENCE (anyio drains handlers before __aexit__);
        # the one window that survives that assumption — a first-ever T1
        # mint in flight in a worker thread across this clear — is closed
        # by the hook's post-mint shutdown sentinel, which re-checks this
        # state before committing ownership or scheduling a refresh.
        _DEFERRED_T1_MINT.clear()
        from nexus import mcp_infra as _mcp_infra_fin  # noqa: PLC0415 — deferred to avoid import cycle at module load
        _mcp_infra_fin.set_t1_pre_init_hook(None)
        # Cancel the refresh task BEFORE closing the session (mirrors
        # the RDR-129 early-stop ordering already used for
        # `_T1_HEARTBEAT_TASK` in Branch 3 below) so it cannot race a
        # re-mint against the session-close call just below. A no-op
        # when no session was ever minted (borrow path, or the
        # no-resolvable-session branch) since the task is only created
        # in the mint branch above.
        await _cancel_t1_session_refresh_task()
        # nexus-d76vc: cancel the handoff watcher too — started
        # unconditionally right after the storage-backend validation
        # above, regardless of which branch this lifespan took.
        await _cancel_t1_handoff_watch_task()
        # Teardown: close the scratch rows (best-effort promptness;
        # backstopped by the service's 24h TTL sweep), then route the
        # session-token close + lease clear through the SAME idempotent
        # `_t1_shutdown()` used by Branch 3 and the SIGTERM /
        # atexit paths (nexus-5daww). Before this fix the lease-clear +
        # token-close lived ONLY here -- inline, reachable solely via a
        # normal `async with` exit -- so a raw SIGTERM (the documented
        # NORMAL stdio shutdown path; `_sigterm_handler` calls
        # `os._exit(0)` right after `_t1_shutdown()` without ever
        # resuming this paused generator past `yield`) leaked BOTH an
        # unrevoked, still-valid server-side session token AND its 0600
        # lease file on every SIGTERM'd session.
        try:
            from nexus.db.http_scratch_store import HttpScratchStore  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
            _svc_log.info("t1_service_session_close_start")
            store = HttpScratchStore()
            deleted = store.close_session()
            store.close()
            _svc_log.info("t1_service_session_close_done", deleted=deleted)
        except Exception as _exc:  # noqa: BLE001 — boundary catch; failure surfaced via log.warning, must not crash caller
            _svc_log.warning("t1_service_session_close_failed", error=str(_exc))
        _t1_shutdown()
    return



def _t1_session_shutdown() -> None:
    """Close the SERVICE-backed T1 session token and clear its lease file.

    nexus-5daww: Branch-0 counterpart to the chroma cleanup below. Reads
    ``_OWNED_T1_SESSION["session_id"]`` (populated by
    ``_t1_lifespan``'s Branch 0 right after a successful mint) and,
    if present, clears the published lease file then revokes the token
    server-side. Called from :func:`_t1_shutdown` so SIGTERM / atexit
    cleanup -- which cannot resume the paused lifespan generator past its
    ``yield`` -- still runs this instead of leaking an unrevoked token plus
    its 0600 lease file. Idempotent: clears ``_OWNED_T1_SESSION`` on entry
    so a second call is a no-op.
    """
    session_id = _OWNED_T1_SESSION.get("session_id")
    if not session_id:
        return
    _OWNED_T1_SESSION.clear()

    # nexus-ngcpo review follow-up: the clean async-generator `finally` path
    # already awaits `_cancel_t1_session_refresh_task()` before reaching
    # here; this SIGTERM/atexit path previously did not, an asymmetry with
    # no observed impact (`_sigterm_handler` calls `os._exit(0)` immediately
    # after, so there is no event-loop turn left for a pending refresh tick
    # to race this cleanup) but worth closing for parity. `Task.cancel()` is
    # synchronous and safe to call without awaiting from this non-async path.
    global _T1_SESSION_REFRESH_TASK
    if _T1_SESSION_REFRESH_TASK is not None:
        _T1_SESSION_REFRESH_TASK.cancel()
        _T1_SESSION_REFRESH_TASK = None

    import structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
    _log = structlog.get_logger(__name__)

    # nexus-c8yvj: remove the published lease FIRST so a stale lease is
    # never read by a later, unrelated process once this session has
    # genuinely ended (mirrors the existing t1_addr.<session_id>
    # cleanup-before-token-close ordering used elsewhere in this module).
    try:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
        from nexus.db.t1 import clear_t1_session_lease  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
        clear_t1_session_lease(session_id, nexus_config_dir())
    except Exception as _exc:  # noqa: BLE001 — boundary catch; best-effort cleanup, must not crash teardown
        _log.warning("t1_session_lease_clear_failed", session_id=session_id, error=str(_exc))
    try:
        from nexus.db.t2.http_token_store import HttpTokenStore  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
        with HttpTokenStore(prefer_data_token=True) as _ts:  # nexus-maf9l
            _ts.close_session(session_id)
        _log.info("t1_session_token_closed", session_id=session_id)
    except Exception as _exc:  # noqa: BLE001 — boundary catch; failure surfaced via log.warning, must not crash caller
        _log.warning("t1_session_token_close_failed", session_id=session_id, error=str(_exc))


def _t1_shutdown() -> None:
    """Close the SERVICE-backed T1 session token + lease (Branch 0,
    nexus-5daww) via :func:`_t1_session_shutdown`.

    RDR-155 P4b: the chroma leg (stop server, rmtree tmpdir, reset
    ``_t1_state.T1_ADDR``) died with the owned-chroma Branch 3; this is
    now the single idempotent teardown for the Branch-0 session state.

    Idempotent. Called from three sites (lifespan async finally,
    :func:`_sigterm_handler`, :mod:`atexit`); whichever fires first does
    the work, the others find ``_OWNED_T1_SESSION`` empty and
    short-circuit. ``_SHUTDOWN_IN_FLIGHT`` prevents re-entry from a
    signal handler that interrupted an in-flight teardown: once set,
    never cleared.
    """
    global _SHUTDOWN_IN_FLIGHT
    if _SHUTDOWN_IN_FLIGHT:
        return
    if not _OWNED_T1_SESSION:
        return
    _SHUTDOWN_IN_FLIGHT = True
    _t1_session_shutdown()


def _sigterm_handler(_signo: int, _frame: Any) -> None:
    """Run the shutdown path then exit.

    When invoked while the lifespan finally is already running cleanup
    (stdin-EOF + SIGTERM race observed in production after 4.12.0
    default-on shipped), ``_SHUTDOWN_IN_FLIGHT`` is already True and
    we return immediately so the in-flight teardown completes cleanly.
    Otherwise (SIGTERM-only path with no prior stdin EOF) we drive the
    shutdown ourselves and ``os._exit(0)`` to terminate.

    Why ``os._exit`` instead of ``sys.exit``: ``sys.exit`` raises
    ``SystemExit``, which propagates through anyio's TaskGroup and
    gets logged as ``mcp_server_crashed`` even though the actual
    shutdown succeeded. ``os._exit`` terminates the process
    immediately without raising; chroma is already cleaned up at
    this point so there is nothing left to coordinate.
    """
    if _SHUTDOWN_IN_FLIGHT:
        # Lifespan / atexit is already running shutdown. Don't
        # interfere -- they hold the cleanup contract for this exit.
        return

    _t1_shutdown()
    _os._exit(0)


# nexus-20iee: the upstream mcp SDK's FastMCP Settings model (a Generic[
# LifespanResultT] BaseSettings) declares its `lifespan` field with a
# forward reference to `FastMCP` that is unresolved at class-definition
# time (`Settings` is defined earlier in the same module than `FastMCP`,
# in mcp/server/fastmcp/server.py), and the SDK never calls
# `model_rebuild()` itself. pydantic-settings 2.15.0 added
# IncompleteFieldDefinitionWarning, which fires from this unresolved
# reference every time a `Settings()` instance is constructed -- i.e.
# every `FastMCP(...)` call, including the one directly below. Rebuilding
# here, now that `FastMCP` is fully defined (its module finished importing
# above), resolves the forward reference process-wide before our own
# instantiation. No-op on pydantic-settings versions that predate the
# warning class. Guarded (nexus-vc5yb/nexus-3hc9z): Settings is an
# UNEXPORTED SDK internal under an unbounded mcp>=1.0,<2 pin — a future
# SDK refactor must degrade this cosmetic fix to a logged warning, never
# crash nx-mcp startup.
if _FastMCPSettings is not None:
    try:
        _FastMCPSettings.model_rebuild()
    except Exception as _rebuild_exc:  # noqa: BLE001 — boundary fallback: cosmetic-warning fix must never kill startup
        structlog.get_logger(__name__).debug(
            "fastmcp_settings_model_rebuild_failed", error=str(_rebuild_exc)
        )
else:  # pragma: no cover — future SDK restructure
    structlog.get_logger(__name__).debug("fastmcp_settings_symbol_unavailable")

mcp = FastMCP("nexus", lifespan=_t1_lifespan)

_DEFAULT_PAGE_SIZE = 10

# Minimum effective timeout for claude -p subagent tools. Planning and
# enrichment agents have been observed passing 180s / 300s overrides
# that re-trigger the class of false-positive timeouts v4.5.3 raised
# the defaults to prevent (see bead nexus-7sbf). The floor clamps
# caller-supplied values upward so agents can raise but not lower
# the effective budget.
_SUBAGENT_TIMEOUT_FLOOR = 300.0


def _clamp_subagent_timeout(requested: float, tool_name: str) -> float:
    """Clamp a caller-supplied subagent timeout to the floor.

    Emits a structured warning when a caller's requested timeout is
    below the floor so the override is visible in logs without
    blocking the call.
    """
    if requested < _SUBAGENT_TIMEOUT_FLOOR:
        import structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
        structlog.get_logger().warning(
            "subagent_timeout_clamped",
            tool=tool_name,
            requested=requested,
            floor=_SUBAGENT_TIMEOUT_FLOOR,
        )
        return _SUBAGENT_TIMEOUT_FLOOR
    return requested


def _subprocess_tool_grant() -> tuple[dict[str, Any], list[str]]:
    """Build the (mcp_servers, allowed_tools) grant for tool-needing
    operator subprocesses. nexus-mawqw / Fix B.

    The agent-replacement tools (``nx_enrich_beads``, ``nx_plan_audit``)
    do open-ended codebase exploration that cannot be pre-fetched, so
    their ``claude -p`` child must be able to call nx MCP read tools plus
    the built-in file tools. We pass the conexus MCP servers *inline* via
    ``--mcp-config`` (claude_dispatch's ``mcp_servers``) so they clear the
    post-CC-2.1.162 pending-approval gate, and allowlist them by server
    key plus the read-only built-ins.

    Server keys here become the child's tool-name prefix
    (``mcp__nexus__search`` etc.), independent of the parent plugin's
    ``mcp__plugin_conexus_nexus__*`` naming — the child gets its own
    fresh, explicitly-trusted server registration.

    ``CLAUDE_PLUGIN_ROOT`` is threaded through from the current env so
    the spawned ``nx-mcp`` resolves the same plugin root as the parent;
    omitted when unset (CLI / non-plugin contexts) so we never inject a
    literal ``${...}`` placeholder.
    """
    env: dict[str, str] = {}
    plugin_root = _os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        env["CLAUDE_PLUGIN_ROOT"] = plugin_root

    server_env = {"env": env} if env else {}
    mcp_servers: dict[str, Any] = {
        "nexus": {"command": "nx-mcp", "args": [], **server_env},
        "nexus_catalog": {"command": "nx-mcp-catalog", "args": [], **server_env},
    }
    allowed_tools = [
        "Read", "Grep", "Glob",
        "mcp__nexus", "mcp__nexus_catalog",
    ]
    return mcp_servers, allowed_tools


# nexus-mawqw / Fix A: cap how much we pre-fetch + inline into the tidy
# prompt. 30 entries at 2 KB each keeps the prompt well under any context
# pressure while covering the realistic consolidation working set.
_TIDY_MAX_ENTRIES = 30
_TIDY_MAX_CHARS_PER_ENTRY = 2000


def _tidy_prefetch(topic: str, collection: str) -> tuple[str, int, str, bool]:
    """Retrieve + hydrate the entries to consolidate, server-side.

    nexus-mawqw / Fix A. The MCP server holds direct T3 access, so the
    ``tidy`` retrieval runs in-process here and the hydrated entries are
    inlined into the prompt. The ``claude -p`` child then does LLM-only
    dedup/summarise with NO tools, making ``nx_tidy`` immune to the
    post-CC-2.1.162 MCP-server-approval gate that broke the old
    subprocess-calls-MCP-tools design.

    Returns ``(entries_block, n_entries, reason, genuine_miss)``. ``reason`` is
    ``""`` when entries were retrieved, and otherwise says WHY there are none.
    ``genuine_miss`` is True for EXACTLY ONE branch — the collection really had
    nothing near the topic — so the caller can never report a failure as an
    empty result.

    nexus-1obui: this previously collapsed six distinct outcomes to ``("", 0)``
    — search raised, search returned an unstructured payload, zero ids, hydrate
    raised, hydrate returned a structured error, zero non-empty bodies — and the
    caller rendered all six as "nothing to consolidate". A T3 outage mid-tidy was
    indistinguishable from a genuinely empty topic, and a threshold drop (the
    common case, since consolidation wants a WIDER net than lookup does) read as
    proof the collection held nothing. Every branch now names itself.
    """
    try:
        hits = search(
            query=topic,
            corpus=collection,
            limit=_TIDY_MAX_ENTRIES,
            structured=True,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort path; must not crash caller
        import structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
        structlog.get_logger().debug("tidy_prefetch_search_failed", exc_info=True)
        return "", 0, f"retrieval FAILED ({type(exc).__name__}: {exc})", False
    if not isinstance(hits, dict):
        # search() returns a human-readable string on no-match / error.
        return "", 0, f"retrieval returned an unstructured payload: {str(hits)[:300]}", False
    ids = hits.get("ids") or []
    if not ids:
        # nexus-1obui: the uro6c threshold diagnostic now rides along on the
        # structured zero-hit, so a drop can be reported as a drop.
        reason = hits.get("no_results_reason") or "no entries matched the topic"
        dropped = bool(hits.get("threshold_dropped"))
        if dropped:
            reason = f"RETRIEVAL THRESHOLD DROPPED every candidate — {reason}"
        return "", 0, str(reason), not dropped
    cols = hits.get("chunk_collections") or hits.get("collections") or [collection]

    try:
        hydrated = store_get_many(
            ids,
            cols,
            max_chars_per_doc=_TIDY_MAX_CHARS_PER_ENTRY,
            structured=True,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort path; must not crash caller
        import structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
        structlog.get_logger().debug("tidy_prefetch_hydrate_failed", exc_info=True)
        return "", 0, (
            f"matched {len(ids)} entries but HYDRATION FAILED "
            f"({type(exc).__name__}: {exc})"
        ), False
    if isinstance(hydrated, dict) and hydrated.get("error"):
        # store_get_many caught an internal error and returned it as a
        # structured field rather than raising. Surface it at DEBUG so a
        # T3 outage during tidy is diagnosable instead of vanishing.
        import structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
        structlog.get_logger().debug(
            "tidy_prefetch_hydrate_error", error=hydrated["error"],
        )
    contents = hydrated.get("contents") if isinstance(hydrated, dict) else None
    if not contents:
        err = hydrated.get("error") if isinstance(hydrated, dict) else None
        return "", 0, (
            f"matched {len(ids)} entries but HYDRATION RETURNED NO CONTENT"
            + (f" ({err})" if err else "")
        ), False
    blocks: list[str] = []
    for i, (doc_id, body) in enumerate(zip(ids, contents), start=1):
        body = (body or "").strip()
        if not body:
            continue
        blocks.append(f"--- Entry {i} (id={doc_id}) ---\n{body}")
    if not blocks:
        return "", 0, (
            f"matched and hydrated {len(ids)} entries but every body was EMPTY"
        ), False
    return "\n\n".join(blocks), len(blocks), "", False


# ── Tier-discipline telemetry (Phase 1A nexus-kren) ─────────────────────────


# nexus-pyzk7: telemetry is best-effort (a failed write must never break a tool
# call), but a SILENT drop is the exact bug this bead fixes. When the persist
# raises — service 5xx/timeout, or a backend with no such store — log ONCE per
# table so the loss is visible in the process log instead of vanishing.
_telemetry_drop_warned: set[str] = set()


def _warn_telemetry_drop(table: str, exc: BaseException) -> None:
    if table in _telemetry_drop_warned:
        return
    _telemetry_drop_warned.add(table)
    import structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
    structlog.get_logger(__name__).warning(
        "telemetry_write_dropped",
        table=table,
        error=f"{type(exc).__name__}: {exc}",
        note="telemetry row not persisted (best-effort); subsequent drops for "
             "this table are suppressed this process (per-table suppression "
             "shipped in nexus-pyzk7).",
    )


def _record_tier_write(
    *,
    tool: str,
    tier: str,
    agent: str | None = None,
    project: str | None = None,
    target_title: str | None = None,
) -> None:
    """Append one row to ``tier_writes`` recording a tier-write call.

    Best-effort: any exception is swallowed. Telemetry must NEVER break
    the hot path of the calling tool. ``session_id`` is resolved by
    :func:`nexus.session.resolve_active_session_id`; rows from a process
    with no bound Claude session are attributed to ``"unknown"`` so
    operators can grep for the sentinel and rows are never lost.

    Phase 1A of the tier-discipline restoration initiative
    (memory: tier-discipline-audit-2026-05-06). Issue #594 / nexus-9e9a
    routed the resolution chain through ``resolve_active_session_id``
    so this site agrees with the T1 chunk store and the SessionEnd
    launcher on attribution.

    Lazy imports inside this function so monkey-patches of
    ``nexus.mcp_infra.t2_ctx`` in tests are picked up at call time
    rather than frozen at module-import time.
    """
    try:
        from datetime import datetime, timezone  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site

        from nexus.mcp_infra import t2_ctx  # noqa: PLC0415 — circular-dep avoidance (mcp package import deferred)
        from nexus.session import resolve_active_session_id  # noqa: PLC0415 — circular-dep avoidance (lifecycle module imports mcp at top)

        session_id = resolve_active_session_id() or "unknown"
        ts = datetime.now(timezone.utc).isoformat()
        with t2_ctx() as t2:
            # nexus-pyzk7: route through the telemetry store, which persists
            # via the service (POST /v1/telemetry/tier_writes/record) — no raw
            # ``.conn`` reach, no silent drop. The SQLite branch this comment
            # used to name went away with the T2 stores (RDR-158 P4).
            t2.telemetry.record_tier_write(
                session_id=session_id, ts=ts, tool=tool, tier=tier,
                agent=agent, project=project, target_title=target_title,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort telemetry, must not crash caller (warned once via _warn_telemetry_drop)
        # Best-effort. Telemetry breaking a tool call is the worst kind of
        # regression — but warn once so the drop is visible, not silent.
        _warn_telemetry_drop("tier_writes", exc)


# ── Post-store hooks (process-local registry constructed above) ─────────────
#
# RDR-095 + symmetric-fire follow-up: every storage event (MCP ``store_put``
# or CLI bulk ingest) fires the three chains on a ``HookRegistry`` threaded
# top-down from the entry point. For MCP the entry point is module load
# time (long-running server process); ``_hooks`` is constructed above and
# ``_install_default_hooks`` wires the load-bearing default consumers:
#
# * Three batch hooks (chash dual-write, taxonomy assign, manifest write)
#   for the cross-cutting catalog / index correctness work.
# * One document hook (RDR-089 aspect-extraction enqueue) which writes to
#   aspect_extraction_queue (microsecond-scale) and lazy-spawns a
#   background worker that drains the queue and invokes the synchronous
#   extract_aspects.
#
# Registration order within the batch chain is load-bearing: chash
# dual-write must precede taxonomy assignment so chash rows exist before
# topic assignment runs.

# ── Registered tools ─────────────────────────────────────────────────────────


def _no_results_message(diagnostics: list, *, base: str = "No results.") -> str:
    """Surface a threshold-drop instead of a silent zero-hit (nexus-uro6c).

    When the distance threshold dropped EVERY candidate of some collection
    (``SearchDiagnostics.worst_offender``), report the closest dropped
    distance + the threshold that blocked it, so the caller can relax the
    ``threshold`` knob rather than conclude "nothing matched". Falls back to
    *base* when nothing was dropped — a genuine miss (raw candidate count 0).

    Pure function of the diagnostics list populated by
    ``search_cross_corpus(diagnostics_out=...)``; the engine itself never
    emits this (MCP does not write stderr).
    """
    if not diagnostics:
        return base
    # nexus-pebfx.8: collections the backend refused to serve were skipped,
    # not searched — a zero-hit must say so or it reads as a genuine miss.
    failed = diagnostics[0].failed_collections
    suffix = ""
    if failed:
        suffix = (
            f" Note: {len(failed)} collection(s) were excluded by service "
            "errors and NOT searched: "
            + "; ".join(f"{c}: {e}" for c, e in failed.items())
        )
    worst = diagnostics[0].worst_offender()
    if worst is None:
        return base + suffix
    name, threshold, top_distance = worst
    thr = f"{threshold:.4f}" if threshold is not None else "the per-corpus default"
    return (
        f"{base} Closest candidate was dropped at distance {top_distance:.4f} "
        f"(threshold {thr}, collection {name}). Re-run with "
        f"threshold={top_distance + 0.05:.2f} (or higher) to include it."
        + suffix
    )


def _structured_no_results(diagnostics: list, *, base: str = "No results.") -> dict:
    """Zero-hit STRUCTURED payload that carries the uro6c diagnostic (nexus-1obui).

    ``_no_results_message`` has surfaced a threshold drop on the prose path since
    nexus-uro6c, expressly so a caller can "relax the ``threshold`` knob rather
    than conclude 'nothing matched'". ``structured=True`` discarded it and
    returned bare empty lists, so every programmatic consumer — ``nx_tidy``, the
    plan-runner step-output contract, anything reading ``ids``/``distances`` —
    could not tell "the threshold dropped every candidate" from "this topic is
    genuinely absent". Measured 2026-08-27: ``nx_tidy`` reported "nothing to
    consolidate" on a topic whose two nearest entries had been dropped at 0.629
    and 0.645, and which a plain prose ``search`` returned.

    Additive only. The six original keys keep their exact shape and meaning, so
    existing consumers are unaffected; the new keys are present on zero-hit
    payloads for callers that want to discriminate.
    """
    worst = diagnostics[0].worst_offender() if diagnostics else None
    out: dict = {
        "ids": [], "tumblers": [], "distances": [],
        "collections": [], "chunk_collections": [], "chunk_text_hash": [],
        "no_results_reason": _no_results_message(diagnostics, base=base),
        # A caller must not have to parse prose to branch on this.
        "threshold_dropped": worst is not None,
    }
    if worst is not None:
        name, threshold, top_distance = worst
        out["closest_dropped"] = {
            "collection": name,
            "threshold": threshold,
            "distance": top_distance,
            "retry_threshold": round(top_distance + 0.05, 2),
        }
    return out


# Note: catalog server also registers a "search" tool. No collision — Claude Code
# disambiguates by server prefix (mcp__plugin_conexus_nexus__search vs
# mcp__plugin_conexus_nexus-catalog__search).
@degrade_loud_when_migrating
def _search_render(
    query: str,
    corpus: str = "knowledge,code,docs",
    limit: int = 10,
    offset: int = 0,
    where: str = "",
    cluster_by: str = "",
    topic: str = "",
    structured: bool = False,
    threshold: float | None = None,
) -> "str | dict":
    """Business logic for the ``search`` MCP tool. Paged results (``offset=N`` for next page).

    Private render function — NOT ``@mcp.tool()``-decorated. This is what
    plan-runner, ``nx doc cite`` (``_phase5_search``), and every direct
    in-process caller import and call; its ``str``/``dict`` return contract
    (keyed on the ``structured`` bool) is unchanged from before nexus-6jlki
    and must stay that way — the wire-facing ``search()`` wrapper below is
    the ONLY thing that changed shape. See ``search()``'s docstring for why
    the split exists (structuredContent needs a ``CallToolResult`` return,
    which is incompatible with the plain ``str`` this function's direct
    callers depend on).

    Ranking: with ``cluster_by=""`` (the default), results are ranked by
    ``hybrid_score`` — vector similarity blended with ``frecency_score``
    (code__ collections, honouring the ``.nexus.yml`` ``[tuning]`` weights
    resolved from THIS MCP SERVER PROCESS'S OWN LAUNCH DIRECTORY, not the
    repo the caller may be asking about, and the ``search.hybrid_default``
    flag) plus the RDR-055 bibliographic quality boost (knowledge__/docs__/
    rdr__ collections with citation metadata) — via
    ``search_engine.apply_ranking_boosts``, the same function ``nx search``
    calls. ``distances`` in ``structured=True`` output is always the raw
    vector distance, unaffected by the boost — only result ORDER changes.

    ``cluster_by="semantic"`` is DIFFERENT: it preserves the cluster-grouped
    display order ``search_cross_corpus`` produced (topic/Ward clustering,
    each cluster's own internal ordering), full stop — the hybrid/bib boost
    is still computed but its reordering is fully discarded to keep same-
    cluster results contiguous for the text renderer, so in this mode the
    boost has NO effect on what the caller sees, in either text or
    ``structured=True`` output (``hybrid_score`` is not surfaced there
    either). Use ``cluster_by=""`` when boosted ranking matters.

    Args:
        query: Search query string
        corpus: Corpus prefixes or collection names, comma-separated. "all" for everything.
        limit: Page size (default 10)
        offset: Skip N results for pagination (default 0)
        where: Metadata filter (KEY=VALUE, comma-separated). Ops: = >= <= > < !=
        cluster_by: "semantic" for topic/Ward clustering (default), empty to disable
        topic: Pre-filter to documents in this topic label (from nx taxonomy discover)
        structured: Return ``{ids, tumblers, distances, collections}`` dict instead
            of human-readable string.  Used by the plan runner so ``$stepN.ids``
            references resolve to actual chunk IDs.
        threshold: Override the per-collection distance threshold uniformly
            (raw cosine distance, lower is stricter). Pass ``float('inf')``
            to disable filtering entirely. ``None`` (default) uses per-corpus
            config thresholds. RDR-087 Phase 1.1 workaround for silent
            threshold-drop on dense-prose collections.
    """
    try:
        from nexus.config import get_tuning_config, load_config  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
        from nexus.filters import sanitize_query  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
        from nexus.search_engine import apply_ranking_boosts, search_cross_corpus  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

        cfg = load_config()
        if cfg.get("search", {}).get("query_sanitizer", True):
            query = sanitize_query(query)

        t3 = _get_t3()
        all_names = _get_collection_names()

        if corpus == "all":
            # True "all": every unique prefix that appears in the live
            # collection list. Fixes the gap where the old constant
            # ("knowledge,code,docs,rdr") missed projects whose only
            # collection is e.g. rdr__* or a custom prefix.
            seen_prefixes: list[str] = []
            for n in all_names:
                prefix = n.split("__", 1)[0]
                if prefix and prefix not in seen_prefixes:
                    seen_prefixes.append(prefix)
            corpus = ",".join(seen_prefixes) if seen_prefixes else "knowledge,code,docs,rdr"

        target: list[str] = []
        for part in corpus.split(","):
            part = part.strip()
            if not part:
                continue
            if "__" in part:
                # nexus-hmxi: route the qualified-with-__ form through
                # ``t3_collection_name`` (with t3) so 2-segment legacy
                # input is grandfathered to an existing legacy
                # collection or auto-promoted to the conformant target,
                # matching ``store_list`` / ``store_put`` resolution.
                # Pre-fix this branch always used the user input as-is,
                # so a 2-segment ``--corpus knowledge__art`` could hit
                # a legacy collection that ``store_list --collection
                # knowledge__art`` was missing.
                target.append(t3_collection_name(part, t3=t3))
            else:
                target.extend(resolve_corpus(part, all_names))

        if not target:
            return f"No collections match corpus {corpus!r}"

        where_dict = _parse_where_str(where)

        # nexus-e4srp: page-turn cache. Each stateless page call re-embeds the
        # query server-side and re-fetches every earlier page's rows. Instead:
        # over-fetch a small lookahead window on a fresh retrieval identity and
        # serve subsequent pages from it (one query embed per burst). The
        # cached list is post-sort, so cached pages are byte-identical to what
        # the uncached path would render.
        need = offset + limit
        fetch_n = need + limit * _PAGE_LOOKAHEAD_PAGES
        cache_key = (
            query, tuple(target), where or "", cluster_by or "",
            topic or "", threshold,
        )
        clustered = bool(cluster_by)
        # Always pass taxonomy for topic grouping + topic boost (RDR-070).
        # Wrapped in context manager to avoid connection leak.
        # nexus-uro6c: capture threshold-filter diagnostics so a silent
        # zero-hit can surface the closest dropped candidate (the MCP tool
        # turns it into an actionable message; the engine still emits no stderr).
        diag: list = []
        cached = _page_cache_get(cache_key, need)
        if cached is not None:
            results, diag = cached
        else:
            results = None
        if results is None:
            with _t2_ctx() as _t2_db:
            # ``telemetry`` wired for RDR-087 Phase 2.2 hot-path logging;
            # opt-out via ``telemetry.search_enabled=false`` in .nexus.yml.
                results = search_cross_corpus(
                    query, target, n_results=fetch_n, t3=t3, where=where_dict,
                    cluster_by=cluster_by or None,
                    catalog=_get_catalog(),
                    link_boost=False,
                    taxonomy=_t2_db.taxonomy,
                    topic=topic or None,
                    threshold_override=threshold,
                    telemetry=_t2_db.telemetry,
                    diagnostics_out=diag,
                )
            # hybrid scoring + RDR-055 E2 quality boost — parity
            # with the CLI (search_cmd.py), which has applied both since
            # RDR-055 / the frecency-scoring work. Before this fix the MCP
            # path ranked by raw vector distance only and never read
            # frecency_score or bib_citation_count at all.
            _cluster_order = [r.id for r in results] if clustered else None
            results = apply_ranking_boosts(
                results,
                hybrid=cfg.get("search", {}).get("hybrid_default", False),
                tuning=get_tuning_config(),
                catalog=_get_catalog(),
            )
            if clustered:
                # apply_ranking_boosts sorts by hybrid_score globally, which
                # would scatter same-cluster results apart — the text
                # renderer below assumes cluster labels appear CONTIGUOUSLY
                # (the `current_cluster` grouping further down). Boosted
                # hybrid_score values are kept on each result; only the
                # display ORDER is restored to the cluster-grouped order
                # search_cross_corpus produced.
                _by_id = {r.id: r for r in results}
                results = [_by_id[i] for i in _cluster_order if i in _by_id]
            # Non-clustered results are now ranked by hybrid_score
            # (apply_ranking_boosts' own sort) rather than raw distance.
            _page_cache_put(cache_key, results, fetch_n, diag)
        if not results:
            if structured:
                return _structured_no_results(diag)
            return _no_results_message(diag)

        # Apply pagination
        total = len(results)
        page = results[offset:offset + limit]
        if not page:
            _off_msg = f"No results at offset {offset} (total {total})."
            if structured:
                return _structured_no_results(diag, base=_off_msg)
            return _off_msg

        # Record search trace for RDR-061 E2 retrieval feedback correlation.
        # Non-fatal — session may be unavailable in test contexts.
        try:
            t1, _ = _get_t1()
            session_id = t1.session_id if hasattr(t1, "session_id") else ""
            if session_id:
                _record_search_trace(
                    session_id,
                    query,
                    [(r.id, r.collection) for r in page],
                )
        except Exception:  # noqa: BLE001 — best-effort path; failure logged via log.debug, must not crash caller
            import structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
            structlog.get_logger().debug("relevance_trace_record_failed", exc_info=True)

        # Structured return for plan-runner step output contract.
        # Resolves $stepN.ids / $stepN.collections / $stepN.distances refs.
        # RDR-086 Phase 3.1: chunk_text_hash forwarded per-result so callers
        # can build chash:<hex> citations without a second fetch.
        #
        # Review #7: ``collections`` is dedup'd (plan-runner contract) while
        # ``chunk_collections`` is per-result aligned with ``ids`` so
        # consumers that need per-chunk origin (e.g. ``nx_answer``) get
        # the right collection for every hit, not just the top result.
        if structured:
            return {
                "ids": [r.id for r in page],
                "tumblers": [r.metadata.get("tumbler", "") for r in page],
                "distances": [float(r.distance) for r in page],
                "collections": list({r.collection for r in page}),
                "chunk_collections": [r.collection for r in page],
                "chunk_text_hash": [
                    r.metadata.get("chunk_text_hash", "") for r in page
                ],
            }

        lines: list[str] = []
        current_cluster: str | None = None
        for r in page:
            # Emit cluster header when group changes
            cluster_label = r.metadata.get("_cluster_label", "")
            if clustered and cluster_label and cluster_label != current_cluster:
                if current_cluster is not None:
                    lines.append("")  # blank separator between clusters
                lines.append(f"── {cluster_label} ──")
                current_cluster = cluster_label
            title = r.metadata.get("title", "")
            # nexus-1qed: prefer the catalog-resolved _display_path so
            # the label survives after the prune verb drops source_path.
            source = (
                r.metadata.get("_display_path")
                or r.metadata.get("source_path", "")
            )
            dist = f"{r.distance:.4f}"
            label = title or source or r.id
            snippet = r.content[:200].replace("\n", " ")
            flag = " [CONTRADICTS ANOTHER RESULT]" if r.metadata.get("_contradiction_flag") else ""
            lines.append(f"[{dist}] {label}{flag}\n  {snippet}")

        # Pagination footer
        shown_end = offset + len(page)
        if shown_end < total:
            lines.append(f"\n--- showing {offset + 1}-{shown_end} of {total}. next: offset={shown_end}")
        elif total >= fetch_n:
            lines.append(f"\n--- showing {offset + 1}-{shown_end}. may have more: offset={shown_end}")
        else:
            lines.append(f"\n--- showing {offset + 1}-{shown_end} of {total} (end)")

        return _cap_text_result("\n\n".join(lines), "search")
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("search", e)


def _truncated_chars_dropped(capped_text: str, tool: str) -> tuple[bool, int]:
    """Detect whether ``_cap_text_result`` fired and recover the drop count.

    ``_cap_text_result`` appends ``"[<tool>: result capped at N chars; D
    chars dropped — ...]"`` as a trailing, never-prepended marker (house
    doctrine: no silent truncation). This is the one place that marker
    format is parsed back out, so a structuredContent consumer can see the
    same truncation fact the text reader sees (nexus-6jlki: truncation must
    be visible in BOTH shapes). A parse miss degrades to ``(True, 0)`` —
    still flagged truncated, just without a char count — never silently
    ``(False, 0)`` (that would hide a real truncation from the structured
    shape).
    """
    marker = f"[{tool}: result capped at "
    if marker not in capped_text:
        return False, 0
    try:
        dropped = int(capped_text.rsplit("; ", 1)[1].split(" chars dropped", 1)[0])
    except (IndexError, ValueError):
        return True, 0
    return True, dropped


@mcp.tool(
    title="Semantic Search",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def search(
    query: str,
    corpus: str = "knowledge,code,docs",
    limit: int = 10,
    offset: int = 0,
    where: str = "",
    cluster_by: str = "",
    topic: str = "",
    structured: bool = False,
    threshold: float | None = None,
) -> "str | dict | CallToolResult":
    """Semantic search across T3 collections. Paged results (``offset=N`` for next page).

    nexus-6jlki spike: the wire-facing tool. ``structured_output=False`` on
    the ``@mcp.tool()`` registration turns OFF FastMCP's own return-type
    auto-detection — left on, the ``str | dict`` return annotation gets
    silently auto-wrapped as ``structuredContent: {"result": <value>}``
    (verified against the pinned mcp==1.27.1; the nexus-r90ao audit,
    T2 [23357], narrowed the affected set: ``str``, ``str | dict`` unions,
    and ``list[...]`` returns were auto-wrapped, while bare unparameterized
    ``dict`` returns never were — nobody designed the wrapping, no client
    is documented to rely on it).
    This tool is the first to replace that accident with a deliberate shape.

    Business logic lives in ``_search_render`` (unchanged since before this
    tool existed) — plan-runner, ``nx doc cite``, and this repo's test
    suite call ``_search_render`` directly and see EXACTLY the same
    ``str``/``dict`` contract as before. Only a real MCP-wire call (an
    actual ``tools/call``, not an in-process Python call) goes through this
    wrapper and can observe the new ``structuredContent`` field; the reason
    for the split is that a ``CallToolResult`` return is the only SDK-level
    way to give a caller byte-identical human text AND separate machine
    data in one response (confirmed via spike — a bare-dict/str return
    ties ``content`` and ``structuredContent`` to the SAME serialization,
    which would replace today's human-readable rendering with a JSON dump).

    Default (``structured=False``, the path every existing MCP caller
    exercises today): ``content`` is ``_search_render``'s human-readable
    string, byte-identical to what this tool returned before nexus-6jlki.
    ``structuredContent`` is NEW — the same ``{ids, tumblers, distances,
    collections, chunk_collections, chunk_text_hash}`` shape
    ``structured=True`` has always returned (fetched via the page-turn
    cache, so this costs no extra vector-store round trip), plus
    ``truncated``/``truncated_chars`` reflecting whether the 250K text cap
    (``_cap_text_result``) fired — visible in the structured shape even
    though the cap marker itself only lives in the text.

    ``structured=True``: UNCHANGED. Returns the bare dict exactly as
    before — no ``CallToolResult``, no new keys, no ``structuredContent``
    wrapping. This is the plan-runner's in-process contract
    (``$stepN.ids`` / ``$stepN.tumblers`` / ``$stepN.distances``
    references) and it is explicitly out of scope for this change.

    Older/non-structuredContent-aware MCP clients are unaffected either
    way: an unrecognized response field is ignored per the protocol, and
    ``outputSchema`` is no longer advertised for this tool (fine — it was
    advertising the wrong, auto-wrapped shape before).

    Args:
        query: Search query string
        corpus: Corpus prefixes or collection names, comma-separated. "all" for everything.
        limit: Page size (default 10)
        offset: Skip N results for pagination (default 0)
        where: Metadata filter (KEY=VALUE, comma-separated). Ops: = >= <= > < !=
        cluster_by: "semantic" for topic/Ward clustering (default), empty to disable
        topic: Pre-filter to documents in this topic label (from nx taxonomy discover)
        structured: Return ``{ids, tumblers, distances, collections}`` dict instead
            of human-readable string (unchanged; see docstring above).  Used by
            the plan runner so ``$stepN.ids`` references resolve to actual chunk IDs.
        threshold: Override the per-collection distance threshold uniformly
            (raw cosine distance, lower is stricter). Pass ``float('inf')``
            to disable filtering entirely. ``None`` (default) uses per-corpus
            config thresholds. RDR-087 Phase 1.1 workaround for silent
            threshold-drop on dense-prose collections.
    """
    result = _search_render(
        query, corpus=corpus, limit=limit, offset=offset, where=where,
        cluster_by=cluster_by, topic=topic, structured=structured,
        threshold=threshold,
    )
    if structured or not isinstance(result, str):
        # structured=True, or an error string that already reads like one —
        # unchanged wire behavior.
        return result

    truncated, dropped = _truncated_chars_dropped(result, "search")
    data = _search_render(
        query, corpus=corpus, limit=limit, offset=offset, where=where,
        cluster_by=cluster_by, topic=topic, structured=True,
        threshold=threshold,
    )
    empty_shape = {
        "ids": [], "tumblers": [], "distances": [], "collections": [],
        "chunk_collections": [], "chunk_text_hash": [],
    }
    structured_content = {
        **(data if isinstance(data, dict) else empty_shape),
        "truncated": truncated,
        "truncated_chars": dropped,
        # SELF-SUFFICIENCY (nexus-6jlki follow-up). A conforming MCP client
        # may render structuredContent INSTEAD of the text block -- the
        # protocol permits it and nexus's own client does exactly that
        # (mcp_client/core.py::_parse_result prefers structuredContent over
        # the first text block), as does Claude Code. The original design
        # (T2 [23351]) modelled this field as purely additive -- "a new
        # structuredContent field they can ignore or start consuming" -- so
        # between c9762865d and this line a default-mode search delivered
        # ids/tumblers/distances and NO paths, line numbers or snippets to
        # a structuredContent-preferring client. Carrying the render here
        # costs one duplicated string and makes the shape correct for
        # whichever representation the client chooses to read.
        "text": result,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=result)],
        structuredContent=structured_content,
    )


#: nexus-e4srp: single-entry page-turn cache for the ``search`` tool. Page
#: bursts are same-identity, seconds apart; one entry with a short TTL covers
#: them without holding stale results past content changes. Thread-safe via
#: the lock (MCP tools can run concurrently).
_PAGE_LOOKAHEAD_PAGES = 2
_PAGE_CACHE_TTL_S = 120.0
_page_cache_lock = threading.Lock()
_page_cache: dict[str, Any] = {}


def _page_cache_get(key: tuple, need: int) -> tuple[list, list] | None:
    """``(results, diagnostics)`` when the entry matches *key*, is TTL-fresh,
    and its fetch covered the needed window (or exhausted the corpus).

    Diagnostics ride the cache (batch-f1655f55 critique): a cached zero-hit
    must render the same nexus-uro6c closest-dropped-candidate hint as the
    uncached path — serving ``[]`` with empty diagnostics silently degraded
    the message to a bare "No results."."""
    with _page_cache_lock:
        if _page_cache.get("key") != key:
            return None
        if time.monotonic() - _page_cache.get("at", 0.0) > _PAGE_CACHE_TTL_S:
            return None
        results = _page_cache.get("results") or []
        fetched = _page_cache.get("fetch_n", 0)
        exhausted = len(results) < fetched   # corpus smaller than the ask
        if fetched >= need or exhausted:
            return results, _page_cache.get("diag") or []
        return None


def _page_cache_put(key: tuple, results: list, fetch_n: int, diag: list) -> None:
    with _page_cache_lock:
        _page_cache.clear()
        _page_cache.update({
            "key": key, "results": results, "fetch_n": fetch_n,
            "diag": diag, "at": time.monotonic(),
        })


def _page_cache_invalidate() -> None:
    """Drop the page-turn cache after a same-process write (batch-f1655f55
    critique): a ``store_put``/``store_delete`` inside the TTL window must
    not leave a page burst serving pre-write results. Cross-process writes
    (CLI indexing) remain bounded by the TTL — this cache lives in the MCP
    process, whose write surface is exactly these tools."""
    with _page_cache_lock:
        _page_cache.clear()


def _reset_page_cache_for_tests() -> None:
    with _page_cache_lock:
        _page_cache.clear()


def _resolve_corpus_target(corpus: str, t3: Any) -> list[str]:
    """Resolve a comma-separated corpus/collection spec to collection names.

    Mirrors the ``search`` tool's routing: ``all`` expands to every live
    prefix; a ``__``-qualified part is a collection name; a bare part is a
    corpus prefix resolved against the live collection list.
    """
    all_names = _get_collection_names()
    if corpus == "all":
        seen: list[str] = []
        for n in all_names:
            prefix = n.split("__", 1)[0]
            if prefix and prefix not in seen:
                seen.append(prefix)
        corpus = ",".join(seen) if seen else "knowledge,code,docs,rdr"
    target: list[str] = []
    for part in corpus.split(","):
        part = part.strip()
        if not part:
            continue
        if "__" in part:
            target.append(t3_collection_name(part, t3=t3))
        else:
            target.extend(resolve_corpus(part, all_names))
    return list(dict.fromkeys(target))


def _group_collections_by_model(target: list[str]) -> list[list[str]]:
    """Group a resolved collection list by embedding model (nexus-3l6gz).

    A combined-query service call embeds the query string ONCE and ranks
    every listed collection's chunks against that single query vector; the
    service's ``requireHomogeneousDim`` guard only checks vector DIMENSION,
    not embedding MODEL, so a multi-prefix corpus spanning two same-dimension
    models (e.g. ``voyage-code-3`` + ``voyage-context-3``, both 1024-dim)
    silently embeds the query with only the first collection's model and
    mis-ranks/drops every other model's chunks (root cause: nexus-3l6gz).

    Groups collections by :func:`embedding_model_for_collection_name` so
    each combined-query call is model-homogeneous. A collection whose name
    is not conformant (the parse returns ``None``) is kept in its OWN
    singleton group keyed by its raw collection name — never guessed into
    an inferred model group and never silently dropped.

    Preserves ``target``'s relative ordering both across and within groups.
    """
    groups: dict[str, list[str]] = {}
    for name in target:
        key = embedding_model_for_collection_name(name) or name
        groups.setdefault(key, []).append(name)
    return list(groups.values())


def _distance_key(row: dict) -> float:
    """Sort key for combined-query merge rows: missing OR ``None`` -> +inf.

    ``row.get("distance", ...)`` alone only covers a MISSING key — a row
    carrying an explicit ``None`` distance (server anomaly) would still
    reach the sort as ``None`` and raise ``TypeError`` when compared against
    a ``float``. Centralized here so every merge-sort site (the two
    model-group merges below, and ``search_topic_scoped``'s pre-existing
    per-collection merge) hardens uniformly instead of drifting per-site.

    nexus-znwc2: the sentinel is ``+inf`` (sorts LAST), not 0.0 — a
    distance-less row promoted to best-match silently corrupted relevance
    in every service-mode merge whenever a field was stripped in transit.
    """
    d = row.get("distance")
    return d if d is not None else float("inf")


def _reported_distances(rows: list[dict]) -> list[float | None]:
    """The emitted structured ``distances`` list: missing/None stays ``None``.

    nexus-znwc2 / nexus-3809x: never fabricate 0.0 (a perfect-match score)
    for a stripped field — and never emit ``float('inf')`` either: the MCP
    text serializer renders it as the bare ``Infinity`` token (invalid JSON,
    breaks strict clients) while pydantic's structuredContent turns the same
    value into ``null``. ``+inf`` lives ONLY inside :func:`_distance_key`'s
    sort ordering; the emitted value for an unknown distance is an honest,
    JSON-safe ``None``, logged loud.
    """
    distances = [r.get("distance") for r in rows]
    missing = sum(1 for d in distances if d is None)
    if missing:
        _log.warning(
            "structured_rows_missing_distance",
            missing=missing, total=len(rows),
            consequence="emitting null distances (never a fabricated 0.0)",
        )
    return distances


def _grouped_combined_query(
    target: list[str],
    call: Callable[[list[str]], list[dict]],
) -> list[dict]:
    """Run *call* once per embedding-model group in *target*, merge the results.

    nexus-3l6gz / nexus-hg745: a combined-query service call embeds the
    query string ONCE and ranks every listed collection's chunks against
    that single query vector, so a call spanning collections from more than
    one embedding model silently mis-ranks/drops the non-first model's
    chunks. This splits *target* into model-homogeneous groups via
    :func:`_group_collections_by_model`, invokes *call* once per group (the
    single shared shape now used by ``search_metadata_scoped``,
    ``search_graph_hop``, and ``query()``'s service-mode catalog branch —
    extracted so the loop-and-merge logic has one hardening point instead of
    drifting across per-site copies), and concatenates the per-group rows
    ordered distance-ascending across groups. Each group individually
    arrives distance-ascending from the service, but the concatenation
    across groups is NOT itself globally sorted, so callers must NOT rely on
    ordering until after this merge sort.

    All-or-nothing by design: *call* is invoked synchronously per group with
    no per-iteration try/except, so a later group's exception propagates
    immediately and the caller gets NO partial result set from only the
    groups that happened to succeed first — matching
    ``search_topic_scoped``'s existing (uncaught) per-collection loop.

    CAVEAT: when *target* spans more than one embedding model, the merge
    ranks rows by raw cosine distance across DIFFERENT embedding-model
    vector spaces (e.g. ``voyage-code-3`` vs ``voyage-context-3``).
    Distance values from different models are not a rigorously comparable
    metric — the merge order can carry a systematic per-model bias. This is
    the same class of accepted caveat ``search_topic_scoped`` already
    documents for its own per-collection merge.
    """
    rows: list[dict] = []
    for group in _group_collections_by_model(target):
        rows.extend(call(group))
    rows.sort(key=_distance_key)
    return rows


def _dedup_by_id(rows: list[dict]) -> list[dict]:
    """Collapse *rows* to one row per ``id``, keeping the best (lowest) distance.

    Assumes *rows* is already globally distance-ascending (e.g. the output
    of :func:`_grouped_combined_query`) so the FIRST occurrence of an id is
    its best. Does NOT truncate — callers that need the pre-truncation count
    (e.g. ``query()``'s "N of M documents" footer) call this directly; callers
    that only need the final page call :func:`_dedup_by_id_keep_best`.
    """
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in rows:
        rid = r.get("id", "")
        if rid in seen:
            continue
        seen.add(rid)
        deduped.append(r)
    return deduped


def _dedup_by_id_keep_best(rows: list[dict], limit: int) -> list[dict]:
    """:func:`_dedup_by_id` then truncate to *limit*.

    Truncates AFTER the dedup: each model group may independently
    contribute up to the caller's fetch size, so the merged/deduped set can
    exceed *limit* even though no single group did.
    """
    return _dedup_by_id(rows)[:limit]


@mcp.tool(
    title="Metadata-Scoped Combined Search",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def search_metadata_scoped(
    query: str,
    corpus: str = "knowledge,code,docs",
    limit: int = 10,
    content_type: str = "",
    author: str = "",
    year: int = 0,
    subtree: str = "",
    where: str = "",
    structured: bool = False,
) -> "str | dict":
    """Metadata-scoped combined search (RDR-156 P4, Decision 5; catalog-008).

    The single-statement unification of the ``query`` tool's catalog-routing
    dance: ``nexus.search_metadata_scoped_<dim>`` joins the chunk table to the
    catalog manifest + documents and filters by catalog metadata in one query
    (HNSW survives the join). Document-level results (``id`` is the tumbler),
    deduped to one row per tumbler at the best (nearest) distance. Each row also
    carries the matched chunk's ``chash`` (RDR-086 ``chunk_text_hash`` source).

    Service-mode only — the combined-query functions live in the pgvector
    Postgres; in local/Chroma mode this returns an error.

    PLAN-RUNNER CAVEAT: the structured ``ids``/``tumblers`` are document tumblers,
    NOT chunk chashes. The runner's auto-hydration (``store_get_many``) is
    chash-keyed, so feeding ``$stepN.ids`` straight into an operator returns empty
    content — use a tumbler-aware hydration path (tracked: nexus-zekpl). The
    ``catalog_documents.corpus`` filter the SQL function supports is NOT exposed
    here (``corpus`` is the collection-routing arg); add it explicitly if needed.

    MULTI-MODEL CORPUS (nexus-3l6gz): when *corpus* resolves to collections
    spanning more than one embedding model (e.g. ``code`` + ``docs`` ->
    voyage-code-3 + voyage-context-3), this tool issues one combined-query
    call PER model group and merges the results — see
    :func:`_grouped_combined_query`. The merge is ALL-OR-NOTHING: if any
    group's call raises, the whole tool call fails (via the outer
    try/except -> ``_mcp_tool_error``) and returns NO partial rows from
    groups that already succeeded. The merge also ranks rows by raw cosine
    distance across DIFFERENT embedding-model vector spaces when more than
    one group contributes — those distances are not rigorously comparable
    across models, so cross-model ordering can carry a systematic per-model
    bias (same accepted caveat as ``search_topic_scoped``'s merge).

    Args:
        query: Search query string.
        corpus: Corpus prefixes or collection names, comma-separated; "all" for all.
        limit: Max rows.
        content_type: Catalog content_type filter ("" = no filter).
        author: Catalog author SUBSTRING filter, case-insensitive (ILIKE; "" = no filter).
        year: Catalog year filter (0 = no filter).
        subtree: Tumbler-prefix scope, e.g. "1.2" → the DESCENDANTS of 1.2 (root-exclusive,
            matching the catalog's descendants()); alias rows excluded ("" = no filter).
        where: Chunk-metadata equality filter, ``KEY=VALUE`` comma-separated, e.g.
            "lang=java,kind=fn" ("" = no filter). Equality only (matches the service
            ``where`` semantics); applied as JSONB containment on chunk metadata.
        structured: Return ``{ids, tumblers, distances, collections, contents, chashes}``.
    """
    try:
        from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

        t3 = _get_t3()
        if not is_service_backed(t3):
            return ("Error: search_metadata_scoped requires service mode "
                    "(pgvector); not available in local/Chroma mode")
        target = _resolve_corpus_target(corpus, t3)
        if not target:
            return f"No collections match corpus {corpus!r}"
        # Parse the KEY=VALUE,... where string into a typed equality dict (applied as
        # JSONB containment on chunk metadata). search_metadata_scoped is EQUALITY-ONLY
        # (matches the service `where` semantics); reject comparison operators LOUDLY
        # rather than silently mis-parsing them (a raw split would turn "bib_year>=2020"
        # into the bogus key "bib_year>" and drop the filter — nexus-889ff review).
        where_map: dict | None = None
        if where.strip():
            from nexus.filters import parse_where_str  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

            parsed = parse_where_str(where)
            if parsed and (
                "$and" in parsed or "$or" in parsed
                or any(isinstance(v, dict) for v in parsed.values())
            ):
                return ("Error: search_metadata_scoped `where` is equality-only "
                        "(KEY=VALUE, comma-separated); comparison operators "
                        "(>=, <=, >, <, !=) are not supported in service mode")
            where_map = parsed or None
        # nexus-3l6gz / nexus-hg745: one combined-query call per
        # embedding-model group, ALL-OR-NOTHING (a group's exception
        # propagates and aborts the whole merge — no silent partial
        # results). Each group is asked for up to `limit` rows (not
        # limit/n_groups) so a group's true top-N isn't truncated away
        # before the merge decides the global top-N. See
        # _grouped_combined_query for the full contract.
        rows = _grouped_combined_query(target, lambda group: t3.search_metadata_scoped(
            query, group,
            content_type=content_type or None,
            author=author or None,
            year=(year or None),
            subtree=(subtree or None),
            where=(where_map or None),
            n_results=limit,
        ))
        # Metadata-scoped is document-level: the function returns one row per
        # matching CHUNK, so a multi-chunk document repeats its tumbler. Collapse
        # to one row per id, keeping the best (nearest) distance, and truncate
        # to `limit` AFTER the merge (see _dedup_by_id_keep_best).
        rows = _dedup_by_id_keep_best(rows, limit)
        if structured:
            # id IS the document tumbler for metadata-scoped (document-level).
            ids = [r.get("id", "") for r in rows]
            return {
                "ids": ids,
                "tumblers": ids,
                "distances": _reported_distances(rows),
                "collections": [r.get("collection", "") for r in rows],
                # contents inline so plan steps can summarize directly via
                # $stepN.contents WITHOUT store_get_many hydration (which is
                # chash-keyed and would miss these document tumblers).
                "contents": [r.get("content", "") for r in rows],
                # matched-chunk chash per row — the repoint wires this into the
                # structured chunk_text_hash (RDR-086 chash-citations).
                "chashes": [r.get("chash", "") for r in rows],
            }
        if not rows:
            return "No documents found."
        return _cap_text_result("\n\n".join(
            f"[{r.get('collection', '')}] {r.get('id', '')} (dist={r.get('distance', 0.0):.4f})"
            f"\n{r.get('content', '')}"
            for r in rows
        ), "search_metadata_scoped")
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("search_metadata_scoped", e)


@mcp.tool(
    title="Topic-Scoped Combined Search",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def search_topic_scoped(
    query: str,
    topic: str,
    corpus: str = "knowledge,code,docs",
    limit: int = 10,
    structured: bool = False,
) -> "str | dict":
    """Topic-scoped combined search (RDR-156 P4, Decision 5).

    ``nexus.search_topic_scoped_<dim>`` joins the chunk table to
    ``topic_assignments`` on chunk chash (topic membership is chunk-level,
    nexus-sa14p) and ranks by vector distance. Chunk-level results (``id`` is
    the chunk chash). Resolved across every collection in *corpus* (topics are
    per-collection — a label belongs to one collection's taxonomy, so the
    multi-collection loop is usually single-hit), merged by distance. NOTE: the
    merge is per-collection-limit-then-merge-then-truncate, so for a label genuinely
    present in multiple collections the global top-N can drop a collection's
    (limit+1)th row that would have ranked; over-fetch per collection if that case
    becomes real.

    Service-mode only.

    Args:
        query: Search query string.
        topic: Topic label (from ``nx taxonomy discover``).
        corpus: Corpus prefixes or collection names, comma-separated; "all" for all.
        limit: Max rows.
        structured: Return ``{ids, tumblers, distances, collections}`` for the plan runner.
    """
    try:
        from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

        t3 = _get_t3()
        if not is_service_backed(t3):
            return ("Error: search_topic_scoped requires service mode "
                    "(pgvector); not available in local/Chroma mode")
        target = _resolve_corpus_target(corpus, t3)
        if not target:
            return f"No collections match corpus {corpus!r}"
        merged: list[dict] = []
        for col in target:
            merged.extend(t3.search_topic_scoped(query, topic, col, n_results=limit))
        merged.sort(key=_distance_key)
        merged = merged[:limit]
        if structured:
            # Chunk-level: ids are chunk chashes; no document tumbler.
            return {
                "ids": [r.get("id", "") for r in merged],
                "tumblers": ["" for _ in merged],
                "distances": _reported_distances(merged),
                "collections": [r.get("collection", "") for r in merged],
                # contents inline so plan steps summarize via $stepN.contents
                # without hydration (topic ids are chunk chashes).
                "contents": [r.get("content", "") for r in merged],
            }
        if not merged:
            return f"No chunks found for topic {topic!r}."
        return _cap_text_result("\n\n".join(
            f"[{r.get('collection', '')}] {r.get('id', '')} (dist={r.get('distance', 0.0):.4f})"
            f"\n{r.get('content', '')}"
            for r in merged
        ), "search_topic_scoped")
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("search_topic_scoped", e)


@mcp.tool(
    title="Graph-Hop Combined Search",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def search_graph_hop(
    query: str,
    seeds: list[str] | str,
    corpus: str = "knowledge,code,docs",
    limit: int = 10,
    link_type: str = "",
    depth: int = 1,
    direction: str = "both",
    where: str = "",
    structured: bool = False,
) -> "str | dict":
    """Graph-hop combined search (RDR-156 P4 follow-on, Decision 5, bead nexus-houg9).

    The single-statement unification of the ``query`` tool's ``follow_links`` dance:
    ``nexus.search_graph_hop_<dim>`` runs a ``WITH RECURSIVE`` BFS over
    ``catalog_links`` from *seeds* to *depth* hops, collects the reachable document
    set, joins the unified ``nexus.chunks`` table (post-RDR-191; the function keeps
    its ``_<dim>`` suffix, but the table it joins is no longer a per-dim shard) and
    vector-ranks — replacing the app-side graphBFS +
    per-collection search + re-join. Document-level results (``id`` is the tumbler),
    deduped to one row per tumbler at the best (nearest) distance. Each row also carries
    the MATCHED chunk's ``chash`` (so the query() repoint populates the RDR-086
    ``chunk_text_hash`` from it, never a per-doc manifest guess).

    Seeds are document tumblers; ``depth`` is clamped to [1,3] service-side; an empty
    ``link_type`` follows all edge types; ``direction`` is ``"out"``/``"in"``/``"both"``
    (default ``"both"``, matching ``Catalog.graph`` / the ``query`` tool's follow_links).

    Service-mode only — the combined-query functions live in the pgvector Postgres; in
    local/Chroma mode this returns an error.

    MULTI-MODEL CORPUS (nexus-3l6gz): when *corpus* resolves to collections
    spanning more than one embedding model (e.g. ``code`` + ``docs`` ->
    voyage-code-3 + voyage-context-3), this tool issues one combined-query
    call PER model group and merges the results — see
    :func:`_grouped_combined_query`. The merge is ALL-OR-NOTHING: if any
    group's call raises, the whole tool call fails (via the outer
    try/except -> ``_mcp_tool_error``) and returns NO partial rows from
    groups that already succeeded. The merge also ranks rows by raw cosine
    distance across DIFFERENT embedding-model vector spaces when more than
    one group contributes — those distances are not rigorously comparable
    across models, so cross-model ordering can carry a systematic per-model
    bias (same accepted caveat as ``search_topic_scoped``'s merge).

    Args:
        query: Search query string.
        seeds: Seed document tumbler(s) to traverse from (list, or a single string).
        corpus: Corpus prefixes or collection names, comma-separated; "all" for all.
        limit: Max rows.
        link_type: Catalog link_type filter ("" = follow all edge types).
        depth: BFS depth (clamped to [1,3]).
        direction: "out" | "in" | "both".
        where: Chunk-metadata equality filter, ``KEY=VALUE`` comma-separated
            ("" = no filter). Equality only — applied as JSONB containment on
            chunk metadata in the post-BFS rank, matching
            ``search_metadata_scoped``'s ``where`` semantics (nexus-7ndh3).
        structured: Return ``{ids, tumblers, distances, collections, contents, chashes}``.
    """
    try:
        from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

        t3 = _get_t3()
        if not is_service_backed(t3):
            return ("Error: search_graph_hop requires service mode "
                    "(pgvector); not available in local/Chroma mode")
        seed_list = [seeds] if isinstance(seeds, str) else list(seeds)
        seed_list = [s for s in seed_list if s]
        if not seed_list:
            return "No seeds provided."
        target = _resolve_corpus_target(corpus, t3)
        if not target:
            return f"No collections match corpus {corpus!r}"
        # EQUALITY-ONLY, like search_metadata_scoped: the SQL predicate is JSONB
        # containment, which cannot express comparison operators. Reject LOUDLY
        # rather than letting an operator dict silently containment-fail to zero
        # rows (nexus-889ff pattern; nexus-7ndh3 critique).
        where_dict = _parse_where_str(where)
        if where_dict and (
            "$and" in where_dict or "$or" in where_dict
            or any(isinstance(v, dict) for v in where_dict.values())
        ):
            return ("Error: search_graph_hop `where` is equality-only "
                    "(KEY=VALUE, comma-separated); comparison operators "
                    "(>=, <=, >, <, !=) are not supported in service mode")
        # nexus-3l6gz / nexus-hg745: one combined-query call per
        # embedding-model group, ALL-OR-NOTHING (a group's exception
        # propagates and aborts the whole merge — no silent partial
        # results). Each group is asked for up to `limit` rows (not
        # limit/n_groups) so a group's true top-N isn't truncated away
        # before the merge decides the global top-N. See
        # _grouped_combined_query for the full contract.
        rows = _grouped_combined_query(target, lambda group: t3.search_graph_hop(
            query, seed_list, group,
            link_type=(link_type or None),
            depth=depth,
            direction=direction,
            where=(where_dict or None),
            n_results=limit,
        ))
        # Document-level: collapse to one row per tumbler, keeping the best
        # (nearest) distance, and truncate to `limit` AFTER the merge (see
        # _dedup_by_id_keep_best).
        rows = _dedup_by_id_keep_best(rows, limit)
        if structured:
            ids = [r.get("id", "") for r in rows]
            return {
                "ids": ids,
                "tumblers": ids,
                "distances": _reported_distances(rows),
                "collections": [r.get("collection", "") for r in rows],
                # contents inline so plan steps summarize via $stepN.contents WITHOUT
                # store_get_many hydration (which is chash-keyed and would miss tumblers).
                "contents": [r.get("content", "") for r in rows],
                # the matched chunk's chash per row — rzqto wires this into the
                # structured chunk_text_hash (RDR-086 chash-citations).
                "chashes": [r.get("chash", "") for r in rows],
            }
        if not rows:
            return "No documents found."
        return _cap_text_result("\n\n".join(
            f"[{r.get('collection', '')}] {r.get('id', '')} (dist={r.get('distance', 0.0):.4f})"
            f"\n{r.get('content', '')}"
            for r in rows
        ), "search_graph_hop")
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("search_graph_hop", e)


@mcp.tool(
    title="Aspect-Scoped Combined Search",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def search_aspect_scoped(
    query: str,
    corpus: str = "knowledge,code,docs",
    limit: int = 10,
    field: str = "",
    pattern: str = "",
    min_confidence: float = 0.0,
    where: str = "",
    structured: bool = False,
) -> "str | dict":
    """Aspect-scoped combined search (RDR-156 Decision 5, bead nexus-ubnwk).

    The single-statement unification of the ``search`` + ``operator_filter(source=
    "aspects")`` two-step app-side path, for the case where the aspect predicate is
    selective: that path filters AFTER the vector top-N truncation and can silently
    miss a distant match the aspect predicate would otherwise have kept (RECALL loss
    exactly when the predicate is selective). ``nexus.search_aspect_scoped_<dim>``
    joins the chunk table to the catalog manifest + documents + ``document_aspects``
    (on ``doc_id = tumbler``) and applies *field*/*pattern*/*min_confidence*/*where*
    inside the SAME statement as the vector rank, so selectivity gates the scan.
    Document-level results (``id`` is the tumbler), deduped to one row per tumbler at
    the best (nearest) distance. Each row also carries the matched chunk's ``chash``
    (RDR-086 ``chunk_text_hash`` source).

    DOC_ID PRECONDITION: a document with no ``document_aspects`` row, or whose row's
    ``doc_id`` is still NULL, never joins and is excluded — by design, not a bug.
    This tool does NOT retire ``operator_filter(source="aspects")`` (keyed on
    ``source_uri``, not ``doc_id`` — it can see rows this tool cannot, and vice versa
    for the recall case above); both remain available, each covering a case the
    other does not.

    REAL COVERAGE OF THE doc_id BACKFILL (not edge-case drift — structural, by
    corpus): ``aspects-004-doc-id-backfill.xml`` attributes a row only when
    ``document_aspects.source_uri`` is byte-for-byte equal to the catalog document's
    own ``source_uri``. Holds for file-keyed corpora (``code__``/``docs__``/``rdr__``,
    both sides key off the same ``file://`` path). Does NOT hold for ``knowledge__``
    collections (the dominant aspects corpus): the catalog's ``source_uri`` there is
    derived from the document TITLE (``src/nexus/catalog/store_hook.py:286``), while
    the aspect extractor's ``source_uri`` is built from ``source_path``
    (``src/nexus/aspect_readers.py:172``, often itself a content-hash string) — two
    different identity fields for the same document family, not one field spelled
    two ways. The large majority of ``knowledge__`` ``document_aspects`` rows
    therefore stay ``doc_id`` NULL after the backfill and only become visible to
    this tool as they are RE-EXTRACTED under ``nexus-x1de2``'s go-forward stamping
    (which keys off the extraction queue's own ``doc_id``, not ``source_uri``, so it
    does not retroactively fix legacy rows). Gap-fill for the ``knowledge__`` family
    is tracked as ``nexus-bocft``.

    FIELD ALLOWLIST (locked wire contract, identical to
    ``HttpVectorClient.ASPECT_SCOPED_FIELD_ALLOWLIST``, the Java 400-guard, and the
    SQL function's CASE): ``problem_formulation``, ``proposed_method``,
    ``experimental_datasets``, ``experimental_baselines``, ``experimental_results``.
    NOT ``extras`` or ``salient_sentences`` — both are ``jsonb`` (aspects-003-type-
    hygiene.xml), not plain text, and the pre-existing two-step path
    (``AspectRepository.ALLOWED_ASPECT_COLUMNS``) already excludes them from
    substring filtering for the same reason. An unrecognized *field* is rejected
    here BEFORE the network call (the engine also 400s, a second, independent line
    of defense).

    Service-mode only — the combined-query functions live in the pgvector Postgres;
    in local/Chroma mode this returns an error.

    PLAN-RUNNER CAVEAT (nexus-zekpl, same as ``search_metadata_scoped`` /
    ``search_graph_hop``): the structured ``ids``/``tumblers`` are document
    tumblers, NOT chunk chashes — the runner's chash-keyed auto-hydration
    (``store_get_many``) needs a tumbler-aware hydration path to consume them.

    MULTI-MODEL CORPUS (nexus-3l6gz): when *corpus* resolves to collections
    spanning more than one embedding model, this tool issues one combined-query
    call PER model group and merges the results — see :func:`_grouped_combined_query`.
    The merge is ALL-OR-NOTHING (same accepted caveat as every sibling combined-query
    tool's merge, including the cross-model raw-cosine-distance caveat).

    Args:
        query: Search query string.
        corpus: Corpus prefixes or collection names, comma-separated; "all" for all.
        limit: Max rows.
        field: One of the field allowlist above ("" = no field-text filter, i.e.
            confidence/where only).
        pattern: Substring to match (case-insensitive) within *field*; ignored when
            *field* is "".
        min_confidence: Aspects confidence floor, inclusive (0.0 = no floor).
        where: Chunk-metadata equality filter, ``KEY=VALUE`` comma-separated
            ("" = no filter). Equality only; applied as JSONB containment on chunk
            metadata — orthogonal to the aspects predicate, not a substitute for it.
        structured: Return ``{ids, tumblers, distances, collections, contents, chashes}``.
    """
    try:
        from nexus.db.http_vector_client import HttpVectorClient, is_service_backed  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

        t3 = _get_t3()
        if not is_service_backed(t3):
            return ("Error: search_aspect_scoped requires service mode "
                    "(pgvector); not available in local/Chroma mode")
        if field and field not in HttpVectorClient.ASPECT_SCOPED_FIELD_ALLOWLIST:
            return (f"Error: search_aspect_scoped field {field!r} is not a known "
                    f"aspect field; allowed: "
                    f"{sorted(HttpVectorClient.ASPECT_SCOPED_FIELD_ALLOWLIST)}")
        target = _resolve_corpus_target(corpus, t3)
        if not target:
            return f"No collections match corpus {corpus!r}"
        where_map: dict | None = None
        if where.strip():
            from nexus.filters import parse_where_str  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

            parsed = parse_where_str(where)
            if parsed and (
                "$and" in parsed or "$or" in parsed
                or any(isinstance(v, dict) for v in parsed.values())
            ):
                return ("Error: search_aspect_scoped `where` is equality-only "
                        "(KEY=VALUE, comma-separated); comparison operators "
                        "(>=, <=, >, <, !=) are not supported in service mode")
            where_map = parsed or None
        # nexus-3l6gz / nexus-hg745: one combined-query call per embedding-model
        # group, ALL-OR-NOTHING. See _grouped_combined_query for the full contract.
        rows = _grouped_combined_query(target, lambda group: t3.search_aspect_scoped(
            query, group,
            field=(field or None),
            pattern=(pattern or None),
            min_confidence=(min_confidence or None),
            where=(where_map or None),
            n_results=limit,
        ))
        # Document-level: the function returns one row per matching CHUNK, so a
        # multi-chunk document repeats its tumbler. Collapse to one row per id,
        # keeping the best (nearest) distance, and truncate to `limit` AFTER the
        # merge (see _dedup_by_id_keep_best).
        rows = _dedup_by_id_keep_best(rows, limit)
        if structured:
            ids = [r.get("id", "") for r in rows]
            return {
                "ids": ids,
                "tumblers": ids,
                "distances": _reported_distances(rows),
                "collections": [r.get("collection", "") for r in rows],
                "contents": [r.get("content", "") for r in rows],
                "chashes": [r.get("chash", "") for r in rows],
            }
        if not rows:
            return "No documents found."
        return _cap_text_result("\n\n".join(
            f"[{r.get('collection', '')}] {r.get('id', '')} (dist={r.get('distance', 0.0):.4f})"
            f"\n{r.get('content', '')}"
            for r in rows
        ), "search_aspect_scoped")
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("search_aspect_scoped", e)


# nexus-descendants-seed-cap: defensive ceiling on the graph-hop seed list
# query() materializes client-side from an explicit `subtree` scope
# (ab7907fb follow-on — descendants() itself no longer truncates a large
# subtree, so this is the next place an unbounded list could reach the
# wire). There is no dedicated per-request cap on POST
# /v1/vectors/search-graph-hop's `seeds` array server-side (it binds as a
# single Postgres array parameter, not a per-item IN-list, so the
# resolve_many-style 32767-bind-limit rationale does not directly apply).
#
# Substantive critique finding 3 (T2 nexus/chroma-residue-C2-durability-
# critique-2026-08-10): the original 1000 was a borrowed number ("it seemed
# safe") that caps the plan's own flagship reproduction subtree
# (4,797 documents, ~5x the old cap) — the exact scale this fix was written
# for. Measured before raising it (this repo has no live large-linked
# corpus in test fixtures, so the measurement is of what CAN be ruled out,
# not a live breaking point): JSON-encoding a synthetic seeds array is
# trivial at every size that matters — n=1000 -> 11KB/0.04ms, n=5000 ->
# 59KB/0.19ms, n=10000 -> 119KB/0.35ms, n=20000 -> 249KB/0.65ms (measured
# 2026-08-10, python3 json.dumps on this endpoint's exact wire shape) — so
# request-body size/encode cost is not a limiting factor anywhere near this
# range. Combined with the two source-verified facts above (no server-side
# per-request cap on this endpoint; seeds bind as ONE array parameter, not
# per-item, so no bind-limit concern), the only real unknown is server-side
# BFS compute cost, which scales with the REACHABLE EDGE set (bounded
# separately by CatalogRepository.MAX_GRAPH_NODES=500, service/**), not
# directly with the seed count — and that was not measured here (no live
# large-graph corpus available in this environment; a synthetic/nonexistent-
# tumbler seed list would only measure IN-list lookup overhead against empty
# results, not real BFS fanout, so it would not be a meaningful proxy).
# 6000 is chosen as a deliberate, disclosed safety valve — not "no cap" —
# sized to clear the known real workload (4,797) with >25% headroom, while
# still bounding the pathological case (a `subtree` prefix that resolves to
# tens of thousands of descendants) that disclosure alone does not protect
# against. MUST stay paired with disclosure (`seed_scope` below) — a cap
# that silently drops seeds reintroduces the exact silent-truncation class
# ab7907fb fixed one layer down. Revisit with a real measurement if a
# corpus this size ever lands in test fixtures.
_MAX_GRAPH_HOP_SEEDS = 6000


@mcp.tool(
    title="Catalog-Aware Document Query",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def query(
    question: str,
    corpus: str = "knowledge",
    where: str = "",
    limit: int = 10,
    author: str = "",
    content_type: str = "",
    follow_links: str = "",
    depth: int = 1,
    subtree: str = "",
    structured: bool = False,
) -> "str | dict":
    """Document-level semantic search for analytical questions.

    Results are capped at ``limit``. When more documents match, a footer line shows
    the total count. Increase ``limit`` to see more.

    Unlike ``search`` which returns individual chunks, ``query`` groups results
    by source document and returns the best-matching snippet per document along
    with full metadata (title, year, citations, page count, extraction method).

    Use this for research questions where you need to know WHICH documents match,
    not just which text fragments. The calling agent handles analysis/synthesis.

    Catalog-aware routing (optional — all require an initialized catalog AND
    service mode (pgvector); RDR-156 P4.2c removed the local/Chroma-mode
    fallback these params used to have — a non-service T3 with any catalog
    param set returns a loud error instead):
        author: Filter to documents by this author (catalog metadata search)
        content_type: Filter to documents of this type (code, paper, rdr, knowledge)
        follow_links: Follow links of this type from catalog results (e.g. "cites", "implements").
            Linked collections are merged (interleaved) with seed collections — results
            are ranked by semantic distance across all collections, not separated by source.
        depth: BFS depth for follow_links traversal (default 1)
        subtree: Tumbler prefix — search only documents in this subtree (e.g. "1.1")

    MULTI-MODEL CORPUS (nexus-3l6gz / nexus-hg745): when a catalog param is
    set AND *corpus* resolves to collections spanning more than one
    embedding model (e.g. ``corpus="all"`` or ``"code,docs"``), this tool's
    catalog-param branch issues one combined-query call PER model group and
    merges — see :func:`_grouped_combined_query`. Same ALL-OR-NOTHING
    semantics and cross-model raw-cosine-distance caveat as
    ``search_metadata_scoped`` / ``search_graph_hop``.

    Ranking: on the plain corpus-based path (no catalog
    params), both the winning chunk per document and the document order are
    chosen by ``hybrid_score`` — vector similarity blended with
    ``frecency_score`` (honouring ``.nexus.yml`` ``[tuning]`` weights
    resolved from THIS MCP SERVER PROCESS'S OWN LAUNCH DIRECTORY, not the
    repo the caller may be asking about) and the RDR-055 bibliographic
    quality boost, via ``search_engine.apply_ranking_boosts`` (the same
    function ``nx search`` calls) — not by raw distance. The per-document
    ``distance`` field reported in output is still the raw vector distance
    of that winning chunk, unaffected by the boost. The CATALOG-PARAM path
    (author, content_type, follow_links, subtree) does NOT apply this
    boost: its rows come from the engine's SQL combined-query functions and
    carry only ``(id, content, distance, collection, chash)`` — no
    ``frecency_score`` or ``bib_citation_count`` to boost with.

    Args:
        question: Natural-language research question
        corpus: Corpus prefix or full collection name (default: knowledge).
                Use "all" for all corpora.
                Note: when catalog params (author, content_type, subtree) are provided,
                corpus is overridden by the resolved catalog collections.
        where: Metadata filter — KEY=VALUE, comma-separated.
               Example: "tags=arch" (equality only).
               Comparison operators (e.g. "bib_year>=2020") and operator-shaped
               filters ($and/$or) are supported ONLY when no catalog param
               (author, content_type, follow_links, subtree) is set — combined
               with a catalog param, `where` is equality-only (the combined-query
               path applies it as JSONB containment) and an operator shape
               returns a loud error naming the workarounds (equality `where`,
               the `search` tool, or dropping the catalog params).
        limit: Maximum documents to return (default 10)
        author: Filter by author (catalog metadata)
        content_type: Filter by content type (catalog metadata)
        follow_links: Follow link type from matched documents (catalog graph)
        depth: BFS depth for follow_links (default 1)
        subtree: Tumbler prefix to scope search to a subtree
    """
    try:
        from nexus.config import get_tuning_config, load_config  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
        from nexus.filters import sanitize_query  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
        from nexus.search_engine import apply_ranking_boosts, search_cross_corpus  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

        cfg = load_config()
        if cfg.get("search", {}).get("query_sanitizer", True):
            question = sanitize_query(question)

        t3 = _get_t3()

        # nexus-descendants-seed-n1 / substantive critique finding 2: declared
        # here (not inside `if has_catalog_params:`) so it is ALWAYS defined
        # by the time the structured-result/text-form code below reads it —
        # a plain corpus-based query (no catalog params at all) must see
        # None, not a NameError. RDR-156 P4.2c (nexus-2bqpn) deleted its
        # sole producer (the app-side follow_links dance in local/Chroma
        # mode); this is permanently None now, kept only for that reason.
        graph_batch_info: dict | None = None
        has_catalog_params = author or content_type or follow_links or subtree

        if has_catalog_params:
            from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
            cat = _get_catalog()
            if cat is None:
                return "Error: catalog not initialized — catalog params (author, content_type, follow_links, subtree) require 'nx catalog setup'"

            # Guard: document-level subtree address (3+ segments) cannot have descendants
            if subtree:
                subtree_depth = len(subtree.split("."))
                if subtree_depth >= 3:
                    return f"Error: subtree '{subtree}' is a document-level address — use an owner prefix (e.g., '{'.'.join(subtree.split('.')[:2])}') to search a subtree"

            # ── CATALOG-PARAM PATH (nexus-rzqto; dance deleted nexus-2bqpn) ──
            # Catalog params route through the combined-query SQL functions
            # (search_metadata_scoped / search_graph_hop), which perform the
            # catalog-metadata join server-side and return document-level rows
            # (id=tumbler, content, distance, collection, chash). RDR-156
            # P4.2c deleted the app-side catalog-dance + search_cross_corpus
            # fallback that used to run when the combined path couldn't be
            # used — this is now the ONLY catalog-param path. Two
            # preconditions the dance used to silently absorb are now loud
            # rejects instead:
            #
            #   1. Non-service T3 (local/Chroma-shaped substrate): the
            #      combined-query functions live in the pgvector Postgres
            #      only. Production T3 is always HttpVectorClient post-
            #      RDR-158; non-service is the InMemoryVectorClient unit-test
            #      substrate.
            #   2. An operator-shaped `where` ($and/$or/any comparison
            #      operator, e.g. {"k": {"$gte": ...}}): the SQL predicate on
            #      both combined-query functions is JSONB containment
            #      (equality-only) — an operator shape would silently
            #      containment-fail to zero rows if allowed through.
            #
            # bib richness (nexus-rzqto): the combined functions return only
            # (id=tumbler, content, distance, collection, chash), so the text
            # form re-hydrates title/author/year AND bib_year/bib_authors/
            # bib_venue/bib_citation_count per tumbler from CatalogEntry — the
            # Java catalog already serializes the bib_* columns
            # (CatalogRepository.docRowFromRecord), surfaced onto CatalogEntry.
            from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
            if not is_service_backed(t3):
                return (
                    "Error: query catalog params (author, content_type, "
                    "follow_links, subtree) require service mode (pgvector); "
                    "not available in local/Chroma mode"
                )
            _where_dict = _parse_where_str(where)
            _operator_where = bool(_where_dict) and (
                "$and" in _where_dict or "$or" in _where_dict
                or any(isinstance(v, dict) for v in _where_dict.values())
            )
            if _operator_where:
                return (
                    "Error: query catalog params (author, content_type, "
                    "follow_links, subtree) combined with an operator-shaped "
                    "`where` ($and/$or, or a comparison operator like "
                    "{'k': {'$gte': ...}}) are not supported — the combined-"
                    "query path supports equality-only `where` with catalog "
                    "params. Use (a) an equality-only `where`, (b) the "
                    "`search` tool (chunk-level, retains full operator "
                    "support), or (c) drop the catalog params."
                )
            # Broad corpus target: the SQL functions filter by catalog
            # metadata internally, so pass the full corpus set.
            target = _resolve_corpus_target(corpus, t3)
            if not target:
                return f"No collections match corpus {corpus!r}"

            where_dict = _where_dict  # equality-only here: the operator-where guard above loud-rejects operator shapes
            fetch_n = limit * 10

            _no_docs_msg = (
                f"No documents found matching catalog filters "
                f"(author={author!r}, content_type={content_type!r}, "
                f"subtree={subtree!r}, follow_links={follow_links!r})"
            )

            # Disclosure envelope for a capped subtree seed list
            # (nexus-descendants-seed-cap) — stays None unless the
            # follow_links+subtree arm below actually runs; surfaced in
            # both the text and structured responses so a cap is never
            # silent.
            seed_scope: dict | None = None

            if follow_links:
                # Graph-hop path: seeds resolved app-side, BFS in SQL.
                seed_tumblers: list[str] = []
                if subtree:
                    desc = cat.descendants(subtree)
                    all_seed_tumblers = [d["tumbler"] for d in desc if d.get("tumbler")]
                    seed_tumblers = all_seed_tumblers[:_MAX_GRAPH_HOP_SEEDS]
                    seed_scope = {
                        "total": len(all_seed_tumblers),
                        "used": len(seed_tumblers),
                        "truncated": len(all_seed_tumblers) > _MAX_GRAPH_HOP_SEEDS,
                    }
                elif author or content_type:
                    if content_type and not author:
                        seed_entries_svc = cat.by_content_type(content_type)
                    else:
                        seed_entries_svc = cat.find(author, content_type=content_type or None)
                        seed_entries_svc = [
                            r for r in seed_entries_svc
                            if author.lower() in (r.author or "").lower()
                        ]
                    seed_tumblers = [str(r.tumbler) for r in seed_entries_svc if r.tumbler]
                else:
                    # follow_links only: use question as catalog seed
                    seed_results_svc = cat.find(question)
                    seed_tumblers = [str(r.tumbler) for r in seed_results_svc[:5] if r.tumbler]

                # nexus-3l6gz / nexus-hg745: route through
                # _grouped_combined_query — a single client call cannot
                # rank a corpus spanning more than one embedding model
                # against one query vector (this branch is query()'s
                # canonical entry point and reproduced the exact
                # nexus-3l6gz symptom via corpus="all"/"code,docs" +
                # author/content_type/follow_links/subtree before this
                # fix). ALL-OR-NOTHING: a group's exception aborts the
                # whole merge, matching search_metadata_scoped /
                # search_graph_hop above.
                if not seed_tumblers:
                    # No graph seeds resolved — fall through to
                    # search_metadata_scoped over the broad target
                    # (mirrors the now-deleted dance's fallback to broad
                    # search when no seed collections resolved).
                    rows = _grouped_combined_query(
                        target, lambda group: t3.search_metadata_scoped(
                            question, group,
                            content_type=content_type or None,
                            author=author or None,
                            subtree=(subtree or None),
                            where=(where_dict or None),
                            n_results=fetch_n,
                        ))
                else:
                    rows = _grouped_combined_query(
                        target, lambda group: t3.search_graph_hop(
                            question, seed_tumblers, group,
                            link_type=(follow_links or None),
                            depth=depth,
                            where=(where_dict or None),
                            n_results=fetch_n,
                        ))
            else:
                # Metadata-scoped path: catalog filters pushed into SQL.
                rows = _grouped_combined_query(
                    target, lambda group: t3.search_metadata_scoped(
                        question, group,
                        content_type=content_type or None,
                        author=author or None,
                        subtree=(subtree or None),
                        where=(where_dict or None),
                        n_results=fetch_n,
                    ))

            # Dedup: one row per tumbler, keeping best (lowest) distance.
            # deduped_svc (pre-truncation) feeds the "N of M documents"
            # header/footer below; rows is the truncated display page.
            deduped_svc = _dedup_by_id(rows)
            rows = deduped_svc[:limit]

            if not rows:
                if structured:
                    empty_result: dict = {
                        "ids": [], "tumblers": [], "distances": [],
                        "collections": [], "chunk_collections": [],
                        "chunk_text_hash": [],
                    }
                    if seed_scope is not None:
                        empty_result["seed_scope"] = seed_scope
                    return empty_result
                if seed_scope is not None and seed_scope["truncated"]:
                    return (
                        f"[WARNING: subtree seed list capped at "
                        f"{seed_scope['used']} of {seed_scope['total']} "
                        f"documents for graph-hop traversal — results may "
                        f"be INCOMPLETE. Narrow `subtree` or split into "
                        f"multiple queries.]\n{_no_docs_msg}"
                    )
                return _no_docs_msg

            if structured:
                tumblers_svc = [r.get("id", "") for r in rows]
                result: dict = {
                    "ids": tumblers_svc,
                    "tumblers": tumblers_svc,
                    "distances": _reported_distances(rows),
                    # sorted distinct across rows
                    "collections": sorted({r.get("collection", "") for r in rows}),
                    # per-row aligned (RDR-086 / review #7)
                    "chunk_collections": [r.get("collection", "") for r in rows],
                    # HIGH-1: chash per matched chunk row, not a manifest guess
                    "chunk_text_hash": [r.get("chash", "") for r in rows],
                }
                # nexus-descendants-seed-cap: disclose a capped subtree
                # seed list — never silent (see _MAX_GRAPH_HOP_SEEDS).
                if seed_scope is not None:
                    result["seed_scope"] = seed_scope
                return result

            # Text form: re-hydrate per tumbler from the catalog.
            parts_note = []
            if author:
                parts_note.append(f"author={author!r}")
            if content_type:
                parts_note.append(f"content_type={content_type!r}")
            if subtree:
                parts_note.append(f"subtree={subtree!r}")
            if follow_links:
                parts_note.append(f"follow_links={follow_links!r}")
            routing_note_svc = (
                f"[Catalog routing: {', '.join(parts_note)} -> {len(target)} collections]"
            )
            header_svc = (
                f"Found {len(rows)} documents "
                f"(from {len(deduped_svc)} across {len(target)} collections)"
            )
            lines_svc: list[str] = []
            if seed_scope is not None and seed_scope["truncated"]:
                # nexus-descendants-seed-cap: never silent — see
                # _MAX_GRAPH_HOP_SEEDS.
                lines_svc.append(
                    f"[WARNING: subtree seed list capped at "
                    f"{seed_scope['used']} of {seed_scope['total']} "
                    f"documents for graph-hop traversal — results may be "
                    f"INCOMPLETE. Narrow `subtree` or split into multiple "
                    f"queries.]"
                )
            lines_svc.append(f"{routing_note_svc}\n{header_svc}")
            lines_svc.append("")
            for i, row in enumerate(rows, 1):
                tumbler_str = row.get("id", "")
                dist = f"{row.get('distance', 0.0):.4f}"
                # Re-hydrate from catalog
                try:
                    entry_svc = cat.resolve(Tumbler.parse(tumbler_str)) if tumbler_str else None
                except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
                    entry_svc = None
                title_svc = (entry_svc.title if entry_svc else tumbler_str or "")[:70]
                # Prefer bib_* (RDR-101
                # enrichment), fall back to the plain author/year fields.
                bib_year_svc = (entry_svc.bib_year or entry_svc.year) if entry_svc else 0
                bib_authors_svc = (entry_svc.bib_authors or entry_svc.author) if entry_svc else ""
                bib_venue_svc = entry_svc.bib_venue if entry_svc else ""
                bib_citation_count_svc = entry_svc.bib_citation_count if entry_svc else 0
                try:
                    chunk_count_svc = len(cat.get_manifest(tumbler_str)) if tumbler_str else 0
                except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
                    chunk_count_svc = 0
                snippet_svc = row.get("content", "")[:300].replace("\n", " ")
                collection_svc = row.get("collection", "")

                lines_svc.append(f"{i}. [{dist}] {title_svc}")
                bib_svc: list[str] = []
                if bib_year_svc:
                    bib_svc.append(str(bib_year_svc))
                if bib_authors_svc:
                    bib_svc.append(bib_authors_svc[:60])
                if bib_venue_svc:
                    bib_svc.append(bib_venue_svc[:30])
                if bib_citation_count_svc:
                    bib_svc.append(f"{bib_citation_count_svc} citations")
                if bib_svc:
                    lines_svc.append(f"   {' · '.join(bib_svc)}")
                if chunk_count_svc:
                    lines_svc.append(f"   [{chunk_count_svc} chunks]")
                lines_svc.append(f"   {collection_svc}")
                lines_svc.append(f"   {snippet_svc}")
                lines_svc.append("")

            if len(deduped_svc) > limit:
                lines_svc.append(
                    f"\n--- showing 1-{len(rows)} of {len(deduped_svc)} documents. "
                    f"Results are capped at limit={limit}."
                )
            return _cap_text_result("\n".join(lines_svc), "query")

        # routing_note stays "" — RDR-156 P4.2c (nexus-2bqpn) deleted the
        # only branch that ever set it (the catalog_collections-driven
        # dance, whose catalog_collections variable is gone too); every
        # query reaching here is the plain corpus-based path.
        routing_note = ""
        all_names = _get_collection_names()

        if corpus == "all":
            seen_prefixes: list[str] = []
            for n in all_names:
                prefix = n.split("__", 1)[0]
                if prefix and prefix not in seen_prefixes:
                    seen_prefixes.append(prefix)
            corpus = ",".join(seen_prefixes) if seen_prefixes else "knowledge,code,docs,rdr"

        target: list[str] = []
        for part in corpus.split(","):
            part = part.strip()
            if not part:
                continue
            if "__" in part:
                target.append(part)
            else:
                target.extend(resolve_corpus(part, all_names))

        if not target:
            return f"No collections match corpus {corpus!r}"

        where_dict = _parse_where_str(where)

        # Over-fetch chunks to ensure good document coverage
        fetch_n = limit * 10
        # nexus-uro6c: capture threshold-filter diagnostics to surface a
        # threshold drop on a zero-hit (same rationale as the search tool).
        qdiag: list = []
        with _t2_ctx() as _t2_db:
            results = search_cross_corpus(
                question, target, n_results=fetch_n, t3=t3, where=where_dict,
                catalog=_get_catalog(),
                link_boost=True,
                taxonomy=_t2_db.taxonomy,
                telemetry=_t2_db.telemetry,
                diagnostics_out=qdiag,
            )
        # hybrid scoring + RDR-055 E2 quality boost — parity
        # with the CLI (search_cmd.py). Chunk-level ranking (and, below,
        # the per-document "best chunk" pick + document ordering) is now by
        # hybrid_score, not raw distance; previously this tool never read
        # frecency_score or bib_citation_count at all.
        results = apply_ranking_boosts(
            results,
            hybrid=cfg.get("search", {}).get("hybrid_default", False),
            tuning=get_tuning_config(),
            catalog=_get_catalog(),
        )
        if not results:
            if structured:
                empty_result: dict = {
                    "ids": [], "tumblers": [], "distances": [],
                    "collections": [], "chunk_collections": [],
                    "chunk_text_hash": [],
                }
                if graph_batch_info is not None:
                    empty_result["graph_scope"] = graph_batch_info
                return empty_result
            no_results_msg = _no_results_message(qdiag, base="No documents found.")
            if graph_batch_info is not None and graph_batch_info["possibly_incomplete"]:
                no_results_msg = (
                    f"[WARNING: link-graph traversal for {graph_batch_info['batches_at_cap']} "
                    f"of {graph_batch_info['batches_total']} seed batch(es) may be "
                    f"INCOMPLETE (hit the per-request node cap) — catalog routing via "
                    f"follow_links may have missed collections. Narrow `subtree` or "
                    f"split into multiple queries.]\n{no_results_msg}"
                )
            return no_results_msg

        if structured:
            page = results[:limit]
            structured_result: dict = {
                "ids": [r.id for r in page],
                "tumblers": [r.metadata.get("tumbler", "") for r in page],
                "distances": [float(r.distance) for r in page],
                # H1 (nexus-rzqto): sorted for deterministic ordering across
                # local and service modes.
                "collections": sorted({r.collection for r in page}),
                # Review #7: per-result aligned list for consumers
                # that need per-chunk origin (e.g. nx_answer envelope).
                "chunk_collections": [r.collection for r in page],
                # RDR-086 Phase 3.2: chunk_text_hash forwarded for chash
                # citation authoring at the document layer.
                "chunk_text_hash": [
                    r.metadata.get("chunk_text_hash", "") for r in page
                ],
            }
            if graph_batch_info is not None:
                # nexus-descendants-seed-n1 / substantive critique finding 2:
                # disclose possible graph-traversal truncation — never
                # silent. graph_batch_info's sole producer (the app-side
                # follow_links dance in local/Chroma mode) was deleted at
                # RDR-156 P4.2c (nexus-2bqpn); this stays None for every
                # live caller now, kept only so a plain corpus-based query
                # never sees a NameError.
                structured_result["graph_scope"] = graph_batch_info
            return structured_result

        # Group by document. RDR-108 Phase 4b (nexus-kosc): post-Phase-3
        # chunks no longer carry ``doc_id`` in their metadata, so the
        # canonical mapping is now ``chunk_text_hash -> [doc_id, ...]``
        # via ``Catalog.docs_for_chashes``. Fall back to the legacy
        # metadata keys (``content_hash``, ``title``, ``_display_path``,
        # ``source_path``, chunk id) for chunks the catalog cannot
        # resolve (catalog absent, orphan chunks, pre-Phase-A chunks).
        chash_to_doc: dict[str, str] = {}
        # nexus-voy5 (RDR-108 Phase 4 review S3): chunk_count was
        # previously read from chunk metadata, but RDR-108 Phase 3
        # (nexus-bdag) removed it. Resolve via the catalog manifest
        # once per unique doc_id so the display value matches D2's
        # "manifest is authoritative" invariant. Empty dict when
        # the catalog is absent (legacy display falls through to "").
        doc_to_chunk_count: dict[str, int] = {}
        try:
            cat = _get_catalog()
        except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
            cat = None
        if cat is not None:
            chashes_seen = sorted({
                r.metadata.get("chunk_text_hash", "")
                for r in results
                if r.metadata.get("chunk_text_hash")
            })
            if chashes_seen:
                try:
                    by_chash = cat.docs_for_chashes(chashes_seen)
                except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
                    by_chash = {}
                # Same chash can appear in multiple docs (identical
                # content); pick the first deterministically so chunks
                # group consistently within a single response.
                for chash, doc_ids in by_chash.items():
                    if doc_ids:
                        chash_to_doc[chash] = sorted(doc_ids)[0]
            # Fetch manifest length for each unique doc_id seen.
            # One get_manifest call per doc; bounded by the result set.
            for doc_id in set(chash_to_doc.values()):
                try:
                    doc_to_chunk_count[doc_id] = len(cat.get_manifest(doc_id))
                except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
                    continue
        docs: dict[str, dict] = {}  # doc_key → {meta, snippets, best_distance}
        for r in results:
            meta = r.metadata
            doc_key = (
                chash_to_doc.get(meta.get("chunk_text_hash", ""))
                or meta.get("doc_id")
                or meta.get("content_hash")
                or meta.get("title")
                or meta.get("_display_path")
                or meta.get("source_path")
                or r.id
            )
            if doc_key not in docs:
                docs[doc_key] = {
                    "title": meta.get("title") or doc_key[:40],
                    "collection": r.collection,
                    "distance": r.distance,
                    # tracks the "best chunk"/doc-order key —
                    # hybrid_score (vector + frecency + quality boost), not
                    # raw distance. ``distance`` above stays the winning
                    # chunk's raw vector distance for display.
                    "hybrid_score": r.hybrid_score,
                    "snippet": r.content[:300].replace("\n", " "),
                    "bib_year": meta.get("bib_year", ""),
                    "bib_authors": meta.get("bib_authors", ""),
                    "bib_citation_count": meta.get("bib_citation_count", ""),
                    "bib_venue": meta.get("bib_venue", ""),
                    # nexus-voy5: derive chunk_count from the catalog
                    # manifest (RDR-108 D2 authoritative source).
                    # Fall back to legacy metadata for chunks the
                    # catalog can't resolve (catalog-absent or
                    # pre-Phase-A chunks).
                    "chunk_count": (
                        doc_to_chunk_count.get(
                            chash_to_doc.get(meta.get("chunk_text_hash", "")),
                            None,
                        )
                        or meta.get("chunk_count", "")
                    ),
                    # page_count / has_formulas are not in ALLOWED_TOP_LEVEL:
                    # normalize() drops them, so the read always returned "".
                    # Removed in nexus-59j0 cleanup. nexus-1oguj later
                    # promoted extraction_method to canonical (it IS in
                    # ALLOWED_TOP_LEVEL for PDF chunks now) — it was not
                    # re-added here since this grouped-doc summary wasn't in
                    # that fix's scope, not because it's still dropped.
                    # nexus-1qed: prefer catalog-resolved _display_path so
                    # the response survives after source_path is pruned.
                    "source_path": (
                        meta.get("_display_path")
                        or meta.get("source_path", "")
                    ),
                }
            elif r.hybrid_score > docs[doc_key]["hybrid_score"]:
                # Better matching chunk — update snippet
                docs[doc_key]["distance"] = r.distance
                docs[doc_key]["hybrid_score"] = r.hybrid_score
                docs[doc_key]["snippet"] = r.content[:300].replace("\n", " ")

        # Sort by best match hybrid_score (), limit
        all_docs = sorted(docs.values(), key=lambda d: d["hybrid_score"], reverse=True)
        sorted_docs = all_docs[:limit]
        total = len(all_docs)

        header = f"Found {len(sorted_docs)} documents (from {len(results)} chunks across {len(target)} collections)"
        lines: list[str] = []
        if graph_batch_info is not None and graph_batch_info["possibly_incomplete"]:
            # nexus-descendants-seed-n1 / substantive critique finding 2:
            # never silent. See the structured branch above for why
            # graph_batch_info is now always None (RDR-156 P4.2c).
            lines.append(
                f"[WARNING: link-graph traversal for "
                f"{graph_batch_info['batches_at_cap']} of "
                f"{graph_batch_info['batches_total']} seed batch(es) may be "
                f"INCOMPLETE (hit the per-request node cap) — catalog "
                f"routing via follow_links may have missed collections. "
                f"Narrow `subtree` or split into multiple queries.]"
            )
        lines.append(f"{routing_note}\n{header}" if routing_note else header)
        lines.append("")
        for i, d in enumerate(sorted_docs, 1):
            dist = f"{d['distance']:.4f}"
            title = d["title"][:70]
            header_parts = [f"[{dist}] {title}"]
            # Bibliographic metadata
            bib_parts: list[str] = []
            if d["bib_year"]:
                bib_parts.append(str(d["bib_year"]))
            if d["bib_authors"]:
                authors = d["bib_authors"][:60]
                bib_parts.append(authors)
            if d["bib_venue"]:
                bib_parts.append(d["bib_venue"][:30])
            if d["bib_citation_count"]:
                bib_parts.append(f"{d['bib_citation_count']} citations")
            # Technical metadata
            tech_parts: list[str] = []
            if d["chunk_count"]:
                tech_parts.append(f"{d['chunk_count']} chunks")

            lines.append(f"{i}. {' | '.join(header_parts)}")
            if bib_parts:
                lines.append(f"   {' · '.join(bib_parts)}")
            if tech_parts:
                lines.append(f"   [{' · '.join(tech_parts)}]")
            lines.append(f"   {d['collection']}")
            lines.append(f"   {d['snippet']}")
            lines.append("")

        if total > limit:
            lines.append(f"\n--- showing 1-{len(sorted_docs)} of {total} documents. Results are capped at limit={limit}.")

        return _cap_text_result("\n".join(lines), "query")
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("query", e)


@mcp.tool(
    title="Store Knowledge Document",
    annotations={"readOnlyHint": False, "destructiveHint": False},
    structured_output=False,
)
def store_put(
    content: str,
    collection: str = "knowledge",
    title: str = "",
    tags: str = "",
    category: str = "",
    ttl: str = "permanent",
    agent: str = "",
    session: str = "",
) -> str:
    """Store content in the T3 permanent knowledge store.

    Args:
        content: Text content to store
        collection: Collection name or prefix (default: knowledge)
        title: Document title (recommended for deduplication). A non-empty
            title makes catalog identity stable: re-putting the same
            (collection, title) pair reconciles onto the existing document
            (its manifest is replaced with the new content, not duplicated)
            instead of minting a sibling (nexus-sdp0u). An empty title
            synthesizes no catalog identity — every empty-title put always
            registers a new document, since a title-less identity would
            collapse all untitled documents together. Note: "replaced, not
            duplicated" is a CATALOG-level guarantee — the OLD T3 chunk
            itself is not deleted and may remain independently visible via
            raw vector search (``nx search`` / ``search()``) until a future
            sweep (nexus-39upx class) reaps it; catalog-aware query paths
            (``query()``, catalog-scoped search) follow the manifest and see
            only the new content.
        tags: Comma-separated tags
        category: Document category for filtered queries (e.g.
            ``rdr_postmortem``). Stamped on the chunk metadata so callers
            can filter via ``where={"category": "<value>"}`` without
            isolating the documents in their own collection.
        ttl: Time-to-live: Nd (days), Nw (weeks), or "permanent"
        agent: Optional subagent / role attribution (e.g. "developer",
            "architect-planner"), mirroring ``memory_put``'s parameter.
            When empty, falls back to ``NX_AGENT`` env; when that is ALSO
            empty, lands the distinct ``"mcp"`` marker rather than an
            empty string or the T3 write path's own "nexus-indexer"
            indexer default (nexus-4ftd7) — an unmarked MCP write must
            never collapse onto either value, since
            ``search_engine._flag_contradictions``'s ``agent_a !=
            agent_b`` precondition (RDR-057 Phase 3a) can only ever fire
            between two DIFFERENT non-empty ``source_agent`` values, and
            a shared constant (or both blank) makes it permanently dead
            on the MCP-written population.
        session: Optional explicit session_id override, mirroring
            ``memory_put``'s parameter. When empty, falls back to
            ``NX_SESSION_ID`` env (subagents carry this via
            ``claude_dispatch``, RDR-094).
    """
    try:
        if not content:
            return "Error: content is required"
        days = parse_ttl(ttl)
        # nexus-tk070.p6b fix-pass (nexus-24rof, RDR-194 D5): pass None
        # through verbatim rather than coercing to 0 — parse_ttl already
        # rejects "0d"/"0w" input (ValueError), so `days` is either None
        # (permanent) or a positive int here; t3.put now rejects an
        # explicit 0 itself as defense-in-depth, but this MCP path can
        # never reach it.
        ttl_days = days
        # nexus-4ftd7: resolve actor attribution HERE (mirrors memory_put's
        # own inline resolution) rather than leaving it to T3Database.put,
        # which — unlike HttpMemoryStore.put's resolve_attribution — has no
        # env-fallback chain of its own and would otherwise persist a bare
        # empty string for every MCP write with no explicit agent.
        agent_arg = agent.strip() or _os.environ.get("NX_AGENT", "").strip() or "mcp"
        session_arg = session.strip() or _os.environ.get("NX_SESSION_ID", "").strip()
        t3 = _get_t3()
        # nexus-hmxi: pass t3 so the resolver grandfathers an existing
        # legacy 2-segment collection ahead of the auto-promoted
        # conformant shape, matching the read-path behaviour and
        # preventing put/list/search split-brain.
        # for_write=True (nexus-35ok4): this tool WRITES new content —
        # a genuinely new corpus mints strictly (raises loud if
        # local.embed_model is voyage-shaped with no key configured).
        col_name = t3_collection_name(collection, t3=t3, for_write=True)

        # RDR-101 Phase 3 PR δ Stage B.5: pre-register the catalog entry
        # so the T3 chunk can carry the resulting tumbler as ``doc_id``
        # at write-time. Same pattern as the CLI ``nx store put`` (B.4).
        # chunk_chroma_id mirrors ``T3Database.put``'s natural-id
        # derivation (chunk_text_hash[:32] per RDR-108 D1 / nexus-kmb6;
        # for single-chunk MCP docs chunk_text == content).
        # nexus-8g79.10 (V1): catalog_store_hook now lives under
        # nexus.catalog (lower layer). MCP infra no longer reaches up into
        # commands/ for this helper. single_chunk_manifest_metadata (GH
        # #1370 Defect 4b) computes the same natural id AND the chunk
        # metadata the manifest-write batch hook needs; it must run
        # unconditionally (not just on the catalog-present path) since
        # fire_batch below needs real metadatas regardless of catalog_doc_id.
        from nexus.catalog.store_hook import (  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
            catalog_store_hook_tracked,
            rollback_minted_catalog_entry,
            single_chunk_manifest_metadata,
            store_put_manifest_direct,
        )
        chunk_chroma_id, manifest_metadatas = single_chunk_manifest_metadata(content)
        catalog_doc_id = ""
        catalog_row_minted = False
        try:
            catalog_doc_id, catalog_row_minted = catalog_store_hook_tracked(
                title=title, doc_id=chunk_chroma_id, collection_name=col_name,
            )
        except Exception:  # noqa: BLE001 — boundary catch; failure surfaced via log.warning, must not crash caller
            import structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
            structlog.get_logger().warning(
                "catalog_store_hook_failed",
                doc_id=chunk_chroma_id,
                collection=col_name,
                exc_info=True,
            )

        # nexus-vw594 F2: producer #10 (MCP store_put / nx store put).
        # content_hash is the same full-digest value single_chunk_manifest_
        # metadata already derived (chunk_text_hash IS content_hash for a
        # single-chunk store — see that function's docstring). Fence begin
        # BEFORE the vector put, mirroring every other producer's T0
        # ordering (memo §3.5); this path was previously entirely unfenced.
        content_hash = manifest_metadatas[0].get("chunk_text_hash", "") if manifest_metadatas else ""
        if catalog_doc_id:
            from nexus.doc_indexer import _fence_begin  # noqa: PLC0415 — deferred import; test patch target
            _fence_begin(catalog_doc_id, content_hash, col_name)

        # nexus-b6enc C2: the catalog row is registered BEFORE t3.put, so
        # a put failure (engine skew / timeout / 500) would strand a ghost
        # row — chunk_count=0, zero manifest, zero chunks — while the
        # content existed only in the failed request. Compensate by
        # deleting the row minted IN THIS CALL (never a pre-existing row
        # the put deduped onto), then surface the original error. The
        # compensation never raises, so it cannot mask the put failure.
        try:
            doc_id = t3.put(
                collection=col_name,
                content=content,
                title=title,
                tags=tags,
                category=category,
                session_id=session_arg,
                source_agent=agent_arg,
                ttl_days=ttl_days,
                catalog_doc_id=catalog_doc_id,
            )
        except Exception as put_exc:
            # nexus-vw594 F2 fix-round IMPORTANT (code-review-expert, T1
            # scratch d9173ec9): a dedup-hit store_put (catalog_row_minted
            # False, so the compensation below never fires — the row is
            # pre-existing, not a ghost) that then fails t3.put leaves the
            # fence stuck at 'indexing' forever with only the 6h doctor
            # sweep as signal. Stamp 'failed' unconditionally, mirroring
            # every doc_indexer producer's own except-block pattern
            # (_fence_fail never raises, so the original exception always
            # propagates unmasked below regardless of rollback outcome).
            if catalog_doc_id:
                from nexus.doc_indexer import _fence_fail  # noqa: PLC0415 — deferred import; test patch target
                _fence_fail(catalog_doc_id, str(put_exc))
            if catalog_doc_id and catalog_row_minted:
                rollback_minted_catalog_entry(
                    catalog_doc_id, original_error=str(put_exc),
                )
            raise

        # nexus-b6enc C3 / F2: the manifest leg must not ride the
        # swallowing fire_batch chain for this producer — write it
        # directly and verify it landed. Failure is captured (not
        # raised) so the remaining post-store consumers still fire; the
        # final result then reports "stored but NOT cataloged" instead
        # of a bare "Stored:".
        manifest_error = ""
        if catalog_doc_id:
            try:
                store_put_manifest_direct(
                    catalog_doc_id, manifest_metadatas, collection=col_name)
            except Exception as manifest_exc:  # noqa: BLE001 — captured for the explicit non-"Stored:" result below
                manifest_error = str(manifest_exc)
                # nexus-vw594 F2 fix-round IMPORTANT: the vector put
                # already succeeded (t3.put above), so this is NOT a
                # dedup-hit-then-put-failure wedge — but the fence began
                # 'indexing' and the manifest write (this producer's only
                # completion path — no manifest_complete ride is possible
                # since store_put_manifest_direct's own write already
                # failed) never happened either. Stamp 'failed' so the row
                # does not sit silently at 'indexing' with only the 6h
                # doctor sweep as signal.
                from nexus.doc_indexer import _fence_fail  # noqa: PLC0415 — deferred import; test patch target
                _fence_fail(catalog_doc_id, manifest_error)
                import structlog  # noqa: PLC0415 — branch-local logging
                structlog.get_logger().warning(
                    "store_put_manifest_direct_failed",
                    doc_id=doc_id,
                    catalog_doc_id=catalog_doc_id,
                    collection=col_name,
                    error=manifest_error[:300],
                    exc_info=True,
                )
        # A committed write makes any cached page burst stale — drop it so a
        # same-identity search re-fetches (batch-f1655f55 critique).
        _page_cache_invalidate()
        # Auto-link from T1 scratch link-context.
        # nexus-a414: replace prior bare-except with named-exception capture
        # so unexpected errors surface at WARNING instead of silently passing.
        # The auto-link path itself stays non-fatal to store_put — the
        # WARNING makes the failure visible without aborting the user's
        # write. Specific skip-count observability lives one layer down in
        # _catalog_auto_link.
        try:
            n = _catalog_auto_link(catalog_doc_id)
        except Exception as auto_link_exc:  # noqa: BLE001 — boundary catch; failure surfaced via log.warning, must not crash caller
            import structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
            structlog.get_logger().warning(
                "store_put_auto_link_failed",
                tumbler=catalog_doc_id,
                error=type(auto_link_exc).__name__,
                detail=str(auto_link_exc)[:200],
            )
        else:
            if n:
                import structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
                structlog.get_logger().debug(
                    "store_put_auto_linked", doc_id=doc_id, link_count=n,
                )
        # All three post-store chains fire from every storage event
        # (RDR-095 symmetric-fire follow-up) via the process-local
        # ``_hooks`` registry constructed at module load. Single-doc
        # chain runs registered per-doc consumers; batch chain runs
        # with a 1-element list so batch-shape consumers (taxonomy,
        # chash, manifest) see MCP ``store_put`` as a single-document
        # batch.
        _hooks.fire_single(doc_id, col_name, content)
        # nexus-vw594 F2: manifest_complete rides this existing call
        # through manifest_write_batch_hook's write_manifest_many
        # completion stamp (the SAME manifest rows store_put_manifest_
        # direct above already wrote — an idempotent re-UPSERT), no extra
        # round trip. store_put is single-chunk by construction so the
        # file-atomicity claim always holds.
        _hooks.fire_batch(
            [doc_id], col_name, [content], None, manifest_metadatas,
            catalog_doc_id=catalog_doc_id,
            manifest_complete={catalog_doc_id: content_hash} if catalog_doc_id else None,
        )
        # RDR-089 document-grain chain — plain sync call (FastMCP wraps
        # this @mcp.tool() body in a thread pool at the framework level;
        # store_put is `def`, not `async def`, so no await/to_thread).
        # content is the full document text already in scope; pass it
        # through literally per the P0.1 content-sourcing contract.
        # source_path (1st positional) is the chunk natural-id here — there
        # is no on-disk file at the MCP boundary, so it serves as the stable
        # per-doc queue key + the identifier the hook uses for failure
        # attribution. It is NOT foreign-keyed (RDR-156 fk-001: aspect_
        # extraction_queue.source_path is a storage path, not a tumbler).
        # nexus-tdgc: forward an explicit doc_id so the aspect-queue hook can
        # stamp it on the queue row.
        # RDR-172 / nexus-pyn35 (closes nexus-ov0sw): the queue row's doc_id
        # carries a composite FK -> catalog_documents(tumbler) (RDR-156
        # fk-001), so it MUST be the catalog tumbler (catalog_doc_id), NOT
        # the t3.put chunk natural-id (sha256(content)[:32]) — a chunk hash
        # is never a tumbler and 500s the service enqueue, which the best-
        # effort hook then swallows (silent, total loss of RDR-089 aspects in
        # service mode). This matches every other fire_document caller
        # (doc_indexer, pipeline_stages, code_indexer, prose_indexer), which
        # all pass catalog_doc_id.
        #
        # hygiene-001 (nexus-tk070.p6a follow-on): when no tumbler was minted,
        # catalog_doc_id is "" — SUPERSEDES the prior "blank sentinel the
        # service NULL-coerces" comment above. aspect_extraction_queue.doc_id
        # is NOT NULL now and the engine refuses a blank doc_id at the
        # boundary (400 "doc_id required").
        #
        # Review round item B (critic C2): the resolve-or-skip decision for a
        # blank doc_id moved to the CHOKE POINT, aspect_extraction_enqueue_hook
        # itself — the one place all seven fire_document call sites (this one
        # included) converge, rather than seven copies of the same guard. So
        # this call site no longer pre-empts the hook by skipping it here;
        # catalog_doc_id is forwarded VERBATIM, blank or not, and the hook's
        # own resolve-by-(collection, source_path) fallback (via the catalog
        # reader) or its aspect_enqueue_skipped_no_doc_id warning is now the
        # ONLY place this decision is made. At the MCP boundary source_path
        # (the ``doc_id`` local below) is a content-hash, not a real
        # file_path/title, so the hook's catalog-lookup fallback cannot
        # resolve one for this specific call site either -- the practical
        # outcome here is unchanged (still skips), but the decision itself is
        # no longer duplicated.
        _hooks.fire_document(doc_id, col_name, content, doc_id=catalog_doc_id)
        # RDR-061 E2: log relevance correlation for the most recent search in
        # this session. Only the newest trace is used to minimize noise —
        # older traces are unlikely to have driven this store_put.
        try:
            t1, _ = _get_t1()
            session_id = t1.session_id if hasattr(t1, "session_id") else ""
            traces = _get_recent_search_traces(session_id) if session_id else []
            if traces:
                latest = traces[-1]
                rows = [
                    (latest["query"], chunk_id, chunk_col, "stored", session_id)
                    for chunk_id, chunk_col in latest["chunks"]
                ]
                with _t2_ctx() as db:
                    db.log_relevance_batch(rows)
        except Exception:  # noqa: BLE001 — best-effort path; failure logged via log.debug, must not crash caller
            import structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
            structlog.get_logger().debug("relevance_log_store_failed", exc_info=True)
        _record_tier_write(
            tool="store_put", tier="T3",
            target_title=title or doc_id,
        )
        if manifest_error:
            # nexus-b6enc C3: never a bare "Stored:" when the catalog
            # manifest did not land — the content IS in T3 (recoverable
            # by doc_id) but catalog-aware consumers will not see it.
            # CRE Imp 3: do NOT suggest 'nx catalog reconcile' here —
            # heal_manifest_gaps' candidate filter (chunk_count>0 OR
            # meta.content_hash) excludes exactly these rows, making it
            # a verified no-op for this failure mode. Retry IS effective
            # (by_doc_id dedup + idempotent t3.put).
            return (
                f"Error: stored to T3 ({doc_id} in {col_name}) but NOT "
                f"cataloged: {manifest_error}. Catalog row {catalog_doc_id} "
                f"may show chunk_count=0; retry store_put with the same "
                f"content (idempotent dedup makes retry safe)."
            )
        return f"Stored: {doc_id} -> {col_name}"
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("store_put", e)


@mcp.tool(
    title="Retrieve Knowledge Document",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
@degrade_loud_when_migrating
def store_get(doc_id: str, collection: str = "knowledge") -> str:
    """Retrieve the full content and metadata of a T3 knowledge entry by document ID or title.

    Use after store_list or search to read the complete document.

    Args:
        doc_id: Exact 64-char content-hash document ID (from store_list / store_put / search),
                OR an exact title (looked up via metadata).
        collection: Collection name or prefix (default: knowledge)
    """
    try:
        if not doc_id:
            return "Error: doc_id is required"
        t3 = _get_t3()
        col_name = t3_collection_name(collection, t3=t3)
        entry = t3.get_by_id(col_name, doc_id)
        if entry is None:
            # Title fallback: 64 lowercase hex chars is the canonical id
            # (RDR-180 full digest); 32 is a legacy half-digest reference —
            # no longer resolvable (nexus-lgdel.l1 retired chash_alias),
            # but still hash-SHAPED, so still never a title. Anything else,
            # try treating it as an exact title.
            looks_like_hash = len(doc_id) in (32, 64) and all(c in "0123456789abcdef" for c in doc_id)
            if not looks_like_hash:
                ids = t3.find_ids_by_title(col_name, doc_id)
                if len(ids) == 1:
                    entry = t3.get_by_id(col_name, ids[0])
                elif len(ids) > 1:
                    return (
                        f"Multiple documents with title {doc_id!r} in {col_name}: "
                        + ", ".join(ids[:5]) + (" …" if len(ids) > 5 else "")
                        + ". Pass a 64-char content-hash to disambiguate."
                    )
        if entry is None:
            return f"Not found: {doc_id!r} in {col_name} (pass a 64-char content-hash from store_list/store_put/search, or an exact title)"
        title = entry.get("title", "")
        tags = entry.get("tags", "")
        indexed_at = (entry.get("indexed_at") or "")[:10]
        # nexus-1oguj: extraction_method IS in ALLOWED_TOP_LEVEL for PDF
        # chunks now (docling/mineru/pymupdf_normalized, or the honest
        # mixed aggregate mineru+docling-degraded) — display it when
        # present. Absent for non-PDF chunks and for chunks indexed
        # before the fix shipped (new-writes-only, no backfill;
        # nexus-0qc4b — absence is "unknown", not "not mineru").
        extraction_method = entry.get("extraction_method", "")
        lines: list[str] = [f"ID:         {entry['id']}", f"Collection: {col_name}"]
        if title:
            lines.append(f"Title:      {title}")
        if tags:
            lines.append(f"Tags:       {tags}")
        if indexed_at:
            lines.append(f"Indexed:    {indexed_at}")
        if extraction_method:
            lines.append(f"Extractor:  {extraction_method}")
        lines.append("")
        lines.append(entry.get("content", ""))
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("store_get", e)


def _unique_preserve_order(ids: Iterable[str]) -> list[str]:
    """Dedupe an id sequence while keeping first-seen order.

    ``store_get_many``'s batched fan-out fix groups ids per candidate
    collection before issuing one batched fetch; a caller-supplied id
    list may repeat the same id at multiple positions (e.g. duplicate
    references in a single hydration request), and fetching it twice in
    the same batch would be wasted round-trip payload for no semantic
    gain — the result is looked up back out of a dict, so duplicates
    resolve identically either way.
    """
    seen: set[str] = set()
    out: list[str] = []
    for doc_id in ids:
        if doc_id not in seen:
            seen.add(doc_id)
            out.append(doc_id)
    return out


def _batched_get_by_ids(t3: Any, col_name: str, ids: list[str]) -> dict[str, dict]:
    """Batch-fetch ``ids`` from ``col_name`` in one (paged) round trip.

    Delegates to ``_ServiceCollectionStub.get(ids=...)``, which already
    pages internally at ``QUOTAS.MAX_RECORDS_PER_WRITE`` (300 — smaller
    than the ``/v1/vectors/store-get`` route's own ``MAX_BATCH_IDS=1000``
    ceiling, nexus-hdx2u) and fails loud on an engine-reported truncation.
    Reshapes the Chroma-style ``{ids, documents, metadatas}`` response into
    the same flat ``{id, content, **meta}`` shape ``HttpVectorClient.get_by_id``
    returns, keyed by id, so callers can do the same ``entry.get("content")``
    lookups as the single-id path.

    BLAST RADIUS (code review T2 nexus/store-get-many-code-review-2026-08-21
    [23303] Item 1): this is NOT equivalent in failure profile to the prior
    per-id loop's ``except Exception: entry = None``, even though the
    OUTCOME SHAPE (a caught exception degrades to "not found", never raises)
    matches. Pre-fix, a transient failure cost exactly one id. Here, a
    single failed request can blank up to ``QUOTAS.MAX_RECORDS_PER_WRITE``
    (300) ids as missing in one shot — ``_post`` has no retry, and
    ``_ServiceCollectionStub.get``'s per-page loop discards already-fetched
    pages when a later page in the same call raises. Two mitigations:
      1. The failure is logged LOUD (below) naming the collection and the
         id count, so "these documents don't exist" and "the batch call to
         fetch them failed" are distinguishable in the log — the pre-fix
         silent-degrade-to-None-per-id was already indistinguishable in
         THIS respect (a single missing id looked the same either way), but
         silently blanking up to 300 at once without any signal is exactly
         the silent-fallback-for-a-correctness-problem shape this repo bans.
      2. A batch failure falls back to a per-id retry
         (:func:`_fallback_get_by_ids_individually`, reusing
         ``HttpVectorClient.get_by_id`` directly) rather than giving up on
         the whole batch — this restores the OLD blast radius (a genuinely
         transient blip now costs one id again, since the retry is a fresh
         request per id) for exactly the failure path; the happy path stays
         batched.

    Uses ``get_or_create_collection`` rather than ``get_collection`` on
    purpose: the latter does its own existence pre-check via a full
    ``list_collections()`` round trip before returning the stub, which
    would tax exactly the fan-out this fix removes (one extra call per
    candidate collection per hydration request). ``get_by_id``, the path
    this replaces on the happy path, never pre-checked existence either —
    it POSTed directly and let a missing/unreachable collection surface as
    a caught exception, which is the behaviour preserved here (now via the
    per-id fallback rather than a blanket "nothing found here").
    """
    if not ids:
        return {}
    try:
        result = t3.get_or_create_collection(col_name).get(ids=ids)
    except Exception as exc:  # noqa: BLE001 — batch-level failure; falls back to per-id retry below, never raises to the caller
        _log.error(
            "store_get_many_batch_fetch_failed",
            collection=col_name,
            id_count=len(ids),
            error=str(exc),
        )
        return _fallback_get_by_ids_individually(t3, col_name, ids)

    out_ids = result.get("ids") or []
    docs = result.get("documents") or []
    metas = result.get("metadatas") or []
    id_to_entry: dict[str, dict] = {}
    for i, rid in enumerate(out_ids):
        meta = metas[i] if i < len(metas) else {}
        entry: dict[str, Any] = {
            "id": rid,
            "content": docs[i] if i < len(docs) else "",
        }
        if isinstance(meta, dict):
            entry.update(meta)
        id_to_entry[rid] = entry
    return id_to_entry


def _fallback_get_by_ids_individually(
    t3: Any, col_name: str, ids: list[str],
) -> dict[str, dict]:
    """Per-id retry after a whole-batch fetch failure.

    Deliberately NOT a general retry framework — no backoff, no attempt
    cap beyond "try once per id," reuses the existing single-id
    ``HttpVectorClient.get_by_id`` path verbatim. Its only job is to
    restore the pre-fix blast radius for the failure case: one bad id
    costs one id again, instead of the whole (up to 300-id) batch going
    missing on one flaky request. If the underlying failure is systemic
    (the service is actually down) every one of these calls fails too and
    the ids end up missing anyway — no worse than the batch path, and the
    per-id calls fail fast (connection-level errors, not slow timeouts).
    """
    id_to_entry: dict[str, dict] = {}
    for doc_id in ids:
        try:
            entry = t3.get_by_id(col_name, doc_id)
        except Exception:  # noqa: BLE001 — per-id graceful degradation, mirrors the pre-fix loop exactly
            entry = None
        if entry is not None:
            id_to_entry[doc_id] = entry
    return id_to_entry


@mcp.tool(
    title="Batch-Retrieve Documents",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
@degrade_loud_when_migrating
def store_get_many(
    ids: str | list,
    collections: str | list = "knowledge",
    *,
    max_chars_per_doc: int = 4000,
    structured: bool = False,
    limit_per_source: int | list[int] | None = None,
) -> str | dict:
    """Batch-hydrate document content by ID. RDR-079 hydration primitive.

    Args:
        ids: Document IDs to fetch. Accepts:
            - comma-separated string
            - ``list[str]`` (single-stream form)
            - ``list[list[str]]`` (parallel-stream form; pair with
              ``list[int]`` ``limit_per_source``)
        collections: Target collection name(s). Accepts a single name,
            comma-separated string, or list. In single-stream form a list
            aligned 1:1 with ``ids`` performs per-id collection routing;
            in parallel-stream form a list aligned 1:1 with the outer
            ``ids`` length performs per-stream collection routing
            (each id in stream i is hydrated from ``collections[i]``).
        max_chars_per_doc: Per-document truncation cap (default 4 KB).
        structured: Return ``{contents, missing}`` dict when True;
            human-readable string when False.
        limit_per_source: Cap input IDs before hydration (RDR-097 P1.0).
            - ``None`` (default): no truncation; preserves prior behavior.
            - ``int``: truncate ``ids`` to first N entries. With
              parallel-stream ``ids``, broadcasts the cap across all
              streams. Negative values raise ``ValueError``.
            - ``list[int]``: requires parallel-stream ``ids``. Each
              stream is truncated to its corresponding cap, then
              flattened stream-major. ``len(limit_per_source)`` must
              equal ``len(ids)`` or ``ValueError`` is raised. (Implemented
              for contract symmetry; current consumers issue scalar calls
              per stream — RDR-097 P1.1.)
    """
    try:
        # Detect parallel-stream form: ids is a non-empty list of lists.
        parallel_form = (
            isinstance(ids, list)
            and len(ids) > 0
            and all(isinstance(stream, list) for stream in ids)
        )

        # Validate limit_per_source shape early.
        if isinstance(limit_per_source, int) and not isinstance(limit_per_source, bool):
            if limit_per_source < 0:
                raise ValueError(
                    f"limit_per_source must be non-negative, got {limit_per_source}"
                )
        elif isinstance(limit_per_source, list):
            if not parallel_form:
                raise ValueError(
                    "limit_per_source as list[int] requires parallel-stream "
                    "ids (list[list[str]]); got single-stream ids"
                )
            if len(limit_per_source) != len(ids):
                raise ValueError(
                    f"limit_per_source list length {len(limit_per_source)} "
                    f"must equal ids stream count {len(ids)}"
                )
            if any(
                (not isinstance(n, int)) or isinstance(n, bool) or n < 0
                for n in limit_per_source
            ):
                raise ValueError(
                    "limit_per_source list entries must be non-negative ints"
                )
        elif limit_per_source is not None:
            raise ValueError(
                "limit_per_source must be int, list[int], or None; "
                f"got {type(limit_per_source).__name__}"
            )

        id_list: list[str]
        coll_list: list[str]

        if parallel_form:
            # Build typed streams.
            streams: list[list[str]] = [
                [str(x) for x in stream if x] for stream in ids
            ]

            # Truncate per limit_per_source.
            if isinstance(limit_per_source, int):
                streams = [s[:limit_per_source] for s in streams]
            elif isinstance(limit_per_source, list):
                streams = [s[:limit_per_source[i]] for i, s in enumerate(streams)]

            # Parse collections input.
            if isinstance(collections, list):
                coll_input = [str(c) for c in collections if c]
            else:
                coll_input = [
                    s.strip()
                    for s in str(collections or "knowledge").split(",")
                    if s.strip()
                ]
            if not coll_input:
                coll_input = ["knowledge"]

            if len(coll_input) == len(streams):
                # Per-stream collection routing: flatten ids and build
                # a parallel coll_list aligned 1:1 with id_list. The
                # downstream loop then takes the existing 1:1 branch.
                id_list = []
                coll_list = []
                for i, s in enumerate(streams):
                    id_list.extend(s)
                    coll_list.extend([coll_input[i]] * len(s))
            else:
                # Broadcast: every id may try every collection in coll_input.
                id_list = [doc_id for stream in streams for doc_id in stream]
                coll_list = coll_input

        else:
            if isinstance(ids, list):
                id_list = [str(i) for i in ids if i]
            else:
                id_list = [s.strip() for s in str(ids or "").split(",") if s.strip()]

            if isinstance(limit_per_source, int):
                id_list = id_list[:limit_per_source]

            if isinstance(collections, list):
                coll_list = [str(c) for c in collections if c]
            else:
                coll_list = [
                    s.strip()
                    for s in str(collections or "knowledge").split(",")
                    if s.strip()
                ]
            if not coll_list:
                coll_list = ["knowledge"]

        t3 = _get_t3()
        contents: list[str] = []
        missing: list[str] = []

        # nexus fan-out fix: batch the per-collection lookups instead of one
        # HTTP round trip per (id, candidate-collection) pair — the prior
        # loop called ``t3.get_by_id`` (single-id POST to
        # ``/v1/vectors/store-get``) once per id per candidate collection,
        # even though that route accepts batches (MAX_BATCH_IDS=1000,
        # nexus-hdx2u) and ``_ServiceCollectionStub.get(ids=...)`` already
        # pages a batch internally at ``QUOTAS.MAX_RECORDS_PER_WRITE``
        # (300 — the smaller of the two ceilings). This is on the
        # ``nx_answer`` hot path (``plans/runner.py``'s auto-hydration runs
        # it on every operator step that carries ids).
        #
        # Semantics preserved exactly:
        #   * per-id routing (``len(coll_list) == len(id_list)``): each id
        #     is looked up in exactly its assigned collection — batched by
        #     grouping ids per collection.
        #   * broadcast routing (the else branch): each id is tried against
        #     every candidate collection in order, first match wins — batched
        #     by resolving the STILL-UNRESOLVED ids per candidate collection,
        #     one batch call per collection instead of one call per
        #     (id, collection) pair.
        #   * a per-collection batch call failing outright (network error,
        #     unknown collection, ...) NEVER crashes the caller — but is
        #     NOT the same blast radius as the old per-id
        #     ``except Exception: entry = None``. Pre-fix, one failure cost
        #     one id. Here a single failed request could otherwise blank up
        #     to 300 ids as "missing" in one shot (a whole
        #     ``QUOTAS.MAX_RECORDS_PER_WRITE`` page), indistinguishable from
        #     genuine absence. ``_batched_get_by_ids`` closes that gap: it
        #     logs the failure loud (collection + id count) and falls back
        #     to a per-id retry, restoring the old one-id blast radius for
        #     the failure path specifically — see that function's docstring
        #     for the full rationale (nexus/store-get-many-code-review-
        #     2026-08-21 [23303] Item 1).
        #   * result order and per-doc truncation are unchanged.
        per_id_routing = len(coll_list) == len(id_list)
        entries: list[dict | None] = [None] * len(id_list)

        if per_id_routing:
            idxs_by_collection: dict[str, list[int]] = {}
            for idx, cand in enumerate(coll_list):
                col_name = t3_collection_name(cand, t3=t3)
                idxs_by_collection.setdefault(col_name, []).append(idx)

            for col_name, idxs in idxs_by_collection.items():
                batch_ids = _unique_preserve_order(id_list[idx] for idx in idxs)
                id_to_entry = _batched_get_by_ids(t3, col_name, batch_ids)
                for idx in idxs:
                    entries[idx] = id_to_entry.get(id_list[idx])
        else:
            remaining_idxs = list(range(len(id_list)))
            for cand in coll_list:
                if not remaining_idxs:
                    break
                col_name = t3_collection_name(cand, t3=t3)
                batch_ids = _unique_preserve_order(
                    id_list[idx] for idx in remaining_idxs
                )
                id_to_entry = _batched_get_by_ids(t3, col_name, batch_ids)
                still_remaining: list[int] = []
                for idx in remaining_idxs:
                    found = id_to_entry.get(id_list[idx])
                    if found is not None:
                        entries[idx] = found
                    else:
                        still_remaining.append(idx)
                remaining_idxs = still_remaining

        for doc_id, entry in zip(id_list, entries):
            if entry is None:
                missing.append(doc_id)
                contents.append("")
                continue

            body = str(entry.get("content") or "")
            if max_chars_per_doc > 0 and len(body) > max_chars_per_doc:
                body = body[:max_chars_per_doc] + "…"
            contents.append(body)

        if structured:
            return {"contents": contents, "missing": missing}
        lines = [f"Hydrated {len(contents) - len(missing)}/{len(id_list)} docs"]
        if missing:
            lines.append(f"Missing: {', '.join(missing[:10])}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        if structured:
            # Structured callers (plan-runner hydration) get a dict, not a string —
            # but the failure must still be logged server-side (nexus-yttqr): a silent
            # swallow here is exactly the diagnostic hole this bead closes.
            _log.error("mcp_store_get_many_failed", error=str(e), exc_info=True)
            return {"contents": [], "missing": [], "error": f"store_get_many failed: {e}"}
        return _mcp_tool_error("store_get_many", e)


@mcp.tool(
    title="List Collection Documents",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def store_list(
    collection: str = "knowledge",
    limit: int = 20,
    offset: int = 0,
    docs: bool = False,
) -> str:
    """List entries in a T3 knowledge collection.

    Results are paged. Use offset to retrieve subsequent pages.

    Args:
        collection: Collection name or prefix (default: knowledge)
        limit: Page size (default 20)
        offset: Skip this many entries (default 0). Use for pagination.
        docs: If True, show unique documents instead of individual chunks.
              Deduplicates by content_hash, shows title, chunk count, page count,
              and extraction method. Ignores offset/limit (scans full collection).
    """
    try:
        t3 = _get_t3()
        col_name = t3_collection_name(collection, t3=t3)
        try:
            info = t3.collection_info(col_name)
            total = info["count"]
        except KeyError:
            return f"Collection not found: {col_name}"
        if total == 0:
            return f"No entries in {col_name}."

        if docs:
            return _store_list_docs(t3, col_name, total)

        page = t3.list_store(col_name, limit=limit, offset=offset)
        if not page:
            return f"No entries at offset {offset} (total {total})."
        lines: list[str] = [f"{col_name}  (showing {offset + 1}-{offset + len(page)} of {total})"]
        from datetime import datetime, timedelta  # noqa: PLC0415 — stdlib deferred to call site (datetime)
        for e in page:
            doc_id = e.get("id", "")  # RDR-180: full id — the list->get handle must round-trip
            title = (e.get("title") or "")[:40]
            tags = e.get("tags") or ""
            ttl_days = e.get("ttl_days", 0)
            indexed_at_full = e.get("indexed_at") or ""
            indexed_at = indexed_at_full[:10]
            if ttl_days and ttl_days > 0 and indexed_at_full:
                try:
                    exp = (datetime.fromisoformat(indexed_at_full)
                           + timedelta(days=ttl_days)).date().isoformat()
                    ttl_str = f"expires {exp}"
                except ValueError:
                    ttl_str = f"ttl {ttl_days}d"
            else:
                ttl_str = "permanent"
            tag_str = f"  [{tags}]" if tags else ""
            lines.append(f"  {doc_id}  {title:<40}  {ttl_str:<24}  {indexed_at}{tag_str}")
        shown_end = offset + len(page)
        if shown_end < total:
            lines.append(f"--- showing {offset + 1}-{shown_end} of {total}. next: offset={shown_end}")
        else:
            lines.append(f"--- showing {offset + 1}-{shown_end} of {total} (end)")
        return _cap_text_result("\n".join(lines), "store_list")
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("store_list", e)


def _store_list_docs(t3, col_name: str, total: int) -> str:
    """Document-level view: deduplicate chunks by content_hash.

    Per-doc chunk count is derived from the dedup pass — entries written by
    ``store_put`` don't set a ``chunk_count`` metadata field (only the PDF
    indexer does), so reading it from metadata produced ``?`` for everything.
    The page-count column is omitted entirely when no document carries it,
    rather than showing ``?p`` for non-PDF entries.
    """
    seen: dict[str, dict] = {}
    chunks_by_hash: dict[str, int] = {}
    offset = 0
    while offset < total:
        entries = t3.list_store(col_name, limit=300, offset=offset)
        if not entries:
            break
        for e in entries:
            h = e.get("content_hash", e.get("id", ""))
            if h not in seen:
                seen[h] = e
            chunks_by_hash[h] = chunks_by_hash.get(h, 0) + 1
        offset += 300

    if not seen:
        return f"No documents in {col_name}."

    docs = sorted(seen.items(), key=lambda kv: kv[1].get("title") or "")
    # page_count is not in ALLOWED_TOP_LEVEL — dropped by normalize() so
    # the read always returned empty; removed in nexus-59j0. nexus-1oguj
    # later promoted extraction_method to canonical, but this compact
    # list table wasn't extended to show it (store_get's single-document
    # display is; see there for the per-chunk value).
    lines = [f"{col_name}  ({len(docs)} documents, {total} chunks)"]
    for i, (h, d) in enumerate(docs, 1):
        # The full content-hash (RDR-180) is the doc_id that store_get
        # accepts. Surfaced whole so the list -> get flow round-trips.
        doc_id = d.get("id") or h
        title = (d.get("title") or "untitled")[:50]
        chunks = chunks_by_hash.get(h, "?")
        indexed = (d.get("indexed_at") or "")[:10]
        lines.append(f"  {i:3d}. {doc_id}  {title:<50}  {chunks:>4} chunks  {indexed}")
    return _cap_text_result("\n".join(lines), "store_list")


def _verify_t2_write_landed(
    *, project: str, title: str, content: str, row_id: object,
) -> tuple[str, str]:
    """Re-read a just-written T2 row and report whether it ACTUALLY landed.

    nexus-zra63 (nexus-piqm5 Layer 2). ``db.put`` returning a row id proves
    the call returned, not that a row exists: a store that silently no-ops
    hands back the same success shape as one that wrote. Layer 1
    (``subagent-stop-writes-scan.py``) catches writes that REPORTED failure by
    reading the agent's transcript; it structurally cannot see this case,
    because the transcript records the success string. So the only way to tell
    them apart is to read the value back.

    Modelled on :func:`nexus.catalog.store_hook` 's manifest verification
    (nexus-b6enc F2, "never trust a silent path"), which writes, re-reads, and
    treats an unavailable verifier as a hard stop rather than a pass.

    THREE-STATE, and the third state is the point. A read-back needs a live
    read path, so when the store is unreachable the check cannot run -- and a
    check that reports SUCCESS when persistence is unavailable is the same
    defect one level up, which nexus-piqm5 explicitly forbids. ``unreachable``
    is therefore neither ``verified`` nor a failure; the caller is told the
    write is unconfirmed in both directions.

    Returns ``(verdict, detail)`` with verdict one of ``verified`` /
    ``missing`` / ``mismatch`` / ``unreachable``.
    """
    try:
        with _t2_ctx() as db:
            landed, _candidates = db.resolve_title(project=project, title=title)
    except Exception as exc:  # noqa: BLE001 — verifier outage is a VERDICT, not a crash; see three-state note above
        return "unreachable", f"{type(exc).__name__}: {exc}"

    if landed is None:
        return "missing", (
            f"re-read of {project}/{title} found NO row, after a write that "
            f"reported success as id {row_id}"
        )
    got = landed.get("content")
    if got != content:
        return "mismatch", (
            f"re-read of {project}/{title} returned {len(got or '')} chars "
            f"but {len(content)} were written; a concurrent upsert of the same "
            f"(project, title) is one possible cause"
        )
    return "verified", ""


@mcp.tool(
    title="Store Memory Entry",
    annotations={"readOnlyHint": False, "destructiveHint": False},
    structured_output=False,
)
def memory_put(
    content: str,
    project: str,
    title: str,
    tags: str = "",
    ttl: int | None = 30,
    agent: str = "",
    session: str = "",
) -> str:
    """Store a memory entry in T2. Upserts by (project, title).

    Args:
        content: Text content to store
        project: Project namespace (e.g. "nexus", "nexus_active")
        title: Entry title (unique within project)
        tags: Comma-separated tags
        ttl: Time-to-live in days. DEFAULT 30: an entry whose call does not
            name a ttl EXPIRES in 30 days (extended by reads: effective ttl =
            ttl * (1 + ln(access_count + 1)), so the row nobody reads is the
            one that goes). Pass ``None``/``null`` EXPLICITLY for a permanent
            row; omitting the argument does NOT make it permanent — the
            signature's default wins (nexus-sv152; a previous version of this
            text said the opposite and every caller that believed it persisted
            nothing). ``memory_get`` shows the stored value on its ``TTL:``
            line. ``ttl=0`` is RETIRED (RDR-194 D5, nexus-tk070.p6a): this tool
            no longer coerces it to ``None``, and the engine itself now
            REJECTS ``ttl=0`` (and any ``ttl<=0``) with a loud 400 naming the
            fix, for every caller and every path — ``POST /v1/memory/put``
            included, not just this MCP tool. That rejection propagates
            through this function's error return (this tool does not
            swallow it). Previously ``ttl=0`` was silently coerced to
            permanent by THIS tool alone while the store and every other
            caller stayed trapped (a stored ``0`` fed
            ``effective_ttl = ttl * (1 + ln(access_count+1)) = 0``, deleted on
            the next sweep) — that MCP-only shim is deleted; "0 means
            permanent" is no longer true anywhere in this system.
        agent: Optional subagent / role attribution (e.g. "developer",
            "architect-planner"). When empty, falls back to
            ``NX_AGENT`` env, then NULL. Phase 1B (nexus-9clx) — lets
            ``nx tier-status`` slice tier writes by which agent did
            the persisting.
        session: Optional explicit session_id override. When empty,
            falls back to the parent's session_id resolution chain
            (``NX_SESSION_ID`` env → claude session file → NULL).
    """
    try:
        if not content:
            return "Error: content is required"
        # Translate empty strings to None so MemoryStore.put's
        # fall-back chain (NX_AGENT env, read_claude_session_id) takes
        # over rather than persisting literal empty strings.
        agent_arg = agent if agent else None
        # Resolve session at the MCP layer so NX_SESSION_ID env wins
        # over MemoryStore's legacy getsid-file fall-back. Subagents
        # carry NX_SESSION_ID set by claude_dispatch (RDR-094); the
        # legacy file is only present in non-MCP contexts.
        if session:
            session_arg: str | None = session
        else:
            import os as _os  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site
            session_arg = _os.environ.get("NX_SESSION_ID", "").strip() or None
        with _t2_ctx() as db:
            row_id = db.put(
                project=project,
                title=title,
                content=content,
                tags=tags,
                # nexus-tk070.p6a: no coercion — ttl flows verbatim. A ttl<=0
                # is rejected by the engine's own boundary validation
                # (MemoryHandler.requirePositiveOrNullTtl -> 400), surfaced to
                # the caller via _mcp_tool_error below, never swallowed here.
                ttl=ttl,
                agent=agent_arg,
                session=session_arg,
            )
        _record_tier_write(
            tool="memory_put", tier="T2",
            agent=agent_arg, project=project, target_title=title,
        )
        # nexus-zra63 (piqm5 Layer 2): verify the row is READABLE before
        # claiming it was stored. See _verify_t2_write_landed for why a
        # returned row id is not evidence and why "unreachable" is its own
        # verdict rather than a pass.
        verdict, detail = _verify_t2_write_landed(
            project=project, title=title, content=content, row_id=row_id,
        )
        if verdict == "unreachable":
            return (
                f"Stored (UNVERIFIED): [{row_id}] {project}/{title}\n"
                f"The write reported success but could NOT be read back: the "
                f"verification path is unavailable ({detail}). This is neither "
                f"a confirmed success nor a confirmed failure — re-read before "
                f"relying on this entry."
            )
        if verdict != "verified":
            # Raised, not returned, so it funnels through _mcp_tool_error and
            # carries the shared "Error: " prefix — which is also what Layer 1's
            # transcript scan keys on, so a silent no-op becomes visible to BOTH
            # layers instead of only this caller.
            raise RuntimeError(
                f"memory_put reported success but the row did not land "
                f"({verdict}): {detail}"
            )
        return f"Stored: [{row_id}] {project}/{title} (ttl: {_render_ttl(ttl)})"
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("memory_put", e)


def _render_ttl(ttl: object) -> str:
    """``permanent`` for a NULL ttl, else ``<n> days`` (nexus-sv152)."""
    if ttl is None or ttl == "":
        return "permanent"
    try:
        return f"{int(ttl)} days"
    except (TypeError, ValueError):
        return str(ttl)


@mcp.tool(
    title="Retrieve Memory Entry",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def memory_get(project: str, title: str = "") -> str:
    """Retrieve a memory entry by project and title.

    Title resolution is exact-then-prefix (nexus-e59o): if ``title`` does
    not match any entry exactly, a unique prefix match is returned. A
    caller passing ``"088-research-1"`` gets the full
    ``"088-research-1: <suffix>"`` entry as long as only one entry
    starts with that prefix in the project. Ambiguous prefixes are
    reported as a list so the caller can disambiguate rather than
    silently pick one.

    When title is empty, lists all entries for the project (titles only — use
    a second call with the specific title to get content).

    Args:
        project: Project namespace
        title: Entry title, exact or unique prefix. Leave empty to LIST
            all entries (titles only).
    """
    try:
        with _t2_ctx() as db:
            if title:
                entry, candidates = db.resolve_title(
                    project=project, title=title,
                )
                if entry is not None:
                    return (
                        f"[{entry['id']}] {entry['project']}/{entry['title']}\n"
                        f"Tags: {entry.get('tags', '')}\n"
                        f"Updated: {entry.get('timestamp', '')}\n"
                        # nexus-sv152: the stored ttl is otherwise invisible to
                        # every read surface, and memory_put's default is 30 days.
                        f"TTL: {_render_ttl(entry.get('ttl'))}\n\n"
                        f"{entry['content']}"
                    )
                if candidates:
                    lines = [
                        f"Ambiguous title prefix: {len(candidates)} entries "
                        f"match {title!r} in project {project!r}",
                    ]
                    for c in candidates:
                        lines.append(f"  [{c['id']}] {c['title']}")
                    lines.append(
                        "Re-call with the full title or a longer prefix.",
                    )
                    return "\n".join(lines)
                return f"Not found: {project}/{title}"
            else:
                entries = db.list_entries(project=project)
                if not entries:
                    return f"No entries for project {project!r}."
                lines: list[str] = [f"{project}  ({len(entries)} entries — titles only, call with title to get content)"]
                for e in entries:
                    lines.append(f"  [{e['id']}] {e['title']}  ({e.get('timestamp', '')[:10]})")
                return "\n".join(lines)
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("memory_get", e)


@mcp.tool(
    title="Delete Memory Entry",
    annotations={"readOnlyHint": False, "destructiveHint": True},
    structured_output=False,
)
def memory_delete(project: str, title: str) -> str:
    """Delete a T2 memory entry by project and title.

    Args:
        project: Project namespace
        title: Entry title to delete
    """
    try:
        if not project or not title:
            return "Error: project and title are required"
        with _t2_ctx() as db:
            deleted = db.delete(project=project, title=title)
        if deleted:
            return f"Deleted: {project}/{title}"
        return f"Not found: {project}/{title}"
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("memory_delete", e)


@mcp.tool(
    title="Search Memory Entries",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def memory_search(query: str, project: str = "", limit: int = 20, offset: int = 0) -> str:
    """Full-text search across T2 memory entries.

    Searches title, content, and tags fields via the engine's PostgreSQL
    full-text search (SQLite/FTS5 is retired — RDR-158 P4). Plain natural-
    language text, not a query grammar: there are no operators to learn.
    Results are paged. Use offset to retrieve subsequent pages.

    Args:
        query: Search query (plain text — matches tokens in title, content,
            and tags; a query made entirely of English stopwords, e.g.
            "and", finds nothing in content specifically and is reported as
            an error rather than a silent empty result, nexus-senub)
        project: Optional project filter
        limit: Page size (default 20)
        offset: Skip this many results (default 0). Use for pagination.
    """
    try:
        with _t2_ctx() as db:
            results = db.search(query, project=project or None)
        if not results:
            return "No results."
        total = len(results)
        page = results[offset:offset + limit]
        if not page:
            return f"No results at offset {offset} (total {total})."
        lines: list[str] = []
        for r in page:
            snippet = r["content"][:200].replace("\n", " ")
            lines.append(f"[{r['id']}] {r['project']}/{r['title']}\n  {snippet}")
        shown_end = offset + len(page)
        if shown_end < total:
            lines.append(f"\n--- showing {offset + 1}-{shown_end} of {total}. next: offset={shown_end}")
        else:
            lines.append(f"\n--- showing {offset + 1}-{shown_end} of {total} (end)")
        return _cap_text_result("\n\n".join(lines), "memory_search")
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("memory_search", e)


@mcp.tool(
    title="Consolidate Memory Entries",
    annotations={"readOnlyHint": False, "destructiveHint": True},
    structured_output=False,
)
def memory_consolidate(
    action: str,
    project: str,
    min_similarity: float = 0.7,
    idle_days: int = 30,
    keep_id: int = 0,
    delete_ids: str = "",
    merged_content: str = "",
    limit: int = 50,
    dry_run: bool = False,
    confirm_destructive: bool = False,
) -> str:
    """Memory consolidation tools (RDR-061 E6): find overlaps, merge entries, flag stale.

    Args:
        action: One of "find-overlaps", "merge", "flag-stale"
        project: T2 project namespace to operate on
        min_similarity: Jaccard threshold for find-overlaps (default 0.7)
        idle_days: Staleness threshold for flag-stale (default 30)
        keep_id: Entry ID to keep when merging
        delete_ids: Comma-separated IDs to delete during merge
        merged_content: Replacement content for kept entry during merge
        limit: Max results for find-overlaps (default 50)
        dry_run: For merge, return a preview without modifying T2 (default False)
        confirm_destructive: Required when merge would delete >1 entry (default False)
    """
    try:
        if action == "find-overlaps":
            if not project:
                return "Error: project is required for find-overlaps"
            with _t2_ctx() as db:
                pairs = db.find_overlapping_memories(
                    project=project,
                    min_similarity=min_similarity,
                    limit=limit,
                )
            if not pairs:
                return f"No overlapping memories in {project!r} (min_similarity={min_similarity})"
            lines = [f"Found {len(pairs)} overlapping pair(s) in {project!r}:"]
            for a, b in pairs:
                lines.append(f"  [{a['id']}] {a['title']}  ↔  [{b['id']}] {b['title']}")
            return "\n".join(lines)

        elif action == "merge":
            if keep_id <= 0 or not delete_ids or not merged_content:
                return "Error: merge requires keep_id>0, delete_ids, and merged_content"
            try:
                del_ids = [int(x.strip()) for x in delete_ids.split(",") if x.strip()]
            except ValueError:
                return "Error: delete_ids must be comma-separated integers"
            if not del_ids:
                return "Error: delete_ids must contain at least one integer ID"
            if keep_id in del_ids:
                return f"Error: keep_id ({keep_id}) must not appear in delete_ids"
            # Safety gate: merges deleting more than one entry require
            # explicit confirmation. Dry-run returns a preview without
            # modifying T2 (matches catalog_link_bulk's pattern).
            if dry_run:
                with _t2_ctx() as db:
                    keep_entry = db.get(id=keep_id)
                if keep_entry is None:
                    return f"Error: keep_id {keep_id} not found"
                preview = (
                    f"[DRY RUN] Would merge:\n"
                    f"  keep: [{keep_id}] {keep_entry['title']}\n"
                    f"  delete: {del_ids}\n"
                    f"  new content: {merged_content[:200]}"
                )
                return preview
            if len(del_ids) > 1 and not confirm_destructive:
                return (
                    f"Error: would delete {len(del_ids)} entries — set "
                    f"confirm_destructive=True to proceed, or dry_run=True to preview"
                )
            with _t2_ctx() as db:
                db.merge_memories(
                    keep_id=keep_id,
                    delete_ids=del_ids,
                    merged_content=merged_content,
                )
            return f"Merged: kept [{keep_id}], deleted {del_ids}"

        elif action == "flag-stale":
            if not project:
                return "Error: project is required for flag-stale"
            with _t2_ctx() as db:
                stale = db.flag_stale_memories(project=project, idle_days=idle_days)
            if not stale:
                return f"No stale entries in {project!r} (idle > {idle_days} days)"
            lines = [f"Stale entries in {project!r} (idle > {idle_days} days):"]
            for e in stale:
                last = e.get("last_accessed") or e.get("timestamp", "")
                lines.append(f"  [{e['id']}] {e['title']}  last: {last[:10]}")
            return "\n".join(lines)

        else:
            return f"Error: unknown action {action!r}. Use: find-overlaps, merge, flag-stale"
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("memory_consolidate", e)


@mcp.tool(
    title="Session Scratch Pad",
    annotations={"readOnlyHint": False, "destructiveHint": False},
    structured_output=False,
)
def scratch(
    action: str,
    content: str = "",
    query: str = "",
    tags: str = "",
    entry_id: str = "",
    limit: int = 10,
    agent: str = "",
) -> str:
    """T1 session scratch pad — ephemeral within-session storage.

    For ``search`` and ``list``, results are capped at ``limit``. A footer
    indicates when more entries exist.

    Args:
        action: One of "put", "search", "list", "get", "delete"
        content: Content to store (for "put")
        query: Search query (for "search")
        tags: Comma-separated tags (for "put")
        entry_id: Entry ID (for "get", "delete")
        limit: Max results for search/list (default 10)
        agent: Optional subagent / role attribution for "put" (e.g.
            "developer"). Empty falls back to ``NX_AGENT`` env, then
            unspecified. Phase 1B follow-up (nexus-9clx) — lets
            ``nx tier-status`` slice T1 writes by agent.
    """
    try:
        t1, isolated = _get_t1()
        prefix = "[T1 isolated] " if isolated else ""

        if action == "put":
            if not content:
                return "Error: content is required for put"
            agent_arg = agent if agent else ""  # T1.put falls back to NX_AGENT env
            doc_id = t1.put(content=content, tags=tags, agent=agent_arg)
            _record_tier_write(
                tool="scratch_put", tier="T1",
                agent=agent_arg or None,
                target_title=tags or doc_id,
            )
            return f"{prefix}Stored: {doc_id}"

        elif action == "search":
            if not query:
                return "Error: query is required for search"
            results = t1.search(query, n_results=limit)
            if not results:
                return f"{prefix}No results."
            lines: list[str] = []
            for r in results:
                snippet = r["content"][:200].replace("\n", " ")
                lines.append(f"{prefix}[{r['id'][:8]}] {snippet}")
            if len(results) >= limit:
                lines.append(f"\n--- showing {len(results)} results (limit={limit}). Increase limit to see more.")
            return _cap_text_result("\n".join(lines), "scratch")

        elif action == "list":
            entries = t1.list_entries()
            if not entries:
                return f"{prefix}No scratch entries."
            total = len(entries)
            entries = entries[:limit]
            lines = []
            for e in entries:
                snippet = e["content"][:80].replace("\n", " ")
                tags_str = f"  [{e.get('tags', '')}]" if e.get("tags") else ""
                lines.append(f"{prefix}[{e['id'][:8]}] {snippet}{tags_str}")
            if total > limit:
                lines.append(f"\n--- showing {limit} of {total} entries. Increase limit to see all.")
            return _cap_text_result("\n".join(lines), "scratch")

        elif action == "get":
            if not entry_id:
                return "Error: entry_id is required for get"
            entry = t1.get(entry_id)
            if entry is None:
                # nexus-zpw6: surface candidate list when the prefix
                # was ambiguous so the operator can disambiguate
                # instead of staring at "Not found" while the entry
                # IS there under a 9-char-prefix sibling.
                candidates = t1.resolve_prefix_candidates(entry_id)
                if len(candidates) > 1:
                    listed = ", ".join(c[:8] for c in candidates[:5])
                    more = "" if len(candidates) <= 5 else f" (+{len(candidates) - 5} more)"
                    return (
                        f"{prefix}Ambiguous prefix {entry_id!r}; "
                        f"matches: {listed}{more}. Pass a longer prefix or full UUID."
                    )
                return f"{prefix}Not found: {entry_id}"
            return f"{prefix}{entry['content']}"

        elif action == "delete":
            if not entry_id:
                return "Error: entry_id is required for delete"
            deleted = t1.delete(entry_id)
            if deleted:
                return f"{prefix}Deleted: {entry_id}"
            # nexus-zpw6: same disambiguation surface as get.
            candidates = t1.resolve_prefix_candidates(entry_id)
            if len(candidates) > 1:
                listed = ", ".join(c[:8] for c in candidates[:5])
                more = "" if len(candidates) <= 5 else f" (+{len(candidates) - 5} more)"
                return (
                    f"{prefix}Ambiguous prefix {entry_id!r}; "
                    f"matches: {listed}{more}. Pass a longer prefix or full UUID."
                )
            return f"{prefix}Not found or not owned: {entry_id}"

        else:
            return f"Error: unknown action {action!r}. Use: put, search, list, get, delete"
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("scratch", e)


@mcp.tool(
    title="Manage Scratch Entry",
    annotations={"readOnlyHint": False, "destructiveHint": False},
    structured_output=False,
)
def scratch_manage(
    action: str,
    entry_id: str,
    project: str = "",
    title: str = "",
) -> str:
    """Manage scratch entries: flag for persistence or promote to T2.

    Args:
        action: One of "flag", "promote"
        entry_id: Scratch entry ID
        project: Target project for promote (required for promote)
        title: Target title for promote (required for promote)
    """
    try:
        t1, isolated = _get_t1()
        prefix = "[T1 isolated] " if isolated else ""

        if action == "flag":
            t1.flag(entry_id, project=project, title=title)
            return f"{prefix}Flagged: {entry_id}"

        elif action == "promote":
            if not project or not title:
                return "Error: project and title are required for promote"
            with _t2_ctx() as t2:
                report = t1.promote(entry_id, project=project, title=title, t2=t2)
            return f"{prefix}Promoted: {entry_id} -> {project}/{title} (action={report.action})"

        else:
            return f"Error: unknown action {action!r}. Use: flag, promote"
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("scratch_manage", e)


@mcp.tool(
    title="List T3 Collections",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def collection_list() -> str:
    """List all T3 collections with document counts and embedding models."""
    try:
        cols = _get_t3().list_collections()
        if not cols:
            return "No collections found."
        lines: list[str] = []
        for c in sorted(cols, key=lambda x: x["name"]):
            model = embedding_model_for_collection(c["name"])
            lines.append(f"{c['name']}  {c['count']:>6} docs  ({model})")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("collection_list", e)


@mcp.tool(
    title="Save Query Plan",
    annotations={"readOnlyHint": False, "destructiveHint": False},
    structured_output=False,
)
def plan_save(
    query: str,
    plan_json: str,
    verb: str = "",
    project: str = "",
    outcome: str = "success",
    tags: str = "",
    ttl: int | None = None,
    scope_tags: str = "",
) -> str:
    """Save a *retrieval* query-execution plan to the T2 plan library.

    The plan_json should be a JSON string with the execution plan structure.
    Minimal schema: {"steps": [...], "tools_used": [...], "outcome_notes": "..."}

    The library is verb-dimensional (RDR-078): a plan is matched to a
    verb-shaped intent, so a ``verb`` is REQUIRED — a verb-less plan has no
    dimensional identity, can never match a verb-filtered nx_answer question,
    and only leaks in via raw FTS (the NULL-verb pollution class, nexus-fiovt).
    This is for reusable *retrieval* plans; **implementation / pipeline / phased
    execution plans belong in beads + T2 memory (``memory_put``), not here.**

    Args:
        query: The original natural-language question the plan answers.
        plan_json: JSON string of the execution plan (see schema above).
        verb: REQUIRED retrieval verb (e.g. research / analyze / query / review
            / debug / document). A verb-less save is refused.
        project: Project namespace for scoping (e.g. "nexus").
        outcome: Plan outcome, "success" or "partial".
        tags: Comma-separated tags (e.g. operation types used).
        ttl: Time-to-live in days. None means permanent (no expiry).
        scope_tags: RDR-091 Phase 2a comma-separated scope-tag string
            (e.g. ``"rdr__arcaneum,code__nexus"``). When empty, inferred
            from plan_json retrieval steps. Normalized at save time:
            trailing 8-char hex suffix and ``*`` globs are stripped.
    """
    try:
        if not query or not plan_json:
            return "Error: query and plan_json are required"
        # nexus-fiovt: refuse verb-less writes. The plan library is
        # verb-dimensional; a NULL-verb plan pollutes it (77/116 rows on the
        # 2026-07-05 audit) — it can false-match non-verb-filtered nx_answer
        # questions via FTS and is un-runnable by the retrieval plan runner.
        # Every legitimate writer (nx_answer's grow path, seeds) sets a verb.
        if not verb.strip():
            return (
                "Plan not saved: a 'verb' is required (a retrieval verb such as "
                "research / analyze / query / review / debug / document). Verb-less "
                "plans pollute the verb-dimensional retrieval-plan-match library. "
                "Implementation / pipeline / phased-execution plans belong in beads "
                "+ T2 memory (memory_put), not the plan library."
            )
        # nexus-vtp8h: refuse non-executable plan_json at the door. The
        # drift audit's plan 138 was a bead-dump that MATCHED at 0.66-0.70
        # then crashed the runner (unknown tool '') — save-time validation
        # kills the class before it can pollute the match library.
        from nexus.plans.schema import PlanTemplateSchemaError, validate_plan_steps  # noqa: PLC0415 — deferred import; rare/branch-local path

        try:
            parsed_plan = json.loads(plan_json)
        except (TypeError, ValueError) as exc:
            return (
                f"Plan not saved: plan_json is not valid JSON ({exc}). The "
                "plan library stores executable retrieval plans only."
            )
        try:
            validate_plan_steps(parsed_plan, require_steps=True)
        except PlanTemplateSchemaError as exc:
            return (
                f"Plan not saved: {exc}. The plan library stores executable "
                "retrieval plans (a non-empty steps list, each step with a "
                "tool); implementation / phased plans belong in beads + T2 "
                "memory (memory_put)."
            )
        # nexus-j5geq: route through the T2 daemon (plans.save_plan is in
        # _WRITE_OPS; the daemon serialises the write through its single WAL
        # writer, eliminating the "database is locked" races seen 2026-06-11).
        _sc_tags = scope_tags or None
        row_id = _t2_index_write(
            lambda db: db.plans.save_plan(
                query=query,
                plan_json=plan_json,
                outcome=outcome,
                tags=tags,
                project=project,
                ttl=ttl,
                scope_tags=_sc_tags,
                verb=verb.strip(),
            )
        )
        _record_tier_write(
            tool="plan_save", tier="plan",
            project=project, target_title=query[:80],
        )
        return f"Saved plan: [{row_id}] {query[:80]}"
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("plan_save", e)


@mcp.tool(
    title="Search Plan Library",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def plan_search(query: str, project: str = "", limit: int = 5, offset: int = 0) -> str:
    """Search the T2 plan library for similar query plans.

    Results are paged. Response footer shows ``offset=N`` for next page.

    Args:
        query: Search query (matched against plan query text and tags)
        project: Optional project filter (e.g. "nexus")
        limit: Maximum results to return (default 5)
        offset: Skip this many results (default 0). Use for pagination.
    """
    try:
        with _t2_ctx() as db:
            # Over-fetch by 1 to detect if there are more.
            # any_lexeme=True (nexus-vi8fp decision C): this is a HUMAN
            # browsing surface — a person judges the list — so it gets the
            # any-lexeme recall fallback plan_match deliberately does not.
            results = db.search_plans(
                query, limit=limit + 1, project=project, any_lexeme=True,
            )
        if offset:
            results = results[offset:]
        has_more = len(results) > limit
        results = results[:limit]
        if not results:
            return "No matching plans."
        lines: list[str] = []
        for r in results:
            plan_preview = r["plan_json"][:100].replace("\n", " ")
            scope_display = r.get("scope_tags") or "(agnostic)"
            lines.append(
                f"[{r['id']}] {r['query'][:60]}\n"
                f"  outcome={r['outcome']}  tags={r['tags']}\n"
                f"  scope={scope_display}\n"
                f"  plan: {plan_preview}..."
            )
        shown_end = offset + len(results)
        if has_more:
            lines.append(f"\n--- showing {offset + 1}-{shown_end}. may have more: offset={shown_end}")
        return _cap_text_result("\n\n".join(lines), "plan_search")
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("plan_search", e)


@mcp.tool(
    title="Delete Query Plan",
    annotations={"readOnlyHint": False, "destructiveHint": True},
    structured_output=False,
)
def plan_delete(plan_id: int) -> str:
    """Delete a plan-library entry by id (nexus-v92zj).

    The counterpart to ``plan_save``: removes a throwaway or incorrect
    entry (e.g. a shakeout probe) from the plan library without direct
    DB access. Get the id from ``plan_search`` output (``[NN]`` prefix)
    or ``nx plan list``.

    Args:
        plan_id: Numeric plan id to delete.
    """
    try:
        with _t2_ctx() as db:
            row = db.plans.get_plan(plan_id)
            if row is None:
                return f"Not found: plan id {plan_id}"
            removed = db.plans.delete_plan(plan_id)
        if not removed:
            # get_plan→delete_plan race: another caller deleted it first.
            return f"Not found: plan id {plan_id} (already deleted)"
        label = row.get("name") or row.get("query") or "(unnamed)"
        return f"Deleted plan id={plan_id}: {label[:80]}"
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("plan_delete", e)


# ── Demoted tools (plain functions, no @mcp.tool()) ──────────────────────────


def store_delete(doc_id: str, collection: str = "knowledge") -> str:
    """Delete a T3 knowledge entry by document ID.

    Args:
        doc_id: Document ID to delete (from store_list or store_put output)
        collection: Collection name or prefix (default: knowledge)
    """
    try:
        if not doc_id:
            return "Error: doc_id is required"
        t3 = _get_t3()
        col_name = t3_collection_name(collection, t3=t3)
        # nexus-b6enc C4: delete asymmetry — a store_put-origin catalog row
        # (knowledge, no file_path) keyed on this chunk id must not survive
        # with a stale chunk_count once the T3 chunk is gone. delete_document
        # tombstones that catalog row. CORRECTED (nexus-3ck2g): this comment
        # previously claimed delete_document cascades the manifest rows —
        # false. The engine soft-tombstones only; document_chunks + the T3
        # chunks survive until `nx catalog purge-trash` reclaims them, and
        # since catalog-026 the sweep honors the --older-than-days grace
        # window (row, manifest, and chunks go together once it passes —
        # see store_hook.store_delete_catalog_cleanup's docstring for the
        # full rationale; nexus-kcm6c corrected the stale not-age-gated
        # claim that used to live here). Cleanup failure here is
        # surfaced, never silent.
        #
        # nexus-c53hy (RDR-191 P2 round-2 fix): resolve-and-VERIFY before
        # cleanup. store_delete_catalog_cleanup resolves purely by chash,
        # with no check that doc_id is actually in col_name -- a bogus/
        # stale doc_id, or one paired with the wrong collection, would
        # otherwise get cleanup run unconditionally and could tombstone a
        # live, unrelated document that owns this exact chash under a
        # DIFFERENT collection. A collection-scoped existence check first
        # closes that off before any catalog mutation happens.
        if t3.get_by_id(col_name, doc_id) is None:
            return f"Not found: {doc_id!r} in {col_name}"

        # nexus-o8dil.5 (RDR-191 F10c follow-up): cleanup now runs BEFORE
        # the T3 delete, not after. PgVectorRepository#delete's anti-join
        # refuses to delete a chash any LIVE manifest row still
        # references — including this entry's OWN not-yet-tombstoned
        # manifest row. Cleaning up first lets the delete actually succeed
        # for the common single-owner case; the old delete-then-cleanup
        # order left the manifest row live at delete time, so a
        # legitimate entry's delete was silently refused by the anti-join
        # and misreported as "Not found" even though the entry
        # demonstrably existed. Now gated on the existence check above,
        # and scoped to col_name as a second defense-in-depth layer.
        from nexus.catalog.store_hook import store_delete_catalog_cleanup  # noqa: PLC0415 — deferred for startup cost
        tumbler, cleanup_error = store_delete_catalog_cleanup(doc_id, expected_collection=col_name)

        deleted = t3.delete_by_id(col_name, doc_id)
        if deleted:
            _page_cache_invalidate()
            if cleanup_error:
                return (
                    f"Deleted: {doc_id} from {col_name} (WARNING: catalog "
                    f"row {tumbler or '?'} NOT removed: {cleanup_error} — "
                    f"a stale catalog entry may survive; run "
                    f"'nx catalog reconcile')"
                )
            return f"Deleted: {doc_id} from {col_name}"
        # Existence was confirmed a moment ago, so this is NOT a plain
        # "not found" -- either a genuine race (something else deleted it
        # concurrently) or the delete hit the anti-join because cleanup
        # above declined to act (e.g. an ambiguous chash shared with
        # another live document). Distinguish from the true not-found
        # case above; cleanup for this id has already run.
        return (
            f"Entry {doc_id!r} existed in {col_name} moments ago but the "
            "delete did not remove it — likely anti-join-protected by "
            "another live reference, or a concurrent modification. Any "
            "catalog cleanup for it has already run; check "
            "'nx catalog reconcile'."
        )
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("store_delete", e)


def collection_info(name: str) -> str:
    """Get detailed information about a T3 collection, including a sample of entries.

    Args:
        name: Fully-qualified collection name (e.g. "knowledge__notes", "code__myrepo")
    """
    try:
        db = _get_t3()
        try:
            info = db.collection_info(name)
        except KeyError:
            return f"Collection not found: {name!r}"
        qry_model = embedding_model_for_collection(name)
        idx_model = index_model_for_collection(name)
        count = info.get("count", 0)
        lines: list[str] = [
            f"Collection:  {name}",
            f"Documents:   {count}",
            f"Index model: {idx_model}",
            f"Query model: {qry_model}",
        ]
        meta = info.get("metadata", {})
        if meta:
            lines.append(f"Metadata:    {meta}")

        # Peek: show first few entry titles for discoverability
        if count > 0:
            peek = db.list_store(name, limit=5, offset=0)
            if peek:
                lines.append("")
                lines.append("Sample entries:")
                for e in peek:
                    title = (e.get("title") or "untitled")[:60]
                    lines.append(f"  - {title}")

        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("collection_info", e)


def collection_verify(name: str) -> str:
    """Verify a collection's retrieval health via known-document probe.

    Args:
        name: Fully-qualified collection name (e.g. "knowledge__notes")
    """
    try:
        db = _get_t3()
        try:
            result = verify_collection_deep(db, name)
        except KeyError:
            return f"Collection not found: {name!r}"
        lines = [
            f"Collection: {name}",
            f"Status:     {result.status}",
            f"Documents:  {result.doc_count}",
        ]
        if result.distance is not None:
            lines.append(f"Probe distance: {result.distance:.4f} ({result.metric})")
        if result.probe_hit_rate is not None:
            lines.append(f"Probe hit rate: {result.probe_hit_rate:.0%}")
        if result.probe_doc_id:
            lines.append(f"Probe doc: {result.probe_doc_id}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001 — MCP tool boundary catch; error surfaced to caller via _mcp_tool_error (logged)
        return _mcp_tool_error("collection_verify", e)


# ── Operator tools ───────────────────────────────────────────────────────────


def _pin_default_model(model: "str | None") -> "str | None":
    """nexus-ek8tr (critic Critical, T2 [23233]): a DIRECT tool call with
    no ``model=`` — an MCP client invoking an operator, or the nx_tidy /
    nx_plan_audit / nx_enrich_beads heavy tools — must still dispatch at
    the EXPLICIT strong pin, never inherit the box CLI default bare.
    Caller-supplied models always win; the ``=0`` kill switch restores
    bare dispatch everywhere. Plan-routed dispatches arrive with an
    explicit model from the runner, so this never fires on that path.
    Note the direct-call contract is unchanged in tier terms: direct
    operator calls were always "strong by default" — this makes the
    strong identity explicit instead of inherited."""
    import os as _os  # noqa: PLC0415 — trivial stdlib; keeps helper self-contained

    if model is not None or _os.environ.get("NX_OPERATOR_MODEL_TIERING") == "0":
        return model
    from nexus.operators.model_tiers import STRONG_DEFAULT_ALIAS  # noqa: PLC0415 — default-path pin (nexus-ek8tr)

    return STRONG_DEFAULT_ALIAS


@mcp.tool(
    title="Extract Structured Fields",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
async def operator_extract(
    inputs: str, fields: str, timeout: float = 300.0, model: str | None = None,
) -> dict:
    """Extract structured fields from each input item using claude -p.

    Args:
        inputs: Items to extract from (plain text or JSON array string).
        fields: Comma-separated field names to extract.
        timeout: Seconds before the subprocess is killed. Default 300s (5 min) — the claude -p substrate handles multi-step analytical workloads; 120s was hitting false timeouts on real input.
        model: Opt-in ``--model`` override (RDR-196 .p2c, nexus-nyry9.16),
            pass-through to ``claude_dispatch``. ``None`` (default) is a
            no-op — argv unchanged. Threaded in by ``plan_run``'s
            ``NX_OPERATOR_MODEL_TIERING`` opt-in only; not consulted by
            any default path.
    """
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — rare/branch-local path; operator dispatch deferred to call time

    prompt = (
        f"Extract the following fields from each item: {fields}\n\n"
        f"Items:\n{inputs}"
    )
    schema = {
        "type": "object",
        "required": ["extractions"],
        "properties": {
            "extractions": {
                "type": "array",
                "items": {"type": "object"},
            }
        },
    }
    return await claude_dispatch(
        prompt, schema, timeout=timeout, model=_pin_default_model(model),
        operator="operator_extract",
    )


@mcp.tool(
    title="Rank Items by Criterion",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
async def operator_rank(
    items: str, criterion: str, timeout: float = 300.0, model: str | None = None,
) -> dict:
    """Rank items by a criterion using claude -p.

    Args:
        items: Items to rank (plain text or JSON array string).
        criterion: Natural-language ranking criterion.
        timeout: Seconds before the subprocess is killed. Default 300s (5 min) — the claude -p substrate handles multi-step analytical workloads; 120s was hitting false timeouts on real input.
        model: Opt-in ``--model`` override (RDR-196 .p2c, nexus-nyry9.16).
            ``None`` (default) is a no-op. See ``operator_extract``'s
            ``model`` docstring for the full contract.
    """
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — rare/branch-local path; operator dispatch deferred to call time

    prompt = (
        f"Rank the following items by {criterion}.\n"
        f"Return them in ranked order, best first.\n\n"
        f"Items:\n{items}"
    )
    schema = {
        "type": "object",
        "required": ["ranked"],
        "properties": {
            "ranked": {"type": "array", "items": {"type": "string"}},
        },
    }
    return await claude_dispatch(
        prompt, schema, timeout=timeout, model=_pin_default_model(model),
        operator="operator_rank",
    )


@mcp.tool(
    title="Compare Items",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
async def operator_compare(
    items: str = "",
    focus: str = "",
    timeout: float = 300.0,
    *,
    items_a: str = "",
    items_b: str = "",
    label_a: str = "A",
    label_b: str = "B",
    model: str | None = None,
) -> dict:
    """Compare items and return a structured comparison using claude -p.

    Two modes:

    * **One-sided** (original): pass *items* only. The comparison runs
      across entries within a single set. Keyword-only ``items_a`` /
      ``items_b`` may be omitted or empty.
    * **Two-sided** (nexus-km5i): pass *items_a* and *items_b* together
      for a cross-set compare. The prompt becomes "Compare set {label_a}
      vs set {label_b}" and asks for shared axes, divergent decisions,
      and philosophy differences. Useful for cross-corpus DAGs where a
      plan needs to align extractions from two different collections
      under one synthesis. ``focus`` scopes both modes.

    List / dict values in ``items`` / ``items_a`` / ``items_b`` are
    JSON-serialized before prompt interpolation so the LLM sees clean
    JSON instead of Python ``repr`` output.

    Args:
        items: Items to compare (plain text or JSON array string). Used
            in one-sided mode; ignored when both ``items_a`` and
            ``items_b`` are provided.
        focus: Optional aspect to focus the comparison on.
        timeout: Seconds before the subprocess is killed. Default 300s
            (5 min). The claude -p substrate handles multi-step
            analytical workloads; 120s hit false timeouts on real input.
        items_a: Side A items for two-sided compare.
        items_b: Side B items for two-sided compare.
        label_a: Human-readable label for side A (default "A").
        label_b: Human-readable label for side B (default "B").
    """
    import json as _json  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site

    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — rare/branch-local path; operator dispatch deferred to call time

    def _fmt(v) -> str:
        if isinstance(v, (list, dict)):
            return _json.dumps(v, indent=2, default=str)
        return v if isinstance(v, str) else str(v)

    focus_clause = f" Focus on: {focus}." if focus else ""
    if items_a and items_b:
        a_text = _fmt(items_a)
        b_text = _fmt(items_b)
        prompt = (
            f"Compare two sets of items across corpora.{focus_clause}\n\n"
            f"Set {label_a}:\n{a_text}\n\n"
            f"Set {label_b}:\n{b_text}\n\n"
            "Name:\n"
            f"  * **Shared axes**: concerns both {label_a} and {label_b} "
            "address with comparable intent (even if mechanism differs).\n"
            f"  * **Divergent decisions**: places where {label_a} and {label_b} "
            "take different approaches on the same question; attribute each "
            "choice to its side.\n"
            f"  * **Side-only axes**: concerns that appear in {label_a} or "
            f"{label_b} but not both.\n"
            "  * **Philosophy difference**: one or two sentences on the "
            "underlying stance difference, if one emerges from the evidence."
        )
    else:
        items_text = _fmt(items)
        prompt = (
            f"Compare the following items.{focus_clause}\n\n"
            f"Items:\n{items_text}"
        )
    schema = {
        "type": "object",
        "required": ["comparison"],
        "properties": {
            "comparison": {"type": "string"},
        },
    }
    return await claude_dispatch(
        prompt, schema, timeout=timeout, model=_pin_default_model(model),
        operator="operator_compare",
    )


@mcp.tool(
    title="Summarize Content",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
async def operator_summarize(
    content: str,
    cited: bool = False,
    timeout: float = 300.0,
    model: str | None = None,
) -> dict:
    """Summarize content using claude -p, optionally with citations.

    Args:
        content: Text to summarize.
        cited: If True, include a citations list in the output.
        timeout: Seconds before the subprocess is killed. Default 300s (5 min) — the claude -p substrate handles multi-step analytical workloads; 120s was hitting false timeouts on real input.
    """
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — rare/branch-local path; operator dispatch deferred to call time

    cite_clause = " Include citations as a list of source references." if cited else ""
    prompt = f"Summarize the following content concisely.{cite_clause}\n\n{content}"
    schema: dict = {
        "type": "object",
        "required": ["summary"],
        "properties": {
            "summary": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "string"}},
        },
    }
    return await claude_dispatch(
        prompt, schema, timeout=timeout, model=_pin_default_model(model),
        operator="operator_summarize",
    )


@mcp.tool(
    title="Generate from Template",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
async def operator_generate(
    template: str,
    context: str,
    cited: bool = False,
    timeout: float = 300.0,
    model: str | None = None,
) -> dict:
    """Generate output from a template and context using claude -p.

    Args:
        template: Named template or description of desired output form.
        context: Source material or context to generate from.
        cited: If True, include a citations list in the output.
        timeout: Seconds before the subprocess is killed. Default 300s (5 min) — the claude -p substrate handles multi-step analytical workloads; 120s was hitting false timeouts on real input.
    """
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — rare/branch-local path; operator dispatch deferred to call time

    cite_clause = " Include citations as a list of source references." if cited else ""
    prompt = (
        f"Generate a {template}.{cite_clause}\n\n"
        f"Context:\n{context}"
    )
    schema: dict = {
        "type": "object",
        "required": ["output"],
        "properties": {
            "output": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "string"}},
        },
    }
    return await claude_dispatch(
        prompt, schema, timeout=timeout, model=_pin_default_model(model),
        operator="operator_generate",
    )


#: Shared evidence-item schema for ``operator_check`` (RDR-088 Phase 2).
#: Each entry is a citation-like record grounding the verdict across a
#: multi-item consistency probe. ``role`` is enum-restricted so downstream
#: plan steps can branch on the trichotomy without parsing free text.
_CHECK_EVIDENCE_ITEM_SCHEMA: dict = {
    "type": "object",
    "required": ["item_id", "quote", "role"],
    "properties": {
        "item_id": {"type": "string"},
        "quote": {"type": "string"},
        "role": {
            "type": "string",
            "enum": ["supports", "contradicts", "neutral"],
        },
    },
}


@mcp.tool(
    title="Filter Items by Criterion",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
async def operator_filter(
    items: str,
    criterion: str,
    timeout: float = 300.0,
    source: str = "auto",
    aspect_field: str = "",
    model: str | None = None,
) -> dict:
    """Filter items by a criterion, returning a subset with rationale.

    RDR-088 Phase 1. Paper §D.4 Filter operator: given a prior-step's
    output list and a natural-language criterion, return the items that
    satisfy the criterion plus a per-item reason for the keep / reject
    decision. Composable with ``operator_extract``, ``operator_rank``,
    and retrieval tools via ``plan_run``. Distinct from ChromaDB's
    metadata ``where=`` filter which operates at retrieval time over
    structured fields; ``operator_filter`` operates over arbitrary
    prior-step results with natural-language predicates.

    Two execution paths (RDR-089 follow-up):

    - **SQL fast path** (default ``source="auto"``): when items carry
      ``collection`` + ``source_path`` identity AND an aspect column
      can be resolved (either explicitly via ``aspect_field`` or by
      heuristic inference from the criterion), filter via a SQL
      query against ``document_aspects`` in PostgreSQL. Returns in
      milliseconds.
    - **LLM path** (``source="llm"`` or fallback): dispatches the
      criterion to ``claude -p`` per call. The original behavior;
      kicks in when SQL prerequisites do not hold.

    Args:
        items: Items to filter (plain text or JSON array string). Each
            element should carry an ``id`` field when rationale round-
            tripping matters; downstream plan steps key on ``id``. For
            the SQL path each item additionally needs ``collection``
            and ``source_path``.
        criterion: Natural-language predicate describing the keep
            condition (e.g. "peer-reviewed only", "published after 2023",
            "uses TPC-C dataset"). For the SQL path the keyword cues
            in this string drive aspect-field inference unless
            ``aspect_field`` overrides.
        timeout: Seconds before the subprocess is killed. Default 300s
            (LLM path only; SQL path is bounded by the engine's query time).
        source: Execution mode. ``"auto"`` (default) tries SQL first,
            falls back to LLM on prerequisite failure. ``"aspects"``
            forces SQL — failing prerequisites yield an empty result
            with rationale rather than a silent LLM dispatch.
            ``"llm"`` skips SQL entirely.
        aspect_field: Explicit ``document_aspects`` column when the
            caller knows which field to filter on (e.g.
            ``"experimental_datasets"``, ``"extras.venue"``). Disables
            heuristic inference for this call.
        model: Opt-in ``--model`` override (RDR-196 .p2c, nexus-nyry9.16),
            LLM path only — the SQL fast path never dispatches. ``None``
            (default) is a no-op. See ``operator_extract``'s ``model``
            docstring for the full contract.
    """
    from nexus.operators.aspect_sql import try_filter  # noqa: PLC0415 — rare/branch-local path; SQL fast-path import deferred to call time
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — rare/branch-local path; operator dispatch deferred to call time

    sql_result = try_filter(
        items, criterion, source=source, aspect_field=aspect_field,
    )
    if sql_result is not None:
        # RDR-196 .p1b (Gap 5, nexus-nyry9.8): minimal marker so plan_run
        # can record source="sql" for this step instead of the default
        # is_operator_tool()-inferred "llm" -- popped off the dict by
        # plan_run before the result reaches step_outputs, so it never
        # leaks into a $stepN.field reference. The LLM-fallback branch
        # below needs no matching marker: plan_run's default inference
        # already says "llm" for any operator-tool dispatch absent this
        # override.
        return {**sql_result, "_dispatch_source": "sql"}

    prompt = (
        f"Filter the following items by this criterion: {criterion}\n"
        f"Return only the items that satisfy the criterion in the 'items' "
        f"array. Populate 'rationale' with one entry per input item, "
        f"keyed by the item's id, giving the reason each item was kept "
        f"or rejected. The output 'items' array must be a subset of the "
        f"input; never add synthetic items.\n\n"
        f"Items:\n{items}"
    )
    schema: dict = {
        "type": "object",
        "required": ["items", "rationale"],
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object"},
            },
            "rationale": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "reason"],
                    "properties": {
                        "id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    }
    return await claude_dispatch(
        prompt, schema, timeout=timeout, model=_pin_default_model(model),
        operator="operator_filter",
    )


@mcp.tool(
    title="Check Cross-Item Consistency",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
async def operator_check(
    items: str,
    check_instruction: str,
    timeout: float = 300.0,
    model: str | None = None,
) -> dict:
    """Check a claim's consistency across peer items using claude -p.

    RDR-088 Phase 2. Paper §D.2 Check operator: validate a claim across
    N peer items (papers, documents, extracted records) and return a
    structured boolean plus grounding evidence. Unlike ``operator_compare``
    which returns free-text, ``operator_check`` returns a composable
    ``{ok: bool, evidence: list[{item_id, quote, role}]}`` payload so
    plan steps can branch deterministically.

    Evidence role is one of ``supports``, ``contradicts``, ``neutral``.
    Populate at least one entry per item unless ``ok=True`` trivially
    (every item agrees with no nuance to report).

    Args:
        items: Items to check for consistency (plain text or JSON array
            string). Each entry should carry an ``id`` field; evidence
            entries key ``item_id`` against these ids.
        check_instruction: Natural-language claim or consistency
            question to evaluate across the items (e.g. "do all papers
            agree on the baseline numbers?").
        timeout: Seconds before the subprocess is killed. Default 300s.
        model: Opt-in ``--model`` override (nexus-3mea3, the 2026-08-21
            check/verify default flip), pass-through to ``claude_dispatch``
            exactly as on ``operator_filter``. ``None`` (default) is a
            no-op — argv unchanged; the plan runner's tiering branch is
            what supplies the cheap alias on the default path.
    """
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — rare/branch-local path; operator dispatch deferred to call time

    prompt = (
        f"Check whether the following items are consistent with this "
        f"claim or question: {check_instruction}\n"
        f"Set ok=true when every item supports the claim, false when at "
        f"least one item contradicts it. Populate 'evidence' with a "
        f"record per item containing a short grounding 'quote' and a "
        f"'role' of 'supports', 'contradicts', or 'neutral'. Keep quotes "
        f"short enough to be verifiable against the source item.\n\n"
        f"Items:\n{items}"
    )
    schema: dict = {
        "type": "object",
        "required": ["ok", "evidence"],
        "properties": {
            "ok": {"type": "boolean"},
            "evidence": {
                "type": "array",
                "items": _CHECK_EVIDENCE_ITEM_SCHEMA,
            },
        },
    }
    return await claude_dispatch(
        prompt, schema, timeout=timeout, model=_pin_default_model(model),
        operator="operator_check",
    )


@mcp.tool(
    title="Verify Claim Against Evidence",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
async def operator_verify(
    claim: str,
    evidence: str,
    timeout: float = 300.0,
    model: str | None = None,
) -> dict:
    """Verify a single claim against a single evidence source using claude -p.

    RDR-088 Phase 2. Paper §D.2 Verify operator: targeted single-claim
    variant of ``operator_check``. Returns ``{verified: bool, reason: str,
    citations: list[str]}`` where citations are span anchors or locators
    pulled from the evidence text that ground the verdict.

    Distinct from ``operator_check`` by cardinality: verify is 1-claim to
    1-evidence; check is 1-claim to N-items.

    Args:
        claim: A single assertion to verify (e.g. "the paper reports 2048
            GPU-hours for training").
        evidence: The source material to verify the claim against.
            Typically a section text, extracted passage, or document
            body. Not a collection of items.
        timeout: Seconds before the subprocess is killed. Default 300s.
        model: Opt-in ``--model`` override (nexus-3mea3, the 2026-08-21
            check/verify default flip) — same pass-through contract as
            ``operator_check``'s ``model``.
    """
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — rare/branch-local path; operator dispatch deferred to call time

    prompt = (
        f"Verify whether the following claim is grounded in the evidence "
        f"provided.\n\n"
        f"Claim: {claim}\n\n"
        f"Evidence:\n{evidence}\n\n"
        f"Set verified=true only when the claim is directly supported by "
        f"the evidence. Provide a concise 'reason' explaining the "
        f"verdict. Populate 'citations' with locators (section, page, "
        f"table, or quoted span snippets) that pinpoint the supporting "
        f"or contradicting passages."
    )
    schema: dict = {
        "type": "object",
        "required": ["verified", "reason", "citations"],
        "properties": {
            "verified": {"type": "boolean"},
            "reason": {"type": "string"},
            "citations": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }
    return await claude_dispatch(
        prompt, schema, timeout=timeout, model=_pin_default_model(model),
        operator="operator_verify",
    )


@mcp.tool(
    title="Group Items by Key",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
async def operator_groupby(
    items: str,
    key: str,
    timeout: float = 300.0,
    source: str = "auto",
    aspect_field: str = "",
    model: str | None = None,
) -> dict:
    """Partition items by a natural-language key.

    Two execution paths (RDR-089 follow-up): ``source="auto"`` (default)
    runs the SQL fast path against ``document_aspects`` when items
    carry ``collection`` + ``source_path`` identity; falls back to
    ``claude -p`` dispatch otherwise. ``source="aspects"`` forces SQL
    and surfaces prerequisite failures as a stub group. ``source="llm"``
    skips SQL. ``aspect_field`` overrides the heuristic inference.

    Group cardinality on the SQL path: scalar columns produce one
    group per unique value (high cardinality for free-text fields).
    JSON-array columns unroll across array values — a paper with
    two datasets appears in two groups. ``extras.<key>`` form maps
    via ``json_extract``.

    RDR-093 Phase 1. Paper §D.4 GroupBy operator: take a flat list of
    items + a partition expression and return a structured grouping.
    Each group carries its label (``key_value``) and the items that
    belong to the group, with **items carried inline** (full dicts,
    not id-only references). Pairs with ``operator_aggregate`` to form
    the canonical ``filter → groupby → aggregate`` pipeline.

    The inline-items contract is load-bearing for the bundled
    ``groupby → aggregate`` path: a single ``claude -p`` dispatch has
    no host-side retrieval, so aggregate must see resolvable content
    inside the bundle prompt. Reverting to id-references would break
    the bundle path. (RDR-093 Gate finding C-1.)

    Items the operator cannot confidently assign land in a group
    with ``key_value="unassigned"``. Plan authors can inspect the
    unassigned group's size as a quality signal.

    Cardinality cap: ``_OPERATOR_MAX_INPUTS=100`` enforced by the
    plan runner's auto-hydration. When the cap fires the runner
    attaches a ``{truncated, original_count, kept_count}`` block to
    this operator's return envelope so callers see the truncation
    rather than silently losing items. Originally scoped to
    ``operator_groupby`` in RDR-093 S-1; generalised to every
    operator that runs through the ids-branch auto-hydration in
    nexus-3j6b.

    Args:
        items: Items to partition (plain text or JSON array string).
            Each element should carry an ``id`` field for round-trip
            composability; downstream operators (e.g. ``aggregate``)
            key on ``id``.
        key: Natural-language partition expression. May name a
            structured field ("publication_year", "method family"),
            an inferred property, or a derived attribute. The
            operator does NOT require the key to surface verbatim
            in the items; inference is fine.
        timeout: Seconds before the subprocess is killed. Default 300s.
        source: ``"auto"`` (default) | ``"aspects"`` | ``"llm"``.
        aspect_field: explicit ``document_aspects`` column override.
        model: Opt-in ``--model`` override (RDR-196 .p2c, nexus-nyry9.16),
            LLM path only — the SQL fast path never dispatches. ``None``
            (default) is a no-op. See ``operator_extract``'s ``model``
            docstring for the full contract.
    """
    from nexus.operators.aspect_sql import try_groupby  # noqa: PLC0415 — rare/branch-local path; SQL fast-path import deferred to call time
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — rare/branch-local path; operator dispatch deferred to call time

    sql_result = try_groupby(
        items, key, source=source, aspect_field=aspect_field,
    )
    if sql_result is not None:
        # RDR-196 .p1b (Gap 5, nexus-nyry9.8): minimal marker so plan_run
        # can record source="sql" for this step instead of the default
        # is_operator_tool()-inferred "llm" -- popped off the dict by
        # plan_run before the result reaches step_outputs, so it never
        # leaks into a $stepN.field reference. The LLM-fallback branch
        # below needs no matching marker: plan_run's default inference
        # already says "llm" for any operator-tool dispatch absent this
        # override.
        return {**sql_result, "_dispatch_source": "sql"}

    prompt = (
        f"Partition the following items by this key: {key}\n"
        f"Output a list of groups. Each group has a string `key_value` "
        f"(the partition label, e.g. a year, a fault model, a system "
        f"property) and an `items` array carrying each item's full "
        f"content INLINE — preserve the original `id` field and any "
        f"other fields verbatim. Every input item appears in exactly "
        f"one group's `items`. Items the partition cannot confidently "
        f"assign go in a group with `key_value` of \"unassigned\".\n\n"
        f"Do not reference items by id-only — carry the full item "
        f"dicts in each group's `items` array so downstream operators "
        f"see the content without a separate lookup.\n\n"
        f"Items:\n{items}"
    )
    schema: dict = {
        "type": "object",
        "required": ["groups"],
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["key_value", "items"],
                    "properties": {
                        "key_value": {"type": "string"},
                        "items": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                    },
                },
            },
        },
    }
    return await claude_dispatch(
        prompt, schema, timeout=timeout, model=_pin_default_model(model),
        operator="operator_groupby",
    )


@mcp.tool(
    title="Aggregate Grouped Items",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
async def operator_aggregate(
    groups: str,
    reducer: str,
    timeout: float = 300.0,
    source: str = "auto",
    aspect_field: str = "",
    model: str | None = None,
) -> dict:
    """Reduce each group of items to a per-group summary.

    Two execution paths (RDR-089 follow-up). The SQL fast path
    (default ``source="auto"``) recognises a small reducer
    vocabulary backed by SQL aggregates: ``count``, ``count
    distinct``, and ``avg`` / ``min`` / ``max confidence``. The
    ``avg/min/max confidence`` reducers query
    ``document_aspects.confidence`` per group. Anything outside the
    recognised vocabulary falls back to ``claude -p`` dispatch
    automatically. ``source="aspects"`` forces SQL and stubs
    out unrecognised reducers; ``source="llm"`` skips SQL entirely.

    RDR-093 Phase 2. Paper §D.4 Aggregate operator: take a keyed
    grouping (typically from a prior ``operator_groupby`` step) plus a
    natural-language reducer instruction, return one summary per
    group with the group's ``key_value`` preserved verbatim. Pairs
    with ``operator_groupby`` to form the canonical
    ``filter -> groupby -> aggregate`` analytic pipeline.

    Items arrive pre-hydrated inside each group's ``items`` array per
    ``operator_groupby``'s C-1 inline-items contract. No runner-side
    nested-id hydration is required; both bundled and isolated paths
    see the same shape.

    Group isolation: the prompt explicitly instructs the model to
    summarise USING ONLY the items in each group. Spike B (bead
    nexus-rojs) verified this framing produces 0% cross-group
    leakage even on adversarial fixtures with vocabulary heavily
    overlapping across groups.

    Args:
        groups: A JSON-serialised ``list[{key_value, items: list[dict]}]``
            from a prior groupby step. Items are dicts (inline), not
            id references.
        reducer: Natural-language reduction instruction
            (e.g. "winning baseline by reported metric",
            "most-cited method", "earliest publication").
        timeout: Seconds before the subprocess is killed. Default 300s.
        source: ``"auto"`` (default) | ``"aspects"`` | ``"llm"``.
        aspect_field: explicit ``document_aspects`` column override.
            Currently unused by the aggregate fast path (the
            recognised reducer vocabulary already disambiguates the
            target column); reserved for forward extensions.
    """
    from nexus.operators.aspect_sql import try_aggregate  # noqa: PLC0415 — rare/branch-local path; SQL fast-path import deferred to call time
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — rare/branch-local path; operator dispatch deferred to call time

    sql_result = try_aggregate(
        groups, reducer, source=source, aspect_field=aspect_field,
    )
    if sql_result is not None:
        # RDR-196 .p1b (Gap 5, nexus-nyry9.8): minimal marker so plan_run
        # can record source="sql" for this step instead of the default
        # is_operator_tool()-inferred "llm" -- popped off the dict by
        # plan_run before the result reaches step_outputs, so it never
        # leaks into a $stepN.field reference. The LLM-fallback branch
        # below needs no matching marker: plan_run's default inference
        # already says "llm" for any operator-tool dispatch absent this
        # override.
        return {**sql_result, "_dispatch_source": "sql"}

    prompt = (
        f"Reduce each group of items into a per-group summary using "
        f"this reducer instruction: {reducer}\n\n"
        f"Output one aggregate per input group, preserving the group's "
        f"`key_value` verbatim. Each `summary` MUST reference only the "
        f"items in that group's `items` array. Do NOT pull content "
        f"from items in other groups, even when vocabulary overlaps "
        f"across groups. The summary is a short paragraph answering "
        f"the reducer instruction USING ONLY this group's items.\n\n"
        f"Groups:\n{groups}"
    )
    schema: dict = {
        "type": "object",
        "required": ["aggregates"],
        "properties": {
            "aggregates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["key_value", "summary"],
                    "properties": {
                        "key_value": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                },
            },
        },
    }
    return await claude_dispatch(
        prompt, schema, timeout=timeout, model=_pin_default_model(model),
        operator="operator_aggregate",
    )


# ── traverse (RDR-078 P3) ─────────────────────────────────────────────────────

#: Depth cap for traverse steps (SC-4).
_TRAVERSE_MAX_DEPTH: int = 3


@mcp.tool(
    title="Walk Catalog Link Graph",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
def traverse(
    seeds: list[str] | str,
    link_types: list[str] | None = None,
    purpose: str = "",
    depth: int = 1,
    direction: str = "both",
) -> dict:
    """Walk the catalog link graph from seed tumblers. RDR-078 P3 (SC-4/SC-5).

    Accepts either explicit ``link_types`` **or** a ``purpose`` name — never
    both (SC-16). Returns the standard retrieval step-output contract so
    downstream plan steps can reference ``$stepN.tumblers``,
    ``$stepN.collections``, or ``$stepN.ids``.

    Args:
        seeds: One or more tumbler strings (e.g. ``["1.1", "1.2"]``).
               Also accepts a single string for convenience.
        link_types: Explicit catalog link types to follow
                    (``"implements"``, ``"cites"``, …).
                    Mutually exclusive with ``purpose``.
        purpose: Named alias for a link-type set (e.g.
                 ``"find-implementations"``).  Resolved via
                 ``nexus.plans.purposes.resolve_purpose``.
                 Mutually exclusive with ``link_types``.
        depth: BFS depth. Capped at 3 (SC-4).
        direction: ``"out"`` | ``"in"`` | ``"both"`` (default).

    Returns:
        ``{"tumblers": [...], "ids": [], "collections": [...]}``
    """
    from nexus.plans.purposes import resolve_purpose  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

    # SC-16: mutual exclusion.
    if link_types and purpose:
        return {"error": "traverse: specify link_types OR purpose, not both"}

    # Normalise seeds to a list.
    if isinstance(seeds, str):
        seeds = [seeds] if seeds else []

    if not seeds:
        return {"tumblers": [], "ids": [], "collections": []}

    # Resolve link types.
    if purpose:
        resolved = resolve_purpose(purpose)
        if not resolved:
            return {
                "tumblers": [], "ids": [], "collections": [],
                "warning": f"traverse: unknown purpose {purpose!r}",
            }
        effective_types: list[str] = resolved
    elif link_types:
        effective_types = list(link_types)
    else:
        effective_types = []

    depth = min(depth, _TRAVERSE_MAX_DEPTH)

    catalog = _get_catalog()
    if catalog is None:
        return {"error": "traverse: catalog not available"}

    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

    seed_tumblers = []
    for s in seeds:
        try:
            seed_tumblers.append(Tumbler.parse(s))
        except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
            pass  # drop unparseable seeds

    if not seed_tumblers:
        return {"tumblers": [], "ids": [], "collections": []}

    kw = dict(depth=depth, direction=direction, link_types=effective_types or None)
    if len(seed_tumblers) == 1:
        result = catalog.graph(seed_tumblers[0], **kw)
    else:
        result = catalog.graph_many(seed_tumblers, **kw)

    nodes = result.get("nodes") or []
    tumblers = [str(n.tumbler) for n in nodes if hasattr(n, "tumbler")]
    collections = list({
        n.physical_collection
        for n in nodes
        if hasattr(n, "physical_collection") and n.physical_collection
    })

    # Resolve chunk IDs from each node's CATALOG MANIFEST.
    #
    # nexus-bm8dd: this used to ask T3 for `ids_for_source(collection,
    # file_path)`. Chunk metadata has carried no source_path since RDR-102 D2
    # removed it from the schema, so that where-filter matched nothing and every
    # traverse returned ids=[] — silently, because the per-node except swallowed
    # it and an empty list is a legitimate answer. The manifest is the addressing
    # RDR-108 replaced it with: documents.tumbler -> document_chunks.chash, one
    # batched round-trip for every node instead of one T3 query per node.
    chunk_ids: list[str] = []
    if tumblers:
        try:
            seen_ids: set[str] = set()
            for rows in catalog.get_manifests(tumblers).values():
                for row in rows:
                    chash = getattr(row, "chash", "") or ""
                    if chash and chash not in seen_ids:
                        seen_ids.add(chash)
                        chunk_ids.append(chash)
        except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
            pass  # catalog manifest unavailable — ids stays empty

    return {"tumblers": tumblers, "ids": chunk_ids, "collections": collections}


# ── nx_answer helpers (RDR-080) ───────────────────────────────────────────────

#: Maximum inputs passed to an operator before auto-inserting a rank winnow.
_OPERATOR_MAX_INPUTS: int = 100

#: Minimum confidence for a plan_match result to count as a hit.
_PLAN_MATCH_MIN_CONFIDENCE: float = 0.40

#: JSON schema for the inline plan-miss planner.
_PLANNER_SCHEMA: dict = {
    "type": "object",
    "required": ["steps"],
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["tool", "args"],
                "additionalProperties": False,
                "properties": {
                    "tool": {"type": "string"},
                    "args": {"type": "object"},
                },
            },
        },
    },
    "additionalProperties": False,
}

#: Tool-signature hint text included in the inline-planner prompt so the
#: LLM generates args that match the actual MCP tool contracts.  Without
#: this, the planner typically emits ``operator_extract(corpus=..., query=...)``
#: — tokens the tool's signature doesn't accept — and the step fails with
#: ``missing required argument 'inputs'/'fields'``.
_PLANNER_TOOL_REFERENCE = """\
Use ONLY these tools (bare names; the runner maps them to MCP calls).

=== Retrieval tools ===
Each returns {"ids": [...], "tumblers": [...], "distances": [...], "collections": [...]}.
THEY DO NOT RETURN CONTENT. To get text, chain into store_get_many.

  search(query, corpus="all", limit=10, topic="", where="")
      - `query` is the search string.  `corpus` must be "all", a prefix
        (rdr/knowledge/code), or a full collection name.
      - Output: {ids, tumblers, distances, collections}

  query(question, corpus="all", limit=10, author="", content_type="",
        subtree="", follow_links="", depth=1)
      - Document-level retrieval with catalog-aware routing.
      - Output: {ids, tumblers, distances, collections}
      - Scope filter guidance (bead nexus-sgrg): prefer `corpus=<collection>`
        for project scoping. The `author` filter matches the catalog
        `author` column, which is rarely populated for RDR/docs; setting
        `author=<repo-name>` almost always returns zero rows. Only use
        `author=` when you know the catalog has it (e.g. knowledge docs
        where an explicit author tag was registered). The same caveat
        applies to `content_type=`: it matches exact values like
        `"rdr"`, `"code"`, `"prose"`, `"knowledge"`, not free-form tags.

  traverse(seeds, link_types=[...] OR purpose="<name>", depth=1, direction="both")
      - Walk catalog edges from seed tumblers.
      - `seeds` is a list of tumbler strings. Specify EITHER link_types
        (e.g. ["implements"]) OR purpose ("find-implementations",
        "decision-evolution", "reference-chain", "documentation-for") —
        never both. Depth capped at 3.
      - Output: {tumblers, ids, collections}

=== Content hydration ===
  store_get_many(ids=[...], collections="knowledge")
      - Batch hydration — turn IDs into actual text.
      - `ids` MUST come from a prior retrieval step: ids=$step1.ids
      - `collections` MUST come from a prior retrieval step:
        collections=$step1.collections
      - Output: {contents: [str, ...], missing: [str, ...]}

=== Operators (LLM-backed) ===
Each requires hydrated text as input — NOT ids/tumblers.

  extract(inputs, fields)
      - `inputs` is a JSON array of content strings. Pass $stepN.contents
        where step N was store_get_many.
      - `fields` is a comma-separated string like "topic,decision,year".
      - Output: {extractions: [dict, ...]}

  rank(items, criterion)
      - `items` is a JSON array. `criterion` is a string.
      - Output: {ranked: [...]}

  compare(items, focus="")
      - `items` is a JSON array.  `focus` is an optional axis.
      - Output: {comparison: str or {dict}}

  summarize(content, cited=false)
      - `content` is a SINGLE string (not a list).  Pass one of:
          * $stepN.contents when step N is store_get_many (runner will
            auto-join the list into a single string).
          * A literal string.
      - Output: {summary: str}

  generate(template, context, cited=false)
      - `template` is a natural-language instruction; `context` is a
        string (similar rules as summarize.content).
      - Output: {text: str}

=== Correct chain patterns ===

Pattern A (search → hydrate → operate):
  step1: search(query=..., corpus="all")      → {ids, tumblers, collections}
  step2: store_get_many(ids=$step1.ids,
                        collections=$step1.collections)
                                               → {contents, missing}
  step3: summarize(content=$step2.contents)    → {summary}

Pattern B (operator auto-hydration shortcut):
  step1: search(query=..., corpus="all")
  step2: summarize(ids=$step1.ids,
                   collections=$step1.collections)
    # Runner auto-calls store_get_many for you when an operator step
    # receives `ids` + `collections`.  Skips the explicit hydration step.

=== Step-output reference plumbing ===
  $stepN.<field> — e.g. $step1.ids, $step2.contents.  Never $stepN alone.
  The <field> must be one the tool actually returns (see output contracts
  above).  A mismatch fails with PlanRunStepRefError.

=== Forbidden tools ===
  Do NOT emit mcp__plugin_conexus_nexus-catalog__* names — use traverse.
  Do NOT emit Read, Grep, Bash, Write, or web_* — they are not part of
  the plan dispatcher.
"""


def _nx_answer_match_is_hit(
    confidence: float | None,
    threshold: float = _PLAN_MATCH_MIN_CONFIDENCE,
) -> bool:
    """Return True when a plan_match confidence qualifies as a hit.

    ``confidence is None`` (FTS5 sentinel, RF-11) is always a hit.
    Numeric confidence must be >= *threshold*. RDR-092 Phase 2 Option A
    makes the threshold caller-overridable: the default tracks the
    RDR-079 P5 calibration (0.40), and verb skills that have validated
    a stricter floor (0.50 per R9) can pin it per-call.
    """
    if confidence is None:
        return True
    return confidence >= threshold


def _nx_answer_normalize_scope(scope: str) -> tuple[str, str | None]:
    """Normalize the ``nx_answer`` *scope* argument; return
    ``(normalized_scope, warning_or_None)``.

    RDR-137 followup (nexus-n1908): ``scope`` is a SINGLE corpus prefix
    (``"knowledge"``) or catalog subtree (``"1.2"``). A comma-list like
    ``"rdr,code,docs"`` is malformed — it filters retrieval to a
    literal collection named ``"rdr,code,docs"`` (which matches
    nothing), and the empty retrieval then lets the operator
    subprocess synthesize a confident off-topic answer from its
    ambient SessionStart hook context. Treating an unparseable
    comma-scope as "no scope" (broad search) turns a silent wrong
    answer into a correct broad answer plus an operator-visible
    warning. Whitespace-only scope normalizes to ``""`` as well.
    """
    s = (scope or "").strip()
    if "," in s:
        return "", (
            f"scope {scope!r} is a comma-list; scope expects a single "
            f"corpus prefix or catalog subtree. Treating as unscoped "
            f"(broad search). Pass one scope token to filter."
        )
    return s, None


def _nx_answer_is_empty_retrieval(steps: "list") -> bool:
    """Return True when a plan's retrieval steps collectively returned
    zero evidence.

    RDR-137 followup (nexus-n1908): when a plan HAS retrieval steps
    (steps carrying an ``ids`` or ``tumblers`` list) but every one of
    them came back empty, the final synthesis step (a ``generate`` /
    ``summarize`` ``claude -p`` subprocess) is at risk of latching onto
    its ambient SessionStart hook context and confidently answering an
    unrelated "describe my environment" instead of signalling the
    miss. Detecting zero-evidence lets ``nx_answer`` return an explicit
    no-match message instead of the misleading synthesis.

    Conservative by construction (never over-fires on the canonical
    query path): only considers a plan "retrieval-bearing" when at
    least one step exposes an ``ids`` or ``tumblers`` key. A pure
    ``generate`` plan (no retrieval) is exempt — it legitimately
    synthesizes without evidence.
    """
    had_retrieval = False
    total_evidence = 0
    for step_out in steps:
        if not isinstance(step_out, dict):
            continue
        ids = step_out.get("ids")
        tumblers = step_out.get("tumblers")
        if isinstance(ids, list):
            had_retrieval = True
            total_evidence += len(ids)
        if isinstance(tumblers, list):
            had_retrieval = True
            total_evidence += len(tumblers)
    return had_retrieval and total_evidence == 0


#: Common English stop-words stripped when synthesizing a grown plan's
#: ``name`` from the question. Kept narrow on purpose; aggressive
#: filtering drops the content words R10 needs for match-text signal.
_GROWN_PLAN_NAME_STOP_WORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "for", "in", "on", "at", "by", "with", "from", "about",
    "and", "or", "but", "so", "as",
    "how", "what", "why", "when", "where", "who", "which",
    "do", "does", "did", "can", "could", "should", "would", "will",
    "this", "that", "these", "those",
    "i", "we", "you", "they", "it", "he", "she",
})


def _infer_grown_plan_verb(
    *,
    caller_dimensions: dict[str, Any] | None,
    plan_json: str,
) -> str:
    """Three-tier verb cascade for a grown plan. RDR-092 Phase 0b.

    Tier 1: caller-supplied ``dimensions["verb"]``.
    Tier 2: operator-shape inference from ``plan_json.steps``:
        compare step → analyze; extract+rank → analyze;
        traverse+search+summarize → research.
    Tier 3: ``"research"`` fallback.
    """
    if caller_dimensions:
        pinned = caller_dimensions.get("verb")
        if isinstance(pinned, str) and pinned.strip():
            return pinned.strip().lower()
    try:
        plan = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
    except (json.JSONDecodeError, TypeError):
        return "research"
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list):
        return "research"
    tools = {
        step.get("tool", "").strip().lower()
        for step in steps if isinstance(step, dict)
    }
    tools.discard("")
    if "compare" in tools:
        return "analyze"
    if {"extract", "rank"}.issubset(tools):
        return "analyze"
    if {"traverse", "search", "summarize"}.issubset(tools):
        return "research"
    return "research"


def _infer_grown_plan_name(
    question: str, *, max_words: int = 5,
) -> str:
    """Kebab-case name from first 3-5 content words of *question*.

    RDR-092 Phase 0b. Drops a narrow set of common English stop-words
    (see :data:`_GROWN_PLAN_NAME_STOP_WORDS`), lowercases the rest, and
    joins up to *max_words* tokens with ``-``. Empty / whitespace-only
    input returns ``"grown-plan"`` so a grown row always has an
    identifier.
    """
    import re  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site

    tokens = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_]*", question.lower())
    content = [t for t in tokens if t not in _GROWN_PLAN_NAME_STOP_WORDS]
    take = content[:max_words] if content else tokens[:max_words]
    return "-".join(take) or "grown-plan"


#: nexus-93cc6 D2: bounds on the generalizer's output. Shorter than a
#: sentence is degenerate; longer than this is prompt-injection-shaped or
#: runaway prose — either way the fail-soft contract says drop it and keep
#: today's verbatim-question match text.
_GROWN_MATCH_DESCRIPTION_MIN_CHARS: int = 40
#: v2 (pipeline measurement, 2026-08-31): v1's 1200 admitted 700-1000-char
#: abstract essays that scored BELOW the verbatim baseline at the 0.55
#: floor (14/24 vs 15/24) — verbose prose dilutes cosine. The winning
#: hand-written shape is ~300 chars, mostly example questions; 600 is the
#: hard quality gate, the prompt asks for 400.
_GROWN_MATCH_DESCRIPTION_MAX_CHARS: int = 600

_GROWN_MATCH_DESCRIPTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"description": {"type": "string"}},
    "required": ["description"],
}


async def _generalize_grown_match_description(
    *, question: str, plan_json: str,
) -> "str | None":
    """Generalized match-text description for a grown plan (nexus-93cc6 D2).

    One tool-free, cheap-tier ``claude_dispatch`` producing the shape the
    93cc6 spike validated (T2 [23880]): a 2-3 sentence statement of what
    the plan ANSWERS, followed by 4-6 short paraphrase exemplars — the
    builtin-template match-text shape, which measured +17pp paraphrase
    recall at the grown floor with zero precision cost against the
    verbatim-question baseline.

    FAIL-SOFT BY CONTRACT: every failure path — dispatch error, timeout,
    empty or out-of-bounds output — returns ``None``, which downstream
    means "synthesize match_text from the verbatim question", exactly
    today's behavior. The generalizer can only ever ADD recall; it must
    never block or delay-fail a grow (the grow itself is already
    best-effort), and it never touches the ``query`` column (the
    verbatim-repeat bypass keys on it).
    """
    try:
        from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — deferred: heavy import, grow path only

        steps_hint = plan_json[:1500]
        # Prompt v2 (measured, 2026-08-31): v1 asked for "2-3 sentences ...
        # then examples" and haiku wrote abstract essays that scored BELOW
        # the verbatim baseline; the compact exemplar-dense register is
        # what the spike validated. The examples ARE the match surface.
        prompt = (
            "Write a match-text description for a saved retrieval plan.\n\n"
            "The plan was grown from this question:\n"
            f"  {question}\n\n"
            "Its retrieval steps (JSON, truncated):\n"
            f"  {steps_hint}\n\n"
            "Produce ONE plain-text paragraph of AT MOST 400 characters: "
            "one short sentence stating what the plan answers, then 5-7 "
            "short paraphrased example questions users might ask, separated "
            "by question marks — the example questions must make up most of "
            "the text. Use concrete domain words from the question and "
            "steps; never abstract phrasing like 'best practices' or "
            "'guidance'. No markdown, no preamble, and do not repeat the "
            "original question verbatim."
        )
        result = await claude_dispatch(
            prompt,
            _GROWN_MATCH_DESCRIPTION_SCHEMA,
            # 45s, measured (2026-09-01): 20s killed all six live probe
            # dispatches mid-generation (claude -p subprocess startup +
            # generation wall, not bare API latency); 60s succeeded 6/6 and
            # 45s itself measured 5/6 with the sixth degrading fail-soft —
            # the accepted trade for a tighter wall bound.
            # This timeout IS the wall-time bound the reviewers asked to
            # see accounted: it extends a plan-miss SUCCESS run (already
            # 80-100s) by the dispatch wall, once per NOVEL question, and
            # only when a plan is actually being grown. Fire-and-forget
            # was considered and rejected (the no-fire-and-forget
            # directive; a detached task dies silently with the process).
            timeout=45.0,
            # Cheap alias on purpose: this is a bounded reformulation task,
            # the same tier the check/verify operators default to
            # (nexus-3mea3 precedent).
            model="haiku",
            operator="plan_grow_generalize",
        )
        description = str(result.get("description") or "").strip()
        if not (
            _GROWN_MATCH_DESCRIPTION_MIN_CHARS
            <= len(description)
            <= _GROWN_MATCH_DESCRIPTION_MAX_CHARS
        ):
            _log.info(
                "plan_grow_generalize_out_of_bounds",
                length=len(description),
            )
            return None
        _log.info(
            "plan_grow_generalize_ok",
            length=len(description),
        )
        return description
    except Exception:  # noqa: BLE001 — fail-soft by contract; the grow proceeds with the verbatim question
        _log.info("plan_grow_generalize_failed", exc_info=True)
        return None


def _nx_answer_classify_plan(match: Any) -> str:
    """Classify a matched plan: ``"single_query"`` | ``"needs_operators"``.

    nexus-nyry9.5 (RDR-196 .r5 review-fix, critic CRITICAL, T2
    review-nexus-nyry9.5): the prior three-way split additionally
    returned a third, retrieval-only literal for a multi-step plan
    with no operator tool call, and that bucket was exempt from
    ``nx_answer``'s ``budget_seconds`` deadline. A census of all 17
    real shipped builtin plans (``conexus/plans/builtin/*.yml``) found
    ZERO that classify that way — 2 are ``single_query``, 15 end in an
    operator — and the bucket's only "proof" of liveness was a
    hand-built ``plan_json`` fed directly to this function, never
    routed through a real ``plan_match()``. Per this bead's own
    no-live-test-from-a-real-match-means-dead rule, the bucket is
    deleted: everything that is not the single-step ``query()`` fast
    path now classifies as ``needs_operators`` and is budget-bound like
    any other ``plan_run`` execution, regardless of whether it actually
    contains an operator tool call. The name is a mild misnomer for a
    would-be-retrieval-only plan under this binary split — it now means
    "goes through Step 4 plan_run", not literally "contains an operator
    step" — kept as the surviving literal because callers/tests already
    key on it.

    nexus-h33x8.6 a4 fold-in (history, no longer load-bearing for
    classification since the two non-single_query cases collapsed):
    this used to distinguish plans by
    :func:`nexus.plans.bundle.is_operator_tool` — the authoritative
    operator-detection set ``plan_run``/``segment_steps`` actually
    dispatch against, broader than ``_OPERATOR_TOOL_MAP``'s bare names.
    """
    try:
        plan = json.loads(match.plan_json)
    except (json.JSONDecodeError, TypeError):
        return "needs_operators"
    steps = plan.get("steps") or []
    if len(steps) == 1 and steps[0].get("tool") == "query":
        return "single_query"
    return "needs_operators"


def _nx_answer_is_single_query(match: Any) -> bool:
    return _nx_answer_classify_plan(match) == "single_query"


def _nx_answer_needs_operators(match: Any) -> bool:
    """Does this plan contain a genuine operator (``claude -p``) step?

    nexus-nyry9.5 (RDR-196 .r5 review-fix): DECOUPLED from
    ``_nx_answer_classify_plan`` — that function's return value now
    drives ONLY the single_query-fast-path-vs-everything-else split
    (its own docstring explains why classification stopped being
    operator-aware: the deadline exemption the distinction used to
    carry was deleted, and with it the reason to make the classifier
    itself operator-aware). This predicate is not consulted anywhere in
    ``nx_answer``'s own control flow — it exists purely so callers
    (tests) can ask "does this plan actually dispatch claude -p",
    independent of the fast-path/budget question, the same way it
    always could before the collapse.
    """
    from nexus.plans.bundle import is_operator_tool  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
    try:
        plan = json.loads(match.plan_json)
    except (json.JSONDecodeError, TypeError):
        return False
    steps = plan.get("steps") or []
    return any(is_operator_tool(step.get("tool", "")) for step in steps)


def _maybe_unwrap_output_envelope(text: str, *, max_depth: int = 3) -> str:
    """GH #555: unwrap nested ``{"output": "..."}`` envelopes.

    The ``operator_generate`` schema emits ``{"output": <prose>}``;
    when an ``extract -> generate`` bundle's terminal step receives a
    prior step's envelope as its ``context`` argument, claude -p has
    been observed emitting a recursive ``{"output": "{\\"output\\":
    \\"...\\"}"}`` shape (the model treats the envelope as raw text
    and re-wraps). This helper repeatedly tries to JSON-parse *text*;
    if the parse yields a single-key ``{"output": <str>}`` dict, it
    pulls the inner string and tries again. Bounded by *max_depth*
    so a malformed payload cannot infinite-loop.

    Returns *text* unchanged when:
      - it does not parse as JSON, OR
      - the parse yields anything other than a single-key dict
        whose only key is ``"output"`` and whose value is a string.

    Cheap (single ``json.loads`` per depth) and safe (the strict
    shape check guarantees no false unwraps on legitimate JSON).
    """
    current = text
    for _ in range(max(0, max_depth)):
        if not current or not current.lstrip().startswith("{"):
            break
        try:
            parsed = json.loads(current)
        except (json.JSONDecodeError, ValueError):
            break
        if (
            isinstance(parsed, dict)
            and len(parsed) == 1
            and "output" in parsed
            and isinstance(parsed["output"], str)
        ):
            current = parsed["output"]
            continue
        break
    return current


def _step_record_to_wire(step: Any) -> dict[str, Any]:
    """One :class:`~nexus.plans.runner.StepRecord` -> the wire shape
    ``POST /v1/telemetry/nx_answer_runs/record``'s optional ``steps[]``
    element expects (RDR-196 .p1c/.p1d; field names mirror
    ``TelemetryHandler.parseNxAnswerSteps`` verbatim). ``run_id`` is
    deliberately dropped — the engine assigns it server-side from the
    parent row inserted in the SAME transaction (``StepRecord``'s own
    docstring, runner.py).
    """
    return {
        "step_index":    step.step_index,
        "operator":      step.operator,
        "source":        step.source,
        "model":         step.model,
        "input_tokens":  step.input_tokens,
        "output_tokens": step.output_tokens,
        "cost_usd":      step.cost_usd,
        "elapsed_ms":    step.elapsed_ms,
        "ok":            step.ok,
        "bundled_steps": list(step.bundled_steps),
    }


def _nx_answer_record_run(
    telemetry: Any,
    *,
    question: str,
    plan_id: int | None,
    matched_confidence: float | None,
    step_count: int,
    final_text: str,
    step_records: list[Any] | None,
    duration_ms: int,
    trace: bool,
) -> None:
    """Persist one ``nx_answer_runs`` row (+ its ``step_records``, when any)
    via the telemetry store. Redacts when ``trace=False``.

    nexus-pyzk7: routes through ``telemetry.record_nx_answer_run`` (the
    service's POST /v1/telemetry/nx_answer_runs/record), not a raw
    ``.conn`` the service lacks.

    ``cost_usd`` (RDR-196 .p1d, nexus-nyry9.10 — replaces 9 former
    hardcoded-zero-cost literals, one per call site): the SUM of every step's
    ``cost_usd`` that is not ``None``, or ``None`` when no step reports a
    known cost — an empty *step_records* trivially falls into the latter
    (sum of nothing is "unknown", never a fabricated zero). This is
    deliberately a BLANKET rule, not case-by-case: a call site with
    genuinely zero steps (e.g. the single-step fast path, which bypasses
    ``plan_run`` and therefore never produces a ``StepRecord`` at all,
    even though it IS a real, free T3-only call) still gets ``None`` here
    rather than an invented ``0.0`` — see this bead's dev-notes writeback
    for the documented trade-off (a real but unmeasured cost reads
    identically to "we don't know", which is honest, if not maximally
    informative; a follow-up teaching the fast path to emit its own
    ``sql``-source StepRecord is future work, not fabricated here).
    """
    q = question if trace else "[redacted]"
    text = final_text if trace else "[redacted]"
    steps = step_records or []
    _known_costs = [s.cost_usd for s in steps if s.cost_usd is not None]
    cost_usd = sum(_known_costs) if _known_costs else None
    wire_steps = [_step_record_to_wire(s) for s in steps] or None
    try:
        telemetry.record_nx_answer_run(
            question=q, plan_id=plan_id, matched_confidence=matched_confidence,
            step_count=step_count, final_text=text, cost_usd=cost_usd,
            duration_ms=duration_ms, steps=wire_steps,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort telemetry, must not crash caller (warned once via _warn_telemetry_drop)
        # Best-effort, but warn once so a service-mode drop is visible (the call
        # sites also swallow; this makes the failure mode observable). nexus-pyzk7.
        _warn_telemetry_drop("nx_answer_runs", exc)


#: Literal no-result texts the single-step reroute path can synthesize. Kept as
#: an explicit set rather than a substring/emptiness heuristic: an answer that
#: merely MENTIONS "no results" is a real answer, and a heuristic that demoted
#: it would swap one wrong signal for another.
_EMPTY_ANSWER_TEXTS: frozenset[str] = frozenset({
    "no results.",
    "no results",
    "",
})


class PlanBindingUnsatisfiableError(ValueError):
    """A matched plan requires a typed binding nothing can supply.

    Raised instead of guessing. The alternative — binding ``""`` so the
    filter coerces to no-filter — restores results but lets a plan that
    MEANT to be type-scoped silently widen to the whole corpus, which is
    a data-correctness fallback of exactly the kind the project forbids.
    """

    def __init__(self, *, binding: str, plan_id: int, plan_name: str) -> None:
        self.binding = binding
        self.plan_id = plan_id
        self.plan_name = plan_name
        # nexus-0yrjr review: ONE definition of the taxonomy, in
        # nexus.plans.schema. It was briefly duplicated here — the same
        # two-hand-typed-constants drift the project already paid for
        # once (REQUIRED_ENGINE_VERSION / PINNED_SERVICE_TAG,
        # nexus-b6qlf). The matcher's offer-time gate and this run-time
        # refusal MUST agree, so they read the same object. Imported at
        # call time to match this module's deferred-import convention.
        from nexus.plans.schema import TYPED_BINDING_DOMAINS  # noqa: PLC0415 — deferred; matches this module's convention

        domain = TYPED_BINDING_DOMAINS.get(binding)
        hint = f" Its domain is {domain}." if domain else ""
        super().__init__(
            f"plan {plan_id} ({plan_name}) requires the binding "
            f"'{binding}', which has an enumerated or numeric domain and "
            f"was not supplied by the caller or by a plan default.{hint} "
            f"nx_answer will not infer it from the question text — doing "
            f"so filters the retrieval to nothing. Either call plan_run "
            f"directly with an explicit '{binding}', or treat this as a "
            f"mis-match: a plan requiring '{binding}' should not have been "
            f"selected for a question that does not specify one."
        )


def _nx_answer_caller_bindings(
    *,
    question: str,
    scope: str,
    bindings: dict[str, Any] | None,
) -> tuple[dict[str, Any], frozenset[str]]:
    """Build ``nx_answer``'s run bindings and declare what it can supply.

    Returns ``(run_bindings, available)``. ``available`` is handed to
    ``plan_match`` so it never offers a plan this call cannot run
    (nexus-0yrjr); ``run_bindings`` is what the runner receives. The two
    are derived together so they cannot disagree — a binding declared
    available but not bound would let the matcher offer a plan that then
    fails at dispatch.

    ``intent`` and ``_nx_scope`` are owned by ``nx_answer`` and applied
    last, so a caller cannot shadow the question or redirect the scope
    through *bindings*.

    ``None``-valued entries are dropped rather than declared: a null
    slips through JSON tool arguments easily, and treating it as supplied
    would reopen the offer-a-plan-that-cannot-run hole from the other
    side.
    """
    # Typed bindings the question itself supplies, derived FIRST so an
    # explicit caller value always overrides one that was inferred
    # (nexus-7y4v0 follow-through). Without this, a plan requiring a typed
    # value is permanently unofferable to nx_answer, which binds only
    # `intent` — and the two templates in that state, find-by-author and
    # type-scoped-search, were written for question shapes that CARRY the
    # value they need. A template unreachable for the question it exists
    # to serve is an unreachable feature, not a safe default.
    #
    # Derivation is deliberately stubborn: an ambiguous question derives
    # nothing and the plan stays unofferable, which is today's behaviour.
    # A WRONG typed filter is the harmful outcome — schema.py records
    # plan 14 returning zero results on a bad content_type while the same
    # query without it returned the right paper first.
    from nexus.plans.binding_infer import infer_typed_bindings  # noqa: PLC0415 — deferred; matches this module's convention

    run: dict[str, Any] = dict(infer_typed_bindings(question))
    run.update({
        k: v for k, v in (bindings or {}).items() if v is not None
    })
    run["intent"] = question
    if scope:
        run["_nx_scope"] = scope
    return run, frozenset(run)


def _autoalias_bindings(
    *,
    required: list[str],
    run_bindings: dict[str, Any],
    defaults: dict[str, Any],
    question: str,
    plan_id: int,
    plan_name: str,
    plan_json: str | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fill unsupplied required bindings from *question*; refuse typed ones.

    The alias exists because library plans declaring
    ``required_bindings: [concept]`` (or ``area``, ``topic``, ...) failed
    at dispatch with ``missing required bindings`` even though ``$intent``
    already carried the equivalent value. It mirrors the inline-planner
    fallback, whose constructed plans get every binding filled from the
    question text. That remains correct for free text.

    A TYPED binding cannot be derived from a question, so an unsatisfied
    one raises
    :class:`PlanBindingUnsatisfiableError` rather than being guessed. The
    raise happens before any binding is handed to the runner, so a plan
    never executes half-bound.

    Caller-supplied values and plan defaults are never overwritten, and
    either one satisfies a typed binding.

    "Typed" must mean the same thing here as it does in the matcher's own
    gate, or a plan the matcher would refuse to offer gets its structural
    filter stuffed with prose the moment something else offers it. Since
    nexus-7y4v0 that means BOTH the name test and the usage test: a
    binding a plan feeds into a typed argument slot is typed whatever it
    is called (``debug-default`` declares ``failing_path`` and passes it
    as ``subtree: $failing_path``). *plan_json* is optional only so a
    caller without the plan text still gets the name test; omitting it
    re-opens exactly the hole nexus-7y4v0 closed.
    """
    from nexus.plans.schema import TYPED_FILTER_BINDINGS, typed_by_usage  # noqa: PLC0415 — deferred; matches this module's convention

    typed = TYPED_FILTER_BINDINGS | typed_by_usage(plan_json)
    out = dict(run_bindings)
    for req in required:
        if req in out or req in defaults:
            continue
        if req in typed:
            raise PlanBindingUnsatisfiableError(
                binding=req, plan_id=plan_id, plan_name=plan_name,
            )
        out[req] = question
    return out


def _nx_answer_text_is_empty(text: str) -> bool:
    """True when *text* is one of the synthesized no-result placeholders.

    nexus-yg49g. Deliberately EXACT-match, not "startswith" or "contains":
    over-matching would record a genuine answer discussing empty results as a
    failure, which is the same class of wrong signal in the other direction.
    """
    return text.strip().lower() in _EMPTY_ANSWER_TEXTS


def _nx_answer_record_outcome(plan_id: int, *, success: bool) -> None:
    """Bump ``success_count`` or ``failure_count`` for a library-matched plan.

    No-op for ``plan_id == 0`` (synthetic inline-planner Match). Swallows
    library errors — telemetry must never break the user-facing path.
    """
    if not plan_id:
        return
    try:
        with _t2_ctx() as db:
            db.plans.increment_run_outcome(plan_id, success=success)
    except Exception:  # noqa: BLE001 — boundary catch; failure surfaced via log.warning, must not crash caller
        import structlog as _slog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
        _slog.get_logger().warning(
            "nx_answer_plan_outcome_increment_failed",
            plan_id=plan_id, success=success, exc_info=True,
        )


#: Max historical plans injected as few-shot examples into the inline
#: planner on a miss (nexus-mhyf3 / CacheRAG R1). Three balances prompt
#: cost against the demonstrated lift.
_PLANNER_FEW_SHOT_MAX = 3
#: Per-example plan-JSON character cap so a pathological stored plan can't
#: blow the planner prompt budget.
_PLANNER_FEW_SHOT_PLAN_CHARS = 1200
#: Soft similarity floor for using a near-miss as an exemplar. Set BELOW the
#: 0.40 hit gate (every match reaching the miss path is below the caller's
#: effective threshold) but high enough to exclude near-random plans: a
#: dissimilar exemplar teaches the planner the wrong structure and is worse
#: than zero-shot (review finding). FTS5 fallback matches (confidence=None)
#: are keyword hits, not semantic similarity, and are excluded outright.
_PLANNER_FEW_SHOT_MIN_CONFIDENCE = 0.25
#: Cap on the rendered example question (the stored plan ``description`` /
#: ``query`` column). Collapsed to a single line + truncated so an
#: agent-authored description cannot inject prompt instructions or crowd the
#: budget (review finding).
_PLANNER_FEW_SHOT_DESC_CHARS = 200


def _format_plan_few_shot(matches: "list | None") -> str:
    """Render the top similar historical plans as few-shot examples.

    nexus-mhyf3 (CacheRAG R1): on a plan MISS the near-miss ``matches``
    are still the nearest stored plans by similarity. Feeding the
    sufficiently-similar ones to the inline planner as
    ``Question -> {"steps": [...]}`` examples is the single highest-leverage
    cache mechanism in CacheRAG's ablation.

    Only matches with a cosine confidence >= ``_PLANNER_FEW_SHOT_MIN_CONFIDENCE``
    are used; FTS5-fallback matches (``confidence is None``) are excluded
    (keyword overlap is not structural similarity). Returns ``""`` when no
    usable example exists so the planner falls back to the prior zero-shot
    prompt unchanged. Only the ``steps`` of each example are shown — the
    exact shape the planner must emit. The example question is collapsed to
    one line and truncated to neutralise prompt-injection via a stored
    description.
    """
    import json as _json  # noqa: PLC0415 — branch-local stdlib import; deferred to miss path

    if not matches:
        return ""
    examples: list[str] = []
    for m in matches:
        if len(examples) >= _PLANNER_FEW_SHOT_MAX:
            break
        conf = getattr(m, "confidence", None)
        if conf is None or conf < _PLANNER_FEW_SHOT_MIN_CONFIDENCE:
            continue
        description = (getattr(m, "description", "") or "").strip()
        # Collapse ALL whitespace (incl. newlines) to single spaces, then
        # truncate — neutralises injection + bounds the prompt cost.
        description = " ".join(description.split())[:_PLANNER_FEW_SHOT_DESC_CHARS]
        raw = getattr(m, "plan_json", "") or ""
        try:
            plan = _json.loads(raw)
        except (ValueError, TypeError):
            continue
        steps = plan.get("steps") if isinstance(plan, dict) else None
        if not description or not isinstance(steps, list) or not steps:
            continue
        rendered = _json.dumps({"steps": steps})
        if len(rendered) > _PLANNER_FEW_SHOT_PLAN_CHARS:
            continue
        examples.append(f"Question: {description}\nPlan: {rendered}")
    if not examples:
        return ""
    body = "\n\n".join(examples)
    return (
        "Here are similar questions that were previously answered with these "
        "plans. Use them as a guide for tool choice and step structure; adapt "
        "to THIS question rather than copying verbatim:\n\n"
        f"{body}\n\n"
    )


async def _nx_answer_plan_miss(
    question: str,
    *,
    scope: str = "",
    max_steps: int = 6,
    few_shot_matches: "list | None" = None,
) -> Any:
    """Decompose *question* into a plan via claude_dispatch, execute it,
    and return a synthetic Match for plan_run.
    """
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — rare/branch-local path; operator dispatch deferred to call time
    from nexus.plans.match import Match  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
    from nexus.mcp_infra import get_collection_names  # noqa: PLC0415 — circular-dep avoidance (mcp package import deferred)

    corpus_hint = f" Focus on the '{scope}' corpus." if scope else ""

    # Give the planner the actual collection names it can search against.
    # Without this, the LLM writes `corpus="knowledge,code,docs"` — generic
    # tokens that may not match any collection in the caller's sandbox.
    try:
        available = get_collection_names()
    except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
        available = []
    corpus_names_hint = ""
    if available:
        corpus_names_hint = (
            f"\n\nAvailable collection names in this environment: "
            f"{', '.join(sorted(available)[:20])}"
            + (f" (and {len(available) - 20} more)" if len(available) > 20 else "")
            + ".  Pass collection names to `search` via `corpus=<name>` — "
            "bare prefixes like 'knowledge' or 'code' will miss if no "
            "collection actually starts with that prefix."
        )

    # nexus-mhyf3 (CacheRAG R1): inject the nearest stored plans as few-shot
    # examples. On a miss these near-miss matches are still the most similar
    # historical plans; demonstrating their structure measurably lifts plan
    # quality over the prior zero-shot prompt.
    few_shot_block = _format_plan_few_shot(few_shot_matches)
    if few_shot_block:
        import structlog as _slog2  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
        # One "\nPlan: " per example: each example is rendered as
        # "Question: <one-line desc>\nPlan: <json>" and the description is
        # whitespace-collapsed (no embedded "\nPlan: "), so this counts
        # examples exactly.
        _slog2.get_logger().info(
            "nx_answer_planner_few_shot",
            examples=few_shot_block.count("\nPlan: "),
        )

    prompt = (
        f"Decompose this question into a retrieval-and-analysis plan "
        f"with at most {max_steps} steps:{corpus_hint}\n\n"
        f"{few_shot_block}"
        f"Question: {question}\n"
        f"{corpus_names_hint}\n\n"
        f"{_PLANNER_TOOL_REFERENCE}\n"
        f"Return the plan as {{\"steps\": [...]}} where each step is "
        f"{{\"tool\": \"<bare name>\", \"args\": {{...}}}}."
    )

    # Inline planner timeout: 300s — decomposing a question into a
    # plan is heavier than a single operator call (multi-step reasoning,
    # tool-choice enumeration). 120s was hitting the timeout on
    # non-trivial questions. Callers of nx_answer see the miss path
    # as a hang when this trips.
    #
    # nexus-wr5o: claude -p occasionally returns malformed JSON despite
    # --output-format json --json-schema (transient model output drift,
    # partial stream, null structured_output on first attempt). One retry
    # on OperatorOutputError catches the transient case; OperatorError
    # (subprocess non-zero) and OperatorTimeoutError do NOT retry — those
    # are not transient. Halved timeout on retry so a single hang doesn't
    # double total wall time.
    from nexus.operators.dispatch import OperatorOutputError as _OpOutputError  # noqa: PLC0415 — rare/branch-local path; operator dispatch deferred to call time
    # RDR-196 .p2c (nexus-nyry9.16): opt-in ``model=`` override for the
    # inline planner's own dispatch, gated on the SAME
    # ``NX_OPERATOR_MODEL_TIERING`` env var that gates
    # ``plans/runner.py``'s per-operator tiering — the two are exercised
    # together by the .p2c A/B (baseline: env unset, no override, argv
    # unchanged; candidate: env set, planner dispatches at the "cheap"
    # tier alias). Unset (default) is a complete no-op — ``model=None``
    # appends no ``--model`` flag, byte-identical to every pre-.p2c call.
    #
    # RDR-196 .p2d (nexus-nyry9.17): the planner is UNMEASURED, not HOLD-
    # by-decision — the .p2c candidate arm never ran. It does NOT join the
    # cheap flip. UPDATED by nexus-ek8tr (2026-08-21, superseding the
    # "default path leaves _planner_model at None" wording that used to
    # sit here): the default path now pins STRONG_DEFAULT_ALIAS
    # explicitly (see the branch below); only the ``== "0"`` kill switch
    # leaves the dispatch bare. The TIER question stays open
    # (nexus-i8to5) — the pin names the incumbent, it does not choose a
    # tier.
    # nexus-ek8tr (2026-08-21): the default path now pins the planner to
    # STRONG_DEFAULT_ALIAS explicitly instead of inheriting the box CLI
    # default bare — same dispatch, same model in practice, no longer an
    # accident. Kill switch "0" restores the bare pre-tiering dispatch.
    # The planner's TIER remains UNMEASURED (Sam decision, nexus-i8to5);
    # this pins the incumbent, it does not change it.
    _planner_model: str | None = None
    if _os.environ.get("NX_OPERATOR_MODEL_TIERING") == "1":
        from nexus.operators.model_tiers import resolve_model_for_tier  # noqa: PLC0415 — rare/branch-local path; measurement-only opt-in

        _planner_model = resolve_model_for_tier("cheap")
    elif _os.environ.get("NX_OPERATOR_MODEL_TIERING") != "0":
        from nexus.operators.model_tiers import STRONG_DEFAULT_ALIAS  # noqa: PLC0415 — rare/branch-local path; default-path pin (nexus-ek8tr)

        _planner_model = STRONG_DEFAULT_ALIAS
    payload = None
    last_output_error: _OpOutputError | None = None
    for attempt in range(2):
        attempt_timeout = 300.0 if attempt == 0 else 150.0
        try:
            payload = await claude_dispatch(
                prompt, _PLANNER_SCHEMA, timeout=attempt_timeout,
                model=_planner_model, operator="planner",
            )
            break
        except _OpOutputError as exc:
            last_output_error = exc
            import structlog as _slog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
            _slog.get_logger().warning(
                "nx_answer_planner_output_error",
                attempt=attempt + 1,
                error=str(exc)[:300],
            )
    if payload is None:
        # Both attempts hit OperatorOutputError. Re-raise the second one
        # (most recent diagnostic) so the caller's error string carries
        # the most-actionable snippet.
        assert last_output_error is not None
        raise last_output_error
    steps = payload.get("steps", []) if isinstance(payload, dict) else []
    if not steps:
        raise ValueError("planner returned empty plan")

    _ALLOWED_TOOLS = {
        "search", "query", "traverse", "store_get_many",
        "extract", "rank", "compare", "summarize", "generate",
    }
    # The LLM planner emits either the bare operator name ("extract") or
    # the resolved MCP tool name ("operator_extract" / full prefix form).
    # Normalize all three to the bare form the dispatcher's _OPERATOR_TOOL_MAP
    # expects as a key.
    _TOOL_ALIASES = {
        "grep": "search", "read": "search", "bash": "search",
        "find": "search", "glob": "search",
        "web_search": "search", "web_fetch": "search",
        # operator_* → bare op name (the runner dispatcher remaps back)
        "operator_extract": "extract",
        "operator_rank": "rank",
        "operator_compare": "compare",
        "operator_summarize": "summarize",
        "operator_generate": "generate",
    }
    # Catalog tools the LLM might reach for — map to the closest allowed
    # tool (traverse covers link walks; search covers catalog_search use
    # cases).  Prevents silent `planner_step_dropped` when the planner
    # hasn't fully internalised the "no catalog_* in plans" rule.
    _CATALOG_TOOL_REDIRECTS = {
        "link_query": "traverse",
        "links": "traverse",
        "catalog_search": "search",
        "catalog_show": "query",
        "catalog_list": "query",
        "catalog_resolve": "traverse",
        "catalog_stats": None,  # nothing plan-step-worthy to redirect to
    }
    _TOOL_ALIASES.update({
        k: v for k, v in _CATALOG_TOOL_REDIRECTS.items() if v is not None
    })
    import structlog as _slog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
    _plog = _slog.get_logger()
    normalized = []
    dropped: list[str] = []
    for step in steps:
        raw_tool = step.get("tool", "")
        bare = raw_tool.rsplit("__", 1)[-1] if raw_tool.startswith("mcp__") else raw_tool
        bare = _TOOL_ALIASES.get(bare.lower(), bare)
        if bare not in _ALLOWED_TOOLS:
            _plog.warning("planner_step_dropped", raw_tool=raw_tool, bare=bare)
            dropped.append(raw_tool or bare or "?")
            continue
        step["tool"] = bare
        normalized.append(step)

    if not normalized:
        # Search review I-5: surface the dropped tools in the error so the
        # caller's "planner failed" message can explain why (e.g. the LLM
        # picked Bash / grep / WebFetch which aren't dispatchable).
        detail = ", ".join(sorted(set(dropped))) if dropped else "(no tools at all)"
        raise ValueError(
            f"planner returned only non-dispatchable tools: {detail}"
        )

    plan_json = json.dumps({"steps": normalized})
    return Match(
        plan_id=0,
        name="ad-hoc",
        description=question,
        confidence=None,
        dimensions={},
        tags="ad-hoc",
        plan_json=plan_json,
        required_bindings=["intent"],
        optional_bindings=[],
        default_bindings={"intent": question},
        parent_dims=None,
    )


# ── RDR-084 helpers ───────────────────────────────────────────────────────────


def _load_ad_hoc_ttl() -> int:
    """Return the TTL (days) applied to auto-saved ad-hoc plans.

    Reads ``.nexus.yml#plans.ad_hoc_ttl`` via :func:`nexus.config.load_config`
    with a 30-day fallback. A config load failure also falls back to 30
    — the growth feature is best-effort and must never block ``nx_answer``.
    """
    try:
        config = load_config()
    except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
        return 30
    plans_section = config.get("plans") if isinstance(config, dict) else None
    if not isinstance(plans_section, dict):
        return 30
    value = plans_section.get("ad_hoc_ttl", 30)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 30


# ── RDR-080 orchestration tools ───────────────────────────────────────────────

#: nexus-h33x8.6 a4: default wall-clock budget for nx_answer's PLAN-
#: EXECUTION phase (Step 4, the ``plan_run`` call). ``None`` = no budget
#: enforced — the pre-a4 behavior, relying only on each operator's own
#: (typically 300s) internal timeout. Kept as a named constant per the
#: "do not change default behavior silently" constraint: every existing
#: caller that doesn't pass ``budget_seconds`` gets exactly this value.
_NX_ANSWER_DEFAULT_BUDGET_SECONDS: float | None = None

#: nexus-nyry9.2 (RDR-196 .r2): sentinel for ``budget_exhausted_at_step``
#: when the budget is exhausted BEFORE any plan step executes -- i.e.
#: during Step 1 (the plan-match gate and, on a miss, the inline
#: LLM planner's own claude -p round trip). Prior to this fix that
#: phase ran wholly outside ``budget_seconds`` (deadline was computed
#: at call entry but never consulted until ``plan_run``), so a slow
#: planner could burn the caller's entire budget and nx_answer would
#: still go on to execute the now-late plan. Deliberately 0 rather
#: than a re-purposed ``-1`` or ``plan_run``'s own 1-indexed step
#: numbering (:mod:`nexus.plans.runner`, always >= 1): 0 can never
#: collide with a real step index, so a marker reading "step 0 of M"
#: is unambiguous evidence that zero plan steps ran.
_NX_ANSWER_BUDGET_EXHAUSTED_PRE_PLAN: int = 0

#: RDR-196 .p1e review-fix round 2 (code-review-expert T2 [23115]):
#: the STABLE PREFIX of the budget-exhausted partial-answer marker text
#: -- the single emitter below (``_budget_exhausted_response``) builds
#: the full marker as
#: ``f"{NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX} ({kind}) after step
#: {N} of {M} — partial answer]"`` (``kind`` is ``"time"`` or ``"cost"``
#: -- RDR-196 .p3c, nexus-nyry9.21: the USD budget reuses this exact
#: marker rather than a second shape); only this prefix is a stable,
#: shared contract (kind/N/M vary per run, so the full string is never
#: itself a constant). ``commands/answer_runs.py``'s ``_row_is_failed`` imports
#: THIS constant to recognize a budget-exhausted row's ``final_text``
#: rather than retyping the literal -- a second hand-typed copy would
#: silently drift the moment either side's wording changed (exactly the
#: duplication this module's docstring notes elsewhere have avoided by
#: keeping the marker to one emitter; the read side needs the prefix
#: too, so it is exported as a named constant instead of inlined here).
NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX: str = "[budget exhausted"


@mcp.tool(
    title="Multi-Step Knowledge Answer",
    annotations={"readOnlyHint": False, "destructiveHint": False},
    structured_output=False,
)
@degrade_loud_when_migrating
async def nx_answer(
    question: str,
    scope: str = "",
    context: str = "",
    max_steps: int = 6,
    budget_usd: float | None = None,
    budget_seconds: float | None = _NX_ANSWER_DEFAULT_BUDGET_SECONDS,
    trace: bool = True,
    dimensions: dict[str, Any] | None = None,
    structured: bool = False,
    min_confidence: float | None = None,
    force_dynamic: bool = False,
    bindings: dict[str, Any] | None = None,
) -> "str | dict":
    """Answer a knowledge question using plan-match-first retrieval. RDR-080 P1.

    Internal flow:

    1. **Plan-match gate**: call ``plan_match(intent=question, dimensions=…)``.
       On hit (confidence >= 0.40 or FTS5 sentinel), execute the matched
       plan.  On miss, dispatch an inline LLM planner via ``claude -p``
       to decompose the question and execute the resulting plan.
    2. **Single-step guard**: if the matched plan has exactly 1 ``query``
       step, reroute to ``query()`` directly.
    3. **Execute plan**: run via ``plan_run``.
    4. **Record**: write run metrics to T2 ``nx_answer_runs``.

    **Plan choice (RDR-196 Phase 3 Step 1, nexus-nyry9.20).** On EVERY
    plan-match hit (not only when more than one candidate is returned —
    ``candidate_count == 1`` is the common case today, per nexus-nyry9.3's
    census), the top-confidence match is no longer picked unconditionally:
    among the CONTIGUOUS PREFIX of ``matches`` (matcher order) within
    ``PLAN_CHOICE_CONFIDENCE_BAND`` (``nexus.plans.cost_estimate``, a
    named constant) of the best raw confidence — the prefix stops
    permanently at the first out-of-band candidate, so cost can never
    reach past a relevance demotion the matcher's own RDR-091 scope-fit
    re-ranking already made — the one with the lowest PREDICTED cost from
    its step shape (not a recorded per-plan median) is chosen; ties break
    by earlier matcher position. Outside the prefix, confidence wins
    regardless of cost. Logged on every hit as a ``nx_answer_plan_choice``
    structlog event (candidate count, candidates, estimates, the choice)
    and in the ``structured=True`` envelope's ``plan_choice`` field. See
    docs/cli-reference.md's ``nx answer-runs`` § "Predicted vs actual
    cost" for the read-time estimate-vs-actual column and what is and is
    not yet persisted.

    **Latency.** This is NOT a sub-second call in the general case, and
    the figures below correct an earlier docstring version that
    overstated the fast end by ~45x (it counted degenerate zero-step
    error rows, p50 0.7s, as successes). Re-measured directly from the
    ``nx_answer_runs`` T2 table (nexus-h33x8.6 DO 1-3, T2 memory
    ``nexus/nx-answer-capability-analysis-2026-08-19``), executed plans
    only (n=142, excludes the zero-step error rows): p50 80.1s, p95
    217.1s, p99 316.7s, mean 97.7s; only 0.7% finish under 5s, 88.7%
    take >= 30s, 33.8% take >= 2min. Each operator step (extract, rank,
    summarize, generate, …) spawns a ``claude -p`` subprocess with a
    300-second timeout (a single round trip has an ~11s floor
    independent of workload — this is the substrate's own session
    bootstrap cost, not reducible without leaving it). The plan-miss
    path adds an inline-planner subprocess (also up to 300s) on top —
    the miss tax is ~53s at p50 (plan-hit p50 64.1s vs plan-miss p50
    117.2s). The documented single-step fast path (``query()`` alone,
    mean ~2s) fires only when the matched plan is a single ``tool:
    query`` step — see the seeded ``document-discovery`` /
    ``corpus-coverage-check`` builtin templates (nexus-h33x8.6 a1) for
    the question shapes that route onto it; a composed/operator plan
    pays the durations above regardless of question phrasing.
    ``plan_run`` emits per-step structured ``nx_answer_step_start`` /
    ``nx_answer_step_complete`` events to ``structlog`` (nexus-0qi9) so
    callers tailing ``~/.config/nexus/logs/mcp.log`` can see progress in
    real time.

    Args:
        question: Natural-language question to answer.
        scope: Catalog subtree or corpus filter (e.g. ``"1.2"`` or ``"knowledge"``).
        context: Supplementary caller-supplied context for the plan matcher.
        max_steps: Cap on plan DAG size (passed to inline planner on miss).
        budget_usd: Soft per-invocation cost GUIDANCE in USD, not a hard
            limit -- an over-estimate warns rather than refuses, and the
            mid-run stop can overshoot by up to one segment's cost (see
            below). ``None`` (the default)
            means "use the derived default",
            :data:`nexus.plans.budget_default.DERIVED_BUDGET_USD` --
            1.0530 since 2026-08-21 (RDR-196 .p3a: p90 of n=30 config-conformant
            runs, ``nx answer-runs --derive-budget``; provenance on the
            constant). The former ``0.25`` literal predated any
            measurement and is gone (RDR-196 § Risks). ENFORCED as of
            RDR-196 .p3c (nexus-nyry9.21) whenever
            :data:`nexus.plans.budget_default.BUDGET_ENFORCEMENT_ENABLED`
            is True (the single gate for everything below -- setting it
            back False is a true rollback):

            - A non-positive ``budget_usd`` (``<= 0``) is a loud bounds
              error, returned immediately, before any dispatch. ``0``
              does NOT mean "unlimited".
            - PRE-FLIGHT PRICE CHECK, a WARNING not a refusal (round-2
              deviation from the accepted RDR text, Sam's decision —
              see ``docs/rdr/rdr-196-cost-aware-nx-answer.md`` § Phase 3
              for the dated note): once Step 1 resolves a plan (matched
              or inline-planner-grown), its predicted cost
              (:func:`nexus.plans.cost_estimate.estimate_plan_cost`,
              the SAME step-shape estimate the .p3b plan-choice logic
              already computes) is compared against the REMAINING
              budget (the cap minus whatever the inline planner's own
              dispatch already spent — the .r2 gap, closed on this
              axis by nexus-nyry9.21 D5). This estimate has NO per-plan
              discriminating power in the live population (a same-day
              20-run measurement found every live plan predicting the
              IDENTICAL bundle-dominant ceiling, median actual/predicted
              ratio 0.805, worst overestimate 11.2x) — so exceeding the
              remaining budget now WARNS and RUNS rather than refusing,
              naming the estimate, the remaining budget, the full cap,
              and any already-spent amount. A plan with NO estimate (an
              operator this table has never priced) gets the same
              treatment — it always ran, never refused on that basis
              alone. Both cases surface via the SAME single warning
              emitter: a leading warning line (text mode) / a
              ``budget_warnings`` field (structured mode).
              nexus-nyry9.21 round 2 also added an ``"unknown-cost"``
              warning kind through the identical emitter: when one or
              more executed steps report no ``cost_usd``, the mid-run
              running sum could not include them and the check below
              may have been blind for part of the run — surfaced once
              per run, never silently.
            - MID-RUN STOP-LINE, not a hard ceiling, and the REAL
              enforcement (it sums MEASURED ``StepRecord`` costs, not a
              step-shape guess): the remaining
              budget is threaded into ``plan_run`` as
              ``budget_usd_remaining``, checked BEFORE dispatching each
              segment against the sum of already-completed steps'
              known cost (see ``plan_run``'s own docstring for why a
              dispatch's real cost can only ever be known AFTER it
              returns). The segment that pushes the running sum over
              the cap still finishes; only the segment AFTER it is
              stopped. The run total can therefore end up above
              ``budget_usd`` by up to that final segment's own cost —
              this is the documented contract, not a bug. When it
              trips, ``nx_answer`` returns the SAME
              ``[budget exhausted ...]`` marker ``budget_seconds``
              already produces (see below), with ``(cost)`` in place of
              ``(time)`` — one emitter, parameterized by which budget
              axis tripped, never a second marker shape.
        budget_seconds: nexus-h33x8.6 a4 — an OPTIONAL hard wall-clock
            budget for the plan-EXECUTION phase ONLY (Step 4 below,
            the ``plan_run`` call), measured from this call's own
            start. ``None`` (the default,
            :data:`_NX_ANSWER_DEFAULT_BUDGET_SECONDS`) enforces no
            budget — identical to pre-a4 behavior. When set and the
            budget runs out (or an operator step's own dispatch raises
            ``OperatorTimeoutError`` while the budget is active), the
            retrieved results plus any reconstructed partial operator
            text are returned instead of raising or blocking further —
            "here are the retrieved chunks; synthesis skipped, budget
            exceeded". Marked with a leading ``[budget exhausted (time)
            after step N of M — partial answer]`` line (text mode) or a
            top-level ``budget_exhausted_at_step`` field (structured
            mode). RDR-196 .p3c (nexus-nyry9.21): the USD budget
            (``budget_usd`` above) reuses this exact marker with
            ``(cost)`` in place of ``(time)`` — one emitter, never a
            second shape.

            **CORRECTED (nexus-nyry9.2, RDR-196 .r2):** the plan-MISS
            inline-planner phase (``_nx_answer_plan_miss``, its own
            claude -p round trip, up to 300s) used to be dispatched
            BEFORE ``deadline`` was ever consulted and paid its cost in
            full regardless of budget. It is now checked immediately
            after Step 1 (the plan-match gate, and on a miss, the
            inline planner) — a budget already exhausted at that point
            returns the exhaustion marker with
            :data:`_NX_ANSWER_BUDGET_EXHAUSTED_PRE_PLAN` (step 0,
            meaning zero plan steps ran) instead of proceeding to
            Step 2 or Step 4. This also closes a second, previously
            undocumented gap: the single-step fast path (Step 2) had no
            deadline check of its own before this fix.

            **DELETED (nexus-nyry9.5, RDR-196 .r5 review-fix, T2
            review-nexus-nyry9.5):** a prior version of this docstring
            named a second, INTENTIONAL boundary — a "retrieval-only"
            matched plan (no operator steps) was exempt from the
            deadline entirely. That bucket was never demonstrated to
            route any real matched plan (a census of all 17 shipped
            builtin plans found zero; see
            ``_nx_answer_classify_plan``'s own docstring) and has been
            deleted along with the exemption. There is no longer any
            boundary where ``budget_seconds`` fails to apply to plan
            execution once Step 1 has produced a match — every
            non-``single_query`` plan is budget-bound at Step 4 like
            any other.
        trace: When False, redacts question and final_text in the run log.
        dimensions: Dimensional filter for the plan-match gate.  Pass
            ``{"verb": "research"}`` (etc.) so verb skills narrow the
            match to templates of the appropriate verb.  Unset means
            the matcher considers every active plan.
        structured: RDR-086 Phase 3.3 opt-in. When True, returns an
            envelope dict ``{final_text, chunks, plan_id, step_count}``
            instead of a bare string. Each entry in ``chunks`` carries
            ``id``, ``chash``, ``collection`` (and ``distance``, ``text``
            when available) so callers can build ``chash:<hex>`` citations
            without a second fetch. The single-step guard path produces
            the same envelope shape — the guard logic itself is unchanged.
            On pure-generate plans or retrieval misses, ``chunks`` is ``[]``.
            ``truncated_chars`` (nexus-2xjge): ``None`` unless ``final_text``
            was capped (a trailing ``[nx_answer: result capped at ...]``
            marker is appended in text mode too), in which case it is the
            count of characters dropped.
        min_confidence: Per-call plan-match floor override (RDR-092 Phase
            2 Option A). ``None`` (default) uses the global
            :data:`_PLAN_MATCH_MIN_CONFIDENCE` (0.40, per RDR-079 P5).
            Verb skills that have validated a stricter precision-first
            floor (0.50 per R9 against a 5+5 probe corpus) pin the
            tighter value per-call without moving the global knob; the
            global default waits on Phase 5's larger-corpus
            validation. Must be in ``[0.0, 1.0]`` when supplied.
        bindings: Caller-supplied plan bindings, e.g.
            ``{"content_type": "rdr"}``. A sibling of ``dimensions`` /
            ``scope``, not a new verb. Two effects: the value reaches the
            plan runner, AND the matcher is told the binding is available
            so it will offer plans that require it. Without this,
            type-scoped builtins (``type-scoped-search`` needs
            ``content_type``, ``find-by-author`` needs ``author``) are
            unreachable, because nx_answer will not infer a typed value
            from the question text (nexus-0yrjr). Values are NOT
            validated: the catalog's ``content_type`` has no closed
            domain (``code``, ``prose``, ``rdr``, ``paper``,
            ``knowledge``, ``blog_post``, ... and it grows), so
            rejecting against a fixed set would refuse legitimate values.
        force_dynamic: RDR-090 P1.1 (nexus-dslg). When True, skip the
            plan-match gate entirely and route directly to the inline
            LLM planner / dynamic-generation path. Default False
            preserves the plan-match-first flow. Used by the
            AgenticScholar bench harness path C to isolate dynamic
            generation from the matched-plan path on collection-scoped
            questions where ``scope`` would otherwise act as a forced-
            miss proxy.

    Returns:
        The final step's output — a string by default, or the envelope
        dict described above when ``structured=True``.
    """
    import time  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site
    import structlog as _slog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
    from types import SimpleNamespace  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site (nexus-nyry9.2 pre-Step-2 budget stand-in)

    from nexus.mcp_infra import get_t1_plan_cache  # noqa: PLC0415 — circular-dep avoidance (mcp package import deferred)
    from nexus.plans.budget_default import (  # noqa: PLC0415 — deferred for startup cost; call-time import so a test's monkeypatch on the module attribute is honored every call (RDR-196 .p3c, nexus-nyry9.21)
        BUDGET_ENFORCEMENT_ENABLED as _budget_enforcement_enabled,
        DERIVED_BUDGET_USD as _derived_budget_usd,
    )
    from nexus.plans.cost_estimate import (  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
        PLAN_CHOICE_CONFIDENCE_BAND,
        PlanCostEstimate,
        choose_within_band,
        estimate_plan_cost,
        get_cached_price_table,
    )
    from nexus.plans.matcher import plan_match as _plan_match  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
    from nexus.plans.runner import plan_run as _plan_run  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
    from nexus.plans.verb_infer import infer_verb as _infer_verb  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

    _log = _slog.get_logger()
    start = time.monotonic()
    # RDR-196 Phase 3 Step 1 (nexus-nyry9.20, code-review round 1
    # dormancy item, T2 nyry9.20-code-review-2026-08-21): captured by
    # Step 1 below when it resolves a plan-match hit, read by `_result`'s
    # closure to populate the structured envelope's `plan_choice` field.
    # Declared here (bound to None) so every `_result(...)` call site --
    # including ones on paths that never reach Step 1's hit branch, e.g.
    # the inline-planner-miss error path -- has a defined value to read;
    # a closure that only got assigned inside one conditional branch
    # would raise on any call from an earlier-returning path.
    _plan_choice_info: "dict | None" = None
    # nexus-h33x8.6 a4: anchored at call entry (not at plan_run's own
    # start) so "budget_seconds" reads as "answer me within N seconds
    # total", matching caller intuition. Threaded through to plan_run
    # unconditionally at Step 4 below (nexus-nyry9.5, RDR-196 .r5: the
    # deadline exemption a prior classify_plan bucket used to carry was
    # deleted — see that function's own docstring).
    deadline = start + budget_seconds if budget_seconds is not None else None

    # RDR-196 .p3c (nexus-nyry9.21): USD budget enforcement state, all
    # declared here (bound to inert defaults) for the same reason
    # `_plan_choice_info` is above -- every `_result(...)` call site,
    # including ones on paths that resolve before pricing ever runs,
    # must see a defined value. `effective_budget_usd` is computed just
    # before Step 1 below (after `_result` is defined, alongside the
    # `min_confidence` bounds check) -- the caller's `budget_usd` when
    # supplied, else the derived default, but ONLY when
    # `BUDGET_ENFORCEMENT_ENABLED` -- the single gate for ALL
    # enforcement (bounds check, pre-flight refusal, mid-run stop).
    effective_budget_usd: "float | None" = None
    #: Captures the inline planner's own claude_dispatch cost (D5 /
    #: nexus-nyry9.21, closing the .r2 gap for the USD axis) via the
    #: ambient sink wrapped around the Step 1 miss-path dispatch below.
    #: Stays empty (spend 0.0) on a plan-match HIT — no planner dispatch
    #: happens on that path.
    _planner_usage_sink: list = []
    #: Set once, in the pre-Step-2 budget check below, to
    #: `effective_budget_usd` minus whatever the planner already spent.
    #: Threaded into `plan_run` at Step 4 as `budget_usd_remaining`.
    #: `None` whenever enforcement is inactive for this call -- mirrors
    #: `deadline`'s own "only pass the kwarg when set" discipline.
    _budget_remaining_usd: "float | None" = None
    #: RDR-196 .p3c round-2 (Sam's decision on the critic's CRITICAL, T2
    #: p3c-critique-2026-08-21) / round-3 (critic Significant, T2
    #: p3c-critique round 3): the ONE accumulator for every budget
    #: WARNING this call can produce -- D4's no-estimate case, the
    #: demoted over-cap pre-flight (was a hard refusal; the step-shape
    #: estimator has no per-plan discriminating power in the live
    #: population, see `_emit_budget_warning`'s own docstring below),
    #: and the mid-run unknown-cost blind-spot signal (critic
    #: Significant 1). Each entry is a ``{"kind": ..., "detail": ...}``
    #: dict, not a formatted string -- round 3 made this
    #: MACHINE-READABLE (critic Significant: "over-cap fires on every
    #: tight-budget call; no-estimate/unknown-cost are rare
    #: cost-tracking-quality anomalies" -- a caller/`.22` needs to tell
    #: those apart without grepping prose) so `_result`'s closure below
    #: can derive BOTH the human leading text line(s) AND a structured
    #: `budget_warnings` list from the SAME data, never two
    #: independently-maintained shapes. A list, not a single dict:
    #: independent warning kinds can co-occur in one call (e.g. a
    #: no-estimate pre-flight warning AND an unknown-cost blind spot
    #: found during execution) and must not silently overwrite each
    #: other. Read by `_result`'s closure so EVERY exit path this call
    #: can take after a warning is emitted carries all of them, in both
    #: shapes.
    _budget_warning_entries: "list[dict[str, str]]" = []

    # RDR-137 followup (nexus-n1908): normalize a malformed comma-list
    # scope to broad search (with a warning) so it doesn't filter
    # retrieval to nothing and trigger the ambient-context synthesis
    # failure. Done before any scope use (plan_match scope_preference,
    # plan-miss planner, grown-plan scope_tags).
    scope, _scope_warning = _nx_answer_normalize_scope(scope)
    if _scope_warning:
        _log.warning("nx_answer_scope_normalized", detail=_scope_warning)

    # nexus-0yrjr: derive the run bindings and the availability set
    # together, AFTER scope normalization so ``_nx_scope`` carries the
    # normalized value. The matcher is told exactly what this call can
    # bind, so it never offers a plan that cannot run — and a caller
    # supplying ``content_type`` / ``author`` makes the type-scoped
    # builtins reachable again.
    _caller_run, _caller_available = _nx_answer_caller_bindings(
        question=question, scope=scope, bindings=bindings,
    )

    # RDR-086 Phase 3.3: envelope builder. String mode returns the text
    # directly; structured mode wraps it into the documented envelope.
    def _result(text: str, *, plan_id: int = 0, step_count: int = 0,
                chunks: "list | None" = None,
                budget_exhausted_at_step: "int | None" = None,
                step_records: "list | None" = None) -> "str | dict":
        # RDR-196 .p3c (nexus-nyry9.21), extended round 2 / round 3:
        # single emitter for every budget WARNING, both shapes. Reads
        # the closure list `_budget_warning_entries` (declared near the
        # top of this function, appended to by `_emit_budget_warning`,
        # never overwritten) so EVERY exit path this call can take
        # after ANY warning is emitted — success, a later error, the
        # exhaustion marker itself — carries all of them, never
        # silently dropped for a structured=False caller. The human
        # text line shape is UNCHANGED from round 2:
        # ``[budget warning (kind): detail]``, one per entry, joined.
        _budget_warning_text = (
            "\n".join(
                f"[budget warning ({e['kind']}): {e['detail']}]"
                for e in _budget_warning_entries
            )
            if _budget_warning_entries else None
        )
        # RDR-196 .p3c round 3 (code review Medium, co-occurrence bug
        # found via the new test): when the exhaustion marker is ALSO
        # present, it must stay the LEADING line — commands/answer_runs.
        # py's `_row_is_failed` keys on
        # ``final_text.startswith(NX_ANSWER_BUDGET_EXHAUSTED_MARKER_
        # PREFIX)``, which an unconditional warning-first prepend would
        # break the moment a warning and an exhaustion co-occur (now a
        # real possibility since a warned run PROCEEDS to execute
        # instead of being refused). Prepend in the normal case
        # (unchanged from round 2); append after the marker when one is
        # present, so the marker's prefix contract always holds.
        # nexus-2xjge: final_text is the last plan step's raw output —
        # unbounded when that step is e.g. operator_generate (an LLM call
        # with no code-level output cap) or a custom-grown plan whose
        # final tool is not one of the ones _cap_text_result already
        # guards. Cap here too, defense in depth. The cap is applied to
        # the RAW text BEFORE warning composition (critique finding,
        # review-2g8y7-2xjge-critique [23267]): capping the composed
        # string could slice an end-appended warning off in the
        # exhausted+warned+oversized co-occurrence, silently dropping it
        # for a structured=False caller — the exact contract round 3
        # exists to keep. The composed result may therefore exceed the
        # cap by the warning text's length; warnings are line-sized and
        # the never-dropped contract outranks the cap's exactness.
        _pre_cap_chars = len(text)
        text = _cap_text_result(text, "nx_answer")
        truncated_chars = (
            _pre_cap_chars - _TEXT_RESULT_CAP_CHARS
            if _pre_cap_chars > _TEXT_RESULT_CAP_CHARS else None
        )
        if not _budget_warning_text:
            _text = text
        elif text.startswith(NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX):
            _text = f"{text}\n{_budget_warning_text}"
        else:
            _text = f"{_budget_warning_text}\n{text}"
        if not structured:
            return _text
        # RDR-196 .p1e (nexus-nyry9.11): per-step breakdown in the
        # structured envelope, same field names as the wire (reuses
        # _step_record_to_wire — the exact dict TelemetryHandler's
        # nx_answer_steps route expects, so a caller sees one shape
        # whether it reads the envelope or `nx answer-runs --json`).
        # ``[]`` when no plan_run steps executed (single-step fast path,
        # or any error/miss path before plan_run ever ran).
        # ``cost_usd`` mirrors _nx_answer_record_run's own blanket rule:
        # the sum of every step's non-None cost, or None when no step
        # reports a known cost — never a fabricated 0.0.
        _steps = step_records or []
        _known_costs = [
            s.cost_usd for s in _steps if getattr(s, "cost_usd", None) is not None
        ]
        cost_usd = sum(_known_costs) if _known_costs else None
        return {
            "final_text": _text,
            "chunks": chunks if chunks is not None else [],
            "plan_id": plan_id,
            "step_count": step_count,
            # nexus-h33x8.6 a4 / RDR-196 marker convention: always present
            # (None on every non-budget-exhausted path) so callers can
            # rely on the key rather than checking membership.
            "budget_exhausted_at_step": budget_exhausted_at_step,
            "steps": [_step_record_to_wire(s) for s in _steps],
            "cost_usd": cost_usd,
            # nexus-2xjge: always present (None when final_text was not
            # capped), same "always present" convention as
            # budget_exhausted_at_step above — a caller relies on the key,
            # never membership-checks it.
            "truncated_chars": truncated_chars,
            # RDR-196 .p3c round 3 (critic Significant, T2 p3c-critique
            # round 3) / round 4 (a THIRD shape carrying the same
            # information — the joined-prose `budget_estimate_warning`
            # field this diff shipped in round 3 — was itself the exact
            # defect class this bead's marker-convergence rule exists to
            # prevent, caught before it ever landed; deleted, not kept
            # for "back-compat" that never existed since the field never
            # shipped). MACHINE-READABLE: over-cap fires chronically on
            # any tight-budget call and is fundamentally different from
            # the rare no-estimate / unknown-cost cost-tracking-quality
            # anomalies; a caller (`.22`, `nx answer-runs`) needs to tell
            # them apart by `kind` without parsing prose. Derived from
            # `_budget_warning_entries` (the SAME accumulator the leading
            # text line above is also built from — exactly two shapes,
            # never three). Always present, ``[]`` (not None) when
            # empty, same "always present" convention as
            # `budget_exhausted_at_step` above and the same empty-list
            # convention as `chunks`.
            "budget_warnings": list(_budget_warning_entries),
            # RDR-196 Phase 3 Step 1 (nexus-nyry9.20): the Step 1 plan-
            # choice decision, when it ran (None on force_dynamic, a
            # plan-miss, or any error path before Step 1's hit branch —
            # a closure read of `_plan_choice_info`, always defined, see
            # its declaration near the top of this function). Shape when
            # present: {candidates, candidate_count, chosen_plan_id,
            # predicted_cost_usd, basis} — the SAME per-candidate dicts
            # the nx_answer_plan_choice structlog event carries, so a
            # caller reading the envelope and one tailing the log see one
            # shape.
            "plan_choice": _plan_choice_info,
        }

    # nexus-h33x8.6 a4 / nexus-nyry9.2 (RDR-196 .r2): single shared
    # emitter for the budget-exhausted marker. Called from TWO sites —
    # Step 4's post-``plan_run`` check (a real ``PlanResult``) and the
    # pre-Step-2 check below (a synthetic stand-in used when Step 1 —
    # the plan-match gate, and on a miss, the inline planner — already
    # spent the whole budget before any plan step ran). Kept as ONE
    # function so there is exactly one place that builds the marker
    # text, per the "one emitter, both shapes" constraint. Returns
    # ``None`` when ``result`` carries no budget-exhausted marker, in
    # which case the caller falls through to its own next step.
    def _budget_exhausted_response(result: Any) -> "str | dict | None":
        # Defensive isinstance check (not a bare truthy/None check): a bare
        # MagicMock's unconfigured ``.budget_exhausted_at_step`` attribute is
        # itself a (truthy, non-None) MagicMock, which would misfire here
        # for any test double that doesn't set the attribute explicitly.
        _budget_step = getattr(result, "budget_exhausted_at_step", None)
        if not isinstance(_budget_step, int):
            return None
        # RDR-196 .p3c (nexus-nyry9.21) D3: which budget axis tripped --
        # "time" (the shipped budget_seconds mechanism) or "cost" (this
        # bead's budget_usd mechanism), reusing this SAME emitter rather
        # than adding a second marker shape. Defaults to "time" for any
        # PlanResult / stand-in that predates this field (every real
        # caller from before .p3c only ever exhausted on time) or a test
        # double that doesn't set it -- never raises on a missing/odd
        # attribute, same defensive posture as `_budget_step` above.
        _budget_kind = getattr(result, "budget_exhausted_kind", None)
        if _budget_kind not in ("time", "cost"):
            _budget_kind = "time"
        # code-review Important (T2 code-review-nexus-h33x8.6-a4-a2-
        # 2026-08-19): use PlanResult.total_planned_steps -- the field
        # a4 added to runner.py specifically so callers don't re-parse
        # match.plan_json -- rather than re-parsing it here. Falls back
        # to a re-parse only for a test double / older PlanResult (or
        # the pre-Step-2 stand-in) that doesn't carry the field
        # (defaults to 0, falsy).
        # nexus-nyry9.2 review (code-review-nexus-nyry9.2-2026-08-20):
        # guard the re-parse fallback the same way
        # ``_nx_answer_classify_plan`` guards its own ``json.loads`` of
        # ``match.plan_json`` (:5724-5727) — a corrupted library-matched
        # plan row reaching this fallback (e.g. via the pre-Step-2
        # stand-in below, whose own guarded parse already defaulted to
        # 0) must still produce the marker, not raise out of the tool.
        try:
            total_planned = (
                getattr(result, "total_planned_steps", 0)
                or len(json.loads(best.plan_json).get("steps") or [])
            )
        except (json.JSONDecodeError, TypeError):
            total_planned = 0
        marker = (
            f"{NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX} ({_budget_kind}) after step "
            f"{_budget_step} of {total_planned} — partial answer]"
        )
        # Harvest retrieved chunks the same way Step 5's structured path
        # does, so a partial answer still surfaces what was found. The
        # pre-Step-2 stand-in has no steps at all, so this is [].
        partial_chunks: list[dict] = []
        for step_out in result.steps:
            if not isinstance(step_out, dict):
                continue
            ids = step_out.get("ids")
            if not isinstance(ids, list) or not ids:
                continue
            hashes = step_out.get("chunk_text_hash", []) or []
            per_chunk_colls = step_out.get("chunk_collections") or []
            dedup_colls = step_out.get("collections", []) or []
            dists = step_out.get("distances", []) or []
            default_coll = dedup_colls[0] if dedup_colls else ""
            for i, cid in enumerate(ids):
                coll = per_chunk_colls[i] if i < len(per_chunk_colls) else default_coll
                partial_chunks.append({
                    "id": cid,
                    "chash": hashes[i] if i < len(hashes) else "",
                    "collection": coll,
                    "distance": dists[i] if i < len(dists) else None,
                })
        # The terminal step is where plan_run puts the OperatorTimeoutError
        # sentinel (isolated path) or the bundle's terminal slot (bundle
        # path) — both carry partial_text when a3's stream-json capture
        # produced anything. Absent for the pre-Step-2 stand-in (no
        # steps), leaving this "".
        last_step = result.steps[-1] if result.steps else {}
        partial_operator_text = (
            str(last_step.get("partial_text") or "")
            if isinstance(last_step, dict) else ""
        )

        lines = [marker]
        if partial_chunks:
            lines.append(
                f"Retrieved {len(partial_chunks)} chunk"
                f"{'s' if len(partial_chunks) != 1 else ''} before the "
                "budget ran out:"
            )
            for ch in partial_chunks[:10]:
                lines.append(f"  - {ch['id']} in {ch['collection']}")
            if len(partial_chunks) > 10:
                lines.append(f"  ... and {len(partial_chunks) - 10} more")
        else:
            lines.append("No retrieval results were captured before the budget ran out.")
        if partial_operator_text:
            lines.append("")
            lines.append(
                "Partial synthesis (incomplete, reconstructed from the "
                "interrupted operator call):"
            )
            lines.append(partial_operator_text)
        budget_final_text = "\n".join(lines)

        _log.info(
            "nx_answer_budget_partial_result",
            plan_id=best.plan_id,
            budget_exhausted_at_step=_budget_step,
            budget_exhausted_kind=_budget_kind,
            total_planned_steps=total_planned,
            chunk_count=len(partial_chunks),
            has_partial_operator_text=bool(partial_operator_text),
        )
        # nexus-nyry9.10 (RDR-196 .p1d): ``result`` here is EITHER a real
        # ``PlanResult`` (Step 4's post-plan_run check — real
        # ``step_records``) OR the pre-Step-2 ``SimpleNamespace`` stand-in,
        # which carries no ``step_records`` attribute at all. Defensive
        # isinstance check (not a bare ``getattr(..., [])`` — a MagicMock
        # test double's unconfigured ``.step_records`` is itself a
        # (truthy, non-list) MagicMock, same landmine
        # ``_budget_exhausted_response``'s own ``_budget_step`` guard
        # above already documents for this exact function).
        _step_records = getattr(result, "step_records", None)
        if not isinstance(_step_records, list):
            _step_records = []
        try:
            with _t2_ctx() as db:
                _nx_answer_record_run(
                    db.telemetry, question=question, plan_id=best.plan_id,
                    matched_confidence=best.confidence, step_count=len(result.steps),
                    final_text=budget_final_text[:2000], step_records=_step_records,
                    duration_ms=int((time.monotonic() - start) * 1000), trace=trace,
                )
        except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
            pass
        # nexus-yg49g doctrine: a budget-exhausted run did not complete —
        # counts as a failure so a chronically-timing-out plan does not
        # accrue a false success rate.
        _nx_answer_record_outcome(best.plan_id, success=False)
        return _result(
            budget_final_text, plan_id=best.plan_id, step_count=len(result.steps),
            chunks=partial_chunks if structured else None,
            budget_exhausted_at_step=_budget_step,
            step_records=_step_records,
        )

    def _emit_budget_warning(kind: str, detail: str) -> None:
        """RDR-196 .p3c round 2 (Sam's decision on the critic's CRITICAL,
        T2 p3c-critique-2026-08-21): single emitter for every budget
        WARNING this call can produce, parameterized by `kind` — the
        same convergence discipline `_budget_exhausted_response` above
        already applies to the exhaustion marker via ITS `kind`. Never
        grow a second, parallel warning mechanism; every new warning
        class calls this with a new `kind` string.

        Known kinds (as of this round): ``"no-estimate"`` (D4's
        original no-history/unpriceable path), ``"over-cap"`` (the
        pre-flight check DEMOTED from a hard refusal — .p3b's
        step-shape estimator has no per-plan discriminating power in
        the live population: a same-day 20-run measurement found all 4
        live plans predicting the IDENTICAL $0.90433 because
        bundle-dominant pricing always resolves to the same
        generate@opus ceiling, median actual/predicted 0.805, worst
        overestimate 11.2x — so a hard refusal at that number is a
        step function, not a per-plan judgement, and would wrongly
        refuse real runs costing a fraction of the estimate),
        ``"unknown-cost"`` (critic Significant 1: the mid-run running
        sum could not accumulate for one or more steps, so the cost
        check may have been blind for part of this run — the mid-run
        stop-line itself is UNCHANGED and still enforces on every
        MEASURED StepRecord cost; only the coverage gap needs
        surfacing, once per run).

        Appends rather than overwrites: independent kinds can co-occur
        in one call (e.g. a `"no-estimate"` pre-flight warning and an
        `"unknown-cost"` blind spot found during execution) and must
        not silently replace each other — `_result`'s closure derives
        BOTH the joined human text (unchanged shape from round 2) AND
        the round-3 machine-readable ``budget_warnings`` structured
        field from this SAME accumulator, never two independently
        maintained shapes.

        Stores a ``{"kind": kind, "detail": detail}`` dict (round 3,
        critic Significant: the joined-prose-only shape gave a caller
        no way to tell "over-cap fired again, as it does on every
        tight-budget call" apart from the rare no-estimate/unknown-cost
        cost-tracking-quality anomalies without grepping text) rather
        than a pre-formatted string — the ONE accumulator, not a
        parallel one.
        """
        _budget_warning_entries.append({"kind": kind, "detail": detail})

    # ── Step 1: plan-match gate ──────────────────────────────────────────
    # RDR-092 Phase 2 Option A: effective floor is the caller's override
    # when supplied, otherwise the RDR-079 P5 default (0.40). Bounds-
    # check the override so an agent caller passing a degenerate value
    # fails loudly (code-review S-4) instead of silently admitting
    # every match (negative) or rejecting every cosine match (> 1.0).
    if min_confidence is not None and not (0.0 <= min_confidence <= 1.0):
        return _result(
            f"min_confidence must be in [0.0, 1.0], got {min_confidence!r}"
        )
    effective_min_confidence = (
        min_confidence if min_confidence is not None
        else _PLAN_MATCH_MIN_CONFIDENCE
    )

    # RDR-196 .p3c (nexus-nyry9.21) D1: BUDGET_ENFORCEMENT_ENABLED gates
    # ALL budget_usd enforcement -- an explicit caller-supplied cap and
    # the derived default alike -- so flipping it back False is a true
    # rollback. Checked here (mirrors the min_confidence bounds check
    # immediately above) so a degenerate cap fails loudly, before any
    # dispatch, exactly like a degenerate min_confidence.
    if _budget_enforcement_enabled:
        if budget_usd is not None and budget_usd <= 0:
            return _result(f"budget_usd must be > 0, got {budget_usd!r}")
        effective_budget_usd = budget_usd if budget_usd is not None else _derived_budget_usd

    if force_dynamic:
        # RDR-090 P1.1: skip the plan-match gate entirely. The
        # dynamic-planner path below picks up matches=[].
        _log.info(
            "nx_answer_force_dynamic",
            question=question[:100] if trace else "[redacted]",
        )
        matches: list = []
    else:
        # Category route (T2 design-dimension-routed-category-plans-
        # 2026-08-21). A caller-pinned verb always wins — the five
        # scenario skills pass dimensions={"verb": ...} and must keep
        # selecting their own verb's plans. Otherwise derive one from
        # the question, which is what lets an unscoped nx_answer call
        # reach a builtin at all: builtins have been selected once in
        # four months, and every one of those misses came in through
        # this path with no verb attached.
        #
        # This travels as its OWN argument. Merging it into `dimensions`
        # would make it a hard filter over every candidate including
        # grown plans, whose verbs are only ever research/analyze, so a
        # derived debug/document verb would drop the entire live hit
        # rate.
        _pinned_verb = (dimensions or {}).get("verb")
        _category_verb = _pinned_verb or _infer_verb(question)
        try:
            with _t2_ctx() as db:
                cache = get_t1_plan_cache(populate_from=db.plans)
                matches = _plan_match(
                    question,
                    library=db.plans,
                    cache=cache,
                    dimensions=dimensions,
                    scope_preference=scope,
                    context={"user_context": context} if context else None,
                    min_confidence=effective_min_confidence,
                    n=5,
                    # nexus-0yrjr: declare what this call can actually
                    # bind so the matcher never offers a plan that cannot
                    # run. nx_answer supplies ``intent`` always and
                    # ``_nx_scope`` when the caller passed a scope; every
                    # free-text binding is aliased from the question, so
                    # only typed bindings can make a plan unrunnable.
                    available_bindings=_caller_available,
                    category_verb=_category_verb,
                )
        except Exception as exc:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
            return _result(f"Error during plan match: {exc}")

    if not matches or not _nx_answer_match_is_hit(
        matches[0].confidence, threshold=effective_min_confidence,
    ):
        # Plan miss — inline LLM planner via claude_dispatch.
        _log.info(
            "nx_answer_plan_miss",
            question=question[:100] if trace else "[redacted]",
        )
        try:
            # RDR-196 .p3c (nexus-nyry9.21) D5: capture the planner's own
            # claude_dispatch cost via the ambient sink so it can be
            # seeded into the running USD spend below (.r2's gap on the
            # cost axis — the planner phase already charges against
            # budget_seconds via the deadline check further down).
            # Wrapped unconditionally (cheap contextvar set/reset); only
            # CONSUMED when budget enforcement is active.
            from nexus.operators.dispatch import (  # noqa: PLC0415 — deferred; matches this function's convention
                ambient_usage_sink as _ambient_usage_sink,
            )
            with _ambient_usage_sink(_planner_usage_sink):
                best = await _nx_answer_plan_miss(
                    question, scope=scope, max_steps=max_steps,
                    few_shot_matches=matches,
                )
        except Exception as exc:  # noqa: BLE001 — boundary catch; failure surfaced via log.warning, must not crash caller
            elapsed_ms = int((time.monotonic() - start) * 1000)
            _log.warning("nx_answer_planner_failed", error=str(exc))
            try:
                with _t2_ctx() as db:
                    _nx_answer_record_run(
                        db.telemetry, question=question, plan_id=None,
                        matched_confidence=matches[0].confidence if matches else None,
                        step_count=0, final_text=f"Planner error: {exc}",
                        step_records=[], duration_ms=elapsed_ms, trace=trace,
                    )
            except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
                pass
            # Search review I-5: propagate the planner's detail — e.g.
            # "planner returned only non-dispatchable tools: Bash, grep"
            # — so the user isn't left guessing why the inline path failed.
            reason = str(exc) or "unknown error"
            return _result(
                f"No matching plan found and inline planner failed: {reason}. "
                "Try rephrasing, or use search/query directly."
            )
    else:
        best = matches[0]
        # RDR-196 Phase 3 Step 1 (nexus-nyry9.20; Sam's 2026-08-21 OPTION
        # (a) decision, superseding the RDR's original recorded-median
        # design after nexus-nyry9.3's .r3 census found zero plans with a
        # rankable recorded-history population): among above-floor
        # candidates within PLAN_CHOICE_CONFIDENCE_BAND of the best raw
        # confidence, prefer the cheapest PREDICTED cost (from step
        # shape, priced against Phase 1 telemetry with a static
        # fallback) instead of unconditionally taking matches[0].
        # RDR-100 fence: `matches` and every Match.confidence value are
        # untouched inputs from the plan-match gate above — this only
        # picks which already-computed candidate the runner executes.
        # Degrades to the pre-existing top-confidence choice (best stays
        # matches[0]) on any failure — cost ranking must never be able to
        # crash or block an otherwise-successful nx_answer call.
        #
        # UNCONDITIONAL (code-review round 1 dormancy item, T2
        # nyry9.20-code-review-2026-08-21): previously gated on
        # `len(matches) > 1`, which — per nexus-nyry9.3's .r3/.r1 census
        # — is almost never true on today's library (matches is usually
        # length 1), making the feature's activity rate unmeasurable.
        # choose_within_band handles a single-candidate list trivially
        # (one cheap estimate_plan_cost call, returns it unchanged), so
        # there is no meaningful cost to always computing + logging this.
        try:
            with _t2_ctx() as _cost_db:
                _price_table = get_cached_price_table(_cost_db.telemetry)
            _chosen, _decision_log = choose_within_band(
                matches, _price_table, band=PLAN_CHOICE_CONFIDENCE_BAND,
            )
            _chosen_row = next(
                (r for r in _decision_log if r["plan_id"] == _chosen.plan_id), None,
            )
            _plan_choice_info = {
                "candidates": _decision_log,
                "candidate_count": len(matches),
                "chosen_plan_id": _chosen.plan_id,
                "predicted_cost_usd": _chosen_row["predicted_cost_usd"] if _chosen_row else None,
                "basis": _chosen_row["basis"] if _chosen_row else None,
            }
            _log.info(
                "nx_answer_plan_choice",
                candidate_count=len(matches),
                candidates=_decision_log,
                chosen_plan_id=_chosen.plan_id,
                top_confidence_plan_id=matches[0].plan_id,
                band=PLAN_CHOICE_CONFIDENCE_BAND,
            )
            best = _chosen
        except Exception:  # noqa: BLE001 — boundary catch; degrade to top-confidence match, must not crash caller
            _log.warning("nx_answer_plan_cost_ranking_failed", exc_info=True)

    if best.plan_id == 0:
        conf_str = "ad-hoc"
    elif best.confidence is None:
        conf_str = "fts5"
    else:
        conf_str = f"{best.confidence:.3f}"

    # nexus-use1: plan execution telemetry. Bump ``use_count`` + stamp
    # ``last_used`` before any execution path (single-step fast path OR
    # _plan_run). Skip plan_id=0 (synthetic inline-planner Match — no
    # library row to update). Downstream paths bump success/failure via
    # ``_nx_answer_record_outcome`` after their try/except completes.
    if best.plan_id:
        try:
            with _t2_ctx() as db:
                db.plans.increment_run_started(best.plan_id)
        except Exception:  # noqa: BLE001 — boundary catch; failure surfaced via log.warning, must not crash caller
            _log.warning(
                "nx_answer_plan_use_increment_failed",
                plan_id=best.plan_id, exc_info=True,
            )

    # nexus-nyry9.2 (RDR-196 .r2): classified once here and reused by
    # Step 2 below (previously computed twice) so the pre-Step-2 budget
    # check and the single-step guard agree on the same classification.
    plan_class = _nx_answer_classify_plan(best)

    # ── Pre-Step-2 budget check (nexus-nyry9.2, RDR-196 .r2) ──────────────
    # Step 1 above — the plan-match gate, and on a miss, the inline LLM
    # planner's own claude -p round trip — is dispatched entirely BEFORE
    # this point. Before this fix that phase ran wholly outside
    # ``budget_seconds``: ``deadline`` was computed at call entry but
    # the first place it was ever consulted was inside ``plan_run``
    # (Step 4) — so a slow planner could burn the caller's whole budget
    # and nx_answer would still go on to execute the (now-late) plan.
    # Checked ONCE here, covering both the single-step fast path
    # (Step 2) and plan execution (Step 4), neither of which had a
    # deadline check of its own before this fix.
    #
    # nexus-nyry9.5 (RDR-196 .r5 review-fix): a deadline exemption for
    # a third classify_plan bucket used to apply here — DELETED along
    # with that bucket (see ``_nx_answer_classify_plan``) since it was
    # never demonstrated to route a real matched plan. Every
    # non-single_query plan is now budget-bound at this check like any
    # other.
    if deadline is not None and time.monotonic() >= deadline:
        # code-review Important (T2 nyry9.2-code-review-2026-08-20):
        # guard this the same way ``_nx_answer_classify_plan`` guards
        # its own parse of ``match.plan_json`` (:5724-5727) — a
        # corrupted library-matched plan row combined with an already-
        # exhausted budget must still flow to the marker, not raise
        # out of the MCP tool. Defaults to 0 (unknown step count) on a
        # malformed row; ``_budget_exhausted_response``'s own fallback
        # re-parse is guarded the same way for the same reason.
        try:
            total_planned = len(json.loads(best.plan_json).get("steps") or [])
        except (json.JSONDecodeError, TypeError):
            total_planned = 0
        _pre_plan_stub = SimpleNamespace(
            budget_exhausted_at_step=_NX_ANSWER_BUDGET_EXHAUSTED_PRE_PLAN,
            budget_exhausted_kind="time",
            steps=[],
            total_planned_steps=total_planned,
        )
        _budget_response = _budget_exhausted_response(_pre_plan_stub)
        if _budget_response is not None:
            return _budget_response

    # ── Pre-Step-2 USD budget check (nexus-nyry9.21, RDR-196 .p3c) ────────
    # D5: the planner's own dispatch cost (captured above via the ambient
    # sink around Step 1's miss path; empty/0.0 on a plan-match HIT,
    # which never dispatches a planner) is seeded into the running spend
    # BEFORE the remaining budget is computed — closes the same .r2-class
    # gap on the cost axis that the time check above already closed on
    # the wall-clock axis.
    if effective_budget_usd is not None:
        _planner_spent_usd = sum(
            u.cost_usd for u in _planner_usage_sink if getattr(u, "cost_usd", None) is not None
        )
        _budget_remaining_usd = effective_budget_usd - _planner_spent_usd
        if _budget_remaining_usd <= 0:
            # D5: reuse the EXISTING pre-plan sentinel path rather than
            # inventing a new one — same stand-in shape as the time
            # check above, kind="cost" is the only difference.
            try:
                total_planned = len(json.loads(best.plan_json).get("steps") or [])
            except (json.JSONDecodeError, TypeError):
                total_planned = 0
            _pre_plan_stub = SimpleNamespace(
                budget_exhausted_at_step=_NX_ANSWER_BUDGET_EXHAUSTED_PRE_PLAN,
                budget_exhausted_kind="cost",
                steps=[],
                total_planned_steps=total_planned,
            )
            _budget_response = _budget_exhausted_response(_pre_plan_stub)
            if _budget_response is not None:
                return _budget_response
        else:
            # D4: pre-flight PRICE CHECK on the chosen/grown plan. Reuse
            # the PlanCostEstimate the .p3b plan-choice path already
            # computed for `best` on a plan-match HIT
            # (`_plan_choice_info["predicted_cost_usd"]`/`["basis"]` —
            # `best` IS `_chosen`, so these values are exactly what a
            # fresh `estimate_plan_cost(best.plan_json, ...)` call would
            # produce); price the grown plan fresh, with the same price
            # table, on a plan-miss / force_dynamic run, where no such
            # candidate estimate exists.
            if _plan_choice_info is not None:
                _plan_estimate_usd = _plan_choice_info.get("predicted_cost_usd")
                _plan_estimate_basis = _plan_choice_info.get("basis") or ""
            else:
                try:
                    with _t2_ctx() as _cost_db2:
                        _price_table2 = get_cached_price_table(_cost_db2.telemetry)
                    _estimate = estimate_plan_cost(best.plan_json, _price_table2)
                except Exception:  # noqa: BLE001 — boundary catch; degrade to "no estimate", must not crash caller
                    _estimate = PlanCostEstimate(usd=None, ms=None, basis="pricing-unavailable")
                _plan_estimate_usd = _estimate.usd
                _plan_estimate_basis = _estimate.basis
            # RDR-196 .p3c round 2 (Sam's decision on the critic's
            # CRITICAL, T2 p3c-critique-2026-08-21): DEMOTED from a hard
            # refusal to a WARNING. The bundle-dominant step-shape
            # estimator has NO per-plan discriminating power in the live
            # population — a same-day 20-run measurement found all 4
            # live plans predicting the IDENTICAL $0.90433 (bundle
            # pricing always resolves to the generate@opus ceiling),
            # median actual/predicted ratio 0.805 (systematic
            # over-estimate), worst overestimate 11.2x, only 3/20
            # under-predicted. A hard refusal at that number is a STEP
            # FUNCTION at $0.90433, not a per-plan judgement: under the
            # derived default cap (1.0530) it never fires; under any
            # caller-supplied budget_usd near real median spend
            # (~$0.70-0.80) it refused every real run deterministically
            # and wrongly, including runs that would truly land under
            # the cap. The mid-run stop-line below is UNCHANGED and
            # remains the real enforcement mechanism — it sums MEASURED
            # StepRecord costs rather than a step-shape guess, which is
            # exactly why it survives review while this predicted-cost
            # half does not. Re-enabling a hard refusal here is gated on
            # the estimator gaining real per-plan discrimination.
            if _plan_estimate_usd is not None and _plan_estimate_usd > _budget_remaining_usd:
                # code-review Medium 1 (T2 p3c-code-review-2026-08-21):
                # name BOTH the actual comparison operand (remaining,
                # which is what "exceeds" is checked against) and the
                # full original cap, plus the already-spent amount when
                # nonzero — the prior text labelled the full cap as
                # "the budget cap" while comparing against the smaller
                # remaining, so the headline clause could read as
                # numerically false whenever D5's planner spend was
                # nonzero (e.g. remaining $0.45 < estimate $0.50 <
                # full cap $1.0530 — "$0.50 exceeds $1.0530" is false
                # as literally stated).
                _emit_budget_warning(
                    "over-cap",
                    f"estimated plan cost ${_plan_estimate_usd:.4f} exceeds the "
                    f"remaining budget ${_budget_remaining_usd:.4f} "
                    f"(full cap ${effective_budget_usd:.4f}"
                    + (
                        f", ${_planner_spent_usd:.4f} already spent before plan execution)"
                        if _planner_spent_usd > 0 else ")"
                    )
                    + f". basis: {_plan_estimate_basis}. Running anyway — the "
                    "step-shape estimate has no per-plan discriminating power "
                    "in the live population; the mid-run cost check (measured, "
                    "not predicted) is the real enforcement.",
                )
            if _plan_estimate_usd is None:
                # D4: no-history / unpriceable plan — WARN, never refuse.
                _emit_budget_warning(
                    "no-estimate",
                    f"({_plan_estimate_basis}) — running without a pre-flight "
                    "cost check",
                )

    # ── Step 2: single-step guard ────────────────────────────────────────
    if plan_class == "single_query":
        _log.info("nx_answer_single_step_guard", plan_id=best.plan_id, confidence=conf_str)
        try:
            from nexus.db.limits import MAX_QUERY_RESULTS  # noqa: PLC0415 — deferred; matches this module's convention
            from nexus.plans.runner import (  # noqa: PLC0415 — deferred; matches this module's convention
                PlanRunUnresolvedVarError,
                resolve_step_bindings,
            )

            plan = json.loads(best.plan_json)
            raw_args = plan["steps"][0].get("args", {})
            # nexus-nyry9.5 (RDR-196 .r5): raw_args holds UNRESOLVED
            # ``$var`` placeholders for any template-authored plan --
            # every builtin plan, including the two single-query seeds
            # (conexus/plans/builtin/document-discovery.yml,
            # corpus-coverage-check.yml), declares
            # ``args: {question: $question, corpus: $corpus, limit:
            # $limit}``. The pre-fix code read this dict with plain
            # ``.get("question", question)`` -- since the KEY "question"
            # IS present, the fallback never fired, so the fast path
            # silently queried for the literal string "$question"
            # against corpus "$corpus" instead of the caller's real
            # text.
            #
            # nexus-nyry9.5 review-fix (code-review SIGNIFICANT, T2
            # review-nexus-nyry9.5): a required binding this plan
            # declares as TYPED (e.g. ``content_type``, ``author`` --
            # see ``TYPED_FILTER_BINDINGS``) that the caller did not
            # supply is refused rather than guessed -- explicit handling
            # here mirrors Step 4's dedicated
            # ``except PlanBindingUnsatisfiableError`` below (step_count=0,
            # logged) instead of falling through to the generic
            # ``except Exception`` further down (which used to record
            # step_count=1 and no telemetry event for this case, wrongly
            # implying one step actually ran).
            try:
                step_bindings = _autoalias_bindings(
                    required=best.required_bindings,
                    run_bindings=dict(_caller_run),
                    defaults=best.default_bindings or {},
                    question=question,
                    plan_id=best.plan_id,
                    plan_name=best.name or "",
                    plan_json=best.plan_json,
                )
            except PlanBindingUnsatisfiableError as exc:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                _log.warning(
                    "nx_answer_plan_binding_unsatisfiable",
                    plan_id=best.plan_id, plan_name=best.name,
                    binding=exc.binding, confidence=best.confidence,
                )
                try:
                    with _t2_ctx() as db:
                        _nx_answer_record_run(
                            db.telemetry, question=question, plan_id=best.plan_id,
                            matched_confidence=best.confidence, step_count=0,
                            final_text=f"Error: {exc}", step_records=[],
                            duration_ms=elapsed_ms, trace=trace,
                        )
                except Exception:  # noqa: BLE001 — graceful degradation; the refusal must still surface
                    pass
                _nx_answer_record_outcome(best.plan_id, success=False)
                return _result(str(exc), plan_id=best.plan_id, step_count=0)

            # Resolve the same way plan_run (Step 4 below) would: caller
            # bindings autoaliased from the question, merged over the
            # plan's own default_bindings with defaults first and caller
            # winning. Shared with plan_run via ``resolve_step_bindings``
            # (nexus-nyry9.5 review-fix, code-review SIGNIFICANT) so this
            # formula lives in exactly one place instead of two
            # independently-maintained copies.
            try:
                step_args = resolve_step_bindings(
                    raw_args,
                    default_bindings=best.default_bindings or {},
                    caller_bindings=step_bindings,
                )
            except PlanRunUnresolvedVarError as exc:
                # nexus-pucte: this fast path bypasses plan_run (and its
                # pre-dispatch _validate_var_refs check) entirely by
                # design; resolve_step_bindings now runs the same check
                # itself. Mirrors the PlanBindingUnsatisfiableError
                # handling immediately above — refuse loudly rather than
                # let a $var with no default and no caller value reach
                # query() as a literal token string.
                elapsed_ms = int((time.monotonic() - start) * 1000)
                _log.warning(
                    "nx_answer_single_query_unresolved_var",
                    plan_id=best.plan_id, plan_name=best.name,
                    var_name=exc.var_name, confidence=best.confidence,
                )
                try:
                    with _t2_ctx() as db:
                        _nx_answer_record_run(
                            db.telemetry, question=question, plan_id=best.plan_id,
                            matched_confidence=best.confidence, step_count=0,
                            final_text=f"Error: {exc}", step_records=[],
                            duration_ms=elapsed_ms, trace=trace,
                        )
                except Exception:  # noqa: BLE001 — graceful degradation; the refusal must still surface
                    pass
                _nx_answer_record_outcome(best.plan_id, success=False)
                return _result(str(exc), plan_id=best.plan_id, step_count=0)
            q = step_args.get("question", question)
            corpus = step_args.get("corpus", "knowledge")
            try:
                limit = int(step_args.get("limit", 10))
            except (TypeError, ValueError):
                limit = 10
            # nexus-nyry9.5 review-fix (code-review IMPORTANT, T2
            # review-nexus-nyry9.5): clamp to the same ceiling every
            # other paging/query-result path in this codebase honours
            # (AGENTS.md § External service limits) -- a plan-supplied
            # or caller-influenced ``limit`` must not bypass it just
            # because this path skips ``plan_run``.
            limit = max(1, min(limit, MAX_QUERY_RESULTS))
            # RDR-086 review #4: exactly one ``query()`` call — the
            # previous structured path re-ran non-structured for
            # ``result_text`` (doubling the T3 round-trip) even though
            # the structured envelope already contains enough to
            # synthesize a result summary.
            if structured:
                q_struct = query(question=q, corpus=corpus, limit=limit, structured=True)
                chunks: list[dict] = []
                if isinstance(q_struct, dict):
                    ids = q_struct.get("ids", [])
                    colls_list = q_struct.get("chunk_collections") or (
                        q_struct.get("collections") or []
                    )
                    hashes = q_struct.get("chunk_text_hash", [])
                    dists = q_struct.get("distances", [])
                    default_coll = colls_list[0] if colls_list else ""
                    for i, cid in enumerate(ids):
                        chunks.append({
                            "id": cid,
                            "chash": hashes[i] if i < len(hashes) else "",
                            # Per-chunk alignment when chunk_collections is
                            # available (Phase 3 surface fix); otherwise
                            # fall back to the first dedup'd collection.
                            "collection": (
                                colls_list[i]
                                if i < len(colls_list)
                                else default_coll
                            ),
                            "distance": dists[i] if i < len(dists) else None,
                        })
                # Synthesize a compact human-readable summary from the
                # envelope — no second query() required.
                if chunks:
                    lines = [
                        f"Found {len(chunks)} result"
                        f"{'s' if len(chunks) != 1 else ''} for {q!r}:",
                    ]
                    for ch in chunks[:5]:
                        lines.append(
                            f"  - {ch['id']} in {ch['collection']} "
                            f"(distance={ch['distance']:.3f})"
                            if ch["distance"] is not None
                            else f"  - {ch['id']} in {ch['collection']}"
                        )
                    if len(chunks) > 5:
                        lines.append(f"  ... and {len(chunks) - 5} more")
                    result_text = "\n".join(lines)
                else:
                    result_text = "No results."
            else:
                result_text = query(question=q, corpus=corpus, limit=limit)
                chunks = []

            elapsed_ms = int((time.monotonic() - start) * 1000)
            try:
                with _t2_ctx() as db:
                    _nx_answer_record_run(
                        db.telemetry, question=question, plan_id=best.plan_id,
                        matched_confidence=best.confidence, step_count=1,
                        final_text=str(result_text)[:2000], step_records=[],
                        duration_ms=elapsed_ms, trace=trace,
                    )
            except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
                pass
            # nexus-yg49g: outcome must reflect whether the run ANSWERED, not
            # merely that it did not raise. This branch can hand back the literal
            # string "No results." (built a dozen lines above), and recording that
            # as a success is what let a plan be 100%-success and 0%-useful.
            _nx_answer_record_outcome(
                best.plan_id,
                success=not _nx_answer_text_is_empty(str(result_text)),
            )
            return _result(
                str(result_text),
                plan_id=best.plan_id,
                step_count=1,
                chunks=chunks,
            )
        except Exception as exc:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
            elapsed_ms = int((time.monotonic() - start) * 1000)
            try:
                with _t2_ctx() as db:
                    _nx_answer_record_run(
                        db.telemetry, question=question, plan_id=best.plan_id,
                        matched_confidence=best.confidence, step_count=1,
                        final_text=f"Error: {exc}", step_records=[],
                        duration_ms=elapsed_ms, trace=trace,
                    )
            except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
                pass
            _nx_answer_record_outcome(best.plan_id, success=False)
            return _result(
                f"Error in single-step query: {exc}",
                plan_id=best.plan_id,
                step_count=1,
            )

    # ── Step 3: seed link-context ────────────────────────────────────────
    try:
        scratch(
            action="put",
            content=json.dumps({"question": question, "scope": scope, "plan_id": best.plan_id}),
            tags="link-context",
        )
    except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
        pass

    # ── Step 4: execute plan ─────────────────────────────────────────────
    # nexus-zs1d Phase 1: propagate caller-supplied scope as the
    # ``_nx_scope`` binding so retrieval steps in library-matched plans
    # honour the caller's corpus intent. Plans that pin their own corpus
    # still win; this only fills in the gap when a plan is agnostic.
    run_bindings = dict(_caller_run)

    # Auto-alias the question text into any required binding the plan
    # declares but the caller didn't pre-supply. This mirrors what the
    # inline-planner fallback already does — its constructed plans get
    # every binding filled from the question text. Without this, any
    # library plan with ``required_bindings: [concept]`` (or area,
    # topic, etc.) failed at dispatch with ``missing required
    # bindings: ['concept']`` even though ``$intent`` carried the
    # equivalent value. Skills that pre-extract entities (e.g.,
    # find-by-author with ``$author``) bypass this path by calling
    # ``plan_run`` directly with explicit bindings.
    #
    # A TYPED binding (content_type, author, ...) is refused rather than
    # guessed — see _autoalias_bindings. That is a loud failure by
    # design: the honest signal is "this plan should not have been
    # selected for this question", and inventing a value to keep the run
    # alive is what produced a zero-result answer that still recorded as
    # a success (nexus-0yrjr, nexus-yg49g).
    defaults = best.default_bindings or {}
    try:
        run_bindings = _autoalias_bindings(
            required=best.required_bindings,
            run_bindings=run_bindings,
            defaults=defaults,
            question=question,
            plan_id=best.plan_id,
            plan_name=best.name or "",
            plan_json=best.plan_json,
        )
    except PlanBindingUnsatisfiableError as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _log.warning(
            "nx_answer_plan_binding_unsatisfiable",
            plan_id=best.plan_id, plan_name=best.name,
            binding=exc.binding, confidence=best.confidence,
        )
        try:
            with _t2_ctx() as db:
                _nx_answer_record_run(
                    db.telemetry, question=question, plan_id=best.plan_id,
                    matched_confidence=best.confidence, step_count=0,
                    final_text=f"Error: {exc}", step_records=[],
                    duration_ms=elapsed_ms, trace=trace,
                )
        except Exception:  # noqa: BLE001 — graceful degradation; the refusal must still surface
            pass
        # Counts as a failure so a chronically mis-matched plan accrues a
        # real failure rate and stops clearing promote.py's threshold.
        _nx_answer_record_outcome(best.plan_id, success=False)
        return _result(str(exc), plan_id=best.plan_id, step_count=0)

    # nexus-nyry9.5 (RDR-196 .r5 review-fix): the retrieval-only
    # deadline exemption that used to apply here was DELETED along with
    # the bucket itself (see ``_nx_answer_classify_plan``) — no real
    # matched plan was ever shown to land in it, so every plan execution
    # is now budget-bound the same way, whether or not it actually
    # contains an operator step.
    #
    # Only pass ``deadline=`` when it's actually set. Several existing
    # tests (and possibly external callers) patch ``plan_run`` with a
    # fixed ``(match, bindings)`` signature; forwarding an unconditional
    # ``deadline=None`` kwarg would TypeError against those and silently
    # degrade to "Error during plan execution" — exactly the "changed
    # default behavior" this parameter must not cause.
    _plan_run_kwargs: dict[str, Any] = {}
    if deadline is not None:
        _plan_run_kwargs["deadline"] = deadline
    # RDR-196 .p3c (nexus-nyry9.21) D5: same "only pass when set"
    # discipline as deadline above -- None whenever enforcement is
    # inactive for this call, so a caller/test double with a fixed
    # (match, bindings) signature and no budget_usd/enforcement in play
    # sees byte-identical behavior.
    if _budget_remaining_usd is not None:
        _plan_run_kwargs["budget_usd_remaining"] = _budget_remaining_usd

    try:
        result = await _plan_run(best, run_bindings, **_plan_run_kwargs)
    except Exception as exc:  # noqa: BLE001 — boundary catch; failure surfaced via log.warning, must not crash caller
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _log.error("nx_answer_plan_run_error", plan_id=best.plan_id, error=str(exc))
        # RDR-196 .p1d critique fold (T2 [23092], nexus-nyry9.11): plan_run
        # now attaches the step_records completed BEFORE the raise to the
        # exception instance itself (runner.py's outer try/except) —
        # extract them here instead of the old hardcoded ``[]`` so a
        # FAILED run still records real per-step telemetry. Defensive
        # isinstance check, not a bare getattr default: an exception this
        # module raises directly (never touched runner.py's attach point)
        # correctly has no such attribute, and a MagicMock test double's
        # unconfigured attribute access is itself a truthy non-list
        # MagicMock — same landmine already documented on
        # ``_result_step_records`` above.
        _exc_step_records = getattr(exc, "step_records", None)
        if not isinstance(_exc_step_records, list):
            _exc_step_records = []
        try:
            with _t2_ctx() as db:
                _nx_answer_record_run(
                    db.telemetry, question=question, plan_id=best.plan_id,
                    matched_confidence=best.confidence,
                    step_count=len(_exc_step_records),
                    final_text=f"Error: {exc}", step_records=_exc_step_records,
                    duration_ms=elapsed_ms, trace=trace,
                )
        except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
            pass
        _nx_answer_record_outcome(best.plan_id, success=False)
        return _result(
            f"Error during plan execution: {exc}",
            plan_id=best.plan_id,
            step_count=len(_exc_step_records),
            step_records=_exc_step_records,
        )
    # nexus-yg49g: the success record USED to be here — before final_text is
    # even extracted (below) and ~60 lines before the empty-retrieval guard that
    # already knows the run produced nothing. It could not have been right: at
    # this point the outcome is literally not yet computed. Moved to the two
    # branches that DO know, further down.

    # RDR-196 .p3c round 2 (critic Significant 1, T2 p3c-critique-2026-08-21):
    # unknown-cost blind-spot signal. "Never fabricate a 0 for an unknown
    # cost_usd" is correct (StepRecord's own doctrine), but its corollary
    # is silent: the mid-run running sum cannot accumulate for a step
    # whose real cost was never captured, so `budget_usd_remaining`'s
    # check may have been blind for part of this run. Fail loud, per the
    # repo's own no-silent-fallbacks-for-correctness rule — once per
    # RUN, not once per step, through the single warning emitter. Only
    # meaningful when enforcement was actually active for this call
    # (`_budget_remaining_usd is not None`); an unenforced run was never
    # promising a cost guarantee to begin with. Checked BEFORE the
    # exhaustion-marker early-return below so the signal still surfaces
    # even on a run that also hit the exhaustion marker — the two are
    # independent observations (a run can be BOTH "stopped early on
    # measured cost" and "some earlier step's cost was never measured").
    if _budget_remaining_usd is not None:
        _unknown_cost_step_records = getattr(result, "step_records", None)
        if not isinstance(_unknown_cost_step_records, list):
            _unknown_cost_step_records = []
        _unknown_cost_count = sum(
            1 for r in _unknown_cost_step_records if getattr(r, "cost_usd", None) is None
        )
        if _unknown_cost_count:
            _emit_budget_warning(
                "unknown-cost",
                f"{_unknown_cost_count} of {len(_unknown_cost_step_records)} "
                "executed step(s) reported no cost_usd — the running spend "
                "could not include them, so the budget_usd_remaining check "
                "may not have caught an overrun for this run",
            )

    # ── nexus-h33x8.6 a4: budget-exhausted partial-result path ────────────
    # Runs BEFORE Step 5's normal extraction — a budget cutoff bypasses
    # the usual final-step-text logic and the empty-retrieval guard
    # entirely; both would either mis-extract the timeout sentinel or
    # wrongly report "no evidence" when evidence WAS retrieved. Shared
    # emitter — see ``_budget_exhausted_response`` above (nexus-nyry9.2,
    # RDR-196 .r2: this is the SAME function the pre-Step-2 check calls).
    _budget_response = _budget_exhausted_response(result)
    if _budget_response is not None:
        return _budget_response

    # nexus-nyry9.10 (RDR-196 .p1d): computed once here for both Step 5's
    # empty-retrieval-guard record and Step 6's success record below.
    # Defensive isinstance check, not a bare ``getattr(result,
    # "step_records", [])`` — several existing tests patch ``plan_run``
    # with a bare ``MagicMock()`` carrying only ``.steps`` (never
    # ``.step_records``), and a MagicMock's unconfigured attribute access
    # auto-creates a truthy, non-list MagicMock rather than raising, which
    # would defeat a plain ``getattr`` default.
    _result_step_records = getattr(result, "step_records", None)
    if not isinstance(_result_step_records, list):
        _result_step_records = []

    # ── Step 5: extract final answer ─────────────────────────────────────
    elapsed_ms = int((time.monotonic() - start) * 1000)
    final_step = result.steps[-1] if result.steps else {}
    # GH #555: include ``output`` (the operator_generate schema's
    # terminal field) and ``comparison`` (operator_compare) in the
    # text-key search so a generate-terminal plan returns its raw
    # prose instead of the JSON-encoded envelope. Pre-fix the search
    # ran ``("text", "summary", "answer")`` only; ``operator_generate``
    # emits ``{"output": "..."}`` so the search missed and fell
    # through to ``json.dumps(final_step)``, producing a wrapped
    # ``'{"output": "..."}'`` final_text. Plus on extract -> generate
    # bundles claude -p sometimes emits ``{"output": "<JSON-encoded
    # {output: prose}>"}`` (the bundle prompt confuses the model into
    # treating the prior step's envelope as its input string), so an
    # additional one-level recursive unwrap on a string-valued ``output``
    # that itself parses to ``{"output": ...}`` recovers the prose.
    text_key = next(
        (k for k in ("text", "summary", "answer", "output", "comparison")
         if k in final_step),
        None,
    )
    final_text = str(final_step.get(text_key, "")) if text_key else json.dumps(final_step)
    final_text = _maybe_unwrap_output_envelope(final_text)

    # RDR-086 Phase 3.3: harvest chunk refs from retrieval-op steps so the
    # envelope's ``chunks`` list carries id+chash+collection for every
    # retrieved chunk, ordered by final-step relevance. Review #7: prefer
    # the per-result ``chunk_collections`` list (Phase 3 fix) so every
    # chunk is tagged with its actual origin — not the first dedup'd
    # collection.
    envelope_chunks: list[dict] = []
    if structured:
        for step_out in result.steps:
            if not isinstance(step_out, dict):
                continue
            ids = step_out.get("ids")
            if not isinstance(ids, list) or not ids:
                continue
            hashes = step_out.get("chunk_text_hash", []) or []
            per_chunk_colls = step_out.get("chunk_collections") or []
            dedup_colls = step_out.get("collections", []) or []
            dists = step_out.get("distances", []) or []
            default_coll = dedup_colls[0] if dedup_colls else ""
            for i, cid in enumerate(ids):
                if i < len(per_chunk_colls):
                    coll = per_chunk_colls[i]
                else:
                    coll = default_coll
                envelope_chunks.append({
                    "id": cid,
                    "chash": hashes[i] if i < len(hashes) else "",
                    "collection": coll,
                    "distance": dists[i] if i < len(dists) else None,
                })

    # RDR-137 followup (nexus-n1908): empty-retrieval guard. If the plan
    # had retrieval steps but they collectively returned zero evidence,
    # the synthesized final_text is at risk of being a confident
    # off-topic answer built from the operator subprocess's ambient
    # SessionStart hook context. Return an explicit no-match message
    # (naming the scope so a malformed/over-narrow scope is visible)
    # instead of the misleading synthesis. Fires BEFORE plan-grow so we
    # never persist an ad-hoc plan that produced no evidence.
    if _nx_answer_is_empty_retrieval(result.steps):
        _log.info(
            "nx_answer_empty_retrieval_guard",
            plan_id=best.plan_id,
            scope=scope or "(unscoped)",
        )
        # nexus-yg49g: counts as a FAILURE, not a success and not nothing.
        #
        # The store is strictly binary (success_count / failure_count; a third
        # "empty" column needs a Liquibase changeset + an engine tag), so the
        # choice is forced. Recording NOTHING was considered and rejected:
        # plans/promote.py gates on success/(success+failure) >= 0.80, so a plan
        # that always returns empty would keep a perfect 1.0 rate and still be
        # promoted — precisely the "preferentially promote plans that return
        # nothing quickly" outcome this bead was filed about.
        #
        # The lost distinction (errored vs found-nothing) is recoverable from
        # telemetry: _nx_answer_record_run below stores the final_text, and the
        # structured event above marks this branch specifically.
        _nx_answer_record_outcome(best.plan_id, success=False)
        no_match = (
            f"No matching evidence found for {question!r}"
            + (f" in scope {scope!r}" if scope else "")
            + ". The plan's retrieval steps returned zero results — "
            "rephrase the question, correct/widen the scope, or use "
            "search/query directly."
        )
        try:
            with _t2_ctx() as db:
                _nx_answer_record_run(
                    db.telemetry, question=question, plan_id=best.plan_id,
                    matched_confidence=best.confidence,
                    step_count=len(result.steps),
                    final_text=no_match[:2000], step_records=_result_step_records,
                    duration_ms=elapsed_ms, trace=trace,
                )
        except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
            pass
        return _result(
            no_match, plan_id=best.plan_id,
            step_count=len(result.steps), chunks=[],
            step_records=_result_step_records,
        )

    # nexus-yg49g: SUCCESS is recorded HERE — past the empty-retrieval guard,
    # with final_text extracted and an actual answer in hand. This is the only
    # point in the plan path where "the plan answered the question" is a
    # statement the code can make truthfully.
    _nx_answer_record_outcome(best.plan_id, success=True)

    _log.info(
        "nx_answer_complete",
        plan_id=best.plan_id,
        confidence=conf_str,
        step_count=len(result.steps),
        duration_ms=elapsed_ms,
    )

    # RDR-084: Save successful ad-hoc plans so the plan library compounds
    # with usage. scope=personal keeps growth isolated to the caller (the
    # project/global scopes are reached only via /conexus:plan-promote). TTL is
    # config-driven; 30-day default. Best-effort — a save failure never
    # affects the user's answer, and the T1 cache upsert is a separate
    # best-effort step inside the same guard.
    if best.plan_id == 0:
        ttl_days = _load_ad_hoc_ttl()
        if ttl_days > 0:
            try:
                from pathlib import Path as _Path  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site

                project_name = _Path.cwd().name
                # RDR-091 critic follow-up (nexus-dfok): anchor the grown
                # plan to the caller's scope. _infer_scope_tags cannot see
                # the runtime corpus injection from ``_nx_scope`` because
                # it only appears in bindings, not plan_json. Passing
                # scope_tags=scope explicitly captures the retrieval space
                # that produced this plan.
                # RDR-092 Phase 0b: R6 three-tier verb cascade populates
                # verb / name / dimensions so the grown row participates in
                # the dimensional identity index instead of landing as a
                # NULL-dimension legacy ghost.
                from nexus.plans.schema import canonical_dimensions_json  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)

                grown_verb = _infer_grown_plan_verb(
                    caller_dimensions=dimensions,
                    plan_json=best.plan_json,
                )
                grown_name = _infer_grown_plan_name(question)
                grown_dimensions = canonical_dimensions_json({
                    "verb": grown_verb,
                    "scope": "personal",
                    "strategy": grown_name,
                })
                # nexus-93cc6 D2: generalize the match text BEFORE the save —
                # a grown plan whose match_text is only its verbatim question
                # matches paraphrases at 62.5%; a generalized description +
                # exemplars measured +17pp with zero precision cost (T2
                # [23880]/[23881]). Fail-soft by contract: any generalizer
                # failure yields None and the save proceeds exactly as today.
                grown_match_description = await _generalize_grown_match_description(
                    question=question, plan_json=best.plan_json,
                )
                # nexus-j5geq: route through daemon (eliminates second WAL writer).
                _grow_scope_tags = scope or None
                _grow_plan_json = best.plan_json
                _grow_project = project_name
                def _do_grow(db):
                    gid = db.plans.save_plan(
                        query=question,
                        plan_json=_grow_plan_json,
                        outcome="success",
                        tags="ad-hoc,grown",
                        project=_grow_project,
                        ttl=ttl_days,
                        scope="personal",
                        scope_tags=_grow_scope_tags,
                        verb=grown_verb,
                        name=grown_name,
                        dimensions=grown_dimensions,
                        match_description=grown_match_description,
                    )
                    # Feed the new plan into the T1 cosine cache so the next
                    # paraphrase can match without a SessionStart re-populate.
                    try:
                        cache = get_t1_plan_cache()
                        if cache is not None:
                            row = db.plans.get_plan(gid)
                            if row:
                                cache.upsert(row)
                    except Exception:  # noqa: BLE001 — best-effort path; failure logged via log.debug, must not crash caller
                        _log.debug("plan_grow_cache_upsert_failed", exc_info=True)
                    return gid
                grown_id = _t2_index_write(_do_grow)
                _log.info(
                    "plan_grow_saved",
                    plan_id=grown_id,
                    ttl_days=ttl_days,
                    project=project_name,
                )
            except Exception as exc:  # noqa: BLE001 — boundary catch; failure surfaced via log.warning, must not crash caller
                _log.warning("plan_grow_save_failed", error=str(exc))

    # ── Step 6: record run ───────────────────────────────────────────────
    try:
        with _t2_ctx() as db:
            _nx_answer_record_run(
                db.telemetry, question=question, plan_id=best.plan_id,
                matched_confidence=best.confidence, step_count=len(result.steps),
                final_text=final_text[:2000], step_records=_result_step_records,
                duration_ms=elapsed_ms, trace=trace,
            )
    except Exception:  # noqa: BLE001 — graceful degradation; fallback value used, must not crash caller
        pass

    return _result(
        final_text,
        plan_id=best.plan_id,
        step_count=len(result.steps),
        chunks=envelope_chunks if structured else None,
        step_records=_result_step_records,
    )


@mcp.tool(
    title="Consolidate Knowledge Topic",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
async def nx_tidy(
    topic: str,
    collection: str = "knowledge",
    timeout: float = 600.0,
) -> str:
    """Consolidate knowledge entries on *topic* via claude -p. RDR-080 P3.

    Replaces the ``knowledge-tidier`` agent. nexus-mawqw / Fix A:
    retrieves and hydrates matching entries **server-side** (in-process,
    a single semantic ``search`` pass), inlines them into the prompt, then
    dispatches a **tool-free** ``claude -p`` to identify duplicates,
    contradictions, and outdated entries. Read-only: it reports a
    consolidated summary plus suggested actions but performs no writes
    (the old prompt claimed ``store_put`` access the child never had).

    Retrieval scope is one semantic-search pass capped at
    ``_TIDY_MAX_ENTRIES`` chunks; it does not expand the query, chase
    related terms, or deduplicate chunks to documents. Best suited to
    note-shaped collections (≈one chunk per entry). When the cap is hit
    the returned summary says so explicitly (no silent truncation).

    Args:
        topic: The knowledge topic to consolidate (e.g. "chromadb quotas").
        collection: T3 collection to search (default: knowledge).
        timeout: Subprocess timeout in seconds. Default 600s (10 min) —
            consolidation on a large corpus does heavy LLM-only reasoning
            over the inlined entries; 120s was hitting the timeout
            routinely on real workloads. Caller can override lower for
            small topics.

    Returns:
        Consolidated summary as a human-readable string.
    """
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — rare/branch-local path; operator dispatch deferred to call time

    schema = {
        "type": "object",
        "required": ["summary", "actions"],
        "properties": {
            "summary": {"type": "string"},
            "actions": {"type": "array", "items": {"type": "object"}},
        },
    }
    # nexus-mawqw / Fix A: pre-fetch the entries server-side and inline
    # them. The child claude -p then consolidates LLM-only with NO tools,
    # so it can never trip the post-CC-2.1.162 MCP-server-approval gate.
    entries_block, n_entries, prefetch_reason, genuine_miss = _tidy_prefetch(
        topic, collection,
    )
    capped = n_entries >= _TIDY_MAX_ENTRIES
    if capped:
        import structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path
        structlog.get_logger().info(
            "tidy_prefetch_capped",
            topic=topic, collection=collection, cap=_TIDY_MAX_ENTRIES,
        )
    if n_entries:
        entries_section = (
            f"\n\nHere are the {n_entries} retrieved entries to consolidate. "
            "Work ONLY from these entries; do NOT call any tools:\n\n"
            f"{entries_block}"
        )
    elif genuine_miss:
        entries_section = (
            "\n\nNo matching entries were retrieved from the collection "
            f"({prefetch_reason}). Report that there is nothing to consolidate. "
            "Do NOT call any tools."
        )
    else:
        # nexus-1obui: retrieval did not merely come back empty, it came back
        # BROKEN (threshold drop, search failure, hydration failure). Reporting
        # that as "nothing to consolidate" is the false-absence bug this bead
        # closes: an operator who trusts it writes the duplicate entry that
        # tidy exists to prevent. Say what actually happened instead.
        entries_section = (
            "\n\nRetrieval did NOT complete, so the collection's contents are "
            f"UNKNOWN — {prefetch_reason}. State plainly that this is a "
            "RETRIEVAL FAILURE and that no conclusion about duplicates or "
            "contradictions can be drawn. Do NOT say there is nothing to "
            "consolidate, and do NOT call any tools."
        )
    prompt = (
        "You are the `tidy` knowledge consolidation operator. You have NO "
        "tools available — all input is provided inline below. Identify "
        "duplicates, contradictions, and outdated entries among the provided "
        "entries, then produce a consolidated summary plus a list of "
        "suggested actions.\n\n"
        f"Consolidate knowledge entries about '{topic}' in collection "
        f"'{collection}'."
        f"{entries_section}"
    )
    payload = await claude_dispatch(prompt, schema, timeout=timeout, model=_pin_default_model(None))

    summary = payload.get("summary", "") if isinstance(payload, dict) else str(payload)
    actions = payload.get("actions", []) if isinstance(payload, dict) else []
    lines = [summary]
    if actions:
        lines.append(f"\n{len(actions)} action(s) suggested.")
    if capped:
        lines.append(
            f"\nNote: retrieval was capped at {_TIDY_MAX_ENTRIES} entries; "
            "the collection may contain additional matching content not seen "
            "by this consolidation pass."
        )
    if prefetch_reason and not genuine_miss:
        # Belt and braces: the child is instructed to report this, but the
        # tool's own return value must not depend on the model complying.
        lines.append(f"\nRETRIEVAL FAILURE — {prefetch_reason}")
    return "\n".join(lines)


@mcp.tool(
    title="Enrich Bead Context",
    annotations={"readOnlyHint": False, "destructiveHint": False},
    structured_output=False,
)
async def nx_enrich_beads(
    bead_description: str,
    context: str = "",
    timeout: float = 300.0,
) -> str:
    """Enrich a bead with execution context via claude -p. RDR-080 P3.

    Replaces the ``plan-enricher`` agent. Spawns a ``claude -p``
    subprocess that searches the codebase for relevant file paths,
    code patterns, constraints, and test commands, then returns enriched
    markdown.

    Args:
        bead_description: The bead's title and description to enrich.
        context: Optional additional context (e.g. audit findings).
        timeout: Subprocess timeout in seconds. Default 300s (5 min) —
            codebase exploration with file:line verification is
            multi-step; 120s was a frequent false-timeout on beads
            with broad scope. Requests below the 300s floor are
            clamped upward (see nexus-7sbf) to prevent agent
            overrides from re-introducing false-positive timeouts;
            a structlog warning is emitted when clamping occurs.

    Returns:
        Enriched bead markdown as a human-readable string.
    """
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — rare/branch-local path; operator dispatch deferred to call time

    timeout = _clamp_subagent_timeout(timeout, "nx_enrich_beads")
    mcp_servers, allowed_tools = _subprocess_tool_grant()

    schema = {
        "type": "object",
        "required": ["enriched_description"],
        "properties": {
            "enriched_description": {"type": "string"},
            "key_files": {"type": "array", "items": {"type": "string"}},
            "test_commands": {"type": "array", "items": {"type": "string"}},
            "constraints": {"type": "array", "items": {"type": "string"}},
        },
    }
    prompt = (
        "You are the `enrich` bead enrichment operator. You have access "
        "to nx MCP tools (search, query) for codebase exploration. "
        "Analyze the bead description, search the codebase for relevant "
        "files, symbols, and patterns, then produce enriched markdown with "
        "key_files, test_commands, and constraints.\n\n"
        f"Enrich this bead with execution context:\n\n{bead_description}"
    )
    if context:
        prompt += f"\n\nAdditional context:\n{context}"

    payload = await claude_dispatch(
        prompt, schema, timeout=timeout,
        mcp_servers=mcp_servers, allowed_tools=allowed_tools,
        model=_pin_default_model(None),
    )
    return (
        payload.get("enriched_description", "")
        if isinstance(payload, dict) else str(payload)
    )


@mcp.tool(
    title="Audit Plan Correctness",
    annotations={"readOnlyHint": True},
    structured_output=False,
)
async def nx_plan_audit(
    plan_json: str,
    context: str = "",
    timeout: float = 600.0,
    round_number: int = 1,
    budget_rounds: int = 0,
) -> str:
    """Audit a plan for correctness and codebase alignment via claude -p. RDR-080 P3.

    Replaces the ``plan-auditor`` agent. Spawns a ``claude -p``
    subprocess that validates the plan's file paths, dependencies,
    and assumptions against the current codebase state.

    **Findings are classified, and the loop terminates** (nexus-ll7zm).
    Every finding is either ``BLOCKS-PLANNING`` (the plan would cause
    someone to build the wrong thing, or cannot be executed as
    sequenced) or ``DISCOVER-AT-IMPLEMENTATION`` (real, but the first
    test run surfaces it). Only the first class can produce a NOT READY
    verdict. From round 3 onward NO verdict blocks: everything found is
    emitted as residuals to record and carry into implementation. The
    rules live in :mod:`nexus.plans.audit_rounds` and are enforced in
    Python, not requested in the prompt, because the failure this
    closes was a defect-finder being asked to judge when to stop finding
    defects. Origin: a five-round audit loop against the RDR-197 plan on
    2026-08-23, where rounds 3-5 refined hypothetical CI semantics for a
    release cut that had never been performed.

    Args:
        plan_json: The plan to audit (JSON string or free-text description).
        context: Optional additional context (e.g. RDR reference).
        timeout: Subprocess timeout in seconds. Default 600s (10 min) —
            a real plan audit verifies file:line pointers, cross-
            references research findings, walks dependency graphs;
            120s was hitting the timeout on RDR-086's real plan
            (11 beads, 5 phases). Requests below the 300s floor are
            clamped upward (see nexus-7sbf) to prevent planning
            agents from re-introducing false-positive timeouts via
            low overrides; a structlog warning is emitted when
            clamping occurs.
        round_number: Which audit round this is for THIS plan, counted by
            the caller. The tool is stateless — one ``claude -p``
            dispatch with no memory between invocations — so the round
            cannot be inferred here. Leaving it at 1 forever reproduces
            the unterminated loop, which is why the planner guidance
            requires passing it.
        budget_rounds: The effort budget the plan author declared up
            front from the feature's own stakes. 0 means unstated. A
            budget may only TIGHTEN the cap, never widen it: a one-day
            change can declare 1, but nothing can buy a third blocking
            round.

    Returns:
        Audit verdict as a human-readable string, with blocking findings
        and residuals shown separately and the recording instruction
        attached to the residuals.
    """
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — rare/branch-local path; operator dispatch deferred to call time
    from nexus.plans.audit_rounds import (  # noqa: PLC0415 — branch-local, keeps MCP import graph flat
        BLOCKS_PLANNING,
        CLASSIFICATION_PROMPT,
        VALID_CLASSIFICATIONS,
        render_audit_report,
        resolve_verdict,
        round_prompt,
    )

    timeout = _clamp_subagent_timeout(timeout, "nx_plan_audit")
    mcp_servers, allowed_tools = _subprocess_tool_grant()

    schema = {
        "type": "object",
        "required": ["verdict", "findings", "summary"],
        "properties": {
            "verdict": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["classification"],
                    "properties": {
                        "severity": {"type": "string"},
                        "title": {"type": "string"},
                        "classification": {
                            "type": "string",
                            "enum": list(VALID_CLASSIFICATIONS),
                        },
                    },
                },
            },
            "summary": {"type": "string"},
        },
    }
    prompt = (
        "You are the `audit` plan validation operator. You have access "
        "to nx MCP tools (search, query) for codebase verification. "
        "Parse the plan, verify file paths exist, check dependency ordering, "
        "identify gaps or incorrect assumptions, then emit a structured verdict.\n\n"
        f"{round_prompt(round_number, budget_rounds)}\n\n"
        f"{CLASSIFICATION_PROMPT}\n\n"
        f"Audit this plan for correctness and codebase alignment:\n\n{plan_json}"
    )
    if context:
        prompt += f"\n\nContext:\n{context}"

    payload = await claude_dispatch(
        prompt, schema, timeout=timeout,
        mcp_servers=mcp_servers, allowed_tools=allowed_tools,
        model=_pin_default_model(None),
    )
    if isinstance(payload, dict):
        findings = payload.get("findings", [])
        if not isinstance(findings, list):
            # A malformed container must never read as a clean audit
            # (e.g. the timeout-drain partial-JSON path can hand back a
            # non-list). Synthesize a blocking finding instead of
            # emptying: same conservative direction as the per-finding
            # unclassified default.
            findings = [
                {
                    "classification": BLOCKS_PLANNING,
                    "severity": "error",
                    "title": (
                        "audit payload malformed: findings was "
                        f"{type(findings).__name__}, not a list — re-run "
                        "the audit; do not treat this round as clean"
                    ),
                }
            ]
        verdict, blocking, residual, reason = resolve_verdict(
            findings, round_number=round_number, budget_rounds=budget_rounds
        )
        _log.info(
            "nx_plan_audit_verdict",
            verdict=verdict,
            round_number=round_number,
            budget_rounds=budget_rounds,
            blocking_count=len(blocking),
            residual_count=len(residual),
            cap_converted=bool(reason),
        )
        return render_audit_report(
            verdict,
            blocking,
            residual,
            summary=payload.get("summary", ""),
            reason=reason,
            round_number=round_number,
        )
    return str(payload)


@mcp.tool(
    title="Uninstall Nexus Daemon",
    annotations={"destructiveHint": True, "idempotentHint": True},
    structured_output=False,
)
def daemon_uninstall(confirm: bool = False, remove_data: bool = False) -> str:
    """Tear down the nexus storage stack's OS-level install (RDR-126 §4).

    Removes BOTH OS autostart units — the storage-service unit, and any
    legacy ``com.nexus.t2`` unit left behind by an install that predates the
    T2 daemon's retirement (nexus-i711w Stage 2 sub-stage B) — then stops the
    engine-service + Postgres stack and clears the first-run marker.

    The T2 daemon this tool was originally written for no longer exists, and
    nothing installs its unit any more; removing a stale one is why the
    removal half survives. There is likewise no first-run daemon auto-install
    to undo.

    Destructive: by default this only DESCRIBES what would be removed and
    asks you to re-call with ``confirm=true``. Nothing is removed until
    ``confirm=true``.

    Args:
        confirm: Must be true to actually remove anything. When false
            (default), returns a description of what would be removed.
        remove_data: When true (and ``confirm=true``), ALSO deletes the nexus
            CONFIG directory (``~/.config/nexus/``, or ``NEXUS_CONFIG_DIR``) —
            your notes, plans, and the catalog. Irreversible. It does NOT
            touch ``~/.local/share/nexus/``, which holds the Chroma store and
            the embedding-model cache; remove that separately for a full wipe.
    """
    from nexus.daemon import installer  # noqa: PLC0415 — circular-dep avoidance (lifecycle module imports mcp at top)

    report = installer.uninstall_daemon(confirm=confirm, remove_data=remove_data)
    lines = [report.message]
    if report.warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in report.warnings)
    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────


def _resolve_mode_diagnostics() -> dict[str, str | None]:
    """Best-effort snapshot of resolved T3 mode + embedder + config path (nexus-hixe9).

    Field evidence (Steve, 5.9.2 shakeout): the Desktop-GUI-spawned .mcpb
    resolves LOCAL mode (bge-768) against cloud (voyage-1024) collections,
    silently zeroing every vector search via the dimension-mismatch skip --
    yet a clean CLI simulation with the SAME credentials resolves cloud
    correctly. The GUI subprocess's runtime context (HOME, NEXUS_CONFIG_DIR,
    NX_LOCAL, which config.yml it actually reads) must differ from the CLI
    in a way not yet reproduced headlessly. This function does not fix that
    divergence -- it captures the fields needed to diagnose it, so a future
    live Desktop run leaves a durable on-disk trail in mcp.log instead of
    requiring the divergence to be reproduced first.

    nexus-smd1k (substantive-critic finding): ``is_local_mode()`` has FOUR
    decision branches since RDR-188 P3.1 (explicit ``NX_LOCAL``,
    ``service_url`` presence, ``pg_credentials`` presence, legacy
    chroma-key fallback) — per the bead's own CLI-vs-GUI divergence
    evidence, the likely root cause sits below branch 1.
    ``service_url_found``/``pg_credentials_found``/``chroma_key_found``
    evidence what each branch actually saw, as booleans ONLY — never the
    credential values. ``voyage_key_found`` is retained as INFORMATIONAL
    (nexus-9o6y2.16): since RDR-188 the voyage key has ZERO mode
    influence — it is engine-bootstrap/migration material; the boolean
    stays because a divergence report that shows it TRUE while mode
    flips would immediately falsify a suspected key-inference regression.

    Never raises: a diagnostic must not block MCP startup. On resolution
    failure returns ``{"mode": "unknown", "error": str(exc)}``.
    """
    try:
        from nexus.config import get_credential, is_local_mode, load_config, nexus_config_dir  # noqa: PLC0415 — deferred, rare/branch-local path

        local = is_local_mode()
        local_embedder = None
        if local:
            from nexus.db.local_ef import local_model_token  # noqa: PLC0415 — deferred, avoids a config.py<->local_ef.py import cycle

            local_embedder = local_model_token()
        return {
            "mode": "local" if local else "cloud",
            "local_embedder": local_embedder,
            "config_dir": str(nexus_config_dir()),
            "home": _os.environ.get("HOME", ""),
            "nx_local_env": _os.environ.get("NX_LOCAL", ""),
            "mode_record": str(
                (load_config().get("install", {}) or {}).get("mode", "") or ""
            ) or None,
            "service_url_found": bool(get_credential("service_url")),
            "pg_credentials_found": (nexus_config_dir() / "pg_credentials").is_file(),
            # Informational only — zero mode influence since RDR-188 P3.1.
            "voyage_key_found": bool(get_credential("voyage_api_key")),
        }
    except Exception as exc:  # noqa: BLE001 — diagnostic-only, must never block startup
        return {"mode": "unknown", "error": str(exc)}


def main():
    # nexus-4xgfy (critique 38b7db3d C1): the dominant post-upgrade path is
    # a Claude session spawning THIS process with no bare `nx` invocation in
    # between — the finish trigger must fire here too. Report-safe: the MCP
    # host never kills anything from its own startup; the transition stamp +
    # safe restarts are handled identically to the CLI trigger.
    try:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import
        from nexus.upgrade_finish import check_version_transition  # noqa: PLC0415 — deferred import

        _summary = check_version_transition(nexus_config_dir())
        if _summary:
            import structlog as _sl  # noqa: PLC0415 — deferred import

            _sl.get_logger(__name__).info("upgrade_finish", summary=_summary)
    except Exception:  # noqa: BLE001 — the trigger must never break server startup
        pass

    """Run the core MCP server on stdio transport.

    Lifecycle logging: emits ``mcp_server_starting``,
    ``mcp_server_stopping``, and ``mcp_server_crashed`` events to
    ``<config>/logs/mcp.log``. Without these, a silent crash leaves
    no on-disk trace and makes diagnosis dependent on Claude Code's
    captured stderr (which is not surfaced through any user-visible
    path).
    """
    import atexit  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site
    import os  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site
    import signal  # noqa: PLC0415 — rare/branch-local path; stdlib import deferred to call site

    import structlog  # noqa: PLC0415 — branch-local logging in fallback/best-effort path

    from nexus.logging_setup import configure_logging  # noqa: PLC0415 — deferred for startup cost (heavy nexus submodule, rare/branch-local)
    from nexus.mcp._first_run import (  # noqa: PLC0415 — circular-dep avoidance (mcp package import deferred)
        apply_embedder_notice,
        apply_stranded_notice,
    )
    from nexus.mcp_infra import check_version_compatibility  # noqa: PLC0415 — circular-dep avoidance (mcp package import deferred)

    configure_logging("mcp")
    log = structlog.get_logger("nexus.mcp.core")
    log.info(
        "mcp_server_starting",
        server="nx-mcp",
        transport="stdio",
        pid=os.getpid(),
        ppid=os.getppid(),
    )
    # nexus-hixe9: durable on-disk trail of resolved mode/embedder/config
    # path for diagnosing runtime-context divergence between a GUI-spawned
    # .mcpb and a CLI simulation with the same credentials. See
    # _resolve_mode_diagnostics' docstring for the field evidence.
    log.info("mcp_server_mode_resolved", **_resolve_mode_diagnostics())
    # nexus-gynt2: stranded-install detector. Disarmed (constant-check
    # no-op) on every migration-capable release; at N+1 an MCP host booting
    # over unmigrated pre-PG data logs the two-hop redirect at ERROR and
    # surfaces it through the server `instructions` channel (the LOUD
    # surface for MCP-only users — see apply_stranded_notice). Detection-
    # only: the server still serves (the doctor check and `nx init`
    # refusal carry the blocking surface).
    apply_stranded_notice(mcp)
    # NO first-run daemon install here: RDR-126 P2's ensure_installed_and_running
    # installed the T2 daemon's autostart unit and started it, and both retired
    # with the daemon (nexus-i711w Stage 2 sub-stage B). It had already stopped
    # firing on any service-backed install (RDR-176 Phase 1), so nothing that ran
    # here still runs.
    # NO first-run banner here either: the RDR-126 §3 banner (instructions
    # injection + content-prepend fallback) was DELETED at nexus-37jha — it had
    # been producer-less since the deletion above, so both calls that used to
    # sit here were a no-op every boot. See nexus/mcp/_first_run.py's module
    # docstring for the supersession record.
    # nexus-g6vb4 (GH #1414): staleness self-detection — an in-place
    # `uv tool upgrade` replaces site-packages under this live process;
    # the first deferred import then fails with an opaque ImportError.
    # The hook warn-logs once and decorates import-shaped failures with
    # a "stale MCP host — restart" note. Never refuses a call; best-effort,
    # never blocks boot.
    # nexus-utpuw.12 / design point 6: one readlink at spawn. A shim-launched
    # process is silent by construction (the shim execs the generation the
    # pointer names); this fires only when something BYPASSED the shim and
    # bound to a generation that is no longer current. Informational —
    # the tree it is running is intact and converges at the next spawn.
    from nexus.upgrade_finish import spawn_tripwire  # noqa: PLC0415 — deferred import

    spawn_tripwire()
    from nexus.mcp._stale_host import install_stale_host_hook  # noqa: PLC0415 — circular-dep avoidance (mcp package import deferred)

    install_stale_host_hook(mcp)
    # RDR-144 P5b: surface the embedder advisory to plugin/Desktop/Cowork-first
    # users who never run the Claude Code SessionStart hook. The MCP server
    # cannot print (stdout is JSON-RPC), so the notice rides the server
    # `instructions` delivered at initialize. Best-effort; never blocks boot.
    apply_embedder_notice(mcp)
    # The FastMCP lifespan finally is the design's primary cleanup
    # path; the signal handlers below are belt-and-braces for the
    # cases where the lifespan does not fire. Empirically, FastMCP's
    # stdio transport on macOS does NOT have anyio install a
    # SIGTERM handler that propagates cancellation through the
    # lifespan async finally (RDR-094 spike, 2026-04-25). Without
    # our explicit handlers, SIGTERM kills the process silently,
    # atexit does NOT run (Python only fires atexit on clean exit),
    # and chroma orphans. The watchdog covers it eventually but
    # the MCP-owned cleanup path simply doesn't run. So we install
    # the signal handlers for SIGTERM and SIGINT to call
    # _t1_shutdown directly. atexit stays as the third
    # belt-and-braces for clean exits via stdin EOF / SystemExit.
    # _t1_shutdown is idempotent so all three paths can fire
    # without double-cleaning.
    atexit.register(_t1_shutdown)
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)
    try:
        check_version_compatibility()
        mcp.run(transport="stdio")
    except (KeyboardInterrupt, SystemExit):
        log.info("mcp_server_stopping", server="nx-mcp", reason="signal")
        raise
    except BaseException as exc:
        log.exception(
            "mcp_server_crashed",
            server="nx-mcp",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        log.info("mcp_server_stopping", server="nx-mcp", reason="exit")


if __name__ == "__main__":
    main()
