# SPDX-License-Identifier: AGPL-3.0-or-later
"""Installation simulation tests for the nx Claude Code plugin.

Simulates what happens when Claude Code installs the plugin from GitHub:
  1. Clone the repo (simulated via `git clone --local`)
  2. Set CLAUDE_PLUGIN_ROOT to the `nx/` subdirectory of the clone
  3. Validate that all referenced files are present in that installed location

This is fundamentally different from test_plugin_structure.py, which validates
the source tree. These tests validate the INSTALLED state that a user would
actually get after running `/plugin marketplace add Hellblazer/nexus`.
"""
import json
import re
import subprocess
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def installed_plugin(tmp_path_factory) -> Path:
    """
    Simulate a Claude Code plugin installation by cloning the repo locally.

    Claude Code does:
      git clone <github-url> ~/.claude/plugins/cache/<org>/<repo>/<version>/
      CLAUDE_PLUGIN_ROOT = <install_path>/<source>   # source from marketplace.json

    We replicate this using `git clone --local` so no network is needed.
    Returns the path that would be CLAUDE_PLUGIN_ROOT.
    """
    clone_root = tmp_path_factory.mktemp("plugin-install")
    # git clone --local simulates a real clone: only committed, tracked files are copied
    result = subprocess.run(
        ["git", "clone", "--local", str(REPO_ROOT), str(clone_root)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"git clone failed:\n{result.stderr}"

    # Read source dir from marketplace.json (same as Claude Code does)
    marketplace = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text()
    )
    source = marketplace["plugins"][0]["source"].lstrip("./")  # "./nx" -> "nx"

    plugin_root = clone_root / source
    assert plugin_root.is_dir(), (
        f"CLAUDE_PLUGIN_ROOT would be {plugin_root} but it does not exist in the clone. "
        f"Check marketplace.json source field: {marketplace['plugins'][0]['source']!r}"
    )
    return plugin_root


# ── Required files and dirs must exist in installed clone ─────────────────────


REQUIRED_FILES = [
    "registry.yaml",
    "README.md",
    "CHANGELOG.md",
    "hooks/hooks.json",
    "agents/_shared/RELAY_TEMPLATE.md",
    "agents/_shared/CONTEXT_PROTOCOL.md",
    "agents/_shared/ERROR_HANDLING.md",
    "agents/_shared/MAINTENANCE.md",
    "agents/_shared/README.md",
    "resources/rdr/TEMPLATE.md",
    "resources/rdr/post-mortem/TEMPLATE.md",
    "resources/rdr/README-TEMPLATE.md",
]

REQUIRED_DIRS = [
    "agents",
    "agents/_shared",
    "skills",
    "commands",
    "hooks/scripts",
    "resources/rdr",
]


@pytest.mark.parametrize("rel_path", REQUIRED_FILES)
def test_required_file_present_after_install(installed_plugin: Path, rel_path: str) -> None:
    """Every required plugin file must be present in a git-cloned install."""
    full = installed_plugin / rel_path
    assert full.exists(), (
        f"CLAUDE_PLUGIN_ROOT/{rel_path} is MISSING from a cloned install.\n"
        f"  This file would not exist for users installing from GitHub.\n"
        f"  Check that it is committed to git and not in .gitignore."
    )
    assert full.stat().st_size > 0, (
        f"CLAUDE_PLUGIN_ROOT/{rel_path} is empty in the cloned install."
    )


@pytest.mark.parametrize("rel_dir", REQUIRED_DIRS)
def test_required_dir_present_after_install(installed_plugin: Path, rel_dir: str) -> None:
    """Every required plugin directory must be present and non-empty after install."""
    full = installed_plugin / rel_dir
    assert full.is_dir(), (
        f"CLAUDE_PLUGIN_ROOT/{rel_dir}/ is MISSING from a cloned install."
    )
    assert any(full.iterdir()), (
        f"CLAUDE_PLUGIN_ROOT/{rel_dir}/ is empty in the cloned install."
    )


# ── $CLAUDE_PLUGIN_ROOT refs resolve in installed clone ───────────────────────


def _collect_all_plugin_root_refs(plugin_root: Path) -> list[tuple[str, str]]:
    """Scan every file in the installed plugin for $CLAUDE_PLUGIN_ROOT/... refs."""
    results = []
    for src_file in sorted(plugin_root.rglob("*")):
        if not src_file.is_file():
            continue
        try:
            text = src_file.read_text()
        except UnicodeDecodeError:
            continue
        label = str(src_file.relative_to(plugin_root))
        for match in re.finditer(r"\$CLAUDE_PLUGIN_ROOT/([^\s'\"`)]+)", text):
            results.append((label, match.group(1)))
    return results


def test_all_plugin_root_refs_resolve_after_install(installed_plugin: Path) -> None:
    """Every $CLAUDE_PLUGIN_ROOT/... reference in the installed plugin must resolve."""
    refs = _collect_all_plugin_root_refs(installed_plugin)
    assert refs, "No $CLAUDE_PLUGIN_ROOT references found — something is wrong"
    missing = []
    for source, rel_path in refs:
        full = installed_plugin / rel_path
        if not full.exists():
            missing.append(f"  {source}: $CLAUDE_PLUGIN_ROOT/{rel_path}")
    assert not missing, (
        f"{len(missing)} $CLAUDE_PLUGIN_ROOT reference(s) would be broken after install:\n"
        + "\n".join(missing)
    )


# ── Relative markdown links resolve in installed clone ────────────────────────


def test_all_shared_relative_links_resolve_after_install(installed_plugin: Path) -> None:
    """Every markdown link to _shared/ must resolve from its location in the clone."""
    missing = []
    for md_file in sorted(installed_plugin.rglob("*.md")):
        if "_shared" in md_file.parts:
            continue
        text = md_file.read_text()
        for match in re.finditer(r"\[([^\]]*)\]\(([^)]*_shared/[^)]*)\)", text):
            raw_path = match.group(2).split("#")[0]
            resolved = (md_file.parent / raw_path).resolve()
            if not resolved.exists():
                label = str(md_file.relative_to(installed_plugin))
                missing.append(f"  {label}: [{raw_path!r}] → {resolved}")
    assert not missing, (
        f"{len(missing)} relative _shared/ link(s) would be broken after install:\n"
        + "\n".join(missing)
    )


# ── All agent files present in installed clone ────────────────────────────────


def test_all_agent_files_present_after_install(installed_plugin: Path) -> None:
    """Every agent .md file from the source tree must be present after install."""
    source_agents = {p.name for p in (REPO_ROOT / "nx" / "agents").glob("*.md")}
    installed_agents = {
        p.name for p in (installed_plugin / "agents").glob("*.md")
    }
    missing = source_agents - installed_agents
    assert not missing, (
        f"Agent files missing from cloned install: {missing}\n"
        f"Check these are committed to git."
    )


# ── All skill SKILL.md files present in installed clone ───────────────────────


def test_all_skill_files_present_after_install(installed_plugin: Path) -> None:
    """Every skills/<name>/SKILL.md from the source tree must survive the install."""
    source_skills = {p.parent.name for p in (REPO_ROOT / "nx" / "skills").glob("*/SKILL.md")}
    installed_skills = {
        p.parent.name for p in (installed_plugin / "skills").glob("*/SKILL.md")
    }
    missing = source_skills - installed_skills
    assert not missing, (
        f"Skill SKILL.md files missing from cloned install: {missing}\n"
        f"Check these are committed to git."
    )


# ── All command files present in installed clone ──────────────────────────────


def test_all_command_files_present_after_install(installed_plugin: Path) -> None:
    """Every commands/*.md from the source tree must survive the install."""
    source_cmds = {p.name for p in (REPO_ROOT / "nx" / "commands").glob("*.md")}
    installed_cmds = {p.name for p in (installed_plugin / "commands").glob("*.md")}
    missing = source_cmds - installed_cmds
    assert not missing, (
        f"Command files missing from cloned install: {missing}\n"
        f"Check these are committed to git."
    )


# ── Hook scripts present in installed clone ───────────────────────────────────


def test_all_hook_scripts_present_after_install(installed_plugin: Path) -> None:
    """Every hook script from the source tree must survive the install."""
    source_scripts = {
        p.name for p in (REPO_ROOT / "nx" / "hooks" / "scripts").iterdir()
        if p.is_file()
    }
    installed_scripts = {
        p.name for p in (installed_plugin / "hooks" / "scripts").iterdir()
        if p.is_file()
    }
    missing = source_scripts - installed_scripts
    assert not missing, (
        f"Hook scripts missing from cloned install: {missing}\n"
        f"Check these are committed to git."
    )
