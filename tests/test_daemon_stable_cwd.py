# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-x3m8e: a detached process starts in a stable cwd, never the caller's.

A MinerU server spawned from a session worktree that was later deleted answered
every task with ``FileNotFoundError`` while ``/health`` said 200. The real call,
from the server's own traceback: importing ``torchvision.ops`` pulls in
``torch._dynamo.config``, whose module body runs
``os.path.join(os.getcwd(), "torch_compile_debug")``. Python's import system is
not the culprit (a bare ``import`` works in a deleted directory); any
``getcwd``/``abspath``/``Path.cwd`` call in the process or its libraries is.

The rule lives in ``nexus.util.stable_cwd`` (re-exported by
``service_registry``, its home): a detached process starts in the filesystem
root, and a process that needs a project directory is handed it as
``NEXUS_REPO_ROOT``. These tests run REAL children against a REAL deleted
launch directory, with a no-fix control so the fix is the only difference
between "survives" and "fails".
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nexus.daemon import service_registry as sr
from nexus.util import stable_cwd
from tests._module_seam import setattr_in

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="deleting a process's own cwd is a POSIX fact; Windows refuses the rmdir",
)

ROOT = os.path.abspath(os.sep)

#: A child that waits for a go-file, then reports what the cwd-dependent calls
#: do. ``os.getcwd()`` is what ``torch._dynamo.config`` runs at import;
#: ``os.path.abspath`` is what any relative-path code runs.
_CHILD = r"""
import json, os, sys, time
go, out = sys.argv[1], sys.argv[2]
deadline = time.monotonic() + 30
while not os.path.exists(go) and time.monotonic() < deadline:
    time.sleep(0.05)
report = {}
try:
    report["cwd"] = os.getcwd()
except Exception as exc:
    report["getcwd_error"] = type(exc).__name__
try:
    report["abspath"] = os.path.abspath("relative-thing")
except Exception as exc:
    report["abspath_error"] = type(exc).__name__
with open(out + ".tmp", "w") as fh:
    json.dump(report, fh)
os.replace(out + ".tmp", out)
"""


def _wait_for(path: Path, timeout: float = 30.0) -> None:
    # The probes write a temp file and os.replace it into place, so the path
    # appears only when the JSON is complete: an existence poll on a file
    # opened with 'w' could read it empty (nexus-ys8a4, CI shard 3/6 on
    # 2ad1f90c9).
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert path.exists(), f"{path} never appeared"


def _run_child_through_a_deleted_launch_dir(
    tmp_path: Path, *, extra_popen_kwargs: dict | None, launch_name: str,
) -> dict:
    """Spawn the child with its cwd = a fresh launch dir (or the stable cwd the
    kwargs name), delete the launch dir, release the child, return its report."""
    launch = tmp_path / launch_name
    launch.mkdir()
    go, out = tmp_path / f"{launch_name}.go", tmp_path / f"{launch_name}.out"
    kwargs = dict(extra_popen_kwargs or {})
    kwargs.setdefault("cwd", str(launch))
    proc = subprocess.Popen(  # noqa: S603 — fixed argv, test-owned child
        [sys.executable, "-c", _CHILD, str(go), str(out)],
        start_new_session=True, **kwargs,
    )
    try:
        time.sleep(0.4)  # the child has started and holds its cwd
        launch.rmdir()
        go.write_text("go")
        _wait_for(out)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    return json.loads(out.read_text())


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cd = tmp_path / "nexus-config"
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cd))
    return cd


# ── the primitive ────────────────────────────────────────────────────────────


def test_the_primitive_is_shared_between_the_registry_and_the_stdlib_only_module() -> None:
    for name in ("stable_daemon_cwd", "detached_spawn_kwargs", "enter_stable_daemon_cwd", "current_project_root"):
        assert getattr(sr, name) is getattr(stable_cwd, name), name


def test_stable_cwd_is_the_filesystem_root_and_creates_nothing(cfg: Path) -> None:
    assert sr.stable_daemon_cwd() == ROOT
    assert not cfg.exists(), "handing out the stable cwd must not create the config dir"


def test_detached_spawn_kwargs_does_not_create_the_default_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
    assert sr.detached_spawn_kwargs() == {"cwd": ROOT}
    assert list(home.iterdir()) == [], "a spawn kwargs helper must not touch the disk"


def test_detached_spawn_kwargs_carries_cwd_and_no_env_for_an_absolute_config_dir(cfg: Path) -> None:
    assert sr.detached_spawn_kwargs() == {"cwd": ROOT}


