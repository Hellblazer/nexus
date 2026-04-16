# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime exercise of every skill and agent via `claude -p`.

For each markdown file in nx/skills/ and nx/agents/, spawn `claude -p`
with the file body as --append-system-prompt plus a canned user task
drawn from the skill/agent's `triggers:` field. Assert that the output
satisfies the skill's contract — semantically, not just syntactically.

Gated on NX_VALIDATE_WITH_LLM=1 — this is where the harness actually
verifies behavior. Total runtime ~15-20 min with 4 parallel workers.

Assertion categories (see EXPECTED_TERMS per case):
  * tool_name    — output must name a specific MCP tool (e.g. nx_answer)
  * agent_name   — output must name a specific agent (e.g. code-review-expert)
  * domain_term  — output must reference the skill's domain concepts
  * verdict      — output must emit a structured verdict / findings

A case is green when at least one term from its `expect` list appears in
the output (case-insensitive).  False positives (skill produces output
that mentions the wrong tool) are caught by the structural suites 05/06.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent.parent
NX = REPO / "nx"

#: Per-call timeout in seconds. Skills are short-form; 60s is generous.
PER_CALL_TIMEOUT = 90.0

#: How many claude -p processes to run in parallel.
WORKERS = int(os.environ.get("NX_VALIDATE_WORKERS", "4"))


# ── Streaming status ─────────────────────────────────────────────────────────

_lock = __import__("threading").Lock()
_pass = 0
_fail = 0
_skip = 0
_failures: list[tuple[str, str]] = []


def ts() -> str:
    return time.strftime("%H:%M:%S")


def step(msg: str) -> None:
    with _lock:
        print(f"\n[{ts()}] ─── {msg} ───", flush=True)


def report(kind: str, name: str, verdict: str, dur_ms: int, detail: str = "") -> None:
    global _pass, _fail, _skip
    symbols = {"pass": "✓", "fail": "✗", "skip": "⊖"}
    sym = symbols[verdict]
    with _lock:
        line = f"[{ts()}]  {sym} {kind}[{name}]  ({dur_ms} ms)"
        if detail:
            line += f"  — {detail}"
        print(line, flush=True)
        if verdict == "pass":
            _pass += 1
        elif verdict == "fail":
            _fail += 1
            _failures.append((f"{kind}[{name}]", detail))
        else:
            _skip += 1


# ── Case tables ──────────────────────────────────────────────────────────────


@dataclass
class Case:
    name: str
    task: str
    expect: list[str]         # any-of semantics; case-insensitive
    kind: str = "skill"       # "skill" or "agent"
    disabled: bool = False    # mark problematic cases without deleting

    @property
    def path(self) -> Path:
        if self.kind == "skill":
            return NX / "skills" / self.name / "SKILL.md"
        return NX / "agents" / f"{self.name}.md"


# ── Skill cases (43 skills) ──────────────────────────────────────────────────

# Meta-prompt prefix: turn "do X" into "describe which tool/agent you'd
# use to do X". Keeps runs under the timeout and tests routing, not
# execution. The goal is verifying the skill's INSTRUCTIONS, not
# re-running the work the skill would dispatch.
_META_PREFIX = (
    "Do NOT actually perform the work.  In ≤3 sentences, name the "
    "specific MCP tool or agent (by its exact name) you would invoke, "
    "and the key arg(s).  Then stop.\n\nTask: "
)

# Category 1: Verb skills — must route through nx_answer with verb dimensions.
# After the "skills through nx_answer" refactor (PR #168 late commits), each
# verb skill's SKILL.md names nx_answer as the tool to call with
# dimensions={verb: <skill>}.
_VERB_SKILLS = [
    Case("research",      _META_PREFIX + "How does the projection quality mechanism work?",
         ["nx_answer", "mcp__plugin_nx_nexus__nx_answer"]),
    Case("review",        _META_PREFIX + "Review a diff that added `def add(a,b): return a+b`.",
         ["nx_answer", "mcp__plugin_nx_nexus__nx_answer"]),
    Case("analyze",       _META_PREFIX + "Compare BM25 vs dense retrieval across our corpora.",
         ["nx_answer", "mcp__plugin_nx_nexus__nx_answer"]),
    Case("debug",         _META_PREFIX + "Test `test_foo` fails with ImportError.",
         ["nx_answer", "mcp__plugin_nx_nexus__nx_answer"]),
    Case("document",      _META_PREFIX + "Audit doc coverage for the retrieval module.",
         ["nx_answer", "mcp__plugin_nx_nexus__nx_answer"]),
    Case("plan-author",   _META_PREFIX + "Draft a plan template for verb=migrate.",
         ["plan_match", "plan_run", "plan_author", "author"]),
    Case("plan-inspect",  _META_PREFIX + "Inspect plan metrics for verb=research.",
         ["plan_match", "plan_run", "plan-inspect", "metric"]),
    Case("plan-promote",  _META_PREFIX + "Rank plans that earn their slot.",
         ["plan_match", "plan_run", "plan-promote", "promot"]),
    Case("plan-first",    _META_PREFIX + "Before running a retrieval, what's the first step?",
         ["plan_match", "plan_run", "nx_answer"]),
]

