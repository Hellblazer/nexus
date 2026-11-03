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
_BARE_RESOURCES = re.compile(r"(?<![/\w}.])(?:\./)?resources/[\w./-]+")
#: ``$RDR_DIR`` reaches a dispatched agent unexpanded: flagged on a relay ``Files:`` line or a line about a brief,
#: unless the line says to write the absolute path (nexus-3811j).
_RDR_DIR = re.compile(r"\$RDR_DIR\b")
_BRIEF_LINE = re.compile(r"^\s*-\s*Files:|\bbrief\b", re.IGNORECASE)
#: Plugin file names an agent or command body might name bare (``registry.yaml``, ``reference.md``): with no base
#: directory a bare name is unanchored. Common names any project has are left out.
#: A nexus repo doc (``docs/collections.md``) named in plugin text: an agent in another project has no such file, so the
#: text links it on GitHub or states the rule inline (nexus-4deer). ``docs/rdr/`` is the project's own RDR directory.
_REPO_DOC = re.compile(r"(?<![\w/.:-])docs/(?!rdr/)[\w./-]+\.md")
_COMMON_NAMES = frozenset({"README.md", "SKILL.md", "CHANGELOG.md", "LICENSE", "plugin.json"})


def _plugin_file_names() -> frozenset[str]:
    root = REPO_ROOT / "conexus"
    names = {p.name for d in ("resources", "skills") for p in (root / d).rglob("*") if p.is_file()}
    return frozenset(n for n in names if "." in n and n not in _COMMON_NAMES)


def _bare_name_pattern(names: frozenset[str]) -> re.Pattern[str]:
    alts = "|".join(sorted((re.escape(n) for n in names), key=len, reverse=True))
    return re.compile(rf"(?<![/\w.$}}\[-])(?:{alts})(?![\w-])")  # a link label `[NAME](...)` is anchored by its target
_UNBRACED_ROOT = re.compile(r"\$CLAUDE_PLUGIN_ROOT\b")
_RELATIVE_TARGET = re.compile(r"\]\((\.\.?/[^)]*)\)")


def _violations(
    text: str, *, relative_links_ok: bool = True, bare_names: re.Pattern[str] | None = None,
) -> list[str]:
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not relative_links_ok:
            for m in _RELATIVE_TARGET.finditer(line):
                found.append(f"{lineno}: relative link {m.group(1)!r} in a prompt with no base directory")
            if bare_names is not None:
                for m in bare_names.finditer(line):
                    found.append(f"{lineno}: bare plugin file name {m.group(0)!r} in a prompt with no base directory")
        for m in _REPO_DOC.finditer(line):
            found.append(f"{lineno}: repo-only doc {m.group(0)!r} (link it on GitHub or state the rule inline)")
        if _RDR_DIR.search(line) and _BRIEF_LINE.search(line) and "absolute" not in line:
            found.append(f"{lineno}: $RDR_DIR in a brief line (write the resolved absolute path)")
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
        "Copy from `./resources/rdr/TEMPLATE.md`.",
        "- Files: $RDR_DIR/NNN-*.md",
        "The dispatch brief includes the register (`$RDR_DIR/REGISTER.md`).",
        "See `/conexus:plan-first` and `docs/plan-authoring-guide.md`.",
        "Per docs/collections.md Rule 1, a collection is a subject.",
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


def test_a_github_link_or_the_projects_rdr_dir_passes() -> None:
    assert not _violations("Rules: https://github.com/Hellblazer/nexus/blob/main/docs/collections.md.")
    assert not _violations("Write the RDR to `docs/rdr/NNN-kebab-title.md`.")


def test_rdr_dir_outside_a_brief_or_with_the_absolute_rule_passes() -> None:
    assert not _violations("All file paths below use `$RDR_DIR` in place of `docs/rdr`.")
    assert not _violations("- Files: <absolute path of the RDR directory> (resolved from `$RDR_DIR`; absolute)")


def test_a_bare_plugin_file_name_is_flagged_in_an_agent_body_only() -> None:
    names = _bare_name_pattern(frozenset({"registry.yaml", "reference.md"}))
    for text in ("Load registry.yaml first.", "See reference.md for the table."):
        assert _violations(text, relative_links_ok=False, bare_names=names), text
        assert not _violations(text, relative_links_ok=True, bare_names=names), text
    for ok in ("Read `${CLAUDE_PLUGIN_ROOT}/resources/registry.yaml`.", "the my-reference.md file", "a/reference.md",
               "[reference.md](${CLAUDE_PLUGIN_ROOT}/skills/x/reference.md)"):
        assert not _violations(ok, relative_links_ok=False, bare_names=names), ok


def test_the_plugin_file_name_census_is_not_empty() -> None:
    names = _plugin_file_names()
    assert "registry.yaml" in names or len(names) >= 10, sorted(names)[:20]


def test_plugin_markdown_names_plugin_files_by_an_openable_path() -> None:
    dirs = (*PLUGIN_MD_DIRS, SHARED_DOCS_DIR)
    files = sorted(p for d in dirs if d.is_dir() for p in d.rglob("*.md"))
    skills = REPO_ROOT / "conexus" / "skills"
    mentions = relative_checked = 0
    bare_names = _bare_name_pattern(_plugin_file_names())
    bad: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        mentions += text.count("resources/") + text.count("CLAUDE_PLUGIN_ROOT")
        has_base_dir = skills in path.parents or SHARED_DOCS_DIR in path.parents
        relative_checked += 0 if has_base_dir else 1
        bad += [
            f"{path.relative_to(REPO_ROOT)}:{v}"
            for v in _violations(text, relative_links_ok=has_base_dir, bare_names=bare_names)
        ]
    print(f"relative-link rule applied to {relative_checked} agent and command files")
    assert relative_checked >= 20, f"only {relative_checked} agent and command files: wrong roots?"
    print(f"scanned {len(files)} files, {mentions} resources/ or plugin-root mentions")
    assert len(files) >= 50, f"only {len(files)} plugin markdown files found: wrong root?"
    assert mentions >= 20, f"only {mentions} mentions: the scan saw nothing to check"
    assert not bad, "\n".join(bad)
