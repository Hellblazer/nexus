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
_COMPONENT_NAMES = frozenset({"nexus-catalog", "nexus-service", "nexus-managed", "nexus-plugins", "nexus-repo", "nexus-backups", "nexus-continuation"})
_PUBLIC_DOCS_URL = re.compile(r"(?:https://)?github\.com/Hellblazer/nexus\S*")
#: The maintainer's private memory files (``feedback_<topic>``), which no user has: a skill that cites one points nowhere.
_PRIVATE_MEMORY_FILE = re.compile(r"\bfeedback_[a-z0-9_]+(?:\.md)?", re.IGNORECASE)
#: nexus's own branch pair, named together as the two a command is for.
_BRANCH_PAIR = re.compile(r"\bdevelop (?:and|or) main\b|\bmain (?:and|or) develop\b|\bdevelop, main\b", re.IGNORECASE)


def hits(text: str) -> list[str]:
    """Lexicon terms in *text*, leaving out component names and public-docs links."""
    text = _PUBLIC_DOCS_URL.sub("", text)
    found = [h for h in _leg.lexicon_hits(text) if h.lower() not in _COMPONENT_NAMES]
    return found + [m.group(0) for m in _BRANCH_PAIR.finditer(text)] + [m.group(0) for m in _PRIVATE_MEMORY_FILE.finditer(text)]


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


def _cli_hits(root: click.Group, excluded: dict[str, str] | None = None) -> tuple[int, list[str]]:
    """Walk every command of *root* (hidden ones included): help, short help, epilog, every option's help and spelling."""
    excluded = excluded or {}
    examined = 0
    found: list[str] = []
    own = [("nx", root.help or ""), ("nx", root.epilog or "")] + [(f"nx {p.name}", getattr(p, "help", None) or "") for p in root.params]
    for where, text in own:
        examined += 1
        found.extend(f"{where}: {h}" for h in hits(text))
    for name, cmd in sorted(root.commands.items()):
        if name in excluded:
            continue
        for where, text in _help_texts(cmd, f"nx {name}"):
            examined += 1
            found.extend(f"{where}: {h}" for h in hits(text))
    return examined, found


def test_no_nx_command_help_names_a_person_branch_or_id() -> None:
    from nexus.cli import main  # noqa: PLC0415 -- the full CLI tree loads here only

    examined, found = _cli_hits(main)
    assert examined >= 2500, f"the walk saw {examined} help texts"
    assert not found, "\n".join(found)


#: Words that open a sentence a reader expects capitalised; one lowercase after a full stop is a sentence that lost its subject.
_SENTENCE_OPENERS = r"(?:the|a|an|it|its|this|these|those|every|each|both|all|added|adds|completes|migrates|form|pre-this-verb)"


def _help_problems(text: str, *, command_help: bool) -> list[str]:
    """Punctuation faults a person sees in rendered help: a space before a full stop or comma, leading punctuation,
    unbalanced parentheses, a sentence (or, for a command's own help, a paragraph) that starts lowercase."""
    text = text.replace("\b", "")
    out = []
    if re.search(r"\S [.,](?![.\w/])", text):
        out.append("a space before a full stop or comma")
    if re.match(r"\s*[.,;:)]", text):
        out.append("starts with punctuation")
    if text.count("(") != text.count(")"):
        out.append("unbalanced parentheses")
    if re.search(rf"(?<!e\.g)(?<!i\.e)[.!?]\s+{_SENTENCE_OPENERS}\b", text):
        out.append("a sentence starts lowercase")
    if command_help:
        for paragraph in re.split(r"\n[ \t]*\n", text.strip("\n")):
            if paragraph[:1].isspace():
                continue  # an indented block is a table or an example, not prose
            word = re.match(r"[a-z]+(?=[\s:,.]|$)", paragraph)
            if word and not paragraph.startswith(("nx ", "e.g", "i.e")):
                out.append(f"a paragraph starts lowercase: {paragraph[:30]!r}")
    return out


def _help_faults(cmd: click.Command, path: str):
    for text in (cmd.help, cmd.short_help, cmd.epilog):
        if text:
            for fault in _help_problems(text, command_help=text is cmd.help):
                yield f"{path}: {fault}"
    for p in cmd.params:
        if getattr(p, "help", None):
            for fault in _help_problems(p.help, command_help=False):
                yield f"{path} {p.name}: {fault}"
    if isinstance(cmd, click.Group):
        for name, sub in cmd.commands.items():
            yield from _help_faults(sub, f"{path} {name}")


