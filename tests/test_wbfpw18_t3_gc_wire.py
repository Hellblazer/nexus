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
_MOVE = "/v1/vectors/gc/quarantine-orphans"
_EXPIRE = "/v1/vectors/gc/expire-quarantine"
#: nexus-wbfpw.58: the sibling probe the client runs between its move and its expiry.
_PROBE = "/v1/vectors/gc/quarantine-restore"
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
                 batches: list[int] | None = None, omit_scope: bool = False,
                 move_script: list | None = None, stuck: dict | None = None,
                 expire: dict | Exception | None = None, siblings: list[str] | None = None) -> None:
        # move_script: results (dict) or exceptions the move route answers with, in order; stuck: a
        # result the route answers with forever once the script is exhausted.
        self.omit_scope = omit_scope
        self.siblings = list(siblings) if siblings is not None else []
        self.expire = {"expired": 0, "refused": 0} if expire is None else expire
        self.move_script = list(move_script) if move_script is not None else None
        self.stuck = stuck
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
            page = {"collection": body["collection"], "returned": 0, "chashes": {},
                    "owners": {}, "totals": totals, "scope_chunk_total": self.total}
            if self.omit_scope:
                del page["scope_chunk_total"]
            return page
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
            if self.move_script is not None:
                if self.move_script:
                    step = self.move_script.pop(0)
                    if isinstance(step, Exception):
                        raise step
                    return step
                if self.stuck is not None:
                    return self.stuck
                raise AssertionError("the move was called more often than the test scripted")
            if self.batches is not None:
                moved = self.batches.pop(0)
                return {"moved": moved, "sample": [], "remaining": sum(self.batches),
                        "row_limit": body.get("row_limit")}
            return {"moved": len(self.reapable), "sample": [], "remaining": 0,
                    "row_limit": body.get("row_limit")}
        if path == "/v1/vectors/gc/restore-rereferenced":
            # Only the indexer's re-reference leg reaches this (nx t3 gc has none): nothing to restore.
            return {"restored": 0, "remaining": 0}
        if path == "/v1/vectors/gc/quarantine-restore":
            # The client's sibling probe (nexus-wbfpw.58): a dry-run over an empty selection that asks the
            # engine which quarantine collections hold the origin's chunks. It must change nothing.
            assert body.get("dry_run") is True and body.get("limit") == 1, body
            return {"origin_collection": body["origin_collection"], "dry_run": True, "rows": [],
                    "quarantine_collection": self.siblings[0] if self.siblings else None,
                    "quarantine_collections": list(self.siblings)}
        if path == "/v1/vectors/gc/expire-quarantine":
            if isinstance(self.expire, Exception):
                raise self.expire
            return dict(self.expire)
        raise AssertionError(f"unexpected engine route {path}: nx t3 gc must not use it")

    def paths(self) -> list[str]:
        return [p for p, _ in self.posted]

    def moves(self) -> list[dict]:
        return [b for p, b in self.posted if p == _MOVE]

    def expiries(self) -> list[dict]:
        return [b for p, b in self.posted if p == _EXPIRE]