def test_detached_spawn_kwargs_pins_a_relative_config_dir_against_the_callers_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative NEXUS_CONFIG_DIR would name a different directory once the
    child's cwd changes; the env copy fixes it against the CALLER's cwd."""
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", "rel-cfg")
    kw = sr.detached_spawn_kwargs()
    assert kw["env"]["NEXUS_CONFIG_DIR"] == os.path.abspath(caller / "rel-cfg")
    assert kw["cwd"] == ROOT
    # the env copy is a full environment, not just the one key
    assert kw["env"].get("PATH") == os.environ.get("PATH")


def test_detached_spawn_kwargs_hands_a_child_its_project_root(cfg: Path, tmp_path: Path) -> None:
    kw = sr.detached_spawn_kwargs(project_root=tmp_path)
    assert kw["env"][stable_cwd.REPO_ROOT_ENV] == str(tmp_path)
    assert "NEXUS_REPO_ROOT" not in os.environ, "the caller's own environment must not be edited"


def test_enter_stable_daemon_cwd_goes_to_the_root_and_pins_what_was_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = tmp_path / "start"
    start.mkdir()
    monkeypatch.chdir(start)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", "rel-cfg")
    monkeypatch.delenv("NEXUS_REPO_ROOT", raising=False)
    assert sr.enter_stable_daemon_cwd(project_root=start) == ROOT
    assert os.getcwd() == ROOT
    # pinned in THIS process, while the old directory still named it
    assert os.environ["NEXUS_CONFIG_DIR"] == os.path.abspath(start / "rel-cfg")
    assert os.environ["NEXUS_REPO_ROOT"] == str(start)
    monkeypatch.delenv("NEXUS_REPO_ROOT", raising=False)  # put back what the helper set


def test_enter_stable_daemon_cwd_survives_a_failed_chdir(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chdir failure must never be what stops a daemon starting."""
    def refuse(_path: str) -> None:
        raise PermissionError("no")

    setattr_in(monkeypatch, stable_cwd, "os.chdir", refuse)
    assert sr.enter_stable_daemon_cwd() == ""


# ── real children against a real deleted launch directory ────────────────────


def test_control_a_child_that_inherits_a_deleted_cwd_breaks(tmp_path: Path) -> None:
    """The no-fix control: WITHOUT the stable cwd the child's cwd-dependent
    calls fail once the launch directory is gone. If this stops failing the
    survival test below proves nothing."""
    report = _run_child_through_a_deleted_launch_dir(
        tmp_path, extra_popen_kwargs=None, launch_name="control",
    )
    assert report.get("getcwd_error") == "FileNotFoundError", report
    assert report.get("abspath_error") == "FileNotFoundError", report


def test_a_detached_child_survives_its_launch_cwd_being_deleted(cfg: Path, tmp_path: Path) -> None:
    report = _run_child_through_a_deleted_launch_dir(
        tmp_path, extra_popen_kwargs=sr.detached_spawn_kwargs(), launch_name="fixed",
    )
    assert "getcwd_error" not in report and "abspath_error" not in report, report
    assert report["cwd"] == ROOT


