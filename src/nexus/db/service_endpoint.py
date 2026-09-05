# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Centralized nexus-service endpoint discovery (RDR-152 nexus-fjwxh).

ONE resolver for every HTTP storage client — the ten T2 stores, the catalog
client, and the T3 vector client. Before this module each client carried its
own copy: the T2/scratch stores were env-only (and so broke the moment the
default flipped to ``service`` on a box that had a running supervisor but no
``NX_SERVICE_PORT`` exported), the catalog client grew an inline lease
fallback, and the vector client owned the canonical ``_discover_lease``. This
centralizes the discovery so the T2 default-flip "just works" wherever the
supervisor is running.

Resolution order (each env half overrides independently — read fresh every
call so a supervisor restart that republishes the lease is picked up):

  1. ``NX_SERVICE_HOST`` / ``NX_SERVICE_PORT`` / ``NX_SERVICE_TOKEN`` env.
  2. The ServiceRegistry lease (``storage_service_addr.<uid>``) the supervisor
     publishes after a healthy ``/health`` — the authoritative source, since
     the supervisor allocates a NEW free port on every (re)start, which is
     exactly what broke env-only resolution after an auto-restart.
  3. FAIL LOUD. The legacy hardcoded ``localhost:8080`` default is retired — a
     silent wrong-port fallback is a correctness hazard.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)


class ServiceEndpointUnresolvableError(RuntimeError):
    """The genuine "no env, no lease, nothing resolvable" failure —
    :func:`resolve_service_config`'s ONE raise site for that exact
    condition (nexus-7dsgp, code-review round 2, Medium).

    A plain ``RuntimeError`` was too coarse a signal for callers deciding
    whether to retry-with-wait (:func:`nexus.db.t2._refreshable_client._resolve_endpoint_with_evidence_gate`):
    this module ALSO raises bare ``RuntimeError`` for two OTHER,
    unrelated conditions — a malformed ``NX_SERVICE_PORT`` value (a
    config-typo bug, not a respawn-gap symptom) and the managed-cloud
    "``service_url`` is set but no token is resolvable" case (which must
    NEVER wait, by the bead's own "never for the managed-cloud URL path"
    contract). Both of those stay plain ``RuntimeError`` — this subclass
    is raised ONLY at the genuine not-resolvable site. Subclassing
    ``RuntimeError`` (rather than a fresh ``Exception`` subclass) means
    every existing ``except RuntimeError`` / ``pytest.raises(RuntimeError)``
    call site keeps matching unchanged; only a caller that specifically
    wants to distinguish "worth a bounded wait" needs to catch this type.
    """




#: Bounded-wait retry mitigation for the supervisor-respawn gap (GH #1405
#: defect 1, nexus-7dsgp): the observed dead-lease-to-live-lease gap is
#: 5-10s, so 12s gives ~2-7s of margin without letting a genuinely-dead
#: supervisor hang a caller for long. Budget arithmetic for callers that
#: layer this ON TOP OF an existing per-attempt timeout/retry axis (gateway
#: 502/503/504 backoff, httpx connect/read timeouts) MUST be documented at
#: the call site — this constant bounds ONLY the added lease-wait, not the
#: caller's total wall clock.
DEFAULT_LEASE_WAIT_BUDGET_S: float = 12.0
#: Poll, don't blind-sleep the whole budget — a lease that republishes
#: partway through is picked up within one interval, not the full budget.
DEFAULT_LEASE_POLL_INTERVAL_S: float = 0.5

#: Process-wide "has this process EVER discovered a live lease" signal
#: (nexus-7dsgp, critic round 1 CRITICAL). The T3 vector client's
#: ``_lease_cache`` doubles as this signal for T3 (non-None means "was
#: warm recently"), because it is a long-lived module-global singleton
#: that survives across calls. The T2 domain-store family has NO
#: equivalent: ``mcp_infra.t2_ctx()`` builds a FRESH ``RefreshableHttpStoreMixin``
#: instance per call, so a construction landing in the republication gap
#: had zero evidence to decide "wait" vs "fail fast" and always chose
#: fail-fast — even when the SAME process had successfully resolved a
#: lease moments earlier (e.g. a prior tool call). This flag is the
#: shared, cross-store equivalent: set on every successful
#: :func:`discover_lease` hit (the ONE discovery implementation every
#: resolution path — vector client, T2 mixin, catalog, token/scratch
#: stores — routes through), read by construction-time callers via
#: :func:`has_ever_resolved_lease` to decide whether a resolution
#: failure is "probably mid-respawn-gap" (worth a bounded wait) or
#: "probably never configured" (fail fast, no evidence to wait on).
_has_ever_resolved_lease: bool = False


