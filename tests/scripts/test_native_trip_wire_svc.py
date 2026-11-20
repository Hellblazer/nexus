# SPDX-License-Identifier: AGPL-3.0-or-later
"""The native-build trip-wire's job gate (scripts/native_trip_wire_svc.py; suite audit item 8, Sam 2026-11-19).

The gate decides whether ci.yml's ``engine-service-build`` job starts at all. It may answer ``false`` only when it
can prove the change range touches none of the build's inputs AND the range's base is itself covered: on a push, the
base commit's own trip-wire check run concluded success or skipped (or is still queued or running). A superseded,
cancelled or missing run leaves the base uncovered, so a ``service/`` change inside a replaced pending run is never
skipped. Every doubt answers ``true``; an empty diff between two good commits fails (exit 1).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "native_trip_wire_svc.py"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
ZERO = "0" * 40

_spec = importlib.util.spec_from_file_location("native_trip_wire_svc", SCRIPT)
gate = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["native_trip_wire_svc"] = gate
_spec.loader.exec_module(gate)  # type: ignore[union-attr]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, path: str, text: str = "x") -> str:
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text + path)
    _git(repo, "add", path)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", path)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    _commit(r, "README.md")
    return r


def _covered(*conclusions: str):
    """A check-run lookup that answers the given runs, as (status, conclusion) pairs."""
    runs = [("completed", c) if c not in ("queued", "in_progress") else (c, None) for c in conclusions]
    return lambda sha: runs


def _decide(repo: Path, **kw):
    kw.setdefault("event", "push")
    kw.setdefault("lookup", _covered("success"))
    return gate.decide(repo=repo, **kw)


def test_a_push_outside_the_inputs_with_a_covered_base_skips(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "docs/x.md")
    _commit(repo, "src/nexus/a.py")
    svc, why = _decide(repo, before=before)
    assert svc is False, why
    assert "no build input" in why


@pytest.mark.parametrize("path", ["service/src/Main.java", ".github/workflows/ci.yml",
                                  ".github/actions/maven-cache/action.yml", "scripts/native_trip_wire_svc.py",
                                  "scripts/check_native_embedded_resources.py"])
def test_a_push_touching_an_input_runs(repo: Path, path: str) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "docs/x.md")
    _commit(repo, path)
    svc, why = _decide(repo, before=before)
    assert svc is True and path in why, why


@pytest.mark.parametrize("conclusions", [(), ("cancelled",), ("cancelled", "cancelled"), ("failure",),
                                         ("timed_out",), ("neutral",), ("failure", "success"),
                                         ("skipped", "failure")])
def test_an_uncovered_base_runs_even_when_the_range_is_clean(repo: Path, conclusions: tuple[str, ...]) -> None:
    """The superseded-run hole: a replaced pending run never built its own commits, so its successor must."""
    before = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "docs/x.md")
    svc, why = _decide(repo, before=before, lookup=_covered(*conclusions))
    assert svc is True and "not covered" in why, why


@pytest.mark.parametrize("conclusions", [("success",), ("skipped",), ("cancelled", "success"), ("queued",),
                                         ("in_progress",)])
def test_a_covered_base_lets_a_clean_range_skip(repo: Path, conclusions: tuple[str, ...]) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "docs/x.md")
    svc, why = _decide(repo, before=before, lookup=_covered(*conclusions))
    assert svc is False, why


def test_a_failed_lookup_runs(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "docs/x.md")

    def broken(sha: str):
        raise OSError("api down")

    svc, why = _decide(repo, before=before, lookup=broken)
    assert svc is True and "could not read" in why, why


@pytest.mark.parametrize("before", [ZERO, "", "1234567890abcdef1234567890abcdef12345678"])
def test_an_unusable_push_base_runs(repo: Path, before: str) -> None:
    _commit(repo, "docs/x.md")
    svc, why = _decide(repo, before=before)
    assert svc is True and "base" in why, why


def test_a_pull_request_diffs_from_the_merge_base_and_needs_no_lookup(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "docs/x.md")

    def never(sha: str):
        raise AssertionError("a pull request range is the PR's own; no run covers its base")

    assert _decide(repo, event="pull_request", base=base, lookup=never)[0] is False
    _commit(repo, "service/pom.xml")
    assert _decide(repo, event="pull_request", base=base, lookup=never)[0] is True
    assert _decide(repo, event="pull_request", base=ZERO, lookup=never)[0] is True


def test_an_empty_diff_between_two_good_commits_fails(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(gate.MalformedRange):
        _decide(repo, before=head)


def test_the_cli_writes_only_true_or_false_to_the_output_file(repo: Path, tmp_path: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "service/x.java")
    out = tmp_path / "out"
    cp = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), "--event", "push", "--before", before,
                         "--output", str(out), "--no-lookup-for-test"], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    assert out.read_text() == "svc=true\n"
    cp = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), "--event", "push",
                         "--before", _git(repo, "rev-parse", "HEAD"), "--output", str(out), "--no-lookup-for-test"],
                        capture_output=True, text=True)
    assert cp.returncode == 1 and "::error::" in cp.stdout + cp.stderr


# ── ci.yml wiring: the gate runs before the job, so a skip starts no runner ──

def _ci() -> dict:
    return yaml.safe_load(CI_YML.read_text())


def test_the_trip_wire_job_is_gated_at_job_level_on_the_changes_output() -> None:
    job = _ci()["jobs"]["engine-service-build"]
    needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
    assert "changes" in needs
    # a changes job that did not succeed BUILDS (never a skip): only the script's own decision can produce
    # `skipped`, which is what lets a later push read a skipped base as covered
    assert job["if"].strip() == ("!cancelled() && (needs.changes.result != 'success' "
                                 "|| needs.changes.outputs.svc == 'true')")
    steps = job["steps"]
    assert not any("paths-filter" in str(s.get("uses", "")) for s in steps), "the dorny step-level gate is gone"
    assert not any("steps.changes.outputs.svc" in str(s.get("if", "")) for s in steps)
    # the job keeps its name: release evidence and history read it verbatim
    assert job["name"] == "engine-service native build (PR trip-wire)"


def test_the_changes_job_computes_svc_with_this_script_and_verifies_it() -> None:
    changes = _ci()["jobs"]["changes"]
    assert changes["outputs"]["svc"] == "${{ steps.svc.outputs.svc }}"
    step = next(s for s in changes["steps"] if s.get("id") == "svc")
    assert "scripts/native_trip_wire_svc.py" in step["run"]
    assert "github.event.before" in step["run"] and "github.event.pull_request.base.sha" in step["run"]
    assert step.get("env", {}).get("GH_TOKEN") == "${{ github.token }}"
    names = [s.get("name", "") for s in changes["steps"]]
    assert any(n.startswith("Verify the svc predicate") for n in names)
    # the check-run read needs checks: read, and the job keeps its checkout
    assert changes.get("permissions", {}).get("checks") == "read"
    assert changes.get("permissions", {}).get("contents") == "read"


def test_the_gate_and_the_trip_wire_name_the_same_inputs() -> None:
    """The script's input list is the trip-wire's input list; the check name it reads is the job's name."""
    assert gate.TRIP_WIRE_CHECK == _ci()["jobs"]["engine-service-build"]["name"]
    assert set(gate.INPUT_PREFIXES) == {"service/", ".github/workflows/ci.yml", ".github/actions/maven-cache/",
                                        "scripts/native_trip_wire_svc.py",
                                        "scripts/check_native_embedded_resources.py"}
    # every script the job runs is an input, so editing one runs the job
    ran = " ".join(str(st.get("run", "")) for st in _ci()["jobs"]["engine-service-build"]["steps"])
    for script in ("scripts/check_native_embedded_resources.py",):
        assert script in ran and script in gate.INPUT_PREFIXES
