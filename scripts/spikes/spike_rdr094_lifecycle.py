#!/usr/bin/env -S uv run python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-094 Critical Assumption #1 + #3 lifecycle probe.

Spike A from RDR-094 §Implementation Plan > Prerequisites. Validates
the chroma-cleanup completion rate by source for four phases:

  1. Clean shutdown via SIGTERM to nx-mcp.
       Expect: FastMCP lifespan finally fires (anyio cancellation
       through async finally, RDR-094 §Approach), chroma exits
       within seconds, no watchdog action.
       Target: lifespan covers >=90% of runs.

  2. SIGKILL to nx-mcp (no lifespan, no atexit).
       Expect: dual-watch watchdog (--mcp-pid trigger) detects mcp_pid
       gone within POLL_INTERVAL=5s, pgrp-signals chroma.
       Target: watchdog covers >=95% of runs, chroma cleanup <=5s.

  3. Simulated OOM via SIGSEGV.
       Expect: same as SIGKILL (signal that bypasses Python handlers).
       Target: same as SIGKILL.

  4. Claude-crash simulation (RDR-094 FM-NEW-1, issue #1935).
       The harness acts as the "Claude" parent that spawns nx-mcp;
       killing the harness mid-run simulates Claude Code dying with
       nx-mcp still alive.
       Expect: dual-watch watchdog (--claude-pid trigger) sends
       SIGTERM to mcp_pid, mcp's lifespan finally runs, chroma exits
       within MCP_GRACE_SECS=2s + POLL_INTERVAL=5s.
       Target: claude-crash path covers >=95% of runs.

Each run records: phase, run_id, mcp_pid, chroma_pid, signal sent,
chroma_alive_after_grace (bool), cleanup_path (one of: lifespan,
atexit, watchdog_mcp, watchdog_claude, none), elapsed_until_cleanup.

cleanup_path attribution comes from mcp.log: events
``mcp_server_stopping`` (with reason='exit' meaning lifespan fired)
or ``mcp_server_crashed`` (lifespan did not fire) discriminate the
graceful path from the fatal-signal path. ``sweep_reaped_orphan_*``
events from the next session start would attribute to the sweep, but
this harness does not start a follow-on session within a single run.

Per-phase API budget: $0 (no claude_dispatch). Wall-clock per run:
~10s (5s poll + 2s grace + 3s slack). Default 5 runs/phase = 20 runs
= ~3-4 minutes total. Pass --runs 10 for the full 40-run protocol
that the RDR's Critical Assumption #1+#3 target.

Usage::

    NEXUS_MCP_OWNS_T1=1 python scripts/spikes/spike_rdr094_lifecycle.py [--runs N]

Output: scripts/spikes/spike_rdr094_results.jsonl (per-run records)
      + scripts/spikes/spike_rdr094_summary.json (aggregate).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_PATH = SCRIPT_DIR / "spike_rdr094_results.jsonl"
SUMMARY_PATH = SCRIPT_DIR / "spike_rdr094_summary.json"

#: Where nx-mcp writes its lifecycle log. Honors NEXUS_CONFIG_DIR.
MCP_LOG_PATH = Path(
    os.environ.get(
        "NEXUS_CONFIG_DIR",
        os.path.expanduser("~/.config/nexus"),
    )
) / "logs" / "mcp.log"

#: Time the harness gives the system to spawn chroma after starting
#: nx-mcp before the harness considers the run a setup failure.
SPAWN_TIMEOUT_S: float = 8.0

#: Time the harness waits after sending the kill signal before
#: checking chroma liveness. Covers the watchdog poll (5s) plus the
#: MCP grace period (2s) plus 1s slack for fs flushes.
CLEANUP_TIMEOUT_S: float = 10.0


@dataclass
class RunRecord:
    phase: str
    run_id: int
    mcp_pid: int
    chroma_pid: int
    signal_sent: str
    chroma_alive_after_grace: bool
    cleanup_path: str
    elapsed_until_cleanup_s: float
    setup_failed: bool
    error: Optional[str]


def _is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _spawn_nx_mcp(env_overrides: Optional[dict] = None) -> subprocess.Popen:
    """Spawn nx-mcp as a child process under this harness.

    The harness becomes the "Claude" parent for the FM-NEW-1 simulation.
    Each run gets its own process group via ``start_new_session=True``
    so we can kill nx-mcp without affecting the harness when needed.

    Inherits the harness's stdio so any pre-lifespan errors are visible.
    Overrides ``NEXUS_MCP_OWNS_T1=1`` (forced) and merges any caller
    overrides.
    """
    env = {**os.environ, "NEXUS_MCP_OWNS_T1": "1"}
    if env_overrides:
        env.update(env_overrides)
    return subprocess.Popen(
        [sys.executable, "-m", "nexus.mcp.core"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )


def _wait_for_chroma_pid(timeout: float) -> Optional[int]:
    """Poll the active session record for the chroma server_pid.

    The MCP server's _t1_chroma_init_if_owner writes the record before
    yielding from the lifespan; we tail the sessions directory for a
    record whose write mtime is newer than the harness start.
    """
    from nexus.db.t1 import SESSIONS_DIR

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if SESSIONS_DIR.exists():
            for f in sorted(
                SESSIONS_DIR.glob("*.session"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            ):
                try:
                    rec = json.loads(f.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                pid = rec.get("server_pid")
                if isinstance(pid, int) and pid > 0 and _is_alive(pid):
                    return pid
        time.sleep(0.1)
    return None


def _classify_cleanup_path(
    log_lines_after: list[str], chroma_alive: bool, signal_sent: str,
) -> str:
    """Inspect mcp.log lines emitted after the kill signal and
    attribute the cleanup path.

    Heuristics in priority order:
      * mcp_server_stopping reason='exit' => lifespan fired (graceful).
      * mcp_server_crashed => fatal signal, lifespan did not fire.
        If chroma is dead anyway, watchdog covered it.
      * No relevant log line + chroma dead => watchdog (no-emit path).
      * No relevant log line + chroma alive => none (regression).

    Watchdog mcp-pid vs claude-pid trigger discrimination is
    inferred from signal_sent (if SIGTERM came from harness, the
    harness was the "Claude" so claude-pid trigger; if SIGKILL or
    SIGSEGV against nx-mcp directly, mcp-pid trigger).
    """
    saw_lifespan_exit = any(
        "mcp_server_stopping" in line and "reason='exit'" in line
        for line in log_lines_after
    )
    saw_crash = any(
        "mcp_server_crashed" in line for line in log_lines_after
    )
    if saw_lifespan_exit:
        return "lifespan"
    if not chroma_alive:
        if saw_crash:
            return "watchdog_mcp"
        if signal_sent in ("SIGKILL", "SIGSEGV"):
            return "watchdog_mcp"
        if signal_sent == "harness_SIGKILL":
            return "watchdog_claude"
        return "watchdog_unknown"
    return "none"


def _read_log_tail_after(start_pos: int) -> list[str]:
    """Return mcp.log lines written after byte offset start_pos.

    Best-effort: file may not exist (logging not configured) or may
    have rotated; in either case return [].
    """
    if not MCP_LOG_PATH.exists():
        return []
    try:
        with MCP_LOG_PATH.open("r") as f:
            f.seek(start_pos)
            return f.readlines()
    except OSError:
        return []


def _log_size() -> int:
    try:
        return MCP_LOG_PATH.stat().st_size
    except OSError:
        return 0


def _run_phase_clean(run_id: int) -> RunRecord:
    """Phase 1: SIGTERM to nx-mcp, expect lifespan finally to clean."""
    log_start = _log_size()
    proc = _spawn_nx_mcp()
    chroma_pid = _wait_for_chroma_pid(SPAWN_TIMEOUT_S)
    if chroma_pid is None:
        # Setup failure: nx-mcp didn't spawn chroma.
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
        return RunRecord(
            phase="clean", run_id=run_id, mcp_pid=proc.pid,
            chroma_pid=0, signal_sent="SIGTERM",
            chroma_alive_after_grace=False, cleanup_path="setup_failed",
            elapsed_until_cleanup_s=0.0, setup_failed=True,
            error="chroma_pid not observed within SPAWN_TIMEOUT_S",
        )

    t0 = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=CLEANUP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass

    # Give the lifespan finally + watchdog a moment to settle the
    # filesystem before we measure.
    time.sleep(1.0)
    elapsed = time.monotonic() - t0
    chroma_alive = _is_alive(chroma_pid)
    log_lines = _read_log_tail_after(log_start)
    cleanup_path = _classify_cleanup_path(
        log_lines, chroma_alive, "SIGTERM",
    )
    if chroma_alive:
        # Cleanup the chroma we leaked so the harness can keep running.
        try:
            os.kill(chroma_pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return RunRecord(
        phase="clean", run_id=run_id, mcp_pid=proc.pid,
        chroma_pid=chroma_pid, signal_sent="SIGTERM",
        chroma_alive_after_grace=chroma_alive,
        cleanup_path=cleanup_path, elapsed_until_cleanup_s=round(elapsed, 2),
        setup_failed=False, error=None,
    )


def _run_phase_kill(run_id: int, kill_signal: int, label: str) -> RunRecord:
    """Phases 2 + 3: bypass the lifespan via SIGKILL or SIGSEGV.

    Watchdog --mcp-pid trigger is the expected cleanup path.
    """
    log_start = _log_size()
    proc = _spawn_nx_mcp()
    chroma_pid = _wait_for_chroma_pid(SPAWN_TIMEOUT_S)
    if chroma_pid is None:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
        return RunRecord(
            phase=label, run_id=run_id, mcp_pid=proc.pid,
            chroma_pid=0, signal_sent=label.upper(),
            chroma_alive_after_grace=False, cleanup_path="setup_failed",
            elapsed_until_cleanup_s=0.0, setup_failed=True,
            error="chroma_pid not observed within SPAWN_TIMEOUT_S",
        )

    t0 = time.monotonic()
    proc.send_signal(kill_signal)
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass
    # Wait for the dual-watch watchdog to detect mcp_pid gone (5s poll)
    # and clean chroma. CLEANUP_TIMEOUT_S has the slack budget.
    time.sleep(CLEANUP_TIMEOUT_S)
    elapsed = time.monotonic() - t0
    chroma_alive = _is_alive(chroma_pid)
    log_lines = _read_log_tail_after(log_start)
    cleanup_path = _classify_cleanup_path(
        log_lines, chroma_alive,
        "SIGKILL" if kill_signal == signal.SIGKILL else "SIGSEGV",
    )
    if chroma_alive:
        try:
            os.kill(chroma_pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return RunRecord(
        phase=label, run_id=run_id, mcp_pid=proc.pid,
        chroma_pid=chroma_pid,
        signal_sent="SIGKILL" if kill_signal == signal.SIGKILL else "SIGSEGV",
        chroma_alive_after_grace=chroma_alive,
        cleanup_path=cleanup_path, elapsed_until_cleanup_s=round(elapsed, 2),
        setup_failed=False, error=None,
    )


_INTERMEDIATE_HANDOFF = SCRIPT_DIR / "_spike_intermediate_handoff.json"


def _spawn_fake_claude_intermediate() -> subprocess.Popen:
    """Spawn an intermediate "fake claude" process that owns nx-mcp.

    The intermediate process:
      1. Spawns nx-mcp with NEXUS_MCP_OWNS_T1=1.
      2. Writes its own PID + nx-mcp's PID to a handoff file so the
         outer harness can read them.
      3. Sleeps forever (waiting to be killed).

    When the outer harness SIGKILLs the intermediate, the dual-watch
    watchdog (which has --claude-pid pointing at the intermediate)
    sees claude_root_pid disappear, sends SIGTERM to mcp_pid, and
    chroma is cleaned up by the lifespan finally inside nx-mcp.

    find_claude_root_pid uses the immediate-PPID fallback when no
    'claude' command-name ancestor is found. Since nx-mcp's PPID is
    the intermediate, the watchdog gets the intermediate's PID
    automatically without any rename trickery.
    """
    bootstrap = (
        "import json, os, subprocess, sys, time;\n"
        "env = dict(os.environ);\n"
        "env['NEXUS_MCP_OWNS_T1'] = '1';\n"
        "p = subprocess.Popen(\n"
        "    [sys.executable, '-m', 'nexus.mcp.core'],\n"
        "    stdin=subprocess.PIPE, stdout=subprocess.PIPE,\n"
        "    stderr=subprocess.PIPE, env=env,\n"
        ");\n"
        f"open({str(_INTERMEDIATE_HANDOFF)!r}, 'w').write(\n"
        "    json.dumps({'mcp_pid': p.pid, 'intermediate_pid': os.getpid()})\n"
        ");\n"
        "while True:\n"
        "    time.sleep(60)\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", bootstrap],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _run_phase_claude_crash(run_id: int) -> RunRecord:
    """Phase 4 (RDR-094 FM-NEW-1, issue #1935): SIGKILL a "fake claude"
    intermediate that owns nx-mcp, observe whether the dual-watch
    watchdog's claude-pid trigger cleans chroma.

    Steps:
      1. Spawn an intermediate process that itself spawns nx-mcp
         (nx-mcp's PPID is the intermediate, so
         find_claude_root_pid's PPID-fallback resolves to the
         intermediate's PID even without a 'claude' command name).
      2. Wait for the intermediate to write nx-mcp's PID to a handoff
         file, then for nx-mcp to publish chroma's PID via the session
         record.
      3. SIGKILL the intermediate (simulates Claude Code crash with
         nx-mcp orphaned).
      4. Wait CLEANUP_TIMEOUT_S for the watchdog: claude-pid trigger
         fires within POLL_INTERVAL=5s, sends SIGTERM to nx-mcp,
         lifespan finally runs (~MCP_GRACE_SECS=2s), chroma exits.
      5. Check chroma liveness; classify cleanup path.
    """
    log_start = _log_size()
    if _INTERMEDIATE_HANDOFF.exists():
        _INTERMEDIATE_HANDOFF.unlink()

    intermediate = _spawn_fake_claude_intermediate()
    intermediate_pid = intermediate.pid

    # Wait for the intermediate to publish nx-mcp's PID.
    deadline = time.monotonic() + SPAWN_TIMEOUT_S
    handoff: dict[str, int] | None = None
    while time.monotonic() < deadline and handoff is None:
        if _INTERMEDIATE_HANDOFF.exists():
            try:
                handoff = json.loads(_INTERMEDIATE_HANDOFF.read_text())
            except (OSError, json.JSONDecodeError):
                pass
        if handoff is None:
            time.sleep(0.1)
    if handoff is None:
        try:
            intermediate.kill()
            intermediate.wait(timeout=2)
        except Exception:
            pass
        return RunRecord(
            phase="claude_crash", run_id=run_id,
            mcp_pid=0, chroma_pid=0, signal_sent="(setup-failed)",
            chroma_alive_after_grace=False, cleanup_path="setup_failed",
            elapsed_until_cleanup_s=0.0, setup_failed=True,
            error="intermediate handoff file never appeared",
        )

    mcp_pid = int(handoff.get("mcp_pid", 0))
    chroma_pid = _wait_for_chroma_pid(SPAWN_TIMEOUT_S)
    if chroma_pid is None or mcp_pid == 0:
        try:
            intermediate.kill()
            intermediate.wait(timeout=2)
        except Exception:
            pass
        if mcp_pid and _is_alive(mcp_pid):
            try:
                os.kill(mcp_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        return RunRecord(
            phase="claude_crash", run_id=run_id,
            mcp_pid=mcp_pid, chroma_pid=0,
            signal_sent="(setup-failed)",
            chroma_alive_after_grace=False, cleanup_path="setup_failed",
            elapsed_until_cleanup_s=0.0, setup_failed=True,
            error="chroma_pid not observed within SPAWN_TIMEOUT_S",
        )

    t0 = time.monotonic()
    # SIGKILL the intermediate (simulates Claude Code crash).
    try:
        os.kill(intermediate_pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    intermediate.wait(timeout=2)

    # Wait for the watchdog's claude-pid trigger:
    #   POLL_INTERVAL=5s + MCP_GRACE_SECS=2s + slack = CLEANUP_TIMEOUT_S
    time.sleep(CLEANUP_TIMEOUT_S)
    elapsed = time.monotonic() - t0
    chroma_alive = _is_alive(chroma_pid)
    log_lines = _read_log_tail_after(log_start)
    cleanup_path = _classify_cleanup_path(
        log_lines, chroma_alive, "harness_SIGKILL",
    )
    # Belt-and-braces cleanup of any leak so the next run starts clean.
    if chroma_alive:
        try:
            os.kill(chroma_pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if mcp_pid and _is_alive(mcp_pid):
        try:
            os.kill(mcp_pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if _INTERMEDIATE_HANDOFF.exists():
        _INTERMEDIATE_HANDOFF.unlink()

    return RunRecord(
        phase="claude_crash", run_id=run_id,
        mcp_pid=mcp_pid, chroma_pid=chroma_pid,
        signal_sent="harness_SIGKILL",
        chroma_alive_after_grace=chroma_alive,
        cleanup_path=cleanup_path,
        elapsed_until_cleanup_s=round(elapsed, 2),
        setup_failed=False, error=None,
    )


def _aggregate(records: list[RunRecord]) -> dict[str, Any]:
    by_phase: dict[str, list[RunRecord]] = {}
    for r in records:
        by_phase.setdefault(r.phase, []).append(r)

    summary: dict[str, Any] = {"phases": {}, "totals": {}}
    grand_total = 0
    grand_clean = 0
    for phase, recs in by_phase.items():
        n = len(recs)
        setup_failed = sum(1 for r in recs if r.setup_failed)
        usable = [r for r in recs if not r.setup_failed]
        chroma_dead = sum(
            1 for r in usable if not r.chroma_alive_after_grace
        )
        path_counts: dict[str, int] = {}
        for r in usable:
            path_counts[r.cleanup_path] = path_counts.get(r.cleanup_path, 0) + 1
        summary["phases"][phase] = {
            "runs": n,
            "setup_failed": setup_failed,
            "usable": len(usable),
            "chroma_cleaned": chroma_dead,
            "cleanup_rate": (
                round(chroma_dead / len(usable), 3) if usable else None
            ),
            "path_counts": path_counts,
            "median_elapsed_s": (
                round(
                    sorted(r.elapsed_until_cleanup_s for r in usable)[
                        len(usable) // 2
                    ],
                    2,
                )
                if usable
                else None
            ),
        }
        grand_total += n
        grand_clean += chroma_dead
    summary["totals"] = {
        "runs": grand_total,
        "chroma_cleaned": grand_clean,
        "overall_cleanup_rate": (
            round(grand_clean / grand_total, 3) if grand_total else None
        ),
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="RDR-094 Spike A: T1 chroma lifecycle probe.",
    )
    parser.add_argument(
        "--runs", type=int, default=5,
        help="Runs per phase (default 5; RDR target is 10).",
    )
    parser.add_argument(
        "--phases", type=str,
        default="clean,mcp_sigkill,mcp_oom,claude_crash",
        help=(
            "Comma-separated phase list. Default runs all four. "
            "Use --phases clean for a quick lifespan-only smoke."
        ),
    )
    args = parser.parse_args()

    phases = [p.strip() for p in args.phases.split(",") if p.strip()]

    results: list[RunRecord] = []
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("a") as out:
        for phase in phases:
            for run_id in range(args.runs):
                if phase == "clean":
                    rec = _run_phase_clean(run_id)
                elif phase == "mcp_sigkill":
                    rec = _run_phase_kill(run_id, signal.SIGKILL, "mcp_sigkill")
                elif phase == "mcp_oom":
                    rec = _run_phase_kill(run_id, signal.SIGSEGV, "mcp_oom")
                elif phase == "claude_crash":
                    rec = _run_phase_claude_crash(run_id)
                else:
                    print(f"unknown phase: {phase}", file=sys.stderr)
                    continue
                out.write(json.dumps(asdict(rec)) + "\n")
                out.flush()
                results.append(rec)
                print(
                    f"[{phase}/{run_id}] cleanup_path={rec.cleanup_path} "
                    f"chroma_alive={rec.chroma_alive_after_grace} "
                    f"elapsed={rec.elapsed_until_cleanup_s:.2f}s",
                    flush=True,
                )

    summary = _aggregate(results)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
