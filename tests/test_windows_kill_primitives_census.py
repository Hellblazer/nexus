# SPDX-License-Identifier: AGPL-3.0-or-later
"""No bare ``signal.SIGKILL`` / ``os.killpg`` / ``os.getpgid`` outside guarded modules.

nexus-34f7r. None of the three exists on Windows. A reference evaluates to
``AttributeError``, and every site that had one sat in a cleanup branch, so
on a Windows client the kill threw from inside the ``except``/``finally``
that was handling some other failure and masked it. The sites now use
``nexus.util.process_group.KILL_SIGNAL`` and ``safe_killpg``, which degrade
where the primitives are absent.

This census keeps it that way. It is parsed rather than grepped, so a
mention in a docstring or comment is not a reference. Each allowed module
names the guard that makes its references safe.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"

_FORBIDDEN: frozenset[tuple[str, str]] = frozenset(
    {("signal", "SIGKILL"), ("os", "killpg"), ("os", "getpgid")}
)

#: module path (relative to src/nexus) -> the guard that makes it safe.
_ALLOWED: dict[str, str] = {
    "util/process_group.py": "the helper itself; every use is behind getattr(os, 'killpg', None)",
    "bounded_subprocess.py": "_kill_tree returns before any reference when getattr(os, 'killpg', None) is None",
    "pdeathsig.py": "Linux-only: the prctl call is gated on sys.platform",
}


def _references(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and (node.value.id, node.attr) in _FORBIDDEN
        ):
            found.append((node.lineno, f"{node.value.id}.{node.attr}"))
    return found


def test_no_bare_posix_only_kill_primitives() -> None:
    scanned = 0
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        scanned += 1
        if rel in _ALLOWED:
            continue
        for lineno, ref in _references(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"src/nexus/{rel}:{lineno}: {ref}")
    assert scanned > 100, f"census scanned only {scanned} files; is _SRC right?"
    assert not offenders, (
        "POSIX-only kill primitives outside a guarded module (nexus-34f7r). "
        "Use nexus.util.process_group.KILL_SIGNAL / safe_killpg, which degrade "
        "on Windows, or add the module to _ALLOWED naming its guard:\n"
        + "\n".join(offenders)
    )


def test_every_allowed_module_still_references_a_primitive() -> None:
    # A stale allowlist entry would silently exempt a future regression in
    # that module, so each entry must still earn its place.
    for rel in _ALLOWED:
        tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
        assert _references(tree), f"{rel} no longer references any primitive; drop it from _ALLOWED"
