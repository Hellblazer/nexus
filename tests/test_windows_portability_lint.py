# SPDX-License-Identifier: AGPL-3.0-or-later
"""Standing gate: no unguarded POSIX-only stdlib import at module scope.

Cloud mode's whole client-side promise is that the box needs no native
service: no local Java engine, no local Postgres, no local embedder (see
``nexus/db/managed_endpoint.py`` — "in cloud mode there is no local Java
service and no local Postgres"). On Windows that is the ONLY reachable
mode, because the engine-service and PG-bundle release matrices build
linux-amd64 / linux-arm64 / mac-arm64 and nothing else.

What actually blocked it was three lines. ``db/t1.py`` did ``import
fcntl`` at module scope; ``cli.py`` imports ``commands.scratch``, which
imports ``db.t1`` — so *every* ``nx`` invocation died at import with
``ModuleNotFoundError: No module named 'fcntl'`` before it could reach a
single line of cloud-mode logic. ``db/data_token.py`` carried the same
import and would have taken out the first authenticated call.

The fix routed all of them through :mod:`nexus._locking`, which has
carried a working ``msvcrt`` branch since the RDR-120 era. This gate is
what keeps the count at zero: a new module-scope ``import fcntl`` is a
one-line change that silently re-breaks an entire platform, and nothing
in CI runs on Windows to catch it (no ``runs-on: windows-*`` exists in
any workflow).

Scope, deliberately narrow: MODULE-LEVEL imports only. A POSIX-only
import nested inside a function, a ``try``/``except ImportError``, or an
``if sys.platform`` branch is a considered choice and passes — that is
exactly what ``_locking.py`` itself does.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"

#: Stdlib modules that do not exist on Windows at all. Importing any of
#: them at module scope makes the importing module — and everything that
#: transitively imports it — unloadable on the platform.
POSIX_ONLY_MODULES = frozenset(
    {"fcntl", "grp", "pwd", "posix", "pty", "spwd", "syslog", "termios", "tty"}
)

#: The one module allowed to import them unguarded is the cross-platform
#: primitive itself, which does so inside an ``if sys.platform`` branch
#: with an ``msvcrt`` alternative. Listed by relative posix path.
_ALLOWED: frozenset[str] = frozenset({"_locking.py"})


def _py_files() -> list[pathlib.Path]:
    return [p for p in SRC_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def _module_level_posix_imports(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, module)`` for each POSIX-only import at module scope.

    Walks ``tree.body`` only — never ``ast.walk`` — so a deferred import
    inside a function, a ``try``, or a platform branch is out of scope by
    construction rather than by pattern-matching the guard.
    """
    found: list[tuple[int, str]] = []
    for node in ast.parse(source).body:
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module.split(".")[0]]
        found.extend(
            (node.lineno, name) for name in names if name in POSIX_ONLY_MODULES
        )
    return found


def test_no_unguarded_posix_only_module_imports() -> None:
    """Zero module-scope POSIX-only imports outside ``_locking.py``."""
    offenders: list[str] = []
    for path in _py_files():
        rel = path.relative_to(SRC_ROOT).as_posix()
        if rel in _ALLOWED:
            continue
        for lineno, module in _module_level_posix_imports(
            path.read_text(encoding="utf-8")
        ):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: import {module}")

    assert not offenders, (
        "A POSIX-only stdlib module is imported at module scope, which makes "
        "the importing module unloadable on Windows — and via nexus.cli's "
        "import chain that can take down the whole CLI, cloud mode included. "
        "For advisory locks use nexus._locking (lock_fd / unlock_fd / "
        "lock_file / unlock_file). Otherwise defer the import into the "
        "function that needs it, or branch on sys.platform. Offenders:\n  "
        + "\n  ".join(offenders)
    )


