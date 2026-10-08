# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import re
import subprocess
import tomllib
from pathlib import Path

import yaml
import pytest
from packaging.requirements import Requirement

from plugin_channel import (
    assert_tag_visibility,
    cut_head_sha,
    parse_plugin_tag,
    wheel_surface_offenders,
)

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).parent.parent
PLUGIN_DIR = REPO_ROOT / "conexus"
AGENTS_DIR = PLUGIN_DIR / "agents"
RESOURCES_DIR = PLUGIN_DIR / "resources"
# nexus-cnzei.4: moved out of agents/ so the five reference docs are no longer
# listed as dispatchable conexus:_shared:* agent entries.
SHARED_DIR = RESOURCES_DIR / "agent-shared"
SKILLS_DIR = PLUGIN_DIR / "skills"
COMMANDS_DIR = PLUGIN_DIR / "commands"
REGISTRY_PATH = PLUGIN_DIR / "registry.yaml"
HOOKS_PATH = PLUGIN_DIR / "hooks" / "hooks.json"
MARKETPLACE_PATH = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

REGISTRY = yaml.safe_load(REGISTRY_PATH.read_text())
REGISTRY_AGENTS: dict = REGISTRY.get("agents", {})

_STANDALONE_SKILLS = {
    "cli-controller", "nexus",
    "brainstorming-gate", "orchestration",
    "using-nx-skills", "writing-nx-skills",
    "rdr-show", "rdr-create",
    # rdr-list has no skill any more (nexus-cnzei.4 fix round): skills/rdr-list
    # was deleted, not the command — its content depended on the command's
    # bash-injected data having already run, and two live E2E scenarios
    # (tests/cc-validation/scenarios/19, 23) test the command's actual
    # injected content, not a skill.
    "sequential-thinking",
    "serena-code-nav", "catalog",
    "receiving-review", "git-worktrees", "finishing-branch",
    # RDR-205 Phase 5 (bead nexus-em75s.24) — reference card for the
    # mailbox/<address> tuple-space convention, no agent dispatch.
    "mailbox",
    # Reference card for messaging other sessions and dispatched agents and
    # sharing one machine with them, no agent dispatch.
    "peer-messaging",
    # RDR-080 P3: pointer skills — delegate directly to MCP tools, no relay structure needed
    "query", "enrich-plan", "knowledge-tidying", "plan-validation",
    # RDR-078 verb skills — dispatch plan_match + plan_run directly, no agent relay
    # nexus-cnzei.4: "debug"/"research"/"review" renamed off their bare verb
    # names — those names collided with, respectively, the debugger-dispatch
    # command, the deep-research-synthesizer-dispatch command, and the
    # code-review naming family. The nx_answer dimensions={"verb": ...} value
    # each skill passes is unchanged; only the skill's own folder/display
    # name moved.
    "design-to-code-trace", "decision-drift-review", "analyze", "why-was-this-written", "document",
    "plan-author", "plan-inspect", "plan-promote", "plan-first",
    # nexus-j327 — phase closeout cross-walk gate. Pointer skill with Python
    # preamble; no agent dispatch. Enforces §Approach coverage at phase
    # boundaries. Restored from archive/develop-2026-05-19 as RDR-120 P0
    # prerequisite (2026-05-19).
    "phase-review-gate",
    # RDR-159 P4 — thin Chroma-to-service upgrade veneer. Pointer skill wrapping
    # the nexus.migration engine (nx migrate-to-service); no agent dispatch,
    # no relay structure.
    "upgrade",
    # 2026-08-05 compression arc — reference card for nexus test-suite layer
    # routing + authoring directives. No agent dispatch, no relay structure.
    "test-authoring",
    # GH #896 / nexus-sxiay — thin CLI wrapper over `nx catalog footnotes`.
    # No agent dispatch, no relay structure; same shape as "catalog"/"upgrade"
    # above.
    "tumbler-footnotes",
}


def agent_files() -> list[Path]:
    return sorted(p for p in AGENTS_DIR.glob("*.md"))

def skill_skill_mds() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))

def command_files() -> list[Path]:
    return sorted(COMMANDS_DIR.glob("*.md"))

ALL_MD_FILES = agent_files() + list(skill_skill_mds()) + command_files()

def _extract_frontmatter(text: str) -> dict | None:
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    return yaml.safe_load(m.group(1)) if m else None

def _extract_recover_block(text: str) -> str | None:
    m = re.search(r"If validation fails.*?(?=\n###|\n##|\Z)", text, re.DOTALL)
    return m.group(0) if m else None

def _collect_shared_links() -> list[tuple[Path, str]]:
    results = []
    for md_file in sorted(PLUGIN_DIR.rglob("*.md")):
        if "agent-shared" in md_file.parts:
            continue
        text = md_file.read_text()
        for match in re.finditer(r"\[([^\]]*)\]\(([^)]*agent-shared/[^)]*)\)", text):
            results.append((md_file, match.group(2)))
    return results


#: nexus-cnzei.6 fix round (CRE 4): the 8 skills that dedupe the
#: tier-aware-discipline preamble into conexus/resources/tier-discipline.md
#: (see that file's own header) each carry a link to it; nothing pinned
#: those links resolve, or that the exact set of 8 skills carrying the link
#: stays 8 (a 9th skill silently re-duplicating the full block inline,
#: instead of linking, would go unnoticed).
_TIER_DISCIPLINE_SKILLS = frozenset({
    "analyze", "design-to-code-trace", "deep-analysis", "document",
    "knowledge-tidying", "query", "research-synthesis", "why-was-this-written",
})


def _collect_tier_discipline_links() -> list[tuple[Path, str]]:
    results = []
    for md_file in sorted(PLUGIN_DIR.rglob("*.md")):
        if md_file.name == "tier-discipline.md":
            continue
        text = md_file.read_text()
        for match in re.finditer(r"\[([^\]]*)\]\(([^)]*resources/tier-discipline\.md[^)]*)\)", text):
            results.append((md_file, match.group(2)))
    return results


def _collect_plugin_root_refs() -> list[tuple[str, str]]:
    results = []
    for src_file in sorted(PLUGIN_DIR.rglob("*")):
        if not src_file.is_file():
            continue
        try:
            text = src_file.read_text()
        except UnicodeDecodeError:
            continue
        label = str(src_file.relative_to(PLUGIN_DIR))
        for match in re.finditer(r"\$CLAUDE_PLUGIN_ROOT/([^\s'\"`)]+)", text):
            results.append((label, match.group(1)))
    return results


#: 20 -> 15 at cleanup steps A2 and A3: six entries deleted outright (21 -> 15).
_MIN_HOOKS_JSON_ENTRIES_EXAMINED = 15
"""Non-vacuity floor for the sub-entry walk in
:func:`TestHooks.test_hooks_json_names_no_deleted_runner_helper`.

Measured 2026-09-19: hooks.json carries 25 ``hooks`` sub-entries across
every event. Set just under the measured count, not pinned exactly, so
routine growth does not force a bump on every unrelated hooks.json edit --
only a walk that finds implausibly little should fail.

nexus-t9klx deleted this file's two other hook floors,
``_MIN_HOOK_SCRIPT_REFS`` and ``_PYTHON_HOOK_SCRIPT_MIN_COUNT``, with the
tests parametrized over them: hooks.json names no plugin-resident script
any more, so their domains are empty by construction, and a floor walked
down to zero is satisfied by an extractor that sees nothing. That a
``python3`` entry cannot come back is ``tests/test_hooks_json_shape_lint.py``'s
job, which rejects the command outright.
"""



class TestRegistryIntegrity:

    def test_registry_exists_and_parses(self) -> None:
        assert REGISTRY_PATH.exists()
        assert "agents" in REGISTRY and "version" in REGISTRY

    def test_registry_references_resolve(self) -> None:
        """Every registry agent has its file and its skill, and every
        pipeline, predecessor, successor and model_summary entry names a
        registered agent."""
        known = set(REGISTRY_AGENTS)
        assert len(known) >= 10, f"only {len(known)} registry agents examined"
        offenders: list[str] = []
        for name, meta in REGISTRY_AGENTS.items():
            if not (AGENTS_DIR / f"{name}.md").exists():
                offenders.append(f"agent '{name}': no agents/{name}.md")
            skill = meta.get("skill")
            if skill and not (SKILLS_DIR / skill / "SKILL.md").exists():
                offenders.append(f"agent '{name}': skill '{skill}' has no SKILL.md")
            for rel in ("predecessors", "successors"):
                for ref in meta.get(rel, []):
                    if ref not in known:
                        offenders.append(f"agent '{name}' {rel} entry '{ref}' not in agents")
        for pname, pmeta in REGISTRY.get("pipelines", {}).items():
            for step in pmeta.get("sequence", []):
                if step not in known:
                    offenders.append(f"pipeline '{pname}' references unknown agent '{step}'")
        for model, listed in REGISTRY.get("model_summary", {}).items():
            for name in listed:
                if name not in known:
                    offenders.append(f"model_summary/{model} references unknown agent '{name}'")
        assert not offenders, "\n".join(offenders)


AGENT_REQUIRED_SECTIONS = [
    "## Relay Reception",
    "## Context Protocol",
    "### Agent-Specific PRODUCE",
    "RECOVER protocol",
]
AGENT_FRONTMATTER_FIELDS = ("name", "version", "description", "model", "color")


