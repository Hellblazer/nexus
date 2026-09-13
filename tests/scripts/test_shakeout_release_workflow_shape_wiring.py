# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-xihsm (critic follow-up, nexus-vpl9c riders): the release-workflow
SHAPE check (scripts/check_release_workflow_shape.py) was landing unwired --
run by nobody, the exact "release-only procedure rots silently" class the
check exists to close.

WHAT THIS EXERCISES. run.sh's native build (multi-minute, real Docker) is
too expensive to drive from a unit test, so the wiring lives in
tests/e2e/migration-rehearsal/lib/shakeout_shape_check.sh's
shakeout_release_workflow_shape_check function -- run.sh sources that SAME
file and calls that SAME function, following the lib/stage_artifacts.sh
(nexus-og52j) and lib/heartbeat_stall_note.sh (nexus-wo6sc) precedent. Every
test below sources that file and drives the function directly against a
stubbed `uv`, so PASS and FAIL are both proven without a real native build
or a real container.

WHY NOT INSIDE rehearse_shakeout.sh. That script runs INSIDE the --shakeout
container, which is a uv-tool-installed wheel with no .git/pyproject.toml
ancestor by design -- the exact absence nexus-xihsm's checkout-vs-installed-
wheel class is about. Phase (a)'s non-vacuity assert (is_dev_checkout_process()
must be True) could only ever refuse there, and phase (b) needs
service/mvnw + a JDK, neither of which the container ships. The wiring is
therefore in run.sh, on the HOST, right after the native build -- see the
lib file's own header for the full argument.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "tests/e2e/migration-rehearsal/lib/shakeout_shape_check.sh"
RUN_SH = REPO_ROOT / "tests/e2e/migration-rehearsal/run.sh"


def _stub_uv(tmp_path: Path, returncode: int) -> tuple[Path, Path]:
    """A fake `uv` that records its argv and exits *returncode* -- stands
    in for the real `uv run python scripts/check_release_workflow_shape.py
    --bin <path>` invocation."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "uv-calls.log"
    uv = bindir / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"$*\" >> '{log}'\n"
        f"exit {returncode}\n"
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    return bindir, log


def _run_function(bindir: Path, bin_path: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    script = f'source "{LIB}"; shakeout_release_workflow_shape_check "$1"'
    return subprocess.run(
        ["bash", "-c", script, "_", bin_path],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30, check=False,
    )


def test_the_lib_exists() -> None:
    assert LIB.is_file(), f"extraction moved or was never made: {LIB}"


def test_pass_prints_the_verdict_and_returns_zero(tmp_path: Path) -> None:
    bindir, log = _stub_uv(tmp_path, 0)
    proc = _run_function(bindir, "/fake/native/nexus-service")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "release-workflow SHAPE check: PASSED" in proc.stdout
    calls = log.read_text().splitlines()
    assert len(calls) == 1
    assert "check_release_workflow_shape.py" in calls[0]
    assert "--bin /fake/native/nexus-service" in calls[0]


def test_fail_prints_the_verdict_and_returns_nonzero(tmp_path: Path) -> None:
    """RED: the check failing must not be swallowed -- the function's own
    return code must carry it, not merely its printed text."""
    bindir, _log = _stub_uv(tmp_path, 1)
    proc = _run_function(bindir, "/fake/native/nexus-service")
    assert proc.returncode != 0
    assert "release-workflow SHAPE check: FAILED" in proc.stderr


def test_passes_the_real_native_binary_path_through_unmodified(tmp_path: Path) -> None:
    """No JVM-jar shim -- the caller's exact binary path reaches the check
    verbatim."""
    bindir, log = _stub_uv(tmp_path, 0)
    _run_function(bindir, "/some/other/path/nexus-service")
    assert "--bin /some/other/path/nexus-service" in log.read_text()


# ── run.sh wiring pins ───────────────────────────────────────────────────

def test_run_sh_sources_and_calls_the_shape_check_lib() -> None:
    text = RUN_SH.read_text(encoding="utf-8")
    assert "lib/shakeout_shape_check.sh" in text, "run.sh no longer sources the lib"
    assert "shakeout_release_workflow_shape_check" in text.replace(
        "lib/shakeout_shape_check.sh", ""
    ), "run.sh sources the lib but never calls the function"


def test_the_call_is_scoped_to_shakeout_only() -> None:
    text = RUN_SH.read_text(encoding="utf-8")
    call_at = text.index("shakeout_release_workflow_shape_check ")
    # The nearest preceding `if` on the call's own guard must name SHAKEOUT.
    guard_at = text.rfind('if [ "$SHAKEOUT" = 1 ]; then', 0, call_at)
    assert guard_at != -1
    # No unrelated `fi` between the guard and the call (i.e. the call is
    # actually inside this if-block, not merely preceded by it somewhere
    # else in the file).
    between = text[guard_at:call_at]
    assert between.count("\nfi\n") == 0, (
        "shakeout_release_workflow_shape_check is not scoped inside the "
        "SHAKEOUT-only if-block any more"
    )


def test_the_call_gates_run_sh_exit_code_never_swallows_failure() -> None:
    text = RUN_SH.read_text(encoding="utf-8")
    call_at = text.index("shakeout_release_workflow_shape_check ")
    line_end = text.index("\n", call_at)
    call_line = text[call_at:line_end]
    assert "|| exit 1" in call_line or "|| exit" in call_line, (
        f"the call must gate run.sh's own exit code on failure, got: {call_line!r}"
    )


def test_the_call_is_positioned_after_the_native_build_step() -> None:
    """The check needs the just-built candidate + freshly generated jOOQ
    sources already on disk -- it must not run before the build step that
    produces them."""
    text = RUN_SH.read_text(encoding="utf-8")
    build_step_at = text.index('-Dnative.image.opt=-Ob')
    call_at = text.index("shakeout_release_workflow_shape_check ")
    assert build_step_at < call_at


def test_no_pipe_into_an_early_exit_consumer_at_the_call_site() -> None:
    """The wiring must not introduce a new pipefail-lint violation (a pipe
    into grep/head/etc as control flow) -- the call uses a plain `||`, not
    a pipe."""
    text = RUN_SH.read_text(encoding="utf-8")
    call_at = text.index("shakeout_release_workflow_shape_check ")
    line_end = text.index("\n", call_at)
    call_line = text[call_at:line_end]
    assert "|" not in call_line.replace("||", "")


def test_artifacts_mode_uses_the_manifest_native_path() -> None:
    """--shakeout --artifacts <dir> must check the MANIFEST-VERIFIED
    candidate, not a stale/absent service/target/nexus-service."""
    text = RUN_SH.read_text(encoding="utf-8")
    assert '$ARTIFACTS/native/nexus-service' in text


def test_default_mode_uses_the_freshly_built_host_binary() -> None:
    text = RUN_SH.read_text(encoding="utf-8")
    assert '$PWD/service/target/nexus-service' in text
