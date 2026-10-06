# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Windows runner's end-of-job process cleanup (RDR-224, nexus-f9bgu.27, critique O7).

``scripts/windows_job_cleanup.py`` stops an engine or Postgres a cancelled job left behind, and
must never touch anyone else's: ``win-release`` also hosts other Postgres instances, so the
decision is by executable PATH under the job's directories, never by bare image name. The
listing and the kill are injected, so the whole decision runs on every OS.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml

import windows_job_cleanup as cleanup
from windows_job_cleanup import Proc

REPO = Path(__file__).resolve().parent.parent
TEMP = r"C:\actions-runner\_work\_temp"
WORK = r"C:\actions-runner\_work\nexus\nexus"


def _procs() -> list[Proc]:
    return [
        Proc(10, "postgres.exe", rf"{TEMP}\pgsmoke-ab12cd34\relocated\bundle\bin\postgres.exe"),
        Proc(11, "postgres.exe", rf"{TEMP}\pgsmoke-ab12cd34\relocated\bundle\bin\postgres.exe"),  # a backend
        Proc(12, "nexus-service.exe", rf"{TEMP}\pgsmoke-ab12cd34\engine\nexus-service.exe"),
        Proc(13, "nexus-service.exe", rf"{WORK}\service\target\nexus-service.exe"),
        Proc(20, "postgres.exe", r"C:\Program Files\PostgreSQL\17\bin\postgres.exe"),  # the host's own
        Proc(21, "postgres.exe", r"D:\pgdata\bin\postgres.exe"),
        Proc(22, "nexus-service.exe", r"C:\Users\Sam\.local\bin\nexus-service.exe"),  # a peer's engine
        Proc(30, "powershell.exe", rf"{TEMP}\x\powershell.exe"),  # under the root, wrong image
        Proc(31, "postgres.exe", rf"{TEMP}-other\bundle\bin\postgres.exe"),  # a sibling directory sharing a prefix
    ]


def _pids(victims: list[Proc]) -> list[int]:
    return sorted(p.pid for p in victims)


def test_only_the_jobs_own_images_under_the_jobs_own_directories_are_selected() -> None:
    assert _pids(cleanup.select_victims(_procs(), [TEMP, WORK])) == [10, 11, 12, 13]


def test_a_bare_name_match_is_exactly_what_this_refuses() -> None:
    """The control: selecting by name alone would also take 20, 21, 22 and 31, the host's own processes."""
    by_name = [p for p in _procs() if p.name in cleanup.DEFAULT_IMAGES]
    assert {20, 21, 22, 31} <= {p.pid for p in by_name}
    assert not ({20, 21, 22, 31} & {p.pid for p in cleanup.select_victims(_procs(), [TEMP, WORK])})


def test_no_roots_selects_nothing_instead_of_falling_back_to_names() -> None:
    assert cleanup.select_victims(_procs(), []) == []
    assert cleanup.select_victims(_procs(), [""]) == []


def test_paths_compare_case_insensitively_with_either_slash_and_on_a_boundary() -> None:
    assert cleanup.is_under(r"c:\ACTIONS-RUNNER\_work\_temp\a\b.exe", "C:/actions-runner/_work/_temp")
    assert cleanup.is_under(TEMP, TEMP + "\\")
    assert not cleanup.is_under(TEMP + "-other\\x.exe", TEMP)
    assert not cleanup.is_under(r"C:\w\x.exe", r"C:\work")


def test_the_image_list_is_replaceable_and_case_insensitive() -> None:
    victims = cleanup.select_victims(_procs(), [TEMP, WORK], ["NEXUS-SERVICE.EXE"])
    assert _pids(victims) == [12, 13]


