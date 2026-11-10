# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor`` never runs a ladder rung's O(store) scan to decide whether it
is pending (nexus-mjmjl; marginalia's py-spy put ~210 s of a 207-239 s doctor
in ManifestSpanFillRung._scan against a LOCAL engine, which nexus-mq980's
managed-only guard let through). A rung that cannot answer cheaply (from its
completion record or a bounded engine count) is reported NOT CHECKED, and the
row names the explicit verb that runs the full check."""

from __future__ import annotations

import pytest

from nexus import health
from nexus.upgrade_ladder.registry import LadderRegistry
from nexus.upgrade_ladder.rungs.manifest_section_labels import ManifestSectionLabelsRung
from nexus.upgrade_ladder.rungs.manifest_span_fill import ManifestSpanFillRung


class _Reader:
    """A catalog reader that answers the one bounded engine count (here: cannot
    say) and fails the test on anything else, which is a walk of the store."""

    def __init__(self, owner: _NoReads) -> None:
        self._owner = owner

    def manifest_section_label_fillable(self, _limit):
        return None

    def close(self) -> None:
        pass

    def __getattr__(self, name):
        self._owner.walks.append(name)
        raise AssertionError(f"nx doctor walked the catalog ({name}) to decide whether a rung is pending")


class _NoReads:
    def __init__(self) -> None:
        self.walks: list[str] = []

    def __call__(self):
        return _Reader(self)


def _rows(monkeypatch, rung) -> list:
    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda: LadderRegistry((rung,)))
    return health._check_pending_rungs()


@pytest.mark.parametrize("make", [
    lambda reader: ManifestSpanFillRung(
        reader_factory=reader, writer_factory=lambda: None, t3_factory=lambda: None,
        record_fn=lambda: None, managed_fn=lambda: False,
    ),
    lambda reader: ManifestSectionLabelsRung(
        reader_factory=reader, writer_factory=lambda: None, t3_factory=lambda: None,
        record_fn=lambda: None, managed_fn=lambda: False, stale_zero_fn=lambda: False,
    ),
], ids=["manifest-span-fill", "manifest-section-labels"])
def test_a_local_doctor_does_not_scan_the_store_and_names_the_full_check(monkeypatch, make) -> None:
    reader = _NoReads()
    rows = _rows(monkeypatch, make(reader))
    assert reader.walks == []
    (row,) = rows
    assert row.ok is False and row.warn is True
    text = f"{row.detail} {' '.join(row.fix_suggestions or [])}"
    assert "not checked" in text.lower()
    assert "nx upgrade --dry-run" in text
