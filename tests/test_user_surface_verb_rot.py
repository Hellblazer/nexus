# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ``nx <verb ...>`` a user is told to run must exist, on its full verb path (nexus-pplwc).

``tests/test_release_artifact_verb_rot.py`` resolves the verbs in release-only artifacts (repo skills, e2e and
release scripts, workflows, hooks.json, the plugin README) on their first one or two words. The 7.95.0 user-surface
inventory found what that leaves out: the plugin's own markdown (``conexus/skills``, ``agents``, ``commands``,
``resources``, and ``sn/``), and the remedies the code itself prints (``nx doctor`` rows, ``ClickException`` text,
log lines). Those named ``nx service start`` (the verb is ``nx daemon service start``), ``nx repo register``,
``nx mineru restart`` and ``nx re-embed``, none of which exist, and an upgrade skill that called three removed
primitives "callable".

This module sweeps both surfaces and resolves each invocation against the live Click tree on its FULL verb path:
every word after ``nx`` that names a subcommand of the group reached so far descends; an option or a non-word ends
the path; a word that is not a subcommand of a group is a dead verb.

* Plugin markdown: fenced blocks and inline code spans (the README extractor of the release-artifact module).
* Python under ``src/nexus``: every string literal that is not a docstring. Prose there says "nx" as a noun ("nx not
  found", "this nx cannot read", "run nx as that user"), so a match counts only in code context (right after a
  backtick, a quote or an opening parenthesis) or when its first word is a real top-level command, which is where a remedy names a verb.
  ``CHANGELOG.md`` is left out: it names removed verbs as history.

A deliberate mention of a removed verb (history, a pinned old release) is allowlisted below with its reason.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import click
import pytest

from tests.test_release_artifact_verb_rot import REPO_ROOT, _FENCE_RE, _INLINE_CODE_RE, _click_tree

pytestmark = pytest.mark.lint

#: The plugin markdown a user or an agent follows.
_MARKDOWN_GLOBS = ("conexus/**/*.md", "sn/**/*.md")
_VERB_RE = re.compile(r"\bnx\s+([a-z][a-z0-9-]*(?:\s+[a-z][a-z0-9-]*){0,3})")
#: In Python strings, the text just before "nx" that marks a command rather than the noun.
_CODE_CONTEXT_RE = re.compile(r"[`'\"(]$")

#: (relative path, dead verb path as resolved) -> why the mention is deliberate.
_ALLOWLIST: dict[tuple[str, str], str] = {
    ("conexus/skills/upgrade/SKILL.md", "guided-upgrade"): (
        "the 6.18.1 pin's own verb: the step reads that pinned release's source and warns never to run it"
    ),
    ("src/nexus/commands/hooks.py", "index post-commit"): (
        "a log banner in the git hook script; the hook's invocation is `nx index repo`"
    ),
    ("src/nexus/doc/footnote_converter.py", "catalog tumbler"): (
        "the footnote text format 'nx catalog tumbler `1.2`' the converter writes and parses, not a command"
    ),
    ("src/nexus/upgrade_finish.py", "daemon t2"): (
        "names the retired `nx daemon t2 start` a stale autostart unit still fires, telling the user to delete it"
    ),
    ("src/nexus/health.py", "daemon t2"): "the same retired T2 daemon autostart unit, named so the user can delete it",
    ("src/nexus/mcp_infra.py", "hooks auto-approved"): (
        "names the retired v4.34.x hook a stale plugin still runs, so the user can uninstall it"
    ),
}


@dataclass(frozen=True)
class Mention:
    file: str
    line: int
    path: str  # the dead verb path, e.g. "service start"
    text: str


def _dead_path(words: list[str], tree: dict[str, click.Command]) -> str | None:
    """The verb path up to the first word that is not a command, or None when the whole path resolves."""
    if words[0] not in tree:
        return words[0]
    cmd: click.Command = tree[words[0]]
    path = [words[0]]
    for word in words[1:]:
        if not isinstance(cmd, click.Group):
            break
        sub = cmd.commands.get(word)
        path.append(word)
        if sub is None:
            return " ".join(path)
        cmd = sub
    return None


