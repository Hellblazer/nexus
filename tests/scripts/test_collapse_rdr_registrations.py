# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for scripts/collapse_rdr_registrations.py (RDR-201 Phase 3.1,
bead nexus-j9z30.20).

``build_plan``/``format_plan``/``apply_plan`` operate on plain
``CatalogEntry`` lists and a structural ``CatalogReader``/``CatalogWriter``
stub — no engine substrate or HTTP wire needed, matching
``rdr_canonical.py``'s own pure-logic test file.
"""
from __future__ import annotations

import inspect

from collapse_rdr_registrations import (
    RdrPlanRow,
    apply_plan,
    build_plan,
    format_plan,
)

from nexus.catalog.rdr_canonical import group_rdr_candidates
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry

CURRENT_OWNER = Tumbler.parse("1.1")
THIS_REPO_PREFIX = "file:///repo/nexus/docs/rdr/"
FOREIGN_REPO_PREFIX = "file:///repo/ART/docs/rdr/"  # live-measured shape: ART shares this catalog


def _entry(
    tumbler: str, *, content_type: str = "rdr", file_path: str, source_uri: str = "",
) -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler),
        title="",
        author="",
        year=0,
        content_type=content_type,
        file_path=file_path,
        corpus="nexus",
        physical_collection="",
        chunk_count=1,
        head_hash="",
        indexed_at="2026-09-01T00:00:00Z",
        source_uri=source_uri,
    )


class _StubWriter:
    """Records update() calls; the only op apply_plan issues."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def update(self, **fields) -> None:
        self.calls.append(fields)


def _fixture_entries() -> list[CatalogEntry]:
    return [
        # rdr-201: duplicate under two owners, same file (same source_uri) --
        # the current-owner copy wins.
        _entry("1.10.1", content_type="rdr", file_path="docs/rdr/rdr-201-x.md", source_uri=THIS_REPO_PREFIX + "rdr-201-x.md"),
        _entry("1.1.1", content_type="rdr", file_path="docs/rdr/rdr-201-x.md", source_uri=THIS_REPO_PREFIX + "rdr-201-x.md"),
        # rdr-052: legacy prose beside the current rdr registration -- rdr wins
        # (admitted via source_uri even though it is not the current owner).
        _entry("1.10.5", content_type="prose", file_path="docs/rdr/rdr-052-y.md", source_uri=THIS_REPO_PREFIX + "rdr-052-y.md"),
        _entry("1.20.5", content_type="rdr", file_path="docs/rdr/rdr-052-y.md", source_uri=THIS_REPO_PREFIX + "rdr-052-y.md"),
        # rdr-110: unresolvable -- two rdr registrations, same file, neither
        # under CURRENT_OWNER -- genuine Finding-4 ambiguity even after scoping.
        _entry("1.10.9", content_type="rdr", file_path="docs/rdr/rdr-110-z.md", source_uri=THIS_REPO_PREFIX + "rdr-110-z.md"),
        _entry("1.20.9", content_type="rdr", file_path="docs/rdr/rdr-110-z.md", source_uri=THIS_REPO_PREFIX + "rdr-110-z.md"),
        # rdr-078: single registration, this repo's own file, nothing to collapse.
        _entry("1.10.3", content_type="prose", file_path="docs/rdr/rdr-078-w.md", source_uri=THIS_REPO_PREFIX + "rdr-078-w.md"),
        # rdr-099: a DIFFERENT repo's RDR sharing this basename (live shape:
        # ART registered content_type="rdr" under a different owner in the
        # SAME catalog) -- must be scoped out entirely, never even appear
        # as a candidate in this repo's plan.
        _entry("1.2.400", content_type="rdr", file_path="docs/rdr/rdr-099-foreign.md", source_uri=FOREIGN_REPO_PREFIX + "rdr-099-foreign.md"),
    ]


