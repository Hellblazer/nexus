# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The sandbox's private tmux server as work its creator's identity owns (RDR-253 Step 6, nexus-hczdo.1.9).

run.py loads this file by path (``_load_slash("sandbox_work")``), as it loads slash_restart.py: it runs under ``python -I``, which leaves
its own directory off sys.path. The sandbox starts one ``tmux -L <sock>`` server per run. The run registers it
(``nx identity work register --kind tmux --ref <sock>``) so that no other session's cleanup stops it, and its own cleanup
asks ``nx identity may-stop`` before ``kill-server``: ownership comes from the work row and liveness from the owner's
holder check, never from the socket's name pattern. After the kill the row is marked done.

``nx`` is the run's ``real_nx`` and runs in the OPERATOR's environment (this process's), not the sandbox's scratch HOME: the identity is the operator's
session, whose token file the process finds through its Claude ancestor. A process with no identity, or no ``nx`` on PATH,
registers nothing and is never refused (exit 0 from the verbs); a check that cannot run is a note, not a refusal: the run
must not leave a credential-bearing server behind because an engine was down. Nothing here raises.
"""
from __future__ import annotations

import subprocess

TIMEOUT_S = 30


def _nx(nx: str, *args: str) -> subprocess.CompletedProcess | None:
    """``nx identity ...`` with the run's own ``nx`` (``Sandbox.real_nx``: the one on PATH, "" when none), so a test that
    stages ``real_nx = ""`` asks no engine anything."""
    if not nx:
        return None
    try:
        return subprocess.run([nx, "identity", *args], capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def register(nx: str, sock: str) -> None:
    """The run's private server is work its identity owns."""
    _nx(nx, "work", "register", "--kind", "tmux", "--ref", sock)


#: ``nx identity may-stop``'s exit status for "a live identity owns it" (``nexus.identity.work.EXIT_OWNED``); 1 is every
#: other failure, the engine not answering included. This file runs under ``python -I`` with no ``nexus`` on its path, so the
#: number is spelled here and pinned against the real command in tests/identity/test_work_registry.py.
EXIT_OWNED = 3


def refusal(nx: str, sock: str) -> str:
    """"" when the server may be stopped; else the owner's refusal. Keyed on the exit status, never on the wording."""
    done = _nx(nx, "may-stop", "--kind", "tmux", "--ref", sock)
    if done is None or done.returncode != EXIT_OWNED:
        return ""
    return (done.stderr or done.stdout).strip() or f"tmux {sock} is owned by a live identity"


def kill_server(sb, notes: list[str]) -> None:
    """``tmux kill-server`` on the sandbox's server unless a live identity other than this run owns it; then mark the row
    done. *sb* has ``sock``, ``real_nx`` and ``tmux(*args, timeout=)``."""
    why = refusal(sb.real_nx, sb.sock)
    if why:
        notes.append(f"tmux kill-server NOT sent: {why}")
        return
    try:
        sb.tmux("kill-server", timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        notes.append("tmux kill-server timed out")
    _nx(sb.real_nx, "work", "done", "--kind", "tmux", "--ref", sb.sock)
