# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-process verdicts of the subagent git-write guard for a stream of commands.

The golden replay (``tests/test_audience_golden_replay.py``) runs this worker once
per guard tree, and compares the verdicts line by line:

    python golden_replay_worker.py <scripts-dir> <mode> [<account-home>] < cases.jsonl

*scripts-dir* is a ``conexus/hooks/scripts`` tree (the working copy, or a copy of
an older commit). *mode*:

``old``       a tree that has no ``_audience.py``; the guard runs as it did.
``new``       the working tree's guard; the account home is *account-home*
              (a flagged home makes the account a maintainer, an empty one
              leaves the repo marker to decide).

Each input line is ``{"command": ..., "cwd": ..., "agent_type": ...}``. Each output
line is ``{"verdict": "<stdout of the hook>", "maintainer": bool|null,
"nexus_repo": bool|null}``. The guard's git probes are cached per cwd and its
telemetry is stubbed, so a 200,000-line corpus runs in minutes: neither changes a
verdict, they only stop a spawn per line.
"""
from __future__ import annotations

import contextlib
import functools
import importlib.util
import io
import json
import os
import sys


def main(argv: list[str]) -> None:
    scripts_dir, mode = os.path.abspath(argv[1]), argv[2]
    os.environ["NX_HOOK_PYTHON"] = sys.executable
    if mode == "new":  # the working tree reads its exempt hosts from a file, never env
        os.environ.pop("NX_REMOTE_TEST_HOSTS", None)
    os.environ.pop("CLAUDE_PROJECT_DIR", None)
    sys.path.insert(0, scripts_dir)
    audience = None
    if mode == "new":
        import _audience as audience  # noqa: PLC0415

        audience._ACCOUNT_HOME_OVERRIDE = argv[3]
    guard_path = os.path.join(scripts_dir, "routing", "subagent_git_write_requires_orchestrator.py")
    spec = importlib.util.spec_from_file_location("replayed_guard", guard_path)
    assert spec and spec.loader
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    guard._lib.log_routing_event = lambda **kwargs: None
    for name in ("_in_linked_worktree", "_current_branch", "_is_linked_worktree_root"):
        setattr(guard, name, functools.cache(getattr(guard, name)))
    maint_cache: dict[str, tuple[bool, bool]] = {}
    for line in sys.stdin:
        case = json.loads(line)
        cwd = case.get("cwd") or ""
        payload = {
            "tool_name": "Bash", "tool_input": {"command": case["command"]},
            "agent_id": "areplay-1", "agent_type": case.get("agent_type") or "general-purpose",
            "session_id": "replay",
        }
        if cwd:
            payload["cwd"] = cwd
        maint: bool | None = None
        nexus: bool | None = None
        if audience is not None:
            if cwd not in maint_cache:
                maint_cache[cwd] = (audience.maintainer(payload), audience.nexus_repo(payload))
            maint, nexus = maint_cache[cwd]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                guard.body(payload)
            except SystemExit:
                pass
        sys.stdout.write(json.dumps({"verdict": out.getvalue(), "maintainer": maint, "nexus_repo": nexus}) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv)
