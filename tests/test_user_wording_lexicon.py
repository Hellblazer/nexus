# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wording a user reads names no internal id, person or branch (nexus-sby49.4 group B, plan section 2 ledger).

The behaviour leg (``tests/e2e/lib/plugin_behaviour.py``) holds the lexicon for what the hooks print. This file applies the
same lexicon to the text the leg cannot drive: the MCP tool descriptions that load into every session (S2-3), the help and
messages of the ``nx ask``, ``nx ci``, ``nx group`` and ``nx hooks`` commands and the hook stanza ``nx hooks install`` writes
into a user's repository (S2-6), the nexus skill's note table (S2-4), the frontmatter ``description`` of every skill, agent
and command (I4), and the writing-nx-skills ``bd update`` pitfall (S2-7).

A link into the project's public documentation (``https://github.com/Hellblazer/nexus/...``) is allowed: it is a pointer a user
can follow. A comment or a docstring that is not help text is not read here; only text a model or a person is shown.

Non-vacuity (nexus-moht0): each scan counts what it examined, and a planted id fails each one.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import click
import pytest

pytestmark = pytest.mark.lint

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "conexus"
COMMANDS = ROOT / "src" / "nexus" / "commands"


def _load_leg():
    spec = importlib.util.spec_from_file_location("plugin_behaviour_for_wording", ROOT / "tests" / "e2e" / "lib" / "plugin_behaviour.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_leg = _load_leg()

#: Names the lexicon's ``nexus-xxxx`` pattern catches that are component or marker names, not bead ids.
_COMPONENT_NAMES = frozenset({"nexus-catalog", "nexus-service", "nexus-managed", "nexus-plugins"})
_PUBLIC_DOCS_URL = re.compile(r"https://github\.com/Hellblazer/nexus/\S*")
#: nexus's own branch pair, named together as the two a command is for.
_BRANCH_PAIR = re.compile(r"\bdevelop (?:and|or) main\b|\bmain (?:and|or) develop\b|\bdevelop, main\b", re.IGNORECASE)


def hits(text: str) -> list[str]:
    """Lexicon terms in *text*, leaving out component names and public-docs links."""
    text = _PUBLIC_DOCS_URL.sub("", text)
    found = [h for h in _leg.lexicon_hits(text) if h.lower() not in _COMPONENT_NAMES]
    return found + [m.group(0) for m in _BRANCH_PAIR.finditer(text)]


# --- S2-3: the MCP tool descriptions -------------------------------------------------------------


def _tool_hits(tools) -> tuple[int, list[str]]:
    examined = 0
    found: list[str] = []
    for t in tools:
        examined += 1
        for h in hits(t.description or ""):
            found.append(f"{t.name}: {h}")
        for param, schema in ((t.parameters or {}).get("properties") or {}).items():
            for h in hits(schema.get("description") or ""):
                found.append(f"{t.name}({param}): {h}")
    return examined, found


def _registered_tools():
    for module_path in ("nexus.mcp.core", "nexus.mcp.catalog"):
        mod = importlib.import_module(module_path)
        yield from (t for _, t in sorted(mod.mcp._tool_manager._tools.items()))


def test_no_mcp_tool_description_names_an_internal_id() -> None:
    examined, found = _tool_hits(_registered_tools())
    assert examined >= 50, f"the scan saw {examined} tools"
    assert not found, "tool descriptions load into every session and name:\n" + "\n".join(found)


def test_a_planted_id_fails_the_tool_scan() -> None:
    planted = [
        SimpleNamespace(name="a", description="Reads a thing (RDR-155).", parameters={}),
        SimpleNamespace(name="b", description="ok", parameters={"properties": {"p": {"description": "see nexus-abcde"}}}),
        SimpleNamespace(name="c", description="Sam's tool", parameters={}),
        SimpleNamespace(name="d", description="ok, see https://github.com/Hellblazer/nexus/blob/main/docs/x.md", parameters={}),
    ]
    examined, found = _tool_hits(planted)
    assert examined == 4
    assert [f.split(": ")[1] for f in found] == ["RDR-155", "nexus-abcde", "Sam"]


# --- S2-6: the nx ask / ci / group / hooks surfaces ------------------------------------------------

_GROUPS = ("ask", "ci", "group", "hooks")
_FILES = ("ask.py", "ci_cmd.py", "group_cmd.py", "hooks.py")


def _help_texts(cmd: click.Command, path: str):
    yield path, cmd.help or ""
    yield path, cmd.short_help or ""
    yield path, cmd.epilog or ""
    for p in cmd.params:
        yield f"{path} {p.name}", getattr(p, "help", None) or ""
        yield f"{path} {p.name}", " ".join(getattr(p, "opts", [])) + " " + str(getattr(p, "metavar", "") or "")
    if isinstance(cmd, click.Group):
        for name, sub in cmd.commands.items():
            yield from _help_texts(sub, f"{path} {name}")


def _cli_hits(root: click.Group, groups=_GROUPS) -> tuple[int, list[str]]:
    examined = 0
    found: list[str] = []
    for g in groups:
        for where, text in _help_texts(root.commands[g], f"nx {g}"):
            examined += 1
            found.extend(f"{where}: {h}" for h in hits(text))
    return examined, found


def test_the_help_of_the_ask_ci_group_and_hooks_commands_names_no_person_branch_or_id() -> None:
    from nexus.cli import main  # noqa: PLC0415 -- the full CLI tree loads here only

    examined, found = _cli_hits(main)
    assert examined >= 100, f"the walk saw {examined} help texts"
    assert not found, "\n".join(found)


def test_a_planted_name_fails_the_cli_walk() -> None:
    @click.group()
    def root() -> None: ...

    @root.group(name="ask")
    def ask() -> None:
        """Answers for Sam on develop and main (RDR-245)."""

    @ask.command(name="go")
    @click.option("--who", help="The one nexus-abcde named.")
    def go(who: str) -> None:
        """Fine."""

    examined, found = _cli_hits(root, groups=("ask",))
    assert examined >= 4
    assert sorted(f.split(": ")[1] for f in found) == ["RDR-245", "Sam", "develop and main", "nexus-abcde"]


def _runtime_strings(path: Path):
    """Every string constant of *path* that is not a docstring (a message, a help= value, shell text written to a user's repo)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)
    }
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            yield n.lineno, n.value


def test_the_messages_and_the_hook_stanza_of_those_commands_name_no_person_branch_or_id() -> None:
    examined = 0
    found: list[str] = []
    for name in _FILES:
        for lineno, text in _runtime_strings(COMMANDS / name):
            examined += 1
            found.extend(f"{name}:{lineno}: {h}" for h in hits(text))
    assert examined >= 200, f"the scan saw {examined} strings"
    assert not found, "\n".join(found)


def test_a_planted_name_fails_the_string_scan(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text('"""Module doc names Sam and is not read."""\nMSG = "a session relaying Sam\'s words"\nOK = "plain"\n', encoding="utf-8")
    assert [h for _, t in _runtime_strings(f) for h in hits(t)] == ["Sam"]


# --- S2-4, I4, S2-7: the plugin's markdown ---------------------------------------------------------


def _frontmatter_description(text: str) -> str | None:
    """The ``description:`` value of a file's frontmatter, folded scalars included (a block ``yaml.safe_load`` refuses in
    one command file is still read here)."""
    m = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return None
    out: list[str] | None = None
    for line in m.group(1).splitlines():
        if out is None:
            if line.startswith("description:"):
                out = [line[len("description:"):]]
        elif re.match(r"[A-Za-z][\w-]*:", line):
            break
        else:
            out.append(line)
    return None if out is None else " ".join(s.strip() for s in out).strip()


def _plugin_files() -> list[Path]:
    return sorted([*PLUGIN.glob("skills/*/SKILL.md"), *PLUGIN.glob("agents/*.md"), *PLUGIN.glob("commands/*.md")])


def _description_hits(files: list[Path]) -> tuple[int, list[str]]:
    described = 0
    found: list[str] = []
    for f in files:
        desc = _frontmatter_description(f.read_text(encoding="utf-8"))
        if desc is None:
            continue
        described += 1
        found.extend(f"{f.parent.name}/{f.name}: {h}" for h in hits(desc))
    return described, found


def test_no_skill_agent_or_command_description_names_an_internal_id_or_person() -> None:
    described, found = _description_hits(_plugin_files())
    assert described >= 60, f"the scan read {described} descriptions"
    assert not found, "descriptions load into every session and name:\n" + "\n".join(found)


def test_a_planted_id_fails_the_description_scan(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: x\ndescription: >\n  Use when asked (RDR-245) by Sam,\n  see nexus-abcde.\nallowed-tools: Bash\n---\nbody\n", encoding="utf-8")
    described, found = _description_hits([skill])
    assert described == 1
    assert sorted(f.split(": ")[1] for f in found) == ["RDR-245", "Sam", "nexus-abcde"]


def test_the_nexus_skill_names_no_person_or_id() -> None:
    text = (PLUGIN / "skills" / "nexus" / "SKILL.md").read_text(encoding="utf-8")
    assert "## Where a note goes" in text
    assert not hits(text), hits(text)


def test_the_bd_update_pitfall_applies_only_where_beads_exist_and_says_what_else_to_do() -> None:
    text = (PLUGIN / "skills" / "writing-nx-skills" / "SKILL.md").read_text(encoding="utf-8")
    section = text.split("## Known Pitfalls", 1)[1].split("\n## ", 1)[0]
    first_command = section.index("`bd update")
    gate = section[:first_command]
    assert ".beads/" in gate, "the section must name its data gate before its first bd command"
    assert re.search(r"\b(?:skip|hand-back|memory_put)\b", gate, re.IGNORECASE), "the gate must say what to do without .beads/"
