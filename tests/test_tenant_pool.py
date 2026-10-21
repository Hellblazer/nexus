# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-fpwj1 (rtg20 phase B): the per-worker data-reset tenant pool, against the REAL substrate engine.

Each behaviour has a CONTROL where the claim could hold by accident: the same scenario with the engine eviction
skipped (``TenantPool(evict=False)``) must show the stale state the eviction exists to remove. A scenario whose
control does not differ proves nothing about the pool.

The tests build their own :class:`TenantPool` over ``ensure_engine()``. The engine only registers the reset route, and
only runs with per-collection index builds and the reaper off, while the pool is on (the default; off only with
``NX_TEST_TENANT_POOL=0``; ``tests/_engine_substrate.py``), so every test that needs the engine skips under ``=0``, and
``tests/test_tenant_pool_end_to_end.py`` then runs this file in a child pytest with the pool on and fails unless the
child ran and passed every test (non-vacuity). The default-on and ``=0`` wiring through the real conftest is the same
file.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

import nexus.aspect_extractor as aspect_extractor
import nexus.search_engine as search_engine
from nexus import mcp_infra
from nexus.db import http_vector_client
from nexus.service_handles import SharedClientSlot
from tests import _ddl_counter, _engine_substrate, _storage_touch_guard, _tenant_pool
from tests._engine_substrate import drop_test_tenant, ensure_engine, mint_test_tenant
from tests._tenant_pool import TenantLeakError, TenantLeaseBusyError, TenantPool

COLLECTION = "knowledge__pooltest__bge-base-en-v15-768__v1"
GHOST_KEY = "rdr204_ghost_sweep_v1"
UNREGISTERED = "unregistered_collection"


@pytest.fixture(scope="module")
def state() -> dict[str, Any]:
    if not _tenant_pool.enabled():
        pytest.skip(f"needs the engine the pool boots (the pool is on unless {_tenant_pool.ENV_FLAG}=0); "
                    "test_tenant_pool_end_to_end.py runs this file in a child with the pool on")
    return ensure_engine()


@pytest.fixture
def make_pool(state: dict[str, Any]) -> Iterator[Callable[..., TenantPool]]:
    """Pools that retire their tenant when the test is done, so no leaves are left on the worker's PG."""
    made: list[TenantPool] = []

    def make(pool_cls: type[TenantPool] = TenantPool, **kwargs: Any) -> TenantPool:
        pool = pool_cls(state, **kwargs)
        made.append(pool)
        return pool

    yield make
    for pool in made:
        pool.retire("test end")


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _post(state: dict[str, Any], path: str, token: str, body: dict[str, Any], timeout: float = 30.0) -> httpx.Response:
    return httpx.post(f"{state['base_url']}{path}", json=body, headers=_hdr(token), timeout=timeout)


def _whoami(state: dict[str, Any], token: str) -> int:
    return httpx.get(f"{state['base_url']}/v1/_whoami", headers=_hdr(token), timeout=30.0).status_code


def _sql(state: dict[str, Any], sql: str, **variables: str) -> str:
    proc = _tenant_pool.psql(state, "\\set ON_ERROR_STOP on\n" + sql, **variables)
    assert proc.returncode == 0, f"psql failed: {proc.stderr}\nSQL: {sql}"
    return proc.stdout.strip()


def _register(state: dict[str, Any], token: str) -> httpx.Response:
    return _post(state, "/v1/catalog/collections/upsert", token, {
        "name": COLLECTION, "content_type": "knowledge", "owner_id": "pooltest",
        "embedding_model": "bge-base-en-v15-768", "model_version": "v1", "display_name": "",
        "legacy_grandfathered": False,
    })


def _write_reason(state: dict[str, Any], token: str) -> str:
    """Why a chunk write to COLLECTION is refused: ``unregistered_collection`` when the engine finds no registry row,
    something else (a manifest refusal) when it believes the collection is registered."""
    resp = _post(state, "/v1/vectors/upsert-chunks", token, {
        "collection": COLLECTION, "ids": ["a" * 64], "documents": ["hello"], "metadatas": [{}]})
    body = resp.json()
    return body.get("reason") or body.get("error", "")[:60]


# --- census: it names what is left, and it will not pass over nothing ----------------------------------------


def test_census_names_a_row_left_in_a_plain_table(state: dict[str, Any]) -> None:
    tenant, _ = mint_test_tenant(state)
    try:
        # a freshly minted tenant holds its token row, and the census says so: the census sees tokens too
        with pytest.raises(TenantLeakError, match=r"nexus\.service_tokens=1"):
            _tenant_pool.census(state, tenant)
        _tenant_pool.wipe(state, tenant)
        _tenant_pool.census(state, tenant)  # positive control: after the wipe the same tenant is empty
        _sql(state, "INSERT INTO nexus.memory (tenant_id, project, title, content, timestamp) "
                    "VALUES (:'tenant', 'p', 't', 'c', now());", tenant=tenant)
        with pytest.raises(TenantLeakError, match=r"nexus\.memory=1"):
            _tenant_pool.census(state, tenant)
        _tenant_pool.wipe(state, tenant)
        _tenant_pool.census(state, tenant)  # and the wipe makes it pass again
    finally:
        drop_test_tenant(state, tenant)


