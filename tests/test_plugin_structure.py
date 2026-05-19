# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plugin structure invariants.

Validates that the ``nx/`` plugin directory has the expected shape:
registry coverage, agent/skill/command structure, hook wiring, shared
resources, cross-references, version sync.

Originally parametrized per-file (679 tests). Collapsed on the
test_suite_reduction sweep to loop-and-collect — each test enumerates
every offender in its failure message, so per-item visibility is
preserved in the assertion output rather than in pytest's collection
IDs.
"""
from __future__ import annotations

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

_STANDALONE_SKILLS = {
    "cli-controller", "nexus",
    "brainstorming-gate", "orchestration",
    "using-nx-skills", "writing-nx-skills",
    "rdr-list", "rdr-show", "rdr-create",
    "sequential-thinking",
    "serena-code-nav", "catalog",
    "receiving-review", "git-worktrees", "finishing-branch",
    "query", "enrich-plan", "knowledge-tidying", "plan-validation",
    "research", "review", "analyze", "debug", "document",
    "plan-author", "plan-inspect", "plan-promote", "plan-first",
    "tuplespace-tasks", "tuplespace-mailbox", "tuplespace-lock",
    "tuplespace-events", "tuplespace-barriers",
    "tuplespace-list", "tuplespace-stats",
    "phase-review-gate",
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


# ── Registry ─────────────────────────────────────────────────────────────────

def test_registry_exists_and_parses() -> None:
    assert REGISTRY_PATH.exists()
    assert "agents" in REGISTRY and "version" in REGISTRY


def test_every_registered_agent_has_md_file() -> None:
    missing = [n for n in REGISTRY_AGENTS if not (AGENTS_DIR / f"{n}.md").exists()]
    assert not missing, f"registered agents with no .md: {missing}"


def test_every_agent_with_skill_has_skill_directory() -> None:
    missing = [
        f"{n}->{m['skill']}"
        for n, m in REGISTRY_AGENTS.items()
        if m.get("skill") and not (SKILLS_DIR / m["skill"] / "SKILL.md").exists()
    ]
    assert not missing, f"agents whose declared skill dir is missing: {missing}"


def test_pipelines_reference_known_agents() -> None:
    known = set(REGISTRY_AGENTS)
    bad = []
    for pname, pmeta in REGISTRY.get("pipelines", {}).items():
        for step in pmeta.get("sequence", []):
            if step not in known:
                bad.append(f"{pname} -> {step}")
    assert not bad, f"pipelines reference unknown agents: {bad}"


def test_predecessor_successor_references_resolve() -> None:
    known = set(REGISTRY_AGENTS)
    bad: list[str] = []
    for n, m in REGISTRY_AGENTS.items():
        for rel in ("predecessors", "successors"):
            for ref in m.get(rel, []):
                if ref not in known:
                    bad.append(f"{n}.{rel} -> {ref}")
    assert not bad, f"unknown predecessor/successor refs: {bad}"


def test_model_summary_matches_agents() -> None:
    known = set(REGISTRY_AGENTS)
    bad = []
    for model, listed in REGISTRY.get("model_summary", {}).items():
        for name in listed:
            if name not in known:
                bad.append(f"model_summary/{model} -> {name}")
    assert not bad, f"model_summary references unknown agents: {bad}"


# ── Agent structure ──────────────────────────────────────────────────────────

AGENT_REQUIRED_SECTIONS = [
    "## Relay Reception",
    "## Context Protocol",
    "### Agent-Specific PRODUCE",
    "RECOVER protocol",
]
AGENT_FRONTMATTER_FIELDS = ("name", "version", "description", "model", "color")


def test_every_agent_has_required_sections_and_frontmatter() -> None:
    failures: list[str] = []
    for ap in agent_files():
        text = ap.read_text()
        for section in AGENT_REQUIRED_SECTIONS:
            if section not in text:
                failures.append(f"{ap.name}: missing '{section}'")
        if "CONTEXT_PROTOCOL.md" not in text:
            failures.append(f"{ap.name}: missing CONTEXT_PROTOCOL.md reference")
        fm = _extract_frontmatter(text)
        if not fm:
            failures.append(f"{ap.name}: no YAML frontmatter found")
            continue
        for field in AGENT_FRONTMATTER_FIELDS:
            if field not in fm:
                failures.append(f"{ap.name}: frontmatter missing '{field}'")
    assert not failures, "\n".join(failures)


def test_planner_review_gates() -> None:
    text = (AGENTS_DIR / "strategic-planner.md").read_text()
    assert "### Review Gates" in text, "strategic-planner.md missing '### Review Gates'"
    start = text.index("### Review Gates")
    end = text.index("###", start + 1)
    assert "code-review-expert" in text[start:end], "Review Gates section missing 'code-review-expert'"
    exec_start = text.index("**Execution Instructions**")
    exec_end = text.index("**Parallelization", exec_start)
    section = text[exec_start:exec_end]
    assert "Code review" in section or "code-review" in section, \
        "Execution Instructions missing code-review mention"


def test_every_agent_recover_block_has_required_steps() -> None:
    failures: list[str] = []
    for ap in agent_files():
        text = ap.read_text()
        block = _extract_recover_block(text)
        if not block:
            failures.append(f"{ap.name}: no 'If validation fails' block")
            continue
        if "6. Proceed with available context" not in block:
            failures.append(f"{ap.name}: RECOVER missing step 6")
        if not ("nx scratch search" in block or 'action="search"' in block
                or "scratch" in block.lower()):
            failures.append(f"{ap.name}: RECOVER missing T1 scratch search step")
        has_cli = "nx memory search" in block
        has_mcp = "memory_search" in block
        has_stale = "nx memory get --project" in block
        if not (has_cli or has_mcp or not has_stale):
            failures.append(f"{ap.name}: RECOVER uses stale 'nx memory get'")
    assert not failures, "\n".join(failures)


# ── CLI syntax in markdown ───────────────────────────────────────────────────

def test_no_stale_cli_patterns_in_plugin_md() -> None:
    failures: list[str] = []
    for md in ALL_MD_FILES:
        text = md.read_text()
        rel = md.relative_to(PLUGIN_DIR)
        if "pm::" in text:
            failures.append(f"{rel}: stale 'pm::' notation")
        if re.search(r"`nx health`|nx health\b", text):
            failures.append(f"{rel}: stale 'nx health' command")
    assert not failures, "\n".join(failures)


def test_nx_store_put_has_pipe_source_in_agents() -> None:
    failures: list[str] = []
    for ap in agent_files():
        text = ap.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if "nx store put -" not in line:
                continue
            stripped = line.strip()
            has_pipe = "|" in stripped and stripped.index("|") < stripped.index("nx store put -")
            is_comment = re.match(r"^\s*[#\-*]", line)
            if not (has_pipe or is_comment):
                failures.append(f"{ap.name}:{i}: 'nx store put -' missing pipe source")
    assert not failures, "\n".join(failures)


# ── Skill structure ──────────────────────────────────────────────────────────

SKILL_RELAY_OR_AGENT = ("## Relay Template", "## Agent Invocation")


def test_every_relay_skill_has_required_sections_and_produce() -> None:
    failures: list[str] = []
    for sp in skill_skill_mds():
        if sp.parent.name in _STANDALONE_SKILLS:
            continue
        text = sp.read_text()
        if not any(alt in text for alt in SKILL_RELAY_OR_AGENT):
            failures.append(f"{sp.parent.name}/SKILL.md: missing one of {SKILL_RELAY_OR_AGENT}")
        if "## Success Criteria" not in text:
            failures.append(f"{sp.parent.name}/SKILL.md: missing '## Success Criteria'")
        if "Agent-Specific PRODUCE" not in text:
            failures.append(f"{sp.parent.name}/SKILL.md: missing 'Agent-Specific PRODUCE'")
        if "scratch" not in text.lower():
            failures.append(f"{sp.parent.name}/SKILL.md: no mention of T1 scratch")
    assert not failures, "\n".join(failures)


def test_relay_templates_include_required_rows() -> None:
    failures: list[str] = []
    for sp in skill_skill_mds():
        text = sp.read_text()
        if "## Relay Template" not in text:
            continue
        relay_section = text.split("## Relay Template")[1]
        for row in ("nx store:", "nx memory:", "Files:"):
            if row not in relay_section:
                failures.append(f"{sp.parent.name}/SKILL.md relay template: missing '{row}'")
    assert not failures, "\n".join(failures)


def test_every_skill_frontmatter_valid() -> None:
    bad_keywords = ["Triggers:", "user says", "workflow", "process:"]
    failures: list[str] = []
    for sp in skill_skill_mds():
        text = sp.read_text()
        fm_match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not fm_match:
            failures.append(f"{sp.parent.name}/SKILL.md: no YAML frontmatter")
            continue
        raw = fm_match.group(1)
        fm = yaml.safe_load(raw)
        extra = set(fm.keys()) - {"name", "description", "effort"}
        if extra:
            failures.append(f"{sp.parent.name}/SKILL.md: non-standard fields {extra}")
        comment_lines = [l for l in raw.splitlines() if l.strip().startswith("#")]
        if comment_lines:
            failures.append(f"{sp.parent.name}/SKILL.md: YAML comments in frontmatter: {comment_lines}")
        desc = fm.get("description", "")
        if not desc.lower().startswith("use when"):
            failures.append(f"{sp.parent.name}/SKILL.md: description must start with 'Use when'. Got: {desc[:80]!r}")
        for kw in bad_keywords:
            if kw in desc:
                failures.append(f"{sp.parent.name}/SKILL.md: description contains workflow keyword {kw!r}")
    assert not failures, "\n".join(failures)


# ── Command structure ───────────────────────────────────────────────────────

def test_command_bash_blocks_have_valid_syntax() -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    failures: list[str] = []
    for cp in command_files():
        text = cp.read_text()
        match = re.search(r"^!\{$(.*?)^}", text, re.MULTILINE | re.DOTALL)
        if not match:
            continue
        result = subprocess.run(
            ["bash", "-n"], input=match.group(1), capture_output=True, text=True,
        )
        if result.returncode != 0:
            failures.append(f"{cp.name}: bash syntax error:\n{result.stderr}")
    assert not failures, "\n".join(failures)


def test_command_grep_patterns_escape_glob() -> None:
    failures: list[str] = []
    for cp in command_files():
        bad = re.findall(r'grep\s+["\']?\*\*["\']?', cp.read_text())
        if bad:
            failures.append(f"{cp.name}: unescaped '**' in grep: {bad}")
    assert not failures, "\n".join(failures)


def test_command_nx_calls_are_guarded() -> None:
    failures: list[str] = []
    for cp in command_files():
        text = cp.read_text()
        match = re.search(r"^!\{$(.*?)^}", text, re.MULTILINE | re.DOTALL)
        if not match:
            continue
        nx_calls = [
            line.strip() for line in match.group(1).splitlines()
            if re.match(r"\s+nx\s+", line) and "2>/dev/null" not in line
            and "command -v nx" not in line
        ]
        if len(nx_calls) >= 5:
            failures.append(f"{cp.name}: {len(nx_calls)} unguarded 'nx' calls")
    assert not failures, "\n".join(failures)


# ── Cross-references ────────────────────────────────────────────────────────

def test_agent_relay_references_resolve() -> None:
    known_agents = {p.stem for p in agent_files()}
    failures: list[str] = []
    for ap in agent_files():
        for ref in re.findall(r"relay to `([a-z][a-z0-9-]*)`", ap.read_text(), re.IGNORECASE):
            if ref not in known_agents:
                failures.append(f"{ap.name}: references unknown agent '{ref}'")
    assert not failures, "\n".join(failures)


def test_shared_context_protocol_reference_valid() -> None:
    missing: list[str] = []
    for ap in agent_files():
        if "CONTEXT_PROTOCOL.md" not in ap.read_text():
            continue
        if not (SHARED_DIR / "CONTEXT_PROTOCOL.md").exists():
            missing.append(ap.name)
    assert not missing, f"agents reference CONTEXT_PROTOCOL.md but it's missing for: {missing}"


# ── Hooks ───────────────────────────────────────────────────────────────────

def test_hooks_json_exists_and_has_matchers() -> None:
    assert HOOKS_PATH.exists()
    hooks = json.loads(HOOKS_PATH.read_text())
    bad = []
    for entry in hooks.get("PostToolUse", []):
        has_matcher = "matcher" in entry
        has_filter = "grep" in entry.get("command", "") or "bd create" in entry.get("command", "")
        if not (has_matcher or has_filter):
            bad.append(entry.get("command", "")[:80])
    assert not bad, f"PostToolUse hooks without matcher: {bad}"


def test_every_hook_script_reference_resolves() -> None:
    missing = [
        f"[{event}] {rp}"
        for event, rp in _hook_script_refs()
        if not (PLUGIN_DIR / rp).exists()
    ]
    assert not missing, f"hooks.json references missing files: {missing}"


@pytest.mark.parametrize("script_name", [
    "session_start_hook.py",
    "rdr_hook.py",
    "stop_failure_hook.py",
    "read_verification_config.py",
    "t2_prefix_scan.py",
])
def test_python_hook_has_future_annotations_and_version_guard(script_name: str) -> None:
    path = PLUGIN_DIR / "hooks" / "scripts" / script_name
    assert path.exists(), f"missing hook script: {path}"
    head = "\n".join(path.read_text().splitlines()[:30])
    assert "from __future__ import annotations" in head, (
        f"{script_name}: missing 'from __future__ import annotations' in first 30 lines"
    )
    assert "sys.version_info < (3, 12)" in head, (
        f"{script_name}: missing 'if sys.version_info < (3, 12)' guard"
    )
    assert "Python 3.12+" in head, (
        f"{script_name}: version guard does not include 'Python 3.12+' user-facing message"
    )


def test_python_hook_runner_helper_present_and_executable() -> None:
    helper = PLUGIN_DIR / "hooks" / "scripts" / "_run_python_hook.sh"
    assert helper.exists(), f"missing Python hook runner: {helper}"
    assert os.access(helper, os.X_OK), f"helper is not executable: {helper}"
    text = helper.read_text()
    assert "python3.13" in text and "python3.12" in text, (
        f"helper should probe explicit python3.13 / python3.12; got:\n{text}"
    )
    assert 'exec python3 "$@"' in text, (
        f"helper missing python3 fallback:\n{text}"
    )


def test_python_hooks_use_runner_helper() -> None:
    data = json.loads(HOOKS_PATH.read_text())
    events = data.get("hooks", data)
    offenders: list[tuple[str, str]] = []
    for event, entries in events.items():
        for entry in entries:
            for sub in entry.get("hooks", []):
                cmd = sub.get("command", "")
                if cmd.endswith(".py") or " .py " in cmd or cmd.rstrip().endswith(".py"):
                    if "_run_python_hook.sh" not in cmd:
                        offenders.append((event, cmd))
                if re.search(r"\bpython3 \S+\.py\b", cmd):
                    offenders.append((event, cmd))
    assert not offenders, (
        "hooks.json invokes Python hooks directly via 'python3'; route through "
        f"_run_python_hook.sh instead. Offenders:\n"
        + "\n".join(f"  [{e}] {c}" for e, c in offenders)
    )


def test_plugin_json_declares_python_engine() -> None:
    plugin_json = json.loads((PLUGIN_DIR / ".claude-plugin" / "plugin.json").read_text())
    engines = plugin_json.get("engines") or {}
    py_req = engines.get("python", "")
    assert py_req, "plugin.json missing 'engines.python' field"
    assert "3.12" in py_req, f"engines.python should require >=3.12, got {py_req!r}"


def test_session_end_hook_registered() -> None:
    """Regression guard: SessionEnd must run a nexus cleanup entry point.

    Chroma is spawned with start_new_session=True (detached process group),
    so OS reaping won't collect it; without this hook one chroma child leaks
    per Claude Code session. RDR-094 Phase C swapped the dispatch — either
    'nx hook session-end' or 'nx-session-end-launcher' is acceptable.
    """
    data = json.loads(HOOKS_PATH.read_text())
    session_end = data.get("hooks", data).get("SessionEnd")
    assert session_end, "SessionEnd hook is missing"
    commands = [
        sub.get("command", "")
        for entry in session_end
        for sub in entry.get("hooks", [])
    ]
    valid_entries = ("nx hook session-end", "nx-session-end-launcher")
    assert any(any(v in cmd for v in valid_entries) for cmd in commands), (
        f"SessionEnd does not invoke a nexus cleanup entry ({valid_entries}); "
        f"found: {commands}"
    )


# ── Standalone skill registry ───────────────────────────────────────────────

def test_standalone_skill_directories_exist() -> None:
    failures: list[str] = []
    for skill_name in REGISTRY.get("standalone_skills", {}):
        skill_dir = SKILLS_DIR / skill_name
        if not skill_dir.is_dir():
            failures.append(f"standalone_skills '{skill_name}' has no directory")
            continue
        if not (skill_dir / "SKILL.md").exists():
            failures.append(f"standalone_skills '{skill_name}' has no SKILL.md")
    assert not failures, "\n".join(failures)


# ── Shared resources ────────────────────────────────────────────────────────

EXPECTED_SHARED_FILES = [
    "RELAY_TEMPLATE.md", "CONTEXT_PROTOCOL.md", "ERROR_HANDLING.md",
    "MAINTENANCE.md", "README.md",
]


def test_shared_files_exist_and_non_empty() -> None:
    failures: list[str] = []
    for filename in EXPECTED_SHARED_FILES:
        path = SHARED_DIR / filename
        if not path.exists():
            failures.append(f"_shared/{filename} missing")
            continue
        if len(path.read_text()) <= 100:
            failures.append(f"_shared/{filename} nearly empty")
    assert not failures, "\n".join(failures)


def test_no_unregistered_shared_files() -> None:
    orphans = {p.name for p in SHARED_DIR.glob("*.md")} - set(EXPECTED_SHARED_FILES)
    assert not orphans, f"Unexpected files in _shared/: {orphans}"


def test_shared_links_resolve() -> None:
    failures: list[str] = []
    for src, raw in _collect_shared_links():
        resolved = (src.parent / raw.split("#")[0]).resolve()
        if not resolved.exists():
            failures.append(f"{src.relative_to(PLUGIN_DIR)}: link {raw!r} -> {resolved} missing")
    assert not failures, "\n".join(failures)


# ── Marketplace version sync ────────────────────────────────────────────────

def _pyproject_version() -> str:
    with PYPROJECT_PATH.open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def test_marketplace_json_exists() -> None:
    assert MARKETPLACE_PATH.exists()


def test_marketplace_version_matches_pyproject() -> None:
    pv = _pyproject_version()
    failures = []
    for plugin in json.loads(MARKETPLACE_PATH.read_text()).get("plugins", []):
        if plugin.get("version", "") != pv:
            failures.append(f"{plugin.get('name')} version={plugin.get('version')!r} != pyproject {pv!r}")
    assert not failures, "\n".join(failures)


def test_uv_lock_version_matches_pyproject() -> None:
    pv = _pyproject_version()
    uv_lock = REPO_ROOT / "uv.lock"
    assert uv_lock.exists()
    m = re.search(
        r'\[\[package\]\]\s+name\s*=\s*"conexus"\s+version\s*=\s*"([^"]+)"',
        uv_lock.read_text(),
    )
    assert m is not None, "conexus not found in uv.lock"
    assert m.group(1) == pv, f"uv.lock {m.group(1)!r} != pyproject.toml {pv!r}"


# ── Plugin root manifest ────────────────────────────────────────────────────

REQUIRED_ROOT_FILES = ["registry.yaml", "README.md", "CHANGELOG.md", "hooks/hooks.json"]
REQUIRED_ROOT_DIRS = [
    "agents", "agents/_shared", "skills", "commands",
    "hooks/scripts", "resources/rdr", "resources/rdr/post-mortem",
]


def test_required_root_files_exist_and_non_empty() -> None:
    failures = []
    for rel in REQUIRED_ROOT_FILES:
        full = PLUGIN_DIR / rel
        if not full.exists():
            failures.append(f"Missing: {rel}")
            continue
        if full.stat().st_size == 0:
            failures.append(f"Empty: {rel}")
    assert not failures, "\n".join(failures)


def test_required_root_dirs_exist_and_non_empty() -> None:
    failures = []
    for rel in REQUIRED_ROOT_DIRS:
        full = PLUGIN_DIR / rel
        if not full.is_dir():
            failures.append(f"Missing dir: {rel}")
            continue
        if not any(full.iterdir()):
            failures.append(f"Empty dir: {rel}")
    assert not failures, "\n".join(failures)


def test_plugin_root_references_resolve() -> None:
    failures = [
        f"{src}: $CLAUDE_PLUGIN_ROOT/{rp} missing"
        for src, rp in _collect_plugin_root_refs()
        if not (PLUGIN_DIR / rp).exists()
    ]
    assert not failures, "\n".join(failures)


# ── Bidirectional registry coverage ─────────────────────────────────────────

def test_every_agent_file_has_registry_entry() -> None:
    registered = set(REGISTRY_AGENTS)
    missing = [af.name for af in agent_files() if af.stem not in registered]
    assert not missing, f"agents/* not in registry.yaml: {missing}"


def test_every_skill_dir_has_registry_entry() -> None:
    agent_skills = {m["skill"] for m in REGISTRY_AGENTS.values() if m.get("skill")}
    standalone = set(REGISTRY.get("standalone_skills", {}))
    rdr = set(REGISTRY.get("rdr_skills", {}))
    all_registered = agent_skills | standalone | rdr
    missing = [sm.parent.name for sm in skill_skill_mds() if sm.parent.name not in all_registered]
    assert not missing, f"skills/* not registered in registry.yaml: {missing}"


def test_every_command_file_has_registry_entry() -> None:
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
    missing = [cf.name for cf in command_files() if cf.stem not in registered]
    assert not missing, f"commands/* not in registry.yaml: {missing}"


# ── RDR-080 stub agents ─────────────────────────────────────────────────────

_DELETED_AGENTS = frozenset({"query-planner", "analytical-operator", "pdf-chromadb-processor"})
_STUB_AGENTS = ("knowledge-tidier", "plan-auditor", "plan-enricher")


def test_stub_agents_do_not_reference_deleted_agents() -> None:
    failures = []
    for name in _STUB_AGENTS:
        stub = PLUGIN_DIR / "agents" / f"{name}.md"
        assert stub.exists(), f"Expected stub file: {stub}"
        content = stub.read_text()
        for deleted in _DELETED_AGENTS:
            if deleted in content:
                failures.append(f"{name}.md references deleted agent '{deleted}'")
    assert not failures, "\n".join(failures)


def test_stub_agents_reference_mcp_tool() -> None:
    failures = []
    for name in _STUB_AGENTS:
        stub = PLUGIN_DIR / "agents" / f"{name}.md"
        if "mcp__plugin_nx_nexus__" not in stub.read_text():
            failures.append(f"{name}.md must reference mcp__plugin_nx_nexus__*")
    assert not failures, "\n".join(failures)
