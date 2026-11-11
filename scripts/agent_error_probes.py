#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Probe battery for agent-facing MCP errors (nexus-3ceda.7): does an agent's next call fix a
failed one? An A/B instrument for error-text changes, kept for future error work.

    agent_error_probes.py capture --out errors.json
    agent_error_probes.py ask --errors errors.json --model M --runs 3 --out run.json
    agent_error_probes.py compare before.json after.json [--bar 20]

``capture`` sends each probe, a failed call of a kind agents hit in the session transcripts,
through the nexus MCP server in this process (a real client, the real middleware and tools) and
keeps the error text. Every probe must fail with its expected error, or capture exits 1. ``ask``
gives the failed call, its error, the goal and the tool's schema to a fresh ``claude -p`` (no
tools, no MCP servers, no setting sources, an empty working directory) and asks for the next call
as JSON; nothing it proposes is executed. A proposal naming a tool the server does not register is
never "fixed"; otherwise the probe's own rule decides. ``compare`` checks that both runs used the
same model, probe set and prompt, then reports the fixed share and the rise in points, with exact
McNemar p at probe level (a probe counts as fixed when most of its runs are) and at run level.

LIMITS. Read a result with these in mind.
- It measures one thing: after an error, does the next proposed call look like a fix? It cannot
  see an agent that moves on to other work, the commonest real outcome (43 of 72 follow-ups in
  the 2026-11 transcript baseline, against 28 corrected retries), because the prompt asks for a
  next call toward the goal.
- Conditions push the fix rate up: the full schema sits beside the error, each goal states the
  intent, and the agent has nothing else on its mind. A strong model near the ceiling is the
  weakest test of error text: run a smaller model too.
- One shot, no tool results: an agent that would look something up first scores by whatever its
  first call is. Some rules credit a sensible lookup; most do not.
- "Fixed" means "looks like the fix" under a hand-written rule, never executed or validated
  against the schema. The rules were audited on the misses of one run, not on the hits.
- Runs within a probe are not independent (most probes give the same answer every run), so the
  probe-level p is the honest one; the run-level p overstates significance.
- A bar of B points cannot pass when the before share is above 100 - B; compare says so.
- capture runs only against a local engine and refuses otherwise (on the laptop nexus resolves to
  the managed service). Every probe is refused before any write while the
  server's guards hold (probes that could write or run work first, an oversized store_put or a
  procedure_run, are left out), but a guard regression would write for real: run it against a
  scratch engine when in doubt. It runs under a fresh session id, so it never touches the calling
  session's T1 or name lease.
- The probes are authored calls carrying real error kinds, not replayed transcripts, and the
  kinds are the ones that survived earlier fixes. Eight are SDK argument-validation errors, which
  a change to the tools' own error text cannot reword.
- The user-level CLAUDE.md may still reach the asked agent; project files do not (empty cwd).

capture runs where a local engine is: ``tests/scripts/probe_capture_entry.py`` runs it under the
suite's self-provisioned engine (on chas: ``uv run pytest tests/scripts/probe_capture_entry.py``
with ``NX_PROBE_OUT`` set). ask and compare run on the laptop, which has ``claude``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_KV = re.compile(r"^\s*[A-Za-z_][\w.]*\s*(=|>=|<=|>|<|!=)\s*\S")
_OPERATOR = re.compile(r"(>=|<=|>|<|!=)")
#: Share of asks that may fail to parse before ``ask`` refuses its own run.
MAX_PARSE_FAILURES = 0.1


@dataclass(frozen=True)
class Probe:
    id: str
    tool: str
    args: dict[str, Any]
    goal: str
    expect: str  # a substring the captured error must contain
    fixed: Callable[[str, dict], bool]
    #: Why this probe cannot write, for a probe whose tool can (checked by the tests).
    refused_by: str = ""


def _holdable(to: str) -> bool:
    """A name a session can hold: the server's own rule (``nexus.tuple_directory.SAFE_NAME_RE``)."""
    from nexus.tuple_directory import SAFE_NAME_RE  # noqa: PLC0415 — stdlib-only module, no store

    return bool(SAFE_NAME_RE.fullmatch(to))


def _where_ok(a: dict) -> bool:
    return not a.get("where") or bool(_KV.match(str(a["where"])))


