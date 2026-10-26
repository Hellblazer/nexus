"""RDR-077 hub staleness sense (Sam's ruling 2026-10-26, nexus-9kp10).

A hub is stale when a contributing collection was discovered after the hub's
latest projection, or one was never discovered. The client derives this from
the /hubs fields so it holds on any engine; engines up to v0.1.164 send an
is_stale computed in the reverse sense.
"""
from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner

import nexus.commands.taxonomy_cmd as tc
from nexus.cli import main
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
from nexus.db.t2.taxonomy_compute import HubRow, hub_is_stale


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


# ── The store derives the verdict; it does not pass the engine's through ────



def _hub_payload(*, is_stale: bool, assigned: str, discover: str) -> dict:
    return {
        "topic_id": 7, "label": "hub", "collection": "code__x", "df": 2,
        "total_chunks": 4, "source_collections": ["code__a", "code__b"],
        "last_assigned_at": assigned, "max_last_discover_at": discover,
        "never_discovered_count": 0, "is_stale": is_stale,
    }


def _store_answering(monkeypatch, row: dict) -> HttpTaxonomyStore:
    store = HttpTaxonomyStore.__new__(HttpTaxonomyStore)
    monkeypatch.setattr(store, "_get", lambda path, params=None: [row], raising=False)
    monkeypatch.setattr(store, "compute_icf_map", lambda: {}, raising=False)
    return store


def test_store_overrides_an_old_sense_engine_that_calls_a_fresh_hub_stale(
    monkeypatch,
) -> None:
    # Engine <= v0.1.164: projection after the discover reads is_stale=True.
    store = _store_answering(monkeypatch, _hub_payload(
        is_stale=True, assigned="2026-04-10T13:04:00Z", discover="2026-04-09T00:00:00Z"))
    [hub] = store.detect_hubs(min_collections=2, warn_stale=True)
    assert hub.is_stale is False


def test_store_overrides_an_old_sense_engine_that_calls_a_stale_hub_fresh(
    monkeypatch,
) -> None:
    store = _store_answering(monkeypatch, _hub_payload(
        is_stale=False, assigned="2026-04-10T13:04:00Z", discover="2026-04-11T00:00:00Z"))
    [hub] = store.detect_hubs(min_collections=2, warn_stale=True)
    assert hub.is_stale is True


def test_stale_line_names_the_later_discover(monkeypatch) -> None:
    row = HubRow(
        topic_id=7, label="hub", collection="code__x",
        distinct_source_collections=2, total_chunks=4, icf=1.0, score=0.0,
        matched_stopwords=(), source_collections=("code__a", "code__b"),
        last_assigned_at="2026-04-10T13:04:00Z",
        max_last_discover_at="2026-04-11T00:00:00Z",
        never_discovered_count=0, is_stale=True,
    )
    db = SimpleNamespace(
        taxonomy=SimpleNamespace(detect_hubs=lambda **k: [row]), close=lambda: None)
    monkeypatch.setattr(tc, "_T2Database", lambda *a, **k: db)
    monkeypatch.setattr(tc, "_command_shared_t2_client", lambda: None)
    result = CliRunner().invoke(main, ["taxonomy", "hubs", "--warn-stale"])
    assert result.exit_code == 0, result.output
    assert (
        "STALE: max_last_discover_at=2026-04-11T00:00:00Z > "
        "last_assigned_at=2026-04-10T13:04:00Z"
    ) in result.output
