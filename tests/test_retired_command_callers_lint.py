# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nothing may INVOKE an `nx` command that can only refuse.

Origin (nexus-01, RDR-215 bead .13, 2026-09-19). ``nx catalog sync`` has
raised unconditionally since conexus 7.0.0. A Stop hook called it anyway,
output discarded, ``|| true``, so it failed silently at every session
close. A whole bead's deliverable — moving that call onto a daemon thread —
was moving a call that cannot succeed, and the careful "a killed thread
costs a deferred sync, never a corrupt one, because the work is idempotent"
analysis written to justify it was rigorous reasoning about a scenario that
cannot occur.

WHY A GATE RATHER THAN MORE CARE. The repo already held the fact. It
contained, at the same time, ``tests/test_catalog_cli.py::TestSyncPullRetired``
asserting the command refuses AND a live hook invoking it as though it
worked. Somebody DID check; the check and the caller never met, because
nothing connected "this command is retired" to "these callers exist". That
is the mechanizable half of the class (nexus-c3): the fact is already in the
code in checkable form, so what is missing is a connection, not diligence.
The unmechanizable half — a measurement taken against a build where the
feature never ran, which no artifact records — is ``nexus-0hqez``'s
territory and no gate reaches it.

THE RETIRED SET IS DERIVED, NEVER LISTED. A hand-kept list is the
seven-enumerations problem: the next retirement lands and nobody adds the
row. The predicate is structural — a command function whose body, ignoring
its docstring, is exactly one unconditional ``raise`` of a click error. Note
what is NOT used: ``hidden=True`` (43 commands carry it, most are live) and
"is retired" in prose (it appears in help text and in error messages that
merely mention a retirement).

TWO THINGS THE FIRST DRAFT OF THIS FILE GOT WRONG, both found by running it:

1. It matched only ``ClickException``. ``aspects gc``, ``gc-fixtures``,
   ``backfill-source-uri``, ``gc-pre-rdr096`` and
   ``taxonomy backfill-source-collection`` raise ``UsageError`` — a
   ClickException subclass — and were invisible. A gate that covers a third
   of its class while reading as complete is worse than the habit it
   replaces, so the predicate now takes any click error.
2. It derived the invocation by regexing the exception MESSAGE for a quoted
   ``'nx ...'``. catalog.py writes that shape; aspects.py writes
   ``"aspects gc is retired: ..."`` with no ``nx`` and no quotes. Deriving
   from the DECORATOR instead has no message-format dependency at all.

NAMING IS NOT INVOKING, and the distinction is enforced structurally rather
than by an allowlist. Documentation, comments and error messages mention
these commands constantly and legitimately — a first pass flagged eight such
lines and one real caller. An allowlist of prose would grow forever and red
CI on every new doc sentence, so instead a hit must be in COMMAND POSITION
in a shell file, or an argv sequence / leading string in Python. That rule
cannot rot the way a list of exceptions does.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMANDS_ROOT = REPO_ROOT / "src" / "nexus" / "commands"

CALLER_SCOPES = ("src", "conexus/hooks/scripts", "scripts", "tests/e2e")
CALLER_SUFFIXES = (".py", ".sh", ".bash", ".zsh")

#: Click error classes. ``UsageError`` and ``BadParameter`` subclass
#: ``ClickException``; all three mean the same thing in a command body that
#: does nothing else.
_CLICK_ERRORS = {"ClickException", "UsageError", "BadParameter"}

#: The derivation must not quietly return nothing (nexus-moht0: a sweep that
#: found nothing to check is a failure, not a pass). Ten exist today; the
#: floor sits below that so ordinary un-retirement does not trip it, and well
#: above zero so a collapse does.
MIN_RETIRED_COMMANDS = 7

#: ``"<invocation>": "<why>"``. Empty by design — the command-position rule
#: below is meant to make prose entries unnecessary. An entry here means the
#: DETECTOR was wrong, so say which line and why rather than just silencing.
ALLOWED_CALLERS: dict[str, str] = {}


def _decorator_command_name(dec: ast.expr) -> tuple[str, str] | None:
    """``(group_var, command_name)`` for ``@group.command("name")``."""
    if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
        return None
    if dec.func.attr != "command" or not isinstance(dec.func.value, ast.Name):
        return None
    # Positional OR ``name=``. 27 commands in this tree use the keyword
    # form, including every retired one in aspects.py, so reading only
    # ``dec.args`` silently drops most of the class -- which is exactly
    # what the first version of this file did.
    for arg in dec.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return dec.func.value.id, arg.value
    for kw in dec.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
            return dec.func.value.id, kw.value.value
    return None


