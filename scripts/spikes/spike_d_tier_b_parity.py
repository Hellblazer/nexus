#!/usr/bin/env -S uv run python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Spike D — tier-B tool-use A/B parity harness: claude_agent vs qwen_agent.

Follow-on to PR #796 (``qwen_agent_dispatch`` + ``nx_enrich_beads``
opt-in routing via ``NEXUS_TIER_B_DISPATCHER=qwen_agent``). Tier-B
tools (``nx_tidy``, ``nx_enrich_beads``, ``nx_plan_audit``) differ
structurally from operator-tier oneshot extractors: their prompts
invite mid-loop MCP tool-use, so spike_c's prompt-in/JSON-out shape
does not fit. This script ships the new harness.

Routing reality
---------------

As of the tier-B completion PR (follow-on to #796/#799), all three
tools (``nx_enrich_beads``, ``nx_tidy``, ``nx_plan_audit``) honor
``NEXUS_TIER_B_DISPATCHER=qwen_agent``. Earlier revisions of this
harness short-circuited ``nx_tidy`` / ``nx_plan_audit`` with
``qwen_agent_skipped: true``; that skip is now opt-in via the
``--skip-unwired`` flag (kept for reproducing the original bench), and
the aggregation / reporting code that handles the skipped state is
retained for backward-compat with earlier JSONL records.

Three-axis metric
-----------------

* **Semantic** — prose fields (``summary``, ``verdict``,
  ``enriched_description``). Length-ratio heuristic (>=0.5) mirrors
  spike_c. LLM-judge generalisation is a separate follow-on script
  (we already have ``judge_aspect_diffs.py`` for spike_c — it gets
  generalised after this PR lands).
* **Structural** — Jaccard overlap on canonical set fields per tool:
    - ``nx_enrich_beads``: ``key_files``, ``test_commands``, ``constraints``
    - ``nx_tidy``: ``actions`` (Jaccard on the JSON-serialised action dicts)
    - ``nx_plan_audit``: ``findings`` (Jaccard on the JSON-serialised
      finding dicts)
* **Tool-call count** — only surfaced on the qwen leg via the
  ``budget.tool_calls`` field PR #796 wired into qwen_oneshot's
  return. Captured by monkey-patching ``_extract_oneshot_result`` to
  stash the full payload, then reading ``budget.tool_calls`` off it
  before the dispatcher discards everything except ``parsed``.

Implementation notes
--------------------

The MCP tools (``nx_enrich_beads`` & friends) stringify their inner
parsed dict before returning. To recover structural fields, the
harness monkey-patches the two dispatchers — ``claude_dispatch`` and
``qwen_agent_dispatch`` — with capture-wrappers that record the
parsed dict before forwarding. This keeps the prompts/schemas
exactly as production constructs them (no DRY duplication of prompt
text in the harness), at the cost of a small in-process patch
window.

Manifest shape
--------------

Operator-supplied JSON list (no fixture corpus shipped with the PR)::

    [
      {
        "name": "enrich-simple-bead",
        "tool": "nx_enrich_beads",
        "input": {
          "bead_description": "Add SQLite-WAL backed cache for ...",
          "context": ""
        },
        "tags": ["smoke"]
      },
      {
        "name": "tidy-chroma-quotas",
        "tool": "nx_tidy",
        "input": {"topic": "chromadb quotas", "collection": "knowledge"}
      },
      {
        "name": "audit-rdr-080-plan",
        "tool": "nx_plan_audit",
        "input": {"plan_json": "{...}"}
      }
    ]

Out of scope
------------

* Semantic-equivalence LLM judge — separate script.
* Fixture corpus.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

# ── Per-tool config ──────────────────────────────────────────────────────────

SUPPORTED_TOOLS: tuple[str, ...] = (
    "nx_enrich_beads",
    "nx_tidy",
    "nx_plan_audit",
)

# Historically (PR #797 era) ``nx_tidy`` and ``nx_plan_audit`` were
# unwired to ``NEXUS_TIER_B_DISPATCHER``; the harness recorded
# ``qwen_agent_skipped: true`` for them. As of the tier-B completion
# PR, all three tier-B tools honor the env. This set is empty by
# default; pass ``--skip-unwired`` to re-enable the historical skip
# (useful only for reproducing pre-completion benches).
QWEN_AGENT_UNWIRED_DEFAULT: frozenset[str] = frozenset()

# Per-tool structural fields used for Jaccard overlap.
STRUCTURAL_FIELDS: dict[str, tuple[str, ...]] = {
    "nx_enrich_beads": ("key_files", "test_commands", "constraints"),
    "nx_tidy": ("actions",),
    "nx_plan_audit": ("findings",),
}

# Per-tool prose fields used for length-ratio semantic agreement.
PROSE_FIELDS: dict[str, tuple[str, ...]] = {
    "nx_enrich_beads": ("enriched_description",),
    "nx_tidy": ("summary",),
    "nx_plan_audit": ("verdict", "summary"),
}

PROSE_LEN_TOL: float = 0.5


# ── Agreement helpers ────────────────────────────────────────────────────────


def _to_hashable(item: Any) -> str:
    """Canonicalise a list element for Jaccard. Dicts → sorted-key JSON;
    strings pass through; anything else → ``repr``."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return json.dumps(item, sort_keys=True, ensure_ascii=False)
    return repr(item)


def _jaccard(a: Any, b: Any) -> float:
    """Jaccard overlap between two iterables (after canonicalisation).
    Two empty sets → 1.0 (vacuous agreement)."""
    sa = {_to_hashable(x) for x in (a or [])}
    sb = {_to_hashable(x) for x in (b or [])}
    if not sa and not sb:
        return 1.0
    union = sa | sb
    if not union:
        return 1.0
    return len(sa & sb) / len(union)


def _prose_agree(a: Any, b: Any, tol: float = PROSE_LEN_TOL) -> bool:
    if a is None and b is None:
        return True
    if not a and not b:
        return True
    if not a or not b:
        return False
    la, lb = len(a), len(b)
    if max(la, lb) == 0:
        return True
    return min(la, lb) / max(la, lb) >= tol


def diff_payloads(
    tool: str,
    claude_payload: dict | None,
    qwen_payload: dict | None,
) -> dict[str, Any]:
    """Compute structural + prose agreement between two parsed payloads.

    Returns dict with: ``claude_ok``, ``qwen_ok``, ``both_ok``,
    ``structural`` (per-field Jaccard), ``prose`` (per-field bool).
    """
    out: dict[str, Any] = {
        "claude_ok": isinstance(claude_payload, dict),
        "qwen_ok": isinstance(qwen_payload, dict),
        "both_ok": (
            isinstance(claude_payload, dict)
            and isinstance(qwen_payload, dict)
        ),
        "structural": {},
        "prose": {},
    }
    if not out["both_ok"]:
        for f in STRUCTURAL_FIELDS.get(tool, ()):
            out["structural"][f] = 0.0
        for f in PROSE_FIELDS.get(tool, ()):
            out["prose"][f] = False
        return out
    for f in STRUCTURAL_FIELDS.get(tool, ()):
        out["structural"][f] = _jaccard(
            claude_payload.get(f), qwen_payload.get(f),
        )
    for f in PROSE_FIELDS.get(tool, ()):
        out["prose"][f] = _prose_agree(
            claude_payload.get(f), qwen_payload.get(f),
        )
    return out


# ── Dispatch capture ─────────────────────────────────────────────────────────


class _Capture:
    """Module-level capture buffer for the last dispatched payload + budget.

    Both ``claude_dispatch`` and ``qwen_agent_dispatch`` return the
    *parsed* dict; ``qwen_agent_dispatch`` additionally has access to
    the raw ``budget`` block but discards it after logging. We patch
    each dispatcher with a thin wrapper that stashes the parsed dict
    here, and for the qwen path we *also* patch
    ``_extract_oneshot_result`` to stash the budget before its caller
    moves on.
    """

    def __init__(self) -> None:
        self.payload: dict | None = None
        self.budget: dict | None = None

    def reset(self) -> None:
        self.payload = None
        self.budget = None


def _install_captures(capture: _Capture) -> list[Any]:
    """Monkey-patch the two dispatchers + qwen oneshot extractor.

    Returns a list of ``(module, attr, original)`` triples for
    teardown. Caller invokes :func:`_uninstall_captures` after the
    dispatch leg completes.
    """
    from nexus.operators import dispatch as _dispatch_mod
    from nexus.operators import qwen_agent_dispatch as _qa_mod

    originals: list[tuple[Any, str, Any]] = []

    orig_claude = _dispatch_mod.claude_dispatch

    async def _wrapped_claude(*args: Any, **kwargs: Any) -> Any:
        result = await orig_claude(*args, **kwargs)
        if isinstance(result, dict):
            capture.payload = result
        return result

    _dispatch_mod.claude_dispatch = _wrapped_claude  # type: ignore[assignment]
    originals.append((_dispatch_mod, "claude_dispatch", orig_claude))

    orig_qa = _qa_mod.qwen_agent_dispatch

    async def _wrapped_qa(*args: Any, **kwargs: Any) -> Any:
        result = await orig_qa(*args, **kwargs)
        if isinstance(result, dict):
            capture.payload = result
        return result

    _qa_mod.qwen_agent_dispatch = _wrapped_qa  # type: ignore[assignment]
    originals.append((_qa_mod, "qwen_agent_dispatch", orig_qa))

    orig_extract = _qa_mod._extract_oneshot_result

    def _wrapped_extract(call_result: Any) -> dict[str, Any]:
        payload = orig_extract(call_result)
        budget = payload.get("budget") if isinstance(payload, dict) else None
        if isinstance(budget, dict):
            capture.budget = budget
        return payload

    _qa_mod._extract_oneshot_result = _wrapped_extract  # type: ignore[assignment]
    originals.append((_qa_mod, "_extract_oneshot_result", orig_extract))

    return originals


def _uninstall_captures(originals: list[Any]) -> None:
    for mod, attr, orig in originals:
        setattr(mod, attr, orig)


# ── Tool invocation ──────────────────────────────────────────────────────────


def _resolve_tool_fn(tool: str):
    """Resolve the MCP tool's underlying async function (unwrap FastMCP)."""
    from nexus.mcp import core

    obj = getattr(core, tool)
    return getattr(obj, "fn", obj)


async def _run_one_leg(
    tool: str,
    inputs: dict[str, Any],
    backend: str,
) -> dict[str, Any]:
    """Invoke *tool* under one backend leg. Returns a row fragment with
    ``payload``, ``budget``, ``elapsed_ms``, ``error``, ``result_str``.
    """
    # Backend toggle. The 'claude_agent' label maps to the default
    # (env unset / 'claude'); 'qwen_agent' sets the opt-in.
    if backend == "claude_agent":
        os.environ.pop("NEXUS_TIER_B_DISPATCHER", None)
    elif backend == "qwen_agent":
        os.environ["NEXUS_TIER_B_DISPATCHER"] = "qwen_agent"
    else:
        raise ValueError(f"unknown backend: {backend!r}")

    capture = _Capture()
    originals = _install_captures(capture)
    fn = _resolve_tool_fn(tool)

    t0 = time.perf_counter()
    error: str | None = None
    result_str: str | None = None
    try:
        result_str = await fn(**inputs)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    _uninstall_captures(originals)

    tool_calls: int | None = None
    if isinstance(capture.budget, dict):
        tc = capture.budget.get("tool_calls")
        if isinstance(tc, int):
            tool_calls = tc

    return {
        "payload": capture.payload,
        "budget": capture.budget,
        "tool_calls": tool_calls,
        "elapsed_ms": elapsed_ms,
        "error": error,
        "result_str": result_str,
    }


# ── Manifest + CLI ───────────────────────────────────────────────────────────


def _load_cases(path: Path) -> list[dict]:
    """Load + validate the per-case manifest. Raises on malformed entries."""
    if not path.exists():
        raise FileNotFoundError(f"cases manifest not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(
            f"cases manifest must be a JSON array; got {type(data).__name__}"
        )
    for ix, case in enumerate(data):
        if not isinstance(case, dict):
            raise ValueError(f"case[{ix}] is not an object: {case!r}")
        tool = case.get("tool")
        if tool not in SUPPORTED_TOOLS:
            raise ValueError(
                f"case[{ix}] has unsupported / missing tool: {tool!r} "
                f"(supported: {SUPPORTED_TOOLS})"
            )
        if not isinstance(case.get("input"), dict):
            raise ValueError(
                f"case[{ix}] missing 'input' dict (tool={tool!r})"
            )
        case.setdefault("name", f"case-{ix}")
    return data


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="A/B parity harness for tier-B tool-use dispatch "
                    "(claude_agent vs qwen_agent)",
    )
    p.add_argument("--cases", type=Path, required=True,
                   help="JSON manifest of cases (see script docstring).")
    p.add_argument("--out", type=Path, required=True,
                   help="JSONL log output path.")
    p.add_argument("--summary", type=Path,
                   help="Optional markdown summary path (default: <out>.md).")
    p.add_argument("--tool", choices=SUPPORTED_TOOLS,
                   help="Filter to a single tool.")
    p.add_argument("--case",
                   help="Filter to a single case by name.")
    p.add_argument("--limit", type=int, default=0,
                   help="Cap cases (0 = unbounded).")
    p.add_argument("--repeat", type=int, default=1,
                   help="Repeat each case N times for variance (default 1).")
    p.add_argument("--backends", default="claude_agent,qwen_agent",
                   help="Comma-separated backends (default: "
                        "claude_agent,qwen_agent).")
    p.add_argument("--skip-unwired", action="store_true",
                   help="Reproduce the pre-completion bench by "
                        "skipping qwen_agent for nx_tidy and "
                        "nx_plan_audit. As of the tier-B completion "
                        "PR all three tools are wired; this flag is "
                        "retained only for replaying historical runs.")
    return p.parse_args(argv)


