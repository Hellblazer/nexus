# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd: the guard that keeps an engine from deleting 16-byte chash rows at boot before a backup exists.

``check`` is driven two ways: by scripted SQL answers (each branch of wren's steps 1-4), and against real Postgres in
throwaway databases inside the test substrate's cluster, for wren's three fixtures: a changelog up to rdr180-001 with
seeded legacy rows (refuse), a store from before rdr180 (text chash, legacy rows: refuse), and one past hygiene-001-5
(proceed, silently). Nothing here starts an engine against those databases, and nothing runs ``nx backup``.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.daemon import legacy_chash_guard as guard
from tests import _engine_substrate


def _scripted(answers: dict[str, str]):
    """A RunSql answering by the first key found in the statement."""
    asked: list[str] = []

    def run(sql: str) -> str:
        asked.append(sql)
        for key, out in answers.items():
            if key in sql:
                return out
        raise AssertionError(f"unscripted SQL: {sql}")

    run.asked = asked  # type: ignore[attr-defined]
    return run


class TestCheckSteps:
    def test_no_changelog_proceeds(self) -> None:
        v = guard.check(_scripted({"to_regclass": "f"}))
        assert v.proceed and "no changelog" in v.reason

    def test_the_deleting_changeset_already_ran_proceeds_without_counting(self) -> None:
        run = _scripted({"to_regclass": "t", "hygiene-001-5": "t"})
        v = guard.check(run)
        assert v.proceed and "already ran" in v.reason
        assert not [q for q in run.asked if "count(*)" in q]

    def test_no_chunks_table_is_the_runbooks_path(self) -> None:
        v = guard.check(_scripted({"to_regclass": "t", "hygiene-001-5": "f", "information_schema": ""}))
        assert v.proceed and "runbook" in v.reason

    @pytest.mark.parametrize(("dtype", "predicate"), [("bytea", "octet_length(chash) <> 32"),
                                                      ("text", "length(chash) <> 64")])
    def test_counts_use_the_columns_type(self, dtype: str, predicate: str) -> None:
        run = _scripted({"to_regclass": "t", "hygiene-001-5": "f",
                         "information_schema": f"chunks|{dtype}\ncatalog_document_chunks|{dtype}",
                         "count(*)": "0"})
        v = guard.check(run)
        assert v.proceed and v.counts == {"chunks": 0, "catalog_document_chunks": 0}
        assert all(predicate in q for q in run.asked if "count(*)" in q)

    def test_a_legacy_row_refuses_with_the_counts(self) -> None:
        def run(sql: str) -> str:
            if "to_regclass" in sql:
                return "t"
            if "hygiene-001-5" in sql:
                return "f"
            if "information_schema" in sql:
                return "chunks|bytea\ncatalog_document_chunks|bytea"
            return "3" if "nexus.chunks " in sql else "0"

        v = guard.check(run)
        assert not v.proceed and v.counts == {"chunks": 3, "catalog_document_chunks": 0}


class TestRecordAndBackup:
    T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)

    def _refuse(self, tmp_path: Path) -> dict:
        return guard.record_refusal(tmp_path, guard.Verdict(False, "x", {"chunks": 3, "catalog_document_chunks": 1}),
                                    now=self.T0)

    def test_a_refusal_is_recorded_and_its_first_time_is_kept(self, tmp_path: Path) -> None:
        first = self._refuse(tmp_path)
        again = guard.record_refusal(tmp_path, guard.Verdict(False, "x", {"chunks": 3}), now=self.T0 + timedelta(hours=1))
        assert again["refused_at"] == first["refused_at"] == self.T0.isoformat()
        assert guard.pending_refusal(tmp_path)["counts"] == {"chunks": 3}

    def test_the_refusal_text_names_the_counts_the_command_and_the_folder(self, tmp_path: Path) -> None:
        text = guard.refusal_text(self._refuse(tmp_path), home=tmp_path)
        assert "3 chunk rows, 1 manifest rows" in text and "`nx backup`" in text
        assert str(tmp_path / "nexus-backups") in text and "hygiene-001-5" in text

    def _dump(self, home: Path, name: str, when: datetime) -> Path:
        folder = home / "nexus-backups"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(b"PGDMP")
        os.utime(path, (when.timestamp(), when.timestamp()))
        return path

    def _runner(self, readable: set[str]):
        def run(argv, **_kw):
            ok = Path(argv[-1]).name in readable
            return subprocess.CompletedProcess(argv, 0 if ok else 1, b"; 1; 0 2 TABLE DATA nexus chunks nexus\n", b"")

        return run

    def test_only_a_newer_dump_that_reads_back_clears_it(self, tmp_path: Path) -> None:
        self._dump(tmp_path, "nexus-old.dump", self.T0 - timedelta(minutes=5))
        self._dump(tmp_path, "nexus-bad.dump", self.T0 + timedelta(minutes=10))
        good = self._dump(tmp_path, "nexus-good.dump", self.T0 + timedelta(minutes=5))
        found = guard.verified_backup_since(self.T0.isoformat(), pg_restore=Path("pg_restore"), home=tmp_path,
                                            runner=self._runner({"nexus-good.dump", "nexus-old.dump"}))
        assert found == good, "the old dump predates the refusal; the newest one does not read back"

    def test_no_dump_means_no_clearance(self, tmp_path: Path) -> None:
        assert guard.verified_backup_since(self.T0.isoformat(), pg_restore=Path("x"), home=tmp_path,
                                           runner=self._runner(set())) is None

    def test_clearing_keeps_the_record_as_history(self, tmp_path: Path) -> None:
        self._refuse(tmp_path)
        guard.mark_cleared(tmp_path, tmp_path / "nexus-backups" / "nexus-good.dump", now=self.T0 + timedelta(hours=1))
        record = json.loads((tmp_path / guard.GUARD_FILENAME).read_text())
        assert record["cleared_by"].endswith("nexus-good.dump") and guard.pending_refusal(tmp_path) is None
        fresh = guard.record_refusal(tmp_path, guard.Verdict(False, "x", {"chunks": 1}), now=self.T0 + timedelta(days=1))
        assert fresh["refused_at"] == (self.T0 + timedelta(days=1)).isoformat(), "a new refusal after a clear starts over"


