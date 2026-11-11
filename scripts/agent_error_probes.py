#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Probe battery for agent-facing MCP errors (nexus-3ceda.7, the primary measure Sam locked
2026-11-10): does an agent's next call fix a failed one?

    agent_error_probes.py capture --out errors.json      # run each bad call, keep its real error text
    agent_error_probes.py ask --errors errors.json --runs 3 --out run.json [--model M]
    agent_error_probes.py compare before.json after.json # fixed share, paired by probe and run

Each probe is a real failed call taken from the error kinds agents hit in the session
transcripts. ``capture`` sends it through the nexus MCP server in this process (a real
client, the real middleware and tools), so the text is exactly what an agent sees today;
every probe is a call the server refuses before any write (probes that could write or run work first, an oversized store_put or a procedure_run, are left out), so nothing is written. ``ask`` hands the failed call,
its error and the tool's real schema to a fresh ``claude -p`` with no tools and no MCP servers,
and asks for the next call as JSON. Nothing the agent proposes is executed. ``fixed`` is a
per-probe rule on the proposed call. ``compare`` pairs the runs and reports McNemar's exact
p for the before/after difference.

Run on the laptop (it needs ``claude`` and the local engine), from the repository root.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_KV = re.compile(r"^\s*[A-Za-z_][\w.]*\s*(=|>=|<=|>|<|!=)\s*\S")


def _has(key: str) -> Callable[[str, dict], bool]:
    return lambda _t, a: a.get(key) not in (None, "", [], {})


@dataclass(frozen=True)
class Probe:
    id: str
    tool: str
    args: dict[str, Any]
    goal: str
    fixed: Callable[[str, dict], bool]


PROBES: list[Probe] = [
    Probe("search-where-word", "search", {"query": "attention mechanisms", "where": "attention"},
          "Find passages about attention mechanisms.",
          lambda t, a: t == "search" and (not a.get("where") or bool(_KV.match(str(a["where"]))))),
    Probe("search-where-title", "search", {"query": "a unified theory of cerebral cortex", "where": "Horizontal"},
          "Find the paper on a unified theory of the cerebral cortex.",
          lambda t, a: t == "search" and (not a.get("where") or bool(_KV.match(str(a["where"]))))),
    Probe("mailbox-message-arg", "mailbox_send", {"to": "00000000-0000-4000-8000-000000000000", "message": "hello"},
          "Send the text 'hello' to that session.",
          lambda t, a: t == "mailbox_send" and bool(a.get("body"))),
    Probe("memory-put-ttl-string", "memory_put",
          {"content": "note", "project": "probe", "title": "t", "ttl": "permanent"},
          "Store a permanent note titled t in project probe.",
          lambda t, a: t == "memory_put" and (a.get("ttl") is None or (isinstance(a["ttl"], int) and a["ttl"] > 0))),
    Probe("memory-get-no-project", "memory_get", {"title": "push-train"},
          "Read the push-train note of the nexus project.", lambda t, a: t == "memory_get" and bool(a.get("project"))),
    Probe("memory-put-no-title", "memory_put", {"content": "note", "project": "probe"},
          "Store a note in project probe.", lambda t, a: t == "memory_put" and bool(a.get("title"))),
    Probe("tuple-subscribe-no-subspace", "tuple_subscribe", {},
          "Subscribe to the mailbox of the name tamsin.",
          lambda t, a: t == "tuple_subscribe" and bool(a.get("subspace"))),
    Probe("tuple-rd-unregistered", "tuple_rd", {"subspace": "ledger/probe-v0174", "keys_pattern": {}},
          "Read the rows of the probe ledger.",
          lambda t, a: t == "tuple_registry" or (t == "tuple_rd" and a.get("subspace") != "ledger/probe-v0174")),
    Probe("tuple-ack-missing", "tuple_ack", {}, "Acknowledge the claim you hold.",
          lambda t, a: t == "tuple_ack" and bool(a.get("claim_id"))),
    Probe("tuple-out-no-keys", "tuple_out", {"subspace": "board/probe", "body": "status"},
          "Post a status row to the probe board.", lambda t, a: t == "tuple_out" and _has("keys")(t, a)),
    Probe("tuple-rd-no-subspace", "tuple_rd", {"keys_pattern": {}},
          "Read the board rows.", lambda t, a: t == "tuple_rd" and bool(a.get("subspace"))),
    Probe("store-put-placeholder", "store_put", {"content": "a fact", "collection": "knowledge", "title": "fact"},
          "Store this fact as permanent knowledge about distributed systems.",
          lambda t, a: t == "store_put" and a.get("collection") not in ("knowledge", "default", "notes", "tmp", "test")),
    Probe("store-get-many-no-ids", "store_get_many", {},
          "Fetch documents abc and def.", lambda t, a: t == "store_get_many" and _has("ids")(t, a)),
    Probe("scratch-unknown-action", "scratch", {"action": "read", "entry_id": "abc"},
          "Read scratch entry abc.", lambda t, a: t == "scratch" and a.get("action") == "get"),
    Probe("scratch-get-no-id", "scratch", {"action": "get"},
          "Read the scratch entry whose id is abc.",
          lambda t, a: t == "scratch" and a.get("action") == "get" and bool(a.get("entry_id"))),
    Probe("metadata-scoped-operator", "search_metadata_scoped", {"query": "consensus", "where": "year>=2020"},
          "Find recent papers on consensus.",
          lambda t, a: t != "search_metadata_scoped" or not re.search(r"(>=|<=|>|<|!=)", str(a.get("where", "")))),
    Probe("aspect-scoped-unknown-field", "search_aspect_scoped", {"query": "consensus", "field": "colour", "value": "red"},
          "Find documents whose extracted aspects mention red.",
          lambda t, a: t != "search_aspect_scoped" or a.get("field") != "colour"),
    Probe("store-get-empty-id", "store_get", {"doc_id": ""}, "Read the document titled push-train.",
          lambda t, a: t == "store_get" and bool(a.get("doc_id"))),
    Probe("mailbox-name-with-spaces", "mailbox_send", {"to": "Seminar seat: Bullock", "body": "hello"},
          "Send hello to the session that holds the seminar seat for Bullock.",
          lambda t, a: t != "mailbox_send" or not re.search(r"[\s:]", str(a.get("to", "")))),
    Probe("query-catalog-where-operator", "query", {"question": "consensus", "author": "Lamport", "where": "year>=2020"},
          "Find documents by Lamport on consensus from 2020 on.",
          lambda t, a: t == "query" and not (a.get("author") and re.search(r"(>=|<=|>|<|!=)", str(a.get("where", ""))))),
]