class TestAgentStructure:

    def test_every_agent_has_required_sections_and_frontmatter(self) -> None:
        agents = agent_files()
        assert len(agents) >= 10, f"only {len(agents)} agent files examined"
        offenders: list[str] = []
        for agent_path in agents:
            text = agent_path.read_text()
            for section in AGENT_REQUIRED_SECTIONS:
                if section not in text:
                    offenders.append(f"{agent_path.name}: missing '{section}'")
            if "CONTEXT_PROTOCOL.md" not in text:
                offenders.append(f"{agent_path.name}: missing CONTEXT_PROTOCOL.md reference")
            fm = _extract_frontmatter(text)
            if not fm:
                offenders.append(f"{agent_path.name}: no YAML frontmatter found")
                continue
            for field in AGENT_FRONTMATTER_FIELDS:
                if field not in fm:
                    offenders.append(f"{agent_path.name}: frontmatter missing '{field}'")
        assert not offenders, "\n".join(offenders)



class TestPlannerReviewGates:

    def test_planner_review_gates(self) -> None:
        # wuerf: inner-axis collapse — three independent assertions over the
        # one strategic-planner.md file, formerly 3 parametrized nodes. Same
        # coverage, one file read.
        text = (AGENTS_DIR / "strategic-planner.md").read_text()
        # has-review-gates-section
        assert "### Review Gates" in text
        # gates-mention-code-review-expert
        start = text.index("### Review Gates")
        end = text.index("###", start + 1)
        assert "code-review-expert" in text[start:end]
        # execution-instructions-include-review
        start = text.index("**Execution Instructions**")
        end = text.index("**Parallelization", start)
        section = text[start:end]
        assert "Code review" in section or "code-review" in section


class TestRecoverProtocol:

    def test_every_agent_has_a_recover_block(self) -> None:
        agents = agent_files()
        assert len(agents) >= 10, f"only {len(agents)} agent files examined"
        offenders: list[str] = []
        for agent_path in agents:
            block = _extract_recover_block(agent_path.read_text())
            if not block:
                offenders.append(f"{agent_path.name}: no 'If validation fails' block found")
                continue
            if "6. Proceed with available context" not in block:
                offenders.append(f"{agent_path.name}: RECOVER block missing step 6")
            if not ("nx scratch search" in block or 'action="search"' in block
                    or "scratch" in block.lower()):
                offenders.append(f"{agent_path.name}: RECOVER block missing T1 scratch search step")
            has_cli = "nx memory search" in block
            has_mcp = "memory_search" in block
            has_stale = "nx memory get --project" in block
            if not (has_cli or has_mcp or not has_stale):
                offenders.append(
                    f"{agent_path.name}: RECOVER uses stale 'nx memory get' instead of 'memory_search'"
                )
        assert not offenders, "\n".join(offenders)


class TestCliSyntax:

    def test_no_stale_patterns(self) -> None:
        assert len(ALL_MD_FILES) >= 40, f"only {len(ALL_MD_FILES)} plugin markdown files examined"
        offenders: list[str] = []
        for md_path in ALL_MD_FILES:
            text = md_path.read_text()
            rel = md_path.relative_to(PLUGIN_DIR)
            if "pm::" in text:
                offenders.append(f"{rel}: stale 'pm::' notation")
            if re.search(r"`nx health`|nx health\b", text):
                offenders.append(f"{rel}: stale 'nx health' command")
        assert not offenders, "\n".join(offenders)

    def test_nx_store_put_has_pipe_source(self) -> None:
        agents = agent_files()
        assert len(agents) >= 10, f"only {len(agents)} agent files examined"
        offenders: list[str] = []
        for agent_path in agents:
            for lineno, line in enumerate(agent_path.read_text().splitlines(), 1):
                if "nx store put -" not in line:
                    continue
                stripped = line.strip()
                has_pipe = "|" in stripped and stripped.index("|") < stripped.index("nx store put -")
                is_comment = re.match(r"^\s*[#\-*]", line)
                if not (has_pipe or is_comment):
                    offenders.append(f"{agent_path.name}:{lineno}: 'nx store put -' missing pipe source")
        assert not offenders, "\n".join(offenders)


class TestSkillStructure:

    REQUIRED_SECTIONS = [
        ("## Relay Template", "## Agent Invocation"),
        "## Success Criteria",
    ]

    def test_agent_backed_skills_have_required_sections_and_produce(self) -> None:
        skills = [p for p in skill_skill_mds() if p.parent.name not in _STANDALONE_SKILLS]
        assert len(skills) >= 10, f"only {len(skills)} agent-backed skills examined"
        offenders: list[str] = []
        for skill_path in skills:
            name = skill_path.parent.name
            text = skill_path.read_text()
            for section in self.REQUIRED_SECTIONS:
                if isinstance(section, tuple):
                    if not any(alt in text for alt in section):
                        offenders.append(f"{name}/SKILL.md: missing one of {section}")
                elif section not in text:
                    offenders.append(f"{name}/SKILL.md: missing '{section}'")
            if "Agent-Specific PRODUCE" not in text:
                offenders.append(f"{name}/SKILL.md: missing 'Agent-Specific PRODUCE'")
            if "scratch" not in text.lower():
                offenders.append(f"{name}/SKILL.md: no mention of T1 scratch")
        assert not offenders, "\n".join(offenders)

    def test_relay_templates_have_required_rows(self) -> None:
        offenders: list[str] = []
        examined = 0
        for skill_path in skill_skill_mds():
            text = skill_path.read_text()
            if "## Relay Template" not in text:
                continue
            examined += 1
            relay_section = text.split("## Relay Template")[1]
            for row in ("nx store:", "nx memory:", "Files:"):
                if row not in relay_section:
                    offenders.append(
                        f"{skill_path.parent.name}/SKILL.md relay template: missing '{row}'"
                    )
        assert examined >= 5, f"only {examined} skills with a relay template examined"
        assert not offenders, "\n".join(offenders)


class TestSkillDescriptionCSO:

    BAD_KEYWORDS = ["Triggers:", "user says", "workflow", "process:"]

    def test_every_skill_frontmatter_is_valid(self) -> None:
        skills = skill_skill_mds()
        assert len(skills) >= 40, f"only {len(skills)} skills examined"
        offenders: list[str] = []
        for skill_path in skills:
            name = skill_path.parent.name
            fm_match = re.match(r"^---\n(.*?)\n---", skill_path.read_text(), re.DOTALL)
            if not fm_match:
                offenders.append(f"{name}/SKILL.md: no YAML frontmatter")
                continue
            raw = fm_match.group(1)
            try:
                fm = yaml.safe_load(raw)
            except yaml.YAMLError as exc:
                offenders.append(f"{name}/SKILL.md: frontmatter is not valid YAML: {exc}")
                continue
            extra = set(fm.keys()) - {"name", "description", "effort"}
            if extra:
                offenders.append(f"{name}/SKILL.md: non-standard fields {sorted(extra)}")
            comment_lines = [l for l in raw.splitlines() if l.strip().startswith("#")]
            if comment_lines:
                offenders.append(f"{name}/SKILL.md: YAML comments in frontmatter: {comment_lines}")
            desc = fm.get("description", "")
            if not desc.lower().startswith("use when"):
                offenders.append(
                    f"{name}/SKILL.md: description must start with 'Use when'. Got: {desc[:80]!r}"
                )
            for kw in self.BAD_KEYWORDS:
                if kw in desc:
                    offenders.append(f"{name}/SKILL.md: description contains workflow keyword {kw!r}")
        assert not offenders, "\n".join(offenders)


class TestSkillListingByteBudget:
    """nexus-cnzei.6 (injection audit S3): Claude Code drops descriptions
    from the Skill-tool listing once the listing exceeds a fraction of the
    context window (docs: skills troubleshooting, skillListingBudgetFraction)
    — the least-invoked skills lose their triggers first, silently. This
    repo cannot reproduce Claude Code's own internal budget math, but it CAN
    catch the thing that actually drives it up over time: every skill,
    command, and agent frontmatter `description:` field summed together.
    This is a regression ceiling, not a reproduction of the real threshold —
    it exists so a newly-added, unusually long description is caught here
    rather than discovered later as a dropped trigger for an unrelated
    skill."""

    #: Measured 2026-09-13: 72 descriptions (skills + commands + agents),
    #: 10,385 bytes total. Ceiling set with ~25% margin above that so
    #: ordinary one- or two-skill additions don't immediately fail this,
    #: while a description-bloat regression (or a batch of verbose new
    #: skills) still trips it.
    _TOTAL_DESCRIPTION_BUDGET_BYTES = 13000

    def _description_bytes(self, path: Path) -> int:
        # A regex line-grab on purpose, not _extract_frontmatter's full
        # yaml.safe_load: some command frontmatter (e.g. continuation.md's
        # `argument-hint: [foo] (optional...)`) is valid enough for Claude
        # Code's own lenient frontmatter reader but not strict YAML — an
        # unquoted `[` starts flow-sequence parsing and the trailing prose
        # after `]` breaks it. This test only needs one field's raw text.
        m = re.search(r"^description:\s*(.*)$", path.read_text(), re.MULTILINE)
        return len(m.group(1).strip().encode("utf-8")) if m else 0

    def test_total_description_bytes_under_budget(self) -> None:
        paths = list(skill_skill_mds()) + command_files() + agent_files()
        assert paths, "no skill/command/agent files found — path resolution broke"
        total = sum(self._description_bytes(p) for p in paths)
        assert total < self._TOTAL_DESCRIPTION_BUDGET_BYTES, (
            f"total frontmatter description bytes across {len(paths)} skills/"
            f"commands/agents is {total}B >= budget "
            f"{self._TOTAL_DESCRIPTION_BUDGET_BYTES}B — the Skill-tool "
            f"listing is at real risk of Claude Code dropping the least-"
            f"invoked skills' descriptions (injection audit S3)"
        )

