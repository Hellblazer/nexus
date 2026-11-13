# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd: the legacy-chash guard against real Postgres.

Throwaway databases inside the test substrate's cluster hold the shapes the engine's real eras produced (the
dim-sharded ``chunks_384/768/1024`` text era that has no ``nexus.chunks``, the bytea era, a partitioned
``nexus.chunks``). The tests check the text decode rule against the changelog's own USING expression, and build dumps
through ``nexus.backup.pg_backup.run_dump``, the verb's own library (tests may import it; the pin scans ``src``).
Nothing here starts an engine, and nothing runs ``nx backup``.

POSIX only: the Windows CI set has no engine substrate, so every test here would skip there. The module skips itself
on win32 and the Windows set leaves it out (tests/test_windows_release_legs_workflow.py), so the skip ceiling keeps
catching a suite that quietly stopped running."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.backup import pg_backup
from nexus.daemon import legacy_chash_guard as guard
from tests import _engine_substrate
from tests.daemon._legacy_chash_support import _RDR180_USING

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs the engine substrate; the Windows set has none")


# ── real Postgres: the engine's real eras, each in its own throwaway database ───────────────────────────────────


def _psql(state: dict, db: str, sql: str) -> str:
    out = subprocess.run(
        [str(Path(state["pg_bin"]) / "psql"), "-X", "-q", "-t", "-A", "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", state["pg_user"], "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[-300:])
    return out.stdout


@pytest.fixture(scope="module")
def substrate_cluster() -> dict:
    """The test substrate's Postgres cluster, without a tenant: these tests make their own databases in it and never
    touch the engine's tables, so minting a tenant per test would only count against the tenant ceiling
    (tests/storage_touch_ceiling.toml). Skips where ``t2_service_env`` would, and boots the substrate the same way."""
    from tests.db._service_fixture import jar_freshness_skip_reason  # noqa: PLC0415 — deferred: the conftest's own import

    if (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and not os.environ.get("NX_T2_SUBSTRATE_EXPECTED")
        and jar_freshness_skip_reason() is not None
    ):
        pytest.skip("engine substrate: service JAR not provisioned on CI "
                    "(tracked; NX_T2_SUBSTRATE_EXPECTED=1 re-arms fail-loud)")
    return _engine_substrate.ensure_engine()


@pytest.fixture
def scratch_db(substrate_cluster):
    state = substrate_cluster
    name = f"guard_{uuid.uuid4().hex[:10]}"
    _psql(state, "postgres", f"CREATE DATABASE {name}")
    try:
        yield (lambda sql: _psql(state, name, sql))
    finally:
        _psql(state, "postgres", f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


_CHANGELOG = ("CREATE TABLE public.databasechangelog (id varchar(255) NOT NULL, author varchar(255), "
              "filename varchar(255))")


def _seed_bytea_era(run, *, changesets: list[str], legacy_chunks: int, legacy_manifest: int,
                    partitioned: bool = False) -> None:
    """The era after rdr180 and vectors-004: nexus.chunks and the manifest, chash bytea. ``partitioned`` makes
    nexus.chunks the engine's later shape: a list-partitioned parent whose rows live in a leaf."""
    run(_CHANGELOG)
    for cs in changesets:
        run(f"INSERT INTO public.databasechangelog (id) VALUES ('{cs}')")
    run("CREATE SCHEMA nexus")
    if partitioned:
        run("CREATE TABLE nexus.chunks (chash bytea, embedding_model text) PARTITION BY LIST (embedding_model)")
        run("CREATE TABLE nexus.chunks_p1 PARTITION OF nexus.chunks FOR VALUES IN ('m1')")
    else:
        run("CREATE TABLE nexus.chunks (chash bytea)")
    run("CREATE TABLE nexus.catalog_document_chunks (chash bytea)")
    model = ", 'm1'" if partitioned else ""
    run(f"INSERT INTO nexus.chunks SELECT decode(repeat('ab', 32), 'hex'){model} FROM generate_series(1, 4)")
    for table, n in (("chunks", legacy_chunks), ("catalog_document_chunks", legacy_manifest)):
        if n:
            extra = model if table == "chunks" else ""
            run(f"INSERT INTO nexus.{table} SELECT decode(repeat('cd', 16), 'hex'){extra} FROM generate_series(1, {n})")


def _seed_dim_sharded_text_era(run, *, legacy: dict[str, int], survivors: int = 2) -> None:
    """The era before vectors-004 (engine <= v0.1.74): chash text, chunks in chunks_384/768/1024, no nexus.chunks."""
    run(_CHANGELOG)
    run("INSERT INTO public.databasechangelog (id) VALUES ('catalog-001'), ('rdr180-1-drop-dependent-views')")
    run("CREATE SCHEMA nexus")
    for table in ("chunks_384", "chunks_768", "chunks_1024", "catalog_document_chunks"):
        run(f"CREATE TABLE nexus.{table} (chash text NOT NULL)")
    for table, n in legacy.items():
        if n:  # the text era's only legal value was 32 lowercase hex characters: 16 bytes once decoded
            run(f"INSERT INTO nexus.{table} SELECT md5(g::text) FROM generate_series(1, {n}) g")
    # 64 lowercase hex decodes to exactly 32 bytes: it survives the delete
    run(f"INSERT INTO nexus.chunks_384 SELECT md5(g::text) || md5(g::text || 'x') FROM generate_series(1, {survivors}) g")


class TestRealPostgres:
    def test_the_bytea_era_with_legacy_rows_refuses(self, scratch_db) -> None:
        _seed_bytea_era(scratch_db, changesets=["catalog-001", "rdr180-001-bytea-chash"], legacy_chunks=3,
                        legacy_manifest=2)
        v = guard.check(scratch_db)
        assert not v.proceed and v.counts == {"chunks": 3, "catalog_document_chunks": 2}

    def test_the_dim_sharded_text_era_refuses_with_per_table_counts(self, scratch_db) -> None:
        """C1/I5: the era's real shape (chunks_<dim>, text chash, no nexus.chunks), not a fictional text nexus.chunks."""
        _seed_dim_sharded_text_era(scratch_db, legacy={"chunks_384": 5, "chunks_768": 2, "catalog_document_chunks": 3})
        v = guard.check(scratch_db)
        assert not v.proceed
        assert v.counts == {"chunks_384": 5, "chunks_768": 2, "chunks_1024": 0, "catalog_document_chunks": 3}
        assert {t for t, _ in v.required} == {"chunks_384", "chunks_768", "chunks_1024", "catalog_document_chunks"}

    def test_a_dim_sharded_store_with_only_32_byte_rows_proceeds(self, scratch_db) -> None:
        _seed_dim_sharded_text_era(scratch_db, legacy={}, survivors=3)
        v = guard.check(scratch_db)
        assert v.proceed and v.counts["chunks_384"] == 0

    def test_the_text_count_agrees_with_the_changelogs_own_decode_rule(self, scratch_db) -> None:
        """S1: count what rdr180-3's USING would produce at a width other than 32, value by value."""
        scratch_db(_CHANGELOG)
        scratch_db("CREATE SCHEMA nexus")
        scratch_db("CREATE TABLE nexus.chunks_384 (chash text NOT NULL)")
        scratch_db("CREATE TABLE nexus.catalog_document_chunks (chash text NOT NULL)")
        values = [
            "md5('a')",                         # 32 lowercase hex: decodes to 16 bytes -> deleted
            "md5('a') || md5('b')",             # 64 lowercase hex: 32 bytes -> survives
            "repeat('Z', 64)",                  # 64 chars, not hex: carried as 64 UTF-8 bytes -> deleted
            "repeat('A', 64)",                  # uppercase is not the rule's hex: 64 bytes -> deleted
            "repeat('a', 33)",                  # odd-length hex is not decoded: 33 bytes -> deleted
            "repeat(chr(233), 16)",             # 16 characters, 32 UTF-8 bytes, not hex -> survives
            "repeat('a', 16)",                  # even-length hex: 8 bytes -> deleted
        ]
        for v in values:
            scratch_db(f"INSERT INTO nexus.chunks_384 SELECT {v}")
        oracle = int(scratch_db(
            f"SELECT count(*) FROM (SELECT {_RDR180_USING} AS b FROM nexus.chunks_384) x WHERE octet_length(b) <> 32"
        ).strip())
        assert oracle == 5
        assert guard.check(scratch_db).counts["chunks_384"] == oracle

    def test_past_both_deleting_changesets_proceeds_silently(self, scratch_db) -> None:
        _seed_bytea_era(scratch_db, changesets=["rdr180-001-bytea-chash", "hygiene-001-5", "hygiene-001-8"],
                        legacy_chunks=3, legacy_manifest=0)
        v = guard.check(scratch_db)
        assert v.proceed and v.counts == {} and v.kind == "applied"

    def test_past_hygiene_001_5_only_still_guards_the_manifest(self, scratch_db) -> None:
        """I2: orphan manifest rows that -5's join leaves are deleted by -8."""
        _seed_bytea_era(scratch_db, changesets=["rdr180-001-bytea-chash", "hygiene-001-5"], legacy_chunks=0,
                        legacy_manifest=2)
        v = guard.check(scratch_db)
        assert not v.proceed and v.counts == {"catalog_document_chunks": 2}

    def test_a_clean_store_proceeds(self, scratch_db) -> None:
        _seed_bytea_era(scratch_db, changesets=["rdr180-001-bytea-chash"], legacy_chunks=0, legacy_manifest=0)
        assert guard.check(scratch_db).proceed is True

    def _refused_store(self, scratch_db, *, partitioned: bool = False, filler: bool = False) -> tuple[dict, str, Path]:
        """A refused bytea-era store: the record, the database name, the bundle's bin directory."""
        _seed_bytea_era(scratch_db, changesets=["rdr180-001-bytea-chash"], legacy_chunks=2, legacy_manifest=1,
                        partitioned=partitioned)
        if filler:  # enough data after the table of contents for a truncation to cut into
            scratch_db("CREATE TABLE nexus.filler AS SELECT repeat('x', 1000) AS v FROM generate_series(1, 3000)")
        verdict = guard.check(scratch_db)
        assert not verdict.proceed
        record = {"refused_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(), "rejected": {},
                  "sentinel": guard.create_sentinel(scratch_db), "required": [list(r) for r in verdict.required]}
        return record, scratch_db("SELECT current_database()").strip(), Path(_engine_substrate._state["pg_bin"])

    def _target(self, db: str, bin_dir: Path) -> pg_backup.BackupTarget:
        state = _engine_substrate._state
        return pg_backup.BackupTarget.from_bin_dir(bin_dir, host="127.0.0.1", port=int(state["pg_port"]),
                                                   user=state["pg_user"], dbname=db)

    def test_a_dump_made_by_the_nx_backup_verb_is_accepted(self, scratch_db, tmp_path) -> None:
        """Through the verb's own library (run_dump): a future -n/-N added to nx backup leaves out the marker schema
        and this goes red, instead of every backup failing the guard in the field."""
        record, db, bin_dir = self._refused_store(scratch_db)
        dest = tmp_path / "nexus-backups" / "nexus-verb.dump"
        pg_backup.run_dump(self._target(db, bin_dir), dest)
        assert guard.verified_backup_since(record, pg_restore=bin_dir / "pg_restore", home=tmp_path) == dest

    def test_a_truncated_real_dump_lists_everything_and_is_still_rejected(self, scratch_db, tmp_path) -> None:
        """What `pg_restore -l` alone would have accepted: the table of contents is at the start of the file."""
        record, db, bin_dir = self._refused_store(scratch_db, filler=True)
        whole = tmp_path / "nexus-backups" / "nexus-whole.dump"
        pg_backup.run_dump(self._target(db, bin_dir), whole)
        cut = tmp_path / "nexus-backups" / "nexus-cut.dump"
        data = whole.read_bytes()
        cut.write_bytes(data[: len(data) // 2])
        os.utime(cut, (whole.stat().st_mtime + 60,) * 2)  # the newest: tried first
        listing = subprocess.run([str(bin_dir / "pg_restore"), "-l", str(cut)], check=True, capture_output=True,
                                 timeout=60).stdout
        assert guard.holds_required_data(listing, record), "the listing of the cut dump looks complete"
        assert guard.verified_backup_since(record, pg_restore=bin_dir / "pg_restore", home=tmp_path) == whole
        assert [why for key, why in record["rejected"].items() if "nexus-cut" in key] == [
            "incomplete: pg_restore could not read all of its data"]

    def test_a_partitioned_chunks_table_is_counted_and_found_in_a_real_dump_by_its_leaves(self, scratch_db,
                                                                                      tmp_path) -> None:
        record, db, bin_dir = self._refused_store(scratch_db, partitioned=True)
        assert guard.check(scratch_db).counts == {"chunks": 2, "catalog_document_chunks": 1}
        assert dict(map(tuple, record["required"])) == {"chunks": True, "catalog_document_chunks": False}
        dest = tmp_path / "nexus-backups" / "nexus-leaves.dump"
        pg_backup.run_dump(self._target(db, bin_dir), dest)
        listing = subprocess.run([str(bin_dir / "pg_restore"), "-l", str(dest)], check=True, capture_output=True,
                                 timeout=60).stdout.decode("utf-8")
        assert "TABLE DATA nexus chunks_p1" in listing, "the data sits in the leaf, not the parent"
        assert guard.verified_backup_since(record, pg_restore=bin_dir / "pg_restore", home=tmp_path) == dest

    def test_a_real_dump_lists_the_marker_schema_and_the_data_the_engine_would_delete(self, scratch_db, tmp_path) -> None:
        """The shape ``pg_restore -l`` really prints for a ``pg_dump -Fc`` of a store the guard refused."""
        state = _engine_substrate._state
        _seed_bytea_era(scratch_db, changesets=["rdr180-001-bytea-chash"], legacy_chunks=2, legacy_manifest=1)
        v = guard.check(scratch_db)
        sentinel = guard.create_sentinel(scratch_db)
        record = {"sentinel": sentinel, "required": [list(r) for r in v.required]}
        db = scratch_db("SELECT current_database()").strip()
        dump = tmp_path / "real.dump"
        bin_dir = Path(state["pg_bin"])
        common = ["-h", "127.0.0.1", "-p", str(state["pg_port"]), "-U", state["pg_user"]]
        subprocess.run([str(bin_dir / "pg_dump"), "-Fc", "-w", *common, "-f", str(dump), db], check=True,
                       capture_output=True, timeout=120)
        listing = subprocess.run([str(bin_dir / "pg_restore"), "-l", str(dump)], check=True, capture_output=True,
                                 timeout=60).stdout
        assert guard.holds_required_data(listing, record), listing.decode("utf-8", "replace")
        assert not guard.holds_required_data(listing, {**record, "sentinel": "nx_legacy_guard_000000000000"})
        only_chunks = tmp_path / "chunks_only.dump"
        subprocess.run([str(bin_dir / "pg_dump"), "-Fc", "-w", *common, "-t", "nexus.chunks", "-f", str(only_chunks),
                        db], check=True, capture_output=True, timeout=120)
        partial = subprocess.run([str(bin_dir / "pg_restore"), "-l", str(only_chunks)], check=True,
                                 capture_output=True, timeout=60).stdout
        assert not guard.holds_required_data(partial, record), "a table-only dump has no marker schema and no manifest"

    def test_a_marker_schema_whose_comment_fails_leaves_no_schema_behind(self, scratch_db) -> None:
        """psql sends the one statement string as one transaction: no orphan schema under a name nobody recorded."""
        def breaking(sql: str) -> str:
            return scratch_db(sql.replace("COMMENT ON SCHEMA", "COMMENT ON SCHEMAX"))

        with pytest.raises(RuntimeError):
            guard.create_sentinel(breaking)
        assert scratch_db("SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'nx_legacy_guard_%'").strip() == "0"

    def test_ensure_recreates_a_lost_marker_schema_and_leaves_a_present_one_alone(self, scratch_db) -> None:
        name = guard.create_sentinel(scratch_db)
        guard.ensure_sentinel(scratch_db, name)
        guard.drop_sentinel(scratch_db, name)
        guard.ensure_sentinel(scratch_db, name)
        assert scratch_db(f"SELECT obj_description('{name}'::regnamespace, 'pg_namespace') IS NOT NULL").strip() == "t"

    def test_the_marker_schema_is_created_and_dropped_for_real(self, scratch_db) -> None:
        name = guard.create_sentinel(scratch_db)
        assert scratch_db(f"SELECT count(*) FROM pg_namespace WHERE nspname = '{name}'").strip() == "1"
        guard.drop_sentinel(scratch_db, name)
        assert scratch_db(f"SELECT count(*) FROM pg_namespace WHERE nspname = '{name}'").strip() == "0"
