# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Native service binary lifecycle for installed users.

pip/uv-installed users have no ``service/target`` directory. RDR-161 made the
signed native nexus-service binary the SOLE launch artifact (the legacy JVM
launch path is expunged); ``nx daemon service install-binary`` acquires and
verifies it (see :mod:`nexus.daemon.binary_install`). This module provides the
read-side helpers the supervisor and the ``service status`` command share:

- the **well-known binary location** ``<config_dir>/service/nexus-service``
  that supervisor discovery execs directly;
- the **installed provenance** read back from the binary sidecar written by
  ``install_binary``;
- the running service's ``/version`` **handshake**;
- psql discovery helpers used by the ``status`` Postgres probe.

(Renamed from the former JVM-lifecycle module in RDR-161 P3; the legacy install
path, fat-binary provenance extraction, and the schema-skew gate were removed
with the legacy JVM launch path.)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "well_known_binary_path",
    "read_installed_provenance",
    "fetch_service_version",
]

_WELL_KNOWN_SUBDIR = "service"
_WELL_KNOWN_BINARY_NAME = "nexus-service"


def well_known_binary_path(config_dir: Path) -> Path:
    """``<config_dir>/service/nexus-service`` — the installed-user NATIVE binary.

    RDR-157 ships per-OS/arch native-image binaries (no JVM). When one is
    positioned here (by ``nx daemon service install-binary`` / ``nx init
    --service``), the storage-service supervisor execs it directly.
    """
    return config_dir / _WELL_KNOWN_SUBDIR / _WELL_KNOWN_BINARY_NAME


def read_installed_provenance(config_dir: Path) -> dict | None:
    """Parsed provenance sidecar for the installed native binary, or ``None``.

    Reads the sidecar written by ``install_binary`` (version, tag, sha256,
    install metadata). Returns ``None`` when no binary has been installed.
    """
    from nexus.daemon.binary_install import binary_sidecar_path  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    path = binary_sidecar_path(config_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        _log.warning("service_binary_sidecar_unreadable", path=str(path))
        return None


def fetch_service_version(
    host: str,
    port: int | None,
    timeout: float = 3.0,
    *,
    scheme: str = "http",
) -> dict | None:
    """GET the running service's /version handshake, or ``None`` when
    unreachable (older service without the endpoint, service down).

    ``scheme`` defaults to ``http`` (the local-supervisor path). A managed TLS
    endpoint passes ``scheme="https"`` so the pre-gate handshake reaches the
    service instead of failing the TLS negotiation (nexus-n3bwh). ``port`` may
    be ``None`` (scheme-default port, e.g. an ``https://host`` URL with no
    explicit ``:443``), in which case it is omitted from the authority."""
    import urllib.request  # noqa: PLC0415 — heavy/optional dep deferred

    authority = f"{host}:{port}" if port is not None else host
    try:
        with urllib.request.urlopen(
            f"{scheme}://{authority}/version", timeout=timeout,
        ) as resp:
            data = json.loads(resp.read())
            return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001 — service version probe; unreachable logged at debug, returns None
        _log.debug("service_version_unreachable", host=host, port=port, error=str(exc))
        return None


# ── Postgres probe helpers (used by `nx daemon service status`) ───────────────


def _psql_bin() -> str | None:
    """psql from the same discovery the supervisor uses for pg_ctl."""
    try:
        from nexus.db.pg_provision import discover_pg_binaries  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        # discover_pg_binaries validates all four binaries incl. psql.
        return str(discover_pg_binaries().psql)
    except Exception:  # noqa: BLE001 — psql-path resolution fallback to shutil.which
        import shutil  # noqa: PLC0415 — stdlib import kept branch-local
        return shutil.which("psql")


def _db_name_from_creds(creds: dict) -> str:
    """Database name from the jdbc URL, defaulting to ``nexus``."""
    url = creds.get("NX_DB_URL", "")
    m = re.search(r"postgresql://[^/]+/([^?]+)", url)
    return m.group(1) if m else "nexus"
