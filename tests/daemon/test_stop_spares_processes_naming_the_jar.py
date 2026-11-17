# SPDX-License-Identifier: AGPL-3.0-or-later
"""The stop matches a ``NEXUS_SERVICE_JAR`` engine by its java command, never by
the jar path appearing anywhere in a command line (nexus-f3yqd.2.22).

Measured on chas, RDR-246 MVV, 3 of 3 runs: ``nx daemon service stop --with-pg``
exited 1 with "pid(s) 698571, 698573 survived SIGKILL" and left PostgreSQL
running. The two pids were lab-run's ``lab-hand-exec`` wrapper, run as root by
sudo, whose command line ends in the driver's own command ``uv run python
scripts/data_move_mvv.py scenario upgrade ... --jar <jar>``. The matcher's JVM
arm tested ``"-jar <jar>" in command``; ``--jar <jar>`` contains that text, so the
wrapper joined the kill set. It is not an ancestor of the stop (lab-run runs the
command beside it, not under it), so the ancestor guard did not spare it; root
owns it, so SIGTERM and SIGKILL both met EPERM and the pid stayed, and the stop
exited before its PostgreSQL step. The pids went away when the lab-run session
ended. ``upgrade_boot_writes.py --new-jar <jar>`` (release Step 3h) has the same
shape. Had the wrapper been the caller's own, the stop would have killed it.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.daemon.service_registry import (
    process_command,
    storage_service_member_check,
    storage_service_stack_matcher,
    sweep_matching_processes,
)
from tests.daemon._children import CHILD_PYTHON, spawn_sleeper


@pytest.fixture
def jar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "mvv" / "nexus-service-mvv.jar"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)
    monkeypatch.setenv("NEXUS_SERVICE_JAR", str(path))
    return path.resolve()


class TestJarArmOfTheMatcher:
    @pytest.mark.parametrize(
        "command",
        [
            # the engine as the supervisor launches it (storage_service_daemon._build_argv)
            "/usr/bin/java -Duser.timezone=UTC -Djava.net.preferIPv4Stack=true "
            "-XX:ErrorFile=/home/u/cfg/logs/hs_err_%p.log -jar {jar}",
            "java -jar {jar}",
            "/usr/bin/java -Xmx1g -jar {jar} --port 8080",
            # a JAVA_HOME with a space, as a quoted first argument
            '"/opt/my jdk/bin/java" -Duser.timezone=UTC -jar {jar}',
        ],
    )
    def test_the_engine_matches(self, tmp_path: Path, jar: Path, command: str) -> None:
        assert storage_service_stack_matcher(tmp_path / "cfg")(command.format(jar=jar))

    @pytest.mark.parametrize(
        "command",
        [
            # the measured pair: lab-run's root wrapper around the MVV driver
            "/bin/bash /usr/local/libexec/lab-hand-exec 524a6b6d /home/u/tmp/lab-run-env.c63k "
            "uv run python scripts/data_move_mvv.py scenario upgrade --work /w --orig /o --jar {jar}",
            "sudo -n /usr/local/libexec/lab-hand-exec 524a6b6d /home/u/tmp/lab-run-env.c63k "
            "uv run python scripts/data_move_mvv.py scenario upgrade --jar {jar}",
            "uv run python scripts/data_move_mvv.py scenario upgrade --jar {jar}",
            # release Step 3h's harness
            "python3 scripts/upgrade_boot_writes.py --prev-client 8.3.0 --new-jar {jar}",
            # a shell that runs java is not the engine; the java it runs is
            "/bin/sh -c /usr/bin/java -jar {jar}",
            # another jar that starts with this one's path
            "/usr/bin/java -jar {jar}.bak",
            "tail -f {jar}",
        ],
    )
    def test_a_command_that_only_names_the_jar_does_not_match(
        self, tmp_path: Path, jar: Path, command: str,
    ) -> None:
        assert not storage_service_stack_matcher(tmp_path / "cfg")(command.format(jar=jar))


@pytest.mark.skipif(sys.platform == "win32", reason="the positive control runs python under a symlink named java")
class TestSweepWithRealProcesses:
    """The sweep the stop runs, against live processes. ``terminate_pids`` is
    recorded and never run, so nothing else on the box is signalled."""

    def _found(self, cfg: Path) -> set[int]:
        with patch("nexus.daemon.service_registry.terminate_pids", return_value=[]):
            result = sweep_matching_processes(
                storage_service_stack_matcher(cfg), member_check=storage_service_member_check(cfg),
            )
        return {pid for pid, _cmd in result.found}

    @staticmethod
    def _visible(pid: int) -> None:
        deadline = time.monotonic() + 10
        while not process_command(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process_command(pid), f"fixture precondition: pid {pid} must be in the process table"

    def test_a_driver_naming_the_jar_is_not_swept_and_a_java_engine_is(
        self, tmp_path: Path, jar: Path,
    ) -> None:
        cfg = tmp_path / "cfg"
        java = tmp_path / "jdk" / "bin" / "java"
        java.parent.mkdir(parents=True)
        os.symlink(CHILD_PYTHON, java)
        driver = spawn_sleeper(["scenario", "upgrade", "--work", str(tmp_path / "w"), "--jar", str(jar)])
        engine = None
        try:
            from tests._child_process import popen_in_group  # noqa: PLC0415

            engine = popen_in_group(
                [str(java), "-c", "import time; time.sleep(120)", "-Duser.timezone=UTC", "-jar", str(jar)],
            )
            self._visible(driver.pid)
            self._visible(engine.pid)
            assert f"-jar {jar}" in process_command(driver.pid), (
                "fixture precondition: the driver's command line must carry the old marker's text, "
                "or this proves nothing"
            )
            found = self._found(cfg)
            assert engine.pid in found, f"non-vacuity: the java engine must match ({process_command(engine.pid)!r})"
            assert driver.pid not in found, (
                f"pid {driver.pid} only names the jar as an option value and must never be signalled: "
                f"{process_command(driver.pid)!r}"
            )
        finally:
            for proc in (driver, engine):
                if proc is not None:
                    proc.kill()
                    proc.wait(timeout=10)
