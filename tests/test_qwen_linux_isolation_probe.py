# SPDX-License-Identifier: AGPL-3.0-or-later
"""The qwen-linux isolation probe is informational: it reports, and fails only on what must never be true.

The probe runs on the self-hosted runner in a public repo, so its trigger and
its shape are pinned here; the steps that can run anywhere are executed.
"""
from __future__ import annotations

import getpass
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PROBE = Path(__file__).parent.parent / ".github" / "workflows" / "qwen-linux-isolation-probe.yml"


def _doc() -> dict:
    return yaml.safe_load(PROBE.read_text())


def _job() -> dict:
    return _doc()["jobs"]["probe"]


def _steps() -> list[dict]:
    return _job()["steps"]


def _step(prefix: str) -> dict:
    found = [s for s in _steps() if s["name"].startswith(prefix)]
    assert len(found) == 1, (prefix, [s["name"] for s in _steps()])
    return found[0]


def _run(step: dict, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", **(env or {})})


OWNER = "Hellblazer"
OWNER_ID = "1234"


class _GhStr(str):
    """A string whose `==` ignores case, as every GitHub Actions `==` on strings does."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, str) and self.casefold() == other.casefold()

    def __ne__(self, other: object) -> bool:
        return not self == other

    __hash__ = str.__hash__


def _runs(*, event: str = "workflow_dispatch", actor_id: str = OWNER_ID, triggering_actor: str = OWNER) -> bool:
    """Evaluate the job's `if:` the way GitHub does for these contexts (same method as test_pytest_gate_qwen_route._route)."""
    cond = str(_job()["if"]).strip()
    py = cond.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"'[^']*'", lambda m: f"_GhStr({m.group(0)})", py)
    contexts = {
        "github.event_name": event,
        "github.actor_id": actor_id,
        "github.repository_owner_id": OWNER_ID,
        "github.triggering_actor": triggering_actor,
        "github.repository_owner": OWNER,
    }
    for name in sorted(contexts, key=len, reverse=True):  # `repository_owner_id` before `repository_owner`
        py = py.replace(name, f"_GhStr({contexts[name]!r})")
    assert "github." not in py, f"unevaluated context left in {py!r}"
    return bool(eval(py, {"__builtins__": {}, "_GhStr": _GhStr}, {}))  # noqa: S307 - the expression is this repo's own YAML


def test_the_probe_triggers_on_workflow_dispatch_only_and_takes_no_permissions() -> None:
    doc = _doc()
    # PyYAML reads the bare key `on` as the boolean True
    triggers = doc.get("on", doc.get(True))
    assert set(triggers) == {"workflow_dispatch"}, triggers
    assert doc["permissions"] == {}


def test_the_probe_condition_admits_exactly_an_owner_dispatch_by_the_owner() -> None:
    """Evaluated, not grepped: `&&` -> `||` or `==` -> `!=` turns a row below red."""
    assert _runs() is True
    assert _runs(triggering_actor=OWNER.lower()) is True  # `==` on strings ignores case
    # every other event, even from the owner
    for event in ("push", "pull_request", "pull_request_target", "schedule", "workflow_run", "issue_comment"):
        assert _runs(event=event) is False, event
    # a dispatch by anyone else, on either identity
    assert _runs(actor_id="9999") is False
    assert _runs(triggering_actor="somebody-else") is False
    assert _runs(actor_id="9999", triggering_actor="somebody-else") is False
    # the owner as the original actor but someone else re-running it
    assert _runs(actor_id=OWNER_ID, triggering_actor="a-collaborator") is False


def test_the_probe_runs_on_the_qwen_runner_label_set_and_runs_no_repo_or_third_party_code() -> None:
    assert _job()["runs-on"] == ["self-hosted", "Linux", "X64", "qwen-linux"]
    assert not any("uses" in s for s in _steps()), "no checkout and no action: nothing from the repo executes on the runner"
    assert "secrets." not in PROBE.read_text()


def test_each_step_after_the_first_runs_even_when_an_earlier_one_failed() -> None:
    steps = _steps()
    assert all(s.get("if") == "always()" for s in steps[1:])
    assert all(s.get("shell") == "bash" for s in steps)


def test_only_the_four_must_never_be_true_checks_can_fail_the_probe() -> None:
    """Identity (nexus or root), passwordless sudo, a readable credential file, the Windows side; docker only reports."""
    can_fail = {s["name"].split(" (")[0] for s in _steps() if re.search(r"\bexit 1\b|\brc=1\b|exit \"\$rc\"", s["run"])}
    assert can_fail == {"Identity", "Passwordless sudo is refused", "Other users' homes by file mode",
                        "Windows side"}, can_fail
    run = _step("Docker is root")["run"]
    assert "exit 1" not in run and "rc=1" not in run and "::error::" not in run


def test_every_expected_refusal_is_captured_so_bash_e_does_not_end_the_step() -> None:
    """The hellmini probe's da3e5f962 lesson: a refused command is the EXPECTED result under bash -e."""
    sudo = _step("Passwordless sudo")["run"]
    assert "status=0" in sudo and "|| status=$?" in sudo
    for prefix in ("Other users' homes", "Docker is root", "Windows side"):
        assert "|| status=$?" in _step(prefix)["run"], prefix


