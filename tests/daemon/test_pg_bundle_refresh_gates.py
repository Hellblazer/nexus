# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd.2.23, review round 2: the idle rule of the bundle swap (a parked data move is not work), the engine-change path's
attended and idle posture, the minors, the doctor row's wiring and the real bodies of the probes."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from nexus import health
from nexus.daemon import replace_quiesce as rq
from nexus.daemon import pg_bundle_refresh
from nexus.db import http_engine_status, pg_bundle, pg_provision, service_endpoint
from tests.daemon._pg_bundle_world import (  # noqa: F401 - the fixtures are used by name
    _pg_ctl_runs_its_script,
    _PINNED_TAG,
    _converge,
    _nx_upgrade,
    _service_starts,
    world,
)


# -- review round 2 (nexus-f3yqd.2.23): the busy rule, the engine-change posture, the minors ---------------


_real_busy = pg_bundle_refresh.engine_busy_reason  # the fixture replaces the module attribute; this is the real one


def _status(*entries: dict) -> dict:
    return {"boot_work": list(entries)}


def _move(label: str, what: str, **extra) -> dict:
    return {"id": f"data-move:{label}", "what": what, **extra}


def _busy_with(monkeypatch, tmp_path, status):
    monkeypatch.setattr(pg_bundle_refresh, "_engine_status", lambda: status)
    monkeypatch.setattr(pg_bundle_refresh, "_service_up", lambda _cfg: True)
    monkeypatch.setattr(pg_bundle_refresh, "_progress_rows", lambda _cfg: 0)
    return pg_bundle_refresh.engine_busy_reason(tmp_path)


def test_a_move_blocked_on_a_missing_pg_background_is_not_work_in_flight(tmp_path, monkeypatch):
    """The state the refresh exists to fix: the engine reports every open move in boot_work, and a blocked one must not
    defer the very swap that unblocks it."""
    blocked = _move("m1", "blocked: pg_background is not installed", reason_class="missing_prerequisite")
    assert _busy_with(monkeypatch, tmp_path, _status(blocked)) is None


def test_failed_and_paused_moves_are_not_work_either(tmp_path, monkeypatch):
    status = _status(_move("f", "failed: f: boom"), _move("p", "paused: operator hold"))
    assert _busy_with(monkeypatch, tmp_path, status) is None


def test_working_queued_and_stalled_moves_and_other_entries_are_work(tmp_path, monkeypatch):
    for entry in (
        _move("w", "working: w: 3 of 9 chunks"), _move("q", "queued: q"), _move("s", "stalled: s: no chunk for 20 min"),
        {"id": "pci-build", "what": "building indexes"}, {"id": "data-move-worker", "what": "blocked: the worker is not running"},
    ):
        assert "background work" in _busy_with(monkeypatch, tmp_path, _status(entry)), entry


def test_a_parked_move_beside_a_working_one_still_reports_the_working_one(tmp_path, monkeypatch):
    status = _status(_move("m1", "blocked: pg_background is not installed"), _move("w", "working: w"))
    reason = _busy_with(monkeypatch, tmp_path, status)
    assert "working: w" in reason and "blocked" not in reason


def test_the_converged_leg_runs_while_a_move_is_blocked_on_the_bundle(world, monkeypatch):
    """With the REAL busy rule (not the fixture's stub): a status whose only boot_work is the blocked move."""
    world.install_old(background=False)
    world.ship(background=True)
    monkeypatch.setattr(pg_bundle_refresh, "engine_busy_reason", _real_busy)
    monkeypatch.setattr(pg_bundle_refresh, "_engine_status", lambda: _status(
        _move("m1", "blocked: pg_background is not installed", reason_class="missing_prerequisite")))
    monkeypatch.setattr(pg_bundle_refresh, "_service_up", lambda _cfg: True)
    monkeypatch.setattr(pg_bundle_refresh, "_progress_rows", lambda _cfg: 0)

    actions, _i, _c = _converge(world, engine="converged")

    assert world.has_background() and any("refreshed" in a for a in actions), actions


def test_on_a_bundle_without_pg_background_no_move_is_work_but_an_index_build_is(tmp_path, monkeypatch):
    """F33: no data move can run without pg_background, so a queued or working one waits for the swap itself."""
    monkeypatch.setattr(pg_bundle_refresh, "_engine_status", lambda: status)
    monkeypatch.setattr(pg_bundle_refresh, "_service_up", lambda _cfg: True)
    monkeypatch.setattr(pg_bundle_refresh, "_progress_rows", lambda _cfg: 0)
    status = _status(_move("q", "queued: q (the worker has not reached it yet)"), _move("w", "working: w"))
    assert pg_bundle_refresh.engine_busy_reason(tmp_path, moves_can_run=False) is None
    status = _status(_move("q", "queued: q"), {"id": "pci-build", "what": "building indexes"})
    reason = pg_bundle_refresh.engine_busy_reason(tmp_path, moves_can_run=False)
    assert "building indexes" in reason and "queued" not in reason


