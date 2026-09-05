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
``monkeypatch.setattr`` + 3 ``unittest.mock.patch`` sites) plus 5 more in
the nexus-grg79 follow-up (1 ``monkeypatch.setattr`` + 4
``unittest.mock.patch`` sites, once their consumers' own src/ module-level
imports were converted -- see below): replace the ``setattr``/``patch``
with ``monkeypatch.setenv("NEXUS_CONFIG_DIR", str(X))`` (or, for a bare
context-manager ``patch(...)`` with no ``monkeypatch`` fixture in scope,
``patch.dict(os.environ, {"NEXUS_CONFIG_DIR": str(X)})``, the equivalent
for that idiom). ``nexus_config_dir()`` reads the env var fresh on every
call regardless of which module's namespace holds the reference, so
env-based patching is immune to the by-value-capture class entirely --
there is no "module.copy" to poison. The sites that remain as
``setattr``/``patch`` on ``nexus_config_dir`` are exempted for one
reason -- see ``_SETATTR_EXEMPT`` below for the per-entry detail: each is
a deliberate regression pin for the leak mechanism ITSELF (proving a
consumer resolves through ``nexus.config`` at call time, not a frozen
import). Converting these to ``setenv`` would make them incapable of
ever catching the bug they exist to catch, since ``setenv`` cannot
distinguish a correctly-resolving consumer from one whose module-level
import already captured the REAL (unpoisoned) function -- this is also
why nexus-grg79's own 4 new regression pins (one src/ module apiece) had
to add exactly ONE new exempted site: every genuine reproduction of this
defect class necessarily patches ``nexus.config.nexus_config_dir`` at
least once, so the 4 pins share ONE physical ``monkeypatch.setattr`` call
site (a context-manager helper) rather than exempting four.

