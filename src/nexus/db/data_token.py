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
(and again on a refresh-due-but-still-cached entry, nexus-dncjg),
:meth:`bearer_for` reads the lease file BEFORE minting; it is accepted
only when its format version, tenant, and base-url digest all match AND
it is not yet refresh-due for the reading manager: its remaining TTL
exceeds that manager's own refresh point, the 20% :data:`_REFRESH_THRESHOLD`
drawn with jitter (below), so an accepted lease is never due on the very
next call. (The read-only peeks, :meth:`has_fresh_lease` and
:meth:`fresh_lease_token`, keep the fixed 20% bar.) A refresh-due entry
adopts the lease only when it is a sibling's REMINT of the cached token:
a different token that expires later (the very token already cached is not
news, however a stepped wall clock makes its re-derived expiry look);
otherwise it mints under the same flock and lease re-check as the cold path
(:meth:`_mint_guarded`), so one refresher per host mints and its siblings
adopt the result. (Until nexus-dncjg a refresh minted directly, with no
lease read and no lock: processes that had borrowed one lease share its
wall-clock expiry, so they all became refresh-due in the same second, a
dozen minted at once past the engine's burst of 5, and every loser kept the
old cache entry and retried the mint forever instead of adopting the
winner's lease -- the 429 burst after a reboot.) A refresh that fails while
the cached token is still valid makes ONE mint attempt with no retry sleep
(the key lock and the mint flock are held throughout), returns the cached
token, and waits a jittered 15-45s (at least the server's ``Retry-After``,
never past the token's own expiry) before the next attempt; a 401 or 403
(revoked credential, wrong tenant) is logged at error level. Each manager
also draws its own refresh point once, 0.20 +/-
:data:`_REFRESH_THRESHOLD_JITTER`, so borrowers of one lease do not come
due together. Any other state (absent,
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
an advisory file lock.

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
NON-BLOCKING :func:`nexus._locking.lock_fd` around mint-on-miss ONLY —
mirroring the shipped :func:`nexus.db.t1._lock_guarded_mint_or_borrow`
precedent, but poll-then-re-read rather than block: a losing racer tries a
non-blocking
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
Lease reads stay lock-free: the guard wraps the mint paths only -- the
genuine cache-miss-with-no-fresh-lease path and, since nexus-dncjg, the
refresh-due path that found no newer lease (see
:meth:`DataTokenManager.bearer_for`). An existing fresh lease is still
borrowed without ever touching the lock file, so the reuse happy path
carries zero lock-contention cost. A refresh-due caller that still holds a
valid token never waits out a sibling's mint: with the lock busy it waits at
most :data:`_REFRESH_LOCK_WAIT_S` for the sibling's lease, then returns the
cached token and retries after the jittered window.

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

Scope boundary — HOST-LOCAL ONLY (nexus-b0svi): the advisory-lock guard
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

401 re-mint single-flight + futility (nexus-umue1, porting nexus-r0d37
defect 2's T1 guard to the shared manager): T1's ``HttpScratchStore`` fix
put both guards in PER-INSTANCE state (``self._remint_futile``,
single-flight on ``self._refresh_lock``) because T1 has exactly one
instance per session. T2 and T3 do not — nine T2 ``Http*Store`` instances
and T3's module-level client all resolve the SAME cached token per
``(base_url, tenant)`` key through this one manager, so the guards have to
live HERE to coordinate across them; per-instance state would just let
each store re-mint independently, reproducing the exact storm T1 fixed for
itself.

:meth:`invalidate_if_current` is the single-flight primitive: a caller
passes the ``Authorization`` bearer its OWN failed request actually sent,
and the cached entry for that key is popped ONLY when it still holds that
exact token — a caller whose bearer was already replaced by a winning
sibling (or whose key is currently marked futile, see below) retries on
the CURRENT token instead of invalidating it and minting a competing
replacement.

Futility EXPIRES (Sam's decision, nexus-umue1 review round 2, superseding
the original "lasts exactly as long as the condition" design): T1's 401 is
authored by the require-minted-session gate on a stale SESSION id, which a
data-token re-mint structurally can never fix — permanent-until-healthy
futility is correct there. T2/T3's 401 is measured (engine AuthFilter
trace, 2026-09-13) to be TOKEN-bound in the overwhelming case — the
non-token 401 reasons (``session_not_minted`` / ``cross_tenant_session``)
require an ``X-Nexus-Session`` header neither T2 nor T3 ever sends — so for
T2/T3 a re-mint is normally the correct remedy and a permanent futility
mark trades a bounded per-call mint-storm risk for an UNBOUNDED outage:
once ANY caller's re-mint+retry both 401 (e.g. a transient rejection
during a key rotation, immediately followed by the OLD cached token
becoming genuinely bad), every OTHER caller sharing the key was refused a
re-mint attempt until the cached token's own natural TTL-refresh point —
up to ~48 minutes at the 3600s default TTL's 20% :data:`_REFRESH_THRESHOLD`.
:meth:`mark_remint_futile` therefore records a MARK TIME (via the same
injectable, monotonic ``clock`` the TTL bookkeeping already uses — never
wall-clock ``time.time``), and :meth:`is_remint_futile` /
:meth:`invalidate_if_current` treat a mark older than
:data:`DEFAULT_REMINT_FUTILE_WINDOW_S` (default 60s, injectable via
``remint_futile_window_seconds`` — see :meth:`DataTokenManager.__init__`)
as NOT futile, dropping the stale mark rather than leaking it forever.
:meth:`clear_remint_futile` (called by every consumer on any non-401
response for the key) still lifts the mark immediately when the condition
resolves before the window elapses. Net bound, stated honestly rather than
claimed exact: a PERSISTENT misconfiguration (the bearer is genuinely,
durably invalid) costs roughly one wasted mint attempt per window per key
— not one per call, and not unbounded; a TRANSIENT rejection heals within
one window, never the old ~48-minute worst case. See
``nexus.db.t2._refreshable_client`` and ``nexus.db.http_vector_client``
for the two call sites.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import stat
import sys
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

from nexus import _locking
from nexus._log_safe_url import (
    endpoint_host_port as _endpoint_host_port,
    log_endpoint as _log_endpoint,
    scrub_transport_error,
)
from nexus._winsec import make_user_dir, open_private
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

#: Per-manager jitter on the refresh point (nexus-dncjg): each manager draws
#: its own threshold once, uniformly in ``_REFRESH_THRESHOLD +/- this``.
#: Borrowers of one lease share its wall-clock expiry, so with a common
#: threshold they all come due in the same second. A manager accepts a lease
#: on a cold read only when it is not refresh-due for ITS OWN draw (so an
#: accepted lease is never due on the very next call); the read-only peeks
#: (:meth:`DataTokenManager.has_fresh_lease`,
#: :meth:`DataTokenManager.fresh_lease_token`) keep the fixed
#: :data:`_REFRESH_THRESHOLD`.
_REFRESH_THRESHOLD_JITTER: float = 0.05

#: After a refresh mint fails (or is deferred to a sibling that holds the
#: mint lock) while the cached token is still valid, the next attempt waits a
#: uniform draw from this range, in seconds (nexus-dncjg), capped at the
#: cached token's remaining life (and raised to the server's ``Retry-After``
#: when the refusal carried a longer one). A lease a sibling publishes is
#: still adopted on every call; only the mint is held back.
_REFRESH_RETRY_WINDOW_S: tuple[float, float] = (15.0, 45.0)

#: Default window (Sam's decision, nexus-umue1 review round 2) a
#: :meth:`DataTokenManager.mark_remint_futile` mark stays in effect before
#: :meth:`DataTokenManager.is_remint_futile` / :meth:`invalidate_if_current`
#: treat it as expired and drop it -- see the module docstring's "Futility
#: EXPIRES" section for why an unbounded mark is wrong for T2/T3 (unlike
#: T1's structurally-permanent 401 cause). Measured against the injectable,
#: monotonic ``clock`` (never wall-clock ``time.time``), so a test can
#: control it exactly via a fake clock rather than a real sleep.
DEFAULT_REMINT_FUTILE_WINDOW_S: float = 60.0

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

#: Exact shape of a mint-lock file name: the prefix plus the FULL 64-hex
#: sha256 :func:`_lease_key` produces. The reaper (nexus-ndfc1) touches a
#: directory entry only on a full match, so a stray backup suffix, a short
#: hash or the sibling ``data_token_lease.*`` family can never be removed
#: by it.
_MINT_LOCK_NAME_RE: re.Pattern[str] = re.compile(re.escape(_MINT_LOCK_PREFIX) + r"[0-9a-f]{64}")

#: A mint lock unused for this long is reap-eligible (nexus-ndfc1). One lock
#: file exists per (host:port, tenant) ever minted against, so they
#: otherwise accumulate for ever (82 on one laptop). One day: safety does
#: NOT rest on the age. It rests on the reaper's non-blocking flock probe
#: and its inode-identity check under that lock, so a short age costs only
#: an occasional re-created file. A longer age also holds more files at a
#: steady state (about 430 at 7 days under a 61-new-key/day burst).
#: "Unused" is the file's mtime, which :func:`_open_mint_lock` refreshes on
#: every mint that reaches the lock (flock itself never changes it).
_MINT_LOCK_STALE_AFTER_S: float = 24 * 3600.0

#: Most files one mint may remove (nexus-ndfc1): the reap runs inline on
#: the mint path, so its cost is capped. A backlog drains over several
#: mints. At most ``4 * K`` held-or-failing candidates are tried per call.
_MINT_LOCK_REAP_MAX_PER_MINT: int = 8

#: Times :meth:`DataTokenManager._mint_guarded` reopens its lock file when,
#: once locked, the descriptor no longer names the file at the path (a reaper
#: unlinked it between our open and our lock). Past the bound the mint goes
#: ahead under the lock it holds, on a dead inode, so it can run concurrently
#: with a minter holding the new file. Reaching the bound takes five unlinks
#: of a file just created or touched, which the reaper's mtime-under-lock
#: check forbids; only an external delete or a filesystem mtime skewed by more
#: than a day gets there (measured: T2 nexus/confirm-ndfc1-74c739621-2026-10-17).
_MINT_LOCK_REOPEN_MAX: int = 5

#: How long a refresh whose cached token is still valid waits for a busy mint
#: lock before returning that token (nexus-dncjg). The holder is usually a
#: sibling's refresh mint (sub-second); a lease it publishes is adopted the
#: instant it appears, and a holder slower than this is retried after the
#: jittered retry window rather than waited out (the cold path's much longer
#: ceiling is for a caller with no token at all).
_REFRESH_LOCK_WAIT_S: float = 2.0

#: How long a lease publish that does not already hold the same-key lock waits
#: for it (nexus-fcwbd). A holder is a reaper (microseconds) or a cold minter
#: in its mint (seconds at worst); past this the publish is skipped, since the
#: lease is only an optimization for the NEXT cold process.
_LEASE_PUBLISH_LOCK_WAIT_S: float = 2.0

#: The Windows reap skip is logged once per process, not once per mint.
_reap_skip_logged: bool = False

#: Exact shape of a lease file name (nexus-4gmw8): the prefix plus the full
#: 64-hex :func:`_lease_key`. The lease reaper touches only a full match, so
#: the ``.<pid>.<uuid>.tmp`` publish temp files, a backup suffix or the
#: sibling ``data_token_mint_lock.*`` family are never candidates.
_LEASE_NAME_RE: re.Pattern[str] = re.compile(re.escape(_DATA_TOKEN_LEASE_PREFIX) + r"[0-9a-f]{64}")

#: A lease is reap-eligible once its ``expires_at`` is more than this many
#: seconds in the past (nexus-4gmw8). The reader already refuses a lease at
#: or below 20% of its TTL, so a lease past ``expires_at`` is dead to every
#: reader; the margin is only slack for wall-clock disagreement between
#: processes (a clock stepped back, a test's injected wall clock), so the
#: reaper never removes a lease some other process may still count as live.
_LEASE_EXPIRED_MARGIN_S: float = 300.0

#: Most leases one mint may remove, and the cap on how many candidates (oldest
#: write first) one call examines: ``4 * K``. A backlog drains over several
#: mints. Same bound and same reasoning as :data:`_MINT_LOCK_REAP_MAX_PER_MINT`.
_LEASE_REAP_MAX_PER_MINT: int = 8

#: A lease file larger than this is not a lease; it is read no further and
#: counts as unparseable.
_LEASE_READ_MAX_BYTES: int = 64 * 1024

#: Open flags that never follow a symlink and never block on a FIFO.
_OPEN_PLAIN_FILE: int = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)

#: Poll interval while a non-blocking flock acquire attempt is retried
#: (nexus-nnr26) -- mirrors nexus.db.t1._lock_guarded_mint_or_borrow's
#: bounded-poll variant's 0.05s interval exactly.
_MINT_LOCK_POLL_INTERVAL_S: float = 0.05

#: Backoff CEILING between attempts when the server supplies no Retry-After
#: header (design of record: "1s/2s"). Indexed by ``attempt - 1``. The sleep
#: is a full-jitter draw, ``uniform(0, value)`` (nexus-kcdbx), so clients
#: that failed together do not retry together; jitter only shortens a sleep,
#: so the worst-case bounds below are unchanged.
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
#: therefore never the binding case here, and the full-jitter draw of
#: nexus-kcdbx, ``uniform(0, value)``, only shortens them further):
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

    ``status`` is the HTTP status of the refusal (``None`` for a transport
    failure) and ``retry_after`` the server's ``Retry-After`` in seconds, when
    it sent one (nexus-dncjg): a refresh that fails while its cached token is
    still valid reads them to log a revoked or wrong-tenant credential loudly
    and to hold the next attempt back at least as long as the limiter asked.
    """

    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _fd_names_path(fd: int, path: str | Path) -> bool:
    """True when the open descriptor ``fd`` is the regular file ``path``
    names right now: same (st_dev, st_ino), and ``path`` is not a symlink.

    A lock only serializes holders of the SAME inode. A holder of an
    unlinked inode is alone, whatever the path now names (nexus-ndfc1
    review B1/B2), so both the reaper (before it unlinks) and the minter
    (once it holds the lock) ask this. False on any OSError (the path is
    gone).
    """
    try:
        held = os.fstat(fd)
        named = os.stat(str(path), follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(named.st_mode) and (named.st_dev, named.st_ino) == (held.st_dev, held.st_ino)


def _write_lock_record(fd: int, record: dict[str, Any]) -> None:
    """Write *record* as the one JSON first line of a lock file this process
    just created, then put the position back at 0. Best-effort: attribution
    never fails the caller."""
    try:
        os.write(fd, (json.dumps(record, sort_keys=True, default=str) + "\n").encode("utf-8"))
        # Windows msvcrt.locking locks from the CURRENT position: put it
        # back at 0 so every process's one-byte range is the same byte.
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        pass


def _open_mint_lock(
    lock_path: Path, describe: Callable[[], dict[str, Any]] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """Open (creating if absent) the mint lock file and mark it used.

    Returns ``(fd, record)``; ``record`` is not None only for the call that
    brought the file into being (``O_EXCL``), which is the one moment worth
    attributing (nexus-28bzt): it is ``describe()``'s dict, also written to
    the lock file as one JSON line so the stray file names its own writer
    (an info log is dropped in CLI mode). The mtime is refreshed on a REUSE,
    through the descriptor so the touch lands on the inode just opened and
    not on whatever the path names by then, so
    :func:`_reap_stale_mint_locks` measures time since last use, not since
    first creation; that touch is best-effort and never fails the mint.
    """
    flags = os.O_WRONLY | os.O_CREAT
    mode = 0o600
    try:
        fd = os.open(str(lock_path), flags | os.O_EXCL, mode)
    except FileExistsError:
        fd = os.open(str(lock_path), flags, mode)
    else:
        try:
            record = describe() if describe is not None else {}
        except Exception:  # noqa: BLE001 -- attribution must never fail a mint
            record = {}
        _write_lock_record(fd, record)
        return fd, record
    try:
        os.utime(fd)
    except (OSError, NotImplementedError, TypeError):
        pass
    return fd, None


def _home_matches_pwd_home() -> bool | None:
    """Whether ``$HOME`` is the account's real home (pwd database), or None
    when that cannot be asked (Windows, or a lookup failure)."""
    try:
        import pwd  # noqa: PLC0415 -- POSIX-only; its ImportError arm IS the non-POSIX branch

        real = pwd.getpwuid(os.getuid()).pw_dir
        home = os.environ.get("HOME")
        if not home:
            return False
        return os.path.realpath(home) == os.path.realpath(real)
    except Exception:  # noqa: BLE001 -- attribution must never fail a mint
        return None


def _parent_process_name() -> str | None:
    try:
        from nexus.session import _command_name_of  # noqa: PLC0415 -- paid only on a first lock creation

        return _command_name_of(os.getppid()) or None
    except Exception:  # noqa: BLE001 -- attribution must never fail a mint
        return None


def _creator_fields(config_dir: Path, *, parent_name: bool = True) -> dict[str, Any]:
    """The process-level fields every lock creation record carries.
    ``parent_name=False`` leaves the name ``None`` instead of running ``ps``
    (:func:`_parent_process_name`), for a caller on a hot path."""
    try:
        cwd: str | None = os.getcwd()
    except OSError:
        cwd = None
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "parent_name": _parent_process_name() if parent_name else None,
        "argv0": sys.argv[0] if sys.argv else None,
        "pytest_current_test": os.environ.get("PYTEST_CURRENT_TEST"),
        "nexus_config_dir": os.environ.get("NEXUS_CONFIG_DIR"),
        "config_dir": str(config_dir),
        "cwd": cwd,
        "home_matches_pwd_home": _home_matches_pwd_home(),
    }


def _mint_lock_creation_record(
    base_url: str, tenant: str, lock_path: Path, config_dir: Path,
) -> dict[str, Any]:
    """Who is creating this lock file (nexus-28bzt). Written to the file and
    logged. Never a token or credential: the endpoint is host:port only and
    nothing here reads a request, header or lease."""
    return {
        **_creator_fields(config_dir),
        "endpoint": _endpoint_host_port(base_url),
        "tenant": tenant,
        "lock_file": lock_path.name,
    }


def _reap_lock_creation_record(lock_path: Path, lease_path: Path | None) -> dict[str, Any]:
    """The same attribution for a lock the lease reaper creates (nexus-fcwbd):
    the reaper knows the lease file, not the endpoint or tenant, which are
    hashed into the key. ``created_by`` is what a stray file's reader asks."""
    return {
        # No ps spawn: the reaper runs on every cold mint (pid and ppid name the parent).
        **_creator_fields(lock_path.parent, parent_name=False),
        "created_by": "lease-reaper",
        "lock_file": lock_path.name,
        "lease_file": lease_path.name if lease_path is not None else None,
    }


def _open_mint_lock_logged(
    base_url: str, tenant: str, lock_path: Path, config_dir: Path,
) -> int:
    """:func:`_open_mint_lock`, plus the one ``data_token_mint_lock_created``
    info event (same fields as the line in the file) when this call created
    the file. Raises OSError like the open."""
    fd, record = _open_mint_lock(
        lock_path, lambda: _mint_lock_creation_record(base_url, tenant, lock_path, config_dir),
    )
    if record is not None:
        try:
            _log.info("data_token_mint_lock_created", **record)
        except Exception:  # noqa: BLE001 -- attribution must never fail a mint
            pass
    return fd


def _reap_stale_mint_locks(
    config_dir: Path,
    *,
    keep: Path | None = None,
    now: float | None = None,
    max_removals: int = _MINT_LOCK_REAP_MAX_PER_MINT,
) -> int:
    """Remove mint-lock files unused for :data:`_MINT_LOCK_STALE_AFTER_S`
    and not currently held (nexus-ndfc1). Returns the number removed.

    Called inline at mint time, never from a background thread. Never
    raises: every failure is a ``debug`` log and the file is left alone.

    A file is touched only when its name matches
    :data:`_MINT_LOCK_NAME_RE` exactly and it is a regular file, never
    ``keep`` (the caller's own lock). "Not held" is established with the
    module's own lock primitive: open without creating (no symlink follow,
    non-blocking so a name swapped to a FIFO cannot hang the mint), require a
    regular file, take a NON-BLOCKING exclusive lock, and skip the file on
    contention. Under the lock the mtime is re-read from the descriptor and
    the path must still name THIS inode (:func:`_fd_names_path`): another
    reaper may have unlinked it and a minter re-created the path since we
    opened, and unlinking by path would then remove the minter's live lock.
    The unlink happens while the lock is held.

    A minter that opened the file just before the unlink sees, once it holds
    the lock, that its descriptor no longer names the path, and reopens
    (:meth:`DataTokenManager._mint_guarded`).

    Skipped on Windows: an open handle blocks the delete, so every attempt
    would fail; the scan is not run (one debug log per process).

    The sibling ``data_token_lease.*`` family is reaped by
    :func:`_reap_expired_leases` (nexus-4gmw8), not here.
    """
    global _reap_skip_logged
    if sys.platform == "win32":
        if not _reap_skip_logged:
            _reap_skip_logged = True
            _log.debug("data_token_mint_lock_reap_skipped", reason="win32: an open handle blocks unlink")
        return 0
    removed = 0
    try:
        cutoff = (time.time() if now is None else now) - _MINT_LOCK_STALE_AFTER_S
        candidates: list[Path] = []
        with os.scandir(config_dir) as it:
            for entry in it:
                if not _MINT_LOCK_NAME_RE.fullmatch(entry.name):
                    continue
                if keep is not None and entry.name == keep.name:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                        continue
                except OSError:
                    continue
                candidates.append(Path(entry.path))
    except Exception as exc:  # noqa: BLE001 -- the reap must never reach the mint
        _log.debug("data_token_mint_lock_reap_scan_failed", error=str(exc))
        return 0

    open_flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    for path in candidates[: max_removals * 4]:
        if removed >= max_removals:
            break
        fd: int | None = None
        try:
            fd = os.open(str(path), open_flags)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                continue  # swapped to a FIFO/device since the scan
            try:
                _locking.lock_fd(fd, blocking=False)  # lifecycle-gate-allow: non-blocking held-probe for the stale mint-lock reaper (nexus-ndfc1), not a lifecycle election
            except BlockingIOError:
                continue  # held by a live process: never reaped
            try:
                if os.fstat(fd).st_mtime >= cutoff:
                    continue  # touched since the scan
                if not _fd_names_path(fd, path):
                    continue  # the path is another file now: not ours to unlink
                os.unlink(str(path))
                removed += 1
            finally:
                _locking.unlock_fd(fd)  # lifecycle-gate-allow: release the reaper's held-probe lock (nexus-ndfc1)
        except Exception as exc:  # noqa: BLE001 -- one bad file must not stop the rest or the mint
            _log.debug("data_token_mint_lock_reap_failed", file=path.name, error=str(exc))
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
    if removed:
        _log.debug("data_token_mint_lock_reaped", removed=removed)
    return removed


def _open_plain_file(path: str | Path, flags: int) -> int:
    """Open *path* without following a symlink or blocking on a FIFO, and
    require a regular file. Raises OSError otherwise (the fd is closed)."""
    fd = os.open(str(path), flags | _OPEN_PLAIN_FILE)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{Path(path).name} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _lease_is_dead(raw: bytes, mtime: float, now: float) -> bool:
    """Whether a lease file's content says it is past use (nexus-4gmw8).

    Read the way :meth:`DataTokenManager._read_lease` reads it: JSON with the
    current ``format_version`` and a numeric ``expires_at`` in wall-clock
    seconds. Dead means ``expires_at`` is more than
    :data:`_LEASE_EXPIRED_MARGIN_S` before *now*. A file that cannot be read
    that way (not JSON, another format version, no or non-finite
    ``expires_at``, oversize) is not known to be dead: it counts as dead only
    when its own mtime is also older than :data:`_MINT_LOCK_STALE_AFTER_S`.
    """
    try:
        if len(raw) > _LEASE_READ_MAX_BYTES:
            raise ValueError("oversize")
        data = json.loads(raw)
        if data.get("format_version") != _LEASE_FORMAT_VERSION:
            raise ValueError("format version")
        expires_at = float(data["expires_at"])
        if not math.isfinite(expires_at):
            raise ValueError("expires_at")
    except (ValueError, KeyError, TypeError, AttributeError):  # JSON and unicode decode errors are ValueErrors
        return mtime < now - _MINT_LOCK_STALE_AFTER_S
    return expires_at < now - _LEASE_EXPIRED_MARGIN_S


def _open_reap_lock(lock_path: Path, *, lease_path: Path | None = None) -> tuple[int, bool]:
    """Open the same-key mint lock for the lease reaper: ``(fd, created)``.

    An existing lock is opened as it is (no mtime touch: that would keep a
    stale lock alive for ever). A missing one is created, because a lease can
    outlive its lock (the mint-lock reaper removes locks after a day); the
    caller removes a file it created. A file it created carries a first-line
    JSON record naming ``created_by: lease-reaper`` (nexus-fcwbd), so one left
    behind by a SIGKILL between create and unlink, or kept by a minter that
    won the race, names its creator like every other lock (nexus-ndfc1).
    """
    try:
        return _open_plain_file(lock_path, os.O_WRONLY), False
    except FileNotFoundError:
        pass
    try:
        fd = open_private(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _OPEN_PLAIN_FILE)
    except FileExistsError:
        return _open_plain_file(lock_path, os.O_WRONLY), False
    try:
        record = _reap_lock_creation_record(lock_path, lease_path)
    except Exception:  # noqa: BLE001 -- attribution must never fail a reap
        record = {"created_by": "lease-reaper"}
    _write_lock_record(fd, record)
    return fd, True


def _reap_one_lease(lease_path: Path, config_dir: Path, now: float) -> bool:
    """Remove *lease_path* if it is dead, under its same-key mint lock.
    True when removed. May raise OSError; the caller logs it."""
    fd = _open_plain_file(lease_path, os.O_RDONLY)
    try:
        raw, mtime = os.read(fd, _LEASE_READ_MAX_BYTES + 1), os.fstat(fd).st_mtime
    finally:
        os.close(fd)
    if not _lease_is_dead(raw, mtime, now):
        return False  # the common case: a live lease costs one read

    lock_path = config_dir / (_MINT_LOCK_PREFIX + lease_path.name[len(_DATA_TOKEN_LEASE_PREFIX):])
    lock_fd, created = _open_reap_lock(lock_path, lease_path=lease_path)
    try:
        try:
            _locking.lock_fd(lock_fd, blocking=False)  # lifecycle-gate-allow: non-blocking same-key probe for the expired-lease reaper (nexus-4gmw8), not a lifecycle election
        except BlockingIOError:
            return False  # a minter holds it: it may be about to write this very lease
        try:
            # Same discipline as the mint-lock reaper: the lock only means
            # something if it is the file the path names now.
            if not _fd_names_path(lock_fd, lock_path):
                return False
            lease_fd = _open_plain_file(lease_path, os.O_RDONLY)
            try:
                # Re-read under the lock: every publish happens while its
                # publisher holds this lock (the guarded mint, cold or
                # refresh, and the unguarded mint, nexus-fcwbd, nexus-dncjg), so a lease replaced
                # after this read cannot be the one the unlink below removes.
                raw, mtime = os.read(lease_fd, _LEASE_READ_MAX_BYTES + 1), os.fstat(lease_fd).st_mtime
                if not _lease_is_dead(raw, mtime, now) or not _fd_names_path(lease_fd, lease_path):
                    return False
                os.unlink(str(lease_path))
            finally:
                os.close(lease_fd)
            return True
        finally:
            try:
                if created and _fd_names_path(lock_fd, lock_path):
                    os.unlink(str(lock_path))  # a lock made only for this reap; a minter that opened it reopens
            finally:
                _locking.unlock_fd(lock_fd)  # lifecycle-gate-allow: release the expired-lease reaper's same-key lock (nexus-4gmw8)
    finally:
        os.close(lock_fd)


def _reap_expired_leases(
    config_dir: Path,
    *,
    now: float,
    keep: Path | None = None,
    max_removals: int = _LEASE_REAP_MAX_PER_MINT,
) -> int:
    """Remove expired ``data_token_lease.*`` files (nexus-4gmw8). Returns the
    number removed.

    One lease is written per (host:port, tenant) and nothing removed a lease
    for an endpoint that never came back, so expired 0600 token files piled up
    beside the mint locks. Called inline at mint time, after the mint-lock
    reap, never from a background thread; never raises (every failure is a
    ``debug`` log and the file is left alone).

    Guards, the same as the mint-lock reaper's: an exact name match
    (:data:`_LEASE_NAME_RE`), a regular file only (opened without following a
    symlink and without blocking), never ``keep`` (the caller's own lease),
    skipped on Windows (an open handle blocks the delete), at most
    *max_removals* per call and ``4 * max_removals`` examined, oldest write
    first so live leases cannot starve the dead ones.

    A lease is removed only while the SAME-KEY mint lock
    (``data_token_mint_lock.<same 64 hex>``) is held non-blocking by the
    reaper. Every publish of a lease happens under that lock (nexus-fcwbd):
    the guarded mint (cold, and since nexus-dncjg refresh) holds it for the
    whole mint, and the unguarded mint takes it for the publish
    (:meth:`DataTokenManager._publish_lease_locked`). So a held lock means a
    publisher may be mid-write and the lease is skipped. The one unlocked
    write is the fallback when the lock file cannot be opened or locked at all
    (a lease is only an optimization, and a directory that refuses the lock
    usually refuses the lease). Under the lock the lease is read again, and the
    path must still name the inode that was read (:func:`_fd_names_path`)
    before it is unlinked, which also covers a fallback publish that lands in
    between (one extra mint by a sibling cold process, never an error). *now*
    is the wall clock the lease reader uses (``expires_at`` is wall-clock
    seconds). See :func:`_lease_is_dead` for what dead means.
    """
    if sys.platform == "win32":
        return 0
    removed = 0
    try:
        candidates: list[tuple[float, Path]] = []
        with os.scandir(config_dir) as it:
            for entry in it:
                if not _LEASE_NAME_RE.fullmatch(entry.name):
                    continue
                if keep is not None and entry.name == keep.name:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    candidates.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
                except OSError:
                    continue
        candidates.sort(key=lambda c: c[0])
    except Exception as exc:  # noqa: BLE001 -- the reap must never reach the mint
        _log.debug("data_token_lease_reap_scan_failed", error=str(exc))
        return 0

    for _, lease_path in candidates[: max_removals * 4]:
        if removed >= max_removals:
            break
        try:
            if _reap_one_lease(lease_path, config_dir, now):
                removed += 1
        except Exception as exc:  # noqa: BLE001 -- one bad file must not stop the rest or the mint
            _log.debug("data_token_lease_reap_failed", file=lease_path.name, error=str(exc))
    if removed:
        _log.debug("data_token_lease_reaped", removed=removed)
    return removed


def _host(url: str) -> str:
    """The whole netloc of *url*: ``[user[:password]@]host[:port]``.

    This is the HASH INPUT of :func:`_lease_key`, and nothing else. It is
    deliberately left as it was so no existing ``data_token_lease.*`` or
    ``data_token_mint_lock.*`` file is renamed (changing it would orphan every
    lease and lock on disk). It can carry userinfo, so it must never reach a
    log event, an exception message or a file body: use :func:`_log_endpoint`
    for those (nexus-q26w1).
    """
    try:
        netloc = urllib.parse.urlsplit(url).netloc
        return netloc or url
    except Exception:  # noqa: BLE001 — a key-derivation helper must never raise
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
        remint_futile_window_seconds: How long a
            :meth:`mark_remint_futile` mark stays in effect before
            :meth:`is_remint_futile` / :meth:`invalidate_if_current` treat
            it as expired (Sam's decision, nexus-umue1 review round 2 --
            see the module docstring's "Futility EXPIRES" section).
            Defaults to :data:`DEFAULT_REMINT_FUTILE_WINDOW_S` (60s).
            Measured against the injectable ``clock`` above (monotonic,
            never wall-clock) -- injectable here so a test controls the
            window via a fake clock rather than a real sleep.
        rng: Source of every random draw (nexus-dncjg, nexus-kcdbx): this
            manager's refresh point (``_REFRESH_THRESHOLD`` +/-
            ``_REFRESH_THRESHOLD_JITTER``, drawn once at construction), the
            full-jitter mint backoff, and the refresh retry window. Defaults
            to an unseeded ``random.Random()``; tests inject a seeded one.
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
        remint_futile_window_seconds: float = DEFAULT_REMINT_FUTILE_WINDOW_S,
        rng: random.Random | None = None,
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
        self._remint_futile_window_seconds = remint_futile_window_seconds
        # nexus-dncjg / nexus-kcdbx: every random draw (the refresh point, the
        # mint backoff, the refresh retry window) goes through this one RNG.
        # Unseeded by default so sibling processes diverge; a test injects a
        # seeded one.
        self._rng = rng if rng is not None else random.Random()
        self._refresh_threshold = _REFRESH_THRESHOLD + self._rng.uniform(
            -_REFRESH_THRESHOLD_JITTER, _REFRESH_THRESHOLD_JITTER,
        )
        # Per-key monotonic time before which a refresh mint is not retried
        # (nexus-dncjg). Guarded by the same per-key lock as ``_cache``.
        self._refresh_retry_at: dict[tuple[str, str], float] = {}
        # nexus-7qz06: sharded per-(base_url, tenant) locks rather than one
        # process-wide lock — see _lock_for. _registry_lock guards only the
        # dict's own get-or-create, never the check-then-mint sequence a
        # per-key lock protects.
        self._registry_lock = threading.Lock()
        self._key_locks: dict[tuple[str, str], threading.Lock] = {}
        self._cache: dict[tuple[str, str], _CachedToken] = {}
        # nexus-umue1 (porting nexus-r0d37 defect 2, WINDOWED per Sam's
        # review-round-2 decision): per-key futility MARK TIME for the 401
        # re-mint guard -- see the module docstring's "Futility EXPIRES"
        # section. Guarded by the SAME per-key lock as ``_cache``
        # (``_lock_for``), never a separate lock. Stores the ``clock()``
        # value at mark time, not a bare bool -- :meth:`_is_futile_locked`
        # is what turns this into a bounded yes/no.
        self._futile_marked_at: dict[tuple[str, str], float] = {}

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
        (nexus-nnr26)" section. A refresh-due entry takes the same road
        (nexus-dncjg, :meth:`_refresh_locked`): it adopts a sibling's newer
        lease, or mints under the same flock; a failed refresh with the cached
        token still valid returns that token, and only a failure once it has
        expired raises.
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
                # nexus-9c7t9: a genuine cache MISS tries the cross-process
                # lease file before minting (a refresh-due entry reads it too,
                # in _refresh_locked -- see the module docstring's
                # "Cross-process lease-file cache" section).
                leased = self._read_lease(base_url, tenant, near_expiry_threshold=self._refresh_threshold)
                if leased is not None:
                    self._cache[key] = leased
                    _log.info(
                        "data_token_lease_reused", tenant=tenant, endpoint=_log_endpoint(base_url),
                    )
                    return leased.token
                # nexus-nnr26: genuine miss AND no fresh lease — serialize
                # concurrent cold-start racers via a non-blocking flock so
                # only the lock holder mints; every other racer borrows
                # instead of independently minting a competing token. The
                # lease-hit branch above is lock-free (a read); the
                # refresh-due branch (_refresh_locked, nexus-dncjg) reads the
                # lease too and mints through the same guard.
                fresh = self._mint_guarded(base_url, tenant, credential)
                self._cache[key] = fresh
                return fresh.token
            assert cached is not None
            return self._refresh_locked(key, base_url, tenant, credential, cached)

    def _refresh_locked(
        self, key: tuple[str, str], base_url: str, tenant: str, credential: str, cached: _CachedToken,
    ) -> str:
        """The refresh-due branch of :meth:`bearer_for` (nexus-dncjg); the
        caller holds the per-key lock.

        1. A sibling's remint of the cached token (a lease with a different
           token that expires later) is adopted -- read on EVERY call, even inside the retry window, so a
           process whose own mint keeps failing still converges on the
           winner's token.
        2. Inside the retry window, with the cached token still valid, the
           cached token is returned and nothing is minted.
        3. Otherwise mint under the cold path's flock and lease re-check
           (:meth:`_mint_guarded`): one refresher per host mints. When the
           lock is busy and the cached token is still valid the cached token
           is returned after at most :data:`_REFRESH_LOCK_WAIT_S` (a
           sibling's lease published in that wait is adopted instead),
           with the retry window set.
        4. A failed mint with the cached token still valid returns it and
           records a jittered next-attempt time, raised to the server's
           ``Retry-After``. That mint is ONE attempt with no retry sleep
           (:meth:`_mint_guarded`); once the cached token has expired the
           mint has its full retry budget and a failure raises
           :class:`DataTokenMintError`. A 401/403 refusal is logged at error
           level.
        """
        leased = self._read_lease_newer(base_url, tenant, cached)
        if leased is not None:
            self._cache[key] = leased
            self._refresh_retry_at.pop(key, None)
            _log.info("data_token_lease_reused", tenant=tenant, endpoint=_log_endpoint(base_url))
            return leased.token
        now = self._clock()
        still_valid = cached.expires_at > now
        if still_valid and now < self._refresh_retry_at.get(key, float("-inf")):
            return cached.token
        try:
            fresh = self._mint_guarded(base_url, tenant, credential, refresh_of=cached)
        except DataTokenMintError as exc:
            if cached.expires_at <= self._clock():
                raise
            self._defer_refresh(key, cached, retry_after=exc.retry_after)
            # A 401/403 is a revoked credential or a wrong tenant, not a
            # busy limiter: the cached token will run out and nothing will
            # mint another, so say so at error level (the token is still
            # valid, so it is still returned).
            log = _log.error if exc.status in (401, 403) else _log.warning
            log(
                "data_token_refresh_failed_keeping_cached", tenant=tenant,
                endpoint=_log_endpoint(base_url), status=exc.status, error=str(exc),
            )
            return cached.token
        if fresh is cached:
            # The mint lock is held by a sibling that has not published yet.
            self._defer_refresh(key, cached)
            return cached.token
        self._cache[key] = fresh
        _log.info(
            "data_token_refresh", tenant=tenant, expires_in=fresh.ttl_seconds, endpoint=_log_endpoint(base_url),
        )
        return fresh.token

    def _defer_refresh(
        self, key: tuple[str, str], cached: _CachedToken, *, retry_after: float | None = None,
    ) -> None:
        """Hold the next refresh mint back by a jittered window (nexus-dncjg),
        at least as long as the server's ``Retry-After`` when it sent one, and
        never past the cached token's own expiry. (So a mint that succeeds
        later, at or after the window's end or once the token has expired,
        always finds the window already past; only a replaced token, by
        :meth:`invalidate` or by adopting a sibling's lease, must clear it.)"""
        now = self._clock()
        window = self._rng.uniform(*_REFRESH_RETRY_WINDOW_S)
        if retry_after is not None:
            window = max(window, retry_after)
        self._refresh_retry_at[key] = now + max(0.0, min(window, cached.expires_at - now))

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
            self._refresh_retry_at.pop(key, None)
            if popped is not None:
                # nexus-vj72t: the lease delete stays INSIDE the key lock.
                # bearer_for reads the lease on a cache miss under this same
                # lock, so a pop that released the lock before the delete let
                # a concurrent caller re-cache the very token being
                # invalidated from the file it was about to lose.
                self._delete_lease(base_url, tenant, expected_token=popped.token)
        if popped is not None:
            _log.info("data_token_invalidated", tenant=tenant, endpoint=_log_endpoint(base_url))

    def _is_futile_locked(self, key: tuple[str, str]) -> bool:
        """The windowed futility check (Sam's decision, nexus-umue1 review
        round 2 — see the module docstring's "Futility EXPIRES" section).

        MUST be called with ``self._lock_for(key)`` already held — this is
        a private helper, never a public entry point of its own. A mark
        younger than :attr:`_remint_futile_window_seconds` is futile; an
        absent mark is not futile; a mark AT OR PAST the window is dropped
        (the dict entry is deleted, not merely ignored) and reported as
        not futile — a stale mark must never leak forever, and dropping it
        here means the NEXT call, not just this one, sees a clean slate.
        """
        marked_at = self._futile_marked_at.get(key)
        if marked_at is None:
            return False
        if self._clock() - marked_at >= self._remint_futile_window_seconds:
            del self._futile_marked_at[key]
            return False
        return True

    def invalidate_if_current(self, base_url: str, tenant: str, sent_token: str) -> bool:
        """Single-flighted, futility-aware compare-and-invalidate for the
        401 self-heal path (nexus-umue1, porting nexus-r0d37 defect 2).

        *sent_token* is the RAW data-token string (never the
        ``"Bearer <token>"``-prefixed header value — the cache stores raw
        tokens, exactly what :meth:`bearer_for` itself returns) the
        caller's OWN failed request carried. A caller holding only the
        full ``Authorization`` header value must strip the ``"Bearer "``
        prefix before calling this method. Returns ``True`` only when
        THIS call actually popped the cache entry — the caller should
        then call :meth:`bearer_for` to mint a fresh one and retry.
        Returns ``False`` (never invalidates) when either:

        - the key is currently marked futile AND STILL WITHIN THE WINDOW
          (:meth:`is_remint_futile`, bounded per the module docstring's
          "Futility EXPIRES" section) — a prior re-mint already healed the
          bearer and the retry still 401'd, so minting again within the
          window is unlikely to help; past the window the mark is dropped
          and a fresh re-mint attempt is allowed again — or
        - the in-process cache no longer holds *sent_token* — a sibling
          caller (another T2 store instance, or T3) already invalidated
          and re-minted while this caller was in flight, so retrying on
          the winner's fresh token is correct and minting a second,
          competing replacement is not.

        Callers retry EITHER way — the retry simply resends whatever
        :meth:`bearer_for` currently returns, which is a fresh mint when
        this call returned ``True`` and the unchanged current token
        otherwise (see the module docstring). The whole check-then-pop
        sequence (futility peek + cache compare + pop + lease delete,
        nexus-vj72t) is atomic under
        this key's :meth:`_lock_for` lock — the same lock
        :meth:`bearer_for` and :meth:`invalidate` use — so a concurrent
        winner's ``bearer_for`` mint-on-miss and a loser's
        ``invalidate_if_current`` can never interleave to both invalidate.
        """
        key = (base_url.rstrip("/"), tenant)
        with self._lock_for(key):
            if self._is_futile_locked(key):
                return False
            cached = self._cache.get(key)
            if cached is None or cached.token != sent_token:
                return False
            popped = self._cache.pop(key)
            # nexus-vj72t: delete the lease before the lock is released, so
            # a loser's bearer_for cannot take the lock between the pop and
            # the delete, miss the cache, and borrow the token this call just
            # invalidated back out of the lease file (see invalidate).
            self._delete_lease(base_url, tenant, expected_token=popped.token)
        _log.info("data_token_invalidated", tenant=tenant, endpoint=_log_endpoint(base_url))
        return True

    def mark_remint_futile(self, base_url: str, tenant: str) -> None:
        """Mark ``(base_url, tenant)`` as futile-to-remint (nexus-umue1),
        starting a :attr:`_remint_futile_window_seconds`-long window.

        Called by a consumer after :meth:`invalidate_if_current` returned
        ``True`` (this call actually re-minted) and the retry on the fresh
        token STILL 401'd — evidence (not proof — see the module
        docstring's "Futility EXPIRES" section) that the bearer was not
        the cause. Every other caller sharing this key skips its own
        re-mint attempt (see :meth:`invalidate_if_current`) until either
        :meth:`clear_remint_futile` (a non-401 response) lifts it early,
        or the window elapses and the NEXT call is allowed to try again.
        Re-marking (calling this again while already futile) resets the
        window's start time to now — a fresh re-mint attempt that ALSO
        401s is fresh evidence, not a continuation of the old one.
        """
        key = (base_url.rstrip("/"), tenant)
        with self._lock_for(key):
            self._futile_marked_at[key] = self._clock()

    def clear_remint_futile(self, base_url: str, tenant: str) -> None:
        """Clear the futility mark for ``(base_url, tenant)`` (nexus-umue1).

        Called by a consumer on ANY non-401 response for this key (a 404
        counts — the bearer and tenant both resolved, the endpoint merely
        had nothing there) so a resolved condition lifts the skip
        IMMEDIATELY rather than waiting out the rest of the window. A
        no-op when the key was never marked futile (or its mark had
        already expired).
        """
        key = (base_url.rstrip("/"), tenant)
        with self._lock_for(key):
            self._futile_marked_at.pop(key, None)

    def is_remint_futile(self, base_url: str, tenant: str) -> bool:
        """True when ``(base_url, tenant)`` is currently marked futile-to-
        remint AND the mark is still within its window (nexus-umue1) — a
        PEEK, mints nothing; DOES drop an expired mark as a side effect
        (see :meth:`_is_futile_locked`), which is itself side-effect-free
        from every caller's observable perspective (an expired mark was
        never going to report ``True`` again regardless)."""
        key = (base_url.rstrip("/"), tenant)
        with self._lock_for(key):
            return self._is_futile_locked(key)

    def has_fresh_lease(self, base_url: str, tenant: str) -> bool:
        """True when a fresh (not due-for-refresh) cross-process lease-file
        entry exists for ``(base_url, tenant)`` — a PEEK, never mints,
        never populates the in-process cache (nexus-9c7t9).

        Distinct from :meth:`has_live_token`, which only checks the
        in-process dict. Lets a caller (the ``nx doctor`` check) report
        "reused (lease file)" separately from "reused (in-process)" and
        "minted a fresh".
        """
        return self._read_lease(base_url, tenant, near_expiry_threshold=_REFRESH_THRESHOLD) is not None

    def fresh_lease_token(
        self, base_url: str, tenant: str, *, near_expiry_threshold: float = _REFRESH_THRESHOLD,
    ) -> str | None:
        """The raw bearer from a fresh cross-process lease-file entry for
        ``(base_url, tenant)``, or ``None`` — a PEEK, never mints, never
        populates the in-process cache (nexus-9c7t9), and never reads the
        in-process cache either (unlike :meth:`bearer_for`).

        For a caller that must present a data-token bearer WITHOUT ever
        risking a mint call — a fire-and-forget hook with no reader and no retry whose credential
        policy refuses to mint under any circumstance. :meth:`has_fresh_lease` answers the same question as
        a bool; this returns the token itself so such a caller need not
        re-read the lease file a second time or duplicate this method's
        validation (format version, tenant, digest, near-expiry threshold
        — see :meth:`_read_lease`).

        *near_expiry_threshold* is the fraction of the granted TTL below
        which a lease counts as absent. The default is the client's own
        refresh threshold, right for a caller that would otherwise mint. A
        read-only caller with a static-token fallback passes ``0.0`` to
        accept any lease not yet expired: the mailbox drain and the routing
        telemetry did so as plugin scripts, and nexus-t9klx's ports kept
        that (nexus-t9klx review).
        """
        cached = self._read_lease(base_url, tenant, near_expiry_threshold=near_expiry_threshold)
        return cached.token if cached is not None else None

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
        return remaining <= cached.ttl_seconds * self._refresh_threshold

    # ── Cross-process lease file (nexus-9c7t9) ──────────────────────────────

    def _read_lease(
        self, base_url: str, tenant: str, *, near_expiry_threshold: float = _REFRESH_THRESHOLD,
    ) -> _CachedToken | None:
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
            raw = path.read_text(encoding="utf-8")
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
            _log.debug("data_token_lease_unparseable", tenant=tenant, endpoint=_log_endpoint(base_url))
            return None
        if not token:
            return None
        remaining = expires_at_wall - self._wall_clock()
        if remaining <= ttl_seconds * near_expiry_threshold:
            # Expired, or too close to expiry to be worth borrowing (at or
            # below the caller's refresh point; bearer_for passes its own
            # jittered draw, the peeks the fixed 20%) — a caller that
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
            make_user_dir(config_dir)
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
            fd = open_private(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            os.replace(str(tmp), str(path))
        except OSError as exc:
            _log.warning(
                "data_token_lease_write_failed", tenant=tenant, endpoint=_log_endpoint(base_url), error=str(exc),
            )

    def _delete_lease(
        self, base_url: str, tenant: str, *, expected_token: str | None = None,
    ) -> None:
        """Remove the cross-process lease file, best-effort/idempotent
        (mirrors :func:`nexus.db.t1.end_owned_t1_session_lease`).

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
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("token") != expected_token:
                    return
            path.unlink()
        except (OSError, ValueError):
            # Unreadable/corrupt lease on the compare path: leave it for
            # _read_lease's own fail-safe rejection rather than deleting
            # blind; missing file is simply done.
            pass

    # ── Flock-guarded mint-on-miss (nexus-nnr26) ────────────────────────────

    def _mint_guarded(
        self, base_url: str, tenant: str, credential: str, *, refresh_of: _CachedToken | None = None,
    ) -> _CachedToken:
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

        Called from :meth:`bearer_for`'s genuine-miss branch and, with
        ``refresh_of`` set (nexus-dncjg), from the refresh-due branch. A
        refresh accepts a lease only when it expires later than
        ``refresh_of`` (a lease that is the very token being refreshed is no
        help), and when the lock is busy and ``refresh_of`` is still valid it
        returns ``refresh_of`` itself once :data:`_REFRESH_LOCK_WAIT_S` has passed,
        so a request thread holding a usable token never waits out a
        sibling's whole mint. An existing fresh
        lease is borrowed WITHOUT ever reaching this method, so the reuse
        happy path never touches the lock file.

        If the lock file itself cannot be created/opened (e.g. a
        read-only config dir), degrades to a direct unguarded mint —
        same best-effort stance as :meth:`_write_lease`: the lock is an
        optimization, the mint (which the credential otherwise allows)
        must never be blocked by it.
        """
        config_dir = self._resolve_config_dir()
        try:
            make_user_dir(config_dir)
            lock_path = _data_token_mint_lock_path(base_url, tenant, config_dir)
            lock_fd = _open_mint_lock_logged(base_url, tenant, lock_path, config_dir)
        except OSError as exc:
            return self._mint_unguarded(base_url, tenant, credential, exc, refresh_of=refresh_of)

        try:
            # nexus-ndfc1: opportunistic, bounded, inline, never raises. Our
            # own lock was just opened and touched, so it is never a
            # candidate. Inside the try so a BaseException cannot leak the fd.
            _reap_stale_mint_locks(config_dir, keep=lock_path)
            # nexus-4gmw8: and expired leases, under their own same-key locks.
            _reap_expired_leases(
                config_dir, now=self._wall_clock(),
                keep=_data_token_lease_path(base_url, tenant, config_dir),
            )

            deadline = time.monotonic() + self._lock_wait_ceiling_seconds
            refresh_deadline = time.monotonic() + _REFRESH_LOCK_WAIT_S
            reopens = 0
            while True:
                try:
                    _locking.lock_fd(lock_fd, blocking=False)  # lifecycle-gate-allow: non-blocking mint-on-miss mutex (nexus-nnr26), not a lifecycle election
                except BlockingIOError:
                    # A sibling holds the lock — check whether it has
                    # already published before waiting further.
                    leased = self._read_lease_newer(base_url, tenant, refresh_of)
                    if leased is not None:
                        _log.info(
                            "data_token_lease_reused", tenant=tenant, endpoint=_log_endpoint(base_url),
                        )
                        return leased
                    if (
                        refresh_of is not None
                        and refresh_of.expires_at > self._clock()
                        and time.monotonic() >= refresh_deadline
                    ):
                        return refresh_of
                    if time.monotonic() >= deadline:
                        raise DataTokenMintError(
                            f"data-token mint-on-miss lock wait exceeded "
                            f"{self._lock_wait_ceiling_seconds:.0f}s for "
                            f"{_log_endpoint(base_url)} tenant={tenant!r} — a sibling "
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
                except OSError as exc:
                    # ENOLCK / EOPNOTSUPP: a filesystem that cannot flock (NFS,
                    # some FUSE mounts). The lock is an optimization; mint
                    # without it, as when the lock file cannot be opened.
                    return self._mint_unguarded(base_url, tenant, credential, exc, refresh_of=refresh_of)
                if reopens < _MINT_LOCK_REOPEN_MAX and not _fd_names_path(lock_fd, lock_path):
                    # nexus-ndfc1 review B2: a reaper unlinked the file we
                    # opened before we locked it. A lock on an unlinked inode
                    # serializes nothing (the next creator locks a new file),
                    # so drop it and lock whatever the path names now.
                    reopens += 1
                    _locking.unlock_fd(lock_fd)  # lifecycle-gate-allow: drop a lock on an unlinked inode (nexus-ndfc1)
                    try:
                        new_fd = _open_mint_lock_logged(base_url, tenant, lock_path, config_dir)
                    except OSError as exc:
                        return self._mint_unguarded(base_url, tenant, credential, exc, refresh_of=refresh_of)
                    os.close(lock_fd)
                    lock_fd = new_fd
                    continue
                try:
                    # Double-check under the lock: a racer may have won
                    # and published between our last poll and acquiring
                    # the lock ourselves.
                    leased = self._read_lease_newer(base_url, tenant, refresh_of)
                    if leased is not None:
                        _log.info(
                            "data_token_lease_reused", tenant=tenant, endpoint=_log_endpoint(base_url),
                        )
                        return leased
                    # A refresh that still holds a valid token makes ONE attempt
                    # and never sleeps: its retry is the jittered refresh
                    # window, and the key lock and this flock are held
                    # throughout (nexus-dncjg). With no valid token (a cold
                    # start, or one that expired) the full retry budget stands.
                    fresh = self._mint(
                        base_url, tenant, credential,
                        single_attempt=refresh_of is not None and refresh_of.expires_at > self._clock(),
                    )
                    self._write_lease(base_url, tenant, fresh)
                    _log.info(
                        "data_token_minted", tenant=tenant, expires_in=fresh.ttl_seconds, endpoint=_log_endpoint(base_url),
                    )
                    return fresh
                finally:
                    _locking.unlock_fd(lock_fd)  # lifecycle-gate-allow: release the mint-on-miss mutex (nexus-nnr26)
        finally:
            os.close(lock_fd)

    def _read_lease_newer(
        self, base_url: str, tenant: str, refresh_of: _CachedToken | None,
    ) -> _CachedToken | None:
        """:meth:`_read_lease` at this manager's own refresh point, narrowed for
        a refresh (nexus-dncjg) to a lease that is a sibling's REMINT of the
        token being refreshed: a different token that expires later. The very
        token being refreshed is no help even when a stepped wall clock makes
        its re-derived expiry look later (token equality, not a time
        tolerance), and a different token that expires no later is no
        improvement."""
        leased = self._read_lease(base_url, tenant, near_expiry_threshold=self._refresh_threshold)
        if leased is None or refresh_of is None:
            return leased
        if leased.token != refresh_of.token and leased.expires_at > refresh_of.expires_at:
            return leased
        return None

    def _mint_unguarded(
        self, base_url: str, tenant: str, credential: str, cause: OSError,
        *, refresh_of: _CachedToken | None = None,
    ) -> _CachedToken:
        """Mint without the lock: the lock file cannot be opened, and the
        mint (which the credential otherwise allows) must not be blocked by
        an optimization. A refresh that still holds a valid token makes one
        attempt with no sleep here too (nexus-dncjg)."""
        _log.warning(
            "data_token_mint_lock_unavailable", tenant=tenant, endpoint=_log_endpoint(base_url), error=str(cause),
        )
        fresh = self._mint(
            base_url, tenant, credential,
            single_attempt=refresh_of is not None and refresh_of.expires_at > self._clock(),
        )
        self._publish_lease_locked(base_url, tenant, fresh)
        _log.info(
            "data_token_minted", tenant=tenant, expires_in=fresh.ttl_seconds, endpoint=_log_endpoint(base_url),
        )
        return fresh

    def _publish_lease_locked(self, base_url: str, tenant: str, cached: _CachedToken) -> None:
        """Publish *cached* while holding the same-key mint lock (nexus-fcwbd).

        The lease reaper removes a dead lease only under this lock, so a
        publisher that does not hold it can have a fresh lease deleted in the
        window between the reaper's inode check and its unlink. Used by the
        paths that do not already hold the lock (:meth:`_mint_unguarded`);
        the guarded mint, cold or refresh, holds it already and calls
        :meth:`_write_lease` directly.

        Best-effort, like the lease itself, and never raises: a lock that
        cannot be opened or flocked falls back to the plain write (it is an
        optimization, and a dir that refuses the lock file usually refuses the
        lease); a lock still held when :data:`_LEASE_PUBLISH_LOCK_WAIT_S` is
        spent means a reaper or a cold minter is in the middle of this key, so
        the lease is left to it and the caller already has its token.
        """
        config_dir = self._resolve_config_dir()
        try:
            make_user_dir(config_dir)
            lock_path = _data_token_mint_lock_path(base_url, tenant, config_dir)
            lock_fd = _open_mint_lock_logged(base_url, tenant, lock_path, config_dir)
        except OSError:
            self._write_lease(base_url, tenant, cached)
            return
        try:
            deadline = time.monotonic() + _LEASE_PUBLISH_LOCK_WAIT_S
            reopens = 0
            while True:
                try:
                    _locking.lock_fd(lock_fd, blocking=False)  # lifecycle-gate-allow: non-blocking same-key mutex for the lease publish (nexus-fcwbd), not a lifecycle election
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        _log.debug(
                            "data_token_lease_publish_skipped_lock_held", tenant=tenant,
                            endpoint=_log_endpoint(base_url),
                        )
                        return
                    self._sleep(_MINT_LOCK_POLL_INTERVAL_S)
                    continue
                except OSError:
                    # ENOLCK / EOPNOTSUPP: a filesystem that cannot flock (NFS,
                    # some FUSE mounts). The caller holds a good token and the
                    # lease is an optimization: write it unlocked.
                    self._write_lease(base_url, tenant, cached)
                    return
                if reopens < _MINT_LOCK_REOPEN_MAX and not _fd_names_path(lock_fd, lock_path):
                    # A reaper unlinked the file between our open and our lock:
                    # a lock on an unlinked inode serializes nothing.
                    reopens += 1
                    _locking.unlock_fd(lock_fd)  # lifecycle-gate-allow: drop a lock on an unlinked inode (nexus-fcwbd)
                    try:
                        new_fd = _open_mint_lock_logged(base_url, tenant, lock_path, config_dir)
                    except OSError:
                        self._write_lease(base_url, tenant, cached)
                        return
                    os.close(lock_fd)
                    lock_fd = new_fd
                    continue
                try:
                    self._write_lease(base_url, tenant, cached)
                finally:
                    _locking.unlock_fd(lock_fd)  # lifecycle-gate-allow: release the lease-publish mutex (nexus-fcwbd)
                return
        finally:
            os.close(lock_fd)

    def _mint(
        self, base_url: str, tenant: str, credential: str, *, single_attempt: bool = False,
    ) -> _CachedToken:
        """One mint round trip, with the bounded retry on a transient status.

        ``single_attempt`` (nexus-dncjg) makes it ONE attempt with no sleep:
        a refresh whose cached token is still valid has a better retry than
        sleeping under the key lock and the mint flock (the jittered refresh
        window), so a refused refresh must come back at once. The refusal's
        status and ``Retry-After`` ride on the raised
        :class:`DataTokenMintError`.
        """
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
        resp_headers: dict[str, str] = {}
        for attempt in range(1, _MINT_MAX_ATTEMPTS + 1):
            mint_error: DataTokenMintError | None = None
            try:
                status, payload, resp_headers = self._poster(url, headers, body)
            except Exception as exc:  # noqa: BLE001 — any transport failure becomes the typed mint error
                # A transport error can quote the URL it choked on:
                # ``http.client.InvalidURL`` for ``https://user:pw@host`` says
                # "nonnumeric port: 'pw@host'", and for a percent-encoded or
                # colon-bearing password a decoded or truncated tail of it.
                # The text is replaced whole when it quotes any of that
                # (nexus-q26w1), and the error is raised OUTSIDE this handler:
                # neither ``__cause__`` nor ``__context__`` may carry the
                # original, whose message would print in a traceback.
                reason = scrub_transport_error(exc, url)
                _log.error("data_token_mint_failed", tenant=body_tenant, endpoint=_log_endpoint(url), error=reason)
                mint_error = DataTokenMintError(
                    f"data-token mint request to {_log_endpoint(url)} failed: {reason}"
                )
            if mint_error is not None:
                raise mint_error from None

            if single_attempt or status not in _RETRYABLE_MINT_STATUSES or attempt == _MINT_MAX_ATTEMPTS:
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
                # nexus-kcdbx: full jitter, so clients refused together do not
                # retry together. Only ever shorter than the schedule value.
                delay = self._rng.uniform(0.0, _MINT_BACKOFF_SCHEDULE[attempt - 1])
            _log.warning(
                "data_token_mint_retry",
                tenant=body_tenant, endpoint=_log_endpoint(url), status=status,
                attempt=attempt, delay=delay, retry_after_present=retry_after is not None,
            )
            self._sleep(delay)

        if status == 403:
            err = payload.get("error", "") if isinstance(payload, dict) else str(payload)
            _log.error(
                "data_token_mint_failed", tenant=body_tenant, endpoint=_log_endpoint(url),
                status=403, error=err,
            )
            # nexus-ssqk9: name BOTH the configured/requested tenant and the
            # remedy — a mint-locked credential 403s the instant the body
            # tenant does not match its OWN bound tenant, and the client has
            # no way to learn that bound tenant except from this message.
            raise DataTokenMintError(
                f"data-token mint failed (403) against {_log_endpoint(url)}: {err}. "
                f"Sent tenant={body_tenant!r} (mint_tenant config="
                f"{configured_mint_tenant or '(unset)'!r}, caller-supplied "
                f"tenant={tenant!r}). A mint-locked credential is bound to "
                f"exactly one tenant server-side and 403s any other. Remedy: "
                f"'nx config set mint_tenant <the credential's bound tenant>'.",
                status=403,
            )

        if status != 200:
            err = payload.get("error", "") if isinstance(payload, dict) else str(payload)
            _log.error(
                "data_token_mint_failed", tenant=body_tenant, endpoint=_log_endpoint(url),
                status=status, error=err,
            )
            raise DataTokenMintError(
                f"data-token mint failed ({status}) against {_log_endpoint(url)}: {err}. "
                f"Remedy: verify the configured mint_token credential is valid, "
                f"unrevoked, and (if mint-locked) bound to tenant {body_tenant!r} "
                f"('nx config set mint_token <bearer>').",
                status=status,
                retry_after=parse_retry_after(resp_headers),
            )

        token = payload.get("data_token") if isinstance(payload, dict) else None
        if not token:
            _log.error("data_token_mint_failed", tenant=body_tenant, endpoint=_log_endpoint(url), error="no data_token in response")
            raise DataTokenMintError(
                f"data-token mint against {_log_endpoint(url)} returned no 'data_token' field"
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
