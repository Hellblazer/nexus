# SPDX-License-Identifier: AGPL-3.0-or-later
"""conexus/hooks/scripts/nx_setup_notice.py: the session-start hook that tells the
model the truth about Nexus on this machine (RDR-228 Phase 1 Step 3, nexus-sv2wx.7;
Phase 2 Step 4, nexus-sv2wx.17).

It reports the first of eight states that matches, in the record's order, and the
order is what most of these cases pin:

1. a setup run is live: its stage;
2. the CLI is found and cannot start (Sam, 2026-10-14): say so, and that setup repairs it;
3. the CLI resolves and a service is usable: nothing, whatever an earlier run left behind;
4. a run was interrupted: say so, name the log, say that ``setup`` called again resumes;
5. the last run failed: the failure line and the log;
6. no CLI: say so and put the one question (the "not now" marker turns this into one line);
7. the CLI resolves, no usable service, a local service is provisioned: it is down;
8. the CLI resolves, no usable service, none provisioned: none is set up.

It starts no process, opens no socket and makes no network request, writes no file, and
exits 0 whatever happens. The cases run the real script as a subprocess. Every child
environment sets ``NX_TOOLS_DIR``, ``NEXUS_CONFIG_DIR`` and a ``PATH`` the test owns, so the
developer's own install and service are not found.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from tests._platform import WINDOWS_SHEBANG, install_windows_exe_fake, prune_python, scrubbed_env

_ROOT = Path(__file__).resolve().parents[2]
_HOOK = _ROOT / "conexus" / "hooks" / "scripts" / "nx_setup_notice.py"
_LAUNCH = _ROOT / "conexus" / "launch"

_FIRST = "uv tool install conexus --python 3.12" + (" --torch-backend cpu" if sys.platform.startswith("linux") else "")
_MANUAL_COMMANDS = (_FIRST, "nx self install", "nx init")
_BROKEN_PHRASE = "Nexus is installed but cannot start; setup repairs it"
_ASK_FIRST = re.compile(r"ask the user first|if the user agrees|ASK THE USER FIRST", re.IGNORECASE)

#: Makes any socket or any child process a failure for the whole child interpreter, and
#: leaves a mark when one is attempted: the hook swallows every exception, so without a
#: mark a refused call in a silent state would pass as silence.
_NO_SIDE_EFFECTS = """
import os, socket, subprocess
def _refuse(*a, **k):
    with open(os.environ["NX_REFUSED_MARK"], "w") as mark:
        mark.write("refused")
    raise RuntimeError("the setup hook must not open a socket or start a process")
