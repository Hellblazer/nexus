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
from typing import TYPE_CHECKING, Callable

import structlog

if TYPE_CHECKING:
    import httpx

_log = structlog.get_logger(__name__)

#: Where the managed multitenant nexus service lives by default. Overridable per
#: half via ``NX_SERVICE_URL`` (base) / ``NX_SERVICE_TOKEN`` (bearer) — the same
#: env vars the local HTTP vector client honours, so a single override re-points
#: every client at a staging or self-hosted managed deployment.
DEFAULT_MANAGED_SERVICE_URL = "https://api.conexus-nexus.com"

#: Minimum managed-service ``app_version`` this client speaks. The ``/version``
#: handshake is the cross-repo contract surface (conexus RDR-001); bump this floor
#: when the client starts to depend on a newer managed-service capability. Parsed
#: leniently (Maven ``-SNAPSHOT`` / qualifier suffixes are stripped).
MIN_MANAGED_APP_VERSION: tuple[int, int, int] = (1, 0, 0)

#: Probe timeout — short, so an unreachable managed service fails fast and loud
#: rather than hanging a CLI command.
_PROBE_TIMEOUT_S = 5.0

_HttpGet = Callable[[str, float], "httpx.Response"]


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
    embedding_mode: str
    embedding_models: list[str]
    schema_latest_id: str | None
    schema_changeset_count: int | None


def resolve_managed_endpoint(*, require_token: bool = True) -> tuple[str, str | None]:
    """Return ``(base_url, token)`` for the managed service.

    ``base_url`` is ``NX_SERVICE_URL`` (trailing slash stripped) or
    :data:`DEFAULT_MANAGED_SERVICE_URL`. ``token`` is ``NX_SERVICE_TOKEN``.

    Fails loud (:class:`ManagedServiceIncompatible`) when ``require_token`` and no
    token is configured — a cloud-mode client with no bearer cannot call any
    ``/v1/*`` route, so a silent ``None`` would only defer the failure to an
    opaque 401 later. The unauthenticated ``/version`` probe itself does not need
    the token; ``require_token=False`` supports probe-only callers.
    """
    base = os.environ.get("NX_SERVICE_URL", "").strip().rstrip("/") or DEFAULT_MANAGED_SERVICE_URL
    token = os.environ.get("NX_SERVICE_TOKEN", "").strip() or None
    if require_token and not token:
        raise ManagedServiceIncompatible(
            "cloud mode is configured but NX_SERVICE_TOKEN is not set — the "
            f"managed service at {base} requires a bearer token for /v1/* calls. "
            "Set it with `nx config set` or export NX_SERVICE_TOKEN=<token> "
            "(and NX_SERVICE_URL to override the default managed endpoint)."
        )
    return base, token


def _parse_version(raw: str) -> tuple[int, int, int] | None:
    """Lenient ``major.minor.patch`` parse; ``-SNAPSHOT``/qualifiers stripped.

    Returns ``None`` when no leading numeric version is present (e.g. ``"unknown"``).
    """
    digits: list[str] = []
    for ch in raw.strip():
        if ch.isdigit() or ch == ".":
            digits.append(ch)
        else:
            break
    core = "".join(digits).strip(".")
    if not core:
        return None
    parts = core.split(".")[:3]
    try:
        nums = [int(p) for p in parts if p != ""]
    except ValueError:
        return None
    if not nums:
        return None
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def probe_managed_service(
    *,
    base_url: str | None = None,
    token: str | None = None,
    http_get: _HttpGet | None = None,
    timeout: float = _PROBE_TIMEOUT_S,
) -> ManagedCapabilities:
    """Probe ``GET {base}/version`` for reachability + compatibility (fail loud).

    * Unreachable (connect / TLS / DNS / timeout) → :class:`ManagedServiceUnreachable`.
    * Non-200, missing/``unknown`` ``app_version``, or an ``app_version`` below
      :data:`MIN_MANAGED_APP_VERSION` → :class:`ManagedServiceIncompatible`.
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
        import httpx

        def http_get(u: str, t: float) -> "httpx.Response":  # noqa: ANN202
            return httpx.get(u, timeout=t)

    try:
        resp = http_get(url, timeout)
    except Exception as exc:  # httpx.ConnectError / TimeoutException / TransportError
        import httpx

        if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
            _log.debug("managed_service_unreachable", url=url, error=str(exc))
            raise ManagedServiceUnreachable(
                f"managed nexus service at {base_url} is unreachable "
                f"({type(exc).__name__}: {exc}). Check connectivity, or set "
                "NX_SERVICE_URL to point at a reachable managed endpoint."
            ) from exc
        raise

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

    app_version = str(body.get("app_version") or "").strip()
    if not app_version or app_version == "unknown":
        raise ManagedServiceIncompatible(
            f"managed nexus service at {base_url} reported no app_version on "
            "/version — cannot verify compatibility. Confirm NX_SERVICE_URL "
            "points at a nexus managed service."
        )

    parsed = _parse_version(app_version)
    if parsed is None or parsed < MIN_MANAGED_APP_VERSION:
        floor = ".".join(str(p) for p in MIN_MANAGED_APP_VERSION)
        raise ManagedServiceIncompatible(
            f"managed nexus service at {base_url} is app_version {app_version!r}, "
            f"below the minimum this client supports ({floor}). Upgrade the "
            "managed service, or upgrade/downgrade the nx client to match."
        )

    models_raw = body.get("embedding_models") or []
    models = [str(m) for m in models_raw] if isinstance(models_raw, list) else []
    sc_count = body.get("schema_changeset_count")
    caps = ManagedCapabilities(
        base_url=base_url,
        app_version=app_version,
        embedding_mode=str(body.get("embedding_mode") or "unknown"),
        embedding_models=models,
        schema_latest_id=(str(body["schema_latest_id"]) if body.get("schema_latest_id") else None),
        schema_changeset_count=(int(sc_count) if isinstance(sc_count, int) else None),
    )
    _log.debug(
        "managed_service_probe_ok",
        base_url=base_url,
        app_version=app_version,
        embedding_mode=caps.embedding_mode,
    )
    return caps