def _command_bash_block(text: str) -> str | None:
    """Return the body of the documented ```! fenced bash block, or None.

    nexus-ln9y5: command preambles inject bash via a ```! fenced block (Claude
    Code's documented multi-line form). The legacy !{ } brace form never
    executed; it is forbidden by test_command_bash_uses_documented_syntax.
    """
    m = re.search(r"(?ms)^```!\n(.*?)\n```[ \t]*$", text)
    return m.group(1) if m else None

class TestCommandStructure:

    def test_no_unescaped_glob_in_grep(self) -> None:
        commands = command_files()
        assert len(commands) >= 15, f"only {len(commands)} commands examined"
        offenders: list[str] = []
        for cmd_path in commands:
            bad = re.findall(r'grep\s+["\']?\*\*["\']?', cmd_path.read_text())
            if bad:
                offenders.append(f"{cmd_path.name}: unescaped '**' in grep pattern: {bad}")
        assert not offenders, "\n".join(offenders)

    def test_command_bash_uses_documented_syntax(self) -> None:
        """Regression for nexus-ln9y5 (supersedes the nexus-t1b1k heredoc guard).

        Claude Code only executes command bash injection in the documented
        forms: inline ``!`cmd``` or a multi-line fenced ````` ```! ````` block.
        The legacy ``!{ ... }`` brace form (used by every conexus command
        through 5.1.1) is NOT a recognized syntax and emits as raw source — the
        preamble never runs. Additionally, ``$CLAUDE_PLUGIN_ROOT`` is empty in
        the command-bash context (it is scoped to hooks/MCP/LSP), so by-path
        script invocation fails.

        Static check only. The render path itself is covered by the
        cc-validation harness (the layer no unit test can reach).
        """
        commands = command_files()
        assert len(commands) >= 15, f"only {len(commands)} commands examined"
        offenders: list[str] = []
        for cmd_path in commands:
            text = cmd_path.read_text()
            if "!{" in text:
                offenders.append(
                    f"{cmd_path.name}: forbidden !{{ }} brace bash form (nexus-ln9y5). "
                    "It does not execute."
                )
            body = _command_bash_block(text)
            if body is not None and "$CLAUDE_PLUGIN_ROOT" in body:
                offenders.append(
                    f"{cmd_path.name}: $CLAUDE_PLUGIN_ROOT is empty in command bash "
                    "(nexus-ln9y5); inline the logic instead of invoking by path."
                )
        assert not offenders, "\n".join(offenders)

    def test_every_command_uses_single_line_nx(self) -> None:
        """RDR-130: a command injects bash via a single-line inline
        ``!`nx …` `` call — never a fenced ```! block or inlined heredoc.
        This locks the thin-command contract (logic lives in the nx CLI, not
        the .md), and with it the absence of any ```! block: no fenced-block
        rule (bash syntax, unguarded nx calls, inner triple backtick) can
        apply to a command that carries none.
        """
        commands = command_files()
        assert len(commands) >= 15, f"only {len(commands)} commands examined"
        offenders: list[str] = []
        for cmd_path in commands:
            text = cmd_path.read_text()
            if _command_bash_block(text) is not None:
                offenders.append(
                    f"{cmd_path.name}: migrated command must inject via a single-line "
                    "!`nx …` call, not a fenced ```! block (RDR-130 P1.5)."
                )
            if not re.search(r"(?m)^!`nx [^`]+`\s*$", text):
                offenders.append(
                    f"{cmd_path.name}: expected a single-line !`nx …` injection (RDR-130 P1.5)."
                )
        assert not offenders, "\n".join(offenders)



class TestCrossReferenceIntegrity:

    def test_relay_to_references_exist(self) -> None:
        agents = agent_files()
        assert len(agents) >= 10, f"only {len(agents)} agent files examined"
        known_agents = {p.stem for p in agents}
        offenders: list[str] = []
        for agent_path in agents:
            for ref in re.findall(r"relay to `([a-z][a-z0-9-]*)`", agent_path.read_text(), re.IGNORECASE):
                if ref not in known_agents:
                    offenders.append(f"{agent_path.name}: references unknown agent '{ref}'")
        assert not offenders, "\n".join(offenders)


class TestHooks:

    def test_hooks_json_exists_and_matchers(self) -> None:
        assert HOOKS_PATH.exists()
        hooks = json.loads(HOOKS_PATH.read_text())
        for entry in hooks.get("PostToolUse", []):
            has_matcher = "matcher" in entry
            has_filter = "grep" in entry.get("command", "") or "bd create" in entry.get("command", "")
            assert has_matcher or has_filter, \
                f"PostToolUse hook without matcher: {entry.get('command', '')[:80]}"

    def test_hooks_json_names_no_deleted_runner_helper(self) -> None:
        """RDR-215 nexus-q02nx.21 deleted ``_run_python_hook.sh``, and
        nexus-t9klx then ported every hook it launched into an ``nx-hook``
        verb. Nothing in hooks.json may route through the launcher.

        Inverts the retired ``test_python_hooks_use_runner_helper``, which
        asserted the OPPOSITE (every Python hook routed THROUGH the
        launcher) and was already vacuous under exec form even before this
        rewrite: the script path lives in ``args``, not ``command``, so its
        ``"_run_python_hook.sh" not in cmd`` walk over ``command`` alone
        found nothing to check -- it would have stayed green straight
        through the launcher's own deletion.

        Walks the PARSED structure (``command`` and every ``args`` string
        value), not the raw file text, so a hit inside unrelated JSON
        punctuation is not possible.
        """
        data = json.loads(HOOKS_PATH.read_text())
        events = data.get("hooks", data)
        examined = 0
        for event, entries in events.items():
            for entry in entries:
                for sub in entry.get("hooks", []):
                    examined += 1
                    command = sub.get("command", "")
                    assert "_run_python_hook.sh" not in command, (
                        f"[{event}] command still names the deleted launcher: {command!r}"
                    )
                    args = sub.get("args") or []
                    for a in args:
                        if isinstance(a, str):
                            assert "_run_python_hook.sh" not in a, (
                                f"[{event}] args still name the deleted launcher: {a!r}"
                            )
        assert examined >= _MIN_HOOKS_JSON_ENTRIES_EXAMINED, (
            f"only {examined} hooks.json sub-entries examined; expected >= "
            f"{_MIN_HOOKS_JSON_ENTRIES_EXAMINED}. The walk found nothing to "
            "check."
        )

    def test_plugin_json_declares_python_engine(self) -> None:
        """conexus/.claude-plugin/plugin.json must declare engines.python so the
        Python ≥3.12 requirement is discoverable from the plugin manifest,
        not just from the runtime guards in each hook script.
        """
        plugin_json = json.loads((PLUGIN_DIR / ".claude-plugin" / "plugin.json").read_text())
        engines = plugin_json.get("engines") or {}
        py_req = engines.get("python", "")
        assert py_req, "plugin.json missing 'engines.python' field"
        assert "3.12" in py_req, f"engines.python should require >=3.12, got {py_req!r}"

    def test_session_end_hook_registered(self) -> None:
        """Regression guard: SessionEnd must run a nexus cleanup entry point.

        The hook was removed in v1.10.1 with the (incorrect) reasoning that
        the T1 chroma server stops with the parent process tree. It does not:
        chroma is spawned with ``start_new_session=True`` (so safe_killpg
        reaches its multiprocessing workers; see beads nexus-dc57 / nexus-ze2a)
        which detaches it from the terminal's process group, so OS-level
        reaping never collects it. Removing this hook leaks one chroma child
        per Claude Code session indefinitely. This test fails loudly if
        anyone drops the hook again.

        RDR-094 Phase C (nexus-l828) swapped the dispatch from
        ``nx hook session-end-detach`` to ``nx-session-end-launcher`` to
        preserve the fork-first cold-start race fix. Either name is
        acceptable as a SessionEnd entry; what matters is that *some*
        nexus cleanup entry is registered.
        """
        data = json.loads(HOOKS_PATH.read_text())
        events = data.get("hooks", data)
        session_end = events.get("SessionEnd")
        assert session_end, "SessionEnd hook is missing -- see test docstring for why it must stay"
        commands = [
            sub.get("command", "")
            for entry in session_end
            for sub in entry.get("hooks", [])
        ]
        valid_entries = ("nx hook session-end", "nx-session-end-launcher")
        assert any(any(v in cmd for v in valid_entries) for cmd in commands), (
            f"SessionEnd registered but does not invoke a nexus cleanup entry "
            f"({valid_entries}); found commands: {commands}"
        )


