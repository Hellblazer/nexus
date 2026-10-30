# SPDX-License-Identifier: AGPL-3.0-or-later
"""The suite never runs the user's real ``claude --version`` (nexus-jypmj.21).

The "Answer-kind extraction" doctor row probes ``claude --version`` on the ``cli`` backend, so a test that reaches
``run_health_checks()`` on a box with ``claude`` on PATH would spawn it. ``tests/conftest.py`` stubs the probe for
every test; this pins that with a stand-in ``claude`` that leaves a marker file if anything runs it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nexus import health
from tests._module_seam import setattr_in


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in claude is a #!/bin/sh script")
def test_a_test_that_reaches_the_doctor_row_does_not_spawn_claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    stub = tmp_path / "claude"
    stub.write_text(f"#!/bin/sh\ntouch '{marker}'\necho 2.1.0\n", encoding="utf-8")
    stub.chmod(0o755)
    setattr_in(monkeypatch, "nexus.operators.llm_backend", "shutil.which", lambda name: str(stub))
    setattr_in(monkeypatch, "nexus.health", "shutil.which", lambda name: str(stub))
    assert health._claude_cli_problem() is None
    (row,) = health._check_answer_kind_extraction()
    assert row.ok
    assert not marker.exists(), "something ran the real claude --version under the suite"