PROBES: list[Probe] = [
    Probe("search-where-word", "search", {"query": "attention mechanisms", "where": "attention"},
          "Find passages about attention mechanisms.", "Invalid where format",
          lambda t, a: t == "search" and _where_ok(a)),
    Probe("search-where-title", "search", {"query": "a unified theory of cerebral cortex", "where": "Horizontal"},
          "Find the paper on a unified theory of the cerebral cortex.", "Invalid where format",
          lambda t, a: t == "search" and _where_ok(a)),
    Probe("mailbox-message-arg", "mailbox_send", {"to": "00000000-0000-4000-8000-000000000000", "message": "hello"},
          "Send the text 'hello' to that session.", "needs a body",
          lambda t, a: t == "mailbox_send" and bool(a.get("body")),
          refused_by="no body: returns before resolving any address"),
    Probe("memory-put-ttl-string", "memory_put",
          {"content": "note", "project": "probe", "title": "t", "ttl": "permanent"},
          "Store a permanent note titled t in project probe.", "valid integer",
          lambda t, a: t == "memory_put" and (a.get("ttl") is None or (isinstance(a["ttl"], int) and a["ttl"] > 0)),
          refused_by="argument validation"),
    Probe("memory-get-no-project", "memory_get", {"title": "push-train"},
          "Read the push-train note of the nexus project.", "project",
          lambda t, a: t == "memory_get" and bool(a.get("project"))),
    Probe("memory-put-no-title", "memory_put", {"content": "note", "project": "probe"},
          "Store a note in project probe.", "title",
          lambda t, a: t == "memory_put" and bool(a.get("title")), refused_by="argument validation"),
    Probe("tuple-subscribe-no-subspace", "tuple_subscribe", {},
          "Subscribe to the mailbox of the name tamsin.", "subspace",
          lambda t, a: t == "tuple_subscribe" and bool(a.get("subspace")), refused_by="argument validation"),
    Probe("tuple-rd-unregistered", "tuple_rd", {"subspace": "ledger/probe-v0174", "keys_pattern": {}},
          "Read the rows of the probe ledger.", "no template registered",
          lambda t, a: t == "tuple_registry" or (t == "tuple_rd" and a.get("subspace") != "ledger/probe-v0174")),
    Probe("tuple-ack-missing", "tuple_ack", {},
          "Acknowledge claim c-123, which you hold as claimant tamsin.", "claim_id",
          lambda t, a: t == "tuple_ack" and a.get("claim_id") == "c-123" and a.get("claimant") == "tamsin",
          refused_by="argument validation"),
    Probe("tuple-out-no-keys", "tuple_out", {"subspace": "board/probe", "body": "status"},
          "Post a status row to the probe board.", "keys",
          lambda t, a: t == "tuple_registry" or (t == "tuple_out" and isinstance(a.get("keys"), dict) and bool(a["keys"])),
          refused_by="argument validation"),
    Probe("tuple-rd-no-subspace", "tuple_rd", {"keys_pattern": {}},
          "Read the rows of the board/ci subspace.", "subspace",
          lambda t, a: t == "tuple_rd" and bool(a.get("subspace"))),
    Probe("store-put-placeholder", "store_put", {"content": "a fact", "collection": "knowledge", "title": "fact"},
          "Store this fact as permanent knowledge about distributed systems.", "placeholder",
          lambda t, a: t == "store_put" and a.get("collection") not in ("knowledge", "default", "notes", "tmp", "test"),
          refused_by="placeholder refusal before any engine call"),
    Probe("store-get-many-no-ids", "store_get_many", {},
          "Fetch documents abc and def.", "ids",
          # ids takes a comma-separated string or a list (the tool's own schema)
          lambda t, a: t == "store_get_many" and isinstance(a.get("ids"), (list, str)) and bool(a["ids"])),
    Probe("scratch-unknown-action", "scratch", {"action": "read", "entry_id": "abc"},
          "Read scratch entry abc.", "unknown action",
          lambda t, a: t == "scratch" and a.get("action") == "get" and a.get("entry_id") == "abc"),
    Probe("scratch-get-no-id", "scratch", {"action": "get"},
          "Read the scratch entry whose id is abc.", "entry_id is required",
          lambda t, a: t == "scratch" and a.get("action") == "get" and a.get("entry_id") == "abc"),
    Probe("metadata-scoped-operator", "search_metadata_scoped", {"query": "consensus", "where": "year>=2020"},
          "Find recent papers on consensus.", "equality-only",
          lambda t, a: t in ("search_metadata_scoped", "search", "query")
          and not (t == "search_metadata_scoped" and _OPERATOR.search(str(a.get("where", ""))))),
    Probe("aspect-scoped-unknown-field", "search_aspect_scoped", {"query": "consensus", "field": "colour", "value": "red"},
          "Find documents whose extracted aspects mention red.", "not a known aspect field",
          lambda t, a: (t == "search_aspect_scoped" and a.get("field") not in (None, "", "colour"))
          or t in ("search", "query")),
    Probe("store-get-empty-id", "store_get", {"doc_id": ""}, "Read the document titled push-train.",
          "doc_id is required", lambda t, a: t == "store_get" and bool(a.get("doc_id"))),
    Probe("mailbox-name-with-spaces", "mailbox_send", {"to": "Seminar seat: Bullock", "body": "hello"},
          "Send hello to the session that holds the seminar seat for Bullock.", "Seminar seat: Bullock",
          lambda t, a: (t == "mailbox_send" and _holdable(str(a.get("to", ""))))
          or t in ("tuple_rd", "tuple_list", "tuple_registry"),
          refused_by="resolve_send_address refuses a name no session can hold before any directory read"),
    Probe("query-catalog-where-operator", "query", {"question": "consensus", "author": "Lamport", "where": "year>=2020"},
          "Find documents by Lamport on consensus from 2020 on.", "operator-shaped",
          # search's where takes KEY=VALUE and comparison operators, so moving the author and the year there is a fix
          lambda t, a: (t == "query" and not (a.get("author") and _OPERATOR.search(str(a.get("where", "")))))
          or (t == "search" and "author=" in str(a.get("where", "")))),
]