def test_census_names_a_row_left_in_a_partition_leaf(state: dict[str, Any]) -> None:
    tenant, _ = mint_test_tenant(state)
    try:
        _tenant_pool.wipe(state, tenant)
        _sql(state, "SET session_replication_role = replica; "
                    "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_768, embedding_model) "
                    "VALUES (:'tenant', 'c', decode(repeat('00', 32), 'hex'), 'x', array_fill(0.1::real, ARRAY[768])::nexus.vector(768), 'bge-base-en-v15-768');", tenant=tenant)
        with pytest.raises(TenantLeakError, match=r"nexus\.chunks=1"):
            _tenant_pool.census(state, tenant)
        _tenant_pool.wipe(state, tenant)
        _tenant_pool.census(state, tenant)
    finally:
        drop_test_tenant(state, tenant)


def test_census_refuses_to_pass_over_too_few_relations_or_a_missing_required_one(state: dict[str, Any]) -> None:
    tenant, _ = mint_test_tenant(state)
    try:
        too_few = _tenant_pool.CENSUS_SQL.replace(
            f"< {_tenant_pool.MIN_CENSUS_RELATIONS} THEN", "< 100000 THEN")
        assert too_few != _tenant_pool.CENSUS_SQL
        _tenant_pool.wipe(state, tenant)
        _tenant_pool.census(state, tenant)  # the unmodified census passes: only the threshold differs below
        proc = _tenant_pool.psql(state, too_few, tenant=tenant, allow_token="")
        assert proc.returncode != 0 and "fewer than" in proc.stderr, proc.stderr
        missing = _tenant_pool.CENSUS_SQL.replace("'nexus.chunks',", "'nexus.no_such_relation', 'nexus.chunks',")
        assert missing != _tenant_pool.CENSUS_SQL
        proc = _tenant_pool.psql(state, missing, tenant=tenant, allow_token="")
        assert proc.returncode != 0 and "no_such_relation" in proc.stderr, proc.stderr
    finally:
        drop_test_tenant(state, tenant)


class _LateWriterPool(TenantPool):
    """A pool whose engine eviction step also commits a row, the way a background sweep that read before the wipe can."""

    def _evict(self, tenant: str) -> None:
        super()._evict(tenant)
        _sql(self.state, "INSERT INTO nexus.memory (tenant_id, project, title, content, timestamp) "
                         "VALUES (:'tenant', 'late', 'writer', 'c', now());", tenant=tenant)


def test_a_row_committed_in_the_wipe_to_census_gap_errors_the_leasing_test_and_retires_the_tenant(
    state: dict[str, Any], make_pool: Callable[..., TenantPool],
) -> None:
    pool = make_pool(_LateWriterPool)
    with pytest.raises(TenantLeakError, match=r"nexus\.memory=1") as exc:
        with pool.lease() as (leaked, _token):
            pass
    assert leaked in str(exc.value)
    assert pool.retired == 1 and pool.tenant is None
    # the retired tenant's leaves are gone; the next lease mints a different tenant
    assert _sql(state, "SELECT count(*) FROM pg_catalog.pg_class c WHERE c.relispartition "
                       "AND nexus.partition_bound_value(c.oid) = :'tenant';", tenant=leaked) == "0"
    with pytest.raises(TenantLeakError):
        with pool.lease() as (next_tenant, _t):
            assert next_tenant != leaked
    assert pool.mints == 2


def test_a_teardown_failure_never_replaces_the_tests_own_exception(
    state: dict[str, Any], make_pool: Callable[..., TenantPool],
) -> None:
    pool = make_pool(_LateWriterPool)
    with pytest.raises(ZeroDivisionError):
        with pool.lease():
            1 / 0  # noqa: B018
    assert pool.retired == 1  # the leak was still found and the tenant still retired


# --- the pool reuses ONE tenant, and a reset costs no DDL ----------------------------------------------------


def _ddl_total(state: dict[str, Any]) -> int:
    text = (Path(state["pgdata"]) / "pg.log").read_text(errors="replace")
    return len(_ddl_counter.parse_log(text).entries)


