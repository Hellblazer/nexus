#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2 prefix-scan: surface T2 namespaces matching a project prefix.

Stdlib-only (nexus-vg6d4) — bare ``python3`` cannot import the ``nexus``
package. nexus-8fvp2: talks to the engine's ``/v1/memory`` HTTP API
(never SQLite ``memory.db``, retired at RDR-158 P4); see
:func:`_resolve_endpoint` and :func:`_check_freshness` below for the
endpoint-resolution and two-arm freshness-assert design notes.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: conexus plugin hook requires Python 3.12+, got {sys.version.split()[0]}\n"
        f"  Resolved: {sys.executable}\n"
        f"  Install: brew install python@3.13 (macOS) | apt install python3.12 (Ubuntu) | uv python install 3.12\n"
    )
    sys.exit(1)

#: nexus-h33x8.5 fix-pass (VERIFICATION 1, combined SessionStart byte
#: budget): tightened from 15/5/8/120 to 8/3/5/70. The per-namespace
#: fetch/budget/freshness machinery below (nexus-9xado/nexus-eg6qe/
#: nexus-8fvp2) is untouched -- only the RENDER density (how many
#: entries, how long a snippet) shrank, since that machinery already
#: fetches everything it needs regardless of these render caps.
_HARD_CAP = 8         # max rendered entries across all namespaces combined
_SNIPPET_LIMIT = 3    # per-namespace: entries up to this rank get a snippet
_TITLE_LIMIT = 5      # per-namespace: entries up to this rank get title-only

#: Cap on distinct namespaces this hook will issue a per-namespace
#: ``/v1/memory/all`` request for, independent of ``_HARD_CAP`` (which only
#: counts RENDERED entries — a run of zero-entry namespaces never counts
#: against it, so an unbounded namespace list would otherwise drive an
#: unbounded number of sequential HTTP round-trips; nexus-9xado).
#: ``_get_namespaces`` already returns rows ``MAX(timestamp) DESC``
#: (``MemoryRepository.getProjectsWithPrefix``), so capping here loses only
#: the least-recently-touched namespace variants, not the most relevant.
#: Neither ``/v1/memory/projects`` nor ``/v1/memory/all`` accepts a
#: server-side limit param (verified against ``MemoryHandler.java`` /
#: ``MemoryRepository.java`` — no ``limit=``/``LIMIT`` on either route), so
#: this client-side slice is the only lever available without an engine
#: change.
_MAX_NAMESPACES = 5

#: Overall wall-clock budget for the WHOLE per-namespace fetch loop in
#: ``_build_output`` (nexus-9xado/nexus-eg6qe) — distinct from
#: ``_DEFAULT_HTTP_TIMEOUT_S``, which bounds a single request. Without this,
#: worst case is ``_MAX_NAMESPACES`` sequential requests each stalling to
#: the full per-request timeout: ``5 * 3.0s = 15s`` added to every
#: SessionStart/SubagentStart under a slow-but-alive (not down — a down
#: engine fails fast via connection-refused) engine. This budget caps the
#: SEQUENCE, checked between namespace requests, so latency stays bounded
#: regardless of namespace count. Override via NX_T2_SCAN_BUDGET_S for
#: testing.
_DEFAULT_SCAN_BUDGET_S = 8.0

#: A namespace's freshest entry older than this is flagged with a visible
#: warning line (nexus-8fvp2 enlargement (d)) — never a silent stale block.
#: T2 entries default to a 30-day TTL; half that is a reasonable "something
#: may be off with the T2 substrate" signal without nagging on ordinary
#: slow-moving projects. Override via NX_T2_SCAN_STALE_DAYS for testing.
_DEFAULT_STALE_DAYS = 14

#: Short by design — this hook runs on every SessionStart/SubagentStart and
#: must never make the injected-context path noticeably slower than the
#: rest of the hook chain. Override via NX_T2_SCAN_TIMEOUT_S for testing.
_DEFAULT_HTTP_TIMEOUT_S = 3.0

#: Matches ServiceRegistry's tier name for the shared nexus-service engine
#: (src/nexus/daemon/service_registry.py TIER_TTLS / storage_service_daemon.py
#: _REGISTRY_TIER) — the lease file is
#: ``<config_dir>/storage_service_addr.<uid>``.
_STORAGE_SERVICE_TIER = "storage_service"

