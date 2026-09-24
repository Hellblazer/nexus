# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fire every command-tier hooks.json entry against one installed CLI.

Usage: drive.py <hooks.json> <plugin-root> <cli-bin-dir-or-"none"> <label> <scratch-dir>

Runs each entry the way Claude Code does: exec form, the entry's own argv with
${CLAUDE_PLUGIN_ROOT} expanded, a small event payload on stdin, PATH holding
only that CLI's bin dir plus the system dirs, a scratch HOME and config dir.
Exit 2 is what blocks a session on UserPromptSubmit, PreToolUse and
PermissionRequest, so any entry exiting 2 is a failure. On a decision event
(PreToolUse, PermissionRequest, PostToolUse, Stop, SubagentStop) Claude Code
reads stdout as the hook's JSON decision, so there non-empty stdout that is not
JSON is a failure too; on SessionStart and UserPromptSubmit plain text is how a
hook adds context, and SessionEnd output is not read, so those are exempt.
A timeout is reported but is not a failure: Claude Code treats it as a
non-blocking error. mcp_tool entries are out of scope (they depend on the MCP
server, not on `nx-hook`).

Prints one line per entry and a final `SKEW-DRIVE <label>: <n> entries, <f>
failures`; exits 1 on any failure, 0 otherwise.
"""
from __future__ import annotations

import json
import os

#: Events whose stdout Claude Code parses as a JSON decision.
_DECISION_EVENTS = frozenset({"PreToolUse", "PermissionRequest", "PostToolUse", "Stop", "SubagentStop"})
import subprocess
import sys
from pathlib import Path


def _payload(event: str, scratch: str) -> bytes:
    body = {
        "hook_event_name": event,
        "session_id": "hook-cli-skew-gate",
        "transcript_path": os.path.join(scratch, "transcript.jsonl"),
        "cwd": scratch,
        "source": "startup",
        "prompt": "hello",
        "tool_name": "Bash",
        "tool_input": {"command": "true"},
        "agent_id": "askewgate0000000",
        "agent_type": "Explore",
        "stop_hook_active": False,
    }
    if event in ("PreToolUse", "PermissionRequest"):
        body["tool_name"] = "Bash"
    return json.dumps(body).encode()


def main(argv: list[str]) -> int:
    hooks_path, plugin_root, cli_bin, label, scratch = argv
    Path(scratch, "transcript.jsonl").touch()
    data = json.loads(Path(hooks_path).read_text())
    path = "/usr/bin:/bin" if cli_bin == "none" else f"{cli_bin}:/usr/bin:/bin"
    env = {
        "PATH": path,
        "HOME": os.path.join(scratch, "home"),
        "NEXUS_CONFIG_DIR": os.path.join(scratch, "home", ".config", "nexus"),
        "CLAUDE_PLUGIN_ROOT": plugin_root,
        "NX_NO_TELEMETRY": "1",
        "TERM": "dumb",
    }
    os.makedirs(env["NEXUS_CONFIG_DIR"], exist_ok=True)
    driven = failures = 0
    for event, groups in data["hooks"].items():
        for group in groups:
            for entry in group.get("hooks", []):
                if entry.get("type") != "command":
                    continue
                argv_ = [entry["command"], *[a.replace("${CLAUDE_PLUGIN_ROOT}", plugin_root) for a in entry.get("args", [])]]
                name = " ".join(a.rsplit("/", 1)[-1] for a in argv_)
                driven += 1
                try:
                    r = subprocess.run(
                        argv_, input=_payload(event, scratch), capture_output=True,
                        env=env, cwd=scratch, timeout=entry.get("timeout", 10) + 5, check=False,
                    )
                except FileNotFoundError:
                    print(f"  ok    [{event}] {name}: command not found (non-blocking)")
                    continue
                except subprocess.TimeoutExpired:
                    print(f"  ok    [{event}] {name}: timed out (non-blocking)")
                    continue
                out = r.stdout.strip()
                bad = None
                if r.returncode == 2:
                    bad = f"exit 2 (blocks): {r.stderr.decode(errors='replace').strip()[:160]}"
                elif out and event in _DECISION_EVENTS:
                    try:
                        json.loads(out)
                    except ValueError:
                        bad = f"stdout is not JSON: {out[:120]!r}"
                if bad:
                    failures += 1
                    print(f"  FAIL  [{event}] {name}: {bad}")
                else:
                    print(f"  ok    [{event}] {name}: rc={r.returncode}")
    print(f"SKEW-DRIVE {label}: {driven} entries, {failures} failures")
    if driven == 0:
        print(f"SKEW-DRIVE {label}: drove nothing; the walk is broken")
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