def discover_lease() -> tuple[str | None, str | None]:
    """``(base_url, token)`` from the supervisor's lease, or ``(None, None)``.

    Best-effort: any failure (no lease file, unreadable, malformed, expired)
    resolves to ``(None, None)`` and the caller fails loud if env cannot fill
    the gap. The single discovery implementation — the vector client's
    ``_discover_lease`` and the catalog/T2 resolvers all route through here.
    """
    global _has_ever_resolved_lease
    try:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
        from nexus.daemon.service_registry import ServiceRegistry  # noqa: PLC0415 — deferred to avoid circular import

        registry = ServiceRegistry(dir=nexus_config_dir(), tier="storage_service")
        lease = registry.discover(str(os.getuid()))
        if lease is not None:
            ep = lease.endpoint
            host = str(ep.get("host", "127.0.0.1"))
            port = int(ep.get("port", 0))
            token = str(ep.get("token", "")) or None
            if port > 0:
                _has_ever_resolved_lease = True
                return f"http://{host}:{port}", token
    except Exception as exc:  # discovery is best-effort; absence fails loud above  # noqa: BLE001 — best-effort: failure logged, must not crash caller
        _log.debug("service_endpoint_lease_discover_failed", error=str(exc))
    return None, None


def has_ever_resolved_lease() -> bool:
    """True if this process has EVER successfully discovered a live
    ServiceRegistry lease via :func:`discover_lease`.

    See the ``_has_ever_resolved_lease`` module docstring above for why
    this exists and who reads it. Deliberately process-lifetime, never
    reset on an individual failure — a single respawn-gap miss must not
    erase the evidence that this IS a lease-based topology.
    """
    return _has_ever_resolved_lease


def resolve_service_endpoint_with_evidence_gate() -> "tuple[str, str]":
    """:func:`resolve_service_endpoint` with the evidence-gated bounded
    wait (nexus-bgh2j — the GH #1405 residual).

    The canonical gate shape (see
    ``_refreshable_client._resolve_endpoint_with_evidence_gate`` for the
    full nexus-7dsgp rationale): a resolution failure retries with
    ``wait_budget_s=DEFAULT_LEASE_WAIT_BUDGET_S`` ONLY when this process
    has previously discovered a live lease (a respawn-gap symptom); a
    cold process fails fast unchanged. Catches only
    :class:`ServiceEndpointUnresolvableError`, never bare RuntimeError
    (a config-typo parse failure must not burn the wait budget).

    Public here so the STANDALONE (non-mixin) stores — HttpTokenStore and
    the T1 HttpScratchStore, which resolve once at CONSTRUCTION for the
    instance lifetime — get the same gate the nine mixin adopters already
    have; both previously called the bare resolver at construction while
    carrying the gated wait only on their call-time rebind leg.
    """
    try:
        return resolve_service_endpoint()
    except ServiceEndpointUnresolvableError:
        if not has_ever_resolved_lease():
            raise
        return resolve_service_endpoint(wait_budget_s=DEFAULT_LEASE_WAIT_BUDGET_S)


def reset_lease_resolution_history_for_tests() -> None:
    """Test-only: reset the process-wide "ever resolved a lease" signal.

    Unit tests share a process (and therefore this module-global) across
    the whole run; without an explicit reset, one test's successful
    resolution would silently make a LATER, unrelated test's
    construction-time resolution failure retry-with-wait instead of
    failing fast — a cross-test leak in exactly the mechanism nexus-1091
    caught for the T3 side of this bead. Tests that exercise the
    ``has_ever_resolved_lease()``-gated wait call this in setup/teardown.
    """
    global _has_ever_resolved_lease
    _has_ever_resolved_lease = False


