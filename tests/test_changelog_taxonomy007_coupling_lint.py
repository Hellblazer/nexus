# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-191 Phase 4, centroid family (bead nexus-jv3ue) — registration-coupling
lint, the SIBLING of tests/test_changelog_vectors004_grants_coupling_lint.py.

Provenance: mirrors the chunks-side lint's three-state invariant
(staged-and-dormant / fully-registered / anything-else-is-RED) for
taxonomy-007-unify-centroids.xml, staged at
service/src/main/resources/db/changelog-staged/. See that file's own header
and tests/test_changelog_vectors004_grants_coupling_lint.py's module
docstring for the shared staging rationale (the changelog-parity drift
lints -- tests/test_rehearsal_seed_coverage_lint.py,
tests/test_changelog_rls_lint.py, tests/test_changelog_validate_precondition_lint.py
-- assert the master include list matches CHANGELOG_DIR's on-disk *.xml
contents EXACTLY; a dormant-but-tested changeset cannot live in
CHANGELOG_DIR without tripping them, so it lives in a sibling
db/changelog-staged/ directory instead).

WHY THIS IS A SIBLING FILE, NOT AN EXTENSION of the chunks lint (judged per
this bead's own instruction to judge extend-vs-sibling): the two changesets'
BATCH REQUIREMENTS diverge, not just their filenames. The chunks changeset
causes a grants-nexus-svc.xml boot-brick on registration (grants-003-purge-
vacuum-maintain unconditionally GRANTs MAINTAIN on chunks_384/768/1024 by
name, runAlways) requiring TWO grants-nexus-svc.xml pieces to land in the
same batch as its <include>. The CENTROID changeset causes NO such
boot-brick -- verified by direct grep of grants-nexus-svc.xml for
"taxonomy_centroids" (zero hits) and by direct read of
CatalogRepository.PURGE_VACUUM_TABLES / TenantScope.VACUUM_ALLOWED_TABLES
(neither names a centroid table; see taxonomy-007-unify-centroids.xml's own
header for the full derivation). This lint's invariant is therefore
strictly SIMPLER: mv + <include> atomicity only, no grants-piece
requirement to check. Reusing the chunks lint's _grants_violations-shaped
machinery for a requirement that doesn't exist here would either (a) always
report zero violations (dead code, misleading to a reader who assumes it
means something) or (b) need a parallel no-op branch threaded through the
chunks file's existing 10 tests -- both worse than a small sibling file
with its own, honestly-simpler, invariant.

THIS LINT's INVARIANT (two clean states, TWO possible violation shapes --
fewer than the chunks lint's four, precisely because there is no grants
axis to go wrong):

  (a) STAGED, NOT REGISTERED (today's real state): the file exists ONLY at
      db/changelog-staged/taxonomy-007-unify-centroids.xml, absent from
      CHANGELOG_DIR, and NOT named in the master include list. PASS --
      nothing to enforce yet.
  (b) FULLY REGISTERED (the batch end-state): the file exists ONLY at
      CHANGELOG_DIR/taxonomy-007-unify-centroids.xml (moved back) AND IS
      named in the master include list. PASS -- no grants piece to verify.
  (c) ANY OTHER COMBINATION is a violation, named specifically: present in
      both locations at once (ambiguous), present in neither (moved or
      deleted unexpectedly), <include>d but absent from CHANGELOG_DIR
      (Liquibase will fail to load it), or moved to CHANGELOG_DIR but not
      yet <include>d (a half-landed registration).

WHAT THIS LINT REUSES: the changelog-lint family's parsing machinery from
tests/test_changelog_rls_lint.py (parse_master_include_order, CHANGELOG_DIR,
MASTER_CHANGELOG), the same reuse discipline the chunks lint already
established for this family.

Kill-control evidence for every arm is via SYNTHETIC changelog trees (see
_write_synthetic_pair below), matching this family's convention of never
depending on mutating real repository files for the PERMANENT test suite;
test_real_repo_state_today_passes additionally guards the live repository
directly. A one-time manual kill-control against the REAL files (temporarily
moving taxonomy-007-unify-centroids.xml into CHANGELOG_DIR without adding
the <include>, watching this guard go RED, reverting and byte-diffing) is
recorded in T2 nexus/rdr-191-p4-unify-centroids-changeset-2026-08-13, the
same round-3 kill-control method the chunks lint used.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_changelog_rls_lint import (
    CHANGELOG_DIR,
    MASTER_CHANGELOG,
    parse_master_include_order,
)

pytestmark = pytest.mark.lint

UNIFY_CHANGELOG_BASENAME = "taxonomy-007-unify-centroids.xml"
STAGED_DIR_NAME = "changelog-staged"


def analyze_registration_coupling(
    changelog_dir: Path = CHANGELOG_DIR, master_path: Path = MASTER_CHANGELOG
) -> list[str]:
    """Return a list of violation strings for the two-clean-state invariant
    described in the module docstring. Empty means either state (a) (staged,
    not registered -- nothing to enforce yet) or state (b) (fully
    registered -- moved back and <include>d; no grants piece to verify for
    the centroid family, see module docstring)."""
    staged_path = changelog_dir.parent / STAGED_DIR_NAME / UNIFY_CHANGELOG_BASENAME
    target_path = changelog_dir / UNIFY_CHANGELOG_BASENAME
    staged_exists = staged_path.exists()
    target_exists = target_path.exists()
    included = UNIFY_CHANGELOG_BASENAME in parse_master_include_order(master_path)

    # State (a): dormant and staged -- the correct "not yet landed" shape.
    if staged_exists and not target_exists and not included:
        return []

    # State (b): moved back and included -- fully registered, nothing else
    # to verify (no grants axis for the centroid family).
    if target_exists and not staged_exists and included:
        return []

    # Anything else is a malformed intermediate state; name every mismatch
    # found rather than picking just one, since more than one can co-occur.
    violations: list[str] = []

    if staged_exists and target_exists:
        violations.append(
            f"{UNIFY_CHANGELOG_BASENAME} exists in BOTH {STAGED_DIR_NAME}/ and "
            f"{changelog_dir.name}/ simultaneously -- ambiguous, remove one"
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
            "load it at boot (still staged? finish moving it back)"
        )

    if target_exists and not included and not staged_exists:
        violations.append(
            f"{UNIFY_CHANGELOG_BASENAME} has been moved to {changelog_dir.name}/ but is "
            f"NOT YET <include>d in {master_path.name} -- either finish the "
            f"registration (add the <include>) or move it back to {STAGED_DIR_NAME}/ "
            "to restore the dormant-and-clean state"
        )

    return violations


# ===========================================================================
# Tests
# ===========================================================================


def _write_synthetic_pair(
    tmp_path: Path,
    unify_location: str,
    include_unify: bool,
) -> tuple[Path, Path]:
    """Write a minimal synthetic changelog dir with a master.xml. *unify_location*
    is one of "staged", "target", "both", "neither" -- controls where (if
    anywhere) the stub taxonomy-007-unify-centroids.xml is written, modeling
    the file-placement axis independently of the include-list axis. Returns
    (changelog_dir, master_path)."""
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
        + '    <changeSet id="taxonomy-007-1" author="t"><sql>SELECT 1;</sql></changeSet>\n'
        + footer
    )

    if unify_location in ("staged", "both"):
        (staged_dir / UNIFY_CHANGELOG_BASENAME).write_text(unify_body)
    if unify_location in ("target", "both"):
        (changelog_dir / UNIFY_CHANGELOG_BASENAME).write_text(unify_body)

    includes = ""
    if include_unify:
        includes += f'    <include file="{UNIFY_CHANGELOG_BASENAME}"/>\n'

    master = changelog_dir / "db.changelog-master.xml"
    master.write_text(header + includes + footer)
    return changelog_dir, master