def test_scan_is_non_vacuous() -> None:
    """Prove the scanner fires on a real offender and stays quiet on the
    guarded forms it must not flag (the nexus-moht0 vacuous-gate rule: a
    sweep that found nothing must be shown capable of finding something).
    """
    offender = "import os\nimport fcntl\n\n\ndef f():\n    return fcntl\n"
    hits = _module_level_posix_imports(offender)
    assert hits == [(2, "fcntl")], "the scan must catch a module-scope import"

    deferred = "import os\n\n\ndef f():\n    import fcntl\n    return fcntl\n"
    assert _module_level_posix_imports(deferred) == [], (
        "a function-local import is a considered choice and must not trip"
    )

    branched = (
        "import sys\n\nif sys.platform == 'win32':\n    import msvcrt\n"
        "else:\n    import fcntl\n"
    )
    assert _module_level_posix_imports(branched) == [], (
        "a platform-branched import is the correct pattern and must not trip"
    )


def test_allowlist_entry_still_exists() -> None:
    """The allowlist must not rot into a reference to a deleted file — an
    entry pointing at nothing would silently widen the gate's blind spot.
    """
    for rel in _ALLOWED:
        assert (SRC_ROOT / rel).is_file(), (
            f"_ALLOWED names {rel}, which no longer exists; drop the entry "
            "rather than leaving a dead exemption"
        )


#: The one module allowed to touch ``fcntl`` at ALL, at any scope.
_FCNTL_ALLOWED: frozenset[str] = frozenset({"_locking.py"})


def _fcntl_references(source: str) -> list[int]:
    """Line numbers of any ``import fcntl`` or ``fcntl.<attr>``, at ANY scope."""
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            hits.extend(
                node.lineno for a in node.names if a.name.split(".")[0] == "fcntl"
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] == "fcntl"
        ):
            hits.append(node.lineno)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "fcntl"
        ):
            hits.append(node.lineno)
    return sorted(set(hits))


def test_no_fcntl_outside_the_locking_shim() -> None:
    """``fcntl`` is reachable only through :mod:`nexus._locking`.

    THIS IS A DIFFERENT FAILURE CLASS FROM THE MODULE-SCOPE TEST ABOVE, and
    deliberately a separate test rather than a widening of it. That one scans
    ``tree.body`` because a MODULE-scope POSIX import kills CLI import on
    Windows outright, and its docstring correctly calls a function-local
    import "a considered choice".

    For ``fcntl`` specifically the shim makes that choice unnecessary, and a
    FUNCTION-level `import fcntl` fails at call time on the one path that
    needs the lock — passing the module-scope test by design. Two sites did
    exactly that (nexus-ijue9.9, RDR-218 Phase 3):
    ``daemon/storage_service_daemon.py`` deferred the import into the spawn
    lock, and ``commands/daemon.py`` into the election lock. Both are now
    routed through ``_locking.lock_fd``/``unlock_fd``.

    Starts at ZERO offenders, so it is a pure ratchet with nothing to drain.
    """
    offenders = {
        p.relative_to(SRC_ROOT).as_posix(): lines
        for p in _py_files()
        if p.name not in _FCNTL_ALLOWED
        and (lines := _fcntl_references(p.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "fcntl is referenced outside nexus._locking:\n  "
        + "\n  ".join(f"{path}:{lines}" for path, lines in sorted(offenders.items()))
        + "\nUse nexus._locking (lock_fd/unlock_fd, lock_file/unlock_file, "
        "acquire_directory_lock). Do NOT add a second platform branch beside it."
    )


def test_the_fcntl_scan_would_catch_a_function_level_import() -> None:
    """Non-vacuity, and it pins the exact shape the other test cannot see.

    A synthetic function-local ``import fcntl`` must be found. Without this,
    a regression in :func:`_fcntl_references` would make the ratchet above
    pass for the wrong reason, and it would pass *quietly* because its
    correct answer is already the empty set.
    """
    source = "def f():\n    import fcntl\n    return fcntl.flock\n"
    assert _fcntl_references(source) == [2, 3], (
        "the scanner missed a function-level import fcntl and/or an fcntl. "
        "attribute; the ratchet above is not watching what it claims to"
    )
