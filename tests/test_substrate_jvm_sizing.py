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

import tests._engine_substrate as sub
import tests.db._service_fixture as fx
from tests.db._service_fixture import jvm_error_file_arg

HEAP = f"-Xmx{fx.SUBSTRATE_ENGINE_HEAP}"
CPUS = f"-XX:ActiveProcessorCount={fx.SUBSTRATE_ENGINE_CPUS}"


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