def discover_lease_with_wait(
    *,
    budget_s: float = 0.0,
    poll_interval_s: float = DEFAULT_LEASE_POLL_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str | None, str | None]:
    """:func:`discover_lease`, polled up to *budget_s* if the first read misses.

    GH #1405 defect 1 (nexus-7dsgp): a retry landing in the 5-10s window
    between the old supervisor's lease TTL expiry and the new supervisor's
    lease publication sees :func:`discover_lease` return ``(None, None)``
    even though the endpoint is about to become resolvable — the pre-fix
    behavior treated that as a permanent failure. This polls instead of
    failing on the first miss, but only for callers that explicitly opt in
    via a nonzero ``budget_s``.

    ``budget_s=0.0`` (the default) degrades to exactly one
    :func:`discover_lease` call with no sleep — i.e. IDENTICAL behavior to
    calling :func:`discover_lease` directly. This is deliberate: every
    caller of this module resolves ONCE at first attempt (must fail loud
    immediately if the supervisor was never started) and only the
    retry/self-heal path should pass a nonzero budget. Never call this with
    a nonzero budget from a first-attempt resolution.

    ``clock``/``sleep`` are injectable (mirrors
    :class:`~nexus.daemon.service_registry.ServiceRegistry`'s own clock
    pattern) so tests exercise the full budget with zero real wall-clock
    time.
    """
    deadline = clock() + budget_s
    attempts = 1
    result = discover_lease()
    while result[0] is None and clock() < deadline:
        remaining = deadline - clock()
        sleep(min(poll_interval_s, max(0.0, remaining)))
        attempts += 1
        result = discover_lease()
    if budget_s > 0:
        _log.info(
            "service_endpoint_reresolved_retry",
            found=result[0] is not None,
            attempts=attempts,
            budget_s=budget_s,
        )
    return result


def recover_endpoint_from_lease(
    current_base_url: str,
    *,
    wait_budget_s: float = 0.0,
    poll_interval_s: float = DEFAULT_LEASE_POLL_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, str | None] | None:
    """Connection-refused recovery (nexus-om64x).

    After a supervisor ``_respawn`` allocates a NEW port, a long-lived MCP
    process still carries the OLD ``NX_SERVICE_PORT`` in its environment — so
    env-first :func:`resolve_service_config` keeps handing back the dead port and
    a store that resolved ONCE at construction is stuck (session mint + T1
    scratch hit connection-refused until the MCP restarts).

    This consults the :class:`ServiceRegistry` lease DIRECTLY (the supervisor's
    source of truth, republished on every restart — it deliberately bypasses the
    stale env). Returns ``(new_base_url, token)`` when the lease points somewhere
    DIFFERENT from *current_base_url* (the store should rebind + retry), or
    ``None`` when there is no lease or it matches the current endpoint (a genuine
    outage — let the original connection error propagate).

    SCOPE / known residuals (nexus-om64x, P2 targeting the FATAL paths):

    * **Phase-1 restart window**: the old lease is NOT relinquished on SIGTERM; it
      lingers until its heartbeat TTL (~3s) expires, and the NEW lease only
      publishes after the replacement JVM is ready. So a request that fails DURING
      that window sees either the old (==current) lease or no lease → this returns
      ``None`` and the error propagates. A request that arrives once the restart
      has SETTLED (the common case) sees the new lease and recovers via the
      caller's single retry. Closing the in-window case would need a relinquish on
      stop or a backoff loop — out of scope for this P2.
    * **Coverage**: only ``http_token_store`` + ``http_scratch_store`` (the
      session-mint + T1-scratch FATAL paths) wire this recovery today. The other
      long-lived service-backed stores (memory/taxonomy/plan/aspect/chash/…) share
      the resolve-once pattern and remain unguarded — tracked for a sweep follow-on.

    ``wait_budget_s`` (nexus-7dsgp, GH #1405 defect 1): the Phase-1 restart
    window noted above — a request landing DURING the gap sees no lease and
    used to give up immediately. Passing a nonzero budget here polls
    (:func:`discover_lease_with_wait`) instead, closing exactly that window.
    Default ``0.0`` preserves the original immediate-miss behavior for any
    caller that does not opt in.
    """
    # An explicitly-pinned NX_SERVICE_URL (a managed TLS endpoint, or any URL the
    # user named) is authoritative — never silently rebind it to a discovered
    # supervisor lease, which is ALWAYS local http (nexus-n3bwh review H1): the
    # https managed base_url would compare unequal to the http lease and rebind
    # every time, routing managed traffic to the wrong (local) service. Lease
    # recovery is for the lease-discovered path only, and the bounded wait below
    # is NEVER applied to the managed-cloud URL path — this early return skips
    # it entirely (nexus-7dsgp requirement).
    from nexus.config import get_credential  # noqa: PLC0415 — deferred to avoid circular import

    if (get_credential("service_url") or "").strip():
        return None
    lease_url, lease_token = discover_lease_with_wait(
        budget_s=wait_budget_s, poll_interval_s=poll_interval_s, clock=clock, sleep=sleep
    )
    if lease_url is not None and lease_url.rstrip("/") != (current_base_url or "").rstrip("/"):
        return lease_url.rstrip("/"), lease_token
    return None