#: nexus-znvjd: the client's cross-process DATA-token lease, written by
#: ``nexus.db.data_token.DataTokenManager._write_lease`` at
#: ``<config_dir>/data_token_lease.<sha256(host[:port]\x00tenant)>``. On an
#: armed pass-through box (RDR-005 step (d)) the persisted ``service_token``
#: is the scope=mint-locked credential: it can mint data tokens but cannot
#: read data paths, so presenting it to ``/v1/memory`` is a 401 every
#: session. The client mints and caches the real bearer here; this hook
#: (stdlib-only, cannot import ``nexus``, must never mint — a mint has no
#: fallback by design) borrows it when fresh and falls back otherwise.
_DATA_TOKEN_LEASE_PREFIX = "data_token_lease."
_DATA_TOKEN_LEASE_FORMAT_VERSION = 1

#: Engine timestamp format: UTC second-precision ISO
#: (MemoryHandler.recordToMap / MemoryRepository.UTC_SECOND on the Java side).
_TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"


class _Unreachable(Exception):
    """The T2 engine's endpoint could not be resolved, or a call to it
    failed (connection refused/timeout/non-2xx/malformed response).

    Caught once at the top of ``main()``; the caller renders a single
    visible warning line into stdout rather than silently producing no
    output — the exact failure mode nexus-8fvp2 exists to close.

    ``http_status`` carries the engine's status code when the failure was
    a non-2xx response (nexus-znvjd: ``main`` keys the credential hint on
    401), else ``None``.
    """

    http_status: int | None = None


def _default_config_dir() -> Path:
    """Stdlib-only mirror of ``nexus.config.nexus_config_dir``.

    Honours ``NEXUS_CONFIG_DIR`` / ``NX_CONFIG_DIR`` env overrides for
    parity with the test sandbox and the release-sandbox harness, then
    falls back to the canonical ``~/.config/nexus``. Kept in sync with the
    resolver in ``src/nexus/config.py``; if that resolver ever grows
    additional precedence rules, mirror them here.
    """
    config_dir = (
        os.environ.get("NEXUS_CONFIG_DIR")
        or os.environ.get("NX_CONFIG_DIR")
    )
    if config_dir:
        return Path(config_dir)
    return Path.home() / ".config" / "nexus"