def test_a_plain_import_in_a_deleted_cwd_works_but_the_cwd_call_does_not(tmp_path: Path) -> None:
    """Pins the corrected root cause: CPython's import system guards ``getcwd``
    for the empty ``sys.path`` entry, so a lazy ``import`` is NOT what fails
    in a deleted directory; the ``getcwd`` call inside the imported module is."""
    launch = tmp_path / "doomed"
    launch.mkdir()
    probe = (
        "import os, sys, shutil\n"
        f"os.chdir({str(launch)!r}); shutil.rmtree({str(launch)!r})\n"
        "assert sys.path[0] == ''\n"
        "import json, csv, email.mime.text  # uncached modules: found without a cwd\n"
        "try:\n"
        "    os.getcwd()\n"
        "except FileNotFoundError:\n"
        "    print('import-ok getcwd-raises')\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)  # noqa: S603
    assert out.stdout.strip() == "import-ok getcwd-raises", out.stderr


# ── the project directory a detached child still needs (.nexus.yml) ──────────

_READ_CONFIG = (
    "import json; from nexus.config import load_config; "
    "print(json.dumps(load_config().get('taxonomy', {}).get('local_exclude_collections')))"
)


def _config_seen_by_a_child(tmp_path: Path, kwargs: dict) -> object:
    env = {**os.environ, "NEXUS_CONFIG_DIR": str(tmp_path / "global-cfg")}
    env.pop("NEXUS_REPO_ROOT", None)
    kw = dict(kwargs)
    if "env" in kw:
        env.update({k: v for k, v in kw.pop("env").items() if k == "NEXUS_REPO_ROOT"})
    out = subprocess.run(  # noqa: S603 — fixed argv, test-owned child
        [sys.executable, "-c", _READ_CONFIG], env=env, capture_output=True, text=True, timeout=120, **kw,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_detached_child_still_reads_the_repo_config_it_was_launched_from(tmp_path: Path) -> None:
    """The deferred-labeling child and the upgrade hook child read the per-repo
    ``.nexus.yml`` through ``load_config()``. Moved off their launch directory,
    they would silently stop; handed the root, they see the same config."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".nexus.yml").write_text("taxonomy:\n  local_exclude_collections: [marker__only]\n")
    # control: the stable cwd with no project root sees nothing of the repo
    assert _config_seen_by_a_child(tmp_path, sr.detached_spawn_kwargs()) != ["marker__only"]
    # the contract: handed the root, the child reads the repo's file
    handed = sr.detached_spawn_kwargs(project_root=str(repo))
    assert _config_seen_by_a_child(tmp_path, handed) == ["marker__only"]


def test_load_config_in_a_deleted_cwd_does_not_raise(tmp_path: Path) -> None:
    """``Path.cwd()`` in ``load_config`` is the same fault class: a process whose
    launch directory is gone must read its config, not die."""
    launch = tmp_path / "doomed"
    launch.mkdir()
    probe = (
        "import os, shutil\n"
        f"os.chdir({str(launch)!r}); shutil.rmtree({str(launch)!r})\n"
        "from nexus.config import load_config\n"
        "load_config(); print('ok')\n"
    )
    env = {**os.environ, "NEXUS_CONFIG_DIR": str(tmp_path / "global-cfg")}
    env.pop("NEXUS_REPO_ROOT", None)
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=120)  # noqa: S603
    assert out.stdout.strip() == "ok", out.stderr


# ── the MinerU server spawn ──────────────────────────────────────────────────


def test_spawn_server_process_starts_the_server_in_the_stable_cwd(
    cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``nx mineru start`` from a worktree later deleted: the real ``spawn_server_process``
    with a fake ``mineru-api`` that reports its cwd after the launch dir is gone."""
    from nexus import _mineru_spawn

    go, out = tmp_path / "srv.go", tmp_path / "srv.out"
    fake = tmp_path / "mineru-api"

    # The fake takes its go/out paths from argv in the shared child body; the
    # real spawn passes --host/--port, so hand them through the environment.
    fake.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "sys.argv[1:] = [os.environ['X3M8E_GO'], os.environ['X3M8E_OUT']]\n"
        f"{_CHILD}\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("X3M8E_GO", str(go))
    monkeypatch.setenv("X3M8E_OUT", str(out))
    pid_path = tmp_path / "mineru.pid"
    monkeypatch.setattr(_mineru_spawn, "_resolve_mineru_api_bin", lambda: str(fake))
    monkeypatch.setattr(_mineru_spawn, "_mineru_output_root", lambda: tmp_path)
    monkeypatch.setattr("nexus._mineru_pid._pid_file_path", lambda: pid_path)

    launch = tmp_path / "worktree"
    launch.mkdir()
    monkeypatch.chdir(launch)
    proc = _mineru_spawn.spawn_server_process(8123)
    assert proc is not None
    try:
        monkeypatch.chdir(tmp_path)
        launch.rmdir()  # the session worktree is deleted under the running server
        go.write_text("go")
        _wait_for(out)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    report = json.loads(out.read_text())
    assert "getcwd_error" not in report, report
    assert report["cwd"] == ROOT
    recorded = json.loads(pid_path.read_text())
    assert recorded["pid"] == proc.pid
    # what `nx mineru status` stats later: where the server runs from
    assert recorded["executable"] == str(fake)
    assert recorded["python"] == sys.executable and recorded["output_root"] == str(tmp_path)


def test_spawn_server_process_pins_a_relative_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server's env carries an ABSOLUTE NEXUS_CONFIG_DIR even when the
    caller's was relative (the child's cwd is not the caller's), and so does its
    output root."""
    from nexus import _mineru_spawn

    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", "rel-cfg")
    seen: dict = {}

    class _Proc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs)
        return _Proc()

    monkeypatch.setattr(_mineru_spawn, "_resolve_mineru_api_bin", lambda: "/fake/mineru-api")
    monkeypatch.setattr("nexus._mineru_pid._pid_file_path", lambda: tmp_path / "mineru.pid")
    setattr_in(monkeypatch, _mineru_spawn, "subprocess.Popen", fake_popen)
    assert _mineru_spawn.spawn_server_process(8124) is not None
    absolute = os.path.abspath(caller / "rel-cfg")
    assert seen["env"]["NEXUS_CONFIG_DIR"] == absolute
    assert seen["env"]["MINERU_API_OUTPUT_ROOT"] == os.path.join(absolute, "mineru-output")
    assert seen["cwd"] == ROOT
    assert seen["start_new_session"] is True


# ── the other detached spawns ────────────────────────────────────────────────


def test_the_upgrade_hook_child_starts_in_the_root_with_the_sessions_directory_handed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.hooks import upgrade_auto

    session_dir = tmp_path / "session-worktree"
    session_dir.mkdir()
    monkeypatch.chdir(session_dir)
    monkeypatch.delenv("NEXUS_REPO_ROOT", raising=False)
    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return object()

    setattr_in(monkeypatch, upgrade_auto, "subprocess.Popen", fake_popen)
    upgrade_auto._spawn_detached(["true"])
    assert captured["cwd"] == ROOT
    assert captured["env"][stable_cwd.REPO_ROOT_ENV] == os.getcwd()


# ── the in-process half, at each daemon entry ────────────────────────────────


def test_the_supervisor_foreground_entry_leaves_the_launch_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``nx daemon service start --foreground`` is what launchd/systemd/schtasks
    run; it must detach itself, and pin a relative config dir first."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from nexus.cli import main

    launch = tmp_path / "worktree"
    launch.mkdir()
    monkeypatch.chdir(launch)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", "rel-cfg")
    monkeypatch.delenv("NEXUS_REPO_ROOT", raising=False)
    seen: dict = {}

    def fake_run(*, config_dir: Path) -> int:
        seen["cwd"] = os.getcwd()
        seen["config_dir"] = config_dir
        seen["env"] = os.environ["NEXUS_CONFIG_DIR"]
        return 0

    with patch("nexus.daemon.storage_service_daemon.run_storage_supervisor", side_effect=fake_run):
        result = CliRunner().invoke(main, ["daemon", "service", "start", "--foreground"])
    assert result.exit_code == 0, result.output
    assert seen["cwd"] == ROOT, "the supervisor stayed in the directory that launched it"
    assert seen["config_dir"].is_absolute() and seen["env"] == str(seen["config_dir"])
    assert seen["env"] == os.path.abspath(launch / "rel-cfg")


def test_the_aspect_worker_entry_leaves_the_launch_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.daemon import aspect_worker_daemon as awd

    launch = tmp_path / "worktree"
    launch.mkdir()
    monkeypatch.chdir(launch)
    seen: dict = {}

    class _Daemon:
        def __init__(self, *, config_dir: Path, **_k: object) -> None:
            seen["cwd"] = os.getcwd()
            seen["config_dir"] = config_dir

        def start(self) -> None:
            raise SystemExit(0)

        def run_until_signal(self) -> None: ...

        def stop(self) -> None: ...

    monkeypatch.setattr(awd, "configure_logging", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(awd, "_require_extraction_credentials", lambda: None)
    monkeypatch.setattr(awd, "AspectWorkerDaemon", _Daemon)
    with pytest.raises(SystemExit):
        awd.run_aspect_worker_daemon(config_dir=Path("rel-aspect-cfg"), tenant="t")
    assert seen["cwd"] == ROOT
    assert seen["config_dir"] == launch / "rel-aspect-cfg", "a relative config dir must be absolute before the chdir"


def test_the_session_end_grandchild_leaves_the_launch_directory(tmp_path: Path) -> None:
    """The POSIX double-fork grandchild outlives the hook whose (disposable)
    worktree cwd it inherited, so it must chdir off it, keeping the old
    directory as NEXUS_REPO_ROOT for the per-repo config the cleanup reads."""
    launch = tmp_path / "worktree"
    launch.mkdir()
    out = tmp_path / "grandchild.json"
    probe = (
        "import json, os, sys, time\n"
        "from nexus import _session_end_launcher as m\n"
        f"out = {str(out)!r}\n"
        "def record():\n"
        "    with open(out + '.tmp', 'w') as fh:\n"
        "        json.dump({'cwd': os.getcwd(), 'root': os.environ.get('NEXUS_REPO_ROOT')}, fh)\n"
        "    os.replace(out + '.tmp', out)\n"
        "m._run_session_end_synchronously = record\n"
        "m._daemonize_and_run()\n"
        "time.sleep(5)  # the original process returns at once; give the grandchild its moment\n"
    )
    env = {**os.environ}
    env.pop("NEXUS_REPO_ROOT", None)
    subprocess.Popen(  # noqa: S603 — fixed argv, test-owned; the forked grandchild exits by itself
        [sys.executable, "-c", probe], cwd=launch, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _wait_for(out, timeout=60)
    report = json.loads(out.read_text())
    assert report["cwd"] == ROOT
    assert report["root"] == str(launch.resolve()) or report["root"] == str(launch)


def test_the_hook_verbs_session_end_grandchild_leaves_the_launch_directory(tmp_path: Path) -> None:
    """``nx hook session-end-detach`` has its own double fork (the other
    detaching daemonizer in ``src/nexus``); same fault, same rule."""
    launch = tmp_path / "worktree"
    launch.mkdir()
    out = tmp_path / "hook-grandchild.json"
    probe = (
        "import json, os\n"
        "from nexus.commands import hook as m\n"
        f"out = {str(out)!r}\n"
        "def record(*a, **k):\n"
        "    with open(out + '.tmp', 'w') as fh:\n"
        "        json.dump({'cwd': os.getcwd(), 'root': os.environ.get('NEXUS_REPO_ROOT')}, fh)\n"
        "    os.replace(out + '.tmp', out)\n"
        "m.hooks.session_end = record\n"
        "m.session_end_detach_cmd.callback()\n"
    )
    env = {**os.environ}
    env.pop("NEXUS_REPO_ROOT", None)
    subprocess.Popen(  # noqa: S603 — fixed argv, test-owned; the forked grandchild exits by itself
        [sys.executable, "-c", probe], cwd=launch, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _wait_for(out, timeout=60)
    report = json.loads(out.read_text())
    assert report["cwd"] == ROOT
    assert report["root"] in (str(launch.resolve()), str(launch))


def test_the_supervisor_argv_is_absolute_when_path_holds_a_relative_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # The daemon starts in "/", so a relative nx from PATH would miss there.
    from nexus.commands import daemon as daemon_cmd

    monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: "darwin")
    setattr_in(monkeypatch, daemon_cmd, "shutil.which", lambda name: "rel/bin/nx")
    monkeypatch.chdir(tmp_path)
    argv = daemon_cmd._resolve_nx_bin()
    assert len(argv) == 1 and Path(argv[0]).is_absolute()
    assert Path(argv[0]).resolve() == (tmp_path / "rel" / "bin" / "nx").resolve()


def test_heal_deleted_cwd_leaves_an_existing_directory_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Claude Code MCP server keeps its project directory (nexus-x6qc7)."""
    monkeypatch.chdir(tmp_path)
    assert stable_cwd.heal_deleted_cwd() is False
    assert os.getcwd() == str(tmp_path.resolve())


def test_heal_deleted_cwd_moves_the_process_off_a_deleted_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude Desktop deletes a plugin's install directory on update while its MCP
    servers run there (nexus-x6qc7, shakeout F19)."""
    gone = tmp_path / "plugin_x.old.1"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    with pytest.raises(FileNotFoundError):
        os.getcwd()
    monkeypatch.setattr(stable_cwd, "_healed_from_deleted", False)
    assert stable_cwd.heal_deleted_cwd() is True
    assert os.getcwd() == stable_cwd.stable_daemon_cwd()


def test_heal_deleted_cwd_leaves_a_non_missing_getcwd_failure_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    def _eacces() -> str:
        raise PermissionError(13, "Permission denied")

    moved: list = []
    setattr_in(monkeypatch, stable_cwd, "os.getcwd", _eacces)
    setattr_in(monkeypatch, stable_cwd, "os.chdir", lambda p: moved.append(p))
    assert stable_cwd.heal_deleted_cwd() is False
    assert not moved


def test_heal_deleted_cwd_rewrites_no_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    monkeypatch.setattr(stable_cwd, "_healed_from_deleted", False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", "relative/cfg")
    monkeypatch.delenv(stable_cwd.REPO_ROOT_ENV, raising=False)
    assert stable_cwd.heal_deleted_cwd() is True
    assert os.environ["NEXUS_CONFIG_DIR"] == "relative/cfg"
    assert stable_cwd.REPO_ROOT_ENV not in os.environ

