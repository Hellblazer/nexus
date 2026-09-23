#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-run timing view of a ladder's results.jsonl (nexus-veh77).

All times are seconds relative to the moment the input box was seen
(``t_ready``). ``connect`` is the probe server's ``Successfully connected``
debug line; for each event, ``twin`` is the command-tier ground truth and
``mcp`` is the probe server's own record of the ``mcp_tool`` hook call.
"""
import json
import sys

EVENTS = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop",
          "SubagentStart", "SubagentStop"]


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def rel(x, t):
    return "-" if x is None or t is None else f"{x - t:+.2f}"


occurrences = False


def main(path: str) -> None:
    for line in open(path):
        r = json.loads(line)
        t = r.get("t_ready")
        head = (f"{r['label']} S={r['server_delay_s']:g} submit={r['submit']} "
                f"{r['kind']} rep={r['rep']}")
        if "error" in r:
            _out(f"{head} ERROR {r['error']}")
            continue
        parts = [f"launch={rel(r['t_launch'], t)}", f"submit={rel(r.get('t_submit_sent'), t)}",
                 f"connect={rel(r.get('connect_ts'), t)}", f"turn1={rel(r.get('turn1_ts'), t)}"]
        if r.get("ss_sleep_s"):
            parts.append(f"ss={r['ss_sleep_s']:g}(begin={rel(r.get('SessionStartBegin'), t)} "
                         f"end={rel(r.get('SessionStartEnd'), t)})")
        if r.get("barrier"):
            parts.append(f"barrier(begin={rel(r.get('BarrierBegin'), t)} "
                         f"end={rel(r.get('BarrierEnd'), t)} "
                         f"wait={r.get('barrier_wait_s')})")
        for ev in EVENTS:
            e = r["events"][ev]
            if e["verdict"] == "not_reached":
                continue
            parts.append(f"{ev}:{e['verdict']}(twin={rel(e['twin_first'], t)} "
                         f"mcp={rel(e['probe_first'], t)} skip={e['skipped_lines']})")
        if r.get("connfail_line"):
            parts.append("CONNFAIL")
        _out(head + " " + " ".join(parts))
        if occurrences:
            # Every occurrence of each event; '*' marks one the mcp_tool hook
            # reached (a probe call within 1 s of the command-tier twin).
            for ev in EVENTS:
                e = r["events"][ev]
                if e.get("twin_all"):
                    marks = " ".join(
                        rel(x, t) + ("*" if any(abs(p - x) <= 1 for p in e["probe_all"]) else "")
                        for x in e["twin_all"])
                    _out(f"    {ev}: {marks}")


if __name__ == "__main__":
    occurrences = "--occurrences" in sys.argv
    main([a for a in sys.argv[1:] if a != "--occurrences"][0])
