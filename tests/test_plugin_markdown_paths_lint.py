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
path) and a ``../`` link target (relative to the markdown file itself).

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

#: ``resources/`` not preceded by ``/`` (``${CLAUDE_PLUGIN_ROOT}/``, ``../``),
#: a word character (``conexus/resources`` is fine as a repo path too, it has
#: a slash) or ``}``/``.``.
_BARE_RESOURCES = re.compile(r"(?<![/\w}.])resources/[\w./-]+")
_UNBRACED_ROOT = re.compile(r"\$CLAUDE_PLUGIN_ROOT\b")


def _violations(text: str) -> list[str]:
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
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
    files = sorted(p for d in PLUGIN_MD_DIRS if d.is_dir() for p in d.rglob("*.md"))
    mentions = 0
    bad: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        mentions += text.count("resources/") + text.count("CLAUDE_PLUGIN_ROOT")
        bad += [f"{path.relative_to(REPO_ROOT)}:{v}" for v in _violations(text)]
    print(f"scanned {len(files)} files, {mentions} resources/ or plugin-root mentions")
    assert len(files) >= 50, f"only {len(files)} plugin markdown files found: wrong root?"
    assert mentions >= 20, f"only {mentions} mentions: the scan saw nothing to check"
    assert not bad, "\n".join(bad)