def _markdown_mentions(tree: dict[str, click.Command], files: list[Path]) -> list[Mention]:
    out: list[Mention] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(REPO_ROOT))
        for chunk in _FENCE_RE.findall(text) + _INLINE_CODE_RE.findall(text):
            for match in _VERB_RE.finditer(chunk):
                dead = _dead_path(match.group(1).split(), tree)
                if dead is not None:
                    line = text[: text.find(chunk)].count("\n") + 1 if chunk in text else 0
                    out.append(Mention(rel, line, dead, match.group(0)))
    return out


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _python_mentions(tree: dict[str, click.Command], files: list[Path]) -> list[Mention]:
    out: list[Mention] = []
    for path in files:
        module = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_ids(module)
        rel = str(path.relative_to(REPO_ROOT))
        for node in ast.walk(module):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in docs:
                continue
            for match in _VERB_RE.finditer(node.value):
                words = match.group(1).split()
                in_code = bool(_CODE_CONTEXT_RE.search(node.value[: match.start()]))
                if not in_code and words[0] not in tree:
                    continue  # "nx not found", "this nx cannot": the noun, not a command
                dead = _dead_path(words, tree)
                if dead is not None:
                    out.append(Mention(rel, node.lineno, dead, node.value[max(0, match.start() - 20):match.end() + 20]))
    return out


def _markdown_files() -> list[Path]:
    """The plugin markdown, less the changelogs: a changelog names removed verbs as history, on purpose."""
    return sorted({p for pattern in _MARKDOWN_GLOBS for p in REPO_ROOT.glob(pattern) if p.name != "CHANGELOG.md"})


def _python_files() -> list[Path]:
    return sorted((REPO_ROOT / "src" / "nexus").rglob("*.py"))


def _offenders(mentions: list[Mention]) -> list[str]:
    return [f"{m.file}:{m.line}: 'nx {m.path}' does not exist ({m.text.strip()!r})"
            for m in mentions if (m.file, m.path) not in _ALLOWLIST]


def test_plugin_markdown_names_only_live_verbs() -> None:
    files = _markdown_files()
    assert len(files) > 40, f"only {len(files)} plugin markdown files found"
    offenders = _offenders(_markdown_mentions(_click_tree(), files))
    assert not offenders, "\n".join(offenders)


def test_user_facing_python_strings_name_only_live_verbs() -> None:
    files = _python_files()
    assert len(files) > 200, f"only {len(files)} source files found"
    offenders = _offenders(_python_mentions(_click_tree(), files))
    assert not offenders, "\n".join(offenders)


def test_the_sweeps_see_real_invocations() -> None:
    """Non-vacuity: both sweeps resolve many live invocations, so an extractor that matches nothing is red."""
    tree = _click_tree()
    seen_md = 0
    for p in _markdown_files():
        text = p.read_text(encoding="utf-8")
        seen_md += sum(len(_VERB_RE.findall(c)) for c in _FENCE_RE.findall(text) + _INLINE_CODE_RE.findall(text))
    assert seen_md > 150, seen_md
    assert _dead_path(["daemon", "service", "start"], tree) is None
    assert _dead_path(["service", "start"], tree) == "service start"
    assert _dead_path(["no-such-verb"], tree) == "no-such-verb"
    assert _dead_path(["index", "repo", "--since-head"], tree) is None


def test_allowlist_entries_still_name_a_dead_verb_in_their_file() -> None:
    """A stale entry (the verb now exists, or the file no longer mentions it) is removed, not kept."""
    tree = _click_tree()
    md = {(m.file, m.path) for m in _markdown_mentions(tree, _markdown_files())}
    py = {(m.file, m.path) for m in _python_mentions(tree, _python_files())}
    stale = sorted(set(_ALLOWLIST) - md - py)
    assert not stale, stale


def test_a_planted_dead_remedy_in_python_is_caught(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text('MSG = "Start the service: nx service start"\nOK = "nx not found on PATH"\n', encoding="utf-8")
    mentions = _python_mentions_at(planted)
    assert [m.path for m in mentions] == ["service start"]


def test_a_planted_dead_verb_in_markdown_is_caught(tmp_path: Path) -> None:
    planted = tmp_path / "planted.md"
    planted.write_text("Run `nx migrate-to-service` and `nx daemon service start`.\n", encoding="utf-8")
    tree = _click_tree()
    found = []
    text = planted.read_text(encoding="utf-8")
    for chunk in _INLINE_CODE_RE.findall(text):
        for match in _VERB_RE.finditer(chunk):
            dead = _dead_path(match.group(1).split(), tree)
            if dead is not None:
                found.append(dead)
    assert found == ["migrate-to-service"]


def _python_mentions_at(path: Path) -> list[Mention]:
    """:func:`_python_mentions` for a file outside the repo (the planted case)."""
    tree = _click_tree()
    module = ast.parse(path.read_text(encoding="utf-8"))
    out: list[Mention] = []
    for node in ast.walk(module):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _VERB_RE.finditer(node.value):
                words = match.group(1).split()
                in_code = bool(_CODE_CONTEXT_RE.search(node.value[: match.start()]))
                if not in_code and words[0] not in tree:
                    continue
                dead = _dead_path(words, tree)
                if dead is not None:
                    out.append(Mention(path.name, node.lineno, dead, node.value))
    return out