# Category 2: Pointer skills — delegate to a specific MCP tool.
_POINTER_SKILLS = [
    Case("query",              _META_PREFIX + "I have an analytical question: 'What did we decide about retrieval?'",
         ["nx_answer", "mcp__plugin_nx_nexus__nx_answer"]),
    Case("enrich-plan",        _META_PREFIX + "Enrich bead 'nexus-abc' with execution context.",
         ["nx_enrich_beads", "mcp__plugin_nx_nexus__nx_enrich_beads"]),
    Case("knowledge-tidying",  _META_PREFIX + "Consolidate knowledge on topic 'chromadb quotas'.",
         ["nx_tidy", "mcp__plugin_nx_nexus__nx_tidy"]),
    Case("plan-validation",    _META_PREFIX + "Audit plan `{\"steps\":[{\"tool\":\"search\"}]}`.",
         ["nx_plan_audit", "mcp__plugin_nx_nexus__nx_plan_audit"]),
]

# Category 3: Agent-dispatcher skills — invoke a specific agent.
# NOTE: meta-prompt asks for agent name explicitly.
_DISPATCHER_SKILLS = [
    Case("code-review",           _META_PREFIX + "Review changes in this diff.",
         ["code-review-expert"]),
    Case("debugging",             _META_PREFIX + "Intermittent test failure in test_foo.",
         ["debugger"]),
    Case("deep-analysis",         _META_PREFIX + "Investigate why latency tripled after refactor.",
         ["deep-analyst"]),
    Case("research-synthesis",    _META_PREFIX + "Research vector DB options and compare.",
         ["deep-research-synthesizer"]),
    Case("test-validation",       _META_PREFIX + "Verify test coverage for the new module.",
         ["test-validator"]),
    Case("strategic-planning",    _META_PREFIX + "Decompose feature 'add --foo flag' into tasks.",
         ["strategic-planner"]),
    Case("architecture",          _META_PREFIX + "Design architecture for a new event bus.",
         ["architect-planner"]),
    Case("development",           _META_PREFIX + "Implement feature from bead nexus-abc (assume it exists).",
         ["developer"]),
    Case("codebase-analysis",     _META_PREFIX + "Analyze module structure of a Python package.",
         ["codebase-deep-analyzer"]),
    Case("substantive-critique",  _META_PREFIX + "Critique an RDR draft for logical consistency.",
         ["substantive-critic"]),
]

# Category 4: RDR lifecycle skills — must describe the T2 / file operation.
_RDR_SKILLS = [
    Case("rdr-create",   _META_PREFIX + "Scaffold a new RDR 'test validation protocol'.",
         ["memory_put", "_rdr", "status: draft", "frontmatter", "create", "rdr"]),
    Case("rdr-gate",     _META_PREFIX + "Gate RDR-042.",
         ["substantive-critic", "assumption", "structural", "gate"]),
    Case("rdr-accept",   _META_PREFIX + "Accept RDR-042.",
         ["memory_put", "_rdr", "status: accepted", "accept"]),
    Case("rdr-close",    _META_PREFIX + "Close RDR-042 as implemented.",
         ["rdr-close", "status: closed", "Gap", "pointer", "close"]),
    Case("rdr-list",     _META_PREFIX + "List all RDRs.",
         ["rdr", "status", "memory", "list"]),
    Case("rdr-show",     _META_PREFIX + "Show RDR-042.",
         ["rdr", "memory", "show", "content"]),
    Case("rdr-research", _META_PREFIX + "Add a research finding to RDR-042.",
         ["rdr", "research", "memory", "finding"]),
    Case("rdr-audit",    _META_PREFIX + "Audit this project's RDR lifecycle.",
         ["rdr", "audit", "scope", "base rate"]),
]

