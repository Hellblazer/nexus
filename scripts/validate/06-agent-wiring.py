# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify agent wiring after the RDR-080 replacements.

This suite is the static counterpart to 07-agent-behavior (LLM-gated).
Covers invariants that do NOT require spawning agents:

 * Stub agent bodies — each of the 3 stub agents names its replacement
   MCP tool in both frontmatter description AND in the body, and
   explicitly instructs callers to use the MCP tool.
 * No stale references — no skill, slash command, agent, or pipeline
   still references a deleted agent name.
 * Dispatch wiring — every `subagent_type: X` / `invoke **X** agent` /
   `dispatches_to: [X]` in a skill or registry points at an agent file
   that actually exists.
 * Pipeline integrity — pipelines reference only live agent names.
 * Registry consistency — `model_summary` (opus/sonnet/haiku buckets)
   and `agents` keys stay in sync.

Does NOT cover:
 * Whether an agent, when actually spawned, calls the right MCP tool
   (LLM behavior — see 07-agent-behavior.py with NX_VALIDATE_WITH_LLM=1).
"""
from __future__ import annotations

import os
import re
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

import yaml

_pass = 0
_fail = 0
_failures: list[tuple[str, str]] = []


def ts() -> str:
    return time.strftime("%H:%M:%S")


def info(msg: str) -> None:
    print(f"[{ts()}]    {msg}", flush=True)


def step(msg: str) -> None:
    print(f"\n[{ts()}] ─── {msg} ───", flush=True)


@contextmanager
def case(name: str):
    global _pass, _fail
    start = time.monotonic()
    try:
        yield
        dur = int((time.monotonic() - start) * 1000)
        print(f"[{ts()}]  ✓ {name}  ({dur} ms)", flush=True)
        _pass += 1
    except AssertionError as exc:
        dur = int((time.monotonic() - start) * 1000)
        print(f"[{ts()}]  ✗ {name}  ({dur} ms) — {exc}", flush=True)
        _fail += 1
        _failures.append((name, str(exc)))
    except Exception as exc:
        dur = int((time.monotonic() - start) * 1000)
        print(f"[{ts()}]  ✗ {name}  ({dur} ms) — {type(exc).__name__}: {exc}", flush=True)
        if os.environ.get("NX_VALIDATE_VERBOSE"):
            traceback.print_exc()
        _fail += 1
        _failures.append((name, f"{type(exc).__name__}: {exc}"))


REPO = Path(__file__).resolve().parent.parent.parent
NX = REPO / "nx"

#: RDR-080 P3: these agents were removed; their references must not persist.
DELETED_AGENTS = frozenset({
    "query-planner",
    "analytical-operator",
    "pdf-chromadb-processor",
})

#: RDR-080 P3: these agents are kept as 40-line stub redirectors to MCP tools.
STUB_AGENTS = {
    "knowledge-tidier": "nx_tidy",
    "plan-auditor":     "nx_plan_audit",
    "plan-enricher":    "nx_enrich_beads",
}


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except Exception:
        return ""


def _all_md_files() -> list[Path]:
    paths = []
    paths.extend(NX.glob("agents/*.md"))
    paths.extend(NX.glob("skills/*/SKILL.md"))
    paths.extend(NX.glob("commands/*.md"))
    return paths


# ── Suite ────────────────────────────────────────────────────────────────────


def run_suite() -> None:
    registry = yaml.safe_load(_read(NX / "registry.yaml"))
    registered_agents = set(registry.get("agents", {}).keys())
    agent_files_on_disk = {p.stem for p in NX.glob("agents/*.md")}

    # ── Stub agent body contracts ────────────────────────────────────────────
    step("Stub agent bodies name their MCP tool replacement")
    for stub_name, mcp_tool in STUB_AGENTS.items():
        with case(f"stub[{stub_name}] → mcp__plugin_nx_nexus__{mcp_tool}"):
            path = NX / "agents" / f"{stub_name}.md"
            assert path.exists(), f"stub file missing: {path}"
            body = _read(path)
            # Frontmatter description must mention the MCP tool
            m = re.match(r"^---\n(.*?)\n---", body, re.DOTALL)
            assert m, "no YAML frontmatter"
            fm = yaml.safe_load(m.group(1))
            desc = fm.get("description", "")
            assert mcp_tool in desc, f"frontmatter description doesn't mention {mcp_tool}"
            assert "STUB" in desc.upper() or "superseded" in desc.lower(), \
                "frontmatter must label as STUB/superseded"
            # Body must NOT reference any deleted agent (not even itself pre-replacement)
            for deleted in DELETED_AGENTS:
                assert deleted not in body, f"body references deleted agent {deleted!r}"
            # Body must name the MCP tool in at least one invocation example
            tool_full = f"mcp__plugin_nx_nexus__{mcp_tool}"
            assert tool_full in body or mcp_tool in body, \
                f"body doesn't show the {mcp_tool} MCP tool call"

    # ── Dispatch wiring — every agent reference points at a live file ────────
    step("No skill / command / agent references a deleted agent")
    for path in _all_md_files():
        with case(f"{path.relative_to(NX)} — no deleted-agent refs"):
            body = _read(path)
            for deleted in DELETED_AGENTS:
                # Allow bare occurrences inside backticks only if part of a
                # historical comment (treat test-friendly: zero tolerance,
                # remove historical mentions if any).
                if deleted in body:
                    # Find the line for a precise error message
                    for i, line in enumerate(body.splitlines(), 1):
                        if deleted in line:
                            raise AssertionError(
                                f"{path.name}:{i} references deleted agent {deleted!r}: "
                                f"{line.strip()[:120]}"
                            )

    # ── Registry consistency: every agent in model_summary exists ────────────
    step("Registry model_summary buckets match registered agents")
    with case("every agent in model_summary is a registered agent"):
        model_summary = registry.get("model_summary") or {}
        bucketed: set[str] = set()
        for bucket in ("opus", "sonnet", "haiku"):
            bucketed |= set(model_summary.get(bucket) or [])
        # Strip '# stub' suffixes and comments that can sneak in
        bucketed = {b.split("#")[0].strip() for b in bucketed}
        orphan = bucketed - registered_agents
        assert not orphan, f"model_summary lists agents not in agents section: {orphan}"

    # ── Every agent named as a skill dispatcher must exist as a file ─────────
    step("Skill dispatch targets exist")
    # Built-in Claude Code agents that are NOT plugin files (no markdown on disk).
    BUILTIN_AGENTS = {"general-purpose", "Explore", "Plan"}

    with case("every `dispatches_to` agent has a file on disk"):
        unknown = set()
        for top_key in ("rdr_skills", "standalone_skills"):
            for skill_name, cfg in (registry.get(top_key) or {}).items():
                for a in cfg.get("dispatches_to") or []:
                    if a not in agent_files_on_disk and a not in BUILTIN_AGENTS:
                        unknown.add((skill_name, a))
        assert not unknown, f"skills dispatch to unknown agents: {unknown}"

    with case("every skill body mention of an agent file is live"):
        # Agent tool invocations in skill bodies
        pattern_a = re.compile(r'subagent_type:\s*["\'](\w[\w-]*)["\']')
        pattern_b = re.compile(r"invoke\s+(?:the\s+)?\*\*([\w-]+)\*\*\s+agent", re.IGNORECASE)
        mentioned = set()
        for skill_md in NX.glob("skills/*/SKILL.md"):
            body = _read(skill_md)
            mentioned |= set(pattern_a.findall(body))
            mentioned |= set(pattern_b.findall(body))
        unknown = mentioned - agent_files_on_disk - {"general-purpose", "Explore", "Plan"}
        assert not unknown, f"skills mention unknown agents: {unknown}"

    # ── Pipeline integrity ──────────────────────────────────────────────────
    step("Pipeline sequences reference only live agents")
    with case("pipelines reference only live agent files"):
        pipelines = registry.get("pipelines") or {}
        unknown = set()
        for pname, pcfg in pipelines.items():
            for step_ref in pcfg.get("sequence") or []:
                # Strip `# stub` suffix comments
                agent = step_ref.split("#")[0].strip()
                if agent and agent not in agent_files_on_disk:
                    unknown.add((pname, agent))
        assert not unknown, f"pipelines reference unknown agents: {unknown}"

    # ── Semantic check: stub-aware skills name the MCP tool replacement ──────
    step("Skills dispatching to stub agents point readers at the MCP tool")
    # A skill that says "invoke knowledge-tidier agent" should ALSO mention
    # the MCP tool replacement so readers know the preferred path.
    for skill_md in NX.glob("skills/*/SKILL.md"):
        body = _read(skill_md)
        for stub_name, mcp_tool in STUB_AGENTS.items():
            if stub_name in body and "stub" not in body.lower() and mcp_tool not in body:
                # An explicit mention of the stub without an MCP-tool hint.
                with case(f"skills/{skill_md.parent.name}/SKILL.md — stub hint"):
                    raise AssertionError(
                        f"references stub agent {stub_name!r} without hinting "
                        f"at MCP tool {mcp_tool!r}"
                    )

    # ── No orphaned agents (every non-stub agent is reachable) ──────────────
    step("Every non-stub agent is dispatchable from at least one skill or pipeline")
    with case("no orphaned non-stub agents"):
        dispatched: set[str] = set()
        # Registry dispatches
        for top_key in ("rdr_skills", "standalone_skills"):
            for cfg in (registry.get(top_key) or {}).values():
                dispatched |= set(cfg.get("dispatches_to") or [])
        # Pipeline sequences
        for pcfg in (registry.get("pipelines") or {}).values():
            for s in pcfg.get("sequence") or []:
                dispatched.add(s.split("#")[0].strip())
        # Skill body dispatches
        pattern_a = re.compile(r'subagent_type:\s*["\'](\w[\w-]*)["\']')
        pattern_b = re.compile(r"invoke\s+(?:the\s+)?\*\*([\w-]+)\*\*\s+agent", re.IGNORECASE)
        for skill_md in NX.glob("skills/*/SKILL.md"):
            body = _read(skill_md)
            dispatched |= set(pattern_a.findall(body))
            dispatched |= set(pattern_b.findall(body))
        # Agents explicitly linked by `agent_name: <skill>` in the registry
        for a_name, a_cfg in (registry.get("agents") or {}).items():
            # These are dispatch targets of their own slash_command
            if a_cfg.get("slash_command"):
                dispatched.add(a_name)
        non_stub = agent_files_on_disk - set(STUB_AGENTS.keys())
        orphans = non_stub - dispatched
        assert not orphans, f"orphaned non-stub agents: {orphans}"


def main() -> int:
    print(f"[{ts()}] Agent wiring validation — static checks (no LLM)")
    try:
        run_suite()
    finally:
        print(f"\n[{ts()}] ── agent-wiring: {_pass} pass, {_fail} fail ──")
        for name, err in _failures:
            print(f"       - {name}: {err}")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