class TestStandaloneSkillRegistry:

    def test_standalone_skill_directory_exists(self) -> None:
        for skill_name in REGISTRY.get("standalone_skills", {}):
            skill_dir = SKILLS_DIR / skill_name
            assert skill_dir.is_dir(), f"standalone_skills '{skill_name}' has no directory"
            assert (skill_dir / "SKILL.md").exists(), f"standalone_skills '{skill_name}' has no SKILL.md"


EXPECTED_SHARED_FILES = [
    "RELAY_TEMPLATE.md", "CONTEXT_PROTOCOL.md", "ERROR_HANDLING.md",
    "MAINTENANCE.md", "README.md",
]


class TestSharedResources:

    def test_shared_files_exist_and_are_non_empty(self) -> None:
        """Includes CONTEXT_PROTOCOL.md, which every agent references."""
        offenders: list[str] = []
        for filename in EXPECTED_SHARED_FILES:
            path = SHARED_DIR / filename
            if not path.exists():
                offenders.append(f"resources/agent-shared/{filename} missing")
            elif len(path.read_text()) <= 100:
                offenders.append(f"resources/agent-shared/{filename} nearly empty")
        assert not offenders, "\n".join(offenders)

    def test_no_unregistered_shared_files(self) -> None:
        orphans = {p.name for p in SHARED_DIR.glob("*.md")} - set(EXPECTED_SHARED_FILES)
        assert not orphans, f"Unexpected files in resources/agent-shared/: {orphans}"

    def test_shared_resources_not_agent_discoverable(self) -> None:
        """nexus-cnzei.4 (S1): the shared reference docs must not live under
        agents/, where recursive discovery lists each of them as a
        dispatchable conexus:_shared:<name> agent entry with nothing
        meaningful to dispatch."""
        assert not (AGENTS_DIR / "_shared").exists(), (
            "agents/_shared/ must not exist — shared reference docs moved to "
            "resources/agent-shared/ (nexus-cnzei.4)"
        )
        assert SHARED_DIR.is_dir()


class TestPluginLinksResolve:
    """Three link families, one rule: a path the plugin names must exist.
    Each category carries its own non-vacuity floor."""

    def test_links_and_plugin_root_refs_resolve(self) -> None:
        offenders: list[str] = []
        shared = _collect_shared_links()
        assert len(shared) >= 40, f"only {len(shared)} agent-shared links examined"
        for source_file, raw_path in shared:
            resolved = (source_file.parent / raw_path.split("#")[0]).resolve()
            if not resolved.exists():
                offenders.append(
                    f"agent-shared link: {source_file.relative_to(PLUGIN_DIR)}: "
                    f"{raw_path!r} -> {resolved} missing"
                )
        tier = _collect_tier_discipline_links()
        assert len(tier) >= 5, f"only {len(tier)} tier-discipline links examined"
        for source_file, raw_path in tier:
            resolved = (source_file.parent / raw_path.split("#")[0]).resolve()
            if not resolved.exists():
                offenders.append(
                    f"tier-discipline link: {source_file.relative_to(PLUGIN_DIR)}: "
                    f"{raw_path!r} -> {resolved} missing"
                )
        root_refs = _collect_plugin_root_refs()
        assert len(root_refs) >= 5, f"only {len(root_refs)} $CLAUDE_PLUGIN_ROOT refs examined"
        for source, rel_path in root_refs:
            if not (PLUGIN_DIR / rel_path).exists():
                offenders.append(f"$CLAUDE_PLUGIN_ROOT ref: {source}: {rel_path} missing")
        assert not offenders, "\n".join(offenders)


class TestTierDisciplineLinks:
    """nexus-cnzei.6 fix round (CRE 4)."""

    def test_exactly_the_known_eight_skills_carry_the_link(self) -> None:
        linking_skill_names = {
            src.parent.name for src, _ in _collect_tier_discipline_links()
            if src.parent.parent == SKILLS_DIR
        }
        assert linking_skill_names == _TIER_DISCIPLINE_SKILLS, (
            f"skills linking resources/tier-discipline.md are {sorted(linking_skill_names)}, "
            f"expected exactly {sorted(_TIER_DISCIPLINE_SKILLS)} — update both together "
            "when adding or removing a skill that prescribes this preamble"
        )

    def test_no_skill_reduplicates_the_full_preamble_inline(self) -> None:
        """A 9th skill re-pasting the full checklist instead of linking to
        resources/tier-discipline.md would recreate the exact duplication
        this bead eliminated, silently. This is the one line specific
        enough to the full block that a legitimate short mention of "search"
        or "widest" elsewhere would never match it."""
        offenders = [
            p for p in skill_skill_mds()
            if "1. **Read** widest" in p.read_text()
        ]
        assert not offenders, (
            f"these skills re-duplicate the tier-discipline checklist inline "
            f"instead of linking to resources/tier-discipline.md: "
            f"{[p.parent.name for p in offenders]}"
        )


# ── Marketplace version sync ─────────────────────────────────────────────────


