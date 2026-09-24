"""Tests for the PostCompact hook, ``nexus.hooks.post_compact``.

RDR-215 bead nexus-q02nx.19 ported this hook from
``conexus/hooks/scripts/post_compact_hook.sh``; bead .21 re-declared its
``hooks.json`` entry to the ``hook_post_compact`` mcp_tool, so the bash
script no longer runs in production and this file drives the Python
module only (nexus-q02nx.21).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

STDIN_PAYLOAD = json.dumps({
    "session_id": "test-session",
    "transcript_path": "/tmp/transcript.jsonl",
    "cwd": "/tmp",
    "permission_mode": "default",
    "hook_event_name": "PostCompact",
    "trigger": "manual",
    "compact_summary": "Summary of compacted conversation.",
})


#: A child process, not an in-process call: these tests vary PATH and the
#: environment per case, and ``os.environ`` is process-global.
_PY_DRIVER = """
import json, sys
from nexus._hook_runtime._io import never_fail
from nexus.hooks import post_compact as _hook

raw = sys.stdin.read()
try:
    payload = json.loads(raw) if raw.strip() else None
except Exception:
    payload = None
if not isinstance(payload, dict):
    payload = None
# nexus-zptvf: GUARD STDOUT while the verb runs. This verb spawns
# through bounded_subprocess.run_bounded, which emits a structlog
# warning on TIMEOUT -- and structlog's unconfigured default
# PrintLoggerFactory writes to STDOUT, the channel this driver
# json.loads() below. Production is safe by a DIFFERENT route than
# the nx-hook verbs: hooks.json wires this one as an mcp_tool against
# nx-mcp, whose own configure_logging("mcp") runs before serving, so
# there is no entry.main fd guard here to inherit. A standing
# reviewer reproduced the corruption in this exact driver.
real_stdout = sys.stdout
sys.stdout = sys.stderr
try:
    result = never_fail(lambda: _hook.run(payload), "post_compact")
finally:
    sys.stdout = real_stdout
if result.stdout is not None:
    sys.stdout.write(result.stdout + "\\n")
sys.exit(result.exit_code)
"""


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
        [sys.executable, "-c", _PY_DRIVER],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )


class TestPostCompactHook:
    """PostCompact hook script tests."""

    def test_exits_zero(self) -> None:
        result = _run_hook()
        assert result.returncode == 0

    def test_output_under_20_lines(self) -> None:
        result = _run_hook()
        lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        assert len(lines) <= 20, (
            f"Output exceeds 20-line budget: {len(lines)} lines\n{result.stdout}"
        )

    def test_contains_beads_section_when_active(self) -> None:
        """Output should contain a beads section when bd has in-progress work."""
        result = _run_hook()
        if subprocess.run(["which", "bd"], capture_output=True).returncode == 0:
            active = subprocess.run(
                ["bd", "list", "--status=in_progress", "--limit=1"],
                capture_output=True, text=True,
            )
            has_active = active.returncode == 0 and "in_progress" in (active.stdout or "")
            if has_active:
                assert any(
                    kw in result.stdout.lower()
                    for kw in ("bead", "in-progress", "in_progress", "work")
                ), f"No beads section in output:\n{result.stdout}"

    def test_contains_scratch_section_when_entries_exist(self) -> None:
        """Output should contain a scratch section when nx has entries."""
        result = _run_hook()
        if subprocess.run(["which", "nx"], capture_output=True).returncode == 0:
            scratch = subprocess.run(
                ["nx", "scratch", "list"], capture_output=True, text=True,
            )
            has_entries = (
                scratch.returncode == 0
                and scratch.stdout.strip()
                and scratch.stdout.strip() != "No scratch entries."
            )
            if has_entries:
                assert any(
                    kw in result.stdout.lower()
                    for kw in ("scratch", "t1")
                ), f"No scratch section in output:\n{result.stdout}"

    def test_graceful_without_bd_or_nx(self) -> None:
        """Script should not fail if bd and nx are not on PATH."""
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


class TestSessionIdExport:
    """nexus-7o1zh: this hook runs detached from any live nx-mcp process and
    cannot rely on env-var inheritance from a parent Claude session. It must
    extract ``session_id`` from its own stdin JSON payload and export it as
    ``NX_SESSION_ID`` before invoking ``nx scratch list``, so the CLI
    resolves the CORRECT session's T1 data instead of falling through to the
    machine-wide (and possibly clobbered-by-a-sibling-session)
    ``current_session`` flat file (nexus-36q84's collision, same class)."""

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
        # STDIN_PAYLOAD's session_id is "test-session".
        assert "NX_SESSION_ID=test-session" in log_contents, log_contents

    def test_missing_session_id_in_payload_preserves_ambient_env(
        self, tmp_path
    ) -> None:
        fake_bin = self._make_fake_nx(tmp_path)
        log_file = tmp_path / "nx_calls.log"

        payload = json.dumps({
            "hook_event_name": "PostCompact",
            "trigger": "manual",
            "compact_summary": "no session_id field",
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
