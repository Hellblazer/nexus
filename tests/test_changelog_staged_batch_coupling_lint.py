# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-191 Phase 4 repoint batch, Step A (bead nexus-o8dil.18, Deliverable 2)
-- ONE directory-driven coupling lint over db/changelog-staged/, replacing
tests/test_changelog_vectors004_grants_coupling_lint.py and
tests/test_changelog_taxonomy007_coupling_lint.py.

WHY CONSOLIDATE, NOT ADD A THIRD SIBLING: [22445] Step A's own instruction.
The two existing per-file lints already share ~90% of their machinery
(_write_synthetic_pair, the state-(a)/(b)/(c) classification, reuse of
parse_master_include_order from test_changelog_rls_lint) and differ only in
which per-file EXTRA requirement applies once a file reaches state (b)
(vectors-004 needs the grants-nexus-svc.xml pieces; taxonomy-007 needs
nothing). A third near-duplicate file for vectors-005 (which also needs
nothing extra beyond correct include ORDER relative to its two siblings)
would triple the drift surface across three files that must always change
in lockstep. This lint generalizes the shared shape into ONE
directory-driven registry (BATCH_FILES) and treats the THREE staged
changesets as ONE ATOMIC BATCH, not three independently-tracked files --
matching [22445] section 2's own R1/R2 risk register entries: "TWO
three-state coupling lints RED every half-way state" (R1) and "Both must
flip in Step B or the tree is red for a reason unrelated to the repoint"
(R2), now generalized to "ALL THREE must flip together."

