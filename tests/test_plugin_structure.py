# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import yaml
import pytest

REPO_ROOT = Path(__file__).parent.parent
PLUGIN_DIR = REPO_ROOT / "nx"
AGENTS_DIR = PLUGIN_DIR / "agents"
SHARED_DIR = AGENTS_DIR / "_shared"
SKILLS_DIR = PLUGIN_DIR / "skills"
COMMANDS_DIR = PLUGIN_DIR / "commands"
REGISTRY_PATH = PLUGIN_DIR / "registry.yaml"
HOOKS_PATH = PLUGIN_DIR / "hooks" / "hooks.json"
MARKETPLACE_PATH = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

REGISTRY = yaml.safe_load(REGISTRY_PATH.read_text())
REGISTRY_AGENTS: dict = REGISTRY.get("agents", {})
AGENT_NAMES = list(REGISTRY_AGENTS.keys())
AGENT_NAMES_AND_META = list(REGISTRY_AGENTS.items())

_STANDALONE_SKILLS = {
    "cli-controller", "nexus",
    "brainstorming-gate", "orchestration",
    "using-nx-skills", "writing-nx-skills",
    "rdr-list", "rdr-show", "rdr-create",
    "sequential-thinking",
    "serena-code-nav", "catalog",
    "receiving-review", "git-worktrees", "finishing-branch",
    # RDR-080 P3: pointer skills — delegate directly to MCP tools, no relay structure needed
    "query", "enrich-plan", "knowledge-tidying", "plan-validation",
    # RDR-078 verb skills — dispatch plan_match + plan_run directly, no agent relay
    "research", "review", "analyze", "debug", "document",
    "plan-author", "plan-inspect", "plan-promote", "plan-first",
}


def agent_files() -> list[Path]:
    return sorted(p for p in AGENTS_DIR.glob("*.md"))

def skill_skill_mds() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))

def command_files() -> list[Path]:
    return sorted(COMMANDS_DIR.glob("*.md"))

def _agent_params():
    return [pytest.param(p, id=p.name) for p in agent_files()]

def _skill_params(*, exclude_standalone: bool = False):
    return [pytest.param(p, id=p.parent.name) for p in skill_skill_mds()
            if not exclude_standalone or p.parent.name not in _STANDALONE_SKILLS]

def _command_params():
    return [pytest.param(p, id=p.name) for p in command_files()]

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
        if "_shared" in md_file.parts:
            continue
        text = md_file.read_text()
        for match in re.finditer(r"\[([^\]]*)\]\(([^)]*_shared/[^)]*)\)", text):
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


def _hook_script_refs() -> list[tuple[str, str]]:
    data = json.loads(HOOKS_PATH.read_text())
    events = data.get("hooks", data)
    results = []
    for event, entries in events.items():
        for entry in entries:
            sub_hooks = entry.get("hooks", [entry]) if isinstance(entry, dict) else []
            for sub in sub_hooks:
                cmd = sub.get("command", "") if isinstance(sub, dict) else ""
                for m in re.finditer(r"\$CLAUDE_PLUGIN_ROOT/([^\s'\"]+)", cmd):
                    results.append((event, m.group(1)))
    return results