# ── Aggregation ──────────────────────────────────────────────────────────────


def _median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def _rate(xs: list[bool]) -> float:
    return (sum(1 for x in xs if x) / len(xs)) if xs else 0.0


def _summarize(records: list[dict]) -> dict:
    """Per-tool aggregation: ok-rate, medians, structural / prose agreement,
    median + max tool-call count (qwen leg only)."""
    by_tool: dict[str, list[dict]] = {}
    for r in records:
        by_tool.setdefault(r["tool"], []).append(r)

    out: dict[str, Any] = {"total": len(records), "by_tool": {}}
    for tool, rows in by_tool.items():
        claude_ms = [r["claude_agent"]["elapsed_ms"] for r in rows
                     if r.get("claude_agent")]
        qwen_ms = [r["qwen_agent"]["elapsed_ms"] for r in rows
                   if r.get("qwen_agent") and not r.get("qwen_agent_skipped")]
        tool_calls = [r["qwen_agent"]["tool_calls"] for r in rows
                      if r.get("qwen_agent")
                      and not r.get("qwen_agent_skipped")
                      and r["qwen_agent"].get("tool_calls") is not None]

        both_ok = [r for r in rows if r.get("diff", {}).get("both_ok")]
        struct_agg: dict[str, list[float]] = {}
        prose_agg: dict[str, list[bool]] = {}
        for r in both_ok:
            for f, v in r["diff"].get("structural", {}).items():
                struct_agg.setdefault(f, []).append(float(v))
            for f, v in r["diff"].get("prose", {}).items():
                prose_agg.setdefault(f, []).append(bool(v))

        out["by_tool"][tool] = {
            "n": len(rows),
            "skipped_qwen_agent": sum(
                1 for r in rows if r.get("qwen_agent_skipped")
            ),
            "claude_ok_rate": _rate([
                bool(r.get("claude_agent") and not r["claude_agent"]["error"])
                for r in rows
            ]),
            "qwen_ok_rate": _rate([
                bool(r.get("qwen_agent") and not r["qwen_agent"]["error"])
                for r in rows if not r.get("qwen_agent_skipped")
            ]),
            "claude_median_ms": _median(claude_ms),
            "qwen_median_ms": _median(qwen_ms),
            "tool_calls_median": _median(tool_calls) if tool_calls else 0.0,
            "tool_calls_max": max(tool_calls) if tool_calls else 0,
            "structural": {
                f: {
                    "mean_jaccard": (sum(v) / len(v)) if v else 0.0,
                    "n": len(v),
                }
                for f, v in struct_agg.items()
            },
            "prose": {
                f: {"agree_rate": _rate(v), "n": len(v)}
                for f, v in prose_agg.items()
            },
        }
    return out