def test_one_tenant_serves_every_lease_and_a_reset_runs_no_ddl(
    state: dict[str, Any], make_pool: Callable[..., TenantPool],
) -> None:
    if os.environ.get(_ddl_counter.COUNT_ENV) == "0":
        pytest.skip(f"{_ddl_counter.COUNT_ENV}=0: the DDL counter is off by the operator's choice, so there is no log to read")
    # positive control: the counter sees a mint and a drop, and a lot of them
    before = _ddl_total(state)
    tenant, _ = mint_test_tenant(state)
    drop_test_tenant(state, tenant)
    seen = _ddl_total(state) - before
    assert seen >= 50, f"the DDL counter saw {seen} statements for a mint and a drop; it cannot prove a delta of 0"

    pool = make_pool()
    with pool.lease() as (first, token):  # the mint is the one DDL burst
        _register(state, token)
    before = _ddl_total(state)
    tenants = set()
    for _ in range(3):
        with pool.lease() as (t, token):
            tenants.add(t)
            assert _register(state, token).status_code == 200
    assert tenants == {first} and pool.leases == 4 and pool.mints == 1
    assert _ddl_total(state) - before == 0, "a lease and its reset must not run DDL"


# --- the engine half: each scenario with its control -------------------------------------------------------


def test_the_old_bearer_is_refused_after_the_reset_and_the_new_one_works(
    state: dict[str, Any], make_pool: Callable[..., TenantPool],
) -> None:
    results = {}
    for evict in (True, False):
        pool = make_pool(evict=evict)
        with pool.lease() as (_t, old):
            assert _whoami(state, old) == 200  # primes the engine's token cache
        with pool.lease() as (_t, new):
            assert new != old
            assert _whoami(state, new) == 200
            results[evict] = _whoami(state, old)
    assert results[True] == 401
    assert results[False] == 200, "control: without the eviction the token cache still answers for the deleted token"


def test_a_parked_tuple_waiter_is_aborted_by_the_reset(
    state: dict[str, Any], make_pool: Callable[..., TenantPool],
) -> None:
    outcome: dict[bool, tuple[int, str, float]] = {}
    for evict, timeout_s in ((True, 20), (False, 2)):
        pool = make_pool(evict=evict)
        done: dict[str, Any] = {}

        def park(token: str, sub: str = f"ledger/park-{evict}") -> None:
            r = _post(state, "/v1/tuples/rd", token, {"subspace": sub, "timeout_s": timeout_s}, timeout=60)
            done["r"] = (r.status_code, r.text, time.monotonic())

        with pool.lease() as (_t, token):
            waiter = threading.Thread(target=park, args=(token,))
            waiter.start()
            time.sleep(0.8)  # let the read register and park
            start = time.monotonic()
        waiter.join(30)
        assert not waiter.is_alive()
        status, text, ended = done["r"]
        outcome[evict] = (status, text, ended - start)
    status, text, elapsed = outcome[True]
    assert status == 409 and "TenantReset" in text, (status, text)
    assert elapsed < 10, f"the aborted waiter must not ride out its 20 s timeout ({elapsed:.1f}s)"
    status, text, elapsed = outcome[False]
    assert status != 409 and elapsed > 0.8, f"control: without the eviction the waiter is not aborted ({status}, {text})"


def test_a_collection_registered_in_lease_a_is_absent_in_lease_b(
    state: dict[str, Any], make_pool: Callable[..., TenantPool],
) -> None:
    seen: dict[bool, tuple[str, str, str]] = {}
    for evict in (True, False):
        pool = make_pool(evict=evict)
        with pool.lease() as (_t, token):
            before = _write_reason(state, token)
            assert _register(state, token).status_code == 200
            after = _write_reason(state, token)
        with pool.lease() as (_t, token):
            seen[evict] = (before, after, _write_reason(state, token))
    before, after, in_b = seen[True]
    assert before == UNREGISTERED, "a never-registered collection is refused as unregistered"
    assert after != UNREGISTERED, "registered in lease A, the engine accepts it as registered"
    assert in_b == UNREGISTERED, "lease B must not inherit lease A's registration"
    assert seen[False][2] != UNREGISTERED, (
        "control: with the eviction skipped the engine's registry still believes the collection exists, so this "
        "test would pass for the wrong reason if the reset did not evict")


def _ghost_markers(state: dict[str, Any], tenant: str) -> int:
    return int(_sql(state, f"SELECT count(*) FROM nexus.catalog_meta WHERE tenant_id = :'tenant' AND key = '{GHOST_KEY}';",
                    tenant=tenant))


def _await_marker(state: dict[str, Any], tenant: str, want: int) -> int:
    deadline = time.monotonic() + 10
    count = _ghost_markers(state, tenant)
    while count != want and time.monotonic() < deadline:
        time.sleep(0.2)
        count = _ghost_markers(state, tenant)
    return count


