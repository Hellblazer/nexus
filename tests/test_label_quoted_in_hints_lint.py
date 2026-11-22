# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ratchet (nexus-88aj0): a remedy hint that prints an ``nx`` command with a collection label in it quotes the label.

Since RDR-229 P3.6 a label may hold a space, ``;``, ``%``, ``:`` and non-ASCII letters, so ``nx collection delete
{name}`` printed bare is a command that splits, or runs something else, when pasted. Every such site passes the label
through :func:`shlex.quote`, as ``nexus.quarantine`` already did; a ``.format`` template names the placeholder
``quoted_<x>`` and its caller fills it with ``shlex.quote``.

The scan reads the AST, not lines: Python joins adjacent string literals into one node, so a command split across two
literals (``"... nx collection " "info {name}"``) is one string here. Each f-string is rebuilt as a template with
``{<expr>}`` for each interpolation, and each ``nx <verb>`` command in it is tokenised: every positional argument of a
label verb, and the value of ``--collection`` or ``-c`` after any verb, must be quoted. A label in plain prose
(``collection {name!r}``) is not a command and is not watched.
"""
from __future__ import annotations

import ast
import pathlib
import re
import shlex

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The verbs whose positional arguments are collection labels.
_LABEL_VERBS: tuple[str, ...] = (
    "nx collection delete", "nx collection info", "nx collection reindex", "nx collection rename",
    "nx collection re-embed", "nx taxonomy project", "nx taxonomy reset", "nx enrich aspects",
)
#: Flags whose value is a collection label, after any verb.
_LABEL_FLAGS: frozenset[str] = frozenset({"--collection", "-c"})
#: Where a printed command ends inside its sentence.
_COMMAND_END = re.compile(r"[`'\")\n;]| {2}|, | \(")
_PLACEHOLDER = re.compile(r"^\{(.+)\}$")

#: Sites left for a series that is editing the same file now: "<path>" -> ((exact command text, count), ...). Empty;
#: an entry must match its count exactly, so a stale one fails.
_PENDING: dict[str, tuple[tuple[str, int], ...]] = {}


def _quoted(placeholder: str) -> bool:
    expr = placeholder[1:-1]
    return expr.startswith("shlex.quote(") or expr.startswith("quoted_")


def _template(node: ast.AST) -> str | None:
    """A string node as text: a constant as itself, an f-string with ``{<expr>}`` for each interpolation."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                parts.append("{" + ast.unparse(value.value) + "}")
        return "".join(parts)
    return None


def _bare_labels_in(text: str) -> list[str]:
    """The commands in *text* that carry an unquoted label, each as the command text."""
    # Mask each {placeholder} so a ')' or a space inside an expression (shlex.quote(old)) neither ends the command
    # nor splits its token.
    held: list[str] = []

    def hold(m: re.Match[str]) -> str:
        held.append(m.group(0))
        return f"\x00{len(held) - 1}\x00"

    masked = re.sub(r"\{[^{}]*(?:\([^()]*\)[^{}]*)*\}", hold, text)

    def unmask(token: str) -> str:
        return re.sub(r"\x00(\d+)\x00", lambda m: held[int(m.group(1))], token)

    found: list[str] = []
    for match in re.finditer(r"\bnx [a-z][a-z-]*", masked):
        rest = masked[match.start():]
        end = _COMMAND_END.search(rest)
        command = rest[: end.start()] if end else rest
        verb = next((v for v in _LABEL_VERBS if command == v or command.startswith(v + " ")), None)
        tokens = [unmask(t) for t in command.split(" ")[(len(verb.split(" ")) if verb else 2):]]
        label_flag = other_flag = False
        for token in tokens:
            is_placeholder = bool(_PLACEHOLDER.match(token))
            if label_flag:
                label_flag = False
                if is_placeholder and not _quoted(token):
                    found.append(unmask(command))
                    break
                continue
            if other_flag:
                other_flag = False
                if is_placeholder:
                    continue  # the value of a flag that does not take a label (--abort {job_id}, --to {model})
            if token in _LABEL_FLAGS:
                label_flag = True
            elif token.startswith("-"):
                other_flag = True
            elif verb and is_placeholder and not _quoted(token):
                found.append(unmask(command))
                break
    return found