def _render_md(summary: dict, out_path: Path) -> str:
    lines = [
        "# Spike D — tier-B tool-use A/B parity (claude_agent vs qwen_agent)",
        "",
        f"- Total runs: **{summary['total']}**",
        "",
    ]
    for tool, s in sorted(summary["by_tool"].items()):
        lines += [
            f"## `{tool}` (n={s['n']})",
            "",
            f"- Qwen-agent skipped (unwired): **{s['skipped_qwen_agent']}**",
            f"- Claude ok-rate: **{s['claude_ok_rate']:.2%}**",
            f"- Qwen ok-rate:   **{s['qwen_ok_rate']:.2%}**",
            f"- Claude median elapsed: **{s['claude_median_ms']:.0f} ms**",
            f"- Qwen median elapsed:   **{s['qwen_median_ms']:.0f} ms**",
            f"- Qwen tool-calls (median / max): "
            f"**{s['tool_calls_median']:.1f}** / **{s['tool_calls_max']}**",
            "",
            "### Structural agreement (Jaccard, both-ok subset)",
            "",
            "| Field | Mean Jaccard | N |",
            "|---|---|---|",
        ]
        for f, info in sorted(s["structural"].items()):
            lines.append(
                f"| `{f}` | {info['mean_jaccard']:.2%} | {info['n']} |"
            )
        lines += [
            "",
            f"### Prose agreement (len-ratio >= {PROSE_LEN_TOL})",
            "",
            "| Field | Agree rate | N |",
            "|---|---|---|",
        ]
        for f, info in sorted(s["prose"].items()):
            lines.append(
                f"| `{f}` | {info['agree_rate']:.2%} | {info['n']} |"
            )
        lines.append("")
    lines.append(f"_Raw JSONL: `{out_path}`_")
    return "\n".join(lines) + "\n"


