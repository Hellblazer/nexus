# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-225 Day 2 (nexus-3wh8d.16): the three doctor rows against a real migrated schema.

``Tenant partitions`` (token tenants against leaves), ``Model partitions`` (``embedding_models`` against model
partitions) and ``Partition leaves`` (the count) run their real SQL here, through the substrate's PG and the same
psql path ``nx doctor`` uses. The unit file ``tests/test_health_partition_rows.py`` pins the parsing and the
wording with canned output; this file pins that the SQL means what the rows say.

Findings are produced on the real schema and cleared the way the row tells the operator to clear them:
the printed ``create_tenant_partitions`` statement is executed and the row must go green.
"""
from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

import nexus.health as h

pytestmark = [pytest.mark.integration]

_PARENTS = ("chunks", "taxonomy_centroids")


@pytest.fixture(scope="module")
def state() -> dict:
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 — substrate boots lazily

    return ensure_engine()


def _psql(state: dict, sql: str, *, dbname: str | None = None) -> list[str]:
    proc = subprocess.run(
        [str(Path(state["pg_bin"]) / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", state["pg_user"], "-d", dbname or state["pg_dbname"], "-v", "ON_ERROR_STOP=1", "-t", "-A", "-c", sql],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"psql failed: {proc.stderr}\nSQL: {sql}"
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _purge_leftover_tokens(state: dict) -> None:
    """Delete the tokens of tenants whose leaves are gone.

    ``nexus.drop_tenant_partitions`` removes a tenant's leaves and, by design, leaves its ``service_tokens`` rows for
    the runbook's later step, so every throwaway tenant the suite dropped is exactly what ``Tenant partitions``
    reports. Those leftovers are correct findings; they are removed here so a test judges its own tenant and not
    the order the suite ran in. Leaves of ``chunks`` identify the live tenants.
    """
    _psql(
        state,
        "DELETE FROM nexus.service_tokens st WHERE st.tenant_id <> 'default' AND NOT EXISTS ("
        "SELECT 1 FROM pg_class p JOIN pg_inherits i ON i.inhparent = p.oid "
        "JOIN pg_inherits j ON j.inhparent = i.inhrelid JOIN pg_class lf ON lf.oid = j.inhrelid "
        "WHERE p.relname = 'chunks' AND p.relnamespace = 'nexus'::regnamespace "
        "AND nexus.partition_bound_value(lf.oid) = st.tenant_id);",
    )


def _rows(state: dict, *, dbname: str | None = None) -> dict[str, h.HealthResult]:
    def runner(cmd, **_kw):
        cmd = list(cmd)
        cmd[cmd.index("-U") + 1] = state["pg_user"]
        return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)

    name = dbname or state["pg_dbname"]
    with tempfile.TemporaryDirectory() as tmp:
        creds = Path(tmp) / "pg_credentials"
        creds.write_text(
            f"PG_PORT={state['pg_port']}\n"
            f"NX_DB_ADMIN_URL=jdbc:postgresql://127.0.0.1:{state['pg_port']}/{name}\n"
            "NX_DB_ADMIN_USER=nexus_admin\nNX_DB_ADMIN_PASS=unused\n",
            encoding="utf-8",
        )
        results = h._check_tenant_model_partitions(
            creds_path=creds, psql_bin=Path(state["pg_bin"]) / "psql", psql_runner=runner,
        )
    assert tuple(r.label for r in results) == ("Tenant partitions", "Model partitions", "Partition leaves")
    return {r.label: r for r in results}


def test_a_healthy_schema_is_green_and_the_counts_match_the_catalog(state: dict) -> None:
    _purge_leftover_tokens(state)
    rows = _rows(state)
    for r in rows.values():
        assert r.ok and not r.warn and not r.fatal, (r.label, r.detail)

    token_tenants = int(_psql(state, "SELECT count(DISTINCT tenant_id) FROM nexus.service_tokens;")[0])
    models = int(_psql(state, "SELECT count(*) FROM nexus.embedding_models;")[0])
    leaves = {
        parent: int(_psql(
            state,
            "SELECT count(*) FROM pg_class p JOIN pg_inherits i ON i.inhparent = p.oid "
            "JOIN pg_inherits j ON j.inhparent = i.inhrelid "
            f"WHERE p.relname = '{parent}' AND p.relnamespace = 'nexus'::regnamespace;",
        )[0])
        for parent in _PARENTS
    }
    # Non-vacuity: there is something to compare, so green is not the empty case.
    assert token_tenants >= 1 and models >= 1 and all(n >= 1 for n in leaves.values())
    assert f"every one of {token_tenants} token tenant(s)" in rows["Tenant partitions"].detail
    assert f"every one of {models} embedding model(s)" in rows["Model partitions"].detail
    detail = rows["Partition leaves"].detail
    assert f"{sum(leaves.values())} leaves in all" in detail
    for parent, n in leaves.items():
        assert f"{parent}: {n} leaves" in detail


def test_a_tenant_with_a_token_and_a_missing_leaf_is_reported_and_the_printed_recovery_clears_it(state: dict) -> None:
    from tests._engine_substrate import minted_test_tenant  # noqa: PLC0415 — substrate-dependent

    _purge_leftover_tokens(state)
    with minted_test_tenant(state) as (tenant, _token):
        control = _rows(state)["Tenant partitions"]
        assert control.ok, f"control: a fresh tenant has every leaf: {control.detail}"

        model_partition, victim = _psql(
            state,
            "SELECT mp.relname || '|' || lf.relname FROM pg_class p JOIN pg_inherits i ON i.inhparent = p.oid "
            "JOIN pg_class mp ON mp.oid = i.inhrelid JOIN pg_inherits j ON j.inhparent = mp.oid "
            "JOIN pg_class lf ON lf.oid = j.inhrelid "
            f"WHERE p.relname = 'chunks' AND p.relnamespace = 'nexus'::regnamespace "
            f"AND nexus.partition_bound_value(lf.oid) = '{tenant}' ORDER BY mp.relname LIMIT 1;",
        )[0].split("|")
        # The runbook's own step: DETACH, then DROP (a referenced partition cannot be dropped in place).
        _psql(state, f"ALTER TABLE nexus.{model_partition} DETACH PARTITION nexus.{victim}; DROP TABLE nexus.{victim};")

        row = _rows(state)["Tenant partitions"]
        assert not row.ok and not row.warn, row.detail
        assert tenant in row.detail and "chunks" in row.detail, row.detail

        fix = next(f for f in row.fix_suggestions if f"'{tenant}'" in f)
        statement = fix.split(": ", 1)[1]
        assert "create_tenant_partitions" in statement
        _psql(state, statement)

        healed = _rows(state)["Tenant partitions"]
        assert healed.ok, f"the printed recovery did not clear the row: {healed.detail}"


def test_a_registered_model_with_no_partition_is_reported(state: dict) -> None:
    _purge_leftover_tokens(state)
    model = f"p16-nopartition-{uuid.uuid4().hex[:8]}"
    _psql(state, f"INSERT INTO nexus.embedding_models (embedding_model, dimension, provider) VALUES ('{model}', 768, 'test');")
    try:
        row = _rows(state)["Model partitions"]
        assert not row.ok and not row.warn, row.detail
        assert model in row.detail and "chunks" in row.detail and "taxonomy_centroids" in row.detail, row.detail
    finally:
        _psql(state, f"DELETE FROM nexus.embedding_models WHERE embedding_model = '{model}';")
    assert _rows(state)["Model partitions"].ok


def test_every_row_is_not_applicable_on_a_box_whose_parents_are_not_partitioned(state: dict) -> None:
    """The real catalog probe on a database that has a nexus.chunks and nothing partitioned: the virgin /
    pre-RDR-225 shape. Not a warning on any row."""
    db = f"p16_virgin_{uuid.uuid4().hex[:8]}"
    _psql(state, f"CREATE DATABASE {db};")
    try:
        _psql(state, "CREATE SCHEMA nexus; CREATE TABLE nexus.chunks (id int); CREATE TABLE nexus.taxonomy_centroids (id int);",
              dbname=db)
        rows = _rows(state, dbname=db)
        for r in rows.values():
            assert r.ok and not r.warn and not r.fatal, (r.label, r.detail)
            assert r.detail.startswith("not applicable"), (r.label, r.detail)
    finally:
        _psql(state, f"DROP DATABASE IF EXISTS {db};")
