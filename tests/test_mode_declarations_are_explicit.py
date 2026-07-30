# SPDX-License-Identifier: AGPL-3.0-only
"""RDR-109 Phase 1 lint: tests that reference cloud-mode embedder names
must opt in to the ``cloud_mode`` fixture (or be in the exclusion list).

The default test mode is local (no API keys, ONNX MiniLM EF). Without
this guard, a test that asserts ``embedding_model == "voyage-context-3"``
would silently pass in CI iff some prior PR happened to leak cloud-mode
state into the session, and fail otherwise. The lint forces the choice
to be explicit.

Implementation: grep + ``request.fixturenames`` introspection. AST-shape
analysis is out of scope (RDR-109 §Phase 1, step 4).
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re

import pytest

from tests.conftest import (
    _MODE_LINT_EXCLUDE_FILES,
    _MODE_LINT_EXCLUDE_NODEIDS,
)

VOYAGE_RE = re.compile(r"voyage-(context|code)-3")

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TESTS_DIR = _REPO_ROOT / "tests"


def test_mode_declarations_are_explicit(request: pytest.FixtureRequest) -> None:
    offenders: list[str] = []
    for item in request.session.items:
        func = getattr(item, "function", None)
        if func is None:
            continue
        try:
            src = inspect.getsource(func)
        except (OSError, TypeError):
            continue
        if not VOYAGE_RE.search(src):
            continue
        # File-level exclusion (every test in the file is exempt).
        # ``item.nodeid`` is e.g. ``tests/test_x.py::test_func[param]``.
        nodeid = item.nodeid
        file_part = nodeid.split("::", 1)[0]
        file_basename = file_part.rsplit("/", 1)[-1]
        if file_basename in _MODE_LINT_EXCLUDE_FILES:
            continue
        # Per-test exclusion (strip parametrize suffix).
        base_nodeid = nodeid.split("[", 1)[0]
        if base_nodeid in _MODE_LINT_EXCLUDE_NODEIDS:
            continue
        fixturenames = set(getattr(item, "fixturenames", ()))
        if "cloud_mode" in fixturenames:
            continue
        offenders.append(nodeid)

    if offenders:
        sample = "\n  ".join(offenders[:20])
        suffix = (
            f"\n  ... (+{len(offenders) - 20} more)"
            if len(offenders) > 20
            else ""
        )
        pytest.fail(
            "RDR-109 Phase 1: the following tests reference voyage-"
            "(context|code)-3 but do not opt in to the `cloud_mode` "
            "fixture and are not listed in `_MODE_LINT_EXCLUDE`:\n  "
            + sample
            + suffix
            + "\n\nFix: add `cloud_mode` to the test's fixture list "
            "(or `pytestmark = pytest.mark.usefixtures(\"cloud_mode\")` "
            "at module/class scope) if the test asserts cloud-mode "
            "behavior; or add the nodeid to `_MODE_LINT_EXCLUDE` in "
            "tests/conftest.py with a documented reason."
        )


# RDR-109 / nexus-vgq89 ratchet: these two exclusion sets may only ever
# SHRINK. A PR that grows either one is silently re-introducing the
# "Phase 1 ships excluded, subsequent PRs promote each" grandfathering
# this bead burned down (2026-07-15) -- every entry above this point
# already carries an individually documented reason; a bare growth of
# the count with no accompanying rationale comment is exactly the
# regression these two assertions exist to catch. To legitimately grow
# either number: add the new exclusion with its own documented
# rationale comment (matching the style used throughout
# `_MODE_LINT_EXCLUDE_FILES` / `_MODE_LINT_EXCLUDE_NODEIDS` above), then
# consciously bump the corresponding constant below in the same diff.
# 72 -> 73 (RDR-155 P4b P3, 2026-07-25): + test_voyage_ef.py. The nexus-owned
# Voyage EF replaced chromadb's at P3.1, and its unit tests assert the exact
# `embed()` kwargs — including `model_name="voyage-code-3"` / `"voyage-context-3"`
# — against a MOCKED voyageai.Client. Reason class "string-literal-as-name": the
# tokens are the wire contract under test, not an embedder that runs. Requesting
# `cloud_mode` would add a live-credential dependency to a fully mocked test,
# which is the opposite of what that fixture is for. Rationale also recorded
# beside the entry in conftest.py.
# 73 -> 69 (nexus-i711w liveness burn-down, 2026-07-29): -4 entries that named
# no test file at all. 88d91bd5 (RDR-155 P4b P2) deleted the Chroma migration
# machinery, taking test_detection.py, test_vector_etl.py, test_pregate.py and
# test_quiesce.py with it; their exclusions survived because the ratchet below
# only counts entries and never asked whether they still resolved. This is a
# SHRINK -- the direction the ratchet already sanctions -- and it removes
# nothing that was excluding anything.
# 67 -> 62 (nexus-i711w terminal deletion): -5 entries whose files died with
# the local catalog (test_catalog_collections_rebuild.py,
# test_catalog_concurrent_writer_lock.py, test_catalog_db.py,
# test_catalog_incremental_rebuild.py, test_collections_owner_backfill.py).
# SHRINK, downward-only.
# 62 -> 61 (nexus-i711w terminal deletion, DIE sweep): one further dying-file
# entry retired during the retirement batches. SHRINK, downward-only.
# 61 -> 60: test_catalog_migrate_fallback.py died in the same sweep.
# 60 -> 58 (RDR-158 P4 Stage 4, nexus-i711w): -2 entries whose files died
# with db/migrations.py and _run_upgrade's local leg
# (test_migrations_rdr108_phase1c.py,
# test_upgrade_name_vs_embed_dim_advisory.py). SHRINK, downward-only.
_MODE_LINT_EXCLUDE_FILES_CEILING = 58
# 43 -> 46 (6.10.1): +3 real keyed integration tests in test_integration.py
# — cloud_mode's fake credentials broke them against the live Voyage API
# (their mode declaration is the requires-key gating; see conftest entry).
# 46 -> 55 (RDR-185 P4 harvest): +9 ladder tests, reason
# "string-literal-as-name" — each builds a conformant RDR-103 collection name
# (or a classification carrying the name's model SEGMENT) and asserts on
# planning / rollback / re-id behaviour keyed off that segment. None calls a
# Voyage embedder: the rung tests inject every collaborator and the local
# bge-768 path is what runs, so cloud_mode changes nothing they assert. The
# mislabel pair (nexus-j5diu) is the sharpest case FOR exclusion rather than
# promotion: their subject is a name whose voyage token LIES, so opting them
# into cloud_mode would assert the opposite of their point.
# SIX of the nine (test_rollback_via_map, test_substrate_leg) predate P4 and
# had this lint red on develop since P2 — the arc ran narrow, path-scoped
# selections, and this lint only fires when the whole session is collected, so
# `pytest tests/upgrade/` never sees it. Rationale per entry in conftest.py.
# Unchanged by nexus-6or3m / nexus-mq42b / nexus-k1m2f (RDR-185 P5): the new
# credential-gate and billed-consent pins name voyage tokens only through
# module-level fixtures (`_GATED`, `_billed_leg`), the pattern this file's own
# `_cls` helper has always used — so the set did not need to grow. Preferred to
# an exclusion: the tests read better without duplicated magic strings, and an
# exclusion the lint does not need is dead weight the ratchet then guards.
# 55→56 (nexus-r5f3c, 2026-07-21): test_voyage_configured_model_still_plumbs —
# "string-literal-as-config-value"; the supervisor env-plumbing mirror case
# needs the literal voyage model name as a CONFIG value, Popen mocked, no
# embedder constructed. Rationale in conftest.py beside the entry.
# 56→58 (nexus-9n485, 2026-07-25): the two rename-collection tombstone-probe
# tests — "string-literal-as-name"; the voyage token is one segment of the
# conformant RDR-103 name passed as the rename TARGET, and both tests patch
# HttpVectorClient's network boundary so no embedder is constructed. Landed
# red on develop: like the RDR-185 P4 pair above, the authoring run used a
# path-scoped selection, and this lint only fires on a whole-session
# collection. Rationale in conftest.py beside the entries.
# 58 -> 37 (nexus-i711w liveness burn-down, 2026-07-29): -21 entries that named
# tests which no longer exist. Same cause as the FILES shrink above -- 88d91bd5
# deleted tests/migration/test_driver.py, test_vector_etl.py,
# test_collision_audit.py, tests/upgrade/test_substrate_{leg,rung}.py,
# test_rollback_via_map.py, and friends. All 21 were verified GONE, not moved
# (no surviving definition of any of those test names anywhere under tests/),
# so every one is a clean drop rather than a retarget.
#
# Why this mattered rather than being cosmetic: the assertion below is exact
# equality, so 21 dead entries were 21 slots a future exclusion could be
# swapped into with no ceiling bump and no written rationale -- precisely the
# grandfathering this ratchet exists to prevent. The two liveness tests added
# below close that hole permanently; keep them passing rather than lowering
# these constants to match whatever the sets happen to contain.
# 37 -> 36 (nexus-i711w terminal deletion): the TestSQLiteCatalogNewMethods
# nodeid died with the SQLite parity arm. SHRINK, downward-only.
_MODE_LINT_EXCLUDE_NODEIDS_CEILING = 36


def test_mode_lint_exclude_files_ratchet() -> None:
    assert len(_MODE_LINT_EXCLUDE_FILES) == _MODE_LINT_EXCLUDE_FILES_CEILING, (
        f"_MODE_LINT_EXCLUDE_FILES has {len(_MODE_LINT_EXCLUDE_FILES)} "
        f"entries, expected exactly {_MODE_LINT_EXCLUDE_FILES_CEILING}. "
        "This set may only shrink (promote a file's tests to `cloud_mode` "
        "or per-test `_MODE_LINT_EXCLUDE_NODEIDS` entries) or grow with a "
        "documented per-entry rationale plus a conscious bump of "
        "`_MODE_LINT_EXCLUDE_FILES_CEILING` in this file."
    )


def test_mode_lint_exclude_nodeids_ratchet() -> None:
    assert len(_MODE_LINT_EXCLUDE_NODEIDS) == _MODE_LINT_EXCLUDE_NODEIDS_CEILING, (
        f"_MODE_LINT_EXCLUDE_NODEIDS has {len(_MODE_LINT_EXCLUDE_NODEIDS)} "
        f"entries, expected exactly {_MODE_LINT_EXCLUDE_NODEIDS_CEILING}. "
        "This set may only shrink (promote a test to `cloud_mode`) or grow "
        "with a documented per-entry rationale plus a conscious bump of "
        "`_MODE_LINT_EXCLUDE_NODEIDS_CEILING` in this file."
    )


def _defs_in(body: list[ast.stmt]) -> set[str]:
    """Names defined DIRECTLY in *body* (not nested further down)."""
    return {
        n.name
        for n in body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _why_unresolved(path: pathlib.Path, parts: list[str]) -> str | None:
    """Reason *parts* names no real test in *path*, or None if it resolves.

    Checks nodeid SHAPE, not merely name presence: pytest addresses a method
    as ``file::Class::test`` and a module-level test as ``file::test``, so a
    two-part entry naming a method is malformed and would never match a
    collected item. Scoping the lookup to the right body catches that, where
    a flat "is this name defined anywhere in the file" scan would not.

    ``ast`` rather than a regex throughout: a comment or docstring that
    merely mentions the name must not be allowed to count as a definition.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    leaf = parts[-1].split("[", 1)[0]

    if len(parts) == 2:
        if leaf not in _defs_in(tree.body):
            return f"file defines no module-level `def {leaf}`"
        return None

    if len(parts) == 3:
        cls = next(
            (
                n
                for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == parts[1]
            ),
            None,
        )
        if cls is None:
            return f"file has no top-level `class {parts[1]}`"
        if leaf not in _defs_in(cls.body):
            return f"`class {parts[1]}` defines no `def {leaf}`"
        return None

    return (
        f"unexpected nodeid shape ({len(parts)} segments); expected "
        "`file::test` or `file::Class::test`"
    )