# ── Driver ───────────────────────────────────────────────────────────────────


async def _run_case(
    case: dict,
    backends: list[str],
    unwired: frozenset[str] = QWEN_AGENT_UNWIRED_DEFAULT,
) -> dict:
    tool = case["tool"]
    row: dict[str, Any] = {
        "name": case["name"],
        "tool": tool,
        "tags": case.get("tags", []),
        "input": case["input"],
    }
    for backend in backends:
        if backend == "qwen_agent" and tool in unwired:
            row["qwen_agent_skipped"] = True
            row["qwen_agent"] = None
            continue
        leg = await _run_one_leg(tool, case["input"], backend)
        row[backend] = leg
    # Diff only if both legs ran and both produced a payload.
    claude_p = (row.get("claude_agent") or {}).get("payload")
    qwen_p = (row.get("qwen_agent") or {}).get("payload") \
        if not row.get("qwen_agent_skipped") else None
    if claude_p is not None and qwen_p is not None:
        row["diff"] = diff_payloads(tool, claude_p, qwen_p)
    else:
        row["diff"] = {
            "claude_ok": claude_p is not None,
            "qwen_ok": qwen_p is not None,
            "both_ok": False,
            "structural": {},
            "prose": {},
        }
    return row


async def _main_async(args: argparse.Namespace) -> int:
    cases = _load_cases(args.cases)
    if args.tool:
        cases = [c for c in cases if c["tool"] == args.tool]
    if args.case:
        cases = [c for c in cases if c["name"] == args.case]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("error: no cases after filtering", file=sys.stderr)
        return 2

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    for b in backends:
        if b not in ("claude_agent", "qwen_agent"):
            print(f"error: unknown backend {b!r}", file=sys.stderr)
            return 2

    unwired = (
        frozenset({"nx_tidy", "nx_plan_audit"})
        if args.skip_unwired
        else QWEN_AGENT_UNWIRED_DEFAULT
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    with args.out.open("w", encoding="utf-8") as fp:
        for repeat_ix in range(args.repeat):
            for case in cases:
                row = await _run_case(case, backends, unwired=unwired)
                row["repeat"] = repeat_ix
                fp.write(json.dumps(row, default=str) + "\n")
                fp.flush()
                records.append(row)
                claude_ok = bool(
                    row.get("claude_agent")
                    and not row["claude_agent"]["error"]
                )
                if row.get("qwen_agent_skipped"):
                    qwen_state = "skipped"
                else:
                    qwen_state = "ok" if (
                        row.get("qwen_agent")
                        and not row["qwen_agent"]["error"]
                    ) else "fail"
                print(
                    f"[{repeat_ix + 1}/{args.repeat}] "
                    f"{row['tool']}::{row['name']}: "
                    f"claude={'ok' if claude_ok else 'fail'} "
                    f"qwen={qwen_state}",
                    file=sys.stderr,
                )

    summary = _summarize(records)
    summary_path = args.summary or args.out.with_suffix(args.out.suffix + ".md")
    summary_path.write_text(_render_md(summary, args.out), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"\nMarkdown summary: {summary_path}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