def _invoke(runner: CliRunner, real_client, engine: _Engine, args: list[str], *, documents=(),
            catalog_knows: bool = True, documents_error: Exception | None = None):
    fake_cat = MagicMock()
    fake_cat.get_collection.return_value = object() if catalog_knows else None
    with (
        patch("nexus.db.http_vector_client._post", engine.post),
        patch("nexus.db.http_vector_client._get", lambda path, **kw: engine.stats()),
        patch("nexus.db.make_t3", return_value=real_client),
        patch("nexus.commands.t3._make_catalog", return_value=fake_cat),
        patch(
            "nexus.indexer_utils.catalog_documents_for_collection",
            **({"side_effect": documents_error} if documents_error is not None
               else {"return_value": list(documents)}),
        ),
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
    # The census gate comes first, then the move, then the client expiry of the sibling it filled,
    # and no hard delete by id anywhere.
    assert paths[0] == "/v1/vectors/manifest-less-census"
    assert paths[-3:] == [_MOVE, _PROBE, _EXPIRE]
    assert "/v1/vectors/store-delete" not in paths
    assert "/v1/vectors/get" not in paths
    move = engine.moves()[-1]
    assert move["collection"] == _COLL
    assert move["quarantine_collection"] == _QUARANTINE
    # The route is collection-wide: it carries no chash list and no exclusion list.
    assert not ({"chashes", "ids", "exclude", "exclusions"} & set(move))
    # The BOUNDED form (row_limit present): the unbounded form has a 5 s statement timeout. And the
    # audit sample is the engine's own ceiling (the sample is what the audit row carries; it is
    # not a restore source, see nx t3 quarantine restore).
    assert move["row_limit"] == 2000
    assert move["sample_limit"] == 5000
    assert "quarantined 3 chunk(s)" in result.output
    # The verb says it MOVED chunks; it promises neither storage freed nor a restore it cannot do.
    assert "moved, not freed" in result.output
    assert "restorable for" not in result.output


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


def test_nothing_reapable_moves_nothing_but_still_expires_an_earlier_quarantine(runner, real_client):
    """A run with nothing to move still leaves what an earlier run moved; the client expiry is what
    ever frees a knowledge__ quarantine, so it runs on every acting run, not only on a run that moved."""
    engine = _Engine(total=10, reapable=[], expire={"expired": 4, "refused": 0})
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()
    assert "nothing to do" in result.output
    (call,) = engine.expiries()
    assert call["quarantine_collection"] == _QUARANTINE and call["origin_collection"] == _COLL
    assert "4 expired, 0 refused" in result.output


def test_the_client_expiry_runs_over_every_sibling_the_engine_resolves(runner, real_client):
    """nexus-wbfpw.58: chunks a client moved under an earlier name of the origin (the catalog row's owner_id
    was rewritten since, catalog-044-3) sit in a sibling the row no longer derives, and carry no
    quarantined_by tag for the reaper to expire. The verb expires from that sibling too."""
    legacy = _QUARANTINE.replace("__nexus-1-1__", "__legacy-owner__")
    engine = _Engine(total=10, reapable=[], expire={"expired": 2, "refused": 0}, siblings=[_QUARANTINE, legacy])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert [c["quarantine_collection"] for c in engine.expiries()] == [_QUARANTINE, legacy]
    assert {c["origin_collection"] for c in engine.expiries()} == {_COLL}
    # One line per sibling, each with its own count: nothing is summed.
    lines = [ln for ln in result.output.splitlines() if ln.startswith("  Client expiry of")]
    assert [ln.split()[3] for ln in lines] == [_QUARANTINE, legacy]
    assert all("2 expired, 0 refused" in ln for ln in lines), lines


def test_an_index_path_prune_sends_no_sibling_probe(real_client):
    """nexus-wbfpw.58 round 2: ``nx index repo`` runs this prune per collection on every index, so it uses the
    two derived names (the row-derived one and the reaper's) and never sends the engine probe, whose resolution
    is an unindexed scan engine-side. nexus-wbfpw.64 retires the probe; ``nx t3 gc`` is the one caller of it."""
    from nexus.indexer import _prune_collection_serverside

    row_derived = _QUARANTINE.replace("__nexus-1-1__", "__rewritten-1-9__")
    engine = _Engine(total=10, reapable=[], expire={"expired": 1, "refused": 0},
                     siblings=[_QUARANTINE.replace("__nexus-1-1__", "__legacy__")])
    with patch("nexus.db.http_vector_client._post", engine.post):
        assert _prune_collection_serverside(real_client, _COLL, row_derived, "2026-01-01T00:00:00Z") is True
    assert _PROBE not in engine.paths(), engine.paths()
    assert [c["quarantine_collection"] for c in engine.expiries()] == [row_derived, _QUARANTINE]


def test_a_failed_probe_still_expires_the_row_derived_sibling(runner, real_client):
    """An engine that cannot answer the sibling probe must not cost the verb its expiry."""
    engine = _Engine(total=10, reapable=[], expire={"expired": 4, "refused": 0})
    real_post = engine.post

    def post(path, body, **kw):
        if path == _PROBE:
            raise VectorServiceError("no such route", code=404)
        return real_post(path, body, **kw)

    engine.post = post
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    (call,) = engine.expiries()
    assert call["quarantine_collection"] == _QUARANTINE and "4 expired" in result.output


def test_nothing_reapable_on_a_dry_run_expires_nothing(runner, real_client):
    engine = _Engine(total=10, reapable=[])
    result = _invoke(runner, real_client, engine, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert engine.expiries() == [] and engine.moves() == []


def test_a_failed_expiry_with_nothing_to_move_is_exit_1_and_says_so(runner, real_client):
    engine = _Engine(total=10, reapable=[], expire=VectorServiceError("engine went away", code=500))
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 1, result.output
    assert "nothing needed moving, but the client expiry" in result.output


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
    assert result.exit_code == 1, result.output
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
    engine = _Engine(total=400, reapable=list(range(1, 121)))  # 30% > the 25% default, 120 >= 100
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code != 0
    assert "NX_GC_FLOOR_FRACTION" in result.output and "NX_GC_FORCE=1" in result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()


def test_nx_gc_force_overrides_the_floor(runner, real_client, monkeypatch):
    monkeypatch.delenv("NX_GC_FLOOR_FRACTION", raising=False)
    monkeypatch.setenv("NX_GC_FORCE", "1")
    engine = _Engine(total=400, reapable=list(range(1, 121)))
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert engine.paths()[-3:] == [_MOVE, _PROBE, _EXPIRE]


def test_the_floor_does_not_apply_below_the_minimum_reapable_count(runner, real_client, monkeypatch):
    """The engine's reading: the 100 minimum counts the REAPABLE set (reaper_quarantine_chunks:
    v_reapable >= p_floor_min_chunks), not the collection's size. 99 reapable chunks is exempt however
    large a share of the collection they are."""
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    monkeypatch.delenv("NX_GC_FLOOR_FRACTION", raising=False)
    engine = _Engine(total=99, reapable=list(range(1, 100)))  # 100% of a 99-chunk collection
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert engine.paths()[-3:] == [_MOVE, _PROBE, _EXPIRE]


def test_a_dry_run_over_the_floor_says_a_real_run_would_refuse_and_exits_one(runner, real_client, monkeypatch):
    """A dry run that names a refusal exits 1, so `nx t3 gc --dry-run && nx t3 gc --no-dry-run --yes`
    stops where the real run would. A clean dry run still exits 0 (pinned by test_dry_run_moves_nothing)."""
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    monkeypatch.delenv("NX_GC_FLOOR_FRACTION", raising=False)
    engine = _Engine(total=400, reapable=list(range(1, 121)))
    result = _invoke(runner, real_client, engine, ["--dry-run"])
    assert result.exit_code == 1, result.output
    assert "REFUSE" in result.output and "NX_GC_FORCE=1" in result.output
    assert _MOVE not in engine.paths() and _EXPIRE not in engine.paths()


# ── the RUNFENCE circuit breaker is unchanged ─────────────────────────────────


def _indexing_doc():
    return SimpleNamespace(title="in-flight", index_state="indexing", file_path="/x.md", meta={})


def test_a_non_complete_document_still_refuses(runner, real_client):
    engine = _Engine(total=10, reapable=[1])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"], documents=[_indexing_doc()])
    assert result.exit_code != 0
    assert "not index_state='complete'" in result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()


def test_a_non_complete_document_with_nothing_reapable_does_not_claim_a_refusal(runner, real_client):
    """RUNFENCE guards the MOVE. With nothing reapable a real run never moves, so it goes straight to
    quarantine expiry; a dry run must not say a real run "will REFUSE" (it printed that on production
    rdr__1-20 on 2026-10-03 while exiting 0, which read as a broken exit code)."""
    engine = _Engine(total=10, reapable=[])
    result = _invoke(runner, real_client, engine, ["--dry-run"], documents=[_indexing_doc()])
    assert result.exit_code == 0, result.output
    assert "REFUSE" not in result.output
    assert "not index_state='complete'" in result.output


def test_a_non_complete_document_with_nothing_reapable_still_expires(runner, real_client):
    engine = _Engine(total=10, reapable=[])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"], documents=[_indexing_doc()])
    assert result.exit_code == 0, result.output
    assert _MOVE not in engine.paths()
    assert _EXPIRE in engine.paths()


def test_the_incomplete_state_override_lets_the_move_run(runner, real_client):
    engine = _Engine(total=10, reapable=[1])
    result = _invoke(runner, real_client, engine,
                     ["--no-dry-run", "--yes", "--allow-incomplete-index-state"],
                     documents=[_indexing_doc()])
    assert result.exit_code == 0, result.output
    assert engine.paths()[-3:] == [_MOVE, _PROBE, _EXPIRE]


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
    assert engine.paths()[-3:] == [_MOVE, _PROBE, _EXPIRE]


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
    assert engine.paths()[-3:] == [_MOVE, _PROBE, _EXPIRE]


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
    assert dry.exit_code == 1, dry.output
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
    over = _Engine(total=200, reapable=list(range(1, 121)))  # 60% > 50%
    refused = _invoke(runner, real_client, over, ["--no-dry-run", "--yes"])
    assert refused.exit_code != 0 and "NX_GC_FLOOR_FRACTION" in refused.output
    under = _Engine(total=250, reapable=list(range(1, 101)))  # 40% < 50%
    allowed = _invoke(runner, real_client, under, ["--no-dry-run", "--yes"])
    assert allowed.exit_code == 0, allowed.output


# ── (round 2) a census without scope_chunk_total refuses; it does not switch the guards off ─────


def test_a_census_without_scope_chunk_total_refuses_and_moves_nothing(runner, real_client, monkeypatch):
    """scope_chunk_total is the floor's denominator and half of the empty-manifest guard. A response
    that omits it used to read as 0, which silently turned BOTH off (a collection of 100 chunks, 90
    reapable, moved with exit 0). A missing blocking bucket already refused; this refuses the same way."""
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    engine = _Engine(total=100, reapable=list(range(1, 91)), omit_scope=True)
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code != 0
    assert "scope_chunk_total" in result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()
    # --allow-empty-manifest-set does not override an unreadable census.
    forced = _invoke(runner, real_client, _Engine(total=100, reapable=[1], omit_scope=True),
                     ["--no-dry-run", "--yes", "--allow-empty-manifest-set"])
    assert forced.exit_code != 0 and "scope_chunk_total" in forced.output


def test_a_dry_run_also_refuses_a_census_without_scope_chunk_total(runner, real_client):
    engine = _Engine(total=10, reapable=[1], omit_scope=True)
    result = _invoke(runner, real_client, engine, ["--dry-run"])
    assert result.exit_code != 0 and "scope_chunk_total" in result.output


def test_a_listing_against_a_zero_scope_census_is_refused(runner, real_client):
    """The listing names reapable chunks while the census says the collection holds none: the two
    disagree and the floor has no denominator, so the verb refuses instead of moving."""
    engine = _Engine(total=0, reapable=[1, 2])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code != 0
    assert "scope_chunk_total = 0" in result.output and "REFUSING" in result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()


# ── (round 2) a drain that ends short of remaining == 0 is exit 1, and says what already moved ──


def _result(moved: int, remaining: int) -> dict:
    return {"moved": moved, "sample": [], "remaining": remaining, "row_limit": 2000}


def test_a_stuck_engine_that_moves_nothing_is_exit_1_not_a_quiet_zero(runner, real_client):
    """An engine answering moved=0 remaining=7 forever used to be polled 200 times and reported as
    'quarantined 0', exit 0. A batch that makes no progress ends the drain."""
    engine = _Engine(total=10, reapable=[1, 2, 3], stuck=_result(0, 7), move_script=[])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 1, result.output
    assert "no progress" in result.output
    assert "0 chunk(s) were moved" in result.output
    assert "re-running this verb is safe" in result.output
    assert "Still reapable when it stopped: 7" in result.output
    assert engine.paths().count("/v1/vectors/gc/quarantine-orphans") == 1, "stop at the first idle batch"
    assert "quarantined 0" not in result.output


def test_the_iteration_cap_with_remaining_left_is_exit_1_with_the_moved_count(
    runner, real_client, monkeypatch,
):
    monkeypatch.setattr("nexus.catalog.chunk_quarantine._gc_loop_max_iterations", lambda _row_limit: 3)
    engine = _Engine(total=10, reapable=[1, 2, 3], move_script=[], stuck=_result(2, 5))
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 1, result.output
    assert "iteration cap" in result.output
    assert "6 chunk(s) were moved" in result.output and "in 3 earlier batch(es)" in result.output
    assert "committed on its own" in result.output
    assert "Still reapable when it stopped: 5" in result.output
    assert engine.paths().count("/v1/vectors/gc/quarantine-orphans") == 3
    assert "quarantined 6" not in result.output


def test_a_failure_after_earlier_batches_reports_what_already_moved(runner, real_client):
    engine = _Engine(
        total=10, reapable=[1, 2, 3],
        move_script=[_result(2, 1), VectorServiceError("engine went away", code=500)],
    )
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 1, result.output
    assert "batch failed" in result.output and "engine went away" in result.output
    assert "2 chunk(s) were moved" in result.output
    assert "re-running this verb is safe" in result.output


def test_a_failure_of_the_first_batch_is_exit_1_with_the_engine_message(runner, real_client):
    engine = _Engine(
        total=10, reapable=[1], move_script=[VectorServiceError("engine went away", code=500)],
    )
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 1, result.output
    assert "No batch is known to have moved a chunk" in result.output
    assert "engine went away" in result.output


def test_a_drain_whose_batches_all_make_progress_still_exits_zero(runner, real_client):
    """Non-vacuity for the strict drain: ordinary batches are not 'stuck'."""
    engine = _Engine(total=10, reapable=[1, 2, 3, 4], move_script=[_result(2, 2), _result(1, 1), _result(1, 0)])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert "quarantined 4 chunk(s)" in result.output


# ── (round 2) the RUNFENCE lookup fails closed ───────────────────────────────────────────────────


def test_a_failed_index_state_lookup_refuses_and_reads_no_chunk(runner, real_client):
    """The index-run state is unverifiable, so the run refuses outright rather than risk moving
    chunks an in-flight reindex has written but not yet manifested (nexus-g6k6b). A fail-open edit
    (treating the failure as 'no incomplete documents') must turn this red."""
    engine = _Engine(total=10, reapable=[1])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"],
                     documents_error=RuntimeError("catalog down"))
    assert result.exit_code == 1, result.output
    assert "Failed to verify index-run state" in result.output and "catalog down" in result.output
    assert engine.posted == [], "nothing may reach the engine once the fence cannot be read"


def test_a_failed_index_state_lookup_refuses_a_dry_run_too(runner, real_client):
    engine = _Engine(total=10, reapable=[1])
    result = _invoke(runner, real_client, engine, ["--dry-run"], documents_error=RuntimeError("catalog down"))
    assert result.exit_code == 1, result.output
    assert "Failed to verify index-run state" in result.output


# ── (round 2) the floor's denominator and its boundary ───────────────────────────────────────────


def test_the_floor_denominator_is_every_stored_chunk_not_just_the_owned_ones(runner, real_client, monkeypatch):
    """1000 chunks, 700 of them manifest-less (the no-owner bucket) and 100 reapable. Over the
    collection that is 10% (under the 25% floor); over only the 300 owned chunks it would be 33%
    (over it). The verb divides by scope_chunk_total, so this moves. A swap to owned_chunks refuses."""
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    monkeypatch.delenv("NX_GC_FLOOR_FRACTION", raising=False)
    engine = _Engine(total=1000, no_owner=700, reapable=list(range(1, 101)))
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert engine.paths()[-3:] == [_MOVE, _PROBE, _EXPIRE]


def test_a_large_manifest_less_bucket_does_not_hide_a_real_floor_breach(runner, real_client, monkeypatch):
    """The other direction: 300 of 1000 is 30% of the collection, over the floor."""
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    monkeypatch.delenv("NX_GC_FLOOR_FRACTION", raising=False)
    engine = _Engine(total=1000, no_owner=500, reapable=list(range(1, 301)))
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code != 0
    assert "300 of 1000 chunk(s) (30%)" in result.output
    assert "/v1/vectors/gc/quarantine-orphans" not in engine.paths()


@pytest.mark.parametrize(("candidates", "refused"), [(100, False), (101, True)])
def test_the_floor_boundary_is_strictly_greater_than_the_fraction(
    runner, real_client, monkeypatch, candidates, refused,
):
    """Exactly 25% of 400 (100 chunks, which is also the 100 minimum) is allowed; one more chunk is
    refused. (> not >=.)"""
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    monkeypatch.delenv("NX_GC_FLOOR_FRACTION", raising=False)
    engine = _Engine(total=400, reapable=list(range(1, candidates + 1)))
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    if refused:
        assert result.exit_code != 0 and "NX_GC_FLOOR_FRACTION" in result.output
        assert _MOVE not in engine.paths()
    else:
        assert result.exit_code == 0, result.output
        assert engine.paths()[-3:] == [_MOVE, _PROBE, _EXPIRE]


@pytest.mark.parametrize(("candidates", "refused"), [(99, False), (100, True)])
def test_the_floor_minimum_counts_the_reapable_chunks_not_the_collection_size(
    runner, real_client, monkeypatch, candidates, refused,
):
    """33% of a 300-chunk collection is over the 25% floor, but the floor engages only from 100
    REAPABLE chunks (the engine's reading). 99 of 300 moves; 100 of 300 is refused. A basis on the
    collection size (300 >= 100 in both) refuses the 99 too."""
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    monkeypatch.delenv("NX_GC_FLOOR_FRACTION", raising=False)
    engine = _Engine(total=300, reapable=list(range(1, candidates + 1)))
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    if refused:
        assert result.exit_code != 0 and "NX_GC_FLOOR_FRACTION" in result.output
        assert _MOVE not in engine.paths()
    else:
        assert result.exit_code == 0, result.output
        assert engine.paths()[-3:] == [_MOVE, _PROBE, _EXPIRE]


@pytest.mark.parametrize(("candidates", "refused"), [(116, False), (117, True)])
def test_the_floor_compares_by_division_not_by_a_float_product(
    runner, real_client, monkeypatch, candidates, refused,
):
    """NX_GC_FLOOR_FRACTION=0.29 over 400 chunks: 116 is exactly 29%, which the engine's
    v_reapable / v_total > fraction allows. The product 0.29 * 400 is 115.99999999999999, so
    `116 > 0.29 * 400` refuses a pass the engine would take. One more chunk is over either way."""
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    monkeypatch.setenv("NX_GC_FLOOR_FRACTION", "0.29")
    engine = _Engine(total=400, reapable=list(range(1, candidates + 1)))
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    if refused:
        assert result.exit_code != 0 and "NX_GC_FLOOR_FRACTION" in result.output
        assert _MOVE not in engine.paths()
    else:
        assert result.exit_code == 0, result.output
        assert engine.paths()[-3:] == [_MOVE, _PROBE, _EXPIRE]


# ---- (round 3) the verb runs the client expiry after its own move -----------------------------


def test_the_act_runs_the_client_expiry_on_the_sibling_it_filled(runner, real_client, monkeypatch):
    """A knowledge__ collection is swept by no repo index, so without this call nothing would ever
    expire what this verb moved. Same call, same cutoff rule, same floor parameters as the indexer's."""
    monkeypatch.delenv("NX_GC_QUARANTINE_DAYS", raising=False)
    monkeypatch.delenv("NX_GC_FLOOR_FRACTION", raising=False)
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    engine = _Engine(total=10, reapable=[1, 2], expire={"expired": 3, "refused": 0})
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    (call,) = engine.expiries()
    assert call["quarantine_collection"] == _QUARANTINE and call["origin_collection"] == _COLL
    assert call["floor_fraction"] == 0.25 and call["floor_min_chunks"] == 100 and call["force"] is False
    from datetime import UTC, datetime, timedelta
    cutoff = datetime.strptime(call["cutoff"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - timedelta(days=14)) - cutoff) < timedelta(minutes=5)
    assert "3 expired, 0 refused" in result.output


def test_the_client_expiry_honours_the_gc_environment(runner, real_client, monkeypatch):
    monkeypatch.setenv("NX_GC_QUARANTINE_DAYS", "3")
    monkeypatch.setenv("NX_GC_FLOOR_FRACTION", "0.6")
    monkeypatch.setenv("NX_GC_FORCE", "1")
    engine = _Engine(total=10, reapable=[1])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    (call,) = engine.expiries()
    assert call["floor_fraction"] == 0.6 and call["force"] is True
    from datetime import UTC, datetime, timedelta
    cutoff = datetime.strptime(call["cutoff"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - timedelta(days=3)) - cutoff) < timedelta(minutes=5)


def test_an_expiry_that_expired_nothing_names_both_things_that_can_refuse(runner, real_client, monkeypatch):
    """expired = 0 with refused > 0 cannot tell a floor refusal from manifest keeps, so the line names
    both and says FORCE overrides only the floor."""
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    engine = _Engine(total=10, reapable=[1], expire={"expired": 0, "refused": 120})
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert "0 expired, 120 refused" in result.output
    assert "the manifest references them again, or the NX_GC_FLOOR_FRACTION floor" in result.output
    assert "NX_GC_FORCE=1 overrides only the floor" in result.output


def test_a_refusal_next_to_an_expiry_is_a_manifest_keep_not_the_floor(runner, real_client, monkeypatch):
    """The engine reports expired = 0 when the floor fires, so expired > 0 with refused > 0 means the
    floor did not fire: those chunks were kept because the manifest references them again, which
    NX_GC_FORCE does not change. The line must not blame the floor or offer FORCE for them."""
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    engine = _Engine(total=10, reapable=[1], expire={"expired": 4, "refused": 2})
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert "4 expired, 2 refused (kept: the manifest references them again" in result.output
    assert "NX_GC_FORCE=1 does not change that" in result.output
    assert "FLOOR_FRACTION floor" not in result.output.split("Client expiry of", 1)[1].split("\n", 1)[0]


def test_a_failed_expiry_after_a_good_move_exits_one_and_says_the_move_stands(runner, real_client):
    engine = _Engine(total=10, reapable=[1, 2], expire=VectorServiceError("engine went away", code=500))
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 1, result.output
    assert "the move succeeded (2 chunk(s) quarantined)" in result.output
    assert "client expiry" in result.output and "engine went away" in result.output


def test_no_expiry_runs_when_the_move_did_not_complete(runner, real_client):
    engine = _Engine(total=10, reapable=[1], move_script=[VectorServiceError("engine went away", code=500)])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 1, result.output
    assert engine.expiries() == []


def test_a_dry_run_runs_no_expiry(runner, real_client):
    engine = _Engine(total=10, reapable=[1])
    result = _invoke(runner, real_client, engine, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert engine.expiries() == [] and engine.moves() == []


# ---- (round 3) the empty-manifest override does not reach the scope == 0 refusal -------------


def test_the_empty_manifest_override_does_not_override_a_zero_scope_census(runner, real_client):
    """scope_chunk_total = 0 with a non-empty listing is a disagreement the floor cannot be judged
    on; it is its own refusal and --allow-empty-manifest-set (the nexus-jqrtp / nexus-v1zdu
    override) is not a license for it. If that flag ever started skipping the scope reason, this
    would move on an unverifiable collection."""
    engine = _Engine(total=0, reapable=[1, 2])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes", "--allow-empty-manifest-set"])
    assert result.exit_code != 0
    assert "scope_chunk_total = 0" in result.output and "REFUSING" in result.output
    assert _MOVE not in engine.paths() and _EXPIRE not in engine.paths()


# ---- (round 3) failure wording: unknown outcome, not "nothing moved"; moved more than listed ---


def test_a_failure_of_the_first_batch_claims_no_more_than_it_knows(runner, real_client):
    """A timeout or transport error can follow a server commit, so 'no batch is known to have moved'
    is as much as the verb can say, and the audit record is named as the way to find out."""
    engine = _Engine(total=10, reapable=[1], move_script=[VectorServiceError("edge timeout", code=504)])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 1, result.output
    assert "No batch is known to have moved a chunk" in result.output
    assert "before any batch moved a chunk" not in result.output
    assert "gc-audit list" in result.output


def test_a_mid_drain_failure_says_the_failed_batchs_own_outcome_is_unknown(runner, real_client):
    engine = _Engine(
        total=10, reapable=[1, 2, 3],
        move_script=[_result(2, 1), VectorServiceError("edge timeout", code=504)],
    )
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 1, result.output
    assert "2 chunk(s) were moved" in result.output
    assert "failed batch's own outcome is unknown" in result.output
    # the other stops (no progress, iteration cap) are not 'unknown outcome' failures
    stuck = _Engine(total=10, reapable=[1], stuck=_result(0, 4), move_script=[])
    other = _invoke(runner, real_client, stuck, ["--no-dry-run", "--yes"])
    assert "outcome is unknown" not in other.output


def test_a_drain_that_moves_more_than_the_listing_says_so(runner, real_client):
    """Chunks can age past the grace while the drain runs; the floor was judged on the listing, so
    the summary names the surplus instead of reporting it as a normal pass."""
    engine = _Engine(total=10, reapable=[1, 2], move_script=[_result(3, 0)])
    result = _invoke(runner, real_client, engine, ["--no-dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert "quarantined 3 chunk(s)" in result.output
    assert "The listing named 2; the engine moved 3, MORE than listed" in result.output
