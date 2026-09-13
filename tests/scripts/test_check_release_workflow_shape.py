# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``scripts/check_release_workflow_shape.py`` (nexus-xihsm).

Three ``engine-service-v*`` tags burned in one day (2026-09-05) on defects
invisible to every local pre-tag gate because those gates run in a different
SHAPE from the release workflow on two axes: checkout-vs-installed-wheel
(the nexus-a2qhz production-write guard's classification), and
Docker-present-vs-absent (the release's ``-Pprebuilt-jooq`` Maven profile).
This test suite drives both check functions with subprocess stubs (a real
run needs Docker absence / a live checkout / an engine substrate, none of
which belong in the fast unit loop), plus one test that parses the REAL
workflow file to pin the extraction against drift.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import check_release_workflow_shape as shape

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── extract_release_native_build_argv ────────────────────────────────────────

_SAMPLE_WORKFLOW = """
jobs:
  build-publish:
    steps:
      - name: Some other step
        run: echo hi
      - name: Build native image (-Pnative -Pprebuilt-jooq)
        working-directory: service
        run: ./mvnw -q -Pnative -Pprebuilt-jooq -DskipTests -Dnative.image.maxheap=${{ matrix.target.maxheap }} package
"""


def test_extract_parses_the_sample_shape() -> None:
    argv = shape.extract_release_native_build_argv(_SAMPLE_WORKFLOW)
    assert argv == [
        "./mvnw", "-q", "-Pnative", "-Pprebuilt-jooq", "-DskipTests",
        "-Dnative.image.maxheap=${{matrix.target.maxheap}}", "package",
    ]


def test_extract_raises_when_the_step_is_missing() -> None:
    with pytest.raises(shape.WorkflowShapeError, match="no step named"):
        shape.extract_release_native_build_argv("jobs:\n  j:\n    steps: []\n")


def test_extract_raises_on_malformed_yaml_top_level() -> None:
    with pytest.raises(shape.WorkflowShapeError, match="'jobs'"):
        shape.extract_release_native_build_argv("not_jobs: {}\n")


def test_extraction_matches_the_real_workflow_file() -> None:
    """Pin against drift: the REAL engine-service-release.yml must parse to
    an invocation carrying every flag this script's Docker-less check relies
    on. A workflow edit that renames the step or drops -Pprebuilt-jooq must
    fail here, not silently stop being checked."""
    workflow_path = REPO_ROOT / ".github" / "workflows" / "engine-service-release.yml"
    assert workflow_path.is_file(), f"workflow moved: {workflow_path}"
    argv = shape.extract_release_native_build_argv(workflow_path.read_text(encoding="utf-8"))
    assert argv[0] == "./mvnw"
    assert "-Pnative" in argv
    assert "-Pprebuilt-jooq" in argv
    assert "-DskipTests" in argv
    assert any(a.startswith("-Dnative.image.maxheap=") for a in argv)
    assert argv[-1] == "package"


def test_substitute_maxheap_replaces_only_the_templated_flag() -> None:
    argv = ["./mvnw", "-Pnative", "-Dnative.image.maxheap=${{ matrix.target.maxheap }}", "package"]
    out = shape._substitute_maxheap(argv, "3g")
    assert out == ["./mvnw", "-Pnative", "-Dnative.image.maxheap=3g", "package"]


# ── check_dockerless_build ────────────────────────────────────────────────

def _stub_mvnw_runner(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    runner = MagicMock()
    runner.return_value = subprocess.CompletedProcess(
        args=["./mvnw"], returncode=returncode, stdout=stdout, stderr=stderr,
    )
    return runner


@pytest.fixture
def jooq_present(tmp_path: Path, monkeypatch) -> Path:
    """Make ``_jooq_sources_present()`` report True without touching the
    real ``service/target`` -- hermetic stand-in for "a caller already ran
    the codegen prerequisite (or this is a warm checkout)". Tests using this
    fixture are exercising ONLY the Docker-less invocation, not the
    self-sufficiency (auto-generate) path -- see the
    ``_ensure_jooq_sources``/``check_dockerless_build_auto_generates_jooq_*``
    tests below for that."""
    marker = tmp_path / "DefaultCatalog.java"
    marker.write_text("// fixture jOOQ marker\n")
    monkeypatch.setattr(shape, "_JOOQ_GENERATED_MARKER", marker)
    return marker


def test_dockerless_build_reports_pass_on_exit_zero(jooq_present) -> None:
    runner = _stub_mvnw_runner(0, stdout="BUILD SUCCESS")
    ok, message = shape.check_dockerless_build(mvnw_runner=runner)
    assert ok
    assert "PASSED" in message
    # DOCKER_HOST must be the nonexistent-socket sentinel, not merely present.
    called_env = runner.call_args.kwargs["env"]
    assert called_env["DOCKER_HOST"] == shape.DOCKER_HOST_NONEXISTENT


def test_dockerless_build_reports_fail_on_nonzero_exit(jooq_present) -> None:
    runner = _stub_mvnw_runner(1, stdout="", stderr="Could not connect to Docker daemon")
    ok, message = shape.check_dockerless_build(mvnw_runner=runner)
    assert not ok
    assert "FAILED" in message
    assert "Docker" in message


def test_dockerless_build_uses_the_release_goal_override(jooq_present) -> None:
    """The invocation's flags come from the workflow; the trailing goal is
    swapped for the caller's choice (default test-compile, upstream of the
    real multi-minute native-image link)."""
    runner = _stub_mvnw_runner(0)
    shape.check_dockerless_build(goal="test-compile", mvnw_runner=runner)
    argv = runner.call_args.args[0]
    assert argv[-1] == "test-compile"
    assert "-Pprebuilt-jooq" in argv
    assert "-DskipTests" in argv


def test_dockerless_build_runs_through_the_leased_wrapper(jooq_present) -> None:
    """AGENTS.md hot rule (nexus-c00dw): never a bare ./mvnw -- a concurrent
    raw invocation against the same service/target can corrupt a peer's
    in-flight jOOQ codegen. The parsed workflow line's own './mvnw' must be
    replaced by the leased wrapper, which supplies that itself."""
    runner = _stub_mvnw_runner(0)
    shape.check_dockerless_build(mvnw_runner=runner)
    argv = runner.call_args.args[0]
    assert argv[0] == str(shape.MVNW_LEASED)
    assert "./mvnw" not in argv


def test_dockerless_build_removes_docker_context_env(jooq_present, monkeypatch) -> None:
    monkeypatch.setenv("DOCKER_CONTEXT", "colima")
    runner = _stub_mvnw_runner(0)
    shape.check_dockerless_build(mvnw_runner=runner)
    called_env = runner.call_args.kwargs["env"]
    assert "DOCKER_CONTEXT" not in called_env


def test_dockerless_build_raises_when_mvnw_leased_missing(jooq_present, monkeypatch) -> None:
    monkeypatch.setattr(shape, "MVNW_LEASED", Path("/nonexistent/mvnw-leased.sh"))
    with pytest.raises(shape.WorkflowShapeError, match="mvnw-leased.sh missing"):
        shape.check_dockerless_build(mvnw_runner=_stub_mvnw_runner(0))


# ── _ensure_jooq_sources / self-sufficiency ─────────────────────────────────

def test_ensure_jooq_sources_noop_when_already_present(jooq_present) -> None:
    """No manual pre-step needed when a caller (or a prior run) already
    populated the directory -- the codegen runner must not be invoked."""
    jooq_runner = MagicMock()
    ok, message = shape._ensure_jooq_sources(jooq_runner=jooq_runner)
    assert ok
    assert message == ""
    jooq_runner.assert_not_called()


def test_ensure_jooq_sources_generates_when_absent(tmp_path: Path, monkeypatch) -> None:
    """Self-sufficiency: a cold service/target must be populated by THIS
    function, via the leased wrapper's generate-sources -- the same command
    the release's separate jooq-codegen job runs -- not merely documented
    as a precondition the caller has to remember."""
    marker = tmp_path / "DefaultCatalog.java"
    monkeypatch.setattr(shape, "_JOOQ_GENERATED_MARKER", marker)

    def _fake_codegen(*_args, **_kwargs):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("// generated\n")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="BUILD SUCCESS", stderr="")

    jooq_runner = MagicMock(side_effect=_fake_codegen)
    ok, message = shape._ensure_jooq_sources(jooq_runner=jooq_runner)
    assert ok
    assert "PASSED" in message
    argv = jooq_runner.call_args.args[0]
    assert argv == [str(shape.MVNW_LEASED), "-q", "generate-sources"]
    # No DOCKER_HOST override here -- this stage NEEDS real Docker, exactly
    # like the release's jooq-codegen job.
    assert "env" not in jooq_runner.call_args.kwargs


def test_ensure_jooq_sources_reports_failure_when_codegen_does_not_produce_the_marker(
    tmp_path: Path, monkeypatch,
) -> None:
    """A codegen run that exits 0 but never actually produced the expected
    output must not read as success -- exactly the "exit zero without the
    marker" vacuity class this whole script exists to close elsewhere."""
    monkeypatch.setattr(shape, "_JOOQ_GENERATED_MARKER", tmp_path / "DefaultCatalog.java")
    jooq_runner = MagicMock(return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""))
    ok, _message = shape._ensure_jooq_sources(jooq_runner=jooq_runner)
    assert not ok


def test_dockerless_build_auto_generates_jooq_sources_when_absent(tmp_path: Path, monkeypatch) -> None:
    """End-to-end self-sufficiency through the public function: a cold
    service/target must not require a manual pre-step."""
    marker = tmp_path / "DefaultCatalog.java"
    monkeypatch.setattr(shape, "_JOOQ_GENERATED_MARKER", marker)

    def _fake_codegen(*_args, **_kwargs):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("// generated\n")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    jooq_runner = MagicMock(side_effect=_fake_codegen)
    mvnw_runner = _stub_mvnw_runner(0)
    ok, message = shape.check_dockerless_build(mvnw_runner=mvnw_runner, jooq_runner=jooq_runner)
    assert ok
    jooq_runner.assert_called_once()
    mvnw_runner.assert_called_once()
    assert "jOOQ prerequisite generation" in message


def test_dockerless_build_fails_loud_when_jooq_prerequisite_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(shape, "_JOOQ_GENERATED_MARKER", tmp_path / "DefaultCatalog.java")
    jooq_runner = MagicMock(return_value=subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="testcontainers: could not find a valid Docker environment",
    ))
    mvnw_runner = _stub_mvnw_runner(0)
    ok, message = shape.check_dockerless_build(mvnw_runner=mvnw_runner, jooq_runner=jooq_runner)
    assert not ok
    assert "FAILED" in message
    mvnw_runner.assert_not_called()