class TestRegistryIntegrity:

    def test_registry_exists_and_parses(self) -> None:
        assert REGISTRY_PATH.exists()
        assert "agents" in REGISTRY and "version" in REGISTRY

    @pytest.mark.parametrize("agent_name", [
        pytest.param(n, id=n) for n in AGENT_NAMES
    ])
    def test_agent_has_md_file(self, agent_name: str) -> None:
        assert (AGENTS_DIR / f"{agent_name}.md").exists()

    @pytest.mark.parametrize("agent_name,agent_meta", [
        pytest.param(n, m, id=n) for n, m in AGENT_NAMES_AND_META if m.get("skill")
    ])
    def test_agent_skill_directory_exists(self, agent_name: str, agent_meta: dict) -> None:
        assert (SKILLS_DIR / agent_meta["skill"] / "SKILL.md").exists()

    @pytest.mark.parametrize("pipeline_name,pipeline_meta", [
        pytest.param(n, m, id=n) for n, m in REGISTRY.get("pipelines", {}).items()
    ])
    def test_pipeline_agents_exist(self, pipeline_name: str, pipeline_meta: dict) -> None:
        known = set(REGISTRY_AGENTS.keys())
        for step in pipeline_meta.get("sequence", []):
            assert step in known, f"Pipeline '{pipeline_name}' references unknown agent '{step}'"

    @pytest.mark.parametrize("agent_name,agent_meta", [
        pytest.param(n, m, id=n) for n, m in AGENT_NAMES_AND_META
    ])
    def test_predecessors_and_successors_exist(self, agent_name: str, agent_meta: dict) -> None:
        known = set(REGISTRY_AGENTS.keys())
        for rel in ("predecessors", "successors"):
            for ref in agent_meta.get(rel, []):
                assert ref in known, f"Agent '{agent_name}' {rel} entry '{ref}' not in agents"

    def test_model_summary_matches_agents(self) -> None:
        known = set(REGISTRY_AGENTS.keys())
        for model, listed in REGISTRY.get("model_summary", {}).items():
            for name in listed:
                assert name in known, f"model_summary/{model} references unknown agent '{name}'"


AGENT_REQUIRED_SECTIONS = [
    "## Relay Reception",
    "## Context Protocol",
    "### Agent-Specific PRODUCE",
    "RECOVER protocol",
]
AGENT_FRONTMATTER_FIELDS = ("name", "version", "description", "model", "color")


class TestAgentStructure:

    @pytest.mark.parametrize("agent_path", _agent_params())
    def test_required_sections_and_frontmatter(self, agent_path: Path) -> None:
        text = agent_path.read_text()
        for section in AGENT_REQUIRED_SECTIONS:
            assert section in text, f"{agent_path.name}: missing '{section}'"
        assert "CONTEXT_PROTOCOL.md" in text, f"{agent_path.name}: missing CONTEXT_PROTOCOL.md reference"
        fm = _extract_frontmatter(text)
        assert fm, f"{agent_path.name}: no YAML frontmatter found"
        for field in AGENT_FRONTMATTER_FIELDS:
            assert field in fm, f"{agent_path.name}: frontmatter missing '{field}'"



class TestPlannerReviewGates:

    @pytest.mark.parametrize("check", [
        pytest.param("section", id="has-review-gates-section"),
        pytest.param("code-review-expert", id="gates-mention-code-review-expert"),
        pytest.param("execution", id="execution-instructions-include-review"),
    ])
    def test_planner_review_gates(self, check: str) -> None:
        text = (AGENTS_DIR / "strategic-planner.md").read_text()
        if check == "section":
            assert "### Review Gates" in text
        elif check == "code-review-expert":
            start = text.index("### Review Gates")
            end = text.index("###", start + 1)
            assert "code-review-expert" in text[start:end]
        elif check == "execution":
            start = text.index("**Execution Instructions**")
            end = text.index("**Parallelization", start)
            section = text[start:end]
            assert "Code review" in section or "code-review" in section



class TestRecoverProtocol:

    @pytest.mark.parametrize("agent_path", _agent_params())
    @pytest.mark.parametrize("check", [
        pytest.param("six_steps", id="six-steps"),
        pytest.param("t1_scratch", id="t1-scratch"),
        pytest.param("memory_search", id="memory-search"),
    ])
    def test_recover_block(self, agent_path: Path, check: str) -> None:
        text = agent_path.read_text()
        block = _extract_recover_block(text)
        if check == "memory_search" and not block:
            pytest.skip(f"{agent_path.name}: no RECOVER block")
            return
        assert block, f"{agent_path.name}: no 'If validation fails' block found"
        if check == "six_steps":
            assert "6. Proceed with available context" in block, \
                f"{agent_path.name}: RECOVER block missing step 6"
        elif check == "t1_scratch":
            assert "nx scratch search" in block or 'action="search"' in block or "scratch" in block.lower(), \
                f"{agent_path.name}: RECOVER block missing T1 scratch search step"
        elif check == "memory_search":
            has_cli = "nx memory search" in block
            has_mcp = "memory_search" in block
            has_stale = "nx memory get --project" in block
            assert has_cli or has_mcp or not has_stale, \
                f"{agent_path.name}: RECOVER uses stale 'nx memory get' instead of 'memory_search'"