THE BATCH INVARIANT (stricter than the old per-file invariant: an
individually-well-formed file is not enough on its own):

  (a) ALL THREE STAGED, NONE REGISTERED (today's real state): every file in
      BATCH_FILES exists ONLY under db/changelog-staged/, absent from
      CHANGELOG_DIR, and none named in the master include list. PASS.
  (b) ALL THREE FULLY REGISTERED (the batch end-state): every file exists
      ONLY at CHANGELOG_DIR/<basename> (moved back), ALL THREE are named in
      the master include list IN THE ORDER vectors-004 -> taxonomy-007 ->
      vectors-005 (R7: computed from the LIVE master file, never a hardcoded
      position), AND every file's own extra per-file requirement passes
      (only vectors-004 has one: both grants-nexus-svc.xml pieces). PASS.
  (c) ANY OTHER COMBINATION is a violation -- including a state where every
      INDIVIDUAL file looks locally well-formed (e.g. two files registered
      cleanly, one still staged cleanly) but the BATCH as a whole is split
      across two states. That split-batch shape is exactly what the plan's
      atomicity guarantee (R1/R2) exists to make structurally impossible,
      so it is flagged even though neither old per-file lint would have
      caught it (each would have separately reported "state (a), clean" or
      "state (b), clean" for its own file in isolation).

WHAT THIS LINT REUSES: parse_master_include_order / CHANGELOG_DIR /
MASTER_CHANGELOG / _XSD_NS from tests/test_changelog_rls_lint.py, the same
reuse discipline the two lints being replaced already established.

Kill-control evidence for every arm (both clean states, the batch-mismatch
arm, the four malformed-placement arms per file, and the ordering-violation
arm) is via SYNTHETIC changelog trees, matching the family's convention of
never depending on mutating real repository files for the PERMANENT test
suite. test_real_repo_state_today_passes additionally guards the live
repository directly.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.test_changelog_rls_lint import (
    CHANGELOG_DIR,
    MASTER_CHANGELOG,
    _XSD_NS,
    parse_master_include_order,
)

pytestmark = pytest.mark.lint

STAGED_DIR_NAME = "changelog-staged"
GRANTS_CHANGELOG_BASENAME = "grants-nexus-svc.xml"
GRANTS_003_ID = "grants-003-purge-vacuum-maintain"
GRANTS_005_ID = "grants-005-chunks-unify-maintain"

VECTORS_004 = "vectors-004-unify-chunks.xml"
TAXONOMY_007 = "taxonomy-007-unify-centroids.xml"
VECTORS_005 = "vectors-005-repoint-functions-views.xml"

# Directory-driven registration ORDER (R7-safe: this is the REQUIRED relative
# order, verified against the LIVE master's actual include positions at
# check time -- never a hardcoded index).
BATCH_FILES: list[str] = [VECTORS_004, TAXONOMY_007, VECTORS_005]


def _grants_violations(changelog_dir: Path) -> list[str]:
    """vectors-004's own extra requirement once registered: both
    grants-nexus-svc.xml pieces of the boot-brick fix (T2 [22433]) must be
    present. Unchanged logic from the file this lint replaces."""
    grants_path = changelog_dir / GRANTS_CHANGELOG_BASENAME
    if not grants_path.exists():
        return [
            f"{VECTORS_004} is registered but {GRANTS_CHANGELOG_BASENAME} "
            "does not exist at all"
        ]

    tree = ET.parse(grants_path)
    root = tree.getroot()

    grants_003 = None
    grants_005 = None
    for cs in root.iter(f"{_XSD_NS}changeSet"):
        cs_id = cs.get("id", "")
        if cs_id == GRANTS_003_ID:
            grants_003 = cs
        elif cs_id == GRANTS_005_ID:
            grants_005 = cs

    violations: list[str] = []

    if grants_003 is None:
        violations.append(
            f"{GRANTS_003_ID} changeset not found in {GRANTS_CHANGELOG_BASENAME} "
            "-- cannot verify the boot-brick guard at all"
        )
    else:
        # The guard is an early-RETURN in the runAlways BODY, deliberately NOT
        # a <preConditions onFail="MARK_RAN"> (nexus-ixsxa /
        # test_changelog_markran_lint.py: runAlways + MARK_RAN appends one
        # DATABASECHANGELOG row per unmet boot, unbounded). Require BOTH the
        # to_regclass existence probe AND a RETURN in grants-003's <sql> body,
        # and require that no MARK_RAN preConditions crept back in.
        guarded = False
        for sql in grants_003.iter(f"{_XSD_NS}sql"):
            body = "".join(sql.itertext())
            if (
                "chunks_384" in body
                and "to_regclass" in body.lower()
                and "return" in body.lower()
            ):
                guarded = True
                break
        if not guarded:
            violations.append(
                f"{VECTORS_004} is registered but {GRANTS_003_ID} has no in-body "
                "existence guard (to_regclass('nexus.chunks_384') ... RETURN) -- "
                "it is runAlways=true and will unconditionally GRANT MAINTAIN on the "
                "now-dropped chunks_384/768/1024, crash-looping the service on the "
                "very next boot. (A <preConditions onFail=MARK_RAN> guard is NOT "
                "acceptable here: nexus-ixsxa, see test_changelog_markran_lint.py.)"
            )

    if grants_005 is None:
        violations.append(
            f"{VECTORS_004} is registered but {GRANTS_005_ID} (the MAINTAIN-grant "
            "successor for nexus.chunks/catalog_document_chunks/catalog_documents) "
            f"is missing from {GRANTS_CHANGELOG_BASENAME} -- MAINTAIN self-healing "
            "for the surviving tables is lost once grants-003 freezes"
        )

    return violations


# basename -> extra-requirements-once-registered callback (None = no extra
# requirement beyond mv+include atomicity). Directory-driven: iterating this
# dict, not a hardcoded three-way if/elif, is what makes adding a FOURTH
# staged batch member later a data change, not a structural one.
_EXTRA_REQUIREMENTS = {
    VECTORS_004: _grants_violations,
    TAXONOMY_007: None,
    VECTORS_005: None,
}


def _file_placement(changelog_dir: Path, basename: str) -> tuple[bool, bool, bool]:
    """Return (staged_exists, target_exists, included) for one basename."""
    staged_path = changelog_dir.parent / STAGED_DIR_NAME / basename
    target_path = changelog_dir / basename
    included = basename in parse_master_include_order(
        changelog_dir / "db.changelog-master.xml"
        if (changelog_dir / "db.changelog-master.xml").exists()
        else MASTER_CHANGELOG
    )
    return staged_path.exists(), target_path.exists(), included


def analyze_batch_registration_coupling(
    changelog_dir: Path = CHANGELOG_DIR, master_path: Path = MASTER_CHANGELOG
) -> list[str]:
    """Return a list of violation strings for the batch invariant described
    in the module docstring. Empty means either state (a) (all three
    staged, none registered) or state (b) (all three fully registered, in
    order, with every per-file extra requirement satisfied)."""
    placements: dict[str, tuple[bool, bool, bool]] = {}
    for basename in BATCH_FILES:
        staged_path = changelog_dir.parent / STAGED_DIR_NAME / basename
        target_path = changelog_dir / basename
        included = basename in parse_master_include_order(master_path)
        placements[basename] = (staged_path.exists(), target_path.exists(), included)

    # Per-file local classification: "staged" | "registered" | "malformed".
    per_file_state: dict[str, str] = {}
    per_file_violations: dict[str, list[str]] = {}

    for basename, (staged_exists, target_exists, included) in placements.items():
        if staged_exists and not target_exists and not included:
            per_file_state[basename] = "staged"
            continue
        if target_exists and not staged_exists and included:
            per_file_state[basename] = "registered"
            continue

        per_file_state[basename] = "malformed"
        reasons: list[str] = []
        if staged_exists and target_exists:
            reasons.append(
                f"{basename} exists in BOTH {STAGED_DIR_NAME}/ and "
                f"{changelog_dir.name}/ simultaneously -- ambiguous, remove one"
            )
        if not staged_exists and not target_exists:
            reasons.append(
                f"{basename} not found in either {STAGED_DIR_NAME}/ or "
                f"{changelog_dir.name}/ -- moved or deleted unexpectedly"
            )
        if included and not target_exists:
            reasons.append(
                f"{basename} is <include>d in {master_path.name} but the file "
                f"is not present at {changelog_dir.name}/ -- Liquibase will fail "
                "to load it at boot"
            )
        if target_exists and not included and not staged_exists:
            reasons.append(
                f"{basename} has been moved to {changelog_dir.name}/ but is NOT "
                f"YET <include>d in {master_path.name} -- either finish the "
                f"registration or move it back to {STAGED_DIR_NAME}/"
            )
        per_file_violations[basename] = reasons

    states = set(per_file_state.values())

    # Clean state (a): every file individually staged.
    if states == {"staged"}:
        return []

    # Clean state (b) candidate: every file individually registered. Verify
    # the per-file extra requirements AND the batch ordering.
    if states == {"registered"}:
        violations: list[str] = []
        for basename in BATCH_FILES:
            extra_fn = _EXTRA_REQUIREMENTS[basename]
            if extra_fn is not None:
                violations.extend(extra_fn(changelog_dir))

        order = parse_master_include_order(master_path)
        try:
            positions = [order.index(b) for b in BATCH_FILES]
        except ValueError as exc:
            violations.append(
                f"batch ordering check could not locate one of {BATCH_FILES} in "
                f"{master_path.name}'s include list despite each individually "
                f"reporting included=True -- {exc}"
            )
            positions = []
        if positions and positions != sorted(positions):
            violations.append(
                f"batch include order is WRONG: {BATCH_FILES} must appear in "
                f"{master_path.name} in exactly that relative order "
                "(vectors-004-unify-chunks.xml -> taxonomy-007-unify-centroids.xml "
                "-> vectors-005-repoint-functions-views.xml), found positions "
                f"{dict(zip(BATCH_FILES, positions))}"
            )
        return violations

    # State (c): the batch is split across states, OR one or more files are
    # individually malformed. Report every mismatch found.
    violations = []
    for basename in BATCH_FILES:
        if per_file_state[basename] == "malformed":
            violations.extend(per_file_violations[basename])
        elif len(states) > 1:
            violations.append(
                f"{basename} is individually well-formed (state={per_file_state[basename]!r}) "
                "but the BATCH is split across states -- all three of "
                f"{BATCH_FILES} must move together (R1/R2: partial-batch "
                "registration is impossible by design)"
            )
    return violations


# ===========================================================================
# Tests
# ===========================================================================


def _write_synthetic_batch(
    tmp_path: Path,
    placements: dict[str, str],
    include_order: list[str] | None,
    grants_changesets_xml: str,
) -> tuple[Path, Path]:
    """Write a minimal synthetic changelog dir + staged dir modeling the
    batch. *placements* maps basename -> one of "staged", "target", "both",
    "neither". *include_order* is the list of basenames to <include>, IN
    THE ORDER GIVEN (None means include none); grants_changesets_xml is
    injected verbatim into a synthetic grants-nexus-svc.xml (only relevant
    once vectors-004 reaches state (b))."""
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    staged_dir = tmp_path / STAGED_DIR_NAME
    staged_dir.mkdir()

    header = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<databaseChangeLog\n'
        '    xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '    xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog '
        'http://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-4.4.xsd">\n'
    )
    footer = "</databaseChangeLog>\n"

    for basename, where in placements.items():
        body = (
            header
            + f'    <changeSet id="{basename}-1" author="t"><sql>SELECT 1;</sql></changeSet>\n'
            + footer
        )
        if where in ("staged", "both"):
            (staged_dir / basename).write_text(body)
        if where in ("target", "both"):
            (changelog_dir / basename).write_text(body)

    includes = ""
    if include_order:
        for basename in include_order:
            includes += f'    <include file="{basename}"/>\n'
    includes += f'    <include file="{GRANTS_CHANGELOG_BASENAME}"/>\n'

    (changelog_dir / GRANTS_CHANGELOG_BASENAME).write_text(
        header + grants_changesets_xml + "\n" + footer
    )

    master = changelog_dir / "db.changelog-master.xml"
    master.write_text(header + includes + footer)
    return changelog_dir, master


