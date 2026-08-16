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