def test_mode_lint_exclude_nodeids_all_resolve() -> None:
    """Every excluded nodeid must still name a test that exists.

    The ratchet above counts entries; it never asks whether they point at
    anything. A stale nodeid does not fail on its own -- it simply stops
    matching -- which produces two distinct failures, both of which had
    already happened when this test was written (nexus-i711w):

    * A RENAME silently downgrades a granted exclusion into a non-exclusion.
      9c0cff18 renamed a taxonomy-tripwire test out from under its entry and
      left the RDR-109 lint red on develop, discoverable only on a
      whole-session collection.
    * A DELETION leaves a dead slot that still satisfies the count. 88d91bd5
      orphaned 21 of 58 entries. Because the ratchet asserts exact equality,
      each dead slot was a free exclusion a later edit could swap into with
      no bump and no rationale -- exactly the grandfathering the ratchet is
      built to prevent.

    Filesystem + AST, deliberately NOT the collected session: a
    collection-based check would inherit the same whole-session blind spot
    that hid both incidents, and would call almost every entry dead under a
    path-scoped run like ``pytest tests/upgrade/``.
    """
    dead: list[str] = []
    for entry in sorted(_MODE_LINT_EXCLUDE_NODEIDS):
        parts = entry.split("::")
        path = _REPO_ROOT / parts[0]
        if not path.is_file():
            dead.append(f"{entry}\n        -> no such file")
            continue
        if (why := _why_unresolved(path, parts)) is not None:
            dead.append(f"{entry}\n        -> {why}")

    assert not dead, (
        f"{len(dead)} mode-lint nodeid exclusion(s) no longer resolve:\n  "
        + "\n  ".join(dead)
        + "\n\nAn exclusion that points at nothing grants nothing. RETARGET "
        "the entry if the test was renamed or moved -- that is not a new "
        "grant, so leave `_MODE_LINT_EXCLUDE_NODEIDS_CEILING` alone. DELETE "
        "the entry and lower the ceiling if the test is gone. Never leave it "
        "dead: the ceiling asserts exact equality, so a dead slot is a free, "
        "unrationalised exclusion for whoever edits this set next."
    )


def test_mode_lint_exclude_files_all_resolve() -> None:
    """Every excluded basename must still name a real test file.

    Same failure class as the nodeid liveness check above; 88d91bd5 left 4
    of these dead. Matched on basename rather than path because that is what
    the lint itself matches on.
    """
    basenames = {p.name for p in _TESTS_DIR.rglob("test_*.py")}
    dead = sorted(f for f in _MODE_LINT_EXCLUDE_FILES if f not in basenames)
    assert not dead, (
        f"{len(dead)} mode-lint file exclusion(s) name no test file under "
        "tests/:\n  "
        + "\n  ".join(dead)
        + "\n\nRetarget if the file was renamed, or delete the entry and "
        "lower `_MODE_LINT_EXCLUDE_FILES_CEILING` if it is gone."
    )