def _text(result: Any) -> str:
    return "\n".join(getattr(c, "text", "") for c in getattr(result, "content", []) or [])


def capture(out: Path) -> int:
    import anyio  # noqa: PLC0415 — only this subcommand needs the server
    from mcp import Client  # noqa: PLC0415

    from nexus.mcp.core import mcp  # noqa: PLC0415

    async def _run() -> dict[str, Any]:
        rows: dict[str, Any] = {}
        async with Client(mcp, mode="legacy") as client:
            schemas = {t.name: t.input_schema for t in (await client.list_tools()).tools}
            for p in PROBES:
                if p.tool not in schemas:
                    rows[p.id] = {"skip": f"no tool {p.tool}"}
                    continue
                r = await client.call_tool(p.tool, p.args)
                rows[p.id] = {"tool": p.tool, "is_error": bool(r.is_error), "error": _text(r), "schema": schemas[p.tool]}
        return rows

    rows = anyio.run(_run)
    usable = [k for k, v in rows.items() if v.get("is_error")]
    manifest = {"captured_at": datetime.now(UTC).isoformat(), "git": _git_sha(), "usable": len(usable)}
    out.write_text(json.dumps({"manifest": manifest, "probes": rows}, indent=1), encoding="utf-8")
    for k, v in rows.items():
        state = "ERROR" if v.get("is_error") else ("SKIP " + v["skip"] if "skip" in v else "NOT AN ERROR")
        sys.stdout.write(f"{k}: {state}: {v.get('error', '')[:140]!r}\n")
    sys.stdout.write(f"{len(usable)} of {len(PROBES)} probes reproduce an error\n")
    return 0 if usable else 1


_PROMPT = """You are an agent using the nexus MCP server. Your goal: {goal}

You just called the tool `{tool}` with these arguments:
{args}

It failed. This is the error result exactly as the server returned it:
{error}

The tool's input schema:
{schema}

Decide your next tool call to reach the goal. You may call this tool again or a different nexus tool.
Reply with ONLY one JSON object, no prose: {{"tool": "<tool name without any prefix>", "arguments": {{...}}}}"""


