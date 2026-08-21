# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Repo-wide lint: no NEW ``monkeypatch.setattr``/``mock.patch`` on
``nexus_config_dir`` in ``tests/``, and a tracked count of the ``from
nexus.config import nexus_config_dir`` by-value imports in ``src/nexus/``.

THE DEFECT CLASS (nexus-78blw, T2 ``nexus/gc-purge-marker-xdist-leak-
2026-08-20``): a consumer module that does ``from nexus.config import
nexus_config_dir`` AT MODULE LEVEL captures the function BY VALUE, once,
at first import. ``nexus_config_dir()`` itself just reads
``NEXUS_CONFIG_DIR`` from the environment each call (``config.py:492``)
-- so patching the real attribute has no lasting effect on a module-level
import UNLESS that consumer happens to be first-imported while the patch
is active. When a test does::

    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)

and, inside that patched window, some OTHER module is first-imported
(directly or via a deferred/lazy import chain), that module's own
module-level copy of ``nexus_config_dir`` is bound to the TEST's lambda
forever after -- ``monkeypatch`` teardown restores ``nexus.config``'s
attribute, but has no idea the lambda leaked into a second module's
namespace. Every later call in that worker process, from ANY test,
resolves against the dead test's ``tmp_path``. Hit twice: ``tests/
test_false_clean_diagnostics_service_mode.py:395-407`` (v7.11.0, PR
#1467) and ``tests/test_doctor_cmd.py:33`` -> ``src/nexus/
gc_purge_marker.py`` (fixed 2026-08-20).

THE FIX, applied to 48 test sites in the nexus-78blw sweep (45
``monkeypatch.setattr`` + 3 ``unittest.mock.patch`` sites): replace the
``setattr``/``patch`` with ``monkeypatch.setenv("NEXUS_CONFIG_DIR",
str(X))`` (or, for a bare context-manager ``patch(...)`` with no
``monkeypatch`` fixture in scope, ``patch.dict(os.environ,
{"NEXUS_CONFIG_DIR": str(X)})``, the equivalent for that idiom).
``nexus_config_dir()`` reads the env var fresh on every call regardless
of which module's namespace holds the reference, so env-based patching
is immune to the by-value-capture class entirely -- there is no
"module.copy" to poison. The 7 sites that remain as ``setattr``/
``patch`` on ``nexus_config_dir`` are exempted for two distinct reasons
-- see ``_SETATTR_EXEMPT`` below for the per-entry detail:

- 3 are deliberate regression pins for the leak mechanism ITSELF
  (proving a consumer resolves through ``nexus.config`` at call time,
  not a frozen import). Converting these to ``setenv`` would make them
  incapable of ever catching the bug they exist to catch, since
  ``setenv`` cannot distinguish a correctly-resolving consumer from one
  whose module-level import already captured the REAL (unpoisoned)
  function.
- 4 target a CONSUMER module's own already-captured attribute directly
  (``nexus.commands.daemon.nexus_config_dir`` /
  ``aw.nexus_config_dir``) because that consumer itself still has an
  unfixed MODULE-LEVEL by-value import (see Sweep 2 below) -- the only
  seam that reliably reaches it until the consumer is converted to the
  module-attribute pattern.

TWO SEPARATE SWEEPS in this file:

1. ``test_no_new_nexus_config_dir_setattr`` -- a ratchet over every
   ``monkeypatch.setattr``/``mock.patch``/``mock.patch.object`` call in
   ``tests/`` that targets ``nexus_config_dir`` (dotted-string form or
   two-arg attribute form). Exact-equality ceiling, mirroring
   ``tests/test_pipefail_early_exit_consumer_lint.py`` /
   ``tests/test_mode_declarations_are_explicit.py``: this set may only
   SHRINK (a site gets converted to ``setenv``) or grow with a new,
   individually-documented entry AND a conscious ceiling bump in the
   same diff.
2. ``test_module_level_by_value_import_count`` /
   ``test_module_level_by_value_imports_are_tracked`` -- an AST census
   of ``from nexus.config import nexus_config_dir`` in ``src/nexus/``,
   split by MODULE-LEVEL (the genuinely vulnerable shape -- captured
   once at import time, exactly the ``gc_purge_marker.py`` bug class)
   vs. function-scoped/deferred (safe: Python re-executes a
   function-local import statement, and therefore re-resolves the
   CURRENT ``nexus.config.nexus_config_dir``, on every call -- this repo
   marks these ``# noqa: PLC0415`` throughout, and the census confirms
   they are not the same defect). Both counts are tracked ratchets so a
   NEW module-level by-value import cannot land silently; the 4 already
   in the tree are allowlisted with reasons (fix pattern: ``from nexus
   import config as _config`` + ``_config.nexus_config_dir()``, the
   pattern ``gc_purge_marker.py`` / ``commands/t3.py`` /
   ``commands/_helpers.py`` already use) -- converting them is OUT OF
   SCOPE for the dispatch that authored this lint (fenced to test files
   + this new lint file only; see nexus-78blw comment thread) and is
   left for a follow-up bead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"
TESTS_ROOT = REPO_ROOT / "tests"

_TARGET = "nexus_config_dir"


def _iter_py_files(root: Path) -> list[Path]:
    """Every tracked ``*.py`` under *root*, skipping the stale captured-log
    snapshot directory (``tests/containers/out/``) -- those files are CI
    output artifacts checked into the tree, not live pytest sources, and
    a stale ``monkeypatch.setattr`` string inside a log line is not a
    violation of anything.
    """
    return sorted(
        p for p in root.rglob("*.py") if "containers/out" not in p.as_posix()
    )


# ── Sweep 1: tests/ setattr/patch targeting nexus_config_dir ───────────────


def _setattr_hits(path: Path) -> list[tuple[int, str]]:
    """Return ``(1-based lineno, source snippet)`` for every call in *path*
    that patches ``nexus_config_dir``, in either recognized shape:

    - dotted-string target: ``monkeypatch.setattr("nexus.config.
      nexus_config_dir", ...)`` / ``mock.patch("...nexus_config_dir", ...)``
    - two-arg attribute target: ``monkeypatch.setattr(TARGET,
      "nexus_config_dir", ...)`` / ``mock.patch.object(TARGET,
      "nexus_config_dir", ...)`` -- regardless of what ``TARGET`` is,
      since the two-arg form is exactly the shape used both by sites that
      alias ``nexus.config`` (``init_mod._config``) and by sites that
      patch a CONSUMER module's own by-value-captured copy directly
      (``aw.nexus_config_dir``) -- both are in scope for this sweep; the
      3 legitimate uses are individually allowlisted below, not exempted
      by shape.
    """
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        call_name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None
        )
        if call_name not in ("setattr", "patch", "object"):
            continue
        args = node.args
        if not args:
            continue
        first = args[0]
        matched = False
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if first.value == _TARGET or first.value.endswith(f".{_TARGET}"):
                matched = True
        if not matched and len(args) >= 2:
            second = args[1]
            if isinstance(second, ast.Constant) and second.value == _TARGET:
                matched = True
        if matched:
            snippet = (ast.get_source_segment(text, node) or "").splitlines()[0]
            hits.append((node.lineno, snippet.strip()))
    return hits


