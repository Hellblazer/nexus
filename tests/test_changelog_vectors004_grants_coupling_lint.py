# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-191 Phase 4 (beads nexus-o8dil.12/.13/.14) — registration-coupling lint.

Provenance: substantive-critic review round on vectors-004-unify-chunks.xml
(T2 nexus/critique-p4-unify-and-o8dil50-2026-08-13 [22436], Significant S1).
The changeset drops nexus.chunks_384/768/1024 in one transaction. The MOMENT
it is <include>d in db.changelog-master.xml, grants-nexus-svc.xml's
grants-003-purge-vacuum-maintain changeset (runAlways="true") becomes a boot
brick: it unconditionally ``EXECUTE 'GRANT MAINTAIN ON nexus.chunks_384 TO
nexus_svc'`` (and the 768/1024 siblings) with no existence guard, so the very
next boot after registration raises "relation does not exist" ->
MigrationException -> Main.exit(1), crash-looping every subsequent restart.

The fix (documented in vectors-004-unify-chunks.xml's own header and in T2
nexus/rdr-191-p4-unify-changeset-2026-08-13 [22433]'s registration appendix)
is two pieces landing together: (a) a checksum-neutral whole-changeset
``<preConditions onFail="MARK_RAN">`` existence guard added to grants-003
in place, and (b) a NEW changeset, grants-005-chunks-unify-maintain, that
takes over grants-003's MAINTAIN-grant responsibility for the surviving
table set (nexus.chunks + catalog_document_chunks + catalog_documents).

WHY THIS CANNOT WAIT for the jOOQ-codegen compile break to catch it: that
break only fires for a registration that ALSO omits the Java repoint (beads
nexus-o8dil.16/.17/.18/.19/.48) -- it says nothing about the grants half
specifically, and a hand-edit of db.changelog-master.xml (bypassing the bd
dependency graph that gates .21 behind .20 behind .41) could in principle
register vectors-004 with the Java repoint present but the grants fix
forgotten. The compile break is real and load-bearing for the Java half; it
is not a guard for this half. This lint is that guard, mechanical and cheap,
independent of bd process discipline.

STAGING (round 3): vectors-004-unify-chunks.xml does not live in
CHANGELOG_DIR at all while dormant -- it lives in a SIBLING directory,
``db/changelog-staged/`` (see db.changelog-master.xml's own comment, and
service/src/test/java/dev/nexus/service/VectorsUnifyChunksIntegrationTest.java,
which loads it from that path). Reason: the repo's changelog-parity lints
(tests/test_rehearsal_seed_coverage_lint.py,
tests/test_changelog_rls_lint.py,
tests/test_changelog_validate_precondition_lint.py) assert the master
include list matches CHANGELOG_DIR's on-disk ``*.xml`` contents EXACTLY --
correct, deliberate behavior that must never be weakened with an allowlist
entry (a file present on disk but not <include>d is exactly the drift class
those lints exist to catch). A dormant-but-tested changeset therefore cannot
live in CHANGELOG_DIR at all; ``db/changelog-staged/`` is a sibling, invisible
to those non-recursive globs, while remaining a normal classpath resource
Liquibase can load directly by path (everything under
``src/main/resources/`` is on the classpath).

THIS LINT's INVARIANT is therefore THREE-STATE, not the original two-state
(unregistered / registered) shape:

  (a) STAGED, NOT REGISTERED (today's real state): the file exists ONLY at
      ``db/changelog-staged/vectors-004-unify-chunks.xml``, absent from
      CHANGELOG_DIR, and NOT named in the master include list. PASS --
      nothing to enforce yet.
  (b) FULLY REGISTERED (the batch end-state): the file exists ONLY at
      ``CHANGELOG_DIR/vectors-004-unify-chunks.xml`` (moved back), IS named
      in the master include list, AND grants-nexus-svc.xml carries both
      required pieces of the boot-brick fix. PASS.
  (c) ANY OTHER COMBINATION is a violation, named specifically: the file
      present in both locations at once (ambiguous), present in neither
      (moved or deleted unexpectedly), <include>d but absent from
      CHANGELOG_DIR (Liquibase will fail to load it), moved to CHANGELOG_DIR
      but not yet <include>d (an incomplete registration, half-landed), or
      <include>d-and-present without one or both grants-nexus-svc.xml
      pieces (the original two-state check, now folded into this one as the
      "fully registered but grants incomplete" arm of state (b)'s candidacy).

WHAT THIS LINT REUSES: the changelog-lint family's parsing machinery from
``tests/test_changelog_rls_lint.py`` (``parse_master_include_order``,
``CHANGELOG_DIR``, ``MASTER_CHANGELOG``, ``_XSD_NS``) rather than
re-deriving it -- the same reuse discipline
``test_changelog_validate_precondition_lint.py`` already established for
this family. ``parse_master_include_order`` returns ``<include file="...">``
BASENAMES only (``Path(file_attr).name``), so "is vectors-004-unify-chunks.xml
in the include order" is insensitive to which directory the ``file``
attribute's path text names -- deliberate, since the registration checklist
puts it back at ``db/changelog/vectors-004-unify-chunks.xml`` and this lint
should not need editing if that convention is ever revisited.

Kill-control evidence for every arm (staged-not-included pass, the four (c)
violation shapes, and the fully-registered pass) is via SYNTHETIC changelog
trees (see ``_write_synthetic_pair`` below), matching this family's
convention of never depending on mutating real repository files for the
PERMANENT test suite; ``test_real_repo_state_today_passes`` additionally
guards the live repository directly, and a one-time manual kill-control
against the REAL files (temporarily registering the real file, watching this
guard go RED, reverting) is recorded in T2
nexus/rdr-191-p4-unify-changeset-2026-08-13 [22433]'s round-2 and round-3
sections.
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

UNIFY_CHANGELOG_BASENAME = "vectors-004-unify-chunks.xml"
STAGED_DIR_NAME = "changelog-staged"
GRANTS_CHANGELOG_BASENAME = "grants-nexus-svc.xml"
GRANTS_003_ID = "grants-003-purge-vacuum-maintain"
GRANTS_005_ID = "grants-005-chunks-unify-maintain"


def _grants_violations(changelog_dir: Path) -> list[str]:
    """The original two-state check, now the arm evaluated once state (b)
    (fully registered) is a candidate: both grants-nexus-svc.xml pieces of
    the boot-brick fix must be present."""
    grants_path = changelog_dir / GRANTS_CHANGELOG_BASENAME
    if not grants_path.exists():
        return [
            f"{UNIFY_CHANGELOG_BASENAME} is registered but "
            f"{GRANTS_CHANGELOG_BASENAME} does not exist at all"
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
        pc = grants_003.find(f"{_XSD_NS}preConditions")
        guarded = False
        if pc is not None:
            for sc in pc.iter(f"{_XSD_NS}sqlCheck"):
                body = "".join(sc.itertext())
                if "chunks_384" in body and "regclass" in body.lower():
                    guarded = True
                    break
        if not guarded:
            violations.append(
                f"{UNIFY_CHANGELOG_BASENAME} is registered but {GRANTS_003_ID} has no "
                "existence-guard <preConditions> naming chunks_384 via to_regclass -- "
                "it is runAlways=true and will unconditionally GRANT MAINTAIN on the "
                "now-dropped chunks_384/768/1024, crash-looping the service on the "
                "very next boot (see vectors-004-unify-chunks.xml header; T2 "
                "nexus/rdr-191-p4-unify-changeset-2026-08-13 [22433] registration "
                "appendix has the exact <preConditions> text to add)"
            )

    if grants_005 is None:
        violations.append(
            f"{UNIFY_CHANGELOG_BASENAME} is registered but {GRANTS_005_ID} (the "
            "MAINTAIN-grant successor for nexus.chunks/catalog_document_chunks/"
            f"catalog_documents) is missing from {GRANTS_CHANGELOG_BASENAME} -- "
            "MAINTAIN self-healing for the surviving tables is lost once grants-003 "
            "freezes (T2 [22433] registration appendix has the exact changeset to add)"
        )

    return violations


def analyze_registration_coupling(
    changelog_dir: Path = CHANGELOG_DIR, master_path: Path = MASTER_CHANGELOG
) -> list[str]:
    """Return a list of violation strings for the three-state invariant
    described in the module docstring. Empty means either state (a) (staged,
    not registered -- nothing to enforce yet) or state (b) (fully registered,
    grants-nexus-svc.xml carries both required pieces)."""
    staged_path = changelog_dir.parent / STAGED_DIR_NAME / UNIFY_CHANGELOG_BASENAME
    target_path = changelog_dir / UNIFY_CHANGELOG_BASENAME
    staged_exists = staged_path.exists()
    target_exists = target_path.exists()
    included = UNIFY_CHANGELOG_BASENAME in parse_master_include_order(master_path)

    # State (a): dormant and staged -- the correct "not yet landed" shape.
    if staged_exists and not target_exists and not included:
        return []

    # State (b) candidate: moved back, included, and ONLY the grants pieces
    # remain to verify.
    if target_exists and not staged_exists and included:
        return _grants_violations(changelog_dir)

    # Anything else is a malformed intermediate state; name every mismatch
    # found rather than picking just one, since more than one can co-occur.
    violations: list[str] = []

    if staged_exists and target_exists:
        violations.append(
            f"{UNIFY_CHANGELOG_BASENAME} exists in BOTH {STAGED_DIR_NAME}/ and "
            f"{changelog_dir.name}/ simultaneously -- ambiguous, remove one "
            "(T2 nexus/rdr-191-p4-unify-changeset-2026-08-13 [22433] registration checklist)"
        )

    if not staged_exists and not target_exists:
        violations.append(
            f"{UNIFY_CHANGELOG_BASENAME} not found in either {STAGED_DIR_NAME}/ or "
            f"{changelog_dir.name}/ -- moved or deleted unexpectedly"
        )

    if included and not target_exists:
        violations.append(
            f"{UNIFY_CHANGELOG_BASENAME} is <include>d in {master_path.name} but the "
            f"file is not present at {changelog_dir.name}/ -- Liquibase will fail to "
            "load it at boot (still staged? finish moving it back per T2 [22433])"
        )

    if target_exists and not included and not staged_exists:
        violations.append(
            f"{UNIFY_CHANGELOG_BASENAME} has been moved to {changelog_dir.name}/ but is "
            f"NOT YET <include>d in {master_path.name} -- either finish the registration "
            "(add the <include> plus the grants-nexus-svc.xml pieces, T2 [22433] "
            f"checklist) or move it back to {STAGED_DIR_NAME}/ to restore the "
            "dormant-and-clean state"
        )

    if included and target_exists:
        violations.extend(_grants_violations(changelog_dir))

    return violations


# ===========================================================================
# Tests
# ===========================================================================


def _write_synthetic_pair(
    tmp_path: Path,
    unify_location: str,
    include_unify: bool,
    grants_changesets_xml: str,
) -> tuple[Path, Path]:
    """Write a minimal synthetic changelog dir with a master.xml and a
    grants-nexus-svc.xml carrying *grants_changesets_xml* verbatim.
    *unify_location* is one of "staged", "target", "both", "neither" --
    controls where (if anywhere) the stub vectors-004-unify-chunks.xml is
    written, modeling the three-state invariant's file-placement axis
    independently of the include-list axis. Returns (changelog_dir, master_path)."""
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
    unify_body = (
        header
        + '    <changeSet id="vectors-004-1" author="t"><sql>SELECT 1;</sql></changeSet>\n'
        + footer
    )

    if unify_location in ("staged", "both"):
        (staged_dir / UNIFY_CHANGELOG_BASENAME).write_text(unify_body)
    if unify_location in ("target", "both"):
        (changelog_dir / UNIFY_CHANGELOG_BASENAME).write_text(unify_body)

    (changelog_dir / GRANTS_CHANGELOG_BASENAME).write_text(
        header + grants_changesets_xml + "\n" + footer
    )

    includes = ""
    if include_unify:
        includes += f'    <include file="{UNIFY_CHANGELOG_BASENAME}"/>\n'
    includes += f'    <include file="{GRANTS_CHANGELOG_BASENAME}"/>\n'

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


# ---------------------------------------------------------------------------
# The two CLEAN states.
# ---------------------------------------------------------------------------


def test_state_a_staged_not_registered_is_a_no_op_pass(tmp_path):
    """State (a), today's real state: staged only, absent from CHANGELOG_DIR,
    not <include>d. Nothing to enforce yet -- must be a clean pass regardless
    of what grants-nexus-svc.xml looks like."""
    changelog_dir, master = _write_synthetic_pair(
        tmp_path,
        unify_location="staged",
        include_unify=False,
        grants_changesets_xml=_UNGUARDED_GRANTS_003,
    )
    assert analyze_registration_coupling(changelog_dir, master) == []


def test_state_b_fully_registered_with_both_grants_pieces_is_clean(tmp_path):
    """State (b), the fully-landed shape: moved to CHANGELOG_DIR, <include>d,
    T2 [22433]'s registration checklist applied in full -- must pass cleanly."""
    changelog_dir, master = _write_synthetic_pair(
        tmp_path,
        unify_location="target",
        include_unify=True,
        grants_changesets_xml=_GUARDED_GRANTS_003 + _GRANTS_005,
    )
    assert analyze_registration_coupling(changelog_dir, master) == []


# ---------------------------------------------------------------------------
# State (c): every malformed intermediate combination.
# ---------------------------------------------------------------------------


def test_state_c_registered_with_unguarded_grants003_and_no_grants005_is_flagged(
    tmp_path,
):
    """Moved + included, but neither grants-nexus-svc.xml piece landed -- the
    exact partial-landing shape this lint exists to catch."""
    changelog_dir, master = _write_synthetic_pair(
        tmp_path,
        unify_location="target",
        include_unify=True,
        grants_changesets_xml=_UNGUARDED_GRANTS_003,
    )
    violations = analyze_registration_coupling(changelog_dir, master)
    assert len(violations) == 2, violations
    assert any(GRANTS_003_ID in v and "preConditions" in v for v in violations), violations
    assert any(GRANTS_005_ID in v for v in violations), violations


def test_state_c_registered_with_guarded_grants003_but_missing_grants005_is_flagged(
    tmp_path,
):
    """Half the grants fix landing is still a violation -- MAINTAIN
    self-healing for the surviving tables would silently stop once grants-003
    freezes."""
    changelog_dir, master = _write_synthetic_pair(
        tmp_path,
        unify_location="target",
        include_unify=True,
        grants_changesets_xml=_GUARDED_GRANTS_003,
    )
    violations = analyze_registration_coupling(changelog_dir, master)
    assert len(violations) == 1, violations
    assert GRANTS_005_ID in violations[0], violations


def test_state_c_decoy_preconditions_not_naming_chunks_384_is_still_flagged(tmp_path):
    """A <preConditions> element merely PRESENT on grants-003, but whose
    sqlCheck doesn't actually reference chunks_384/regclass, must not be
    treated as the guard -- same anchoring discipline as the sibling
    VALIDATE-precondition lint's decoy-guard rejection."""
    decoy = f"""
    <changeSet id="{GRANTS_003_ID}" author="nexus-0ys55" runAlways="true">
        <preConditions onFail="MARK_RAN">
            <sqlCheck expectedResult="1">SELECT 1</sqlCheck>
        </preConditions>
        <sql>SELECT 'GRANT MAINTAIN ON nexus.chunks_384 TO nexus_svc';</sql>
    </changeSet>
""" + _GRANTS_005
    changelog_dir, master = _write_synthetic_pair(
        tmp_path, unify_location="target", include_unify=True, grants_changesets_xml=decoy
    )
    violations = analyze_registration_coupling(changelog_dir, master)
    assert len(violations) == 1, violations
    assert GRANTS_003_ID in violations[0], violations


def test_state_c_present_in_both_locations_simultaneously_is_flagged(tmp_path):
    """The file exists at BOTH the staged path and CHANGELOG_DIR at once --
    ambiguous regardless of the include-list state, must be flagged."""
    changelog_dir, master = _write_synthetic_pair(
        tmp_path,
        unify_location="both",
        include_unify=False,
        grants_changesets_xml=_GUARDED_GRANTS_003 + _GRANTS_005,
    )
    violations = analyze_registration_coupling(changelog_dir, master)
    assert len(violations) == 1, violations
    assert "BOTH" in violations[0], violations


def test_state_c_present_in_neither_location_is_flagged(tmp_path):
    """The file is absent from both the staged path and CHANGELOG_DIR --
    moved or deleted unexpectedly."""
    changelog_dir, master = _write_synthetic_pair(
        tmp_path,
        unify_location="neither",
        include_unify=False,
        grants_changesets_xml=_UNGUARDED_GRANTS_003,
    )
    violations = analyze_registration_coupling(changelog_dir, master)
    assert len(violations) == 1, violations
    assert "not found in either" in violations[0], violations


def test_state_c_included_but_file_absent_from_target_is_flagged(tmp_path):
    """The master <include>s the changeset by basename, but the file is only
    present at the staged path -- Liquibase would fail to load it at boot."""
    changelog_dir, master = _write_synthetic_pair(
        tmp_path,
        unify_location="staged",
        include_unify=True,
        grants_changesets_xml=_GUARDED_GRANTS_003 + _GRANTS_005,
    )
    violations = analyze_registration_coupling(changelog_dir, master)
    assert len(violations) == 1, violations
    assert "Liquibase will fail to load it" in violations[0], violations


def test_state_c_moved_to_target_but_not_yet_included_is_flagged(tmp_path):
    """The file has been moved back to CHANGELOG_DIR but the <include> has
    not been added yet -- a half-finished registration, distinct from state
    (a)'s clean dormant shape."""
    changelog_dir, master = _write_synthetic_pair(
        tmp_path,
        unify_location="target",
        include_unify=False,
        grants_changesets_xml=_UNGUARDED_GRANTS_003,
    )
    violations = analyze_registration_coupling(changelog_dir, master)
    assert len(violations) == 1, violations
    assert "NOT YET" in violations[0], violations


# ---------------------------------------------------------------------------
# Real repository guard.
# ---------------------------------------------------------------------------


def test_real_repo_state_today_passes():
    """Guard against the real repository: as of this lint's authorship,
    vectors-004-unify-chunks.xml is staged at db/changelog-staged/ (state (a)
    -- see db.changelog-master.xml's own comment explaining why, T2
    nexus/rdr-191-p4-unify-changeset-2026-08-13 [22433]). This must pass
    today and will go RED the moment the file lands in any state other than
    (a) staged-and-unregistered or (b) fully-registered-with-both-grants-
    pieces. Kill-control demonstrated manually against the real files (round
    2 and round 3) and recorded in T2 [22433], not left as a permanent
    mutation here."""
    assert analyze_registration_coupling() == []
