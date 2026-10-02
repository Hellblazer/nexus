# SPDX-License-Identifier: AGPL-3.0-or-later
"""The qwen-linux isolation probe is informational: it reports, and fails only on what must never be true.

The probe runs on the self-hosted runner in a public repo, so its trigger and
its shape are pinned here; the steps that can run anywhere are executed.
"""
from __future__ import annotations

import os
import re
import shutil
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


BASH = shutil.which("bash") or "/bin/bash"


def _run(step: dict, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([BASH, "-eo", "pipefail", "-c", step["run"]], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", **(env or {})})


OWNER = "Hellblazer"
OWNER_ID = "1234"

# A name no fixed wording in a step can contain. The steps say "the runner user", so asserting on the HOST's real
# account (GitHub's hosted runner is literally named `runner`) false-matches; the account is injected instead.
ACCT = "zz-probe-acct-7"


def _fake_id(tmp_path: Path, name: str = ACCT, groups: str = f"{ACCT} docker") -> str:
    """A PATH whose `id` reports an injected account and group list, ahead of the real tools (never the host's identity)."""
    bin_dir = tmp_path / "idbin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "id"
    stub.write_text(f'#!/bin/sh\ncase "$*" in\n  -un) echo {name} ;;\n  -nG) echo {groups} ;;\n  *) exit 2 ;;\nesac\n')
    stub.chmod(0o755)
    return f"{bin_dir}:/usr/bin:/bin"


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


def test_the_probe_triggers_on_dispatch_and_on_a_push_to_runner_probe_qwen_branches_only() -> None:
    """A dispatch works only from the default branch (main); the push trigger is how the probe runs from develop-era code."""
    doc = _doc()
    # PyYAML reads the bare key `on` as the boolean True
    triggers = doc.get("on", doc.get(True))
    assert set(triggers) == {"workflow_dispatch", "push"}, triggers
    assert triggers["push"] == {"branches": ["runner-probe/qwen-*"]}, "no other branch, and no tags, may start the probe"
    assert "tags" not in triggers["push"] and "paths" not in triggers["push"]
    assert doc["permissions"] == {}


def test_the_documented_probe_branch_name_matches_the_trigger_filter() -> None:
    """The header's `git push` recipe must name a branch the filter actually admits."""
    import fnmatch

    text = PROBE.read_text()
    m = re.search(r"HEAD:refs/heads/(runner-probe/qwen-<date>)", text)
    assert m, "the header must give the throwaway-branch recipe"
    pattern = _doc().get("on", _doc().get(True))["push"]["branches"][0]
    assert fnmatch.fnmatchcase(m.group(1).replace("<date>", "2026-10-02"), pattern)
    assert not fnmatch.fnmatchcase("develop", pattern) and not fnmatch.fnmatchcase("runner-probe/hellmini-x", pattern)
    assert ":refs/heads/runner-probe/qwen-<date>" in text, "and the recipe to delete the branch afterwards"


def test_the_probe_condition_admits_exactly_an_owner_dispatch_or_an_owner_push() -> None:
    """Evaluated, not grepped: `&&` -> `||` or `==` -> `!=` turns a row below red."""
    assert _runs() is True
    assert _runs(event="push") is True, "the owner's push to a runner-probe/qwen-* branch"
    assert _runs(triggering_actor=OWNER.lower()) is True  # `==` on strings ignores case
    assert _runs(event="push", triggering_actor=OWNER.lower()) is True
    # every other event, even from the owner
    for event in ("pull_request", "pull_request_target", "schedule", "workflow_run", "issue_comment", "create"):
        assert _runs(event=event) is False, event
    # a push by anyone else, on either identity
    assert _runs(event="push", actor_id="9999") is False
    assert _runs(event="push", triggering_actor="somebody-else") is False
    assert _runs(event="push", actor_id="9999", triggering_actor="somebody-else") is False
    # a dispatch by anyone else, on either identity
    assert _runs(actor_id="9999") is False
    assert _runs(triggering_actor="somebody-else") is False
    assert _runs(actor_id="9999", triggering_actor="somebody-else") is False
    # the owner as the original actor but someone else re-running it
    assert _runs(actor_id=OWNER_ID, triggering_actor="a-collaborator") is False


def test_the_probe_runs_on_the_qwen_custom_label_only_and_runs_no_repo_or_third_party_code() -> None:
    assert _job()["runs-on"] == "qwen-linux"
    assert not any("uses" in s for s in _steps()), "no checkout and no action: nothing from the repo executes on the runner"
    assert "secrets." not in PROBE.read_text()


def test_each_step_after_the_first_runs_even_when_an_earlier_one_failed() -> None:
    steps = _steps()
    assert all(s.get("if") == "always()" for s in steps[1:])
    assert all(s.get("shell") == "bash" for s in steps)


def test_only_the_four_must_never_be_true_checks_can_fail_the_probe() -> None:
    """Identity (not ghci), passwordless sudo, a readable file under the nexus config dir, the Windows side; docker only reports."""
    can_fail = {s["name"].split(" (")[0] for s in _steps() if re.search(r"\bexit 1\b|\brc=1\b|exit \"\$rc\"", s["run"])}
    assert can_fail == {"Identity", "Passwordless sudo is refused", "Other users' homes by file mode",
                        "Windows side"}, can_fail
    run = _step("Docker reachability")["run"]
    assert "exit 1" not in run and "rc=1" not in run and "::error::" not in run


def test_every_expected_refusal_is_captured_so_bash_e_does_not_end_the_step() -> None:
    """The hellmini probe's da3e5f962 lesson: a refused command is the EXPECTED result under bash -e."""
    sudo = _step("Passwordless sudo")["run"]
    assert "status=0" in sudo and "|| status=$?" in sudo
    for prefix in ("Other users' homes", "Docker reachability", "Windows side"):
        assert "|| status=$?" in _step(prefix)["run"], prefix


def test_the_probe_reads_the_surfaces_the_review_named_and_never_a_credential_content() -> None:
    text = PROBE.read_text()
    for needle in ("sudo -n true", "/home/nexus", "/home/nxtest", "docker info", "/mnt/c", "cmd.exe", "WSLInterop",
                   "/etc/wsl.conf", "/home/nexus/.config/nexus"):
        assert needle in text, needle
    scripts = "\n".join(s["run"] for s in _steps())
    assert not re.search(r"\b(cat|head|tail|less|more|strings|base64)\b[^\n]*\.config/nexus", scripts)
    assert "printenv" not in scripts and not re.search(r"(^|\s)env(\s|$)", scripts)


def test_the_probe_runs_no_container_and_pulls_no_image() -> None:
    """Item 9: the docker step that pulled an unpinned alpine image was DROPPED, not pinned by digest.

    An image is third-party code on a self-hosted runner, and the docker route is a known gap that running more
    code does not measure.
    """
    scripts = "\n".join(s["run"] for s in _steps())
    for banned in ("docker run", "docker pull", "docker create", "docker exec", "docker build", "alpine", "image:"):
        assert banned not in scripts, banned
    assert "docker info" in scripts, "reachability is still reported"


def test_the_identity_step_asserts_exactly_the_runner_user_ghci() -> None:
    run = _step("Identity")["run"]
    assert "expected=ghci\n" in run
    assert re.search(r'\[ "\$user" != "\$expected" \]', run), "a different user, nxtest and root included, must fail"


def test_the_identity_step_fails_for_any_account_that_is_not_ghci(tmp_path: Path) -> None:
    proc = _run(_step("Identity"), env={"PATH": _fake_id(tmp_path)})
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "does not run as the runner user" in proc.stdout and ACCT not in proc.stdout + proc.stderr


@pytest.mark.parametrize(("groups", "needle"), [(f"{ACCT} docker", "docker group: member"),
                                                 (f"{ACCT} users", "docker group: not a member")])
def test_the_identity_step_passes_when_the_account_is_the_expected_one_and_reports_docker_membership(
        tmp_path: Path, groups: str, needle: str) -> None:
    """The positive half: repoint `expected` at the injected account, as the host's runner user would match it."""
    run = _step("Identity")["run"].replace("expected=ghci\n", f"expected={ACCT}\n")
    proc = _run({"run": run}, env={"PATH": _fake_id(tmp_path, groups=groups)})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert needle in proc.stdout and "runs as the runner user (expected)" in proc.stdout
    assert "groups=" not in proc.stdout and ACCT not in proc.stdout + proc.stderr


def _fake_docker(tmp_path: Path, exit_code: int) -> str:
    """A PATH holding only a stub `docker` (the step needs nothing else: `command -v` and echo are builtins)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    d = bin_dir / "docker"
    d.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    d.chmod(0o755)
    return str(bin_dir)


def test_the_docker_step_reports_and_never_fails_when_there_is_no_docker(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    proc = _run(_step("Docker reachability"), env={"PATH": str(empty)})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "REPORT: no docker on PATH" in proc.stdout


@pytest.mark.parametrize(("code", "needle"), [(0, "docker = root on this distro"), (1, "no container-root route from this user")])
def test_the_docker_step_reports_reachability_either_way_and_says_it_bounds_nothing(
        tmp_path: Path, code: int, needle: str) -> None:
    proc = _run(_step("Docker reachability"), env={"PATH": _fake_docker(tmp_path, code)})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert needle in proc.stdout
    if code == 0:
        assert "nothing here bounds it" in proc.stdout and "/etc/wsl.conf" in proc.stdout


FIX = "/etc/wsl.conf"


_WSL_VERSION = "Linux version 6.6.87.2-microsoft-standard-WSL2 (root@build) (gcc 11.2.0)\n"


def _windows_step(mnt: Path, binfmt: Path, proc_version: Path) -> dict:
    """The real Windows-side step with its three roots pointed at a fake tree."""
    run = _step("Windows side")["run"]
    assert "mnt_root=/mnt\n" in run and "binfmt_dir=/proc/sys/fs/binfmt_misc\n" in run
    assert "proc_version=/proc/version\n" in run
    run = (run.replace("mnt_root=/mnt\n", f"mnt_root={mnt}\n")
           .replace("binfmt_dir=/proc/sys/fs/binfmt_misc\n", f"binfmt_dir={binfmt}\n")
           .replace("proc_version=/proc/version\n", f"proc_version={proc_version}\n"))
    return {"run": run}


def _windows_run(tmp_path: Path, *, path: str = "/usr/bin:/bin", stub_timeout: str | None = None,
                 version: str | None = _WSL_VERSION, binfmt_status: bool = True) -> subprocess.CompletedProcess[str]:
    """Run the step on a fake WSL host (positive control satisfied) unless a test breaks that on purpose."""
    mnt, binfmt, proc_version = tmp_path / "mnt", tmp_path / "binfmt", tmp_path / "proc_version"
    mnt.mkdir(exist_ok=True)
    binfmt.mkdir(exist_ok=True)
    if version is not None:
        proc_version.write_text(version)
    if binfmt_status:
        (binfmt / "status").write_text("enabled\n")
    if stub_timeout is not None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        t = bin_dir / "timeout"
        t.write_text(stub_timeout)
        t.chmod(0o755)
        path = f"{bin_dir}:{path}"
    return _run(_windows_step(mnt, binfmt, proc_version), env={"PATH": path})


def _fake_cmd_exe(tmp_path: Path, body: str, mode: int = 0o755) -> Path:
    cmd = tmp_path / "mnt" / "c" / "Windows" / "System32" / "cmd.exe"
    cmd.parent.mkdir(parents=True, exist_ok=True)
    cmd.write_text(body)
    cmd.chmod(mode)
    return cmd


@pytest.mark.parametrize("version", ["Linux version 6.8.0-generic (buildd@lcy02) (gcc 13.2.0)\n", "", "Darwin\n"])
def test_the_windows_side_step_fails_closed_off_wsl_instead_of_passing_vacuously(tmp_path: Path, version: str) -> None:
    """POSITIVE CONTROL: every check below is true on a machine that is not WSL, so being off WSL must fail."""
    proc = _windows_run(tmp_path, version=version)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "not a WSL kernel" in proc.stdout and "positive control: WSL kernel" not in proc.stdout


def test_the_windows_side_step_fails_closed_when_binfmt_misc_is_not_mounted(tmp_path: Path) -> None:
    proc = _windows_run(tmp_path, binfmt_status=False)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "binfmt_misc is not mounted" in proc.stdout


def test_the_windows_side_step_passes_on_a_host_with_no_windows_reach(tmp_path: Path) -> None:
    proc = _windows_run(tmp_path)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "no PATH entry points under /mnt/<drive> (expected)" in proc.stdout
    assert "no WSLInterop binfmt handler is registered" in proc.stdout
    assert "positive control: WSL kernel and binfmt_misc present" in proc.stdout
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


def _homes_probe(cfg: Path) -> dict:
    """The real homes step with its paths pointed at a fixture tree (cfg = <tmp>/home/.config/nexus), never the host's /home."""
    run = _step("Other users' homes")["run"]
    assert "for home in /home/nexus /home/nxtest; do" in run
    run = run.replace("for home in /home/nexus /home/nxtest; do",
                      f"for home in {cfg.parents[1]}/h-nexus {cfg.parents[1]}/h-nxtest; do")
    return {"run": run.replace("/home/nexus/.config/nexus", str(cfg))}


@pytest.mark.skipif(sys.platform != "linux", reason="uses GNU find -readable")
@pytest.mark.parametrize("name", ["config.yml", "settings.toml", "api_token.json", "plain", ".hidden"])
def test_ANY_readable_file_under_the_nexus_config_dir_fails_the_homes_step_with_a_count_only(
        tmp_path: Path, name: str) -> None:
    """Not a filename heuristic: config.yml carries the credentials block and matches no *cred*/*token* pattern."""
    cfg = tmp_path / "home" / ".config" / "nexus"
    cfg.mkdir(parents=True)
    assert _run(_homes_probe(cfg)).returncode == 0, "an empty directory is the control: nothing readable, nothing to fail"
    (cfg / name).write_text("SECRET-VALUE\n")
    bad = _run(_homes_probe(cfg))
    assert bad.returncode == 1, (bad.stdout, bad.stderr)
    assert "1 file(s) under" in bad.stdout
    # a count only: neither the file's name nor its content reaches the log
    out = bad.stdout + bad.stderr
    assert name not in out
    assert "SECRET-VALUE" not in out


@pytest.mark.skipif(sys.platform != "linux", reason="uses GNU find -readable")
def test_a_nested_readable_file_is_counted_too_and_the_count_is_exact(tmp_path: Path) -> None:
    cfg = tmp_path / "home" / ".config" / "nexus"
    (cfg / "deep" / "er").mkdir(parents=True)
    (cfg / "a").write_text("x")
    (cfg / "deep" / "er" / "b").write_text("x")
    bad = _run(_homes_probe(cfg))
    assert bad.returncode == 1 and "2 file(s) under" in bad.stdout, (bad.stdout, bad.stderr)


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() == 0, reason="root reads every file, so an unreadable one cannot be built")
def test_a_file_the_runner_user_cannot_read_does_not_fail_the_homes_step(tmp_path: Path) -> None:
    cfg = tmp_path / "home" / ".config" / "nexus"
    cfg.mkdir(parents=True)
    f = cfg / "config.yml"
    f.write_text("x")
    f.chmod(0o000)
    try:
        ok = _run(_homes_probe(cfg))
    finally:
        f.chmod(0o600)
    assert ok.returncode == 0, (ok.stdout, ok.stderr)
    assert "holds no file readable by the runner user (expected)" in ok.stdout


def test_a_missing_config_dir_is_reported_as_not_checked_never_as_a_pass(tmp_path: Path) -> None:
    """An absent or unreachable directory cannot fail (a closed /home/nexus looks the same), but must not read as a pass."""
    (tmp_path / "home").mkdir()
    closed = _run(_homes_probe(tmp_path / "home" / ".config" / "nexus"))
    assert closed.returncode == 0, (closed.stdout, closed.stderr)
    assert "NOT CHECKED" in closed.stdout
    assert "holds no file" not in closed.stdout
    nohome = _run(_homes_probe(tmp_path / "nohome" / ".config" / "nexus"))
    assert nohome.returncode == 0 and "NOT CHECKED: there is no /home/nexus on this host" in nohome.stdout
    # Round-4 review M2: a REPORT line in the log is not enough; the run summary must carry a WARNING annotation,
    # or a probe that never read the credential directory shows as plain green.
    for out in (closed.stdout, nohome.stdout):
        assert re.search(r"^::warning::NOT CHECKED: .*not a pass", out, re.M), out


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches every directory, so a closed home cannot be built")
@pytest.mark.parametrize("mode", [0o000, 0o600, 0o644])
def test_a_home_the_runner_user_cannot_traverse_is_the_stronger_state_and_passes_without_a_warning(
        tmp_path: Path, mode: int) -> None:
    """nexus-4r5lv: /home/nexus at 0700 from ghci's side. The config directory cannot be reached through it, so the
    credential line is a PASS (no readable file is possible), not a NOT CHECKED, and no warning annotation is raised."""
    home = tmp_path / "home"
    cfg = home / ".config" / "nexus"
    cfg.mkdir(parents=True)
    (cfg / "config.yml").write_text("SECRET-VALUE\n")
    (cfg / "config.yml").chmod(0o644)
    home.chmod(mode)  # no search (x) bit for the test user, who owns it: what a peer sees at 0700
    try:
        proc = _run(_homes_probe(cfg))
    finally:
        home.chmod(0o755)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "closed to the runner user" in proc.stdout and "stronger state" in proc.stdout, proc.stdout
    assert "NOT CHECKED" not in proc.stdout and "::warning::" not in proc.stdout, proc.stdout
    assert "holds no file readable" not in proc.stdout, "the closed home is not the CHECKED-clean line either"
    out = proc.stdout + proc.stderr
    assert "config.yml" not in out and "SECRET-VALUE" not in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches every directory, so a closed home cannot be built")
def test_a_closed_home_pass_does_not_mask_a_traversable_home_whose_config_directory_is_absent(tmp_path: Path) -> None:
    """The discriminator is the HOME's traversability: a traversable home with no config directory is still NOT CHECKED
    with a warning (a moved install looks the same), and so is a missing home. Closed and open are different runs."""
    home = tmp_path / "home"
    home.mkdir()
    cfg = home / ".config" / "nexus"
    open_home = _run(_homes_probe(cfg))
    assert open_home.returncode == 0, (open_home.stdout, open_home.stderr)
    assert "NOT CHECKED" in open_home.stdout and "closed to the runner user" not in open_home.stdout
    assert re.search(r"^::warning::NOT CHECKED: .*not a pass", open_home.stdout, re.M), open_home.stdout
    home.chmod(0o000)
    try:
        closed = _run(_homes_probe(cfg))
    finally:
        home.chmod(0o755)
    assert "closed to the runner user" in closed.stdout and "::warning::" not in closed.stdout, closed.stdout
    # a home that does not exist at all is not "closed": nothing was established, so it still warns
    gone = _run(_homes_probe(tmp_path / "nohome" / ".config" / "nexus"))
    assert "closed to the runner user" not in gone.stdout
    assert re.search(r"^::warning::NOT CHECKED: .*not a pass", gone.stdout, re.M), gone.stdout


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() == 0,
                    reason="root lists every directory, so a searchable-but-unlistable one cannot be built")
