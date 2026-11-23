# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.5c (nexus-p2m1x.25): a disposition that says VIOLATING is a known post-latch label read, and P3.7 must flip it.

Both disposition files (``p35b-reader-dispositions.tsv``, ``p35c-java-reader-dispositions.tsv``) may carry a reason that starts
``VIOLATING``: the site matches a collection by its label after the latch (gate 3) and P3.7 (wren) switches it to the id. Such a
row is ``label-kept`` until then, so a pin that counts id and both rows as id readers is not misled. This test keeps the list
closed and makes sure P3.7 cannot ship with one left:

* a VIOLATING row must be in ``PENDING_P37`` and every ``PENDING_P37`` entry must be a VIOLATING row (the constant is the list
  P3.7 works through; it holds the row key, so a fifth site cannot be added by editing a reason alone);
* once ``vectors-083-*.xml`` (P3.7's changeset) is in the tree, ``PENDING_P37`` must be empty and no reason may start
  ``VIOLATING``. The trigger is a file in the tree, so the test cannot skip quietly.

Two notes on the trigger (linda 2026-11-24):
1. vectors-083 already exists in P3.7's commits that come before its gc_expire and quarantineRestoreSelected flips, so the guard is
   RED on those middle commits. That is intended: only P3.7's tip stacks. Do not loosen the guard to make a middle commit green.
2. If P3.7 were dropped from the cut, the guard passes with the VIOLATING rows still in place. That is correct only because dropping
   P3.7 is Sam's decision, which would ship those known violations knowingly.
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).parent.parent
_RES = _REPO / "service" / "src" / "test" / "resources" / "rdr229"
_CHANGELOG = _REPO / "service" / "src" / "main" / "resources" / "db" / "changelog"
_FILES = (_RES / "p35b-reader-dispositions.tsv", _RES / "p35c-java-reader-dispositions.tsv")
_KEY = "metadata.origin_collection"

# (kind, object, name_column) of every disposition row P3.7 still owes a flip to `id` (wren; vectors-083 and taggedWith by id):
#   quarantineRestoreSelected (PgVectorRepository.java, two rows: the expr and the where on the same line), and the SQL functions
#   gc_expire_quarantine, gc_expire_quarantine_bounded, gc_restore_rereferenced, gc_restore_rereferenced_bounded,
#   quarantine_restore_chunks and quarantine_reattach_plan, whose live definer (vectors-077) matches COALESCE(metadata->>'origin_collection',
#   p_origin_collection) = p_origin_collection with no latch arm.
PENDING_P37: frozenset[tuple[str, str, str]] = frozenset({
    ("java", "vectors/PgVectorRepository.java chunks expr", _KEY),
    ("java", "vectors/PgVectorRepository.java chunks where", _KEY),
    ("function", "nexus.gc_expire_quarantine", _KEY),
    ("function", "nexus.gc_expire_quarantine_bounded", _KEY),
    ("function", "nexus.gc_restore_rereferenced", _KEY),
    ("function", "nexus.gc_restore_rereferenced_bounded", _KEY),
    ("function", "nexus.quarantine_restore_chunks", _KEY),
    ("function", "nexus.quarantine_reattach_plan", _KEY),
})


def _violating(files=_FILES) -> set[tuple[str, str, str]]:
    out = set()
    for f in files:
        for ln in f.read_text().splitlines():
            if not ln or ln.startswith("#") or ln.startswith("kind\t"):
                continue
            kind, obj, col, _disposition, reason = (ln.split("\t") + [""] * 5)[:5]
            if reason.strip().startswith("VIOLATING"):
                out.add((kind, obj, col))
    return out


def _guard_breaches(violating, pending, changelog_dir: Path) -> list[str]:
    out = []
    if any(changelog_dir.glob("vectors-083-*.xml")):
        if pending:
            out.append(f"vectors-083 is in the tree, so PENDING_P37 must be empty; it still holds {sorted(pending)}")
        if violating:
            out.append(f"vectors-083 is in the tree, so no reason may start VIOLATING; still: {sorted(violating)}")
        return out
    if violating - pending:
        out.append(f"VIOLATING rows not in PENDING_P37: {sorted(violating - pending)}")
    if pending - violating:
        out.append(f"PENDING_P37 entries with no VIOLATING row: {sorted(pending - violating)}")
    return out


def test_the_pending_p37_list_is_exactly_the_violating_rows_and_is_empty_once_p37_lands():
    violating = _violating()
    assert len(violating) == len(PENDING_P37), "non-vacuity: the guard must have read the VIOLATING rows of both files"
    assert _guard_breaches(violating, PENDING_P37, _CHANGELOG) == []


def test_the_guard_fails_when_p37_lands_with_a_violating_row_and_passes_without_it(tmp_path):
    some = {("function", "nexus.f", _KEY)}
    # without vectors-083: the list and the rows agree, so it passes; a row outside the list, or a stale entry, fails
    assert _guard_breaches(some, some, tmp_path) == []
    assert _guard_breaches(some | {("function", "nexus.g", _KEY)}, some, tmp_path) != []
    assert _guard_breaches(set(), some, tmp_path) != []
    # with a planted vectors-083: any VIOLATING row, or any PENDING_P37 entry, fails; both empty passes
    (tmp_path / "vectors-083-p37.xml").write_text("<databaseChangeLog/>")
    assert len(_guard_breaches(some, some, tmp_path)) == 2
    assert len(_guard_breaches(some, set(), tmp_path)) == 1
    assert len(_guard_breaches(set(), some, tmp_path)) == 1
    assert _guard_breaches(set(), set(), tmp_path) == []


def test_the_violating_reader_finds_a_planted_row(tmp_path):
    f = tmp_path / "d.tsv"
    f.write_text("kind\tobject\tname_column\tdisposition\treason\n"
                 f"java\ta chunks where\t{_KEY}\tlabel-kept\tVIOLATING: label match after the latch\n"
                 f"java\tb chunks where\t{_KEY}\tlabel-kept\tR1 reaper ruling (nexus-p2m1x.35)\n")
    assert _violating((f,)) == {("java", "a chunks where", _KEY)}
