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

from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry

CURRENT_OWNER = Tumbler.parse("1.1")


def _entry(tumbler: str, *, content_type: str = "rdr", file_path: str) -> CatalogEntry:
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
    )


class _StubWriter:
    """Records update() calls; the only op apply_plan issues."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def update(self, **fields) -> None:
        self.calls.append(fields)


def _fixture_entries() -> list[CatalogEntry]:
    return [
        # rdr-201: duplicate under two owners, both content_type=rdr.
        _entry("1.10.1", content_type="rdr", file_path="docs/rdr/rdr-201-x.md"),
        _entry("1.1.1", content_type="rdr", file_path="docs/rdr/rdr-201-x.md"),
        # rdr-052: legacy prose beside the current rdr registration.
        _entry("1.10.5", content_type="prose", file_path="docs/rdr/rdr-052-y.md"),
        _entry("1.20.5", content_type="rdr", file_path="docs/rdr/rdr-052-y.md"),
        # rdr-110: unresolvable -- two rdr registrations, neither under CURRENT_OWNER.
        _entry("1.10.9", content_type="rdr", file_path="docs/rdr/rdr-110-z.md"),
        _entry("1.20.9", content_type="rdr", file_path="docs/rdr/rdr-110-z.md"),
        # rdr-078: single registration, nothing to collapse.
        _entry("1.10.3", content_type="prose", file_path="docs/rdr/rdr-078-w.md"),
    ]


class TestBuildPlan:
    def test_plan_rows_sorted_and_resolved_per_finding_4_shapes(self) -> None:
        rows = build_plan(_fixture_entries(), CURRENT_OWNER)
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


class TestFormatPlan:
    def test_dry_run_report_names_keep_and_collapse_targets(self) -> None:
        rows = build_plan(_fixture_entries(), CURRENT_OWNER)
        report = format_plan(rows, current_owner=CURRENT_OWNER)
        assert "4 RDR(s) found: 3 resolved, 1 unresolvable" in report
        assert "rdr-201-x: canonical=1.1.1" in report
        assert "KEEP        1.1.1" in report
        assert "collapse -> 1.10.1  content_type=rdr 1.1.1" in report
        assert "rdr-110-z: UNRESOLVABLE" in report

    def test_format_plan_writes_nothing(self) -> None:
        """--dry-run (the default codepath) never touches a writer at all --
        format_plan takes no writer argument, so there is nothing it *could*
        write; this test pins that contract so a future edit cannot
        accidentally thread a writer through the dry-run path."""
        assert "writer" not in inspect.signature(format_plan).parameters


class TestApplyPlan:
    def test_apply_sets_alias_of_on_losers_only(self) -> None:
        rows = build_plan(_fixture_entries(), CURRENT_OWNER)
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