class TestMarketplaceVersion:

    def _pyproject_version(self) -> str:
        with PYPROJECT_PATH.open("rb") as f:
            return tomllib.load(f)["project"]["version"]

    def test_marketplace_json_exists(self) -> None:
        assert MARKETPLACE_PATH.exists()

    def test_marketplace_version_matches_pyproject(self) -> None:
        pv = self._pyproject_version()
        for plugin in json.loads(MARKETPLACE_PATH.read_text()).get("plugins", []):
            assert plugin.get("version", "") == pv, \
                f"marketplace.json '{plugin['name']}' version != pyproject.toml {pv!r}"

    def test_every_plugins_own_plugin_json_version_matches_pyproject(self) -> None:
        """nexus-smsau follow-up (2026-09-27): ``conexus/.claude-plugin/
        plugin.json`` had NO dedicated parity test -- only sn's own copy
        (``tests/test_sn_plugin.py::test_sn_version_matches_pyproject``,
        now REMOVED as an exact duplicate of what this test does for the
        "sn" case) did. Loops over EVERY plugin marketplace.json lists
        (today conexus and sn; not a fixed pair), so a plugin added there
        gets this coverage with no test edit, AND conexus is covered for
        the first time.

        ``test_sn_version_matches_plugin_json`` (still in
        ``tests/test_sn_plugin.py``) stays -- it compares marketplace.json's
        own per-plugin version FIELD against that plugin's plugin.json, a
        different pair of values than pyproject.toml vs plugin.json, so it
        is not redundant with this test (only transitively implied by it
        together with :func:`test_marketplace_version_matches_pyproject`,
        which is not the same as duplicating it).
        """
        pv = self._pyproject_version()
        for plugin in json.loads(MARKETPLACE_PATH.read_text()).get("plugins", []):
            source = plugin.get("source")
            assert isinstance(source, dict), (
                f"marketplace.json {plugin['name']!r} source must be the "
                f"object form -- covered by test_marketplace_source_ref_matches_pyproject"
            )
            plugin_json_path = REPO_ROOT / source.get("path", "") / ".claude-plugin" / "plugin.json"
            assert plugin_json_path.exists(), (
                f"marketplace.json {plugin['name']!r} source.path has no "
                f"plugin.json at {plugin_json_path} -- covered by "
                f"test_plugin_source_path_has_plugin_json"
            )
            plugin_json_version = json.loads(plugin_json_path.read_text()).get("version")
            assert plugin_json_version == pv, (
                f"{plugin['name']}/.claude-plugin/plugin.json version "
                f"{plugin_json_version!r} != pyproject.toml {pv!r}"
            )

    def test_marketplace_source_ref_matches_pyproject(self) -> None:
        """nexus-mkj6u, extended by RDR-197 P1b (nexus-a2wmi.3): each
        plugin's source.ref is judged under invariant R, per plugin --
        the client form 'v{pv}', or the anchored form
        'plugin-v{pv}-{n}' with a clean tag-anchored wheel-surface proof
        (scripts/plugin_channel.py's docstring owns invariant R and the
        anchoring rule; this test cites them, it does not restate them).
        Pinning the plugin source to a tag decouples main commits from
        marketplace publication; CI enforces this per-plugin parity so a
        partial or malformed bump can't ship.
        """
        pv = self._pyproject_version()
        for plugin in json.loads(MARKETPLACE_PATH.read_text()).get("plugins", []):
            source = plugin.get("source")
            assert isinstance(source, dict), (
                f"marketplace.json '{plugin['name']}' source must be the "
                f"object form (git-subdir with ref pinning), got {source!r}"
            )
            assert source.get("source") == "git-subdir", (
                f"marketplace.json '{plugin['name']}' source.source must be "
                f"'git-subdir' for tag-pinned releases, got {source.get('source')!r}"
            )
            _assert_ref_valid_for_plugin(
                plugin["name"], source.get("ref", ""), pv, cwd=REPO_ROOT
            )

    def test_plugin_source_path_exists_in_repo(self) -> None:
        """nexus-mkj6u: marketplace.json plugins[].source.path must point
        at an actual directory in the repo. Catches typos and orphaned
        entries before they ship.

        (Pattern from the global_directives marketplace-pinned-source-
        playbook; adopted from the parallel palinex implementation.)
        """
        for plugin in json.loads(MARKETPLACE_PATH.read_text()).get("plugins", []):
            source = plugin["source"]
            assert isinstance(source, dict), "covered by ref test"
            path = source.get("path", "")
            plugin_dir = REPO_ROOT / path
            assert plugin_dir.is_dir(), (
                f"marketplace.json '{plugin['name']}' source.path "
                f"{path!r} -> {plugin_dir} does not exist or is not a directory"
            )

    def test_plugin_source_path_has_plugin_json(self) -> None:
        """Each plugin's source.path subdir must contain its own
        .claude-plugin/plugin.json. CLAUDE_PLUGIN_ROOT resolves there
        at runtime."""
        for plugin in json.loads(MARKETPLACE_PATH.read_text()).get("plugins", []):
            source = plugin["source"]
            assert isinstance(source, dict)
            manifest = REPO_ROOT / source.get("path", "") / ".claude-plugin" / "plugin.json"
            assert manifest.exists(), (
                f"marketplace.json '{plugin['name']}' source.path is missing "
                f"its own .claude-plugin/plugin.json at {manifest}"
            )

    def test_mcpb_manifest_version_matches_pyproject(self) -> None:
        """nexus-bsjro: the Claude Desktop .mcpb bundle's manifest.json
        and pyproject.toml versions both track the canonical conexus
        version. CI catches forgot-to-bump-mcpb releases."""
        mcpb_manifest = REPO_ROOT / "mcpb" / "manifest.json"
        mcpb_pyproject = REPO_ROOT / "mcpb" / "pyproject.toml"
        # RDR-126 §7 (nexus-2yajx): the .mcpb bundle ships, so its
        # manifest is a hard requirement, not a soft skip. A missing
        # manifest is a regression, not a no-op.
        assert mcpb_manifest.exists(), (
            "mcpb/manifest.json is missing — the Claude Desktop .mcpb "
            "bundle is a shipped product surface (RDR-126); it must exist."
        )
        assert mcpb_pyproject.exists(), (
            "mcpb/pyproject.toml is missing — the .mcpb bundle pins the "
            "published conexus version; it must exist."
        )
        pv = self._pyproject_version()
        manifest_version = json.loads(mcpb_manifest.read_text()).get("version")
        assert manifest_version == pv, (
            f"mcpb/manifest.json version {manifest_version!r} "
            f"!= pyproject.toml {pv!r}"
        )
        with mcpb_pyproject.open("rb") as f:
            mcpb_proj = tomllib.load(f)["project"]
        mcpb_pv = mcpb_proj["version"]
        assert mcpb_pv == pv, (
            f"mcpb/pyproject.toml version {mcpb_pv!r} "
            f"!= pyproject.toml {pv!r}"
        )

    def test_mcpb_pins_bare_conexus(self) -> None:
        """The .mcpb bundle depends on bare ``conexus``, the same distribution
        the Claude Code plugin's ``nx-mcp`` runs from (a generation built
        without extras). #1068 pinned ``conexus[local]`` because the client
        once embedded T3 queries itself; since the service-vector cutover the
        engine embeds every T3 write and search, and collection names follow
        the service's bge-768 token whether or not fastembed is importable
        (nexus-xq8f9, ``corpus.effective_embedding_model_for_writes``). With
        fastembed present the extension's ``LocalEmbeddingFunction`` (plan
        session cache) picked a different model than the plugin's and fetched
        its own bge copy on first use. Also assert the pin version tracks the
        mcpb version, so a release bump can't silently drop or stale it."""
        mcpb_pyproject = REPO_ROOT / "mcpb" / "pyproject.toml"
        assert mcpb_pyproject.exists()
        with mcpb_pyproject.open("rb") as f:
            mcpb_proj = tomllib.load(f)["project"]
        deps = mcpb_proj["dependencies"]
        conexus_dep = next((d for d in deps if d.startswith("conexus")), None)
        assert conexus_dep is not None, "mcpb/pyproject.toml has no conexus dependency"
        assert Requirement(conexus_dep).extras == set(), (
            f"mcpb/pyproject.toml must pin bare conexus, got {conexus_dep!r}: an "
            "extra makes the extension's environment differ from the plugin's."
        )
        # The pin version must equal the mcpb version (release bump must update
        # both the [project].version AND this dependency pin in lock-step).
        assert f">={mcpb_proj['version']}" in conexus_dep, (
            f"mcpb conexus pin {conexus_dep!r} must pin >={mcpb_proj['version']} "
            "(the mcpb version) — a release bump left the dependency pin stale."
        )

    def test_mcpb_manifest_python_constraint_matches_pyproject(self) -> None:
        """nexus-oqh4s (round 2 of the 7.45.0 audit-fixes bead): a 2026-09-13
        commit (207f9108d) aligned mcpb/pyproject.toml's requires-python
        (">=3.12,<3.14") with the root pyproject.toml, but left
        mcpb/manifest.json's own compatibility.runtimes.python field at the
        stale ">=3.12" -- the same drift class the commit was fixing, just
        in the sibling field. Nothing enforced these two mcpb surfaces stay
        in lock-step, so a future python-floor bump can silently repeat it.
        This pins them together going forward."""
        mcpb_manifest = REPO_ROOT / "mcpb" / "manifest.json"
        mcpb_pyproject = REPO_ROOT / "mcpb" / "pyproject.toml"
        assert mcpb_manifest.exists()
        assert mcpb_pyproject.exists()
        manifest_python = (
            json.loads(mcpb_manifest.read_text())
            .get("compatibility", {})
            .get("runtimes", {})
            .get("python")
        )
        with mcpb_pyproject.open("rb") as f:
            mcpb_requires_python = tomllib.load(f)["project"]["requires-python"]
        assert manifest_python == mcpb_requires_python, (
            f"mcpb/manifest.json compatibility.runtimes.python "
            f"{manifest_python!r} != mcpb/pyproject.toml requires-python "
            f"{mcpb_requires_python!r} -- bump both together."
        )

    def test_release_workflow_verifies_mcpb_version(self) -> None:
        """RDR-126 §7 (nexus-2yajx): the release workflow must guard the
        .mcpb manifest version against the tag at release time, as a
        belt-and-braces companion to the pytest parity check. Without
        this, a forgot-to-bump-mcpb release could ship a stale bundle
        version even though CI's pytest run is green on the release SHA
        (the tag is applied after merge)."""
        workflow = REPO_ROOT / ".github" / "workflows" / "release.yml"
        body = workflow.read_text()
        assert "mcpb/manifest.json" in body, (
            "release.yml has no step referencing mcpb/manifest.json; the "
            "release-time mcpb version-sync guard is missing."
        )
        # The guard must compare against the tag (same mechanism as the
        # pyproject tag check just above it).
        assert re.search(r"mcpb/manifest\.json.*version|version.*mcpb/manifest\.json", body, re.DOTALL), (
            "release.yml references mcpb/manifest.json but not its version; "
            "the sync guard must check the manifest version."
        )

    def test_release_workflow_waits_for_simple_index_before_gh_release(self) -> None:
        """nexus-r433b: PyPI's simple index lags the upload by ~10-25 min
        (4 consecutive releases measured); resolvers read the simple
        index, so a GitHub release created inside that window announces a
        version that hard-fails ==/>= installs — the .mcpb download it
        carries dies with a resolver error. release.yml must poll the
        simple index AFTER the PyPI publish and BEFORE creating the
        GitHub release, so announcement never precedes installability."""
        workflow = REPO_ROOT / ".github" / "workflows" / "release.yml"
        body = workflow.read_text()
        publish = body.find("gh-action-pypi-publish")
        wait = body.find("wait_pypi_simple_index.py")
        gh_release = body.find("action-gh-release")
        assert publish != -1, "release.yml no longer publishes via gh-action-pypi-publish?"
        assert gh_release != -1, "release.yml no longer creates the GH release via action-gh-release?"
        assert wait != -1, (
            "release.yml has no wait_pypi_simple_index.py step; the GH release "
            "(announcement + .mcpb download) can go live inside the PyPI "
            "propagation window (nexus-r433b)."
        )
        assert publish < wait < gh_release, (
            "release.yml's simple-index wait must sit between the PyPI publish "
            "and the GitHub-release step — polling before publish waits on "
            "nothing, and polling after the GH release defeats the point."
        )
        assert "--require-served" in body, (
            "release.yml's wait step must pass --require-served: the next "
            "step is the announcement with no downstream check, so the "
            "below-served-max case must fail loud there, never fast-exit 0 "
            "(substantive-critic finding, 2026-08-31)."
        )

    def test_plugin_source_sha_is_well_formed_when_present(self) -> None:
        """Optional `source.sha` for tag-force-push protection. When
        present, must be a 40-character lowercase hex string (full
        git SHA-1). Empty or absent is allowed; partial / uppercase
        / non-hex is rejected to keep the field useful as an integrity
        check."""
        for plugin in json.loads(MARKETPLACE_PATH.read_text()).get("plugins", []):
            source = plugin["source"]
            sha = source.get("sha")
            if not sha:
                continue
            assert isinstance(sha, str), f"source.sha must be a string, got {type(sha).__name__}"
            assert len(sha) == 40, (
                f"marketplace.json '{plugin['name']}' source.sha must be 40 chars "
                f"(full git SHA-1), got {len(sha)}: {sha!r}"
            )
            assert all(c in "0123456789abcdef" for c in sha), (
                f"marketplace.json '{plugin['name']}' source.sha must be "
                f"lowercase hex, got: {sha!r}"
            )

    def test_uv_lock_version_matches_pyproject(self) -> None:
        pv = self._pyproject_version()
        uv_lock = REPO_ROOT / "uv.lock"
        assert uv_lock.exists()
        m = re.search(
            r'\[\[package\]\]\s+name\s*=\s*"conexus"\s+version\s*=\s*"([^"]+)"',
            uv_lock.read_text(),
        )
        assert m is not None, "conexus not found in uv.lock"
        assert m.group(1) == pv, f"uv.lock {m.group(1)!r} != pyproject.toml {pv!r}"