socket.socket = _refuse
subprocess.Popen = _refuse
"""


def _dirs(tmp_path: Path) -> dict[str, Path]:
    tmp_path.mkdir(exist_ok=True)
    dirs = {name: tmp_path / name for name in ("home", "tools", "bin", "config", "site")}
    for d in dirs.values():
        d.mkdir()
    return dirs


def _install_nx_mcp(bindir: Path) -> None:
    if os.name == "nt":
        install_windows_exe_fake(bindir, "nx-mcp", f"{WINDOWS_SHEBANG}\nprint('real')\n")
    else:
        fake = bindir / "nx-mcp"
        fake.write_text("#!/bin/sh\necho real\n")
        fake.chmod(0o755)


def _point_current(tools: Path, gen: Path) -> None:
    if os.name == "nt":
        import _winapi  # noqa: PLC0415 -- Windows only

        _winapi.CreateJunction(str(gen), str(tools / "current"))
    else:
        os.symlink(gen, tools / "current")


def _run(
    dirs: dict[str, Path], *, guarded: bool = False, hook: Path = _HOOK, remote: str | None = None,
    extra_env: dict[str, str] | None = None, **popen: object,
) -> subprocess.CompletedProcess[bytes]:
    env = scrubbed_env(
        PATH=str(dirs["bin"]), NX_TOOLS_DIR=str(dirs["tools"]), NEXUS_CONFIG_DIR=str(dirs["config"]),
        HOME=str(dirs["home"]), USERPROFILE=str(dirs["home"]),
    )
    if remote is not None:
        env["CLAUDE_CODE_REMOTE"] = remote
    env.update(extra_env or {})
    if guarded:
        (dirs["site"] / "sitecustomize.py").write_text(_NO_SIDE_EFFECTS)
        env["PYTHONPATH"] = str(dirs["site"])
        env["NX_REFUSED_MARK"] = str(dirs["site"] / "refused.mark")
    return subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({"hook_event_name": "SessionStart", "source": "startup"}).encode(),
        capture_output=True, env=env, timeout=30, check=False, cwd=str(dirs["home"]), **popen,
    )


def _plugin_copy(tmp_path: Path) -> Path:
    """The plugin directories the hook loads from, copied with no bytecode."""
    for sub in ("hooks/scripts", "launch"):
        shutil.copytree(
            _ROOT / "conexus" / sub, tmp_path / "plugin" / sub, ignore=shutil.ignore_patterns("__pycache__"),
        )
    return tmp_path / "plugin"


def _tree(dirs: dict[str, Path]) -> list[str]:
    return sorted(
        str(p.relative_to(base.parent)) for name, base in dirs.items() if name != "site" for p in base.rglob("*")
    )


def _context(r: subprocess.CompletedProcess[bytes]) -> str:
    """The text the hook hands the model. Standard output must be exactly one
    JSON object in the SessionStart shape, and nothing else."""
    out = json.loads(r.stdout.decode("utf-8"))
    assert set(out) == {"hookSpecificOutput"}, out
    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "SessionStart"
    assert set(specific) == {"hookEventName", "additionalContext"}, specific
    return specific["additionalContext"]


def _load(path: Path, name: str):
    for directory in (str(_ROOT / "conexus" / "hooks" / "scripts"), str(_LAUNCH)):
        if directory not in sys.path:
            sys.path.insert(0, directory)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -- the facts each state is built from ------------------------------------------------------------


def _service_usable(dirs: dict[str, Path]) -> None:
    """A saved address and token: a usable service, with nothing running."""
    (dirs["config"] / "config.yml").write_text("credentials:\n  service_url: https://svc.example\n  service_token: tok\n")


def _address_without_a_token(dirs: dict[str, Path]) -> None:
    (dirs["config"] / "config.yml").write_text("credentials:\n  service_url: https://svc.example\n")


def _setup_dir(dirs: dict[str, Path]) -> Path:
    path = dirs["config"] / "setup"
    path.mkdir(exist_ok=True)
    return path


def _status(dirs: dict[str, Path], **fields: object) -> None:
    (_setup_dir(dirs) / "status.json").write_text(json.dumps(fields), encoding="utf-8")


def _lock(dirs: dict[str, Path], *, age: float) -> None:
    lock = _setup_dir(dirs) / "lock"
    lock.mkdir(exist_ok=True)
    (lock / "owner").write_text("a-token", encoding="utf-8")
    heartbeat = lock / "heartbeat"
    heartbeat.write_text("x", encoding="utf-8")
    then = time.time() - age
    os.utime(heartbeat, (then, then))


def _live_run(dirs: dict[str, Path]) -> None:
    _status(dirs, state="running", stage="build", mode="local", log=str(_setup_dir(dirs) / "setup.log"))
    _lock(dirs, age=1.0)


def _interrupted_run(dirs: dict[str, Path]) -> None:
    _status(dirs, state="running", stage="init", mode="local", autostart=True)
    _lock(dirs, age=3600.0)


def _failed_run(dirs: dict[str, Path]) -> None:
    _status(dirs, state="failed", stage="build", mode="cli-only", message="the build exited 2.", failed_step="build")


def _doctor_failed_run(dirs: dict[str, Path]) -> None:
    _status(dirs, state="doctor-failed", stage="verify", mode="local", doctor_failing=["[✗] something"])


def _not_now(dirs: dict[str, Path]) -> None:
    (_setup_dir(dirs) / "not-now").write_text("2026-01-01T00:00:00Z", encoding="utf-8")


def _provisioned(dirs: dict[str, Path]) -> None:
    config = dirs["config"]
    (config / "postgres").mkdir()
    (config / "service").mkdir()
    (config / "pg_credentials").write_text("x")
    (config / "postgres" / "PG_VERSION").write_text("17")
    (config / "service" / ("nexus-service.exe" if os.name == "nt" else "nexus-service")).write_text("x")


def _cli_cannot_start(dirs: dict[str, Path]) -> None:
    """The layout holds ``nx-mcp`` and a ``pyvenv.cfg`` naming a Python that is gone."""
    gen = dirs["tools"] / "gen-20260101T000000Z"
    (gen / "bin").mkdir(parents=True)
    _install_nx_mcp(gen / "bin")
    prune_python(gen, dirs["home"] / "uv" / "cpython-3.12-pruned")
    _point_current(dirs["tools"], gen)


def _cli(dirs: dict[str, Path]) -> None:
    _install_nx_mcp(dirs["bin"])


def _build(dirs: dict[str, Path], *facts: Callable[[dict[str, Path]], None]) -> dict[str, Path]:
    for fact in facts:
        fact(dirs)
    return dirs


#: One machine per state, built from the fewest facts that make it that state.
STATES: dict[str, tuple[Callable[[dict[str, Path]], None], ...]] = {
    "1 a setup run is live": (_cli, _live_run),
    "2 the CLI cannot start": (_cli_cannot_start,),
    "3 the CLI and a usable service": (_cli, _service_usable),
    "4 a run was interrupted": (_cli, _interrupted_run),
    "5 the last run failed": (_cli, _failed_run),
    "6 no CLI": (),
    "7 the CLI, no service, one provisioned": (_cli, _provisioned),
    "8 the CLI, no service, none provisioned": (_cli,),
}

#: What each state's text must contain.
EXPECT: dict[str, tuple[str, ...]] = {
    "1 a setup run is live": ("setup run is in progress", "Stage: build", "setup_status"),
    "2 the CLI cannot start": (_BROKEN_PHRASE, "cpython-3.12-pruned", "/conexus:setup", "cli-only"),
    "4 a run was interrupted": ("interrupted", "setup.log", "call setup again"),
    "5 the last run failed": ("Setup failed.", "the build exited 2.", "setup.log", "call setup again"),
    "6 no CLI": ("Nexus is not installed", "| Local |", "| CLI only |", "| Not now |", "/conexus:setup"),
    "7 the CLI, no service, one provisioned": ("not running", "/conexus:setup"),
    "8 the CLI, no service, none provisioned": ("no Nexus service is set up", "/conexus:setup"),
}


def _says(r: subprocess.CompletedProcess[bytes]) -> str | None:
    assert r.returncode == 0, r.stderr
    assert r.stderr == b""
    return _context(r) if r.stdout else None


@pytest.mark.parametrize("state", sorted(STATES))
def test_each_state_says_what_it_is(tmp_path: Path, state: str) -> None:
    text = _says(_run(_build(_dirs(tmp_path), *STATES[state])))
    if state == "3 the CLI and a usable service":
        assert text is None
        return
    assert text is not None, state
    for needle in EXPECT[state]:
        assert needle in text, (needle, text)


def test_the_states_are_exactly_the_eight_in_the_records_order() -> None:
    assert [name.split()[0] for name in STATES] == list("12345678")


# -- every text that suggests a call asks first, in the setup tools' own words ---------------------


@pytest.mark.parametrize(
    "state", [s for s in sorted(STATES) if s not in ("1 a setup run is live", "3 the CLI and a usable service")],
)
def test_every_text_that_suggests_an_action_asks_the_user_first(tmp_path: Path, state: str) -> None:
    text = _says(_run(_build(_dirs(tmp_path), *STATES[state])))
    assert text is not None and _ASK_FIRST.search(text), text


def test_states_1_4_and_5_use_the_setup_tools_wording_exactly(tmp_path: Path) -> None:
    """The hook renders the same status the ``setup_status`` tool renders: the resume
    line with the recorded choice, the log, the next step. Same function, so not two
    texts that drift."""
    nx_setup = _load(_LAUNCH / "nx_setup.py", "nx_setup_for_notice_wording")
    standin = _load(_LAUNCH / "nx_standin.py", "nx_standin_for_notice_wording")
    for state in ("1 a setup run is live", "4 a run was interrupted", "5 the last run failed"):
        dirs = _build(_dirs(tmp_path / state.split()[0]), *STATES[state])
        text = _says(_run(dirs))
        view = nx_setup.status_view(dirs["config"] / "setup")
        assert text is not None and standin.render(view) in text, state


def test_no_cli_puts_the_one_question_with_the_standins_table(tmp_path: Path) -> None:
    standin = _load(_LAUNCH / "nx_standin.py", "nx_standin_for_notice_table")
    text = _says(_run(_dirs(tmp_path)))
    assert text is not None
    assert standin.QUESTION_TABLE in text and standin.LOCAL_FOLLOW_UP in text
    assert "ASK THE USER FIRST" in text
    assert "Cloud" not in text and "cloud" not in text, "the Cloud row waits for the credentials page (nexus-sv2wx.18)"
    for command in _MANUAL_COMMANDS:
        assert f"`{command}`" in text
    assert text.rstrip().endswith(standin.INSTALL_PAGE)
    assert "reconnects the `nexus` server or starts a new session" in text


# -- the order between the states -----------------------------------------------------------------------


def test_a_live_run_is_reported_before_anything_else(tmp_path: Path) -> None:
    """Live beats a CLI that cannot start, a usable service and no CLI."""
    for i, facts in enumerate([(_cli_cannot_start, _live_run), (_cli, _service_usable, _live_run), (_live_run,)]):
        text = _says(_run(_build(_dirs(tmp_path / str(i)), *facts)))
        assert text is not None and "setup run is in progress" in text, facts


def test_a_cli_that_cannot_start_beats_a_usable_service(tmp_path: Path) -> None:
    """Sam, 2026-10-14: checked right after a live run, before 'usable service'. The
    server the session got is the stand-in, so a service that answers does not
    make Nexus work."""
    text = _says(_run(_build(_dirs(tmp_path), _cli_cannot_start, _service_usable)))
    assert text is not None and _BROKEN_PHRASE in text


def test_a_cli_that_cannot_start_also_reports_a_failed_or_interrupted_last_run(tmp_path: Path) -> None:
    for i, run in enumerate((_failed_run, _interrupted_run, _doctor_failed_run)):
        text = _says(_run(_build(_dirs(tmp_path / str(i)), _cli_cannot_start, run)))
        assert text is not None and _BROKEN_PHRASE in text and "setup.log" in text, run


def test_a_usable_service_beside_a_failed_status_is_silent(tmp_path: Path) -> None:
    """Record: state 3 holds whatever an earlier run left in the setup directory."""
    for i, run in enumerate((_failed_run, _interrupted_run, _doctor_failed_run, _not_now)):
        dirs = _build(_dirs(tmp_path / str(i)), _cli, _service_usable, run)
        assert _says(_run(dirs)) is None, run


def test_a_live_lease_beside_a_failed_status_is_silent(tmp_path: Path) -> None:
    """The third resolver leg, a local service's lease, is a usable service too."""
    from nexus.daemon.service_registry import ServiceRegistry, service_identity

    dirs = _build(_dirs(tmp_path), _cli, _failed_run)
    ServiceRegistry(dir=dirs["config"], tier="storage_service").publish(
        service_identity(), endpoint={"host": "127.0.0.1", "port": 4242, "token": "t"}, version="t", owner_token="o",
    )
    assert _says(_run(dirs)) is None