_UNGUARDED_GRANTS_003 = f"""
    <changeSet id="{GRANTS_003_ID}" author="nexus-0ys55" runAlways="true">
        <sql>SELECT 'GRANT MAINTAIN ON nexus.chunks_384 TO nexus_svc';</sql>
    </changeSet>
"""

_GUARDED_GRANTS_003 = f"""
    <changeSet id="{GRANTS_003_ID}" author="nexus-0ys55" runAlways="true">
        <sql>DO $$
BEGIN
    IF to_regclass('nexus.chunks_384') IS NULL THEN
        RETURN;
    END IF;
    EXECUTE 'GRANT MAINTAIN ON nexus.chunks_384 TO nexus_svc';
END
$$;</sql>
    </changeSet>
"""

# The nexus-ixsxa ANTI-shape: a preConditions MARK_RAN guard is functionally an
# existence guard but appends a DATABASECHANGELOG row per unmet boot on a
# runAlways changeset (see test_changelog_markran_lint.py). The coupling lint
# must NOT accept it as "guarded".
_MARKRAN_GUARDED_GRANTS_003 = f"""
    <changeSet id="{GRANTS_003_ID}" author="nexus-0ys55" runAlways="true">
        <preConditions onFail="MARK_RAN">
            <sqlCheck expectedResult="1">SELECT (to_regclass('nexus.chunks_384') IS NOT NULL)::int</sqlCheck>
        </preConditions>
        <sql>SELECT 'GRANT MAINTAIN ON nexus.chunks_384 TO nexus_svc';</sql>
    </changeSet>
"""

