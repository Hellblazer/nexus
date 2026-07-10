# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Cloud-mode managed-service endpoint config + capability probe (nexus-vwvv5.12).

RDR-001 consumer requirement (multitenant cloud service). The corrected cloud
topology: in cloud mode there is **no local Java service and no local Postgres**.
The ``nx`` CLI + local MCP server talk HTTPS to the *managed* nexus service
(``https://api.conexus-nexus.com``), which owns its cloud PG + pgvector entirely
server-side. INVARIANT: the local Java service connects ONLY to a LOCAL Postgres,
never remote — so this module never opens a Postgres connection, never provisions,
never runs Liquibase. It only:

  1. :func:`resolve_managed_endpoint` — where the managed service lives
     (default :data:`DEFAULT_MANAGED_SERVICE_URL`; ``NX_SERVICE_URL`` /
     ``NX_SERVICE_TOKEN`` env override, shared with the local HTTP vector client).
  2. :func:`probe_managed_service` — an HTTP reachability + capability/version
     compatibility check against the unauthenticated ``GET /version`` handshake
     (see the Java ``VersionHandler``) that FAILS LOUD with a remedy when the
     service is unreachable or incompatible. No silent fallback.

The pgvector ``>=0.8`` (iterative_scan) floor is the managed service's own
server-side concern (conexus RDR-001), not a client check.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

import httpx
import structlog

from nexus.engine_version import REQUIRED_ENGINE_VERSION, parse_engine_version

_log = structlog.get_logger(__name__)

#: Where the managed multitenant nexus service lives by default. Overridable per
#: half via ``NX_SERVICE_URL`` (base) / ``NX_SERVICE_TOKEN`` (bearer) — the same
#: env vars the local HTTP vector client honours, so a single override re-points
#: every client at a staging or self-hosted managed deployment.
DEFAULT_MANAGED_SERVICE_URL = "https://api.conexus-nexus.com"

#: CROSS-REPO CONTRACT (conexus RDR-001): the managed multitenant service MUST
#: expose ``GET /version`` UNAUTHENTICATED with ``release_version`` (and
#: ``app_version``). conexus relay [4566] (2026-06-23) confirmed the managed
#: ``/version`` now returns ``release_version`` and was trimmed to
#: ``{app_version, release_version}`` (the embedding-mode / model / schema
#: disclosure was dropped from the public endpoint).
#:
#: The version floor itself (:data:`nexus.engine_version.REQUIRED_ENGINE_VERSION`)
#: is the SAME floor the native/local path enforces
#: (:func:`nexus.migration.guided_upgrade.verify_service_version`) — nexus-b6qlf
#: unified what used to be two independently-drifting constants (this module's
#: own ``MIN_MANAGED_RELEASE_VERSION`` was introduced at ``(0,1,8)`` in
#: nexus-x2g1z, 2026-06-24, and never bumped again while the native floor
#: moved to ``(0,1,34)`` — exactly the drift a single source of truth
#: prevents).

#: Probe timeout — short, so an unreachable managed service fails fast and loud
#: rather than hanging a CLI command.
_PROBE_TIMEOUT_S = 5.0

_HttpGet = Callable[[str, float], httpx.Response]


class ManagedServiceError(RuntimeError):
    """Base class for managed-service config / probe failures (fail-loud)."""


class ManagedServiceUnreachable(ManagedServiceError):
    """The managed service could not be reached (DNS / TLS / connect / timeout)."""


class ManagedServiceIncompatible(ManagedServiceError):
    """The managed service answered but is misconfigured or version-incompatible.

    ``deployed_version`` / ``required_version`` are OPTIONAL structured
    fields (nexus-b6qlf Fix 2) populated only by the below-floor raise site
    in :func:`probe_managed_service` -- the one case where the message would
    otherwise embed the client's own "upgrade the managed service, or
    upgrade/downgrade the nx client to match" remedy verbatim, which
    self-contradicts a cloud-mode wrapper's "cannot be fixed locally"
    framing (see :func:`nexus.db.http_vector_client._cloud_probe_failure_message`).
    Every other raise site in this module (no token, non-200, non-JSON, no
    usable release_version) constructs this exception with just a message,
    so both fields default to ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        deployed_version: str | None = None,
        required_version: str | None = None,
    ) -> None:
        super().__init__(message)
        self.deployed_version = deployed_version
        self.required_version = required_version


@dataclass(frozen=True)
class ManagedCapabilities:
    """What the managed service reported on its ``/version`` handshake."""

    base_url: str
    app_version: str
    #: The release identity the version gate pins on (nexus-x2g1z). ``""`` only
    #: for a self-hosted service that predates the field — the probe refuses
    #: before constructing caps in that case, so a returned caps always carries
    #: a real release.
    release_version: str
    #: embedding_mode / models / schema_* are informational and OPTIONAL: the
    #: managed public ``/version`` was trimmed to ``{app_version,
    #: release_version}`` (conexus relay [4566]); a self-hosted/local service
    #: may still report them. Absent -> ``"unknown"`` / ``[]`` / ``None``.
    embedding_mode: str
    embedding_models: list[str]
    schema_latest_id: str | None
    schema_changeset_count: int | None


def resolve_managed_endpoint(*, require_token: bool = True) -> tuple[str, str | None]:
    """Return ``(base_url, token)`` for the managed service.

    ``base_url`` is the resolved ``service_url`` (trailing slash stripped) or
    :data:`DEFAULT_MANAGED_SERVICE_URL`. ``token`` is the resolved
    ``service_token``. Both resolve via :func:`nexus.config.get_credential` —
    env (``NX_SERVICE_URL`` / ``NX_SERVICE_TOKEN``) FIRST, then the persisted
    ``config.yml`` credential a greenfield user set with ``nx config set``
    (RDR-166 nexus-v3p0x). Without this the probe would ignore a config.yml-only
    user's endpoint and silently target the default.

    Fails loud (:class:`ManagedServiceIncompatible`) when ``require_token`` and no
    token is configured — a cloud-mode client with no bearer cannot call any
    ``/v1/*`` route, so a silent ``None`` would only defer the failure to an
    opaque 401 later. The unauthenticated ``/version`` probe itself does not need
    the token; ``require_token=False`` supports probe-only callers.
    """
    from nexus.config import get_credential  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import

    base = (get_credential("service_url") or "").strip().rstrip("/") or DEFAULT_MANAGED_SERVICE_URL
    token = (get_credential("service_token") or "").strip() or None
    if require_token and not token:
        raise ManagedServiceIncompatible(
            "cloud mode is configured but NX_SERVICE_TOKEN is not set — the "
            f"managed service at {base} requires a bearer token for /v1/* calls. "
            "Set it with `nx config set` or export NX_SERVICE_TOKEN=<token> "
            "(and NX_SERVICE_URL to override the default managed endpoint)."
        )
    return base, token


def probe_managed_service(
    *,
    base_url: str | None = None,
    token: str | None = None,
    http_get: _HttpGet | None = None,
    timeout: float = _PROBE_TIMEOUT_S,
) -> ManagedCapabilities:
    """Probe ``GET {base}/version`` for reachability + compatibility (fail loud).

    * Unreachable (connect / TLS / DNS / timeout) → :class:`ManagedServiceUnreachable`.
    * Non-200, a missing / null / dev / SNAPSHOT / unparseable ``release_version``,
      or a ``release_version`` below :data:`nexus.engine_version.REQUIRED_ENGINE_VERSION` →
      :class:`ManagedServiceIncompatible` (the gate FAILS CLOSED). ``app_version``
      is informational only and is not gated (nexus-x2g1z).
    * Otherwise returns the parsed :class:`ManagedCapabilities`.

    ``base_url`` defaults to :func:`resolve_managed_endpoint` (probe-only, so no
    token is required). ``http_get`` is injectable for tests; production uses
    ``httpx.get``. The ``/version`` route is unauthenticated by contract (Java
    ``VersionHandler``), so the probe sends no bearer.
    """
    if base_url is None:
        base_url, _ = resolve_managed_endpoint(require_token=False)
    base_url = base_url.rstrip("/")
    url = f"{base_url}/version"

    if http_get is None:
        def http_get(u: str, t: float) -> httpx.Response:
            return httpx.get(u, timeout=t)

    try:
        resp = http_get(url, timeout)
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        # Reachability failure: connect / TLS / DNS / read / timeout. Any other
        # exception (a programming error, a bad injected callable) propagates.
        _log.debug("managed_service_unreachable", url=url, error=str(exc))
        raise ManagedServiceUnreachable(
            f"managed nexus service at {base_url} is unreachable "
            f"({type(exc).__name__}: {exc}). Check connectivity, or set "
            "NX_SERVICE_URL to point at a reachable managed endpoint."
        ) from exc

    if resp.status_code != 200:
        raise ManagedServiceIncompatible(
            f"managed nexus service at {base_url} answered /version with HTTP "
            f"{resp.status_code} (expected 200). The endpoint may not be a nexus "
            "managed service, or it is unhealthy — check NX_SERVICE_URL and the "
            "service status page."
        )

    try:
        body = resp.json()
    except Exception as exc:
        raise ManagedServiceIncompatible(
            f"managed nexus service at {base_url} returned a non-JSON /version "
            f"body — not a nexus managed service? ({exc})"
        ) from exc

    # app_version is informational only (the JAR's frozen 1.0-SNAPSHOT
    # coordinate); the gate pins on release_version below. nexus-x2g1z.
    app_version = str(body.get("app_version") or "").strip()

    # Version gate: pin on the dedicated release_version field, FAIL-CLOSED.
    # A missing / null / blank / dev / SNAPSHOT / unparseable release_version
    # means a dev/unstamped engine, which is by definition below the floor.
    release_raw = body.get("release_version")
    release_version = str(release_raw).strip() if isinstance(release_raw, str) else ""
    parsed = parse_engine_version(release_version)
    if parsed is None:
        floor = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
        raise ManagedServiceIncompatible(
            f"managed nexus service at {base_url} reported no usable "
            f"release_version on /version (got {release_raw!r}) — a "
            f"dev/unstamped or pre-release engine is older than the minimum "
            f"this client supports (v{floor}). Confirm NX_SERVICE_URL points "
            "at a current nexus managed service."
        )
    if parsed < REQUIRED_ENGINE_VERSION:
        floor = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
        raise ManagedServiceIncompatible(
            f"managed nexus service at {base_url} is release_version "
            f"{release_version!r}, below the minimum this client supports "
            f"(v{floor}). Upgrade the managed service, or upgrade/downgrade "
            "the nx client to match.",
            deployed_version=release_version,
            required_version=floor,
        )

    models_raw = body.get("embedding_models") or []
    models = [str(m) for m in models_raw] if isinstance(models_raw, list) else []
    sc_count = body.get("schema_changeset_count")
    # bool is an int subclass in Python — reject a stray `true` from the server.
    sc_count_ok = isinstance(sc_count, int) and not isinstance(sc_count, bool)
    caps = ManagedCapabilities(
        base_url=base_url,
        app_version=app_version,
        release_version=release_version,
        embedding_mode=str(body.get("embedding_mode") or "unknown"),
        embedding_models=models,
        schema_latest_id=(str(body["schema_latest_id"]) if body.get("schema_latest_id") else None),
        schema_changeset_count=(int(sc_count) if sc_count_ok else None),
    )
    _log.debug(
        "managed_service_probe_ok",
        base_url=base_url,
        app_version=app_version,
        release_version=release_version,
        embedding_mode=caps.embedding_mode,
    )
    return caps
