# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""DataTokenManager — client-side self-minting of short-TTL data tokens.

RDR-005 2a (nexus-wrwb7): once conexus retires the edge JIT-injection path
(conexus-qomd), the nexus client must hold its own ``scope=mint-locked``
credential and self-mint the short-TTL ``scope=data`` bearer it presents on
every engine call, instead of relying on the edge to inject one. This module
is that self-minting client.

DESIGN OF RECORD: bd show nexus-wrwb7 (comment 2026-08-16, "DESIGN OF
RECORD"). Engine contract: ``POST /v1/data-tokens/mint``
(``service/src/main/java/dev/nexus/service/http/DataTokenHandler.java``) —
``{"tenant": <tenant>, "ttl_seconds": <optional>} -> {"data_token": <raw>,
"expires_in_seconds": <n>}``, authenticated with a ``scope=mint`` or
``scope=mint-locked`` bearer.

Resolution seam (zero behavior change when unconfigured): when the
``mint_token`` credential (config key / ``NX_MINT_TOKEN`` env, same
``get_credential`` machinery as ``service_token``) is NOT configured,
:meth:`DataTokenManager.bearer_for` returns ``None`` and every caller falls
through to its existing static-``service_token`` resolution untouched. When
IT IS configured, callers present the manager's minted data token instead —
see ``nexus.db.t2._refreshable_client``, ``nexus.db.http_vector_client``,
and ``nexus.db.http_scratch_store`` for the three call sites (T1/T2/T3).

Mint failures with a mint credential CONFIGURED fail LOUD
(:class:`DataTokenMintError`) — a half-provisioned install (bad/revoked
credential, unreachable endpoint, cross-tenant 403) must surface, never
silently fall back to the static token as though nothing were configured.

Residue discipline (nexus-lgiqw): ONE live token per ``(base_url, tenant)``
key, cached and refreshed only when needed — never minted per call.

Mint-body tenant resolution (nexus-ssqk9): every ``Http*Store`` defaults its
own ``tenant`` constructor kwarg to ``DEFAULT_TENANT = "default"`` — but a
real ``scope=mint-locked`` credential is bound server-side to WHATEVER
tenant the operator issued it under (e.g. ``"nexus"``), and
``DataTokenHandler`` 403s the instant the mint request body's ``tenant``
differs from that bound tenant. The ``mint_tenant`` credential (config key /
``NX_MINT_TENANT`` env) lets an operator name the credential's real bound
tenant once; :meth:`DataTokenManager._mint` sends ``mint_tenant`` (when
configured) as the mint body's ``tenant`` field INSTEAD OF the caller-passed
tenant — the caller-passed tenant remains the CACHE key (unaffected) and is
still sent when ``mint_tenant`` is unconfigured (today's behavior,
unchanged). ``mint_token`` and ``mint_tenant`` travel as a PAIR: configuring
one without the other is a valid but likely wrong half-provisioned state
whenever the credential's bound tenant is not literally ``"default"``.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog

from nexus.rate_brake import parse_retry_after

_log = structlog.get_logger(__name__)

#: Default requested TTL for a self-minted data token (design of record).
#: The engine's own ceiling (default 3600s, env-overridable via
#: NX_DATA_TOKEN_TTL_CEILING_SECONDS) is authoritative server-side; this is
#: merely the client's DEFAULT request, not an enforced cap.
DEFAULT_TTL_SECONDS: int = 3600

#: Refresh a cached token once less than this fraction of its granted TTL
#: window remains (design of record: "<20% of TTL remains").
_REFRESH_THRESHOLD: float = 0.20

#: Transport timeout for the mint POST (design of record: "~10s").
_MINT_TIMEOUT_S: float = 10.0

#: HTTP statuses worth a small bounded retry inside a single ``bearer_for``
#: call (critic S2, nexus-ssqk9): MintRateLimiter genuinely 429s under load,
#: and a gateway in front of the engine can 502/503/504 transiently. Matches
#: ``nexus.retry._RETRYABLE_HTTP_STATUSES`` deliberately -- same transient
#: class, not redefined independently. 401/403/500 are NOT here: an auth
#: failure or a real validation error is not transient and must fail on the
#: first attempt (see ``test_mint_401_fails_loud_typed`` /
#: ``test_cross_tenant_403_surfaces_verbatim``).
_RETRYABLE_MINT_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})

