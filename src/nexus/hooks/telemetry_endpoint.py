# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether the account names an engine for telemetry: the wheel's mirror of the plugin's ``_endpoint_resolve.pinned_endpoint``.

The plugin decides where its telemetry senders may post (``pinned_endpoint``, stdlib, in the plugin's scripts). The settings
reader's report, the notice and ``nx settings`` say whether that decision found no engine, and the wheel has no ``_endpoint_resolve``
to ask, so this answers the same question from nexus's own resolver pieces. The two must agree on every shape of the account's
configuration; ``tests/hooks/test_pinned_endpoint_parity.py`` holds them to it. Nothing here reads an environment variable.
"""
from __future__ import annotations

import time
from pathlib import Path

_DEFAULT_TENANT = "default"


def telemetry_endpoint_missing(config_dir: Path) -> bool:
    """True when no engine is named under *config_dir*, the account's ``.config/nexus``.

    This must agree with the plugin's ``_endpoint_resolve.pinned_endpoint(config_dir)`` (nexus-sby49.10): the same three sources and
    no environment leg. An engine is a ``credentials.service_url`` in ``config.yml`` (no ``user:password@`` in it) with a token
    (a fresh data-token lease for that URL, the file's ``service_token``, or a live lease's token when the lease names that same
    origin), or a live local supervisor lease alone; and no bearer goes over plain http off this machine (nexus-ec37o), so an
    ``http`` URL that is not loopback names no engine either. Nothing here reads ``NX_SERVICE_*`` or any other environment variable.
    """
    from nexus._log_safe_url import has_userinfo  # noqa: PLC0415
    from nexus.config import persisted_credentials  # noqa: PLC0415
    from nexus.db.service_endpoint import host_port_url, is_loopback_host, normalize_origin, origins_equal  # noqa: PLC0415

    def carries_a_bearer(url: str) -> bool:
        origin = normalize_origin(url)
        return origin is not None and (origin[0] == "https" or is_loopback_host(origin[1]))

    lease = _lease(config_dir)
    if lease is not None and not lease.get("token"):
        lease = None
    lease_url = host_port_url(lease["host"], lease["port"]) if lease is not None else ""
    try:
        credentials = persisted_credentials(config_dir)
    except Exception:  # noqa: BLE001 -- an unreadable config.yml names no engine
        credentials = {}
    url = credentials.get("service_url", "").strip().rstrip("/")
    if url:
        if has_userinfo(url) or not carries_a_bearer(url):
            return True
        if credentials.get("service_token", "").strip() or (lease is not None and origins_equal(url, lease_url)):
            return False
        return _data_token(config_dir, url) is None
    return lease is None or not carries_a_bearer(lease_url)


def _lease(config_dir: Path) -> dict[str, str] | None:
    """The local supervisor lease under *config_dir* as ``{"host", "port", "token"}``, or None.

    Read the way the plugin's ``read_storage_service_lease`` reads it: the lease file for this account's service identity, status
    ``live``, plain TTL freshness (no grace for a live owner, no ``/health`` probe), and never a write: ``ServiceRegistry.discover``
    would probe a stale lease and reap one, which a settings report must not do and which the plugin's answer does not do.
    """
    from nexus.daemon.service_registry import LeaseRecord, service_identity  # noqa: PLC0415

    try:
        path = config_dir / f"storage_service_addr.{service_identity()}"
        record = LeaseRecord.from_json(path.read_text(encoding="utf-8"))
        if not record.is_fresh(time.time()):
            return None
        endpoint = record.endpoint
        if int(endpoint.get("port", 0)) <= 0:
            return None
        return {"host": str(endpoint.get("host", "127.0.0.1")), "port": str(endpoint["port"]), "token": str(endpoint.get("token", ""))}
    except Exception:  # noqa: BLE001 -- any failure reads as no lease
        return None


def _data_token(config_dir: Path, url: str) -> str | None:
    from nexus.db.data_token import DataTokenManager  # noqa: PLC0415

    try:
        return DataTokenManager(config_dir=config_dir).fresh_lease_token(url, _DEFAULT_TENANT, near_expiry_threshold=0.0)
    except Exception:  # noqa: BLE001
        return None