def _read_lease(config_dir: Path) -> dict[str, Any] | None:
    """Best-effort read of the local supervisor's ServiceRegistry lease.

    Stdlib mirror of ``nexus.db.service_endpoint.discover_lease``'s
    local-supervisor leg (the ONE discovery mechanism every other T2/T3
    client routes through) — this hook cannot import
    ``nexus.daemon.service_registry`` (nexus-vg6d4), so it parses the same
    on-disk JSON lease record directly:
    ``<config_dir>/storage_service_addr.<uid>``, written by
    ``ServiceRegistry.publish``/``heartbeat``
    (``src/nexus/daemon/service_registry.py``). Any failure — missing file,
    unreadable, malformed JSON, non-``live`` status, or a heartbeat older
    than its TTL — resolves to ``None``; the caller falls back to env vars
    or fails loud via :class:`_Unreachable`. Never raises.
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
        token = str(endpoint.get("token", ""))
    except (KeyError, TypeError, ValueError):
        return None
    if port <= 0 or not token:
        return None
    if (time.time() - heartbeat_epoch) >= ttl:
        return None
    return {"host": host, "port": port, "token": token}


def _read_data_token_lease(config_dir: Path, base_url: str) -> str | None:
    """Best-effort read of the client's cached DATA token for *base_url*
    (nexus-znvjd) — the freshest unexpired lease whose digest matches
    ``(host[:port], its own tenant)``, or ``None``.

    Stdlib mirror of ``DataTokenManager._read_lease``: same format-version
    check, same digest rule (``sha256(host\x00tenant)``, host =
    ``urlsplit(base_url).netloc``), same fail-safe stance — absent,
    unreadable, malformed, wrong digest, or expired all resolve to ``None``
    and the caller keeps today's static-token path. The tenant is read
    from each lease's own ``tenant`` field (the caller-passed tenant the
    client keyed on; NOT ``mint_tenant``, which is a mint-request
    override), so the hook derives nothing from config. Never mints, never
    raises.
    """
    host = urllib.parse.urlsplit(base_url).netloc or base_url
    now = time.time()
    best_token, best_expiry = "", 0.0
    try:
        candidates = sorted(config_dir.glob(f"{_DATA_TOKEN_LEASE_PREFIX}*"))
    except OSError:
        return None
    for path in candidates:
        try:
            data = json.loads(path.read_text())
            if data.get("format_version") != _DATA_TOKEN_LEASE_FORMAT_VERSION:
                continue
            tenant = str(data["tenant"])
            digest = hashlib.sha256(f"{host}\x00{tenant}".encode("utf-8")).hexdigest()
            if data.get("base_url_digest") != digest:
                continue
            token = str(data["token"])
            expires_at = float(data["expires_at"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if token and expires_at > now and expires_at > best_expiry:
            best_token, best_expiry = token, expires_at
    return best_token or None


def _read_config_yml_credentials(config_dir: Path) -> dict[str, str]:
    """Bounded, stdlib-only extraction of ``service_url``/``service_token``
    from the persisted ``config.yml`` (nexus-sdtsx).

    NOT a general YAML parser — this hook cannot import ``nexus`` (nor a
    third-party YAML library) per nexus-vg6d4. It is a line-oriented scan
    restricted to exactly the two keys this hook needs, under a top-level
    ``credentials:`` block, matching the EXACT shape
    ``nexus.config.set_credential`` writes (``yaml.dump({"credentials":
    {...}}, default_flow_style=False)`` — two-space indented ``key: value``
    lines, no flow-style ``{...}``). ``nx config set service_url``/
    ``service_token`` is the canonical managed-cloud onboarding path
    (docs/managed-onboarding.md) and the ONLY credential source a Desktop
    ``.mcpb`` install has (docs/desktop-deployment.md: the .mcpb reads
    config.yml, never inherits shell env) — without this, T2 injection was
    permanently absent for that entire population, catch-all-warned as if
    it were the local-mode "supervisor not started" case.

    PyYAML's default representer only quotes a plain scalar when it must
    (leading indicator chars, ``": "``, etc.); a URL (``https://host:port``)
    or a bearer token (alnum/``-``/``_``/``.``) round-trips as an
    unquoted plain scalar — confirmed against the real writer. A value that
    PyYAML DID quote (single or double, no embedded escapes) is unwrapped
    here too. Anything this narrow scanner does not recognize — a
    hand-edited flow-style file, an escaped quote, a value spanning
    multiple lines — is silently skipped (returns without that key), never
    guessed: the caller's normal env/lease fallback and eventual
    ``_Unreachable`` take over exactly as if the key were absent, so this
    function can only WIDEN coverage, never produce a wrong answer.

    Returns ``{}`` (no keys) when the file is absent, unreadable, or has no
    ``credentials:`` block.
    """
    path = config_dir / "config.yml"
    try:
        text = path.read_text()
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
            # Dedented back out of the credentials block — done scanning.
            break
        for key in ("service_url", "service_token"):
            prefix = f"{key}:"
            if not stripped.startswith(prefix):
                continue
            value = stripped[len(prefix):].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if value:
                result[key] = value
    return result


def _resolve_endpoint(config_dir: Path) -> tuple[str, str, bool]:
    """Resolve ``(base_url, token, via_data_token)`` for the nexus-service
    T2 HTTP API; ``via_data_token`` says whether *token* came from the
    data-token lease (so ``main`` can key its 401 hint on what was
    actually presented, not on a second, later lease read).

    Stdlib-only mirror of
    ``nexus.db.service_endpoint.resolve_service_endpoint`` — bare
    ``python3`` cannot import the ``nexus`` package (nexus-vg6d4), so this
    covers the three legs that need no third-party YAML parser:

      1. ``service_url`` — ``NX_SERVICE_URL`` env FIRST, then the
         persisted ``config.yml`` credential (:func:`_read_config_yml_credentials`,
         nexus-sdtsx) — matching ``get_credential``'s env-then-config.yml
         precedence, the same order the real client uses. Token resolves
         the same way (``NX_SERVICE_TOKEN`` env, then config.yml), falling
         back to the lease's token only as a last resort — mirrors
         ``resolve_service_endpoint``'s leg 1 exactly.
      2. ``NX_SERVICE_HOST``/``NX_SERVICE_PORT`` (+ ``NX_SERVICE_TOKEN``,
         falling back to the lease's token), then the bare local-supervisor
         lease (:func:`_read_lease`) — the local-supervisor leg. This leg
         does NOT consult config.yml: neither does
         ``resolve_service_config``, the real client's counterpart for this
         leg (host/port are env/lease only, never persisted).

    On every leg, once the base URL is known, a fresh data-token lease for
    that host (:func:`_read_data_token_lease`, nexus-znvjd) wins over the
    static token however the static token was supplied — mirroring the
    real client, where ``DataTokenManager.bearer_for`` beats the static
    credential whenever minting is configured. A lease for a different
    host never matches (digest keyed on host:port), so an unarmed box
    resolves exactly as before.

    Raises:
        _Unreachable: no endpoint/token combination is resolvable.
    """
    lease = _read_lease(config_dir)
    yaml_creds = _read_config_yml_credentials(config_dir)

    url = os.environ.get("NX_SERVICE_URL", "").strip().rstrip("/")
    if not url:
        url = yaml_creds.get("service_url", "").strip().rstrip("/")
    if url:
        data_token = _read_data_token_lease(config_dir, url)
        if data_token:
            return url, data_token, True
        token = os.environ.get("NX_SERVICE_TOKEN", "").strip()
        if not token:
            token = yaml_creds.get("service_token", "").strip()
        if not token:
            token = lease["token"] if lease else ""
        if not token:
            raise _Unreachable(
                "service_url is configured (env or config.yml) but no token "
                "is resolvable — export NX_SERVICE_TOKEN or run "
                "`nx config set service_token <bearer>`"
            )
        return url, token, False

    port_str = os.environ.get("NX_SERVICE_PORT", "").strip()
    if port_str:
        try:
            port = int(port_str)
        except ValueError as exc:
            raise _Unreachable(
                f"NX_SERVICE_PORT is not an integer: {port_str!r}"
            ) from exc
        host = os.environ.get("NX_SERVICE_HOST", "").strip() or "127.0.0.1"
        data_token = _read_data_token_lease(config_dir, f"http://{host}:{port}")
        if data_token:
            return f"http://{host}:{port}", data_token, True
        token = os.environ.get("NX_SERVICE_TOKEN", "").strip() or (
            lease["token"] if lease else ""
        )
        if not token:
            raise _Unreachable(
                "NX_SERVICE_PORT is set but no NX_SERVICE_TOKEN and no live "
                "supervisor lease is resolvable"
            )
        return f"http://{host}:{port}", token, False

    if lease is not None:
        url = f"http://{lease['host']}:{lease['port']}"
        data_token = _read_data_token_lease(config_dir, url)
        if data_token:
            return url, data_token, True
        return url, lease["token"], False

    lease_path = config_dir / f"{_STORAGE_SERVICE_TIER}_addr.{os.getuid()}"
    raise _Unreachable(
        "no service_url (env/config.yml), no NX_SERVICE_PORT env, and no "
        f"live local supervisor lease at {lease_path} — local install: "
        "`nx daemon service start`; managed/cloud: `nx config set "
        "service_url <url>` (and `service_token <bearer>`, or export "
        "NX_SERVICE_URL/NX_SERVICE_TOKEN)"
    )


def _http_get_json(
    base_url: str, token: str, path: str, params: dict[str, str], timeout: float
) -> Any:
    """One GET against the engine's T2 HTTP API. Raises ``_Unreachable`` on
    any transport failure, non-2xx response, or malformed JSON body."""
    query = urllib.parse.urlencode(params)
    url = f"{base_url}{path}?{query}" if query else f"{base_url}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Nexus-Tenant": "default",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed http(s) scheme built above, not user input
            body = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = exc.read().decode("utf-8", "replace")
        err = _Unreachable(
            f"T2 engine returned HTTP {exc.code} for {path}: {detail[:200]}"
        )
        err.http_status = exc.code
        raise err from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _Unreachable(f"T2 engine unreachable at {base_url}{path}: {exc}") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _Unreachable(f"T2 engine returned malformed JSON for {path}: {exc}") from exc


def _snippet(content: str, max_chars: int = 70) -> str:
    """Return first meaningful line of content, truncated."""
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or set(line) <= set("-="):
            continue
        return line[:max_chars] + ("…" if len(line) > max_chars else "")
    return ""


def _get_namespaces(
    base_url: str, token: str, prefix: str, timeout: float
) -> list[dict[str, Any]]:
    """Return ``[{"project": ..., "last_updated": ...}, ...]`` matching
    *prefix*, recency-ordered (DESC) — server-side via ``GET
    /v1/memory/projects`` (``MemoryRepository.getProjectsWithPrefix``
    already escapes LIKE metacharacters and orders by ``MAX(timestamp)
    DESC``, mirroring the pre-fix SQLite query)."""
    if not prefix:
        return []
    return _http_get_json(base_url, token, "/v1/memory/projects", {"prefix": prefix}, timeout)


def _get_entries(
    base_url: str, token: str, project: str, timeout: float
) -> list[tuple[str, str]]:
    """Return ``[(title, content), ...]`` for *project*, recency-ordered
    (DESC) — server-side via ``GET /v1/memory/all``."""
    rows = _http_get_json(base_url, token, "/v1/memory/all", {"project": project}, timeout)
    return [(row.get("title", "") or "", row.get("content") or "") for row in rows]


def _build_output(
    base_url: str,
    token: str,
    project_name: str,
    namespaces: list[dict[str, Any]],
    timeout: float,
    scan_budget_s: float = _DEFAULT_SCAN_BUDGET_S,
) -> list[str]:
    """Render the ``### T2 Memory (...)`` block(s), capped per the same
    per-namespace/whole-scan budget the pre-fix SQLite path used.

    Per-namespace fetch failures are isolated (nexus-eg6qe): a bad/slow
    namespace N gets its own warning line and the loop moves on, rather
    than an exception from namespace N discarding namespaces
    ``1..N-1``'s already-rendered output too. ``namespaces`` is capped to
    ``_MAX_NAMESPACES`` and the whole loop is bounded by *scan_budget_s*
    (nexus-9xado) — both independent of ``_HARD_CAP``, which only counts
    RENDERED entries and does not fire for a run of empty namespaces.
    """
    lines: list[str] = []
    total = 0  # rendered entries across all namespaces
    capped_namespaces = namespaces[:_MAX_NAMESPACES]
    skipped_for_cap = len(namespaces) - len(capped_namespaces)
    deadline = time.monotonic() + scan_budget_s

    for idx, ns_row in enumerate(capped_namespaces):
        if total >= _HARD_CAP:
            break
        if time.monotonic() >= deadline:
            remaining = len(capped_namespaces) - idx
            lines.append(
                f"  … (scan budget exceeded — {remaining} namespace(s) not checked)"
            )
            break

        ns = ns_row.get("project", "")
        try:
            entries = _get_entries(base_url, token, ns, timeout)
        except _Unreachable as exc:
            lines.append(f"  WARNING: T2 memory namespace {ns!r} unreachable: {exc}")
            continue
        if not entries:
            continue

        suffix = ns[len(project_name):].lstrip("_") if ns != project_name else ""
        label = f"T2 Memory ({suffix})" if suffix else "T2 Memory"

        ns_lines: list[str] = []
        ns_remaining = 0
        ns_rank = 0  # per-namespace position (1-based)

        for title, content in entries:
            if total >= _HARD_CAP:
                ns_remaining += 1
                continue
            ns_rank += 1
            if ns_rank <= _SNIPPET_LIMIT:
                snip = _snippet(content)
                ns_lines.append(f"  {title}" + (f" — {snip}" if snip else ""))
                total += 1
            elif ns_rank <= _TITLE_LIMIT:
                ns_lines.append(f"  {title}")
                total += 1
            else:
                ns_remaining += 1

        if ns_lines:
            lines.append(f"### {label}")
            lines.extend(ns_lines)
            if ns_remaining:
                lines.append(f"  … ({ns_remaining} more)")
            lines.append("")

    if skipped_for_cap:
        lines.append(
            f"  … ({skipped_for_cap} older namespace(s) not checked — "
            f"_MAX_NAMESPACES={_MAX_NAMESPACES})"
        )

    return lines


def _check_freshness(last_updated: str, stale_days: int) -> str | None:
    """Two-arm freshness assert, arm 1: the freshest entry across every
    matched namespace (``namespaces[0]["last_updated"]`` — already the max
    since the caller receives DESC order) is older than *stale_days*.

    Returns ``None`` when *last_updated* is empty/unparseable (never
    fabricate a warning from data we could not read) or is fresh enough.
    Arm 2 (source-unreachable) is handled entirely by ``_Unreachable`` at
    the call sites above — this function only ever sees a REACHABLE,
    non-empty result.
    """
    if not last_updated:
        return None
    try:
        ts = datetime.strptime(last_updated, _TIMESTAMP_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    age_days = (datetime.now(timezone.utc) - ts).days
    if age_days > stale_days:
        return (
            f"WARNING: T2 memory freshest entry is {age_days}d old "
            f"(> {stale_days}d threshold) — verify the T2 substrate is current"
        )
    return None


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: t2_prefix_scan.py <project_name>", file=sys.stderr)
        sys.exit(1)

    project_name = sys.argv[1]
    config_dir = _default_config_dir()
    timeout = float(os.environ.get("NX_T2_SCAN_TIMEOUT_S", str(_DEFAULT_HTTP_TIMEOUT_S)))
    stale_days = int(os.environ.get("NX_T2_SCAN_STALE_DAYS", str(_DEFAULT_STALE_DAYS)))
    scan_budget_s = float(
        os.environ.get("NX_T2_SCAN_BUDGET_S", str(_DEFAULT_SCAN_BUDGET_S))
    )

    base_url, via_data_token = "", False
    try:
        base_url, token, via_data_token = _resolve_endpoint(config_dir)
        namespaces = _get_namespaces(base_url, token, project_name, timeout)
    except _Unreachable as exc:
        # nexus-8fvp2 arm 2: source-unreachable is ALWAYS a visible line —
        # never a silent no-op, and never confused with "reachable but
        # genuinely empty" (a fresh install with no T2 entries yet).
        hint = ""
        if exc.http_status == 401 and base_url and not via_data_token:
            # nexus-znvjd: the static token was presented because no fresh
            # data-token lease matched this host at resolution time (keyed
            # on what was actually sent — never a second lease read, which
            # could disagree with the first). On an armed pass-through box
            # that token is mint-locked and this 401 is permanent until the
            # client mints a lease — say so, rather than reading as a
            # generic bad-token line.
            host = urllib.parse.urlsplit(base_url).netloc or base_url
            hint = (
                f" (no fresh data-token lease for {host} under "
                f"{config_dir}/{_DATA_TOKEN_LEASE_PREFIX}*, so the static "
                "service_token was presented; on an armed pass-through box "
                "that credential is mint-locked — run `nx search` once to "
                "mint a lease)"
            )
        print(f"WARNING: T2 memory unreachable: {exc}{hint}")
        return

    if not namespaces:
        # Reachable, zero matching namespaces: a genuinely empty T2 (fresh
        # install, or no entries under this prefix yet). Empty is not
        # stale (nexus-8fvp2 enlargement (b)) — stay silent exactly as the
        # pre-fix "no rows" case did.
        return

    # nexus-eg6qe: _build_output isolates per-namespace _Unreachable
    # failures internally (a warning line per bad namespace, not an
    # exception) — it no longer raises _Unreachable itself, so there is no
    # outer catch here to swallow a partial render.
    lines = _build_output(
        base_url, token, project_name, namespaces, timeout, scan_budget_s
    )

    freshness_warning = _check_freshness(namespaces[0].get("last_updated", ""), stale_days)
    if freshness_warning:
        lines.append(freshness_warning)
        lines.append("")

    if lines:
        print("\n".join(lines), end="")


if __name__ == "__main__":
    main()
