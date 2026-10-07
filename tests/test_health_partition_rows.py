# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-225 Phase 2 Step 3 (nexus-3wh8d.16): the doctor's partition coverage.

Two things, both against an injected psql runner so they need no database:

* ``_check_rls_present`` also walks the partition trees of ``nexus.chunks`` and
  ``nexus.taxonomy_centroids`` (every model partition and every tenant leaf), because
  PostgreSQL inherits neither the row-security flags nor the policies down a tree.
* ``_check_tenant_model_partitions`` reports three rows: token tenants against leaves,
  ``embedding_models`` against model partitions, and the leaf count.

The real SQL against a real migrated schema is ``tests/db/test_doctor_partition_rows.py``
and ``tests/db/test_rls_canary_partitioned_tables.py``.
"""
from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from unittest.mock import patch

import nexus.health as h
from tests.test_health_service_checks import _ALL_TENANT_TABLES, _make_creds_file, _rls_row


def _sql_of(cmd: list[str]) -> str:
    return cmd[cmd.index("-c") + 1]


def _done(cmd, out: str = "", rc: int = 0, err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout=out, stderr=err)


# ── the RLS canary over the partition trees ──────────────────────────────────────────────────


def _canary_runner(tree_lines: list[str] | None, *, tree_rc: int = 0):
    """Table query -> every listed table healthy; tree query -> ``tree_lines`` (None: not partitioned)."""
    rows = "\n".join(_rls_row(t, "t", "t", 2) for t in sorted(_ALL_TENANT_TABLES)) + "\n"

    def runner(cmd, **_kw):
        sql = _sql_of(cmd)
        if "VALUES" in sql:
            return _done(cmd, rows)
        assert "pg_inherits" in sql, "the second query must be the partition-tree walk"
        return _done(cmd, "\n".join(tree_lines or []) + "\n", rc=tree_rc, err="boom" if tree_rc else "")

    return runner


def _canary(tmp_path: Path, runner) -> h.HealthResult:
    results = h._check_rls_present(
        creds_path=_make_creds_file(tmp_path), psql_bin=Path("/fake/psql"), psql_runner=runner,
    )
    assert len(results) == 1
    return results[0]


_CLEAN_TREE = ["N|chunks|13|||||", "N|taxonomy_centroids|13|||||"]


def test_clean_partition_trees_pass_and_the_detail_says_how_many_relations_were_read(tmp_path):
    r = _canary(tmp_path, _canary_runner(_CLEAN_TREE))
    assert r.ok and not r.fatal
    assert "26" in r.detail and "partition" in r.detail.lower()


def test_a_leaf_without_force_is_fatal_and_named(tmp_path):
    leaf = "chunks_m1a2b3c4d_t_00112233445566ff"
    r = _canary(tmp_path, _canary_runner(_CLEAN_TREE + [f"B|chunks|{leaf}|2|t|f||"]))
    assert not r.ok and r.fatal and not r.warn
    assert leaf in r.detail and "not forced" in r.detail
    assert any("partition_sync_access" in s for s in r.fix_suggestions)


def test_a_model_partition_without_rls_is_fatal(tmp_path):
    mp = "taxonomy_centroids_m0a90d9bc"
    r = _canary(tmp_path, _canary_runner(_CLEAN_TREE + [f"B|taxonomy_centroids|{mp}|1|f|t||"]))
    assert r.fatal and mp in r.detail and "not enabled" in r.detail


def test_a_leaf_missing_a_policy_or_carrying_a_stray_one_is_fatal(tmp_path):
    r = _canary(tmp_path, _canary_runner(_CLEAN_TREE + ["B|chunks|leaf_a|2|t|t|tenant_isolation|"]))
    assert r.fatal and "leaf_a" in r.detail and "tenant_isolation" in r.detail and "missing or different" in r.detail
    r = _canary(tmp_path, _canary_runner(_CLEAN_TREE + ["B|chunks|leaf_b|2|t|t||rogue"]))
    assert r.fatal and "leaf_b" in r.detail and "rogue" in r.detail


def test_the_parent_itself_failing_in_the_tree_walk_is_fatal(tmp_path):
    r = _canary(tmp_path, _canary_runner(_CLEAN_TREE + ["B|chunks|chunks|0|t|f||"]))
    assert r.fatal and "chunks" in r.detail


def test_the_gap_list_is_capped_but_counts_the_rest(tmp_path):
    bad = [f"B|chunks|leaf_{i}|2|t|f||" for i in range(9)]
    r = _canary(tmp_path, _canary_runner(_CLEAN_TREE + bad))
    assert r.fatal and "9" in r.detail and "more" in r.detail
    assert "leaf_8" not in r.detail


def test_a_failing_tree_query_is_fatal_not_silently_skipped(tmp_path):
    r = _canary(tmp_path, _canary_runner(None, tree_rc=1))
    assert r.fatal and "partition" in r.detail.lower()


def test_a_box_whose_parents_are_not_partitioned_has_nothing_more_to_check(tmp_path):
    """An engine that predates RDR-225: the tree query returns no rows, and the table verdict stands alone."""
    r = _canary(tmp_path, _canary_runner(None))
    assert r.ok and not r.fatal
    assert str(len(_ALL_TENANT_TABLES)) in r.detail


def test_tree_gaps_outrank_a_green_table_verdict_and_join_a_red_one(tmp_path):
    rows_bad = [_rls_row(t, "f" if t == "nexus.memory" else "t", "t", 2) for t in sorted(_ALL_TENANT_TABLES)]

    def runner(cmd, **_kw):
        if "VALUES" in _sql_of(cmd):
            return _done(cmd, "\n".join(rows_bad) + "\n")
        return _done(cmd, "\n".join(_CLEAN_TREE + ["B|chunks|leaf_c|2|t|f||"]) + "\n")

    r = _canary(tmp_path, runner)
    assert r.fatal and "nexus.memory" in r.detail and "leaf_c" in r.detail


# ── the three partition rows ─────────────────────────────────────────────────────────────────

LABELS = ("Tenant partitions", "Model partitions", "Partition leaves")


def _rows_runner(layout: list[str] | None, compare: list[str] | None = None, *, rc_layout: int = 0, rc_compare: int = 0):
    """First query (the layout probe, no registry reference) -> ``layout``; second (the comparison) -> ``compare``."""
    calls: list[str] = []

    def runner(cmd, **_kw):
        sql = _sql_of(cmd)
        calls.append(sql)
        if "service_tokens" in sql:
            return _done(cmd, "\n".join(compare or []) + "\n", rc=rc_compare, err="cmp-boom" if rc_compare else "")
        return _done(cmd, "\n".join(layout or []) + "\n", rc=rc_layout, err="probe-boom" if rc_layout else "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _rows(tmp_path: Path, runner) -> dict[str, h.HealthResult]:
    results = h._check_tenant_model_partitions(
        creds_path=_make_creds_file(tmp_path), psql_bin=Path("/fake/psql"), psql_runner=runner,
    )
    assert tuple(r.label for r in results) == LABELS
    return {r.label: r for r in results}


_LAYOUT = ["P|chunks|4|8", "P|taxonomy_centroids|4|8"]
_HEALTHY = ["K|2||", "R|4||"]


def test_every_row_is_not_applicable_without_pg_credentials(tmp_path):
    for local in (True, False):
        with patch("nexus.config.is_local_mode", return_value=local):
            results = h._check_tenant_model_partitions(creds_path=tmp_path / "pg_credentials")
        assert tuple(r.label for r in results) == LABELS
        for r in results:
            assert r.ok and not r.warn and not r.fatal
            assert r.detail.startswith("not applicable")


def test_every_row_is_not_applicable_when_the_parents_are_not_partitioned(tmp_path):
    runner = _rows_runner(layout=[])
    rows = _rows(tmp_path, runner)
    for r in rows.values():
        assert r.ok and not r.warn and r.detail.startswith("not applicable")
        assert "not partitioned" in r.detail
    assert len(runner.calls) == 1, "nothing to compare, so the registry and the tokens are never read"


def test_healthy_layout_reports_each_comparison_and_the_leaf_count(tmp_path):
    rows = _rows(tmp_path, _rows_runner(_LAYOUT, _HEALTHY))
    t, m, n = rows["Tenant partitions"], rows["Model partitions"], rows["Partition leaves"]
    assert t.ok and "2" in t.detail and "token tenant" in t.detail
    assert m.ok and "4" in m.detail and "embedding model" in m.detail
    assert n.ok and not n.warn
    assert "16" in n.detail                      # 8 + 8 leaves
    assert "chunks: 8" in n.detail and "taxonomy_centroids: 8" in n.detail
    assert "mint" in n.detail.lower()            # the operator-credential note


def test_a_tenant_with_a_token_and_a_missing_leaf_is_a_finding_with_the_recovery(tmp_path):
    compare = ["T|acme|chunks|voyage-code-3,minilm-l6-v2-384", "T|acme|taxonomy_centroids|voyage-code-3"] + _HEALTHY
    rows = _rows(tmp_path, _rows_runner(_LAYOUT, compare))
    t = rows["Tenant partitions"]
    assert not t.ok and not t.warn
    assert "acme" in t.detail and "chunks" in t.detail and "voyage-code-3" in t.detail
    fix = "\n".join(t.fix_suggestions)
    assert "create_tenant_partitions" in fix
    assert "'nexus.chunks'::regclass, 'acme'" in fix
    assert "'nexus.taxonomy_centroids'::regclass, 'acme'" in fix
    assert rows["Model partitions"].ok, "a tenant finding does not colour the model row"


def test_a_tenant_name_is_quoted_in_the_recovery_statement(tmp_path):
    compare = ["T|o'brien|chunks|voyage-code-3"] + _HEALTHY
    t = _rows(tmp_path, _rows_runner(_LAYOUT, compare))["Tenant partitions"]
    assert "'o''brien'" in "\n".join(t.fix_suggestions)


def test_a_model_with_no_partition_is_a_finding(tmp_path):
    compare = ["M|new-model-9|chunks|", "M|new-model-9|taxonomy_centroids|"] + _HEALTHY
    rows = _rows(tmp_path, _rows_runner(_LAYOUT, compare))
    m = rows["Model partitions"]
    assert not m.ok and not m.warn
    assert "new-model-9" in m.detail and "chunks" in m.detail and "taxonomy_centroids" in m.detail
    assert "create_model_partition" in "\n".join(m.fix_suggestions)
    assert rows["Tenant partitions"].ok


def test_the_leaf_count_row_stays_informational_beside_findings(tmp_path):
    compare = ["T|acme|chunks|voyage-code-3", "M|x|chunks|"] + _HEALTHY
    n = _rows(tmp_path, _rows_runner(_LAYOUT, compare))["Partition leaves"]
    assert n.ok and not n.warn


def test_no_token_tenants_and_no_models_are_each_not_applicable(tmp_path):
    rows = _rows(tmp_path, _rows_runner(_LAYOUT, ["K|0||", "R|0||"]))
    assert rows["Tenant partitions"].ok and rows["Tenant partitions"].detail.startswith("not applicable")
    assert rows["Model partitions"].ok and rows["Model partitions"].detail.startswith("not applicable")
    assert rows["Partition leaves"].ok


def test_a_failing_probe_or_comparison_warns_on_all_three_rows(tmp_path):
    for runner in (_rows_runner(_LAYOUT, rc_layout=1), _rows_runner(_LAYOUT, _HEALTHY, rc_compare=1)):
        rows = _rows(tmp_path, runner)
        for r in rows.values():
            assert not r.ok and r.warn and not r.fatal
            assert "psql exit" in r.detail


def test_the_rows_are_registered_after_the_rls_canary():
    src = inspect.getsource(h)
    assert src.index("results.extend(_check_rls_present())") < src.index(
        "results.extend(_check_tenant_model_partitions())"
    )
