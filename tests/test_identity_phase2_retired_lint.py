# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-253 Phase 2: nothing reads or writes the retired session paths (nexus-hczdo.2.1).

Phase 2 retires four things a session used to be found by, now that every session is bound to an identity that
outlives ``/clear`` and restart:

1. the four-tier session resolver, ``resolve_active_session_id``;
2. the ``~/.config/nexus/current_session`` file, its writer and every reader;
3. ``directory/<name>`` as a mail address (the RDR-208 name lease);
4. ``mailbox/<session id>`` as a mail address (identity mail goes to ``mailbox/<identity id>``).

A reference that survives is a path the retirement missed, and each of these reads plausibly until the day it
resolves the wrong session. So the sweep fails on any of:

- the resolver, the file's accessors or the ``session_mailbox`` property by name (:data:`_RETIRED_NAMES`: a call, an
  import, a definition, an attribute);
- a string literal naming the ``current_session`` file, outside a docstring (an identifier merely called
  ``current_session``, such as :func:`nexus.identity.work.current_session` or the tuple-wait parameter, is a different
  thing and is not matched);
- a string literal that starts a ``directory/`` address;
- a ``mailbox/`` address built from a session id: an f-string or a ``+`` whose first interpolated part is a name that
  mentions ``session``;
- a send marked as session-addressed: the string ``address_kind=session``, or ``address_kind`` set to ``"session"`` as a
  keyword, a dict entry or an assignment (a send's ``address_kind`` is ``identity`` or ``agent``; a script that builds its
  address from a plain ``sid`` name escapes the previous rule, and this marker does not);

in Python under ``src/nexus``, ``conexus`` and ``scripts`` (extensionless files that open with a python shebang, such
as ``scripts/claude-restart-sessions``, included), and in the shell scripts there (``current_session``,
``resolve_active_session_id`` and ``directory/`` as text: the push script's claimant read the file with ``cat``).

An entry in :data:`_ALLOWED` is a decision with its reason. The floors make a sweep that found no files fail.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
TREES = {"src/nexus": REPO_ROOT / "src" / "nexus", "conexus": REPO_ROOT / "conexus", "scripts": REPO_ROOT / "scripts"}

#: Python modules each tree must yield, so a moved tree or a broken glob cannot pass by scanning nothing.
_MIN_PY = {"src/nexus": 300, "conexus": 5, "scripts": 40}
#: Extensionless python scripts the sweep must parse in scripts/ (claude-restart-sessions), so a broken shebang test
#: cannot pass by dropping them.
_MIN_EXTENSIONLESS_PY = 1
#: Shell scripts the sweep must read in scripts/ (git-push-develop.sh among them).
_MIN_SHELL = 10

#: The resolver, the file's accessors, and the session-mailbox property: a use of any of them is a retired path, whatever
#: string it builds. (The drains pass the session id to a shared drain as a plain value, which no static match can tell
#: from an identity: the drain tests prove those drain the identity's mailbox alone.)
_RETIRED_NAMES = frozenset({"resolve_active_session_id", "read_claude_session_id", "write_claude_session_id",
                            "claude_session_file", "CLAUDE_SESSION_FILE", "session_mailbox"})
_SHELL_TOKENS = re.compile(r"current_session|resolve_active_session_id|\bdirectory/")
#: Paths never scanned: generated experiment output, and caches.
_SKIP_PARTS = {"__pycache__", "out", ".venv", "node_modules"}

#: ``<repo-relative path>:<line>`` -> reason. Adding an entry is a decision; leaving one out is a defect.
_ALLOWED: dict[str, str] = {}


def _is_extensionless_python(p: pathlib.Path) -> bool:
    """An extensionless file whose first line is a python shebang (``#!/usr/bin/env python3``)."""
    if p.suffix != "" or not p.is_file():
        return False
    try:
        with p.open("rb") as fh:
            first = fh.readline(256)
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first


def _py_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in root.rglob("*")
                  if not _SKIP_PARTS & set(p.relative_to(root).parts) and (p.suffix == ".py" and p.is_file()
                                                                         or _is_extensionless_python(p)))