def test_a_config_file_readable_by_name_in_an_unlistable_directory_still_fails_the_homes_step(tmp_path: Path) -> None:
    """Round-4 review L1: `find -readable` needs read on the DIRECTORY, so mode 0711 + a 0644 config file counted 0 and passed."""
    cfg = tmp_path / "home" / ".config" / "nexus"
    cfg.mkdir(parents=True)
    (cfg / "config.yml").write_text("SECRET-VALUE\n")
    (cfg / "config.yml").chmod(0o644)
    cfg.chmod(0o311)  # the test user owns the directory: 0711 would still let it list, so drop the owner's read bit (0711 is what a PEER sees)
    try:
        bad = _run(_homes_probe(cfg))
    finally:
        cfg.chmod(0o755)
    assert bad.returncode == 1, (bad.stdout, bad.stderr)
    assert "1 file(s) under" in bad.stdout
    out = bad.stdout + bad.stderr
    assert "config.yml" not in out and "SECRET-VALUE" not in out


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() == 0,
                    reason="root lists every directory, so a searchable-but-unlistable one cannot be built")
def test_an_unlistable_config_directory_with_no_readable_file_is_not_checked_and_does_not_fail(tmp_path: Path) -> None:
    """The control for the test above: the by-name check must not turn every 0711 directory into a failure.

    nexus-4r5lv follow-up: it is no longer a PASS line either. find could not list the directory, so a readable file
    in it would be invisible; the step says NOT CHECKED (a warning annotation, rc 0) instead of "holds no file readable".
    """
    cfg = tmp_path / "home" / ".config" / "nexus"
    cfg.mkdir(parents=True)
    (cfg / "config.yml").write_text("x")
    (cfg / "config.yml").chmod(0o000)  # the test user OWNS the file, so only mode 000 makes it unreadable to itself
    cfg.chmod(0o311)  # the test user owns the directory: 0711 would still let it list, so drop the owner's read bit (0711 is what a PEER sees)
    try:
        ok = _run(_homes_probe(cfg))
    finally:
        cfg.chmod(0o755)
        (cfg / "config.yml").chmod(0o600)
    assert ok.returncode == 0, (ok.stdout, ok.stderr)
    assert "NOT CHECKED" in ok.stdout
    assert "holds no file readable" not in ok.stdout
    assert re.search(r"^::warning::NOT CHECKED: .*not a pass", ok.stdout, re.M), ok.stdout


