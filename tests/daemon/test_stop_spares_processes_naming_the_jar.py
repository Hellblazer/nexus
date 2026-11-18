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

from nexus._install.winproc_core import render_command_line
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
            # a JAVA_HOME with a space: process tables render it unquoted
            "/opt/my jdk/bin/java -Duser.timezone=UTC -jar {jar}",
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
            # a wrapper before an absolute java is the wrapper, not the engine (review round 2)
            "sudo /usr/bin/java -jar {jar}",
            "env A=1 /usr/bin/java -jar {jar}",
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


def test_a_windows_jdk_under_program_files_matches_as_the_process_table_renders_it(tmp_path: Path, jar: Path) -> None:
    """The Windows table dequotes (winproc_core.render_command_line), so the spaced JDK path arrives as words; the
    first version of the fix anchored on a no-space head and missed it (review of 3f549e254)."""
    raw = (r'"C:\Program Files\Eclipse Adoptium\jdk-21.0.5.11-hotspot\bin\java.exe" '
           rf'-Duser.timezone=UTC -Djava.net.preferIPv4Stack=true -jar "{jar}"')
    rendered = render_command_line(raw)
    assert '"' not in rendered and "Program Files" in rendered, rendered
    assert storage_service_stack_matcher(tmp_path / "cfg")(rendered)
