# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ``nx <verb ...>`` a user is told to run must exist, on its full verb path (nexus-pplwc).

``tests/test_release_artifact_verb_rot.py`` resolves the verbs in release-only artifacts (repo skills, e2e and
release scripts, workflows, hooks.json, the plugin README) on their first one or two words. The 7.95.0 user-surface
inventory found what that leaves out: the plugin's own markdown and hook scripts, and the remedies the code itself
prints (``nx doctor`` rows, ``ClickException`` text, log lines, ``--help`` text). Those named ``nx service start``
(the verb is ``nx daemon service start``), ``nx repo register``, ``nx mineru restart`` and ``nx re-embed``, none of
which exist, and an upgrade skill that called three removed primitives "callable".

Scope, all resolved against the live Click tree on the FULL verb path:

* Text: every ``.md`` under ``conexus/`` and ``sn/`` (prose, inline code and every fence alike) and every ``.md``,
  ``.html`` and ``.txt`` under ``src/nexus``. ``CHANGELOG.md`` and ``PENDING_RELEASE.md`` are left out: they are
  change records and name removed verbs as history.
* Python under ``src/nexus`` and ``conexus/hooks``: every string literal that is not a docstring, plus the docstring
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

_TEXT_GLOBS = ("conexus/**/*.md", "sn/**/*.md", "src/nexus/**/*.md", "src/nexus/**/*.html", "src/nexus/**/*.txt")
_PYTHON_GLOBS = ("src/nexus/**/*.py", "conexus/hooks/**/*.py")
_RECORDS = frozenset({"CHANGELOG.md", "PENDING_RELEASE.md"})
_VERB_RE = re.compile(r"\bnx\s+([a-z][a-z0-9-]*(?:\s+[a-z][a-z0-9-]*){0,3})")

#: Words after "nx" that make it the program's name in a sentence, not a command. Closed on purpose: a new noun use
#: in prose that is not here fails the lint, and the remedy is to add the word, never to drop the check.
_PROSE_WORDS: frozenset[str] = frozenset("""
    a already an and are as at be been builds call can cannot client code command commands configuration could
    creates did does effort entry for from has have health in is it itself log looks makes manages may must never
    not of on option or parses path pins process quietly reads restriction running runs ships should spawn
    subcommand that the then this tier to tools unreachable until uses via was when which will with would
""".split())

#: (relative path, dead verb path, a fragment of the mention text) -> why the mention is deliberate.
_ALLOWLIST: dict[tuple[str, str, str], str] = {
    ("conexus/skills/upgrade/SKILL.md", "guided-upgrade", "Never run the pin's"): (
        "the 6.18.1 pin's own verb: the step reads that pinned release's source and warns never to run it"
    ),
    ("src/nexus/commands/hooks.py", "index post-commit", "nx index post-commit"): (
        "a log banner in the git hook script; the hook's invocation is `nx index repo`"
    ),
    ("src/nexus/doc/footnote_converter.py", "catalog tumbler", "nx catalog tumbler"): (
        "the footnote text format 'nx catalog tumbler `1.2`' the converter writes and parses, not a command"
    ),
    ("src/nexus/upgrade_finish.py", "daemon t2", "daemon t2"): (
        "names the retired `nx daemon t2 start` a stale autostart unit still fires, telling the user to delete it"
    ),
    ("src/nexus/health.py", "daemon t2", "daemon t2"): (
        "the same retired T2 daemon autostart unit, named so the user can delete it"
    ),
    ("src/nexus/mcp_infra.py", "hooks auto-approved", "hooks auto-approved"): (
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
    """The verb path up to the first word that is not a command, or None when the path resolves (or names nothing)."""
    if words[0] in _PROSE_WORDS:
        return None
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
    assert sweep.examined > 300, sweep.examined  # 376 at nexus-pplwc
    offenders = _offenders(sweep.mentions)
    assert not offenders, "\n".join(offenders)


def test_user_facing_python_strings_name_only_live_verbs() -> None:
    files = _files(_PYTHON_GLOBS)
    assert len(files) > 200, f"only {len(files)} source files found"
    sweep = _python_mentions(_click_tree(), files, REPO_ROOT)
    assert sweep.examined > 1000, sweep.examined  # 1385 at nexus-pplwc
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


#: "a && nx b" in a printed remedy: Windows PowerShell 5.1 (the default shell on Windows 10 and 11) rejects "&&", so
#: a user pasting it gets a parse error. Remedies say "a, then b" instead. A macOS-only launchctl line is exempt.
_AND_CHAIN_RE = re.compile(r"&&\s*`*nx\s")
#: (file, fragment) -> why the "&&" is the point of the text.
_AND_CHAIN_ALLOWED: dict[tuple[str, str], str] = {
    ("src/nexus/commands/t3.py", "--dry-run && nx t3 gc"): (
        "the help shows scripting a dry run whose exit 1 must stop the real run; '&&' is that gate"
    ),
}


def _and_chained_remedies(files: Iterable[Path], root: Path) -> list[str]:
    out: list[str] = []
    for path in files:
        module = ast.parse(path.read_text(encoding="utf-8"))
        skip = _non_help_docstrings(module)
        for node in ast.walk(module):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
                    and _AND_CHAIN_RE.search(node.value) and "launchctl" not in node.value
                    and not any(f == _rel(path, root) and frag in node.value for f, frag in _AND_CHAIN_ALLOWED)):
                out.append(f"{_rel(path, root)}:{node.lineno}: {node.value.strip()[:100]!r}")
    return out


def test_no_user_facing_remedy_chains_nx_commands_with_and_and() -> None:
    offenders = _and_chained_remedies(_files(_PYTHON_GLOBS), REPO_ROOT)
    assert not offenders, "\n".join(offenders)


def test_the_and_chain_check_catches_a_planted_remedy(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text('R = "restart: `nx daemon service stop && nx daemon service start`"\n'
                       'OK = "nx daemon service stop, then nx daemon service start"\n', encoding="utf-8")
    assert len(_and_chained_remedies([planted], tmp_path)) == 1
