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
    "bounded_subprocess.py": "kill_child_and_descendants returns before any reference when getattr(os, 'killpg', None) is None",
    "pdeathsig.py": "Linux-only: the prctl call is gated on sys.platform",
}


def _references(tree: ast.AST) -> list[tuple[int, str]]:
    """Every reference to a forbidden primitive, however it was imported.

    An attribute walk that assumes the module is bound as ``os``/``signal``
    misses ``import signal as s`` and ``from signal import SIGKILL``, the
    blind spot this project has already paid for once with an AST lint. So
    the names each import binds are collected first: a module alias
    (``import os as o``) maps to its module, and a directly imported
    forbidden name (``from os import killpg as k``) is itself a reference.
    ``getattr(module, "NAME")`` with a literal name counts too, unless it
    passes a default, which is exactly the guarded form.
    """
    module_names: dict[str, str] = {"os": "os", "signal": "signal"}
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("os", "signal"):
                    module_names[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in ("os", "signal"):
            for alias in node.names:
                if (node.module, alias.name) in _FORBIDDEN:
                    found.append((node.lineno, f"from {node.module} import {alias.name}"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and (module_names.get(node.value.id), node.attr) in _FORBIDDEN
        ):
            found.append((node.lineno, f"{node.value.id}.{node.attr}"))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Name)
            and isinstance(node.args[1], ast.Constant)
            and (module_names.get(node.args[0].id), node.args[1].value) in _FORBIDDEN
        ):
            found.append((node.lineno, f"getattr({node.args[0].id}, {node.args[1].value!r})"))
    return found


@pytest.mark.parametrize(
    "source",
    [
        "import signal as s\ns.SIGKILL\n",
        "from signal import SIGKILL\n",
        "from os import killpg as k\n",
        "import os as o\no.getpgid(1)\n",
        "import signal\ngetattr(signal, 'SIGKILL')\n",
    ],
    ids=["module-alias", "from-import", "from-import-aliased", "os-alias", "getattr-no-default"],
)
def test_census_sees_aliased_and_indirect_forms(source: str) -> None:
    assert _references(ast.parse(source)), source


def test_census_passes_the_guarded_getattr_form() -> None:
    assert _references(ast.parse("import signal\ngetattr(signal, 'SIGKILL', 15)\n")) == []


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


# -- os.kill(pid, SIGTERM): TerminateProcess on Windows (RDR-224 test review m9) ----------------
#
# The census above is about names that do not EXIST on Windows. This one is about a name that
# does and means something else: ``os.kill(pid, signal.SIGTERM)`` is ``TerminateProcess`` there,
# a hard kill with no grace window, where POSIX gets a catchable signal. A stop that must be
# graceful goes through ``service_registry.request_graceful_stop`` (CTRL_BREAK), and a hard
# kill through ``hard_kill_pid``. A raw SIGTERM send is allowed only where each site's own
# reason is written down here, so a new one is a decision and not an accident.

#: (module relative to src/nexus, enclosing function) -> why a raw SIGTERM send is right there.
_SIGTERM_SEND_ALLOWED: dict[tuple[str, str], str] = {
    ("daemon/service_registry.py", "request_graceful_stop"): (
        "the primitive itself: this is its POSIX arm, and the Windows arm returns before it"
    ),
    ("session.py", "_kill_orphan_tracker_pids"): (
        "unreachable from production on Windows (sweep_orphan_trackers returns 0 before it lists "
        "anything); a direct caller there gets TerminateProcess followed by hard_kill_pid, which "
        "is the ladder's own end state, and a multiprocessing resource_tracker is no console group"
    ),
    ("commands/mineru.py", "start"): (
        "cleanup of a mineru-api that never became healthy: TerminateProcess is the same end "
        "state `nx mineru stop` documents for Windows (mineru.py, 'there is no grace window'), "
        "and a server that never served holds no state to flush"
    ),
    ("mcp_client/devonthink.py", "_sigterm_handler"): (
        "DEVONthink is macOS-only (commands/dt.py gates every verb on _is_darwin), and the call "
        "re-raises the process's own SIGTERM after restoring its disposition"
    ),
}


def _sigterm_sends(tree: ast.AST) -> list[tuple[str, int]]:
    """``(enclosing function, line)`` of every ``os.kill(<x>, signal.SIGTERM | SIGTERM)``."""
    out: list[tuple[str, int]] = []

    def is_sigterm(node: ast.AST) -> bool:
        return (isinstance(node, ast.Attribute) and node.attr == "SIGTERM") or (
            isinstance(node, ast.Name) and node.id == "SIGTERM"
        )

    def visit(node: ast.AST, scope: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = node.name
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "kill"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and len(node.args) == 2
            and is_sigterm(node.args[1])
        ):
            out.append((scope, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, "<module>")
    return out


def test_the_sigterm_send_detector_sees_both_spellings_and_ignores_the_rest() -> None:
    assert _sigterm_sends(ast.parse("import os, signal\ndef f(p):\n    os.kill(p, signal.SIGTERM)\n")) == [("f", 3)]
    assert _sigterm_sends(ast.parse("import os\nfrom signal import SIGTERM\nos.kill(1, SIGTERM)\n")) == [("<module>", 3)]
    assert _sigterm_sends(ast.parse("import os\nos.kill(1, 0)\n")) == []  # a liveness probe, not a send
    assert _sigterm_sends(ast.parse("import os, signal\nos.kill(1, signal.SIGINT)\n")) == []


def test_no_unlisted_raw_sigterm_send() -> None:
    found: dict[tuple[str, str], list[int]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        for scope, lineno in _sigterm_sends(ast.parse(path.read_text(encoding="utf-8"))):
            found.setdefault((rel, scope), []).append(lineno)
    assert set(_SIGTERM_SEND_ALLOWED) <= set(found), (
        "the scan did not find a listed site; the detector is broken or the site moved: "
        f"{sorted(set(_SIGTERM_SEND_ALLOWED) - set(found))}"
    )
    stray = {k: v for k, v in found.items() if k not in _SIGTERM_SEND_ALLOWED}
    assert not stray, (
        "os.kill(pid, SIGTERM) is TerminateProcess on Windows. Use service_registry."
        "request_graceful_stop (graceful) or hard_kill_pid (hard), or list the site in "
        "_SIGTERM_SEND_ALLOWED with its reason: "
        + ", ".join(f"src/nexus/{p}:{lines} in {s}" for (p, s), lines in sorted(stray.items()))
    )


def test_every_allowed_module_still_references_a_primitive() -> None:
    # A stale allowlist entry would silently exempt a future regression in
    # that module, so each entry must still earn its place.
    for rel in _ALLOWED:
        tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
        assert _references(tree), f"{rel} no longer references any primitive; drop it from _ALLOWED"