def probe_set_hash() -> str:
    return hashlib.sha256(json.dumps([[p.id, p.tool, p.args, p.goal] for p in PROBES], sort_keys=True,
                                     default=repr).encode()).hexdigest()[:16]


def _text(result: Any) -> str:
    return "\n".join(getattr(c, "text", "") for c in getattr(result, "content", []) or [])


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def local_engine_refusal() -> str:
    """Why capture must not run here, or "" when the endpoint is a local engine.

    The probes are reads and refused writes, but they are still calls to whatever store this
    process resolves, and the laptop resolves to the cloud (2026-11-11: five unannounced runs
    there). Only a local engine is allowed."""
    from urllib.parse import urlparse  # noqa: PLC0415

    from nexus.config import get_service_url, is_local_mode  # noqa: PLC0415 — config only, no store

    if not is_local_mode():
        return "nexus is not in local mode here, so capture would reach the managed service"
    # The client reads NX_SERVICE_URL and then config.yml (get_service_url), and NX_LOCAL=1 makes
    # is_local_mode True even with a cloud service_url configured: check what the client would use.
    url = get_service_url()
    if url and urlparse(url).hostname not in _LOOPBACK:
        return f"the configured service_url names {urlparse(url).hostname!r}, not a local engine"
    return ""


def capture(out: Path) -> int:
    refusal = local_engine_refusal()
    if refusal:
        sys.stderr.write(f"refused: {refusal}. Run capture against a local engine "
                         "(tests/scripts/probe_capture_entry.py on chas provisions one).\n")
        return 1
    # A fresh session id: the server's lifespan mints T1 and arms a name lease for whatever
    # session the environment names, and must never touch the calling session's.
    os.environ["NX_SESSION_ID"] = str(uuid.uuid4())
    os.environ["NX_NO_TELEMETRY"] = "1"
    import anyio  # noqa: PLC0415 — only this subcommand needs the server
    from mcp import Client  # noqa: PLC0415

    from nexus.mcp.core import mcp  # noqa: PLC0415

    async def _run() -> tuple[dict[str, Any], list[str]]:
        rows: dict[str, Any] = {}
        async with Client(mcp, mode="legacy") as client:
            schemas = {t.name: t.input_schema for t in (await client.list_tools()).tools}
            for p in PROBES:
                if p.tool not in schemas:
                    rows[p.id] = {"ok": False, "why": f"no tool {p.tool}"}
                    continue
                r = await client.call_tool(p.tool, p.args)
                text = _text(r)
                ok = bool(r.is_error) and p.expect in text
                rows[p.id] = {"tool": p.tool, "ok": ok, "error": text, "schema": schemas[p.tool],
                              "why": "" if ok else f"want an error containing {p.expect!r}"}
        return rows, sorted(schemas)

    rows, tools = anyio.run(_run)
    bad = [k for k, v in rows.items() if not v["ok"]]
    manifest = {"captured_at": datetime.now(UTC).isoformat(), "git": _git_sha(), "probe_set": probe_set_hash(),
                "tools": tools}
    out.write_text(json.dumps({"manifest": manifest, "probes": rows}, indent=1), encoding="utf-8")
    for k, v in rows.items():
        sys.stdout.write(f"{k}: {'ok' if v['ok'] else 'BAD ' + v['why']}: {v.get('error', '')[:120]!r}\n")
    sys.stdout.write(f"{len(PROBES) - len(bad)} of {len(PROBES)} probes fail with their expected error\n")
    return 1 if bad else 0


