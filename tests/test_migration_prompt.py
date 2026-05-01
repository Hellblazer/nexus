# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-101 Phase 3 follow-up D (nexus-o6aa.9.9): TTY-gated upgrade
prompt for the bootstrap-fallback state.

The prompt must:
* Fire once per process (sentinel reset between tests).
* Suppress in non-TTY contexts.
* Suppress under NEXUS_NO_PROMPTS=1.
* Suppress when fallback_active is False.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.commands import _migration_prompt


@pytest.fixture(autouse=True)
def _reset_sentinel():
    """The module-level _PROMPTED sentinel persists between tests in
    the same process. Reset before each test so tests are independent.
    """
    _migration_prompt._PROMPTED = False
    yield
    _migration_prompt._PROMPTED = False


def test_prompt_fires_once_when_fallback_active_and_tty(capsys):
    """All gates pass: stderr is a TTY, NEXUS_NO_PROMPTS unset, fallback
    active. Expect a stderr WARNING with the migration verb name.
    """
    with patch(
        "nexus.commands.catalog._check_bootstrap_status",
        return_value={"fallback_active": True},
    ), patch("sys.stderr.isatty", return_value=True), \
       patch.dict("os.environ", {}, clear=False):
        # Make sure NEXUS_NO_PROMPTS is unset.
        import os
        os.environ.pop("NEXUS_NO_PROMPTS", None)

        _migration_prompt.maybe_emit_bootstrap_prompt()

    captured = capsys.readouterr()
    assert "bootstrap-fallback active" in captured.err
    assert "nx catalog migrate" in captured.err
    assert "NEXUS_NO_PROMPTS" in captured.err


def test_prompt_fires_only_once_per_process(capsys):
    """Two consecutive calls when gates pass: only the first emits.
    Multiple Catalog constructions in one CLI run must not double-
    prompt.
    """
    with patch(
        "nexus.commands.catalog._check_bootstrap_status",
        return_value={"fallback_active": True},
    ), patch("sys.stderr.isatty", return_value=True):
        import os
        os.environ.pop("NEXUS_NO_PROMPTS", None)
        _migration_prompt.maybe_emit_bootstrap_prompt()
        _migration_prompt.maybe_emit_bootstrap_prompt()

    captured = capsys.readouterr()
    # Single emission — count occurrences of the unique header line.
    assert captured.err.count("bootstrap-fallback active") == 1


def test_prompt_suppressed_when_not_tty(capsys):
    """Non-TTY context (CI / cron / MCP / pipe redirect): no prompt."""
    with patch(
        "nexus.commands.catalog._check_bootstrap_status",
        return_value={"fallback_active": True},
    ), patch("sys.stderr.isatty", return_value=False):
        import os
        os.environ.pop("NEXUS_NO_PROMPTS", None)
        _migration_prompt.maybe_emit_bootstrap_prompt()

    captured = capsys.readouterr()
    assert "bootstrap-fallback" not in captured.err


def test_prompt_suppressed_by_nexus_no_prompts_env(capsys, monkeypatch):
    """NEXUS_NO_PROMPTS=1 escape hatch: no prompt even in TTY context."""
    monkeypatch.setenv("NEXUS_NO_PROMPTS", "1")
    with patch(
        "nexus.commands.catalog._check_bootstrap_status",
        return_value={"fallback_active": True},
    ), patch("sys.stderr.isatty", return_value=True):
        _migration_prompt.maybe_emit_bootstrap_prompt()

    captured = capsys.readouterr()
    assert "bootstrap-fallback" not in captured.err


@pytest.mark.parametrize("val", ["true", "yes", "on", "TRUE", "1"])
def test_prompt_suppressed_by_truthy_no_prompts_values(
    val: str, capsys, monkeypatch,
):
    """Several truthy spellings of NEXUS_NO_PROMPTS suppress."""
    monkeypatch.setenv("NEXUS_NO_PROMPTS", val)
    with patch(
        "nexus.commands.catalog._check_bootstrap_status",
        return_value={"fallback_active": True},
    ), patch("sys.stderr.isatty", return_value=True):
        _migration_prompt.maybe_emit_bootstrap_prompt()

    captured = capsys.readouterr()
    assert "bootstrap-fallback" not in captured.err


def test_prompt_suppressed_when_fallback_not_active(capsys):
    """Catalog is fine — no fallback. No prompt."""
    with patch(
        "nexus.commands.catalog._check_bootstrap_status",
        return_value={"fallback_active": False},
    ), patch("sys.stderr.isatty", return_value=True):
        import os
        os.environ.pop("NEXUS_NO_PROMPTS", None)
        _migration_prompt.maybe_emit_bootstrap_prompt()

    captured = capsys.readouterr()
    assert "bootstrap-fallback" not in captured.err


def test_prompt_swallows_check_exceptions(capsys):
    """If _check_bootstrap_status raises (filesystem issue, etc.),
    the prompt suppresses rather than disrupting the CLI invocation.
    The prompt is advisory; the underlying state is logged via
    structlog and surfaced in doctor's structured output regardless.
    """
    with patch(
        "nexus.commands.catalog._check_bootstrap_status",
        side_effect=RuntimeError("simulated check failure"),
    ), patch("sys.stderr.isatty", return_value=True):
        import os
        os.environ.pop("NEXUS_NO_PROMPTS", None)
        # Must not raise.
        _migration_prompt.maybe_emit_bootstrap_prompt()

    captured = capsys.readouterr()
    assert "bootstrap-fallback" not in captured.err