class TestCliSyntax:

    @pytest.mark.parametrize("md_path", [
        pytest.param(p, id=str(p.relative_to(PLUGIN_DIR))) for p in ALL_MD_FILES
    ])
    @pytest.mark.parametrize("pattern,msg", [
        pytest.param("pm::", "stale 'pm::' notation", id="no-pm-colon"),
        pytest.param(None, "stale 'nx health' command", id="no-nx-health"),
    ])
    def test_no_stale_patterns(self, md_path: Path, pattern: str | None, msg: str) -> None:
        text = md_path.read_text()
        if pattern == "pm::":
            assert "pm::" not in text, f"{md_path}: {msg}"
        else:
            assert not re.search(r"`nx health`|nx health\b", text), f"{md_path}: {msg}"

    @pytest.mark.parametrize("agent_path", _agent_params())
    def test_nx_store_put_has_pipe_source(self, agent_path: Path) -> None:
        text = agent_path.read_text()
        lines_with_put = [
            (i + 1, line)
            for i, line in enumerate(text.splitlines())
            if "nx store put -" in line
        ]
        for lineno, line in lines_with_put:
            stripped = line.strip()
            has_pipe = "|" in stripped and stripped.index("|") < stripped.index("nx store put -")
            is_comment = re.match(r"^\s*[#\-*]", line)
            assert has_pipe or is_comment, \
                f"{agent_path.name}:{lineno}: 'nx store put -' missing pipe source"



class TestSkillStructure:

    REQUIRED_SECTIONS = [
        ("## Relay Template", "## Agent Invocation"),
        "## Success Criteria",
    ]

    @pytest.mark.parametrize("skill_path", _skill_params(exclude_standalone=True))
    def test_required_sections_and_produce(self, skill_path: Path) -> None:
        text = skill_path.read_text()
        for section in self.REQUIRED_SECTIONS:
            if isinstance(section, tuple):
                assert any(alt in text for alt in section), \
                    f"{skill_path.parent.name}/SKILL.md: missing one of {section}"
            else:
                assert section in text, \
                    f"{skill_path.parent.name}/SKILL.md: missing '{section}'"
        assert "Agent-Specific PRODUCE" in text, \
            f"{skill_path.parent.name}/SKILL.md: missing 'Agent-Specific PRODUCE'"
        assert "scratch" in text.lower(), \
            f"{skill_path.parent.name}/SKILL.md: no mention of T1 scratch"

    @pytest.mark.parametrize("skill_path", _skill_params())
    def test_relay_template_has_required_rows(self, skill_path: Path) -> None:
        text = skill_path.read_text()
        if "## Relay Template" not in text:
            if "RELAY_TEMPLATE.md" in text:
                return
            pytest.skip("No relay template in this skill")
        relay_section = text.split("## Relay Template")[1]
        for row in ("nx store:", "nx memory:", "Files:"):
            assert row in relay_section, \
                f"{skill_path.parent.name}/SKILL.md relay template: missing '{row}'"



class TestSkillDescriptionCSO:

    BAD_KEYWORDS = ["Triggers:", "user says", "workflow", "process:"]

    @pytest.mark.parametrize("skill_path", _skill_params())
    def test_frontmatter_valid(self, skill_path: Path) -> None:
        text = skill_path.read_text()
        fm_match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        assert fm_match, f"{skill_path.parent.name}/SKILL.md: no YAML frontmatter"
        raw = fm_match.group(1)
        fm = yaml.safe_load(raw)
        # Only standard fields allowed
        extra = set(fm.keys()) - {"name", "description", "effort"}
        assert not extra, f"{skill_path.parent.name}/SKILL.md: non-standard fields {extra}"
        # No YAML comments
        comment_lines = [l for l in raw.splitlines() if l.strip().startswith("#")]
        assert not comment_lines, \
            f"{skill_path.parent.name}/SKILL.md: YAML comments in frontmatter: {comment_lines}"
        # Description starts with 'Use when'
        desc = fm.get("description", "")
        assert desc.lower().startswith("use when"), \
            f"{skill_path.parent.name}/SKILL.md: description must start with 'Use when'. Got: {desc[:80]!r}"
        # No workflow keywords
        for kw in self.BAD_KEYWORDS:
            assert kw not in desc, \
                f"{skill_path.parent.name}/SKILL.md: description contains workflow keyword {kw!r}"