def _group_cli_names(tree: ast.Module) -> dict[str, str]:
    """``{python_var: cli_name}`` for click groups defined in this module."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if not isinstance(target, ast.Attribute) or target.attr != "group":
                continue
            explicit = next(
                (a.value for a in getattr(dec, "args", [])
                 if isinstance(a, ast.Constant) and isinstance(a.value, str)),
                None,
            ) or next(
                (k.value.value for k in getattr(dec, "keywords", [])
                 if k.arg == "name" and isinstance(k.value, ast.Constant)),
                None,
            )
            names[node.name] = explicit or node.name.replace("_", "-")
    return names


def _refuses_unconditionally(fn: ast.FunctionDef) -> bool:
    """True when *fn*'s whole body is one ``raise <ClickError>(...)``.

    "Whole body" is the point: a command that raises behind a guard is a
    live command, not a retired one, and must never be swept in.
    """
    body = list(fn.body)
    if (
        body and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Raise):
        return False
    exc = body[0].exc
    if not isinstance(exc, ast.Call):
        return False
    name = exc.func.attr if isinstance(exc.func, ast.Attribute) else getattr(exc.func, "id", "")
    return name in _CLICK_ERRORS


def _retired_commands() -> dict[str, str]:
    """``{"nx <group> <name>": "file:line"}``, derived from decorators."""
    found: dict[str, str] = {}
    for path in sorted(COMMANDS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        groups = _group_cli_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not _refuses_unconditionally(node):
                continue
            for dec in node.decorator_list:
                pair = _decorator_command_name(dec)
                if pair is None:
                    continue
                group_var, cmd = pair
                group = groups.get(group_var, group_var.replace("_cmds", "").replace("_", "-"))
                found[f"nx {group} {cmd}"] = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
    return found


def _shell_invocations(text: str, invocation: str) -> list[int]:
    """Line numbers where *invocation* sits in COMMAND POSITION.

    Command position means the start of a statement, allowing the usual
    lead-ins (``&&``, ``||``, ``;``, ``|``, ``$(``, ``if``, ``then``, ``do``,
    ``sudo``). A mention inside a comment or mid-sentence is not a call.
    """
    words = re.escape(invocation).replace(r"\ ", r"\s+")
    pattern = re.compile(
        r"(?:^|[;&|(]|&&|\|\||\$\(|\b(?:if|then|else|do|sudo|exec|time)\s+)"
        r"\s*" + words + r"\b"
    )
    hits = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = re.sub(r"(^|\s)#.*$", "", raw)  # strip comments
        if pattern.search(line):
            hits.append(lineno)
    return hits


def _python_invocations(text: str, invocation: str, path: Path) -> list[int]:
    """Line numbers where *invocation* is an argv sequence or a leading string.

    Prose inside an error message has the command mid-string, so it is not a
    call. ``["nx", "catalog", "sync"]`` and ``"nx catalog sync -m ..."`` are.
    """
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            parts = [
                e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
            if parts and " ".join(parts).startswith(invocation):
                hits.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.strip().startswith(invocation):
                hits.append(node.lineno)
    return hits


def _caller_files() -> list[Path]:
    out: list[Path] = []
    for scope in CALLER_SCOPES:
        root = REPO_ROOT / scope
        if root.is_dir():
            out.extend(p for p in root.rglob("*") if p.is_file() and p.suffix in CALLER_SUFFIXES)
    return sorted(out)


@pytest.mark.lint
def test_the_retired_set_is_derivable_and_non_empty() -> None:
    """Non-vacuity: an empty derivation would pass every other test here."""
    retired = _retired_commands()
    assert len(retired) >= MIN_RETIRED_COMMANDS, (
        f"derived only {len(retired)} retired command(s) ({sorted(retired)}), "
        f"expected at least {MIN_RETIRED_COMMANDS}. Either retirement is now "
        "expressed some other way, in which case this lint is silently "
        "checking nothing and _refuses_unconditionally() must be updated, or "
        "that many were genuinely un-retired, in which case lower "
        "MIN_RETIRED_COMMANDS in the same diff."
    )
    # The case this lint exists for, and one from the OTHER exception class,
    # so a regression to ClickException-only is caught here rather than by
    # the scan quietly examining a smaller set.
    assert "nx catalog sync" in retired, sorted(retired)
    assert "nx aspects gc" in retired, sorted(retired)


@pytest.mark.lint
def test_the_scan_examines_something() -> None:
    files = _caller_files()
    assert len(files) >= 200, (
        f"only {len(files)} candidate caller file(s) under {CALLER_SCOPES}; "
        "the scopes are wrong or the repo moved."
    )


@pytest.mark.lint
def test_the_detector_tells_naming_from_invoking() -> None:
    """The distinction the whole lint rests on, pinned on both sides.

    Without this, a detector that flagged every mention would still pass the
    scan test above while making the gate unusable, and one that flagged
    nothing would pass it while checking nothing.
    """
    assert _shell_invocations('nx catalog sync -m "x" || true\n', "nx catalog sync") == [1]
    assert _shell_invocations('  if nx catalog sync; then\n', "nx catalog sync") == [1]
    assert _shell_invocations('# was `nx catalog sync`, retired\n', "nx catalog sync") == []
    assert _shell_invocations('echo "run nx catalog sync yourself"\n', "nx catalog sync") == []

    p = Path("x.py")
    assert _python_invocations('run(["nx", "catalog", "sync", "-m", "x"])', "nx catalog sync", p)
    assert not _python_invocations(
        'raise ClickException("--repo needs the catalog (nx catalog setup)")',
        "nx catalog setup", p,
    )


@pytest.mark.lint
def test_nothing_invokes_a_retired_command() -> None:
    retired = _retired_commands()
    offenders: list[str] = []

    for path in _caller_files():
        rel = str(path.relative_to(REPO_ROOT))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for invocation, defined_at in retired.items():
            if rel == defined_at.split(":")[0] or invocation in ALLOWED_CALLERS:
                continue
            lines = (
                _python_invocations(text, invocation, path)
                if path.suffix == ".py"
                else _shell_invocations(text, invocation)
            )
            offenders.extend(f"{rel}:{n}  invokes '{invocation}'" for n in lines)

    assert not offenders, (
        "These invoke a command whose body is an unconditional refusal, so "
        "the call cannot succeed:\n  "
        + "\n  ".join(sorted(offenders))
        + "\n\nRemove the call. Merely NAMING one of these — in a comment, a "
        "docstring or an error message — is fine and is not flagged; if one "
        "of the lines above only names it, the DETECTOR is wrong, so fix "
        "_shell_invocations/_python_invocations rather than adding an "
        "ALLOWED_CALLERS entry.\nRetired commands:\n  "
        + "\n  ".join(f"{k}  ({v})" for k, v in sorted(retired.items()))
    )
