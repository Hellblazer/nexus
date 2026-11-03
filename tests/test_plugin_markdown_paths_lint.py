# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plugin skill, command and agent markdown names plugin files by a path an
agent can open.

On 2026-10-27 a gate critic was briefed "see resources/rdr/REGISTER.md",
could not find it under the worktree, and ran ``find /`` over the whole disk
(nexus-egv6q). The rdr-gate-checklist skill wrote the path that way, and the
orchestrator copied it into the brief. Two spellings reach an agent
unresolved:

- a bare ``resources/...`` path: it has no anchor outside the plugin;
- an unbraced ``$CLAUDE_PLUGIN_ROOT``: Claude Code substitutes only the
  braced ``${CLAUDE_PLUGIN_ROOT}`` in plugin markdown, and the variable is
  not set in a session's Bash, so the unbraced form stays literal.

Allowed: ``${CLAUDE_PLUGIN_ROOT}/resources/...`` (rendered as an absolute
path), and in skills only a ``../`` link target: Claude Code shows a loaded
skill with its base directory, so the reader can resolve it. An agent or
command body arrives as a prompt with no base directory, so a ``../`` target
there is unanchored too (review r1 of nexus-egv6q: 38 such links).

File-only, so it runs in the ``-m lint`` job. Planted fixtures prove each
rule fires (nexus-moht0), and the live check prints what it scanned.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MD_DIRS = tuple(REPO_ROOT / "conexus" / d for d in ("skills", "commands", "agents"))
#: Plugin docs an agent is sent to read; they carry snippets of agent text. An
#: agent opens them by path, so a relative link between them resolves.
SHARED_DOCS_DIR = REPO_ROOT / "conexus" / "resources" / "agent-shared"

#: ``resources/`` not preceded by ``/`` (``${CLAUDE_PLUGIN_ROOT}/``, ``../``),
#: a word character (``conexus/resources`` is fine as a repo path too, it has
#: a slash) or ``}``/``.``.
_BARE_RESOURCES = re.compile(r"(?<![/\w}.])resources/[\w./-]+")
_UNBRACED_ROOT = re.compile(r"\$CLAUDE_PLUGIN_ROOT\b")
_RELATIVE_TARGET = re.compile(r"\]\((\.\.?/[^)]*)\)")


def _violations(text: str, *, relative_links_ok: bool = True) -> list[str]:
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not relative_links_ok:
            for m in _RELATIVE_TARGET.finditer(line):
                found.append(f"{lineno}: relative link {m.group(1)!r} in a prompt with no base directory")
        for m in _BARE_RESOURCES.finditer(line):
            found.append(f"{lineno}: bare plugin path {m.group(0)!r}")
        for m in _UNBRACED_ROOT.finditer(line):
            found.append(f"{lineno}: unbraced {m.group(0)!r} (write ${{CLAUDE_PLUGIN_ROOT}})")
    return found


@pytest.mark.parametrize(
    "text",
    [
        "Brief the critic: see resources/rdr/REGISTER.md.",
        "Full checklist: [resources/tier-discipline.md](../../resources/tier-discipline.md)",
        "Copy from `$CLAUDE_PLUGIN_ROOT/resources/rdr/TEMPLATE.md`.",
    ],
)
def test_planted_bad_path_is_flagged(text: str) -> None:
    assert _violations(text)


def test_relative_link_is_flagged_outside_skills_only() -> None:
    text = "Follow the [protocol](../resources/agent-shared/CONTEXT_PROTOCOL.md)."
    assert _violations(text, relative_links_ok=False)
    assert not _violations(text, relative_links_ok=True)


@pytest.mark.parametrize(
    "text",
    [
        "Read `${CLAUDE_PLUGIN_ROOT}/resources/rdr/REGISTER.md`.",
        "See [RELAY_TEMPLATE.md](../../resources/agent-shared/RELAY_TEMPLATE.md).",
        "The repo file conexus/resources/rdr/TEMPLATE.md.",
    ],
)
def test_anchored_path_passes(text: str) -> None:
    assert not _violations(text)


def test_plugin_markdown_names_plugin_files_by_an_openable_path() -> None:
    dirs = (*PLUGIN_MD_DIRS, SHARED_DOCS_DIR)
    files = sorted(p for d in dirs if d.is_dir() for p in d.rglob("*.md"))
    skills = REPO_ROOT / "conexus" / "skills"
    mentions = relative_checked = 0
    bad: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        mentions += text.count("resources/") + text.count("CLAUDE_PLUGIN_ROOT")
        has_base_dir = skills in path.parents or SHARED_DOCS_DIR in path.parents
        relative_checked += 0 if has_base_dir else 1
        bad += [f"{path.relative_to(REPO_ROOT)}:{v}" for v in _violations(text, relative_links_ok=has_base_dir)]
    print(f"relative-link rule applied to {relative_checked} agent and command files")
    assert relative_checked >= 20, f"only {relative_checked} agent and command files: wrong roots?"
    print(f"scanned {len(files)} files, {mentions} resources/ or plugin-root mentions")
    assert len(files) >= 50, f"only {len(files)} plugin markdown files found: wrong root?"
    assert mentions >= 20, f"only {mentions} mentions: the scan saw nothing to check"
    assert not bad, "\n".join(bad)