# ── RDR-197 P1b (nexus-a2wmi.3): per-plugin source.ref parity ───────────────
#
# Invariant R and the anchoring rule live in scripts/plugin_channel.py's
# module docstring; this section cites them, it does not restate them.
# The channel is stateless: no counter file is read or written anywhere
# below (RDR amended by 874bd681c).


def _resolve_ref_or_head(ref: str, *, cwd: Path) -> str:
    """The proof's target: *ref* itself when it resolves as a tag, else the
    cut PR's head commit before the tag exists (``plugin_channel.cut_head_sha``:
    the pull_request payload's head sha in CI — HEAD there is the synthetic
    merge ref — and HEAD on the cut branch). Per the ANCHORING rule: never
    a merge base, never a branch that tracks ongoing development.
    """
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=cwd, capture_output=True, text=True,
    )
    if probe.returncode == 0:
        return ref
    return cut_head_sha(cwd=cwd)


def _assert_ref_valid_for_plugin(
    plugin_name: str, ref: str, pv: str, *, cwd: Path
) -> None:
    """Invariant R, judged for ONE plugin's ref alone -- never a quantifier
    across plugins. Accepts the client form unchanged, or the anchored
    form when its shape parses, its version matches *pv*, and the
    tag-anchored wheel-surface proof (base client tag to the plugin's
    anchored tag when it resolves, else HEAD) is clean.
    """
    client_form = f"v{pv}"
    if ref == client_form:
        return
    parsed = parse_plugin_tag(ref)
    assert parsed is not None, (
        f"marketplace.json '{plugin_name}' source.ref {ref!r} matches "
        f"neither accepted shape: the client form {client_form!r}, nor "
        f"the anchored form 'plugin-v{pv}-<n>' (n >= 1, no leading zero)"
    )
    version, _n = parsed
    assert version == pv, (
        f"marketplace.json '{plugin_name}' source.ref {ref!r} anchors "
        f"version {version!r}, which != pyproject.toml {pv!r}"
    )
    # Blind-checkout sentinel BEFORE the proof (review finding, a2wmi.6):
    # in a checkout that never fetched the base client tag the diff below
    # would fail as an opaque GitCommandError; the sentinel names the real
    # cause and the remedy. Today ci.yml's lint job checks out with
    # fetch-depth 0, but that is that workflow's choice, not a guarantee.
    assert_tag_visibility(pv, cwd=cwd)
    target = _resolve_ref_or_head(ref, cwd=cwd)
    offenders = wheel_surface_offenders(client_form, target, cwd=cwd)
    assert offenders == [], (
        f"marketplace.json '{plugin_name}' source.ref {ref!r} touches "
        f"wheel-surface paths outside the channel allowlist: {offenders}"
    )


def _channel_git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _channel_repo(tmp_path: Path, version: str) -> Path:
    """A tmp git repo tagged ``v{version}`` at its seed commit -- the base
    client tag every anchored-form proof diffs against. Mirrors the
    fixture style in tests/test_plugin_channel.py's ``_make_repo``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _channel_git("init", "-q", "-b", "main", cwd=repo)
    _channel_git("config", "user.email", "test@test.invalid", cwd=repo)
    _channel_git("config", "user.name", "test", cwd=repo)
    (repo / "seed").write_text("seed\n", encoding="utf-8")
    _channel_git("add", "seed", cwd=repo)
    _channel_git("commit", "-q", "-m", "seed", cwd=repo)
    _channel_git("tag", f"v{version}", cwd=repo)
    return repo


def _channel_cut(repo: Path, tag: str, paths: list[str]) -> None:
    """Commit content at *paths* and tag the result *tag*: a clean or
    dirty anchored cut depending on whether *paths* sit inside the channel
    allowlist (scripts/plugin_channel.py's ALLOWED_PREFIXES/DENIED_PREFIXES).
    """
    for rel in paths:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("cut content\n", encoding="utf-8")
        _channel_git("add", rel, cwd=repo)
    _channel_git("commit", "-q", "-m", "cut", cwd=repo)
    _channel_git("tag", tag, cwd=repo)


_PARITY_VERSION = "9.9.9"


class TestSourceRefPerPluginParity:
    """RDR-197 P1b (nexus-a2wmi.3), steps 7-12: the mixed-state truth
    table's parity row (bead .4's table), covered against synthetic
    marketplace refs and tmp git repos rather than the real (today
    all-client-form) marketplace.json.
    """

    VERSION = _PARITY_VERSION

    def test_mixed_state_anchored_conexus_client_sn_passes(
        self, tmp_path: Path
    ) -> None:
        """Step 7 -- S2/S3: conexus anchored plugin-v{pv}-1 with a clean
        proof, sn client form v{pv}. This is the cut-PR state and the
        post-cut develop state; a rule written as a quantifier across all
        refs (e.g. "every ref must share one shape") rejects this state,
        which is exactly why invariant R is judged per plugin.
        """
        repo = _channel_repo(tmp_path, self.VERSION)
        _channel_cut(repo, f"plugin-v{self.VERSION}-1", ["conexus/skills/ok.md"])
        _assert_ref_valid_for_plugin(
            "conexus", f"plugin-v{self.VERSION}-1", self.VERSION, cwd=repo
        )
        _assert_ref_valid_for_plugin("sn", f"v{self.VERSION}", self.VERSION, cwd=repo)

    def test_both_anchored_independent_sequence_numbers_pass(
        self, tmp_path: Path
    ) -> None:
        """Step 8: two plugins anchored at independent sequence numbers --
        the sixth truth-table cell bead .4 hands off to this bead. Nothing
        cross-checks one plugin's n against another's, so conexus can hold
        the higher number while sn holds the lower.
        """
        repo = _channel_repo(tmp_path, self.VERSION)
        _channel_cut(repo, f"plugin-v{self.VERSION}-1", ["conexus/skills/a.md"])
        _channel_cut(repo, f"plugin-v{self.VERSION}-2", ["sn/commands/b.md"])
        _assert_ref_valid_for_plugin(
            "conexus", f"plugin-v{self.VERSION}-2", self.VERSION, cwd=repo
        )
        _assert_ref_valid_for_plugin(
            "sn", f"plugin-v{self.VERSION}-1", self.VERSION, cwd=repo
        )

    @pytest.mark.parametrize(
        "ref", [f"plugin-v{_PARITY_VERSION}-0", f"plugin-v{_PARITY_VERSION}-01"]
    )
    def test_zero_and_zero_padded_sequence_numbers_are_rejected(
        self, tmp_path: Path, ref: str
    ) -> None:
        """Step 9: n=0 and a leading-zero n are shape failures -- rejected
        by parse_plugin_tag's own ``[1-9]\\d*`` before any git call."""
        with pytest.raises(AssertionError):
            _assert_ref_valid_for_plugin("conexus", ref, self.VERSION, cwd=tmp_path)

    def test_anchored_ref_for_a_different_version_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """Step 10: the anchored form parses but names a version other
        than pyproject's -- rejected before any proof is attempted."""
        with pytest.raises(AssertionError):
            _assert_ref_valid_for_plugin(
                "conexus", "plugin-v1.2.3-1", self.VERSION, cwd=tmp_path
            )

    def test_anchored_ref_touching_src_nexus_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """Step 11: shape and version both pass, but the range from the
        base client tag to the anchored tag touches src/nexus/ -- off-
        allowlist wheel content, so the proof fails."""
        repo = _channel_repo(tmp_path, self.VERSION)
        _channel_cut(repo, f"plugin-v{self.VERSION}-1", ["src/nexus/cli.py"])
        with pytest.raises(AssertionError):
            _assert_ref_valid_for_plugin(
                "conexus", f"plugin-v{self.VERSION}-1", self.VERSION, cwd=repo
            )

    def test_all_client_form_state_passes(self, tmp_path: Path) -> None:
        """Step 12 -- S1/S4: every plugin at the client form v{pv}, the
        pre-cut and post-client-release states. No git call happens: the
        client-form branch returns before any tag lookup."""
        _assert_ref_valid_for_plugin(
            "conexus", f"v{self.VERSION}", self.VERSION, cwd=tmp_path
        )
        _assert_ref_valid_for_plugin("sn", f"v{self.VERSION}", self.VERSION, cwd=tmp_path)

    def test_anchored_ref_in_a_blind_checkout_names_the_cause(
        self, tmp_path: Path
    ) -> None:
        """R1 review finding (a2wmi.6): a checkout that never fetched the
        base client tag must fail as TagVisibilityError naming the fetch
        remedy, not as an opaque diff failure. Dormant until a real cut
        PR meets such a checkout, which is exactly when an opaque error
        would cost the most."""
        from plugin_channel import TagVisibilityError

        repo = tmp_path / "blind"
        repo.mkdir()
        _channel_git("init", "-q", "-b", "main", cwd=repo)
        _channel_git("config", "user.email", "test@test.invalid", cwd=repo)
        _channel_git("config", "user.name", "test", cwd=repo)
        (repo / "seed").write_text("seed\n", encoding="utf-8")
        _channel_git("add", "seed", cwd=repo)
        _channel_git("commit", "-q", "-m", "seed", cwd=repo)
        with pytest.raises(TagVisibilityError, match="blind to tags"):
            _assert_ref_valid_for_plugin(
                "conexus", f"plugin-v{self.VERSION}-1", self.VERSION, cwd=repo
            )


