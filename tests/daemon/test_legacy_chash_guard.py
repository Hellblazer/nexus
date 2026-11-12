# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd: the guard's check, gate and reconcile, driven by a fake cluster that answers its SQL (every branch: the
role, the changelog, the era's tables, each table gated on its own changeset, the cluster a record is bound to, the
wait the user can see). Backups are in ``test_legacy_chash_guard_backup.py`` (with the real-Postgres cases), the
supervisor and CLI wiring in ``test_legacy_chash_guard_wiring.py``. Nothing here starts an engine or runs ``nx backup``."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.daemon import legacy_chash_guard as guard
from tests.daemon._legacy_chash_support import _RDR180_USING, _Cluster


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

    def test_the_text_predicate_is_the_changelogs_decode_condition(self) -> None:
        """The guard's text width follows rdr180-3's USING, read from the changelog: when the engine's rule changes,
        this goes red before a count is wrong in the field."""
        cond = re.search(r"WHEN (.*?) THEN", _RDR180_USING).group(1)
        assert cond in guard._predicate("text")

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


class TestCheckHardening:
    def test_a_role_that_cannot_see_every_row_cannot_ask(self, tmp_path: Path) -> None:
        """FORCE row-level security and the privilege-filtered catalogs read 0 to such a role: never "clean"."""
        with pytest.raises(RuntimeError, match="BYPASSRLS"):
            guard.check(_Cluster(role_ok=False, counts={"chunks": 5}))
        with pytest.raises(guard.GuardCannotAsk, match="BYPASSRLS"):
            guard.gate(tmp_path, _Cluster(role_ok=False, counts={"chunks": 5}), pg_data="/pg")
        assert list(tmp_path.iterdir()) == []

    def test_table_types_come_from_the_catalogs_not_the_privilege_filtered_view(self) -> None:
        run = _Cluster()
        guard.check(run)
        assert any("pg_attribute" in q and "format_type" in q for q in run.asked)
        assert not any("information_schema" in q for q in run.asked)

    def test_the_count_is_an_exists_probe_then_a_capped_count(self) -> None:
        run = _Cluster(counts={"chunks": 7, "catalog_document_chunks": 0})
        v = guard.check(run)
        assert v.counts == {"chunks": 7, "catalog_document_chunks": 0}
        queries = run.counted()
        assert [q.startswith("SELECT EXISTS") for q in queries] == [True, False, True], "exists, count, exists"
        assert f"LIMIT {guard.COUNT_CAP}" in queries[1]
        assert not [q for q in queries if "count(*)" in q and "catalog_document_chunks" in q], "0 rows: no count at all"

    def test_a_count_at_the_cap_reads_at_least(self) -> None:
        v = guard.check(_Cluster(counts={"chunks": guard.COUNT_CAP + 5}))
        assert v.counts["chunks"] == guard.COUNT_CAP
        text = guard.refusal_text({"counts": v.counts, "sentinel": "nx_legacy_guard_abcdef012345", "refused_at": "t"})
        assert "at least 1,000,000 chunk rows" in text

    def test_a_verdict_names_the_cluster(self) -> None:
        assert guard.check(_Cluster(sid="424242")).cluster == "424242"


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

    def test_the_refusal_names_the_cluster_it_is_about(self, tmp_path: Path) -> None:
        self._refuse(tmp_path, _Cluster(counts={"chunks": 1}, sid="111"))
        assert guard.pending_refusal(tmp_path)["cluster"] == {"pg_data": "/pg", "system_identifier": "111"}

    def test_a_pending_refusal_for_another_cluster_is_retired_and_a_new_one_made(self, tmp_path: Path) -> None:
        """A store wiped or re-initialised under the same config dir would otherwise get 75 forever."""
        run = self._refuse(tmp_path, _Cluster(counts={"chunks": 1}, sid="111"))
        first = guard.pending_refusal(tmp_path)
        other = _Cluster(counts={"chunks": 2}, sid="222")
        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, other, pg_data="/pg")
        record = guard.pending_refusal(tmp_path)
        assert record["cluster"]["system_identifier"] == "222" and record["sentinel"] != first["sentinel"]
        assert record["refused_at"] != first["refused_at"] and len(other.sentinels) == 1 and run.sentinels

    def test_a_clearance_for_another_cluster_is_not_honoured(self, tmp_path: Path) -> None:
        self._refuse(tmp_path, _Cluster(counts={"chunks": 1}, sid="111"))
        guard.mark_cleared(tmp_path, tmp_path / "x.dump")
        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, _Cluster(counts={"chunks": 1}, sid="222"), pg_data="/pg")
        assert guard.read_record(tmp_path)["cluster"]["system_identifier"] == "222"

    def test_a_clearance_for_the_same_cluster_still_is(self, tmp_path: Path) -> None:
        run = self._refuse(tmp_path, _Cluster(counts={"chunks": 1}, sid="111"))
        guard.mark_cleared(tmp_path, tmp_path / "x.dump")
        assert guard.gate(tmp_path, run, pg_data="/other-path-same-cluster").kind == "cleared"

    def test_a_store_no_longer_at_risk_retires_the_pending_refusal(self, tmp_path: Path) -> None:
        run = self._refuse(tmp_path, _Cluster(counts={"chunks": 1}))
        sentinel = guard.pending_refusal(tmp_path)["sentinel"]
        run.counts = {}
        assert guard.gate(tmp_path, run, pg_data="/pg").proceed
        assert guard.pending_refusal(tmp_path) is None and guard.read_record(tmp_path)["retired_at"]
        assert sentinel not in run.schemas

    def test_the_marker_schema_is_ensured_on_every_refusal(self, tmp_path: Path) -> None:
        run = self._refuse(tmp_path, _Cluster(counts={"chunks": 1}))
        sentinel = guard.pending_refusal(tmp_path)["sentinel"]
        run.schemas.clear()  # the cluster lost it
        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, run, pg_data="/pg")
        assert sentinel in run.schemas and guard.pending_refusal(tmp_path)["sentinel"] == sentinel

    def test_the_wait_status_is_cleared_when_the_guard_can_ask_again(self, tmp_path: Path) -> None:
        guard.write_wait_status(tmp_path, "starting up", tmp_path / "log")
        guard.gate(tmp_path, _Cluster(changelog=False), pg_data="/pg")
        assert guard.wait_status_text(tmp_path) is None and not (tmp_path / guard.WAIT_FILENAME).exists()

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