def test_an_address_with_no_token_is_not_a_usable_service(tmp_path: Path) -> None:
    """Residual 9: the plugin's old resolver returned the address, the CLI raises."""
    text = _says(_run(_build(_dirs(tmp_path), _cli, _address_without_a_token)))
    assert text is not None and "no Nexus service is set up" in text


def test_a_failed_run_is_reported_before_the_missing_cli(tmp_path: Path) -> None:
    text = _says(_run(_build(_dirs(tmp_path), _failed_run)))
    assert text is not None and "Setup failed." in text and "Nexus is not installed" not in text


def test_a_doctor_failure_and_an_unsupported_host_are_failures_too(tmp_path: Path) -> None:
    text = _says(_run(_build(_dirs(tmp_path / "a"), _cli, _doctor_failed_run)))
    assert text is not None and "nx doctor reported problems" in text
    dirs = _build(_dirs(tmp_path / "b"), _cli)
    _status(dirs, state="unsupported", stage="starting", mode="local", message="cannot detach here")
    text = _says(_run(dirs))
    assert text is not None and "cannot run on this host" in text


def test_a_finished_run_with_the_cli_and_no_service_still_says_there_is_no_service(tmp_path: Path) -> None:
    dirs = _build(_dirs(tmp_path), _cli)
    _status(dirs, state="done", stage="verify", mode="cli-only")
    text = _says(_run(dirs))
    assert text is not None and "no Nexus service is set up" in text


