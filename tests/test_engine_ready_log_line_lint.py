# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine log line the substrate waits for exists in the Java source (nexus-kmfz6).

``tests/_engine_substrate.py::_wait_engine_ready`` decides a boot is ready only when
the engine's own log carries ``_ENGINE_READY_PREFIX`` plus the port. That text is
written in exactly one place, ``NexusService.start``. Rename the Java event and every
substrate boot waits out the readiness timeout and fails, with a log that looks
healthy. This pins the two sides to each other from the cheap, per-PR lint bucket.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from tests import _engine_substrate as sub

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
NEXUS_SERVICE_JAVA = (
    REPO_ROOT / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service" / "NexusService.java"
)


def ready_line_defect(java_text: str, prefix: str) -> str | None:
    """Why *java_text* does not log *prefix* + port after ``server.start()``, else None."""
    method = re.search(r"public void start\(\)\s*\{(?P<body>.*?)\n    \}", java_text, re.DOTALL)
    if method is None:
        return "NexusService has no `public void start()` method"
    body = method.group("body")
    started = body.find("server.start();")
    if started < 0:
        return "NexusService.start() no longer calls server.start()"
    log_call = re.search(r'log\.info\(\s*"' + re.escape(prefix) + r'\{\}"', body)
    if log_call is None:
        return f'NexusService.start() has no log.info("{prefix}{{}}", ...) call'
    if log_call.start() < started:
        return "the ready log line is written BEFORE server.start() returns"
    return None


def test_the_ready_prefix_is_the_text_the_java_logs_after_the_server_starts() -> None:
    assert NEXUS_SERVICE_JAVA.is_file(), f"{NEXUS_SERVICE_JAVA} moved; update this lint"
    defect = ready_line_defect(NEXUS_SERVICE_JAVA.read_text(encoding="utf-8"), sub._ENGINE_READY_PREFIX)
    assert defect is None, f"{defect}; _wait_engine_ready would wait out its timeout on every boot"


_GOOD = """
    public void start() {
        server.start();
        log.info("event=service_started port={}", getPort());
    }
"""


@pytest.mark.parametrize(
    ("java", "needle"),
    [
        (_GOOD.replace("service_started", "service_listening"), "no log.info"),
        (_GOOD.replace("port={}", "addr={}"), "no log.info"),
        (_GOOD.replace("server.start();", "server.begin();"), "no longer calls"),
        (
            """
    public void start() {
        log.info("event=service_started port={}", getPort());
        server.start();
    }
""",
            "BEFORE",
        ),
    ],
    ids=["event-renamed", "port-placeholder-renamed", "start-call-removed", "log-before-start"],
)
def test_the_check_fails_when_the_java_line_is_edited(java: str, needle: str) -> None:
    assert ready_line_defect(_GOOD, sub._ENGINE_READY_PREFIX) is None
    defect = ready_line_defect(java, sub._ENGINE_READY_PREFIX)
    assert defect is not None and needle in defect