_PROMPT = """You are an agent using the nexus MCP server. Your goal: {goal}

You just called the tool `{tool}` with these arguments:
{args}

It failed. This is the error result exactly as the server returned it:
{error}

The tool's input schema:
{schema}

Decide your next tool call to reach the goal. You may call this tool again or a different nexus tool.
Reply with ONLY one JSON object, no prose: {{"tool": "<tool name without any prefix>", "arguments": {{...}}}}"""

PROMPT_HASH: str = hashlib.sha256(_PROMPT.encode()).hexdigest()[:16]


def _ask_once(prompt: str, model: str, cwd: str) -> dict[str, Any]:
    argv = ["claude", "-p", prompt, "--model", model, "--tools", "", "--strict-mcp-config",
            "--setting-sources", "", "--no-session-persistence", "--output-format", "json"]
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "NX_SESSION_ID")}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False, cwd=cwd, env=env)  # noqa: S603
    except subprocess.TimeoutExpired:
        return {"parse": "timeout"}
    try:
        reply = json.loads(proc.stdout).get("result", "")
    except ValueError:
        return {"parse": f"no json envelope (rc {proc.returncode})"}
    m = re.search(r"\{.*\}", reply or "", re.DOTALL)
    try:
        call = json.loads(m.group(0)) if m else None
    except ValueError:
        call = None
    if not isinstance(call, dict):
        return {"parse": "no call in reply"}
    tool = str(call.get("tool", "")).removeprefix("mcp__plugin_conexus_nexus__")
    args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    return {"tool": tool, "arguments": args}


def score(probe: Probe, nxt: dict[str, Any], tools: set[str]) -> bool:
    """A proposal is fixed only when it names a registered tool and the probe's rule accepts it."""
    return "tool" in nxt and nxt["tool"] in tools and bool(probe.fixed(nxt["tool"], nxt["arguments"]))


