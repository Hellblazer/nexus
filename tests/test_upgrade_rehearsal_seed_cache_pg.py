# SPDX-License-Identifier: AGPL-3.0-or-later
"""The seed cache's PG side, offline: the cluster started alone and stopped (against fake ``pg_ctl`` and
``pg_controldata`` scripts) and the SQL helpers (against a fake psql). The cache's own rules are pinned in
``tests/test_upgrade_rehearsal_seed_cache.py``, whose fixtures these tests share.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_upgrade_rehearsal_seed_cache import (
    NEW_PORT,
    OLD_PORT,
    ROWS,
    make_run,
    sc,
)

# --- the PG cluster, stopped: against a fake bundle -----------------------------------------------------------------

PG_CTL = """#!/bin/sh
echo "pg_ctl $*" >> "@LOG@"
case "$*" in
  *" start") touch "@PGDATA@/postmaster.pid"; [ -e "@STATE@/start_fails" ] && { rm "@PGDATA@/postmaster.pid"; echo "no" >&2; exit 1; }; exit 0 ;;
  *"-m fast"*) [ -e "@STATE@/stop_fails" ] && exit 1; rm -f "@PGDATA@/postmaster.pid"; echo "shut down" > "@STATE@/cluster"; exit 0 ;;
  *"-m immediate"*) rm -f "@PGDATA@/postmaster.pid"; exit 0 ;;
esac
"""

PG_CONTROLDATA = """#!/bin/sh
echo "pg_controldata $*" >> "@LOG@"
echo "Database cluster state:               $(cat "@STATE@/cluster")"
"""


@pytest.fixture
def fake_bundle(tmp_path: Path):
    run = make_run(tmp_path, "pgrun")
    config, state = run / "config", tmp_path / "state"
    state.mkdir()
    (state / "cluster").write_text("shut down")
    # the scripts run under the helper's own scrubbed environment, so the paths are written into them
    subs = {
        "@LOG@": str(state / "calls.log"),
        "@STATE@": str(state),
        "@PGDATA@": str(config / "postgres"),
    }
    for name, body in (("pg_ctl", PG_CTL), ("pg_controldata", PG_CONTROLDATA)):
        for k, v in subs.items():
            body = body.replace(k, v)
        f = config / "pg-bundle" / "bundle" / "bin" / name
        f.write_text(body)
        f.chmod(0o755)
    (state / "calls.log").write_text("")
    return config, state


def _calls(state: Path) -> list[str]:
    return (state / "calls.log").read_text().splitlines()


def test_a_cleanly_stopped_cluster_reads_as_clean(fake_bundle) -> None:
    config, state = fake_bundle
    assert sc.stopped_clean(config) is None
    (state / "cluster").write_text("in production")
    assert "'in production'" in sc.stopped_clean(config)
    (state / "cluster").write_text("shut down")
    (config / "postgres" / "postmaster.pid").write_text("1")
    assert "postmaster.pid" in sc.stopped_clean(config)


def test_the_work_runs_with_pg_started_alone_on_the_stores_port_and_stopped_cleanly(
    fake_bundle,
) -> None:
    config, state = fake_bundle
    seen: list[bool] = []
    out = sc.rows_with_pg_up(
        config,
        lambda: (
            seen.append((config / "postgres" / "postmaster.pid").exists()) or 86_000
        ),
    )
    assert out == 86_000 and seen == [True], "the work ran while PG was up"
    calls = _calls(state)
    assert [c.split()[0] for c in calls] == ["pg_ctl", "pg_ctl", "pg_controldata"]
    assert calls[0].endswith(f"-o -p {OLD_PORT} start"), calls[0]
    assert "-m fast" in calls[1]
    assert not (config / "postgres" / "postmaster.pid").exists()


def test_the_start_follows_the_ports_in_the_credentials(fake_bundle) -> None:
    config, state = fake_bundle
    creds = config / "pg_credentials"
    creds.write_text(
        creds.read_text().replace(f"PG_PORT={OLD_PORT}", f"PG_PORT={NEW_PORT}")
    )
    sc.rows_with_pg_up(config, lambda: 1)
    assert _calls(state)[0].endswith(f"-o -p {NEW_PORT} start")


def test_work_that_fails_still_stops_pg(fake_bundle) -> None:
    config, _ = fake_bundle

    def boom() -> int:
        raise RuntimeError("psql failed")

    with pytest.raises(RuntimeError):
        sc.rows_with_pg_up(config, boom)
    assert not (config / "postgres" / "postmaster.pid").exists()


def test_a_pg_that_does_not_start_is_stopped_and_an_error(fake_bundle) -> None:
    config, state = fake_bundle
    (state / "start_fails").write_text("1")
    with pytest.raises(sc.SeedCacheError, match="pg_ctl start failed"):
        sc.rows_with_pg_up(config, lambda: 1)
    calls = _calls(state)
    assert calls[0].endswith(" start") and any(
        "-m immediate" in c for c in calls[1:]
    ), "a start that failed is followed by a stop"


def test_a_pg_that_does_not_stop_is_forced_and_an_error(fake_bundle) -> None:
    config, state = fake_bundle
    (state / "stop_fails").write_text("1")
    with pytest.raises(sc.SeedCacheError, match="pg_ctl stop failed"):
        sc.rows_with_pg_up(config, lambda: 1)
    assert any("-m immediate" in c for c in _calls(state))


def test_a_cluster_that_reads_unclean_after_the_work_is_an_error(fake_bundle) -> None:
    config, _ = fake_bundle
    stop = config / "pg-bundle" / "bundle" / "bin" / "pg_ctl"
    stop.write_text(
        stop.read_text().replace('echo "shut down" >', 'echo "in crash recovery" >')
    )
    with pytest.raises(sc.SeedCacheError, match="after the row count"):
        sc.rows_with_pg_up(config, lambda: 1)


# --- the SQL helpers, against a fake psql -----------------------------------------------------------------------------


class FakePg:
    """Answers the helpers' queries from tables of column -> values."""

    def __init__(
        self, tables: dict[str, dict[str, list[str]]], rows: int = ROWS
    ) -> None:
        self.tables, self.rows, self.queries = tables, rows, []

    def __call__(self, q: str) -> list[list[str]]:
        self.queries.append(q)
        if q.startswith("select count(*)::bigint from nexus.chunks"):
            return [[str(self.rows)]]
        if q.startswith(
            "select table_name, column_name from information_schema.columns"
        ):
            wanted = set(re.findall(r"'([a-z_]+)'", q.split("column_name in")[1]))
            return [
                [t, c]
                for t, cols in sorted(self.tables.items())
                for c in sorted(cols)
                if c in wanted
            ]
        m = re.match(r'select count\(\*\)::bigint from nexus\."(\w+)" where (.*)', q)
        if m:
            t, cond = m.groups()
            col = re.search(r'in "(\w+)"\) > 0', cond).group(1)
            needles = [
                n.replace("''", "'")
                for n in re.findall(r"position\('((?:[^']|'')*)' in", cond)
            ]
            return [
                [str(sum(any(n in v for n in needles) for v in self.tables[t][col]))]
            ]
        m = re.match(
            r'update nexus\."(\w+)" set "(\w+)" = replace\("\w+", \'((?:[^\']|\'\')*)\', \'((?:[^\']|\'\')*)\'\)',
            q,
        )
        if m:
            t, col, old, new = m.groups()
            old, new = old.replace("''", "'"), new.replace("''", "'")
            self.tables[t][col] = [v.replace(old, new) for v in self.tables[t][col]]
            return []
        raise AssertionError(f"unexpected query: {q}")


