# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd: the guard that keeps an engine from deleting legacy-width chash rows at boot before a backup exists.

``check`` is driven two ways: by a fake cluster that answers the guard's SQL (every branch: the changelog, the era's
tables, each table gated on its own changeset), and against real Postgres in throwaway databases inside the test
substrate's cluster, with the shapes the engine's real eras produced (the dim-sharded ``chunks_384/768/1024`` text era
that has no ``nexus.chunks``, the bytea era, and the text decode rule checked against the changelog's own USING
expression). The supervisor's wiring is driven through ``_start_locked`` itself. Nothing here starts an engine against
those databases, and nothing runs ``nx backup``.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands import upgrade
from nexus.daemon import legacy_chash_guard as guard
from nexus.daemon import readiness
from nexus.daemon import storage_service_daemon as ssd
from nexus.daemon.storage_service_daemon import StorageServiceStartError, StorageServiceSupervisor
from tests import _engine_substrate
from tests.daemon.test_storage_service_daemon import _FakeClock, _FakeProc, _make_supervisor

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


class _Cluster:
    """A fake cluster answering the guard's SQL from a declared shape; records what was asked."""

    def __init__(self, *, changelog: bool = True, ran: tuple[str, ...] = (), types: dict[str, str] | None = None,
                 counts: dict[str, int] | None = None) -> None:
        self.changelog = changelog
        self.ran = ran
        self.types = types if types is not None else {"chunks": "bytea", "catalog_document_chunks": "bytea"}
        self.counts = counts or {}
        self.asked: list[str] = []
        self.sentinels: list[str] = []

    def __call__(self, sql: str) -> str:
        self.asked.append(sql)
        if "to_regclass" in sql:
            return "t" if self.changelog else "f"
        if "databasechangelog WHERE" in sql:
            return "\n".join(self.ran)
        if "information_schema" in sql:
            return "\n".join(f"{t}|{d}" for t, d in self.types.items())
        if "count(*)" in sql:
            table = sql.split("FROM nexus.")[1].split(" ")[0]
            return str(self.counts.get(table, 0))
        if sql.startswith("CREATE SCHEMA nx_legacy_guard_"):
            self.sentinels.append(sql.split(" ")[2])
            return ""
        if sql.startswith("COMMENT ON SCHEMA nx_legacy_guard_") or sql.startswith("DROP SCHEMA IF EXISTS nx_legacy_guard_"):
            return ""
        raise AssertionError(f"unscripted SQL: {sql}")

    def counted(self) -> list[str]:
        return [q for q in self.asked if "count(*)" in q]


# ── check: the changelog, the era, and each table gated on its own changeset ────────────────────────────────────


