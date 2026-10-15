#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Score arm C validation runs: ab/tuplespace/out/<ARM>/<name>/r<rep>/.

Per claude -p session (split by x-claude-code-session-id; subagent requests
marked by cc_is_subagent=true in system[0]):
  main1/sub1   first main / subagent request: seqthink eager, eager nexus names
  hook         subagent first request carries the conexus SubagentStart block
  calls        tool_use names (deduped by id); ToolSearch and seqthink counts
  turns        main-model requests (requests carrying tools)
  errs         tool_result is_error count
Tuple mode adds poststate census lines, result text checks, drain probe.
Usage: analyse_ts.py <out dir> [--json f]
"""
from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path

NX = "mcp__plugin_conexus_nexus__"
SEQ = "mcp__plugin_conexus_sequential-thinking__sequentialthinking"


def rows(p: Path) -> list[dict]:
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return [r for r in out if "/v1/messages" in str(r.get("path")) and isinstance(r.get("body"), dict)]


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


def eager(b: dict) -> tuple[bool, list[str]]:
    tools = b.get("tools") or []
    names = {t.get("name"): t for t in tools}
    seq = SEQ in names and not names[SEQ].get("defer_loading")
    nx = sorted(n[len(NX):] for n, t in names.items() if n and n.startswith(NX) and not t.get("defer_loading"))
    return seq, nx


def session(rs: list[dict]) -> dict:
    main = [r for r in rs if r["body"].get("tools") and not is_sub(r["body"])]
    sub = [r for r in rs if r["body"].get("tools") and is_sub(r["body"])]
    d: dict = {"turns": len(main), "sub_reqs": len(sub)}
    if main:
        d["main1_seq"], d["main1_nx"] = eager(main[0]["body"])
    if sub:
        b = sub[0]["body"]
        d["sub1_seq"], d["sub1_nx"] = eager(b)
        msgs = b.get("messages") or []
        t1 = text_of(msgs[1]) if len(msgs) > 1 else ""
        d["hook_in_msg1"] = "Claimant id" in t1
        d["hook_anywhere"] = any("Claimant id" in text_of(m) for m in msgs)
        d["hook_header"] = "SubagentStart hook additional context" in "".join(text_of(m) for m in msgs)
    seen: dict[str, str] = {}
    errs = 0
    for r in main + sub:
        for m in r["body"].get("messages") or []:
            c = m.get("content")
            if not isinstance(c, list):
                continue
            for blk in c:
                if blk.get("type") == "tool_use":
                    seen[blk["id"]] = blk["name"]
    err_ids = set()
    for r in main + sub:
        for m in r["body"].get("messages") or []:
            c = m.get("content")
            if isinstance(c, list):
                for blk in c:
                    if blk.get("type") == "tool_result" and blk.get("is_error"):
                        err_ids.add(blk.get("tool_use_id"))
    calls = list(seen.values())
    d["calls"] = [c.replace(NX, "nx:").replace(SEQ, "SEQ") for c in calls]
    d["toolsearch"] = calls.count("ToolSearch")
    d["seq_calls"] = calls.count(SEQ)
    d["errs"] = len(err_ids)
    d["err_tools"] = [seen.get(i, "?") for i in err_ids]
    d["all_text"] = "".join(text_of(r["body"].get("messages")) for r in main)
    return d


def debug_hook(run: Path) -> list[str]:
    out = []
    for f in run.glob("debug.*.log.gz"):
        for line in gzip.open(f, "rt", errors="replace"):
            if "hook_subagent_start" in line and re.search(r"error|fail|not found|timed out", line, re.I):
                out.append(f.name + ": " + line.strip()[:200])
    return out


def run_dir(run: Path) -> dict:
    rs = rows(run / "requests.jsonl")
    by: dict[str, list[dict]] = {}
    for r in rs:
        sid = {k.lower(): v for k, v in (r.get("headers") or {}).items()}.get("x-claude-code-session-id", "?")
        by.setdefault(sid, []).append(r)
    ids = json.loads((run / "ids.json").read_text()) if (run / "ids.json").exists() else {}
    label = {v: k for k, v in ids.items()}
    res: dict = {"sessions": {}, "cost": 0.0, "debug_hook_errors": debug_hook(run)}
    for sid, rr in by.items():
        res["sessions"][label.get(sid, sid[:8])] = session(rr)
    for f in run.glob("result.*.json"):
        try:
            j = json.loads(f.read_text())
            res["cost"] += j.get("total_cost_usd") or 0
            res.setdefault("results", {})[f.name.split(".")[1]] = (j.get("result") or "")[:4000]
        except ValueError:
            res.setdefault("results", {})[f.name.split(".")[1]] = "UNPARSEABLE"
    ps = run / "poststate.txt"
    res["poststate"] = ps.read_text() if ps.exists() else ""
    if ids.get("NONCE"):
        n = ids["NONCE"]
        r = res.get("results", {})
        res["s2_body_ok"] = f"PING-{n}-agent" in r.get("S2", "")
        s3 = res["sessions"].get("U3", {})
        res["drain_in_s3_context"] = f"PING-{n}-session" in s3.get("all_text", "")
        res["s3_quoted"] = f"PING-{n}-session" in r.get("S3", "")
    for s in res["sessions"].values():
        s.pop("all_text", None)
    return res


def main() -> None:
    root = Path(sys.argv[1])
    allres = {}
    for run in sorted(root.glob("*/*/r*")):
        if not (run / "requests.jsonl").exists():
            continue
        key = "/".join(run.parts[-3:])
        allres[key] = run_dir(run)
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(allres, indent=1))
    for k, v in allres.items():
        print(f"== {k} cost={v['cost']:.3f} debug_hook_errors={len(v['debug_hook_errors'])}")
        for e in v["debug_hook_errors"][:3]:
            print("   DBG", e)
        for lab, s in v["sessions"].items():
            print(f"  [{lab}] turns={s['turns']} sub_reqs={s['sub_reqs']} ts={s['toolsearch']} seq={s['seq_calls']} "
                  f"errs={s['errs']}{s['err_tools'] or ''} main1_seq={s.get('main1_seq')} "
                  f"sub1_seq={s.get('sub1_seq')} hook_msg1={s.get('hook_in_msg1')} hook_any={s.get('hook_anywhere')}")
            print(f"      main1_nx={s.get('main1_nx')}")
            if "sub1_nx" in s:
                print(f"      sub1_nx={s.get('sub1_nx')}")
            print(f"      calls={s['calls']}")
        if "s2_body_ok" in v:
            print(f"  s2_body_ok={v['s2_body_ok']} drain_in_s3_context={v['drain_in_s3_context']} s3_quoted={v['s3_quoted']}")
            for line in v["poststate"].splitlines():
                if line.startswith("#") or re.search(r"total|available|claimed|consumed|holder|session", line, re.I):
                    print("   PS", line[:160])


if __name__ == "__main__":
    main()
