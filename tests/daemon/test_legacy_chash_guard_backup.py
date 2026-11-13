# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd: which dump counts as the copy the guard waits for.

Driven by a fake ``pg_restore``. The same rules against real Postgres and real dumps are in
``test_legacy_chash_guard_real_pg.py``."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.daemon import legacy_chash_guard as guard
from tests.daemon._legacy_chash_support import (
    _FULL,
    _MARK,
    T0,
    _Cluster,
    _listing,
    _record,
    clear_record,
)


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

    def test_a_full_read_that_times_out_is_not_accepted_and_is_remembered_and_named(self, tmp_path: Path) -> None:
        dump = self._dump(tmp_path, "nexus-big.dump", T0 + timedelta(minutes=1))
        rec = _record()
        calls: list[list[str]] = []
        runner = self._split_runner(_FULL, subprocess.TimeoutExpired("pg_restore", 1), calls)
        assert guard.verified_backup_since(rec, pg_restore=Path("pg_restore"), home=tmp_path, runner=runner) is None
        assert next(iter(rec["rejected"].values())) == "timed out reading after 30 min"
        guard.verified_backup_since(rec, pg_restore=Path("pg_restore"), home=tmp_path, runner=runner)
        assert len(calls) == 2, "not silently re-read forever (every client call, 30 minutes each)"
        assert "nexus-big.dump (timed out reading after 30 min)" in guard.refusal_text(rec, home=tmp_path)
        # naming the dump with --legacy-backup is the user's call to try again
        ok = self._split_runner(_FULL, 0, calls)
        assert guard.verified_backup_since(rec, pg_restore=Path("pg_restore"), home=tmp_path, explicit=dump,
                                           runner=ok) == dump

    def test_a_dump_rejected_as_incomplete_is_not_retried_even_when_named(self, tmp_path: Path) -> None:
        dump = self._dump(tmp_path, "nexus-cut.dump", T0 + timedelta(minutes=1))
        rec = _record()
        calls: list[list[str]] = []
        runner = self._split_runner(_FULL, 1, calls)
        guard.verified_backup_since(rec, pg_restore=Path("pg_restore"), home=tmp_path, explicit=dump, runner=runner)
        guard.verified_backup_since(rec, pg_restore=Path("pg_restore"), home=tmp_path, explicit=dump, runner=runner)
        assert len(calls) == 2

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
        clear_record(tmp_path, "nexus-backups/nexus-good.dump", now=T0 + timedelta(hours=1))
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