class TestCheckSteps:
    def test_no_changelog_proceeds(self) -> None:
        v = guard.check(_Cluster(changelog=False))
        assert v.proceed and v.kind == "no_changelog" and "no changelog" in v.reason

    def test_both_deleting_changesets_already_ran_proceeds_without_counting(self) -> None:
        run = _Cluster(ran=("hygiene-001-5", "hygiene-001-8"))
        v = guard.check(run)
        assert v.proceed and v.kind == "applied" and "already ran" in v.reason
        assert not run.counted()

    def test_the_manifest_is_gated_on_its_own_changeset(self) -> None:
        """I2: a boot that failed between hygiene-001-5 and -8 leaves -8 pending; its delete reaches the manifest."""
        run = _Cluster(ran=("hygiene-001-5",), counts={"chunks": 9, "catalog_document_chunks": 2})
        v = guard.check(run)
        assert not v.proceed and v.counts == {"catalog_document_chunks": 2}
        assert not [q for q in run.counted() if "nexus.chunks " in q], "the chunk delete already ran: not counted"

    def test_neither_changeset_ran_counts_both_tables(self) -> None:
        v = guard.check(_Cluster(counts={"chunks": 3, "catalog_document_chunks": 0}))
        assert not v.proceed and v.counts == {"chunks": 3, "catalog_document_chunks": 0}

    def test_a_legacy_row_only_in_the_manifest_refuses(self) -> None:
        v = guard.check(_Cluster(counts={"chunks": 0, "catalog_document_chunks": 1}))
        assert not v.proceed and v.required == (("catalog_document_chunks", False),)

    def test_the_dim_sharded_era_is_counted_table_by_table(self) -> None:
        """C1: a store from before vectors-004 has no nexus.chunks; its rows are in chunks_384/768/1024."""
        run = _Cluster(types={"chunks_384": "text", "chunks_768": "text", "catalog_document_chunks": "text"},
                       counts={"chunks_384": 4, "chunks_768": 0, "catalog_document_chunks": 4})
        v = guard.check(run)
        assert not v.proceed
        assert v.counts == {"chunks_384": 4, "chunks_768": 0, "catalog_document_chunks": 4}
        assert dict(v.required) == {"chunks_384": False, "chunks_768": False, "catalog_document_chunks": False}

    def test_a_partitioned_chunks_table_is_required_by_its_leaves(self) -> None:
        v = guard.check(_Cluster(counts={"chunks": 2, "catalog_document_chunks": 0}))
        assert dict(v.required) == {"chunks": True, "catalog_document_chunks": False}

    def test_no_chash_tables_at_all_proceeds(self) -> None:
        v = guard.check(_Cluster(types={}))
        assert v.proceed and v.kind == "no_tables"

    @pytest.mark.parametrize("dtype", ["bytea", "text"])
    def test_counts_use_the_columns_type(self, dtype: str) -> None:
        run = _Cluster(types={"chunks": dtype, "catalog_document_chunks": dtype})
        assert guard.check(run).proceed
        counted = run.counted()
        assert len(counted) == 2
        if dtype == "bytea":
            assert all("octet_length(chash) <> 32" in q and "length(chash) / 2" not in q for q in counted)
        else:
            # rdr180-3's decode rule: lowercase even-length hex halves, anything else is its UTF-8 width
            assert all("'^[0-9a-f]+$'" in q and "length(chash) % 2 = 0" in q and "length(chash) / 2" in q
                       and "ELSE octet_length(chash)" in q for q in counted)


# ── the gate: refusal record, marker schema, nothing fails open, could-not-ask is not a refusal ──────────────────


