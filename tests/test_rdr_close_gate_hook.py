# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nx/hooks/scripts/rdr_close_gate_hook.sh.

The hook is a PreToolUse gate on Edit/Write. It blocks frontmatter edits
that set `status: closed` on RDR files unless a matching `rdr-close-active`
T1 scratch marker is present. The marker is written ONLY by the /nx:rdr-close
command preamble at successful Pass 2 of the Problem Statement Replay gate.

This gate exists to prevent manual-walkthrough closes from assistant contexts
that bypass the Step 1.5 preamble and Step 1.75 fresh critic dispatch — the
silent-scope-reduction failure mode caught in RDR-069 Phase 4c and RDR-066
Phase 5c (both auto-closed manually before the hook shipped).

Tests use a subprocess invocation of the hook shell script with synthetic
JSON payloads matching the PreToolUse hook input contract.
"""
import json
import subprocess
from pathlib import Path

import pytest

HOOK_SCRIPT = (
    Path(__file__).parent.parent / "nx" / "hooks" / "scripts" / "rdr_close_gate_hook.sh"
)


def run_hook(payload: dict) -> dict:
    """Invoke the hook with a JSON payload and parse the hookSpecificOutput."""
    assert HOOK_SCRIPT.exists(), f"hook script missing: {HOOK_SCRIPT}"
    result = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"hook exited non-zero: {result.returncode}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip(), "hook produced no stdout"
    parsed = json.loads(result.stdout.strip())
    return parsed["hookSpecificOutput"]


def _payload(tool: str, file_path: str, content: str) -> dict:
    """Build a minimal PreToolUse payload for Edit or Write."""
    if tool == "Edit":
        return {
            "tool_name": "Edit",
            "tool_input": {"file_path": file_path, "new_string": content},
        }
    if tool == "Write":
        return {
            "tool_name": "Write",
            "tool_input": {"file_path": file_path, "content": content},
        }
    raise ValueError(f"unsupported tool: {tool}")


class TestRdrCloseGateHook:
    """Four scenarios covering the hook's decision matrix."""

    def test_non_rdr_file_is_allowed(self) -> None:
        """Edits to non-RDR files pass through unconditionally."""
        out = run_hook(_payload("Edit", "/tmp/foo.py", "print(1)"))
        assert out["permissionDecision"] == "allow"

    def test_rdr_file_without_status_closed_is_allowed(self) -> None:
        """Edits to RDR files that do NOT set status: closed pass through."""
        out = run_hook(
            _payload(
                "Edit",
                "/Users/hal.hildebrand/git/nexus/docs/rdr/rdr-099-example.md",
                "status: accepted",
            )
        )
        assert out["permissionDecision"] == "allow"

    def test_rdr_file_setting_status_closed_without_marker_is_denied(self) -> None:
        """The load-bearing case: status: closed without marker must deny."""
        # Use RDR-997 — unlikely to have a real marker in the session scratch.
        out = run_hook(
            _payload(
                "Edit",
                "/Users/hal.hildebrand/git/nexus/docs/rdr/rdr-997-synthetic.md",
                "status: closed",
            )
        )
        assert out["permissionDecision"] == "deny", (
            "hook should block status: closed edit without marker, "
            f"got: {out}"
        )
        assert "rdr_close_gate_hook" in out["reason"]
        assert "997" in out["reason"]
        assert "/nx:rdr-close" in out["reason"]

    def test_rdr_file_with_marker_is_allowed(self) -> None:
        """With the rdr-close-active marker present, the edit passes through."""
        # Seed a marker via nx scratch, run the hook, clean up.
        rdr_num = "998"
        seed = subprocess.run(
            ["nx", "scratch", "put", rdr_num, "--tags", f"rdr-close-active,rdr-{rdr_num}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert seed.returncode == 0, f"failed to seed marker: {seed.stderr}"
        # Extract the scratch entry ID for cleanup.
        stored_id = None
        for line in seed.stdout.splitlines():
            if line.startswith("Stored:"):
                stored_id = line.split(":", 1)[1].strip()
                break
        assert stored_id, f"could not parse scratch put output: {seed.stdout}"

        try:
            out = run_hook(
                _payload(
                    "Edit",
                    f"/Users/hal.hildebrand/git/nexus/docs/rdr/rdr-{rdr_num}-example.md",
                    "status: closed",
                )
            )
            assert out["permissionDecision"] == "allow", (
                f"hook should allow when marker present, got: {out}"
            )
        finally:
            subprocess.run(
                ["nx", "scratch", "delete", stored_id],
                capture_output=True,
                text=True,
                timeout=10,
            )

    def test_marker_is_rdr_specific(self) -> None:
        """A marker for rdr-999 must not unlock a close on rdr-997."""
        seed = subprocess.run(
            ["nx", "scratch", "put", "999", "--tags", "rdr-close-active,rdr-999"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        stored_id = None
        for line in seed.stdout.splitlines():
            if line.startswith("Stored:"):
                stored_id = line.split(":", 1)[1].strip()
                break
        assert stored_id

        try:
            out = run_hook(
                _payload(
                    "Edit",
                    "/Users/hal.hildebrand/git/nexus/docs/rdr/rdr-997-synthetic.md",
                    "status: closed",
                )
            )
            assert out["permissionDecision"] == "deny", (
                "marker for rdr-999 must not unlock rdr-997; "
                f"got: {out}"
            )
        finally:
            subprocess.run(
                ["nx", "scratch", "delete", stored_id],
                capture_output=True,
                text=True,
                timeout=10,
            )

    def test_write_tool_is_also_gated(self) -> None:
        """Write (not just Edit) is also covered by the gate."""
        out = run_hook(
            _payload(
                "Write",
                "/Users/hal.hildebrand/git/nexus/docs/rdr/rdr-996-synthetic.md",
                "---\nstatus: closed\n---\n# RDR-996",
            )
        )
        assert out["permissionDecision"] == "deny"

    def test_other_tool_names_pass_through(self) -> None:
        """Bash, Read, etc. are not gated by this hook."""
        out = run_hook(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo 'status: closed' > /tmp/rdr-066-fake.md"},
            }
        )
        assert out["permissionDecision"] == "allow"