_GRANTS_005 = f"""
    <changeSet id="{GRANTS_005_ID}" author="nexus-o8dil.12" runAlways="true">
        <sql>SELECT 'GRANT MAINTAIN ON nexus.chunks TO nexus_svc';</sql>
    </changeSet>
"""

_FULL_GRANTS_FIX = _GUARDED_GRANTS_003 + _GRANTS_005


# ---------------------------------------------------------------------------
# The two CLEAN batch states.
# ---------------------------------------------------------------------------


def test_state_a_all_three_staged_is_a_no_op_pass(tmp_path):
    """State (a), today's real state: all three staged only, none
    registered. Nothing to enforce yet."""
    changelog_dir, master = _write_synthetic_batch(
        tmp_path,
        placements={b: "staged" for b in BATCH_FILES},
        include_order=None,
        grants_changesets_xml=_UNGUARDED_GRANTS_003,
    )
    assert analyze_batch_registration_coupling(changelog_dir, master) == []


def test_state_b_all_three_fully_registered_in_order_is_clean(tmp_path):
    """State (b), the fully-landed batch end-state: all three moved back,
    included in the correct relative order, grants pieces present."""
    changelog_dir, master = _write_synthetic_batch(
        tmp_path,
        placements={b: "target" for b in BATCH_FILES},
        include_order=BATCH_FILES,
        grants_changesets_xml=_FULL_GRANTS_FIX,
    )
    assert analyze_batch_registration_coupling(changelog_dir, master) == []


