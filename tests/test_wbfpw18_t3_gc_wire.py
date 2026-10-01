# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-wbfpw.18 (RDR-192 Step 8, client half): ``nx t3 gc`` takes its
candidates from the engine's reapable machinery and MOVES them with the
engine's own statement. It never lists chunks and then deletes them by id.

Wire-level tests: a real ``HttpVectorClient`` over a faked transport
(``_post`` / ``_get``), driven through the real CLI, so a verb that reached for
``/v1/vectors/store-delete`` (the hard delete by id) or for a route the engine
does not carry fails HERE. The engine's own behaviour (the predicate, the
sweep gate, the racing-write recheck) is proven against a real engine in
``tests/test_wbfpw18_t3_gc_substrate.py``.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.http_vector_client import (
    HttpVectorClient,
    VectorServiceError,
    reset_http_vector_client_for_tests,
)

_COLL = "knowledge__nexus-1-1__voyage-context-3__v1"
_QUARANTINE = "quarantine-knowledge__nexus-1-1__voyage-context-3__v1"
_BUCKETS = ("superseded", "legacy-unmanifested", "dead-owner", "no-owner", "unclassified")


def _chash(i: int) -> str:
    return f"{i:064x}"


def _row(i: int) -> dict:
    return {
        "chash": _chash(i), "created_at": "2026-01-01T00:00:00Z",
        "last_written_at": "2026-01-01T00:00:00Z", "title": f"t{i}", "catalog_doc_id": None,
    }


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def real_client():
    reset_http_vector_client_for_tests()
    yield HttpVectorClient()
    reset_http_vector_client_for_tests()


class _Engine:
    """A scripted engine: records every POST and answers the four routes the
    verb may use. Anything else is a test failure, which is the point."""

    def __init__(self, *, total: int, reapable: list[int], legacy: int = 0,
                 census_error: Exception | None = None, page: int | None = None,
                 in_t3: bool = True, no_owner: int = 0, legacy_on_recheck: int | None = None,
                 unclassified: int = 0, unclassified_on_recheck: int | None = None,
                 batches: list[int] | None = None) -> None:
        self.unclassified = unclassified
        self.unclassified_on_recheck = unclassified_on_recheck
        self.batches = list(batches) if batches is not None else None
        self.legacy_on_recheck = legacy_on_recheck
        self.census_calls = 0
        self.no_owner = no_owner
        self.in_t3 = in_t3
        self.total = total
        self.reapable = list(reapable)
        self.legacy = legacy
        self.census_error = census_error
        self.page = page
        self.posted: list[tuple[str, dict]] = []

    def stats(self) -> list[dict]:
        if not self.in_t3:
            return []
        return [{
            "name": _COLL, "count": self.total, "stored_count": self.total,
            "content_type": "knowledge", "owner_id": "nexus-1-1",
            "embedding_model": "voyage-context-3", "lifecycle_state": "live",
        }]

    def post(self, path: str, body: dict, **_kw):
        self.posted.append((path, dict(body)))
        if path == "/v1/vectors/manifest-less-census":
            if self.census_error is not None:
                raise self.census_error
            self.census_calls += 1
            totals = dict.fromkeys(_BUCKETS, 0)
            totals["legacy-unmanifested"] = (
                self.legacy_on_recheck
                if self.census_calls > 1 and self.legacy_on_recheck is not None else self.legacy
            )
            totals["no-owner"] = self.no_owner
            totals["unclassified"] = (
                self.unclassified_on_recheck
                if self.census_calls > 1 and self.unclassified_on_recheck is not None else self.unclassified
            )
            return {"collection": body["collection"], "returned": 0, "chashes": {},
                    "owners": {}, "totals": totals, "scope_chunk_total": self.total}
        if path == "/v1/vectors/reapable":
            assert "offset" not in body, "the listing is paged by keyset, never offset"
            assert "grace_seconds" not in body, "the verb never passes a grace; the engine default stands"
            limit = min(int(body.get("limit", 100)), self.page or 300)
            after = body.get("after_chash")
            rows = [i for i in self.reapable if after is None or _chash(i) > after][:limit]
            return {
                "collection": body["collection"], "grace_seconds": None,
                "returned": len(rows),
                "next_after": _chash(rows[-1]) if len(rows) >= limit else None,
                "chunks": [_row(i) for i in rows],
            }
        if path == "/v1/vectors/gc/quarantine-orphans":
            if self.batches is not None:
                moved = self.batches.pop(0)
                return {"moved": moved, "sample": [], "remaining": sum(self.batches),
                        "row_limit": body.get("row_limit")}
            return {"moved": len(self.reapable), "sample": [], "remaining": 0,
                    "row_limit": body.get("row_limit")}
        raise AssertionError(f"unexpected engine route {path}: nx t3 gc must not use it")

    def paths(self) -> list[str]:
        return [p for p, _ in self.posted]


