# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-225: the doctor's RLS canary against the partitioned ``chunks`` and ``taxonomy_centroids``.

Both tables are LIST-partitioned by embedding_model, then tenant_id (vectors-030-1), so a
migrated database holds a relation per model and a leaf per (model, tenant) beside each parent,
every one with its own ``relrowsecurity`` / ``relforcerowsecurity`` and its own copy of the policy.
``_check_rls_present`` joins ``_RLS_TENANT_TABLES`` to ``pg_class`` and ``pg_policies`` by EXACT
relation name, so a leaf (``chunks_m<hash>_t_<hash>``) can never match a listed name and cannot
inflate a policy count. These tests run the real check against the real migrated schema and pin
that: one row per listed table, the two parents reported on their own flags and their own
policies, and a result that is not fatal. Asserting the same on every partition and leaf is bead
nexus-3wh8d.16, not this file.

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
        captured["sql"] = cmd[cmd.index("-c") + 1]
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
        assert f"nexus.{parent} " not in result.detail, f"canary names nexus.{parent}: {result.detail}"

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
