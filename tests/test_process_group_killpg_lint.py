# SPDX-License-Identifier: AGPL-3.0-or-later
"""No group signal is sent from ``src/nexus`` except by ``util/process_group.py``
(nexus-x3m8e).

Every hard group kill must spare the ``multiprocessing`` resource tracker, or
the group's POSIX named semaphores leak for good (macOS: 10,000 in total, then
``ENOSPC`` machine-wide). That rule lives in ONE place,
``process_group._signal_group``; a second ``os.killpg`` anywhere else is a second
copy of the rule's absence, which is how the leak came back after two call sites
had been patched and a third had not (``bounded_subprocess`` and
``kill_child_and_descendants`` still sent the whole-group SIGKILL).

The scan is keyed on the CALL, not on the module or the signal: a ``killpg``
reached as ``os.killpg(...)``, through any alias of ``os``, as a name imported
with ``from os import killpg [as k]``, through a variable bound to ``os.killpg``
or to ``getattr(os, "killpg", ...)``, or handed on as a value
(``functools.partial(os.killpg, ...)``, ``executor.submit(os.killpg, ...)``) is
a violation. Probing for the function's existence is not a send and stays
allowed: ``getattr(os, "killpg", None) is None``, ``hasattr(os, "killpg")``.
(The Windows census in ``test_windows_kill_primitives_census.py`` allows the
reference where it is guarded; this file is about sending.)
"""
from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.lint

SRC = pathlib.Path(__file__).parent.parent / "src" / "nexus"

#: The only module that sends a group signal.
PRIMITIVE = "util/process_group.py"


def _is_getattr_killpg(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "killpg"
    )


def find_group_signal_sends(source: str) -> list[tuple[int, str]]:
    """``(line, why)`` of every way *source* sends or hands on ``killpg``."""
    tree = ast.parse(source)
    bound: set[str] = set()  # names that mean os.killpg
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "killpg":
                    bound.add(alias.asname or "killpg")
                    hits.append((node.lineno, "from os import killpg"))
        elif isinstance(node, ast.Assign) and (
            _is_getattr_killpg(node.value)
            or (isinstance(node.value, ast.Attribute) and node.value.attr == "killpg")
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.add(target.id)
    # a getattr(...) binding used only to probe is fine; a CALL through it is not
    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "killpg":
                hits.append((node.lineno, "killpg(...) call"))
            elif isinstance(f, ast.Name) and f.id in bound:
                hits.append((node.lineno, f"call through {f.id}, bound to killpg"))
            elif _is_getattr_killpg(f):
                hits.append((node.lineno, "getattr(os, 'killpg')(...) call"))
        elif isinstance(node, ast.Attribute) and node.attr == "killpg" and id(node) not in call_funcs:
            # os.killpg as a VALUE (partial, submit, an alias assignment).
            hits.append((node.lineno, "os.killpg handed on as a value"))
    return sorted(set(hits))


def test_no_module_but_the_primitive_sends_a_group_signal() -> None:
    offenders: list[str] = []
    scanned = 0
    primitive_hits = 0
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        scanned += 1
        hits = find_group_signal_sends(path.read_text(encoding="utf-8"))
        if rel == PRIMITIVE:
            primitive_hits = len(hits)
            continue
        offenders += [f"src/nexus/{rel}:{line}: {why}" for line, why in hits]
    assert scanned > 100, f"scanned only {scanned} files; is SRC right?"
    assert primitive_hits >= 1, "the detector found no killpg in the primitive: it is blind"
    assert not offenders, (
        "a group signal is sent outside nexus.util.process_group (nexus-x3m8e). Use safe_killpg / "
        "safe_killpg_group / kill_tree: they spare the multiprocessing resource tracker, and a "
        "second killpg is a second chance to leak the group's POSIX named semaphores:\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("source,expected", [
    ("import os\nos.killpg(1, 9)\n", True),
    ("import os as _o\n_o.killpg(1, 9)\n", True),
    ("from os import killpg\nkillpg(1, 9)\n", True),
    ("from os import killpg as k\nk(1, 9)\n", True),
    ("import os\nk = getattr(os, 'killpg', None)\nk(1, 9)\n", True),
    ("import os\ngetattr(os, 'killpg')(1, 9)\n", True),
    ("import os, functools\nf = functools.partial(os.killpg, 1)\n", True),
    ("import os\nex.submit(os.killpg, 1, 9)\n", True),
    ("import os\nk = os.killpg\nk(1, 9)\n", True),
    # probes are not sends
    ("import os\nif getattr(os, 'killpg', None) is None:\n    pass\n", False),
    ("import os\nok = hasattr(os, 'killpg')\n", False),
    ("import os\nos.kill(1, 9)\n", False),
])
def test_the_detector_sees_every_shape_of_a_group_signal(source: str, expected: bool) -> None:
    assert bool(find_group_signal_sends(source)) is expected, source