def test_the_probe_reads_the_surfaces_the_review_named_and_never_a_credential_content() -> None:
    text = PROBE.read_text()
    for needle in ("sudo -n true", "/home/nexus", "/home/nxtest", "docker run --rm -v /:/h", "ls -A /h/home/nexus",
                   "/mnt/c", "cmd.exe", "WSLInterop", "/etc/wsl.conf", "/home/nexus/.config/nexus"):
        assert needle in text, needle
    scripts = "\n".join(s["run"] for s in _steps())
    assert not re.search(r"\b(cat|head|tail|less|more|strings|base64)\b[^\n]*\.config/nexus", scripts)
    assert "printenv" not in scripts and not re.search(r"(^|\s)env(\s|$)", scripts)


@pytest.mark.skipif(getpass.getuser() in {"root", "nexus"}, reason="the identity step refuses exactly these users")
def test_the_identity_step_reports_and_passes_for_an_ordinary_user() -> None:
    proc = _run(_step("Identity"))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "docker group:" in proc.stdout
    assert "groups=" not in proc.stdout and getpass.getuser() not in proc.stdout.split()


def test_the_docker_step_reports_and_never_fails_when_there_is_no_docker() -> None:
    proc = _run(_step("Docker is root"))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "REPORT: no docker on PATH" in proc.stdout


FIX = "/etc/wsl.conf"


def _windows_step(mnt: Path, binfmt: Path) -> dict:
    """The real Windows-side step with its two roots pointed at a fake tree."""
    run = _step("Windows side")["run"]
    assert "mnt_root=/mnt\n" in run and "binfmt_dir=/proc/sys/fs/binfmt_misc\n" in run
    run = run.replace("mnt_root=/mnt\n", f"mnt_root={mnt}\n").replace(
        "binfmt_dir=/proc/sys/fs/binfmt_misc\n", f"binfmt_dir={binfmt}\n")
    return {"run": run}


def _windows_run(tmp_path: Path, *, path: str = "/usr/bin:/bin", stub_timeout: str | None = None
                 ) -> subprocess.CompletedProcess[str]:
    mnt, binfmt = tmp_path / "mnt", tmp_path / "binfmt"
    mnt.mkdir(exist_ok=True)
    binfmt.mkdir(exist_ok=True)
    if stub_timeout is not None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        t = bin_dir / "timeout"
        t.write_text(stub_timeout)
        t.chmod(0o755)
        path = f"{bin_dir}:{path}"
    return _run(_windows_step(mnt, binfmt), env={"PATH": path})


def _fake_cmd_exe(tmp_path: Path, body: str, mode: int = 0o755) -> Path:
    cmd = tmp_path / "mnt" / "c" / "Windows" / "System32" / "cmd.exe"
    cmd.parent.mkdir(parents=True, exist_ok=True)
    cmd.write_text(body)
    cmd.chmod(mode)
    return cmd


def test_the_windows_side_step_passes_on_a_host_with_no_windows_reach(tmp_path: Path) -> None:
    proc = _windows_run(tmp_path)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "no PATH entry points under /mnt/<drive> (expected)" in proc.stdout
    assert "no WSLInterop binfmt handler is registered" in proc.stdout
    assert "::error::" not in proc.stdout


def test_the_windows_side_step_passes_when_interop_and_automount_are_off(tmp_path: Path) -> None:
    """The state the host fix produces: a disabled handler and an empty /mnt/c (automount off, or a bare mount point)."""
    (tmp_path / "mnt" / "c").mkdir(parents=True)
    (tmp_path / "binfmt").mkdir()
    (tmp_path / "binfmt" / "WSLInterop").write_text("disabled\n")
    ok = _windows_run(tmp_path)
    assert ok.returncode == 0, (ok.stdout, ok.stderr)
    assert "exists and is empty" in ok.stdout and "::error::" not in ok.stdout
    assert "binfmt handler is registered but not enabled (disabled)" in ok.stdout


def test_a_cmd_exe_that_cannot_execute_is_reported_as_interop_off_not_as_a_failure_of_the_exec_check(tmp_path: Path) -> None:
    """An unexecutable file is exit 126: the exec attempt passes. The mounted drive still fails (b)."""
    # a directory cannot be executed on any filesystem (a mode bit would mean nothing on drvfs-like mounts)
    (tmp_path / "mnt" / "c" / "Windows" / "System32" / "cmd.exe").mkdir(parents=True)
    proc = _windows_run(tmp_path)
    assert "could not be executed (exit 126): interop is off for it" in proc.stdout
    assert "cmd.exe ran" not in proc.stdout
    assert proc.returncode == 1 and "is mounted and listable" in proc.stdout


