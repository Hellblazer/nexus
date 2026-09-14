# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for DataTokenManager (nexus-wrwb7).

Deterministic: injected clock + poster, no real network. Thread-safety test
uses real threads with a synthetic poster delay to force the race window.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
import structlog

from nexus.db.data_token import (
    DataTokenManager,
    DataTokenMintError,
    get_data_token_manager,
    reset_data_token_manager,
)

# nexus-9c7t9: every mint now also (best-effort) writes a cross-process
# lease file under ``nexus_config_dir()``. The managers in this file never
# pass an explicit ``config_dir``, so they resolve the real function --
# but the suite-wide autouse ``_isolate_config_dir`` fixture (conftest.py)
# already redirects ``NEXUS_CONFIG_DIR`` to a per-test ``tmp_path``, so no
# extra isolation fixture is needed here. See the loop-test tenant
# comment below for the one place per-test (not per-suite) isolation
# still mattered: three manager instances sharing ONE test's tmp_path.


class _FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FakePoster:
    """Records calls; returns a canned (status, body, headers) sequence."""

    def __init__(self, responses: list[tuple[int, dict, dict]] | None = None) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self._responses = list(responses or [])
        self._default = (200, {"data_token": "tok-default", "expires_in_seconds": 300}, {})
        self.delay: float = 0.0

    def __call__(self, url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
        self.calls.append((url, dict(headers), dict(body)))
        if self.delay:
            time.sleep(self.delay)
        if self._responses:
            return self._responses.pop(0)
        return self._default

    def queue(self, status: int, body: dict, headers: dict | None = None) -> None:
        self._responses.append((status, body, dict(headers or {})))


class _FakeSleep:
    """Records requested delays instead of actually sleeping (critic S2:
    deterministic retry-backoff tests)."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


BASE_URL = "http://127.0.0.1:9999"
TENANT = "acme"


def _manager(
    poster: _FakePoster,
    clock: _FakeClock,
    *,
    credential: str | None = "mintcred",
    mint_tenant: str | None = "",
    sleep: Callable[[float], None] | None = None,
) -> DataTokenManager:
    cred_fn = (lambda: credential) if credential is not None else (lambda: "")
    # nexus-ssqk9: default to an explicit empty override (never the REAL
    # nexus.config.get_credential("mint_tenant")) so these tests stay
    # deterministic regardless of what NX_MINT_TENANT/config.yml happens to
    # hold on the machine running them -- the same isolation discipline
    # mint_credential already gets.
    tenant_fn = lambda: (mint_tenant or "")  # noqa: E731 — small local closure, test-only
    kwargs: dict[str, Any] = {"mint_tenant": tenant_fn}
    if sleep is not None:
        kwargs["sleep"] = sleep
    return DataTokenManager(clock=clock, poster=poster, mint_credential=cred_fn, **kwargs)


# ── mint-on-first-use / cache hit ────────────────────────────────────────────


def test_mint_on_first_use() -> None:
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock())

    token = mgr.bearer_for(BASE_URL, TENANT)

    assert token == "tok-1"
    assert len(poster.calls) == 1
    url, headers, body = poster.calls[0]
    assert url == f"{BASE_URL}/v1/data-tokens/mint"
    assert headers["Authorization"] == "Bearer mintcred"
    assert body["tenant"] == TENANT


def test_cache_hit_no_second_mint() -> None:
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    clock = _FakeClock()
    mgr = _manager(poster, clock)

    first = mgr.bearer_for(BASE_URL, TENANT)
    clock.advance(10)  # well within the 300s TTL, nowhere near the 20% threshold
    second = mgr.bearer_for(BASE_URL, TENANT)

    assert first == second == "tok-1"
    assert len(poster.calls) == 1


def test_residue_discipline_n_calls_one_mint() -> None:
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 3600})
    mgr = _manager(poster, _FakeClock())

    for _ in range(25):
        mgr.bearer_for(BASE_URL, TENANT)

    assert len(poster.calls) == 1


# ── refresh at <20% TTL remaining ────────────────────────────────────────────


def test_refresh_below_twenty_percent_ttl() -> None:
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 100})
    poster.queue(200, {"data_token": "tok-2", "expires_in_seconds": 100})
    clock = _FakeClock()
    mgr = _manager(poster, clock)

    first = mgr.bearer_for(BASE_URL, TENANT)
    clock.advance(85)  # 15s remaining of 100s TTL = 15% < 20% threshold
    second = mgr.bearer_for(BASE_URL, TENANT)

    assert first == "tok-1"
    assert second == "tok-2"
    assert len(poster.calls) == 2


def test_no_refresh_above_twenty_percent_ttl() -> None:
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 100})
    clock = _FakeClock()
    mgr = _manager(poster, clock)

    mgr.bearer_for(BASE_URL, TENANT)
    clock.advance(75)  # 25s remaining of 100s TTL = 25% > 20% threshold
    token = mgr.bearer_for(BASE_URL, TENANT)

    assert token == "tok-1"
    assert len(poster.calls) == 1


# ── has_live_token / granted_ttl_seconds peeks (critic S1/S3) ────────────────


def test_has_live_token_false_before_any_mint() -> None:
    poster = _FakePoster()
    mgr = _manager(poster, _FakeClock())
    assert mgr.has_live_token(BASE_URL, TENANT) is False
    assert poster.calls == []  # a peek must never itself trigger a mint


def test_has_live_token_true_after_mint_reflects_reuse_vs_fresh() -> None:
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    clock = _FakeClock()
    mgr = _manager(poster, clock)

    assert mgr.has_live_token(BASE_URL, TENANT) is False  # nothing cached yet
    mgr.bearer_for(BASE_URL, TENANT)
    assert mgr.has_live_token(BASE_URL, TENANT) is True  # now live, reusable

    clock.advance(260)  # 40s remaining of 300s TTL = 13% < 20% threshold
    assert mgr.has_live_token(BASE_URL, TENANT) is False  # due for refresh, not "live"


def test_granted_ttl_seconds_none_before_any_mint() -> None:
    poster = _FakePoster()
    mgr = _manager(poster, _FakeClock())
    assert mgr.granted_ttl_seconds(BASE_URL, TENANT) is None


def test_granted_ttl_seconds_reports_the_minted_value() -> None:
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock())

    mgr.bearer_for(BASE_URL, TENANT)

    assert mgr.granted_ttl_seconds(BASE_URL, TENANT) == 300.0


# ── invalidate-then-remint ───────────────────────────────────────────────────


def test_invalidate_then_remint() -> None:
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    poster.queue(200, {"data_token": "tok-2", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock())

    first = mgr.bearer_for(BASE_URL, TENANT)
    mgr.invalidate(BASE_URL, TENANT)
    second = mgr.bearer_for(BASE_URL, TENANT)

    assert first == "tok-1"
    assert second == "tok-2"
    assert len(poster.calls) == 2


def test_invalidate_unknown_key_is_a_noop() -> None:
    poster = _FakePoster()
    mgr = _manager(poster, _FakeClock())
    mgr.invalidate(BASE_URL, TENANT)  # never minted — must not raise
    assert poster.calls == []


# ── mint-body tenant resolution (nexus-ssqk9) ────────────────────────────────


def test_mint_body_tenant_defaults_to_caller_tenant_when_unconfigured() -> None:
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock(), mint_tenant="")

    mgr.bearer_for(BASE_URL, TENANT)

    _, _, body = poster.calls[0]
    assert body["tenant"] == TENANT


def test_configured_mint_tenant_overrides_caller_tenant_in_mint_body() -> None:
    """The scenario nexus-ssqk9 exists for: every Http*Store defaults its
    caller-passed tenant to 'default', but the real mint-locked credential
    is bound to something else ('nexus') -- mint_tenant lets the mint BODY
    carry the credential's actual bound tenant while the store keeps using
    'default' as its cache-key/X-Nexus-Tenant convention unchanged."""
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock(), mint_tenant="nexus")

    mgr.bearer_for(BASE_URL, "default")

    _, _, body = poster.calls[0]
    assert body["tenant"] == "nexus"


def test_configured_mint_tenant_does_not_change_the_cache_key() -> None:
    """The cache key stays the CALLER-passed tenant regardless of
    mint_tenant -- only the wire-level mint body tenant changes."""
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock(), mint_tenant="nexus")

    mgr.bearer_for(BASE_URL, "default")

    assert (BASE_URL, "default") in mgr._cache  # noqa: SLF001 — verifying the cache-key contract is the point


# ── mint retry/backoff on transient statuses (critic S2) ─────────────────────


def test_mint_retries_on_429_then_succeeds() -> None:
    poster = _FakePoster()
    poster.queue(429, {"error": "rate limit exceeded, retry later"})
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    sleep = _FakeSleep()
    mgr = _manager(poster, _FakeClock(), sleep=sleep)

    token = mgr.bearer_for(BASE_URL, TENANT)

    assert token == "tok-1"
    assert len(poster.calls) == 2
    assert sleep.calls == [1.0]  # first backoff slot, no Retry-After supplied


def test_mint_retry_honors_retry_after_header() -> None:
    poster = _FakePoster()
    poster.queue(429, {"error": "rate limit exceeded"}, headers={"Retry-After": "5"})
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    sleep = _FakeSleep()
    mgr = _manager(poster, _FakeClock(), sleep=sleep)

    token = mgr.bearer_for(BASE_URL, TENANT)

    assert token == "tok-1"
    assert sleep.calls == [5.0]  # server-supplied Retry-After wins over the default schedule


def test_mint_retry_after_capped_at_site_ceiling_not_shared_clamp() -> None:
    """A Retry-After near ``parse_retry_after``'s shared 300s clamp must be
    capped to ``_MINT_RETRY_AFTER_CAP_S`` (15s): the mint is a synchronous
    auth round trip on interactive/shutdown paths (the session-end
    launcher's zero-wait-risk invariant), so the write-path-sized clamp
    must never be slept verbatim here. Review round-2 Significant
    (nexus-ssqk9 thread): pre-fix worst case was ~600s across the retry
    budget; post-fix hard ceiling is seconds-scale."""
    from nexus.db.data_token import _MINT_RETRY_AFTER_CAP_S

    poster = _FakePoster()
    poster.queue(429, {"error": "rate limit exceeded"}, headers={"Retry-After": "300"})
    poster.queue(429, {"error": "rate limit exceeded"}, headers={"Retry-After": "300"})
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    sleep = _FakeSleep()
    mgr = _manager(poster, _FakeClock(), sleep=sleep)

    token = mgr.bearer_for(BASE_URL, TENANT)

    assert token == "tok-1"
    assert sleep.calls == [_MINT_RETRY_AFTER_CAP_S, _MINT_RETRY_AFTER_CAP_S]
    assert sum(sleep.calls) <= 60.0  # the documented hard ceiling holds


def test_mint_retries_on_502_503_504_then_succeeds() -> None:
    for gateway_status in (502, 503, 504):
        poster = _FakePoster()
        poster.queue(gateway_status, {"error": "bad gateway"})
        poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
        sleep = _FakeSleep()
        mgr = _manager(poster, _FakeClock(), sleep=sleep)

        # nexus-9c7t9: a distinct tenant per iteration -- three FRESH
        # manager instances share this test's one tmp_path config_dir
        # (the isolation fixture above), so reusing TENANT across
        # iterations would let iteration N borrow iteration N-1's
        # lease-file mint instead of exercising its own fake poster.
        token = mgr.bearer_for(BASE_URL, f"{TENANT}-{gateway_status}")

        assert token == "tok-1"
        assert len(poster.calls) == 2


def test_mint_retry_exhausts_after_three_attempts_then_fails_loud() -> None:
    poster = _FakePoster()
    poster.queue(503, {"error": "unavailable"})
    poster.queue(503, {"error": "unavailable"})
    poster.queue(503, {"error": "still unavailable"})
    sleep = _FakeSleep()
    mgr = _manager(poster, _FakeClock(), sleep=sleep)

    with pytest.raises(DataTokenMintError, match="503"):
        mgr.bearer_for(BASE_URL, TENANT)

    assert len(poster.calls) == 3  # 1 initial + 2 retries, no more
    assert sleep.calls == [1.0, 2.0]  # design of record: "1s/2s"


def test_mint_retry_never_touches_the_shared_rate_brake() -> None:
    """critic S2: the mint retry is self-contained -- it must not import or
    call into nexus.rate_brake at all (a mint is a single infrequent auth
    round trip, not a bulk-write worker the shared brake coordinates)."""
    from nexus.rate_brake import get_brake, reset_brake

    reset_brake()
    brake = get_brake()
    baseline_trips = brake.trips

    poster = _FakePoster()
    poster.queue(429, {"error": "rate limit exceeded"})
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    sleep = _FakeSleep()
    mgr = _manager(poster, _FakeClock(), sleep=sleep)

    mgr.bearer_for(BASE_URL, TENANT)

    assert get_brake().trips == baseline_trips


# ── failure modes: fail loud, typed ──────────────────────────────────────────


def test_mint_transport_failure_is_typed_and_loud() -> None:
    def boom(url: str, headers: dict, body: dict) -> tuple[int, dict]:
        raise ConnectionRefusedError("no route to host")

    mgr = _manager(boom, _FakeClock())  # type: ignore[arg-type]

    with pytest.raises(DataTokenMintError, match="mint request"):
        mgr.bearer_for(BASE_URL, TENANT)


def test_mint_401_fails_loud_typed() -> None:
    poster = _FakePoster()
    poster.queue(401, {"error": "invalid or revoked credential"})
    mgr = _manager(poster, _FakeClock())

    with pytest.raises(DataTokenMintError, match="401"):
        mgr.bearer_for(BASE_URL, TENANT)


def test_cross_tenant_403_surfaces_verbatim() -> None:
    poster = _FakePoster()
    poster.queue(403, {
        "error": "forbidden: this mint credential is locked to tenant 'acme' "
                 "and cannot mint for tenant 'other'",
    })
    mgr = _manager(poster, _FakeClock())

    with pytest.raises(DataTokenMintError) as exc_info:
        mgr.bearer_for(BASE_URL, "other")

    assert "locked to tenant 'acme'" in str(exc_info.value)
    assert "cannot mint for tenant 'other'" in str(exc_info.value)


def test_cross_tenant_403_names_configured_and_requested_tenant_plus_remedy() -> None:
    """nexus-ssqk9: the 403 teaching message must name BOTH the configured
    mint_tenant (or its absence) and the requested/caller tenant, plus the
    concrete remedy command -- not just relay the server's own text."""
    poster = _FakePoster()
    poster.queue(403, {"error": "forbidden: this mint credential is locked to tenant 'nexus'"})
    mgr = _manager(poster, _FakeClock(), mint_tenant="wrong-tenant")

    with pytest.raises(DataTokenMintError) as exc_info:
        mgr.bearer_for(BASE_URL, TENANT)

    message = str(exc_info.value)
    assert "wrong-tenant" in message  # the configured mint_tenant that was sent
    assert TENANT in message  # the caller-supplied tenant, for contrast
    assert "nx config set mint_tenant" in message


def test_cross_tenant_403_names_unset_when_mint_tenant_not_configured() -> None:
    poster = _FakePoster()
    poster.queue(403, {"error": "forbidden: cross-tenant mint"})
    mgr = _manager(poster, _FakeClock(), mint_tenant="")

    with pytest.raises(DataTokenMintError, match=r"mint_tenant config='\(unset\)'"):
        mgr.bearer_for(BASE_URL, TENANT)


def test_mint_missing_data_token_field_fails_loud() -> None:
    poster = _FakePoster()
    poster.queue(200, {"expires_in_seconds": 300})  # malformed: no data_token
    mgr = _manager(poster, _FakeClock())

    with pytest.raises(DataTokenMintError, match="data_token"):
        mgr.bearer_for(BASE_URL, TENANT)


def test_failed_mint_does_not_poison_cache() -> None:
    """A failed mint must not leave a broken/partial entry that a later
    successful call would mistake for a valid cache hit."""
    poster = _FakePoster()
    poster.queue(500, {"error": "internal server error"})
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock())

    with pytest.raises(DataTokenMintError):
        mgr.bearer_for(BASE_URL, TENANT)

    token = mgr.bearer_for(BASE_URL, TENANT)
    assert token == "tok-1"
    assert len(poster.calls) == 2


# ── no-credential-configured -> manager inert ────────────────────────────────


def test_no_credential_configured_returns_none() -> None:
    poster = _FakePoster()
    mgr = _manager(poster, _FakeClock(), credential=None)

    token = mgr.bearer_for(BASE_URL, TENANT)

    assert token is None
    assert poster.calls == []


def test_is_configured_reflects_credential_presence() -> None:
    poster = _FakePoster()
    configured = _manager(poster, _FakeClock(), credential="mintcred")
    unconfigured = _manager(poster, _FakeClock(), credential=None)

    assert configured.is_configured() is True
    assert unconfigured.is_configured() is False


# ── thread-safety: two threads, one mint ────────────────────────────────────


def test_concurrent_bearer_for_mints_exactly_once() -> None:
    poster = _FakePoster()
    poster.delay = 0.05  # widen the race window
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    # Extra entries in case of a bug that mints more than once — assertion
    # below is the real guard, but this keeps a bug from raising instead of
    # just being wrong.
    for _ in range(9):
        poster.queue(200, {"data_token": "tok-extra", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock())

    results: list[str | None] = [None] * 10

    def worker(i: int) -> None:
        results[i] = mgr.bearer_for(BASE_URL, TENANT)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(poster.calls) == 1
    assert all(r == "tok-1" for r in results)


# ── credential value never logged ───────────────────────────────────────────


def test_token_and_credential_never_in_log_output() -> None:
    # data_token_minted logs at INFO; the suite-wide default filter is
    # WARNING (tests/conftest.py) -- bump so capture_logs() actually sees it.
    # tests/conftest.py's _restore_structlog_after_test autouse fixture
    # restores the saved config after this test regardless.
    import logging

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))

    poster = _FakePoster()
    poster.queue(200, {"data_token": "SUPER-SECRET-TOKEN", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock(), credential="SUPER-SECRET-CREDENTIAL")

    with structlog.testing.capture_logs() as captured:
        mgr.bearer_for(BASE_URL, TENANT)

    rendered = str(captured)
    assert "SUPER-SECRET-TOKEN" not in rendered
    assert "SUPER-SECRET-CREDENTIAL" not in rendered


def test_mint_failure_never_logs_token_or_credential() -> None:
    poster = _FakePoster()
    poster.queue(401, {"error": "invalid or revoked credential"})
    mgr = _manager(poster, _FakeClock(), credential="SUPER-SECRET-CREDENTIAL")

    with structlog.testing.capture_logs() as captured:
        with pytest.raises(DataTokenMintError):
            mgr.bearer_for(BASE_URL, TENANT)

    rendered = str(captured)
    assert "SUPER-SECRET-CREDENTIAL" not in rendered


# ── module-level default accessor ───────────────────────────────────────────


def test_get_data_token_manager_is_a_singleton() -> None:
    reset_data_token_manager()
    try:
        a = get_data_token_manager()
        b = get_data_token_manager()
        assert a is b
    finally:
        reset_data_token_manager()


def test_reset_data_token_manager_yields_a_fresh_instance() -> None:
    reset_data_token_manager()
    try:
        a = get_data_token_manager()
        reset_data_token_manager()
        b = get_data_token_manager()
        assert a is not b
    finally:
        reset_data_token_manager()


# ── nexus-umue1: invalidate_if_current / futility (prophylactic port of ────
# nexus-r0d37 defect 2's T1 guard to the shared manager) ────────────────────
#
# T1's fix put both guards in PER-INSTANCE state because T1 has exactly one
# instance per session. T2's nine Http*Store instances and T3's module-level
# client all share ONE cached token per (base_url, tenant) key through this
# manager, so the guards live here instead -- these tests pin the primitive
# directly, independent of either client's plumbing (which its own test
# files pin separately).


def test_invalidate_if_current_pops_when_token_matches() -> None:
    """The ordinary case: the caller's sent bearer is still the cached one."""
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    poster.queue(200, {"data_token": "tok-2", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock())

    token = mgr.bearer_for(BASE_URL, TENANT)
    assert token == "tok-1"

    popped = mgr.invalidate_if_current(BASE_URL, TENANT, token)
    assert popped is True

    second = mgr.bearer_for(BASE_URL, TENANT)
    assert second == "tok-2"
    assert len(poster.calls) == 2


def test_invalidate_if_current_skips_when_nothing_cached() -> None:
    """No mint has ever happened for this key -- nothing to compare against."""
    poster = _FakePoster()
    mgr = _manager(poster, _FakeClock())

    assert mgr.invalidate_if_current(BASE_URL, TENANT, "Bearer whatever") is False
    assert poster.calls == []


def test_invalidate_if_current_skips_when_sent_token_already_rotated() -> None:
    """Discriminates the single-flight compare: a caller holding a STALE
    sent_token (a sibling already invalidated-and-reminted since this
    caller's request went out) must retry on the sibling's fresh token
    instead of invalidating it and minting a competing replacement.

    Mutation check: replacing the ``cached.token != sent_token`` compare
    with an unconditional pop makes this test fail (it would report
    ``True`` and mint a THIRD token) while
    test_invalidate_if_current_pops_when_token_matches keeps passing.
    """
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    poster.queue(200, {"data_token": "tok-2", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock())

    first = mgr.bearer_for(BASE_URL, TENANT)
    assert first == "tok-1"
    # A sibling invalidates-and-reminted meanwhile.
    mgr.invalidate(BASE_URL, TENANT)
    second = mgr.bearer_for(BASE_URL, TENANT)
    assert second == "tok-2"

    # This caller's own (now-stale) sent bearer no longer matches.
    stale_popped = mgr.invalidate_if_current(BASE_URL, TENANT, first)
    assert stale_popped is False
    assert len(poster.calls) == 2, "the stale caller must not mint a third token"
    # The fresh (sibling's) token is untouched and still cached.
    assert mgr.has_live_token(BASE_URL, TENANT) is True


def test_invalidate_if_current_concurrent_single_flight() -> None:
    """N concurrent callers all holding the SAME sent bearer must produce
    exactly ONE ``True`` (one invalidate) — every other caller sees the
    cache already empty and retries without invalidating anything.

    Mirrors nexus-r0d37's T1 ``TestRemintSingleFlight`` shape: mutation
    check is test_invalidate_if_current_skips_when_sent_token_already_rotated
    above, which pins the compare half; this pins the concurrency half.
    """
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock())

    token = mgr.bearer_for(BASE_URL, TENANT)
    sent = token  # invalidate_if_current compares against the RAW token

    n = 7
    barrier = threading.Barrier(n)
    results: list[bool] = [False] * n

    def worker(i: int) -> None:
        barrier.wait(timeout=10)
        results[i] = mgr.invalidate_if_current(BASE_URL, TENANT, sent)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(not t.is_alive() for t in threads), "a worker thread hung"
    assert sum(results) == 1, f"expected exactly ONE invalidate, saw {sum(results)}"
    assert mgr.has_live_token(BASE_URL, TENANT) is False, "the winner popped the cache"


def test_is_remint_futile_false_by_default() -> None:
    poster = _FakePoster()
    mgr = _manager(poster, _FakeClock())
    assert mgr.is_remint_futile(BASE_URL, TENANT) is False


def test_mark_remint_futile_blocks_invalidate_if_current() -> None:
    """Discriminates the futility guard: once marked, invalidate_if_current
    returns False even for a caller holding the exact CURRENT token —
    minting again cannot fix whatever is actually wrong.

    Mutation check: removing the ``self._is_futile_locked(key)``
    short-circuit at the top of ``invalidate_if_current`` makes this test
    fail (it would report ``True`` and pop the cache) while the
    single-flight tests above keep passing — the two guards fail
    independently.
    """
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock())

    token = mgr.bearer_for(BASE_URL, TENANT)
    mgr.mark_remint_futile(BASE_URL, TENANT)
    assert mgr.is_remint_futile(BASE_URL, TENANT) is True

    popped = mgr.invalidate_if_current(BASE_URL, TENANT, token)
    assert popped is False
    assert mgr.has_live_token(BASE_URL, TENANT) is True, "the cache must be untouched while futile"
    assert len(poster.calls) == 1


def test_clear_remint_futile_lifts_the_skip() -> None:
    """A resolved condition lifts the skip IMMEDIATELY, without waiting
    out the rest of the window — pinned so the negative cache can never
    become a permanent disabling of a heal that works in other failure
    modes. (This is the EARLY-CLEAR path; window EXPIRY is pinned
    separately below by test_futility_mark_expires_after_the_window --
    Sam's decision, nexus-umue1 review round 2.)"""
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    poster.queue(200, {"data_token": "tok-2", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock())

    token = mgr.bearer_for(BASE_URL, TENANT)
    mgr.mark_remint_futile(BASE_URL, TENANT)
    mgr.clear_remint_futile(BASE_URL, TENANT)
    assert mgr.is_remint_futile(BASE_URL, TENANT) is False

    popped = mgr.invalidate_if_current(BASE_URL, TENANT, token)
    assert popped is True
    assert mgr.bearer_for(BASE_URL, TENANT) == "tok-2"


def test_clear_remint_futile_on_unmarked_key_is_a_noop() -> None:
    poster = _FakePoster()
    mgr = _manager(poster, _FakeClock())
    mgr.clear_remint_futile(BASE_URL, TENANT)  # never marked — must not raise
    assert mgr.is_remint_futile(BASE_URL, TENANT) is False


# ── Sam's decision (nexus-umue1 review round 2): futility EXPIRES ──────────
#
# Critic Critical 1: an unbounded futility mark trades a bounded per-call
# mint-storm risk for an UNBOUNDED-until-natural-TTL-refresh outage risk on
# T2/T3, where a re-mint is normally the correct remedy (unlike T1's
# structurally-permanent stale-session 401). Fix: the mark carries a mark
# TIME (the injectable, monotonic ``clock``) and expires after
# ``remint_futile_window_seconds`` (default DEFAULT_REMINT_FUTILE_WINDOW_S
# = 60s).


def test_futility_mark_expires_after_the_window() -> None:
    """Discriminates the window-expiry guard (Sam's decision): a mark past
    the window is not futile, and a fresh invalidate_if_current on the
    still-cached (never actually re-minted, because it stayed marked)
    token succeeds again."""
    from nexus.db.data_token import DEFAULT_REMINT_FUTILE_WINDOW_S

    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 3600})
    poster.queue(200, {"data_token": "tok-2", "expires_in_seconds": 3600})
    clock = _FakeClock()
    mgr = _manager(poster, clock)

    token = mgr.bearer_for(BASE_URL, TENANT)
    mgr.mark_remint_futile(BASE_URL, TENANT)
    assert mgr.is_remint_futile(BASE_URL, TENANT) is True

    clock.advance(DEFAULT_REMINT_FUTILE_WINDOW_S)  # AT the window: still expired (>=)
    assert mgr.is_remint_futile(BASE_URL, TENANT) is False

    popped = mgr.invalidate_if_current(BASE_URL, TENANT, token)
    assert popped is True, "the window elapsed -- a fresh re-mint attempt must be allowed again"
    assert mgr.bearer_for(BASE_URL, TENANT) == "tok-2"


