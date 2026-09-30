# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keep a raw ``os.environ`` write from outliving its test (nexus-4h20a).

``monkeypatch.delenv(key, raising=False)`` records no undo when the key starts
absent, so a raw ``os.environ[key] = ...`` in the code under test outlives the
test. CI runs its shards serially, so a leaked value reaches every later test
in the shard.

A pytest plugin, registered from ``tests/conftest.py``'s ``pytest_plugins``,
not conftest fixtures, for two reasons. Plugin autouse fixtures are set up
before every conftest fixture and torn down after them, so the snapshot is
taken before ``_isolate_t1_sessions`` mints its token through ``monkeypatch``
and compared after that undo has run. And a ``pytester`` session can load this
module on its own, which is how ``tests/test_env_restore.py`` proves the
fixtures' behaviour instead of grepping for them.
"""
from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager

import pytest

#: Keys product code writes into ``os.environ`` directly, restored after every
#: test. ``NEXUS_PG_BIN``: ``nexus.commands.init._select_bundled_pg`` points
#: ``provision`` at the extracted bundle, and tests assert that write.
#: ``NX_T1_SESSION`` / ``NX_T1_SESSION_ID``: the MCP lease wiring
#: (``nexus.mcp.core``, ``nexus.db.http_scratch_store``) publishes the minted
#: session. Under ``NX_TEST_T2_SUBSTRATE=none`` nothing records their undo
#: (``_isolate_t1_sessions`` returns early), and one test was measured leaking
#: both (nexus-ye6cb).
PRODUCT_WRITTEN_ENV_KEYS: tuple[str, ...] = ("NEXUS_PG_BIN", "NX_T1_SESSION", "NX_T1_SESSION_ID")

#: The engine's DB-connection env, which a leaked value would hand to every
#: engine a later test spawns. A leak here FAILS the test (see
#: ``_no_leaked_engine_db_env``): no product code writes these.
ENGINE_DB_ENV_KEYS: tuple[str, ...] = (
    "NX_DB_URL", "NX_DB_USER", "NX_DB_PASS",
    "NX_DB_ADMIN_URL", "NX_DB_ADMIN_USER", "NX_DB_ADMIN_PASS",
)


def restore_changed_keys(before: dict[str, str | None]) -> dict[str, str | None]:
    """Put back every key in ``before`` whose value changed; return the changed
    keys mapped to the value that was found (``None`` for a deleted key)."""
    leaked = {k: os.environ.get(k) for k, v in before.items() if os.environ.get(k) != v}
    for k in leaked:
        old = before[k]
        if old is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = old
    return leaked


@contextmanager
def restore_env_after(*keys: str) -> Generator[None]:
    """Snapshot each of ``keys`` (or its absence) and restore it on exit."""
    before = {k: os.environ.get(k) for k in keys}
    try:
        yield
    finally:
        restore_changed_keys(before)


@pytest.fixture(autouse=True)
def _restore_product_written_env() -> Generator[None]:
    """Put :data:`PRODUCT_WRITTEN_ENV_KEYS` back after every test.

    Found as nexus-4h20a: a single-process ``pytest tests/daemon`` ran
    ``test_pg_bundle_install.py``'s round trip first, and
    ``test_storage_service_daemon_pg_monitor_backfill.py``'s fixture then took
    the leaked ``NEXUS_PG_BIN``, a fake bundle whose ``psql`` is ``exit 0``, as
    an operator override. Every query printed nothing and its revoke
    precondition failed. Restored rather than failed, because the write is the
    behaviour those tests assert.
    """
    with restore_env_after(*PRODUCT_WRITTEN_ENV_KEYS):
        yield


@pytest.fixture(autouse=True)
def _no_leaked_engine_db_env() -> Generator[None]:
    """Fail the test that leaves the engine's DB-connection env behind.

    Every engine a later test spawns from ``{**os.environ, ...}`` inherits
    these keys, and the engine reads ``NX_DB_ADMIN_*`` for its migration
    pool whenever they are set. ``tests/db/test_pg_provision_token.py``
    loaded a fake ``pg_credentials`` file into ``os.environ`` through
    ``pg_provision.load_service_credentials_into_env`` (since deleted, it had
    no production caller) and never took it back out, so in a
    single-process ``pytest tests/db`` every engine
    booted after it tried to migrate against the file's dead
    ``127.0.0.1:15999`` and exited on HikariPool fail-fast: 35 setup errors
    in ``test_xnz0o_commands_integration.py``, hidden under ``-n auto``
    because the two files usually land on different workers
    (tests-db-isolation, 2026-09-23).

    A key a test sets through ``monkeypatch`` is already restored when this
    compares (see the module docstring on ordering). What remains is a raw
    ``os.environ`` write. The fixture puts the old values back before failing,
    so one leak does not cascade.
    """
    before = {k: os.environ.get(k) for k in ENGINE_DB_ENV_KEYS}
    yield
    leaked = restore_changed_keys(before)
    if leaked:
        pytest.fail(
            "test left engine DB env changed in os.environ (restored now): "
            f"{sorted(leaked)}. An engine spawned later inherits these; set "
            "them with monkeypatch, or before code that writes os.environ "
            "directly call monkeypatch.setenv(key, 'x') then "
            "monkeypatch.delenv(key): that records the undo and leaves the key "
            "absent (delenv(key, raising=False) records no undo when the key "
            "starts absent; setenv(key, '') is not absent to the engine, which "
            "treats an empty NX_DB_ADMIN_* as set).",
            pytrace=False,
        )
