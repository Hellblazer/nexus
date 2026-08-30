# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Store-parametrized CRUD + auth contract for the T2 HTTP stores
(test-suite-compression P1d, nexus-test-cleanup 2026-08-05).

P1a-c consolidated the self-heal contract, the fake-HTTP-server boilerplate,
and the config/env-resolution contract across the T2 store family, but never
attempted the CRUD/error/auth layer for memory/telemetry/plan_library/
chash_index/scratch — each still carried a full set of granular
put-returns-id / get-roundtrips / get-missing-returns-none /
delete-then-delete-again classes, duplicated with the same shape and
different endpoint names. This file is that consolidation, following the
same case-binding-dataclass pattern P1a's ``SelfHealCase``
(``test_http_store_selfheal.py``) established.

TWO templates, not one:

  - ``CrudCase`` / ``test_put_get_upsert_delete_journey``: memory,
    plan_library, scratch. All three expose a synthetic-identifier CRUD
    surface (``put`` returns an id/uuid; ``get(identifier)`` round-trips;
    ``delete(identifier)`` is truthy once then falsy on repeat) — the
    genuinely shared shape. ``chash_index`` and ``telemetry`` do NOT fit
    this template (chash's identity is the natural ``(chash, collection)``
    key with no synthetic id and no per-row get; telemetry is an
    append/query log with no per-row delete) and are deliberately excluded
    — forcing them in would be exactly the "store-specific behaviour
    silently generalized" this task's brief warns against.
  - ``AuthCase`` / ``test_wrong_token_raises_cannot_self_heal``: telemetry,
    plan_library, chash_index — the three stores that both adopt
    ``RefreshableHttpStoreMixin`` AND were previously pinning this
    IDENTICAL one-line "wrong token -> RuntimeError('cannot self-heal')"
    test once each in their own file. ``test_t2_store_config_contract.py``
    (P1b) explicitly declined to fold this one, citing the cost of
    "importing three sibling test modules' private fake-handler classes
    for a ~15-line net reduction". That calculus differs here: this file's
    own CRUD cases ALREADY import ``plan_library``'s and ``chash_index``'s
    fake handlers for the journey template, so adding ``telemetry``'s makes
    the auth fold near-free. ``memory`` has no equivalent test (never
    pinned this behaviour) and ``scratch`` raises a DIFFERENT error shape
    (``RuntimeError`` matching "401", not "cannot self-heal" — it does not
    adopt the mixin, it has its own T1-session-lease self-heal, exercised
    separately in ``test_http_scratch_store.py::TestTokenRotationSelfHeal``)
    — both are genuine store-specific deltas, not omissions, and stay out.

Fake handlers are REUSED from each store's own unit-test module (not
reimplemented) — importing ``_Fake*Handler`` + the module's ``_STORE``/
``_ID_SEQ`` state directly. This is the same class of cross-file test-infra
coupling ``test_http_store_selfheal.py`` already carries (there, for its
OWN from-scratch lighter-weight fakes); here it additionally avoids a THIRD
reimplementation of five already-faithful, already-tested handlers. Each
case's ``reset_state`` clears the imported module's shared dict before the
case runs, mirroring that module's own ``autouse`` clear fixture — safe
because pytest runs one test file's tests at a time within a worker process
(no concurrent access to the same module-level dict).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import Any

import pytest

from nexus.db.http_scratch_store import DEFAULT_TENANT as SCRATCH_TENANT
from nexus.db.http_scratch_store import HttpScratchStore
from nexus.db.t2.http_chash_index import HttpChashIndex
from nexus.db.t2.http_memory_store import HttpMemoryStore
from nexus.db.t2.http_plan_library import HttpPlanLibrary
from nexus.db.t2.http_telemetry_store import HttpTelemetryStore
from tests.db._fake_t2_server import fake_http_server
from tests.db.test_http_chash_index import _FakeChashHandler
from tests.db.test_http_chash_index import _STORE as _CHASH_STORE
from tests.db.test_http_chash_index import _STORE_LOCK as _CHASH_LOCK
from tests.db.test_http_memory_store import TOKEN as MEM_TOKEN
from tests.db.test_http_memory_store import _FakeMemoryHandler
from tests.db.test_http_memory_store import _ID_SEQ as _MEM_ID_SEQ
from tests.db.test_http_memory_store import _STORE as _MEM_STORE
from tests.db.test_http_memory_store import _STORE_LOCK as _MEM_LOCK
from tests.db.test_http_plan_library import TOKEN as PLAN_TOKEN
from tests.db.test_http_plan_library import _FakePlanHandler
from tests.db.test_http_plan_library import _ID_SEQ as _PLAN_ID_SEQ
from tests.db.test_http_plan_library import _STORE as _PLAN_STORE
from tests.db.test_http_plan_library import _STORE_LOCK as _PLAN_LOCK
from tests.db.test_http_scratch_store import SESSION as SCRATCH_SESSION
from tests.db.test_http_scratch_store import TOKEN as SCRATCH_TOKEN
from tests.db.test_http_scratch_store import _FakeScratchHandler
from tests.db.test_http_scratch_store import _STORE as _SCRATCH_STORE
from tests.db.test_http_scratch_store import _STORE_LOCK as _SCRATCH_LOCK
from tests.db.test_http_telemetry_store import _clear_all as _reset_telemetry
from tests.db.test_http_telemetry_store import _FakeTelemetryHandler


