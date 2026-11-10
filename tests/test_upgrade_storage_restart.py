# SPDX-License-Identifier: AGPL-3.0-or-later
"""The storage-service restart in ``nx upgrade`` (first-run audit item 14, nexus-mjmjl).

It used to run ``nx daemon service stop`` then ``start`` with both streams discarded and never
read either exit code: a start that failed left the service stopped and the user told nothing.
Two places run it: the precondition stage (where a stale service is actually restarted) and the
command's final pass. Now:

* a dead start is a failure: what failed, what to run, and what ``start`` itself said;
* a start that reports the service up but not ready yet (``start`` exits 2 for both), or that
  outlives our own timeout, is "still starting" (a first boot can migrate for many minutes):
  said, and not a failure;
* a stop that fails before a start that works is a failure only if the old version still serves;
* an interactive upgrade prints a line before the restart and one after it, and exits 1 on a
  failure from either place; ``--auto`` and the lockstep hook's unattended child stay silent,
  log it, and exit 0.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import nexus.commands.upgrade as upgrade_mod
import nexus.config as nexus_config
import nexus.lockstep_marker as lockstep_marker
from nexus import upgrade_finish
from nexus.cli import main
from nexus.upgrade_ladder.preconditions import converge_preconditions

#: The real stage, captured before any fixture replaces it on the module.
_REAL_CONVERGE_PRECONDITIONS = upgrade_mod._converge_preconditions

_DEAD = (
    "Error: Storage service supervisor exited with code 1 before the service became ready.\n"
    "The supervisor reported: PG did not start\n"
    "Supervisor log: /cfg/logs/storage_service.log\n"
    "Supervisor stderr: /cfg/logs/storage_service.crash.log"
)
#: The autostart-unit path (daemon.py, _start_via_autostart_unit): with the supervisor's error
#: it is a failure (still_starting=False), without one it may still be migrating.
_UNIT_FAILED = (
    "Error: The autostart unit nexus-service was started but the storage service did not become"
    " ready within 90s; not retrying.\nThe supervisor reported: Schema migration failed\n"
    "Check /cfg/logs/nexus-service.log, /cfg/logs/storage_service.log (supervisor), or stop the unit"
    " and run 'nx daemon service start --foreground' to see the error."
)
_UNIT_SLOW = (
    "Error: The autostart unit nexus-service was started but the storage service did not become"
    " ready within 90s; not retrying.\nCheck /cfg/logs/nexus-service.log, /cfg/logs/storage_service.log"
    " (supervisor), or stop the unit and run 'nx daemon service start --foreground' to see the error."
)
_SLOW = (
    "Error: Storage service supervisor did not become ready within 60s. Check /cfg/logs/"
    "storage_service.log (supervisor), or run 'nx daemon service start --foreground' to see the error."
)


#: structlog's console line: a timestamp, then the level in brackets.
_LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\S* \[")


class _Lease:
    def __init__(self, version: str) -> None:
        self.version = version


def _said(capsys) -> list[str]:
    """What the user reads: stdout without structlog's lines (under pytest an unconfigured
    structlog writes to stdout; the CLI routes it to stderr and the log)."""
    return [ln for ln in capsys.readouterr().out.splitlines() if not _LOG_LINE.match(ln)]


def _runner(codes: dict):
    """A run_bounded stand-in. Each verb maps to an exit code, ``(code, stderr)``, or an
    exception class to raise."""
    calls: list[str] = []

    def run(argv, **_kwargs):
        verb = argv[-1]
        calls.append(verb)
        outcome = codes.get(verb, 0)
        if isinstance(outcome, type):
            raise outcome(argv, 60) if outcome is subprocess.TimeoutExpired else outcome("boom")
        code, err = outcome if isinstance(outcome, tuple) else (outcome, "")
        return subprocess.CompletedProcess(argv, code, "", err)

    return run, calls


def _cycle(codes, *, auto_mode: bool = False, after: str = "2.0.0"):
    """One restart of a stale 1.0.0 service with 2.0.0 installed; the lease after it names *after*."""
    run, calls = _runner(codes)
    leases = iter([_Lease("1.0.0"), _Lease(after), _Lease(after)])
    out = upgrade_mod._cycle_storage_service_to_current(
        auto_mode=auto_mode,
        _discover_fn=lambda: next(leases),
        _run_fn=run,
        _nx_bin_fn=lambda: ["nx"],
        _installed_version_fn=lambda: "2.0.0",
    )
    return out, calls


# ── the restart ────────────────────────────────────────────────────────────────────


def test_a_clean_restart_says_so_before_and_after(capsys) -> None:
    out, calls = _cycle({})
    assert out is None and calls == ["stop", "start"]
    printed = _said(capsys)
    assert printed[0] == "Restarting the storage service so it runs conexus 2.0.0 (background index builds continue)"
    assert printed[-1] == "Storage service restarted."


def test_a_dead_start_says_what_start_said_and_what_to_run(capsys) -> None:
    out, _calls = _cycle({"start": (2, _DEAD)})
    assert out is not None
    first = out.splitlines()[0]
    assert first == (
        "The storage service did not restart after the upgrade: starting it exited 2. It may be"
        " stopped; run `nx daemon service start`."
    )
    assert "`nx daemon service start` said:" in out and "PG did not start" in out
    assert "storage_service.crash.log" in out, "start's own text names the logs"
    assert "Storage service restarted." not in _said(capsys)


@pytest.mark.parametrize(
    "codes",
    [{"start": (2, _SLOW)}, {"start": (2, _UNIT_SLOW)}, {"start": subprocess.TimeoutExpired}],
    ids=["detached-not-ready", "unit-not-ready", "our-timeout"],
)
def test_a_slow_start_is_still_starting_not_a_failure(capsys, codes) -> None:
    """A first boot after an upgrade can migrate the database for many minutes: telling that user
    'it may be stopped' and failing the upgrade would be false (critique r1, C2)."""
    out, _calls = _cycle(codes)
    assert out is None
    printed = _said(capsys)
    assert any(line.startswith("The storage service is still starting on the new version.") for line in printed)
    assert "`nx daemon service status`" in printed[-1]
    assert "Storage service restarted." not in printed


def test_a_unit_whose_supervisor_reported_an_error_is_a_failure_not_still_starting(capsys) -> None:
    """The unit path says "did not become ready within" for a crash loop or a halted migration
    too; the supervisor's error line makes it a failure (review r2, NB1). Autostart units are
    the default after `nx init --yes`."""
    out, _calls = _cycle({"start": (2, _UNIT_FAILED)})
    assert out is not None and "Schema migration failed" in out
    assert not any("still starting" in line for line in _said(capsys))


def test_the_phrases_the_verdict_reads_are_still_what_start_prints() -> None:
    """The verdict reads start's text; if daemon.py rewords it, this fails before users do."""
    import inspect  # noqa: PLC0415 -- this test only

    from nexus.commands import daemon  # noqa: PLC0415 -- this test only

    src = inspect.getsource(daemon)
    for phrase in (*upgrade_mod._STILL_STARTING[:1], *upgrade_mod._FAILED_START):
        assert phrase in src, phrase
    assert "still_starting=not last_error" in src and "still_starting=True" in src


