# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-zz2w7: the embedded-resources trip-wire runs on every shipped native binary.

Until nexus-zz2w7 the check ran only in ci.yml's linux PR trip-wire, while
engine-service-release.yml built every shipped binary (mac-arm64 on hellmini
included) with ``-q`` and no report, so a mac-only regression shipped green. The
checker's logic is tested in tests/scripts/test_check_native_embedded_resources.py;
this pins that the release legs actually feed it: the report option is set on the
one existing native build (no extra build), the Maven log is kept un-quieted, and
the check step runs with the leg's own matrix arch, which must name a PLATFORMS
entry. Workflow shell is not executable in CI-of-CI, so these read the YAML with
comment lines stripped (a pin a comment can satisfy is not a pin).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import check_native_embedded_resources as chk

REPO = Path(__file__).parent.parent
RELEASE = REPO / ".github" / "workflows" / "engine-service-release.yml"
CI = REPO / ".github" / "workflows" / "ci.yml"

OPTIONS = "-H:+UnlockExperimentalVMOptions -H:+GenerateEmbeddedResourcesFile"


def _code(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _steps(path: Path, job: str) -> list[dict]:
    return yaml.safe_load(path.read_text())["jobs"][job]["steps"]


def _step(steps: list[dict], name_prefix: str) -> tuple[int, dict]:
    for index, step in enumerate(steps):
        if str(step.get("name", "")).startswith(name_prefix):
            return index, step
    raise AssertionError(f"no step named {name_prefix!r}")


def _release_native_steps() -> tuple[list[dict], int, dict, int, dict]:
    steps = _steps(RELEASE, "build-publish")
    build_index, build = _step(steps, "Build native image")
    check_index, check = _step(steps, "Check the embedded native resources")
    return steps, build_index, build, check_index, check


def test_release_matrix_arches_are_all_checker_platforms() -> None:
    """The check step passes `--platform ${{ matrix.target.arch }}`, so a matrix
    arch with no PLATFORMS entry would fail its own leg (or, worse, tempt someone
    to skip the check for it)."""
    spec = yaml.safe_load(RELEASE.read_text())
    arches = [t["arch"] for t in spec["jobs"]["build-publish"]["strategy"]["matrix"]["target"]]
    assert len(arches) >= 3, f"matrix read as {arches}: the workflow shape changed and this pin would be vacuous"
    assert {"linux-amd64", "linux-arm64", "mac-arm64"} <= set(arches)
    assert set(arches) <= set(chk.PLATFORMS), f"matrix arches without a checker platform: {set(arches) - set(chk.PLATFORMS)}"


def test_release_build_emits_the_report_without_a_second_build() -> None:
    _, _, build, _, _ = _release_native_steps()
    assert build["env"]["NATIVE_IMAGE_OPTIONS"] == OPTIONS
    run = _code(build["run"])
    assert len(re.findall(r"-Pnative\b", run)) == 1, "the leg must build once and report from that build"
    native_builds = [ln for ln in _code(RELEASE.read_text()).splitlines() if "mvnw" in ln and "-Pnative" in ln]
    assert len(native_builds) == 1, (
        f"a second native build in the release workflow is the extra expensive job the trip-wire must not add: {native_builds}"
    )


def test_release_build_keeps_the_maven_log_the_checker_reads() -> None:
    """-q suppresses the `--- jar:...`, `--- native:...`, `--- shade:...` execution
    lines the checker proves build order from. The log goes through tee, and pipefail
    keeps a failed build failing the step."""
    _, _, build, _, _ = _release_native_steps()
    run = _code(build["run"])
    mvnw = next(line for line in run.splitlines() if "./mvnw" in line)
    assert not re.search(r"\s-q(?:\s|$)", mvnw), f"-q would blind the checker's log read: {mvnw}"
    assert re.search(r"\s-ntp(?:\s|$)", mvnw)
    assert "pipefail" in run and re.search(r'\|\s*tee\s+"\$RUNNER_TEMP/native-build\.log"', run)


def test_release_check_step_runs_after_the_build_and_before_anything_ships() -> None:
    steps, build_index, _, check_index, check = _release_native_steps()
    assert check_index == build_index + 1, "the check belongs directly after the build it reads"
    for later in ("Native smoke test", "Stage artifact + sha256", "Sign release asset", "Upload native binary assets"):
        assert check_index < _step(steps, later)[0], f"the check must run before {later!r}"
    run = _code(check["run"])
    assert "scripts/check_native_embedded_resources.py" in run
    assert "--platform ${{ matrix.target.arch }}" in run
    assert "--report service/target/embedded-resources.json" in run
    assert '--build-log "$RUNNER_TEMP/native-build.log"' in run
    assert "if" not in check, "the check must run on every leg and every trigger, tag push or not"


def test_pr_trip_wire_uses_the_same_checker_and_platform_table() -> None:
    """ci.yml's linux PR trip-wire is the same check on the same table."""
    text = _code(CI.read_text())
    assert "--platform linux-amd64" in text
    assert "--platform-dir" not in text
    assert OPTIONS in text


@pytest.mark.parametrize("path", [RELEASE, CI])
def test_workflow_keeps_its_concurrency_group(path: Path) -> None:
    spec = yaml.safe_load(path.read_text())
    assert spec.get("concurrency", {}).get("group"), f"{path.name} lost its concurrency group"