def env_host_port_url() -> str | None:
    """``http://{host}:{port}`` from the NX_SERVICE_HOST/PORT env halves, or None.

    nexus-edwlp: the T3 vector client historically honored only
    ``NX_SERVICE_URL`` or the lease, while every T2 store also read the
    host/port halves via :func:`resolve_service_config` — so a box with
    HOST/PORT/TOKEN exported (the local-service gate, docs' documented env
    leg) served T2 fine and failed loud on T3. This helper is the shared
    env-halves-to-base-url leg both resolvers agree on. Local-supervisor
    semantics: always ``http``, host defaults to ``127.0.0.1``.
    """
    port_str = os.environ.get("NX_SERVICE_PORT", "").strip()
    if not port_str:
        return None
    try:
        port = int(port_str)
    except ValueError as exc:
        raise RuntimeError(
            f"NX_SERVICE_PORT must be an integer, got: {port_str!r}"
        ) from exc
    host = os.environ.get("NX_SERVICE_HOST", "").strip() or "127.0.0.1"
    return f"http://{host}:{port}"


def resolve_service_config(
    *,
    wait_budget_s: float = 0.0,
    poll_interval_s: float = DEFAULT_LEASE_POLL_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, int, str]:
    """``(host, port, token)`` — env halves, then the lease, then fail loud.

    The local-supervisor 3-tuple — ALWAYS ``http`` (env ``NX_SERVICE_HOST``/
    ``NX_SERVICE_PORT`` carry no scheme; the lease is local http). New HTTP
    storage clients must NOT build ``http://{host}:{port}`` from this: call
    :func:`resolve_service_endpoint` instead, which is scheme-correct and also
    serves managed TLS endpoints (nexus-n3bwh). This function now backs only the
    non-``NX_SERVICE_URL`` fallback leg of :func:`resolve_service_endpoint`.
    Restart-safety note: service-backed clients
    resolve ONCE at construction and hold a long-lived ``httpx.Client`` — they
    ride a supervisor restart only because callers construct a fresh client per
    operation (the ``get_catalog()`` / ``t2_ctx()`` pattern). Do not cache a
    store instance across an operation that may span a restart.

    ``wait_budget_s`` (nexus-7dsgp, GH #1405 defect 1): passed through to
    :func:`discover_lease_with_wait` — ``0.0`` (the default) is a single
    immediate lease read, identical to pre-fix behavior; a caller retrying
    after an unresolvable-endpoint failure passes a nonzero budget to close
    the supervisor-respawn gap. Never pass a nonzero budget from a
    first-attempt resolution (construction-time callers must still fail
    loud immediately when the supervisor was never started).
    """
    env_host = os.environ.get("NX_SERVICE_HOST", "").strip()
    port_str = os.environ.get("NX_SERVICE_PORT", "").strip()
    env_token = os.environ.get("NX_SERVICE_TOKEN", "").strip()

    port: int | None = None
    if port_str:
        try:
            port = int(port_str)
        except ValueError as exc:
            raise RuntimeError(
                f"NX_SERVICE_PORT must be an integer, got: {port_str!r}"
            ) from exc

    host, token = env_host or None, env_token or None
    if port is None or token is None or host is None:
        lease_url, lease_token = discover_lease_with_wait(
            budget_s=wait_budget_s, poll_interval_s=poll_interval_s, clock=clock, sleep=sleep
        )
        if lease_url is not None:
            from urllib.parse import urlsplit  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

            parsed = urlsplit(lease_url)
            host = host or parsed.hostname
            port = port if port is not None else parsed.port
        token = token or lease_token
    host = host or "127.0.0.1"

    if port is None or not token:
        # RDR-155 P4b: the nexus-0rwwv migration-hint bridge died with the
        # migration module; stranded pre-PG installs are redirected by the
        # stranded-install detector at CLI/MCP startup.
        raise ServiceEndpointUnresolvableError(
            "nexus-service endpoint is not resolvable (NX_STORAGE_BACKEND="
            "service): start the supervisor with 'nx daemon service start' "
            "(publishes the endpoint lease this client auto-discovers), or "
            "export NX_SERVICE_PORT / NX_SERVICE_TOKEN (and optionally "
            "NX_SERVICE_HOST) explicitly."
        )
    return host, port, token


