#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Score the nexus-ly9c2 A/B runs (ab/out/<ARM>/<task>/r<rep>/).

Per run, from requests.jsonl (proxy log) and result.json:
  first      first substantive tool call (ToolSearch and sequentialthinking skipped)
  correct    first is in the task's expected set (near-miss: no nexus tool at all)
  reached    first nexus call is acceptable and the expected tool is called (near-miss: = correct)
  success    task outcome check (answer text / post-state / tool result)
  turns      main-model /v1/messages requests
  ts         ToolSearch calls
  err        tool results with is_error
  wrong      nexus tool calls outside the task's acceptable set
  tokens     input / cache_creation / cache_read / output, summed over every request
  prefix     first main request's input + cache_creation + cache_read
  wall       claude -p wall time (s); cost = result.json total_cost_usd

Usage: score.py <ab/out dir> [--json out.json]
"""
from __future__ import annotations

import json
import re
import statistics as st
import sys
from pathlib import Path

NX = "mcp__plugin_conexus_nexus__"
SKIP = {"ToolSearch", "mcp__plugin_conexus_sequential-thinking__sequentialthinking"}

# task -> (expected first tools, acceptable nexus tools, success check)
TASKS: dict[str, tuple[set[str], set[str], str]] = {
    "t1_memsearch": ({"memory_search", "memory_get"}, {"memory_search", "memory_get"}, "ans:clock"),
    "t2_memget": ({"memory_get"}, {"memory_get", "memory_search"}, "ans:24"),
    "t3_memput": ({"memory_put"}, {"memory_put", "memory_search", "memory_get"}, "post:memput"),
    "t4_search": ({"search"}, {"search", "query", "store_get"}, "ans:90"),
    "t5_query": ({"query"}, {"query", "search", "store_list"}, "ans:backpressure-design"),
    "t6_scratch": ({"scratch"}, {"scratch"}, "tool:scratch"),
    "t7_storeput": ({"store_put"}, {"store_put", "collection_list", "store_list"}, "post:storeput"),
    "t8_storeget": ({"store_get"}, {"store_get", "search", "query", "store_list"}, "ans:two-phase"),
    "n1_grep": (set(), set(), "ans:src/cli.py"),
    "n2_lines": (set(), set(), "ans:5"),
    "n3_git": (set(), set(), "ans:injected clock"),
}


def _rows(p: Path) -> list[dict]:
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _usage(row: dict) -> dict[str, int]:
    u = row.get("usage") or {}
    start = u.get("start") or u.get("json_usage") or {}
    delta = u.get("delta") or {}
    return {
        "in": int(start.get("input_tokens") or 0),
        "cw": int(start.get("cache_creation_input_tokens") or 0),
        "cr": int(start.get("cache_read_input_tokens") or 0),
        "out": int(delta.get("output_tokens") or start.get("output_tokens") or 0),
    }


def score_run(d: Path, task: str) -> dict | None:
    req = d / "requests.jsonl"
    res = d / "result.json"
    if not req.exists() or not res.exists() or res.stat().st_size == 0:
        return None
    rows = [r for r in _rows(req) if "/v1/messages" in r.get("path", "")
            and "count_tokens" not in r.get("path", "") and isinstance(r.get("body"), dict)]
    result = json.loads(res.read_text(encoding="utf-8"))
    main_model = None
    main = []
    for r in rows:
        m = r["body"].get("model", "")
        if "opus" in m:
            main_model = m
            main.append(r)
    tok = {"in": 0, "cw": 0, "cr": 0, "out": 0}
    for r in rows:
        for k, v in _usage(r).items():
            tok[k] += v
    first_u = _usage(main[0]) if main else {"in": 0, "cw": 0, "cr": 0}
    prefix = first_u["in"] + first_u["cw"] + first_u["cr"]
    n_tools_first = len(main[0]["body"].get("tools") or []) if main else 0
    nx_eager_first = sum(1 for t in (main[0]["body"].get("tools") or [])
                         if t.get("name", "").startswith(NX) and not t.get("defer_loading")) if main else 0
    # Tool calls: the last main request carries the whole conversation so far;
    # the final assistant turn (text only) is in result.json.
    calls: list[dict] = []
    results: dict[str, dict] = {}
    if main:
        for msg in main[-1]["body"].get("messages", []):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    calls.append(b)
                elif b.get("type") == "tool_result":
                    results[b.get("tool_use_id", "")] = b
    names = [c["name"] for c in calls]
    subst = [n for n in names if n not in SKIP]
    first = subst[0] if subst else "-"
    short = lambda n: n[len(NX):] if n.startswith(NX) else n  # noqa: E731
    expected, acceptable, check = TASKS[task]
    nx_calls = [short(n) for n in names if n.startswith(NX)]
    if expected:
        correct = short(first) in expected and first.startswith(NX)
        reached = bool(nx_calls) and nx_calls[0] in acceptable and bool(set(nx_calls) & expected)
        wrong = sum(1 for n in nx_calls if n not in acceptable)
    else:
        correct = not nx_calls
        reached = correct
        wrong = len(nx_calls)
    err = 0
    for c in calls:
        r = results.get(c.get("id", ""))
        if r is not None and r.get("is_error"):
            err += 1
    answer = str(result.get("result") or "")
    kind, _, arg = check.partition(":")
    post = (d / "poststate.txt").read_text(encoding="utf-8") if (d / "poststate.txt").exists() else ""
    if kind == "ans":
        if arg == "5":
            success = re.search(r"\b5\b", answer) is not None
        else:
            success = arg.lower() in answer.lower()
    elif kind == "post" and arg == "memput":
        success = len(re.findall(r"^\[\d+\] aurora/", post, re.M)) >= 4
    elif kind == "post" and arg == "storeput":
        success = "checkpoint-interval" in post
    elif kind == "tool":
        ok = [c for c in calls if short(c["name"]) == arg
              and not (results.get(c.get("id", "")) or {}).get("is_error")]
        success = bool(ok)
    else:
        success = False
    ts_queries = [json.dumps(c.get("input"))[:80] for c in calls if c["name"] == "ToolSearch"]
    wall = None
    ex = d / "exit.txt"
    if ex.exists():
        m = re.search(r"wall=([\d.]+)", ex.read_text())
        wall = float(m.group(1)) if m else None
    return {
        "task": task, "first": short(first), "correct": correct, "reached": reached, "success": success,
        "turns": len(main), "ts": names.count("ToolSearch"), "err": err, "wrong": wrong,
        "seq": [short(n) for n in names], "ts_queries": ts_queries,
        "tok": tok, "prefix": prefix, "tools_first": n_tools_first, "nx_eager_first": nx_eager_first,
        "wall": wall, "cost": result.get("total_cost_usd"), "is_error": result.get("is_error"),
        "model": main_model, "answer": answer[:200],
    }


def main() -> None:
    root = Path(sys.argv[1])
    out: dict[str, list[dict]] = {}
    for arm_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        runs = []
        for task_dir in sorted(p for p in arm_dir.iterdir() if p.is_dir()):
            for rep in sorted(task_dir.iterdir()):
                s = score_run(rep, task_dir.name)
                if s:
                    s["rep"] = rep.name
                    runs.append(s)
        out[arm_dir.name] = runs
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(out, indent=1))
    mean = lambda xs: st.mean(xs) if xs else float("nan")  # noqa: E731
    print("arm  n   correct  reached success  turns  ts    err   wrong  in      cw       cr        out    prefix   wall   cost$")
    for arm, runs in out.items():
        if not runs:
            continue
        print(f"{arm:4} {len(runs):3} {mean([r['correct'] for r in runs]):7.2f} {mean([r['reached'] for r in runs]):7.2f} {mean([r['success'] for r in runs]):8.2f}"
              f" {mean([r['turns'] for r in runs]):6.2f} {mean([r['ts'] for r in runs]):5.2f} {mean([r['err'] for r in runs]):5.2f}"
              f" {mean([r['wrong'] for r in runs]):5.2f} {mean([r['tok']['in'] for r in runs]):7.0f} {mean([r['tok']['cw'] for r in runs]):8.0f}"
              f" {mean([r['tok']['cr'] for r in runs]):9.0f} {mean([r['tok']['out'] for r in runs]):6.0f} {mean([r['prefix'] for r in runs]):8.0f}"
              f" {mean([r['wall'] or 0 for r in runs]):6.1f} {mean([r['cost'] or 0 for r in runs]):6.3f}")
    print()
    print("per task: correct/success counts per arm (first tool calls)")
    tasks = sorted({r["task"] for runs in out.values() for r in runs})
    for t in tasks:
        cells = []
        for arm, runs in out.items():
            rs = [r for r in runs if r["task"] == t]
            if rs:
                firsts = ",".join(r["first"] for r in rs)
                cells.append(f"{arm}: {sum(r['correct'] for r in rs)}/{len(rs)} reach {sum(r['reached'] for r in rs)} ok {sum(r['success'] for r in rs)}/{len(rs)} ts={sum(r['ts'] for r in rs)} [{firsts}]")
        print(f"{t:13} " + " | ".join(cells))
    per_task(out)


def per_task(out: dict[str, list[dict]]) -> None:
    print()
    print("per task means: turns / ToolSearch / cache_read K / cost $ / wall s")
    tasks = sorted({r["task"] for runs in out.values() for r in runs})
    for t in tasks:
        cells = []
        for arm, runs in out.items():
            rs = [r for r in runs if r["task"] == t]
            if rs:
                cells.append(f"{arm}: {st.mean(r['turns'] for r in rs):.1f} / {st.mean(r['ts'] for r in rs):.1f}"
                             f" / {st.mean(r['tok']['cr'] for r in rs)/1000:.0f} / {st.mean(r['cost'] or 0 for r in rs):.3f}"
                             f" / {st.mean(r['wall'] or 0 for r in rs):.1f}")
        print(f"{t:13} " + " | ".join(cells))


if __name__ == "__main__":
    main()