class TestBuildPlan:
    def test_plan_rows_sorted_and_resolved_per_finding_4_shapes(self) -> None:
        rows = build_plan(_fixture_entries(), CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        by_key = {r.rdr_key: r for r in rows}
        assert [r.rdr_key for r in rows] == sorted(by_key)

        assert by_key["rdr-201-x"].canonical == Tumbler.parse("1.1.1")
        assert by_key["rdr-201-x"].losers == [Tumbler.parse("1.10.1")]

        assert by_key["rdr-052-y"].canonical == Tumbler.parse("1.20.5")
        assert by_key["rdr-052-y"].losers == [Tumbler.parse("1.10.5")]

        assert by_key["rdr-110-z"].canonical is None
        assert by_key["rdr-110-z"].losers == []  # unresolvable -> nothing collapsed

        assert by_key["rdr-078-w"].canonical == Tumbler.parse("1.10.3")
        assert by_key["rdr-078-w"].losers == []  # single candidate, nothing to collapse

    def test_foreign_repo_entry_is_reported_unresolvable_not_dropped(self) -> None:
        """A foreign repo's same-basename document must never be accepted as
        canonical. It is REFUSED and the refusal is reported: round 2 replaced
        the pre-grouping drop with one admission gate, because a dropped
        record leaves the census silently short (T2
        nexus/critique-nexus-j9z30-20-round2-2026-09-01)."""
        rows = build_plan(_fixture_entries(), CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        by_key = {r.rdr_key: r for r in rows}
        assert by_key["rdr-099-foreign"].canonical is None

    def test_a_lone_foreign_entry_is_unresolvable_not_accepted(self) -> None:
        """A single foreign candidate is UNRESOLVABLE, never silently kept —
        the singleton path runs the same admission check as any other."""
        foreign_only = [
            _entry("1.2.400", content_type="rdr", file_path="docs/rdr/rdr-099-foreign.md", source_uri=FOREIGN_REPO_PREFIX + "rdr-099-foreign.md"),
        ]
        rows = build_plan(foreign_only, CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        assert len(rows) == 1
        assert rows[0].canonical is None  # refused, and the refusal is visible


class TestFormatPlan:
    def test_dry_run_report_names_keep_and_collapse_targets(self) -> None:
        rows = build_plan(_fixture_entries(), CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        report = format_plan(rows, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        assert "5 RDR(s) fetched: 3 resolved, 1 unresolvable, 1 other repos'" in report
        assert "rdr-201-x: canonical=1.1.1" in report
        assert "KEEP        1.1.1" in report
        assert "collapse -> 1.10.1  content_type=rdr 1.1.1" in report
        assert "rdr-110-z: UNRESOLVABLE" in report
        # The foreign row is CLASSIFIED and counted in the header, not listed
        # among this repo's findings and not dropped (round-2 critique).
        assert "other repos (refused, not this repo's to collapse): ART=1" in report

    def test_format_plan_writes_nothing(self) -> None:
        """--dry-run (the default codepath) never touches a writer at all --
        format_plan takes no writer argument, so there is nothing it *could*
        write; this test pins that contract so a future edit cannot
        accidentally thread a writer through the dry-run path."""
        assert "writer" not in inspect.signature(format_plan).parameters


class TestApplyPlan:
    def test_apply_sets_alias_of_on_losers_only(self) -> None:
        rows = build_plan(_fixture_entries(), CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        writer = _StubWriter()
        n = apply_plan(writer, rows)
        assert n == 2
        assert {"tumbler": "1.10.1", "alias_of": "1.1.1"} in writer.calls
        assert {"tumbler": "1.10.5", "alias_of": "1.20.5"} in writer.calls

    def test_apply_skips_unresolvable_rows(self) -> None:
        row = RdrPlanRow(rdr_key="rdr-110-z", candidates=[], canonical=None)
        writer = _StubWriter()
        assert apply_plan(writer, [row]) == 0
        assert writer.calls == []


class TestNothingVanishesFromThePlan:
    """Round-2 critique (T2 nexus/critique-nexus-j9z30-20-round2-2026-09-01):
    build_plan pre-filtered on source_uri prefix alone, stricter than the
    rule's own owner-OR-path admission, so a current-owner record with an
    empty source_uri was dropped before grouping — neither resolved nor
    unresolvable, absent. Live instance: tumbler 1.1.2770,
    docs/rdr/rdr-174-unified-nx-init-service-lifecycle.md. RDR-201 §Failure
    Modes forbids the silent disappearance; one admission gate now."""

    def test_current_owner_entry_with_empty_source_uri_reaches_the_plan(self) -> None:
        entry = _entry(
            "1.1.2770",
            file_path="docs/rdr/rdr-174-unified-nx-init-service-lifecycle.md",
            source_uri="",
        )
        rows = build_plan([entry], CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        assert [r.rdr_key for r in rows] == ["rdr-174-unified-nx-init-service-lifecycle"]
        assert rows[0].canonical == entry.tumbler, "admitted via the owner branch, not dropped"

    def test_unattributable_row_stays_in_this_repos_unresolvable_list(self) -> None:
        """A candidate with no source_uri under a foreign owner belongs to
        nobody: it cannot be filed under another repo, so it stays where
        someone has to look at it. Live instance: rdr-125, owner 1.23,
        content_type=prose, empty source_uri."""
        entry = _entry("1.23.2", content_type="prose", file_path="docs/rdr/rdr-125.md")
        rows = build_plan([entry], CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        assert rows[0].canonical is None
        assert not rows[0].is_foreign(CURRENT_OWNER, THIS_REPO_PREFIX)
        report = format_plan(
            rows, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
        )
        assert "1 unresolvable" in report
        assert "0 other repos'" in report

    def test_owner_admitted_row_is_not_filed_under_another_repo(self) -> None:
        """Round-3 critique: is_foreign tested the path alone while the
        admission gate tests owner-OR-path, so a record admitted via the
        OWNER branch (current owner, source_uri outside this prefix) was
        resolved correctly and then displayed under another repo. The
        report and the rule must agree."""
        entry = _entry(
            "1.1.2771",
            file_path="docs/rdr/rdr-175-os-init-single-process-watchdog.md",
            source_uri="chroma://collection/doc-id",
        )
        rows = build_plan([entry], CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        assert rows[0].canonical == entry.tumbler
        assert not rows[0].is_foreign(CURRENT_OWNER, THIS_REPO_PREFIX)
        report = format_plan(
            rows, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
        )
        assert "1 resolved" in report
        assert "0 other repos'" in report
        assert "rdr-175-os-init-single-process-watchdog: canonical=1.1.2771" in report

    def test_current_owner_entry_with_non_file_scheme_reaches_the_plan(self) -> None:
        entry = _entry(
            "1.1.2771",
            file_path="docs/rdr/rdr-175-os-init-single-process-watchdog.md",
            source_uri="chroma://collection/doc-id",
        )
        rows = build_plan([entry], CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        assert [r.rdr_key for r in rows] == ["rdr-175-os-init-single-process-watchdog"]
        assert rows[0].canonical == entry.tumbler

    def test_foreign_repo_entry_is_reported_unresolvable_never_absent(self) -> None:
        """The ART shape: a same-shaped record under a different owner and a
        different source_uri root. It must be REFUSED, and refusal is a row
        in the plan — the census must account for it."""
        entry = _entry(
            "1.2.900",
            file_path="docs/rdr/rdr-201-closed-vocabularies-as-checked-tables.md",
            source_uri=FOREIGN_REPO_PREFIX + "rdr-201-closed-vocabularies-as-checked-tables.md",
        )
        rows = build_plan([entry], CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        assert len(rows) == 1
        assert rows[0].canonical is None

    def test_census_invariant_every_fetched_rdr_is_resolved_or_unresolvable(self) -> None:
        """The standing guard: no entry the fetch returns may vanish. Counts
        must reconcile, so a future pre-filter cannot silently reappear."""
        entries = _fixture_entries() + [
            _entry("1.1.2770", file_path="docs/rdr/rdr-174-x.md", source_uri=""),
            _entry(
                "1.2.900",
                file_path="docs/rdr/rdr-199-y.md",
                source_uri=FOREIGN_REPO_PREFIX + "rdr-199-y.md",
            ),
        ]
        rows = build_plan(entries, CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        expected_keys = set(group_rdr_candidates(entries))
        assert {r.rdr_key for r in rows} == expected_keys
        assert expected_keys, "vacuous: no RDR keys in the fixture set"
        # LITERAL expected counts for this known input, not a partition of
        # rows by the very functions under test — that shape sums to the
        # whole for any classifier, correct or broken (round-4 critique, T2
        # nexus/critique-nexus-j9z30-20-round4-2026-09-01, which caught the
        # previous version of this assertion claiming to be more than it was).
        # The fixture set: 3 resolvable this-repo RDRs, 1 unresolvable
        # this-repo RDR (rdr-110-z), 1 foreign (rdr-099-foreign), plus the
        # two appended above — rdr-174-x (current owner, empty source_uri,
        # resolvable) and rdr-199-y (foreign).
        report = format_plan(
            rows, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
        )
        assert f"{len(rows)} RDR(s) fetched: 4 resolved, 1 unresolvable, 2 other repos'" in report