# ── real Postgres: wren's three fixtures, each in its own throwaway database ─────────────────────────────────────


def _psql(state: dict, db: str, sql: str) -> str:
    out = subprocess.run(
        [str(Path(state["pg_bin"]) / "psql"), "-X", "-q", "-t", "-A", "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", state["pg_user"], "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, timeout=60, check=False,
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


def _seed(run, *, chash_type: str, changesets: list[str], legacy_chunks: int, legacy_manifest: int) -> None:
    run(_CHANGELOG)
    for cs in changesets:
        run(f"INSERT INTO public.databasechangelog (id) VALUES ('{cs}')")
    run("CREATE SCHEMA nexus")
    run(f"CREATE TABLE nexus.chunks (chash {chash_type})")
    run(f"CREATE TABLE nexus.catalog_document_chunks (chash {chash_type})")
    if chash_type == "bytea":
        full, legacy = "decode(repeat('ab', 32), 'hex')", "decode(repeat('cd', 16), 'hex')"
    else:
        full, legacy = "repeat('a', 64)", "repeat('c', 32)"
    run(f"INSERT INTO nexus.chunks SELECT {full} FROM generate_series(1, 4)")
    for table, n in (("chunks", legacy_chunks), ("catalog_document_chunks", legacy_manifest)):
        if n:
            run(f"INSERT INTO nexus.{table} SELECT {legacy} FROM generate_series(1, {n})")


class TestRealPostgres:
    def test_changelog_up_to_rdr180_with_legacy_rows_refuses(self, scratch_db) -> None:
        _seed(scratch_db, chash_type="bytea", changesets=["catalog-001", "rdr180-001-bytea-chash"],
              legacy_chunks=3, legacy_manifest=2)
        v = guard.check(scratch_db)
        assert not v.proceed and v.counts == {"chunks": 3, "catalog_document_chunks": 2}

    def test_a_store_from_before_rdr180_with_text_chash_refuses(self, scratch_db) -> None:
        _seed(scratch_db, chash_type="text", changesets=["catalog-001"], legacy_chunks=5, legacy_manifest=0)
        v = guard.check(scratch_db)
        assert not v.proceed and v.counts == {"chunks": 5, "catalog_document_chunks": 0}

    def test_past_hygiene_001_5_proceeds_silently(self, scratch_db) -> None:
        _seed(scratch_db, chash_type="bytea", changesets=["rdr180-001-bytea-chash", "hygiene-001-5"],
              legacy_chunks=3, legacy_manifest=0)
        v = guard.check(scratch_db)
        assert v.proceed and v.counts == {} and "already ran" in v.reason

    def test_a_clean_store_proceeds(self, scratch_db) -> None:
        _seed(scratch_db, chash_type="bytea", changesets=["rdr180-001-bytea-chash"], legacy_chunks=0,
              legacy_manifest=0)
        assert guard.check(scratch_db).proceed is True


# ── the gate, the supervisor's wait and the CLI's exit 75 ───────────────────────────────────────────────────────


def _legacy(sql: str) -> str:
    if "to_regclass" in sql:
        return "t"
    if "hygiene-001-5" in sql:
        return "f"
    if "information_schema" in sql:
        return "chunks|bytea\ncatalog_document_chunks|bytea"
    return "2"


class TestGate:
    def test_a_legacy_store_is_refused_and_recorded(self, tmp_path: Path) -> None:
        with pytest.raises(guard.LegacyChashRefused, match="nx backup"):
            guard.gate(tmp_path, _legacy)
        assert guard.pending_refusal(tmp_path)["counts"] == {"chunks": 2, "catalog_document_chunks": 2}

    def test_a_cleared_refusal_lets_the_start_go_on_while_the_rows_remain(self, tmp_path: Path) -> None:
        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, _legacy)
        guard.mark_cleared(tmp_path, tmp_path / "x.dump")
        assert guard.gate(tmp_path, _legacy).proceed is False  # the rows are there; the gate lets it go on anyway

    def test_an_explicit_backup_must_be_newer_and_hold_the_chunk_data(self, tmp_path: Path) -> None:
        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, _legacy)
        dump = tmp_path / "elsewhere.dump"
        dump.write_bytes(b"PGDMP")
        listing = {"ok": b"; 4321; 0 16390 TABLE DATA nexus chunks nexus\n", "no": b"; 1; 0 1 TABLE DATA nexus other x\n"}

        def runner(which: str):
            return lambda argv, **_k: subprocess.CompletedProcess(argv, 0, listing[which], b"")

        assert guard.try_clear(tmp_path, pg_restore=Path("pg_restore"), explicit=dump, runner=runner("no")) is None
        assert guard.try_clear(tmp_path, pg_restore=Path("pg_restore"), explicit=dump, runner=runner("ok")) == dump
        assert guard.read_record(tmp_path)["cleared_by"] == str(dump)

    def test_a_partitioned_chunk_table_counts_as_the_chunk_data(self) -> None:
        assert guard.holds_chunk_data("; 9; 0 1 TABLE DATA nexus chunks_m1_t1 nexus")
        assert not guard.holds_chunk_data("; 9; 0 1 TABLE DATA nexus chunkstore nexus")