# ── Shared-state reset helpers (mirror each module's own autouse fixture) ───


def _reset_memory() -> None:
    with _MEM_LOCK:
        _MEM_STORE.clear()
        _MEM_ID_SEQ[0] = 1


def _reset_plan_library() -> None:
    with _PLAN_LOCK:
        _PLAN_STORE.clear()
        _PLAN_ID_SEQ[0] = 0


def _reset_scratch() -> None:
    with _SCRATCH_LOCK:
        _SCRATCH_STORE.clear()


def _reset_chash() -> None:
    with _CHASH_LOCK:
        _CHASH_STORE.clear()


# ── Per-store identifier/content assertions (named, not inline lambdas) ────


def _assert_int_identifier(identifier: Any) -> None:
    assert isinstance(identifier, int) and identifier > 0


def _assert_uuid_identifier(identifier: Any) -> None:
    assert isinstance(identifier, str) and len(identifier) == 36


def _assert_memory_content(entry: Any, tag: str) -> None:
    assert entry is not None
    assert entry["content"] == f"crud-mem-content-{tag}"


def _assert_plan_content(entry: Any, tag: str) -> None:
    assert entry is not None
    assert entry["plan_json"] == f'{{"tag":"{tag}"}}'


def _assert_scratch_content(entry: Any, tag: str) -> None:
    assert entry is not None
    assert entry["content"] == f"crud-scratch-content-{tag}"


# ── CRUD journey contract (memory / plan_library / scratch) ────────────────


@dataclass(frozen=True)
class CrudCase:
    """One store's binding of the shared synthetic-identifier CRUD contract
    (put -> get roundtrip -> get-missing-is-None -> [upsert] -> delete ->
    delete-again) onto its own fake service and store class."""

    store_id: str
    handler_cls: type[BaseHTTPRequestHandler]
    reset_state: Callable[[], None]
    make_store: Callable[[str], Any]
    do_put: Callable[[Any, str], Any]
    assert_identifier: Callable[[Any], None]
    do_get: Callable[[Any, Any], Any]
    assert_content: Callable[[Any, str], None]
    do_get_missing: Callable[[Any], Any]
    supports_upsert: bool
    do_delete: Callable[[Any, Any], Any]


CRUD_CASES: list[CrudCase] = sorted(
    [
        CrudCase(
            store_id="memory",
            handler_cls=_FakeMemoryHandler,
            reset_state=_reset_memory,
            make_store=lambda url: HttpMemoryStore(base_url=url, _token=MEM_TOKEN),
            do_put=lambda store, tag: store.put(
                "crud-mem-proj", "crud-mem-key", f"crud-mem-content-{tag}", ttl=30
            ),
            assert_identifier=_assert_int_identifier,
            do_get=lambda store, identifier: store.get(id=identifier),
            assert_content=_assert_memory_content,
            do_get_missing=lambda store: store.get(
                project="crud-mem-proj", title="crud-mem-missing"
            ),
            supports_upsert=True,
            do_delete=lambda store, identifier: store.delete(id=identifier),
        ),
        CrudCase(
            store_id="plan_library",
            handler_cls=_FakePlanHandler,
            reset_state=_reset_plan_library,
            make_store=lambda url: HttpPlanLibrary(base_url=url, _token=PLAN_TOKEN),
            do_put=lambda store, tag: store.save_plan(verb="query", 
                query="crud-plan-query",
                plan_json=f'{{"tag":"{tag}"}}',
                project="crud-plan-proj",
            ),
            assert_identifier=_assert_int_identifier,
            do_get=lambda store, identifier: store.get_plan(identifier),
            assert_content=_assert_plan_content,
            do_get_missing=lambda store: store.get_plan(999999999),
            supports_upsert=True,
            do_delete=lambda store, identifier: store.delete_plan(identifier),
        ),
        CrudCase(
            store_id="scratch",
            handler_cls=_FakeScratchHandler,
            reset_state=_reset_scratch,
            make_store=lambda url: HttpScratchStore(
                base_url=url,
                tenant=SCRATCH_TENANT,
                session_id=SCRATCH_SESSION,
                _token=SCRATCH_TOKEN,
            ),
            do_put=lambda store, tag: store.put(f"crud-scratch-content-{tag}"),
            assert_identifier=_assert_uuid_identifier,
            do_get=lambda store, identifier: store.get(identifier),
            assert_content=_assert_scratch_content,
            do_get_missing=lambda store: store.get(
                "00000000-0000-0000-0000-000000000000"
            ),
            supports_upsert=False,
            do_delete=lambda store, identifier: store.delete(identifier),
        ),
    ],
    key=lambda case: case.store_id,
)