# ---------------------------------------------------------------------------
# The two CLEAN states.
# ---------------------------------------------------------------------------


def test_state_a_staged_not_registered_is_a_no_op_pass(tmp_path):
    """State (a), today's real state: staged only, absent from CHANGELOG_DIR,
    not <include>d. Nothing to enforce yet."""
    changelog_dir, master = _write_synthetic_pair(
        tmp_path, unify_location="staged", include_unify=False
    )
    assert analyze_registration_coupling(changelog_dir, master) == []


def test_state_b_fully_registered_is_clean(tmp_path):
    """State (b), the fully-landed shape: moved to CHANGELOG_DIR and
    <include>d -- must pass cleanly, no grants piece to verify."""
    changelog_dir, master = _write_synthetic_pair(
        tmp_path, unify_location="target", include_unify=True
    )
    assert analyze_registration_coupling(changelog_dir, master) == []


# ---------------------------------------------------------------------------
# State (c): every malformed intermediate combination.
# ---------------------------------------------------------------------------


def test_state_c_present_in_both_locations_simultaneously_is_flagged(tmp_path):
    """The file exists at BOTH the staged path and CHANGELOG_DIR at once --
    ambiguous regardless of the include-list state, must be flagged."""
    changelog_dir, master = _write_synthetic_pair(
        tmp_path, unify_location="both", include_unify=False
    )
    violations = analyze_registration_coupling(changelog_dir, master)
    assert len(violations) == 1, violations
    assert "BOTH" in violations[0], violations


def test_state_c_present_in_neither_location_is_flagged(tmp_path):
    """The file is absent from both the staged path and CHANGELOG_DIR --
    moved or deleted unexpectedly."""
    changelog_dir, master = _write_synthetic_pair(
        tmp_path, unify_location="neither", include_unify=False
    )
    violations = analyze_registration_coupling(changelog_dir, master)
    assert len(violations) == 1, violations
    assert "not found in either" in violations[0], violations


def test_state_c_included_but_file_absent_from_target_is_flagged(tmp_path):
    """The master <include>s the changeset by basename, but the file is only
    present at the staged path -- Liquibase would fail to load it at boot."""
    changelog_dir, master = _write_synthetic_pair(
        tmp_path, unify_location="staged", include_unify=True
    )
    violations = analyze_registration_coupling(changelog_dir, master)
    assert len(violations) == 1, violations
    assert "Liquibase will fail to load it" in violations[0], violations


def test_state_c_moved_to_target_but_not_yet_included_is_flagged(tmp_path):
    """The file has been moved back to CHANGELOG_DIR but the <include> has
    not been added yet -- a half-finished registration, distinct from state
    (a)'s clean dormant shape."""
    changelog_dir, master = _write_synthetic_pair(
        tmp_path, unify_location="target", include_unify=False
    )
    violations = analyze_registration_coupling(changelog_dir, master)
    assert len(violations) == 1, violations
    assert "NOT YET" in violations[0], violations


# ---------------------------------------------------------------------------
# Real repository guard.
# ---------------------------------------------------------------------------


def test_real_repo_state_today_passes():
    """Guard against the real repository: as of this lint's authorship,
    taxonomy-007-unify-centroids.xml is staged at db/changelog-staged/
    (state (a)). This must pass today and will go RED the moment the file
    lands in any state other than (a) staged-and-unregistered or (b)
    fully-registered. Kill-control demonstrated manually against the real
    files (round 3 method: real mv, watch RED, revert, byte-diff) and
    recorded in T2 nexus/rdr-191-p4-unify-centroids-changeset-2026-08-13, not
    left as a permanent mutation here."""
    assert analyze_registration_coupling() == []
