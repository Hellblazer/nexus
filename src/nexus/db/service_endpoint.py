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

import structlog

_log = structlog.get_logger(__name__)


def discover_lease() -> tuple[str | None, str | None]:
    """``(base_url, token)`` from the supervisor's lease, or ``(None, None)``.

    Best-effort: any failure (no lease file, unreadable, malformed, expired)
    resolves to ``(None, None)`` and the caller fails loud if env cannot fill
    the gap. The single discovery implementation — the vector client's
    ``_discover_lease`` and the catalog/T2 resolvers all route through here.
    """
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
                return f"http://{host}:{port}", token
    except Exception as exc:  # discovery is best-effort; absence fails loud above  # noqa: BLE001 — best-effort: failure logged, must not crash caller
        _log.debug("service_endpoint_lease_discover_failed", error=str(exc))
    return None, None


def recover_endpoint_from_lease(current_base_url: str) -> tuple[str, str | None] | None:
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
    """
    # An explicitly-pinned NX_SERVICE_URL (a managed TLS endpoint, or any URL the
    # user named) is authoritative — never silently rebind it to a discovered
    # supervisor lease, which is ALWAYS local http (nexus-n3bwh review H1): the
    # https managed base_url would compare unequal to the http lease and rebind
    # every time, routing managed traffic to the wrong (local) service. Lease
    # recovery is for the lease-discovered path only.
    from nexus.config import get_credential  # noqa: PLC0415 — deferred to avoid circular import

    if (get_credential("service_url") or "").strip():
        return None
    lease_url, lease_token = discover_lease()
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


def resolve_service_config() -> tuple[str, int, str]:
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
        lease_url, lease_token = discover_lease()
        if lease_url is not None:
            from urllib.parse import urlsplit  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

            parsed = urlsplit(lease_url)
            host = host or parsed.hostname
            port = port if port is not None else parsed.port
        token = token or lease_token
    host = host or "127.0.0.1"

    if port is None or not token:
        # nexus-0rwwv: an un-migrated 5.x→6.x install hits this wall with a
        # stock remedy that is WRONG for it — append the migration pointer.
        from nexus.migration.guided_upgrade import (  # noqa: PLC0415 — deferred import — the bridge dies with the migration module at RDR-155 P4b
            endpoint_failure_migration_hint,
        )

        raise RuntimeError(
            "nexus-service endpoint is not resolvable (NX_STORAGE_BACKEND="
            "service): start the supervisor with 'nx daemon service start' "
            "(publishes the endpoint lease this client auto-discovers), or "
            "export NX_SERVICE_PORT / NX_SERVICE_TOKEN (and optionally "
            "NX_SERVICE_HOST) explicitly." + endpoint_failure_migration_hint()
        )
    return host, port, token


def resolve_service_endpoint() -> tuple[str, str]:
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
    """
    from nexus.config import get_credential  # noqa: PLC0415 — deferred to avoid circular import

    url = (get_credential("service_url") or "").strip().rstrip("/") or None
    if url is not None:
        token = (get_credential("service_token") or "").strip() or None
        if token is None:
            _, token = discover_lease()
        if not token:
            raise RuntimeError(
                "service_url is set but no service_token is resolvable (neither "
                "NX_SERVICE_TOKEN env, config.yml, nor supervisor lease): set it "
                "with `nx config set service_token <bearer>` (or export "
                "NX_SERVICE_TOKEN) and re-run."
            )
        return url, token
    host, port, token = resolve_service_config()
    return f"http://{host}:{port}", token
