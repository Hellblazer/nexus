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

Cross-process lease-file cache (nexus-9c7t9): the in-process cache above
solves residue/rate-limit pressure WITHIN one long-lived process (the MCP
server); it does nothing for short-lived ``nx`` CLI subprocesses, where
every invocation starts with an empty ``_cache`` dict and mints fresh. Once
a mint credential is configured, minting more than
``MintRateLimiter.burst=5`` times per (credential, tenant) per minute
fails loud server-side — five back-to-back CLI invocations exhausts it.

Fix: mirror the lease-file precedent in :mod:`nexus.db.t1`
(``publish_t1_session_lease`` / ``read_t1_session_lease``) — persist the
short-TTL DATA token (never the mint credential) to
``~/.config/nexus/data_token_lease.<key>``, where ``<key>`` is a
filesystem-safe digest of ``(base_url host:port, tenant)`` (never a raw
URL in the filename — see :func:`_lease_key`). Mode ``0600``, atomic
temp-file + ``os.replace`` publish. On a genuine in-process cache MISS
(never on a refresh-due-but-still-cached entry), :meth:`bearer_for` reads
the lease file BEFORE minting; it is accepted only when its format
version, tenant, and base-url digest all match AND its remaining TTL
exceeds the same 20% :data:`_REFRESH_THRESHOLD` the in-process cache
enforces. A refresh-due-but-still-cached entry deliberately does NOT
consult the lease: a sibling may well have republished something newer,
but a refresh-due borrower would re-check within one threshold window
anyway, and always minting on refresh keeps exactly one code path
responsible for extending the machine-wide token lifetime (the trade is
one possibly-redundant mint per TTL window against a second
read-then-trust branch on the hot path). Any other state (absent,
corrupt, foreign, stale) is treated as a clean miss (debug log) and the
manager mints as before. Every
successful mint (fresh OR refresh) publishes the lease so the NEXT cold
process can borrow it; a lease-write failure is logged as a warning and
never fails the mint itself — the lease is an optimization, the mint is
the source of truth. :meth:`invalidate` (the 401 self-heal path) removes
the lease file alongside the in-process cache entry, best-effort.

