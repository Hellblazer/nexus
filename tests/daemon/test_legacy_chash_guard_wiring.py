# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd: the guard's wiring: the supervisor's step in ``_start_locked`` (driven with fakes, so it fails if the
guard is unwired, runs after the spawn, or a refusal reaches it), the waits and the run loop, the CLI's exit 75, and the
messages ``nx upgrade`` and the replace-quiesce restart print."""

from __future__ import annotations

import os
import subprocess
import threading
from datetime import UTC, datetime
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
from tests.daemon._legacy_chash_support import _Cluster, _listing
from tests.daemon.test_storage_service_daemon import _FakeClock, _FakeProc, _make_supervisor


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
        monkeypatch.setattr(guard, "wait_round", lambda *_a, **_k: "pending")
        got = ssd._wait_for_legacy_backup(self._sup(tmp_path), err, threading.Event(), lambda: None)
        assert got == ssd._ClusterFloorWait.RETRY and len(asked) == 3
        assert "nx backup" in capsys.readouterr().err, "--foreground has no CLI wrapper: the refusal goes to stderr"
        assert ssd._wait_for_legacy_backup(self._sup(tmp_path), ssd.StorageServiceStartError("other"),
                                           threading.Event(), lambda: None) is None

    def test_the_wait_drops_a_record_that_no_longer_applies_and_starts_over(self, tmp_path: Path,
                                                                           monkeypatch) -> None:
        monkeypatch.setattr(ssd, "_BELOW_FLOOR_RECHECK_S", 0.0)
        with pytest.raises(guard.LegacyChashRefused) as refused:
            guard.gate(tmp_path, _Cluster(counts={"chunks": 2}), pg_data="/pg")
        err = ssd.StorageServiceStartError(str(refused.value))
        err.__cause__ = refused.value
        seen: list[tuple] = []

        def round_(config_dir, creds, state, **_k):
            seen.append((config_dir, creds["PG_DATA"], state is not None))
            return "retired"

        monkeypatch.setattr(guard, "wait_round", round_)
        monkeypatch.setattr(guard, "is_cleared", lambda _c: pytest.fail("a retired record must not wait for a backup"))
        sup = self._sup(tmp_path)
        assert ssd._wait_for_legacy_backup(sup, err, threading.Event(), lambda: None) == ssd._ClusterFloorWait.RETRY
        assert seen == [(tmp_path, str(tmp_path / "pg"), True)]

    def test_a_skipped_guard_says_so_in_the_log(self, tmp_path: Path, monkeypatch) -> None:
        """Managed/BYO Postgres (or a bundled cluster whose credentials lost both keys) is not checked: say it."""
        said: list[tuple[str, dict]] = []

        class Log:
            def info(self, event: str, **kw) -> None:
                said.append((event, kw))

            def __getattr__(self, _name):
                return lambda *_a, **_k: None

        monkeypatch.setattr(ssd, "_log", Log())
        sup = self._sup(tmp_path)
        sup._creds = {"PG_PORT": "1"}
        sup._legacy_chash_guard()
        assert [e for e, _ in said] == ["legacy_chash_guard_skipped"] and "managed/BYO" in said[0][1]["reason"]

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

    def test_a_cannot_ask_wait_says_why_to_stderr_once_and_to_the_cli_through_a_status_file(self, tmp_path: Path,
                                                                                          monkeypatch, capsys) -> None:
        monkeypatch.setattr(ssd, "_GUARD_RETRY_S", 0.0)
        err = ssd.StorageServiceStartError("x")
        err.__cause__ = guard.GuardCannotAsk("RuntimeError: the database system is starting up")
        sup = self._sup(tmp_path)
        for _ in range(3):
            assert ssd._wait_for_guard_cannot_ask(sup, err, threading.Event(), lambda: None) == \
                ssd._ClusterFloorWait.RETRY
        said = capsys.readouterr().err
        assert said.count("has not started yet") == 1 and "starting up" in said and "nx doctor" in said
        assert "starting up" in guard.wait_status_text(tmp_path)
        assert str(tmp_path / "logs" / "storage_service.log") in guard.wait_status_text(tmp_path)

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
        assert text and "legacy-chash guard" in text and "`nx backup`" in text and "no time limit" in text

    def test_a_waiting_supervisor_is_said_once_on_stderr_and_added_to_a_timeout(self, tmp_path: Path, capsys) -> None:
        from nexus.commands import daemon as cmd  # noqa: PLC0415 — the module under test, imported where it is used

        guard.write_wait_status(tmp_path, "the database system is starting up", tmp_path / "logs" / "x.log")
        assert cmd._legacy_chash_refusal(tmp_path) is None
        assert cmd._legacy_chash_refusal(tmp_path) is None
        err = capsys.readouterr().err
        assert err.count("has not started yet") == 1 and "starting up" in err
        assert "No backup is needed while it waits" in cmd._legacy_chash_waiting_note(tmp_path)
        guard.clear_wait_status(tmp_path)
        assert cmd._legacy_chash_waiting_note(tmp_path) == ""

    def _creds(self, tmp_path: Path) -> None:
        (tmp_path / "pg_credentials").write_text("PG_PORT=1\nPG_DATA=/pg\n", encoding="utf-8")

    def test_a_refusal_for_another_cluster_is_retired_by_the_next_start(self, tmp_path: Path, monkeypatch,
                                                                       capsys) -> None:
        from nexus.commands import daemon as cmd  # noqa: PLC0415 — the module under test, imported where it is used

        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, _Cluster(counts={"chunks": 2}, sid="111"), pg_data="/pg")
        self._creds(tmp_path)
        monkeypatch.setattr(guard, "bundled_run_sql", lambda *_a, **_k: _Cluster(counts={"chunks": 2}, sid="222"))
        cmd._clear_legacy_chash_refusal(tmp_path, None)  # no exit 75: the refusal was about a cluster that is gone
        assert guard.pending_refusal(tmp_path) is None and "no longer applies" in capsys.readouterr().err

    def test_the_next_start_keeps_the_marker_schema_of_a_refusal_that_still_applies(self, tmp_path: Path,
                                                                                   monkeypatch) -> None:
        from nexus.commands import daemon as cmd  # noqa: PLC0415 — the module under test, imported where it is used

        run = _Cluster(counts={"chunks": 2}, sid="111")
        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, run, pg_data="/pg")
        name = guard.pending_refusal(tmp_path)["sentinel"]
        run.schemas.clear()
        self._creds(tmp_path)
        monkeypatch.setattr(guard, "bundled_run_sql", lambda *_a, **_k: run)
        monkeypatch.setattr(guard, "pg_restore_path", lambda _c: tmp_path / "pg_restore")
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        with pytest.raises(StorageServiceStartError) as exc:
            cmd._clear_legacy_chash_refusal(tmp_path, None)
        assert exc.value.exit_code == 75 and name in run.schemas

    def test_the_restart_after_a_replace_names_nx_backup_when_the_guard_refused(self) -> None:
        from nexus.daemon import replace_quiesce as rq  # noqa: PLC0415 — the module under test, imported where it is used

        def refuse() -> None:
            raise ssd.LegacyChashStartRefused("Not starting the engine. Run `nx backup` first.")

        ops = rq.QuiesceOps(stop_service=lambda: None, pg_running=lambda: False, stop_pg=lambda: None,
                            start_pg=lambda: None, start_service=refuse)
        with pytest.raises(rq.RestartAfterReplaceError) as exc:
            rq._restart(ops, rq.QuiesceState(stopped_service=True))
        assert "Run `nx backup`, then 'nx daemon service start'." in str(exc.value)

        def other() -> None:
            raise RuntimeError("boom")

        ops2 = rq.QuiesceOps(stop_service=lambda: None, pg_running=lambda: False, stop_pg=lambda: None,
                             start_pg=lambda: None, start_service=other)
        with pytest.raises(rq.RestartAfterReplaceError) as exc2:
            rq._restart(ops2, rq.QuiesceState(stopped_service=True))
        assert "nx backup" not in str(exc2.value) and "Run 'nx daemon service start'." in str(exc2.value)

    # round 3, item 6: a pending refusal with Postgres down must not be a dead end

    def _down(self, tmp_path: Path, monkeypatch, *, accepting: bool) -> None:
        (tmp_path / "pg_credentials").write_text("PG_PORT=5999\nPG_DATA=/pg\n", encoding="utf-8")
        monkeypatch.setattr(guard, "pg_restore_path", lambda _c: tmp_path / "pg_restore")
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        monkeypatch.setattr(ssd, "_port_accepting", lambda _host, _port: accepting)

        def cannot(*_a, **_k):
            def run(_sql: str) -> str:
                raise RuntimeError("could not connect")

            return run

        monkeypatch.setattr(guard, "bundled_run_sql", cannot)

    def test_with_postgres_down_the_start_goes_on_so_the_supervisor_can_bring_it_up(self, tmp_path: Path, monkeypatch,
                                                                                    capsys) -> None:
        from nexus.commands import daemon as cmd  # noqa: PLC0415 — the module under test, imported where it is used

        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, _Cluster(counts={"chunks": 2}), pg_data="/pg")
        self._down(tmp_path, monkeypatch, accepting=False)
        cmd._clear_legacy_chash_refusal(tmp_path, None)  # no exit 75: nothing else could start Postgres
        assert "Postgres is not running; starting it" in capsys.readouterr().err
        assert guard.pending_refusal(tmp_path) is not None, "the refusal still stands; the engine stays stopped"

    def test_with_postgres_up_the_refusal_is_raised(self, tmp_path: Path, monkeypatch) -> None:
        from nexus.commands import daemon as cmd  # noqa: PLC0415 — the module under test, imported where it is used

        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, _Cluster(counts={"chunks": 2}), pg_data="/pg")
        self._down(tmp_path, monkeypatch, accepting=True)
        with pytest.raises(StorageServiceStartError) as exc:
            cmd._clear_legacy_chash_refusal(tmp_path, None)
        assert exc.value.exit_code == 75

    def test_a_named_backup_that_is_not_accepted_is_said_even_with_postgres_down(self, tmp_path: Path,
                                                                                 monkeypatch) -> None:
        from nexus.commands import daemon as cmd  # noqa: PLC0415 — the module under test, imported where it is used

        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, _Cluster(counts={"chunks": 2}), pg_data="/pg")
        self._down(tmp_path, monkeypatch, accepting=False)
        monkeypatch.setattr("nexus.bounded_subprocess.run_bounded",
                            lambda argv, **_k: subprocess.CompletedProcess(argv, 0, _listing("nexus chunks nexus"), b""))
        with pytest.raises(StorageServiceStartError, match="was not accepted"):
            cmd._clear_legacy_chash_refusal(tmp_path, self._dump(tmp_path))

    def test_the_start_waits_for_postgres_before_it_raises_the_refusal(self, tmp_path: Path, monkeypatch) -> None:
        """The spawned supervisor is still bringing Postgres up; the refusal promises it is left running."""
        from nexus.commands import daemon as cmd  # noqa: PLC0415 — the module under test, imported where it is used

        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, _Cluster(counts={"chunks": 2}), pg_data="/pg")
        self._down(tmp_path, monkeypatch, accepting=False)
        assert cmd._legacy_chash_refusal(tmp_path) is None
        monkeypatch.setattr(ssd, "_port_accepting", lambda _host, _port: True)
        assert cmd._legacy_chash_refusal(tmp_path).exit_code == 75

    # round 3, item 7: nx upgrade reports the guard, not "still starting"

    def _upgrade(self, monkeypatch, tmp_path: Path, *, timeout: bool):
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

        def run(argv, **_k):
            if argv[-1] == "start" and timeout:
                raise subprocess.TimeoutExpired(argv, 150)
            return subprocess.CompletedProcess(argv, 0, "", "")

        return upgrade._restart(["nx"], run, lambda: None, "9.9.9", auto_mode=True)

    def test_an_upgrade_whose_start_timed_out_reading_a_dump_reports_the_guard(self, tmp_path: Path,
                                                                              monkeypatch) -> None:
        with pytest.raises(guard.LegacyChashRefused):
            guard.gate(tmp_path, _Cluster(counts={"chunks": 2}), pg_data="/pg")
        text = self._upgrade(monkeypatch, tmp_path, timeout=True)
        assert text and "legacy-chash guard" in text and "`nx backup`" in text
        assert "`nx daemon service start`" in text and "no time limit" in text

    def test_an_upgrade_start_that_timed_out_with_no_refusal_is_still_starting(self, tmp_path: Path,
                                                                              monkeypatch) -> None:
        assert self._upgrade(monkeypatch, tmp_path, timeout=True) is None
