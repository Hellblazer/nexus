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

MUST STAY IN THE DEFAULT LOOP -- do not mark this file ``lint`` (nexus-8x4le,
2026-08-05). Every OTHER repo-structure census reclassified to the ``lint``
marker by 37a53f3c (``test_storage_boundary_lint.py``, ``test_no_new_sqlite.py``,
``test_private_handle_access_census.py``, etc.) walks the FILESYSTEM directly
(``SRC.rglob("*.py")``), so its census is invocation-independent -- it sees the
same files whether pytest was invoked with ``-m lint`` or the full default
selection. ``test_mode_declarations_are_explicit`` is structurally different:
its census is ``request.session.items``, i.e. whatever THIS pytest invocation
happened to collect and select, specifically because it needs pytest's own
fixture-resolution machinery (``item.fixturenames``) to know whether a test
truly opts into ``cloud_mode`` (including via class/module ``pytestmark =
pytest.mark.usefixtures(...)``) -- a AST-only re-implementation was explicitly
scoped OUT above. A prior fix attempt (3d9f07ad) added
``pytestmark = pytest.mark.lint`` here on the theory that this is "just
another repo-structure census". It is not: CI's ``test-lint`` job runs
``pytest -m lint``, and pytest's own ``-m``/``-k`` deselection mutates
``session.items`` in place (confirmed empirically: 11733 default-loop items
collapse to 803 under ``-m lint``) -- so a lint-marked copy of this test can
only ever see the ~800 other lint-bucket tests, permanently blind to
voyage-token references anywhere else in the ~11.7k-test default corpus this
guard exists to police. That blind spot was not hypothetical: while
lint-marked, this census missed a real, live RDR-109 violation that had
landed in ``tests/test_health_service_checks.py`` shortly before the
reclassification (see the ``nexus-8x4le`` entry in
``_MODE_LINT_EXCLUDE_NODEIDS`` below, added once the census was restored to
the default loop and caught it for the first time).