def ask(errors: Path, runs: int, out: Path, model: str) -> int:
    data = json.loads(errors.read_text(encoding="utf-8"))
    if data["manifest"]["probe_set"] != probe_set_hash():
        sys.stderr.write("refused: the errors file was captured for another probe set; capture again\n")
        return 1
    bad = [k for k, v in data["probes"].items() if not v.get("ok")]
    if bad:
        sys.stderr.write(f"refused: the errors file has probes that did not capture their expected error: {bad}\n")
        return 1
    tools = set(data["manifest"]["tools"])
    by_id = {p.id: p for p in PROBES}
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="nx-probe-") as cwd:
        for pid, row in data["probes"].items():
            p = by_id[pid]
            shown = {k: (v if len(json.dumps(v)) < 400 else f"<{len(str(v))} characters>") for k, v in p.args.items()}
            prompt = _PROMPT.format(goal=p.goal, tool=p.tool, args=json.dumps(shown), error=row["error"],
                                    schema=json.dumps(row["schema"]))
            for run in range(runs):
                nxt = _ask_once(prompt, model, cwd)
                ok = score(p, nxt, tools)
                results.append({"probe": pid, "run": run, "fixed": ok, "next_tool": nxt.get("tool"),
                                "next_arguments": nxt.get("arguments"), "parse": nxt.get("parse")})
                sys.stdout.write(f"{pid} run {run}: {'fixed' if ok else 'not fixed'} ({nxt.get('tool') or nxt.get('parse')})\n")
    failures = sum(1 for r in results if r["parse"])
    fixed = sum(r["fixed"] for r in results)
    manifest = {"asked_at": datetime.now(UTC).isoformat(), "git": _git_sha(), "model": model, "runs": runs,
                "probe_set": probe_set_hash(), "prompt": PROMPT_HASH, "errors_git": data["manifest"]["git"],
                "parse_failures": failures}
    out.write_text(json.dumps({"manifest": manifest, "results": results}, indent=1), encoding="utf-8")
    sys.stdout.write(f"fixed {fixed} of {len(results)} ({fixed / max(len(results), 1):.0%}); parse failures {failures}\n")
    if failures > MAX_PARSE_FAILURES * len(results):
        sys.stderr.write(f"refused: {failures} of {len(results)} asks did not parse; the run is not a measurement\n")
        return 1
    return 0


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p for b before-only and c after-only fixes."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def compare(before: Path, after: Path, bar: int = 20) -> int:
    bd, ad = json.loads(before.read_text()), json.loads(after.read_text())
    for key in ("model", "probe_set", "prompt", "runs"):
        if bd["manifest"].get(key) != ad["manifest"].get(key):
            sys.stderr.write(f"refused: the runs differ in {key} ({bd['manifest'].get(key)} vs {ad['manifest'].get(key)})\n")
            return 1
    a = {(r["probe"], r["run"]): r["fixed"] for r in bd["results"]}
    z = {(r["probe"], r["run"]): r["fixed"] for r in ad["results"]}
    pairs = sorted(set(a) & set(z))
    if not pairs:
        sys.stderr.write("refused: no paired runs\n")
        return 1
    n = len(pairs)
    fixed_a, fixed_z = sum(a[k] for k in pairs), sum(z[k] for k in pairs)
    b = sum(a[k] and not z[k] for k in pairs)
    c = sum(z[k] and not a[k] for k in pairs)
    probes = sorted({k[0] for k in pairs})

    def _majority(d: dict, pid: str) -> bool:
        votes = [d[k] for k in pairs if k[0] == pid]
        return sum(votes) * 2 > len(votes)

    pb = sum(_majority(a, p) and not _majority(z, p) for p in probes)
    pc = sum(_majority(z, p) and not _majority(a, p) for p in probes)
    probe_p = mcnemar_exact(pb, pc)
    reachable = (n - fixed_a) * 100 >= bar * n
    passes = (fixed_z - fixed_a) * 100 >= bar * n and probe_p < 0.05
    sys.stdout.write(json.dumps({
        "pairs": n, "probes": len(probes), "before": round(fixed_a / n, 3), "after": round(fixed_z / n, 3),
        "rise_points": round((fixed_z - fixed_a) * 100 / n, 1), "bar_points": bar,
        "probe_level": {"before_only": pb, "after_only": pc, "mcnemar_p": round(probe_p, 4)},
        "run_level": {"before_only": b, "after_only": c, "mcnemar_p": round(mcnemar_exact(b, c), 4)},
        "bar_reachable": reachable, "passes": passes,
    }) + "\n")
    if not reachable:
        sys.stderr.write(f"note: the before share leaves less than {bar} points to gain, so the bar cannot pass\n")
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
    k.add_argument("--model", required=True)
    k.add_argument("--runs", type=int, default=3)
    k.add_argument("--out", type=Path, required=True)
    m = sub.add_parser("compare")
    m.add_argument("before", type=Path)
    m.add_argument("after", type=Path)
    m.add_argument("--bar", type=int, default=20)
    a = ap.parse_args(argv)
    if a.cmd == "capture":
        return capture(a.out)
    if a.cmd == "ask":
        return ask(a.errors, a.runs, a.out, a.model)
    return compare(a.before, a.after, a.bar)


if __name__ == "__main__":
    sys.exit(main())