def test_futility_mark_still_active_just_before_the_window_elapses() -> None:
    """Boundary pin: a mark 1s short of the window is STILL futile —
    distinguishes '>=' (correct) from '>' in the expiry compare."""
    from nexus.db.data_token import DEFAULT_REMINT_FUTILE_WINDOW_S

    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 3600})
    clock = _FakeClock()
    mgr = _manager(poster, clock)

    token = mgr.bearer_for(BASE_URL, TENANT)
    mgr.mark_remint_futile(BASE_URL, TENANT)

    clock.advance(DEFAULT_REMINT_FUTILE_WINDOW_S - 1.0)
    assert mgr.is_remint_futile(BASE_URL, TENANT) is True
    assert mgr.invalidate_if_current(BASE_URL, TENANT, token) is False


def test_remint_futile_window_is_injectable() -> None:
    """The window is a constructor kwarg, not a hardcoded module constant
    baked into the manager -- a test (or an operator) can override it."""
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 3600})
    clock = _FakeClock()
    mgr = DataTokenManager(
        clock=clock, poster=poster, mint_credential=lambda: "mintcred",
        remint_futile_window_seconds=5.0,
    )

    mgr.bearer_for(BASE_URL, TENANT)
    mgr.mark_remint_futile(BASE_URL, TENANT)
    clock.advance(4.0)
    assert mgr.is_remint_futile(BASE_URL, TENANT) is True, "still inside the custom 5s window"
    clock.advance(1.0)
    assert mgr.is_remint_futile(BASE_URL, TENANT) is False, "the custom 5s window has elapsed"


