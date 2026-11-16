# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nothing but the ``nx backup`` verb runs a backup (nexus-f3yqd.4; Sam: NO automatic backup, ever).

What the scan CATCHES, under ``src``, ``conexus``, ``sn``, ``scripts``, ``mcpb``, ``dt``, ``prose-edit``,
``tool-spill``, ``bench``, ``infra`` and ``.github`` (``tests`` and ``docs`` are not scanned):

* Python: an import of ``nexus.backup.pg_backup`` or ``nexus.commands.backup_cmd`` (or a string naming either, for
  ``importlib``) outside the verb and ``cli.py``'s registration;
* Python: a list or tuple argv that runs the verb: ``["nx", "backup"]``, ``[nx_bin, "backup"]``,
  ``[*_resolve_nx_bin(), "backup"]``, flags before ``backup``, ``[sys.executable, "-m", "nexus", "backup"]``,
  ``["backup"]`` (``main(["backup"])``);
* Python: a string that is a command line (``nx backup``, f-strings included) handed to a subprocess call, to
  ``os.system`` and the like, to ``shlex.split``, or to anything with ``shell=True``;
* shell, batch, PowerShell, YAML and unit files: a line that starts a command ``nx backup``; JSON files
  (``hooks.json``): a string value that is such a command; Markdown: the ``!`...` `` bash-injection form of a
  slash command.

What it CANNOT catch: a command assembled at run time (a variable that holds ``"backup"``, a string built from
pieces, a file read from disk), a program in another language, and a caller outside the scanned directories.
It matches command SHAPES, not words: a suggestion in an ``echo``, a ``print`` or an error message stays possible.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

SCAN_ROOTS = (
    "src", "conexus", "sn", "scripts", "mcpb", "dt", "prose-edit", "tool-spill", "bench", "infra", ".github",
)
#: Files allowed to mention the verb however they like: the verb, its libraries (the local dump, and the cloud
#: arm's session driver and file format, nexus-csayy.3), and cli.py (which imports the verb).
_SELF = {
    "src/nexus/commands/backup_cmd.py", "src/nexus/backup/pg_backup.py",
    "src/nexus/backup/cloud_export.py", "src/nexus/backup/cloud_import.py", "src/nexus/backup/tenant_file.py",
    "src/nexus/backup/local_token.py",  # the restore arm's local-token credentials (rdr248-local-root)
}
_VERB_AND_ARM = "src/nexus/commands/backup_cmd.py"
_MODULES = {
    "nexus.backup.pg_backup": {_VERB_AND_ARM},
    "nexus.backup.cloud_export": {_VERB_AND_ARM},
    "nexus.backup.cloud_import": {_VERB_AND_ARM},
    "nexus.backup.tenant_file": {_VERB_AND_ARM},
    "nexus.commands.backup_cmd": {"src/nexus/cli.py"},
}
_SKIP_DIRS = {"__pycache__", "node_modules", ".venv", "target", ".git", "dist", "build"}
_SHELL_SUFFIXES = {".sh", ".bash", ".zsh", ".cmd", ".bat", ".ps1", ".yml", ".yaml", ".service", ".plist", ".toml"}
_MAX_BYTES = 2_000_000

#: A command line that runs the verb: ``nx backup``, ``nx.exe -q backup``, ``/usr/local/bin/nx backup``,
#: ``python -m nexus backup``, at the start of the text or after a shell separator or a ``key:`` / ``- `` of YAML.
_CMD = re.compile(
    r"(?:^|[;&|(`]|\$\(|:\s|-\s)\s*(?:exec\s+)?"
    r"(?:(?:[\w.~/\\-]*[/\\])?nx(?:\.exe)?|(?:[\w.~/\\-]*[/\\])?python3?(?:\.exe)?\s+-m\s+nexus(?:\.cli)?)"
    r"\s+(?:-\S+\s+)*backup(?:\s|$|[;&|)`\"'])"
)
_MD_INJECT = re.compile(r"!`[^`]*\bnx(?:\.exe)?\s+(?:-\S+\s+)*backup\b")
_NOT_A_COMMAND = re.compile(r"^\s*(?:#|::|rem\b|echo\b|printf\b|write-host\b|write-output\b|print\b)", re.IGNORECASE)
_SPAWNERS = {
    "run", "call", "check_call", "check_output", "Popen", "system", "popen", "getoutput", "getstatusoutput",
    "run_bounded", "create_subprocess_shell", "create_subprocess_exec", "split", "execv", "execvp", "spawn",
}
_NX_HEADS = {"nx", "nx.exe", "nexus"}
#: Calls that hand an argv list to the nx CLI itself: main(["backup"]), CliRunner().invoke(main, ["backup"]).
_CLI_CALLS = {"main", "invoke", "standalone_main"}


