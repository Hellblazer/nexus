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


def test_the_probe_is_owner_only_workflow_dispatch_and_nothing_else() -> None:
    doc = _doc()
    # PyYAML reads the bare key `on` as the boolean True
    triggers = doc.get("on", doc.get(True))
    assert set(triggers) == {"workflow_dispatch"}, triggers
    assert doc["permissions"] == {}
    cond = _job()["if"]
    for token in ("github.event_name == 'workflow_dispatch'",
                  "github.actor_id == github.repository_owner_id",
                  "github.triggering_actor == github.repository_owner"):
        assert token in cond


def test_the_probe_runs_on_the_qwen_runner_label_set_and_runs_no_repo_or_third_party_code() -> None:
    assert _job()["runs-on"] == ["self-hosted", "Linux", "X64", "qwen-linux"]
    assert not any("uses" in s for s in _steps()), "no checkout and no action: nothing from the repo executes on the runner"
    assert "secrets." not in PROBE.read_text()


def test_each_step_after_the_first_runs_even_when_an_earlier_one_failed() -> None:
    steps = _steps()
    assert all(s.get("if") == "always()" for s in steps[1:])
    assert all(s.get("shell") == "bash" for s in steps)


def test_only_the_three_must_never_be_true_checks_can_fail_the_probe() -> None:
    """Identity (nexus or root), passwordless sudo, a readable credential file; the rest only report."""
    can_fail = {s["name"].split(" (")[0] for s in _steps() if re.search(r"\bexit 1\b|\brc=1\b|exit \"\$rc\"", s["run"])}
    assert can_fail == {"Identity", "Passwordless sudo is refused", "Other users' homes by file mode"}, can_fail
    for prefix in ("Docker is root", "Windows side"):
        run = _step(prefix)["run"]
        assert "exit 1" not in run and "rc=1" not in run and "::error::" not in run, prefix


def test_every_expected_refusal_is_captured_so_bash_e_does_not_end_the_step() -> None:
    """The hellmini probe's da3e5f962 lesson: a refused command is the EXPECTED result under bash -e."""
    sudo = _step("Passwordless sudo")["run"]
    assert "status=0" in sudo and "|| status=$?" in sudo
    for prefix in ("Other users' homes", "Docker is root"):
        assert "|| status=$?" in _step(prefix)["run"], prefix


def test_the_probe_reads_the_surfaces_the_review_named_and_never_a_credential_content() -> None:
    text = PROBE.read_text()
    for needle in ("sudo -n true", "/home/nexus", "/home/nxtest", "docker run --rm -v /:/h", "ls -A /h/home/nexus",
                   "/mnt/c", "cmd.exe", "WSLInterop", "id -Gn", "/home/nexus/.config/nexus"):
        assert needle in text, needle
    scripts = "\n".join(s["run"] for s in _steps())
    assert not re.search(r"\b(cat|head|tail|less|more|strings|base64)\b[^\n]*\.config/nexus", scripts)
    assert "printenv" not in scripts and not re.search(r"(^|\s)env(\s|$)", scripts)


@pytest.mark.skipif(getpass.getuser() in {"root", "nexus"}, reason="the identity step refuses exactly these users")
def test_the_identity_step_reports_and_passes_for_an_ordinary_user() -> None:
    proc = _run(_step("Identity"))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "groups=" in proc.stdout and "docker group:" in proc.stdout


def test_the_docker_step_reports_and_never_fails_when_there_is_no_docker() -> None:
    proc = _run(_step("Docker is root"))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "REPORT: no docker on PATH" in proc.stdout


def test_the_windows_side_step_reports_and_never_fails() -> None:
    proc = _run(_step("Windows side"))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "REPORT: /mnt/c" in proc.stdout and "interop" in proc.stdout


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
    assert "api_token.json" in bad.stdout and "credential-shaped" in bad.stdout
    # names only: the file's content never reaches the log
    (cfg / "api_token.json").write_text("SECRET-VALUE\n")
    assert "SECRET-VALUE" not in _run(probe).stdout


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
