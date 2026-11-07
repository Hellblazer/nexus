# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Synthetic coverage of the deferral parser in scripts/plugin_ledger.py (nexus-rbmja.10).

The parser used to be driven by the retired anchored-tag cut script's own
tests. The drift-ledger test reads the real tree, which says nothing about the
parser's edge cases, so these tests feed it synthetic ledger text. The RDR-228
Phase 2 guard and its fixture-repo tests were pruned by nexus-rbmja.9.
"""

from __future__ import annotations

from plugin_ledger import _deferred_entries, deferred_paths


class TestDeferralParsing:
    def test_an_entry_under_the_heading_is_deferred(self) -> None:
        text = (
            "# Pending\n"
            "- `conexus/hooks/live.py`: not deferred (nexus-aaaaa)\n"
            "\n"
            "## Deferred to the next client release\n"
            "\n"
            "- `conexus/hooks/held.py`: deferred (nexus-bbbbb)\n"
        )
        deferred = _deferred_entries(text)
        assert len(deferred) == 1
        assert "held.py" in deferred[0]
        assert "live.py" not in "".join(deferred)

    def test_a_heading_ends_the_section(self) -> None:
        """Membership is by the LAST '## ' heading seen, not everything after
        the first Deferred heading."""
        text = (
            "# Pending\n"
            "## Deferred to the next client release\n"
            "- `conexus/hooks/held.py`: deferred (nexus-bbbbb)\n"
            "## Some other section\n"
            "- `conexus/hooks/other.py`: not deferred (nexus-ccccc)\n"
        )
        deferred = _deferred_entries(text)
        assert len(deferred) == 1
        assert "held.py" in deferred[0]

    def test_a_ledger_with_no_deferred_heading_parses_as_none(self) -> None:
        text = (
            "# Pending\n"
            "- Every file that differs MUST be declared here.\n"
            "- `conexus/hooks/h.py`: real entry (nexus-aaaaa)\n"
        )
        assert _deferred_entries(text) == []
        assert deferred_paths(text) == set()

    def test_deferred_paths_excludes_wheel_and_bare_prefix_spans(self) -> None:
        """Only allowlisted, path-shaped spans count: the wheel half of a
        split entry was never going to ship, and a bare prefix root
        (`conexus/`) is prose, not a path."""
        text = (
            "# Pending\n"
            "## Deferred to the next client release\n"
            "- `conexus/agents/dev.md` (see `conexus/`): split delivery, "
            "wheel half `src/nexus/plans/x.py` (nexus-fffff)\n"
        )
        assert deferred_paths(text) == {"conexus/agents/dev.md"}

    def test_a_wheel_span_alone_yields_no_path(self) -> None:
        text = (
            "## Deferred to the next client release\n"
            "- wheel half `src/nexus/plans/x.py` (nexus-fffff)\n"
        )
        assert deferred_paths(text) == set()

    def test_a_bare_prefix_span_alone_yields_no_path(self) -> None:
        text = (
            "## Deferred to the next client release\n"
            "- see `conexus/hooks/` and `conexus/` (nexus-fffff)\n"
        )
        assert "conexus/" not in deferred_paths(text)
        assert "conexus" not in deferred_paths(text)
