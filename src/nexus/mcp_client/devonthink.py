# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Per-call DEVONthink MCP client over the shared core seam (RDR-139 Layer A).

The DEVONthink 4 built-in MCP server is an always-on localhost HTTP endpoint
(``http://localhost:8420/mcp``, no spawn/teardown). This module is the
**CLI-path-only** face: each :func:`dt_call` opens a session, runs one tool,
and closes it. Async callers (the aspect worker, any future daemon path) must
NOT use this module — they use the Layer A′ server face. :func:`dt_call`
enforces that contract with a running-loop guard.

Every helper is fail-soft: a missing/unreachable DT, an excluded record, or a
malformed result yields ``[]`` / ``None`` / ``False`` and a structured log
line, never an exception. The :func:`available` gate lets each layer fall back
to its tested pre-RDR-139 behaviour (Gap 0).
"""

from __future__ import annotations

import asyncio
import atexit
import os
import signal
import threading
from pathlib import Path
from typing import Any, TypedDict

import structlog

from nexus.config import load_config
from nexus.mcp_client.core import (
    MCPEndpoint,
    StdioEndpoint,
    call_tool,
    describe_exception,
    open_session,
    open_stdio_session,
)

log = structlog.get_logger(__name__)

#: Default DEVONthink built-in MCP endpoint (spike-verified, RDR-139).
DEFAULT_DT_MCP_URL = "http://localhost:8420/mcp"

#: Default location of the DEVONthink 4 MCP binary (nexus-fdk1x). This is the
#: same LoginItems copy the desktop app spawns at login with ``--stdio`` --
#: it is a real transport even when the app's HTTP listener
#: (``DEFAULT_DT_MCP_URL``) is off or unreachable. Overridable via config
#: ``devonthink.mcp.command``.
DEFAULT_DT_MCP_STDIO_PATH = (
    "/Applications/DEVONthink.app/Contents/Library/LoginItems/"
    "DEVONthink MCP.app/Contents/MacOS/DEVONthink MCP"
)

#: Module-level availability cache; ``None`` = not yet probed.
_AVAIL_CACHE: bool | None = None

#: What the most recent dt_call() tried, when it reached NO transport at
#: all (``None`` once any transport succeeds, or before the first call).
#: Read by ``nx dt index`` to build the loud unreachable message (nexus-fdk1x)
#: instead of the silent "0 ..." the fail-soft contract used to produce.
_LAST_UNREACHABLE_DETAIL: str | None = None


class Neighbour(TypedDict):
    """A DEVONthink record adjacent to a query record (similarity or link)."""

    uuid: str
    score: float
    name: str


def dt_mcp_url() -> str:
    """Resolve the DT MCP endpoint URL (config ``devonthink.mcp.url``, else default)."""
    cfg = load_config()
    url = (
        cfg.get("devonthink", {}).get("mcp", {}).get("url")
        if isinstance(cfg.get("devonthink"), dict)
        else None
    )
    return url or DEFAULT_DT_MCP_URL


def _dt_mcp_config() -> dict[str, Any]:
    cfg = load_config()
    section = cfg.get("devonthink")
    if not isinstance(section, dict):
        return {}
    mcp_cfg = section.get("mcp")
    return mcp_cfg if isinstance(mcp_cfg, dict) else {}


def dt_mcp_transport() -> str:
    """Resolve the configured transport policy (nexus-fdk1x).

    ``"http"`` -- HTTP only (no stdio fallback). ``"stdio"`` -- stdio only
    (skip the HTTP endpoint entirely). ``"auto"`` (default, and the value
    for anything else unrecognised) -- try HTTP first, fall back to stdio
    when the HTTP attempt fails. Config: ``devonthink.mcp.transport``.
    """
    transport = _dt_mcp_config().get("transport")
    return transport if transport in ("http", "stdio", "auto") else "auto"


def _dt_mcp_stdio_path() -> str:
    """The configured/default stdio binary path, regardless of whether it exists."""
    command = _dt_mcp_config().get("command")
    return command if isinstance(command, str) and command else DEFAULT_DT_MCP_STDIO_PATH


def dt_mcp_stdio_command() -> str | None:
    """Resolve the stdio MCP binary path, or ``None`` when it doesn't exist.

    A missing binary means the stdio transport is skipped outright (never
    spawned, never counted as "tried") -- the loud unreachable message
    still names the path that was checked via :func:`_dt_mcp_stdio_path`.
    """
    path = _dt_mcp_stdio_path()
    return path if Path(path).is_file() else None


def reset_availability_cache() -> None:
    """Clear the cached :func:`available` result (tests; long-lived processes)."""
    global _AVAIL_CACHE
    _AVAIL_CACHE = None


def last_unreachable_detail() -> str | None:
    """Detail of what the most recent :func:`dt_call` tried, or ``None``.

    ``None`` means the most recent call reached SOME transport (or no call
    has been made yet in this process); otherwise a human-readable clause
    naming every transport attempted (e.g. ``"http://localhost:8420/mcp,
    stdio binary /Applications/.../DEVONthink MCP"``) -- used by
    ``nx dt index`` to build its loud "DEVONthink MCP unreachable (...)"
    message (nexus-fdk1x) when a DT-dependent layer flag was requested.
    """
    return _LAST_UNREACHABLE_DETAIL


class _StdioSessionHolder:
    """Persistent background-thread MCP session over the stdio transport.

    :func:`dt_call` bridges into asyncio via a FRESH ``asyncio.run()`` per
    call (see its own docstring) -- correct for the always-on HTTP listener,
    but a stdio-spawned subprocess must NOT be restarted on that same
    per-call cadence: DEVONthink MCP takes real wall-clock to boot, and
    ``nx dt index`` calls ``is_running`` once plus several per-record tools
    (``extract_record_highlights``, ``get_record_links``, ``set_record_tags``,
    ...). This holder runs its own background thread with a long-lived
    event loop, opens ONE :func:`nexus.mcp_client.core.open_stdio_session`
    and keeps it entered for the life of the nx process (or until
    :meth:`close`) -- nexus-fdk1x's "one session per nx process, reused
    across calls" requirement.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: Any = None
        self._session_cm: Any = None
        self._ready = threading.Event()
        self._start_error: str | None = None
        # nexus-fdk1x code-review finding 2 (T2 [24110], round 2): the lock
        # alone serializes individual field writes but does NOT stop a
        # STALE boot attempt (one close() already superseded) from writing
        # a late result into the CURRENT generation's state after close()
        # already reset it -- measured via TestStdioSessionHolderLocking's
        # own reuse-after-close test, which failed with exactly that stale
        # write until this generation guard was added. Every write _boot()
        # makes is gated on "is my generation still the holder's current
        # one"; close() bumps this FIRST, under the lock, before touching
        # anything else, so any boot thread it is racing against necessarily
        # sees itself as stale on its next lock acquisition.
        self._generation = 0

    def _boot(
        self, command: str, args: tuple[str, ...], generation: int, ready: threading.Event,
    ) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:  # nexus-fdk1x code-review finding 2: same lock close() uses
            if generation != self._generation:
                loop.close()
                ready.set()  # unblock the ensure_started() call that spawned us
                return  # superseded before we even started -- nothing to do
            self._loop = loop

        async def _connect() -> None:
            cm = open_stdio_session(StdioEndpoint(command=command, args=args))
            session = await cm.__aenter__()
            with self._lock:
                stale = generation != self._generation
                if not stale:
                    self._session = session
                    self._session_cm = cm
            if stale:
                # Superseded by a close()/reset while connecting -- tear
                # down the subprocess we just opened rather than leaking
                # it; it belongs to nobody now.
                await cm.__aexit__(None, None, None)

        try:
            loop.run_until_complete(_connect())
        except Exception as exc:  # noqa: BLE001 -- recorded for the caller; must never raise across the thread boundary
            with self._lock:
                if generation == self._generation:
                    self._start_error = describe_exception(exc)
        finally:
            # nexus-fdk1x code-review finding 1: this Event is the ONLY thing
            # that ever marks the boot attempt terminal (success OR failure).
            # ensure_started()'s own wait-timeout must NEVER write
            # _start_error itself -- that was the permanent-poison bug: a
            # connect that is merely SLOW (still in flight past one caller's
            # bounded wait) must not forever block every later dt_call() in
            # this process behind a stale "timed out" error once it actually
            # succeeds.
            #
            # Set the *captured* ``ready`` parameter, NEVER the live
            # ``self._ready`` attribute (round-2 fix, T2 [24110]): close()
            # replaces ``self._ready`` with a brand-new Event on every call,
            # so a STALE boot reading the live attribute here would set the
            # CURRENT generation's event early -- before that generation's
            # own boot has written anything -- making a subsequent
            # ensure_started() wake up immediately and report "not ready"
            # even though its own (different) boot attempt is genuinely
            # still in flight. Reproduced by
            # TestStdioSessionHolderLocking::test_holder_is_reusable_after_a_racing_close
            # before this fix. A stale boot still signals its OWN
            # generation's ready event so its own spawning
            # ensure_started() call never waits out the full timeout.
            ready.set()

        with self._lock:
            stale = generation != self._generation
            boot_failed = stale or self._start_error is not None
        if not boot_failed:
            loop.run_forever()
        loop.close()

    def ensure_started(
        self, command: str, args: tuple[str, ...], *, timeout: float = 20.0,
    ) -> bool:
        """Start the background thread + subprocess on first use; idempotent.

        nexus-fdk1x code-review finding 1: a connect attempt still in
        flight past *timeout* returns ``False`` for THIS call only ("not
        ready yet") without recording any error -- the boot thread keeps
        running, and the NEXT ``dt_call()``'s ``ensure_started()`` waits
        again (bounded) on the SAME background boot rather than starting
        a second subprocess or being stuck behind a stale timeout. Only
        the boot's own outcome (a real connect success or failure,
        signalled via its own generation's ready event) is terminal.

        ``ready`` (the Event to wait on) is captured under the SAME lock
        acquisition as ``self._thread``/``self._generation`` -- reading
        ``self._ready`` again AFTER releasing the lock would risk a
        concurrent ``close()`` having already swapped it out for a new
        (unrelated, not-yet-signalled) Event in between, which would make
        this call wait out the full *timeout* for nothing.
        """
        with self._lock:
            if self._thread is None:
                _install_sigterm_handler()
                generation = self._generation
                ready = self._ready
                self._thread = threading.Thread(
                    target=self._boot, args=(command, args, generation, ready), daemon=True, name="dt-mcp-stdio",
                )
                self._thread.start()
            else:
                ready = self._ready
        if not ready.wait(timeout):
            return False  # still connecting -- try again on the next call
        with self._lock:
            return self._start_error is None and self._session is not None

    def call(self, tool: str, args: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any] | None:
        """Run *tool* on the persistent session. Caller must call :meth:`ensure_started` first."""
        with self._lock:
            loop, session = self._loop, self._session
        assert loop is not None and session is not None
        future = asyncio.run_coroutine_threadsafe(call_tool(session, tool, args), loop)
        return future.result(timeout=timeout)

    def start_error(self) -> str | None:
        with self._lock:
            return self._start_error

    def close(self) -> None:
        """Tear down the subprocess + background thread. Safe to call repeatedly."""
        with self._lock:
            self._generation += 1  # supersede any in-flight boot FIRST (finding 2)
            loop, session_cm, thread = self._loop, self._session_cm, self._thread
            self._loop = None
            self._session = None
            self._session_cm = None
            self._thread = None
            self._start_error = None
            self._ready = threading.Event()
        if loop is None or not loop.is_running():
            return

        async def _teardown() -> None:
            if session_cm is not None:
                await session_cm.__aexit__(None, None, None)

        try:
            asyncio.run_coroutine_threadsafe(_teardown(), loop).result(timeout=5)
        except Exception:  # noqa: BLE001 -- best-effort teardown on process exit / test reset
            pass
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)


_STDIO_HOLDER = _StdioSessionHolder()


def reset_stdio_session() -> None:
    """Tear down and discard the cached stdio session (tests; long-lived processes)."""
    global _STDIO_HOLDER
    _STDIO_HOLDER.close()
    _STDIO_HOLDER = _StdioSessionHolder()


atexit.register(lambda: _STDIO_HOLDER.close())

#: The SIGTERM disposition observed at install time (a callable handler,
#: ``signal.SIG_DFL``, ``signal.SIG_IGN``, or ``None`` when unknown).
_PREV_SIGTERM_HANDLER: Any = None
_SIGTERM_HANDLER_INSTALLED = False
_SIGTERM_INSTALL_LOCK = threading.Lock()


def _sigterm_handler(signum: int, frame: Any) -> None:
    """Tear down the spawned DEVONthink MCP child on SIGTERM, then chain on.

    nexus-fdk1x code-review finding 3: SIGTERM's default disposition
    bypasses ``atexit`` entirely (unlike SIGINT, which raises
    ``KeyboardInterrupt`` and unwinds normally through the interpreter,
    running ``atexit`` handlers on the way out) -- a bare ``kill <pid>``
    mid ``nx dt index`` leaked the spawned subprocess + background
    thread. Runs the SAME teardown ``close()``/``atexit`` use, then
    re-dispatches to whatever SIGTERM handler was installed before this
    one (another library's handler, or the interpreter default) so this
    module never changes the process's overall SIGTERM behaviour -- it
    only adds a cleanup step in front of it.
    """
    try:
        _STDIO_HOLDER.close()
    finally:
        prev = _PREV_SIGTERM_HANDLER
        if callable(prev):
            prev(signum, frame)
        else:
            # SIG_DFL / SIG_IGN / unknown (None): restore that disposition
            # and re-raise the signal so the process still terminates (or
            # not) exactly as it would have without this handler installed.
            signal.signal(signal.SIGTERM, prev if prev is not None else signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTERM)


def _install_sigterm_handler() -> None:
    """Install :func:`_sigterm_handler` once, lazily, on first real stdio spawn.

    Only installed the moment a stdio subprocess is ACTUALLY about to be
    spawned (called from :meth:`_StdioSessionHolder.ensure_started`) --
    a process that never touches the stdio transport never pays for a
    process-wide signal handler. ``signal.signal`` only works from the
    main thread; a non-main-thread caller (unusual for this CLI-path-only
    module, but not impossible for an embedding of it) skips installation
    silently -- the ``atexit`` teardown still covers every non-SIGTERM
    exit path.
    """
    global _PREV_SIGTERM_HANDLER, _SIGTERM_HANDLER_INSTALLED
    if _SIGTERM_HANDLER_INSTALLED:
        return
    with _SIGTERM_INSTALL_LOCK:
        if _SIGTERM_HANDLER_INSTALLED:
            return
        if threading.current_thread() is not threading.main_thread():
            return
        try:
            _PREV_SIGTERM_HANDLER = signal.signal(signal.SIGTERM, _sigterm_handler)
        except (ValueError, OSError):
            return
        _SIGTERM_HANDLER_INSTALLED = True


def _dt_call_http(tool: str, args: dict[str, Any], tried: list[str]) -> dict[str, Any] | None:
    url = dt_mcp_url()
    tried.append(url)
    endpoint = MCPEndpoint(url=url)

    async def _run() -> dict[str, Any] | None:
        async with open_session(endpoint) as session:
            return await call_tool(session, tool, args)

    try:
        return asyncio.run(_run())
    except Exception as exc:  # connect/transport failure -> fail-soft  # noqa: BLE001 -- transport boundary; fail-soft to None, logged at warning
        # nexus-56pmt / GH #1351: asyncio.run's TaskGroup wraps ANY failure in an
        # ExceptionGroup; str(exc) alone gives the content-free "unhandled errors
        # in a TaskGroup (1 sub-exception)" -- describe_exception unwraps to the
        # real root cause (connection refused, transport teardown, ...).
        log.warning(
            "dt_call_failed", tool=tool, transport="http",
            error=describe_exception(exc), error_type=type(exc).__name__,
        )
        return None


def _dt_call_stdio(tool: str, args: dict[str, Any], tried: list[str]) -> dict[str, Any] | None:
    command = dt_mcp_stdio_command()
    if command is None:
        # nexus-fdk1x: binary absent -> skip outright, never spawned. Still
        # named in `tried` (with its checked path) so the loud unreachable
        # message tells the operator exactly what was looked for.
        tried.append(f"stdio binary {_dt_mcp_stdio_path()} (not found)")
        return None
    tried.append(f"stdio binary {command}")
    try:
        if not _STDIO_HOLDER.ensure_started(command, ("--stdio",)):
            raise RuntimeError(_STDIO_HOLDER.start_error() or "stdio MCP session failed to start")
        return _STDIO_HOLDER.call(tool, args)
    except Exception as exc:  # noqa: BLE001 -- transport boundary; fail-soft to None, logged at warning
        log.warning(
            "dt_call_failed", tool=tool, transport="stdio",
            error=describe_exception(exc), error_type=type(exc).__name__,
        )
        return None


def dt_call(
    tool: str, args: dict[str, Any] | None = None, *, transport: str | None = None,
) -> dict[str, Any] | None:
    """Run one DT MCP tool synchronously, fail-soft (``None`` on any failure).

    Bridges the async ``mcp`` SDK into the synchronous CLI via ``asyncio.run``.
    A running event loop is a contract violation (Layer A is CLI-path-only):
    calling ``asyncio.run`` from one raises an opaque ``RuntimeError`` that the
    fail-soft contract would otherwise mask as a benign ``None``. The guard
    logs a DISTINCT ``dt_asyncio_context_error`` so the misuse is visible.

    nexus-fdk1x: tries the HTTP endpoint first (unless the resolved
    transport is ``"stdio"``), then falls back to a persistent
    stdio-spawned session (unless it's ``"http"``) when HTTP fails to
    connect -- the DEVONthink 4 MCP app is always reachable over stdio
    even when its HTTP listener is off, so a ConnectError on
    :data:`DEFAULT_DT_MCP_URL` alone is no longer the end of the road.
    Every caller of this module (``available()``, ``dt_extract_highlights()``,
    ``dt_writeback``, ``dt_link_generator``, ...) gets the fallback for
    free -- none of them need to change.

    *transport* overrides :func:`dt_mcp_transport` for THIS call only
    (``None`` -- the default -- uses the configured policy). code-review
    finding 4: ``nx dt index --dt-content`` alone must keep its pre-fdk1x
    fast-fail latency (RDR-139's original HTTP-only contract) rather than
    inheriting the stdio-spawn fallback the loud layers
    (link-semantic/writeback/highlights) pay for -- callers that need
    that narrower behaviour pass ``transport="http"`` explicitly.
    """
    global _LAST_UNREACHABLE_DETAIL
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # expected: no running loop, asyncio.run is safe
    else:
        log.error(
            "dt_asyncio_context_error",
            tool=tool,
            hint="nexus.mcp_client.devonthink is CLI-path-only; use the Layer A' server face from async contexts",
        )
        return None

    call_args = args or {}
    resolved_transport = transport if transport in ("http", "stdio", "auto") else dt_mcp_transport()
    tried: list[str] = []
    result: dict[str, Any] | None = None

    if resolved_transport != "stdio":
        result = _dt_call_http(tool, call_args, tried)

    if result is None and resolved_transport != "http":
        result = _dt_call_stdio(tool, call_args, tried)

    _LAST_UNREACHABLE_DETAIL = None if result is not None else (
        ", ".join(tried) if tried else "no transport configured"
    )
    return result


def available(*, refresh: bool = False, transport: str | None = None) -> bool:
    """Whether DEVONthink is reachable and running (cached).

    Probes the ``is_running`` tool (``{running: bool, ...}``). Unreachable
    server or ``running=False`` → ``False``. The result is cached; pass
    ``refresh=True`` to re-probe. *transport* is forwarded to :func:`dt_call`
    (per-call override; see its docstring) -- not part of the cache key,
    so callers that need a narrower probe alongside a broader one elsewhere
    in the same process should prefer ``refresh=True`` to avoid reusing a
    stale cached value from the other transport scope.
    """
    global _AVAIL_CACHE
    if not refresh and _AVAIL_CACHE is not None:
        return _AVAIL_CACHE
    result = dt_call("is_running", transport=transport)
    _AVAIL_CACHE = bool(result) and bool(result.get("running"))
    return _AVAIL_CACHE


def dt_find_similar(uuid: str, *, limit: int = 25, floor: float = 0.0) -> list[Neighbour]:
    """Similarity neighbours of ``uuid`` (DT 'See Also'), filtered by ``floor``.

    ``floor`` is also passed to the server as ``min_score`` for an early prune;
    the client-side filter is a defensive backstop. Entries without a UUID are
    dropped. Empty list when DT is unavailable or returns nothing.
    """
    result = dt_call(
        "find_similar_records",
        {"mode": "record", "uuid": uuid, "limit": limit, "min_score": floor},
    )
    if not result:
        return []
    # Single-record mode returns a BARE neighbour array; core wraps a bare JSON
    # array as {"result": [...]}. Batch/summary mode would use {"results": [...]}.
    # Accept both shapes (live finding — the spike's {count, results} shape did
    # not match single-uuid mode).
    neighbours = result.get("results")
    if neighbours is None:
        neighbours = result.get("result")
    out: list[Neighbour] = []
    for r in neighbours or []:
        if not isinstance(r, dict):
            continue
        ruuid = r.get("uuid")
        score = float(r.get("score", 0.0) or 0.0)
        if not ruuid or score < floor:
            continue
        out.append(Neighbour(uuid=ruuid, score=score, name=r.get("name", "")))
    return out


def dt_record_links(uuid: str) -> list[Neighbour]:
    """DEVONthink's own deliberate link neighbours (item links, both directions).

    Higher precision than similarity: these are author-curated references. Score
    is fixed at ``1.0`` to mark them as deliberate. Empty list when unavailable.
    """
    result = dt_call("get_record_links", {"uuid": uuid, "direction": "both", "kind": "item"})
    if not result:
        return []
    entries: list[dict[str, Any]] = []
    for key in ("incoming", "outgoing"):
        value = result.get(key)
        if isinstance(value, list):
            entries.extend(value)
    seen: set[str] = set()
    out: list[Neighbour] = []
    for r in entries:
        ruuid = r.get("uuid")
        if not ruuid or ruuid in seen:
            continue
        seen.add(ruuid)
        out.append(Neighbour(uuid=ruuid, score=1.0, name=r.get("name", "")))
    return out


def dt_resolve_doi(doi: str) -> dict[str, Any] | None:
    """Resolve a DOI to CrossRef bibliographic fields, or ``None`` (Layer C source)."""
    if not doi:
        return None
    return dt_call("resolve_doi_metadata", {"doi": doi})


def dt_extract_content(uuid: str) -> str | None:
    """AI-optimised text body of a record, or ``None`` (Layer D, non-file-backed records).

    Joins a sectioned/paged result into one string; returns ``None`` when no
    text is available (or the record is excluded from AI access).
    """
    result = dt_call("extract_record_content", {"uuid": uuid})
    if not result:
        return None
    # Short/plain docs return a single text body ({"text": ...}); structured
    # docs (Markdown/PDF/EPUB) return a BARE array of section/page dicts, which
    # core wraps as {"result": [...]}. Handle both (live finding — sectioned
    # PDFs are the common paper case and were returning None, wrongly tripping
    # the Layer F exclusion guard).
    text = result.get("text")
    if isinstance(text, str) and text:
        return text
    sections = result.get("sections") or result.get("pages") or result.get("result")
    if isinstance(sections, list):
        parts = [s.get("text", "") for s in sections if isinstance(s, dict)]
        joined = "\n".join(p for p in parts if p)
        return joined or None
    return None


def dt_record_name(uuid: str) -> str:
    """Display name of a record, or ``""`` (Layer D title source).

    Used to give a non-file-backed record's DT-extracted text a human title
    for ``derive_title`` / search. Fail-soft: empty string when unavailable.
    """
    result = dt_call("get_record_properties", {"uuid": uuid})
    if not result:
        return ""
    name = result.get("name")
    return name if isinstance(name, str) else ""


#: Status messages DT returns when a record carries zero annotations/mentions.
#: These are not content — a result that IS one of these (the whole stripped
#: body starts with the phrase AND is short) maps to None. The full-phrase form
#: ("...found") plus a length guard avoids false-positives on a real highlight
#: blob that merely opens with "No annotations ..." prose.
_NO_CONTENT_PREFIXES: tuple[str, ...] = (
    "no highlights found",
    "no mentions found",
    "no annotations found",
)
#: A body longer than this is treated as real content even if it opens with a
#: sentinel-looking phrase (the status messages are short, one-liners).
_NO_CONTENT_MAX_LEN: int = 200


def _dt_markdown_or_none(result: dict[str, Any] | None) -> str | None:
    """Pull a markdown body from a DT highlights/mentions result, or ``None``.

    ``extract_record_highlights`` / ``extract_record_mentions`` return either
    the markdown text (success) or a "No highlights found ..." status string
    (zero annotations). core wraps a plain-text content as ``{"text": ...}``;
    partial-success returns ``{"markdown": ..., ...}``. Accept both keys and
    map any no-content status message to ``None`` so callers don't store it.
    """
    if not result:
        return None
    text = result.get("text")
    if not isinstance(text, str) or not text:
        md = result.get("markdown")
        text = md if isinstance(md, str) else None
    stripped = text.strip() if text else ""
    if not stripped:
        return None
    # A short body that opens with a "No ... found" status phrase is the
    # zero-annotation sentinel; a long body that merely mentions the phrase is
    # real content and is kept.
    if (
        len(stripped) <= _NO_CONTENT_MAX_LEN
        and stripped.lower().startswith(_NO_CONTENT_PREFIXES)
    ):
        return None
    return text


def dt_extract_highlights(uuid: str) -> str | None:
    """Markdown summary of a record's annotations/highlights, or ``None`` (Layer E).

    Maps DT's zero-annotation status message to ``None``. Fail-soft.
    """
    return _dt_markdown_or_none(dt_call("extract_record_highlights", {"uuid": uuid}))


def dt_extract_mentions(uuid: str) -> str | None:
    """Markdown summary of a record's mentions, or ``None`` (Layer E). Fail-soft."""
    return _dt_markdown_or_none(dt_call("extract_record_mentions", {"uuid": uuid}))


def _uuid_from_capture_result(result: dict[str, Any] | None) -> str | None:
    """Pull the new record's UUID from a capture/import/download result.

    ``capture_web_page`` / ``import_file`` put ``uuid`` at the top level;
    ``download_pdf_from_doi`` nests the imported record under ``record`` (or
    ``imported``) and returns metadata-only (no UUID) when no PDF was found.
    """
    if not isinstance(result, dict):
        return None
    uuid = result.get("uuid")
    if isinstance(uuid, str) and uuid:
        return uuid
    for key in ("record", "imported"):
        nested = result.get(key)
        if isinstance(nested, dict):
            nuuid = nested.get("uuid")
            if isinstance(nuuid, str) and nuuid:
                return nuuid
    return None


def dt_capture_web_page(
    url: str, *, capture_type: str = "webarchive", name: str | None = None,
) -> str | None:
    """Capture ``url`` into DEVONthink, returning the new record's UUID (Layer G).

    ``capture_type`` is one of html/webarchive/markdown/pdf. ``None`` on failure
    (fail-soft). PDF captures are file-backed; the others are not.
    """
    args: dict[str, Any] = {"url": url, "type": capture_type}
    if name:
        args["name"] = name
    return _uuid_from_capture_result(dt_call("capture_web_page", args))


def dt_download_pdf_from_doi(
    doi: str, *, contact_email: str = "", name: str | None = None,
) -> str | None:
    """Resolve ``doi`` and download its open-access PDF into DEVONthink (Layer G).

    Returns the imported record's UUID, or ``None`` when no open-access PDF was
    found (metadata-only result) or DT is unavailable. ``contact_email`` enables
    Unpaywall's PDF discovery (without it only CrossRef metadata is fetched).
    """
    if not doi:
        return None
    args: dict[str, Any] = {"doi": doi}
    if contact_email:
        args["contact_email"] = contact_email
    if name:
        args["name"] = name
    return _uuid_from_capture_result(dt_call("download_pdf_from_doi", args))


def dt_import_file(path: str, *, mode: str = "import") -> str | None:
    """Import a loose file into DEVONthink, returning the new record's UUID (Layer G).

    ``mode`` is import (copy in) or index (reference in place). ``None`` on failure.
    """
    if not path:
        return None
    return _uuid_from_capture_result(dt_call("import_file", {"path": path, "mode": mode}))


def dt_set_tags(uuid: str, tags: list[str], *, mode: str = "add") -> bool:
    """Write tags onto a record (default additive). ``True`` on success (Layer F)."""
    if not tags:
        return False
    result = dt_call("set_record_tags", {"uuid": uuid, "tags": tags, "mode": mode})
    return result is not None


def dt_annotation_text(uuid: str) -> str | None:
    """Current annotation body of a record, or ``None`` (no annotation / excluded).

    Two-hop: ``get_record_annotation`` yields the annotation record's UUID, then
    ``get_record_text`` reads its body. Used to make annotation write-back
    idempotent (append only when the backlink is not already present).
    """
    meta = dt_call("get_record_annotation", {"uuid": uuid})
    if not meta:
        return None
    ann_uuid = meta.get("annotation_uuid")
    if not ann_uuid:
        return None
    result = dt_call("get_record_text", {"uuid": ann_uuid})
    if not result:
        return None
    text = result.get("text") if isinstance(result, dict) else None
    return text if isinstance(text, str) else None


def dt_set_annotation(uuid: str, text: str, *, mode: str = "append") -> bool:
    """Write an annotation note onto a record. ``True`` on success (Layer F backlink).

    The RDR Layer F design uses this to stamp a backlink to the nexus tumbler.
    Defaults to ``mode="append"`` so a nexus backlink never clobbers an existing
    annotation (no-clobber; DEVONthink also auto-checkpoints prior content when
    the host DB has versioning enabled). Empty text short-circuits to ``False``.
    """
    if not text:
        return False
    result = dt_call("set_record_annotation", {"uuid": uuid, "text": text, "mode": mode})
    return result is not None


def dt_set_custom_metadata(uuid: str, fields: dict[str, Any], *, mode: str = "merge") -> bool:
    """Write custom-metadata fields onto a record (default merge). ``True`` only on a real write.

    DEVONthink custom-metadata identifiers must be PRE-DEFINED in the database's
    custom-metadata schema; unknown fields are silently dropped server-side (the
    response lists them in ``dropped_fields``). Also: DT strips ``-``/``.`` from
    identifiers, so ``nxtumbler`` is the maximally-namespaced legal key. This
    helper returns ``False`` when DT dropped every field (an honest no-op, not a
    false success) so callers don't believe a write happened that didn't. Empty
    fields short-circuit to ``False``.
    """
    if not fields:
        return False
    result = dt_call(
        "set_record_custom_metadata", {"uuid": uuid, "metadata": fields, "mode": mode}
    )
    if result is None:
        return False
    dropped = result.get("dropped_fields")
    if not isinstance(dropped, list):
        dropped = []
    # If DT dropped as many fields as we sent, nothing was committed.
    return len(dropped) < len(fields)