class TestReconcileAndWaitStatus:
    """A pending refusal stays satisfiable and true while the supervisor waits (and when the CLI starts)."""

    def _pending(self, tmp_path: Path, run: _Cluster | None = None) -> _Cluster:
        run = run or _Cluster(counts={"chunks": 3}, sid="111")
        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, run, pg_data="/pg")
        return run

    def test_no_pending_record_is_none(self, tmp_path: Path) -> None:
        assert guard.reconcile_pending(tmp_path, _Cluster(), "/pg", recheck=True) == "none"

    def test_a_record_without_a_marker_schema_gets_one(self, tmp_path: Path) -> None:
        """The CREATE SCHEMA that failed at refusal is retried: the text's "the supervisor retries" is true."""
        class NoDdl(_Cluster):
            fail = True

            def __call__(self, sql: str) -> str:
                if sql.startswith("CREATE SCHEMA") and self.fail:
                    raise RuntimeError("disk full")
                return super().__call__(sql)

        run = NoDdl(counts={"chunks": 3}, sid="111")
        self._pending(tmp_path, run)
        assert guard.pending_refusal(tmp_path)["sentinel"] is None
        run.fail = False
        assert guard.reconcile_pending(tmp_path, run, "/pg", recheck=False) == "pending"
        name = guard.pending_refusal(tmp_path)["sentinel"]
        assert name in run.schemas and re.fullmatch(r"nx_legacy_guard_[0-9a-f]{12}", name)

    def test_a_marker_schema_the_cluster_lost_is_recreated_under_the_same_name(self, tmp_path: Path) -> None:
        run = self._pending(tmp_path)
        name = guard.pending_refusal(tmp_path)["sentinel"]
        run.schemas.clear()
        assert guard.reconcile_pending(tmp_path, run, "/pg", recheck=False) == "pending"
        assert run.schemas == {name} and guard.pending_refusal(tmp_path)["sentinel"] == name

    def test_a_record_for_another_cluster_is_retired_not_applied(self, tmp_path: Path) -> None:
        self._pending(tmp_path)
        other = _Cluster(counts={"chunks": 3}, sid="999")
        assert guard.reconcile_pending(tmp_path, other, "/pg", recheck=True) == "retired"
        assert guard.pending_refusal(tmp_path) is None
        assert "another cluster" in guard.read_record(tmp_path)["retired_reason"]
        assert not other.sentinels, "no marker schema is made for a cluster the record is not about"

    def test_a_store_no_longer_at_risk_retires_the_record_and_drops_the_schema(self, tmp_path: Path) -> None:
        run = self._pending(tmp_path)
        name = guard.pending_refusal(tmp_path)["sentinel"]
        run.counts = {}
        assert guard.reconcile_pending(tmp_path, run, "/pg", recheck=True) == "retired"
        assert "no longer at risk" in guard.read_record(tmp_path)["retired_reason"] and name not in run.schemas

    def test_without_recheck_the_tables_are_not_counted(self, tmp_path: Path) -> None:
        run = self._pending(tmp_path)
        run.asked.clear()
        run.counts = {}
        assert guard.reconcile_pending(tmp_path, run, "/pg", recheck=False) == "pending"
        assert not run.counted()

    def test_the_wait_round_rechecks_once_a_minute_and_survives_errors(self, tmp_path: Path, monkeypatch) -> None:
        run = self._pending(tmp_path)
        monkeypatch.setattr(guard, "bundled_run_sql", lambda *_a, **_k: run)
        now = [1000.0]
        state: dict = {}

        def round_() -> str:
            run.asked.clear()
            out = guard.wait_round(tmp_path, {"PG_DATA": "/pg"}, state, clock=lambda: now[0])
            assert out == "pending"
            return str(bool(run.counted()))

        assert [round_()] == ["True"], "the first round does the full check"
        now[0] += 5
        assert [round_()] == ["False"], "five seconds later it only ensures the marker"
        now[0] += 60
        assert [round_()] == ["True"]

        def broken(*_a, **_k):
            raise RuntimeError("starting up password=hunter2secret")

        monkeypatch.setattr(guard, "bundled_run_sql", broken)
        assert guard.wait_round(tmp_path, {}, {}) == "unknown"

    def test_the_wait_status_carries_the_cause_and_goes_stale(self, tmp_path: Path) -> None:
        text = guard.write_wait_status(tmp_path, "the database system is starting up", tmp_path / "logs" / "x.log")
        assert "starting up" in text and "Nothing is wrong with your data" in text and "nx doctor" in text
        assert guard.wait_status_text(tmp_path) == text
        first = json.loads((tmp_path / guard.WAIT_FILENAME).read_text(encoding="utf-8"))["since"]
        guard.write_wait_status(tmp_path, "still starting up", tmp_path / "logs" / "x.log")
        assert json.loads((tmp_path / guard.WAIT_FILENAME).read_text(encoding="utf-8"))["since"] == first
        old = (datetime.now(UTC) - timedelta(minutes=5)).timestamp()
        os.utime(tmp_path / guard.WAIT_FILENAME, (old, old))
        assert guard.wait_status_text(tmp_path) is None, "a status from a supervisor that is gone is not shown"

    def test_try_clear_does_not_undo_a_clearance_another_start_wrote(self, tmp_path: Path) -> None:
        self._pending(tmp_path)
        dump = tmp_path / "nexus-backups" / "nexus-a.dump"
        dump.parent.mkdir()
        dump.write_bytes(b"PGDMP")
        os.utime(dump, (datetime.now(UTC).timestamp() + 60,) * 2)

        def runner(argv, **_k):  # while this start reads the dump, a second `nx` clears the refusal
            guard.mark_cleared(tmp_path, tmp_path / "other.dump")
            return subprocess.CompletedProcess(argv, 1, b"", b"not a dump")

        assert guard.try_clear(tmp_path, pg_restore=Path("x"), home=tmp_path, runner=runner) is None
        record = guard.read_record(tmp_path)
        assert record["cleared_at"] and record["cleared_by"].endswith("other.dump")