def test_a_provisioned_service_that_is_down_is_state_7_and_not_8(tmp_path: Path) -> None:
    down = _says(_run(_build(_dirs(tmp_path / "a"), _cli, _provisioned)))
    none = _says(_run(_build(_dirs(tmp_path / "b"), _cli)))
    assert down is not None and none is not None and down != none
    assert "not running" in down and "no Nexus service is set up" in none


@pytest.mark.parametrize("state", ["7 the CLI, no service, one provisioned", "8 the CLI, no service, none provisioned"])
def test_states_7_and_8_are_one_line(tmp_path: Path, state: str) -> None:
    text = _says(_run(_build(_dirs(tmp_path), *STATES[state])))
    assert text is not None and "\n" not in text.strip()


# -- the user's choice is honoured (Sam, 2026-10-14) ---------------------------------------------------
#
# A recorded CLI-only choice (the status says mode cli-only, state done) or the "not now"
# marker turns states 7 and 8 into one quiet line that names /conexus:setup and asks nothing.
# The marker also turns a stale failed or interrupted run's report (4, 5) into one quiet line
# naming /conexus:setup and the log. A live run (1), a CLI that cannot start (2) and a usable
# service (3) are not affected, and state 6 already read the marker.


def _cli_only_done(dirs: dict[str, Path]) -> None:
    _status(dirs, state="done", stage="verify", mode="cli-only", message="The Nexus CLI is installed. No service is set up.")


#: The states the marker changes, and the ones it leaves alone.
_MARKER_CHANGES = ("4 a run was interrupted", "5 the last run failed", "6 no CLI", "7 the CLI, no service, one provisioned", "8 the CLI, no service, none provisioned")
_MARKER_LEAVES = ("1 a setup run is live", "2 the CLI cannot start", "3 the CLI and a usable service")


def _quiet(text: str | None) -> bool:
    """One line, names /conexus:setup, asks nothing."""
    return (
        text is not None and "\n" not in text.strip() and "/conexus:setup" in text
        and not _ASK_FIRST.search(text) and "?" not in text and "| Local |" not in text
    )


def test_the_marker_and_the_states_it_changes_and_leaves_are_all_the_eight() -> None:
    assert sorted(_MARKER_CHANGES + _MARKER_LEAVES) == sorted(STATES)


@pytest.mark.parametrize("state", _MARKER_CHANGES)
def test_the_marker_turns_the_state_into_one_quiet_line(tmp_path: Path, state: str) -> None:
    dirs = _build(_dirs(tmp_path), *STATES[state])
    without = _says(_run(dirs))
    _not_now(dirs)
    with_marker = _says(_run(dirs))
    assert without is not None and with_marker is not None and with_marker != without
    assert _quiet(with_marker), with_marker
    if state in ("4 a run was interrupted", "5 the last run failed"):
        assert "setup.log" in with_marker, "it names the log"
    if state == "6 no CLI":
        assert "Nexus is not installed" in with_marker


@pytest.mark.parametrize("state", _MARKER_LEAVES)
def test_the_marker_changes_nothing_in_a_live_run_a_broken_cli_or_a_usable_service(tmp_path: Path, state: str) -> None:
    dirs = _build(_dirs(tmp_path), *STATES[state])
    without = _says(_run(dirs))
    _not_now(dirs)
    assert _says(_run(dirs)) == without, state


@pytest.mark.parametrize("state", ["7 the CLI, no service, one provisioned", "8 the CLI, no service, none provisioned"])
def test_a_recorded_cli_only_choice_turns_7_and_8_into_one_quiet_line(tmp_path: Path, state: str) -> None:
    dirs = _build(_dirs(tmp_path), *STATES[state])
    asks = _says(_run(dirs))
    assert asks is not None and _ASK_FIRST.search(asks), "without the choice it asks"
    _cli_only_done(dirs)
    quiet = _says(_run(dirs))
    assert _quiet(quiet), quiet
    marker_dirs = _build(_dirs(tmp_path / "m"), *STATES[state], _not_now)
    assert _says(_run(marker_dirs)) == quiet, "the marker and the choice say the same thing"


def test_the_quiet_lines_for_7_and_8_differ_and_say_what_is_true(tmp_path: Path) -> None:
    down = _says(_run(_build(_dirs(tmp_path / "a"), _cli, _provisioned, _cli_only_done)))
    none = _says(_run(_build(_dirs(tmp_path / "b"), _cli, _cli_only_done)))
    assert down is not None and none is not None and down != none
    assert "not running" in down and "no Nexus service" in none