# Category 5: Infrastructure skills — domain-topical assertions.
# NOTE: these describe themselves rather than dispatch; narrow tasks.
_INFRA_SKILLS = [
    Case("using-nx-skills",     _META_PREFIX + "What's the rule for invoking nx skills?",
         ["skill", "invoke", "before"]),
    Case("writing-nx-skills",   _META_PREFIX + "I want to create a new skill — what's the structure?",
         ["skill", "frontmatter", "SKILL.md"]),
    Case("brainstorming-gate",  _META_PREFIX + "I want to implement a new feature.",
         ["brainstorm", "design", "explor", "approval"]),
    Case("orchestration",       _META_PREFIX + "Which agent should I use?",
         ["agent", "pipeline", "orchestrat"]),
    Case("finishing-branch",    _META_PREFIX + "Implementation is done — what now?",
         ["merge", "PR", "branch", "tests"]),
    Case("git-worktrees",       _META_PREFIX + "Set up isolated workspace for parallel feature work.",
         ["worktree", "isolat"]),
    Case("receiving-review",    _META_PREFIX + "A reviewer suggests I change my code.",
         ["review", "verify", "technical"]),
    Case("cli-controller",      _META_PREFIX + "I need to debug with pdb interactively.",
         ["tmux", "interact", "CLI"]),
    Case("nexus",               _META_PREFIX + "I want to run `nx search` — what flags are available?",
         ["nx", "search", "memory", "store"]),
    Case("catalog",             _META_PREFIX + "Create a typed link between two catalog entries.",
         ["catalog", "tumbler", "link", "auto-linker"]),
    Case("serena-code-nav",     _META_PREFIX + "Find all callers of the `plan_match` function.",
         ["serena", "find_referencing", "find_symbol", "symbol"]),
    Case("composition-probe",   _META_PREFIX + "Run composition probe on bead nexus-abc.",
         ["composition", "coordinator", "probe", "test"]),
]


SKILL_CASES: list[Case] = (
    _VERB_SKILLS + _POINTER_SKILLS + _DISPATCHER_SKILLS + _RDR_SKILLS + _INFRA_SKILLS
)


# ── Agent cases (13 agents) ──────────────────────────────────────────────────

# Agents get a meta-prompt too — we want their *approach*, not for them
# to fully execute the task (which can run for minutes).
_AGENT_META = (
    "Do NOT execute the task fully.  In ≤5 sentences, describe your "
    "approach: the first action you'd take and what you'd look for. "
    "Then stop.\n\nTask: "
)

_AGENT_CASES = [
    # Stubs — must direct the caller to the replacement MCP tool.
    Case("knowledge-tidier", "Consolidate knowledge on topic 'chromadb quotas'.  What tool should I call?",
         ["nx_tidy", "mcp__plugin_nx_nexus__nx_tidy"], kind="agent"),
    Case("plan-auditor",     "Audit this plan. What tool should I call?",
         ["nx_plan_audit", "mcp__plugin_nx_nexus__nx_plan_audit"], kind="agent"),
    Case("plan-enricher",    "Enrich this bead: 'Add --tag flag'.",
         ["nx_enrich_beads", "mcp__plugin_nx_nexus__nx_enrich_beads"], kind="agent"),

    # Active agents — assert on their core output contracts.
    Case("substantive-critic",        _AGENT_META + "Critique: 'A plan library is useful.'",
         ["finding", "critical", "significant", "minor", "priority",
          "critique", "claim", "evidence", "assumption"], kind="agent"),
    Case("code-review-expert",        _AGENT_META + "Review: `def add(a,b): return a+b`. Any issues?",
         ["type", "hint", "quality", "test", "concern", "ok", "good"], kind="agent"),
    Case("debugger",                  _AGENT_META + "Test fails with `AssertionError` intermittently.",
         ["hypothesis", "evidence", "reproduce", "isolate", "log"], kind="agent"),
    Case("deep-analyst",              _AGENT_META + "Latency tripled after we added vector search.",
         ["hypothesis", "evidence", "cause", "analy", "component"], kind="agent"),
    Case("deep-research-synthesizer", _AGENT_META + "Research best practices for semantic search reranking.",
         ["source", "research", "synthes", "finding", "compare"], kind="agent"),
    Case("test-validator",            _AGENT_META + "Verify test coverage for a new `retry()` function.",
         ["test", "coverage", "assert", "run"], kind="agent"),
    Case("strategic-planner",         _AGENT_META + "Decompose 'add --foo flag to nx search' into tasks.",
         ["phase", "task", "step", "bead"], kind="agent"),
    Case("architect-planner",         _AGENT_META + "Architecture for a new event bus. Assume scope is 'Nexus internal decoupling'.",
         ["component", "interface", "architecture", "phase", "design",
          "module", "boundary", "subscriber", "publisher", "event"], kind="agent"),
    Case("codebase-deep-analyzer",    _AGENT_META + "Analyze the module structure of a typical Python package.",
         ["module", "structure", "pattern", "depend", "entry"], kind="agent"),
    Case("developer",                 _AGENT_META + "Implement: 'add --foo flag to nx search'. Assume bead exists.",
         ["implement", "test", "file", "function", "flag"], kind="agent"),
]