class TestGate:
    def _refuse(self, tmp_path: Path, run: _Cluster | None = None) -> _Cluster:
        run = run or _Cluster(counts={"chunks": 2, "catalog_document_chunks": 2})
        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, run, pg_data="/pg")
        return run

    def test_a_legacy_store_is_refused_recorded_and_given_a_marker_schema(self, tmp_path: Path) -> None:
        run = self._refuse(tmp_path)
        record = guard.pending_refusal(tmp_path)
        assert record["counts"] == {"chunks": 2, "catalog_document_chunks": 2}
        assert len(run.sentinels) == 1 and record["sentinel"] == run.sentinels[0]
        assert record["sentinel"].startswith("nx_legacy_guard_")

    def test_the_refusal_text_names_the_command_the_marker_schema_and_why(self, tmp_path: Path) -> None:
        self._refuse(tmp_path)
        text = guard.refusal_text(guard.pending_refusal(tmp_path), home=tmp_path)
        sentinel = guard.pending_refusal(tmp_path)["sentinel"]
        assert "2 chunk rows, 2 manifest rows" in text and "`nx backup`" in text and "hygiene-001-5" in text
        assert f"schema {sentinel}" in text and "proves it is a dump of this store" in text
        assert str(tmp_path / "nexus-backups") in text

    def test_a_second_refusal_keeps_the_first_marker_table_and_time(self, tmp_path: Path) -> None:
        run = self._refuse(tmp_path)
        first = guard.pending_refusal(tmp_path)
        self._refuse(tmp_path, run)
        again = guard.pending_refusal(tmp_path)
        assert again["sentinel"] == first["sentinel"] and again["refused_at"] == first["refused_at"]
        assert len(run.sentinels) == 1

    def test_a_refusal_whose_record_cannot_be_written_is_still_a_refusal(self, tmp_path: Path, monkeypatch) -> None:
        """C2: after a refuse verdict nothing fails open."""

        def disk_full(*_a, **_k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(guard, "_write_json", disk_full)
        with pytest.raises(guard.LegacyChashRefused, match="nx backup"):
            guard.gate(tmp_path, _Cluster(counts={"chunks": 1}), pg_data="/pg")

    def test_a_marker_schema_that_cannot_be_created_is_still_a_refusal_and_accepts_no_backup(self, tmp_path: Path) -> None:
        class NoDdl(_Cluster):
            def __call__(self, sql: str) -> str:
                if sql.startswith("CREATE SCHEMA"):
                    raise RuntimeError("permission denied")
                return super().__call__(sql)

        with pytest.raises(guard.LegacyChashRefused, match="could not be created"):
            guard.gate(tmp_path, NoDdl(counts={"chunks": 1}), pg_data="/pg")
        assert not guard.holds_required_data(b"TABLE DATA nexus catalog_document_chunks", guard.pending_refusal(tmp_path))

    def test_a_cleared_refusal_lets_the_start_go_on_and_drops_the_marker_schema_once(self, tmp_path: Path) -> None:
        run = self._refuse(tmp_path)
        guard.mark_cleared(tmp_path, tmp_path / "x.dump")
        v = guard.gate(tmp_path, run, pg_data="/pg")
        assert v.proceed and v.kind == "cleared"
        sentinel = guard.read_record(tmp_path)["sentinel"]
        assert [q for q in run.asked if q == f"DROP SCHEMA IF EXISTS {sentinel} CASCADE"]
        assert guard.read_record(tmp_path)["sentinel_dropped"] is True
        run.asked.clear()
        guard.gate(tmp_path, run, pg_data="/pg")
        assert not [q for q in run.asked if q.startswith("DROP")], "dropped once"

    def test_a_clearance_is_spent_once_the_deleting_changesets_are_seen_applied(self, tmp_path: Path) -> None:
        """A restored older database under the same config directory must not ride an earlier backup's clearance."""
        run = self._refuse(tmp_path)
        guard.mark_cleared(tmp_path, tmp_path / "x.dump")
        assert guard.gate(tmp_path, run, pg_data="/pg").kind == "cleared"
        guard.gate(tmp_path, _Cluster(ran=("hygiene-001-5", "hygiene-001-8")), pg_data="/pg")
        assert not guard.is_cleared(tmp_path)
        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, _Cluster(counts={"chunks": 4}), pg_data="/pg")
        assert guard.pending_refusal(tmp_path)["counts"] == {"chunks": 4, "catalog_document_chunks": 0}

    def test_a_sentinel_name_from_the_file_is_never_spliced_into_sql(self) -> None:
        asked: list[str] = []
        guard.drop_sentinel(asked.append, "x; DROP TABLE nexus.chunks")  # type: ignore[arg-type]
        assert asked == []

    # C3: could-not-ask is a wait, never a refusal and never an exit

    def test_a_guard_that_cannot_ask_raises_cannot_ask_and_records_nothing(self, tmp_path: Path) -> None:
        def broken(_sql: str) -> str:
            raise RuntimeError("the database system is starting up")

        with pytest.raises(guard.GuardCannotAsk, match="starting up"):
            guard.gate(tmp_path, broken, pg_data="/pg")
        assert list(tmp_path.iterdir()) == [], "no refusal record, no backup demand, no marker"

    def test_a_marker_for_this_cluster_lets_a_start_that_cannot_ask_go_on(self, tmp_path: Path) -> None:
        guard.write_marker(tmp_path, "/pg", "applied")

        def broken(_sql: str) -> str:
            raise RuntimeError("timeout")

        v = guard.gate(tmp_path, broken, pg_data="/pg")
        assert v.proceed and v.kind == "marker"

    def test_a_marker_for_another_data_directory_does_not(self, tmp_path: Path) -> None:
        guard.write_marker(tmp_path, "/old-pg", "applied")

        def broken(_sql: str) -> str:
            raise RuntimeError("timeout")

        with pytest.raises(guard.GuardCannotAsk):
            guard.gate(tmp_path, broken, pg_data="/new-pg")

    def test_the_marker_is_written_only_when_the_gate_saw_the_delete_applied(self, tmp_path: Path) -> None:
        guard.gate(tmp_path, _Cluster(changelog=False), pg_data="/pg")
        assert guard.read_marker(tmp_path, "/pg") is None, "no changelog is not yet evidence: the engine must be healthy"
        guard.gate(tmp_path, _Cluster(ran=("hygiene-001-5",), counts={}), pg_data="/pg")
        assert guard.read_marker(tmp_path, "/pg") is None, "hygiene-001-8 pending: not applied"
        guard.gate(tmp_path, _Cluster(ran=("hygiene-001-5", "hygiene-001-8")), pg_data="/pg")
        assert guard.read_marker(tmp_path, "/pg")["evidence"] == "applied"

    def test_the_cannot_ask_text_carries_no_credentials(self, tmp_path: Path) -> None:
        def broken(_sql: str) -> str:
            raise RuntimeError("connection failed password=hunter2secret")

        with pytest.raises(guard.GuardCannotAsk) as exc:
            guard.gate(tmp_path, broken, pg_data="/pg")
        assert "hunter2secret" not in str(exc.value)

    def test_the_psql_error_is_redacted(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("nexus.db.pg_provision.discover_pg_binaries",
                            lambda *_a, **_k: type("B", (), {"psql": Path("psql")})())
        monkeypatch.setattr("nexus.db.pg_provision.bootstrap_superuser", lambda *a, **k: "me")
        run = guard.bundled_run_sql(
            tmp_path, {"PG_PORT": "5", "PG_SUPERUSER_PASS": "s"},
            psql_runner=lambda cmd, **_k: subprocess.CompletedProcess(cmd, 2, "", "FATAL password=hunter2secret"),
        )
        with pytest.raises(RuntimeError) as exc:
            run("SELECT 1")
        assert "hunter2secret" not in str(exc.value) and "FATAL" in str(exc.value)


# ── backups: bound to this cluster, bounded in cost, and a missing pg_restore is not the user's fault ─────────────


def _listing(*entries: str) -> bytes:
    """A ``pg_restore -l`` listing: ``SCHEMA - name owner`` entries as written, anything else as a TABLE DATA entry."""
    return "".join(
        f"; {i}; 2615 {16390 + i} {e}\n" if e.startswith("SCHEMA - ") else f"; {i}; 0 {16390 + i} TABLE DATA {e}\n"
        for i, e in enumerate(entries)
    ).encode()


def _record(sentinel: str | None = "nx_legacy_guard_abcdef012345", required=(("chunks", True),
                                                                          ("catalog_document_chunks", False))) -> dict:
    return {"refused_at": T0.isoformat(), "counts": {"chunks": 3}, "sentinel": sentinel,
            "required": [list(r) for r in required], "rejected": {}}


_MARK = "SCHEMA - nx_legacy_guard_abcdef012345 nexus"
_FULL = _listing(_MARK, "nexus chunks nexus", "nexus catalog_document_chunks nexus")


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
#: rdr180-3's USING expression, verbatim: the value the converted bytea column holds.
_RDR180_USING = ("CASE WHEN chash ~ '^[0-9a-f]+$' AND length(chash) % 2 = 0 THEN decode(chash, 'hex') "
                 "ELSE convert_to(chash, 'UTF8') END")


def _seed_bytea_era(run, *, changesets: list[str], legacy_chunks: int, legacy_manifest: int) -> None:
    """The era after rdr180 and vectors-004: nexus.chunks and the manifest, chash bytea."""
    run(_CHANGELOG)
    for cs in changesets:
        run(f"INSERT INTO public.databasechangelog (id) VALUES ('{cs}')")
    run("CREATE SCHEMA nexus")
    run("CREATE TABLE nexus.chunks (chash bytea)")
    run("CREATE TABLE nexus.catalog_document_chunks (chash bytea)")
    run("INSERT INTO nexus.chunks SELECT decode(repeat('ab', 32), 'hex') FROM generate_series(1, 4)")
    for table, n in (("chunks", legacy_chunks), ("catalog_document_chunks", legacy_manifest)):
        if n:
            run(f"INSERT INTO nexus.{table} SELECT decode(repeat('cd', 16), 'hex') FROM generate_series(1, {n})")


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


# ── the supervisor: wiring into _start_locked, the waits, the run loop ──────────────────────────────────────────


class TestSupervisorWiring:
    def _sup(self, tmp_path: Path):
        sup = StorageServiceSupervisor.__new__(StorageServiceSupervisor)
        sup._config_dir = tmp_path
        sup._creds = {"PG_DATA": str(tmp_path / "pg"), "PG_PORT": "1"}
        return sup

    def test_the_guard_refuses_the_spawn_as_a_start_error(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(guard, "bundled_run_sql", lambda *_a, **_k: _Cluster(counts={"chunks": 2}))
        with pytest.raises(StorageServiceStartError) as exc:
            self._sup(tmp_path)._legacy_chash_guard()
        assert isinstance(exc.value.__cause__, guard.LegacyChashRefused)

    def test_a_guard_that_cannot_ask_blocks_the_start_without_a_refusal(self, tmp_path: Path, monkeypatch) -> None:
        """C3, flipped: the engine is not started unasked, and no refusal is recorded."""
        def broken(*_a, **_k):
            def run(_sql: str) -> str:
                raise RuntimeError("no psql")

            return run

        monkeypatch.setattr(guard, "bundled_run_sql", broken)
        with pytest.raises(StorageServiceStartError) as exc:
            self._sup(tmp_path)._legacy_chash_guard()
        assert isinstance(exc.value.__cause__, guard.GuardCannotAsk)
        assert guard.read_record(tmp_path) is None

    def test_managed_postgres_is_not_checked(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(guard, "bundled_run_sql", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError))
        sup = self._sup(tmp_path)
        sup._creds = {"PG_PORT": "1"}
        sup._legacy_chash_guard()

    def test_a_bundled_install_without_pg_data_is_still_checked(self, tmp_path: Path, monkeypatch) -> None:
        """S5: the superuser password only a bundled provision writes marks the bundled cluster."""
        monkeypatch.setattr(guard, "bundled_run_sql", lambda *_a, **_k: _Cluster(counts={"chunks": 2}))
        sup = self._sup(tmp_path)
        sup._creds = {"PG_PORT": "1", "PG_SUPERUSER_PASS": "x"}
        with pytest.raises(StorageServiceStartError):
            sup._legacy_chash_guard()

    def test_the_wait_retries_only_once_the_user_cleared_it_and_says_so_on_stderr(self, tmp_path: Path, monkeypatch,
                                                                                  capsys) -> None:
        monkeypatch.setattr(ssd, "_BELOW_FLOOR_RECHECK_S", 0.0)
        with pytest.raises(guard.LegacyChashRefused) as refused:
            guard.gate(tmp_path, _Cluster(counts={"chunks": 2}), pg_data="/pg")
        err = ssd.StorageServiceStartError(str(refused.value))
        err.__cause__ = refused.value
        asked: list[int] = []

        def is_cleared(cfg: Path) -> bool:
            asked.append(1)
            return len(asked) >= 3

        monkeypatch.setattr(guard, "is_cleared", is_cleared)
        got = ssd._wait_for_legacy_backup(self._sup(tmp_path), err, threading.Event(), lambda: None)
        assert got == ssd._ClusterFloorWait.RETRY and len(asked) == 3
        assert "nx backup" in capsys.readouterr().err, "--foreground has no CLI wrapper: the refusal goes to stderr"
        assert ssd._wait_for_legacy_backup(self._sup(tmp_path), ssd.StorageServiceStartError("other"),
                                           threading.Event(), lambda: None) is None

    def test_the_cannot_ask_wait_retries_and_stops(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(ssd, "_GUARD_RETRY_S", 0.0)
        err = ssd.StorageServiceStartError("x")
        err.__cause__ = guard.GuardCannotAsk("starting up")
        sup = self._sup(tmp_path)
        assert ssd._wait_for_guard_cannot_ask(sup, err, threading.Event(), lambda: None) == ssd._ClusterFloorWait.RETRY
        stopped = threading.Event()
        stopped.set()
        assert ssd._wait_for_guard_cannot_ask(sup, err, stopped, lambda: None) == ssd._ClusterFloorWait.STOPPED
        assert ssd._wait_for_guard_cannot_ask(sup, ssd.StorageServiceStartError("other"), threading.Event(),
                                              lambda: None) is None

    def test_the_run_loop_waits_on_cannot_ask_instead_of_exiting(self, tmp_path: Path, monkeypatch) -> None:
        """C3(1): never an exit (the unit would crash-loop): the loop starts over and a stop ends it with 0."""
        monkeypatch.setattr(ssd, "_GUARD_RETRY_S", 0.0)
        starts: list[int] = []

        class Sup:
            _config_dir = tmp_path

            def start(self, *, stop_requested=None):
                starts.append(1)
                if len(starts) == 1:
                    err = ssd.StorageServiceStartError("not yet")
                    err.__cause__ = guard.GuardCannotAsk("starting up")
                    raise err
                raise readiness.ReadinessStopRequestedError("stop")

            def stop(self):
                pass

        assert ssd._supervise_until_stopped(Sup(), threading.Event(), lambda: None) == 0
        assert len(starts) == 2


class TestStartLockedWiring:
    """I4: the central claim, that no engine spawns on an at-risk store, driven through ``_start_locked`` itself."""

    def _sup(self, tmp_path: Path, monkeypatch, gate):
        cfg = tmp_path / "cfg"
        cfg.mkdir(mode=0o700)
        sup = _make_supervisor(cfg, _FakeClock())
        order: list[str] = []
        sup._ensure_pg_running = lambda: order.append("ensure_pg")  # type: ignore[method-assign]
        sup._release_stale_changelog_lock = lambda reason: order.append("release_lock")  # type: ignore[method-assign]

        def spawn():
            order.append("spawn")
            return _FakeProc(pid=44900), 18077

        sup._spawn_service = spawn  # type: ignore[method-assign]
        sup._wait_for_service_ready = lambda *a, **k: order.append("ready")  # type: ignore[method-assign]
        sup._publish = lambda port: order.append("publish")  # type: ignore[method-assign]
        stub = MagicMock()
        stub.record.generation = 1
        sup._supervisor = stub

        def fake_gate(config_dir, run_sql, *, pg_data=""):
            order.append("guard")
            return gate()

        monkeypatch.setattr(guard, "gate", fake_gate)
        return sup, order, cfg

    def test_the_guard_runs_after_postgres_and_before_the_engine_spawns(self, tmp_path: Path, monkeypatch) -> None:
        sup, order, _ = self._sup(tmp_path, monkeypatch, lambda: guard.Verdict(True, "ok", {}, "clean"))
        sup._start_locked()
        assert order == ["ensure_pg", "release_lock", "guard", "spawn", "ready", "publish"]

    def test_a_refusal_reaches_no_spawn(self, tmp_path: Path, monkeypatch) -> None:
        def refuse():
            raise guard.LegacyChashRefused("nx backup first")

        sup, order, _ = self._sup(tmp_path, monkeypatch, refuse)
        with pytest.raises(StorageServiceStartError):
            sup._start_locked()
        assert "spawn" not in order and order[-1] == "guard"

    def test_a_guard_that_cannot_ask_reaches_no_spawn(self, tmp_path: Path, monkeypatch) -> None:
        def broken():
            raise guard.GuardCannotAsk("starting up")

        sup, order, _ = self._sup(tmp_path, monkeypatch, broken)
        with pytest.raises(StorageServiceStartError):
            sup._start_locked()
        assert "spawn" not in order

    def test_a_store_with_no_changelog_is_remembered_only_once_the_engine_is_healthy(self, tmp_path: Path,
                                                                                    monkeypatch) -> None:
        sup, order, cfg = self._sup(tmp_path, monkeypatch, lambda: guard.Verdict(True, "fresh", {}, "no_changelog"))
        pg_data = sup._creds["PG_DATA"]
        seen_at_ready: list[bool] = []
        sup._wait_for_service_ready = lambda *a, **k: seen_at_ready.append(  # type: ignore[method-assign]
            guard.read_marker(cfg, pg_data) is not None)
        sup._start_locked()
        assert seen_at_ready == [False], "not before the engine is healthy"
        assert guard.read_marker(cfg, pg_data)["evidence"] == "no_changelog"


# ── the CLI: exit 75, --legacy-backup on both start paths, and nx upgrade's message ─────────────────────────────


class TestCli:
    def _pending(self, tmp_path: Path) -> dict:
        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, _Cluster(counts={"chunks": 2}), pg_data="/pg")
        return guard.pending_refusal(tmp_path)

    def _no_pg(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(guard, "pg_restore_path", lambda _cfg: tmp_path / "pg_restore")
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    def test_a_pending_refusal_without_a_backup_exits_75_before_anything_starts(self, tmp_path: Path,
                                                                                monkeypatch) -> None:
        self._pending(tmp_path)
        self._no_pg(tmp_path, monkeypatch)
        spawned: list[int] = []
        monkeypatch.setattr("nexus.commands.daemon._start_via_autostart_unit", lambda *a, **k: spawned.append(1))
        r = CliRunner().invoke(main, ["daemon", "service", "start", "--config-dir", str(tmp_path)])
        assert r.exit_code == 75, r.output
        assert "nx backup" in r.stderr and not spawned

    def test_a_pg_restore_that_cannot_run_is_said_apart_from_a_bad_backup(self, tmp_path: Path, monkeypatch) -> None:
        self._pending(tmp_path)

        def cannot(_cfg):
            raise guard.PgRestoreUnavailable("no bundle")

        monkeypatch.setattr(guard, "pg_restore_path", cannot)
        r = CliRunner().invoke(main, ["daemon", "service", "start", "--config-dir", str(tmp_path)])
        assert r.exit_code == 75 and "pg_restore could not be run" in r.stderr and "not a problem with" in r.stderr

    def _dump(self, tmp_path: Path) -> Path:
        dump = tmp_path / "elsewhere.dump"
        dump.write_bytes(b"PGDMP")
        os.utime(dump, (datetime.now(UTC).timestamp() + 60,) * 2)
        return dump

    def _foreground(self, tmp_path: Path, monkeypatch, listing: bytes):
        self._no_pg(tmp_path, monkeypatch)
        monkeypatch.setattr("nexus.bounded_subprocess.run_bounded",
                            lambda argv, **_k: subprocess.CompletedProcess(argv, 0, listing, b""))
        ran: list[Path] = []
        monkeypatch.setattr("nexus.daemon.storage_service_daemon.run_storage_supervisor",
                            lambda config_dir: ran.append(config_dir) or 0)
        monkeypatch.setattr("nexus.daemon.service_registry.enter_stable_daemon_cwd", lambda: None)
        result = CliRunner().invoke(main, ["daemon", "service", "start", "--foreground", "--config-dir",
                                           str(tmp_path), "--legacy-backup", str(self._dump(tmp_path))])
        return result, ran

    def test_foreground_honours_legacy_backup(self, tmp_path: Path, monkeypatch) -> None:
        """S2: the unit execs --foreground; --legacy-backup there used to be ignored."""
        sentinel = self._pending(tmp_path)["sentinel"]
        listing = _listing(f"SCHEMA - {sentinel} nexus", "nexus chunks nexus", "nexus catalog_document_chunks nexus")
        r, ran = self._foreground(tmp_path, monkeypatch, listing)
        assert r.exit_code == 0, r.output
        assert ran and guard.is_cleared(tmp_path)

    def test_foreground_refuses_a_backup_that_is_not_this_stores(self, tmp_path: Path, monkeypatch) -> None:
        self._pending(tmp_path)
        r, ran = self._foreground(tmp_path, monkeypatch, _listing("nexus chunks nexus"))
        assert r.exit_code == 75 and not ran and "was not accepted" in r.stderr

    def test_nx_upgrade_names_the_guard_and_nx_backup(self) -> None:
        refusal = "Not starting the engine: this store still holds rows ... Run `nx backup` now"

        def run(argv, **_k):
            if argv[-1] == "start":
                return subprocess.CompletedProcess(argv, 75, "", f"Error: {refusal}")
            return subprocess.CompletedProcess(argv, 0, "", "")

        text = upgrade._restart(["nx"], run, lambda: None, "9.9.9", auto_mode=True)
        assert text and "legacy-chash guard" in text and "`nx backup`" in text and "exit 75" in text