def _scan(src: str, rel: str) -> list[str]:
    tree = ast.parse(src)
    inner: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.JoinedStr, ast.FormattedValue)):
            inner.update(id(child) for child in ast.iter_child_nodes(node) if isinstance(child, ast.Constant))
    out: list[str] = []
    for node in ast.walk(tree):
        if id(node) in inner:
            continue
        text = _template(node)
        if text is None or "nx " not in text:
            continue
        out += [f"{rel}:{getattr(node, 'lineno', 0)}: {command}" for command in _bare_labels_in(text)]
    return out


def test_the_detector_finds_the_shapes_it_claims_to() -> None:
    src = (
        'a = f"Re-run `nx collection delete {name}`."\n'  # 1: bare
        'b = f"nx collection delete {shlex.quote(name)}"\n'  # 2: quoted
        'c = f"nx t3 gc -c {other} --dry-run"\n'  # 3: bare flag value
        'd = f"pass it: nx store delete --collection {holder}"\n'  # 4: bare flag value
        'e = f"collection {name!r} is gone"\n'  # 5: prose
        'f = f"nx collection rename {shlex.quote(old)} {shlex.quote(new)}"\n'  # 6: quoted
        'g = "nx taxonomy reset -c {collection} and discover"\n'  # 7: a .format template, bare
        'h = "nx taxonomy reset -c {quoted_collection} and discover"\n'  # 8: filled with shlex.quote by convention
        'i = (f"Run `nx " f"collection info {name}` to see it")\n'  # 9: split across two literals
        'j = f"nx collection rename {shlex.quote(old)} {new} --force"\n'  # 10: the second argument is bare
        'k = f"nx enrich aspects {coll} --limit 5"\n'  # 11: the enrich verb
        'l = f"nx collection re-embed {shlex.quote(legacy)} --to {model}"\n'  # 12: --to takes a model, not a label
    )
    assert [f.split(":")[1] for f in _scan(src, "x.py")] == ["1", "3", "4", "7", "9", "10", "11"]


def test_no_command_hint_prints_a_label_unquoted() -> None:
    bad: list[str] = []
    scanned = 0
    for path in sorted((REPO_ROOT / "src" / "nexus").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        found = _scan(path.read_text(encoding="utf-8"), rel)
        scanned += 1
        for command, count in _PENDING.get(rel, ()):
            hits = [f for f in found if f.endswith(": " + command)]
            assert len(hits) == count, f"{rel}: pending {command!r} matched {len(hits)} sites, not {count}"
            found = [f for f in found if f not in hits]
        bad += found
    assert scanned > 300, scanned
    assert not bad, "quote the label with shlex.quote (nexus-88aj0):\n" + "\n".join(bad)


def test_a_label_with_a_space_and_a_semicolon_survives_the_round_trip() -> None:
    label = "notes 50%: café; rm -rf x"
    assert shlex.split(f"nx collection delete {shlex.quote(label)}") == ["nx", "collection", "delete", label]


def test_every_shlex_quote_call_has_an_import_in_scope() -> None:
    """A hint's f-string is evaluated only when it prints, so a missing ``import shlex`` is a NameError on the error
    path and nowhere else (the sweep's first cut put the import inside two module docstrings; the migrate-fallback
    test was the only thing that reached one). Each ``shlex.quote`` call needs ``import shlex`` at module scope or in
    a function that encloses the call; an import anywhere else (a docstring is not code, a sibling function's import
    does not bind here) does not count."""
    missing: list[str] = []
    for path in sorted((REPO_ROOT / "src" / "nexus").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "shlex.quote(" not in text:
            continue
        tree = ast.parse(text)
        rel = path.relative_to(REPO_ROOT).as_posix()

        def imports_shlex(body: list[ast.stmt]) -> bool:
            return any(isinstance(n, ast.Import) and any(a.name == "shlex" for a in n.names) for n in body)

        def visit(node: ast.AST, covered: bool) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                covered = covered or any(
                    imports_shlex([n]) for n in ast.walk(node) if isinstance(n, ast.Import)
                    and not any(isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f is not node
                                and n in ast.walk(f) for f in ast.walk(node))
                )
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "quote"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "shlex" and not covered):
                missing.append(f"{rel}:{node.lineno}")
            for child in ast.iter_child_nodes(node):
                visit(child, covered)

        visit(tree, imports_shlex(tree.body))
    assert not missing, "shlex.quote with no import shlex in scope:\n" + "\n".join(missing)