def test_the_start_timeout_outlasts_starts_own_wait() -> None:
    """``nx daemon service start`` waits up to 60 s for the lease after spawning the supervisor
    and PG; ours must be longer, or every slow start reaches us as our own timeout."""
    seen: dict = {}

    def run(argv, **kwargs):
        seen[argv[-1]] = kwargs
        return subprocess.CompletedProcess(argv, 0, "", "")

    upgrade_mod._cycle_storage_service_to_current(
        auto_mode=True, _discover_fn=lambda: _Lease("1.0.0"), _run_fn=run,
        _nx_bin_fn=lambda: ["nx"], _installed_version_fn=lambda: "2.0.0",
    )
    assert seen["start"]["timeout"] >= 120
    assert seen["start"]["stderr"] is subprocess.PIPE and seen["start"]["contained"] is False


def test_a_start_that_cannot_be_spawned_names_both_service_logs(capsys, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    out, _calls = _cycle({"start": OSError})
    logs = nexus_config.nexus_config_dir() / "logs"
    assert out is not None and "starting it failed (OSError: boom)" in out
    assert f"{logs / 'storage_service.log'} and {logs / 'storage_service.crash.log'}" in out


@pytest.mark.parametrize("stop", [3, subprocess.TimeoutExpired, OSError], ids=["nonzero", "timeout", "spawn-error"])
def test_a_failed_stop_before_a_start_that_works_is_fine_when_the_new_version_serves(capsys, stop) -> None:
    out, calls = _cycle({"stop": stop})
    assert out is None and calls == ["stop", "start"], "the start runs whatever the stop did"
    assert _said(capsys)[-1] == "Storage service restarted."


def test_a_failed_stop_that_leaves_the_old_version_serving_is_a_failure(capsys) -> None:
    """``start`` exits 0 on an already-live lease, so a stop that did not stop leaves the old code
    running under a clean start (review r1, S1)."""
    out, _calls = _cycle({"stop": 3}, after="1.0.0")
    assert out is not None
    assert "stopping the old service exited 3, then the service still runs 1.0.0" in out
    assert "run `nx daemon service stop`, then `nx daemon service start`" in out
    assert "Storage service restarted." not in _said(capsys)


def test_a_failed_stop_and_a_dead_start_report_both(capsys) -> None:
    out, _calls = _cycle({"stop": subprocess.TimeoutExpired, "start": (1, "")})
    assert "stopping the old service did not finish within 60 s, then starting it exited 1" in out


@pytest.mark.parametrize(
    ("codes", "fails"),
    [({}, False), ({"start": (2, _SLOW)}, False), ({"start": (2, _DEAD)}, True)],
    ids=["clean", "still-starting", "dead"],
)
def test_auto_mode_prints_nothing_and_still_returns_the_failure(capsys, codes, fails) -> None:
    out, _calls = _cycle(codes, auto_mode=True)
    assert _said(capsys) == [], "--auto runs from a hook: silent"
    assert (out is not None) is fails


def test_nothing_running_or_already_current_restarts_nothing_and_says_nothing(capsys) -> None:
    run, calls = _runner({})
    assert upgrade_mod._cycle_storage_service_to_current(
        auto_mode=False, _discover_fn=lambda: None, _run_fn=run, _nx_bin_fn=lambda: ["nx"],
    ) is None
    assert upgrade_mod._cycle_storage_service_to_current(
        auto_mode=False, _discover_fn=lambda: _Lease("2.0.0"), _run_fn=run, _nx_bin_fn=lambda: ["nx"],
        _installed_version_fn=lambda: "2.0.0",
    ) is None
    assert calls == [] and _said(capsys) == []


def test_the_supervised_cycle_passes_the_mode_and_the_failure_through(monkeypatch) -> None:
    seen: dict = {}

    def cycle(**kw):
        seen.update(kw)
        return "SENTENCE"

    monkeypatch.setattr(upgrade_mod, "_cycle_storage_service_to_current", cycle)
    assert upgrade_mod._cycle_supervised_daemons_to_current(skip_t3=False, auto_mode=False) == "SENTENCE"
    assert seen == {"auto_mode": False}
    assert upgrade_mod._cycle_supervised_daemons_to_current(skip_t3=True, auto_mode=False) is None


# ── the precondition stage, where a stale service is actually restarted (review r1, B1) ──────


def _converge_kwargs(**over):
    base = dict(
        config_dir=Path("/nonexistent"),
        _engine_detect_fn=lambda: SimpleNamespace(applicable=False, converged=True),
        _engine_converge_fn=lambda: [],
        _installed_version_fn=lambda: "2.0.0",
        _provisioned_fn=lambda: True,
        _plugin_version_fn=lambda: None,
        _lockstep_marker_fn=lambda: None,
        _plan_library_fn=lambda: (0, 0),
    )
    base.update(over)
    return base


def _process(cycle_result, lease_after):
    leases = iter([_Lease("1.0.0"), lease_after])
    reports = converge_preconditions(
        **_converge_kwargs(_lease_fn=lambda: next(leases), _cycle_fn=lambda: cycle_result)
    )
    return next(r for r in reports if r.name == "process")


def test_a_failed_precondition_restart_is_reported_and_never_claims_a_cycle() -> None:
    """A failed restart can leave no lease, which reads as 'nothing to cycle': the restart's own
    verdict decides."""
    process = _process("The storage service did not restart after the upgrade: X.", None)
    assert process.failure == "The storage service did not restart after the upgrade: X."
    assert process.current is False and process.actions == ()


def test_a_clean_precondition_restart_reports_the_cycle() -> None:
    process = _process(None, _Lease("2.0.0"))
    assert process.failure is None and process.current is True
    assert process.actions == ("cycled supervised service to the installed version",)


@pytest.mark.parametrize("auto_mode", [True, False])
def test_the_precondition_restart_runs_in_the_commands_mode(monkeypatch, auto_mode) -> None:
    seen: dict = {}
    monkeypatch.setattr(upgrade_mod, "_cycle_storage_service_to_current", lambda **kw: seen.update(kw))
    leases = iter([_Lease("1.0.0"), _Lease("2.0.0")])
    converge_preconditions(**_converge_kwargs(auto_mode=auto_mode, _lease_fn=lambda: next(leases)))
    assert seen == {"auto_mode": auto_mode}


# ── the command ────────────────────────────────────────────────────────────────────


@pytest.fixture
def quiet_upgrade(monkeypatch, tmp_path: Path) -> dict:
    """``nx upgrade`` with every step stubbed; each test sets what the two restarts report."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("NX_UPGRADE_UNATTENDED", raising=False)
    monkeypatch.setattr(upgrade_finish, "check_version_transition", lambda *_a, **_k: None)
    for name in (
        "_run_upgrade", "_run_ladder", "_emit_name_vs_embed_dim_advisory",
        "_migrate_repos_json_to_catalog", "_refresh_all_git_hooks", "_converge_plugins",
        "_refresh_generated_agents", "_install_beads_prime_best_effort",
    ):
        monkeypatch.setattr(upgrade_mod, name, lambda *_a, **_k: None)
    monkeypatch.setattr(lockstep_marker, "settle_upgrade_retry", lambda *_a, **_k: None)
    monkeypatch.setattr(nexus_config, "backfill_install_mode_record", lambda *_a, **_k: None, raising=False)
    state: dict = {"precondition": None, "final": None, "calls": []}

    def precondition(**kw):
        state["calls"].append(("precondition", kw))
        return state["precondition"]

    def final(**kw):
        state["calls"].append(("final", kw))
        return state["final"]

    monkeypatch.setattr(upgrade_mod, "_converge_preconditions", precondition)
    monkeypatch.setattr(upgrade_mod, "_cycle_supervised_daemons_to_current", final)
    return state


def _upgrade(monkeypatch, *args: str):
    monkeypatch.setattr(sys, "argv", ["nx", "upgrade", *args])
    return CliRunner().invoke(main, ["upgrade", *args])


@pytest.mark.parametrize("where", ["precondition", "final"])
def test_an_interactive_upgrade_fails_on_a_failed_restart(monkeypatch, quiet_upgrade, where) -> None:
    quiet_upgrade[where] = "The storage service did not restart after the upgrade: X."
    result = _upgrade(monkeypatch)
    assert result.exit_code == 1, result.output
    assert "Error: The storage service did not restart after the upgrade: X." in result.output
    assert ("final", {"skip_t3": False, "auto_mode": False}) in quiet_upgrade["calls"]


def test_an_interactive_upgrade_with_clean_restarts_exits_zero(monkeypatch, quiet_upgrade) -> None:
    result = _upgrade(monkeypatch)
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("mode", ["--auto", "unattended"])
def test_an_unattended_upgrade_never_fails_on_a_failed_restart(monkeypatch, quiet_upgrade, mode) -> None:
    """--auto is the SessionStart hook; NX_UPGRADE_UNATTENDED is the lockstep hook's detached
    plain `nx upgrade`, whose non-zero exit the hook counts as a failed upgrade (review r1, S2)."""
    quiet_upgrade["precondition"] = quiet_upgrade["final"] = "The storage service did not restart: X."
    if mode == "unattended":
        monkeypatch.setenv("NX_UPGRADE_UNATTENDED", "1")
        result = _upgrade(monkeypatch)
    else:
        result = _upgrade(monkeypatch, "--auto")
    assert result.exit_code == 0, result.output
    assert "did not restart" not in result.output


def test_a_body_error_wins_over_a_failed_restart(monkeypatch, quiet_upgrade) -> None:
    quiet_upgrade["final"] = "The storage service did not restart after the upgrade: X."

    def boom(**_kw):
        raise RuntimeError("the ladder broke")

    monkeypatch.setattr(upgrade_mod, "_run_ladder", boom)
    result = _upgrade(monkeypatch)
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError) and "the ladder broke" in str(result.exception)
    assert "did not restart" not in result.output


def test_a_dry_run_restarts_nothing(monkeypatch, quiet_upgrade) -> None:
    result = _upgrade(monkeypatch, "--dry-run")
    assert result.exit_code == 0, result.output
    assert quiet_upgrade["calls"] == []


def test_the_precondition_stage_hands_the_restart_failure_to_the_command(monkeypatch, capsys) -> None:
    """``_converge_preconditions`` returns the process report's failure (the CLI tests above stub
    the whole stage, so this is the seam between them and the report)."""
    from nexus.commands import self_cmd  # noqa: PLC0415 -- its repair is stubbed for this one test
    from nexus.upgrade_ladder import preconditions  # noqa: PLC0415 -- the converge is stubbed for this one test
    from nexus.upgrade_ladder.preconditions import PreconditionReport  # noqa: PLC0415

    monkeypatch.setattr(self_cmd, "repair_uv_takeover", lambda: [])
    for name in ("_converge_vc_runtime", "_converge_pdf_stack", "_converge_installer_rebuild"):
        monkeypatch.setattr(upgrade_mod, name, lambda **_k: None)
    failed = PreconditionReport(
        name="process", applicable=True, current=False,
        detail="the restart failed (reported at the end)", failure="SENTENCE",
    )
    monkeypatch.setattr(preconditions, "converge_preconditions", lambda **_k: [failed])
    assert upgrade_mod._converge_preconditions(auto_mode=False) == "SENTENCE"
    assert any("pending: the restart failed (reported at the end)" in line for line in _said(capsys))
    monkeypatch.setattr(preconditions, "converge_preconditions",
                        lambda **_k: [PreconditionReport(name="process", applicable=True, current=True)])
    assert upgrade_mod._converge_preconditions(auto_mode=False) is None


# ── P0 (nexus-f3yqd): the SessionStart hook's `nx upgrade --auto` never restarts the service ──
#
# The hook spawns `nx upgrade --auto` on every Claude session start. Under version skew (two
# clients installed, a lease written by the other one) the process axis saw a version mismatch
# every time and cycled a healthy storage service, which threw away the engine's index build on
# Sam's machine. Under --auto both restarts are report-only now, the posture
# converge_engine(unattended=True) already takes; an MCP server's start never installs or
# restarts the engine either.


def _stub_precondition_neighbours(monkeypatch) -> dict:
    from nexus.commands import self_cmd  # noqa: PLC0415 -- its repair is stubbed for these tests
    from nexus.upgrade_ladder import preconditions  # noqa: PLC0415 -- the converge is stubbed for these tests

    monkeypatch.setattr(self_cmd, "repair_uv_takeover", lambda: [])
    for name in ("_converge_vc_runtime", "_converge_pdf_stack", "_converge_installer_rebuild"):
        monkeypatch.setattr(upgrade_mod, name, lambda **_k: None)
    seen: dict = {}
    monkeypatch.setattr(preconditions, "converge_preconditions", lambda **kw: seen.update(kw) or [])
    return seen


@pytest.mark.parametrize(("auto_mode", "cycles"), [(True, False), (False, True)])
def test_the_auto_precondition_stage_never_restarts_the_service(monkeypatch, auto_mode, cycles) -> None:
    seen = _stub_precondition_neighbours(monkeypatch)
    upgrade_mod._converge_preconditions(auto_mode=auto_mode)
    assert seen["allow_process_cycle"] is cycles
    assert seen["allow_engine_install"] is cycles


def test_the_auto_final_pass_never_restarts_the_service(monkeypatch, quiet_upgrade) -> None:
    result = _upgrade(monkeypatch, "--auto")
    assert result.exit_code == 0, result.output
    assert [c for c in quiet_upgrade["calls"] if c[0] == "final"] == [], "--auto restarts nothing"
    result = _upgrade(monkeypatch)
    assert ("final", {"skip_t3": False, "auto_mode": False}) in quiet_upgrade["calls"], "an interactive run still does"


def _engine_status(converged: bool):
    from nexus.daemon.binary_install import REQUIRED_ENGINE_VERSION  # noqa: PLC0415

    return upgrade_finish.EngineConvergence(
        applicable=True, installed_version=(0, 0, 1),
        required_version=REQUIRED_ENGINE_VERSION, converged=converged,
    )


def test_a_report_only_engine_convergence_installs_and_restarts_nothing(monkeypatch, tmp_path: Path) -> None:
    acted: list[str] = []
    monkeypatch.setattr(upgrade_finish, "detect_engine_convergence", lambda _cd: _engine_status(False))
    monkeypatch.setattr(upgrade_finish, "_poison_probe", lambda *_a, **_k: upgrade_finish.PoisonProbe())
    monkeypatch.setattr(upgrade_finish, "_restart_and_verify", lambda *_a, **_k: acted.append("restart") or [])
    monkeypatch.setattr("nexus.daemon.binary_install.install_binary", lambda *_a, **_k: acted.append("install"))
    monkeypatch.setattr("nexus.daemon.binary_install.PINNED_SERVICE_TAG", "engine-service-v0.1.9")
    lines = upgrade_finish.converge_engine(tmp_path, unattended=True, acquire=False)
    assert acted == []
    assert len(lines) == 1
    assert lines[0].startswith("NOTE: the storage engine is out of date (installed 0.0.1, this release needs ")
    for words in ("Nothing was changed.", "Run `nx upgrade`", "restarts the storage service",
                  "may migrate your data", "`nx doctor`"):
        assert words in lines[0], words


@pytest.mark.parametrize(("kwargs", "acquire"), [({}, True), ({"engine_report_only": True}, False)])
def test_an_mcp_servers_start_never_acquires_or_restarts_the_engine(monkeypatch, tmp_path: Path, kwargs, acquire) -> None:
    from nexus.upgrade_finish import SkewReport  # noqa: PLC0415

    (tmp_path / "last_seen_version").write_text("6.7.0\n")
    monkeypatch.setattr(upgrade_finish, "running_from_tool_install", lambda: True)
    monkeypatch.setattr(upgrade_finish, "install_mtime_and_version", lambda: (0.0, "6.7.1"))
    monkeypatch.setattr(upgrade_finish, "detect_stale_processes", lambda: SkewReport(installed_version="6.7.1"))
    monkeypatch.setattr(upgrade_finish, "pending_data_rung_callout", lambda *_a, **_k: [])
    seen: dict = {}
    monkeypatch.setattr(upgrade_finish, "converge_engine", lambda _cd, **kw: seen.update(kw) or [])
    upgrade_finish.check_version_transition(tmp_path, **kwargs)
    assert seen["unattended"] is True and seen["acquire"] is acquire


@pytest.mark.parametrize("module", ["nexus.mcp.core", "nexus.mcp.catalog"])
def test_both_mcp_servers_start_the_finish_pass_report_only(module) -> None:
    import importlib  # noqa: PLC0415
    import inspect  # noqa: PLC0415

    src = inspect.getsource(importlib.import_module(module).main)
    assert "check_version_transition(nexus_config_dir(), engine_report_only=True)" in src
    assert "never kills anything" not in src
    # The stamp is consumed here, so the comment must not promise a later `nx` finishes it.
    assert "the first `nx` command" not in src and "`nx doctor` names it" in src



def test_report_only_never_restarts_a_stale_running_engine_either(monkeypatch, tmp_path: Path) -> None:
    """acquire=False is report-only on every branch, not only when the caller also says
    unattended (critique r1, S3)."""
    acted: list[str] = []
    monkeypatch.setattr(upgrade_finish, "detect_engine_convergence", lambda _cd: _engine_status(True))
    monkeypatch.setattr("nexus.daemon.binary_install.verify_installed_binary",
                        lambda _cd: SimpleNamespace(ok=True, reason=None))
    monkeypatch.setattr(upgrade_finish, "_running_engine", lambda _cd: SimpleNamespace(version=(0, 0, 1)))
    monkeypatch.setattr(upgrade_finish, "_restart_and_verify", lambda *_a, **_k: acted.append("restart") or [])
    monkeypatch.setattr(upgrade_finish, "_reassign_diag_view_before_restart", lambda _cd: [])
    lines = upgrade_finish.converge_engine(tmp_path, unattended=False, acquire=False)
    assert acted == [] and lines and lines[0].startswith("NOTE:")


@pytest.mark.parametrize(("argv", "report_only"), [
    (["upgrade", "--auto"], True), (["upgrade"], False), (["doctor"], False),
])
def test_the_root_groups_trigger_reports_the_engine_under_auto(monkeypatch, quiet_upgrade, argv, report_only) -> None:
    """`nx upgrade --auto` reaches the root group's version-stamp trigger first; there it used to
    install the engine and restart the service once per version (review r1, B1)."""
    seen: dict = {}
    monkeypatch.setattr(upgrade_finish, "check_version_transition", lambda _cd, **kw: seen.update(kw))
    monkeypatch.setattr(sys, "argv", ["nx", *argv])
    CliRunner().invoke(main, [*argv, "--help"] if argv == ["doctor"] else argv)
    assert seen.get("engine_report_only") is report_only


@pytest.mark.parametrize(("args", "env", "cycles"), [
    (["upgrade"], {}, True), (["upgrade", "--auto"], {}, False),
    (["upgrade"], {"NX_UPGRADE_UNATTENDED": "1"}, False), (["upgrade", "--skip-t3"], {}, False),
])
def test_the_finish_pass_knows_which_upgrades_restart_the_service(monkeypatch, args, env, cycles) -> None:
    """The finish pass drops its NEEDS HUMAN line when the running upgrade restarts the service
    next; --auto and the unattended run no longer do (review r1, S1)."""
    monkeypatch.delenv("NX_UPGRADE_UNATTENDED", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert upgrade_finish.upgrade_invocation(args).cycles_service is cycles


def test_the_unattended_child_never_restarts_on_a_version_string_but_keeps_its_engine_install(monkeypatch, quiet_upgrade) -> None:
    """The lockstep hook's plain `nx upgrade` with NX_UPGRADE_UNATTENDED (review r1, S2): no process
    restart and no final restart; its engine install stays, and an install does restart the service."""
    seen = _stub_precondition_neighbours(monkeypatch)
    monkeypatch.setenv("NX_UPGRADE_UNATTENDED", "1")
    _REAL_CONVERGE_PRECONDITIONS(auto_mode=False)
    assert seen["allow_process_cycle"] is False and seen["allow_engine_install"] is True
    result = _upgrade(monkeypatch)
    assert result.exit_code == 0, result.output
    assert [c for c in quiet_upgrade["calls"] if c[0] == "final"] == []


def test_a_newer_lease_is_never_cycled_or_downgraded(capsys) -> None:
    """With two clients installed, the lease is written by whichever started the service; to the
    older one a newer lease read as a mismatch, and cycling it downgraded the service (review r1, S3)."""
    run, calls = _runner({})
    out = upgrade_mod._cycle_storage_service_to_current(
        auto_mode=False, _discover_fn=lambda: _Lease("8.1.0"), _run_fn=run,
        _nx_bin_fn=lambda: ["nx"], _installed_version_fn=lambda: "8.0.0",
    )
    assert out is None and calls == []
    reports = converge_preconditions(**_converge_kwargs(
        _lease_fn=lambda: _Lease("8.1.0"), _installed_version_fn=lambda: "8.0.0",
        _cycle_fn=lambda: calls.append("cycled"),
    ))
    report = next(r for r in reports if r.name == "process")
    assert calls == [] and report.current is True and "not downgrading it" in report.detail


@pytest.mark.parametrize(("lease", "installed", "newer"), [
    ("8.1.0", "8.0.0", True), ("8.0.0", "8.1.0", False), ("8.0.1.dev3", "8.0.0", True),
    ("8.0.0", "8.0.0", False), ("", "8.0.0", False), ("weird", "8.0.0", False),
])
def test_lease_is_newer_compares_versions_not_strings(lease, installed, newer) -> None:
    from nexus.upgrade_ladder.preconditions import lease_is_newer  # noqa: PLC0415

    assert lease_is_newer(lease, installed) is newer



def test_the_report_names_a_missing_engine_as_missing_not_out_of_date(monkeypatch, tmp_path: Path) -> None:
    """A receipt the binary does not back: 'installed 0.1.184, needs 0.1.184' contradicts itself
    (critique r2)."""
    monkeypatch.setattr(upgrade_finish, "detect_engine_convergence", lambda _cd: _engine_status(True))
    monkeypatch.setattr("nexus.daemon.binary_install.verify_installed_binary",
                        lambda _cd: SimpleNamespace(ok=False, reason="the engine binary is missing"))
    monkeypatch.setattr(upgrade_finish, "_poison_probe", lambda *_a, **_k: upgrade_finish.PoisonProbe())
    lines = upgrade_finish.converge_engine(tmp_path, unattended=True, acquire=False)
    assert lines[0].startswith(
        "NOTE: the installed storage engine is missing or damaged (the engine binary is missing)."
    ), lines
