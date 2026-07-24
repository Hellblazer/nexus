# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185: every deferred import inside the ladder actually resolves.

The bug this exists for (found by the P4.3 era-hop's first honest live run,
2026-07-16): ``SubstrateEtlRung._default_migrate`` imported ``open_read_legs``
from ``nexus.migration.chroma_read``. It lives in ``nexus.migration.detection``
and is not re-exported there, so the production default raised ImportError on
the first real converge — the substrate rung, the entire point of RDR-185's
P2/P4.0 work, had NEVER successfully run. Every user with a Chroma footprint
would have hit it.

Why the suite was blind: the rung takes ``migrate_fn`` by constructor
injection, and every unit test injects a fake. That is correct test design —
it keeps unit tests off real infrastructure — but it means the PRODUCTION
default's body, including its deferred imports, was never once executed. A
function-local import is invisible to import-time checking and to any test
that never calls the function, so it fails first in front of a user.

The pin is deliberately over the whole PACKAGE, not just the one call site:
the defect class is "a deferred import in a code path no test executes", and
the ladder is full of them (they are load-bearing here — deferred imports keep
cold CLI start cheap and break import cycles, so the answer is never to move
them to the top). Resolving them statically costs milliseconds and catches the
whole class.

This checks that the NAME is importable from the MODULE named — it does not
execute the enclosing function, so it is not a substitute for the era-hop's
end-to-end proof.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import nexus.upgrade_ladder

_PACKAGE_ROOT = Path(nexus.upgrade_ladder.__file__).parent


def _package_modules() -> list[Path]:
    return sorted(p for p in _PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _deferred_imports(path: Path) -> list[tuple[str, str, int]]:
    """Every ``from X import Y`` nested inside a function body.

    Module-level imports are already proven by importing the module, so only
    function-local ones are in scope — those are exactly the ones no import of
    the package will ever validate.
    """
    tree = ast.parse(path.read_text())
    out: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.ImportFrom) and sub.module and sub.level == 0:
                out.extend((sub.module, alias.name, sub.lineno) for alias in sub.names)
    return out


_CASES = [
    pytest.param(mod, name, path, lineno, id=f"{path.name}:{lineno}:{mod}.{name}")
    for path in _package_modules()
    for mod, name, lineno in _deferred_imports(path)
]


@pytest.mark.parametrize(("module", "name", "path", "lineno"), _CASES)
def test_deferred_import_resolves(module: str, name: str, path: Path, lineno: int) -> None:
    try:
        imported = importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - the failure this pin exists for
        pytest.fail(
            f"{path.name}:{lineno} defers `from {module} import {name}`, but the "
            f"MODULE does not import: {exc}"
        )
    assert hasattr(imported, name), (
        f"{path.name}:{lineno} defers `from {module} import {name}`, but "
        f"{module!r} has no attribute {name!r} — this import raises ImportError "
        f"the first time the enclosing function actually runs, which for a "
        f"production default means in front of a user, not in this suite."
    )


def test_the_pin_is_not_vacuous() -> None:
    """A parametrize list that silently went empty would pass everything."""
    assert _CASES, "no deferred imports discovered — the AST walk or the package layout moved"
    # The rung whose production default the era-hop caught must be in scope.
    scanned = {path.name for _, _, path, _ in
               ((c.values[0], c.values[1], c.values[2], c.values[3]) for c in _CASES)}
    # RDR-155 P4b: substrate_etl.py (the original anchor) died with the
    # migration machinery; the surviving rung anchors the non-vacuity pin.
    assert "chash_rekey.py" in scanned, (
        "chash_rekey.py contributes no deferred imports to the census — "
        "the module moved or the walk stopped seeing it"
    )
