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


def _run_function(bindir: Path, bin_path: str, *extra: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    script = f'source "{LIB}"; shakeout_release_workflow_shape_check "$@"'
    return subprocess.run(
        ["bash", "-c", script, "_", bin_path, *extra],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30, check=False,
    )


def _stub_exe(bindir: Path, name: str, body: str) -> None:
    exe = bindir / name
    exe.write_text("#!/usr/bin/env bash\n" + body)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)


def _fake_elf(tmp_path: Path) -> Path:
    """Enough of an ELF header for `file -b` to call it ELF, and never
    runnable anywhere."""
    elf = tmp_path / "nexus-service"
    elf.write_bytes(b"\x7fELF\x02\x01\x01" + b"\x00" * 57)
    elf.chmod(0o755)
    return elf


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
    """A path that is not an ELF (here, not a file at all) reaches the check
    verbatim, with no JVM-jar shim."""
    bindir, log = _stub_uv(tmp_path, 0)
    _run_function(bindir, "/some/other/path/nexus-service")
    assert "--bin /some/other/path/nexus-service" in log.read_text()


# ── JVM-jar shim when the host cannot execute the candidate ─────────────
# First real --shakeout on a macOS host (2026-09-13): the -Ob build runs in
# a Linux container there, so the candidate is a Linux ELF and native-smoke.sh
# died with "Exec format error". `uname` is stubbed so these behave the same
# on a macOS box and a Linux CI runner.

def test_linux_elf_on_a_non_linux_host_is_checked_through_a_jvm_shim(tmp_path: Path) -> None:
    bindir, log = _stub_uv(tmp_path, 0)
    _stub_exe(bindir, "uname", "echo Darwin\n")
    elf = _fake_elf(tmp_path)
    jar = tmp_path / "nexus-service-1.0-SNAPSHOT.jar"
    jar.write_bytes(b"PK")
    proc = _run_function(bindir, str(elf), str(jar))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    call = log.read_text().splitlines()[0]
    checked = call.split("--bin ", 1)[1].strip()
    assert checked != str(elf), "the unrunnable ELF reached native-smoke.sh"
    assert checked.endswith("nexus-service-jvm-shim")
    assert f"boots the same build's JVM jar ({jar})" in proc.stdout


def test_jvm_shim_moves_system_properties_ahead_of_jar(tmp_path: Path) -> None:
    """native-smoke.sh starts $BIN with -Duser.timezone=UTC; java ignores a
    -D placed after -jar, so the shim must reorder it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv_log = tmp_path / "java-argv.log"
    _stub_exe(bindir, "java", f"printf '%s\\n' \"$@\" > '{argv_log}'\n")
    jar = tmp_path / "engine.jar"
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    made = subprocess.run(
        ["bash", "-c", f'source "{LIB}"; shakeout_shape_write_jvm_shim "$1" "$2"', "_", str(jar), str(shim_dir)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30, check=True,
    )
    shim = made.stdout.strip()
    subprocess.run([shim, "-Duser.timezone=UTC", "serve"], env=env, check=True, timeout=30)
    assert argv_log.read_text().splitlines() == ["-Duser.timezone=UTC", "-jar", str(jar), "serve"]


def test_linux_elf_on_a_linux_host_runs_the_real_candidate(tmp_path: Path) -> None:
    bindir, log = _stub_uv(tmp_path, 0)
    _stub_exe(bindir, "uname", "echo Linux\n")
    elf = _fake_elf(tmp_path)
    proc = _run_function(bindir, str(elf), str(tmp_path / "unused.jar"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"--bin {elf}" in log.read_text()


def test_unrunnable_candidate_with_no_jar_fails_loud_without_running_the_check(tmp_path: Path) -> None:
    bindir, log = _stub_uv(tmp_path, 0)
    _stub_exe(bindir, "uname", "echo Darwin\n")
    elf = _fake_elf(tmp_path)
    proc = _run_function(bindir, str(elf), str(tmp_path / "missing.jar"))
    assert proc.returncode != 0
    assert "release-workflow SHAPE check: FAILED" in proc.stderr
    assert "cannot execute" in proc.stderr
    assert not log.exists(), "the check ran against a binary this host cannot execute"


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


def test_run_sh_passes_the_same_builds_jvm_jar() -> None:
    """A host that cannot execute the Linux candidate needs the jar; the call
    site must hand it over in both default and --artifacts mode."""
    text = RUN_SH.read_text(encoding="utf-8")
    call_at = text.index("shakeout_release_workflow_shape_check ")
    call_line = text[call_at:text.index("\n", call_at)]
    assert '"$_shakeout_shape_jar"' in call_line
    assert '"$ARTIFACTS/jar"' in text
    assert "'nexus-service-*.jar'" in text


def test_default_mode_uses_the_freshly_built_host_binary() -> None:
    text = RUN_SH.read_text(encoding="utf-8")
    assert '$PWD/service/target/nexus-service' in text