@dataclass
class Scan:
    found: list[str] = field(default_factory=list)
    py_files: int = 0
    hook_py_files: int = 0
    shell_files: int = 0
    json_files: int = 0
    markdown_files: int = 0


def _const(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _text_of(node: ast.AST) -> str | None:
    """The constant text of a string or f-string (a formatted value becomes a placeholder)."""
    if (c := _const(node)) is not None:
        return c
    if isinstance(node, ast.JoinedStr):
        return "".join(p.value if isinstance(p, ast.Constant) else "\x00" for p in node.values)
    return None


def _is_backup_argv(elts: list[ast.expr], *, in_call: bool) -> bool:
    """Is this list the argv of ``nx backup``? ``"backup"`` first is the verb's own argument vector
    (``main(["backup"])``, ``subprocess.run(["backup", ...])``) and counts only as an argument of a call:
    a bare ``["backup", "restore"]`` is a list of words. Otherwise the head must be ``nx``/``nexus`` or not
    a constant at all (``nx_bin``, ``*_resolve_nx_bin()``, ``sys.executable``), with only flags, the
    ``nexus`` module name or non-constants before ``backup``."""
    consts = [_const(e) for e in elts]
    if not elts:
        return False
    if consts[0] == "backup":
        return in_call
    if "backup" not in consts[1:]:
        return False
    i = consts.index("backup", 1)
    if consts[0] is not None and consts[0] not in _NX_HEADS:  # ["git", "backup"]: some other program
        return False
    return all(
        c is None or c.startswith("-") or c in _NX_HEADS or c == "conexus" for c in consts[1:i]
    )


def _python_findings(rel: str, tree: ast.AST) -> list[str]:
    found: list[str] = []
    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            refs.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            refs.add(node.module)
            refs.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, (ast.List, ast.Tuple)):
            if _is_backup_argv(node.elts, in_call=False):
                found.append(f"{rel} runs `nx backup` (argv)")
            for e in node.elts:
                if (t := _text_of(e)) is not None and _CMD.search(t):
                    found.append(f"{rel} runs `nx backup` (command string in a list)")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            refs.add(node.value)
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            shell = any(k.arg == "shell" and getattr(k.value, "value", None) is True for k in node.keywords)
            args = [*node.args, *(k.value for k in node.keywords if k.arg == "args")]
            if name in _SPAWNERS or name in _CLI_CALLS or shell:
                for a in args:  # the list handed to a call: ["backup"] is the verb's argv there
                    if isinstance(a, (ast.List, ast.Tuple)) and _is_backup_argv(a.elts, in_call=True):
                        found.append(f"{rel} runs `nx backup` (argv)")
            if (name in _SPAWNERS or shell) and node.args:
                t = _text_of(node.args[0])
                if t is not None and _CMD.search(t):
                    found.append(f"{rel} runs `nx backup` (command string)")
            if name == "split" and isinstance(node.func, ast.Attribute):  # "nx backup".split()
                t = _text_of(node.func.value)
                if t is not None and _CMD.search(t):
                    found.append(f"{rel} runs `nx backup` (command string split)")
    for module, allowed in _MODULES.items():
        if rel not in allowed and any(r == module or r.startswith(module + ".") for r in refs):
            found.append(f"{rel} imports {module}")
    return list(dict.fromkeys(found))


def _json_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _json_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _json_strings(v)]
    return []