def resolve_service_endpoint(
    *,
    wait_budget_s: float = 0.0,
    poll_interval_s: float = DEFAULT_LEASE_POLL_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, str]:
    """``(base_url, token)`` — the scheme-correct base-url authority.

    Every HTTP storage client that builds a base URL (the T2 domain stores, the
    catalog client, the T1 scratch store, the migration pre-gate) routes through
    here so a managed TLS endpoint survives end-to-end. Resolution order mirrors
    the T3 data path (:func:`nexus.db.http_vector_client._resolve_endpoint`):

      1. ``service_url`` — the authoritative FULL endpoint, used VERBATIM
         (scheme + host + port preserved). Resolved via
         :func:`nexus.config.get_credential`, i.e. ``NX_SERVICE_URL`` env FIRST,
         then the persisted ``config.yml`` credential a greenfield user set with
         ``nx config set service_url`` (RDR-166 nexus-v3p0x). This is the ONLY
         scheme source: ``https://api.conexus-nexus.com:443`` stays ``https``;
         flattening it to ``http://…:443`` (the pre-RDR-166 bug, nexus-n3bwh)
         broke every managed migration leg. The token half (``service_token``)
         resolves the same way, then falls back to the lease independently.
      2. Otherwise ``http://{host}:{port}`` from :func:`resolve_service_config`
         (env halves → lease → fail loud) — the local-supervisor path, always
         ``http``.

    ``wait_budget_s`` (nexus-7dsgp, GH #1405 defect 1) applies ONLY to leg 2
    (the local-lease path) — a retry against the managed ``service_url`` leg
    NEVER waits on the lease, by construction: leg 1 returns before
    ``resolve_service_config`` is even called, and its own lease read (the
    token-only fallback) stays an unwrapped, non-waiting :func:`discover_lease`
    call. This is the bead's "never for the managed-cloud URL path" contract
    enforced structurally rather than by a caller-side guard.
    """
    from nexus.config import get_credential  # noqa: PLC0415 — deferred to avoid circular import

    url = (get_credential("service_url") or "").strip().rstrip("/") or None
    if url is not None:
        token = (get_credential("service_token") or "").strip() or None
        if token is None:
            _, token = discover_lease()  # deliberately NOT discover_lease_with_wait — see docstring
        if not token:
            raise RuntimeError(
                "service_url is set but no service_token is resolvable (neither "
                "NX_SERVICE_TOKEN env, config.yml, nor supervisor lease): set it "
                "with `nx config set service_token <bearer>` (or export "
                "NX_SERVICE_TOKEN) and re-run."
            )
        return url, token
    host, port, token = resolve_service_config(
        wait_budget_s=wait_budget_s, poll_interval_s=poll_interval_s, clock=clock, sleep=sleep
    )
    return f"http://{host}:{port}", token