def test_the_ghost_sweep_marker_is_recreated_in_the_next_lease(
    state: dict[str, Any], make_pool: Callable[..., TenantPool],
) -> None:
    seen: dict[bool, tuple[int, int]] = {}
    for evict in (True, False):
        pool = make_pool(evict=evict)
        with pool.lease() as (tenant, token):
            assert _whoami(state, token) == 200  # the tenant's first authenticated request runs the sweep
            in_a = _await_marker(state, tenant, 1)
        with pool.lease() as (tenant, token):
            assert _ghost_markers(state, tenant) == 0, "the reset deleted the marker row"
            assert _whoami(state, token) == 200
            seen[evict] = (in_a, _await_marker(state, tenant, 1))
    assert seen[True] == (1, 1), "evicted: lease B's first request re-sweeps and writes the marker again"
    assert seen[False] == (1, 0), "control: not evicted, the engine believes the sweep is done and writes nothing"


def test_the_mint_burst_is_restored_by_the_reset(state: dict[str, Any], make_pool: Callable[..., TenantPool]) -> None:
    """A mint credential (scope 'mint') bound to ANOTHER tenant mints data tokens for the pool tenant; its per-tenant
    bucket (burst 5) is exhausted in lease A. The credential survives the reset, so only the engine's bucket for the
    pool tenant decides lease B's first mint."""
    other, _ = mint_test_tenant(state)
    try:
        seen: dict[bool, tuple[list[int], int]] = {}
        for evict in (True, False):
            # one credential per run: the credential's GLOBAL bucket (10/min) must not be what refuses the control
            issued = _post(state, "/v1/service-tokens/issue", state["bearer"], {"tenant": other, "scope": "mint"})
            assert issued.status_code == 200, issued.text
            mint_credential = issued.json()["token"]
            pool = make_pool(evict=evict)
            with pool.lease() as (tenant, _token):
                burst = [_post(state, "/v1/data-tokens/mint", mint_credential, {"tenant": tenant}).status_code
                         for _ in range(6)]
            with pool.lease() as (tenant, _token):
                seen[evict] = (burst, _post(state, "/v1/data-tokens/mint", mint_credential,
                                            {"tenant": tenant}).status_code)
        assert seen[True][0] == [200] * 5 + [429], f"the burst of 5 is exhausted in lease A: {seen[True][0]}"
        assert seen[False][0] == [200] * 5 + [429], f"and in the control's lease A: {seen[False][0]}"
        assert seen[True][1] == 200, "evicted: lease B starts with the pool tenant's full burst"
        assert seen[False][1] == 429, "control: without the eviction the exhausted bucket is still there"
    finally:
        drop_test_tenant(state, other)


# --- Python caches --------------------------------------------------------------------------------------


def test_python_client_caches_are_reset_on_every_lease(
    state: dict[str, Any], make_pool: Callable[..., TenantPool], monkeypatch: pytest.MonkeyPatch,
) -> None:
    resets: list[str] = []
    monkeypatch.setattr(http_vector_client, "reset_http_vector_client_for_tests", lambda: resets.append("client"))

    def leak() -> None:
        search_engine._poisoned_collections.add("code__leaked__x__v1")
        aspect_extractor._ENGINE_ASPECTS_ENABLED_CACHE["code__leaked__x__v1"] = (time.monotonic(), True, True)
        http_vector_client._lease_cache = ("http://leaked", "tok")

    def clean() -> bool:
        return (not search_engine._poisoned_collections
                and not aspect_extractor._ENGINE_ASPECTS_ENABLED_CACHE
                and http_vector_client._lease_cache is None)

    monkeypatch.setattr(search_engine, "_poisoned_collections", set())
    monkeypatch.setattr(aspect_extractor, "_ENGINE_ASPECTS_ENABLED_CACHE", {})
    monkeypatch.setattr(http_vector_client, "_lease_cache", None)
    pool = make_pool()
    leak()
    assert not clean(), "control: the three caches hold state before the lease"
    with pool.lease():
        assert clean(), "a lease starts with the caches a previous test left emptied"
        leak()  # this test pollutes them
    assert clean(), "and the lease's end empties what the test filled"
    assert resets == ["client"] * 2  # one reset when the lease starts and one when it ends


