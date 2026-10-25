# SPDX-License-Identifier: AGPL-3.0-or-later
"""A doctor warning is ``ok=False, warn=True``; ``ok=True`` always prints ✓.

The 7.86.1 shakeout found three rows built ``ok=True, warn=True`` (the two-OpenCV
PDF row off Linux, the Windows "cannot be installed yet" PDF row, the Dependency
lock row). The renderer checks ``ok`` first, so each printed a green ✓ next to
its warning text. This sweep refuses a ``HealthResult`` whose ``ok`` and ``warn``
are both the literal True or the same expression, so they cannot both hold.
"""
from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"


def _calls() -> list[tuple[str, int, ast.expr, ast.expr]]:
    found = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "HealthResult":
                continue
            kw = {k.arg: k.value for k in node.keywords}
            if "ok" in kw and "warn" in kw:
                found.append((str(path.relative_to(_SRC)), node.lineno, kw["ok"], kw["warn"]))
    return found


def _both_true(ok: ast.expr, warn: ast.expr) -> bool:
    if isinstance(ok, ast.Constant) and isinstance(warn, ast.Constant):
        return ok.value is True and warn.value is True
    return ast.unparse(ok) == ast.unparse(warn)


def test_no_health_row_sets_ok_and_warn_together() -> None:
    calls = _calls()
    assert len(calls) >= 20, f"sweep found only {len(calls)} HealthResult(ok=, warn=) calls"
    bad = [f"{p}:{n} ok={ast.unparse(o)} warn={ast.unparse(w)}" for p, n, o, w in calls if _both_true(o, w)]
    assert not bad, "a warning is ok=False, warn=True; these print ✓:\n" + "\n".join(bad)
