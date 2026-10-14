# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fire every command-tier hooks.json entry against one installed CLI.

Usage: drive.py <hooks.json> <plugin-root> <cli-bin-dir-or-"none"> <label> <scratch-dir> [<min-executed>]

Runs each entry the way Claude Code does: exec form, the entry's own argv with
${CLAUDE_PLUGIN_ROOT} expanded, a small event payload on stdin, PATH holding
only that CLI's bin dir, a directory holding `uv` alone, and the system dirs,
a scratch HOME and config dir.

Every conexus entry starts with `uv tool run ... python launch/nx_launch.py
<program>` (RDR-228), so the CLI under test is found by the LAUNCHER, not by
the entry: NX_TOOLS_DIR names an empty directory, so the launcher's generation
layout leg finds nothing and its PATH leg finds <cli-bin-dir>. Without that, the
launcher would find this machine's own install and the run would test it.

An entry that did not START is a failure, never a pass. The driver used to
count "command not found" as non-blocking; once every command became `uv`, a
PATH without `uv` made all entries "not found" and the gate printed PASSED
having run none (found 2026-10-13). Two guards: (1) any entry whose process
could not be started fails; (2) fewer than <min-executed> entries started fails
(default DEFAULT_MIN_EXECUTED), so a manifest that lost its entries, or a walk
that skipped them, is not a pass either. Raise the default when entries are added.
Under a CLI the launcher must also have FOUND it (no "is not installed" line),
and under no CLI it must have said so, once per launcher entry: that is the
proof the launcher, and not just `uv`, ran.
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
import shutil

#: Entries every driven hooks.json must start at minimum: the 15 command-tier
#: entries of conexus/hooks/hooks.json (RDR-228 Phase 1). A stated floor, not
#: a derived one, on purpose: a derived count falls with the manifest.
DEFAULT_MIN_EXECUTED = 15

#: What the launcher writes when it finds no CLI (_cli_lookup.missing_note).
_MISSING_MARK = "is not installed"

#: Events whose stdout Claude Code parses as a JSON decision.
_DECISION_EVENTS = frozenset({"PreToolUse", "PermissionRequest", "PostToolUse", "Stop", "SubagentStop"})
import subprocess
import sys
from pathlib import Path


def _uv_only_dir(scratch: str) -> str | None:
    """A directory holding a symlink to the `uv` this process can see, and
    nothing else, so the driven PATH has `uv` and not the rest of the caller's
    PATH (which holds this machine's own nx programs)."""
    uv = shutil.which("uv")
    if uv is None:
        return None
    d = os.path.join(scratch, "uvbin")
    os.makedirs(d, exist_ok=True)
    link = os.path.join(d, "uv")
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(os.path.realpath(uv), link)
    return d


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
    hooks_path, plugin_root, cli_bin, label, scratch, *rest = argv
    min_executed = int(rest[0]) if rest else DEFAULT_MIN_EXECUTED
    Path(scratch, "transcript.jsonl").touch()
    data = json.loads(Path(hooks_path).read_text())
    uv_dir = _uv_only_dir(scratch)
    if uv_dir is None:
        print(f"SKEW-DRIVE {label}: `uv` is not on PATH, so no entry can start; this is not a pass")
        return 1
    # An empty generation root: the launcher's layout leg finds nothing, so its
    # PATH leg finds the CLI under test (or none).
    tools = os.path.join(scratch, "empty-tools")
    os.makedirs(tools, exist_ok=True)
    path = ":".join(([] if cli_bin == "none" else [cli_bin]) + [uv_dir, "/usr/bin", "/bin"])
    env = {
        "PATH": path,
        "NX_TOOLS_DIR": tools,
        "HOME": os.path.join(scratch, "home"),
        "NEXUS_CONFIG_DIR": os.path.join(scratch, "home", ".config", "nexus"),
        "CLAUDE_PLUGIN_ROOT": plugin_root,
        "NX_NO_TELEMETRY": "1",
        "TERM": "dumb",
    }
    # uv's caches live under the caller's, not the scratch HOME: a fresh HOME
    # per CLI would re-resolve and may re-download an interpreter every time.
    for passthrough in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR"):
        if os.environ.get(passthrough):
            env[passthrough] = os.environ[passthrough]
    os.makedirs(env["NEXUS_CONFIG_DIR"], exist_ok=True)
    driven = started = failures = 0
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
                    # Not a pass: an entry that never started proves nothing about
                    # the CLI. Every command in a conexus manifest is `uv`, which
                    # this driver puts on PATH, so none is absent by design.
                    failures += 1
                    print(f"  FAIL  [{event}] {name}: did not start; command {entry['command']!r} not found on the driven PATH")
                    continue
                except subprocess.TimeoutExpired:
                    print(f"  ok    [{event}] {name}: timed out (non-blocking)")
                    continue
                started += 1
                out = r.stdout.strip()
                bad = None
                launcher = any(a.endswith("launch/nx_launch.py") for a in argv_)
                missing = _MISSING_MARK in r.stderr.decode(errors="replace")
                if r.returncode == 2:
                    bad = f"exit 2 (blocks): {r.stderr.decode(errors='replace').strip()[:160]}"
                elif launcher and cli_bin != "none" and missing:
                    bad = f"the launcher did not find the CLI under test: {r.stderr.decode(errors='replace').strip()[:160]}"
                elif launcher and cli_bin == "none" and not missing:
                    bad = "no CLI, and the launcher did not say so: it may not have run"
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
    print(f"SKEW-DRIVE {label}: {driven} entries, {started} started, {failures} failures")
    if started < min_executed:
        print(f"SKEW-DRIVE {label}: only {started} entries started, fewer than the floor of {min_executed}; the walk is blind")
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