def test_no_nx_help_text_has_a_punctuation_or_capitalisation_fault() -> None:
    from nexus.cli import main  # noqa: PLC0415

    faults = list(_help_faults(main, "nx"))
    assert not faults, "\n".join(faults)


def test_the_help_fault_check_catches_each_class_it_names() -> None:
    cases = {
        "Do not install the file this run . Explicit decline.": "a space before a full stop or comma",
        ". Explicit decline of the prompt.": "starts with punctuation",
        "Unpaged by default (every matching subspace.": "unbalanced parentheses",
        "Reads the table. the engine fills the rest.": "a sentence starts lowercase",
    }
    for text, fault in cases.items():
        assert fault in _help_problems(text, command_help=False), text
    assert any("a paragraph starts lowercase" in f for f in _help_problems("Fine.\n\nthe service starts.", command_help=True))
    assert _help_problems("Fine (e.g. a chunker). Costs (t3_gc, ...).", command_help=True) == []


def test_the_cli_walk_reads_answer_plan_and_its_hidden_nx_plan_alias() -> None:
    """Group C renamed ``nx plan`` to ``nx answer-plan`` and kept the old group as a hidden alias; both are walked, none excluded."""
    from nexus.cli import main  # noqa: PLC0415

    assert "answer-plan" in main.commands and "plan" in main.commands
    assert main.commands["plan"].hidden, "nx plan is a hidden deprecated alias"
    seen = {where.split()[1] for where, _ in (t for name in ("answer-plan", "plan") for t in _help_texts(main.commands[name], f"nx {name}"))}
    assert seen == {"answer-plan", "plan"}


def test_a_planted_name_fails_the_cli_walk() -> None:
    @click.group()
    def root() -> None: ...

    @root.group(name="deep")
    def deep() -> None:
        """Answers for Sam on develop and main (RDR-245)."""

    @deep.group(name="deeper")
    def deeper() -> None:
        """Fine."""

    @deeper.command(name="go")
    @click.option("--who", help="The one nexus-abcde named.")
    def go(who: str) -> None:
        """Fine."""

    @root.group(name="skipped")
    def skipped() -> None:
        """Names RDR-1 and is excluded."""

    examined, found = _cli_hits(root, excluded={"skipped": "test"})
    assert examined >= 8
    assert sorted(f.split(": ")[1] for f in found) == ["RDR-245", "Sam", "develop and main", "nexus-abcde"]
    assert any(f.startswith("nx deep deeper go who") for f in found), "a nested option is walked"


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


# --- Fix round 1: every string a command, the MCP server or a hook prints, and the plan library's text ---------------------

_LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log", "bind"})

#: Lexicon terms the CLI's own runtime strings may carry, each with its reason.
_CLI_VOCABULARY: dict[str, str] = {
    "bd": "`nx command-context` reports whether the beads tracker is installed; naming it there is the report",
}
#: Wording a hook's text keeps although it names a person. Empty since RDR-259 S6: the ask-answer frame says "the user"
#: and fences the words with ``user-words``.
_PINNED_FRAME_LITERALS: tuple[str, ...] = ()