#: Attempt budget for the mint retry: 1 initial + 2 retries (3 total).
_MINT_MAX_ATTEMPTS: int = 3

#: Backoff between attempts when the server supplies no Retry-After header
#: (design of record: "1s/2s"). Indexed by ``attempt - 1``.
_MINT_BACKOFF_SCHEDULE: tuple[float, ...] = (1.0, 2.0)

#: Site-specific ceiling on a single honored Retry-After sleep. The shared
#: ``parse_retry_after`` clamp is 300s — sized for the WRITE-path brake,
#: where a long pause trades run time for completion. A mint is a
#: synchronous auth round trip on interactive and shutdown paths (the
#: session-end launcher's telemetry summary documents a zero-wait-risk
#: invariant, and mixin adopters mint at construction time), so the worst
#: case must stay seconds-scale: 2 sleeps x 15s + 3 x 10s timeouts = 60s
#: hard ceiling. A server demanding a longer pause than this fails loud on
#: the final attempt instead — a mint the caller must wait minutes for is
#: an outage to surface, not absorb (review round-2 Significant,
#: nexus-ssqk9 thread).
_MINT_RETRY_AFTER_CAP_S: float = 15.0


class DataTokenMintError(RuntimeError):
    """Minting a data token failed and there is no silent fallback.

    Raised whenever a ``mint_token`` credential IS configured but the mint
    round trip itself fails (network error, non-200 response, missing
    ``data_token`` field). Callers must not catch this and silently keep
    using a stale/static token — a half-provisioned install (bad or revoked
    credential, unreachable endpoint, cross-tenant 403) has to surface.
    """


def _host(url: str) -> str:
    """Return ``host[:port]`` for *url* — never the full URL (which may
    carry a path) and never the credential — for log-safe endpoint identity.
    """
    try:
        netloc = urllib.parse.urlsplit(url).netloc
        return netloc or url
    except Exception:  # noqa: BLE001 — logging helper must never raise
        return url