#: The release boundary at which ``PINNED_SERVICE_TAG`` must become non-None.
#: Pre-6.0 the pin is intentionally ``None`` (no engine-service release exists
#: yet); the first major that ships the service stack must carry a real pin.
#: NOTE: this fires on ANY 6.x pyproject version, including dev/pre-release
#: strings like ``6.0.0.dev0`` — ``_pyproject_major()`` parses the leading
#: integer, so 6.x feature-branch work must carry a real pin from the first
#: 6.x version bump, not only at the final release commit (substantive-critic
#: SIG-2, 2026-06-23).
_ENGINE_PIN_REQUIRED_MAJOR = 6


def _pyproject_major() -> int:
    with PYPROJECT_PATH.open("rb") as f:
        version = tomllib.load(f)["project"]["version"]
    return int(version.split(".", 1)[0])


class TestEnginePinParity:
    """nexus-3rq00 — extend the release parity gate across the Python/Java boundary.

    conexus↔engine compatibility rides two hand-edited constants that sit
    OUTSIDE the 7-manifest parity gate, with no cross-check between them:

    * ``PINNED_SERVICE_TAG`` (``src/nexus/daemon/binary_install.py``) — the
      ``engine-service-vX.Y.Z`` GitHub release tag this build auto-installs.
      ``None`` by design pre-6.0 (no real engine-service release exists yet).
    * ``REQUIRED_ENGINE_VERSION`` (``src/nexus/engine_version.py``) — the
      minimum engine version any nexus client (native/local guided-upgrade
      handoff OR the managed-cloud probe) requires (RDR-002; unified single
      source of truth, nexus-b6qlf).

    Without a cross-check, a release could ship a client pinned to an engine
    tag whose numeric version is BELOW ``REQUIRED_ENGINE_VERSION`` — a client
    that auto-installs an engine it then refuses as too old. These assertions
    catch that, and gate the "pin must be set" requirement on the 6.0 release
    boundary so the suite stays green pre-6.0 while the pin is legitimately
    ``None``.
    """

    def test_pinned_tag_at_or_above_required_release(self) -> None:
        """If a pin is set, its numeric version must equal REQUIRED_ENGINE_VERSION
        EXACTLY, not just be >= it.

        Prior to 2026-07-12 this only checked ``>=``, tolerating a pin that
        had drifted stale relative to the floor (pinned at v0.1.36 while the
        floor had already moved to a verified v0.1.39). ``PINNED_SERVICE_TAG``
        is now DERIVED from ``REQUIRED_ENGINE_VERSION`` in
        ``binary_install.py`` (one number, not two independently-hand-typed
        constants), so this assertion is now a regression guard against a
        FUTURE edit reintroducing an independent literal — it should be
        impossible to fail by construction, and failing it means someone
        bypassed the derivation.

        Skipped only while ``PINNED_SERVICE_TAG is None`` (no engine-service
        release pinned yet) — NOT keyed on the major version, so a pin set
        during 5.x dev work is validated too. The ``engine-service-v``
        namespace prefix is stripped before parsing — the pin format is
        ``engine-service-vX.Y.Z``, not ``vX.Y.Z``.
        """
        from nexus.daemon.binary_install import PINNED_SERVICE_TAG, TAG_NAMESPACE_PREFIX
        from nexus.engine_version import REQUIRED_ENGINE_VERSION, parse_engine_version

        if PINNED_SERVICE_TAG is None:
            pytest.skip(
                "PINNED_SERVICE_TAG is None — no engine-service-v* release pinned "
                "yet (set when the first engine-service tag is cut)"
            )

        tag = PINNED_SERVICE_TAG
        assert tag.startswith(TAG_NAMESPACE_PREFIX), (
            f"PINNED_SERVICE_TAG {tag!r} must carry the "
            f"{TAG_NAMESPACE_PREFIX!r} namespace prefix"
        )
        parsed = parse_engine_version(tag[len(TAG_NAMESPACE_PREFIX):])
        assert parsed is not None, (
            f"PINNED_SERVICE_TAG {tag!r} is not a clean release semver "
            f"(blank/SNAPSHOT/dev/pre-release are rejected fail-closed)"
        )
        assert parsed == REQUIRED_ENGINE_VERSION, (
            f"PINNED_SERVICE_TAG {tag!r} -> {parsed} does NOT EXACTLY MATCH "
            f"REQUIRED_ENGINE_VERSION {REQUIRED_ENGINE_VERSION}. There is only "
            f"one number now -- PINNED_SERVICE_TAG must be DERIVED from "
            f"REQUIRED_ENGINE_VERSION in binary_install.py, never an "
            f"independent literal. If this fired, someone reintroduced a "
            f"hand-typed PINNED_SERVICE_TAG; fix the derivation, not this test."
        )

    @pytest.mark.xfail(
        condition=_pyproject_major() < _ENGINE_PIN_REQUIRED_MAJOR,
        reason=(
            "nexus-3rq00: PINNED_SERVICE_TAG intentionally None on pre-6.0 builds "
            "— no engine-service-v* release exists until 6.0. strict=True trips "
            "this xfail if a pin gets set early (XPASS) so the deferral stays visible."
        ),
        strict=True,
    )
    def test_pin_is_set_at_release_boundary(self) -> None:
        """At the 6.0 cut (and beyond), ``PINNED_SERVICE_TAG`` must be non-None.

        xfail(strict=True) below ``_ENGINE_PIN_REQUIRED_MAJOR`` rather than a
        plain skip: a skip is invisible in CI summaries, an xfail is a tracked,
        searchable expected-failure that self-trips if the condition stops
        holding. At 6.x the xfail lifts and this becomes a hard assertion — a
        release that bumps pyproject to 6.x without a real pin trips it.
        (nexus-3rq00 / blocks the Release-N readiness gate nexus-h3ilf;
        substantive-critic SIG-1, 2026-06-23.)
        """
        from nexus.daemon.binary_install import PINNED_SERVICE_TAG

        assert PINNED_SERVICE_TAG is not None, (
            "PINNED_SERVICE_TAG must be a real engine-service-vX.Y.Z tag at the "
            "6.0 release boundary — a release cut from develop with the service "
            "stack must pin a compatible engine. See binary_install.py."
        )


# ── Plugin root manifest ─────────────────────────────────────────────────────

REQUIRED_ROOT_FILES = ["registry.yaml", "README.md", "CHANGELOG.md", "hooks/hooks.json"]
REQUIRED_ROOT_DIRS = [
    "agents", "resources/agent-shared", "skills", "commands",
    "hooks/scripts", "resources/rdr", "resources/rdr/post-mortem",
]


class TestPluginRootManifest:

    def test_required_root_files_exist_and_are_non_empty(self) -> None:
        offenders: list[str] = []
        for rel_path in REQUIRED_ROOT_FILES:
            full = PLUGIN_DIR / rel_path
            if not full.exists():
                offenders.append(f"Missing: {rel_path}")
            elif full.stat().st_size == 0:
                offenders.append(f"Empty: {rel_path}")
        assert not offenders, "\n".join(offenders)

    def test_required_root_dirs_exist_and_are_non_empty(self) -> None:
        offenders: list[str] = []
        for rel_dir in REQUIRED_ROOT_DIRS:
            full = PLUGIN_DIR / rel_dir
            if not full.is_dir():
                offenders.append(f"Missing dir: {rel_dir}")
            elif not any(full.iterdir()):
                offenders.append(f"Empty dir: {rel_dir}")
        assert not offenders, "\n".join(offenders)