# ── Dev-checkout production-write guard (nexus-a2qhz) ────────────────────────
#
# Three incidents (2026-08-19, 2026-08-21 x2 — see the bead) reached Sam's
# live production T2/T3/catalog substrate from a dev-checkout process running
# OUTSIDE pytest's `_pin_t2_substrate` autouse fixture: a `uv run python -c`
# one-liner, a deliberate arc-end MVV, and a scratchpad verification script
# that imported `tests._catalog_fixture_ops` from outside `tests/`. The
# critic's finding, confirmed on the bead: no guard anywhere in the HTTP
# storage-client path stops this. The design (locked on the bead by three
# recorded incidents, option (a)): gate WRITES with an explicit opt-in, fail
# loud naming the opt-in, never a silent cwd-based redirect.
#
# Detection signal: NOT the process cwd (the third incident's script ran
# from a scratchpad directory entirely outside the checkout — its danger
# came from an IMPORT resolving nexus's package to the checkout's editable
# install, not from where the script itself lived). Instead: does THIS
# module's own file resolve inside a conexus git checkout (a plain clone or
# a worktree — `.git` is a directory for the former, a file for the latter,
# both satisfied by `.exists()`) with a `pyproject.toml` naming the
# `conexus` project? An installed generation (`<tools>/gen-*/.../
# site-packages/nexus/...`) or a plain `uv tool install` copy has no such
# ancestor at any depth — nothing under `site-packages` ships a
# `pyproject.toml`. This is exactly the "nexus package resolves from an
# editable dev checkout" signal the bead asked for, and it transfers to any
# script that imports nexus (or a tests helper that imports nexus), from
# anywhere, regardless of that script's own location.
#
# KNOWN LIMIT (documented, not fixed here): a copied tree with neither a
# `.git` nor a `pyproject.toml` ancestor at any depth — e.g. a Docker build
# stage that `COPY`s `src/` without git metadata — is NOT detected as a dev
# checkout and receives zero guard coverage regardless of env state. See
# docs/architecture.md's "Dev-checkout production-write guard" section.
#
# Production identity, REVISED (nexus-a2qhz, second review round): the
# first cut of this guard exempted a write whenever ANY of `NX_SERVICE_URL`
# / `NX_SERVICE_HOST` / `NX_SERVICE_PORT` / `NX_SERVICE_TOKEN` was set,
# reasoning that an explicit env var meant "the caller deliberately pinned
# this endpoint." That heuristic is DEFEATED by conexus's own documented
# cloud onboarding: docs/getting-started.md and docs/managed-onboarding.md
# both instruct `export NX_SERVICE_URL=https://api.conexus-nexus.com` —
# exactly the permanently-exported shell shape every one of the three
# recorded incidents ran under. An exported env var cannot distinguish "a
# fixture pinned an ephemeral endpoint for THIS process" from "this shell
# always points at the operator's live production service." So the
# exemption is GONE: env-var presence is no longer consulted at all. A
# dev-checkout process is refused on every write regardless of how its
# endpoint resolved — the ONLY bypass is the named opt-in below, which
# requires a REASON (not a bare truthy flag), so pytest's own substrate
# fixtures declare their exemption explicitly (see
# tests/conftest.py's `_exempt_pytest_from_production_write_guard`) rather
# than being silently inferred.

#: The opt-in that lets a dev-checkout process perform a deliberate WRITE
#: against the endpoint it resolved. Named in every refusal. Must carry a
#: REASON — a bare boolean-shaped value (see
#: :data:`_OPT_IN_BOOLEAN_LOOKALIKES`) is refused exactly like an unset
#: var, so the intent stays greppable (nexus-a2qhz review: "1 alone
#: invites reflexive bypass by an agent — two of the three recorded
#: incidents were subagents").
PROD_WRITE_OPT_IN_ENV = "NX_ALLOW_PROD_WRITE"

#: Values that look like a leftover boolean flag rather than a reason —
#: refused exactly like an unset opt-in. Case-insensitive.
_OPT_IN_BOOLEAN_LOOKALIKES: frozenset[str] = frozenset(
    {"0", "1", "true", "false", "yes", "no"}
)

#: Ancestors of this module's own file to inspect while looking for the
#: conexus checkout root. Bounded so a pathological install layout cannot
#: make the walk expensive — a plain checkout
#: (``.../src/nexus/db/service_endpoint.py``) and a git worktree
#: (``.../worktrees/<name>/src/nexus/db/service_endpoint.py``) both find
#: their root within the first few ancestors; an installed generation
#: never finds one at any depth (no ``pyproject.toml`` ships under
#: ``site-packages``).
_DEV_CHECKOUT_SEARCH_DEPTH = 12

#: Sentinel distinguishing "never computed" from a genuine ``None`` result
#: in the process-wide cache below (the checkout root cannot change for
#: the life of the interpreter, so this is computed at most once per
#: process on the real, uncached call shape).
_DEV_CHECKOUT_ROOT_UNSET: Any = object()
_dev_checkout_root_cache: Any = _DEV_CHECKOUT_ROOT_UNSET


class ProductionWriteGuardError(RuntimeError):
    """Raised by :func:`guard_production_write` when a dev-checkout process
    attempts an HTTP WRITE without the explicit, reason-bearing
    :data:`PROD_WRITE_OPT_IN_ENV` opt-in.

    Design of record: bead nexus-a2qhz. Reads are unaffected; only writes
    route through :func:`guard_production_write` — every
    ``RefreshableHttpStoreMixin`` adopter (the T2 domain stores and the
    catalog client, which share that one transport), T3's module-level
    ``_post``, and the two bespoke (non-mixin) clients, ``HttpTokenStore``
    and ``HttpScratchStore`` (T1).
    """