def test_the_store_read_counts_rows_and_the_values_that_name_the_run_dir() -> None:
    pg = FakePg(
        {
            "catalog_owners": {"repo_root": ["/w/a/x", "/else"], "name": ["x", "y"]},
            "catalog_documents": {
                "source_uri": ["file:///w/a/x/1", "file:///w/a/x/2", "file:///else/3"]
            },
        }
    )
    got = sc.read_store(pg, ["/w/a/x"])
    assert got.rows == ROWS
    assert got.path_rows == {
        "catalog_owners.repo_root": 1,
        "catalog_documents.source_uri": 2,
    }
    asked = next(q for q in pg.queries if "information_schema" in q)
    for col in sc.PATH_COLUMNS:
        assert f"'{col}'" in asked
    assert "'name'" not in asked


def test_the_store_rewrite_moves_the_run_dir_in_every_path_column_and_proves_it() -> (
    None
):
    pg = FakePg(
        {
            "catalog_owners": {"repo_root": ["/w/a/x", "/else"]},
            "catalog_documents": {"source_uri": ["file:///w/a/x/1", "file:///w/a/x/2"]},
            "aspects": {"source_uri": ["file:///w/a/x/1"]},
        }
    )
    got = sc.rewrite_store(pg, [("/w/a/x", "/w/b/x")])
    assert got.before == {
        "aspects.source_uri": 1,
        "catalog_documents.source_uri": 2,
        "catalog_owners.repo_root": 1,
    }
    assert set(got.after.values()) == {0}
    assert pg.tables["catalog_owners"]["repo_root"] == ["/w/b/x", "/else"]
    assert pg.tables["catalog_documents"]["source_uri"] == [
        "file:///w/b/x/1",
        "file:///w/b/x/2",
    ]


def test_the_store_rewrite_escapes_quotes_and_skips_columns_that_name_nothing() -> None:
    pg = FakePg(
        {"catalog_owners": {"repo_root": ["/w/it's/x"], "file_path": ["rel/a"]}}
    )
    got = sc.rewrite_store(pg, [("/w/it's/x", "/w/new/x")])
    assert pg.tables["catalog_owners"]["repo_root"] == ["/w/new/x"]
    assert got.before == {"catalog_owners.file_path": 0, "catalog_owners.repo_root": 1}
    updates = [q for q in pg.queries if q.startswith("update")]
    assert len(updates) == 1 and "'/w/it''s/x'" in updates[0]


def test_a_reused_run_dir_is_counted_and_left_alone() -> None:
    pg = FakePg({"catalog_owners": {"repo_root": ["/w/a/x"]}})
    got = sc.rewrite_store(pg, [("/w/a/x", "/w/a/x")])
    assert got.before == {"catalog_owners.repo_root": 1} and got.after == {
        "catalog_owners.repo_root": 0
    }
    assert not [q for q in pg.queries if q.startswith("update")]


def test_an_empty_catalog_reads_as_no_path_rows() -> None:
    assert sc.read_store(FakePg({}), ["/w/a"]).path_rows == {}


def test_an_identifier_that_cannot_be_quoted_is_refused() -> None:
    with pytest.raises(sc.SeedCacheError):
        sc.rewrite_store(
            FakePg({'bad"table': {"repo_root": ["/w/a"]}}), [("/w/a", "/w/b")]
        )