def test_short_form_paths_are_matched_through_the_long_path_resolver() -> None:
    procs = [Proc(1, "postgres.exe", r"C:\Users\RUNNER~1\AppData\Local\Temp\pgsmoke\bin\postgres.exe")]
    assert cleanup.select_victims(procs, [r"C:\Users\runneradmin\AppData\Local\Temp"]) == []
    longer = cleanup.select_victims(
        procs, [r"C:\Users\runneradmin\AppData\Local\Temp"], long_path=lambda p: p.replace("RUNNER~1", "runneradmin")
    )
    assert _pids(longer) == [1]


def test_an_8_3_short_form_root_is_matched_through_the_long_path_resolver() -> None:
    """The documented real case: RUNNER_TEMP arrives as C:\\Users\\RUNNER~1\\..., the process path is the long form.
    Without resolving the ROOTS too, nothing matches and the cleanup reports success over a stray postgres."""
    procs = [Proc(1, "postgres.exe", r"C:\Users\runneradmin\AppData\Local\Temp\pgsmoke\bin\postgres.exe")]
    short_root = r"C:\Users\RUNNER~1\AppData\Local\Temp"

    def long_form(p: str) -> str:
        return p.replace("RUNNER~1", "runneradmin")

    assert cleanup.select_victims(procs, [short_root]) == [], "control: unresolved, the short root matches nothing"
    assert _pids(cleanup.select_victims(procs, [short_root], long_path=long_form)) == [1]
    killed: list[int] = []
    rc = cleanup.run([short_root], cleanup.DEFAULT_IMAGES, lister=lambda: procs, killer=lambda pid: killed.append(pid) or True,
                     long_path=long_form, emit=lambda _l: None)
    assert rc == 0 and killed == [1]


def test_the_default_images_are_exactly_the_engine_and_postgres_programs() -> None:
    assert cleanup.DEFAULT_IMAGES == ("nexus-service.exe", "postgres.exe", "pg_ctl.exe", "initdb.exe")
    procs = [Proc(1, "cmd.exe", rf"{TEMP}\x\cmd.exe"), Proc(2, "python.exe", rf"{TEMP}\x\python.exe"),
             Proc(3, "pg_ctl.exe", rf"{TEMP}\x\pg_ctl.exe"), Proc(4, "initdb.exe", rf"{TEMP}\x\initdb.exe")]
    assert _pids(cleanup.select_victims(procs, [TEMP])) == [3, 4]


def _taskkill(monkeypatch: pytest.MonkeyPatch, rc: int, out: str = "", err: str = "") -> list[list[str]]:
    seen: list[list[str]] = []

    def fake_run(cmd, **kw):  # noqa: ANN001
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)

    monkeypatch.setattr(cleanup.subprocess, "run", fake_run)
    return seen


