#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Score uncued sequential-thinking runs: ab/seqthink/out/<ARM>-<GUIDE>/<prompt>/r<rep>/.

Per run (main session = requests with tools and no cc_is_subagent marker):
  seq        main-session sequentialthinking tool_use count (deduped by id)
  first      main turn whose response made the first call (1-based), or None
  turns      main-model requests
  sub_seq    sequentialthinking calls made inside subagents
  eager      sequentialthinking present and not deferred in the first main request
  cost       result.S.json total_cost_usd
Usage: analyse_sq.py <out dir> [--json f]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

SEQ = "mcp__plugin_conexus_sequential-thinking__sequentialthinking"
NX = "mcp__plugin_conexus_nexus__"


def rows(p: Path) -> list[dict]:
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if "/v1/messages" in str(r.get("path")) and isinstance(r.get("body"), dict):
            out.append(r)
    return out


def text_of(x) -> str:
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        return "".join(text_of(i) for i in x)
    if isinstance(x, dict):
        return text_of(x.get("text", "")) + text_of(x.get("content", ""))
    return ""


def is_sub(b: dict) -> bool:
    s = b.get("system")
    return "cc_is_subagent=true" in text_of(s[:1] if isinstance(s, list) else s)


def tool_uses(b: dict) -> dict[str, str]:
    seen: dict[str, str] = {}
    for m in b.get("messages") or []:
        c = m.get("content")
        if isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    seen[blk["id"]] = blk["name"]
    return seen


def run_dir(run: Path) -> dict:
    rs = rows(run / "requests.jsonl")
    main = [r for r in rs if r["body"].get("tools") and not is_sub(r["body"])]
    sub = [r for r in rs if r["body"].get("tools") and is_sub(r["body"])]
    d: dict = {"turns": len(main), "sub_reqs": len(sub)}
    if main:
        tools = {t.get("name"): t for t in main[0]["body"].get("tools") or []}
        d["eager"] = SEQ in tools and not tools[SEQ].get("defer_loading")
        d["eager_nx"] = sum(1 for n, t in tools.items() if n and n.startswith(NX) and not t.get("defer_loading"))
        d["model"] = main[0]["body"].get("model")
    first = None
    calls: dict[str, str] = {}
    for k, r in enumerate(main, start=1):
        tu = tool_uses(r["body"])
        if first is None and SEQ in tu.values():
            first = k - 1
        calls.update(tu)
    names = list(calls.values())
    d["seq"] = names.count(SEQ)
    d["first"] = first
    d["toolsearch"] = names.count("ToolSearch")
    d["agent"] = names.count("Agent") + names.count("Task")
    d["nx_calls"] = sum(1 for n in names if n.startswith(NX))
    subcalls: dict[str, str] = {}
    for r in sub:
        subcalls.update(tool_uses(r["body"]))
    d["sub_seq"] = list(subcalls.values()).count(SEQ)
    d["cost"] = 0.0
    f = run / "result.S.json"
    if f.exists():
        try:
            j = json.loads(f.read_text())
            d["cost"] = j.get("total_cost_usd") or 0.0
            d["is_error"] = j.get("is_error")
        except ValueError:
            d["is_error"] = "UNPARSEABLE"
    dirty = run / "repo_dirty.txt"
    d["dirty"] = bool(dirty.exists() and dirty.read_text().strip())
    return d


def main() -> None:
    root = Path(sys.argv[1])
    res = {}
    for run in sorted(root.glob("*/*/r*")):
        if (run / "requests.jsonl").exists():
            res["/".join(run.parts[-3:])] = run_dir(run)
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(res, indent=1))
    agg: dict[str, list[dict]] = defaultdict(list)
    for k, v in res.items():
        print(f"{k:34s} seq={v['seq']} first={v['first']} turns={v['turns']} sub_seq={v['sub_seq']} "
              f"eager={v.get('eager')} nx_eager={v.get('eager_nx')} ts={v['toolsearch']} agent={v['agent']} "
              f"nx={v['nx_calls']} cost={v['cost']:.3f} err={v.get('is_error')} dirty={v['dirty']} model={v.get('model')}")
        agg[k.split("/")[0]].append(v)
    total = 0.0
    print()
    for cell, vs in sorted(agg.items()):
        used = sum(1 for v in vs if v["seq"] > 0)
        anyuse = sum(1 for v in vs if v["seq"] > 0 or v["sub_seq"] > 0)
        cost = sum(v["cost"] for v in vs)
        total += cost
        print(f"{cell}: main-seq {used}/{len(vs)}, incl-subagent {anyuse}/{len(vs)}, "
              f"calls {sum(v['seq'] for v in vs)}, eager {sum(1 for v in vs if v.get('eager'))}/{len(vs)}, "
              f"mean turns {sum(v['turns'] for v in vs) / len(vs):.1f}, cost {cost:.2f}")
    print(f"TOTAL cost {total:.2f} over {len(res)} runs")


if __name__ == "__main__":
    main()
