#!/usr/bin/env python3
"""Decision-coverage census (bead nexus-4bqre.1) — PRE-INTERVENTION BASELINE.

Measures sequential-thinking compliance per agent type from Claude Code
transcripts. Read-only. No repo writes.

DEFINITIONS (frozen 2026-08-22; do not change without versioning the schema)
  run       one transcript file. Top-level = <ROOT>/<session-uuid>.jsonl;
            subagent = <ROOT>/<session-uuid>/subagents/agent-<id>.jsonl
  tool call an assistant-record content block with type == "tool_use"
  thought   a tool call whose name contains "sequentialthinking"
  mutation  a tool call whose name is in MUT (Edit/Write/NotebookEdit/MultiEdit)
  MUT_EXT   mutation OR a serena in-place editor (replace_in_files,
            replace_symbol_body, insert_before_symbol, insert_after_symbol).
            Reported separately so a later run can detect mutation leaking
            out of the primary set.
  adjacency a mutation whose IMMEDIATELY PRECEDING tool call in the run's
            ordered tool sequence is a thought
  front-half a thought at 0-based index i of a run of N tool calls with
            i < N/2. Share = front-half thoughts / all thoughts.
  window    a run is IN WINDOW if its FIRST record timestamp falls in
            [start, end). Whole runs only — never split, because splitting
            corrupts the front-half denominator.

ATTRIBUTION
  subagents  primary: sibling agent-<id>.meta.json -> "agentType".
             fallback: the record field "attributionAgent".
             cross-check: START rows in the RDR-184 expectations ledger.
  top level  the literal type "__toplevel__". Records with
             isSidechain == true are EXCLUDED from top-level runs (they are
             subagent traffic replayed into the session file).
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import os
import sys
from datetime import datetime, timezone

ROOT_DEFAULT = os.path.expanduser(
    "~/.claude/projects/-Users-hal-hildebrand-git-nexus"
)
LEDGER_DEFAULT = os.path.expanduser(
    "~/.local/state/nexus/orchestration-archive"
)

MUT = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
SERENA_MUT = {
    "mcp__plugin_sn_serena__replace_in_files",
    "mcp__plugin_sn_serena__replace_symbol_body",
    "mcp__plugin_sn_serena__insert_before_symbol",
    "mcp__plugin_sn_serena__insert_after_symbol",
}
THOUGHT_SUBSTR = "sequentialthinking"
TOPLEVEL = "__toplevel__"


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def file_digest(path, nbytes=None):
    """sha256 over the first nbytes (or all) bytes, plus the byte length used."""
    import hashlib

    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        while True:
            want = 65536 if nbytes is None else min(65536, nbytes - total)
            if want <= 0:
                break
            chunk = fh.read(want)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


def tool_sequence(path, drop_sidechain, nbytes=None):
    """Ordered list of tool names, plus the run's first record timestamp.

    nbytes pins the read to a byte prefix so a LIVE, still-appending
    transcript yields the same answer on every re-run.
    """
    seq = []
    first_ts = None
    # nexus-4bqre.1: the pin is a BYTE prefix (file_digest hashes bytes), so
    # it must be re-read as bytes. A text-mode read(nbytes) consumes nbytes
    # CHARACTERS and silently overshoots the pinned prefix on any file with
    # multibyte content -- which is every transcript here. Harmless while the
    # file has not grown (the read just hits EOF early), but on a file that
    # HAS grown since the pin it pulls in records the pin excluded, which is
    # precisely the live-append race the pin exists to defeat. Found when the
    # checked-in script reproduced the frozen baseline off by one tool call.
    if nbytes is not None:
        with open(path, "rb") as fh:
            body = fh.read(nbytes).decode("utf-8", errors="replace")
        lines = body.split("\n")
        fh = None
    else:
        fh = open(path, errors="replace")
        lines = fh
    try:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ts = parse_ts(rec.get("timestamp"))
            if first_ts is None and ts is not None:
                first_ts = ts
            if rec.get("type") != "assistant":
                continue
            if drop_sidechain and rec.get("isSidechain"):
                continue
            for blk in rec.get("message", {}).get("content", []) or []:
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    seq.append(blk.get("name") or "")
    finally:
        if fh is not None:
            fh.close()
    return seq, first_ts


def ledger_types(ledger_dir):
    """agent id -> agent type, from START rows in *.expectations."""
    out = {}
    for path in sorted(glob.glob(os.path.join(ledger_dir, "*.expectations"))):
        with open(path, errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 4 and parts[1] == "START":
                    out[parts[2]] = parts[3]
    return out


def meta_type(agent_path):
    meta = agent_path[: -len(".jsonl")] + ".meta.json"
    if not os.path.exists(meta):
        return None
    try:
        with open(meta, errors="replace") as fh:
            return (json.load(fh) or {}).get("agentType")
    except (ValueError, OSError):
        return None


def record_attribution(agent_path):
    try:
        with open(agent_path, errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                val = rec.get("attributionAgent")
                if val:
                    return val
    except OSError:
        pass
    return None


class Acc:
    __slots__ = (
        "runs", "zero_runs", "tools", "thoughts", "muts", "muts_ext",
        "adj", "front", "run_muts",
    )

    def __init__(self):
        self.runs = 0
        self.zero_runs = 0
        self.tools = 0
        self.thoughts = 0
        self.muts = 0
        self.muts_ext = 0
        self.adj = 0
        self.front = 0
        self.run_muts = []


def accumulate(acc, seq):
    n = len(seq)
    acc.runs += 1
    acc.tools += n
    n_thought = 0
    n_mut = 0
    for i, name in enumerate(seq):
        is_thought = THOUGHT_SUBSTR in name
        if is_thought:
            n_thought += 1
            if i < n / 2:
                acc.front += 1
        if name in MUT:
            n_mut += 1
            acc.muts += 1
            if i > 0 and THOUGHT_SUBSTR in seq[i - 1]:
                acc.adj += 1
        if name in MUT or name in SERENA_MUT:
            acc.muts_ext += 1
    acc.thoughts += n_thought
    if n_thought == 0:
        acc.zero_runs += 1
    acc.run_muts.append(n_mut)


def metrics(acc):
    return {
        "runs": acc.runs,
        "tool_calls": acc.tools,
        "thoughts": acc.thoughts,
        "mutations": acc.muts,
        "mutations_ext": acc.muts_ext,
        "thoughts_per_mutation": (
            round(acc.thoughts / acc.muts, 4) if acc.muts else None
        ),
        "thoughts_per_100_tools": (
            round(100.0 * acc.thoughts / acc.tools, 3) if acc.tools else None
        ),
        "pct_runs_zero_thoughts": (
            round(100.0 * acc.zero_runs / acc.runs, 2) if acc.runs else None
        ),
        "adjacency_pct": (
            round(100.0 * acc.adj / acc.muts, 3) if acc.muts else None
        ),
        "adjacent_mutations": acc.adj,
        "front_half_share_pct": (
            round(100.0 * acc.front / acc.thoughts, 2) if acc.thoughts else None
        ),
        "front_half_thoughts": acc.front,
    }


def run_census(root, ledger_dir, start, end, min_tools, attribution,
               pin=None):
    """pin: optional dict relpath -> [sha256, nbytes] freezing the input set.

    When pin is given, ONLY the pinned files are read, each truncated to its
    recorded byte length, and a sha mismatch is reported. That makes the
    artifact byte-reproducible even though the live session's transcript is
    still being appended to while the census runs.
    """
    led = ledger_types(ledger_dir) if os.path.isdir(ledger_dir) else {}
    accs = collections.defaultdict(Acc)
    inputs = {"toplevel": [], "subagent": []}
    manifest = {}
    mismatches = []
    untyped = 0
    typed_by = collections.Counter()

    def pinned_bytes(rel):
        if pin is None:
            return None
        entry = pin.get(rel)
        return None if entry is None else entry[1]

    def note(rel, path, nbytes):
        digest, used = file_digest(path, nbytes)
        manifest[rel] = [digest, used]
        if pin is not None and rel in pin and pin[rel][0] != digest:
            mismatches.append(rel)

    for path in sorted(glob.glob(os.path.join(root, "*.jsonl"))):
        rel = os.path.relpath(path, root)
        if pin is not None and rel not in pin:
            continue
        nb = pinned_bytes(rel)
        seq, first_ts = tool_sequence(path, drop_sidechain=True, nbytes=nb)
        if first_ts is None or not (start <= first_ts < end):
            continue
        if len(seq) < min_tools:
            continue
        accumulate(accs[TOPLEVEL], seq)
        inputs["toplevel"].append(rel)
        note(rel, path, nb)

    for path in sorted(
        glob.glob(os.path.join(root, "*", "subagents", "agent-*.jsonl"))
    ):
        rel = os.path.relpath(path, root)
        if pin is not None and rel not in pin:
            continue
        agent_id = os.path.basename(path)[len("agent-"):-len(".jsonl")]
        if attribution == "ledger":
            atype = led.get(agent_id)
            src = "ledger" if atype else None
        else:
            atype = meta_type(path)
            src = "meta" if atype else None
            if not atype:
                atype = record_attribution(path)
                src = "record" if atype else None
            if not atype:
                atype = led.get(agent_id)
                src = "ledger" if atype else None
        if not atype:
            untyped += 1
            continue
        nb = pinned_bytes(rel)
        seq, first_ts = tool_sequence(path, drop_sidechain=False, nbytes=nb)
        if first_ts is None or not (start <= first_ts < end):
            continue
        if len(seq) < min_tools:
            continue
        typed_by[src] += 1
        accumulate(accs[atype], seq)
        inputs["subagent"].append(rel)
        note(rel, path, nb)

    out = {
        "schema": "decision-coverage-census/v1",
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "transcript_root": root,
        "ledger_archive": ledger_dir,
        "attribution_mode": attribution,
        "attribution_source_counts": dict(typed_by),
        "untyped_subagents_skipped": untyped,
        "min_tools_per_run": min_tools,
        "mutation_set": sorted(MUT),
        "mutation_ext_set": sorted(MUT | SERENA_MUT),
        "thought_match": THOUGHT_SUBSTR,
        "by_agent_type": {k: metrics(v) for k, v in sorted(accs.items())},
        "input_counts": {
            "toplevel_runs": len(inputs["toplevel"]),
            "subagent_runs": len(inputs["subagent"]),
        },
    }
    totals = Acc()
    for a in accs.values():
        totals.runs += a.runs
        totals.zero_runs += a.zero_runs
        totals.tools += a.tools
        totals.thoughts += a.thoughts
        totals.muts += a.muts
        totals.muts_ext += a.muts_ext
        totals.adj += a.adj
        totals.front += a.front
    out["TOTAL"] = metrics(totals)
    out["pin_mismatches"] = sorted(mismatches)
    out["_inputs"] = inputs
    out["_manifest"] = manifest
    return out


def table(res, min_runs=1):
    hdr = (
        f"{'agent type':34s} {'runs':>5s} {'tools':>7s} {'muts':>6s} "
        f"{'thts':>5s} {'t/mut':>7s} {'t/100':>6s} {'%zero':>6s} "
        f"{'adj%':>6s} {'frnt%':>6s}"
    )
    lines = [hdr, "-" * len(hdr)]
    rows = list(res["by_agent_type"].items())
    rows.sort(key=lambda kv: -(kv[1]["thoughts_per_100_tools"] or 0))
    for name, m in rows:
        if m["runs"] < min_runs:
            continue
        def f(v, w, p):
            return f"{v:{w}.{p}f}" if v is not None else f"{'-':>{w}s}"
        lines.append(
            f"{name:34s} {m['runs']:5d} {m['tool_calls']:7d} "
            f"{m['mutations']:6d} {m['thoughts']:5d} "
            f"{f(m['thoughts_per_mutation'],7,3)} "
            f"{f(m['thoughts_per_100_tools'],6,2)} "
            f"{f(m['pct_runs_zero_thoughts'],6,1)} "
            f"{f(m['adjacency_pct'],6,2)} "
            f"{f(m['front_half_share_pct'],6,1)}"
        )
    m = res["TOTAL"]
    lines.append("-" * len(hdr))
    lines.append(
        f"{'ALL':34s} {m['runs']:5d} {m['tool_calls']:7d} {m['mutations']:6d} "
        f"{m['thoughts']:5d} {m['thoughts_per_mutation']:7.3f} "
        f"{m['thoughts_per_100_tools']:6.2f} "
        f"{m['pct_runs_zero_thoughts']:6.1f} {m['adjacency_pct']:6.2f} "
        f"{m['front_half_share_pct']:6.1f}"
    )
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT_DEFAULT)
    ap.add_argument("--ledger", default=LEDGER_DEFAULT)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--min-tools", type=int, default=0)
    ap.add_argument(
        "--attribution", choices=["meta", "ledger"], default="meta"
    )
    ap.add_argument("--min-runs", type=int, default=1)
    ap.add_argument("--json-out", default="")
    ap.add_argument("--min-typed-agents", type=int, default=50)
    ap.add_argument("--min-mutations", type=int, default=500)
    ap.add_argument(
        "--pin",
        default="",
        help="a previous run's JSON; re-reads exactly its input set, each "
             "truncated to the recorded byte length",
    )
    args = ap.parse_args()

    pin = None
    if args.pin:
        with open(args.pin) as fh:
            pin = json.load(fh)["_manifest"]

    start = parse_ts(args.start)
    end = parse_ts(args.end)
    res = run_census(
        args.root, args.ledger, start, end, args.min_tools, args.attribution,
        pin=pin,
    )
    inputs = res.pop("_inputs")
    manifest = res.pop("_manifest")
    if res["pin_mismatches"]:
        print(
            f"PIN MISMATCH on {len(res['pin_mismatches'])} files",
            file=sys.stderr,
        )
    print(table(res, args.min_runs))
    print()
    print(
        f"typed subagent runs={res['input_counts']['subagent_runs']} "
        f"toplevel runs={res['input_counts']['toplevel_runs']} "
        f"untyped skipped={res['untyped_subagents_skipped']} "
        f"attribution={res['attribution_source_counts']}"
    )

    if args.json_out:
        res["_inputs"] = inputs
        res["_manifest"] = manifest
        with open(args.json_out, "w") as fh:
            json.dump(res, fh, indent=2, sort_keys=True)
        print(f"wrote {args.json_out}")

    # non-vacuity floor (nexus-moht0 doctrine)
    typed = res["input_counts"]["subagent_runs"]
    muts = res["TOTAL"]["mutations"]
    if typed < args.min_typed_agents:
        print(
            f"VACUOUS: only {typed} typed agents (< {args.min_typed_agents}) "
            "— likely swept ledger or wrong transcript root",
            file=sys.stderr,
        )
        return 2
    if muts < args.min_mutations:
        print(
            f"VACUOUS: only {muts} mutations (< {args.min_mutations})",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