def _real_busy_with_queued_moves(world, monkeypatch):
    world.install_old(background=False)
    world.ship(background=True)
    monkeypatch.setattr(pg_bundle_refresh, "engine_busy_reason", _real_busy)
    monkeypatch.setattr(pg_bundle_refresh, "_engine_status", lambda: _status(
        _move("repair-catalog-collection-model-version", "queued: repair-catalog-collection-model-version (the worker has not reached it yet)"),
        _move("rdr229-reown-repo-stems-from-curator", "queued: rdr229-reown-repo-stems-from-curator")))
    monkeypatch.setattr(pg_bundle_refresh, "_service_up", lambda _cfg: True)
    monkeypatch.setattr(pg_bundle_refresh, "_progress_rows", lambda _cfg: 0)


def test_the_converged_leg_runs_while_moves_are_queued_on_a_bundle_without_pg_background(world, monkeypatch):
    """F33, the 8.2.0 rehearsal leg: the finish pass installed the engine and held the bundle (unattended); the walking
    nx upgrade then found the registered moves queued and deferred the swap they wait for. They must not defer it."""
    _real_busy_with_queued_moves(world, monkeypatch)

    actions, _i, _c = _converge(world, engine="converged")

    assert world.has_background() and any("refreshed" in a for a in actions), actions


def test_the_engine_change_path_swaps_while_moves_are_queued_on_a_bundle_without_pg_background(world, monkeypatch):
    _real_busy_with_queued_moves(world, monkeypatch)

    actions, install, _c = _converge(world, engine="older")

    install.assert_called_once()
    assert world.has_background() and not any("held back" in a for a in actions), actions


def test_the_deferral_names_the_explicit_route_too(world):
    world.install_old(background=False)
    world.ship(background=True)
    world.busy = "the engine reports background work (working: w)"

    actions, _i, _c = _converge(world, engine="converged")

    assert any("nx upgrade" in a and f"nx daemon service install-binary {_PINNED_TAG}" in a for a in actions), actions


def test_the_engine_change_path_holds_the_bundle_back_while_the_engine_is_busy(world):
    """The bundle swap stops PostgreSQL, which an engine replacement alone does not: a busy engine installs the engine
    as always and leaves the bundle for the next attended run."""
    world.install_old(background=False)
    world.ship(background=True)
    world.busy = "the engine reports background work (index build)"

    actions, install, cycle = _converge(world, engine="older")

    install.assert_called_once()
    assert _service_starts(cycle) == 1  # the engine still converges
    assert not world.has_background() and world.downloads == [] and "stop_pg" not in world.stops
    assert any("held back" in a and "index build" in a and "nx upgrade" in a for a in actions), actions
    assert any("converged engine" in a for a in actions)


def test_the_engine_change_path_holds_the_bundle_back_when_unattended(world):
    world.install_old(background=False)
    world.ship(background=True)

    actions, install, cycle = _converge(world, engine="older", unattended=True)

    install.assert_called_once()
    assert _service_starts(cycle) == 1
    assert not world.has_background() and world.downloads == [] and "stop_pg" not in world.stops
    assert any("held back" in a and "unattended" in a for a in actions), actions


def test_an_unattended_engine_change_says_nothing_about_a_bundle_that_has_pg_background(world):
    world.install_old(background=True, variant="old")
    world.ship(background=True, variant="new")

    actions, install, _cycle = _converge(world, engine="older", unattended=True)

    install.assert_called_once()
    assert world.downloads == [] and world.stops == []
    assert all("bundle" not in a.lower() for a in actions), actions


def test_the_lockstep_detached_nx_upgrade_only_reports_the_old_bundle(world, monkeypatch):
    """NX_UPGRADE_UNATTENDED=1 nx upgrade on a converged engine with an old bundle: the precondition stage passes the
    posture to converge_engine, so it reports and changes nothing."""
    monkeypatch.setenv("NX_UPGRADE_UNATTENDED", "1")
    world.install_old(background=False)
    world.ship(background=True)

    result, install, _cycle = _nx_upgrade(world, monkeypatch)

    assert result.exit_code == 0, result.output
    install.assert_not_called()
    assert world.downloads == [] and world.stops == [] and not world.has_background()
    assert "pg_background" in result.output and "nx upgrade" in result.output


# -- minors ---------------------------------------------------------------------------------------------


def test_a_refused_swap_is_not_downloaded_again_by_the_next_pass(world, monkeypatch):
    world.install_old(background=False, cluster="17", variant="old")
    world.ship(background=True, pg_version="18.1", variant="new")

    first, _i, _c = _converge(world, engine="converged")
    assert any("major" in a for a in first) and len(world.downloads) == 1

    again, _i, _c = _converge(world, engine="converged")  # the same process: the refusal is cached
    assert len(world.downloads) == 1 and any("major" in a and "already tried" in a for a in again), again

    monkeypatch.setattr(pg_bundle_refresh, "_REFRESH_FAILURES", {})  # a new process: the placed archive is re-checked
    third, _i, _c = _converge(world, engine="converged")
    assert len(world.downloads) == 1, "the archive is already placed for this tag: no download"
    assert any("major" in a for a in third), third
    assert all("tries again" not in a and "retry" not in a.lower() for a in first + again + third)