def _invoke(runner: CliRunner, real_client, engine: _Engine, args: list[str], *, documents=(),
            catalog_knows: bool = True):
    fake_cat = MagicMock()
    fake_cat.get_collection.return_value = object() if catalog_knows else None
    with (
        patch("nexus.db.http_vector_client._post", engine.post),
        patch("nexus.db.http_vector_client._get", lambda path, **kw: engine.stats()),
        patch("nexus.db.make_t3", return_value=real_client),
        patch("nexus.commands.t3._make_catalog", return_value=fake_cat),
        patch("nexus.indexer_utils.catalog_documents_for_collection", return_value=list(documents)),
        patch("nexus.catalog.factory.make_catalog_writer",
              side_effect=AssertionError("nx t3 gc must not write its own gc_audit row")),
    ):
        return runner.invoke(main, ["t3", "gc", "-c", _COLL, *args])


# ── --orphan-window is gone ───────────────────────────────────────────────────


@pytest.mark.parametrize("window", ["1s", "7d", "30d"])
def test_orphan_window_is_refused_naming_its_removal(runner, real_client, window):
    """The engine functions carry no tunable grace, so the flag cannot be honoured; a script that
    still passes it gets a refusal that names the removal, and nothing reaches the engine."""
    engine = _Engine(total=10, reapable=[1])
    result = _invoke(runner, real_client, engine, ["--orphan-window", window, "--dry-run"])
    assert result.exit_code != 0
    assert "--orphan-window" in result.output
    assert "removed" in result.output
    assert engine.posted == []


# ── the move goes through the engine route, never list-then-delete-by-id ──────


def test_act_moves_through_the_engine_route_and_never_deletes_by_id(runner, real_client):
    engine = _Engine(total=10, reapable=[1, 2, 3])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    paths = engine.paths()
    # The census gate comes first, the move last, and no hard delete by id anywhere.
    assert paths[0] == "/v1/vectors/manifest-less-census"
    assert paths[-1] == "/v1/vectors/gc/quarantine-orphans"
    assert "/v1/vectors/store-delete" not in paths
    assert "/v1/vectors/get" not in paths
    move = engine.posted[-1][1]
    assert move["collection"] == _COLL
    assert move["quarantine_collection"] == _QUARANTINE
    # The route is collection-wide: it carries no chash list and no exclusion list.
    assert not ({"chashes", "ids", "exclude", "exclusions"} & set(move))
    # The BOUNDED form (row_limit present): the unbounded form has a 5 s statement timeout. And the
    # audit sample is the engine's own ceiling, so every chunk of a batch is in its gc_audit row and
    # an operator can restore from the row.
    assert move["row_limit"] == 2000
    assert move["sample_limit"] == 5000
    assert "quarantined 3 chunk(s)" in result.output
    assert "restorable" in result.output