def _fake_find(tmp_path: Path, *, exit_code: int, prints: str = "") -> str:
    """A PATH whose `find` exits `exit_code` after printing `prints`, ahead of the real tools (a find that failed)."""
    bin_dir = tmp_path / "findbin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "find"
    body = f"echo {prints}" if prints else ":"
    stub.write_text(f"#!/bin/sh\n{body}\nexit {exit_code}\n")
    stub.chmod(0o755)
    return f"{bin_dir}:/usr/bin:/bin"


def test_a_find_that_fails_with_nothing_countable_is_not_checked_never_the_pass_line(tmp_path: Path) -> None:
    """nexus-4r5lv follow-up (both reviewers' S1): `|| true` turned ANY find failure into a count of 0 and the pass line.

    A find that errors (EIO, no -readable, not on PATH) examined nothing, so it must read NOT CHECKED, as an absent
    directory does. Host-independent: the stub find fails, so this runs on macOS too.
    """
    cfg = tmp_path / "home" / ".config" / "nexus"
    cfg.mkdir(parents=True)
    proc = _run(_homes_probe(cfg), env={"PATH": _fake_find(tmp_path, exit_code=2)})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "NOT CHECKED" in proc.stdout
    assert "holds no file readable" not in proc.stdout
    assert re.search(r"^::warning::NOT CHECKED: .*not a pass", proc.stdout, re.M), proc.stdout