class TestCommandStructure:

    @pytest.mark.parametrize("cmd_path", _command_params())
    def test_bash_block_syntax(self, cmd_path: Path) -> None:
        if shutil.which("bash") is None:
            pytest.skip("bash not available")
        text = cmd_path.read_text()
        match = re.search(r"^!\{$(.*?)^}", text, re.MULTILINE | re.DOTALL)
        if not match:
            pytest.skip(f"{cmd_path.name}: no !{{}} bash block found")
        result = subprocess.run(["bash", "-n"], input=match.group(1), capture_output=True, text=True)
        assert result.returncode == 0, f"{cmd_path.name}: bash syntax error:\n{result.stderr}"

    @pytest.mark.parametrize("cmd_path", _command_params())
    def test_no_unescaped_glob_in_grep(self, cmd_path: Path) -> None:
        bad = re.findall(r'grep\s+["\']?\*\*["\']?', cmd_path.read_text())
        assert not bad, f"{cmd_path.name}: unescaped '**' in grep pattern: {bad}"

    @pytest.mark.parametrize("cmd_path", _command_params())
    def test_nx_commands_guarded(self, cmd_path: Path) -> None:
        text = cmd_path.read_text()
        match = re.search(r"^!\{$(.*?)^}", text, re.MULTILINE | re.DOTALL)
        if not match:
            pytest.skip(f"{cmd_path.name}: no !{{}} bash block found")
        nx_calls = [
            line.strip() for line in match.group(1).splitlines()
            if re.match(r"\s+nx\s+", line) and "2>/dev/null" not in line
            and "command -v nx" not in line
        ]
        assert len(nx_calls) < 5, \
            f"{cmd_path.name}: {len(nx_calls)} unguarded 'nx' calls in !{{}} block"



class TestCrossReferenceIntegrity:

    @pytest.mark.parametrize("agent_path", _agent_params())
    def test_relay_to_references_exist(self, agent_path: Path) -> None:
        known_agents = {p.stem for p in agent_files()}
        for ref in re.findall(r"relay to `([a-z][a-z0-9-]*)`", agent_path.read_text(), re.IGNORECASE):
            assert ref in known_agents, f"{agent_path.name}: references unknown agent '{ref}'"

    @pytest.mark.parametrize("agent_path", _agent_params())
    def test_shared_context_protocol_reference_valid(self, agent_path: Path) -> None:
        if "CONTEXT_PROTOCOL.md" not in agent_path.read_text():
            pytest.skip("no CONTEXT_PROTOCOL.md reference")
        assert (SHARED_DIR / "CONTEXT_PROTOCOL.md").exists()