# ── check_checkout_shape_smoke ────────────────────────────────────────────

def test_checkout_smoke_fails_loud_when_classification_is_false() -> None:
    """Non-vacuity: if this process is NOT classified as a dev checkout, the
    check must refuse outright rather than run the smoke and report a
    meaningless pass."""
    classify_proc = subprocess.CompletedProcess(args=[], returncode=3, stdout="", stderr="")
    runner = MagicMock(return_value=classify_proc)
    ok, message = shape.check_checkout_shape_smoke(runner=runner)
    assert not ok
    assert "NON-VACUITY FAILURE" in message
    # Only the classification probe ran -- the smoke script must not have
    # been invoked when the precondition already failed.
    assert runner.call_count == 1


def test_checkout_smoke_runs_the_real_script_when_classification_is_true() -> None:
    classify_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    smoke_proc = subprocess.CompletedProcess(
        args=["bash"], returncode=0, stdout="...\nNATIVE SMOKE PASS\n", stderr="",
    )
    runner = MagicMock(side_effect=[classify_proc, smoke_proc])
    ok, message = shape.check_checkout_shape_smoke(bin_path="/tmp/fake-bin", runner=runner)
    assert ok
    assert "PASSED" in message
    assert runner.call_count == 2
    smoke_call_env = runner.call_args_list[1].kwargs["env"]
    assert smoke_call_env["BIN"] == "/tmp/fake-bin"


