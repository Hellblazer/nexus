# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ``nx <verb ...>`` a user is told to run must exist, on its full verb path (nexus-pplwc).

``tests/test_release_artifact_verb_rot.py`` resolves the verbs in release-only artifacts (repo skills, e2e and
release scripts, workflows, hooks.json, the plugin README) on their first one or two words. The 7.95.0 user-surface
inventory found what that leaves out: the plugin's own markdown and hook scripts, and the remedies the code itself
prints (``nx doctor`` rows, ``ClickException`` text, log lines, ``--help`` text). Those named ``nx service start``
(the verb is ``nx daemon service start``), ``nx repo register``, ``nx mineru restart`` and ``nx re-embed``, none of
which exist, and an upgrade skill that called three removed primitives "callable".

Scope, all resolved against the live Click tree on the FULL verb path:

* Text: every ``.md`` under ``conexus/`` and ``sn/`` (prose, inline code and every fence alike), every ``.md``,
  ``.html`` and ``.txt`` under ``src/nexus``, ``conexus/registry.yaml``, the plan library YAML, the mcpb manifest and
  both ``plugin.json`` files. ``CHANGELOG.md`` and ``PENDING_RELEASE.md`` are left out: they are
  change records and name removed verbs as history.
* Python under ``src/nexus``, ``conexus/hooks``, ``conexus/launch``, ``conexus/mcp``, ``mcpb/src`` and
  ``sn/hooks/scripts``: every string literal that is not a docstring, plus the docstring
  of every Click command or group (its ``--help`` text).

``nx`` is also a noun in prose ("nx not found", "this nx cannot read", "the nx process"). A word after ``nx`` that is
in :data:`_PROSE_WORDS` ends the path there, so ``nx is ...`` names nothing and ``nx index is suspended`` resolves
as ``nx index``. Any other word is a verb: a first word that is not a top-level command, or a later word that is not
a subcommand of the group reached so far, is a dead verb. An option or a non-word also ends the path.

A deliberate mention of a removed verb (history, a pinned old release) is allowlisted below by file, dead path and
a fragment of the mention's own text, with its reason.
"""
from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import click
import pytest

from tests.test_release_artifact_verb_rot import REPO_ROOT, _click_tree

pytestmark = pytest.mark.lint

_TEXT_GLOBS = ("conexus/**/*.md", "sn/**/*.md", "src/nexus/**/*.md", "src/nexus/**/*.html", "src/nexus/**/*.txt",
               "conexus/registry.yaml", "conexus/plans/**/*.yml", "conexus/plans/**/*.yaml", "mcpb/manifest.json",
               "conexus/.claude-plugin/plugin.json", "sn/.claude-plugin/plugin.json")
_PYTHON_GLOBS = ("src/nexus/**/*.py", "conexus/hooks/**/*.py", "conexus/launch/**/*.py", "conexus/mcp/**/*.py",
                 "mcpb/src/**/*.py", "sn/hooks/scripts/**/*.py")
_RECORDS = frozenset({"CHANGELOG.md", "PENDING_RELEASE.md"})
_VERB_RE = re.compile(r"\bnx\s+([a-z][a-z0-9-]*(?:\s+[a-z][a-z0-9-]*){0,3})")

#: Words after "nx" that make it the program's name in a sentence, not a command. Closed on purpose: a new noun use
#: in prose that is not here fails the lint, and the remedy is to add the word, never to drop the check.
_PROSE_WORDS: frozenset[str] = frozenset("""
    a already an and are as at be been builds call can cannot client code command commands configuration could
    creates did does effort entry for from has have health in is it itself log looks makes manages may must never
    not of on option or parses path pins process quietly reads restriction running runs ships should spawn
    subcommand that the then this tier to tools unreachable until uses via was when which will with would you