def test_the_slot_clients_are_rebuilt_for_each_lease(
    state: dict[str, Any], make_pool: Callable[..., TenantPool], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The T2 slot keys its client on the resolved ``(url, token)``; a per-lease bearer makes the key differ, so the
    slot hands out a new client without any slot-specific reset."""
    slot: SharedClientSlot[object] = SharedClientSlot(
        object, lambda c: None, endpoint_key=mcp_infra._service_t2_endpoint_key_uncached)
    pool = make_pool()
    clients = []
    for _ in range(2):
        with pool.lease() as (_t, token), _storage_touch_guard.tenant_active():
            monkeypatch.setenv("NX_SERVICE_URL", state["base_url"])
            monkeypatch.setenv("NX_SERVICE_TOKEN", token)
            first, _w = slot.resolve_and_acquire()
            again, _w = slot.resolve_and_acquire()
            assert again is first, "inside one lease the slot keeps its client"
            slot.release(first, evict=False)
            slot.release(again, evict=False)
            clients.append(first)
    assert clients[0] is not clients[1], "a new lease's bearer makes the slot build a new client"
    slot.reset_for_tests()


# --- the switch, the opt-out marker, and the end-of-session accounting ---------------------------------------


class _Req:
    def __init__(self, marks: set[str]) -> None:
        self.node = self
        self.nodeid = "tests/x.py::test_x"
        self._marks = marks

    def get_closest_marker(self, name: str) -> object | None:
        return object() if name in self._marks else None


def test_the_pool_is_on_unless_the_flag_is_exactly_zero_and_the_marker_opts_a_test_out(monkeypatch: pytest.MonkeyPatch) -> None:
    for value, want in (("1", True), ("0", False), ("", True), ("true", True), ("2", True), ("00", True), (" 0", True)):
        assert _tenant_pool.enabled({_tenant_pool.ENV_FLAG: value}) is want, value
    assert _tenant_pool.enabled({}) is True, "the pool is on by default (nexus-7zloy)"

    monkeypatch.setattr(_tenant_pool, "get_pool", lambda s: type("P", (), {"lease": staticmethod(lambda owner="": "POOL")})())
    monkeypatch.setattr(_engine_substrate, "minted_test_tenant", lambda s: "MINT")
    monkeypatch.setitem(_tenant_pool._entries, "pooled", 0)
    monkeypatch.setitem(_tenant_pool._entries, "fresh", 0)
    state: dict[str, Any] = {}
    monkeypatch.setenv(_tenant_pool.ENV_FLAG, "0")
    assert _tenant_pool.tenant_lease(state, _Req(set())) == "MINT"
    assert _tenant_pool._entries == {"pooled": 0, "fresh": 0}, "off: nothing is counted"
    monkeypatch.delenv(_tenant_pool.ENV_FLAG, raising=False)
    assert _tenant_pool.tenant_lease(state, _Req(set())) == "POOL"
    assert _tenant_pool.tenant_lease(state, _Req({"fresh_tenant"})) == "MINT"
    assert _tenant_pool.tenant_lease(state, None) == "POOL"
    assert _tenant_pool._entries == {"pooled": 2, "fresh": 1}


def test_the_end_of_session_report_fails_a_dirty_pool_tenant_and_a_vacuous_run(
    state: dict[str, Any], make_pool: Callable[..., TenantPool], monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = make_pool()
    monkeypatch.setattr(_tenant_pool, "_pool", pool)
    monkeypatch.setitem(_tenant_pool._entries, "pooled", 0)
    monkeypatch.setitem(_tenant_pool._entries, "fresh", 0)
    with pool.lease():
        pass
    monkeypatch.setitem(_tenant_pool._entries, "pooled", 1)
    clean = _tenant_pool.worker_report()
    assert clean["violations"] == [] and clean["leases"] == 1 and clean["retired"] == 0
    # the allowance is exactly the bearer the pool holds: its row is there, and it is the only token row
    assert _sql(state, "SELECT count(*) FROM nexus.service_tokens WHERE tenant_id = :'tenant' "
                       "AND token_hash = :'hash';", tenant=pool.tenant, hash=_tenant_pool.token_hash(pool.token)) == "1"
    assert _sql(state, "SELECT count(*) FROM nexus.service_tokens WHERE tenant_id = :'tenant';", tenant=pool.tenant) == "1"

    # a SECOND token row (a late writer's mint) is a leak even though one is allowed
    _sql(state, "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label) VALUES ('f' || repeat('0', 63), :'tenant', 'x');",
         tenant=pool.tenant)
    dirty = _tenant_pool.worker_report()
    assert any("nexus.service_tokens=1" in v for v in dirty["violations"]), dirty
    _sql(state, "DELETE FROM nexus.service_tokens WHERE token_hash = 'f' || repeat('0', 63);")
    dirty = _tenant_pool.worker_report()
    assert dirty["violations"] == [], "control: with the extra row gone the report is clean again"
    _sql(state, "INSERT INTO nexus.memory (tenant_id, project, title, content, timestamp) "
                "VALUES (:'tenant', 'late', 'writer', 'c', now());", tenant=pool.tenant)
    dirty = _tenant_pool.worker_report()
    assert any("end-of-session census" in v and "nexus.memory=1" in v for v in dirty["violations"]), dirty

    monkeypatch.setattr(_tenant_pool, "_pool", None)
    monkeypatch.setitem(_tenant_pool._entries, "pooled", 5)
    vacuous = _tenant_pool.worker_report()
    assert any("no lease was taken" in v for v in vacuous["violations"]), vacuous

    merged = _tenant_pool.merge_reports([clean, dirty, vacuous])
    assert merged["workers"] == 3 and merged["leases"] == 2 and len(merged["violations"]) >= 2
    assert "VIOLATIONS=" in _tenant_pool.summary_line(merged)
    assert "VIOLATIONS" not in _tenant_pool.summary_line(_tenant_pool.merge_reports([clean]))


# --- a teardown failure of ANY kind retires the tenant (nexus-fpwj1 fix round) -------------------------------


class _PostFailsOnce(TenantPool):
    """A pool whose engine call on ``fail_path`` fails once: raises ``failure`` when it is an exception, else
    returns it as the response."""

    def __init__(self, state: dict[str, Any], *, fail_path: str, failure: Any, **kwargs: Any) -> None:
        super().__init__(state, **kwargs)
        self._fail_path = fail_path
        self._failure = failure
        self.failed = False

    def _post(self, path: str, body: dict[str, str]) -> Any:
        if path == self._fail_path and not self.failed:
            self.failed = True
            if isinstance(self._failure, Exception):
                raise self._failure
            return self._failure
        return super()._post(path, body)


def _failing_pool(case: str, make_pool: Callable[..., TenantPool], monkeypatch: pytest.MonkeyPatch) -> TenantPool:
    """A pool whose first reset dies in the way ``case`` names."""
    if case == "post_raises":
        return make_pool(_PostFailsOnce, fail_path="/v1/_test/tenant-reset",
                         failure=httpx.ConnectError("engine unreachable"))
    if case == "issue_token_bad_reply":
        return make_pool(_PostFailsOnce, fail_path="/v1/tenants/create",
                         failure=httpx.Response(200, text="<html>not json</html>"))
    assert case == "psql_timeout"
    real = _tenant_pool.psql
    calls: list[int] = []

    def flaky(state: Any, sql: str, **variables: str) -> Any:
        # the first call after the lease starts is the wipe of the first reset: the lease-start census has run by then
        if sql == _tenant_pool.WIPE_SQL and not calls:
            calls.append(1)
            raise subprocess.TimeoutExpired(cmd="psql", timeout=60)
        return real(state, sql, **variables)

    monkeypatch.setattr(_tenant_pool, "psql", flaky)
    return make_pool()


_FAILURE_CASES = [("post_raises", "ConnectError"), ("psql_timeout", "TimeoutExpired"),
                  ("issue_token_bad_reply", "JSONDecodeError")]


def _assert_retired_and_next_lease_is_fresh(state: dict[str, Any], pool: TenantPool, gone: str) -> None:
    assert pool.retired == 1 and pool.tenant is None and pool.token is None, "the failed tenant is retired"
    assert _sql(state, "SELECT count(*) FROM pg_catalog.pg_class c WHERE c.relispartition "
                       "AND nexus.partition_bound_value(c.oid) = :'tenant';", tenant=gone) == "0"
    with pool.lease() as (fresh, token):
        assert fresh != gone and pool.mints == 2, "the next lease mints a new tenant"
        assert _whoami(state, token) == 200, "and its bearer works"
        assert _register(state, token).status_code == 200
        assert _write_reason(state, token) != UNREGISTERED
    assert pool.retired == 1, "a clean lease after the failure resets cleanly"


@pytest.mark.parametrize(("case", "cause"), _FAILURE_CASES)
def test_any_reset_failure_retires_the_tenant_and_errors_the_test_naming_the_cause(
    state: dict[str, Any], make_pool: Callable[..., TenantPool], monkeypatch: pytest.MonkeyPatch,
    case: str, cause: str,
) -> None:
    pool = _failing_pool(case, make_pool, monkeypatch)
    with pytest.raises(TenantLeakError, match=cause) as exc:
        with pool.lease() as (gone, token):
            _register(state, token)
    assert gone in str(exc.value), "the error names the tenant"
    _assert_retired_and_next_lease_is_fresh(state, pool, gone)


@pytest.mark.parametrize(("case", "cause"), _FAILURE_CASES)
def test_a_reset_failure_never_replaces_the_tests_own_exception_and_still_retires(
    state: dict[str, Any], make_pool: Callable[..., TenantPool], monkeypatch: pytest.MonkeyPatch,
    case: str, cause: str,
) -> None:
    pool = _failing_pool(case, make_pool, monkeypatch)
    gone = ""
    with capture_logs() as logs:
        with pytest.raises(ZeroDivisionError):
            with pool.lease() as (gone, _token):
                1 / 0  # noqa: B018
    warned = [e for e in logs if e.get("event") == "tenant_pool_teardown_failed_after_test_error"]
    assert len(warned) == 1 and cause in warned[0]["error"], logs
    _assert_retired_and_next_lease_is_fresh(state, pool, gone)


def test_a_second_concurrent_lease_on_one_pool_raises_and_the_first_lease_is_unharmed(
    state: dict[str, Any], make_pool: Callable[..., TenantPool],
) -> None:
    pool = make_pool()
    with pool.lease() as (tenant, token):
        _register(state, token)
        with pytest.raises(TenantLeaseBusyError):
            with pool.lease():
                pytest.fail("a second lease must not be handed out")
        assert pool.tenant == tenant and pool.token == token, "the refused lease touched nothing"
        assert _write_reason(state, token) != UNREGISTERED, "the first lease's data is still there"
    assert pool.retired == 0 and pool.leases == 1
    with pool.lease() as (again, _t):  # the flag was released: the pool serves the next test
        assert again == tenant


# --- the lease-start census ---------------------------------------------------------------------------------


def _late_row(state: dict[str, Any], tenant: str) -> None:
    _sql(state, "INSERT INTO nexus.memory (tenant_id, project, title, content, timestamp) "
                "VALUES (:'tenant', 'late', 'writer', 'c', now());", tenant=tenant)


def test_a_writer_between_one_teardown_and_the_next_setup_errors_the_new_test_naming_the_old_one(
    state: dict[str, Any], make_pool: Callable[..., TenantPool],
) -> None:
    pool = make_pool()
    with pool.lease(owner="tests/t.py::test_leaky") as (tenant, _t):
        pass
    _late_row(state, tenant)  # lands after test_leaky's teardown census, before test_victim's setup
    with pytest.raises(TenantLeakError, match=r"nexus\.memory=1") as exc:
        with pool.lease(owner="tests/t.py::test_victim"):
            pytest.fail("the new test's body must not run on a dirty tenant")
    text = str(exc.value)
    assert "tests/t.py::test_victim" in text and "tests/t.py::test_leaky" in text and "START of the lease" in text, text
    assert pool.retired == 1 and pool.tenant is None
    with pool.lease(owner="tests/t.py::test_next") as (fresh, _t):  # the retired tenant is replaced, the pool recovers
        assert fresh != tenant


def test_control_without_the_start_census_the_late_row_reaches_the_next_test_and_is_wiped_silently(
    state: dict[str, Any], make_pool: Callable[..., TenantPool],
) -> None:
    """What the pool did before the lease-start census: the next test's body sees the leaked row, and its teardown
    wipes it before its own census, so nothing is ever reported."""
    pool = make_pool(start_census=False)
    with pool.lease(owner="tests/t.py::test_leaky") as (tenant, _t):
        pass
    _late_row(state, tenant)
    with pool.lease(owner="tests/t.py::test_victim") as (same, _t):
        assert same == tenant
        assert _sql(state, "SELECT count(*) FROM nexus.memory WHERE tenant_id = :'tenant';", tenant=tenant) == "1"
    assert pool.retired == 0, "control: no error, no retire; the leak is invisible"


def test_a_fresh_mint_and_a_clean_reset_pass_the_start_census(
    state: dict[str, Any], make_pool: Callable[..., TenantPool],
) -> None:
    pool = make_pool()
    for _ in range(3):
        with pool.lease(owner="tests/t.py::test_ok"):
            pass
    assert pool.retired == 0 and pool.mints == 1 and pool.leases == 3


# --- teardown order, the census's pci_ clause, and the substrate posture -----------------------------------


class _Resp:
    status_code = 200
    text = ""

    def json(self) -> dict[str, Any]:
        return {"token": "reissued", "evicted": {}}


def test_reset_runs_wipe_then_evict_then_census_then_reissue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The order is the design: evicting before the wipe lets a request that lands between the two re-register
    state for rows about to vanish, and a census before the eviction proves nothing about the JVM. Stubbed psql and
    HTTP, so only the order is under test (no engine needed)."""
    order: list[str] = []

    class _Proc:
        returncode, stdout, stderr = 0, "", ""

    def fake_psql(state: Any, sql: str, **variables: str) -> Any:
        order.append("wipe" if sql == _tenant_pool.WIPE_SQL else "census" if sql == _tenant_pool.CENSUS_SQL else sql[:20])
        return _Proc()

    class _Recording(TenantPool):
        def _post(self, path: str, body: dict[str, str]) -> Any:
            order.append({"/v1/_test/tenant-reset": "evict", "/v1/tenants/create": "reissue"}.get(path, path))
            return _Resp()

    monkeypatch.setattr(_tenant_pool, "psql", fake_psql)
    pool = _Recording({"base_url": "http://x", "bearer": "b"}, mint=lambda s: ("t", "tok"), drop=lambda s, t: True)
    assert pool.reset("t") == "reissued"
    assert order == ["wipe", "evict", "census", "reissue"]


def test_the_census_names_a_pci_index_on_a_tenant_leaf(state: dict[str, Any]) -> None:
    """Positive control for the census's ``pci_`` clause: this engine runs with PCI off, so no ``pci_`` index ever
    exists, and a clause that cannot fire proves nothing. Create one by hand on a pool tenant's leaf."""
    tenant, _ = mint_test_tenant(state)
    try:
        _tenant_pool.wipe(state, tenant)
        _tenant_pool.census(state, tenant)  # control: no pci_ index, the census passes
        leaf = _sql(state, "SELECT c.oid::regclass::text FROM pg_catalog.pg_class c "
                           "WHERE c.relnamespace = 'nexus'::regnamespace AND c.relname LIKE 'chunks\\_m%' "
                           "AND c.relispartition AND nexus.partition_bound_value(c.oid) = :'tenant' ORDER BY 1 LIMIT 1;", tenant=tenant)
        assert leaf, "the tenant has a nexus.chunks leaf"
        _sql(state, f"CREATE INDEX pci_fpwj1_probe ON {leaf} (tenant_id);")
        with pytest.raises(TenantLeakError, match="pci_fpwj1_probe"):
            _tenant_pool.census(state, tenant)
        _sql(state, f"DROP INDEX {leaf.rsplit('.', 1)[0]}.pci_fpwj1_probe;")
        _tenant_pool.census(state, tenant)  # and it passes again once the index is gone
    finally:
        drop_test_tenant(state, tenant)


def test_the_substrate_engine_runs_with_pci_off_the_reset_route_on_and_the_reaper_off_when_the_pool_is_on(state: dict[str, Any]) -> None:
    """While the pool is on (the default) ``tests/_engine_substrate.py`` sets ``NX_SEARCH_PCI=0``,
    ``NX_TEST_TENANT_RESET_ROUTE=1`` and ``NX_REAPER_ENABLED=false``; the pool depends on all three (the route answers
    503 while PCI is on; a reaper pass writes ``gc_audit`` rows for a tenant between its wipe and its census, which
    failed the census in the nexus-7zloy A/B). Read them off the engine. The ``NX_TEST_TENANT_POOL=0`` posture
    (none of them) is ``test_tenant_pool_end_to_end.py``."""
    resp = httpx.get(f"{state['base_url']}/v1/status", headers=_hdr(state["bearer"]), timeout=30.0)
    assert resp.status_code == 200, resp.text
    status = resp.json()
    assert status["test_tenant_reset_route"] is True
    assert status["per_collection_indexes"]["this_engine"]["builder_state"] == "off"
    assert status["reaper"]["enabled"] is False


def test_the_end_of_session_line_reports_the_pool_tenants_leaf_bloat(
    state: dict[str, Any], make_pool: Callable[..., TenantPool], monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = make_pool()
    monkeypatch.setattr(_tenant_pool, "_pool", pool)
    with pool.lease() as (tenant, _t):
        _sql(state, "SET session_replication_role = replica; "
                    "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_768, embedding_model) "
                    "VALUES (:'tenant', 'c', decode(repeat('00', 32), 'hex'), 'x', "
                    "array_fill(0.1::real, ARRAY[768])::nexus.vector(768), 'bge-base-en-v15-768');", tenant=tenant)
    dead, live, size = pool.leaf_bloat() or (-1, -1, -1)
    assert dead >= 0 and live >= 0 and size > 0, "the reset left a chunk leaf that once held a row, so it has bytes"
    report = _tenant_pool.worker_report()
    assert report["leaf_bytes"] == size and report["leaf_dead_tup"] >= 0
    line = _tenant_pool.summary_line(_tenant_pool.merge_reports([report]))
    assert f"leaf_bytes={size}" in line and "leaf_dead_tup=" in line, line


def test_the_end_of_session_line_breaks_a_lease_down_by_step(
    make_pool: Callable[..., TenantPool], monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = make_pool()
    monkeypatch.setattr(_tenant_pool, "_pool", pool)
    with pool.lease():
        pass
    assert set(pool.step_s) == {"caches", "start_census", "wipe", "evict", "census", "reissue"}, pool.step_s
    assert all(sec > 0 for sec in pool.step_s.values()), pool.step_s
    merged = _tenant_pool.merge_reports([_tenant_pool.worker_report(), _tenant_pool.worker_report()])
    assert merged["step_s"]["wipe"] == pytest.approx(2 * pool.step_s["wipe"])
    line = _tenant_pool.summary_line(merged)
    assert "per_lease[" in line and all(f"{step}=" in line for step in pool.step_s), line
    assert "per_lease[" not in _tenant_pool.summary_line(_tenant_pool.merge_reports([]) | {"leases": 0}), "no leases, no breakdown"