def test_a_find_that_fails_after_listing_a_readable_file_still_fails_the_homes_step(tmp_path: Path) -> None:
    """A partial listing that already found a readable file is a failure, not a NOT CHECKED."""
    cfg = tmp_path / "home" / ".config" / "nexus"
    cfg.mkdir(parents=True)
    proc = _run(_homes_probe(cfg), env={"PATH": _fake_find(tmp_path, exit_code=1, prints="/x/one")})
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "1 file(s) under" in proc.stdout
    assert "/x/one" not in proc.stdout + proc.stderr


def test_a_find_that_fails_beside_a_world_readable_config_yml_still_fails_by_name(tmp_path: Path) -> None:
    cfg = tmp_path / "home" / ".config" / "nexus"
    cfg.mkdir(parents=True)
    (cfg / "config.yml").write_text("SECRET-VALUE\n")
    proc = _run(_homes_probe(cfg), env={"PATH": _fake_find(tmp_path, exit_code=2)})
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "1 file(s) under" in proc.stdout
    assert "SECRET-VALUE" not in proc.stdout + proc.stderr


def test_a_find_that_succeeds_with_nothing_readable_still_passes_with_the_pass_line(tmp_path: Path) -> None:
    """The control: a find that ran and counted 0 is the expected good state, and says so."""
    cfg = tmp_path / "home" / ".config" / "nexus"
    cfg.mkdir(parents=True)
    proc = _run(_homes_probe(cfg), env={"PATH": _fake_find(tmp_path, exit_code=0)})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "holds no file readable by the runner user (expected)" in proc.stdout
    assert "NOT CHECKED" not in proc.stdout