def test_an_executable_cmd_exe_fails_the_windows_side_step_and_names_the_fix(tmp_path: Path) -> None:
    _fake_cmd_exe(tmp_path, "#!/bin/sh\nexit 0\n")
    proc = _windows_run(tmp_path)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "a Windows cmd.exe ran (exit 0)" in proc.stdout
    assert "System32" not in proc.stdout
    assert "[interop] enabled=false" in proc.stdout and FIX in proc.stdout


def test_a_cmd_exe_that_times_out_still_counts_as_running(tmp_path: Path) -> None:
    """Exit 124 is a started program that did not finish, not a refusal to execute."""
    _fake_cmd_exe(tmp_path, "#!/bin/sh\nsleep 60\n")
    proc = _windows_run(tmp_path, stub_timeout="#!/bin/sh\nexit 124\n")
    assert proc.returncode == 1 and "ran (exit 124)" in proc.stdout, (proc.stdout, proc.stderr)


def test_an_enabled_wslinterop_binfmt_entry_fails_the_windows_side_step(tmp_path: Path) -> None:
    (tmp_path / "binfmt").mkdir()
    (tmp_path / "binfmt" / "WSLInterop-late").write_text("enabled\ninterpreter /init\nflags: PF\n")
    proc = _windows_run(tmp_path)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "WSLInterop-late binfmt handler is enabled" in proc.stdout and FIX in proc.stdout


def test_a_readable_mnt_drive_fails_the_windows_side_step_without_printing_a_name(tmp_path: Path) -> None:
    drive = tmp_path / "mnt" / "d"
    (drive / "Users" / "a-person-name").mkdir(parents=True)
    (drive / "id_ed25519").write_text("SECRET-VALUE\n")
    proc = _windows_run(tmp_path)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "a /mnt drive is mounted and listable" in proc.stdout and "(2 entries)" in proc.stdout
    assert str(drive) not in proc.stdout
    assert "[automount] enabled=false" in proc.stdout
    for leaked in ("a-person-name", "id_ed25519", "SECRET-VALUE"):
        assert leaked not in proc.stdout + proc.stderr


def test_a_non_letter_mnt_directory_is_not_a_drive(tmp_path: Path) -> None:
    other = tmp_path / "mnt" / "data"
    other.mkdir(parents=True)
    (other / "file").write_text("x")
    proc = _windows_run(tmp_path)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


def test_a_windows_path_entry_fails_the_windows_side_step_without_printing_it(tmp_path: Path) -> None:
    proc = _windows_run(tmp_path, path="/usr/bin:/bin:/mnt/c/Users/a-person-name/AppData:/mnt/d")
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "2 PATH entries point under /mnt/<drive>" in proc.stdout and "appendWindowsPath=false" in proc.stdout
    assert "a-person-name" not in proc.stdout + proc.stderr


def test_a_path_entry_that_only_resembles_a_drive_is_not_flagged(tmp_path: Path) -> None:
    proc = _windows_run(tmp_path, path="/usr/bin:/bin:/mnt/cdrom:/opt/mnt/c")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


@pytest.mark.skipif(sys.platform != "linux", reason="uses GNU find -readable")
def test_a_readable_credential_shaped_file_fails_the_homes_step_and_an_ordinary_one_does_not(tmp_path: Path) -> None:
    step = _step("Other users' homes")
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "settings.toml").write_text("x = 1\n")
    probe = {"run": step["run"].replace("/home/nexus/.config/nexus", str(cfg))}
    ok = _run(probe)
    assert ok.returncode == 0, (ok.stdout, ok.stderr)
    assert "expected" in ok.stdout
    (cfg / "api_token.json").write_text("{}\n")
    bad = _run(probe)
    assert bad.returncode == 1, (bad.stdout, bad.stderr)
    assert "1 credential-shaped file(s)" in bad.stdout
    # a count only: neither the file's name nor its content reaches the log
    (cfg / "api_token.json").write_text("SECRET-VALUE\n")
    out = _run(probe).stdout
    assert "api_token" not in out and "SECRET-VALUE" not in out


def test_the_homes_step_with_no_nexus_config_reports_and_passes() -> None:
    """On a host with no /home/nexus (this one, or the hosted runner) there is nothing to read."""
    if Path("/home/nexus/.config/nexus").is_dir():
        pytest.skip("this host has a live /home/nexus/.config/nexus")
    proc = _run(_step("Other users' homes"))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "REPORT" in proc.stdout


def test_the_runner_label_is_registered_with_actionlint() -> None:
    cfg = yaml.safe_load((Path(__file__).parent.parent / ".github" / "actionlint.yaml").read_text())
    assert "qwen-linux" in cfg["self-hosted-runner"]["labels"]


def test_the_public_log_carries_no_modes_owners_or_user_names_from_any_step() -> None:
    """Pass or fail and counts only: no `ls -l`, no `stat`, no `id -un` or group list printed."""
    scripts = "\n".join(s["run"] for s in _steps())
    assert not re.search(r"\bls\s+-\w*l", scripts), "ls -l prints modes and owners"
    assert "stat " not in scripts and "id -Gn" not in scripts and "id -un" not in scripts.replace('user="$(id -un)"', "")
    assert 'echo "user=' not in scripts and 'echo "groups=' not in scripts