def test_kill_reads_taskkills_result(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _taskkill(monkeypatch, 0, 'SUCCESS: The process with PID 77 has been terminated.')
    assert cleanup.kill(77) is True
    assert seen == [["taskkill", "/F", "/PID", "77"]], "forced, by pid, never by image name"
    _taskkill(monkeypatch, 128, err='ERROR: The process "77" not found.')
    assert cleanup.kill(77) is True, "already gone counts as stopped"
    _taskkill(monkeypatch, 1, err="ERROR: The process with PID 77 could not be terminated.\nReason: Access is denied.")
    assert cleanup.kill(77) is False, "an access-denied refusal must fail the job"
    _taskkill(monkeypatch, 1, out="", err="")
    assert cleanup.kill(77) is False, "an unexplained failure is not 'gone'"


def test_an_empty_listing_is_a_failure_not_a_clean() -> None:
    """The listing always contains at least this process; an empty one means it broke, and the cleanup
    must not report 'no stray process' over a list it never read."""
    out: list[str] = []
    rc = cleanup.run([TEMP], cleanup.DEFAULT_IMAGES, lister=lambda: [], killer=lambda pid: True, emit=out.append)
    assert rc == 1
    assert "examined 0 process" in out[0] and any("listing is empty" in ln for ln in out)


def test_this_process_is_never_selected() -> None:
    procs = [Proc(99, "postgres.exe", rf"{TEMP}\x\postgres.exe")]
    assert cleanup.select_victims(procs, [TEMP], self_pid=99) == []


def test_parse_process_list_takes_one_object_a_list_or_nothing() -> None:
    one = json.dumps({"ProcessId": 5, "Name": "postgres.exe", "ExecutablePath": r"C:\a\postgres.exe"})
    assert cleanup.parse_process_list(one) == [Proc(5, "postgres.exe", r"C:\a\postgres.exe")]
    many = json.dumps([{"ProcessId": 1, "Name": "a.exe", "ExecutablePath": "C:\\a.exe"}, {"ProcessId": 2, "Name": "b.exe", "ExecutablePath": None}])
    assert [p.pid for p in cleanup.parse_process_list(many)] == [1]
    assert cleanup.parse_process_list("") == [] and cleanup.parse_process_list("  \n") == []


def test_run_stops_the_victims_reports_each_and_exits_zero() -> None:
    killed: list[int] = []
    out: list[str] = []
    rc = cleanup.run([TEMP, WORK], cleanup.DEFAULT_IMAGES, lister=_procs, killer=lambda pid: killed.append(pid) or True, emit=out.append)
    assert rc == 0 and sorted(killed) == [10, 11, 12, 13]
    stopped = [ln for ln in out if ln.startswith("cleanup: stopped")]
    assert len(stopped) == 4
    assert not any("PostgreSQL" in ln or "Sam" in ln for ln in out)
    assert out[0] == f"cleanup: examined {len(_procs())} process(es) with an executable path"


def test_run_fails_loudly_when_a_victim_cannot_be_stopped() -> None:
    out: list[str] = []
    rc = cleanup.run([TEMP], cleanup.DEFAULT_IMAGES, lister=_procs, killer=lambda pid: pid != 12, emit=out.append)
    assert rc == 1 and any("could NOT stop nexus-service.exe pid 12" in ln for ln in out)


def test_run_with_nothing_to_stop_says_so_and_exits_zero() -> None:
    out: list[str] = []
    killed: list[int] = []
    rc = cleanup.run([r"C:\elsewhere"], cleanup.DEFAULT_IMAGES, lister=_procs, killer=lambda pid: killed.append(pid) or True, emit=out.append)
    assert rc == 0 and killed == [] and "no stray" in out[-1]
    assert f"examined {len(_procs())} process" in out[0], "the denominator is printed, so a clean is distinguishable from never-listed"


def test_dry_run_kills_nothing() -> None:
    killed: list[int] = []
    out: list[str] = []
    rc = cleanup.run([TEMP], cleanup.DEFAULT_IMAGES, lister=_procs, killer=lambda pid: killed.append(pid) or True, emit=out.append, dry_run=True)
    assert rc == 0 and killed == [] and out
    assert all("would stop" in ln for ln in out if not ln.startswith("cleanup: examined"))


def test_main_refuses_off_windows_and_without_a_scope(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cleanup, "sys", types.SimpleNamespace(platform="linux", stderr=sys.stderr))
    assert cleanup.main(["--under", TEMP]) == 2
    assert "Windows only" in capsys.readouterr().err
    monkeypatch.setattr(cleanup, "sys", types.SimpleNamespace(platform="win32", stderr=sys.stderr))
    assert cleanup.main([]) == 2
    assert "--under" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Wiring: every Windows job ends with the cleanup, behind always()
# --------------------------------------------------------------------------- #

ACTION = REPO / ".github" / "actions" / "windows-job-cleanup" / "action.yml"
WORKFLOWS = REPO / ".github" / "workflows"
#: A win-release job that starts neither an engine nor Postgres, so the cleanup (which only ever stops those)
#: has nothing to do there. The reason is checked below, not trusted: such a job must not call any step
#: that starts them.
NO_ENGINE_OR_POSTGRES = {
    ("windows-pg-bundle-rehearsal.yml", "conformance"): "runs the supervisor conformance suite under NX_TEST_T2_SUBSTRATE=none",
}
_STARTS_ENGINE_OR_POSTGRES = ("windows-engine-leg", "engine_windows_smoke", "pg_bundle_windows_smoke", "build_pg_bundle_windows", "nexus-service.exe", "postgres.exe")

#: Every job that can land on win-release, DISCOVERED (a sixth job added tomorrow is on this list without anyone
#: editing it), as (workflow, job id).
def _discover_windows_jobs() -> list[tuple[str, str]]:
    """(workflow file, job id) for every job whose runs-on or matrix can land on win-release. Read as UTF-8
    explicitly: the rehearsal runs this file on Windows, where the default codec would choke on the
    workflows' non-ASCII comments."""
    found: list[tuple[str, str]] = []
    for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_id, job in ((doc or {}).get("jobs") or {}).items():
            if isinstance(job, dict) and "win-release" in yaml.safe_dump({"r": job.get("runs-on"), "s": job.get("strategy")}):
                found.append((path.name, job_id))
    return found


WINDOWS_JOBS = _discover_windows_jobs()


def test_the_discovered_windows_jobs_include_the_ones_the_cleanup_was_written_for() -> None:
    """Non-vacuity: a discovery that found nothing would pass every parametrised check below."""
    assert {
        ("engine-service-release.yml", "build-publish-pg-bundle-windows"),
        ("engine-service-release.yml", "build-publish-engine-windows"),
        ("pg-bundle-cache-seed.yml", "seed-windows"),
        ("windows-pg-bundle-rehearsal.yml", "bundle"),
        ("windows-pg-bundle-rehearsal.yml", "engine"),
    } <= set(WINDOWS_JOBS), WINDOWS_JOBS
    assert set(NO_ENGINE_OR_POSTGRES) <= set(WINDOWS_JOBS), "an exemption for a job that no longer exists"


def test_the_action_runs_the_script_over_both_job_roots_without_failing_when_python_is_unresolved() -> None:
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    run = "\n".join(s.get("run", "") for s in steps)
    assert "windows_job_cleanup.py" in run
    assert '--under "$env:RUNNER_TEMP"' in run and '--under "$env:GITHUB_WORKSPACE"' in run
    assert "$LASTEXITCODE" in run, "an engine that could not be stopped must fail the job"


@pytest.mark.parametrize(("workflow", "job"), WINDOWS_JOBS)
def test_every_windows_job_ends_with_the_always_cleanup(workflow: str, job: str) -> None:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))["jobs"][job]
    steps = doc["steps"]
    if (workflow, job) in NO_ENGINE_OR_POSTGRES:
        text = yaml.safe_dump(doc)
        started = [needle for needle in _STARTS_ENGINE_OR_POSTGRES if needle in text]
        assert not started, f"{workflow}:{job} is exempt from the cleanup but starts {started}"
        assert doc["env"]["NX_TEST_T2_SUBSTRATE"] == "none", "the exemption's premise: no Postgres substrate"
        return
    last = steps[-1]
    assert last.get("uses") == "./.github/actions/windows-job-cleanup", f"{workflow}:{job} does not end with the cleanup: {last}"
    assert str(last.get("if", "")).replace(" ", "") == "always()", f"{workflow}:{job}: the cleanup must run on cancel and failure"


def test_the_rehearsal_triggers_on_the_cleanup_inputs() -> None:
    doc = yaml.safe_load((WORKFLOWS / "windows-pg-bundle-rehearsal.yml").read_text(encoding="utf-8"))
    paths = (doc.get("on") or doc.get(True))["push"]["paths"]
    for needed in ("scripts/windows_job_cleanup.py", "tests/test_windows_job_cleanup.py", ".github/actions/windows-job-cleanup/**"):
        assert needed in paths, needed