def _all_setattr_hits() -> dict[str, list[tuple[int, str]]]:
    out: dict[str, list[tuple[int, str]]] = {}
    for path in _iter_py_files(TESTS_ROOT):
        hits = _setattr_hits(path)
        if hits:
            out[path.relative_to(REPO_ROOT).as_posix()] = hits
    return out


# Ratchet exemption set (mirrors tests/test_pipefail_early_exit_consumer_
# lint.py's `_PIPEFAIL_EARLY_EXIT_EXEMPT` / tests/
# test_mode_declarations_are_explicit.py's `_MODE_LINT_EXCLUDE_NODEIDS`):
# "relative/path.py:LINENO" -> may only SHRINK (site converted to
# `monkeypatch.setenv("NEXUS_CONFIG_DIR", ...)`) or grow with a new,
# individually-documented entry AND a conscious ceiling bump in the same
# diff. Every entry below is a REAL, confirmed instance deliberately NOT
# converted in the nexus-78blw sweep because it deliberately tests the
# by-value-capture mechanism itself, or because the consumer it targets
# still has an unfixed module-level by-value import out of that sweep's
# touch-fence.
_SETATTR_EXEMPT: frozenset[str] = frozenset({
    # test_marker_path_resolves_config_dir_at_call_time: the regression pin
    # for the ORIGINAL gc_purge_marker.py incident. It deliberately patches
    # nexus.config.nexus_config_dir, importlib.reload()s the consumer
    # INSIDE that patched window (simulating a first-import-under-patch),
    # then asserts the consumer follows NEXUS_CONFIG_DIR again once the
    # patch is undone. Converting to setenv would make this test incapable
    # of ever catching a by-value-capture regression -- setenv cannot
    # distinguish a consumer that resolves through nexus.config at call
    # time from one whose module-level import already captured the real
    # (unpoisoned) function; only literally replacing the function object
    # and observing whether the replacement propagates proves the shape.
    "tests/test_health_service_checks.py:2657",
    # test_backfill_state_path_uses_config_module_attr_not_frozen_import:
    # same class, for nexus.commands.t3._backfill_state_path(). Proves the
    # consumer reads nexus.config.nexus_config_dir via module-attribute
    # access (`_config.nexus_config_dir()`) rather than a `from nexus.config
    # import nexus_config_dir` binding captured once at t3.py's own import
    # time -- same reasoning as the entry above.
    "tests/commands/test_t3_backfill_state_path.py:55",
    # test_enqueue_hook_service_mode_reaches_daemon_spawn: targets
    # nexus.aspect_worker's OWN attribute (`aw.nexus_config_dir`), not
    # nexus.config's. src/nexus/aspect_worker.py:72 still carries an
    # unfixed MODULE-LEVEL `from nexus.config import nexus_config_dir`
    # by-value import (see `_MODULE_LEVEL_BY_VALUE_IMPORT_EXEMPT` below) --
    # setattr(aw, "nexus_config_dir", ...) is the only seam that reliably
    # reaches THIS consumer's already-captured reference regardless of
    # import order; a plain NEXUS_CONFIG_DIR env var would only work if
    # aspect_worker.py itself were converted to the module-attribute
    # pattern first (out of the nexus-78blw dispatch's touch-fence --
    # follow-up bead needed).
    "tests/daemon/test_aspect_worker_spawn.py:179",
    # tests/daemon/test_restart_stale_engine_convergence.py (4 sites, all
    # `patch("nexus.commands.daemon.nexus_config_dir", return_value=...)`):
    # same class as the aspect_worker entry above. src/nexus/commands/
    # daemon.py:35 has `from nexus.config import
    # nexus_config_dir` at MODULE LEVEL (see `_MODULE_LEVEL_BY_VALUE_
    # IMPORT_EXEMPT` below) -- patching the string path
    # "nexus.commands.daemon.nexus_config_dir" reaches THAT captured
    # attribute directly, which `monkeypatch.setenv("NEXUS_CONFIG_DIR",
    # ...)` cannot do until commands/daemon.py is converted to the
    # module-attribute pattern (out of touch-fence, follow-up bead).
    "tests/daemon/test_restart_stale_engine_convergence.py:32",
    "tests/daemon/test_restart_stale_engine_convergence.py:130",
    "tests/daemon/test_restart_stale_engine_convergence.py:163",
    "tests/daemon/test_restart_stale_engine_convergence.py:279",
})
_SETATTR_EXEMPT_CEILING = 7


