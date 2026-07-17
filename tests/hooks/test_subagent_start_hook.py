"""Tests for the SubagentStart hook script's session_id export (nexus-7o1zh)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus"
    / "hooks"
    / "scripts"
    / "subagent-start.sh"
)

STDIN_PAYLOAD = json.dumps({
    "session_id": "test-session",
    "hook_event_name": "SubagentStart",
    "task": "general research task",
    "prompt": "look into something",
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


class TestSubagentStartHook:
    def test_script_exists_and_is_executable(self) -> None:
        assert SCRIPT.exists(), f"Script not found: {SCRIPT}"
        assert os.access(SCRIPT, os.X_OK), f"Script not executable: {SCRIPT}"

    def test_exits_zero(self) -> None:
        result = _run_hook()
        assert result.returncode == 0

    def test_emits_json_envelope(self) -> None:
        result = _run_hook()
        payload = json.loads(result.stdout)
        assert payload["hookSpecificOutput"]["hookEventName"] == "SubagentStart"

    def test_orchestration_directive_rows_injected(self) -> None:
        """RDR-184 P1.3 (nexus-ccs9v.8): the THREE orchestration directive
        rows — Completion (Gap 1), Inbox (Gap 2), Git (Gap 4) — ride the
        live injection path into every subagent's initial context."""
        result = _run_hook()
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "| Completion |" in ctx
        assert "SendMessage full result to main BEFORE idling" in ctx
        assert "| Inbox |" in ctx
        assert "Re-check inbox right before composing any hand-back" in ctx
        assert "| Git |" in ctx
        assert "NEVER git add/commit" in ctx
        assert "orchestrator commits pathspec-limited" in ctx

    def test_heredoc_bodies_respect_deadlock_ceiling(self) -> None:
        """The file's own rule: heredoc bodies stay under 500 bytes (bash
        5.3.x pipe deadlock). Guard the NEW ORCH heredoc mechanically;
        pre-existing PHASE_GATE (540 bytes) is grandfathered until its
        owner trims it."""
        import re

        src = SCRIPT.read_text()
        m = re.search(r"cat <<'ORCH'\n(.*?)\nORCH\n", src, re.S)
        assert m is not None, "ORCH heredoc missing from subagent-start.sh"
        assert len(m.group(1).encode()) < 500


class TestSessionIdExport:
    """nexus-7o1zh: this hook runs detached from any live nx-mcp process and
    cannot rely on env-var inheritance from a parent Claude session. It must
    extract ``session_id`` from its own stdin JSON payload and export it as
    ``NX_SESSION_ID`` before invoking ``nx scratch list`` (the "Inject
    current T1 scratch entries" section), so the CLI resolves the CORRECT
    session's T1 data instead of falling through to the machine-wide (and
    possibly clobbered-by-a-sibling-session) ``current_session`` flat file
    (nexus-36q84's collision, same class)."""

    @staticmethod
    def _make_fake_nx(tmp_path: Path) -> Path:
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        nx_script = fake_bin / "nx"
        nx_script.write_text(
            "#!/bin/bash\n"
            'echo "NX_SESSION_ID=${NX_SESSION_ID:-<unset>}" >> "$NX_CALL_LOG"\n'
            'echo "no scratch entries"\n'
            "exit 0\n"
        )
        nx_script.chmod(0o755)
        return fake_bin

    def test_exports_session_id_from_stdin_payload(self, tmp_path) -> None:
        fake_bin = self._make_fake_nx(tmp_path)
        log_file = tmp_path / "nx_calls.log"

        result = _run_hook(
            env_overrides={
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "NX_CALL_LOG": str(log_file),
            },
        )

        assert result.returncode == 0
        log_contents = log_file.read_text() if log_file.exists() else ""
        assert "NX_SESSION_ID=test-session" in log_contents, log_contents

    def test_missing_session_id_in_payload_preserves_ambient_env(
        self, tmp_path
    ) -> None:
        fake_bin = self._make_fake_nx(tmp_path)
        log_file = tmp_path / "nx_calls.log"

        payload = json.dumps({
            "hook_event_name": "SubagentStart",
            "task": "no session_id field",
        })

        result = _run_hook(
            stdin=payload,
            env_overrides={
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "NX_CALL_LOG": str(log_file),
                "NX_SESSION_ID": "pre-existing-ambient-value",
            },
        )

        assert result.returncode == 0
        log_contents = log_file.read_text() if log_file.exists() else ""
        assert "NX_SESSION_ID=pre-existing-ambient-value" in log_contents, log_contents