# ---------------------------------------------------------------------------
# State (c): split-batch (the NEW arm this consolidation adds).
# ---------------------------------------------------------------------------


def test_state_c_split_batch_two_registered_one_still_staged_is_flagged(tmp_path):
    """The critical new arm: vectors-004 and taxonomy-007 are BOTH
    individually well-formed and fully registered, but vectors-005 is still
    cleanly staged. Neither old per-file lint would catch this (each would
    separately report its own file clean) -- this is exactly the
    partial-batch shape R1/R2 say must be structurally impossible."""
    changelog_dir, master = _write_synthetic_batch(
        tmp_path,
        placements={VECTORS_004: "target", TAXONOMY_007: "target", VECTORS_005: "staged"},
        include_order=[VECTORS_004, TAXONOMY_007],
        grants_changesets_xml=_FULL_GRANTS_FIX,
    )
    violations = analyze_batch_registration_coupling(changelog_dir, master)
    assert len(violations) == 3, violations
    assert any(VECTORS_004 in v and "split across states" in v for v in violations), violations
    assert any(TAXONOMY_007 in v and "split across states" in v for v in violations), violations
    assert any(VECTORS_005 in v and "split across states" in v for v in violations), violations


def test_state_c_split_batch_one_registered_two_staged_is_flagged(tmp_path):
    """The mirror shape: only vectors-004 has been registered so far. Every
    well-formed-but-mismatched file in the batch is named (not just the
    minority), so a reader sees at a glance which files are out of sync."""
    changelog_dir, master = _write_synthetic_batch(
        tmp_path,
        placements={VECTORS_004: "target", TAXONOMY_007: "staged", VECTORS_005: "staged"},
        include_order=[VECTORS_004],
        grants_changesets_xml=_FULL_GRANTS_FIX,
    )
    violations = analyze_batch_registration_coupling(changelog_dir, master)
    assert len(violations) == 3, violations
    assert all("split across states" in v for v in violations), violations
    assert any(VECTORS_004 in v for v in violations), violations
    assert any(TAXONOMY_007 in v for v in violations), violations
    assert any(VECTORS_005 in v for v in violations), violations


# ---------------------------------------------------------------------------
# State (c): ordering violation once fully registered.
# ---------------------------------------------------------------------------


def test_state_c_wrong_include_order_is_flagged(tmp_path):
    """All three registered, but in the WRONG relative order (vectors-005
    included before taxonomy-007) -- must be flagged even though every
    individual file's own placement (staged xor target+included) is
    locally clean."""
    changelog_dir, master = _write_synthetic_batch(
        tmp_path,
        placements={b: "target" for b in BATCH_FILES},
        include_order=[VECTORS_004, VECTORS_005, TAXONOMY_007],
        grants_changesets_xml=_FULL_GRANTS_FIX,
    )
    violations = analyze_batch_registration_coupling(changelog_dir, master)
    assert len(violations) == 1, violations
    assert "batch include order is WRONG" in violations[0], violations


# ---------------------------------------------------------------------------
# State (c): every per-file malformed-placement arm, preserved from the two
# lints this file replaces (one representative pass per shape, keyed on
# vectors-005 -- the file with no extra requirement, isolating the
# placement-shape check from the grants-requirement check already covered
# above).
# ---------------------------------------------------------------------------


def test_state_c_present_in_both_locations_simultaneously_is_flagged(tmp_path):
    """vectors-005 is genuinely malformed (present in both places); the
    other two are individually "staged" but that no longer matches the
    now-malformed third file's state, so all three are named."""
    changelog_dir, master = _write_synthetic_batch(
        tmp_path,
        placements={VECTORS_004: "staged", TAXONOMY_007: "staged", VECTORS_005: "both"},
        include_order=None,
        grants_changesets_xml=_UNGUARDED_GRANTS_003,
    )
    violations = analyze_batch_registration_coupling(changelog_dir, master)
    assert any(
        VECTORS_005 in v and "BOTH" in v for v in violations
    ), violations