def backup_callers(root: Path) -> Scan:
    """Scan *root* (a repository checkout, or a tree shaped like one) for anything but the verb that runs it."""
    scan = Scan()
    for top in SCAN_ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or _SKIP_DIRS & set(path.relative_to(root).parts):
                continue
            rel = path.relative_to(root).as_posix()
            try:
                too_big = path.stat().st_size > _MAX_BYTES
            except OSError:
                continue
            if rel in _SELF or too_big:
                continue
            suffix = path.suffix.lower()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if suffix == ".py":
                scan.py_files += 1
                if rel.startswith("conexus/hooks/") or rel.startswith("src/nexus/hooks/"):
                    scan.hook_py_files += 1
                try:
                    tree = ast.parse(text, filename=rel)
                except SyntaxError:
                    continue
                scan.found += _python_findings(rel, tree)
            elif suffix == ".json":
                scan.json_files += 1
                try:
                    strings = _json_strings(json.loads(text))
                except ValueError:
                    continue
                scan.found += [f"{rel} runs `nx backup` (json command)" for s in strings if _CMD.search(s)]
            elif suffix == ".md":
                scan.markdown_files += 1
                if any(_MD_INJECT.search(line) for line in text.splitlines()):
                    scan.found.append(f"{rel} runs `nx backup` (bash injection)")
            elif suffix in _SHELL_SUFFIXES or (suffix == "" and text.startswith("#!")):
                scan.shell_files += 1
                for line in text.splitlines():
                    if not _NOT_A_COMMAND.match(line) and _CMD.search(line):
                        scan.found.append(f"{rel} runs `nx backup` (command line)")
                        break
    return scan


# -- the real tree -----------------------------------------------------------------------------------


def test_nothing_but_the_cli_verb_reaches_the_backup_entry_point() -> None:
    scan = backup_callers(REPO)
    assert scan.found == []
    # non-vacuity: the scan really walked the hook scripts, the shell and JSON launchers and the code
    assert scan.py_files > 500, f"only {scan.py_files} Python files scanned"
    assert scan.hook_py_files >= 10, f"only {scan.hook_py_files} hook Python files scanned"
    assert scan.shell_files > 10 and scan.json_files > 5, (scan.shell_files, scan.json_files)


def test_the_files_the_scan_trusts_still_exist() -> None:
    assert (REPO / "src/nexus/commands/backup_cmd.py").is_file()
    assert (REPO / "src/nexus/backup/pg_backup.py").is_file()
    assert (REPO / "src/nexus/backup/cloud_export.py").is_file()
    assert (REPO / "src/nexus/backup/tenant_file.py").is_file()
    assert "backup_cmd" in (REPO / "src/nexus/cli.py").read_text(encoding="utf-8")


# -- planted callers: each is found, so the scan is not vacuous --------------------------------------