nexus-grg79 (T2 ``nexus/78blw-dev-notes-2026-08-21``) converted the 4
formerly-exempted CONSUMER-side sites (``nexus.commands.daemon.
nexus_config_dir`` x4 / ``aw.nexus_config_dir`` x1) to ``setenv``/
``patch.dict`` now that Sweep 2's 4 module-level by-value imports are
fixed (module-attribute access via ``_config.nexus_config_dir()``) --
env-based patching now reaches every consumer regardless of import
order, so the consumer-attribute seam is no longer needed.

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
   NEW module-level by-value import cannot land silently. The 4 that
   were in the tree when this lint was authored (fix pattern: ``from
   nexus import config as _config`` + ``_config.nexus_config_dir()``,
   the pattern ``gc_purge_marker.py`` / ``commands/t3.py`` /
   ``commands/_helpers.py`` already use) were converted by the
   nexus-grg79 follow-up -- the allowlist is now EMPTY (ceiling 0);
   converting them had been OUT OF SCOPE for the dispatch that authored
   this lint only (fenced to test files
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
    """Return ``(1-based lineno, source snippet)`` for every site in *path*
    that patches ``nexus_config_dir``, in any recognized shape:

    - dotted-string target: ``monkeypatch.setattr("nexus.config.
      nexus_config_dir", ...)`` / ``mock.patch("...nexus_config_dir", ...)``
    - two-arg attribute target: ``monkeypatch.setattr(TARGET,
      "nexus_config_dir", ...)`` / ``mock.patch.object(TARGET,
      "nexus_config_dir", ...)`` -- regardless of what ``TARGET`` is,
      since the two-arg form is exactly the shape used both by sites that
      alias ``nexus.config`` (``init_mod._config``) and by sites that
      patch a CONSUMER module's own by-value-captured copy directly
      (``aw.nexus_config_dir``) -- both are in scope for this sweep; the
      legitimate uses are individually allowlisted below, not exempted
      by shape.
    - direct attribute ASSIGNMENT: ``nexus.config.nexus_config_dir = ...``
      (nexus-grg79 comment thread) -- the non-context-manager form of the
      same by-value-capture hazard; ``monkeypatch``/``mock.patch`` at
      least restore the original attribute on teardown, a bare assignment
      restores nothing at all, so this shape is strictly worse and must
      not become a silent escape hatch from Sweep 1.
    - ``mock.patch.multiple(TARGET, nexus_config_dir=...)`` kwarg form
      (nexus-grg79 comment thread) -- the multi-attribute cousin of
      ``patch.object``; a ``nexus_config_dir=`` keyword here is the exact
      same hazard as the two-arg ``patch.object`` form above, just spread
      across ``patch.multiple``'s kwargs instead of positional args.

    Neither of the last two shapes existed anywhere in the tree as of
    nexus-grg79 -- detection is proven via the synthetic-input
    falsification tests below (``test_setattr_detector_catches_direct_
    assignment`` / ``test_setattr_detector_catches_patch_multiple_kwarg``),
    not via any real hit in this repo.
    """
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == _TARGET:
                    snippet = (ast.get_source_segment(text, node) or "").splitlines()[0]
                    hits.append((node.lineno, snippet.strip()))
                    break
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        call_name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None
        )
        if call_name == "multiple":
            if any(kw.arg == _TARGET for kw in node.keywords):
                snippet = (ast.get_source_segment(text, node) or "").splitlines()[0]
                hits.append((node.lineno, snippet.strip()))
            continue
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
# converted, because it deliberately tests the by-value-capture mechanism
# itself -- nexus-grg79 converted the OTHER reason this set used to carry
# entries for (a consumer's own unfixed module-level by-value import,
# see `_MODULE_LEVEL_BY_VALUE_IMPORT_EXEMPT` below, now empty), so every
# remaining entry here is a genuine mechanism regression pin.
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
    "tests/test_health_service_checks.py:2668",
    # test_backfill_state_path_uses_config_module_attr_not_frozen_import:
    # same class, for nexus.commands.t3._backfill_state_path(). Proves the
    # consumer reads nexus.config.nexus_config_dir via module-attribute
    # access (`_config.nexus_config_dir()`) rather than a `from nexus.config
    # import nexus_config_dir` binding captured once at t3.py's own import
    # time -- same reasoning as the entry above.
    "tests/commands/test_t3_backfill_state_path.py:55",
    # tests/test_nexus_config_dir_call_time_resolution.py's
    # `_poisoned_then_reloaded` helper (nexus-grg79): the SAME regression-
    # pin mechanism as the two entries above, applied to the 4 consumers
    # nexus-78blw's census found with an unfixed MODULE-LEVEL by-value
    # import (aspect_worker.py, commands/daemon.py, migration/state.py,
    # console/routes/health.py). Deliberately factored to ONE shared
    # context-manager call site instead of one per consumer -- every
    # genuine reproduction of this defect class necessarily patches
    # nexus.config.nexus_config_dir at least once, so sharing the helper
    # keeps this ratchet's growth to +1 for 4 new regression pins instead
    # of +4.
    "tests/test_nexus_config_dir_call_time_resolution.py:66",
})
_SETATTR_EXEMPT_CEILING = 3


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
    site in *path* that captures ``nexus_config_dir`` BY VALUE, in either
    of two shapes:

    1. ``from nexus.config import ... nexus_config_dir ...`` -- the
       ORIGINAL nexus-78blw shape.
    2. A module-attribute ALIAS ASSIGNMENT -- ``X = <alias>.nexus_config_dir``
       where ``<alias>`` is a local name bound, at MODULE level, to the
       ``nexus.config`` module object itself (``from nexus import config
       [as <alias>]`` / ``import nexus.config as <alias>``). Found by the
       nexus-grg79 critic (T2 ``nexus/grg79-critique-2026-08-21``): that
       bead's OWN diff made ``from nexus import config as _config`` the
       canonical fix pattern across four MORE files, which is exactly the
       context that invites someone to later write ``X = _config.
       nexus_config_dir`` as a convenience alias -- binding the FUNCTION
       OBJECT once, at whatever it resolved to at assignment time, and
       reintroducing the identical by-value-capture hazard wearing the
       new convention's clothes. Only the ``_config.nexus_config_dir()``
       CALL form -- module-attribute access AT THE CALL SITE, never bound
       to a name first -- is actually safe.

       Scope: covers the ``_config``-alias and plain ``from nexus import
       config`` spellings the critic demonstrated, plus ``import
       nexus.config as X`` for free (identical single-hop attribute shape
       once the alias set is collected). Does NOT cover the bare ``import
       nexus.config`` two-hop chain (``nexus.config.nexus_config_dir``,
       no ``as``) -- that needs a second attribute-chain shape and
       resolving whether ``nexus`` itself is bound, which is a
       restructuring rather than an added case; nothing in the tree uses
       that form today. Also does not follow imports nested inside a
       module-level ``if``/``try`` block (matches this file's existing
       simplicity level; nothing in the tree does that either), and does
       not chase a SECOND-ORDER alias (``_real = _config`` followed by
       ``_CACHED = _real.nexus_config_dir``) -- the alias set is collected
       from import statements only, not from name-to-name rebinding, so
       that chain evades detection. Confirmed empirically during the
       nexus-grg79 round-2 review; same complexity-vs-value tradeoff as
       the two-hop gap above, and likewise unused in the tree. Shadowing
       in the other direction (rebinding the alias to something else
       before the grab) still FLAGS, which is the safe failure direction.

    A single depth-first pass tracking whether the current node is nested
    inside any ``FunctionDef``/``AsyncFunctionDef`` -- deliberately NOT
    ``ast.walk`` per function (which double-counts an import nested inside
    TWO enclosing functions once per enclosing level). A function-scoped
    alias assignment is SAFE for the same reason a function-scoped
    by-value import is: the attribute access re-executes, and therefore
    re-resolves the CURRENT ``nexus.config.nexus_config_dir``, every time
    that line runs.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return [], []
    module_level: list[int] = []
    function_scoped: list[int] = []

    # Local names bound, at MODULE level ONLY, to the `nexus.config` module
    # object -- shape 2's prerequisite. Only top-level statements count.
    config_aliases: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "nexus":
            for alias in stmt.names:
                if alias.name == "config":
                    config_aliases.add(alias.asname or alias.name)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == "nexus.config" and alias.asname:
                    config_aliases.add(alias.asname)

    def _is_import_target(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.ImportFrom)
            and node.module == "nexus.config"
            and any(alias.name == _TARGET for alias in node.names)
        )

    def _is_alias_assign_target(node: ast.AST) -> bool:
        if not isinstance(node, ast.Assign):
            return False
        value = node.value
        return (
            isinstance(value, ast.Attribute)
            and value.attr == _TARGET
            and isinstance(value.value, ast.Name)
            and value.value.id in config_aliases
        )

    def _visit(node: ast.AST, in_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if _is_import_target(child) or _is_alias_assign_target(child):
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
# effect. The 4 sites that were in the tree when the nexus-78blw sweep
# authored this lint (fenced to test files + this new lint file only;
# converting src/ was left for a follow-up bead) were ALL converted by
# the nexus-grg79 follow-up -- this set is now EMPTY. It may only shrink
# further (there is nothing left to shrink) or grow with a documented
# per-entry rationale plus a conscious ceiling bump in the same diff, if
# a new module-level by-value import lands.
_MODULE_LEVEL_BY_VALUE_IMPORT_EXEMPT: frozenset[str] = frozenset()
_MODULE_LEVEL_BY_VALUE_IMPORT_CEILING = 0

# Informational ratchet on the TOTAL by-value-import count (module-level +
# function-scoped/deferred). The function-scoped majority is SAFE (Python
# re-executes a function-local import statement -- and therefore
# re-resolves the CURRENT nexus.config.nexus_config_dir -- on every call;
# this repo marks these `# noqa: PLC0415` throughout), so this ratchet is
# not a "must fix" gate the way the module-level one is. It exists so a
# NEW site (of either shape) is a conscious, reviewed addition rather than
# a silent one -- growth is expected and fine as the codebase grows;
# bump the ceiling in the same diff. Dropped from 81 to 77 by nexus-grg79:
# the 4 module-level sites it converted no longer match this census at
# all (`from nexus import config as _config` is a different import shape
# than `from nexus.config import nexus_config_dir`), so the total shrank
# by exactly the 4 it fixed rather than shifting into the function-scoped
# bucket.
# nexus-lgdel: src/nexus/remediation/consent.py's function-scoped
# `from nexus.config import nexus_config_dir` import (inside
# remediation_opt_in()) was deleted with the whole file, shrinking the
# total by 1.
_TOTAL_BY_VALUE_IMPORT_CEILING = 78  # +1: upgrade_finish.py aspect-worker respawn (nexus-06fu4/restart-stale fix); the 3o4lt bump to 79 was reverted by switching doc_indexer to a module import


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


# ── nexus-grg79 comment thread: two new patterns, PROVEN via synthetic
# input since neither exists anywhere in the tree today (a real hit would
# be the ratchet's own job to catch; these prove the DETECTOR itself
# works, which a ceiling of zero real hits cannot demonstrate on its own).


def test_setattr_detector_catches_direct_attribute_assignment(
    tmp_path: Path,
) -> None:
    """``nexus.config.nexus_config_dir = ...`` -- a bare assignment, never
    restored on teardown, is strictly worse than the context-manager forms
    above and must not be a silent escape hatch from this ratchet."""
    bad = tmp_path / "test_bad_direct_assign.py"
    bad.write_text(
        'import nexus.config\n'
        'def test_x():\n'
        '    nexus.config.nexus_config_dir = lambda: "/poisoned"\n'
    )
    hits = _setattr_hits(bad)
    assert [h[0] for h in hits] == [3]


def test_setattr_detector_catches_patch_multiple_kwarg(tmp_path: Path) -> None:
    """``mock.patch.multiple(TARGET, nexus_config_dir=...)`` -- the
    multi-attribute cousin of ``patch.object``; a ``nexus_config_dir=``
    keyword here is the same hazard spread across ``patch.multiple``'s
    kwargs instead of a positional two-arg form."""
    bad = tmp_path / "test_bad_patch_multiple.py"
    bad.write_text(
        'from unittest import mock\n'
        'def test_x():\n'
        '    with mock.patch.multiple(\n'
        '        some_mod, nexus_config_dir=lambda: "/poisoned",\n'
        '    ):\n'
        '        pass\n'
    )
    hits = _setattr_hits(bad)
    assert [h[0] for h in hits] == [3]


def test_setattr_detector_ignores_unrelated_attribute_assignment(
    tmp_path: Path,
) -> None:
    """An assignment to some OTHER attribute must not be flagged -- proves
    the direct-assignment detector matches on the target attribute name,
    not on every module-level ``Attribute = ...`` statement."""
    benign = tmp_path / "test_benign_assign.py"
    benign.write_text(
        'import nexus.config\n'
        'def test_x():\n'
        '    nexus.config.is_local_mode = lambda: True\n'
        '    some_mod.other_attr = 1\n'
    )
    assert _setattr_hits(benign) == []


def test_setattr_detector_ignores_unrelated_patch_multiple_kwarg(
    tmp_path: Path,
) -> None:
    """A ``patch.multiple(...)`` call with unrelated kwargs must not be
    flagged -- proves the detector matches on the ``nexus_config_dir=``
    keyword specifically, not on every ``patch.multiple`` call."""
    benign = tmp_path / "test_benign_patch_multiple.py"
    benign.write_text(
        'from unittest import mock\n'
        'def test_x():\n'
        '    with mock.patch.multiple(some_mod, is_local_mode=lambda: True):\n'
        '        pass\n'
    )
    assert _setattr_hits(benign) == []


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


# ── nexus-grg79 critique: module-attribute ALIAS ASSIGNMENT, shape 2 above.
# Neither spelling below exists anywhere in the tree today (confirmed via
# the live census, same as the Sweep 1 additions) -- proven via synthetic
# input, matching the falsification pattern used throughout this file.


def test_import_classifier_catches_alias_assignment_via_config_alias(
    tmp_path: Path,
) -> None:
    """``X = _config.nexus_config_dir`` after ``from nexus import config as
    _config`` -- the exact shape nexus-grg79's own diff made canonical
    across four more src/ files, and the exact shape the critic
    demonstrated was invisible to the pre-extension detector."""
    bad = tmp_path / "bad_config_alias.py"
    bad.write_text(
        "from nexus import config as _config\n"
        "\n"
        "_CACHED = _config.nexus_config_dir\n"
    )
    module_level, function_scoped = _classify_import_sites(bad)
    assert module_level == [3]
    assert function_scoped == []


def test_import_classifier_catches_alias_assignment_via_plain_config_binding(
    tmp_path: Path,
) -> None:
    """``X = config.nexus_config_dir`` after a plain ``from nexus import
    config`` (no ``as``) -- the second spelling the critic named."""
    bad = tmp_path / "bad_plain_config.py"
    bad.write_text(
        "from nexus import config\n"
        "\n"
        "_CACHED = config.nexus_config_dir\n"
    )
    module_level, function_scoped = _classify_import_sites(bad)
    assert module_level == [3]
    assert function_scoped == []


def test_import_classifier_catches_alias_assignment_via_import_as(
    tmp_path: Path,
) -> None:
    """``X = ncfg.nexus_config_dir`` after ``import nexus.config as ncfg``
    -- the same single-hop-attribute shape as the ``_config`` alias case,
    reached through the other Python import statement that can produce it."""
    bad = tmp_path / "bad_import_as.py"
    bad.write_text(
        "import nexus.config as ncfg\n"
        "\n"
        "_CACHED = ncfg.nexus_config_dir\n"
    )
    module_level, function_scoped = _classify_import_sites(bad)
    assert module_level == [3]
    assert function_scoped == []


def test_import_classifier_ignores_unrelated_alias_assignment(
    tmp_path: Path,
) -> None:
    """An alias assignment to some OTHER config attribute must not be
    flagged -- proves the detector matches on the target attribute name,
    not on every assignment sourced from a ``nexus.config`` alias."""
    benign = tmp_path / "benign_alias.py"
    benign.write_text(
        "from nexus import config as _config\n"
        "\n"
        "_CACHED = _config.is_local_mode\n"
    )
    module_level, function_scoped = _classify_import_sites(benign)
    assert module_level == []
    assert function_scoped == []


def test_import_classifier_ignores_alias_assignment_from_unrelated_module(
    tmp_path: Path,
) -> None:
    """``X = some_other_module.nexus_config_dir`` where
    ``some_other_module`` was never bound to ``nexus.config`` must not be
    flagged -- proves the detector requires the alias to actually resolve
    to the config module, not just a matching attribute name anywhere."""
    benign = tmp_path / "benign_unrelated_module.py"
    benign.write_text(
        "import other_module\n"
        "\n"
        "_CACHED = other_module.nexus_config_dir\n"
    )
    module_level, function_scoped = _classify_import_sites(benign)
    assert module_level == []
    assert function_scoped == []


def test_import_classifier_treats_function_scoped_alias_assignment_as_safe(
    tmp_path: Path,
) -> None:
    """A module-level ``_config`` alias combined with a FUNCTION-scoped
    assignment of ``_config.nexus_config_dir`` to a local name is SAFE --
    the assignment re-executes, and therefore re-resolves the CURRENT
    ``nexus.config.nexus_config_dir``, every time the function runs. Must
    land in ``function_scoped``, not ``module_level``."""
    safe = tmp_path / "safe_alias.py"
    safe.write_text(
        "from nexus import config as _config\n"
        "\n"
        "def helper():\n"
        "    cached = _config.nexus_config_dir\n"
        "    return cached()\n"
    )
    module_level, function_scoped = _classify_import_sites(safe)
    assert module_level == []
    assert function_scoped == [4]