def _compute_dev_checkout_root(start: Path) -> Path | None:
    """The conexus checkout root *start* resolves under, or ``None``."""
    try:
        here = start.resolve()
    except OSError:
        return None
    for ancestor in list(here.parents)[:_DEV_CHECKOUT_SEARCH_DEPTH]:
        pyproject = ancestor / "pyproject.toml"
        git_marker = ancestor / ".git"
        if not (pyproject.is_file() and git_marker.exists()):
            continue
        try:
            import tomllib  # noqa: PLC0415 — deferred, stdlib; avoids module-load cost on the common (non-dev) path

            data = tomllib.loads(pyproject.read_text())
        except Exception:  # noqa: BLE001 — a malformed/foreign pyproject.toml at some unrelated ancestor is not a match, never a crash
            continue
        if data.get("project", {}).get("name") == "conexus":
            return ancestor
    return None


def _dev_checkout_root(start: Path | None = None) -> Path | None:
    """The conexus checkout root this process's ``nexus`` package resolves
    from, or ``None`` when it resolves from an installed (non-editable)
    copy.

    *start* defaults to THIS module's own file — ``nexus/db/
    service_endpoint.py`` — deliberately never ``sys.argv[0]`` or the
    caller's cwd (see the module-level design comment above). Passing
    *start* explicitly bypasses the process-wide cache entirely and is for
    tests only; real callers always take the default and pay the
    filesystem walk at most once per process.
    """
    global _dev_checkout_root_cache
    if start is not None:
        return _compute_dev_checkout_root(start)
    if _dev_checkout_root_cache is _DEV_CHECKOUT_ROOT_UNSET:
        _dev_checkout_root_cache = _compute_dev_checkout_root(Path(__file__))
    return _dev_checkout_root_cache


def is_dev_checkout_process() -> bool:
    """True when this process's ``nexus`` package resolves from a conexus
    dev checkout (editable install or git worktree) rather than an
    installed generation / ``uv tool install`` copy."""
    return _dev_checkout_root() is not None


def reset_dev_checkout_cache_for_tests() -> None:
    """Test-only: force the next :func:`_dev_checkout_root` (default-arg)
    call to recompute rather than reuse the process-wide cache."""
    global _dev_checkout_root_cache
    _dev_checkout_root_cache = _DEV_CHECKOUT_ROOT_UNSET


#: Test-only in-process override for the accepted opt-in reason — NEVER an
#: env var, precisely so it cannot leak into a subprocess's inherited
#: ``os.environ`` the way a real ``NX_ALLOW_PROD_WRITE`` would (nexus-a2qhz
#: round-2 review: the blanket pytest exemption previously used
#: ``monkeypatch.setenv``, which DOES mutate the real process
#: ``os.environ`` for the test's duration — any subprocess a test spawns
#: via ``env=os.environ.copy()`` silently inherited the reason regardless
#: of whether that subprocess's OWN substrate was correctly pinned).
#: ``None`` (the default) means "no override" — the real env var still
#: resolves normally. Set only via :func:`set_test_only_opt_in_reason_for_tests`.
_test_only_opt_in_reason: str | None = None


def set_test_only_opt_in_reason_for_tests(reason: str | None) -> None:
    """Test-only in-process override for :func:`guard_production_write`'s
    opt-in check — see :data:`_test_only_opt_in_reason`'s docstring.

    ``tests/conftest.py``'s blanket pytest exemption calls this via
    ``monkeypatch.setattr(service_endpoint, "_test_only_opt_in_reason",
    "<reason>")`` (still undone per test, same as any other monkeypatch)
    instead of setting the real env var — the exemption then never
    appears in ``os.environ`` and cannot leak into a subprocess a test
    spawns. A test that spawns a subprocess needing the REAL guard
    behavior (e.g. a genuine dev-checkout write against the test
    substrate) must set the real ``NX_ALLOW_PROD_WRITE`` env var
    explicitly for that subprocess's own environment — this override
    covers only THIS process's in-process store constructions.
    """
    global _test_only_opt_in_reason
    _test_only_opt_in_reason = reason