def test_checkout_smoke_fails_when_pass_marker_absent_despite_exit_zero() -> None:
    """A script that exits 0 without ever printing the PASS marker (e.g. it
    short-circuited before reaching the end) must not read as a pass."""
    classify_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    smoke_proc = subprocess.CompletedProcess(args=["bash"], returncode=0, stdout="nothing conclusive", stderr="")
    runner = MagicMock(side_effect=[classify_proc, smoke_proc])
    ok, _message = shape.check_checkout_shape_smoke(runner=runner)
    assert not ok


def test_checkout_smoke_raises_when_native_smoke_sh_missing(monkeypatch) -> None:
    monkeypatch.setattr(shape, "NATIVE_SMOKE_SH", Path("/nonexistent/native-smoke.sh"))
    with pytest.raises(shape.WorkflowShapeError, match="native-smoke.sh missing"):
        shape.check_checkout_shape_smoke(runner=MagicMock())


# ── main() wiring ─────────────────────────────────────────────────────────

def test_main_skips_both_phases_and_still_reports_a_verdict(capsys) -> None:
    rc = shape.main(["--skip-checkout-smoke", "--skip-dockerless-build"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "phase (a) SKIPPED" in out
    assert "phase (b) SKIPPED" in out
    assert "RELEASE WORKFLOW SHAPE CHECK PASSED" in out