class TestHooks:

    def test_hooks_json_exists_and_matchers(self) -> None:
        assert HOOKS_PATH.exists()
        hooks = json.loads(HOOKS_PATH.read_text())
        for entry in hooks.get("PostToolUse", []):
            has_matcher = "matcher" in entry
            has_filter = "grep" in entry.get("command", "") or "bd create" in entry.get("command", "")
            assert has_matcher or has_filter, \
                f"PostToolUse hook without matcher: {entry.get('command', '')[:80]}"

    @pytest.mark.parametrize("event,rel_path", [
        pytest.param(ev, rp, id=f"{ev}:{rp}") for ev, rp in _hook_script_refs()
    ])
    def test_hook_script_exists(self, event: str, rel_path: str) -> None:
        assert (PLUGIN_DIR / rel_path).exists(), f"hooks.json [{event}] references missing: {rel_path}"

    @pytest.mark.parametrize("script_name", [
        "session_start_hook.py",
        "rdr_hook.py",
        "stop_failure_hook.py",
        "read_verification_config.py",
        "t2_prefix_scan.py",
    ])
    def test_python_hook_has_future_annotations_and_version_guard(self, script_name: str) -> None:
        """Every Python hook script must (a) defer annotation evaluation via
        ``from __future__ import annotations`` so PEP 604 unions parse on
        Python ≥3.7, and (b) fail fast with a clear error if running under
        Python <3.12 (the floor conexus targets).

        Without (a), `int | None` annotations crash the parser on Python
        3.9 (still default on macOS Sonoma without homebrew Python). Without
        (b), the script may run partially under an unsupported interpreter
        and produce confusing failures further down. Both are required so
        a missing-system-Python case surfaces as a single actionable error
        rather than a SyntaxError or AttributeError chain.
        """
        path = PLUGIN_DIR / "hooks" / "scripts" / script_name
        assert path.exists(), f"missing hook script: {path}"
        head = "\n".join(path.read_text().splitlines()[:30])
        assert "from __future__ import annotations" in head, (
            f"{script_name}: missing 'from __future__ import annotations' in first 30 lines"
        )
        assert "sys.version_info < (3, 12)" in head, (
            f"{script_name}: missing 'if sys.version_info < (3, 12)' guard in first 30 lines"
        )
        assert "Python 3.12+" in head, (
            f"{script_name}: version guard does not include the 'Python 3.12+' user-facing message"
        )

    def test_python_hook_runner_helper_present_and_executable(self) -> None:
        """The hooks.json command lines route Python hook invocations through
        ``_run_python_hook.sh`` so a system whose default ``python3`` is older
        than 3.12 still picks up a usable Python (probing python3.13 then
        python3.12 explicitly, falling back to plain ``python3``).

        Without this layer, hook invocations resolved ``python3`` directly
        and broke under macOS framework Python installs (3.10) that win
        PATH precedence over Homebrew's 3.13. The hook script's runtime
        version guard fires as a defence-in-depth backstop.
        """
        helper = PLUGIN_DIR / "hooks" / "scripts" / "_run_python_hook.sh"
        assert helper.exists(), f"missing Python hook runner: {helper}"
        # Executable bit must be set; the json command invokes it directly.
        assert os.access(helper, os.X_OK), f"helper is not executable: {helper}"
        text = helper.read_text()
        # Assert the version preference is encoded — anyone removing the
        # python3.13/python3.12 lookups falls back to the broken behaviour
        # we just fixed.
        assert "python3.13" in text and "python3.12" in text, (
            f"helper should probe explicit python3.13 / python3.12; got:\n{text}"
        )
        # Final fallback to plain python3 must remain so the hook script's
        # own version guard still gets a chance to surface a friendly error.
        assert 'exec python3 "$@"' in text, (
            f"helper missing python3 fallback (needed for the runtime guard's clean error path):\n{text}"
        )

    def test_python_hooks_use_runner_helper(self) -> None:
        """Every Python hook script registered in hooks.json must be invoked
        via ``_run_python_hook.sh`` rather than a bare ``python3``. Catches
        the regression where someone adds a new Python hook and reaches for
        the old ``python3 path/to/hook.py`` pattern out of habit.
        """
        data = json.loads(HOOKS_PATH.read_text())
        events = data.get("hooks", data)
        offenders: list[tuple[str, str]] = []
        for event, entries in events.items():
            for entry in entries:
                for sub in entry.get("hooks", []):
                    cmd = sub.get("command", "")
                    if cmd.endswith(".py") or " .py " in cmd or cmd.rstrip().endswith(".py"):
                        # Cheap heuristic: command ends in a .py invocation
                        if "_run_python_hook.sh" not in cmd:
                            offenders.append((event, cmd))
                    # Stricter check: any 'python3 …/hook.py' literal is wrong
                    if re.search(r"\bpython3 \S+\.py\b", cmd):
                        offenders.append((event, cmd))
        assert not offenders, (
            "hooks.json invokes Python hooks directly via 'python3'; route through "
            f"_run_python_hook.sh instead. Offenders:\n" +
            "\n".join(f"  [{e}] {c}" for e, c in offenders)
        )

    def test_plugin_json_declares_python_engine(self) -> None:
        """nx/.claude-plugin/plugin.json must declare engines.python so the
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

    @pytest.mark.parametrize("filename", EXPECTED_SHARED_FILES)
    def test_shared_file_exists_and_non_empty(self, filename: str) -> None:
        path = SHARED_DIR / filename
        assert path.exists(), f"_shared/{filename} missing"
        assert len(path.read_text()) > 100, f"_shared/{filename} nearly empty"

    def test_no_unregistered_shared_files(self) -> None:
        orphans = {p.name for p in SHARED_DIR.glob("*.md")} - set(EXPECTED_SHARED_FILES)
        assert not orphans, f"Unexpected files in _shared/: {orphans}"


class TestSharedRelativePaths:

    @pytest.mark.parametrize("source_file,raw_path", [
        pytest.param(src, rp, id=f"{src.relative_to(PLUGIN_DIR)}->{rp}")
        for src, rp in _collect_shared_links()
    ])
    def test_shared_link_resolves(self, source_file: Path, raw_path: str) -> None:
        resolved = (source_file.parent / raw_path.split("#")[0]).resolve()
        assert resolved.exists(), \
            f"{source_file.relative_to(PLUGIN_DIR)}: link {raw_path!r} -> {resolved} missing"


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


# ── Plugin root manifest ─────────────────────────────────────────────────────

REQUIRED_ROOT_FILES = ["registry.yaml", "README.md", "CHANGELOG.md", "hooks/hooks.json"]
REQUIRED_ROOT_DIRS = [
    "agents", "agents/_shared", "skills", "commands",
    "hooks/scripts", "resources/rdr", "resources/rdr/post-mortem",
]


class TestPluginRootManifest:

    @pytest.mark.parametrize("rel_path", REQUIRED_ROOT_FILES)
    def test_required_root_file_exists(self, rel_path: str) -> None:
        full = PLUGIN_DIR / rel_path
        assert full.exists(), f"Missing: {rel_path}"
        assert full.stat().st_size > 0, f"Empty: {rel_path}"

    @pytest.mark.parametrize("rel_dir", REQUIRED_ROOT_DIRS)
    def test_required_root_dir_exists(self, rel_dir: str) -> None:
        full = PLUGIN_DIR / rel_dir
        assert full.is_dir(), f"Missing dir: {rel_dir}"
        assert any(full.iterdir()), f"Empty dir: {rel_dir}"


# ── $CLAUDE_PLUGIN_ROOT references ───────────────────────────────────────────


class TestPluginRootRefs:

    @pytest.mark.parametrize("source,rel_path", [
        pytest.param(src, rp, id=f"{src}->{rp}") for src, rp in _collect_plugin_root_refs()
    ])
    def test_plugin_root_ref_resolves(self, source: str, rel_path: str) -> None:
        assert (PLUGIN_DIR / rel_path).exists(), \
            f"{source}: $CLAUDE_PLUGIN_ROOT/{rel_path} missing"


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

# Agents deleted by RDR-080 P3/P4. Stub files must not reference these names
# or an agent reading the stub would try to dispatch a non-existent agent.
_DELETED_AGENTS = frozenset({
    "query-planner",
    "analytical-operator",
    "pdf-chromadb-processor",
})

# Stub agent files that redirect to MCP tools (RDR-080 SC-5).
_STUB_AGENTS = ("knowledge-tidier", "plan-auditor", "plan-enricher")


class TestRdr080StubAgents:
    """Stub agents must redirect to MCP tools and not reference deleted agents."""

    @pytest.mark.parametrize("agent_name", _STUB_AGENTS)
    def test_stub_does_not_reference_deleted_agents(self, agent_name: str) -> None:
        stub = PLUGIN_DIR / "agents" / f"{agent_name}.md"
        assert stub.exists(), f"Expected stub file: {stub}"
        content = stub.read_text()
        for deleted in _DELETED_AGENTS:
            assert deleted not in content, (
                f"nx/agents/{agent_name}.md references deleted agent '{deleted}'. "
                "Stubs must redirect to MCP tools only (RDR-080 SC-4)."
            )

    @pytest.mark.parametrize("agent_name", _STUB_AGENTS)
    def test_stub_references_mcp_tool(self, agent_name: str) -> None:
        stub = PLUGIN_DIR / "agents" / f"{agent_name}.md"
        content = stub.read_text()
        assert "mcp__plugin_nx_nexus__" in content, (
            f"nx/agents/{agent_name}.md must reference an MCP tool "
            "(mcp__plugin_nx_nexus__*) as its redirect target (RDR-080)."
        )