def test_no_new_nexus_config_dir_setattr() -> None:
    hits = _all_setattr_hits()
    flat: list[str] = []
    for rel, entries in sorted(hits.items()):
        for lineno, snippet in entries:
            key = f"{rel}:{lineno}"
            if key in _SETATTR_EXEMPT:
                continue
            flat.append(f"{key}  {snippet}")
    assert not flat, (
        "The following test sites patch `nexus_config_dir` via setattr/"
        "patch rather than `monkeypatch.setenv(\"NEXUS_CONFIG_DIR\", "
        "str(...))` (nexus-78blw -- by-value imports elsewhere in src/ "
        "capture a setattr'd lambda across the rest of the worker "
        "process; setenv is immune since it reads fresh every call):\n  "
        + "\n  ".join(flat)
        + "\n\nFix: convert to `monkeypatch.setenv(\"NEXUS_CONFIG_DIR\", "
        "str(X))`. If the site deliberately tests the by-value-capture "
        "mechanism itself (proving a consumer resolves nexus_config_dir "
        "at call time rather than import time), add it to `_SETATTR_"
        "EXEMPT` above with a documented reason and bump `_SETATTR_"
        "EXEMPT_CEILING` in the same diff."
    )


def test_setattr_exempt_ratchet() -> None:
    assert len(_SETATTR_EXEMPT) == _SETATTR_EXEMPT_CEILING, (
        f"_SETATTR_EXEMPT has {len(_SETATTR_EXEMPT)} entries, expected "
        f"exactly {_SETATTR_EXEMPT_CEILING}. This set may only shrink "
        "(convert a site to `monkeypatch.setenv`) or grow with a "
        "documented per-entry rationale plus a conscious bump of "
        "`_SETATTR_EXEMPT_CEILING` in this file."
    )


