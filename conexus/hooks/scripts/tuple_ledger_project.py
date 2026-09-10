#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-205 Phase 2 Step 3 (bead nexus-em75s.11): the async projection body
for the two ``ledger/<session_id>`` tuple writes.

Invoked ONLY from inside ``subagent-start-tuple-async.sh`` /
``subagent-stop-tuple-async.sh`` — never directly from ``hooks.json`` (so
it is not subject to the Python-hook ``_run_python_hook.sh`` routing rule,
which governs only commands ``hooks.json`` invokes itself; see
``subagent-stop-scan.py`` for the identical precedent of a bash hook
shelling to a stdlib sibling ``.py``). Those two wrapper scripts are the
``async: true`` entries beside ``subagent-start.sh``/``subagent-start-
stamp.sh`` and ``subagent-stop.sh`` in ``hooks.json`` — this module does
the actual work after the wrapper has already detached and returned.

NO HOOK MINTS ANYTHING (RDR-205 "Identity and addressing"). This module
reads the client library's own cross-process caches --
``storage_service_addr.<uid>`` (host/port, written by
``nexus.daemon.service_registry.ServiceRegistry``) and
``data_token_lease.<digest>`` (the bearer, written by
``nexus.db.data_token.DataTokenManager._write_lease``) -- and PRESENTS
what it finds. It never calls ``/v1/data-tokens/mint``, never falls back
to a static/mint-locked ``service_token`` (unlike
``conexus/hooks/scripts/routing/_lib.py``'s ``_resolve_endpoint``, which
DOES fall back -- this is deliberately narrower: a wrong-scoped static
token would 401 silently on a fire-and-forget write with no reader, so a
missing or near-expiry data-token lease is a SKIP, not a fallback).

Stdlib-only mirror of ``nexus.db.data_token``'s lease-file format and
``nexus.db.service_endpoint``'s local-supervisor discovery leg -- this
script cannot import ``nexus`` (bare ``python3``, same constraint as
``t2_prefix_scan.py`` / ``routing/_lib.py``, nexus-vg6d4).

Wire shape: mirrors ``nexus.db.t2.http_tuple_store.HttpTupleStore.out``
POSTing to ``/v1/tuples/out`` -- ``{"subspace": ..., "keys": {...},
"dims": {...}}`` -- against the ``ledger/<session_id>`` template
(``service/src/main/resources/tuples/templates/ledger.yaml``): keys
``agent_id`` (unconstrained) and ``kind`` (pinned to
``{start, report}``), dimension ``agent_type``. ``id_from: keys`` means
the tuple id is derived from ``(agent_id, kind)`` alone, so a retried
``out`` for the same pair lands on the same row -- idempotent by
construction, no de-dup needed here.

Every failure path -- unresolvable endpoint, no fresh data-token lease,
transport failure, a non-2xx response -- appends one line to a
log file beside the session's expectations ledger
(``<state_dir>/<session_id>.tuple-projection.log``) and exits 0. This
script's stdout, stderr and exit code are never read by anyone (the
wrapper backgrounds it with all three fds already redirected away) --
the log file is the only diagnostic surface, and every code path reaches
it or exits silently on a session_id too malformed to build a safe path
from.
"""
from __future__ import annotations

import hashlib
import json
import os
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
#: ``<config_dir>/storage_service_addr.<uid>``. Same constant name/value
#: as ``t2_prefix_scan.py``/``routing/_lib.py``.
_STORAGE_SERVICE_TIER = "storage_service"

#: Mirrors ``nexus.db.data_token``'s cross-process DATA-token lease
#: filename prefix: ``<config_dir>/data_token_lease.<sha256(host[:port]
#: \x00tenant)>``.
_DATA_TOKEN_LEASE_PREFIX = "data_token_lease."
_DATA_TOKEN_LEASE_FORMAT_VERSION = 1

#: Same 20% refresh/"near-expiry" threshold as
#: ``nexus.db.data_token._REFRESH_THRESHOLD`` -- a lease this close to
#: expiring is not worth presenting on a fire-and-forget write with no
#: retry and no reader of the response.
_NEAR_EXPIRY_THRESHOLD = 0.20

#: Bound on the whole POST round trip -- research 5 measured ~10ms for a
#: healthy engine; this is a ceiling for a degraded/rate-limiting one, not
#: a target. The wrapper has already detached, so this bound only keeps a
#: hung engine from leaving an orphaned connection open forever.
_POST_TIMEOUT_S = 5

_ROUTE = "/v1/tuples/out"

#: The tenant this script's writes are scoped to. Matches
#: ``nexus.db.t2.http_tuple_store``'s (and every sibling ``Http*Store``'s)
#: ``DEFAULT_TENANT`` -- ``HttpTupleStore`` is never constructed with a
#: non-default tenant anywhere this hook's writes correspond to, so a
#: data-token lease minted for any OTHER tenant must never be presented
#: here even when it is the freshest lease on disk for the same host
#: (nexus-em75s.12 review fix: the lease-selection loop used to pick
#: purely on host-digest + freshest-expiry, so a second tenant's lease
#: for the same host could win and be sent as this write's bearer).
_RESOLVED_TENANT = "default"

_SESSION_ID_RE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)


class _Skip(Exception):
    """Any resolution/transport failure -- caught once in main(), logged,
    and the script exits 0. Never propagates as a traceback (nothing
    reads stderr anyway, but a clean exit keeps the intent explicit)."""


def _default_config_dir() -> Path:
    config_dir = os.environ.get("NEXUS_CONFIG_DIR") or os.environ.get("NX_CONFIG_DIR")
    if config_dir:
        return Path(config_dir)
    return Path.home() / ".config" / "nexus"


def _default_state_dir() -> Path:
    """Mirrors ``expectations.sh``'s ``_expectations_dir``:
    ``${XDG_STATE_HOME:-$HOME/.local/state}/nexus/orchestration`` -- the
    log file lives beside the session's ``.expectations`` ledger there."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "nexus" / "orchestration"


