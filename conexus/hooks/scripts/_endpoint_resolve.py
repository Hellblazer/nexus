#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stdlib-only, no-``nexus``-import mirror of the client's nexus-service
endpoint-resolution primitives (nexus-vg6d4) -- the ONE shared module for
every hook script under ``conexus/hooks/scripts/`` that must resolve the
service base URL and read its on-disk lease/credential files without
importing the ``nexus`` package.

Factored out under nexus-aginu, after nexus-0zsmg: ``t2_prefix_scan.py``
carried the first stdlib mirror of this precedence, ``routing/_lib.py``
ported it "verbatim" (nexus-gjv9b), and ``tuple_ledger_project.py``
(nexus-0zsmg/nexus-g2lln) was a THIRD independent copy -- and the one that
fell behind: its mirror lacked the persisted ``config.yml`` ``service_url``
leg the other two already had. A drift between hand-maintained copies is
exactly how that happened. This module is the single source for the parts
that were actually drifting.

WHAT THIS MODULE OWNS: the raw building blocks -- config-dir resolution,
lease-file reads, ``config.yml`` credential parsing, data-token-lease
matching, and the full base-URL precedence mirroring
``nexus.db.service_endpoint.resolve_service_endpoint``:

  1. ``NX_SERVICE_URL`` env.
  2. The persisted ``config.yml`` ``credentials.service_url``
     (``nx config set service_url``).
  3. ``NX_SERVICE_HOST``/``NX_SERVICE_PORT`` env (host filled from a live
     local supervisor lease when PORT is set but HOST is not -- nexus-aginu,
     matching ``resolve_service_config``'s per-field lease merge).
  4. The local ``ServiceRegistry`` supervisor lease
     (``storage_service_addr.<uid>``).

WHAT THIS MODULE DOES NOT OWN: which CREDENTIAL a caller presents once the
base URL is known. Callers differ here BY DESIGN, not by drift:
``t2_prefix_scan.py``/``routing/_lib.py`` are synchronous GET/POST callers
that accept a persisted-config or env static ``service_token`` as a last
resort -- a real, documented managed-onboarding path (``nx config set
service_token``). ``tuple_ledger_project.py`` is a fire-and-forget async
write with no reader and no retry, so it deliberately refuses any
credential but a fresh tenant-scoped data-token lease on a MANAGED
endpoint, falling back to a LOCAL supervisor lease's own token only when
the base URL itself came from that same local lease (nexus-g2lln).
:func:`resolve_base_url` reports which leg won (``is_local_supervisor``) so
a caller can implement either policy without re-deriving the lease/config
reads; :func:`resolve_endpoint_and_token` is the tuple_ledger_project.py
policy, factored here so its precedence + reads are the single-sourced
part even though the policy stays specific to that one caller.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: conexus plugin hook requires Python 3.12+, got {sys.version.split()[0]}\n"
        f"  Resolved: {sys.executable}\n"
        f"  Install: brew install python@3.13 (macOS) | apt install python3.12 (Ubuntu) | uv python install 3.12\n"
    )
    sys.exit(1)

#: Mirrors ``nexus.daemon.service_registry``'s tier name for the shared
#: nexus-service engine -- the lease file is
#: ``<config_dir>/storage_service_addr.<uid>``.
STORAGE_SERVICE_TIER = "storage_service"

#: Mirrors ``nexus.db.data_token``'s cross-process DATA-token lease
#: filename prefix: ``<config_dir>/data_token_lease.<sha256(host[:port]
#: \x00tenant)>``.
DATA_TOKEN_LEASE_PREFIX = "data_token_lease."
DATA_TOKEN_LEASE_FORMAT_VERSION = 1

#: The tenant every ``Http*Store`` in this repo is constructed with.
#: Matches ``nexus.db.t2.http_tuple_store.DEFAULT_TENANT`` (and its
#: siblings' identical constant) -- never a non-default tenant anywhere
#: these hooks' reads/writes correspond to.
DEFAULT_TENANT = "default"


class EndpointUnresolvable(Exception):
    """No endpoint/credential combination could be resolved. The message
    names every leg that was checked and why, never a bare 'not found'."""


def default_config_dir() -> Path:
    """Stdlib-only mirror of ``nexus.config.nexus_config_dir``."""
    config_dir = os.environ.get("NEXUS_CONFIG_DIR") or os.environ.get("NX_CONFIG_DIR")
    if config_dir:
        return Path(config_dir)
    return Path.home() / ".config" / "nexus"