def test_a_swap_from_a_bundle_without_pg_background_to_another_without_it_does_nothing(world):
    world.install_old(background=False, variant="old")
    world.ship(background=False, variant="also-old")  # different bytes, still no pg_background

    actions, _i, _c = _converge(world, engine="converged")

    assert world.stops == [] and not world.has_background()
    assert any("does not contain pg_background" in a for a in actions), actions
    assert not pg_bundle.staging_dir(world.root).exists()


def test_a_problem_text_that_ends_in_a_period_does_not_double_it():
    line = pg_bundle_refresh.BundleRefresh(problem="a major change needs pg_upgrade first.").lines()[0]
    assert ".." not in line and ". ." not in line and "first. The bundle in place" in line


def _ops_with_start_error(world, error):
    def ops(_cfg):
        real = world.stack.ops(_cfg)

        def start_service():
            raise error

        return rq.QuiesceOps(real.stop_service, real.pg_running, real.stop_pg, real.start_pg, start_service)

    return ops


def test_a_slow_start_after_the_swap_is_a_note_and_a_failed_one_needs_a_human(world, monkeypatch):
    world.install_old(background=False, variant="old")
    world.ship(background=True, variant="new")
    slow = RuntimeError("the engine has not answered in 60 s")
    slow.still_starting = True
    monkeypatch.setattr(rq, "default_ops", _ops_with_start_error(world, slow))

    actions, _i, _c = _converge(world, engine="converged")

    assert world.has_background()
    assert any("refreshed" in a for a in actions) and any("still starting" in a and a.startswith("NOTE") for a in actions)
    assert all("NEEDS HUMAN" not in a for a in actions)



def test_a_start_that_failed_after_the_swap_needs_a_human(world, monkeypatch):
    world.install_old(background=False, variant="old")
    world.ship(background=True, variant="new")
    monkeypatch.setattr(rq, "default_ops", _ops_with_start_error(world, RuntimeError("supervisor exited")))

    actions, _i, _c = _converge(world, engine="converged")

    assert any(a.startswith("NEEDS HUMAN") and "supervisor exited" in a for a in actions), actions


# -- the doctor row is in the sweep; the probes' real bodies run ---------------------------------------------


def test_the_doctor_runs_the_bundle_row(world, monkeypatch):
    world.install_old(background=False)
    ran: list[str] = []

    def stepper(_progress):
        def step(name, fn):
            ran.append(name)
            return list(fn()) if name == "_check_pg_bundle" else []

        return step

    monkeypatch.setattr(health, "_row_stepper", stepper)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(world.cfg))

    with patch("nexus.config.is_local_mode", return_value=True):
        results, _local = health.run_health_checks(engine_status=None)

    assert "_check_pg_bundle" in ran
    assert any(r.label == "PostgreSQL bundle" and r.warn for r in results)


def test_the_engine_status_probe_returns_what_the_status_fetch_returns(monkeypatch):
    monkeypatch.setattr(http_engine_status, "fetch_engine_status", lambda **_k: {"boot_work": [{"id": "x"}]})
    assert pg_bundle_refresh._engine_status() == {"boot_work": [{"id": "x"}]}


def test_the_service_probe_reads_the_lease(monkeypatch, tmp_path):
    monkeypatch.setattr(service_endpoint, "discover_lease", lambda: ("http://127.0.0.1:1", "t"))
    assert pg_bundle_refresh._service_up(tmp_path) is True
    monkeypatch.setattr(service_endpoint, "discover_lease", lambda: (None, None))
    assert pg_bundle_refresh._service_up(tmp_path) is False


def test_the_progress_probe_runs_its_real_body_against_a_stub_psql(tmp_path, monkeypatch):
    (tmp_path / "pg_credentials").write_text(
        "PG_PORT=5433\nNX_DB_ADMIN_URL=jdbc:postgresql://127.0.0.1:5433/nexusdb\nNX_DB_ADMIN_USER=adm\nNX_DB_ADMIN_PASS=pw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pg_provision, "discover_pg_binaries", lambda *_a, **_k: SimpleNamespace(psql=Path("psql-stub")))
    seen: list[tuple] = []
    answer = {"rc": 0, "out": "2\n"}

    def stub_psql(psql_bin, host, port, dbname, user, password, sql, **kw):
        seen.append((str(psql_bin), host, port, dbname, user, password, sql))
        return subprocess.CompletedProcess([], answer["rc"], stdout=answer["out"], stderr="")

    monkeypatch.setattr(health, "_run_psql", stub_psql)

    assert pg_bundle_refresh._progress_rows(tmp_path) == 2
    (_psql, host, port, db, user, password, sql), = seen
    assert (host, port, db, user, password) == ("127.0.0.1", 5433, "nexusdb", "adm", "pw")
    assert "pg_stat_progress_create_index" in sql and "pg_stat_progress_vacuum" in sql
    answer.update(rc=1, out="")
    assert pg_bundle_refresh._progress_rows(tmp_path) is None  # unreadable is not idle
    answer.update(rc=0, out="not a number")
    assert pg_bundle_refresh._progress_rows(tmp_path) is None