The original xdist-flake hypothesis this bead was filed under (partition-
blindness or item-ordering divergence across ``-n auto`` workers) was
independently disproven: a serial run and an ``-n 2`` run of a full default
collection, probed at ``pytest_collection_finish`` before any test executes,
produced IDENTICAL ``session.items`` counts (11733) and IDENTICAL offender
lists both times. ``request.session.items`` reflects the SAME fully-collected,
fully-deselected list in every worker process (xdist workers each perform
their own full local collection and the controller cross-checks they agree;
a genuine mismatch aborts the run with its own loud error, not a subtle test
failure). The observed "passes serially, fails under -n auto" pattern earlier
on 2026-08-05 is fully explained by the tree changing between runs -- commit
dbd2cb46 (08:59:44) landed the real offender shortly after 37a53f3c
(08:08:13) introduced the ``-n auto`` dev loop; any full-default-loop
invocation (serial OR parallel) run after that commit would deterministically
fail, and any run before it would deterministically pass. Nothing about xdist
itself was ever broken here.
"""
from __future__ import annotations

import ast
import inspect
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time

import pytest

from tests.conftest import (
    _MODE_LINT_EXCLUDE_FILES,
    _MODE_LINT_EXCLUDE_NODEIDS,
    _SESSION_ITEMS_CENSUS_TEST_NODEIDS,
    partial_session_view_reason,
    pytest_runtest_setup as _guard_pytest_runtest_setup,
)
from tests import conftest as _conftest

VOYAGE_RE = re.compile(r"voyage-(context|code)-3")

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TESTS_DIR = _REPO_ROOT / "tests"


def _scan_offenders(items: list) -> list[str]:
    """Return nodeids of *items* that reference a voyage embedder name
    without declaring cloud mode, and are not exempted.

    Pure function of *items* -- deliberately takes no ``request``/``config``
    so it can be unit-tested against a synthetic item list independent of
    the partial-view guard in ``test_mode_declarations_are_explicit``. It is
    exactly as vacuous on a shrunk item list as the pre-nexus-vdti6 census
    was on one shard of CI's real ``--splits``/``--group`` matrix; see
    ``test_scan_offenders_is_vacuous_on_a_shrunk_item_list`` below for the
    kill control that proves it, and why the guard runs BEFORE this.
    """
    offenders: list[str] = []
    for item in items:
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
    return offenders


def test_mode_declarations_are_explicit(request: pytest.FixtureRequest) -> None:
    # STRUCTURAL HONESTY (nexus-vdti6): refuse loud rather than silently
    # scan a proven-partial request.session.items -- see
    # partial_session_view_reason's docstring in tests/conftest.py.
    #
    # BELT-AND-BRACES ONLY (nexus-vdti6, CI red 2026-08-06): the PRIMARY
    # guard is now tests/conftest.py's `pytest_runtest_setup` hookimpl,
    # which fires before this test's own fixtures (including the autouse
    # engine substrate) are ever resolved -- see that hook's docstring for
    # why a check here, AFTER fixture setup, is too late to avoid an
    # unnecessary engine boot (and, under real --splits/--group CI shards,
    # too late to avoid a real port collision). This line should be
    # unreachable in practice; it stays only as a fallback for the case
    # where a future edit adds a new session.items-based census test and
    # forgets to also register its nodeid in
    # `_SESSION_ITEMS_CENSUS_TEST_NODEIDS`.
    if (reason := partial_session_view_reason(request)) is not None:
        pytest.skip(reason)

    offenders = _scan_offenders(request.session.items)

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
# 58 -> 59 (nexus-h1zu0, 2026-08-05): +1, test_h1zu0_dim_routing.py — every
# test in the file asserts dim_for_model_token(token) -> int|None, a pure
# dict lookup against the routing table mirroring Java's
# PgVectorRepository.MODEL_DIMS; "voyage-code-3"/"voyage-context-3" are
# literal ARGUMENTS naming the table's keys, not a behavior assertion about
# which embedder ran. Rationale in conftest.py beside the entry.
# 59 -> 58 (nexus-sghyo, 2026-08-06): -1, test_voyage_ef.py DELETED along
# with the module it tested (nexus.db.voyage_ef) — client-side Voyage
# embedding is retired (Hal determination 2026-07-28: "we do no embedding
# on the client"). SHRINK, downward-only.
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
# nexus-bm8dd (2026-07-31): 36 -> 35. TestUpdateSourcePath was deleted with the
# retired source_path-keyed methods, so its exclusion pointed at nothing. Removing
# a dead slot is not a new grant.
# 35 -> 36 (nexus-8x4le, 2026-08-05): +1,
# test_health_service_checks.py::TestCheckChashConformanceReport::
# test_fully_routable_collections_still_render_plain_clean — reason
# "string-literal-as-name", found by the whole-session census once it was
# restored to the default loop (a mistaken lint-bucket reclassification had
# blinded it to everything outside ~800 lint-marked tests; see the module
# docstring above). Rationale in conftest.py beside the entry.
# 36 -> 37 (nexus-sghyo, 2026-08-07): +1,
# test_indexer_e2e.py::test_migration_moves_prose_from_code_to_docs —
# reason "string-literal-as-name". The `cloud_mode` fixture this test used
# to declare was dropped (it collided with the now-retired non-service
# client-embedding leg); the voyage-code-3 string it still references is a
# fabricated metadata literal for a seeded fake chunk, never a real
# embedder call. Rationale in conftest.py beside the entry.
# 37 -> 39 (nexus-wxjr6, 2026-08-09): +2, test_manifest_write_many.py::
# TestWriteManifestManyCombined::{test_combined_wire_shape,
# test_ack_echo_raises_when_chunks_written_absent} — reason
# "string-literal-as-name". Both assert the kl2z6 combined-write request
# body carries a conformant collection-name string verbatim against a
# monkeypatched `_post`; no embedder runs. Rationale in conftest.py beside
# the entries.
# 39 -> 40 (nexus-wxjr6, 2026-08-09, code review Important I1 follow-up):
# +1, test_indexer_seam_b_cutover.py::test_run_index_batch_flush_retries_
# transient_failure_then_succeeds — reason "string-literal-as-name", same
# class as the two pre-existing force_re_embed siblings in the same file.
# Missed in the I1 commit because the full suite ran BEFORE the mode-lint
# census saw this specific new test (a targeted battery run doesn't
# collect the whole session, so this lint's `request.session.items` census
# is blind to it until a full run — see the module docstring). Rationale
# in conftest.py beside the entry.
_MODE_LINT_EXCLUDE_NODEIDS_CEILING = 40


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


# ── partial-view guard tests (nexus-vdti6) ─────────────────────────────────
#
# Fast unit coverage for tests.conftest.partial_session_view_reason,
# independent of any real pytest invocation shape. The slower subprocess
# probe further down proves the REAL --splits/--group wiring; these prove
# the decision function's logic in isolation, including the exemptions
# (-k, no-baseline) that must NOT false-alarm.


class _FakeConfig:
    def __init__(self, options: dict[str, object]) -> None:
        self._options = options

    def getoption(self, name: str, default: object = None) -> object:
        return self._options.get(name, default)


class _FakeSession:
    def __init__(self, items: list) -> None:
        self.items = items


class _FakeRequest:
    def __init__(
        self, config: _FakeConfig, items: list, nodeid: str = ""
    ) -> None:
        self.config = config
        self.session = _FakeSession(items)
        # Only used by the pytest_runtest_setup hook tests below --
        # partial_session_view_reason itself never reads this. A real
        # pytest.Item and a real request both expose .config / .session in
        # this exact shape (Node.__init__ sets both directly), so this
        # fake doubles as a stand-in for either.
        self.nodeid = nodeid


def test_partial_session_view_reason_flags_pytest_split_shard() -> None:
    """Signal 1: --splits/--group present -> loud refusal, regardless of
    how many items survived selection. This is the EXACT condition CI's
    real `test` matrix job runs under (nexus-vdti6's root cause).
    """
    config = _FakeConfig({"splits": 4, "group": 3, "keyword": ""})
    request = _FakeRequest(config, items=list(range(2796)))
    reason = partial_session_view_reason(request)
    assert reason is not None
    assert "pytest-split" in reason
    assert "nexus-vdti6" in reason


def test_partial_session_view_reason_none_for_full_default_loop_view() -> None:
    """No --splits/--group, no -k, and session.items close to the raw
    collected count (the normal default-loop shape -- `-m 'not integration
    and not slow and not lint'` trims ~10%, reproduced live 2026-08-06:
    11733/13101) -> trustworthy, no refusal.
    """
    saved = _conftest._raw_collected_count
    _conftest._raw_collected_count = 13101
    try:
        config = _FakeConfig({"splits": None, "group": None, "keyword": ""})
        request = _FakeRequest(config, items=list(range(11733)))
        assert partial_session_view_reason(request) is None
    finally:
        _conftest._raw_collected_count = saved


def test_partial_session_view_reason_exempts_deliberate_keyword_narrowing() -> None:
    """-k narrows session.items to almost nothing but must NOT false-alarm
    -- a developer targeting one test by keyword is a normal local
    convenience run, not shard-blindness reappearing under a new name.
    """
    saved = _conftest._raw_collected_count
    _conftest._raw_collected_count = 13101
    try:
        config = _FakeConfig(
            {"splits": None, "group": None, "keyword": "mode_declarations"}
        )
        request = _FakeRequest(config, items=list(range(3)))
        assert partial_session_view_reason(request) is None
    finally:
        _conftest._raw_collected_count = saved


def test_partial_session_view_reason_flags_generic_shrink_below_floor() -> None:
    """No --splits/--group, no -k, but session.items shrank below the
    sanity floor anyway (some OTHER future session.items-shrinking
    mechanism) -> loud refusal. This is the generic net nexus-vdti6 asked
    for: "the same class could affect any future session.items-based
    census."
    """
    saved = _conftest._raw_collected_count
    _conftest._raw_collected_count = 13101
    try:
        config = _FakeConfig({"splits": None, "group": None, "keyword": ""})
        request = _FakeRequest(config, items=list(range(800)))  # ~6%
        reason = partial_session_view_reason(request)
        assert reason is not None
        assert "800/13101" in reason
    finally:
        _conftest._raw_collected_count = saved


def test_partial_session_view_reason_inert_with_no_raw_baseline() -> None:
    """A raw baseline of 0 (e.g. this guard called outside a real
    collection cycle) must not false-alarm -- absence of evidence is not
    evidence of a shrink.
    """
    saved = _conftest._raw_collected_count
    _conftest._raw_collected_count = 0
    try:
        config = _FakeConfig({"splits": None, "group": None, "keyword": ""})
        request = _FakeRequest(config, items=[])
        assert partial_session_view_reason(request) is None
    finally:
        _conftest._raw_collected_count = saved


# ── pytest_runtest_setup pre-fixture hook tests (nexus-vdti6, CI red
# 2026-08-06) ────────────────────────────────────────────────────────────
#
# The in-body `partial_session_view_reason` call above only prevents a
# VACUOUS SCAN. It runs too late to prevent an unnecessary (and, under a
# real --splits/--group CI shard, collision-prone) engine-substrate boot,
# because by the time a test's body executes, its fixtures -- including
# the autouse engine substrate -- have already been resolved. These tests
# cover the REAL fix: tests/conftest.py's `pytest_runtest_setup` hookimpl,
# which fires before ANY fixture setup for a registered census test.


def test_pytest_runtest_setup_hook_skips_registered_census_under_partial_view() -> None:
    """The hook raises Skipped for a registered census nodeid when the
    partial-view guard fires -- BEFORE any fixture would be touched (this
    call itself never resolves a fixture; `item.setup()` is never invoked
    here at all, which is the structural proof: there is no code path
    between this hook and a fixture, so a real pytest.Item literally cannot
    reach `item.setup()` if this hook already raised).
    """
    nodeid = next(iter(_SESSION_ITEMS_CENSUS_TEST_NODEIDS))
    config = _FakeConfig({"splits": 4, "group": 2, "keyword": ""})
    item = _FakeRequest(config, items=list(range(2796)), nodeid=nodeid)

    with pytest.raises(pytest.skip.Exception) as excinfo:
        _guard_pytest_runtest_setup(item)
    assert "pytest-split" in str(excinfo.value)


def test_pytest_runtest_setup_hook_does_not_skip_registered_census_under_full_view() -> None:
    """Sanity check requested alongside the CI fix: the SAME pre-fixture
    hook must NOT skip the census when the view is trustworthy -- e.g. the
    dedicated `test-mode-census` CI job's own invocation (no --splits/
    --group, session.items close to the raw collected count). A hook that
    skipped unconditionally would silently defeat the whole point of that
    job (it exists specifically so the census executes for real).
    """
    saved = _conftest._raw_collected_count
    _conftest._raw_collected_count = 13101
    try:
        nodeid = next(iter(_SESSION_ITEMS_CENSUS_TEST_NODEIDS))
        config = _FakeConfig({"splits": None, "group": None, "keyword": ""})
        item = _FakeRequest(config, items=list(range(11733)), nodeid=nodeid)
        # No exception -- the hook must return normally, letting pytest
        # proceed to real fixture setup and then the test body.
        _guard_pytest_runtest_setup(item)
    finally:
        _conftest._raw_collected_count = saved


def test_pytest_runtest_setup_hook_ignores_non_census_items() -> None:
    """A shrunk session.items on an UNREGISTERED test must not skip it --
    the hook is scoped to `_SESSION_ITEMS_CENSUS_TEST_NODEIDS`, not every
    test in the file. Every other test in this module (the ratchet/
    liveness checks, these very guard tests) must keep running normally
    under --splits/--group; only the one registered census reads
    session.items and needs the pre-empt.
    """
    config = _FakeConfig({"splits": 4, "group": 1, "keyword": ""})
    item = _FakeRequest(
        config,
        items=list(range(3)),
        nodeid="tests/test_mode_declarations_are_explicit.py::test_mode_lint_exclude_files_ratchet",
    )
    # No exception -- an unregistered nodeid is untouched by this hook
    # regardless of how partial its session.items looks.
    _guard_pytest_runtest_setup(item)


def _fake_offender_func() -> None:
    assert MODEL_NAME == "voyage-context-3"  # noqa: F821 -- source text only, never called


def _fake_clean_func() -> None:
    assert True


class _FakeItem:
    def __init__(
        self, function, nodeid: str, fixturenames: tuple[str, ...] = ()
    ) -> None:
        self.function = function
        self.nodeid = nodeid
        self.fixturenames = fixturenames


def test_scan_offenders_is_vacuous_on_a_shrunk_item_list() -> None:
    """KILL CONTROL: prove `_scan_offenders` alone -- with no guard in
    front of it -- is exactly as blind on a shrunk item list as the
    pre-nexus-vdti6 census was on one shard of CI's real pytest-split
    matrix.

    A real offender exists in the full corpus (represented here) but the
    shrunk view handed to the scan doesn't include it -- `_scan_offenders`
    reports a clean pass on the partial list even though a violation
    exists elsewhere. This is the exact failure mode
    `partial_session_view_reason` exists to intercept BEFORE the scan
    runs; `test_mode_declarations_are_explicit` calls the guard first
    specifically so this vacuous-pass shape never reaches a real CI
    report.
    """
    offender_item = _FakeItem(
        _fake_offender_func, nodeid="tests/test_x.py::test_uses_voyage"
    )
    clean_item = _FakeItem(
        _fake_clean_func, nodeid="tests/test_y.py::test_unrelated"
    )

    full_view = [offender_item, clean_item]
    shrunk_view = [clean_item]  # the offender landed in a DIFFERENT shard

    assert _scan_offenders(full_view) == ["tests/test_x.py::test_uses_voyage"]
    assert _scan_offenders(shrunk_view) == []  # <- the vacuous pass


def test_mode_declarations_census_skips_loud_under_real_pytest_split_shard() -> None:
    """Integration proof (nexus-vdti6): actually invoke pytest with
    --splits/--group against this file -- the same flag pytest-split uses
    in CI's real `test` matrix job (.github/workflows/ci.yml) -- and
    confirm the census SKIPS with the guard's reason rather than silently
    running a partial scan.

    Deliberately a REAL subprocess `--splits`/`--group` invocation, not a
    `--collect-only` probe or an xdist (`-n auto`) run: nexus-8x4le's
    earlier fix validated with `-n auto` x3 and that shape does NOT
    reproduce CI's real invocation -- xdist workers each collect the FULL
    item list locally (session.items unmutated), whereas pytest-split
    genuinely shrinks it. Repeating that same validation mistake here is
    exactly what this bead was filed to correct, so this probe uses the
    real flags CI's `test` job actually passes.

    ``NX_TEST_T2_SUBSTRATE=none`` on the nested invocation (the same
    convention ``tests/db/test_i711w_sqlite_substrate_retired.py`` uses for
    a substrate-free subprocess probe): this test's PURPOSE is to prove
    collection/skip semantics, not engine behavior, and passing it makes
    the "no engine boot" kill control below a STRUCTURAL guarantee rather
    than a hopeful empirical one -- with it set, `_pin_t2_substrate`
    returns immediately for every test in the nested run (census included),
    so nothing in that subprocess can attempt an engine boot regardless of
    which items land in which group. Before this env var was added here
    (nexus-vdti6, CI red 2026-08-06, develop run 31061260804), the nested
    invocation's own engine boot collided with the outer CI shard job's
    already-bound engine port and failed loudly; the REAL fix is the
    pre-fixture `pytest_runtest_setup` hook in tests/conftest.py (see its
    docstring), and this env var additionally keeps THIS specific probe
    fast and side-effect-free on top of that.
    """
    # This test's OWN nodeid must be excluded from the scoped invocation
    # below -- otherwise landing this same test in one of the 4 groups
    # would recursively re-spawn 4 more subprocesses from inside itself
    # (reproduced directly: group 4/4 hung for the full 120s timeout on the
    # first version of this probe, which scoped the whole file with no
    # exclusion).
    # Derived, not hardcoded: pytest does NOT error on an unmatched
    # --deselect, so a file/function rename with a stale literal here would
    # silently reintroduce the self-recursion this exclusion exists to
    # prevent (code-review suggestion, nexus-vdti6).
    _self_file = pathlib.Path(__file__)
    _self_nodeid = (
        _self_file.relative_to(_self_file.parents[1]).as_posix()
        + "::"
        + test_mode_declarations_census_skips_loud_under_real_pytest_split_shard.__name__
    )
    # Engine-boot KILL CONTROL (nexus-vdti6): _engine_substrate.py's
    # ensure_engine() creates a `nexus_t2_substrate_pg_*` temp dir on every
    # boot (tempfile.mkdtemp(prefix=...)). With NX_TEST_T2_SUBSTRATE=none
    # in effect nothing in the nested run may boot an engine at all -- so
    # the absence of any NEW such dir after each call is a structural
    # proof, not a timing guess. A generous 8s ceiling backs it up (a real
    # engine boot is documented elsewhere in this repo as ~10s minimum for
    # the JAR+PG combination alone; collecting and running one small file
    # with no engine involved measured well under 4s locally).
    _pg_tmp_prefix = "nexus_t2_substrate_pg_"
    _census_nodeid = (
        "tests/test_mode_declarations_are_explicit.py"
        "::test_mode_declarations_are_explicit"
    )

    def _group_contains_census(group: int) -> bool:
        # `--collect-only` prints one nodeid per line and is unaffected by
        # WHERE a skip() call reports its location -- unlike the real run's
        # `-rs` summary below (a skip raised from tests/conftest.py's
        # pytest_runtest_setup hook reports as "tests/conftest.py:LINE:
        # <reason>", NOT the item's own nodeid; reproduced directly while
        # building this probe -- the first cut here matched on
        # "test_mode_declarations_are_explicit" appearing anywhere in a REAL
        # run's output, which silently stopped proving anything the moment
        # the skip moved from the test body to the pre-fixture hook).
        # Collection never executes a test body, so no engine boot risk
        # here regardless of NX_TEST_T2_SUBSTRATE.
        probe = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_mode_declarations_are_explicit.py",
                "--deselect",
                _self_nodeid,
                "--splits",
                "4",
                "--group",
                str(group),
                "--collect-only",
                "-q",
                "--no-header",
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return _census_nodeid in probe.stdout

    found_shard = False
    for group in range(1, 5):
        if not _group_contains_census(group):
            continue  # census landed in a different group -- fine
        found_shard = True

        tmp_before = {
            p for p in pathlib.Path(tempfile.gettempdir()).glob(f"{_pg_tmp_prefix}*")
        }
        started = time.monotonic()
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_mode_declarations_are_explicit.py",
                "--deselect",
                _self_nodeid,
                "--splits",
                "4",
                "--group",
                str(group),
                "-q",
                "-rs",
                "--no-header",
            ],
            cwd=_REPO_ROOT,
            env={**os.environ, "NX_TEST_T2_SUBSTRATE": "none"},
            capture_output=True,
            text=True,
            timeout=120,
        )
        elapsed = time.monotonic() - started
        tmp_after = {
            p for p in pathlib.Path(tempfile.gettempdir()).glob(f"{_pg_tmp_prefix}*")
        }
        new_pg_dirs = tmp_after - tmp_before
        assert not new_pg_dirs, (
            f"group {group}/4: a new {_pg_tmp_prefix}* dir appeared "
            f"({new_pg_dirs}) -- something in the nested invocation booted "
            f"an engine despite NX_TEST_T2_SUBSTRATE=none. That is exactly "
            f"the class of collision that made this probe fail under real "
            f"CI (develop run 31061260804)."
        )
        assert elapsed < 8.0, (
            f"group {group}/4: nested invocation took {elapsed:.1f}s -- "
            f"well over the few-seconds ceiling for a substrate-free "
            f"single-file run, suggesting something DID pay an engine-boot "
            f"cost even though no {_pg_tmp_prefix}* dir was left behind."
        )

        output = proc.stdout + proc.stderr
        assert "pytest-split active" in output, (
            f"group {group}/4: the census ran but was not skipped with the "
            f"partial-view guard's reason -- guard did not fire under a "
            f"real --splits/--group invocation:\n{output}"
        )
        assert proc.returncode == 0, (
            f"group {group}/4: expected a clean (skip-only) exit, got "
            f"{proc.returncode}:\n{output}"
        )
    assert found_shard, (
        "test_mode_declarations_are_explicit never appeared in any of the "
        "4 --splits groups -- pytest-split's algorithm or invocation shape "
        "changed; this probe needs updating"
    )


def test_mode_declarations_census_executes_for_real_under_ci_env_shape() -> None:
    """Regression test for a SECOND bug found while fixing nexus-vdti6 (CI
    red 2026-08-06), caught in review before the `test-mode-census` job
    ever ran on real CI: without `NX_TEST_T2_SUBSTRATE=none` in that job's
    env, the census would have hit `t2_service_env`'s own PRE-EXISTING
    graceful skip ("engine substrate: service JAR not provisioned on CI"),
    which fires whenever `GITHUB_ACTIONS=='true'` AND
    `NX_T2_SUBSTRATE_EXPECTED` is unset AND no jar exists -- exactly the
    `test-mode-census` job's shape (it deliberately provisions no jar).
    That would make the census silently SKIP on every real run of that job:
    reporting green while never executing its scan at all -- the identical
    vacuous-pass shape nexus-vdti6 exists to eliminate, just relocated from
    "shard-blind" to "substrate-skipped".

    Proof: simulate the job's exact env combination (`GITHUB_ACTIONS=true`,
    `NX_TEST_T2_SUBSTRATE=none`, no --splits/--group so the partial-view
    guard is not in play here) against ONLY the census test, and confirm it
    actually EXECUTES its scan (PASSED, not SKIPPED) -- proving
    `NX_TEST_T2_SUBSTRATE=none` makes the substrate-skip path unreachable
    regardless of `GITHUB_ACTIONS` or jar presence, since `_pin_t2_
    substrate` returns before ever calling `t2_service_env` (see
    tests/conftest.py). Selecting the single test by nodeid on the command
    line -- not `-k` -- is itself a form of collection scoping (like a
    path-scoped run), so `partial_session_view_reason`'s own guard sees a
    1/1 view and does not fire; this test is entirely about the SEPARATE
    substrate-skip path, not the partial-view guard.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_mode_declarations_are_explicit.py"
            "::test_mode_declarations_are_explicit",
            "-q",
            "-rs",
            "--no-header",
        ],
        cwd=_REPO_ROOT,
        env={**os.environ, "GITHUB_ACTIONS": "true", "NX_TEST_T2_SUBSTRATE": "none"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = proc.stdout + proc.stderr
    assert "service JAR not provisioned" not in output, (
        "the census hit the engine-substrate graceful skip under the "
        "test-mode-census job's exact env shape (GITHUB_ACTIONS=true, no "
        "jar) -- NX_TEST_T2_SUBSTRATE=none did not bypass it as expected:\n"
        + output
    )
    assert "1 passed" in output, (
        f"expected the census to execute and pass for real under this env "
        f"shape, got:\n{output}"
    )
