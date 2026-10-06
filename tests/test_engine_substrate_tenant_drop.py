"""RDR-225: the engine substrate drops a minted test tenant's partition leaves when the test is done.

Every minted tenant gets a leaf under every model partition of nexus.chunks and nexus.taxonomy_centroids
(8 leaves, about 216 relations), and the suite mints one per test. Left in place, a worker's PG grew past 2,000
tenants and the engine's token insert (bounded at 1 s x 3) answered 503 tenant_creation_busy to 6,106 tests.
``minted_test_tenant`` / ``drop_test_tenant`` call ``nexus.drop_tenant_partitions`` as the owning role.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from tests._engine_substrate import (
    drop_test_tenant,
    ensure_engine,
    minted_test_tenant,
    mint_test_tenant,
)


def _scalar(state: dict, sql: str, tenant: str) -> str:
    proc = subprocess.run(
        [str(Path(state["pg_bin"]) / "psql"), "-X", "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", state["pg_user"], "-d", state["pg_dbname"], "-t", "-A", "-v", "ON_ERROR_STOP=1",
         "-v", f"tenant={tenant}", "-f", "-"],
        input=sql, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"psql failed: {proc.stderr}\nSQL: {sql}"
    return proc.stdout.strip()


_LEAVES = (
    "SELECT count(*) FROM pg_catalog.pg_inherits i JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid "
    "WHERE nexus.partition_bound_value(c.oid) = :'tenant';"
)
_MODEL_PARTITIONS = (
    "SELECT count(*) FROM pg_catalog.pg_inherits i JOIN pg_catalog.pg_class p ON p.oid = i.inhparent "
    "JOIN pg_catalog.pg_namespace n ON n.oid = p.relnamespace "
    "WHERE n.nspname = 'nexus' AND p.relname IN ('chunks', 'taxonomy_centroids') AND :'tenant' <> '';"
)


def _leaf_count(state: dict, tenant: str) -> int:
    return int(_scalar(state, _LEAVES, tenant))


def test_a_minted_tenant_has_leaves_and_drop_test_tenant_removes_them() -> None:
    state = ensure_engine()
    tenant, _token = mint_test_tenant(state)
    expected = int(_scalar(state, _MODEL_PARTITIONS, tenant))
    assert expected >= 2
    assert _leaf_count(state, tenant) == expected

    assert drop_test_tenant(state, tenant) is True

    assert _leaf_count(state, tenant) == 0
    # idempotent: a second call finds nothing and still succeeds
    assert drop_test_tenant(state, tenant) is True


def test_minted_test_tenant_drops_on_a_clean_exit_and_when_the_body_raises() -> None:
    state = ensure_engine()
    with minted_test_tenant(state) as (tenant, token):
        assert token
        assert _leaf_count(state, tenant) > 0
    assert _leaf_count(state, tenant) == 0

    with pytest.raises(RuntimeError, match="the test's own failure"):
        with minted_test_tenant(state) as (failed_tenant, _t):
            assert _leaf_count(state, failed_tenant) > 0
            raise RuntimeError("the test's own failure")
    assert _leaf_count(state, failed_tenant) == 0


def test_a_drop_that_fails_is_logged_and_never_raises_or_hides_the_test_result() -> None:
    state = ensure_engine()
    broken = {**state, "pg_port": 1}  # nothing listens there
    with capture_logs() as logs:
        assert drop_test_tenant(broken, "anything") is False
    assert [e["event"] for e in logs] == ["test_tenant_drop_failed"]
    assert logs[0]["tenant"] == "anything"

    # the default tenant is refused by the function; the helper reports it the same way
    with capture_logs() as logs:
        assert drop_test_tenant(state, "default") is False
    assert [e["event"] for e in logs] == ["test_tenant_drop_failed"]
    assert _leaf_count(state, "default") > 0