def _branch_globs_match(patterns: list[str], branch: str) -> bool:
    """GitHub's `on.push.branches` semantics: patterns in order, a later `!` pattern removes an earlier match.

    `*` matches within one path segment, `**` across segments.
    """
    def rx(pat: str) -> re.Pattern[str]:
        out, i = "", 0
        while i < len(pat):
            if pat.startswith("**", i):
                out, i = out + ".*", i + 2
            elif pat[i] == "*":
                out, i = out + "[^/]*", i + 1
            else:
                out, i = out + re.escape(pat[i]), i + 1
        return re.compile(out + r"\Z")

    matched = False
    for pat in patterns:
        negate = pat.startswith("!")
        if rx(pat[1:] if negate else pat).match(branch):
            matched = not negate
    return matched


def test_a_runner_probe_qwen_branch_fires_this_probe_and_no_other_workflow() -> None:
    """Round-4 review L2: hellmini-probe's `runner-probe/**` also matched `runner-probe/qwen-*`, so the documented
    throwaway-branch recipe started a 20-minute job on the hellmini RELEASE runner too. The header claims it routes
    nothing else; this is that claim, evaluated against every workflow's push filter."""
    branch = "runner-probe/qwen-2026-10-02"
    fired = []
    for wf in sorted(PROBE.parent.glob("*.y*ml")):
        doc = yaml.safe_load(wf.read_text())
        on = doc.get("on", doc.get(True))
        push = on.get("push") if isinstance(on, dict) else None
        if push is None and isinstance(on, list) and "push" in on:
            fired.append(wf.name)  # a bare `push` trigger has no branch filter
        elif isinstance(push, dict) and "branches" in push and _branch_globs_match(push["branches"], branch):
            fired.append(wf.name)
    assert fired == [PROBE.name], fired
    # non-vacuity: the evaluator does see the old over-match, and still matches the other probe branches
    assert _branch_globs_match(["runner-probe/**"], branch)
    assert not _branch_globs_match(["runner-probe/**", "!runner-probe/qwen-*"], branch)
    assert _branch_globs_match(["runner-probe/**", "!runner-probe/qwen-*"], "runner-probe/hellmini-2026-10-02")


