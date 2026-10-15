#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Summarise a prefix-capture requests.jsonl (nexus-4mptw).

For every /v1/messages request: model, usage, the ordered system blocks and
message content blocks (bytes, cache_control, first 120 chars). Then, for each
pair of "first" requests (messages length 1) that share a model, the first
byte where their cache prefix (tools, then system, then messages, the order
the prompt cache hashes) diverges, and what sits there.

Usage: analyse.py requests.jsonl [--full]
"""
from __future__ import annotations

import json
import re
import sys

_REDACT = re.compile(r"(sk-ant-[A-Za-z0-9_\-]+|Bearer\s+\S+)")


def red(s: str) -> str:
    return _REDACT.sub("<redacted>", s)


def head(s: str, n: int = 120) -> str:
    return red(s[:n]).replace("\n", "\\n")


def block_text(b: object) -> str:
    if isinstance(b, str):
        return b
    if not isinstance(b, dict):
        return json.dumps(b)
    t = b.get("type")
    if t == "text":
        return b.get("text", "")
    if t == "tool_use":
        return json.dumps({"name": b.get("name"), "input": b.get("input")})
    if t == "tool_result":
        c = b.get("content")
        return c if isinstance(c, str) else json.dumps(c)
    return json.dumps(b)


def describe(row: dict) -> list[str]:
    body = row.get("body") or {}
    u = row.get("usage") or {}
    st = u.get("start") or u.get("json_usage") or {}
    out = [f"#{row['seq']} {row['path']} model={body.get('model')} status={row['status']}"
           f" msgs={len(body.get('messages') or [])} tools={len(body.get('tools') or [])}",
           f"  usage: input={st.get('input_tokens')} cache_creation={st.get('cache_creation_input_tokens')}"
           f" cache_read={st.get('cache_read_input_tokens')} out={(u.get('delta') or {}).get('output_tokens')}"]
    tools = body.get("tools") or []
    deferred = sum(1 for t in tools if t.get("defer_loading"))
    tcc = [i for i, t in enumerate(tools) if t.get("cache_control")]
    out.append(f"  tools: {len(tools)} ({deferred} defer_loading) bytes={len(json.dumps(tools))}"
               f" cache_control at tool idx {tcc}")
    sysv = body.get("system")
    if isinstance(sysv, str):
        sysv = [{"type": "text", "text": sysv}]
    for i, b in enumerate(sysv or []):
        txt = block_text(b)
        out.append(f"  system[{i}] {len(txt.encode())}B cc={b.get('cache_control')} | {head(txt)}")
    for mi, m in enumerate(body.get("messages") or []):
        c = m.get("content")
        blocks = [c] if isinstance(c, str) else (c or [])
        for bi, b in enumerate(blocks):
            txt = block_text(b)
            cc = b.get("cache_control") if isinstance(b, dict) else None
            typ = b.get("type") if isinstance(b, dict) else "str"
            out.append(f"  msg[{mi}].{m.get('role')}[{bi}] {typ} {len(txt.encode())}B cc={cc} | {head(txt)}")
    return out


def prefix(body: dict) -> str:
    return (json.dumps(body.get("tools"), sort_keys=False) + "\x00SYSTEM\x00"
            + json.dumps(body.get("system")) + "\x00MESSAGES\x00"
            + json.dumps(body.get("messages")))


def main() -> None:
    rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
    msgs = [r for r in rows if r["path"].startswith("/v1/messages") and isinstance(r.get("body"), dict)
            and r["body"].get("messages") is not None]
    other = [r for r in rows if r not in msgs]
    print(f"{len(rows)} requests; {len(msgs)} /v1/messages; other paths: "
          f"{sorted({r['path'].split('?')[0] for r in other})}")
    for r in msgs:
        print("\n".join(describe(r)))
    firsts = [r for r in msgs if not any(m.get("role") == "assistant" for m in r["body"]["messages"])]
    print("\n== first-turn requests:", [r["seq"] for r in firsts])
    for i in range(len(firsts)):
        for j in range(i + 1, len(firsts)):
            a, b = firsts[i], firsts[j]
            pa, pb = prefix(a["body"]), prefix(b["body"])
            k = next((x for x in range(min(len(pa), len(pb))) if pa[x] != pb[x]), min(len(pa), len(pb)))
            region = "tools" if k < pa.find("\x00SYSTEM\x00") else (
                "system" if k < pa.find("\x00MESSAGES\x00") else "messages")
            print(f"diverge #{a['seq']} vs #{b['seq']} ({a['body'].get('model')} / {b['body'].get('model')}): "
                  f"offset {k} of {len(pa)}/{len(pb)}, region={region}")
            print(f"   A: {head(pa[max(0, k - 60):k + 160], 220)}")
            print(f"   B: {head(pb[max(0, k - 60):k + 160], 220)}")


if __name__ == "__main__":
    main()