def _ask_once(prompt: str, model: str | None) -> dict[str, Any]:
    argv = ["claude", "-p", prompt, "--tools", "", "--strict-mcp-config", "--output-format", "json"]
    if model:
        argv += ["--model", model]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)  # noqa: S603
    try:
        reply = json.loads(proc.stdout).get("result", "")
    except ValueError:
        return {"parse": "no json envelope", "rc": proc.returncode}
    m = re.search(r"\{.*\}", reply, re.DOTALL)
    try:
        call = json.loads(m.group(0)) if m else None
    except ValueError:
        call = None
    if not isinstance(call, dict):
        return {"parse": "no call in reply"}
    tool = str(call.get("tool", "")).removeprefix("mcp__plugin_conexus_nexus__")
    args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    return {"tool": tool, "arguments": args}


def ask(errors: Path, runs: int, out: Path, model: str | None) -> int:
    data = json.loads(errors.read_text(encoding="utf-8"))
    by_id = {p.id: p for p in PROBES}
    results: list[dict[str, Any]] = []
    for pid, row in data["probes"].items():
        if not row.get("is_error"):
            continue
        p = by_id[pid]
        shown = {k: (v if len(json.dumps(v)) < 400 else f"<{len(str(v))} characters>") for k, v in p.args.items()}
        prompt = _PROMPT.format(goal=p.goal, tool=p.tool, args=json.dumps(shown), error=row["error"],
                                schema=json.dumps(row["schema"]))
        for run in range(runs):
            nxt = _ask_once(prompt, model)
            ok = "tool" in nxt and bool(p.fixed(nxt["tool"], nxt["arguments"]))
            results.append({"probe": pid, "run": run, "fixed": ok, "next_tool": nxt.get("tool"), "parse": nxt.get("parse")})
            sys.stdout.write(f"{pid} run {run}: {'fixed' if ok else 'not fixed'} ({nxt.get('tool') or nxt.get('parse')})\n")
    fixed = sum(r["fixed"] for r in results)
    manifest = {"asked_at": datetime.now(UTC).isoformat(), "git": _git_sha(), "model": model or "default",
                "runs": runs, "errors_file": str(errors), "errors_git": data["manifest"]["git"]}
    out.write_text(json.dumps({"manifest": manifest, "results": results}, indent=1), encoding="utf-8")
    sys.stdout.write(f"fixed {fixed} of {len(results)} ({fixed / max(len(results), 1):.0%})\n")
    return 0


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p for b before-only and c after-only fixes."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def compare(before: Path, after: Path) -> int:
    a = {(r["probe"], r["run"]): r["fixed"] for r in json.loads(before.read_text())["results"]}
    z = {(r["probe"], r["run"]): r["fixed"] for r in json.loads(after.read_text())["results"]}
    pairs = sorted(set(a) & set(z))
    b = sum(a[k] and not z[k] for k in pairs)
    c = sum(z[k] and not a[k] for k in pairs)
    share_a = sum(a[k] for k in pairs) / max(len(pairs), 1)
    share_z = sum(z[k] for k in pairs) / max(len(pairs), 1)
    rise = (share_z - share_a) * 100
    p = mcnemar_exact(b, c)
    sys.stdout.write(json.dumps({"pairs": len(pairs), "before": round(share_a, 3), "after": round(share_z, 3),
                                 "rise_points": round(rise, 1), "before_only": b, "after_only": c,
                                 "mcnemar_p": round(p, 4), "passes": rise >= 20 and p < 0.05}) + "\n")
    return 0


def _git_sha() -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)  # noqa: S607
    return r.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Probe battery for agent-facing MCP errors (nexus-3ceda.7).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--out", type=Path, required=True)
    k = sub.add_parser("ask")
    k.add_argument("--errors", type=Path, required=True)
    k.add_argument("--runs", type=int, default=3)
    k.add_argument("--out", type=Path, required=True)
    k.add_argument("--model")
    m = sub.add_parser("compare")
    m.add_argument("before", type=Path)
    m.add_argument("after", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "capture":
        return capture(a.out)
    if a.cmd == "ask":
        return ask(a.errors, a.runs, a.out, a.model)
    return compare(a.before, a.after)


if __name__ == "__main__":
    sys.exit(main())
