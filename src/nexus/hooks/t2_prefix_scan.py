# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""T2 prefix-scan, IN-PROCESS (RDR-215 bead nexus-b5ugt).

Port of ``conexus/hooks/scripts/t2_prefix_scan.py`` (502 lines, a
stdlib-only mirror of the client's HTTP/credential-resolution primitives,
written because a bare ``python3`` subprocess cannot import the ``nexus``
package). Code running inside the wheel has no such constraint, so this
module calls the REAL primitives directly:

  - :func:`nexus.db.service_endpoint`'s resolution chain (env, persisted
    ``config.yml``, the local supervisor lease) is not re-implemented at
    all — it is reached transitively through :class:`HttpMemoryStore`
    (below), which is the SAME client every other T2 memory caller in
    this codebase uses.
  - :class:`~nexus.db.t2.http_memory_store.HttpMemoryStore` already
    exposes ``get_projects_with_prefix``/``get_all`` — the exact two
    ``/v1/memory/*`` endpoints the plugin script's ``urllib`` calls hit —
    so there is no HTTP glue to port either. Constructing it with no
    ``base_url``/``_token`` resolves the endpoint via
    :func:`~nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate`
    and applies the data-token override
    (:meth:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin._apply_data_token_override`)
    exactly as every other T2 domain store does.

**The live defect this closes**: ``conexus/.mcp.json`` sets the MCP
server's ``env`` block to ``{"CLAUDE_PLUGIN_ROOT":
"${CLAUDE_PLUGIN_ROOT}"}``, and Claude Code does not expand ``${...}``
inside an MCP ``env`` block — every ``nx-mcp`` process therefore carries
that LITERAL string. ``subagent_start._t2_memory_section`` built
``f"{plugin_root}/hooks/scripts/t2_prefix_scan.py"`` from it, which never
resolved to a real path, so the "## T2 Memory" section was silently
missing from every dispatched subagent's context. Calling :func:`scan`
in-process removes the ``CLAUDE_PLUGIN_ROOT``-resolved subprocess launch
entirely — there is no path to fail to resolve.

**Behavioural divergence from the plugin mirror, noted rather than
silently carried or silently fixed** (RDR-215 bead nexus-b5ugt instructed
reporting this rather than papering over it): the plugin mirror's
``_resolve_endpoint`` treats a "data-token lease" purely as something to
BORROW — it is stdlib-only and cannot mint one itself, so on an armed
pass-through box presenting a stale static token it can only 401 and
print a hint suggesting the operator run a command that mints one.
``HttpMemoryStore`` construction goes through the REAL
``DataTokenManager.bearer_for()`` (via ``_apply_data_token_override``),
which — when a ``mint_token`` credential is configured — mints a fresh
token itself (or reuses a cached one) rather than only reading an
already-published lease file. This is a strict improvement (the
mirror's "borrow-only" design was itself a description of what a
stdlib-only script COULD do, not a description of the client's real
capability), so this port does not reproduce the mirror's read-only
fallback or its mint-locked-token 401 hint message — a 401 here is a
genuine credential failure, not a resolvable "run this to mint a lease"
condition. (bead nexus-b5ugt.)

Two smaller, deliberate simplifications, both losing render density only
(never data the caller could act on):

  - The mirror's 3.0s-default ``_DEFAULT_HTTP_TIMEOUT_S`` is preserved
    (passed through as ``HttpMemoryStore(timeout=...)``) so this hook
    stays fast; ``HttpMemoryStore``'s own construction-time endpoint
    resolution can add a BOUNDED wait on top of that (up to
    ``DEFAULT_LEASE_WAIT_BUDGET_S`` = 12s) but ONLY when this process has
    evidence of a previously-live lease (a respawn-gap retry) — a cold
    process fails fast unchanged, matching every other T2 store's
    resolution contract.
  - The mirror's own "the presented token was the static fallback, not a
    fresh data-token lease" 401 hint is dropped for the reason above.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from nexus._hook_runtime._io import configure_hook_logging
from nexus.db.t2.http_memory_store import HttpMemoryStore

_log = structlog.get_logger(__name__)

__all__ = ["scan"]

#: nexus-h33x8.5 fix-pass constants, carried unchanged from the plugin
#: mirror (see that module's own comment for the tightening history).
_HARD_CAP = 8  # max rendered entries across all namespaces combined
_SNIPPET_LIMIT = 3  # per-namespace: entries up to this rank get a snippet
_TITLE_LIMIT = 5  # per-namespace: entries up to this rank get title-only

#: Cap on distinct namespaces this scan issues a per-namespace
#: ``get_all`` request for — see the plugin mirror's identical constant
#: for the nexus-9xado rationale (unbounded namespace list -> unbounded
#: sequential HTTP round-trips).
_MAX_NAMESPACES = 5

#: Overall wall-clock budget for the per-namespace fetch loop, distinct
#: from the per-request timeout below. Override via NX_T2_SCAN_BUDGET_S.
_DEFAULT_SCAN_BUDGET_S = 8.0

#: A namespace's freshest entry older than this is flagged with a visible
#: warning line — never a silent stale block. Override via
#: NX_T2_SCAN_STALE_DAYS.
_DEFAULT_STALE_DAYS = 14

#: Short by design — this hook runs on every SessionStart/SubagentStart
#: and must never make the injected-context path noticeably slower than
#: the rest of the hook chain. Override via NX_T2_SCAN_TIMEOUT_S. This
#: is the CEILING each individual HTTP call may use; the actual per-call
#: timeout is the SMALLER of this and whatever is left of the whole-scan
#: budget (nexus-8t9w8 fold-in: critic follow-up on nexus-fow78/9b038bfae
#: -- one namespace issues up to 1 ``list_entries`` + ``_SNIPPET_LIMIT``
#: ``get`` calls, and each independently risking this full timeout could
#: burn ``(1 + _SNIPPET_LIMIT) * _DEFAULT_HTTP_TIMEOUT_S`` = 4 * 3.0s =
#: 12.0s against an 8.0s whole-scan budget for a SINGLE degraded
#: namespace, the same stacking-timeout defect the stamp-budget fix
#: closed for ``_stamp_ids``. See ``_clamped_call_timeout``.)
_DEFAULT_HTTP_TIMEOUT_S = 3.0

#: Floor for a single HTTP call's clamped timeout once the scan budget
#: is nearly exhausted -- mirrors ``_STAMP_TIMEOUT_FLOOR`` in
#: ``pre_close_verification.py``: never zero (a 0.0 timeout is not "try
#: briefly", it is "don't try"), small enough that a near-exhausted
#: budget still fails fast rather than eating what little is left.
_HTTP_TIMEOUT_FLOOR = 0.5


def _clamped_call_timeout(deadline: float, ceiling: float) -> float:
    """The per-call HTTP timeout for a request issued now, against
    *deadline* (an absolute ``time.monotonic()`` value) and never larger
    than *ceiling* (the configured ``NX_T2_SCAN_TIMEOUT_S``).

    The smaller of "what's configured" and "what's actually left of the
    scan budget" -- so a namespace already deep into a slow scan gets a
    correspondingly SHORTER attempt on its next call, rather than every
    call independently risking the full configured ceiling regardless of
    how much budget prior calls in the same scan already spent.
    """
    remaining = deadline - time.monotonic()
    return max(_HTTP_TIMEOUT_FLOOR, min(ceiling, remaining))

#: Engine timestamp format: UTC second-precision ISO
#: (MemoryHandler.recordToMap / MemoryRepository.UTC_SECOND on the Java side).
_TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"

#: Exceptions treated as "the T2 engine is unreachable" — a construction
#: failure (no endpoint/token resolvable: ServiceEndpointUnresolvableError
#: and the other plain-RuntimeError raise sites in
#: nexus.db.service_endpoint, plus DataTokenMintError, all RuntimeError
#: subclasses), an HTTP transport/status failure, or a bare OSError
#: (a connection-refused/reset that leaks past httpx's own wrapping —
#: see HttpMemoryStore.find_overlapping_memories's identical catch).
_UNREACHABLE_EXC: tuple[type[BaseException], ...] = (
    RuntimeError,
    httpx.HTTPError,
    OSError,
)


def _snippet(content: str, max_chars: int = 70) -> str:
    """Return first meaningful line of content, truncated."""
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or set(line) <= set("-="):
            continue
        return line[:max_chars] + ("…" if len(line) > max_chars else "")
    return ""


def _snippet_for(
    store: HttpMemoryStore, project: str, title: str, *, timeout: float
) -> str:
    """Fetch one entry's content and reduce it to a snippet.

    A per-entry fetch failure (unreachable, deleted between the summary
    list and this call) degrades to no snippet rather than aborting the
    namespace — the title alone still renders. ``timeout`` is the
    caller-computed, budget-clamped per-call value (see
    ``_clamped_call_timeout``), not the store's own client-wide default.

    ``retry_read_timeout=False``: a clamped-short *timeout* that this
    call itself trips must not be retried with the SAME clamp value —
    that silently doubles the wait against a budget that was
    deliberately shortened to avoid exactly that (measured: without
    this, a single clamped ``get()`` call cost ~2x its clamp, not the
    clamp itself).
    """
    try:
        entry = store.get(
            project=project, title=title, timeout=timeout, retry_read_timeout=False
        )
    except _UNREACHABLE_EXC:
        return ""
    if not entry:
        return ""
    return _snippet(entry.get("content") or "")


def _build_output(
    store: HttpMemoryStore,
    project_name: str,
    namespaces: list[dict[str, Any]],
    scan_budget_s: float = _DEFAULT_SCAN_BUDGET_S,
    request_timeout_s: float = _DEFAULT_HTTP_TIMEOUT_S,
) -> list[str]:
    """Render the ``### T2 Memory (...)`` block(s), capped per the same
    per-namespace/whole-scan budget the plugin mirror used.

    **Scoped to what is rendered (nexus-fow78).** A namespace's ranked
    list comes from :meth:`HttpMemoryStore.list_entries` — the same
    ``project`` filter and ``timestamp DESC`` order as the retired
    ``get_all`` call, but without each row's ``content`` column, which is
    the overwhelming majority of the payload on a namespace with a real
    history (measured against production T2: ``get_all('nexus')`` = 3840
    rows / ~20.9MB content / ~15s; ``list_entries(project='nexus')`` =
    same 3840 rows / ~2.4s with no per-row content). Full content is then
    fetched with a targeted :meth:`HttpMemoryStore.get` call, and ONLY for
    the ranks that actually render a snippet (``_SNIPPET_LIMIT`` = 3 per
    namespace) — titles beyond that never pay for content at all, matched
    or not. Both are existing ``/v1/memory/*`` routes; no engine change
    was needed.

    Per-namespace fetch failures are isolated: a bad/slow namespace N gets
    its own warning line and the loop moves on, rather than an exception
    from namespace N discarding namespaces ``1..N-1``'s already-rendered
    output too. ``namespaces`` is capped to ``_MAX_NAMESPACES`` and the
    whole loop is bounded by *scan_budget_s* — both independent of
    ``_HARD_CAP``, which only counts RENDERED entries and does not fire
    for a run of empty namespaces. The deadline is also checked BETWEEN
    per-entry snippet fetches (not only between namespaces): once the
    deadline passes mid-namespace, remaining snippet ranks render
    title-only instead of issuing another network call.

    **Every HTTP call's own timeout is clamped to what's left of the
    scan budget, AND opts out of the mixin's default retry-on-
    ReadTimeout** (nexus-8t9w8 fold-in), not a flat *request_timeout_s*
    every call gets independently: a single degraded namespace issuing
    up to ``1 + _SNIPPET_LIMIT`` calls (one ``list_entries`` plus up to
    ``_SNIPPET_LIMIT`` ``get`` calls) could otherwise burn
    ``(1 + _SNIPPET_LIMIT) * request_timeout_s`` against *scan_budget_s*
    on its own — with the defaults, 4 * 3.0s = 12.0s against an 8.0s
    budget for ONE namespace, the exact stacking-timeout shape the
    stamp-budget fix closed for ``_stamp_ids`` (code review of
    9b038bfae). Measured WORSE than that estimate during the fix itself:
    ``RefreshableHttpStoreMixin._get`` retries a ``ReadTimeout`` exactly
    once by default (``retry_read_timeout=True``), so an UNCLAMPED call
    against a truly hung server pays roughly 2x its nominal timeout, not
    1x — a single ``get()`` call in the consequence test below measured
    ~6.9s against a nominal 3.0s ceiling before this fix added
    ``retry_read_timeout=False`` alongside the clamp (a clamped-short
    timeout must not be retried with the SAME clamp; that silently
    doubles the wait against a budget deliberately shortened to avoid
    exactly that). See ``_clamped_call_timeout``.
    """
    lines: list[str] = []
    total = 0  # rendered entries across all namespaces
    capped_namespaces = namespaces[:_MAX_NAMESPACES]
    skipped_for_cap = len(namespaces) - len(capped_namespaces)
    deadline = time.monotonic() + scan_budget_s

    for idx, ns_row in enumerate(capped_namespaces):
        if total >= _HARD_CAP:
            break
        if time.monotonic() >= deadline:
            remaining = len(capped_namespaces) - idx
            lines.append(
                f"  … (scan budget exceeded — {remaining} namespace(s) not checked)"
            )
            break

        ns = ns_row.get("project", "")
        try:
            # nexus-xn9ut: read only the rows this scan can render, plus
            # one to learn whether more exist. The whole-project list cost
            # 2.4s on a 3,840-row namespace against the hook's 9s bound.
            summaries = store.list_entries(
                project=ns,
                limit=_TITLE_LIMIT + 1,
                timeout=_clamped_call_timeout(deadline, request_timeout_s),
                retry_read_timeout=False,
            )
        except _UNREACHABLE_EXC as exc:
            lines.append(f"  WARNING: T2 memory namespace {ns!r} unreachable: {exc}")
            continue
        if not summaries:
            continue

        suffix = ns[len(project_name) :].lstrip("_") if ns != project_name else ""
        label = f"T2 Memory ({suffix})" if suffix else "T2 Memory"

        ns_lines: list[str] = []
        ns_remaining = 0
        ns_rank = 0  # per-namespace position (1-based)

        for s in summaries:
            if total >= _HARD_CAP:
                ns_remaining += 1
                continue
            ns_rank += 1
            title = s.get("title", "") or ""
            if ns_rank <= _SNIPPET_LIMIT:
                if time.monotonic() < deadline:
                    snip = _snippet_for(
                        store, ns, title,
                        timeout=_clamped_call_timeout(deadline, request_timeout_s),
                    )
                    ns_lines.append(f"  {title}" + (f" — {snip}" if snip else ""))
                else:
                    ns_lines.append(f"  {title}")
                total += 1
            elif ns_rank <= _TITLE_LIMIT:
                ns_lines.append(f"  {title}")
                total += 1
            else:
                ns_remaining += 1

        if ns_lines:
            lines.append(f"### {label}")
            lines.extend(ns_lines)
            if ns_remaining:
                # The list is bounded (nexus-xn9ut), so the number past the
                # rendered titles is not known; the extra row says there
                # are more.
                lines.append("  … (more)")
            lines.append("")

    if skipped_for_cap:
        lines.append(
            f"  … ({skipped_for_cap} older namespace(s) not checked — "
            f"_MAX_NAMESPACES={_MAX_NAMESPACES})"
        )

    return lines


def _check_freshness(last_updated: str, stale_days: int) -> str | None:
    """Two-arm freshness assert, arm 1: the freshest entry across every
    matched namespace (``namespaces[0]["last_updated"]`` — already the max
    since the caller receives DESC order) is older than *stale_days*.

    Returns ``None`` when *last_updated* is empty/unparseable (never
    fabricate a warning from data we could not read) or is fresh enough.
    Arm 2 (source-unreachable) is handled by :func:`scan`'s own
    construction/first-call catch — this function only ever sees a
    REACHABLE, non-empty result.
    """
    if not last_updated:
        return None
    try:
        ts = datetime.strptime(last_updated, _TIMESTAMP_FMT).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    age_days = (datetime.now(timezone.utc) - ts).days
    if age_days > stale_days:
        return (
            f"WARNING: T2 memory freshest entry is {age_days}d old "
            f"(> {stale_days}d threshold) — verify the T2 substrate is current"
        )
    return None


def scan(project: str) -> str:
    """Return the T2 prefix-scan body for *project* — the same content
    the plugin's ``t2_prefix_scan.py`` printed to stdout, produced
    in-process against the real T2 HTTP client instead of shelling out.

    Returns ``""`` when *project* is empty, the engine is reachable but
    has zero matching namespaces (a genuinely empty T2 — fresh install,
    or no entries under this prefix yet; empty is not stale), or nothing
    ends up rendered. A source-unreachable failure (construction, or the
    first ``get_projects_with_prefix`` call) is ALWAYS a visible
    ``WARNING: ...`` line — never a silent no-op, matching the plugin
    mirror's nexus-8fvp2 contract that a reachability failure must never
    be confused with "reachable but genuinely empty".
    """
    if not project:
        return ""

    # Point structlog at a file sink BEFORE touching HttpMemoryStore.
    #
    # This module's own `_log` is the small half. The large half is that
    # going in-process drags the TRANSITIVE logging surface of everything
    # it now calls into the hook's output channel: HttpMemoryStore ->
    # DataTokenManager logs `data_token_mint_failed` through its own
    # ambient `structlog.get_logger()`, correctly, as a library should.
    # With no configured sink, structlog's default PrintLoggerFactory
    # writes that to STDOUT, which for a hook is the decision channel.
    # Measured on this port before the fix: a scan against an unreachable
    # engine put two lines on stdout, one of them from a wheel module
    # nobody would think to audit when reviewing a hook.
    #
    # `configure_hook_logging` documents this exact case -- "a verb whose
    # own implementation logs through an ambient structlog.get_logger()
    # it does not own, which is every verb reaching into nexus.hooks".
    # Its own caution is to call it only from a verb that already pays
    # for structlog; this one imports httpx and the T2 client, so the
    # 0.06 s is already spent.
    #
    # Belt and braces rather than the only guard: `entry.main` routes
    # stray stdout to stderr for a command-tier dispatch, and `nx-mcp`
    # configures logging at startup for the tool tier, so both live paths
    # were already covered. What this fixes is the bare-subprocess case
    # -- which is what the tests run, and a test harness that silently
    # eats a corrupted envelope is how the defect stays invisible.
    configure_hook_logging()

    timeout = float(
        os.environ.get("NX_T2_SCAN_TIMEOUT_S", str(_DEFAULT_HTTP_TIMEOUT_S))
    )
    stale_days = int(os.environ.get("NX_T2_SCAN_STALE_DAYS", str(_DEFAULT_STALE_DAYS)))
    scan_budget_s = float(
        os.environ.get("NX_T2_SCAN_BUDGET_S", str(_DEFAULT_SCAN_BUDGET_S))
    )

    try:
        store = HttpMemoryStore(timeout=timeout)
    except _UNREACHABLE_EXC as exc:
        _log.debug("t2_prefix_scan.unreachable", phase="construct", error=str(exc))
        return f"WARNING: T2 memory unreachable: {exc}"

    try:
        try:
            namespaces = store.get_projects_with_prefix(project)
        except _UNREACHABLE_EXC as exc:
            _log.debug(
                "t2_prefix_scan.unreachable", phase="list_namespaces", error=str(exc)
            )
            return f"WARNING: T2 memory unreachable: {exc}"

        if not namespaces:
            # Reachable, zero matching namespaces: a genuinely empty T2.
            return ""

        lines = _build_output(
            store, project, namespaces, scan_budget_s, request_timeout_s=timeout
        )

        freshness_warning = _check_freshness(
            namespaces[0].get("last_updated", ""), stale_days
        )
        if freshness_warning:
            lines.append(freshness_warning)
            lines.append("")

        return "\n".join(lines) if lines else ""
    finally:
        store.close()