def _visible_strings(path: Path):
    """``(line, text)`` for each string constant of *path* a person can read: not a docstring, not the argument of a log call,
    and not a bare token (an identifier, a regular expression, a route name has no space)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body \
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
            skip.add(id(n.body[0].value))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _LOG_METHODS:
            skip.update(id(a) for a in ast.walk(n) if isinstance(a, ast.Constant))
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip and re.search(r"\s", n.value.strip()):
            yield n.lineno, n.value


def _string_hits(files: list[Path], root: Path, exempt: dict[str, str] | None = None,
                 literals: tuple[str, ...] = ()) -> tuple[int, list[str]]:
    examined = 0
    found: list[str] = []
    for f in files:
        rel = f.relative_to(root).as_posix()
        for lineno, text in _visible_strings(f):
            examined += 1
            for literal in literals:
                text = text.replace(literal, "")
            found.extend(f"{rel}:{lineno}: {h}" for h in hits(text) if h.lower() not in (exempt or {}))
    return examined, found


def _command_modules() -> list[Path]:
    return sorted(COMMANDS.rglob("*.py"))


def test_no_command_runtime_string_names_an_internal_id_or_person() -> None:
    """click.echo text, raised error messages and ``nx doctor`` rows, in every command module (the four modules of S2-6 are
    read more closely above)."""
    files = _command_modules()
    examined, found = _string_hits(files, ROOT / "src" / "nexus", _CLI_VOCABULARY)
    assert len(files) >= 80 and examined >= 5000, f"the scan read {len(files)} modules, {examined} strings"
    assert not found, "a command prints:\n" + "\n".join(found)


def test_no_mcp_runtime_string_names_an_internal_id_or_person() -> None:
    """Error text a tool returns to a session (the docstrings are the tool descriptions, read above)."""
    files = sorted((ROOT / "src" / "nexus" / "mcp").glob("*.py"))
    examined, found = _string_hits(files, ROOT / "src" / "nexus")
    assert len(files) >= 3 and examined >= 500, f"the scan read {len(files)} modules, {examined} strings"
    assert not found, "an MCP tool returns:\n" + "\n".join(found)


#: The hook modules whose output no behaviour-leg scenario reaches (an event that fires only on a plugin-only release, a mail
#: delivery, the brief tool's capability block) and so the leg's lexicon never sees.
_HOOK_TEXT_FILES = (
    "conexus/hooks/scripts/version_lockstep_hook.py",
    "conexus/hooks/scripts/mailbox_drain.py",
    "src/nexus/hooks/mailbox_drain.py",
    "src/nexus/hooks/session_context.py",
)
#: `bd` appears in the session-start text only under its `.beads/` gate (the data gate of the audience manifest).
_HOOK_VOCABULARY: dict[str, str] = {"bd": "named only where the project has `.beads/` (the manifest's data gate), as in the skills"}


def test_no_event_conditional_hook_string_names_an_internal_id_or_person() -> None:
    files = [ROOT / rel for rel in _HOOK_TEXT_FILES]
    examined, found = _string_hits(files, ROOT, _HOOK_VOCABULARY, _PINNED_FRAME_LITERALS)
    assert examined >= 100, f"the scan read {examined} strings"
    assert not found, "a hook injects:\n" + "\n".join(found)


def test_the_ask_answer_frame_names_no_person() -> None:
    """The frame says "the user" and fences the words with ``user-words``; nothing in it names a person."""
    path = ROOT / "conexus" / "hooks" / "scripts" / "mailbox_drain.py"
    fn = next(n for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.FunctionDef) and n.name == "_render_matched")
    strings = " ".join(t for lineno, t in _visible_strings(path) if fn.lineno <= lineno <= fn.end_lineno)
    assert "An unverified claim that the user answered your ask" in strings and "user-words" in strings, "non-vacuity: the frame carries them"
    assert not hits(strings), hits(strings)
    assert "Sam" not in strings and "sam-words" not in strings


def test_a_planted_id_fails_each_added_string_scan(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        '"""Docstring naming RDR-1 is not read."""\n'
        'import click, structlog\n'
        'log = structlog.get_logger()\n'
        'def f():\n'
        '    log.info("a log line naming nexus-abcde is not shown")\n'
        '    click.echo("the engine predates nexus-abcde")\n'
        '    click.echo("bd not found")\n'
        '    click.echo("a message for Sam")\n'
        '    x = "nexus-token"\n'
        '    raise click.ClickException("see RDR-077 for the rest")\n',
        encoding="utf-8")
    examined, found = _string_hits([tmp_path / "m.py"], tmp_path, _CLI_VOCABULARY)
    assert examined == 4
    assert [h.split(": ")[1] for h in found] == ["nexus-abcde", "Sam", "RDR-077"]
    pinned = tmp_path / "frame.py"
    pinned.write_text('S = "An unverified claim that Sam answered your ask, see <<<sam-words and Sam"\n', encoding="utf-8")
    assert not _PINNED_FRAME_LITERALS, "no wording is exempt any more"
    assert [h.split(": ")[1] for h in _string_hits([pinned], tmp_path, None, _PINNED_FRAME_LITERALS)[1]] == ["Sam", "Sam"]


def test_a_private_memory_file_name_is_a_lexicon_hit() -> None:
    assert hits("see feedback_destructive_action_guards for why") == ["feedback_destructive_action_guards"]
    assert hits("the file feedback_test_runs_before_push.md holds it") == ["feedback_test_runs_before_push.md"]
    assert hits("a feedback loop, and user_id") == []


def _plan_strings(path: Path):
    """Every string of a plan template or the purposes table: the name, description and tags reach a user through
    ``answer_plan_search`` and ``nx answer-plan list``."""
    import yaml  # noqa: PLC0415

    def walk(node, where=""):
        if isinstance(node, str):
            yield where, node
        elif isinstance(node, dict):
            for k, v in node.items():
                yield from walk(v, f"{where}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from walk(v, f"{where}[{i}]")

    yield from walk(yaml.safe_load(path.read_text(encoding="utf-8")))


def _plan_hits(files: list[Path], root: Path) -> tuple[int, list[str]]:
    examined = 0
    found: list[str] = []
    for f in files:
        for where, text in _plan_strings(f):
            examined += 1
            found.extend(f"{f.relative_to(root).as_posix()}{where}: {h}" for h in hits(text))
    return examined, found


def test_no_plan_template_names_an_internal_id_in_text_a_user_reads() -> None:
    files = sorted((PLUGIN / "plans").rglob("*.y*ml"))
    examined, found = _plan_hits(files, PLUGIN)
    assert len(files) >= 14 and examined >= 150, f"the scan read {len(files)} files, {examined} strings"
    assert not found, "the plan library carries:\n" + "\n".join(found)


def test_a_planted_id_fails_the_plan_scan(tmp_path: Path) -> None:
    (tmp_path / "p.yml").write_text("name: x\ntags: builtin-template,rdr-078,nexus-abcde,analyze\nplan_json:\n  steps: [{tool: search}]\n", encoding="utf-8")
    examined, found = _plan_hits([tmp_path / "p.yml"], tmp_path)
    assert examined >= 3
    assert [h.split(": ")[1] for h in found] == ["rdr-078", "nexus-abcde"]


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


#: Lexicon terms the dispatch-process text uses as ordinary vocabulary, each with its reason. Skills, agent and command bodies
#: and the shared resources are scanned for ids, people and hosts, not for these.
_PROCESS_VOCABULARY: dict[str, str] = {
    "bd": "the beads tracker command, named only under its `.beads/` gate (the text says what to do without one)",
    "orchestrator": "the role that dispatches agents and reads their hand-backs, a process term",
}

#: Literals a user types or reads that the lexicon would flag: the `nx doctor` row label and a `--kind` value. Removed
#: from a line before the scan; renaming either is a product change, not wording.
_PRODUCT_LITERALS = ("RDR-229 completion", "--kind labrun")


def _skill_files() -> list[Path]:
    return sorted(p for p in (PLUGIN / "skills").rglob("*") if p.is_file())


def _skill_hits(files: list[Path], root: Path, owned: dict[tuple[str, str], str] | None = None) -> tuple[int, list[str]]:
    """Every line of every file under the skills directory (SKILL.md, reference.md and the rest) that a session can load."""
    lines = 0
    found: list[str] = []
    for f in files:
        rel = f.relative_to(root).as_posix()
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            lines += 1
            if any(rel == r and line.startswith(prefix) for (r, prefix) in (owned or {})):
                continue
            for literal in _PRODUCT_LITERALS:
                line = line.replace(literal, "")
            found.extend(f"{rel}:{n}: {h}" for h in hits(line) if h.lower() not in _PROCESS_VOCABULARY)
    return lines, found


def test_no_skill_file_names_an_internal_id_or_person() -> None:
    files = _skill_files()
    lines, found = _skill_hits(files, PLUGIN / "skills")
    assert len(files) >= 40 and lines >= 4000, f"the scan read {len(files)} files, {lines} lines"
    assert not found, "skill text loads into a user's session and names:\n" + "\n".join(found)


def _agent_files() -> list[Path]:
    return sorted((PLUGIN / "agents").glob("*.md"))


def _command_files() -> list[Path]:
    return sorted((PLUGIN / "commands").glob("*.md"))


def _resource_files() -> list[Path]:
    """Everything under ``conexus/resources`` (the shared relay and protocol files every subagent is pointed at, and the RDR
    templates)."""
    return sorted(p for p in (PLUGIN / "resources").rglob("*") if p.is_file())


def test_no_agent_body_names_an_internal_id_or_person() -> None:
    files = _agent_files()
    lines, found = _skill_hits(files, PLUGIN)
    assert len(files) == 10 and lines >= 3000, f"the scan read {len(files)} files, {lines} lines"
    assert not found, "agent text loads into every dispatch and names:\n" + "\n".join(found)


def test_no_command_body_names_an_internal_id_or_person() -> None:
    files = _command_files()
    lines, found = _skill_hits(files, PLUGIN)
    assert len(files) >= 18 and lines >= 700, f"the scan read {len(files)} files, {lines} lines"
    assert not found, "command text loads into a user's session and names:\n" + "\n".join(found)


def test_no_shared_resource_names_an_internal_id_or_person() -> None:
    files = _resource_files()
    lines, found = _skill_hits(files, PLUGIN)
    assert len(files) >= 12 and lines >= 1700, f"the scan read {len(files)} files, {lines} lines"
    assert not found, "resource text is injected into every subagent and names:\n" + "\n".join(found)


@pytest.mark.parametrize("surface", ["agents", "commands", "resources"])
def test_a_planted_id_in_a_body_fails_each_added_scan(tmp_path: Path, surface: str) -> None:
    """Each added surface reads a body line, not only a frontmatter description: the id sits on the last line of the file."""
    (tmp_path / surface).mkdir()
    f = tmp_path / surface / "x.md"
    f.write_text("---\nname: x\ndescription: fine\n---\nuse bd under .beads/ for the orchestrator\nthe record (RDR-080) and nexus-9clx, ask Sam\n", encoding="utf-8")
    lines, found = _skill_hits([f], tmp_path)
    assert lines == 6
    assert [h.split(": ")[1] for h in found] == ["RDR-080", "nexus-9clx", "Sam"]


def test_the_scanned_surfaces_are_the_ones_the_plugin_ships() -> None:
    """A new top-level directory of loadable text under conexus/ must be scanned or named here with its reason."""
    scanned = {"agents", "commands", "skills", "resources"}
    unread = {
        "hooks": "its injected text is read by the behaviour leg, which drives the scripts",
        "launch": "launcher code, no prose a session loads",
        "plans": "read by the plan-library scan above: a template's name, description and tags reach users through answer_plan_search",
        "daemon": "service unit templates",
        "registry.yaml": "a machine table",
        "mcp": "the plugin's MCP server declaration (a command and arguments), no prose",
        "evals": "plugin evaluation cases, run by the maintainer's harness, never loaded into a session",
        "retrieval-agents.txt": "a registry file nothing reads (its own header says so)",
    }
    present = {p.name for p in PLUGIN.iterdir() if not p.name.startswith(".")}
    extra = sorted(present - scanned - set(unread) - {"README.md", "CHANGELOG.md", "PENDING_RELEASE.md", "LICENSE", "docs"})
    assert not extra, f"conexus/ holds {extra}: scan it in this file or name it in `unread` with the reason"


def test_a_planted_id_fails_the_skill_scan(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "reference.md").write_text(
        "fine\nwalks the graph (RDR-078 P3)\nask Sam\nrun bd ready for the orchestrator\nsee nexus-umm29\n", encoding="utf-8")
    lines, found = _skill_hits([tmp_path / "x" / "reference.md"], tmp_path, owned=None)
    assert lines == 5
    assert [f.split(": ")[1] for f in found] == ["RDR-078", "Sam", "nexus-umm29"]
    owned = {("x/reference.md", "walks the graph"): "test"}
    assert [f.split(": ")[1] for f in _skill_hits([tmp_path / "x" / "reference.md"], tmp_path, owned=owned)[1]] == ["Sam", "nexus-umm29"]


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


def test_no_table_exempts_a_persons_name() -> None:
    """RDR-259 Stage 2 ship-when: the four tables exempt generic terms and two product literals, none of them a name
    (a person's name is what ``LEXICON_CS`` matches), and ``_PINNED_FRAME_LITERALS`` is empty since the frame's S6 change."""
    others = {
        "_CLI_VOCABULARY": list(_CLI_VOCABULARY),
        "_HOOK_VOCABULARY": list(_HOOK_VOCABULARY),
        "_PROCESS_VOCABULARY": list(_PROCESS_VOCABULARY),
        "_PRODUCT_LITERALS": list(_PRODUCT_LITERALS),
    }
    assert sum(len(v) for v in others.values()) >= 6, "the check read the four tables"
    named = {t: [e for e in entries if _leg.LEXICON_CS.search(e)] for t, entries in others.items()}
    assert not any(named.values()), named
    assert _PINNED_FRAME_LITERALS == ()
