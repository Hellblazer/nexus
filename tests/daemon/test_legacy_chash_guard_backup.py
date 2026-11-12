# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd: which dump counts as the copy the guard waits for, and the guard against real Postgres.

The first half is driven by a fake ``pg_restore``; the real-Postgres half runs in throwaway databases inside the test
substrate's cluster with the shapes the engine's real eras produced (the dim-sharded ``chunks_384/768/1024`` text era
that has no ``nexus.chunks``, the bytea era, a partitioned ``nexus.chunks``), checks the text decode rule against the
changelog's own USING expression, and builds dumps through ``nexus.backup.pg_backup.run_dump``, the verb's own library
(tests may import it; the pin scans ``src``). Nothing here starts an engine, and nothing runs ``nx backup``."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.backup import pg_backup
from nexus.daemon import legacy_chash_guard as guard
from tests import _engine_substrate
from tests.daemon._legacy_chash_support import _FULL, _MARK, _RDR180_USING, T0, _Cluster, _listing, _record


class TestBackups:
    def test_a_dump_must_list_the_marker_schema_and_the_data_the_engine_would_delete(self) -> None:
        rec = _record()
        assert guard.holds_required_data(_FULL, rec)
        other_cluster = _listing("nexus chunks nexus", "nexus catalog_document_chunks nexus")
        assert not guard.holds_required_data(other_cluster, rec), "a dump of another store has no marker schema"
        wrong_marker = _listing("SCHEMA - nx_legacy_guard_000000000000 nexus", "nexus chunks nexus",
                                "nexus catalog_document_chunks nexus")
        assert not guard.holds_required_data(wrong_marker, rec)
        assert not guard.holds_required_data(_listing(_MARK, "nexus chunks nexus"), rec), "-t nexus.chunks: no manifest"
        assert not guard.holds_required_data(_listing(_MARK, "nexus catalog_document_chunks nexus"), rec)
        assert not guard.holds_required_data(_FULL, _record(sentinel=None)), "no marker schema, no backup accepted"

    def test_a_partitioned_chunks_table_is_listed_by_its_leaves(self) -> None:
        leaves = _listing(_MARK, "nexus chunks_m1_t1 nexus", "nexus catalog_document_chunks nexus")
        assert guard.holds_required_data(leaves, _record())
        other = _listing(_MARK, "nexus chunkstore nexus", "nexus catalog_document_chunks nexus")
        assert not guard.holds_required_data(other, _record())

    def test_each_shard_table_of_the_dim_sharded_era_is_required(self) -> None:
        rec = _record(required=(("chunks_384", False), ("chunks_768", False), ("catalog_document_chunks", False)))
        both = _listing(_MARK, "nexus chunks_384 nexus", "nexus chunks_768 nexus", "nexus catalog_document_chunks nexus")
        assert guard.holds_required_data(both, rec)
        assert not guard.holds_required_data(
            _listing(_MARK, "nexus chunks_384 nexus", "nexus catalog_document_chunks nexus"), rec)

    def _dump(self, home: Path, name: str, when: datetime) -> Path:
        folder = home / "nexus-backups"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(b"PGDMP" + name.encode())
        os.utime(path, (when.timestamp(), when.timestamp()))
        return path

    def _runner(self, listings: dict[str, bytes], calls: list[str] | None = None):
        def run(argv, **_kw):
            name = Path(argv[-1]).name
            if calls is not None:
                calls.append(name)
            if name not in listings:
                return subprocess.CompletedProcess(argv, 1, b"", b"not a dump")
            return subprocess.CompletedProcess(argv, 0, listings[name], b"")

        return run

    def _refuse(self, tmp_path: Path) -> None:
        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, _Cluster(counts={"chunks": 3}), pg_data="/pg")

    def test_only_a_newer_dump_that_reads_back_with_the_marker_clears_it(self, tmp_path: Path) -> None:
        self._dump(tmp_path, "nexus-old.dump", T0 - timedelta(minutes=5))
        self._dump(tmp_path, "nexus-bad.dump", T0 + timedelta(minutes=10))
        self._dump(tmp_path, "nexus-foreign.dump", T0 + timedelta(minutes=8))
        good = self._dump(tmp_path, "nexus-good.dump", T0 + timedelta(minutes=5))
        runner = self._runner({"nexus-good.dump": _FULL, "nexus-old.dump": _FULL,
                               "nexus-foreign.dump": _listing("nexus chunks nexus",
                                                              "nexus catalog_document_chunks nexus")})
        found = guard.verified_backup_since(_record(), pg_restore=Path("pg_restore"), home=tmp_path, runner=runner)
        assert found == good, "old predates the refusal, bad is unreadable, foreign is another store's dump"

    def test_no_dump_means_no_clearance(self, tmp_path: Path) -> None:
        assert guard.verified_backup_since(_record(), pg_restore=Path("x"), home=tmp_path,
                                           runner=self._runner({})) is None

    def test_a_rejected_dump_is_not_read_again_and_a_changed_one_is(self, tmp_path: Path) -> None:
        """S6: try_clear runs on every client call; a dump already rejected costs nothing the second time."""
        dump = self._dump(tmp_path, "nexus-a.dump", T0 + timedelta(minutes=1))
        rec = _record()
        calls: list[str] = []
        runner = self._runner({}, calls)
        guard.verified_backup_since(rec, pg_restore=Path("x"), home=tmp_path, runner=runner)
        guard.verified_backup_since(rec, pg_restore=Path("x"), home=tmp_path, runner=runner)
        assert calls == ["nexus-a.dump"]
        dump.write_bytes(b"PGDMP longer now")  # same path, new size: a dump still being written gets read again
        os.utime(dump, (T0.timestamp() + 60,) * 2)
        guard.verified_backup_since(rec, pg_restore=Path("x"), home=tmp_path, runner=runner)
        assert calls == ["nexus-a.dump", "nexus-a.dump"]

    def test_the_rejections_survive_to_the_next_call(self, tmp_path: Path) -> None:
        self._refuse(tmp_path)
        self._dump(tmp_path, "nexus-a.dump", datetime.now(UTC) + timedelta(minutes=1))
        calls: list[str] = []
        for _ in range(2):
            assert guard.try_clear(tmp_path, pg_restore=Path("x"), home=tmp_path,
                                   runner=self._runner({}, calls)) is None
        assert calls == ["nexus-a.dump"]

    def test_one_attempt_reads_at_most_a_few_dumps(self, tmp_path: Path) -> None:
        for i in range(guard.MAX_DUMPS_PER_ATTEMPT + 4):
            self._dump(tmp_path, f"nexus-{i}.dump", T0 + timedelta(minutes=i + 1))
        calls: list[str] = []
        guard.verified_backup_since(_record(), pg_restore=Path("x"), home=tmp_path, runner=self._runner({}, calls))
        assert len(calls) == guard.MAX_DUMPS_PER_ATTEMPT

    def test_a_pg_restore_that_cannot_run_is_reported_apart_from_a_rejected_dump(self, tmp_path: Path) -> None:
        """I3: exit 127 (the loader) is not 'your dump is unreadable'."""
        self._dump(tmp_path, "nexus-a.dump", T0 + timedelta(minutes=1))

        def missing(argv, **_k):
            raise FileNotFoundError(argv[0])

        with pytest.raises(guard.PgRestoreUnavailable):
            guard.verified_backup_since(_record(), pg_restore=Path("pg_restore"), home=tmp_path, runner=missing)

        def loader(argv, **_k):
            return subprocess.CompletedProcess(argv, 127, b"", b"error while loading shared libraries: libpq.so.5")

        with pytest.raises(guard.PgRestoreUnavailable, match="libpq"):
            guard.verified_backup_since(_record(), pg_restore=Path("pg_restore"), home=tmp_path, runner=loader)

    def test_pg_restore_runs_with_the_bundles_library_path(self, tmp_path: Path) -> None:
        bundle = tmp_path / "pgbundle"
        (bundle / "bin").mkdir(parents=True)
        (bundle / "lib").mkdir()
        tool = bundle / "bin" / "pg_restore"
        tool.write_bytes(b"")
        self._dump(tmp_path, "nexus-a.dump", T0 + timedelta(minutes=1))
        seen: list[dict] = []

        def run(argv, **kw):
            seen.append(kw.get("env") or {})
            return subprocess.CompletedProcess(argv, 1, b"", b"")

        guard.verified_backup_since(_record(), pg_restore=tool, home=tmp_path, runner=run)
        assert seen and (os.name == "nt" or str((bundle / "lib").resolve()) in seen[0].get("LD_LIBRARY_PATH", ""))

    def _split_runner(self, listing: bytes, read_rc: int | Exception, calls: list[list[str]] | None = None):
        """-l answers ``listing``; the full read (-a) answers ``read_rc`` (an exit code, or an exception to raise)."""
        def run(argv, **_kw):
            if calls is not None:
                calls.append([Path(a).name if i == len(argv) - 1 else a for i, a in enumerate(argv)])
            if "-a" in argv:
                if isinstance(read_rc, Exception):
                    raise read_rc
                return subprocess.CompletedProcess(argv, read_rc, b"", b"pg_restore: error: unexpected end of file")
            return subprocess.CompletedProcess(argv, 0, listing, b"")

        return run

    def test_a_dump_that_lists_everything_but_cannot_be_read_to_its_end_is_not_accepted(self, tmp_path: Path) -> None:
        """A truncated or still-written dump: its table of contents is at the start and reads fine."""
        self._dump(tmp_path, "nexus-cut.dump", T0 + timedelta(minutes=1))
        rec = _record()
        calls: list[list[str]] = []
        runner = self._split_runner(_FULL, 1, calls)
        assert guard.verified_backup_since(rec, pg_restore=Path("pg_restore"), home=tmp_path, runner=runner) is None
        assert "incomplete" in next(iter(rec["rejected"].values()))
        guard.verified_backup_since(rec, pg_restore=Path("pg_restore"), home=tmp_path, runner=runner)
        assert len(calls) == 2, "listed once, read once, and then remembered as rejected"

    def test_the_full_read_is_a_data_only_restore_to_the_null_device(self, tmp_path: Path) -> None:
        dump = self._dump(tmp_path, "nexus-ok.dump", T0 + timedelta(minutes=1))
        calls: list[list[str]] = []
        found = guard.verified_backup_since(_record(), pg_restore=Path("pg_restore"), home=tmp_path,
                                            runner=self._split_runner(_FULL, 0, calls))
        assert found == dump
        assert calls[1][1:] == ["-f", os.devnull, "-a", "nexus-ok.dump"]

    def test_a_full_read_that_times_out_is_neither_accepted_nor_written_off(self, tmp_path: Path) -> None:
        self._dump(tmp_path, "nexus-big.dump", T0 + timedelta(minutes=1))
        rec = _record()
        runner = self._split_runner(_FULL, subprocess.TimeoutExpired("pg_restore", 1))
        assert guard.verified_backup_since(rec, pg_restore=Path("pg_restore"), home=tmp_path, runner=runner) is None
        assert rec["rejected"] == {}

    @pytest.mark.parametrize("code", [126, 127, 3221225781, -1073741515])
    def test_the_codes_that_mean_the_tool_could_not_run_are_not_the_dumps_fault(self, tmp_path: Path, code: int) -> None:
        """127 and 126 on POSIX; STATUS_DLL_NOT_FOUND (0xC0000135) on Windows, unsigned or signed."""
        self._dump(tmp_path, "nexus-a.dump", T0 + timedelta(minutes=1))

        def run(argv, **_k):
            return subprocess.CompletedProcess(argv, code, b"", b"")

        with pytest.raises(guard.PgRestoreUnavailable):
            guard.verified_backup_since(_record(), pg_restore=Path("pg_restore"), home=tmp_path, runner=run)

    def test_the_refusal_says_what_is_lost_where_to_read_and_which_dumps_were_rejected(self) -> None:
        rec = {**_record(), "counts": {"chunks": 3, "catalog_document_chunks": 1},
               "rejected": {"/home/u/nexus-backups/nexus-a.dump|123|456": "incomplete: pg_restore could not read all "
                                                                         "of its data"}}
        text = guard.refusal_text(rec, home=Path("/home/u"))
        assert "deletes for good" in text and "recovered only from a copy" in text
        assert "github.com/Hellblazer/nexus/blob/main/docs/operations/backup-restore.md" in text and "`nx backup`" in text
        assert "/home/u/nexus-backups/nexus-a.dump (incomplete" in text
        assert "legacy-width" not in text, "no engine jargon up front"
        assert text.index("hygiene-001-5") > text.index("`nx backup`"), "the migration's name comes last"

    def test_clearing_keeps_the_record_as_history_and_a_later_refusal_starts_over(self, tmp_path: Path) -> None:
        self._refuse(tmp_path)
        guard.mark_cleared(tmp_path, tmp_path / "nexus-backups" / "nexus-good.dump", now=T0 + timedelta(hours=1))
        record = json.loads((tmp_path / guard.GUARD_FILENAME).read_text(encoding="utf-8"))
        assert record["cleared_by"].endswith("nexus-good.dump") and guard.pending_refusal(tmp_path) is None
        later = T0 + timedelta(days=1)
        fresh = guard.record_refusal(tmp_path, guard.Verdict(False, "x", {"chunks": 1}), now=later)
        assert fresh["refused_at"] == later.isoformat()

    def test_an_explicit_backup_is_checked_alone(self, tmp_path: Path) -> None:
        self._refuse(tmp_path)
        sentinel = guard.pending_refusal(tmp_path)["sentinel"]
        dump = tmp_path / "elsewhere.dump"
        dump.write_bytes(b"PGDMP")
        os.utime(dump, (datetime.now(UTC).timestamp() + 60,) * 2)
        no = self._runner({"elsewhere.dump": _listing("nexus other x")})
        assert guard.try_clear(tmp_path, pg_restore=Path("pg_restore"), explicit=dump, runner=no) is None
        yes = self._runner({"elsewhere.dump": _listing(f"SCHEMA - {sentinel} nexus", "nexus chunks nexus",
                                                       "nexus catalog_document_chunks nexus")})
        dump.write_bytes(b"PGDMP2")  # a different dump at the same path (size changed) is read again
        os.utime(dump, (datetime.now(UTC).timestamp() + 60,) * 2)
        assert guard.try_clear(tmp_path, pg_restore=Path("pg_restore"), explicit=dump, runner=yes) == dump
        assert guard.read_record(tmp_path)["cleared_by"] == str(dump)


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


@pytest.fixture
def scratch_db(t2_service_env):
    state = _engine_substrate._state
    assert state, "the engine substrate is up (t2_service_env)"
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

    def test_the_marker_schema_is_created_and_dropped_for_real(self, scratch_db) -> None:
        name = guard.create_sentinel(scratch_db)
        assert scratch_db(f"SELECT count(*) FROM pg_namespace WHERE nspname = '{name}'").strip() == "1"
        guard.drop_sentinel(scratch_db, name)
        assert scratch_db(f"SELECT count(*) FROM pg_namespace WHERE nspname = '{name}'").strip() == "0"