def _default_poster(
    url: str, headers: dict[str, str], body: dict[str, Any],
) -> tuple[int, dict[str, Any], Mapping[str, str]]:
    """urllib-based POST, consistent with ``http_vector_client``'s transport
    (nexus-wrwb7 design of record: "via urllib consistent with
    http_vector_client's transport").

    Returns ``(status_code, parsed_json_body, response_headers)``. The third
    element (added nexus-ssqk9, critic S2) lets :meth:`DataTokenManager._mint`
    honor a server-supplied ``Retry-After`` on a retryable transient status —
    empty mapping when there is genuinely nothing to parse. Never raises for
    a non-2xx HTTP response (``urllib.error.HTTPError`` is caught and its
    body parsed the same as a success) — only a genuine transport failure
    (connection refused, DNS, timeout) propagates, which the caller wraps
    into :class:`DataTokenMintError`.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_MINT_TIMEOUT_S) as resp:  # noqa: S310 — fixed https/http scheme from configured base_url
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {}), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload: dict[str, Any] = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {"error": raw.decode("utf-8", errors="replace")}
        resp_headers = dict(exc.headers) if exc.headers is not None else {}
        return exc.code, payload, resp_headers


#: Type of the pluggable poster callable — swapped for a fake in tests.
#: Third element is the response headers (nexus-ssqk9, Retry-After parsing);
#: an empty mapping is valid when the caller has no headers to offer.
Poster = Callable[
    [str, dict[str, str], dict[str, Any]],
    tuple[int, dict[str, Any], Mapping[str, str]],
]


@dataclass
class _CachedToken:
    token: str
    minted_at: float
    expires_at: float
    ttl_seconds: float


class DataTokenManager:
    """Mints and caches short-TTL data tokens against a mint-locked credential.

    One live token per ``(base_url, tenant)`` key (nexus-lgiqw residue
    discipline). Thread-safe: the whole check-then-mint sequence is guarded
    by a single lock, so concurrent callers racing on an empty/expired cache
    entry mint exactly once — the simplest correct shape given a mint is a
    single bounded HTTP round trip, not a hot-path operation.

    Args:
        clock: Monotonic clock, injectable for deterministic tests.
        poster: ``(url, headers, body) -> (status, json_body, resp_headers)``
            transport, injectable for deterministic tests (no real network).
        mint_credential: Optional override returning the mint bearer
            directly (test injection point). ``None`` (the default) reads
            the ``mint_token`` credential via ``nexus.config.get_credential``
            (config.yml / ``NX_MINT_TOKEN`` env — env wins).
        mint_tenant: Optional override returning the mint-body tenant
            directly (test injection point). ``None`` (the default) reads
            the ``mint_tenant`` credential via ``nexus.config.get_credential``
            (config.yml / ``NX_MINT_TENANT`` env — env wins). Empty/unset
            falls back to the caller-passed tenant at mint time (nexus-ssqk9).
        default_ttl_seconds: Requested ``ttl_seconds`` on each mint.
        sleep: Backoff sleep, injectable for deterministic tests (critic S2 —
            no real ``time.sleep`` in a unit test).
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        poster: Poster = _default_poster,
        mint_credential: Callable[[], str] | None = None,
        mint_tenant: Callable[[], str] | None = None,
        default_ttl_seconds: int = DEFAULT_TTL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock = clock
        self._poster = poster
        self._mint_credential = mint_credential
        self._mint_tenant = mint_tenant
        self._default_ttl_seconds = default_ttl_seconds
        self._sleep = sleep
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], _CachedToken] = {}

    # ── Credential resolution ────────────────────────────────────────────────

    def _resolve_credential(self) -> str:
        if self._mint_credential is not None:
            return (self._mint_credential() or "").strip()
        from nexus.config import get_credential  # noqa: PLC0415 — deferred to avoid circular import

        return (get_credential("mint_token") or "").strip()

    def _resolve_mint_tenant(self) -> str:
        """The configured ``mint_tenant`` override, or ``""`` when unset —
        an empty result means "send the caller-passed tenant" (today's
        behavior, nexus-ssqk9)."""
        if self._mint_tenant is not None:
            return (self._mint_tenant() or "").strip()
        from nexus.config import get_credential  # noqa: PLC0415 — deferred to avoid circular import

        return (get_credential("mint_tenant") or "").strip()

    def is_configured(self) -> bool:
        """True when a ``mint_token`` credential is configured."""
        return bool(self._resolve_credential())

    # ── Public API ───────────────────────────────────────────────────────────

    def bearer_for(self, base_url: str, tenant: str) -> str | None:
        """Return a valid data-token bearer for ``(base_url, tenant)``.

        Returns ``None`` when no mint credential is configured — the caller
        falls back to its existing static-token resolution unchanged (ZERO
        behavior change for unconfigured installs). When a credential IS
        configured, mints (or reuses a cached, non-expiring-soon token) and
        returns the raw data-token string; a mint failure raises
        :class:`DataTokenMintError` — never a silent ``None`` in that case.
        """
        credential = self._resolve_credential()
        if not credential:
            return None
        key = (base_url.rstrip("/"), tenant)
        with self._lock:
            cached = self._cache.get(key)
            first_mint = cached is None
            if cached is not None and not self._needs_refresh(cached):
                return cached.token
            fresh = self._mint(base_url, tenant, credential)
            self._cache[key] = fresh
            event = "data_token_minted" if first_mint else "data_token_refresh"
            _log.info(event, tenant=tenant, expires_in=fresh.ttl_seconds, endpoint=_host(base_url))
            return fresh.token

    def invalidate(self, base_url: str, tenant: str) -> None:
        """Drop the cached token for ``(base_url, tenant)``, if any.

        Called by a consuming client after it observes a 401 with this
        token — the next :meth:`bearer_for` call re-mints instead of
        returning the same rejected value.
        """
        key = (base_url.rstrip("/"), tenant)
        with self._lock:
            had = self._cache.pop(key, None) is not None
        if had:
            _log.info("data_token_invalidated", tenant=tenant, endpoint=_host(base_url))

    def has_live_token(self, base_url: str, tenant: str) -> bool:
        """True when a cached, not-yet-due-for-refresh token already exists
        for ``(base_url, tenant)`` — a PEEK, never triggers a mint.

        Critic S3 (nexus-ssqk9): lets a caller (the ``nx doctor`` check) tell
        "this call is about to REUSE a live token" from "this call is about
        to MINT a fresh one" before calling :meth:`bearer_for`, without
        itself causing a mint (unlike constructing a throwaway
        ``DataTokenManager()`` per invocation, which always mints on its
        first call — the exact residue-discipline bug this method fixes).
        """
        key = (base_url.rstrip("/"), tenant)
        with self._lock:
            cached = self._cache.get(key)
            return cached is not None and not self._needs_refresh(cached)

    def granted_ttl_seconds(self, base_url: str, tenant: str) -> float | None:
        """The GRANTED ``ttl_seconds`` (from the mint response, not time
        remaining) of the cached token for ``(base_url, tenant)``, or
        ``None`` when nothing is cached — a PEEK, never triggers a mint.

        Critic S1 (nexus-ssqk9): the doctor check's own docstring, and two
        other doc surfaces, already claimed the success line reports the
        granted TTL; this is what makes that claim true.
        """
        key = (base_url.rstrip("/"), tenant)
        with self._lock:
            cached = self._cache.get(key)
            return cached.ttl_seconds if cached is not None else None

    # ── Internal ─────────────────────────────────────────────────────────────

    def _needs_refresh(self, cached: _CachedToken) -> bool:
        remaining = cached.expires_at - self._clock()
        return remaining <= cached.ttl_seconds * _REFRESH_THRESHOLD

    def _mint(self, base_url: str, tenant: str, credential: str) -> _CachedToken:
        url = base_url.rstrip("/") + "/v1/data-tokens/mint"
        headers = {"Authorization": f"Bearer {credential}"}
        # nexus-ssqk9: the mint BODY tenant is the configured mint_tenant
        # override when set, else the caller-passed tenant (today's
        # behavior) — see the module docstring's "Mint-body tenant
        # resolution" section. The CACHE KEY (bearer_for's own concern,
        # already computed by the caller) is unaffected: it stays keyed on
        # the caller-passed tenant regardless of this override.
        configured_mint_tenant = self._resolve_mint_tenant()
        body_tenant = configured_mint_tenant or tenant
        body: dict[str, Any] = {"tenant": body_tenant, "ttl_seconds": self._default_ttl_seconds}

        status: int
        payload: dict[str, Any]
        for attempt in range(1, _MINT_MAX_ATTEMPTS + 1):
            try:
                status, payload, resp_headers = self._poster(url, headers, body)
            except Exception as exc:  # noqa: BLE001 — any transport failure becomes the typed mint error
                _log.error("data_token_mint_failed", tenant=body_tenant, endpoint=_host(url), error=str(exc))
                raise DataTokenMintError(
                    f"data-token mint request to {_host(url)} failed: {exc}"
                ) from exc

            if status not in _RETRYABLE_MINT_STATUSES or attempt == _MINT_MAX_ATTEMPTS:
                break
            # critic S2 (nexus-ssqk9): a SMALL bounded retry on a transient
            # gateway/rate-limit status — never touches the shared
            # nexus.rate_brake brake (that brake coordinates WRITE-path
            # workers across a bulk indexing run; a mint is a single
            # infrequent auth round trip, including at construction time
            # for mixin adopters, so sharing that brake would couple two
            # unrelated concerns). Honors a server Retry-After when present.
            retry_after = parse_retry_after(resp_headers)
            if retry_after is not None:
                # Site-specific cap: the shared 300s clamp is write-path
                # sized; a synchronous mint must stay seconds-scale (see
                # _MINT_RETRY_AFTER_CAP_S).
                delay = min(retry_after, _MINT_RETRY_AFTER_CAP_S)
            else:
                delay = _MINT_BACKOFF_SCHEDULE[attempt - 1]
            _log.warning(
                "data_token_mint_retry",
                tenant=body_tenant, endpoint=_host(url), status=status,
                attempt=attempt, delay=delay, retry_after_present=retry_after is not None,
            )
            self._sleep(delay)

        if status == 403:
            err = payload.get("error", "") if isinstance(payload, dict) else str(payload)
            _log.error(
                "data_token_mint_failed", tenant=body_tenant, endpoint=_host(url),
                status=403, error=err,
            )
            # nexus-ssqk9: name BOTH the configured/requested tenant and the
            # remedy — a mint-locked credential 403s the instant the body
            # tenant does not match its OWN bound tenant, and the client has
            # no way to learn that bound tenant except from this message.
            raise DataTokenMintError(
                f"data-token mint failed (403) against {_host(url)}: {err}. "
                f"Sent tenant={body_tenant!r} (mint_tenant config="
                f"{configured_mint_tenant or '(unset)'!r}, caller-supplied "
                f"tenant={tenant!r}). A mint-locked credential is bound to "
                f"exactly one tenant server-side and 403s any other. Remedy: "
                f"'nx config set mint_tenant <the credential's bound tenant>'."
            )

        if status != 200:
            err = payload.get("error", "") if isinstance(payload, dict) else str(payload)
            _log.error(
                "data_token_mint_failed", tenant=body_tenant, endpoint=_host(url),
                status=status, error=err,
            )
            raise DataTokenMintError(
                f"data-token mint failed ({status}) against {_host(url)}: {err}. "
                f"Remedy: verify the configured mint_token credential is valid, "
                f"unrevoked, and (if mint-locked) bound to tenant {body_tenant!r} "
                f"('nx config set mint_token <bearer>')."
            )

        token = payload.get("data_token") if isinstance(payload, dict) else None
        if not token:
            _log.error("data_token_mint_failed", tenant=body_tenant, endpoint=_host(url), error="no data_token in response")
            raise DataTokenMintError(
                f"data-token mint against {_host(url)} returned no 'data_token' field"
            )
        ttl = payload.get("expires_in_seconds", self._default_ttl_seconds)
        try:
            ttl_seconds = float(ttl)
        except (TypeError, ValueError):
            ttl_seconds = float(self._default_ttl_seconds)
        now = self._clock()
        return _CachedToken(token=token, minted_at=now, expires_at=now + ttl_seconds, ttl_seconds=ttl_seconds)


# ── Module-level default accessor (nexus-wrwb7 design of record) ────────────

_default_manager: DataTokenManager | None = None
_default_manager_lock = threading.Lock()


def get_data_token_manager() -> DataTokenManager:
    """Process-wide default :class:`DataTokenManager` singleton."""
    global _default_manager
    with _default_manager_lock:
        if _default_manager is None:
            _default_manager = DataTokenManager()
        return _default_manager


def reset_data_token_manager() -> None:
    """Test-only: drop the singleton so the next call mints a fresh one."""
    global _default_manager
    with _default_manager_lock:
        _default_manager = None
