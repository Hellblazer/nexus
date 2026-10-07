# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-225: the doctor's RLS canary against the partitioned ``chunks`` and ``taxonomy_centroids``.

Both tables are LIST-partitioned by embedding_model, then tenant_id (vectors-030-1), so a
migrated database holds a relation per model and a leaf per (model, tenant) beside each parent,
every one with its own ``relrowsecurity`` / ``relforcerowsecurity`` and its own copy of the policy.
``_check_rls_present`` joins ``_RLS_TENANT_TABLES`` to ``pg_class`` and ``pg_policies`` by EXACT
relation name, so a leaf (``chunks_m<hash>_t_<hash>``) can never match a listed name and cannot
inflate a policy count. These tests run the real check against the real migrated schema and pin
that: one row per listed table, the two parents reported on their own flags and their own
policies, and a result that is not fatal. Bead nexus-3wh8d.16 adds the partitions and leaves: the
canary's second query walks both trees through ``pg_inherits`` and judges every model partition and
tenant leaf against its parent (RLS enabled, forced, the parent's permissive policies), so a single
leaf that lost FORCE or a policy is a fatal result naming it. The defects are injected as the schema
owner on a throwaway tenant's leaf and repaired in a ``finally``.

Non-vacuity: the substrate really has partitions of both parents, and the leaves carry policies of
their own (a leaf policy summed into the parent's count is the miscount this guards).
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from nexus.health import _RLS_TENANT_TABLES, _check_rls_present

pytestmark = [pytest.mark.integration]

_PARENTS = ("chunks", "taxonomy_centroids")


def _psql_rows(state: dict, sql: str) -> list[str]:
    proc = subprocess.run(
        [str(Path(state["pg_bin"]) / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", state["pg_user"], "-d", state["pg_dbname"], "-v", "ON_ERROR_STOP=1", "-t", "-A", "-c", sql],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"psql failed: {proc.stderr}\nSQL: {sql}"
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


@pytest.fixture(scope="module")
def state() -> dict:
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 — substrate boots lazily

    return ensure_engine()


def test_the_substrate_has_leaves_that_carry_their_own_policies(state: dict) -> None:
    """Non-vacuity: leaves exist under both parents and have policy rows of their own."""
    for parent in _PARENTS:
        leaves = _psql_rows(
            state,
            "SELECT count(*) FROM pg_inherits i JOIN pg_class ch ON ch.oid = i.inhrelid "
            "JOIN pg_class pr ON pr.oid = i.inhparent JOIN pg_namespace n ON n.oid = pr.relnamespace "
            f"WHERE n.nspname = 'nexus' AND pr.relname = '{parent}';",
        )
        assert int(leaves[0]) >= 1, f"nexus.{parent} has no partitions on this substrate"
    leaf_policies = _psql_rows(
        state,
        "SELECT count(*) FROM pg_policies WHERE schemaname = 'nexus' "
        "AND (tablename LIKE 'chunks\\_m%' OR tablename LIKE 'taxonomy\\_centroids\\_m%');",
    )
    assert int(leaf_policies[0]) >= 1, (
        "no partition carries a policy: the leaf-miscount check below would pass vacuously"
    )


def test_canary_reports_each_partitioned_parent_once_on_its_own_flags(state: dict) -> None:
    """The check's query returns exactly one row per listed table, and for each partitioned parent
    the flags and the policy count are the parent's own, never a sum over its partitions."""
    captured: dict[str, str] = {}

    def runner(cmd, **_kw):
        sql = cmd[cmd.index("-c") + 1]
        if "VALUES" in sql:                  # the per-table query; the partition-tree walk is the second
            captured["sql"] = sql
        cmd = list(cmd)
        cmd[cmd.index("-U") + 1] = state["pg_user"]
        cmd[cmd.index("-d") + 1] = state["pg_dbname"]
        cmd[cmd.index("-p") + 1] = str(state["pg_port"])
        return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)

    with tempfile.TemporaryDirectory() as tmp:
        creds = Path(tmp) / "pg_credentials"
        creds.write_text(
            f"PG_PORT={state['pg_port']}\n"
            f"NX_DB_ADMIN_URL=jdbc:postgresql://127.0.0.1:{state['pg_port']}/{state['pg_dbname']}\n"
            "NX_DB_ADMIN_USER=nexus_admin\nNX_DB_ADMIN_PASS=unused\n",
            encoding="utf-8",
        )
        results = _check_rls_present(creds_path=creds, psql_bin=Path(state["pg_bin"]) / "psql", psql_runner=runner)

    assert len(results) == 1
    result = results[0]
    assert not result.fatal, f"the RLS canary is fatal on the migrated schema: {result.detail}"
    for parent in _PARENTS:
        assert "RLS missing" not in result.detail and "does not mirror" not in result.detail, (
            f"canary reports a gap on a healthy schema: {result.detail}"
        )

    rows = _psql_rows(state, captured["sql"])
    assert len(rows) == len(_RLS_TENANT_TABLES), (
        f"{len(rows)} rows for {len(_RLS_TENANT_TABLES)} listed tables: a partition matched a listed name"
    )
    by_table = {r.split("|")[1]: r.split("|") for r in rows}
    for parent in _PARENTS:
        _schema, _name, rls_on, rls_force, policy_count = by_table[parent]
        assert (rls_on, rls_force) == ("t", "t"), f"nexus.{parent}: RLS flags {rls_on}/{rls_force}"
        own = _psql_rows(
            state, f"SELECT count(*) FROM pg_policy WHERE polrelid = 'nexus.{parent}'::regclass;",
        )
        assert int(policy_count) == int(own[0]) >= 1, (
            f"nexus.{parent}: canary counts {policy_count} policies, the parent itself has {own[0]}"
        )


# ── nexus-3wh8d.16: every model partition and leaf ───────────────────────────────────────────


def _run_canary(state: dict):
    from nexus.health import _check_rls_present  # noqa: PLC0415 — substrate-dependent

    def runner(cmd, **_kw):
        cmd = list(cmd)
        cmd[cmd.index("-U") + 1] = state["pg_user"]
        return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)

    with tempfile.TemporaryDirectory() as tmp:
        creds = Path(tmp) / "pg_credentials"
        creds.write_text(
            f"PG_PORT={state['pg_port']}\n"
            f"NX_DB_ADMIN_URL=jdbc:postgresql://127.0.0.1:{state['pg_port']}/{state['pg_dbname']}\n"
            "NX_DB_ADMIN_USER=nexus_admin\nNX_DB_ADMIN_PASS=unused\n",
            encoding="utf-8",
        )
        results = _check_rls_present(creds_path=creds, psql_bin=Path(state["pg_bin"]) / "psql", psql_runner=runner)
    assert len(results) == 1
    return results[0]


def _tree_relation_count(state: dict) -> int:
    rows = _psql_rows(
        state,
        "WITH RECURSIVE t AS (SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'nexus' AND c.relkind = 'p' AND c.relname IN ('chunks', 'taxonomy_centroids') "
        "UNION ALL SELECT i.inhrelid FROM t JOIN pg_inherits i ON i.inhparent = t.oid) SELECT count(*) FROM t;",
    )
    return int(rows[0])


def _a_leaf_of(state: dict, tenant: str, parent: str) -> tuple[str, str]:
    """(model partition, leaf) of ``tenant`` under ``parent``, by bound, never by name."""
    rows = _psql_rows(
        state,
        "SELECT mp.relname || '|' || lf.relname FROM pg_class p "
        "JOIN pg_inherits i ON i.inhparent = p.oid JOIN pg_class mp ON mp.oid = i.inhrelid "
        "JOIN pg_inherits j ON j.inhparent = mp.oid JOIN pg_class lf ON lf.oid = j.inhrelid "
        f"WHERE p.relname = '{parent}' AND p.relnamespace = 'nexus'::regnamespace "
        f"AND nexus.partition_bound_value(lf.oid) = '{tenant}' ORDER BY mp.relname LIMIT 1;",
    )
    assert rows, f"tenant {tenant} has no leaf under nexus.{parent}"
    model_partition, leaf = rows[0].split("|")
    return model_partition, leaf


def test_the_canary_reads_every_relation_of_both_trees_on_a_healthy_schema(state: dict) -> None:
    """Non-vacuity: the detail's relation count equals an independent count of both trees and exceeds the two
    parents, so a canary that read only the parents (2) could not produce it."""
    result = _run_canary(state)
    assert result.ok and not result.fatal, result.detail
    expected = _tree_relation_count(state)
    assert expected > len(_PARENTS), "the substrate has no partitions to walk"
    assert f"all {expected} relations" in result.detail, result.detail


@pytest.mark.parametrize("parent", _PARENTS)
@pytest.mark.parametrize("level", ["model_partition", "leaf"])
@pytest.mark.parametrize("defect", ["no_force", "no_rls", "policy_dropped", "policy_widened"])
def test_a_defect_on_one_partition_or_leaf_is_a_fatal_canary_naming_it(
    state: dict, parent: str, level: str, defect: str,
) -> None:
    from tests._engine_substrate import minted_test_tenant  # noqa: PLC0415 — substrate-dependent

    with minted_test_tenant(state) as (tenant, _token):
        model_partition, leaf = _a_leaf_of(state, tenant, parent)
        target = leaf if level == "leaf" else model_partition
        inject, repair = {
            "no_force": (f"ALTER TABLE nexus.{target} NO FORCE ROW LEVEL SECURITY",
                         f"ALTER TABLE nexus.{target} FORCE ROW LEVEL SECURITY"),
            "no_rls": (f"ALTER TABLE nexus.{target} DISABLE ROW LEVEL SECURITY",
                       f"ALTER TABLE nexus.{target} ENABLE ROW LEVEL SECURITY"),
            "policy_dropped": (f"DROP POLICY tenant_isolation ON nexus.{target}", None),
            "policy_widened": (f"ALTER POLICY tenant_isolation ON nexus.{target} USING (true)", None),
        }[defect]
        # Control: clean immediately before the injection.
        clean = _run_canary(state)
        assert clean.ok, clean.detail
        _psql_rows(state, inject + ";")
        try:
            result = _run_canary(state)
            assert result.fatal and not result.ok, f"{defect} on {target} was not caught: {result.detail}"
            assert target in result.detail, result.detail
        finally:
            if repair:
                _psql_rows(state, repair + ";")
            else:
                _psql_rows(state, f"SELECT nexus.partition_sync_access('nexus.{parent}'::regclass);")
        repaired = _run_canary(state)
        assert repaired.ok and not repaired.fatal, f"repair did not clear the canary: {repaired.detail}"


def test_a_stray_permissive_policy_on_a_leaf_is_fatal(state: dict) -> None:
    from tests._engine_substrate import minted_test_tenant  # noqa: PLC0415 — substrate-dependent

    with minted_test_tenant(state) as (tenant, _token):
        _model_partition, leaf = _a_leaf_of(state, tenant, "chunks")
        _psql_rows(state, f"CREATE POLICY rogue_p16 ON nexus.{leaf} FOR SELECT USING (true);")
        try:
            result = _run_canary(state)
            assert result.fatal and leaf in result.detail and "rogue_p16" in result.detail, result.detail
        finally:
            _psql_rows(state, f"DROP POLICY rogue_p16 ON nexus.{leaf};")
        assert _run_canary(state).ok