def test_a_recorded_cli_only_choice_changes_no_other_state(tmp_path: Path) -> None:
    """The choice is read only where a service is missing: a live run, a CLI that cannot
    start, a usable service, a failed run and no CLI at all read as they do without it."""
    for i, (facts, expect) in enumerate([
        ((_cli_cannot_start,), _BROKEN_PHRASE),
        ((_cli, _service_usable), None),
        ((), "Nexus is not installed"),
    ]):
        dirs = _build(_dirs(tmp_path / str(i)), *facts)
        before = _says(_run(dirs))
        _cli_only_done(dirs)
        after = _says(_run(dirs))
        if expect is None:
            assert before is None and after is None
        else:
            assert before is not None and after is not None and expect in after
            assert (before == after) or facts == (_cli_cannot_start,), (facts, before, after)


def test_only_a_finished_cli_only_run_is_the_choice() -> None:
    """``chose_cli_only`` reads the record: the CLI-only mode AND a run that finished. (Where
    the hook asks, a failed or interrupted run has already been reported, so the second half
    cannot be seen through the hook; the function states it.)"""
    mod = _load(_HOOK, "nx_setup_notice_choice")
    assert mod.chose_cli_only({"mode": "cli-only", "state": "done"}) is True
    for view in (
        {"mode": "cli-only", "state": "failed"}, {"mode": "cli-only", "state": "interrupted"},
        {"mode": "cli-only", "state": "running"}, {"mode": "cli-only", "state": "doctor-failed"},
        {"mode": "local", "state": "done"}, {"mode": "not-now", "state": "done"}, {"state": "done"}, {},
    ):
        assert mod.chose_cli_only(view) is False, view


def test_a_cli_only_choice_whose_run_failed_still_reports_the_failure(tmp_path: Path) -> None:
    """Only the marker quiets a stale failure: ``mode cli-only`` with state failed is a run
    that did not finish, and the user has not said to stop hearing about it."""
    dirs = _build(_dirs(tmp_path), _cli, _failed_run)
    text = _says(_run(dirs))
    assert text is not None and "Setup failed." in text and _ASK_FIRST.search(text)
    assert not _quiet(text)


def test_a_local_choice_with_the_service_gone_still_asks(tmp_path: Path) -> None:
    dirs = _build(_dirs(tmp_path), _cli)
    _status(dirs, state="done", stage="verify", mode="local")
    text = _says(_run(dirs))
    assert text is not None and _ASK_FIRST.search(text) and "no Nexus service is set up" in text


def test_a_cli_only_choice_beside_a_provisioned_service_that_is_down_is_quiet_too(tmp_path: Path) -> None:
    """The literal rule (Sam): CLI only quiets 7 as well as 8."""
    text = _says(_run(_build(_dirs(tmp_path), _cli, _provisioned, _cli_only_done)))
    assert _quiet(text) and "not running" in text


# -- the hook starts nothing, opens nothing, writes nothing ---------------------------------------------


@pytest.mark.parametrize("state", sorted(STATES))
def test_in_no_state_does_it_open_a_socket_or_start_a_process(tmp_path: Path, state: str) -> None:
    dirs = _build(_dirs(tmp_path), *STATES[state])
    plain = _says(_run(dirs))
    guarded = _run(dirs, guarded=True)
    assert not (dirs["site"] / "refused.mark").exists(), "the hook tried to open a socket or start a process"
    assert _says(guarded) == plain, state