def test_dry_run_moves_nothing(runner, real_client):
    engine = _Engine(total=10, reapable=[1, 2])
    result = _invoke(runner, real_client, engine, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()
    assert _chash(1) in result.output and _chash(2) in result.output
    assert "would quarantine up to 2 chunk(s)" in result.output


def test_no_dry_run_without_yes_is_report_only(runner, real_client):
    engine = _Engine(total=10, reapable=[1])
    result = _invoke(runner, real_client, engine, ["--no-dry-run"])
    assert result.exit_code == 0, result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()
    assert "Add --yes" in result.output


def test_nothing_reapable_is_a_clean_noop(runner, real_client):
    engine = _Engine(total=10, reapable=[])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()
    assert "nothing to do" in result.output


# ── the advisory listing pages by keyset ──────────────────────────────────────


def test_listing_pages_by_keyset_never_offset(runner, real_client):
    engine = _Engine(total=10, reapable=[1, 2, 3, 4, 5], page=2)
    result = _invoke(runner, real_client, engine, ["--dry-run"])
    assert result.exit_code == 0, result.output
    listing = [b for p, b in engine.posted if p == "/v1/vectors/reapable"]
    # 5 rows at 2 per page: three requests, each carrying the previous page's cursor.
    assert [b.get("after_chash") for b in listing] == [None, _chash(2), _chash(4)]
    for i in (1, 2, 3, 4, 5):
        assert _chash(i) in result.output


# ── R8: the census gate ───────────────────────────────────────────────────────


def test_a_nonzero_legacy_census_refuses_and_moves_nothing(runner, real_client):
    engine = _Engine(total=10, reapable=[1, 2], legacy=1)
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code != 0
    assert "legacy-unmanifested" in result.output
    assert "REFUSING" in result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()


def test_the_census_is_read_again_immediately_before_the_move(runner, real_client):
    """The gate must be as fresh as it can be: a note that went legacy-unmanifested while the
    listing was paging (a census that read 0 first and 1 just before the act) stops the move."""
    engine = _Engine(total=10, reapable=[1, 2], legacy=0, legacy_on_recheck=1)
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code != 0
    assert "legacy-unmanifested" in result.output and "REFUSING" in result.output
    assert engine.paths().count("/v1/vectors/manifest-less-census") == 2
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()
    # the recheck is the LAST read before the move: nothing but the move follows it
    assert engine.paths()[-1] == "/v1/vectors/manifest-less-census"


def test_the_census_is_read_on_every_run_not_only_when_acting(runner, real_client):
    engine = _Engine(total=10, reapable=[1], legacy=2)
    result = _invoke(runner, real_client, engine, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert "/v1/vectors/manifest-less-census" in engine.paths()
    # A dry run says out loud that a real run would refuse, the way the index-state breaker does.
    assert "legacy-unmanifested" in result.output
    assert "REFUSE" in result.output


def test_an_engine_without_the_routes_refuses_with_a_clear_message(runner, real_client):
    engine = _Engine(total=10, reapable=[1],
                     census_error=VectorServiceError("no route", code=404))
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code != 0
    assert "engine" in result.output and "predates" in result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()


# ── the fraction floor ────────────────────────────────────────────────────────


def test_a_pass_over_the_floor_is_refused(runner, real_client, monkeypatch):
    monkeypatch.delenv("NX_GC_FLOOR_FRACTION", raising=False)
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    engine = _Engine(total=100, reapable=list(range(1, 31)))  # 30% > the 25% default
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code != 0
    assert "NX_GC_FLOOR_FRACTION" in result.output and "NX_GC_FORCE=1" in result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()


def test_nx_gc_force_overrides_the_floor(runner, real_client, monkeypatch):
    monkeypatch.delenv("NX_GC_FLOOR_FRACTION", raising=False)
    monkeypatch.setenv("NX_GC_FORCE", "1")
    engine = _Engine(total=100, reapable=list(range(1, 31)))
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert engine.paths()[-1] == "/v1/vectors/gc/quarantine-orphans"


def test_the_floor_does_not_apply_below_the_minimum_collection_size(runner, real_client, monkeypatch):
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    engine = _Engine(total=99, reapable=list(range(1, 100)))  # 100% of a 99-chunk collection
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert engine.paths()[-1] == "/v1/vectors/gc/quarantine-orphans"


def test_a_dry_run_over_the_floor_says_a_real_run_would_refuse(runner, real_client, monkeypatch):
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    engine = _Engine(total=100, reapable=list(range(1, 31)))
    result = _invoke(runner, real_client, engine, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert "REFUSE" in result.output and "NX_GC_FORCE=1" in result.output


# ── the RUNFENCE circuit breaker is unchanged ─────────────────────────────────


def _indexing_doc():
    return SimpleNamespace(title="in-flight", index_state="indexing", file_path="/x.md", meta={})


def test_a_non_complete_document_still_refuses(runner, real_client):
    engine = _Engine(total=10, reapable=[1])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"], documents=[_indexing_doc()])
    assert result.exit_code != 0
    assert "not index_state='complete'" in result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()


def test_the_incomplete_state_override_lets_the_move_run(runner, real_client):
    engine = _Engine(total=10, reapable=[1])
    result = _invoke(runner, real_client, engine,
                     ["--no-dry-run", "--yes", "--allow-incomplete-index-state"],
                     documents=[_indexing_doc()])
    assert result.exit_code == 0, result.output
    assert engine.paths()[-1] == "/v1/vectors/gc/quarantine-orphans"


# ── the collection-name guards are unchanged ──────────────────────────────────


def test_a_name_no_collection_carries_is_refused_before_anything_else(runner, real_client):
    """nexus-sis0m.3: a typo is not a collection; the engine's "register it first" advice is wrong for it."""
    engine = _Engine(total=0, reapable=[], in_t3=False)
    result = _invoke(runner, real_client, engine, ["--dry-run"], catalog_knows=False)
    assert result.exit_code == 1, result.output
    assert f"no collection named {_COLL!r}" in result.output
    assert "register it first" not in result.output
    assert engine.posted == []


def test_the_act_refuses_a_collection_the_catalog_has_never_heard_of(runner, real_client):
    """nexus-v1zdu: the catalog read cannot tell "known, zero documents" from "unknown", so an
    unregistered name reads as having nothing to protect. Refused at act time, nothing moves."""
    engine = _Engine(total=10, reapable=[1])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"], catalog_knows=False)
    assert result.exit_code != 0, result.output
    assert _COLL in result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()


def test_the_override_on_an_unknown_collection_says_so_out_loud(runner, real_client):
    engine = _Engine(total=10, reapable=[1])
    result = _invoke(runner, real_client, engine,
                     ["--no-dry-run", "--yes", "--allow-empty-manifest-set"], catalog_knows=False)
    assert result.exit_code == 0, result.output
    assert "WARNING: the catalog does not know a collection named" in result.output
    assert engine.paths()[-1] == "/v1/vectors/gc/quarantine-orphans"


def test_a_collection_whose_manifest_names_none_of_its_chunks_refuses(runner, real_client):
    """nexus-jqrtp: read off the census (stored chunks minus the manifest-less buckets is 0)."""
    engine = _Engine(total=10, reapable=[1, 2], no_owner=10)
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code != 0
    assert "names NONE of the 10" in result.output
    assert "--allow-empty-manifest-set" in result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()


def test_the_empty_manifest_override_lets_the_move_run(runner, real_client):
    engine = _Engine(total=10, reapable=[1, 2], no_owner=10)
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes", "--allow-empty-manifest-set"])
    assert result.exit_code == 0, result.output
    assert engine.paths()[-1] == "/v1/vectors/gc/quarantine-orphans"


# ── (RDR-192 side-table critique 28255 issue 1) census, bounded form, permanent floor ───────────


def test_a_nonzero_unclassified_census_refuses_and_moves_nothing(runner, real_client):
    """The reaper refuses on unclassified > 0 as well as legacy-unmanifested > 0; the verb matches it.
    A census that cannot classify a row has not understood the collection."""
    engine = _Engine(total=10, reapable=[1, 2], unclassified=1)
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code != 0
    assert "unclassified = 1" in result.output and "REFUSING" in result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()


def test_the_unclassified_gate_is_reported_by_a_dry_run_and_rechecked_before_the_move(runner, real_client):
    engine = _Engine(total=10, reapable=[1])
    engine.unclassified = 2
    dry = _invoke(runner, real_client, engine, ["--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "REFUSE" in dry.output and "unclassified" in dry.output

    flipped = _Engine(total=10, reapable=[1], unclassified=0, unclassified_on_recheck=1)
    result = _invoke(runner, real_client, flipped, ["--no-dry-run", "--yes"])
    assert result.exit_code != 0
    assert "unclassified" in result.output and "REFUSING" in result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in flipped.paths()


def test_the_bounded_move_is_drained_until_remaining_is_zero(runner, real_client):
    engine = _Engine(total=10, reapable=[1, 2, 3], batches=[2, 1])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    moves = [b for p, b in engine.posted if p == "/v1/vectors/gc/quarantine-orphans"]
    assert len(moves) == 2 and all(m["row_limit"] == 2000 for m in moves)
    assert "quarantined 3 chunk(s)" in result.output


def test_the_floor_is_the_gc_family_variable_and_never_the_reapers(runner, real_client, monkeypatch):
    """The client floor is permanent (the engine route carries none; the reaper's floor lives in a
    function with no HTTP route) and reads NX_GC_FLOOR_FRACTION, the name the indexer's expiry floor
    already uses, not the engine reaper's NX_REAPER_FLOOR_FRACTION (a different process's env)."""
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    monkeypatch.setenv("NX_REAPER_FLOOR_FRACTION", "1.0")  # must not loosen the verb's floor
    monkeypatch.setenv("NX_GC_FLOOR_FRACTION", "0.5")
    over = _Engine(total=100, reapable=list(range(1, 61)))  # 60% > 50%
    refused = _invoke(runner, real_client, over, ["--no-dry-run", "--yes"])
    assert refused.exit_code != 0 and "NX_GC_FLOOR_FRACTION" in refused.output
    under = _Engine(total=100, reapable=list(range(1, 41)))  # 40% < 50%
    allowed = _invoke(runner, real_client, under, ["--no-dry-run", "--yes"])
    assert allowed.exit_code == 0, allowed.output
