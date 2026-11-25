#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Env canary: does a project's ``.claude/settings.json`` ``env`` reach plugin
command hooks? (nexus-sby49.1, plugin-portability plan section 7 Phase 0.)

A scratch project sets ``env.NX_PROBE=<nonce>``. A fixture plugin, loaded for
one run with ``--plugin-dir``, registers SessionStart and PreToolUse(Bash)
command hooks that write ``os.environ.get("NX_PROBE")``, the OS-account home
(``pwd``, never ``$HOME``) and the payload cwd to ``<out>/<event>.txt``.

Run 1 is plain ``claude -p`` with one Bash ``true``; run 2 adds
``--setting-sources user`` (the project settings file is then not read, so
the nonce must be absent). Only run 1's PreToolUse file decides D11.

Usage (light, local, needs ``claude``)::

    uv run python tests/e2e/lib/plugin_env_canary.py [--scratch DIR] [--timeout S]

The scratch project is never the nexus checkout and the fixture plugin is
loaded by ``--plugin-dir``, so nothing under ``~/.claude`` is written by this
module. (The ``claude`` run itself reads the account's own settings and auth.)
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

PROBE_SCRIPT = '''\
import json, os, pwd, sys

out_dir, event = sys.argv[1], sys.argv[2]
try:
    payload = json.load(sys.stdin)
except Exception as exc:  # recorded, never fatal
    payload = {"_stdin_error": repr(exc)}
try:
    acct_home = pwd.getpwuid(os.getuid()).pw_dir
except Exception as exc:
    acct_home = "ERR:" + repr(exc)
lines = [
    "event=" + event,
    "NX_PROBE=" + repr(os.environ.get("NX_PROBE")),
    "account_home=" + acct_home,
    "env_HOME=" + repr(os.environ.get("HOME")),
    "CLAUDE_PROJECT_DIR=" + repr(os.environ.get("CLAUDE_PROJECT_DIR")),
    "payload_cwd=" + repr(payload.get("cwd")),
    "process_cwd=" + os.getcwd(),
]
with open(os.path.join(out_dir, event + ".txt"), "w") as fh:
    fh.write("\\n".join(lines) + "\\n")
'''


def build_plugin(plugin_dir: Path, out_dir: Path) -> None:
    """Write the fixture plugin; hook argv carries the output dir."""
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "scripts").mkdir(exist_ok=True)
    (plugin_dir / "hooks").mkdir(exist_ok=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "nx-canary", "version": "0.0.0",
                    "description": "env canary fixture (nexus-sby49.1)"})
    )
    script = plugin_dir / "scripts" / "probe.py"
    script.write_text(PROBE_SCRIPT)

    def hook(event: str) -> dict:
        return {"type": "command",
                "command": f"{sys.executable} {script} {out_dir} {event}",
                "timeout": 15}

    (plugin_dir / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {
        "SessionStart": [{"hooks": [hook("SessionStart")]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [hook("PreToolUse")]}],
    }}, indent=2))


def build_project(project_dir: Path, nonce: str) -> None:
    (project_dir / ".claude").mkdir(parents=True, exist_ok=True)
    (project_dir / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"NX_PROBE": nonce}}, indent=2))


def run_once(*, project_dir: Path, plugin_dir: Path, out_dir: Path,
             setting_sources: str | None, timeout: int) -> dict:
    """One ``claude -p`` run with a single Bash ``true``; returns the evidence."""
    out_dir.mkdir(parents=True, exist_ok=True)
    build_plugin(plugin_dir, out_dir)
    cmd = ["claude", "-p", "--plugin-dir", str(plugin_dir),
           "--allowedTools", "Bash", "--no-session-persistence",
           "--output-format", "text"]
    if setting_sources:
        cmd += ["--setting-sources", setting_sources]
    cmd += ["Use the Bash tool exactly once to run the command `true`, "
            "then reply with the single word done."]
    proc = subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True,
                          timeout=timeout, check=False)
    files = {p.name: p.read_text() for p in sorted(out_dir.glob("*.txt"))}
    return {"cmd": cmd, "returncode": proc.returncode,
            "stdout": proc.stdout, "stderr": proc.stderr[-2000:], "files": files}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scratch", type=Path, default=None,
                    help="scratch root (default: a fresh temp dir)")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args(argv)

    root = args.scratch or Path(tempfile.mkdtemp(prefix="nx-canary-"))
    root.mkdir(parents=True, exist_ok=True)
    nonce = "nx-probe-" + secrets.token_hex(8)
    project = root / "project"
    build_project(project, nonce)
    print(f"scratch={root}\nnonce={nonce}")

    verdict = {}
    for name, sources in (("run1", None), ("run2", "user")):
        res = run_once(project_dir=project, plugin_dir=root / f"plugin-{name}",
                       out_dir=root / f"out-{name}", setting_sources=sources,
                       timeout=args.timeout)
        print(f"\n===== {name}: {' '.join(res['cmd'][:-1])} <prompt>")
        print(f"returncode={res['returncode']} stdout={res['stdout']!r}")
        if res["stderr"]:
            print(f"stderr(tail)={res['stderr']!r}")
        for fname, text in res["files"].items():
            print(f"--- {fname}\n{text}", end="")
        pre = res["files"].get("PreToolUse.txt", "")
        verdict[name] = nonce in pre
        if not pre:
            print("!!! PreToolUse.txt absent: hook did not fire")
    print(f"\nproject_env_reaches_command_hooks(run1)={verdict['run1']}")
    print(f"nonce_in_run2_PreToolUse={verdict['run2']}")
    return 0 if verdict["run1"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
