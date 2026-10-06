# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pure parts of ``scripts/windows_service_stop_driver.py`` (RDR-224, nexus-f9bgu.33).

The driver itself needs a real Windows box with the engine and PostgreSQL; what
runs here is what decides its verdict: which processes belong to the stack, how
a stop's output is classified, what pg.log says about a shutdown, and that a leg
that could not be exercised fails instead of passing.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "windows_service_stop_driver.py"
_spec = importlib.util.spec_from_file_location("windows_service_stop_driver", _PATH)
assert _spec and _spec.loader
drv = importlib.util.module_from_spec(_spec)
sys.modules["windows_service_stop_driver"] = drv
_spec.loader.exec_module(drv)

CFG = r"C:\build\x\cfg"


def _row(pid: int, name: str, cmd: str) -> dict[str, object]:
    return {"ProcessId": pid, "Name": name, "CommandLine": cmd}


ROWS = [
    _row(100, "python.exe", rf"C:\build\x\venv\Scripts\python.exe -m nexus.cli daemon service start --foreground --config-dir {CFG}"),
    _row(101, "python.exe", rf"C:\build\x\venv\Scripts\python.exe -m nexus.cli daemon service start --foreground --config-dir {CFG}"),
    _row(200, "nexus-service.exe", rf"{CFG}\service\nexus-service.exe"),
    _row(300, "postgres.exe", rf'"C:\pg\bin\postgres.exe" -D "{CFG}\pgdata"'),
    # another stack, an unrelated python, and a bare engine: none are ours
    _row(900, "python.exe", r"C:\other\python.exe -m nexus.cli daemon service start --foreground --config-dir C:\other\cfg"),
    _row(901, "python.exe", r"C:\Python313\python.exe some_script.py"),
    _row(902, "nexus-service.exe", r"C:\other\cfg\service\nexus-service.exe"),
]


def test_stack_pids_picks_only_this_config_dirs_processes_by_role() -> None:
    got = drv.stack_pids(ROWS, CFG)
    assert got.supervisors == (100, 101)  # the venv trampoline and the real python
    assert got.engines == (200,)
    assert got.postgres == (300,)
    assert got.service_up is True
    assert set(got.all_pids) == {100, 101, 200, 300}


def test_the_config_dir_match_is_case_insensitive_and_ignores_a_trailing_separator() -> None:
    assert drv.stack_pids(ROWS, CFG.upper() + "\\").supervisors == (100, 101)


def test_an_empty_listing_or_a_bad_row_yields_an_empty_stack() -> None:
    assert drv.stack_pids([], CFG) == drv.StackPids()
    assert drv.stack_pids([{"Name": "python.exe"}, {"ProcessId": "x"}], CFG).service_up is False


def test_postgres_alone_is_not_the_service() -> None:
    only_pg = drv.stack_pids([ROWS[3]], CFG)
    assert only_pg.postgres == (300,) and only_pg.service_up is False


def test_a_stop_that_succeeded_is_recognised() -> None:
    out = drv.StopOutput(0, "Storage service stopped (pid(s)=37724). Postgres left running (port 58036)")
    assert out.stopped and not out.refused and not out.pg_stopped


def test_a_with_pg_stop_names_postgres() -> None:
    out = drv.StopOutput(0, "Storage service stopped (pid(s)=1).\nPostgres stopped (port 58036)")
    assert out.stopped and out.pg_stopped


def test_a_refusal_needs_the_exit_code_the_mark_and_the_no_kill_sentence() -> None:
    text = (
        "nx daemon service stop: REFUSED. The storage service (pid 37724) runs in Windows "
        "session 1; this shell is in session 0.\nNothing was signalled or killed."
    )
    assert drv.StopOutput(1, text).refused
    assert not drv.StopOutput(0, text).refused  # exit 0 is not a refusal
    assert not drv.StopOutput(1, text.replace("Nothing was signalled or killed", "x")).refused
    assert not drv.StopOutput(1, "nx daemon service stop: failed").refused


def test_a_refusal_is_not_a_stop() -> None:
    assert not drv.StopOutput(1, "REFUSED ... Nothing was signalled or killed").stopped


CLEAN = """\
2026-10-05 06:59:28.620 LOG:  received fast shutdown request
2026-10-05 06:59:29.876 LOG:  database system is shut down
2026-10-05 06:59:40.100 LOG:  database system was shut down at 2026-10-05 06:59:29
"""