_PLANTED = [
    ("src/nexus/upgrade_ladder/x.py", "from nexus.backup.pg_backup import run_dump\n", "imports nexus.backup.pg_backup"),
    ("src/nexus/upgrade_ladder/x.py", "from nexus.backup import pg_backup\n", "imports nexus.backup.pg_backup"),
    ("src/nexus/upgrade_ladder/x.py", "import nexus.backup.pg_backup\n", "imports nexus.backup.pg_backup"),
    ("src/nexus/upgrade_ladder/x.py", "from nexus.commands.backup_cmd import backup\n", "imports nexus.commands.backup_cmd"),
    ("src/nexus/upgrade_ladder/x.py", "from nexus.backup.cloud_export import run_export\n", "imports nexus.backup.cloud_export"),
    ("src/nexus/upgrade_ladder/x.py", "from nexus.backup import cloud_export\n", "imports nexus.backup.cloud_export"),
    ("src/nexus/upgrade_ladder/x.py", "from nexus.backup.tenant_file import verify_file\n", "imports nexus.backup.tenant_file"),
    ("src/nexus/upgrade_ladder/x.py", "import importlib\nimportlib.import_module('nexus.backup.cloud_export')\n", "imports nexus.backup.cloud_export"),
    ("src/nexus/upgrade_ladder/x.py", "import importlib\nimportlib.import_module('nexus.backup.pg_backup')\n", "imports nexus.backup.pg_backup"),
    ("src/nexus/upgrade_ladder/x.py", "import subprocess\nsubprocess.run(['nx', 'backup'])\n", "(argv)"),
    ("src/nexus/upgrade_ladder/x.py", "main(['backup'])\n", "(argv)"),
    ("src/nexus/upgrade_ladder/x.py", "CliRunner().invoke(main, ['backup', '--to', p])\n", "(argv)"),
    ("src/nexus/upgrade_ladder/x.py", "subprocess.run('nx backup'.split())\n", "(command string split)"),
    ("src/nexus/upgrade_ladder/x.py", "argv = 'nx --quiet backup'.split()\n", "(command string split)"),
    ("src/nexus/upgrade_ladder/x.py", "import subprocess\nsubprocess.run([nx_bin, 'backup'])\n", "(argv)"),
    ("src/nexus/upgrade_ladder/x.py", "subprocess.run([*_resolve_nx_bin(), 'backup'], check=True)\n", "(argv)"),
    ("src/nexus/upgrade_ladder/x.py", "subprocess.run(['nx', '--quiet', 'backup'])\n", "(argv)"),
    ("src/nexus/upgrade_ladder/x.py", "subprocess.run([sys.executable, '-m', 'nexus', 'backup'])\n", "(argv)"),
    ("src/nexus/upgrade_ladder/x.py", "import subprocess\nsubprocess.run('nx backup', shell=True)\n", "(command string)"),
    ("src/nexus/upgrade_ladder/x.py", "import os\nos.system(f'nx backup --to {dest}')\n", "(command string)"),
    ("src/nexus/upgrade_ladder/x.py", "subprocess.run(['sh', '-c', 'cd /tmp && nx backup'])\n", "(command string in a list)"),
    ("conexus/hooks/scripts/fake.py", "import subprocess\nsubprocess.run(['nx', 'backup'])\n", "(argv)"),
    ("conexus/hooks/scripts/fake.py", "subprocess.run([*_resolve_nx_bin(), 'backup'])\n", "(argv)"),
    ("conexus/hooks/scripts/fake.sh", "#!/bin/sh\nset -e\nnx backup\n", "(command line)"),
    ("conexus/hooks/scripts/fake.sh", "#!/bin/sh\ncd \"$HOME\" && exec nx backup --to /tmp/x\n", "(command line)"),
    ("conexus/hooks/hooks.json", json.dumps({"hooks": {"SessionStart": [{"command": "nx backup"}]}}), "(json command)"),
    ("conexus/commands/fake.md", "Run it:\n!`nx backup`\n", "(bash injection)"),
    (".github/workflows/fake.yml", "steps:\n  - run: nx backup\n", "(command line)"),
]


@pytest.mark.parametrize(("rel", "content", "named"), _PLANTED, ids=[f"{i}-{p[0].rsplit('/', 1)[-1]}" for i, p in enumerate(_PLANTED)])
def test_a_planted_caller_is_found(rel: str, content: str, named: str, tmp_path: Path) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    scan = backup_callers(tmp_path)
    assert len(scan.found) >= 1 and all(rel in f for f in scan.found), scan.found
    assert any(named in f for f in scan.found), scan.found


@pytest.mark.parametrize(("rel", "content"), [
    ("src/nexus/health.py", "import click\nclick.echo('Run `nx backup` before you upgrade.')\n"),
    ("src/nexus/health.py", "MSG = 'nx backup'\nprint('hint: nx backup first')\n"),
    ("src/nexus/health.py", "raise SystemExit(f'take a backup: nx backup --to {p}')\n"),
    ("src/nexus/health.py", "subprocess.run(['git', 'backup'])\n"),
    ("src/nexus/health.py", "ACTIONS = ['backup', 'restore']\n"),
    ("src/nexus/health.py", "for verb in ('backup', 'restore', 'verify'):\n    print(verb)\n"),
    ("src/nexus/health.py", "WORDS = 'back up and restore'.split()\n"),
    ("conexus/hooks/scripts/ok.sh", "#!/bin/sh\necho 'nx backup is available'\n# nx backup\n"),
    ("conexus/commands/ok.md", "Suggest running `nx backup` first.\n"),
])
def test_text_that_only_suggests_the_verb_is_not_a_caller(rel: str, content: str, tmp_path: Path) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    assert backup_callers(tmp_path).found == []
