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

_log = structlog.get_logger(__name__)

#: Where the managed multitenant nexus service lives by default. Overridable per
#: half via ``NX_SERVICE_URL`` (base) / ``NX_SERVICE_TOKEN`` (bearer) — the same
#: env vars the local HTTP vector client honours, so a single override re-points
#: every client at a staging or self-hosted managed deployment.
DEFAULT_MANAGED_SERVICE_URL = "https://api.conexus-nexus.com"

#: Minimum managed-service RELEASE this client speaks (nexus-x2g1z, 2026-06-24).
#:
#: The gate pins on the dedicated ``release_version`` field of the ``/version``
#: handshake, NOT ``app_version``. ``app_version`` is the JAR's frozen Maven
#: coordinate ``1.0-SNAPSHOT`` (parses to ``(1,0,0)``) — pinning on it was a
#: structural NO-OP (any build cleared it). ``release_version`` is the real
#: release identity: stamped from the ``engine-service-vX.Y.Z`` git tag at
#: build time and ``null``/dev/SNAPSHOT on unstamped builds, so it FAILS CLOSED
#: by construction (mirrors ``guided_upgrade.verify_service_version`` /
#: RDR-002 for the native binary).
#:
#: This is the MANAGED cloud floor, deliberately SEPARATE from the native floor
#: ``guided_upgrade.REQUIRED_RELEASE_VERSION`` (different topology / deploy
#: cadence) — both currently ``(0,1,8)`` but free to move independently.
#:
#: CROSS-REPO CONTRACT (conexus RDR-001): the managed multitenant service MUST
#: expose ``GET /version`` UNAUTHENTICATED with ``release_version`` (and
#: ``app_version``). conexus relay [4566] (2026-06-23) confirmed the managed
#: ``/version`` now returns ``release_version`` and was trimmed to
#: ``{app_version, release_version}`` (the embedding-mode / model / schema
#: disclosure was dropped from the public endpoint).
MIN_MANAGED_RELEASE_VERSION: tuple[int, int, int] = (0, 1, 8)

#: Probe timeout — short, so an unreachable managed service fails fast and loud
#: rather than hanging a CLI command.
_PROBE_TIMEOUT_S = 5.0

_HttpGet = Callable[[str, float], httpx.Response]


class ManagedServiceError(RuntimeError):
    """Base class for managed-service config / probe failures (fail-loud)."""


class ManagedServiceUnreachable(ManagedServiceError):
    """The managed service could not be reached (DNS / TLS / connect / timeout)."""


class ManagedServiceIncompatible(ManagedServiceError):
    """The managed service answered but is misconfigured or version-incompatible."""


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


def _parse_release_version(raw: str | None) -> tuple[int, int, int] | None:
    """FAIL-CLOSED ``X.Y.Z`` parse for the managed ``release_version`` gate.

    Returns ``None`` (caller refuses) for a blank, ``snapshot``/``dev``-qualified,
    or otherwise non-clean-release value — a dev/unstamped engine is by
    definition older than any required release. Trailing qualifiers
    (``-rc1``, ``+meta``, a 4th segment) are rejected, not silently accepted.
    Mirrors ``guided_upgrade._parse_semver`` (RDR-002); kept local to avoid a
    ``db`` -> ``migration`` import.
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    if s[:1] in ("v", "V"):
        s = s[1:]
    lower = s.lower()
    if "snapshot" in lower or "dev" in lower:
        return None
    parts = s.split(".")
    if len(parts) != 3:
        return None
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError:
        return None
    if major < 0 or minor < 0 or patch < 0:
        return None
    return (major, minor, patch)


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
      or a ``release_version`` below :data:`MIN_MANAGED_RELEASE_VERSION` →
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
    parsed = _parse_release_version(release_version)
    if parsed is None:
        floor = ".".join(str(p) for p in MIN_MANAGED_RELEASE_VERSION)
        raise ManagedServiceIncompatible(
            f"managed nexus service at {base_url} reported no usable "
            f"release_version on /version (got {release_raw!r}) — a "
            f"dev/unstamped or pre-release engine is older than the minimum "
            f"this client supports (v{floor}). Confirm NX_SERVICE_URL points "
            "at a current nexus managed service."
        )
    if parsed < MIN_MANAGED_RELEASE_VERSION:
        floor = ".".join(str(p) for p in MIN_MANAGED_RELEASE_VERSION)
        raise ManagedServiceIncompatible(
            f"managed nexus service at {base_url} is release_version "
            f"{release_version!r}, below the minimum this client supports "
            f"(v{floor}). Upgrade the managed service, or upgrade/downgrade "
            "the nx client to match."
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