def test_the_homes_step_with_no_nexus_config_reports_and_passes(tmp_path: Path) -> None:
    """With no nexus config directory there is nothing to read (a fixture tree, not this host's /home)."""
    proc = _run(_homes_probe(tmp_path / "nohome" / ".config" / "nexus"))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "REPORT" in proc.stdout and "NOT CHECKED" in proc.stdout


def test_the_runner_label_is_registered_with_actionlint() -> None:
    cfg = yaml.safe_load((Path(__file__).parent.parent / ".github" / "actionlint.yaml").read_text())
    assert "qwen-linux" in cfg["self-hosted-runner"]["labels"]


_IDENTITY_SOURCES = (r"\bwhoami\b", r"\blogname\b", r"\bgetent\b", r"\$\{?USER\}?", r"\$\{?LOGNAME\}?", r"\$\{?HOME\}?",
                     r"\bid\s+-\w*[unG]\w*", r"\bgroups\b", r"\bw\b\s", r"\bwho\b", r"\bhostname\b", r"\buname\b")


def test_the_public_log_carries_no_modes_owners_or_user_names_from_any_step() -> None:
    """Pass or fail and counts only: no `ls -l`, no `stat`, no way for a step to print an account or group name."""
    scripts = "\n".join(s["run"] for s in _steps())
    assert not re.search(r"\bls\s+-\w*l", scripts), "ls -l prints modes and owners"
    assert "stat " not in scripts and "id -Gn" not in scripts
    # the ONE place the account is read is the identity step's compare, and its value is never echoed
    assert scripts.count('user="$(id -un)"') == 1
    # the one other `id`: a membership TEST whose output is discarded by grep -q
    assert scripts.count("id -nG | tr ' ' '\\n' | grep -qx docker") == 1
    stripped = scripts.replace('user="$(id -un)"', "").replace("id -nG | tr ' ' '\\n' | grep -qx docker", "")
    for pattern in _IDENTITY_SOURCES:
        assert not re.search(pattern, stripped), f"a step can read an identity with {pattern!r}"
    assert 'echo "user=' not in scripts and 'echo "groups=' not in scripts
    assert "$user" not in re.sub(r'\[ "\$user" != "\$expected" \]', "", scripts), "$user is only compared, never printed"