@pytest.mark.parametrize("case", CRUD_CASES, ids=[c.store_id for c in CRUD_CASES])
def test_put_get_upsert_delete_journey(case: CrudCase) -> None:
    """put -> get roundtrips content -> get(missing key) is None ->
    [same natural key upserts to the same identifier, content updates] ->
    delete is truthy -> repeat delete is falsy.

    Subsumes, per store: memory's ``test_put_returns_id`` /
    ``test_put_upsert_returns_same_id`` / ``test_get_by_id`` /
    ``test_get_missing_returns_none`` / ``test_delete_by_id``;
    plan_library's ``test_save_returns_id`` / ``test_save_conflict_upserts``
    / ``test_get_absent_returns_none`` / ``test_delete_existing`` /
    ``test_delete_absent_returns_zero``; scratch's ``test_put_returns_uuid``
    / ``test_get_returns_entry`` / ``test_get_absent_returns_none`` /
    ``test_delete_returns_true`` / ``test_delete_twice_returns_false``.
    """
    case.reset_state()
    with fake_http_server(case.handler_cls) as url:
        store = case.make_store(url)
        try:
            id1 = case.do_put(store, "1")
            case.assert_identifier(id1)

            entry = case.do_get(store, id1)
            case.assert_content(entry, "1")

            missing = case.do_get_missing(store)
            assert missing is None, f"{case.store_id}: expected None for a missing key"

            if case.supports_upsert:
                id2 = case.do_put(store, "2")
                assert id2 == id1, (
                    f"{case.store_id}: re-putting the same natural key must "
                    f"upsert to the same identifier, not mint a new one"
                )
                updated = case.do_get(store, id1)
                case.assert_content(updated, "2")

            first_delete = case.do_delete(store, id1)
            assert bool(first_delete), f"{case.store_id}: first delete must report success"

            post_delete = case.do_get(store, id1)
            assert post_delete is None, (
                f"{case.store_id}: get after delete must return None — a truthy "
                f"delete return is not proof the row is actually gone"
            )

            second_delete = case.do_delete(store, id1)
            assert not second_delete, (
                f"{case.store_id}: repeat delete of an already-removed row must "
                f"report failure, not succeed again"
            )
        finally:
            store.close()


# ── Wrong-token auth contract (telemetry / plan_library / chash_index) ─────


@dataclass(frozen=True)
class AuthCase:
    """One store's binding of the shared "fully-pinned wrong token raises"
    contract onto its own fake service and store class."""

    store_id: str
    handler_cls: type[BaseHTTPRequestHandler]
    reset_state: Callable[[], None]
    make_bad_store: Callable[[str], Any]
    do_write: Callable[[Any], None]


AUTH_CASES: list[AuthCase] = sorted(
    [
        AuthCase(
            store_id="chash_index",
            handler_cls=_FakeChashHandler,
            reset_state=_reset_chash,
            make_bad_store=lambda url: HttpChashIndex(base_url=url, _token="wrong-token"),
            do_write=lambda store: store.upsert(
                chash="auth-fail-chash", collection="auth-fail-coll"
            ),
        ),
        AuthCase(
            store_id="plan_library",
            handler_cls=_FakePlanHandler,
            reset_state=_reset_plan_library,
            make_bad_store=lambda url: HttpPlanLibrary(base_url=url, _token="wrong-token"),
            do_write=lambda store: store.save_plan(verb="query", query="auth-fail", plan_json="{}"),
        ),
        AuthCase(
            store_id="telemetry",
            handler_cls=_FakeTelemetryHandler,
            reset_state=_reset_telemetry,
            make_bad_store=lambda url: HttpTelemetryStore(base_url=url, _token="wrong-token"),
            do_write=lambda store: store.log_relevance("auth-fail-q", "auth-fail-c", "a"),
        ),
    ],
    key=lambda case: case.store_id,
)


@pytest.mark.parametrize("case", AUTH_CASES, ids=[c.store_id for c in AUTH_CASES])
def test_wrong_token_raises_cannot_self_heal(case: AuthCase) -> None:
    """A store constructed with a WRONG bearer token — both ``base_url`` and
    ``_token`` explicitly pinned, matching the per-store test-double
    construction pattern — must raise ``RuntimeError("cannot self-heal")``,
    not retry-and-succeed or silently swallow the 401.
    ``RefreshableHttpStoreMixin._invalidate_and_reresolve`` refuses to
    re-resolve a fully pinned instance: nothing it could change would fix a
    fully-pinned endpoint.

    Subsumes: chash_index's ``TestAuth.test_bad_token_raises``,
    plan_library's ``TestAuthErrors.test_wrong_token_raises``, telemetry's
    ``TestLogRelevance.test_log_sends_auth_headers``.
    """
    case.reset_state()
    with fake_http_server(case.handler_cls) as url:
        store = case.make_bad_store(url)
        try:
            with pytest.raises(RuntimeError, match="cannot self-heal"):
                case.do_write(store)
        finally:
            store.close()