""".split())

#: (relative path, dead verb path, a fragment of the mention text) -> why the mention is deliberate.
_ALLOWLIST: dict[tuple[str, str, str], str] = {
    ("conexus/skills/upgrade/SKILL.md", "guided-upgrade", "Never run the pin's `nx guided-upgrade"): (
        "the 6.18.1 pin's own verb: the step reads that pinned release's source and warns never to run it"
    ),
    ("src/nexus/commands/hooks.py", "index post-commit", 'echo "=== nx index post-commit'): (
        "a log banner in the git hook script; the hook's invocation is `nx index repo`"
    ),
    ("src/nexus/doc/footnote_converter.py", "catalog tumbler", "]: nx catalog tumbler `"): (
        "the footnote text format 'nx catalog tumbler `1.2`' the converter writes, not a command"
    ),
    ("src/nexus/doc/footnote_converter.py", "catalog tumbler", r"\]:\s*nx catalog tumbler"): (
        "the regex that parses that footnote format back"
    ),
    ("src/nexus/upgrade_finish.py", "daemon t2", "it fires a `nx daemon t2 start` that no longer exists"): (
        "names the retired verb a stale autostart unit still fires, telling the user to delete the unit"
    ),
    ("src/nexus/health.py", "daemon t2", "immediately-failing `nx daemon t2 start`"): (
        "the same retired T2 daemon autostart unit, named so the user can delete it"
    ),
    ("src/nexus/mcp_infra.py", "hooks auto-approved", "v4.34.x nx hooks auto-approved every Bash command"): (
        "names the retired v4.34.x hook a stale plugin still runs, so the user can uninstall it"
    ),
    ("src/nexus/daemon/AGENTS.md", "entry", "an nx entry point stands directly before"): "'nx entry point': a noun",
    ("src/nexus/daemon/CLAUDE.md", "entry", "an nx entry point stands directly before"): "the same file (a symlink)",
    ("src/nexus/_session_end_launcher.py", "tier", "nx tier writes"): "'nx tier writes': a noun phrase",
    ("src/nexus/commands/command_context.py", "option", "nx option parsing"): "'nx option parsing': a noun phrase",
    ("src/nexus/operators/dispatch.py", "tools", "files and nx tools through it"): "'nx tools': a noun",
    ("src/nexus/pdf_stack.py", "pins", "this nx pins carries"): "'this nx pins': nx as the subject of 'pins'",
}


@dataclass(frozen=True)
class Mention:
    file: str
    line: int
    path: str  # the dead verb path, e.g. "service start"
    text: str


#: Prose words that are also nouns a dead verb could be named for ("nx health", "nx log tail"): one of these reads as
#: prose only when no word follows it or the next word is prose too ("nx log is", "the nx process (or").
_NOUNS: frozenset[str] = frozenset(
    "client code command commands configuration effort entry health log option path pins process restriction "
    "subcommand tier tools".split())


def _dead_path(words: list[str], tree: dict[str, click.Command]) -> str | None:
    """The verb path up to the first word that is not a command, or None when the path resolves (or names nothing)."""
    if words[0] in _NOUNS and len(words) > 1 and words[1] not in _PROSE_WORDS:
        return words[0] if words[0] not in tree else _dead_path_from(words, tree)
    if words[0] in _PROSE_WORDS:
        return None
    return _dead_path_from(words, tree)


def _dead_path_from(words: list[str], tree: dict[str, click.Command]) -> str | None:
    if words[0] not in tree:
        return words[0]
    cmd: click.Command = tree[words[0]]
    path = [words[0]]
    for word in words[1:]:
        if not isinstance(cmd, click.Group) or word in _PROSE_WORDS:
            break
        sub = cmd.commands.get(word)
        path.append(word)
        if sub is None:
            return " ".join(path)
        cmd = sub
    return None


@dataclass
class Sweep:
    mentions: list[Mention]
    examined: int  # every "nx <word>" looked at, live or dead


def _scan(text: str, rel: str, first_line: int, tree: dict[str, click.Command], sweep: Sweep) -> None:
    for match in _VERB_RE.finditer(text):
        sweep.examined += 1
        dead = _dead_path(match.group(1).split(), tree)
        if dead is not None:
            line = first_line + text.count("\n", 0, match.start())
            context = text[max(0, match.start() - 30):match.end() + 30]
            sweep.mentions.append(Mention(rel, line, dead, " ".join(context.split())))


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _text_mentions(tree: dict[str, click.Command], files: Iterable[Path], root: Path) -> Sweep:
    sweep = Sweep([], 0)
    for path in files:
        _scan(path.read_text(encoding="utf-8"), _rel(path, root), 1, tree, sweep)
    return sweep


def _is_click_command(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Decorated with ``@x.command(...)``, ``@x.group(...)``, ``@command`` or ``@group``: its docstring is help text."""
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name in {"command", "group"}:
            return True
    return False


