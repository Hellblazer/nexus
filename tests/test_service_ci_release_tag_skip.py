# SPDX-License-Identifier: AGPL-3.0-or-later
"""service-ci.yml's release-tag step, executed verbatim (nexus-jh1f2 cut 6).

On a pull request into main, the Java suite skips when service/ is
byte-identical to the engine tag REQUIRED_ENGINE_VERSION names and the workflow
itself did not change. Each case builds a scratch repository with a real tag and
runs the step's own script with its expressions substituted."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVICE_CI = ROOT / ".github" / "workflows" / "service-ci.yml"
_EXPR = re.compile(r"\$\{\{\s*(.+?)\s*\}\}")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _step_run() -> str:
    doc = yaml.safe_load(SERVICE_CI.read_text(encoding="utf-8"))
    (step,) = [s for s in doc["jobs"]["changes"]["steps"] if s.get("id") == "release_tag"]
    return step["run"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "service").mkdir(parents=True)
    (r / "src" / "nexus").mkdir(parents=True)
    (r / ".github" / "workflows").mkdir(parents=True)
    (r / "service" / "Main.java").write_text("class Main {}\n")
    (r / "src" / "nexus" / "engine_version.py").write_text(
        "REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 187)\n")
    (r / ".github" / "workflows" / "service-ci.yml").write_text("name: x\n")
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "engine")
    _git(r, "tag", "engine-service-v0.1.187")
    (r / "src" / "nexus" / "client.py").write_text("X = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "client work after the tag")
    return r


_GH_STUB = """#!/usr/bin/env bash
# A stand-in for `gh api .../commits/<sha>/check-runs`: one green Java run on each sha in GREEN_SHAS.
for a in "$@"; do
  case "$a" in */commits/*/check-runs) c="${a#*/commits/}"; c="${c%/check-runs}" ;; esac
done
printf '%s\\n' "$*" >> "$GH_CALLS"
case " $GREEN_SHAS " in *" $c "*) echo 1 ;; *) echo 0 ;; esac
"""


def _run(repo: Path, *, event: str = "pull_request", base_ref: str = "main", base: str | None = None,
         green: tuple[str, ...] | None = None) -> tuple[str, str]:
    """Run the step. *green* lists the shas with a green Java run (default: the tag's commit)."""
    values = {
        "github.event_name": event,
        "github.base_ref": base_ref,
        "github.event.pull_request.base.sha": base or _git(repo, "rev-list", "--max-parents=0", "HEAD"),
        "github.repository": "Hellblazer/nexus",
    }
    script = _EXPR.sub(lambda m: values[m.group(1).strip()], _step_run())
    out = repo.parent / "gh_output"
    out.write_text("")
    bindir = repo.parent / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "gh").write_text(_GH_STUB)
    (bindir / "gh").chmod(0o755)
    if green is None:
        tagged = subprocess.run(["git", "rev-parse", "-q", "--verify", "engine-service-v0.1.187^{commit}"],
                                cwd=repo, capture_output=True, text=True, check=False).stdout.strip()
        green = (tagged,) if tagged else ()
    env = {**os.environ, "GITHUB_OUTPUT": str(out), "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
           "GREEN_SHAS": " ".join(green), "GH_CALLS": str(repo.parent / "gh_calls"),
           "JAVA_CHECK": "Java tests + jOOQ codegen drift guard"}
    proc = subprocess.run(["bash", "-c", script], cwd=repo, env=env, capture_output=True, text=True, check=True)
    return dict(line.split("=", 1) for line in out.read_text().splitlines())["same"], proc.stdout


def _commit(repo: Path, path: str, text: str) -> None:
    (repo / path).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"touch {path}")


def test_a_release_pr_whose_service_tree_is_the_pinned_tags_skips(repo: Path) -> None:
    same, stdout = _run(repo)
    assert same == "true", stdout
    assert "engine-service-v0.1.187" in stdout


def test_a_service_change_after_the_tag_runs_the_suite(repo: Path) -> None:
    _commit(repo, "service/Main.java", "class Main { int x; }\n")
    assert _run(repo)[0] == "false"


def test_a_change_to_the_workflow_runs_the_suite(repo: Path) -> None:
    _commit(repo, ".github/workflows/service-ci.yml", "name: y\n")
    assert _run(repo)[0] == "false"


def test_a_pinned_tag_missing_from_the_checkout_runs_the_suite(repo: Path) -> None:
    _git(repo, "tag", "-d", "engine-service-v0.1.187")
    same, stdout = _run(repo)
    assert same == "false"
    assert "not in this checkout" in stdout


def test_a_pin_to_another_tag_compares_against_that_tag(repo: Path) -> None:
    _commit(repo, "src/nexus/engine_version.py", "REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 188)\n")
    same, stdout = _run(repo)
    assert same == "false"
    assert "engine-service-v0.1.188" in stdout


@pytest.mark.parametrize(("event", "base_ref"), [("pull_request", "develop"), ("push", "")])
def test_only_a_pull_request_into_main_can_skip(repo: Path, event: str, base_ref: str) -> None:
    assert _run(repo, event=event, base_ref=base_ref)[0] == "false"


# -- review r1: the skip needs a green Java run on the tag's service/ tree --------------------


def test_no_green_java_run_on_the_tags_tree_runs_the_suite(repo: Path) -> None:
    same, stdout = _run(repo, green=())
    assert same == "false"
    assert "no green" in stdout


def test_a_green_run_on_an_earlier_commit_with_the_same_tree_counts(tmp_path: Path) -> None:
    r = tmp_path / "repo2"
    (r / "service").mkdir(parents=True)
    (r / "src" / "nexus").mkdir(parents=True)
    (r / ".github" / "workflows").mkdir(parents=True)
    (r / "service" / "Main.java").write_text("class Main {}\n")
    (r / "src" / "nexus" / "engine_version.py").write_text("REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 187)\n")
    (r / ".github" / "workflows" / "service-ci.yml").write_text("name: x\n")
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "service change: service-ci ran here")
    tested = _git(r, "rev-parse", "HEAD")
    _commit(r, "README", "docs only\n")
    _git(r, "tag", "engine-service-v0.1.187")
    assert _run(r, green=(tested,))[0] == "true"


def test_a_green_run_on_a_different_service_tree_does_not_count(tmp_path: Path) -> None:
    r = tmp_path / "repo3"
    (r / "service").mkdir(parents=True)
    (r / "src" / "nexus").mkdir(parents=True)
    (r / ".github" / "workflows").mkdir(parents=True)
    (r / "service" / "Main.java").write_text("class Old {}\n")
    (r / "src" / "nexus" / "engine_version.py").write_text("REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 187)\n")
    (r / ".github" / "workflows" / "service-ci.yml").write_text("name: x\n")
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "old service")
    old = _git(r, "rev-parse", "HEAD")
    _commit(r, "service/Main.java", "class Main {}\n")
    _git(r, "tag", "engine-service-v0.1.187")
    assert _run(r, green=(old,))[0] == "false"
