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
        from nexus.config import nexus_config_dir
        from nexus.daemon.service_registry import ServiceRegistry

        registry = ServiceRegistry(dir=nexus_config_dir(), tier="storage_service")
        lease = registry.discover(str(os.getuid()))
        if lease is not None:
            ep = lease.endpoint
            host = str(ep.get("host", "127.0.0.1"))
            port = int(ep.get("port", 0))
            token = str(ep.get("token", "")) or None
            if port > 0:
                return f"http://{host}:{port}", token
    except Exception as exc:  # discovery is best-effort; absence fails loud above
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
    lease_url, lease_token = discover_lease()
    if lease_url is not None and lease_url.rstrip("/") != (current_base_url or "").rstrip("/"):
        return lease_url.rstrip("/"), lease_token
    return None


def resolve_service_config() -> tuple[str, int, str]:
    """``(host, port, token)`` — env halves, then the lease, then fail loud.

    The shape the T2 domain stores and the catalog client consume (they build
    ``http://{host}:{port}`` themselves). Restart-safety note: these clients
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
            from urllib.parse import urlsplit

            parsed = urlsplit(lease_url)
            host = host or parsed.hostname
            port = port if port is not None else parsed.port
        token = token or lease_token
    host = host or "127.0.0.1"

    if port is None or not token:
        raise RuntimeError(
            "nexus-service endpoint is not resolvable (NX_STORAGE_BACKEND="
            "service): start the supervisor with 'nx daemon service start' "
            "(publishes the endpoint lease this client auto-discovers), or "
            "export NX_SERVICE_PORT / NX_SERVICE_TOKEN (and optionally "
            "NX_SERVICE_HOST) explicitly."
        )
    return host, port, token


def resolve_service_endpoint() -> tuple[str, str]:
    """``(base_url, token)`` — the shape the T3 vector client consumes.

    Thin adapter over :func:`resolve_service_config` so the vector client and
    the T2/catalog stores share one resolution path despite their differing
    return shapes.
    """
    host, port, token = resolve_service_config()
    return f"http://{host}:{port}", token
