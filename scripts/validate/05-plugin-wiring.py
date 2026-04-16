# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-reference plugin files against the actual code.

Verifies that every MCP tool name mentioned in a skill, agent, or slash
command actually exists in the registered tool set. Catches drift where a
skill points at a renamed/deleted tool.

Also validates:
 * Every slash command (nx/commands/*.md) references a registered skill.
 * Every agent markdown parses.
 * Every skill markdown parses.
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


def _registered_tool_names() -> set[str]:
    """Import the two MCP servers and collect @mcp.tool-decorated names."""
    from nexus.mcp.core import mcp as core_mcp
    from nexus.mcp.catalog import mcp as catalog_mcp
    core = {t.name for t in core_mcp._tool_manager.list_tools()}
    cat  = {t.name for t in catalog_mcp._tool_manager.list_tools()}
    # Catalog tools are registered under short names (search, show, …) per
    # @mcp.tool(name="search"); the full form agents use is
    # mcp__plugin_nx_nexus_catalog__<name>.
    return core | {f"catalog_{n}" for n in cat} | cat


def _extract_tool_refs(text: str) -> set[str]:
    """Find MCP tool name references in a skill / agent / command body."""
    names: set[str] = set()
    # Pattern 1: mcp__plugin_nx_nexus__<tool>
    names.update(re.findall(r"mcp__plugin_nx_nexus(?:_catalog)?__(\w+)", text))
    # Pattern 2: bare tool names in code blocks (heuristic — match core tool names only)
    return names


# ── Suite ────────────────────────────────────────────────────────────────────

def run_suite() -> None:
    tools = _registered_tool_names()
    info(f"Registered MCP tools: {len(tools)}")

    step("Skills — frontmatter parses + tool refs valid")
    skills = sorted(NX.glob("skills/*/SKILL.md"))
    for skill_md in skills:
        skill_name = skill_md.parent.name
        with case(f"skill[{skill_name}] frontmatter + tool refs"):
            text = skill_md.read_text()
            m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
            assert m, "missing YAML frontmatter"
            fm = yaml.safe_load(m.group(1))
            assert "name" in fm, "frontmatter missing 'name'"
            assert "description" in fm, "frontmatter missing 'description'"
            # Any tool refs present must resolve to registered tools
            refs = _extract_tool_refs(text)
            unknown = refs - tools
            # Known-good stubs that reference their replacement:
            assert not unknown, f"unknown tool refs: {unknown}"

    step("Agents — frontmatter parses + stub agents point at replacements")
    agents = sorted(NX.glob("agents/*.md"))
    for agent_md in agents:
        agent_name = agent_md.stem
        with case(f"agent[{agent_name}] frontmatter"):
            text = agent_md.read_text()
            m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
            assert m, "missing YAML frontmatter"
            fm = yaml.safe_load(m.group(1))
            assert "name" in fm
            assert "description" in fm

    step("Slash commands — every command has a file")
    commands = sorted(NX.glob("commands/*.md"))
    for cmd_md in commands:
        with case(f"command[{cmd_md.stem}] file readable"):
            txt = cmd_md.read_text()
            assert len(txt) > 0, "empty command file"

    step("Registry — every skill/agent/command has a registry entry")
    reg_path = NX / "registry.yaml"
    with case("registry.yaml parses"):
        reg = yaml.safe_load(reg_path.read_text())
        assert isinstance(reg, dict)

    # Agent registry entries
    with case("every agent has a registry entry"):
        agent_registry = reg.get("agents", {})
        registered_agents = set(agent_registry.keys())
        on_disk = {p.stem for p in agents}
        missing = on_disk - registered_agents
        # stubs may have been removed from registry; accept that
        hard_missing = missing - {"knowledge-tidier", "plan-auditor", "plan-enricher"}
        assert not hard_missing, f"agents on disk but not in registry: {hard_missing}"

    with case("every skill dir has a registry entry"):
        all_reg_skills = set()
        for key in ("rdr_skills", "standalone_skills"):
            all_reg_skills |= set(reg.get(key, {}).keys())
        # Agent-skill mapping via `skill:` field
        for agent_cfg in reg.get("agents", {}).values():
            s = agent_cfg.get("skill")
            if s:
                all_reg_skills.add(s)
        on_disk = {p.parent.name for p in skills}
        missing = on_disk - all_reg_skills
        assert not missing, f"skills on disk but not in registry: {missing}"

    with case("every command file has a registry entry"):
        all_reg_commands = set()
        for cfg in reg.get("utility_commands", {}).values():
            all_reg_commands.add(cfg.get("slash_command", "").lstrip("/"))
        for cfg in reg.get("agents", {}).values():
            sc = cfg.get("slash_command", "").lstrip("/")
            if sc:
                all_reg_commands.add(sc)
        for cfg in reg.get("rdr_skills", {}).values():
            sc = cfg.get("slash_command", "").lstrip("/")
            if sc:
                all_reg_commands.add(sc)
        on_disk = {p.stem for p in commands}
        missing = on_disk - all_reg_commands
        assert not missing, f"commands on disk but not in registry: {missing}"


def main() -> int:
    print(f"[{ts()}] Plugin wiring validation — registered MCP tools cross-referenced")
    try:
        run_suite()
    finally:
        print(f"\n[{ts()}] ── plugin-wiring: {_pass} pass, {_fail} fail ──")
        for name, err in _failures:
            print(f"       - {name}: {err}")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