def _valid_session_id(session_id: str) -> bool:
    """Mirrors ``expectations_file``'s path-safe charset guard: a
    traversal-bearing or otherwise unsafe session_id must never be used to
    build a filesystem path."""
    if not session_id or len(session_id) > 128:
        return False
    if not (session_id[0].isalnum()):
        return False
    return all(c in _SESSION_ID_RE_CHARS for c in session_id)


def _log_path(session_id: str) -> Path:
    return _default_state_dir() / f"{session_id}.tuple-projection.log"


def _log_skip(session_id: str, reason: str) -> None:
    """Best-effort append; a failure to even log is not this script's
    problem to escalate -- there is no reader waiting on it either way."""
    if not _valid_session_id(session_id):
        return
    try:
        path = _log_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{ts}\t{reason}\n")
    except OSError:
        pass


# ── Endpoint + data-token-lease resolution (stdlib mirror; no mint) ────────


def _read_storage_service_lease(config_dir: Path) -> dict[str, Any] | None:
    """Stdlib mirror of ``nexus.db.service_endpoint.discover_lease``'s
    local-supervisor leg -- same file, same freshness rule, same
    fail-to-None-never-raise contract as ``routing/_lib.py``'s
    ``_read_lease``. host/port only carry no auth risk on their own, so
    this leg's token field (the supervisor's static credential) is read
    but never used as a fallback below -- host/port from here, bearer only
    from the data-token lease.
    """
    path = config_dir / f"{_STORAGE_SERVICE_TIER}_addr.{os.getuid()}"
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
    except (KeyError, TypeError, ValueError):
        return None
    if port <= 0:
        return None
    if (time.time() - heartbeat_epoch) >= ttl:
        return None
    return {"host": host, "port": port}


def _resolve_base_url(config_dir: Path) -> str:
    """``(env|lease) -> base_url``, or raise :class:`_Skip`. Host/port
    only -- never touches the bearer."""
    url = os.environ.get("NX_SERVICE_URL", "").strip().rstrip("/")
    if url:
        return url

    port_str = os.environ.get("NX_SERVICE_PORT", "").strip()
    if port_str:
        try:
            port = int(port_str)
        except ValueError as exc:
            raise _Skip(f"NX_SERVICE_PORT is not an integer: {port_str!r}") from exc
        host = os.environ.get("NX_SERVICE_HOST", "").strip() or "127.0.0.1"
        return f"http://{host}:{port}"

    lease = _read_storage_service_lease(config_dir)
    if lease is not None:
        return f"http://{lease['host']}:{lease['port']}"

    lease_path = config_dir / f"{_STORAGE_SERVICE_TIER}_addr.{os.getuid()}"
    raise _Skip(
        f"no service endpoint resolvable: no NX_SERVICE_URL, no "
        f"NX_SERVICE_PORT, and no live local supervisor lease at {lease_path}"
    )