# ── One entry point per situation (nexus-cnzei.4) ────────────────────────────
#
# A command file (conexus/commands/<name>.md) and a skill (conexus/skills/
# <name>/SKILL.md) sharing the same <name> is a real collision: Claude Code's
# listing shows only one entry per name, so the other is shadowed and
# unreachable by name — the audit behind nexus-cnzei.4 found 17 such pairs.
# All 17 are now resolved: deleting the redundant command, deleting a skill
# that depended on its command having already run, or renaming the skill.
#
# The last four (rdr-gate, rdr-fix, rdr-accept, rdr-audit) needed the rename
# path rather than a delete, because unlike the other 13 pairs the command
# side carries load-bearing behavior a skill cannot replicate: the `!`nx rdr
# preamble <name>`` bash injection and $ARGUMENTS parsing that make
# `/rdr-gate <id>` a real, argument-taking slash command. Deleting the
# command would break that; deleting the skill would lose the checklist
# content test_rdr_audit_skill.py pins across both files. nexus-cnzei.6 renamed the skill side instead — same strategy
# nexus-cnzei.4 already used for the "debug"/"research"/"review" verb
# skills: conexus/skills/rdr-gate/ -> rdr-gate-checklist/ (and
# rdr-fix-checklist, rdr-accept-checklist, rdr-audit-checklist), each
# skill's own frontmatter `name:` and registry.yaml's `rdr_skills:` key
# updated to match, `slash_command:`/`command_file:` left naming the
# unrenamed `/rdr-gate` etc. command. No collision remains, so this
# allowlist is empty; it stays as the mechanism for any future one.
_KNOWN_COMMAND_SKILL_COLLISIONS: frozenset[str] = frozenset()

# nexus-cnzei.4 fix round: identifiers with NO surviving file anywhere in the
# plugin under that exact name — not "a command was deleted" (most deleted
# commands, e.g. architecture/deep-analysis/rdr-create, have a same-named
# skill that IS the survivor, so the bare name stays live and must NOT be
# flagged) but "this exact string names nothing real any more." Verified
# empirically: an earlier, broader draft of this set that also listed every
# deleted command name (including "upgrade" and "rdr-create") false-positived
# on the since-deleted era-hop rehearsal and shakeout scripts' unrelated `nx upgrade` CLI-verb checks and
# 00_debug_load.sh's own (correct, unrelated) skills/rdr-create/SKILL.md
# packaging check. The three verb skills renamed off "debug"/"research"/
# "review" are excluded for the same reason — those remain valid words as
# live command names. See
# TestOneEntryPointPerName.test_retired_names_absent_from_e2e_and_cc_validation_fixtures.
_RETIRED_NAMES = frozenset({
    # Deleted agents (RDR-080 stubs) — no skill, no command, nothing survives.
    "knowledge-tidier", "plan-auditor", "plan-enricher",
    # Deleted command, merged into the differently-named knowledge-tidying
    # skill — "knowledge-tidy" itself (unlike architecture, rdr-create, etc.)
    # has no surviving same-named file.
    "knowledge-tidy",
})


class TestOneEntryPointPerName:

    def test_no_undeclared_command_skill_name_collisions(self) -> None:
        skill_names = {p.parent.name for p in skill_skill_mds()}
        command_names = {p.stem for p in command_files()}
        collisions = skill_names & command_names
        undeclared = collisions - _KNOWN_COMMAND_SKILL_COLLISIONS
        assert not undeclared, (
            f"commands/{{name}}.md and skills/{{name}}/SKILL.md share a name for "
            f"{sorted(undeclared)} — one shadows the other in the Skill-tool "
            "listing. Delete the redundant command (default), rename the skill, "
            "or add the name to _KNOWN_COMMAND_SKILL_COLLISIONS with a reason "
            "if it is a deliberately-kept dual surface."
        )
        resolved = _KNOWN_COMMAND_SKILL_COLLISIONS - collisions
        assert not resolved, (
            f"_KNOWN_COMMAND_SKILL_COLLISIONS names {sorted(resolved)} but no "
            "collision exists any more for them — shrink the allowlist to "
            "match, so it can't silently paper over a future re-collision "
            "under the same name."
        )

    def test_every_conexus_slash_reference_resolves(self) -> None:
        """Every /conexus:<name> reference inside the live routing surface
        (agents, skills, commands) must name a real entry point — a skill
        directory, a command file, or a registered standalone/utility/rdr
        skill. Catches a rename that updates most call sites but misses one,
        and a command that points at a skill deleted out from under it."""
        skill_names = {p.parent.name for p in skill_skill_mds()}
        command_names = {p.stem for p in command_files()}
        registered_names = (
            skill_names | command_names
            | set(REGISTRY.get("standalone_skills", {}))
            | set(REGISTRY.get("utility_commands", {}))
            | set(REGISTRY.get("rdr_skills", {}))
        )
        ref_re = re.compile(r"/conexus:([a-zA-Z0-9][a-zA-Z0-9-]*)")
        offenders: list[str] = []
        for md_file in ALL_MD_FILES:
            for name in ref_re.findall(md_file.read_text()):
                if name not in registered_names:
                    offenders.append(f"{md_file.relative_to(PLUGIN_DIR)}: /conexus:{name}")
        assert not offenders, (
            "References to a /conexus:<name> that resolves to no skill, "
            "command, or registered entry point:\n" + "\n".join(sorted(offenders))
        )

    def test_retired_names_absent_from_e2e_and_cc_validation_fixtures(self) -> None:
        """nexus-cnzei.4 fix round: tests/e2e/scenarios/00_debug_load.sh:188,190
        hardcoded a positive-presence check for the deleted plan-auditor and
        knowledge-tidier agents — missed by the collision/reference sweeps
        above because they only scan ALL_MD_FILES (agents/skills/commands
        markdown), never shell fixtures. `_RETIRED_NAMES` is a closed set of
        the exact identifiers this bead deleted outright (agent names whose
        .md file is gone, command basenames whose .md file is gone) — NOT the
        three verb skills renamed off "debug"/"research"/"review", since
        those remain valid words as commands and would flood this check with
        unrelated prose matches. Checked as a quoted string literal
        (`"name"` or `'name'`), the shape every hit found so far actually
        used, to avoid flagging a retired name inside unrelated prose."""
        offenders: list[str] = []
        shell_files = sorted((REPO_ROOT / "tests" / "e2e").rglob("*.sh"))
        cc_validation_dir = REPO_ROOT / "tests" / "cc-validation"
        if cc_validation_dir.is_dir():
            shell_files += sorted(p for p in cc_validation_dir.rglob("*") if p.is_file())
        for path in shell_files:
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            for name in _RETIRED_NAMES:
                if f'"{name}"' in text or f"'{name}'" in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {name!r}")
        assert not offenders, (
            "Retired agent/command name(s) referenced in an E2E or "
            "cc-validation fixture (the file, not this test, needs updating "
            "to a still-live name):\n" + "\n".join(sorted(offenders))
        )


# ── Bidirectional registry coverage ──────────────────────────────────────────


class TestBidirectionalRegistry:

    def test_every_agent_file_has_registry_entry(self) -> None:
        registered = set(REGISTRY_AGENTS.keys())
        for af in agent_files():
            assert af.stem in registered, f"agents/{af.name} not in registry.yaml"

    def test_every_skill_dir_has_registry_entry(self) -> None:
        agent_skills = {m["skill"] for m in REGISTRY_AGENTS.values() if m.get("skill")}
        standalone = set(REGISTRY.get("standalone_skills", {}).keys())
        rdr = set(REGISTRY.get("rdr_skills", {}).keys())
        all_registered = agent_skills | standalone | rdr
        for sm in skill_skill_mds():
            assert sm.parent.name in all_registered, \
                f"skills/{sm.parent.name} not registered in registry.yaml"

    def test_every_command_file_has_registry_entry(self) -> None:
        registered: set[str] = set()
        for meta in REGISTRY_AGENTS.values():
            if sc := meta.get("slash_command"):
                registered.add(sc.lstrip("/"))
        for meta in REGISTRY.get("rdr_skills", {}).values():
            if sc := meta.get("slash_command"):
                registered.add(sc.lstrip("/"))
        for name in REGISTRY.get("standalone_skills", {}):
            registered.add(name)
        for name in REGISTRY.get("utility_commands", {}):
            registered.add(name)
        for cf in command_files():
            assert cf.stem in registered, f"commands/{cf.name} not in registry.yaml"


# ── RDR-080 stub agent content guards ────────────────────────────────────────
#
# nexus-cnzei.4 (S2): the RDR-080 stub agents (knowledge-tidier, plan-auditor,
# plan-enricher) were themselves deleted outright — they were 40-line files
# whose only content was "call this MCP tool instead", so keeping the stub
# was pure indirection. TestRdr080StubAgents, _STUB_AGENTS, and
# _DELETED_AGENTS (which existed only to parametrize that class) were removed
# with them. The MCP-tool redirect these stubs pointed at is now documented
# directly on the pointer skills (enrich-plan, knowledge-tidying,
# plan-validation) and the commands that inject context for them.


def test_changelog_has_a_section_for_pyprojects_version() -> None:
    """nexus-5crgr: six of the seven version surfaces had parity tests; the
    CHANGELOG had none, and release.yml silently fell back to a stub
    release body when the section was missing (that fallback is now a hard
    failure — this is the pre-tag half, which catches the omission on the
    release branch before the PR merges, where the fix is cheapest)."""
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text()
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=\n## \[|\Z)",
                  changelog, re.DOTALL | re.MULTILINE)
    assert m, (
        f"CHANGELOG.md has no '## [{version}]' section for pyproject.toml's "
        "current version — the release would have published a stub body "
        "pre-nexus-5crgr, and now fails at the tag instead; add the section "
        "in the same commit that bumps the version"
    )
    assert m.group(1).strip(), (
        f"CHANGELOG.md's '## [{version}]' section is empty — a heading with "
        "no content is the same stub-body problem one level down"
    )
