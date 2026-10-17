#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PreToolUse probe that answers through the REAL ask envelope (nexus-mcfaj).

    ask_real.py <routing-dir> <log-file>

Same trigger and log line as probe_hook.sh, but the verdict is produced by
``conexus/hooks/scripts/routing/_lib.py::ask`` rather than a hand-written
JSON string, so a run proves the shipped envelope (hookEventName, reason,
no stray fields) is what Claude Code obeys.
"""
from __future__ import annotations

import sys
import time

routing_dir, log = sys.argv[1], sys.argv[2]
sys.path.insert(0, routing_dir)
import _lib  # noqa: E402

raw = sys.stdin.read()
if "HOOKASK_PROBE" not in raw:
    sys.exit(0)
who = "subagent" if '"agent_id"' in raw else "main"
with open(log, "a") as fh:
    fh.write(f"{int(time.time())} ask-real fired caller={who}\n")
_lib.ask("hook-ask-headless real ask_envelope (nexus-mcfaj)")
