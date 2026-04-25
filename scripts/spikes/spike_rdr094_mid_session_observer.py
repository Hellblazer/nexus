#!/usr/bin/env -S uv run python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-094 Spike C observer for issue #40207 (mid-session SIGTERM).

Run this script alongside a real Claude Code session to detect
whether Claude Code's MCP client sends mid-session SIGTERM to
nx-mcp. The probe is necessarily collaborative because the trigger
under investigation lives inside Claude Code itself, not in
anything this script can simulate.

## Usage

In one terminal::

    uv run python scripts/spikes/spike_rdr094_mid_session_observer.py \\
        --duration-min 30

In another terminal::

    NEXUS_MCP_OWNS_T1=1 claude

Then leave the Claude session idle for the duration. The observer
tails ~/.config/nexus/logs/mcp.log and watches for:

  * mcp_server_starting events (each tracks a fresh nx-mcp spawn)
  * mcp_server_stopping events with reason='signal' (SIGTERM /
    SIGINT received during the session)
  * mcp_server_crashed events (lifespan didn't fire)
  * Time gaps between consecutive starting events (restart cycle)

If issue #40207 applies to vanilla stdio servers like nx-mcp, the
observer will see start/stop pairs at decreasing intervals
(documented as 60s, 30s, 10s in the issue). If not, the start
should fire once and stay stopping-free for the full duration.

The observer also watches process state: it lists every nx-mcp
process every 30 seconds and notes its PID, parent PID, and uptime.
Restart cycles produce visible PID churn in the log.

## Output

  scripts/spikes/spike_rdr094_mid_session_log.jsonl
  scripts/spikes/spike_rdr094_mid_session_summary.json

Per-event records (jsonl):
  {timestamp, event_type, source, payload}

Summary (json):
  {
    duration_seconds,
    start_count,
    stop_count_signal,
    stop_count_exit,
    crash_count,
    pid_set,
    restart_cycles,  # observed start->stop->start within 90s
    interpretation: "no_mid_session_sigterm" | "issue_40207_confirmed"
                    | "inconclusive"
  }

## Decision rule

  * 0 mid-session signals over the duration => issue #40207 does NOT
    affect vanilla stdio servers; the FM-NEW-2 TCP-reuse path remains
    cheap insurance, not a load-bearing mitigation.
  * >=1 mid-session SIGTERM with restart cycles => confirmed; FM-NEW-2
    is load-bearing and the spike work continues with the TCP-reuse
    path enabled to verify it eliminates the T1 gap.
  * Other => inconclusive; re-run with longer duration or document
    the partial signal.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
LOG_PATH = SCRIPT_DIR / "spike_rdr094_mid_session_log.jsonl"
SUMMARY_PATH = SCRIPT_DIR / "spike_rdr094_mid_session_summary.json"

MCP_LOG_PATH = Path(
    os.environ.get(
        "NEXUS_CONFIG_DIR",
        os.path.expanduser("~/.config/nexus"),
    )
) / "logs" / "mcp.log"

#: Lines emitted by lifecycle hooks (PR #286). Anchor patterns are
#: substrings (KeyValueRenderer output is single-line).
_PATTERN_STARTING = re.compile(r"event='mcp_server_starting'")
_PATTERN_STOPPING = re.compile(
    r"event='mcp_server_stopping'.*?reason='([^']+)'",
)
_PATTERN_CRASHED = re.compile(r"event='mcp_server_crashed'")
_PATTERN_PID = re.compile(r"\bpid=(\d+)")
#: Structlog ``TimeStamper(fmt='iso', utc=True)`` field. Used to skip
#: log entries that predate the observer start so historical content
#: in mcp.log isn't replayed as if it occurred during the window
#: (the original Spike C run mis-classified pre-existing entries as
#: +0.0s mcp_events because pos was initialised to 0).
_PATTERN_EVENT_TIMESTAMP = re.compile(r"timestamp='([^']+)'")

#: Process-state poll cadence (seconds).
PROC_POLL_S: float = 30.0

#: Window inside which a stop-then-start counts as a restart cycle.
RESTART_WINDOW_S: float = 90.0


def _extract_event_timestamp(line: str) -> float | None:
    """Return epoch seconds from the structlog ``timestamp=...`` field, or None.

    The MCP server's structlog chain emits ``timestamp='<ISO>'`` via
    ``TimeStamper(fmt='iso', utc=True)``. Both ``Z`` and ``+00:00``
    suffixes are accepted; missing or malformed fields return None.
    """
    m = _PATTERN_EVENT_TIMESTAMP.search(line)
    if not m:
        return None
    raw = m.group(1).replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _parse_log_line(line: str) -> dict[str, Any] | None:
    """Return a {event_type, pid, event_ts, raw} dict for nx-mcp events, or None."""
    if _PATTERN_STARTING.search(line):
        ev = "starting"
    elif (m := _PATTERN_STOPPING.search(line)):
        ev = f"stopping_{m.group(1)}"
    elif _PATTERN_CRASHED.search(line):
        ev = "crashed"
    else:
        return None
    pid_match = _PATTERN_PID.search(line)
    pid = int(pid_match.group(1)) if pid_match else 0
    return {
        "event_type": ev,
        "pid": pid,
        "event_ts": _extract_event_timestamp(line),
        "raw": line.rstrip(),
    }


def _is_historical(parsed: dict[str, Any], observer_started: float) -> bool:
    """True if the parsed event's structlog timestamp predates the observer.

    Entries without a parseable ``timestamp=...`` field are NOT classified
    as historical: better to over-emit one event with a missing timestamp
    than to silently drop live activity.
    """
    ts = parsed.get("event_ts")
    if ts is None:
        return False
    return ts < observer_started


def _list_nx_mcp_pids() -> list[tuple[int, int, int]]:
    """Return [(pid, ppid, etime_s), ...] for every running nx-mcp.

    Uses ``ps -e -o pid,ppid,etime,command`` and filters for the
    nx-mcp command. etime is parsed best-effort; format is
    ``[[dd-]hh:]mm:ss``.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-e", "-o", "pid=,ppid=,etime=,command="],
            text=True, timeout=2,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            OSError):
        return []
    rows: list[tuple[int, int, int]] = []
    for line in out.splitlines():
        if "nx-mcp" not in line or "nx-mcp-catalog" in line:
            # We track the core nx-mcp only. Catalog server is a
            # separate subprocess and not the subject of #40207.
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        etime = _etime_seconds(parts[2])
        rows.append((pid, ppid, etime))
    return rows


def _etime_seconds(s: str) -> int:
    """Parse ps's etime format [[dd-]hh:]mm:ss into seconds."""
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = s.split(":")
    if len(parts) == 2:
        h, m_s = "0", f"{parts[0]}:{parts[1]}"
    else:
        h = parts[0]
        m_s = f"{parts[1]}:{parts[2]}"
    m, sec = m_s.split(":")
    return days * 86400 + int(h) * 3600 + int(m) * 60 + int(sec)


def _follow_log(path: Path, stop_at: float) -> "Iterator[str]":
    """Tail-f generator; yields every new line from path until stop_at.

    Re-opens on rotation (RotatingFileHandler). Emits empty strings
    on each idle poll so the consumer can check stop_at without
    blocking on read().
    """
    pos = 0
    last_inode = 0
    while time.time() < stop_at:
        try:
            stat = path.stat()
            if stat.st_ino != last_inode:
                pos = 0
                last_inode = stat.st_ino
            with path.open("r") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            if chunk:
                for line in chunk.splitlines(keepends=False):
                    yield line
        except OSError:
            pass
        time.sleep(0.5)
        yield ""


def _emit(out, record: dict[str, Any]) -> None:
    record["wallclock"] = time.time()
    record["wallclock_iso"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(record["wallclock"]),
    )
    out.write(json.dumps(record) + "\n")
    out.flush()
    print(json.dumps(record), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="RDR-094 Spike C: mid-session SIGTERM observer.",
    )
    parser.add_argument(
        "--duration-min", type=float, default=30.0,
        help="Observation window in minutes (default 30).",
    )
    parser.add_argument(
        "--proc-poll-s", type=float, default=PROC_POLL_S,
        help=f"Seconds between process-state polls (default {PROC_POLL_S}).",
    )
    args = parser.parse_args()

    duration_s = args.duration_min * 60.0
    started = time.time()
    deadline = started + duration_s
    if not MCP_LOG_PATH.exists():
        print(
            f"WARNING: {MCP_LOG_PATH} does not exist yet. Will tail "
            f"once nx-mcp creates it.",
            file=sys.stderr,
        )
        MCP_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    print(
        f"Observing for {args.duration_min:.1f} minutes. "
        f"Tail: {MCP_LOG_PATH}; "
        f"output: {LOG_PATH}",
        flush=True,
    )

    summary = {
        "started_at": started,
        "duration_seconds": duration_s,
        "start_count": 0,
        "stop_count_signal": 0,
        "stop_count_exit": 0,
        "crash_count": 0,
        "pid_set": [],
        "restart_cycles": 0,
        "interpretation": "inconclusive",
    }
    pid_seen: set[int] = set()
    last_stop_at: float | None = None
    last_proc_poll = 0.0

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as out:
        _emit(out, {
            "event_type": "observer_started",
            "duration_min": args.duration_min,
        })

        for line in _follow_log(MCP_LOG_PATH, deadline):
            if line:
                parsed = _parse_log_line(line)
                if parsed and _is_historical(parsed, started):
                    # Pre-existing mcp.log content; do not replay as if
                    # it occurred during the observation window.
                    parsed = None
                if parsed:
                    _emit(out, {
                        "event_type": "mcp_event",
                        "subtype": parsed["event_type"],
                        "pid": parsed["pid"],
                        "raw": parsed["raw"],
                    })
                    if parsed["event_type"] == "starting":
                        summary["start_count"] += 1
                        if last_stop_at is not None:
                            gap = time.time() - last_stop_at
                            if gap < RESTART_WINDOW_S:
                                summary["restart_cycles"] += 1
                                _emit(out, {
                                    "event_type": "restart_cycle",
                                    "gap_seconds": round(gap, 2),
                                })
                        if parsed["pid"]:
                            pid_seen.add(parsed["pid"])
                    elif parsed["event_type"] == "stopping_signal":
                        summary["stop_count_signal"] += 1
                        last_stop_at = time.time()
                    elif parsed["event_type"] == "stopping_exit":
                        summary["stop_count_exit"] += 1
                        last_stop_at = time.time()
                    elif parsed["event_type"] == "crashed":
                        summary["crash_count"] += 1

            now = time.time()
            if now - last_proc_poll >= args.proc_poll_s:
                last_proc_poll = now
                rows = _list_nx_mcp_pids()
                _emit(out, {
                    "event_type": "proc_poll",
                    "nx_mcp_count": len(rows),
                    "details": [
                        {"pid": p, "ppid": pp, "etime_s": e}
                        for p, pp, e in rows
                    ],
                })

        _emit(out, {"event_type": "observer_stopped"})

    summary["pid_set"] = sorted(pid_seen)
    summary["actual_duration_s"] = round(time.time() - started, 1)

    # Decision rule.
    if summary["stop_count_signal"] >= 1 and summary["restart_cycles"] >= 1:
        summary["interpretation"] = "issue_40207_confirmed"
    elif summary["stop_count_signal"] == 0 and summary["start_count"] >= 1:
        summary["interpretation"] = "no_mid_session_sigterm"
    else:
        summary["interpretation"] = "inconclusive"

    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