def _non_help_docstrings(module: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(module):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_click_command(node):
                continue  # --help text: scanned
            ids.add(id(first.value))
    return ids


def _python_mentions(tree: dict[str, click.Command], files: Iterable[Path], root: Path) -> Sweep:
    sweep = Sweep([], 0)
    for path in files:
        module = ast.parse(path.read_text(encoding="utf-8"))
        skip = _non_help_docstrings(module)
        rel = _rel(path, root)
        for node in ast.walk(module):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                _scan(node.value, rel, node.lineno, tree, sweep)
    return sweep


def _files(globs: tuple[str, ...]) -> list[Path]:
    return sorted({p for pattern in globs for p in REPO_ROOT.glob(pattern) if p.name not in _RECORDS})


def _allowed(m: Mention) -> bool:
    return any((f, p) == (m.file, m.path) and frag in m.text for f, p, frag in _ALLOWLIST)


def _offenders(mentions: list[Mention]) -> list[str]:
    return [f"{m.file}:{m.line}: 'nx {m.path}' does not exist ({m.text!r})" for m in mentions if not _allowed(m)]


def test_plugin_and_package_text_names_only_live_verbs() -> None:
    files = _files(_TEXT_GLOBS)
    assert len(files) > 40, f"only {len(files)} text files found"
    sweep = _text_mentions(_click_tree(), files, REPO_ROOT)
    assert sweep.examined > 300, sweep.examined  # 347 at nexus-pplwc r2
    offenders = _offenders(sweep.mentions)
    assert not offenders, "\n".join(offenders)


def test_user_facing_python_strings_name_only_live_verbs() -> None:
    files = _files(_PYTHON_GLOBS)
    assert len(files) > 200, f"only {len(files)} source files found"
    sweep = _python_mentions(_click_tree(), files, REPO_ROOT)
    assert sweep.examined > 1000, sweep.examined  # 1409 at nexus-pplwc r2
    offenders = _offenders(sweep.mentions)
    assert not offenders, "\n".join(offenders)


def test_the_resolver_reads_paths_and_prose() -> None:
    tree = _click_tree()
    assert _dead_path(["daemon", "service", "start"], tree) is None
    assert _dead_path(["service", "start"], tree) == "service start"
    assert _dead_path(["no-such-verb"], tree) == "no-such-verb"
    assert _dead_path(["index", "repo", "--since-head"], tree) is None
    assert _dead_path(["is", "not", "running"], tree) is None
    assert _dead_path(["index", "is", "suspended"], tree) is None
    assert _dead_path(["store", "via", "the"], tree) is None
    assert _dead_path(["log", "tail"], tree) == "log"
    assert _dead_path(["tools", "list"], tree) == "tools"
    assert _dead_path(["process", "or", "until"], tree) is None
    assert _dead_path(["health"], tree) is None


def test_allowlist_entries_still_match_a_mention() -> None:
    """A stale entry (the verb now exists, or the file no longer says it) is removed, not kept."""
    tree = _click_tree()
    mentions = (_text_mentions(tree, _files(_TEXT_GLOBS), REPO_ROOT).mentions
                + _python_mentions(tree, _files(_PYTHON_GLOBS), REPO_ROOT).mentions)
    stale = sorted(k for k in _ALLOWLIST
                   if not any((m.file, m.path) == k[:2] and k[2] in m.text for m in mentions))
    assert not stale, stale


_PLANTED_PY = '''\
import click

A = "Start the service: nx service start"
B = "Run nx guided-upgrade first"
C = "Then try: nx migrate-to-service"
D = "Use nx repo register to add it"
OK1 = "nx not found on PATH"
OK2 = "this nx cannot read it; nx index is suspended"


def helper():
    """Docstring of a helper: nx remediate is history here, not scanned."""


@click.command()
def cli():
    """Help text: mirrors nx daemon t2 install."""
'''


def test_the_production_python_sweep_catches_planted_rot(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text(_PLANTED_PY, encoding="utf-8")
    sweep = _python_mentions(_click_tree(), [planted], tmp_path)
    assert sorted(m.path for m in sweep.mentions) == [
        "daemon t2", "guided-upgrade", "migrate-to-service", "repo", "service start"]
    assert {m.line for m in sweep.mentions if m.path == "daemon t2"} == {17}


def test_the_production_text_sweep_catches_planted_rot(tmp_path: Path) -> None:
    planted = tmp_path / "planted.md"
    planted.write_text("Intro.\n\nRun nx migrate-to-service, then `nx daemon service start`.\n\n"
                       "```\nnx repo register .\n```\n\nIf nx is not running, nx migrate-to-service again.\n",
                       encoding="utf-8")
    sweep = _text_mentions(_click_tree(), [planted], tmp_path)
    assert [(m.path, m.line) for m in sweep.mentions] == [
        ("migrate-to-service", 3), ("repo", 6), ("migrate-to-service", 9)]
    assert sweep.examined == 5


#: "a && b" in a printed string: Windows PowerShell 5.1 (the default shell on Windows 10 and 11) rejects "&&", so a
#: user pasting it gets a parse error. Remedies say "`a`, then `b`" instead. Any "&&" in a non-docstring literal (an
#: f-string part, one half of a concatenation) fails unless it is listed here with the reason it is not a remedy.
_AND_CHAIN_ALLOWED: dict[tuple[str, str], str] = {
    ("src/nexus/commands/t3.py", "--dry-run && nx t3 gc"): (
        "the help shows scripting a dry run whose exit 1 must stop the real run; '&&' is that gate"
    ),
    ("src/nexus/commands/hooks.py", "REPO_TOP=\"$(git rev-parse --show-toplevel)\""): (
        "the body of the git post-commit hook nexus writes; git runs it under sh"
    ),
    ("src/nexus/commands/doctor.py", "awk '$2==1 && /multiprocessing/"): (
        "an awk condition inside a POSIX ps | awk | xargs pipeline, not a command chain"
    ),
    ("conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py", "(?:&&|"): (
        "a regex that splits a shell command line at its separators"
    ),
}


def _and_chained_remedies(files: Iterable[Path], root: Path) -> list[str]:
    out: list[str] = []
    for path in files:
        module = ast.parse(path.read_text(encoding="utf-8"))
        skip = _non_help_docstrings(module)
        rel = _rel(path, root)
        for node in ast.walk(module):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
                    and "&&" in node.value
                    and not any(f == rel and frag in node.value for f, frag in _AND_CHAIN_ALLOWED)):
                out.append(f"{rel}:{node.lineno}: {node.value.strip()[:100]!r}")
    return out


def test_no_user_facing_string_chains_commands_with_and_and() -> None:
    offenders = _and_chained_remedies(_files(_PYTHON_GLOBS), REPO_ROOT)
    assert not offenders, "\n".join(offenders)


def test_and_chain_allowlist_entries_still_match() -> None:
    files = {_rel(p, REPO_ROOT): p for p in _files(_PYTHON_GLOBS)}
    stale = [k for k in _AND_CHAIN_ALLOWED if k[0] not in files or k[1] not in files[k[0]].read_text(encoding="utf-8")]
    assert not stale, stale


def test_the_and_chain_check_catches_planted_remedies(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text(
        'A = "restart: `nx daemon service stop && nx daemon service start`"\n'
        'B = f"run {cmd} && {other}"\n'
        'C = "nx a && " + "nx b"\n'
        'D = "uv sync && uv run nx doctor"\n'
        'E = "launchctl enable gui/1/x && nx daemon service install"\n'
        'F = "winget install uv && claude plugin install x"\n'
        'OK = "nx daemon service stop, then nx daemon service start"\n', encoding="utf-8")
    assert len(_and_chained_remedies([planted], tmp_path)) == 6