def _opt_in_reason() -> str | None:
    """The caller's stated reason for bypassing the guard, or ``None`` when
    no valid opt-in is present.

    Checks :data:`_test_only_opt_in_reason` FIRST (test-only, never an env
    var) — when set, it wins outright, matching the "later call wins"
    contract every other autouse-fixture override in this codebase
    follows. Otherwise: a valid opt-in is a non-empty
    :data:`PROD_WRITE_OPT_IN_ENV` value that is not a boolean lookalike
    (:data:`_OPT_IN_BOOLEAN_LOOKALIKES`, checked case-insensitively) — a
    bare ``"1"`` (the RETIRED spelling) or a leftover
    ``"0"``/``"true"``/``"false"``/``"yes"``/``"no"`` all fail this check
    exactly like an unset var. Anything else is treated as a reason and
    returned verbatim (for logging/messages) — this function does not,
    and cannot, judge whether the text is a GOOD reason, only that it is
    not a rebadged boolean flag.
    """
    if _test_only_opt_in_reason is not None:
        return _test_only_opt_in_reason
    raw = (os.environ.get(PROD_WRITE_OPT_IN_ENV, "") or "").strip()
    if not raw or raw.lower() in _OPT_IN_BOOLEAN_LOOKALIKES:
        return None
    return raw


def guard_production_write(base_url: str) -> None:
    """Refuse an HTTP WRITE to *base_url* from a dev-checkout process
    unless :data:`PROD_WRITE_OPT_IN_ENV` carries an explicit reason.

    Called from every HTTP storage client's write path: every
    ``RefreshableHttpStoreMixin`` adopter's ``_send`` (T2 domain stores +
    the catalog client) for non-GET verbs with ``mutates=True``, T3's
    module-level ``_post`` for its write-shaped path suffixes, and the two
    bespoke (non-mixin) clients' own write methods — ``HttpTokenStore``
    and ``HttpScratchStore`` (T1).

    Exactly two conditions exempt a write, checked in this order:

    1. A REASON-bearing opt-in is present (:func:`_opt_in_reason`) — a
       visible, greppable, reviewable statement that THIS write is
       deliberate (e.g. an arc-end MVV that must exercise the real
       substrate). Endpoint-resolution env vars (``NX_SERVICE_URL`` etc.)
       are NEVER consulted here — a permanently-exported cloud
       ``NX_SERVICE_URL`` (conexus's own documented onboarding) must not
       silently exempt a write, since that is the exact shell shape every
       recorded incident ran under.
    2. This process is not a dev checkout (an installed generation, a
       plain ``uv tool install``, or any process whose ``nexus`` package
       did not resolve from an editable checkout) — Sam's real installed
       ``nx`` must never trip this, regardless of env state.

    Otherwise raises :class:`ProductionWriteGuardError` — a stop-and-verify
    message, not a fix-it instruction: it names the target as the
    operator's real store, states that the write must be deliberate and
    reviewed, and requires a reason rather than inviting a reflexive
    export.

    Every ACCEPTED opt-in is logged at WARNING (nexus-a2qhz round-2
    review: "auditable after the fact" was previously just a docstring
    claim — nothing persisted past the process's own environment; INFO
    would be filtered by default in most real deployments and by this
    suite's own ``pytest_configure``, which defaults ``--log-level`` to
    WARNING). Logged only when the guard actually ENGAGES (a dev-checkout
    process) — an installed ``nx`` with a stray ``NX_ALLOW_PROD_WRITE``
    lingering in its environment never reaches this branch at all, so
    ordinary production traffic never pays for a log line the guard never
    needed to consult.
    """
    checkout_root = _dev_checkout_root()
    if checkout_root is None:
        return
    reason = _opt_in_reason()
    if reason is not None:
        # WARNING, not INFO: a dev-checkout process about to write past
        # this guard is a genuinely unusual, audit-worthy event even when
        # correctly exempted -- INFO is filtered by default in most
        # real-world deployments and by this suite's own pytest_configure
        # (WARNING is the default --log-level), which would make the
        # "auditable after the fact" claim false again by construction.
        _log.warning(
            "guard_production_write.opt_in_accepted",
            base_url=base_url,
            checkout_root=str(checkout_root),
            reason=reason,
        )
        return
    raise ProductionWriteGuardError(
        f"STOP: refusing a WRITE to {base_url!r}. This process's nexus "
        f"package resolves from a dev checkout ({checkout_root}), so this "
        "write is presumed to target the OPERATOR'S REAL, LIVE STORE — "
        "the same substrate a real install reaches — regardless of how "
        "the endpoint was resolved (an exported NX_SERVICE_URL included). "
        "Do not bypass this reflexively. If, and only if, this write is "
        "genuinely deliberate and has been reviewed, state WHY in the "
        f"opt-in itself: {PROD_WRITE_OPT_IN_ENV}=\"<why this write is "
        f"safe and intended>\" (a bare \"1\", or any other boolean-shaped "
        "value, is refused exactly like an unset var — name the reason)."
    )