def test_state_c_present_in_neither_location_is_flagged(tmp_path):
    changelog_dir, master = _write_synthetic_batch(
        tmp_path,
        placements={VECTORS_004: "staged", TAXONOMY_007: "staged", VECTORS_005: "neither"},
        include_order=None,
        grants_changesets_xml=_UNGUARDED_GRANTS_003,
    )
    violations = analyze_batch_registration_coupling(changelog_dir, master)
    assert any(
        VECTORS_005 in v and "not found in either" in v for v in violations
    ), violations


def test_state_c_included_but_file_absent_from_target_is_flagged(tmp_path):
    changelog_dir, master = _write_synthetic_batch(
        tmp_path,
        placements={VECTORS_004: "staged", TAXONOMY_007: "staged", VECTORS_005: "staged"},
        include_order=[VECTORS_005],
        grants_changesets_xml=_UNGUARDED_GRANTS_003,
    )
    violations = analyze_batch_registration_coupling(changelog_dir, master)
    # vectors-005 is malformed (included but not at target); the other two
    # are individually "staged" but the batch states now differ.
    assert any(
        VECTORS_005 in v and "Liquibase will fail to load it" in v for v in violations
    ), violations


def test_state_c_moved_to_target_but_not_yet_included_is_flagged(tmp_path):
    changelog_dir, master = _write_synthetic_batch(
        tmp_path,
        placements={VECTORS_004: "staged", TAXONOMY_007: "staged", VECTORS_005: "target"},
        include_order=None,
        grants_changesets_xml=_UNGUARDED_GRANTS_003,
    )
    violations = analyze_batch_registration_coupling(changelog_dir, master)
    assert any(VECTORS_005 in v and "NOT YET" in v for v in violations), violations


# ---------------------------------------------------------------------------
# State (c): vectors-004's grants requirement, preserved from the lint this
# file replaces.
# ---------------------------------------------------------------------------


def test_state_c_registered_batch_with_unguarded_grants003_and_no_grants005(tmp_path):
    changelog_dir, master = _write_synthetic_batch(
        tmp_path,
        placements={b: "target" for b in BATCH_FILES},
        include_order=BATCH_FILES,
        grants_changesets_xml=_UNGUARDED_GRANTS_003,
    )
    violations = analyze_batch_registration_coupling(changelog_dir, master)
    assert len(violations) == 2, violations
    assert any(
        GRANTS_003_ID in v and "in-body existence guard" in v for v in violations
    ), violations
    assert any(GRANTS_005_ID in v for v in violations), violations


def test_state_c_markran_precondition_guard_is_rejected_not_accepted(tmp_path):
    """The nexus-ixsxa anti-shape: a preConditions MARK_RAN guard must NOT
    satisfy the coupling lint's guard requirement -- on a runAlways changeset
    it appends one DATABASECHANGELOG row per unmet-precondition boot,
    unbounded (test_changelog_markran_lint.py owns the shape ban; this arm
    keeps THIS lint from steering anyone back into it)."""
    changelog_dir, master = _write_synthetic_batch(
        tmp_path,
        placements={b: "target" for b in BATCH_FILES},
        include_order=BATCH_FILES,
        grants_changesets_xml=_MARKRAN_GUARDED_GRANTS_003 + _GRANTS_005,
    )
    violations = analyze_batch_registration_coupling(changelog_dir, master)
    assert len(violations) == 1, violations
    assert GRANTS_003_ID in violations[0], violations
    assert "in-body existence guard" in violations[0], violations


def test_state_c_registered_batch_with_guarded_grants003_but_missing_grants005(tmp_path):
    changelog_dir, master = _write_synthetic_batch(
        tmp_path,
        placements={b: "target" for b in BATCH_FILES},
        include_order=BATCH_FILES,
        grants_changesets_xml=_GUARDED_GRANTS_003,
    )
    violations = analyze_batch_registration_coupling(changelog_dir, master)
    assert len(violations) == 1, violations
    assert GRANTS_005_ID in violations[0], violations


# ---------------------------------------------------------------------------
# Real repository guard.
# ---------------------------------------------------------------------------


def test_real_repo_state_today_passes():
    """Guard against the real repository: as of this lint's authorship, all
    three batch files are staged at db/changelog-staged/ (state (a)). Must
    pass today; goes RED the moment any file's placement, inclusion, order,
    or grants-piece requirement deviates from a clean state (a) or (b)."""
    assert analyze_batch_registration_coupling() == []