def test_no_step_prints_the_account_name_it_runs_as(tmp_path: Path) -> None:
    """Dynamic twin of the source scan: run every step against an INJECTED account and look for its name in the output."""
    fake = _fake_id(tmp_path)
    outputs = []
    ident = _step("Identity")["run"].replace("expected=ghci\n", f"expected={ACCT}\n")
    outputs.append(_run({"run": ident}, env={"PATH": fake}))
    # the stub `id` is consulted: the identity pass run exits 0 (a stub that was ignored would fail the compare here)
    assert outputs[0].returncode == 0, (outputs[0].stdout, outputs[0].stderr)
    outputs.append(_run({"run": ident.replace(f"expected={ACCT}", "expected=somebody-else")}, env={"PATH": fake}))
    outputs.append(_run(_step("Passwordless sudo"), env={"PATH": fake}))
    cfg = tmp_path / "home" / ".config" / "nexus"
    cfg.mkdir(parents=True)
    outputs.append(_run(_homes_probe(cfg), env={"PATH": fake}))
    outputs.append(_run(_step("Docker reachability"), env={"PATH": _fake_docker(tmp_path, 0)}))
    outputs.append(_windows_run(tmp_path, path=fake))
    assert len(outputs) == 6 and all(p.stdout for p in outputs), "non-vacuity: every step produced output to inspect"
    for proc in outputs:
        assert ACCT not in proc.stdout + proc.stderr