def _read_data_token_lease(
    config_dir: Path, base_url: str, tenant: str = _RESOLVED_TENANT,
) -> str:
    """The freshest data-token lease for *tenant* whose digest matches
    *base_url*'s host, with remaining TTL ABOVE the near-expiry threshold
    -- or raise :class:`_Skip` naming why. Never mints. Never falls back
    to a static token: the caller has nothing else to try.

    Filters on *tenant* explicitly (nexus-em75s.12 review fix), not only
    on digest self-consistency: the digest is recomputed from the SAME
    lease file's own ``tenant`` field, so a lease for a different tenant
    on the same host still reproduces a matching digest and would
    otherwise be indistinguishable from a same-tenant lease by that check
    alone. Two leases for one host (different tenants) must resolve to
    the one actually scoped to *tenant*, never to whichever has the
    furthest expiry.
    """
    host = urllib.parse.urlsplit(base_url).netloc or base_url
    now = time.time()
    best_token, best_expiry = "", 0.0
    try:
        candidates = sorted(config_dir.glob(f"{_DATA_TOKEN_LEASE_PREFIX}*"))
    except OSError:
        candidates = []
    for path in candidates:
        try:
            data = json.loads(path.read_text())
            if data.get("format_version") != _DATA_TOKEN_LEASE_FORMAT_VERSION:
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
        if remaining <= ttl_seconds * _NEAR_EXPIRY_THRESHOLD:
            continue  # expired, or too close to worth presenting
        if expires_at > best_expiry:
            best_token, best_expiry = token, expires_at
    if not best_token:
        raise _Skip(
            f"no fresh data-token lease for {host} tenant={tenant!r} under "
            f"{config_dir}/{_DATA_TOKEN_LEASE_PREFIX}* (missing, wrong "
            f"host/tenant digest, or within {int(_NEAR_EXPIRY_THRESHOLD * 100)}% "
            f"of expiry)"
        )
    return best_token


# ── Payload + POST ───────────────────────────────────────────────────────


def _extract_fields(raw_payload: str) -> tuple[str, str, str]:
    try:
        data = json.loads(raw_payload)
    except (json.JSONDecodeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    session_id = str(data.get("session_id") or "")
    agent_id = str(data.get("agent_id") or "")
    agent_type = str(data.get("agent_type") or "")
    return session_id, agent_id, agent_type


def _post_via_urllib(base_url: str, token: str, body: dict[str, Any]) -> None:
    """POST *body* to ``{base_url}/v1/tuples/out`` via stdlib
    ``urllib.request`` -- never via a subprocess argv (nexus-em75s.12
    review fix: the prior ``curl -H "Authorization: Bearer <token>"``
    invocation put the bearer in that process's argv, readable by any
    co-resident user via ``ps``/``/proc`` for the life of the call).
    Same repo precedent as ``routing/_lib.py``'s routing-event POST and
    ``t2_prefix_scan.py``'s ``_http_get_json``. Raises :class:`_Skip`
    naming the failure; never raises anything else.
    """
    import urllib.error  # noqa: PLC0415 — stdlib, only needed on this path
    import urllib.request  # noqa: PLC0415 — stdlib, only needed on this path

    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    url = f"{base_url}{_ROUTE}"
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_POST_TIMEOUT_S) as resp:  # noqa: S310 — fixed internal engine URL, not user input
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _Skip(f"transport failure posting to {url}: {exc}") from exc
    if not (200 <= status < 300):
        raise _Skip(f"engine returned HTTP {status} posting to {url}")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("start", "report"):
        # Malformed invocation -- nothing to log a session_id against yet.
        return 0
    kind = argv[1]
    raw_payload = sys.stdin.read()
    session_id, agent_id, agent_type = _extract_fields(raw_payload)

    if not (session_id and agent_id and agent_type):
        _log_skip(session_id, f"SKIP kind={kind} incomplete payload fields")
        return 0

    config_dir = _default_config_dir()
    try:
        base_url = _resolve_base_url(config_dir)
        token = _read_data_token_lease(config_dir, base_url)
        body = {
            "subspace": f"ledger/{session_id}",
            "keys": {"agent_id": agent_id, "kind": kind},
            "dims": {"agent_type": agent_type},
        }
        _post_via_urllib(base_url, token, body)
    except _Skip as exc:
        _log_skip(session_id, f"SKIP kind={kind} agent_id={agent_id} {exc}")
        return 0
    except Exception as exc:  # noqa: BLE001 — best-effort projection must never propagate a traceback
        _log_skip(session_id, f"SKIP kind={kind} agent_id={agent_id} unexpected: {exc}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