Concurrency, UPDATED (nexus-nnr26): the original design of record (nexus-
9c7t9 point 3) left mint-on-miss deliberately unlocked — two cold processes
racing to fill an empty/stale cache slot might both mint and both publish,
last writer wins on the file, and the loser's own in-process token is still
perfectly valid (it just isn't the one on disk any more). That reasoning is
still correct for the SMALL, near-simultaneous race (bounded to ~2 wasted
mints, well under `MintRateLimiter`'s burst=5) — a double mint there is an
efficiency cost, not a correctness bug, and does not by itself justify
``fcntl.flock``.

What changed: a scripted fan-out of M>5 TRULY CONCURRENT cold processes
(parallel ``&`` loops, parallel make, CI legs) is a DIFFERENT race class —
every one of them misses the lease and mints, `MintRateLimiter`'s burst=5
absorbs only the first five, and the excess M-5 hard-fail with
:class:`DataTokenMintError`: the client's own mint retry (3 attempts,
``Retry-After`` capped at :data:`_MINT_RETRY_AFTER_CAP_S`, ~30-60s total,
see :meth:`DataTokenManager._mint`) is structurally incapable of bridging
the server's 1/min refill window, so this is not a rare edge case that
usually self-heals — it is a DETERMINISTIC hard-fail of user commands for
any scripted fan-out wider than the burst. That crosses the correctness-
only bar nexus-9c7t9 point 3 set: a CI gate or automation script reddening
because 3 of 8 concurrent ``nx`` invocations raised
:class:`DataTokenMintError` is a correctness problem for that script, even
though no token data is ever corrupted.

Fix: :meth:`DataTokenManager._mint_guarded`, a per-``(base_url, tenant)``
NON-BLOCKING ``fcntl.flock`` around mint-on-miss ONLY — mirroring the
shipped :func:`nexus.db.t1._lock_guarded_mint_or_borrow` precedent, but
poll-then-re-read rather than block: a losing racer tries a non-blocking
exclusive acquire; on failure it re-reads the lease file (the winner will
have published while holding the lock) and returns the borrowed token the
instant it appears, without ever itself acquiring the lock. Only the lock
HOLDER mints. A bounded wait ceiling (:data:`_MINT_LOCK_WAIT_CEILING_S`,
DERIVED per nexus-9rr0a from the mint's own worst-case retry wall
constants plus explicit headroom — see :data:`_MINT_RETRY_WALL_WORST_CASE_S`
and :data:`_MINT_LOCK_WAIT_HEADROOM_S` — never a hand-typed literal)
protects against a holder that never publishes (e.g. its own mint failed)
— past the ceiling the waiter fails loud with :class:`DataTokenMintError`
(naming BOTH possibilities — a genuinely stuck sibling, or a degraded mint
endpoint whose sibling is still within its own legitimate retry budget —
nexus-9rr0a) rather than hang indefinitely, mirroring the deadline-exceeded
behavior of the t1 precedent's own bounded-poll variant (nexus-by875).
Reads stay entirely lock-free: the guard wraps ONLY the genuine
cache-miss-with-no-fresh-lease path (see :meth:`DataTokenManager.bearer_for`)
— an existing fresh lease is still borrowed without ever touching the lock
file, so the reuse happy path carries zero lock-contention cost.

Per-key in-process locking (nexus-7qz06): the in-process guard around
:meth:`bearer_for`'s check-then-mint sequence is SHARDED per
``(base_url, tenant)`` key (a dict of ``threading.Lock`` objects behind a
short-lived registry lock, see :meth:`DataTokenManager._lock_for`) rather
than one process-wide lock. A slow/degraded mint-on-miss race for one key
(up to the full :data:`_MINT_LOCK_WAIT_CEILING_S` wait, plus this caller's
own mint attempt if it becomes the next holder) no longer blocks an
UNRELATED key's ``bearer_for`` call in the same process — relevant to a
long-lived multi-tenant process (e.g. an MCP server juggling several
tenants/endpoints through the process-wide :func:`get_data_token_manager`
singleton), which is exactly the shape this sharding targets.

Scope boundary — HOST-LOCAL ONLY (nexus-b0svi): the ``fcntl.flock`` guard
above coordinates processes on ONE machine. It provides ZERO coordination
across DIFFERENT hosts sharing the same ``mint_token`` credential — a
multi-host fleet racing a cold start (e.g. a CI fleet of separate runners,
or nexus-wrwb7's eventual multi-machine edge-JIT-retirement rollout) still
deterministically hits ``MintRateLimiter``'s ``burst=5`` hard-fail exactly
as before this fix, just at HOST granularity instead of process
granularity. This fix closes the single-machine fan-out case only; a
multi-host rollout needs either server-side coordination or per-host
credentials, and should not treat this module's docstring as having
already solved that case.

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

import fcntl
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

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

#: Cross-process lease file (nexus-9c7t9): filename prefix under
#: ``nexus_config_dir()``, mirroring ``nexus.db.t1``'s
#: ``t1_session_lease.<session_id>`` naming convention.
_DATA_TOKEN_LEASE_PREFIX: str = "data_token_lease."

#: Bumped whenever the lease-file JSON shape changes. A lease written by a
#: mismatched format version is treated as absent (fail-safe), never
#: partially trusted.
_LEASE_FORMAT_VERSION: int = 1

#: Cross-process mint-on-miss lock file (nexus-nnr26): filename prefix under
#: ``nexus_config_dir()``, mirroring ``nexus.db.t1``'s
#: ``t1_mint_<session_id>.lock`` naming convention. A SEPARATE file from the
#: lease itself -- the lock guards only the miss-then-mint critical section,
#: never a read.
_MINT_LOCK_PREFIX: str = "data_token_mint_lock."

#: Poll interval while a non-blocking flock acquire attempt is retried
#: (nexus-nnr26) -- mirrors nexus.db.t1._lock_guarded_mint_or_borrow's
#: bounded-poll variant's 0.05s interval exactly.
_MINT_LOCK_POLL_INTERVAL_S: float = 0.05

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

#: DERIVED (nexus-9rr0a): the worst-case wall time a single
#: :meth:`DataTokenManager._mint` call can legitimately burn retrying
#: against a degraded server (repeated 429/502/503/504) before it either
#: succeeds or gives up and raises. Every attempt pays its own transport
#: timeout; every attempt but the last is followed by a sleep capped at
#: ``_MINT_RETRY_AFTER_CAP_S`` (an honored server ``Retry-After`` can hit
#: that cap; the unforced ``_MINT_BACKOFF_SCHEDULE`` values are smaller and
#: therefore never the binding case here):
#:
#:     _MINT_MAX_ATTEMPTS(3) x _MINT_TIMEOUT_S(10s)                = 30s
#:   + (_MINT_MAX_ATTEMPTS(3) - 1) x _MINT_RETRY_AFTER_CAP_S(15s)  = 30s
#:   ---------------------------------------------------------------------
#:                                                                  = 60s
#:
#: A module-level derivation (not a hand-typed literal) so this and
#: :data:`_MINT_LOCK_WAIT_CEILING_S` below can never silently drift apart
#: the way a hand-maintained comment-vs-literal pair can (nexus-9rr0a,
#: filed from a substantive-critic finding on nexus-nnr26: the prior 65.0s
#: literal carried only ~8%/5s margin over this same 60s figure, computed
#: by hand in a comment rather than from these constants).
_MINT_RETRY_WALL_WORST_CASE_S: float = (
    _MINT_MAX_ATTEMPTS * _MINT_TIMEOUT_S
    + (_MINT_MAX_ATTEMPTS - 1) * _MINT_RETRY_AFTER_CAP_S
)

#: Explicit headroom (nexus-9rr0a) above :data:`_MINT_RETRY_WALL_WORST_CASE_S`
#: for :data:`_MINT_LOCK_WAIT_CEILING_S` below. A waiting racer's deadline
#: is evaluated ONLY while it is still waiting for a BUSY lock (see
#: :meth:`DataTokenManager._mint_guarded`) — the instant it becomes the
#: holder itself, it is free to run its own full retry wall with no further
#: deadline check. So the risk this headroom defends against is narrower
#: than "two retry walls back to back": it is a waiting racer's deadline
#: firing while the CURRENT holder is still legitimately inside its own
#: single retry wall (scheduling jitter, GC pauses, or a Retry-After
#: response arriving a beat later than the theoretical cap). A full extra
#: retry-wall's worth of headroom (rather than the prior ~8%) is cheap to
#: grant — this path only ever costs real wall time when the mint endpoint
#: is already degraded, i.e. the unhappy path is already slow.
#:
#: DELIBERATE POLICY BOUNDARY (nexus-9rr0a critique): the ceiling below
#: (wall + this headroom = two walls) rides out at most ONE holder-to-holder
#: handoff — holder A burns its full wall and fails, holder B (some other
#: racer that won the released lock) starts a fresh wall — before a racer
#: STILL waiting fails loud. A waiter that wins the lock at any point stops
#: being subject to the deadline entirely (it runs its own wall as holder),
#: so the only exposure is a waiter that loses the lock race across two or
#: more consecutive predecessors' walls against a degraded endpoint. No
#: finite ceiling survives an arbitrary chain; two walls then fail-loud is
#: the chosen bound, and the timeout message names the chain case so the
#: error is not misread as a stuck sibling.
_MINT_LOCK_WAIT_HEADROOM_S: float = _MINT_RETRY_WALL_WORST_CASE_S

#: Hard ceiling on how long a losing racer waits for the lock holder to
#: either publish a lease or release the lock (nexus-nnr26; DERIVED per
#: nexus-9rr0a from :data:`_MINT_RETRY_WALL_WORST_CASE_S` +
#: :data:`_MINT_LOCK_WAIT_HEADROOM_S` above, rather than a hand-typed
#: literal). Past this, the wait fails loud (DataTokenMintError) rather
#: than block indefinitely -- mirrors
#: nexus.db.t1._lock_guarded_mint_or_borrow's deadline-exceeded behavior
#: (nexus-by875).
_MINT_LOCK_WAIT_CEILING_S: float = (
    _MINT_RETRY_WALL_WORST_CASE_S + _MINT_LOCK_WAIT_HEADROOM_S
)


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


def _lease_key(base_url: str, tenant: str) -> str:
    """Filesystem-safe digest identifying a ``(base_url host:port, tenant)``
    pair for the lease-file name (nexus-9c7t9 design point 1: "do not embed
    a raw URL in the filename"). Deterministic, collision-resistant, and
    stable across processes -- the same digest for the same pair every
    time, which is what lets a cold process compute the exact lease path
    to check without ever listing the directory.

    The FULL 64-char hex digest is kept (never sliced) -- this is a
    filename, not a chash, but `tests/test_no_chash_truncation.py`'s
    repo-wide ``hexdigest()[:N]`` scan cannot tell the two apart, and a
    full digest is just as valid a filename component as a truncated one.
    """
    host = _host(base_url)
    return hashlib.sha256(f"{host}\x00{tenant}".encode("utf-8")).hexdigest()


def _data_token_lease_path(base_url: str, tenant: str, config_dir: Path) -> Path:
    return config_dir / f"{_DATA_TOKEN_LEASE_PREFIX}{_lease_key(base_url, tenant)}"


def _data_token_mint_lock_path(base_url: str, tenant: str, config_dir: Path) -> Path:
    """Per-``(base_url, tenant)`` lock-file path guarding
    :meth:`DataTokenManager._mint_guarded`'s mint-on-miss critical section
    (nexus-nnr26). Distinct from :func:`_data_token_lease_path` -- the lock
    file never holds a token, only a flock target."""
    return config_dir / f"{_MINT_LOCK_PREFIX}{_lease_key(base_url, tenant)}"


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
    discipline). Thread-safe: the whole check-then-mint sequence for a given
    key is guarded by a lock SHARDED per ``(base_url, tenant)`` key
    (nexus-7qz06 — see :meth:`_lock_for`), so concurrent callers racing on
    the SAME key's empty/expired cache entry mint exactly once, while
    callers racing on DIFFERENT keys never serialize on each other. Prior to
    nexus-7qz06 a single process-wide lock was held across the full
    mint-on-miss poll loop (up to :data:`_MINT_LOCK_WAIT_CEILING_S`), so a
    slow cold-start race for one key could stall an unrelated key's
    ``bearer_for`` in the same process — see the module docstring's
    "Per-key in-process locking" section.

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
        config_dir: Directory the cross-process lease file lives under
            (nexus-9c7t9). ``None`` (the default) resolves
            ``nexus.config.nexus_config_dir()`` lazily on first use —
            deferred so importing this module never pulls in the config
            module's own import graph. Injectable in tests (a ``tmp_path``)
            so lease-file tests never touch the real ``~/.config/nexus``.
        wall_clock: Wall-clock time source for the lease file's absolute
            ``expires_at`` (nexus-9c7t9). Deliberately SEPARATE from
            ``clock`` (which stays monotonic, in-process-only, and
            untouched by this change) -- a lease file is read by a
            DIFFERENT process with its own monotonic clock, so only
            wall-clock time is comparable across processes. Injectable
            for deterministic tests; defaults to ``time.time``.
        lock_wait_ceiling_seconds: Hard ceiling on how long a losing racer
            waits for the mint-on-miss lock holder to publish or release
            (nexus-nnr26). Defaults to :data:`_MINT_LOCK_WAIT_CEILING_S`,
            DERIVED (nexus-9rr0a) from the mint's own worst-case retry wall
            plus explicit headroom -- see :data:`_MINT_RETRY_WALL_WORST_CASE_S`
            and :data:`_MINT_LOCK_WAIT_HEADROOM_S`. Injectable so a test can
            force the deadline-exceeded path without a real multi-second
            wait. Deliberately measured against real
            ``time.monotonic()`` inside :meth:`_mint_guarded`, never the
            injectable ``clock`` above -- mirrors
            ``nexus.db.t1._lock_guarded_mint_or_borrow``'s own deadline,
            which stays wired to real time regardless of any test clock
            injected for cache-TTL purposes.
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
        config_dir: Path | None = None,
        wall_clock: Callable[[], float] = time.time,
        lock_wait_ceiling_seconds: float = _MINT_LOCK_WAIT_CEILING_S,
    ) -> None:
        self._clock = clock
        self._poster = poster
        self._mint_credential = mint_credential
        self._mint_tenant = mint_tenant
        self._default_ttl_seconds = default_ttl_seconds
        self._sleep = sleep
        self._config_dir = config_dir
        self._wall_clock = wall_clock
        self._lock_wait_ceiling_seconds = lock_wait_ceiling_seconds
        # nexus-7qz06: sharded per-(base_url, tenant) locks rather than one
        # process-wide lock — see _lock_for. _registry_lock guards only the
        # dict's own get-or-create, never the check-then-mint sequence a
        # per-key lock protects.
        self._registry_lock = threading.Lock()
        self._key_locks: dict[tuple[str, str], threading.Lock] = {}
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

    def _resolve_config_dir(self) -> Path:
        if self._config_dir is not None:
            return self._config_dir
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import

        return nexus_config_dir()

    def _lock_for(self, key: tuple[str, str]) -> threading.Lock:
        """Return the per-``(base_url, tenant)`` lock for *key*, creating it
        under a short-lived registry lock on first use (nexus-7qz06).

        Locks are never removed once created — one extra ``threading.Lock``
        object per distinct key for the life of the process is negligible,
        and removing entries would reintroduce a race between "no one holds
        this key's lock right now" and deleting the very object a
        concurrent racer is about to acquire.
        """
        with self._registry_lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    # ── Public API ───────────────────────────────────────────────────────────

    def bearer_for(self, base_url: str, tenant: str) -> str | None:
        """Return a valid data-token bearer for ``(base_url, tenant)``.

        Returns ``None`` when no mint credential is configured — the caller
        falls back to its existing static-token resolution unchanged (ZERO
        behavior change for unconfigured installs). When a credential IS
        configured, mints (or reuses a cached, non-expiring-soon token) and
        returns the raw data-token string; a mint failure raises
        :class:`DataTokenMintError` — never a silent ``None`` in that case.

        A genuine cache miss with no fresh cross-process lease is
        flock-guarded (:meth:`_mint_guarded`, nexus-nnr26) so concurrent
        cold-start callers converge on one mint instead of each minting
        independently — see the module docstring's "Concurrency, UPDATED
        (nexus-nnr26)" section.
        """
        credential = self._resolve_credential()
        if not credential:
            return None
        key = (base_url.rstrip("/"), tenant)
        with self._lock_for(key):
            cached = self._cache.get(key)
            first_mint = cached is None
            if cached is not None and not self._needs_refresh(cached):
                return cached.token
            if first_mint:
                # nexus-9c7t9: a genuine cache MISS (never a refresh-due
                # entry — see the module docstring's "Cross-process
                # lease-file cache" section) tries the cross-process
                # lease file before minting.
                leased = self._read_lease(base_url, tenant)
                if leased is not None:
                    self._cache[key] = leased
                    _log.info(
                        "data_token_lease_reused", tenant=tenant, endpoint=_host(base_url),
                    )
                    return leased.token
                # nexus-nnr26: genuine miss AND no fresh lease — serialize
                # concurrent cold-start racers via a non-blocking flock so
                # only the lock holder mints; every other racer borrows
                # instead of independently minting a competing token. This
                # is the ONLY path that ever touches the lock file — the
                # lease-hit branch above stays entirely lock-free.
                fresh = self._mint_guarded(base_url, tenant, credential)
                self._cache[key] = fresh
                return fresh.token
            fresh = self._mint(base_url, tenant, credential)
            self._cache[key] = fresh
            self._write_lease(base_url, tenant, fresh)
            _log.info(
                "data_token_refresh", tenant=tenant, expires_in=fresh.ttl_seconds, endpoint=_host(base_url),
            )
            return fresh.token

    def invalidate(self, base_url: str, tenant: str) -> None:
        """Drop the cached token for ``(base_url, tenant)``, if any.

        Called by a consuming client after it observes a 401 with this
        token — the next :meth:`bearer_for` call re-mints instead of
        returning the same rejected value. Also removes the cross-process
        lease file (nexus-9c7t9, best-effort) so a sibling process does not
        borrow the same now-rejected token — but ONLY when the on-disk
        lease still holds the token being invalidated (compare-and-delete,
        review round-1 Significant): in a 401 storm two processes hold the
        same revoked token, and after the first one re-mints and publishes
        a FRESH lease, the second one's invalidate must not wipe that
        fresh sibling lease (it would force a needless extra mint during
        exactly the recovery path this cache exists to smooth). A process
        with no in-process record of the token skips the file entirely for
        the same reason: it has nothing to compare, so deleting would only
        ever clobber a sibling.
        """
        key = (base_url.rstrip("/"), tenant)
        with self._lock_for(key):
            popped = self._cache.pop(key, None)
        if popped is not None:
            self._delete_lease(base_url, tenant, expected_token=popped.token)
            _log.info("data_token_invalidated", tenant=tenant, endpoint=_host(base_url))

    def has_fresh_lease(self, base_url: str, tenant: str) -> bool:
        """True when a fresh (not due-for-refresh) cross-process lease-file
        entry exists for ``(base_url, tenant)`` — a PEEK, never mints,
        never populates the in-process cache (nexus-9c7t9).

        Distinct from :meth:`has_live_token`, which only checks the
        in-process dict. Lets a caller (the ``nx doctor`` check) report
        "reused (lease file)" separately from "reused (in-process)" and
        "minted a fresh".
        """
        return self._read_lease(base_url, tenant) is not None

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
        with self._lock_for(key):
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
        with self._lock_for(key):
            cached = self._cache.get(key)
            return cached.ttl_seconds if cached is not None else None

    # ── Internal ─────────────────────────────────────────────────────────────

    def _needs_refresh(self, cached: _CachedToken) -> bool:
        remaining = cached.expires_at - self._clock()
        return remaining <= cached.ttl_seconds * _REFRESH_THRESHOLD

    # ── Cross-process lease file (nexus-9c7t9) ──────────────────────────────

    def _read_lease(self, base_url: str, tenant: str) -> _CachedToken | None:
        """Read the cross-process lease file for ``(base_url, tenant)``, IF
        it is fresh and genuinely belongs to this pair. Returns ``None`` on
        ANY of: absent, corrupt, wrong format version, tenant/digest
        mismatch, or remaining TTL at or below the refresh threshold —
        fail-safe, never fail-open (mirrors ``nexus.db.t1.read_t1_session_
        lease``'s stance exactly). Never raises, never mints, never mutates
        ``self._cache`` — a pure read, safe to call as a peek
        (:meth:`has_fresh_lease`) or as part of :meth:`bearer_for`.
        """
        path = _data_token_lease_path(base_url, tenant, self._resolve_config_dir())
        try:
            raw = path.read_text()
            data = json.loads(raw)
            if data.get("format_version") != _LEASE_FORMAT_VERSION:
                return None
            if data.get("tenant") != tenant:
                return None
            if data.get("base_url_digest") != _lease_key(base_url, tenant):
                return None
            token = data["token"]
            expires_at_wall = float(data["expires_at"])
            ttl_seconds = float(data["ttl_seconds"])
        except OSError:
            return None  # absent — the common case, not worth a log line
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            _log.debug("data_token_lease_unparseable", tenant=tenant, endpoint=_host(base_url))
            return None
        if not token:
            return None
        remaining = expires_at_wall - self._wall_clock()
        if remaining <= ttl_seconds * _REFRESH_THRESHOLD:
            # Expired, or too close to expiry to be worth borrowing (same
            # 20% threshold the in-process cache enforces) — a caller that
            # borrowed this would pay a near-immediate re-mint anyway.
            return None
        now = self._clock()
        return _CachedToken(token=token, minted_at=now, expires_at=now + remaining, ttl_seconds=ttl_seconds)

    def _write_lease(self, base_url: str, tenant: str, cached: _CachedToken) -> None:
        """Publish *cached* to the cross-process lease file, best-effort.

        Never raises: a write failure is a lost optimization for the NEXT
        cold process, not a reason to fail a mint that already succeeded
        (nexus-9c7t9 design point 2 — "never fails the mint"). Atomic
        temp-file + ``os.replace`` publish, mode ``0600``, mirroring
        :func:`nexus.db.t1.publish_t1_session_lease` exactly. Never
        contains the mint credential — only the short-TTL data token.
        """
        try:
            remaining = cached.expires_at - self._clock()
            if remaining <= 0:
                return  # already expired by the time we got here — nothing worth publishing
            config_dir = self._resolve_config_dir()
            config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = _data_token_lease_path(base_url, tenant, config_dir)
            payload = {
                "format_version": _LEASE_FORMAT_VERSION,
                "token": cached.token,
                "tenant": tenant,
                "base_url_digest": _lease_key(base_url, tenant),
                "expires_at": self._wall_clock() + remaining,
                "ttl_seconds": cached.ttl_seconds,
                "minted_by_pid": os.getpid(),
            }
            data = json.dumps(payload).encode("utf-8")
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
            fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            os.replace(str(tmp), str(path))
        except OSError as exc:
            _log.warning(
                "data_token_lease_write_failed", tenant=tenant, endpoint=_host(base_url), error=str(exc),
            )

    def _delete_lease(
        self, base_url: str, tenant: str, *, expected_token: str | None = None,
    ) -> None:
        """Remove the cross-process lease file, best-effort/idempotent
        (mirrors :func:`nexus.db.t1.clear_t1_session_lease`).

        With ``expected_token`` given (the :meth:`invalidate` path), the
        file is unlinked ONLY when its on-disk token still equals that
        value — compare-and-delete, so invalidating a revoked token never
        clobbers a FRESH lease a sibling process republished in the
        meantime (401-storm interleave, review round-1 Significant). The
        read-compare-unlink is not atomic; the surviving race window is a
        sibling publishing between the compare and the unlink, which
        costs that sibling one extra mint — the same bounded, harmless
        class as the documented cold-start double-mint.
        """
        path = _data_token_lease_path(base_url, tenant, self._resolve_config_dir())
        try:
            if expected_token is not None:
                data = json.loads(path.read_text())
                if data.get("token") != expected_token:
                    return
            path.unlink()
        except (OSError, ValueError):
            # Unreadable/corrupt lease on the compare path: leave it for
            # _read_lease's own fail-safe rejection rather than deleting
            # blind; missing file is simply done.
            pass

    # ── Flock-guarded mint-on-miss (nexus-nnr26) ────────────────────────────

    def _mint_guarded(self, base_url: str, tenant: str, credential: str) -> _CachedToken:
        """Non-blocking flock-guarded double-check-then-mint-or-borrow.

        Serializes concurrent cold-start racers for the SAME
        ``(base_url, tenant)`` so exactly one racer mints a fresh token
        while every other racer borrows the winner's published lease
        instead of independently minting a competing one — see the module
        docstring's "Concurrency, UPDATED (nexus-nnr26)" section for why
        this closes a genuine correctness gap (deterministic hard-fail of
        M>5 truly concurrent cold processes), not just an efficiency one.

        Mirrors :func:`nexus.db.t1._lock_guarded_mint_or_borrow`'s
        double-check-under-lock shape, but the WAIT is non-blocking
        poll-then-re-read rather than a blocking ``LOCK_EX`` acquire: a
        losing racer tries a non-blocking exclusive lock; on failure it
        re-reads the lease file (the holder will have published while
        still holding the lock) and returns the instant it appears,
        without ever itself blocking on the lock. Bounded by
        ``self._lock_wait_ceiling_seconds`` (measured against real
        ``time.monotonic()``, never the injectable ``self._clock`` —
        mirrors the t1 precedent's own deadline, nexus-by875): past the
        ceiling this fails loud with :class:`DataTokenMintError` rather
        than waiting indefinitely for a holder that never publishes (e.g.
        its own mint failed).

        Only ever called from :meth:`bearer_for`'s genuine-miss branch —
        an existing fresh lease is borrowed WITHOUT ever reaching this
        method, so the reuse happy path never touches the lock file.

        If the lock file itself cannot be created/opened (e.g. a
        read-only config dir), degrades to a direct unguarded mint —
        same best-effort stance as :meth:`_write_lease`: the lock is an
        optimization, the mint (which the credential otherwise allows)
        must never be blocked by it.
        """
        config_dir = self._resolve_config_dir()
        try:
            config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = _data_token_mint_lock_path(base_url, tenant, config_dir)
            lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        except OSError as exc:
            _log.warning(
                "data_token_mint_lock_unavailable", tenant=tenant, endpoint=_host(base_url), error=str(exc),
            )
            fresh = self._mint(base_url, tenant, credential)
            self._write_lease(base_url, tenant, fresh)
            _log.info(
                "data_token_minted", tenant=tenant, expires_in=fresh.ttl_seconds, endpoint=_host(base_url),
            )
            return fresh

        deadline = time.monotonic() + self._lock_wait_ceiling_seconds
        try:
            while True:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # lifecycle-gate-allow: non-blocking mint-on-miss mutex (nexus-nnr26), not a lifecycle election
                except BlockingIOError:
                    # A sibling holds the lock — check whether it has
                    # already published before waiting further.
                    leased = self._read_lease(base_url, tenant)
                    if leased is not None:
                        _log.info(
                            "data_token_lease_reused", tenant=tenant, endpoint=_host(base_url),
                        )
                        return leased
                    if time.monotonic() >= deadline:
                        raise DataTokenMintError(
                            f"data-token mint-on-miss lock wait exceeded "
                            f"{self._lock_wait_ceiling_seconds:.0f}s for "
                            f"{_host(base_url)} tenant={tenant!r} — a sibling "
                            "process holds the mint lock without publishing "
                            "a lease. This can mean the sibling's own mint "
                            "failed or is genuinely stuck, OR that the mint "
                            "endpoint is degraded (repeated "
                            "429/502/503/504) and the sibling is still "
                            "within its own legitimate retry budget "
                            f"(~{_MINT_RETRY_WALL_WORST_CASE_S:.0f}s worst "
                            "case), OR that a chain of siblings each burned "
                            "a retry wall in turn against a degraded "
                            "endpoint (this wait rides out one holder "
                            "handoff, then gives up); giving up rather than "
                            "waiting indefinitely — retry, or check the "
                            "mint endpoint's health if this recurs."
                        )
                    self._sleep(_MINT_LOCK_POLL_INTERVAL_S)
                    continue
                try:
                    # Double-check under the lock: a racer may have won
                    # and published between our last poll and acquiring
                    # the lock ourselves.
                    leased = self._read_lease(base_url, tenant)
                    if leased is not None:
                        _log.info(
                            "data_token_lease_reused", tenant=tenant, endpoint=_host(base_url),
                        )
                        return leased
                    fresh = self._mint(base_url, tenant, credential)
                    self._write_lease(base_url, tenant, fresh)
                    _log.info(
                        "data_token_minted", tenant=tenant, expires_in=fresh.ttl_seconds, endpoint=_host(base_url),
                    )
                    return fresh
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)  # lifecycle-gate-allow: release the mint-on-miss mutex (nexus-nnr26)
        finally:
            os.close(lock_fd)

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
