# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Ratchet: capture+timeout ``subprocess`` calls route through ``run_bounded``.

THE SHAPE THIS WATCHES::

    subprocess.run(argv, capture_output=True, timeout=5)

:mod:`nexus.bounded_subprocess` carries the full mechanism, including the
correction that matters for reading this lint's severity: the unbounded
post-kill drain is Windows-only in CPython 3.12, so on POSIX this shape is
an ORPHAN LEAK (the timed-out call reaps its direct child and leaves the
descendants running) while on Windows it is a genuine unbounded HANG. Both
are worth removing; only one hangs a user's session.

WHY A RATCHET AND NOT A FLAG DAY. The census that produced
``run_bounded`` found 81 of these in ``src/nexus``, and nexus-t10nc's own
judgement — which Sam accepted — is that converting them one at a time is
what produced them in the first place, while converting all of them in one
change is a large blind diff across daemon, catalog, indexer and CLI paths.
So the count is pinned and drains. A new site is refused; an existing site
is fixed when its module is next touched for a reason of its own, and this
file's number goes down with it.

The ceiling is EXACT EQUALITY, per file, never ``<=``. An inequality
ceiling silently accepts a file that converted one site and added two, and
this repo's ratchets (``test_mode_declarations_are_explicit.py``, RDR-109;
``test_pipefail_early_exit_consumer_lint.py``) are all exact for that
reason. Per FILE rather than per line because line numbers move under
ordinary editing and a line-keyed ratchet reds on a rename.

SCOPE EXCLUDES ``src/nexus/hooks/``, and this is temporary and deliberate.
Those 24 sites are the ones that can hang a user's session rather than a
CLI command they can interrupt, so they are the most valuable to convert
and the first that should come into scope. They are excluded because
nexus-t9klx is moving hook entry points between modules as this lands, and
a per-file ratchet over a directory being restructured reds on the
restructuring rather than on a defect. ADDING THEM IS OWED once t9klx
closes; :data:`_HOOKS_EXCLUSION_IS_TEMPORARY` exists so that obligation is
greppable rather than remembered.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"

#: See the module docstring. Flipping this to False is the t9klx follow-up.
_HOOKS_EXCLUSION_IS_TEMPORARY: bool = True

#: Directory (relative to ``src/nexus``) held out of scope, with the bead
#: that has to close before it comes in.
_EXCLUDED_DIR = "hooks"
_EXCLUDED_DIR_BEAD = "nexus-t9klx"

#: Exact per-file count of unconverted capture+timeout subprocess calls.
#: Measured 2026-09-22 against develop. Numbers go DOWN. A file that
#: reaches zero is deleted from this map, and a file absent from it is
#: allowed zero.
_UNCONVERTED: dict[str, int] = {
    "src/nexus/_install/census_core.py": 1,
    "src/nexus/_install/layout_core.py": 1,
    "src/nexus/commands/command_context.py": 1,
    "src/nexus/commands/daemon.py": 2,
    "src/nexus/commands/doctor.py": 1,
    "src/nexus/commands/rdr.py": 12,
    "src/nexus/commands/self_cmd.py": 2,
    "src/nexus/daemon/installer.py": 1,
    "src/nexus/daemon/service_registry.py": 3,
    "src/nexus/db/admin_sql.py": 1,
    "src/nexus/db/diag_connection.py": 1,
    "src/nexus/db/pg_provision.py": 1,
    "src/nexus/db/svc_monitor.py": 1,
    "src/nexus/formatters.py": 2,
    "src/nexus/health.py": 1,
    "src/nexus/upgrade_finish.py": 6,
}


def _in_scope(path: pathlib.Path) -> bool:
    rel = path.relative_to(SRC_ROOT)
    return "__pycache__" not in path.parts and rel.parts[0] != _EXCLUDED_DIR


def _capture_with_timeout_calls(source: str) -> list[int]:
    """Line numbers of ``subprocess.run``/``check_output`` calls that both
    capture output and pass a timeout."""
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
            and func.attr in {"run", "check_output"}
        ):
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        captures = (
            "capture_output" in kwargs
            or "stdout" in kwargs
            or func.attr == "check_output"
        )
        if captures and "timeout" in kwargs:
            found.append(node.lineno)
    return found


def _census() -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if not _in_scope(path):
            continue
        try:
            lines = _capture_with_timeout_calls(path.read_text())
        except (
            SyntaxError
        ):  # pragma: no cover - a syntax error is another test's failure
            continue
        if lines:
            out[path.relative_to(REPO_ROOT).as_posix()] = lines
    return out


def test_scan_is_not_vacuous() -> None:
    """The sweep reached a real tree.

    A lint whose scan found nothing passes for two very different reasons —
    the defect is gone, or the scan is broken — and this repo has already
    paid for one gate that could not tell them apart (the nexus-moht0
    vacuous-gate doctrine). The floor is deliberately loose: it only has to
    distinguish "scanned the codebase" from "scanned nothing".
    """
    scanned = [p for p in SRC_ROOT.rglob("*.py") if _in_scope(p)]
    assert len(scanned) > 200, (
        f"only {len(scanned)} files in scope under {SRC_ROOT}; the scan root or the "
        "exclusion filter is wrong, so a green result here means nothing"
    )


def test_unconverted_subprocess_calls_match_the_ratchet() -> None:
    """Exact per-file equality against the pinned census."""
    actual = {path: len(lines) for path, lines in _census().items()}

    new_files = sorted(set(actual) - set(_UNCONVERTED))
    assert not new_files, (
        "these files gained a capture+timeout subprocess call and are not in the "
        f"ratchet: {new_files}. Route it through nexus.bounded_subprocess.run_bounded "
        "rather than adding an entry -- the ratchet only ever counts DOWN."
    )

    regressions = {
        p: (_UNCONVERTED[p], actual[p]) for p in actual if actual[p] > _UNCONVERTED[p]
    }
    assert not regressions, (
        "these files gained capture+timeout subprocess calls (pinned, actual): "
        f"{regressions}. Use nexus.bounded_subprocess.run_bounded."
    )

    improvements = {
        p: (_UNCONVERTED[p], actual.get(p, 0))
        for p in _UNCONVERTED
        if actual.get(p, 0) < _UNCONVERTED[p]
    }
    assert not improvements, (
        "these files have FEWER unconverted calls than the ratchet records "
        f"(pinned, actual): {improvements}. That is good -- lower the numbers in "
        "_UNCONVERTED to match, or delete the entry if it reached zero, so the "
        "ratchet cannot drift back up."
    )


def test_hooks_exclusion_names_its_reason() -> None:
    """The temporary hold-out stays greppable and stays honest.

    Flipping ``_HOOKS_EXCLUSION_IS_TEMPORARY`` to False without bringing
    ``src/nexus/hooks`` into scope is the failure this pins: that would turn
    a deliberate, bead-linked deferral into a permanent silent blind spot
    over the 24 most dangerous sites in the census.
    """
    if _HOOKS_EXCLUSION_IS_TEMPORARY:
        assert (SRC_ROOT / _EXCLUDED_DIR).is_dir(), (
            f"{_EXCLUDED_DIR} is excluded from this lint but no longer exists; "
            "delete the exclusion"
        )
        assert _EXCLUDED_DIR_BEAD, "a temporary exclusion names the bead that ends it"
        return
    assert _in_scope(SRC_ROOT / _EXCLUDED_DIR / "__init__.py"), (
        "the hooks exclusion was declared permanent but the directory is still "
        "filtered out of _in_scope"
    )
