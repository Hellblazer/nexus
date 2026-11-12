# SPDX-License-Identifier: AGPL-3.0-or-later
"""The allocator-drift oracle counts tombstoned children, as the engine's rule does (nexus-f3yqd.6).

``catalog_documents``' unique key is ``(tenant_id, tumbler)`` and covers soft-deleted rows, so a tombstoned
document's tumbler is still taken, and the engine's own ``highestChildSeq`` counts it. A rule that read the live
documents only (``/list`` filters ``deleted_at IS NULL``) would call an owner whose highest child had been deleted clean.
``next_seq_drift_rows_over`` is the Python form of the rule the engine's ``GET /v1/catalog/owners/next_seq_drift``
computes; the shared table ``next-seq-drift-cases.json`` pins the same cases against the SQL.
"""
from __future__ import annotations

from types import SimpleNamespace

import nexus.health as h


def _row(owners: list[dict], live: list[str], trash: list[str]) -> h.HealthResult:
    return h._next_seq_drift_rows_over(owners, [SimpleNamespace(tumbler=t) for t in live], trash)[0]


def test_a_tombstoned_high_child_is_drift() -> None:
    r = _row([{"tumbler_prefix": "1.12", "next_seq": 7}], ["1.12.1", "1.12.7"], ["1.12.9"])
    assert r.ok is False and r.warn is True, r.detail
    assert "1.12" in r.detail and "highest child=9" in r.detail


def test_the_highest_of_many_tombstones_counts() -> None:
    r = _row([{"tumbler_prefix": "1.12", "next_seq": 7}], ["1.12.7"], ["1.12.2", "1.12.3", "1.12.4", "1.12.5", "1.12.11"])
    assert "highest child=11" in r.detail, r.detail


def test_no_tombstones_is_the_live_rule() -> None:
    r = _row([{"tumbler_prefix": "1.12", "next_seq": 7}], ["1.12.7"], [])
    assert r.ok is True and r.detail == "none (1 owner(s) checked)", r.detail


def test_a_clean_owner_with_a_lower_tombstone_stays_clean() -> None:
    r = _row([{"tumbler_prefix": "1.12", "next_seq": 7}], ["1.12.7"], ["1.12.3"])
    assert r.ok is True and r.detail == "none (1 owner(s) checked)", r.detail


def test_a_lower_tombstone_never_lowers_the_high_water() -> None:
    r = _row([{"tumbler_prefix": "1.12", "next_seq": 5}], ["1.12.9"], ["1.12.3"])
    assert r.ok is False and "highest child=9" in r.detail, r.detail


def test_odd_trash_entries_are_ignored() -> None:
    r = _row([{"tumbler_prefix": "1.12", "next_seq": 7}], ["1.12.7"], ["1.12.x", "1.12.8.1", "junk", ""])
    assert r.ok is True and r.detail == "none (1 owner(s) checked)", r.detail


def test_a_tombstone_of_another_owner_does_not_leak() -> None:
    r = _row([{"tumbler_prefix": "1.12", "next_seq": 7}, {"tumbler_prefix": "1.2", "next_seq": 1}], ["1.12.7"], ["1.2.1"])
    assert r.ok is True and r.detail == "none (2 owner(s) checked)", r.detail