# ── Runner ───────────────────────────────────────────────────────────────────

def _run_one(case: Case) -> tuple[Case, str, str]:
    """Run one case. Returns (case, verdict, detail)."""
    if case.disabled:
        return case, "skip", "disabled"

    if not case.path.exists():
        return case, "fail", f"markdown not found: {case.path}"

    system_prompt = case.path.read_text()
    env = os.environ.copy()
    # claude -p subprocess needs the user's real HOME for auth (see lib.sh)
    if (orig := env.get("ORIG_HOME")):
        env["HOME"] = orig

    start = time.monotonic()
    try:
        result = subprocess.run(
            ["claude", "-p", case.task, "--append-system-prompt", system_prompt],
            capture_output=True, text=True, timeout=PER_CALL_TIMEOUT, env=env,
        )
    except subprocess.TimeoutExpired:
        dur_ms = int((time.monotonic() - start) * 1000)
        return case, "fail", f"timeout after {PER_CALL_TIMEOUT}s"

    dur_ms = int((time.monotonic() - start) * 1000)

    if result.returncode != 0:
        return case, "fail", (
            f"claude -p rc={result.returncode}: "
            f"stderr={result.stderr[:150]!r}"
        )

    out_lower = result.stdout.lower()
    hits = [term for term in case.expect if term.lower() in out_lower]
    if not hits:
        head = result.stdout.replace("\n", " ")[:200]
        return case, "fail", (
            f"output lacks any expected term {case.expect!r}. "
            f"head: {head!r}"
        )

    return case, "pass", f"matched: {hits[0]!r} ({dur_ms} ms)"


def run_suite() -> None:
    all_cases = SKILL_CASES + _AGENT_CASES

    step(f"Runtime exercise of {len(SKILL_CASES)} skills + {len(_AGENT_CASES)} agents "
         f"(workers={WORKERS}, per-call timeout={PER_CALL_TIMEOUT}s)")

    futures = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for c in all_cases:
            futures[pool.submit(_run_one, c)] = c

        # Stream completions in real time
        for fut in as_completed(futures):
            case = futures[fut]
            try:
                _, verdict, detail = fut.result()
            except Exception as exc:
                if os.environ.get("NX_VALIDATE_VERBOSE"):
                    traceback.print_exc()
                verdict, detail = "fail", f"{type(exc).__name__}: {exc}"
            # Extract duration from detail when present
            dur = 0
            if "(" in detail and "ms)" in detail:
                try:
                    dur = int(detail.rsplit("(", 1)[1].split(" ", 1)[0])
                except Exception:
                    pass
            report(case.kind, case.name, verdict, dur, detail)


def main() -> int:
    if os.environ.get("NX_VALIDATE_WITH_LLM") != "1":
        print(f"[{ts()}] plugin-runtime: NX_VALIDATE_WITH_LLM=1 required")
        print(f"[{ts()}] ── plugin-runtime: 0 pass, 0 fail (all skipped) ──")
        return 0
    if shutil.which("claude") is None:
        print(f"[{ts()}] claude CLI not on PATH — skipping")
        return 0

    print(f"[{ts()}] Plugin-runtime suite — {len(SKILL_CASES) + len(_AGENT_CASES)} cases via `claude -p`")
    try:
        run_suite()
    finally:
        print(f"\n[{ts()}] ── plugin-runtime: {_pass} pass, {_fail} fail, {_skip} skip ──")
        for name, err in _failures:
            print(f"       - {name}: {err}")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