def storage_service_lease_path(config_dir: Path) -> Path:
    return config_dir / f"{STORAGE_SERVICE_TIER}_addr.{os.getuid()}"


def read_storage_service_lease(config_dir: Path) -> dict[str, Any] | None:
    """Best-effort read of the local supervisor's ``ServiceRegistry``
    lease: ``{"host", "port", "token"}``, or ``None``. Any failure --
    missing file, unreadable, malformed JSON, non-``live`` status, or a
    heartbeat older than its TTL -- resolves to ``None``. Never raises.
    """
    path = storage_service_lease_path(config_dir)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    try:
        if str(data.get("status", "live")) != "live":
            return None
        heartbeat_epoch = float(data["heartbeat_epoch"])
        ttl = float(data["ttl"])
        endpoint = data["endpoint"]
        host = str(endpoint.get("host", "127.0.0.1"))
        port = int(endpoint.get("port", 0))
        token = str(endpoint.get("token", ""))
    except (KeyError, TypeError, ValueError):
        return None
    if port <= 0:
        return None
    if (time.time() - heartbeat_epoch) >= ttl:
        return None
    return {"host": host, "port": port, "token": token}


def read_local_supervisor_token(config_dir: Path) -> str:
    """The LOCAL SUPERVISOR's own static token, straight off the
    ``storage_service_addr.<uid>`` lease record -- or raise
    :class:`EndpointUnresolvable` naming why.

    Refuses (never trusts) a lease file that is not owner-only: the token
    it carries authorizes real engine writes, and a group/other-readable
    lease file means some other local account could have read it too.
    Re-verifies the same liveness checks :func:`read_storage_service_lease`
    already applies (rather than taking a pre-checked value as a parameter)
    so this function is a complete, independent audit trail for the one
    credential-bearing read in this module.
    """
    path = storage_service_lease_path(config_dir)
    try:
        st_result = path.stat()
    except OSError as exc:
        raise EndpointUnresolvable(
            f"local supervisor lease unavailable: cannot stat {path}: {exc}"
        ) from exc
    mode = stat.S_IMODE(st_result.st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise EndpointUnresolvable(
            f"local supervisor lease {path} is group/other-accessible "
            f"(mode {oct(mode)}); refusing to use its token as a bearer"
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise EndpointUnresolvable(
            f"local supervisor lease {path} unreadable/malformed: {exc}"
        ) from exc
    try:
        if str(data.get("status", "live")) != "live":
            raise EndpointUnresolvable(f"local supervisor lease {path} is not live")
        heartbeat_epoch = float(data["heartbeat_epoch"])
        ttl = float(data["ttl"])
        token = str(data["endpoint"].get("token", "") or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise EndpointUnresolvable(f"local supervisor lease {path} malformed: {exc}") from exc
    if (time.time() - heartbeat_epoch) >= ttl:
        raise EndpointUnresolvable(f"local supervisor lease {path} is stale (past ttl)")
    if not token:
        raise EndpointUnresolvable(f"local supervisor lease {path} carries no token")
    return token


def _strip_inline_comment(value: str) -> str:
    """Truncate *value* at an unquoted, comment-starting ``#`` -- YAML's
    inline-comment rule: a ``#`` that is the first character, or is
    immediately preceded by whitespace, starts a comment UNLESS it falls
    inside a quoted scalar (tracked from position 0 only -- this is a
    narrow line scanner, not a real YAML parser, so a quote character
    that is not the value's own opening/closing quote is not specially
    handled). Nothing here changes when *value* has no unquoted ``#`` --
    the common case -- beyond a trailing-whitespace ``rstrip()``.
    """
    if not value:
        return value
    quote: str | None = None
    for i, ch in enumerate(value):
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if i == 0 and ch in ("'", '"'):
            quote = ch
            continue
        if ch == "#" and (i == 0 or value[i - 1] in " \t"):
            return value[:i].rstrip()
    return value.rstrip()


def read_config_yml_credentials(config_dir: Path) -> dict[str, str]:
    """Bounded, stdlib-only extraction of ``service_url``/``service_token``
    from the persisted ``config.yml``.

    NOT a general YAML parser -- this module cannot import ``nexus`` or a
    third-party YAML library (nexus-vg6d4). It is a line-oriented scan
    restricted to exactly the two keys these hooks need, under a
    top-level ``credentials:`` block, matching the EXACT shape
    ``nexus.config.set_credential`` writes (``yaml.dump({"credentials":
    {...}}, default_flow_style=False)`` -- two-space indented ``key:
    value`` lines, no flow-style ``{...}``). A value PyYAML quoted
    (single or double, no embedded escapes) is unwrapped; a trailing
    inline ``# comment`` is stripped (nexus-aginu -- real YAML/PyYAML
    strips it too; the three pre-consolidation mirrors this module
    replaces did not, so a hand-edited config.yml with an inline comment
    on a credential line fed the comment text into the resolved value).
    Last-key-wins for a duplicate key (mirrors ``yaml.safe_load``).

    Anything this narrow scanner does not recognize -- a hand-edited
    flow-style file, an escaped quote, a value spanning multiple lines --
    is silently skipped (that key absent from the result), never guessed:
    a caller's normal env/lease fallback takes over exactly as if the
    credential were absent, never mis-resolving a base URL from a
    misparse.

    Returns ``{}`` when the file is absent, unreadable, or has no
    ``credentials:`` block.
    """
    path = config_dir / "config.yml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    result: dict[str, str] = {}
    in_credentials = False
    cred_indent = 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if not in_credentials:
            if stripped == "credentials:":
                in_credentials = True
                cred_indent = indent
            continue
        if indent <= cred_indent:
            # Dedented back out of the credentials block -- done scanning.
            break
        for key in ("service_url", "service_token"):
            prefix = f"{key}:"
            if not stripped.startswith(prefix):
                continue
            value = stripped[len(prefix):].strip()
            value = _strip_inline_comment(value)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if value:
                result[key] = value
    return result


def read_data_token_lease(
    config_dir: Path,
    base_url: str,
    *,
    tenant: str = DEFAULT_TENANT,
    near_expiry_threshold: float = 0.0,
) -> str | None:
    """The freshest data-token lease for *tenant* whose digest matches
    *base_url*'s host, with remaining TTL strictly above
    ``ttl_seconds * near_expiry_threshold`` -- or ``None``. Never raises.

    ``near_expiry_threshold=0.0`` (the default) means "not yet expired" --
    the historical behaviour for a synchronous GET/POST caller that can
    retry on a 401 (``t2_prefix_scan.py`` / ``routing/_lib.py``). A
    fire-and-forget caller with no retry and no reader
    (``tuple_ledger_project.py``) passes ``0.20``, the same margin
    ``nexus.db.data_token._REFRESH_THRESHOLD`` uses, so a lease that is
    about to expire is never presented on a write nothing will retry.

    Filters on *tenant* explicitly (nexus-em75s.12), not only on digest
    self-consistency: the digest is recomputed from the SAME lease file's
    own ``tenant`` field, so a lease for a different tenant on the same
    host still reproduces a matching digest and would otherwise be
    indistinguishable from a same-tenant lease by that check alone.
    """
    host = urllib.parse.urlsplit(base_url).netloc or base_url
    now = time.time()
    best_token: str | None = None
    best_expiry = 0.0
    try:
        candidates = sorted(config_dir.glob(f"{DATA_TOKEN_LEASE_PREFIX}*"))
    except OSError:
        candidates = []
    for path in candidates:
        try:
            data = json.loads(path.read_text())
            if data.get("format_version") != DATA_TOKEN_LEASE_FORMAT_VERSION:
                continue
            lease_tenant = str(data["tenant"])
            if lease_tenant != tenant:
                continue
            digest = hashlib.sha256(f"{host}\x00{lease_tenant}".encode("utf-8")).hexdigest()
            if data.get("base_url_digest") != digest:
                continue
            token = str(data["token"])
            expires_at = float(data["expires_at"])
            ttl_seconds = float(data["ttl_seconds"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if not token:
            continue
        remaining = expires_at - now
        if remaining <= ttl_seconds * near_expiry_threshold:
            continue  # expired, or too close to worth presenting
        if expires_at > best_expiry:
            best_token, best_expiry = token, expires_at
    return best_token


def resolve_base_url(config_dir: Path) -> tuple[str, bool]:
    """``(base_url, is_local_supervisor)``, or raise
    :class:`EndpointUnresolvable`.

    Mirrors ``nexus.db.service_endpoint.resolve_service_endpoint``'s
    precedence: the managed-cloud ``service_url`` leg -- env
    ``NX_SERVICE_URL`` first, then the persisted ``config.yml`` credential
    a user set with ``nx config set service_url`` (RDR-166 nexus-v3p0x) --
    is checked BEFORE the local-supervisor legs, exactly like the real
    HTTP storage clients (nexus-0zsmg: a cloud-mode box with no
    ``NX_SERVICE_URL`` exported -- an all-persisted-config install, the
    common shape after ``nx init`` -- must still resolve).

    The ``NX_SERVICE_HOST``/``NX_SERVICE_PORT`` env leg fills a missing
    HOST from a live local supervisor lease before defaulting to
    ``127.0.0.1`` (nexus-aginu, matching ``resolve_service_config``'s
    per-field lease merge -- the pre-consolidation
    ``tuple_ledger_project.py`` mirror always defaulted host to
    ``127.0.0.1`` here without ever consulting a live lease).

    ``is_local_supervisor`` is True ONLY for the last leg -- the endpoint
    was resolved by literally reading the ``storage_service_addr.<uid>``
    lease file for BOTH host and port. It is False for every other leg,
    the host/port-env leg included even when that leg's host came from
    the same lease file: naming a port via env is an explicit pin, and a
    caller that wants to know whether a lease record specifically backed
    the credential should check the lease directly rather than infer it
    from this flag.
    """
    url = os.environ.get("NX_SERVICE_URL", "").strip().rstrip("/")
    if not url:
        url = read_config_yml_credentials(config_dir).get("service_url", "").strip().rstrip("/")
    if url:
        return url, False

    port_str = os.environ.get("NX_SERVICE_PORT", "").strip()
    if port_str:
        try:
            port = int(port_str)
        except ValueError as exc:
            raise EndpointUnresolvable(
                f"NX_SERVICE_PORT is not an integer: {port_str!r}"
            ) from exc
        host = os.environ.get("NX_SERVICE_HOST", "").strip()
        if not host:
            lease = read_storage_service_lease(config_dir)
            host = lease["host"] if lease else "127.0.0.1"
        return f"http://{host}:{port}", False

    lease = read_storage_service_lease(config_dir)
    if lease is not None:
        return f"http://{lease['host']}:{lease['port']}", True

    lease_path = storage_service_lease_path(config_dir)
    raise EndpointUnresolvable(
        f"no service endpoint resolvable: no NX_SERVICE_URL, no persisted "
        f"config.yml service_url, no NX_SERVICE_PORT, and no live local "
        f"supervisor lease at {lease_path}"
    )


def resolve_endpoint_and_token(
    config_dir: Path,
    *,
    tenant: str = DEFAULT_TENANT,
    near_expiry_threshold: float = 0.20,
) -> tuple[str, str, bool]:
    """``(base_url, token, is_local_supervisor)`` using nexus-g2lln's
    policy -- or raise :class:`EndpointUnresolvable`.

    A fresh tenant-scoped data-token lease always wins. On a LOCAL
    SUPERVISOR endpoint (:func:`resolve_base_url`'s last leg) only, a
    missing/near-expiry data-token lease falls back to that SAME lease
    record's own owner-only-gated token (:func:`read_local_supervisor_token`)
    -- the credential the real local client already presents on this box
    when no ``mint_token`` is configured. A MANAGED endpoint never gets
    that fallback: a wrong-scoped static token would 401 silently on a
    fire-and-forget write with no reader.
    """
    base_url, is_local_supervisor = resolve_base_url(config_dir)
    token = read_data_token_lease(
        config_dir, base_url, tenant=tenant, near_expiry_threshold=near_expiry_threshold,
    )
    if token:
        return base_url, token, is_local_supervisor
    host = urllib.parse.urlsplit(base_url).netloc or base_url
    no_lease_msg = (
        f"no fresh data-token lease for {host} tenant={tenant!r} under "
        f"{config_dir}/{DATA_TOKEN_LEASE_PREFIX}* (missing, wrong host/tenant "
        f"digest, or within {int(near_expiry_threshold * 100)}% of expiry)"
    )
    if not is_local_supervisor:
        raise EndpointUnresolvable(no_lease_msg)
    try:
        token = read_local_supervisor_token(config_dir)
    except EndpointUnresolvable as local_exc:
        raise EndpointUnresolvable(f"{no_lease_msg}; {local_exc}") from local_exc
    return base_url, token, is_local_supervisor