def test_the_guard_in_the_test_above_really_refuses(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    (dirs["site"] / "sitecustomize.py").write_text(_NO_SIDE_EFFECTS)
    r = subprocess.run(
        [sys.executable, "-c", "import subprocess; subprocess.Popen(['x'])"],
        capture_output=True,
        env=scrubbed_env(PYTHONPATH=str(dirs["site"]), NX_REFUSED_MARK=str(dirs["site"] / "refused.mark")),
        timeout=30, check=False,
    )
    assert r.returncode != 0 and b"must not open a socket or start a process" in r.stderr
    assert (dirs["site"] / "refused.mark").exists(), "the mark is how a swallowed refusal is seen"


@pytest.mark.parametrize("state", sorted(STATES))
def test_in_no_state_does_it_write_anything(tmp_path: Path, state: str) -> None:
    """Not under the home, the config or the tools directory, and not in the plugin's own
    directory either: importing its siblings must leave no bytecode cache in the installed
    plugin. Until ``setup`` is called a session start writes nothing."""
    dirs = _build(_dirs(tmp_path), *STATES[state])
    plugin = _plugin_copy(tmp_path)
    dirs["plugin"] = plugin
    before = _tree(dirs)
    r = _run(dirs, hook=plugin / "hooks" / "scripts" / "nx_setup_notice.py")
    assert r.returncode == 0 and r.stderr == b""
    assert _tree(dirs) == before, state


def test_a_session_start_with_nothing_set_up_leaves_the_config_directory_absent(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    shutil.rmtree(dirs["config"])
    assert _says(_run(dirs)) is not None
    assert not dirs["config"].exists(), "the hook must not create the Nexus config directory"


# -- the rest of the Phase 1 pins ----------------------------------------------------------------------


@pytest.mark.parametrize("value", ["true", "TRUE", "1"])
@pytest.mark.parametrize("state", sorted(STATES))
def test_in_a_remote_workspace_the_hook_says_nothing_in_every_state(tmp_path: Path, value: str, state: str) -> None:
    """A Desktop conversation's loop runs in a cloud workspace that has ``uv`` and no
    CLI, while the servers run on the user's machine (measured 2026-10-13, T2
    nexus_rdr/228-research-22). The hook cannot see those servers from there, so it
    says nothing and reads nothing, in any state."""
    r = _run(_build(_dirs(tmp_path), *STATES[state]), remote=value)
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")


@pytest.mark.parametrize("value", ["", "false", "0", "no"])
def test_a_remote_marker_that_is_not_set_true_does_not_silence_the_hook(tmp_path: Path, value: str) -> None:
    r = _run(_dirs(tmp_path), remote=value)
    assert r.returncode == 0
    assert "Nexus is not installed" in _context(r)


def test_a_cli_in_the_generation_layout_alone_is_a_cli(tmp_path: Path) -> None:
    """The lookup is the launcher's: the layout, then PATH. An application started
    before the install has the old PATH and must still be told nothing."""
    dirs = _dirs(tmp_path)
    gen = dirs["tools"] / "gen-20260101T000000Z"
    (gen / "bin").mkdir(parents=True)
    _install_nx_mcp(gen / "bin")
    _point_current(dirs["tools"], gen)
    _service_usable(dirs)
    assert _says(_run(dirs)) is None


def test_it_exits_0_with_standard_output_closed_or_unread(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    env = scrubbed_env(PATH=str(dirs["bin"]), NX_TOOLS_DIR=str(dirs["tools"]), NEXUS_CONFIG_DIR=str(dirs["config"]))
    closed = subprocess.run(
        [sys.executable, "-c", f"import os, runpy, sys; os.close(1); sys.argv=[{str(_HOOK)!r}]; runpy.run_path({str(_HOOK)!r}, run_name='__main__')"],
        capture_output=True, env=env, timeout=30, check=False,
    )
    assert closed.returncode == 0, closed.stderr
    proc = subprocess.Popen([sys.executable, str(_HOOK)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.close()
    assert proc.wait(timeout=30) == 0
    assert proc.stderr.read() == b""


@pytest.mark.parametrize("missing", ["launch", "hooks/scripts/_endpoint_resolve.py", "launch/nx_setup.py"])
def test_it_exits_0_when_a_sibling_cannot_be_imported(tmp_path: Path, missing: str) -> None:
    """A plugin laid out without a part the hook loads: silence and exit 0, not a traceback."""
    plugin = _plugin_copy(tmp_path)
    target = plugin / missing
    shutil.rmtree(target) if target.is_dir() else target.unlink()
    r = _run(_dirs(tmp_path / "d"), hook=plugin / "hooks" / "scripts" / "nx_setup_notice.py")
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")


def test_the_hook_speaks_exactly_when_the_launcher_serves_the_stand_in(tmp_path: Path) -> None:
    """The same facts for both: a machine with ``nx`` and no ``nx-mcp`` (an incomplete
    install) gets the stand-in from the launcher and the notice from the hook, and the
    notice names the program that was not found. And a CLI that cannot start gets the
    stand-in from the launcher and state 2 from the hook."""
    dirs = _dirs(tmp_path)
    if os.name == "nt":
        install_windows_exe_fake(dirs["bin"], "nx", f"{WINDOWS_SHEBANG}\nprint('nx')\n")
    else:
        (dirs["bin"] / "nx").write_text("#!/bin/sh\necho nx\n")
        (dirs["bin"] / "nx").chmod(0o755)
    launch = _LAUNCH / "nx_launch.py"
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n"
    env = scrubbed_env(PATH=str(dirs["bin"]), NX_TOOLS_DIR=str(dirs["tools"]), NEXUS_CONFIG_DIR=str(dirs["config"]))

    def served_by_the_stand_in() -> bool:
        r = subprocess.run([sys.executable, str(launch), "nx-mcp"], input=ping, capture_output=True, env=env, timeout=30, check=False)
        return r.stdout.strip() == b'{"jsonrpc": "2.0", "id": 1, "result": {}}'

    assert served_by_the_stand_in()
    text = _context(_run(dirs))
    assert "`nx-mcp`) was not found" in text and "or its install is incomplete" in text

    _install_nx_mcp(dirs["bin"])
    _service_usable(dirs)
    assert not served_by_the_stand_in()
    assert _run(dirs).stdout == b""

    broken = _build(_dirs(tmp_path / "broken"), _cli_cannot_start, _service_usable)
    broken_env = scrubbed_env(PATH=str(broken["bin"]), NX_TOOLS_DIR=str(broken["tools"]), NEXUS_CONFIG_DIR=str(broken["config"]))
    r = subprocess.run([sys.executable, str(launch), "nx-mcp"], input=ping, capture_output=True, env=broken_env, timeout=30, check=False)
    assert r.stdout.strip() == b'{"jsonrpc": "2.0", "id": 1, "result": {}}', "the launcher serves the stand-in"
    assert _BROKEN_PHRASE in _context(_run(broken))


def test_the_commands_and_the_address_are_the_stand_in_s_own(tmp_path: Path) -> None:
    standin = _load(_LAUNCH / "nx_standin.py", "nx_standin_for_notice")
    text = _context(_run(_dirs(tmp_path)))
    for command in standin.manual_commands():
        assert f"`{command}`" in text
    assert text.endswith(standin.INSTALL_PAGE)


def test_it_imports_only_the_standard_library_and_its_siblings() -> None:
    tree = ast.parse(_HOOK.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            found.add((node.module or "").split(".")[0])
    assert found, "no imports found; the scan examined nothing"
    assert found <= set(sys.stdlib_module_names) | {
        "__future__", "_cli_lookup", "_endpoint_resolve", "nx_setup", "nx_standin",
    }, found
    assert "subprocess" not in found and "socket" not in found


# -- defensive: nothing the setup directory holds can make the hook fail --------------------------------


@pytest.mark.parametrize(
    "damage", ["status is not json", "status is a list", "state is unknown", "setup is a file", "lock is a file"],
)
def test_a_damaged_setup_directory_is_never_a_traceback(tmp_path: Path, damage: str) -> None:
    dirs = _build(_dirs(tmp_path), _cli)
    setup = _setup_dir(dirs)
    match damage:
        case "status is not json":
            (setup / "status.json").write_text("{ nope", encoding="utf-8")
        case "status is a list":
            (setup / "status.json").write_text("[1, 2]", encoding="utf-8")
        case "state is unknown":
            _status(dirs, state="exploded")
        case "setup is a file":
            shutil.rmtree(setup)
            setup.write_text("x", encoding="utf-8")
        case "lock is a file":
            (setup / "lock").write_text("x", encoding="utf-8")
    r = _run(dirs)
    assert r.returncode == 0 and r.stderr == b""
    assert "no Nexus service is set up" in _context(r), "a damaged record reads as no record"


def test_a_status_reader_that_raises_does_not_silence_the_other_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook wraps ``status_view`` defensively: if it raises, the hook goes on as
    if there were no setup record."""
    mod = _load(_HOOK, "nx_setup_notice_defensive")
    dirs = _dirs(tmp_path)
    for key, value in {"PATH": str(dirs["bin"]), "NX_TOOLS_DIR": str(dirs["tools"]), "NEXUS_CONFIG_DIR": str(dirs["config"])}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)

    def boom(*_a: object, **_k: object) -> dict:
        raise RuntimeError("the status reader failed")

    mod.load_siblings()
    monkeypatch.setattr(mod.nx_setup, "status_view", boom)
    text = mod.decide()
    assert text is not None and "Nexus is not installed" in text


# -- the hook's time budget on Windows: a held file cannot stretch it -------------------------------------


def _hold_unreadable(path: Path):
    """Make every read of *path* fail with a PermissionError until the returned release
    is called: a mode-000 file on POSIX; on Windows the file is opened with no sharing,
    which is what a writer in the middle of an ``os.replace`` presents to a reader."""
    if os.name != "nt":
        path.chmod(0)
        return lambda: path.chmod(0o600)
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid = ctypes.c_void_p(-1).value
    handle = k32.CreateFileW(str(path), 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)  # read+write, share none, OPEN_EXISTING
    assert handle not in (None, invalid), ctypes.get_last_error()
    return lambda: k32.CloseHandle(handle)


def _held_files(dirs: dict[str, Path]) -> list:
    """A status file and a lease, both held. Returns the release callables."""
    endpoint = _load(_ROOT / "conexus" / "hooks" / "scripts" / "_endpoint_resolve.py", "endpoint_for_budget")
    _status(dirs, state="failed", stage="build", mode="local", message="x")
    lease = dirs["config"] / f"storage_service_addr.{endpoint.service_identity()}"
    lease.write_text("{}", encoding="utf-8")
    return [_hold_unreadable(dirs["config"] / "setup" / "status.json"), _hold_unreadable(lease)]


def _held_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str):
    """The hook module on the Windows branch (driven from POSIX) with a status file and a lease
    both held, and every ``time.sleep`` recorded: returns ``(module, dirs, requested_sleeps)``."""
    from tests._module_seam import setattr_in

    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    mod = _load(_HOOK, name)
    dirs = _build(_dirs(tmp_path), _cli)
    for key, value in {"PATH": str(dirs["bin"]), "NX_TOOLS_DIR": str(dirs["tools"]), "NEXUS_CONFIG_DIR": str(dirs["config"])}.items():
        monkeypatch.setenv(key, value)
    for env_name in ("CLAUDE_CODE_REMOTE", "NX_SERVICE_URL", "NX_SERVICE_TOKEN", "NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_MINT_TOKEN"):
        monkeypatch.delenv(env_name, raising=False)
    mod.load_siblings()
    monkeypatch.setattr(mod.nx_setup, "_windows", lambda: True)
    monkeypatch.setattr(mod._endpoint_resolve, "_is_windows", lambda platform=None: True)
    requested: list[float] = []
    real_sleep = time.sleep

    def record(seconds: float) -> None:
        requested.append(seconds)
        real_sleep(seconds)

    setattr_in(monkeypatch, [mod.nx_setup, mod._endpoint_resolve], "time.sleep", record)  # their view of ``time`` only
    return mod, dirs, requested


@pytest.mark.skipif(os.name == "nt", reason="the real Windows case is the next test; this one drives the Windows branch from POSIX")
def test_a_held_status_file_and_lease_cannot_stretch_the_hook_past_its_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The status reads went through the retry that waits up to two seconds for a Windows
    sharing violation, the lease read through a second one, and the old hook read no file
    at all. The hook now asks for ONE bound, ``HOOK_READ_BUDGET_S`` (30 ms), spent across
    both: with both files held, the whole decision comes back in a fraction of a second
    (unbounded it is two reads of two seconds), reading both as absent.

    The bound is on the time the reads ASKED to wait, not on elapsed time, so a slow box
    cannot make it flaky and a regression to 0.3 s cannot hide inside a loose clock bound:
    the retries sleep ``min(10 ms, what is left)``, so together they cannot ask for more
    than the budget."""
    mod, dirs, requested = _held_hook(tmp_path, monkeypatch, "nx_setup_notice_budget")
    releases = _held_files(dirs)
    try:
        started = time.monotonic()
        text = mod.decide()
        elapsed = time.monotonic() - started
    finally:
        for release in releases:
            release()
    assert requested, "the held files caused no wait at all: the test examined nothing"
    assert sum(requested) <= mod.HOOK_READ_BUDGET_S + 1e-9, f"the reads asked to wait {sum(requested):.3f}s in all"
    assert elapsed < 0.3, f"the hook took {elapsed:.2f}s with its files held; the bound is {mod.HOOK_READ_BUDGET_S}s"
    assert text is not None and "no Nexus service is set up" in text, "both held files read as absent"


@pytest.mark.skipif(os.name == "nt", reason="drives the Windows branch from POSIX")
def test_the_read_deadline_starts_after_the_siblings_are_imported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Import time is not the reads' time. With the plugin parts slow to import (a cold start
    on Windows), a deadline taken before the import is spent before the first read, and a
    single sharing violation then reads a live run as no history. The retry must still have
    its 30 ms."""
    mod, dirs, requested = _held_hook(tmp_path, monkeypatch, "nx_setup_notice_late_deadline")
    real_load = mod.load_siblings
    slow_once = {"left": True}

    def slow_load() -> None:
        if slow_once["left"]:
            slow_once["left"] = False
            _spend(mod.HOOK_READ_BUDGET_S * 3)
        real_load()

    monkeypatch.setattr(mod, "load_siblings", slow_load)
    releases = _held_files(dirs)
    try:
        mod.decide()
    finally:
        for release in releases:
            release()
    assert requested, "the budget was spent on the import, so the first sharing violation was not retried"


def _spend(seconds: float) -> None:
    """Burn *seconds* of wall time without going through ``time.sleep`` (which the test records)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass


@pytest.mark.skipif(os.name != "nt", reason="a file opened with no sharing is the Windows case")
def test_on_windows_a_held_status_file_and_lease_end_the_real_hook_quickly(tmp_path: Path) -> None:
    dirs = _build(_dirs(tmp_path), _cli)
    releases = _held_files(dirs)
    try:
        started = time.monotonic()
        r = _run(dirs)
        elapsed = time.monotonic() - started
    finally:
        for release in releases:
            release()
    assert r.returncode == 0 and r.stderr == b""
    assert elapsed < 1.5, f"the real hook took {elapsed:.2f}s; unbounded it waits two seconds per held file"
    assert "no Nexus service is set up" in _context(r)


def test_the_budget_is_far_inside_the_hooks_100_ms() -> None:
    mod = _load(_HOOK, "nx_setup_notice_budget_value")
    assert 0 < mod.HOOK_READ_BUDGET_S <= 0.03


# -- the words fit the state of the last run (review 2, N5) ---------------------------------------


_LAST_RUNS = {
    "interrupted": ("was interrupted", "did not finish"),
    "failed": ("failed", "did not finish"),
    "doctor-failed": ("finished with nx doctor problems", "did not finish"),
    "unsupported": ("could not run on this host", "did not finish"),
}


@pytest.mark.parametrize("state", list(_LAST_RUNS))
def test_the_quiet_line_for_an_unfinished_run_says_what_happened_to_it(state: str) -> None:
    """``doctor-failed`` is a run that finished and a doctor that complained; ``unsupported``
    is a run that never started. Neither "did not finish"."""
    mod = _load(_HOOK, "nx_setup_notice_words")
    mod.load_siblings()
    words, wrong = _LAST_RUNS[state]
    text = mod.quiet_unfinished_text({"state": state, "log": "/c/setup/setup.log"})
    assert f"The last Nexus setup run {words}," in text and wrong not in text
    assert "/c/setup/setup.log" in text


@pytest.mark.parametrize("state", list(_LAST_RUNS))
def test_the_cannot_start_text_says_what_happened_to_the_last_run(state: str) -> None:
    mod = _load(_HOOK, "nx_setup_notice_words2")
    mod.load_siblings()
    words, wrong = _LAST_RUNS[state]
    text = mod.cannot_start_text("the Python is gone", {"state": state, "log": "/c/setup/setup.log"})
    assert f"The last setup run {words}." in text and wrong not in text
    assert mod.nx_standin.BROKEN_PHRASE in text


@pytest.mark.parametrize("state", ["none", "done", "failed", "interrupted", "doctor-failed", "unsupported"])
def test_the_cannot_start_text_and_the_stand_in_poll_agree_on_every_recorded_state(
    state: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State 2 and ``setup_status`` while the CLI still cannot start open with the same
    statement and carry the same repair offer, whatever the record says; a ``done`` is never
    read as a finished repair by either."""
    mod = _load(_HOOK, "nx_setup_notice_agree")
    mod.load_siblings()
    standin = mod.nx_standin
    view = {"state": state, "stage": "build", "mode": "cli-only", "log": "/c/setup/setup.log", "live": False}
    reason = "the Python it was built with (/x/gone) is gone"
    hook = mod.cannot_start_text(reason, view)
    monkeypatch.setattr(standin, "cli_state", lambda: ("broken", reason))
    monkeypatch.setattr(standin, "_call_script", lambda args, broken=None: dict(view))
    poll = standin.run_status("the Python it was built with (/launcher-time) is gone")
    for text in (hook, poll):
        assert standin.broken_text(reason) in text and standin.BROKEN_REPAIR in text
        assert "Setup is done" not in text and "Reconnect the nexus server or start" not in text