def test_setattr_exempt_entries_are_live() -> None:
    """Every exempted ``path:lineno`` must still name a real setattr/patch
    call targeting ``nexus_config_dir`` at that exact line -- a stale entry
    (file edited, line shifted, site converted without removing the
    exemption) is a free, unrationalised exclusion slot the exact-equality
    ratchet above cannot see on its own.
    """
    live = _all_setattr_hits()
    live_keys = {
        f"{rel}:{lineno}" for rel, entries in live.items() for lineno, _ in entries
    }
    dead = sorted(e for e in _SETATTR_EXEMPT if e not in live_keys)
    assert not dead, (
        f"{len(dead)} `_SETATTR_EXEMPT` entries no longer resolve to a "
        f"live nexus_config_dir setattr/patch call:\n  " + "\n  ".join(dead)
        + "\n\nRetarget if the line moved, or delete the entry and lower "
        "`_SETATTR_EXEMPT_CEILING` if the site was converted to setenv."
    )


# ── Sweep 2: src/nexus/ module-level by-value imports ──────────────────────


def _classify_import_sites(path: Path) -> tuple[list[int], list[int]]:
    """Return ``(module_level_linenos, function_scoped_linenos)`` for every
    ``from nexus.config import ... nexus_config_dir ...`` in *path*.

    A single depth-first pass tracking whether the current node is nested
    inside any ``FunctionDef``/``AsyncFunctionDef`` -- deliberately NOT
    ``ast.walk`` per function (which double-counts an import nested inside
    TWO enclosing functions once per enclosing level).
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return [], []
    module_level: list[int] = []
    function_scoped: list[int] = []

    def _is_target(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.ImportFrom)
            and node.module == "nexus.config"
            and any(alias.name == _TARGET for alias in node.names)
        )

    def _visit(node: ast.AST, in_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if _is_target(child):
                (function_scoped if in_function else module_level).append(
                    child.lineno
                )
            _visit(
                child,
                in_function or isinstance(
                    child, ast.FunctionDef | ast.AsyncFunctionDef
                ),
            )

    _visit(tree, False)
    return module_level, function_scoped


def _all_import_sites() -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    module_level: dict[str, list[int]] = {}
    function_scoped: dict[str, list[int]] = {}
    for path in _iter_py_files(SRC_ROOT):
        ml, fs = _classify_import_sites(path)
        rel = path.relative_to(REPO_ROOT).as_posix()
        if ml:
            module_level[rel] = ml
        if fs:
            function_scoped[rel] = fs
    return module_level, function_scoped


# Ratchet exemption set for the genuinely vulnerable shape: a MODULE-LEVEL
# `from nexus.config import nexus_config_dir` is captured ONCE at first
# import and never re-resolves -- exactly the gc_purge_marker.py bug
# class. Fix pattern (already used by src/nexus/gc_purge_marker.py,
# src/nexus/commands/t3.py, src/nexus/commands/_helpers.py): `from nexus
# import config as _config` + call `_config.nexus_config_dir()` at each
# use site, so a patch applied AFTER this module is imported still takes
# effect. Not converted in the nexus-78blw sweep that authored this lint
# -- that dispatch was fenced to test files + this new lint file only
# (concurrent sibling work owned other src/ surfaces); left for a
# follow-up bead. This set may only shrink (a site gets converted) or
# grow with a documented per-entry rationale plus a conscious ceiling
# bump in the same diff.
_MODULE_LEVEL_BY_VALUE_IMPORT_EXEMPT: frozenset[str] = frozenset({
    "src/nexus/aspect_worker.py:72",
    "src/nexus/commands/daemon.py:35",
    "src/nexus/migration/state.py:41",
    "src/nexus/console/routes/health.py:17",
})
_MODULE_LEVEL_BY_VALUE_IMPORT_CEILING = 4

# Informational ratchet on the TOTAL by-value-import count (module-level +
# function-scoped/deferred). The function-scoped majority is SAFE (Python
# re-executes a function-local import statement -- and therefore
# re-resolves the CURRENT nexus.config.nexus_config_dir -- on every call;
# this repo marks these `# noqa: PLC0415` throughout), so this ratchet is
# not a "must fix" gate the way the module-level one is. It exists so a
# NEW site (of either shape) is a conscious, reviewed addition rather than
# a silent one -- growth is expected and fine as the codebase grows;
# bump the ceiling in the same diff.
_TOTAL_BY_VALUE_IMPORT_CEILING = 81


def test_module_level_by_value_import_ratchet() -> None:
    module_level, _ = _all_import_sites()
    flat = [
        f"{rel}:{lineno}"
        for rel, linenos in sorted(module_level.items())
        for lineno in linenos
    ]
    unexempted = [e for e in flat if e not in _MODULE_LEVEL_BY_VALUE_IMPORT_EXEMPT]
    assert not unexempted, (
        "The following src/nexus/ modules import `nexus_config_dir` BY "
        "VALUE at MODULE LEVEL (nexus-78blw / gc_purge_marker.py bug "
        "class -- captured once at first import, permanently blind to a "
        "later patch of nexus.config.nexus_config_dir if this module was "
        "first-imported inside another test's patched window):\n  "
        + "\n  ".join(unexempted)
        + "\n\nFix: `from nexus import config as _config` + call "
        "`_config.nexus_config_dir()` at each use site. If genuinely "
        "deferred out of scope for this change, add to "
        "`_MODULE_LEVEL_BY_VALUE_IMPORT_EXEMPT` with a documented reason "
        "and bump `_MODULE_LEVEL_BY_VALUE_IMPORT_CEILING` in the same diff."
    )
    assert len(_MODULE_LEVEL_BY_VALUE_IMPORT_EXEMPT) == (
        _MODULE_LEVEL_BY_VALUE_IMPORT_CEILING
    ), (
        f"_MODULE_LEVEL_BY_VALUE_IMPORT_EXEMPT has "
        f"{len(_MODULE_LEVEL_BY_VALUE_IMPORT_EXEMPT)} entries, expected "
        f"exactly {_MODULE_LEVEL_BY_VALUE_IMPORT_CEILING}. Shrink by "
        "converting a site to the module-attribute pattern; grow only "
        "with a documented rationale plus a conscious ceiling bump."
    )


def test_module_level_by_value_import_exempt_entries_are_live() -> None:
    module_level, _ = _all_import_sites()
    live_keys = {
        f"{rel}:{lineno}" for rel, linenos in module_level.items() for lineno in linenos
    }
    dead = sorted(e for e in _MODULE_LEVEL_BY_VALUE_IMPORT_EXEMPT if e not in live_keys)
    assert not dead, (
        f"{len(dead)} `_MODULE_LEVEL_BY_VALUE_IMPORT_EXEMPT` entries no "
        f"longer resolve to a live module-level nexus_config_dir "
        f"by-value import:\n  " + "\n  ".join(dead)
        + "\n\nRetarget if the line moved, or delete the entry and lower "
        "`_MODULE_LEVEL_BY_VALUE_IMPORT_CEILING` if the site was fixed."
    )


def test_total_by_value_import_count_is_tracked() -> None:
    module_level, function_scoped = _all_import_sites()
    total = sum(len(v) for v in module_level.values()) + sum(
        len(v) for v in function_scoped.values()
    )
    assert total == _TOTAL_BY_VALUE_IMPORT_CEILING, (
        f"src/nexus/ now has {total} `from nexus.config import "
        f"nexus_config_dir` by-value imports (module-level + "
        f"function-scoped/deferred), expected exactly "
        f"{_TOTAL_BY_VALUE_IMPORT_CEILING} (nexus-78blw census). This is "
        "an informational ratchet, not a correctness gate -- most of "
        "these are safe, function-scoped `# noqa: PLC0415` deferred "
        "imports. Bump `_TOTAL_BY_VALUE_IMPORT_CEILING` in the same diff "
        "as whatever added or removed a site; if the new site is "
        "MODULE-LEVEL, it also needs a `_MODULE_LEVEL_BY_VALUE_IMPORT_"
        "EXEMPT` entry (or, preferably, the module-attribute fix instead)."
    )


# ── falsification controls (non-vacuity) ────────────────────────────────


def test_setattr_detector_catches_dotted_string_form(tmp_path: Path) -> None:
    bad = tmp_path / "test_bad_dotted.py"
    bad.write_text(
        'def test_x(monkeypatch, tmp_path):\n'
        '    monkeypatch.setattr("nexus.config.nexus_config_dir", '
        'lambda: tmp_path)\n'
    )
    hits = _setattr_hits(bad)
    assert [h[0] for h in hits] == [2]


def test_setattr_detector_catches_two_arg_attribute_form(tmp_path: Path) -> None:
    bad = tmp_path / "test_bad_attr.py"
    bad.write_text(
        'def test_x(monkeypatch, tmp_path):\n'
        '    monkeypatch.setattr(some_mod, "nexus_config_dir", '
        'lambda: tmp_path)\n'
    )
    hits = _setattr_hits(bad)
    assert [h[0] for h in hits] == [2]


def test_setattr_detector_catches_mock_patch_object(tmp_path: Path) -> None:
    bad = tmp_path / "test_bad_patch_object.py"
    bad.write_text(
        'from unittest import mock\n'
        'def test_x():\n'
        '    with mock.patch.object(some_mod, "nexus_config_dir"):\n'
        '        pass\n'
    )
    hits = _setattr_hits(bad)
    assert [h[0] for h in hits] == [3]


def test_setattr_detector_ignores_unrelated_setattr(tmp_path: Path) -> None:
    benign = tmp_path / "test_benign.py"
    benign.write_text(
        'def test_x(monkeypatch, tmp_path):\n'
        '    monkeypatch.setattr("nexus.config.is_local_mode", '
        'lambda: True)\n'
        '    monkeypatch.setattr(some_mod, "other_attr", 1)\n'
    )
    assert _setattr_hits(benign) == []


def test_setattr_detector_ignores_setenv(tmp_path: Path) -> None:
    """The fix shape itself must never be flagged."""
    benign = tmp_path / "test_fixed.py"
    benign.write_text(
        'def test_x(monkeypatch, tmp_path):\n'
        '    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))\n'
    )
    assert _setattr_hits(benign) == []


def test_import_classifier_distinguishes_module_level_from_function_scoped(
    tmp_path: Path,
) -> None:
    mixed = tmp_path / "mixed.py"
    mixed.write_text(
        "from nexus.config import nexus_config_dir\n"  # module-level, line 1
        "\n"
        "\n"
        "def helper() -> None:\n"
        "    from nexus.config import nexus_config_dir  # noqa: PLC0415\n"
        "    return nexus_config_dir()\n"
    )
    module_level, function_scoped = _classify_import_sites(mixed)
    assert module_level == [1]
    assert function_scoped == [5]


def test_import_classifier_ignores_unrelated_imports(tmp_path: Path) -> None:
    benign = tmp_path / "benign.py"
    benign.write_text(
        "from nexus.config import is_local_mode\n"
        "from pathlib import Path\n"
    )
    module_level, function_scoped = _classify_import_sites(benign)
    assert module_level == []
    assert function_scoped == []


def test_import_classifier_handles_nested_function_once(tmp_path: Path) -> None:
    """A doubly-nested function-local import must be counted exactly once,
    not once per enclosing function level -- the failure mode a naive
    `ast.walk(fn)`-per-FunctionDef implementation would produce.
    """
    nested = tmp_path / "nested.py"
    nested.write_text(
        "def outer():\n"
        "    def inner():\n"
        "        from nexus.config import nexus_config_dir  # noqa: PLC0415\n"
        "        return nexus_config_dir()\n"
        "    return inner\n"
    )
    module_level, function_scoped = _classify_import_sites(nested)
    assert module_level == []
    assert function_scoped == [3]
