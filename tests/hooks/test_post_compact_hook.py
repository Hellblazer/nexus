"""Tests for the PostCompact hook script."""
from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "nx" / "hooks" / "scripts" / "post_compact_hook.sh"

STDIN_PAYLOAD = json.dumps({
    "session_id": "test-session",
    "transcript_path": "/tmp/transcript.jsonl",
    "cwd": "/tmp",
    "permission_mode": "default",
    "hook_event_name": "PostCompact",
    "trigger": "manual",
    "compact_summary": "Summary of compacted conversation.",
})


def _run_hook(
    *,
    env_overrides: dict[str, str] | None = None,
    stdin: str = STDIN_PAYLOAD,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": os.environ.get("PATH", ""),
        **(env_overrides or {}),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )


class TestPostCompactHook:
    """PostCompact hook script tests."""

    def test_script_exists_and_is_executable(self) -> None:
        assert SCRIPT.exists(), f"Script not found: {SCRIPT}"
        assert os.access(SCRIPT, os.X_OK), f"Script not executable: {SCRIPT}"

    def test_exits_zero(self) -> None:
        result = _run_hook()
        assert result.returncode == 0

    def test_output_under_20_lines(self) -> None:
        result = _run_hook()
        lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        assert len(lines) <= 20, (
            f"Output exceeds 20-line budget: {len(lines)} lines\n{result.stdout}"
        )

    def test_contains_beads_section_header(self) -> None:
        """Output should contain a beads section when bd is available."""
        result = _run_hook()
        # If bd is on PATH, output should mention beads/work
        if subprocess.run(["which", "bd"], capture_output=True).returncode == 0:
            assert any(
                kw in result.stdout.lower()
                for kw in ("bead", "in-progress", "in_progress", "work")
            ), f"No beads section in output:\n{result.stdout}"

    def test_contains_scratch_section_header(self) -> None:
        """Output should contain a scratch section when nx is available."""
        result = _run_hook()
        if subprocess.run(["which", "nx"], capture_output=True).returncode == 0:
            assert any(
                kw in result.stdout.lower()
                for kw in ("scratch", "t1")
            ), f"No scratch section in output:\n{result.stdout}"

    def test_graceful_without_bd(self) -> None:
        """Script should not fail if bd is not on PATH."""
        result = _run_hook(env_overrides={"PATH": "/usr/bin:/bin"})
        assert result.returncode == 0

    def test_graceful_without_nx(self) -> None:
        """Script should not fail if nx is not on PATH."""
        result = _run_hook(env_overrides={"PATH": "/usr/bin:/bin"})
        assert result.returncode == 0

    def test_auto_trigger(self) -> None:
        """Script handles auto trigger identically."""
        payload = json.dumps({
            "session_id": "s", "hook_event_name": "PostCompact",
            "trigger": "auto", "compact_summary": "auto compact",
            "cwd": "/tmp", "transcript_path": "/tmp/t.jsonl",
        })
        result = _run_hook(stdin=payload)
        assert result.returncode == 0
