"""RDR-077 hub staleness sense (Sam's ruling 2026-10-26, nexus-9kp10).

A hub is stale when a contributing collection was discovered after the hub's
latest projection, or one was never discovered. The client derives this from
the /hubs fields so it holds on any engine; engines up to v0.1.164 send an
is_stale computed in the reverse sense.
"""
from __future__ import annotations

from nexus.db.t2.taxonomy_compute import hub_is_stale


def test_discover_after_projection_is_stale() -> None:
    assert hub_is_stale("2026-04-10T13:04:00Z", "2026-04-11T00:00:00Z", 0) is True


def test_projection_after_discover_is_fresh() -> None:
    assert hub_is_stale("2026-04-10T13:04:00Z", "2026-04-09T00:00:00Z", 0) is False


def test_equal_timestamps_are_fresh() -> None:
    assert hub_is_stale("2026-04-10T13:04:00Z", "2026-04-10T13:04:00Z", 0) is False


def test_never_discovered_source_is_stale_whatever_the_timestamps() -> None:
    assert hub_is_stale("2026-04-10T13:04:00Z", "2026-04-09T00:00:00Z", 1) is True
    assert hub_is_stale(None, None, 1) is True


def test_missing_timestamp_is_not_stale_on_the_timestamp_test() -> None:
    assert hub_is_stale(None, "2026-04-11T00:00:00Z", 0) is False
    assert hub_is_stale("2026-04-10T13:04:00Z", None, 0) is False


def test_timestamps_compare_as_instants_not_strings() -> None:
    # Lexicographically "...T00:00Z" > "...T00:00:00Z" ('Z' > ':'), so a string
    # comparison calls this discover later; as instants they are equal.
    assert hub_is_stale("2026-04-11T00:00:00Z", "2026-04-11T00:00Z", 0) is False
    # An offset rendering of an earlier instant must not read as later.
    assert hub_is_stale("2026-04-11T00:00:00Z", "2026-04-11T01:00:00+02:00", 0) is False