def _shell_files(root: pathlib.Path) -> list[pathlib.Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or _SKIP_PARTS & set(p.relative_to(root).parts) or p.suffix == ".py" \
                or _is_extensionless_python(p):
            continue
        if p.suffix == ".sh":
            out.append(p)
            continue
        if p.suffix == "":
            try:
                head = p.open("rb").read(64)
            except OSError:
                continue
            if head.startswith(b"#!") and (b"sh" in head.split(b"\n", 1)[0]):
                out.append(p)
    return out


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _mentions_session(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and "session" in n.id.lower():
            return True
        if isinstance(n, ast.Attribute) and "session" in n.attr.lower():
            return True
    return False


def _session_mailbox(node: ast.AST) -> bool:
    """``f"mailbox/{session_id}"`` or ``"mailbox/" + session_id``: the first interpolated part names a session."""
    if isinstance(node, ast.JoinedStr) and len(node.values) >= 2:
        head, nxt = node.values[0], node.values[1]
        return isinstance(head, ast.Constant) and head.value == "mailbox/" and isinstance(nxt, ast.FormattedValue) \
            and _mentions_session(nxt.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = node.left
        return isinstance(left, ast.Constant) and left.value == "mailbox/" and _mentions_session(node.right)
    return False


def _is_session_kind(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value == "session"


def _session_address_kind(node: ast.AST, docs: set[int]) -> bool:
    """``"address_kind=session"``, ``address_kind="session"`` (keyword), ``{"address_kind": "session"}`` or an assignment."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
        return node.value == "address_kind=session"
    if isinstance(node, ast.keyword):
        return node.arg == "address_kind" and _is_session_kind(node.value)
    if isinstance(node, ast.Dict):
        return any(isinstance(k, ast.Constant) and k.value == "address_kind" and _is_session_kind(v)
                   for k, v in zip(node.keys, node.values))
    if isinstance(node, ast.Assign):
        return _is_session_kind(node.value) and any(isinstance(t, ast.Name) and t.id == "address_kind"
                                                    for t in node.targets)
    return False


def _rel(path: pathlib.Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def _py_offenders(path: pathlib.Path) -> list[str]:
    rel = _rel(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    docs = _docstring_nodes(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else \
            node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        if name in _RETIRED_NAMES:
            found.append((line, name))
        elif isinstance(node, ast.alias) and node.name.split(".")[-1] in _RETIRED_NAMES:
            found.append((line, f"import {node.name.split('.')[-1]}"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if "current_session" in node.value:
                found.append((line, "the current_session file"))
            if node.value.startswith("directory/"):
                found.append((line, "a directory/ address"))
        elif isinstance(node, ast.JoinedStr):
            head = node.values[0] if node.values else None
            if isinstance(head, ast.Constant) and isinstance(head.value, str) and head.value.startswith("directory/"):
                found.append((line, "a directory/ address"))
        if _session_mailbox(node):
            found.append((line, "a mailbox/<session id> address"))
        if _session_address_kind(node, docs):
            found.append((line, "a session-addressed send (address_kind=session)"))
    return [f"{rel}:{ln} {what}" for ln, what in sorted(set(found)) if f"{rel}:{ln}" not in _ALLOWED]


def _shell_offenders(path: pathlib.Path) -> list[str]:
    rel = path.relative_to(REPO_ROOT).as_posix()
    out = []
    for ln, text in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        code = text.split("#", 1)[0] if not text.lstrip().startswith("#!") else ""
        m = _SHELL_TOKENS.search(code)
        if m and f"{rel}:{ln}" not in _ALLOWED:
            out.append(f"{rel}:{ln} {m.group(0)}")
    return out


def test_nothing_reads_or_writes_current_session() -> None:
    offenders: list[str] = []
    scanned: dict[str, int] = {}
    for name, root in TREES.items():
        files = _py_files(root)
        scanned[name] = len(files)
        for path in files:
            offenders.extend(_py_offenders(path))
    shell = [p for root in TREES.values() for p in _shell_files(root)]
    for path in shell:
        offenders.extend(_shell_offenders(path))

    for name, floor in _MIN_PY.items():
        assert scanned[name] >= floor, f"{name}: scanned {scanned[name]} Python modules, floor {floor}"
    extensionless = [p for p in _py_files(TREES["scripts"]) if p.suffix == ""]
    assert len(extensionless) >= _MIN_EXTENSIONLESS_PY, \
        f"scripts: parsed {len(extensionless)} extensionless python scripts, floor {_MIN_EXTENSIONLESS_PY}"
    assert sum(1 for p in shell if "scripts" in p.relative_to(REPO_ROOT).parts[:1]) >= _MIN_SHELL, \
        f"scanned {len(shell)} shell scripts, floor {_MIN_SHELL} in scripts/"
    assert not offenders, (
        "RDR-253 Phase 2: a retired session path is still read or written (the resolver, the current_session file, "
        "a directory/ address or a mailbox/<session id> address). Move it onto the identity path, or add it to "
        "_ALLOWED with a reason:\n  " + "\n  ".join(offenders))


@pytest.mark.parametrize("src, expect", [
    ('x = resolve_active_session_id()', True),
    ('from nexus.session import resolve_active_session_id', True),
    ('p = cfg / "current_session"', True),
    ('def f():\n    """Reads the current_session file."""\n', False),
    ('def f(current_session): return current_session()', False),
    ('a = "directory/" + name', True),
    ('a = f"directory/{name}"', True),
    ('a = f"mailbox/{session_id}"', True),
    ('a = "mailbox/" + self.session_id', True),
    ('a = f"mailbox/{identity}"', False),
    ('a = "a directory/ path"', False),
    ('s = self.session_mailbox', True),
    ('from nexus.session import read_claude_session_id', True),
    ('argv = ["tuple", "out", "--dim", "address_kind=session"]', True),
    ('argv = ["--dim", f"address_kind=session"]', True),
    ('send(address_kind="session")', True),
    ('dims = {"from": me, "address_kind": "session"}', True),
    ('address_kind = "session"', True),
    ('argv = ["--dim", "address_kind=identity"]', False),
    ('send(address_kind="identity")', False),
    ('dims = {"address_kind": kind}', False),
    ('def f():\n    """A send marked address_kind=session is retired."""\n', False),
])
def test_the_sweep_matches_what_it_names(tmp_path: pathlib.Path, src: str, expect: bool) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(src + "\n", encoding="utf-8")
    assert bool(_py_offenders(probe)) is expect


def test_an_extensionless_python_script_is_parsed(tmp_path: pathlib.Path) -> None:
    """scripts/claude-restart-sessions builds ``tuple out`` argv by hand and has no ``.py``: the sweep reads it."""
    script = tmp_path / "tool"
    script.write_text('#!/usr/bin/env python3\nargv = ["tuple", "out", "--dim", "address_kind=session"]\n',
                      encoding="utf-8")
    shell = tmp_path / "launcher"
    shell.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    plain = tmp_path / "notes"
    plain.write_text("address_kind=session\n", encoding="utf-8")
    assert _py_files(tmp_path) == [script]
    assert _shell_files(tmp_path) == [shell]
    assert len(_py_offenders(script)) == 1