def test_mark_remint_futile_resets_the_window_on_a_re_mark() -> None:
    """Re-marking (e.g. a second caller's own re-mint also 401ing) resets
    the window's start time — the mark does not expire on the ORIGINAL
    mark's schedule once it has been refreshed."""
    from nexus.db.data_token import DEFAULT_REMINT_FUTILE_WINDOW_S

    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 3600})
    clock = _FakeClock()
    mgr = _manager(poster, clock)

    mgr.bearer_for(BASE_URL, TENANT)
    mgr.mark_remint_futile(BASE_URL, TENANT)
    clock.advance(DEFAULT_REMINT_FUTILE_WINDOW_S - 1.0)
    assert mgr.is_remint_futile(BASE_URL, TENANT) is True
    mgr.mark_remint_futile(BASE_URL, TENANT)  # re-mark: window restarts from now

    clock.advance(DEFAULT_REMINT_FUTILE_WINDOW_S - 1.0)  # would be past the ORIGINAL window
    assert mgr.is_remint_futile(BASE_URL, TENANT) is True, "the re-mark's own window has not elapsed yet"


# ── CRE Minor: the "cached is None" cross-instance interleave ──────────────


def test_invalidate_if_current_when_cache_popped_by_a_sibling_mid_flight() -> None:
    """CRE Minor (nexus-umue1 review): the 'cached is None' branch,
    exercised for the actual cross-instance interleave it is meant to
    cover -- a SIBLING pops the entry between this caller's request going
    out and this caller calling invalidate_if_current, not merely 'nothing
    was ever cached' (already pinned by
    test_invalidate_if_current_skips_when_nothing_cached above)."""
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    poster.queue(200, {"data_token": "tok-2", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock())

    token = mgr.bearer_for(BASE_URL, TENANT)
    assert token == "tok-1"

    # A sibling races ahead: its own invalidate_if_current call (holding
    # the SAME sent_token, having 401'd on the identical cached bearer)
    # wins and pops the entry, leaving the cache genuinely EMPTY -- not
    # "never populated".
    sibling_popped = mgr.invalidate_if_current(BASE_URL, TENANT, token)
    assert sibling_popped is True
    assert mgr.has_live_token(BASE_URL, TENANT) is False

    # THIS caller's own request 401'd on the SAME stale token and now
    # calls invalidate_if_current with the identical sent_token -- the
    # cache is None because a SIBLING already popped it. Must decline
    # rather than double-invalidating (there is nothing left to compare).
    late_popped = mgr.invalidate_if_current(BASE_URL, TENANT, token)
    assert late_popped is False

    # The caller then retries via bearer_for(), which is lock-serialized
    # with the sibling's own mint-on-miss (_mint_guarded) -- it observes
    # the sibling's fresh token rather than resending the rejected one
    # forever, and no THIRD mint happens.
    healed = mgr.bearer_for(BASE_URL, TENANT)
    assert healed == "tok-2"
    assert len(poster.calls) == 2, "exactly one re-mint total across both callers"
