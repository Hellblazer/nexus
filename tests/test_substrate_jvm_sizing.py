# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every test-launched engine JVM states its heap and CPU budget (nexus-0yym1).

A JVM with no ``-Xmx`` sizes its heap to a quarter of the memory it can see, and
its thread pools to the CPUs it can see. Inside a lab-run grant those are the
grant's (a ``lab-l`` grant is 48 GiB and 16 CPUs), so each substrate engine could
claim about 12 GiB of heap and an xdist run of them could reach the grant's hard
cap (exit 137). The flags go on the command line, never through
``JAVA_TOOL_OPTIONS``: the JVM prints a ``Picked up ...`` line to stderr for that
variable, and tests read engine stderr.
"""

from __future__ import annotations

import tempfile

import pytest

import tests._engine_substrate as sub
import tests.db._service_fixture as fx
from tests.db._service_fixture import jvm_error_file_arg

HEAP = f"-Xmx{fx.SUBSTRATE_ENGINE_HEAP}"
CPUS = f"-XX:ActiveProcessorCount={fx.SUBSTRATE_ENGINE_CPUS}"


@pytest.fixture(autouse=True)
def _no_moved_tmpdir(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lab-run grant run sets the tmp-root session variable; these tests state the argv
    of a run that did not move TMPDIR, and the tests below set it themselves."""
    monkeypatch.delenv(fx.TMP_SESSION_ENV, raising=False)


def _before_jar(argv: list[str], flag: str) -> bool:
    return flag in argv and argv.index(flag) < argv.index("-jar")


def test_the_substrate_engine_argv_carries_heap_and_cpu_before_jar() -> None:
    argv = sub.engine_argv("/usr/bin/java")
    assert _before_jar(argv, HEAP), argv
    assert _before_jar(argv, CPUS), argv


def test_with_substrate_sizing_inserts_before_jar_and_is_idempotent() -> None:
    cmd = ["/usr/bin/java", jvm_error_file_arg(), "-jar", "a.jar"]
    out = fx.with_substrate_sizing(cmd)
    assert out == ["/usr/bin/java", jvm_error_file_arg(), HEAP, CPUS, "-jar", "a.jar"]
    assert fx.with_substrate_sizing(out) == out, "must not add the flags twice"
    assert cmd == ["/usr/bin/java", jvm_error_file_arg(), "-jar", "a.jar"], (
        "input must not be mutated"
    )


def test_a_callers_own_heap_or_cpu_flag_wins() -> None:
    own = ["java", jvm_error_file_arg(), "-Xmx512m", "-jar", "a.jar"]
    assert fx.with_substrate_sizing(own) == ["java", jvm_error_file_arg(), "-Xmx512m", CPUS, "-jar", "a.jar"]
    own_cpu = ["java", jvm_error_file_arg(), "-XX:ActiveProcessorCount=8", "-jar", "a.jar"]
    assert fx.with_substrate_sizing(own_cpu) == [
        "java", jvm_error_file_arg(), "-XX:ActiveProcessorCount=8", HEAP, "-jar", "a.jar",
    ]


def test_a_native_launch_is_left_alone() -> None:
    native = ["/opt/nexus-service", "-Duser.timezone=UTC"]
    assert fx.with_substrate_sizing(native) == native


# ── java.io.tmpdir follows the run's TMPDIR while it is on tmpfs (nexus-kyujr) ──


@pytest.mark.parametrize("session", ["tmpfs=/dev/shm/nexus-tmp-1/rabc", "forced=/keep/here"])
def test_a_moved_tmpdir_becomes_the_engines_java_io_tmpdir_before_jar(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session: str,
) -> None:
    monkeypatch.setenv(fx.TMP_SESSION_ENV, session)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    flag = f"-Djava.io.tmpdir={tmp_path}"
    argv = sub.engine_argv("/usr/bin/java")
    assert _before_jar(argv, flag), argv
    out = fx.with_substrate_sizing(["java", jvm_error_file_arg(), "-jar", "a.jar"])
    assert flag in out and out.index(flag) < out.index("-jar")
    assert fx.with_substrate_sizing(out) == out, "must not add it twice"


@pytest.mark.parametrize("session", [None, "disk=not inside a lab-run grant", "disk="])
def test_an_unmoved_tmpdir_adds_no_java_io_tmpdir(monkeypatch: pytest.MonkeyPatch, session) -> None:
    if session is not None:
        monkeypatch.setenv(fx.TMP_SESSION_ENV, session)
    assert fx.run_tmpdir_arg() is None
    assert not any(a.startswith("-Djava.io.tmpdir=") for a in sub.engine_argv("/usr/bin/java"))


def test_a_callers_own_java_io_tmpdir_wins_and_a_native_launch_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(fx.TMP_SESSION_ENV, "tmpfs=/dev/shm/nexus-tmp-1/rabc")
    own = ["java", jvm_error_file_arg(), "-Djava.io.tmpdir=/mine", "-jar", "a.jar"]
    assert [a for a in fx.with_substrate_sizing(own) if a.startswith("-Djava.io.tmpdir=")] == ["-Djava.io.tmpdir=/mine"]
    native = ["/opt/nexus-service", "-Duser.timezone=UTC"]
    assert fx.with_substrate_sizing(native) == native
