# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-glivh: dry-run link preview + the doctor link-coverage check.

Both exist because index-time link generation is scoped to newly registered
tumblers, so a corpus can sit permanently unlinked with every other catalog
check green. The dry run is what makes a generation pass measurable BEFORE it
writes; the doctor check is what makes the unlinked state detectable without
someone running two ad-hoc commands and happening to notice.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.catalog.link_generator import (
    DryRunLinkWriter,
    load_existing_link_keys,
)
from nexus.commands.catalog_cmds.doctor import (
    MIN_TYPE_DOCS,
    _run_link_coverage,
)


class TestDryRunLinkWriter:
    def test_novel_link_is_recorded_and_reported_as_created(self) -> None:
        w = DryRunLinkWriter()
        assert w.link_if_absent("1.1.1", "1.1.2", "implements", "fp") is True
        assert [(p.from_tumbler, p.to_tumbler) for p in w.proposed] == [("1.1.1", "1.1.2")]

    def test_existing_link_is_not_counted_as_would_create(self) -> None:
        """A count must mean 'would create', never 'would attempt'."""
        w = DryRunLinkWriter(existing={("1.1.1", "1.1.2", "implements")})
        assert w.link_if_absent("1.1.1", "1.1.2", "implements", "fp") is False
        assert w.proposed == []

    def test_same_edge_proposed_twice_counts_once(self) -> None:
        """Projected volume must not be inflated by a generator's own repetition."""
        w = DryRunLinkWriter()
        assert w.link_if_absent("1.1.1", "1.1.2", "implements", "fp") is True
        assert w.link_if_absent("1.1.1", "1.1.2", "implements", "fp") is False
        assert len(w.proposed) == 1

    def test_link_type_participates_in_identity(self) -> None:
        w = DryRunLinkWriter(existing={("1.1.1", "1.1.2", "implements")})
        assert w.link_if_absent("1.1.1", "1.1.2", "cites", "bib") is True

    def test_direct_link_call_fails_loud(self) -> None:
        """A silently-accepted link() would write for real and preview nothing."""
        w = DryRunLinkWriter()
        with pytest.raises(NotImplementedError, match="link_if_absent"):
            w.link("1.1.1", "1.1.2", "implements", "fp")

    def test_breakdowns(self) -> None:
        w = DryRunLinkWriter()
        w.link_if_absent("1.1.1", "1.1.2", "implements", "fp")
        w.link_if_absent("1.1.1", "1.1.3", "implements", "fp")
        w.link_if_absent("1.1.9", "1.1.4", "cites", "bib")
        assert w.count_by_link_type() == {"implements": 2, "cites": 1}
        assert w.count_by_creator() == {"fp": 2, "bib": 1}
        # fan-out is the flood signal: total count hides one document
        # proposing everything.
        assert w.fan_out() == {"1.1.1": 2, "1.1.9": 1}

    def test_close_is_a_noop(self) -> None:
        DryRunLinkWriter().close()


class _FakeLinkReader:
    """Pages link_query the way HttpCatalogClient does."""

    def __init__(self, links: list[tuple[str, str, str]]) -> None:
        self._links = links
        self.calls = 0

    def link_query(self, *, limit: int = 200, offset: int = 0, **_kw: object) -> list[object]:
        self.calls += 1
        window = self._links[offset:offset + limit]
        return [
            SimpleNamespace(from_tumbler=f, to_tumbler=t, link_type=lt)
            for f, t, lt in window
        ]


class TestLoadExistingLinkKeys:
    def test_pages_until_short_read(self) -> None:
        links = [(f"1.1.{i}", "1.1.0", "implements") for i in range(450)]
        reader = _FakeLinkReader(links)
        keys = load_existing_link_keys(reader, page=200)
        assert len(keys) == 450
        assert reader.calls == 3  # 200, 200, 50

    def test_empty_catalog_yields_empty_set(self) -> None:
        assert load_existing_link_keys(_FakeLinkReader([]), page=200) == set()


def _patch_catalog(monkeypatch, rows, *, raises: Exception | None = None) -> None:
    from nexus.commands import catalog as _cat_cmd

    def _get_catalog():
        if raises is not None:
            raise raises
        return SimpleNamespace(coverage_by_content_type=lambda _prefix: rows)

    monkeypatch.setattr(_cat_cmd, "_get_catalog", _get_catalog)


class TestDoctorLinkCoverage:
    def test_zero_links_with_enough_docs_fails(self, monkeypatch) -> None:
        """The unambiguous signal: a generator has never run for this type."""
        _patch_catalog(monkeypatch, [{"content_type": "code", "total": 15424, "linked": 0}])
        report = _run_link_coverage()
        assert report["pass"] is False
        assert report["failures"][0]["content_type"] == "code"

    def test_zero_links_below_min_docs_is_not_a_failure(self, monkeypatch) -> None:
        """A 2-document type at 0% is noise, not evidence."""
        _patch_catalog(
            monkeypatch,
            [{"content_type": "markdown", "total": MIN_TYPE_DOCS - 1, "linked": 0}],
        )
        report = _run_link_coverage()
        assert report["pass"] is True
        assert report["failures"] == []

    def test_low_but_nonzero_coverage_is_not_flagged(self, monkeypatch) -> None:
        """8.3% is the natural state, not a defect.

        A measured dry run of every generator over this corpus yields 133
        links total, so nothing available moves this number. Flagging it
        would be a permanent warning with no remedy.
        """
        _patch_catalog(monkeypatch, [{"content_type": "code", "total": 15424, "linked": 1275}])
        report = _run_link_coverage()
        assert report["pass"] is True
        assert report["warnings"] == []
        assert report["failures"] == []

    def test_healthy_type_neither_fails_nor_warns(self, monkeypatch) -> None:
        _patch_catalog(monkeypatch, [{"content_type": "paper", "total": 107, "linked": 68}])
        report = _run_link_coverage()
        assert report["pass"] is True
        assert report["warnings"] == [] and report["failures"] == []

    def test_measured_2026_08_23_baseline_passes_with_no_warnings(self, monkeypatch) -> None:
        """Pins the real corpus against the no-threshold decision.

        Every one of these types is low, and none is a defect: the measured
        full generator pass over this corpus yields 133 links. If someone
        re-adds a percentage threshold, this fails and sends them to the
        measurement first.
        """
        _patch_catalog(monkeypatch, [
            {"content_type": "code", "total": 15424, "linked": 1275},
            {"content_type": "rdr", "total": 278, "linked": 62},
            {"content_type": "prose", "total": 3085, "linked": 409},
            {"content_type": "knowledge", "total": 421, "linked": 41},
            {"content_type": "paper", "total": 107, "linked": 68},
        ])
        report = _run_link_coverage()
        assert report["pass"] is True
        assert report["warnings"] == []
        assert report["failures"] == []
        assert report["totals"]["documents"] == 19315
        assert report["totals"]["pct"] == 9.6

    def test_empty_catalog_is_a_failure_not_a_pass(self, monkeypatch) -> None:
        """A sweep that found nothing to check is a failure (nexus-moht0)."""
        _patch_catalog(monkeypatch, [])
        report = _run_link_coverage()
        assert report["pass"] is False
        assert "nothing was checked" in report["error"]

    def test_unreadable_catalog_is_unknown_not_a_pass(self, monkeypatch) -> None:
        _patch_catalog(monkeypatch, [], raises=RuntimeError("engine unreachable"))
        report = _run_link_coverage()
        assert report["pass"] is False
        assert "engine unreachable" in report["error"]