def test_a_clean_pg_log_reads_clean() -> None:
    got = drv.analyze_pg_log(CLEAN)
    assert got == {"fast_shutdown": True, "clean_shutdown": True, "crash_recovery": False, "stale_pid": False}


@pytest.mark.parametrize(
    "line",
    [
        "LOG:  database system was not properly shut down; automatic recovery in progress",
        "LOG:  database system was interrupted; last known up at 2026-10-05",
        "LOG:  automatic recovery in progress",
    ],
)
def test_a_crash_recovery_line_is_found(line: str) -> None:
    assert drv.analyze_pg_log(CLEAN + line + "\n")["crash_recovery"] is True


def test_a_stale_postmaster_pid_is_found() -> None:
    text = 'FATAL:  lock file "postmaster.pid" already exists\n'
    assert drv.analyze_pg_log(text)["stale_pid"] is True


def test_an_empty_log_claims_nothing() -> None:
    assert drv.analyze_pg_log("") == {
        "fast_shutdown": False, "clean_shutdown": False, "crash_recovery": False, "stale_pid": False,
    }


def test_tasklist_csv_finds_a_pid_and_only_that_pid() -> None:
    text = '"python.exe","1234","Console","1","12,000 K"\n"python.exe","12345","Console","1","1 K"\n'
    assert drv.parse_tasklist_csv(text, 1234) is True
    assert drv.parse_tasklist_csv(text, 123) is False
    assert drv.parse_tasklist_csv("INFO: No tasks are running which match the specified criteria.\n", 1234) is False


def _leg(name: str, *oks: bool, error: str = "") -> object:
    leg = drv.LegResult(name, error=error)
    for i, ok in enumerate(oks):
        leg.add(f"check {i}", ok)
    return leg


def test_every_leg_passing_is_a_pass_with_exit_zero() -> None:
    code, body = drv.verdict([_leg("primary", True, True), _leg("refused", True)], ["primary", "refused"])
    assert code == 0 and body["verdict"] == "PASSED"


def test_one_failed_check_fails_the_run() -> None:
    code, body = drv.verdict([_leg("primary", True, False)], ["primary"])
    assert code == 1 and body["verdict"] == "FAILED" and body["failed"] == ["primary"]


def test_a_requested_leg_that_never_ran_is_a_failure_not_a_skip() -> None:
    code, body = drv.verdict([_leg("primary", True)], ["primary", "withpg"])
    assert code == 1 and body["missing"] == ["withpg"]


def test_a_leg_with_no_checks_proved_nothing_and_fails() -> None:
    code, _ = drv.verdict([_leg("primary")], ["primary"])
    assert code == 1


def test_a_leg_that_raised_fails_whatever_its_checks_said() -> None:
    code, body = drv.verdict([_leg("primary", True, error="RuntimeError: service start failed")], ["primary"])
    assert code == 1
    assert body["legs"]["primary"]["error"].startswith("RuntimeError")


def test_no_requested_legs_is_a_failure() -> None:
    code, _ = drv.verdict([], [])
    assert code == 1


def test_the_verdict_body_is_json_serialisable() -> None:
    _, body = drv.verdict([_leg("primary", True)], ["primary"])
    assert json.loads(json.dumps(body))["verdict"] == "PASSED"


def test_the_scheduled_task_is_interactive_and_runs_as_the_named_user() -> None:
    argv = drv.schtasks_create_argv("t", "sam", r"C:\v\pythonw.exe", r"C:\d.py --run-child out.json nx stop")
    assert argv[:2] == ["schtasks", "/Create"]
    assert "/IT" in argv and argv[argv.index("/RU") + 1] == "sam"
    assert argv[argv.index("/TR") + 1] == r'"C:\v\pythonw.exe" C:\d.py --run-child out.json nx stop'
    # never a SYSTEM task: that would put the service in session 0
    assert "SYSTEM" not in " ".join(argv)


def test_the_legs_the_driver_runs_are_the_three_the_rdr_names() -> None:
    assert drv.LEGS == ("primary", "refused", "withpg")
    assert all(hasattr(drv.Driver, f"leg_{leg}") for leg in drv.LEGS)


def test_main_refuses_an_unknown_leg_and_a_non_windows_host() -> None:
    if sys.platform == "win32":
        assert drv.main(["bogus", "--python", "x", "--config-dir", "x", "--out-dir", "x", "--user", "u"]) == 2
    else:
        assert drv.main(["primary", "--python", "x", "--config-dir", "x", "--out-dir", "x", "--user", "u"]) == 2
