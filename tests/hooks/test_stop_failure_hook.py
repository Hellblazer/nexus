"""Tests for the StopFailure hook script."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "nx" / "hooks" / "scripts" / "stop_failure_hook.py"

FAILURE_TYPES = [
    "rate_limit",
    "authentication_failed",
    "billing_error",
    "invalid_request",
    "server_error",
    "max_output_tokens",
    "unknown",
]


def _make_payload(error: str, details: str = "test details") -> str:
    return json.dumps({
        "session_id": "test-session",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp",
        "hook_event_name": "StopFailure",
        "error": error,
        "error_details": details,
        "last_assistant_message": "I was working on...",
    })


def _run_hook(
    stdin: str,
    *,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": os.environ.get("PATH", ""),
        **(env_overrides or {}),
    }
    return subprocess.run(
        ["python3", str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )


class TestStopFailureHook:
    """StopFailure hook script tests."""

    def test_script_exists(self) -> None:
        assert SCRIPT.exists(), f"Script not found: {SCRIPT}"

    @pytest.mark.parametrize("error_type", FAILURE_TYPES)
    def test_exits_zero_for_all_failure_types(self, error_type: str) -> None:
        result = _run_hook(_make_payload(error_type))
        assert result.returncode == 0, (
            f"Non-zero exit for {error_type}: stderr={result.stderr}"
        )

    def test_exits_zero_on_malformed_json(self) -> None:
        result = _run_hook("not valid json {{{")
        assert result.returncode == 0

    def test_exits_zero_on_empty_stdin(self) -> None:
        result = _run_hook("")
        assert result.returncode == 0

    def test_exits_zero_on_missing_error_field(self) -> None:
        result = _run_hook(json.dumps({"session_id": "s", "hook_event_name": "StopFailure"}))
        assert result.returncode == 0

    def test_graceful_without_bd(self) -> None:
        """Script should not fail if bd is not on PATH."""
        result = _run_hook(
            _make_payload("rate_limit"),
            env_overrides={"PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0

    def test_rate_limit_logs_remember(self) -> None:
        """rate_limit should attempt bd remember."""
        result = _run_hook(_make_payload("rate_limit"))
        # Script runs bd remember; if bd not available, it gracefully skips.
        # We verify exit 0 — side effect tested via integration.
        assert result.returncode == 0

    def test_unknown_error_type_handled(self) -> None:
        """Completely unknown error values should still be handled."""
        result = _run_hook(_make_payload("some_future_error_type"))
        assert result.returncode == 0

    def test_null_error_details(self) -> None:
        """error_details may be null in JSON — must not crash on None[:200]."""
        payload = json.dumps({
            "session_id": "s",
            "hook_event_name": "StopFailure",
            "error": "rate_limit",
            "error_details": None,
        })
        result = _run_hook(payload)
        assert result.returncode == 0

    def test_skips_side_effects_without_claudecode_env(self) -> None:
        """Without CLAUDECODE=1, script must not call bd (no junk beads)."""
        # Ensure CLAUDECODE is NOT set
        result = _run_hook(
            _make_payload("rate_limit"),
            env_overrides={"CLAUDECODE": "", "NX_HOOK_DEBUG": "1"},
        )
        assert result.returncode == 0
        assert "skipping side effects" in result.stderr.lower()