class TestSupervisorWiring:
    def _sup(self, tmp_path: Path):
        from nexus.daemon.storage_service_daemon import StorageServiceSupervisor

        sup = StorageServiceSupervisor.__new__(StorageServiceSupervisor)
        sup._config_dir = tmp_path
        sup._creds = {"PG_DATA": str(tmp_path / "pg"), "PG_PORT": "1"}
        return sup

    def test_the_guard_refuses_the_spawn_as_a_start_error(self, tmp_path: Path, monkeypatch) -> None:
        from nexus.daemon.storage_service_daemon import StorageServiceStartError

        monkeypatch.setattr(guard, "bundled_run_sql", lambda *_a, **_k: _legacy)
        with pytest.raises(StorageServiceStartError) as exc:
            self._sup(tmp_path)._legacy_chash_guard()
        assert isinstance(exc.value.__cause__, guard.LegacyChashRefused)

    def test_a_guard_that_cannot_ask_does_not_block_the_start(self, tmp_path: Path, monkeypatch) -> None:
        def broken(*_a, **_k):
            raise RuntimeError("no psql")

        monkeypatch.setattr(guard, "bundled_run_sql", broken)
        self._sup(tmp_path)._legacy_chash_guard()  # logs an error, does not raise

    def test_managed_postgres_is_not_checked(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(guard, "bundled_run_sql", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError))
        sup = self._sup(tmp_path)
        sup._creds = {"PG_PORT": "1"}
        sup._legacy_chash_guard()

    def test_the_wait_retries_only_once_the_user_cleared_it(self, tmp_path: Path, monkeypatch) -> None:
        import threading

        from nexus.daemon import storage_service_daemon as ssd

        monkeypatch.setattr(ssd, "_BELOW_FLOOR_RECHECK_S", 0.0)
        with pytest.raises(guard.LegacyChashRefused) as refused:
            guard.gate(tmp_path, _legacy)
        err = ssd.StorageServiceStartError(str(refused.value))
        err.__cause__ = refused.value
        stop = threading.Event()
        cleared_after: list[int] = []

        def is_cleared(cfg: Path) -> bool:
            cleared_after.append(1)
            return len(cleared_after) >= 3

        monkeypatch.setattr(guard, "is_cleared", is_cleared)
        assert ssd._wait_for_legacy_backup(self._sup(tmp_path), err, stop, lambda: None) == ssd._ClusterFloorWait.RETRY
        assert len(cleared_after) == 3
        assert ssd._wait_for_legacy_backup(self._sup(tmp_path), ssd.StorageServiceStartError("other"), stop,
                                           lambda: None) is None


class TestCliStart:
    def test_a_pending_refusal_without_a_backup_exits_75_before_anything_starts(self, tmp_path: Path,
                                                                                 monkeypatch) -> None:
        from click.testing import CliRunner

        from nexus.cli import main

        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, _legacy)
        monkeypatch.setattr("nexus.db.pg_provision.discover_pg_binaries",
                            lambda *_a, **_k: type("B", (), {"bin_dir": tmp_path})())
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        spawned: list[int] = []
        monkeypatch.setattr("nexus.commands.daemon._start_via_autostart_unit", lambda *a, **k: spawned.append(1))
        r = CliRunner().invoke(main, ["daemon", "service", "start", "--config-dir", str(tmp_path)])
        assert r.exit_code == 75, r.output
        assert "nx backup" in r.stderr and not spawned
