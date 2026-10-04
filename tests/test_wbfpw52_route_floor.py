# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-wbfpw.52 (RDR-192): the fraction floor on ``POST /v1/vectors/gc/quarantine-orphans``, client half.

Two callers move through that route: ``nx t3 gc`` and ``indexer._prune_deleted_files``. Both now send the GC
family's floor (``NX_GC_FLOOR_FRACTION``, the 100-chunk minimum, ``NX_GC_FORCE``), the ENGINE judges it under its
sweep gate on the whole reapable set, and each caller reads the engine's echo of the floor back: an engine that
predates the fields ignores them, moves unguarded, and answers without the echo.

Three layers:

* the wrappers in ``chunk_quarantine`` and ``HttpVectorClient``, against fake handles (what rides the request,
  how an echo, no echo and a refusal are read);
* the indexer's prune against a fake handle (what it logs, that the expiry still runs after a refusal);
* the real engine (``t2_service_env``): refusal, override, the strict boundary, the minimum, the audit row, the
  indexer's prune and ``nx t3 gc`` end to end. The engine's own SQL is pinned in
  ``GcQuarantineOrphansFloorRouteTest`` (service/).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

import nexus.catalog.chunk_quarantine as cq
from nexus.catalog.chunk_quarantine import GcFloor, GcFloorRefused
from tests._catalog_fixture_ops import ActiveCatalog
from tests._reapable_cli_fixture import build_mixed_collection

_COLL = "knowledge__nexus-1-1__voyage-context-3__v1"
_QUAR = "quarantine-" + _COLL
_STAMP = "2026-10-04T00:00:00Z"


def _echo(*, given: bool = True) -> dict:
    return {"floor": {"given": given, "fraction": 0.25, "min_chunks": 100, "force": False}}


class _Db:
    """A handle that records the keyword fields each move call carried and answers from a script."""

    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def gc_quarantine_orphans_bounded(self, c, q, at, sample, row_limit, **floor):
        self.calls.append(floor)
        return self.responses.pop(0)

    def gc_quarantine_orphans(self, c, q, at, sample, **floor):
        self.calls.append(floor)
        return self.responses.pop(0)


def _batch(moved: int, remaining: int, **extra) -> dict:
    return {"moved": moved, "sample": [], "remaining": remaining, "row_limit": 2000, **extra}


# ── the wrappers ──────────────────────────────────────────────────────────────


def test_without_a_floor_the_request_is_the_unchanged_one() -> None:
    db = _Db(_batch(2, 0))
    assert cq.quarantine_orphans_bounded_serverside(db, _COLL, _QUAR, _STAMP) == (2, [])
    assert db.calls == [{}], "no floor fields may ride a request that was not given a floor"
    unbounded = _Db({"moved": 1, "sample": []})
    assert cq.quarantine_orphans_serverside(unbounded, _COLL, _QUAR, _STAMP) == (1, [])
    assert unbounded.calls == [{}]


def test_the_floor_rides_every_batch_and_an_echo_marks_the_engine_as_applying_it() -> None:
    floor = GcFloor(0.25, 100)
    db = _Db(_batch(2, 1, **_echo()), _batch(1, 0, **_echo()))
    assert cq.quarantine_orphans_bounded_serverside(db, _COLL, _QUAR, _STAMP, floor=floor) == (3, [])
    assert db.calls == [{"floor_fraction": 0.25, "floor_min_chunks": 100, "force": False}] * 2
    assert floor.engine_applied is True


def test_a_response_with_no_echo_is_an_engine_that_ignored_the_floor() -> None:
    floor = GcFloor(0.25, 100)
    db = _Db(_batch(2, 0))  # an older engine: the fields are ignored and nothing is echoed
    assert cq.quarantine_orphans_bounded_serverside(db, _COLL, _QUAR, _STAMP, floor=floor) == (2, [])
    assert floor.engine_applied is False


def test_a_given_false_echo_is_not_support_for_this_requests_floor() -> None:
    """``{"given": false}`` is what a floor-aware engine writes for a request that carried NO floor. The
    wrapper sent one, so an answer saying none was given is not the engine applying it."""
    floor = GcFloor(0.25, 100)
    cq.quarantine_orphans_bounded_serverside(_Db(_batch(1, 0, **_echo(given=False))), _COLL, _QUAR, _STAMP, floor=floor)
    assert floor.engine_applied is False


def test_one_unechoed_batch_makes_the_whole_drain_unguarded() -> None:
    floor = GcFloor(0.25, 100)
    db = _Db(_batch(2, 1, **_echo()), _batch(1, 0))
    cq.quarantine_orphans_bounded_serverside(db, _COLL, _QUAR, _STAMP, floor=floor)
    assert floor.engine_applied is False


def test_an_engine_refusal_raises_with_the_engines_counts_and_what_earlier_batches_moved() -> None:
    floor = GcFloor(0.25, 100)
    refused = _batch(0, 130, refused=True, reapable_count=130, total_count=400, **_echo())
    db = _Db(_batch(2, 1, **_echo()), refused)
    with pytest.raises(GcFloorRefused) as caught:
        cq.quarantine_orphans_bounded_serverside(db, _COLL, _QUAR, _STAMP, floor=floor)
    assert (caught.value.reapable, caught.value.total, caught.value.moved) == (130, 400, 2)
    assert caught.value.floor is floor and floor.engine_applied is True


def test_a_refusal_is_the_engines_decision_not_a_stalled_strict_drain() -> None:
    """strict=True turns a no-progress batch into BoundedDrainIncomplete; a refusal also moves nothing, and
    must stay what it is."""
    refused = _batch(0, 130, refused=True, reapable_count=130, total_count=400, **_echo())
    with pytest.raises(GcFloorRefused):
        cq.quarantine_orphans_bounded_serverside(
            _Db(refused), _COLL, _QUAR, _STAMP, strict=True, floor=GcFloor(0.25, 100))


def test_the_unbounded_form_reads_the_floor_too() -> None:
    floor = GcFloor(0.25, 100, force=True)
    db = _Db({"moved": 4, "sample": [], **_echo()})
    assert cq.quarantine_orphans_serverside(db, _COLL, _QUAR, _STAMP, floor=floor) == (4, [])
    assert db.calls == [{"floor_fraction": 0.25, "floor_min_chunks": 100, "force": True}]
    assert floor.engine_applied is True
    refused = _Db({"moved": 0, "sample": [], "refused": True, "reapable_count": 9, "total_count": 10, **_echo()})
    with pytest.raises(GcFloorRefused):
        cq.quarantine_orphans_serverside(refused, _COLL, _QUAR, _STAMP, floor=GcFloor(0.25, 100))


def test_a_refused_flag_without_an_echo_is_not_believed() -> None:
    """Only an engine that echoes the floor can refuse by it; a stray ``refused`` key is not a refusal."""
    floor = GcFloor(0.25, 100)
    assert cq.quarantine_orphans_bounded_serverside(
        _Db(_batch(1, 0, refused=True)), _COLL, _QUAR, _STAMP, floor=floor) == (1, [])
    assert floor.engine_applied is False


def test_the_http_client_sends_the_floor_fields_only_when_given_one() -> None:
    from nexus.db.http_vector_client import HttpVectorClient

    sent: list[dict] = []

    def fake_post(path, body, **_kw):
        sent.append(body)
        return {"moved": 0, "sample": [], "remaining": 0}

    client = HttpVectorClient.__new__(HttpVectorClient)
    client._tenant = None
    with patch("nexus.db.http_vector_client._post", fake_post):
        client.gc_quarantine_orphans(_COLL, _QUAR, _STAMP)
        client.gc_quarantine_orphans_bounded(_COLL, _QUAR, _STAMP, 20, 2000)
        client.gc_quarantine_orphans(_COLL, _QUAR, _STAMP, floor_fraction=0.25, floor_min_chunks=100, force=True)
        client.gc_quarantine_orphans_bounded(_COLL, _QUAR, _STAMP, 20, 2000, floor_fraction=0.5)
    floor_keys = {"floor_fraction", "floor_min_chunks", "force"}
    assert not (floor_keys & set(sent[0])) and not (floor_keys & set(sent[1]))
    assert (sent[2]["floor_fraction"], sent[2]["floor_min_chunks"], sent[2]["force"]) == (0.25, 100, True)
    assert sent[3]["floor_fraction"] == 0.5 and sent[3]["force"] is False and "floor_min_chunks" not in sent[3]
    assert sent[3]["row_limit"] == 2000


# ── the indexer's prune ───────────────────────────────────────────────────────


class _PruneDb:
    """Every route ``_prune_collection_serverside`` drives; the move answers from *move*."""

    def __init__(self, move: dict) -> None:
        self.move, self.calls, self.floor = move, [], None

    def gc_restore_rereferenced_bounded(self, quarantine, origin, row_limit):
        self.calls.append("restore")
        return {"restored": 0, "remaining": 0}

    def gc_quarantine_orphans_bounded(self, origin, quarantine, at, sample, row_limit, **floor):
        self.calls.append("move")
        self.floor = floor
        return self.move

    def gc_expire_quarantine(self, quarantine, origin, cutoff, fraction, minimum, force):
        self.calls.append("expire")
        return {"expired": 0, "refused": 0}


def _prune(db) -> bool:
    from nexus.indexer import _prune_collection_serverside

    return _prune_collection_serverside(db, _COLL, _QUAR, _STAMP)


def _events(log) -> dict[str, dict]:
    return {c.args[0]: c.kwargs for c in log.warning.call_args_list}


def test_the_prune_sends_the_gc_familys_floor_and_override(monkeypatch) -> None:
    monkeypatch.delenv("NX_GC_FLOOR_FRACTION", raising=False)
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    db = _PruneDb(_batch(0, 0, **_echo()))
    with patch("nexus.indexer._log") as log:
        assert _prune(db) is True
    assert db.floor == {"floor_fraction": 0.25, "floor_min_chunks": 100, "force": False}
    assert _events(log) == {}, "an engine that echoes the floor earns no warning"

    monkeypatch.setenv("NX_GC_FLOOR_FRACTION", "0.4")
    monkeypatch.setenv("NX_GC_FORCE", "1")
    db = _PruneDb(_batch(0, 0, **_echo()))
    with patch("nexus.indexer._log"):
        _prune(db)
    assert db.floor == {"floor_fraction": 0.4, "floor_min_chunks": 100, "force": True}


def test_the_prune_warns_when_the_engine_did_not_apply_the_floor(monkeypatch) -> None:
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    db = _PruneDb(_batch(2, 0))  # an older engine: no echo
    with patch("nexus.indexer._log") as log:
        assert _prune(db) is True
    warned = _events(log)["gc_prune_floor_not_applied_by_engine"]
    assert warned["collection"] == _COLL and warned["floor_fraction"] == 0.25
    assert db.calls == ["restore", "move", "expire"]


def test_a_refused_prune_moves_nothing_logs_the_counts_and_still_expires(monkeypatch) -> None:
    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    db = _PruneDb(_batch(0, 130, refused=True, reapable_count=130, total_count=400, **_echo()))
    with patch("nexus.indexer._log") as log:
        assert _prune(db) is True, "a refusal is a decision, not a failed prune"
    warned = _events(log)["gc_prune_refused_by_floor"]
    assert (warned["reapable"], warned["total"], warned["collection"]) == (130, 400, _COLL)
    assert "gc_prune_floor_not_applied_by_engine" not in _events(log)
    assert db.calls == ["restore", "move", "expire"], "the expiry has no floor and is not the move"


# ── a real engine ─────────────────────────────────────────────────────────────

@pytest.fixture
def env(t2_service_env, monkeypatch):
    import nexus.db.http_vector_client as hvc

    monkeypatch.delenv("NX_GC_FORCE", raising=False)
    monkeypatch.delenv("NX_GC_FLOOR_FRACTION", raising=False)
    return hvc.HttpVectorClient(tenant=t2_service_env), ActiveCatalog()


def _ids(client, coll: str) -> set[str]:
    return set(client.get_collection(coll).get_all_metadata(include_non_live=True)["ids"])


def _qname(coll: str) -> str:
    return cq.quarantine_collection_name(coll)


def _audit(coll: str, operation: str) -> list[dict]:
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    return [r for r in HttpCatalogClient().gc_audit_list(collection=coll, limit=50) if r["operation"] == operation]


# The mixed collection holds 6 chunks, 2 of them reapable: a third. Floors below are chosen around that.


def test_the_engine_refuses_an_over_floor_move_and_audits_the_refusal(env) -> None:
    client, cat = env
    fx = build_mixed_collection(cat, "w52-refuse")
    floor = GcFloor(0.25, 2)

    with pytest.raises(GcFloorRefused) as caught:
        cq.quarantine_orphans_bounded_serverside(client, fx.name, _qname(fx.name), _STAMP, floor=floor)

    assert (caught.value.reapable, caught.value.total) == (2, 6), "the engine's counts, judged under its gate"
    assert floor.engine_applied is True
    assert _ids(client, fx.name) == fx.everything, "a refusal moves nothing"
    rows = _audit(fx.name, "gc_quarantine_orphans_refused")
    assert len(rows) == 1
    assert rows[0]["details"]["reapable_count"] == 2 and rows[0]["details"]["total_count"] == 6
    assert _audit(fx.name, "gc_quarantine_orphans_bounded") == [], "no move row for a move that did not happen"


def test_the_unbounded_route_refuses_too(env) -> None:
    client, cat = env
    fx = build_mixed_collection(cat, "w52-unbounded")
    with pytest.raises(GcFloorRefused):
        cq.quarantine_orphans_serverside(client, fx.name, _qname(fx.name), _STAMP, floor=GcFloor(0.25, 2))
    assert _ids(client, fx.name) == fx.everything


def test_force_moves_what_the_floor_would_refuse(env) -> None:
    client, cat = env
    fx = build_mixed_collection(cat, "w52-force")
    floor = GcFloor(0.25, 2, force=True)

    moved, _sample = cq.quarantine_orphans_bounded_serverside(client, fx.name, _qname(fx.name), _STAMP, floor=floor)

    assert moved == 2 and floor.engine_applied is True, "force skips the judgement; the engine still echoes the floor"
    assert _ids(client, _qname(fx.name)) == fx.reapable
    assert _audit(fx.name, "gc_quarantine_orphans_refused") == []


@pytest.mark.parametrize(
    ("fraction", "minimum"),
    [(0.25, 3), (0.34, 2), (1.0, 2)],
    ids=["under the minimum", "exactly 1/3 is not more than 0.34", "a fraction of 1.0 can never fire"],
)
def test_a_pass_the_floor_admits_moves(env, fraction: float, minimum: int) -> None:
    client, cat = env
    fx = build_mixed_collection(cat, f"w52-admit-{minimum}-{int(fraction * 100)}")
    floor = GcFloor(fraction, minimum)

    moved, _ = cq.quarantine_orphans_bounded_serverside(client, fx.name, _qname(fx.name), _STAMP, floor=floor)

    assert moved == 2 and floor.engine_applied is True
    assert _ids(client, _qname(fx.name)) == fx.reapable


def test_a_call_with_no_floor_is_unchanged_and_reports_no_echo_state(env) -> None:
    client, cat = env
    fx = build_mixed_collection(cat, "w52-nofloor")

    moved, _ = cq.quarantine_orphans_bounded_serverside(client, fx.name, _qname(fx.name), _STAMP)

    assert moved == 2, "a third of the collection moves when no floor is given, as it always did"
    assert _audit(fx.name, "gc_quarantine_orphans_refused") == []


def test_the_indexer_prune_is_refused_over_the_floor_and_forced_through_by_nx_gc_force(env, monkeypatch) -> None:
    import nexus.indexer as indexer

    client, cat = env
    fx = build_mixed_collection(cat, "w52-prune")
    monkeypatch.setattr(indexer, "_GC_FLOOR_MIN_CHUNKS", 2)

    with patch("nexus.indexer._log") as log:
        indexer._prune_deleted_files(fx.name, "docs__w52-unused", client, catalog=cat)
    assert _ids(client, fx.name) == fx.everything, "a third of the collection is over the floor: nothing moves"
    assert _events(log)["gc_prune_refused_by_floor"]["reapable"] == 2
    assert len(_audit(fx.name, "gc_quarantine_orphans_refused")) == 1

    monkeypatch.setenv("NX_GC_FORCE", "1")
    indexer._prune_deleted_files(fx.name, "docs__w52-unused", client, catalog=cat)
    assert _ids(client, _qname(fx.name)) == fx.reapable


def test_nx_t3_gc_reports_the_engines_refusal_when_the_set_grew_after_its_listing(env, monkeypatch) -> None:
    """The verb's listing check passes (it is shown one reapable chunk, under the minimum of 2); the engine,
    reading the whole reapable set under its gate, finds 2 of 6 over the floor and refuses."""
    import nexus.db.http_vector_client as hvc
    import nexus.indexer as indexer
    from nexus.cli import main

    client, cat = env
    fx = build_mixed_collection(cat, "w52-verb")
    monkeypatch.setattr(indexer, "_GC_FLOOR_MIN_CHUNKS", 2)
    real = hvc.HttpVectorClient.reapable_chunks

    def short_listing(self, *a, **kw):
        return iter(list(real(self, *a, **kw))[:1])

    with patch.object(hvc.HttpVectorClient, "reapable_chunks", short_listing):
        result = CliRunner().invoke(main, ["t3", "gc", "-c", fx.name, "--no-dry-run", "--yes"])

    assert result.exit_code == 1, result.output
    assert "REFUSING to move: the engine judged 2 of 6 chunk(s)" in result.output
    assert "The listing this verb checked first read 1" in result.output
    assert _ids(client, fx.name) == fx.everything
    assert len(_audit(fx.name, "gc_quarantine_orphans_refused")) == 1

    monkeypatch.setenv("NX_GC_FORCE", "1")
    with patch.object(hvc.HttpVectorClient, "reapable_chunks", short_listing):
        forced = CliRunner().invoke(main, ["t3", "gc", "-c", fx.name, "--no-dry-run", "--yes"])
    assert forced.exit_code == 0, forced.output
    assert "did not echo" not in forced.output
    assert _ids(client, _qname(fx.name)) == fx.reapable


def test_nx_t3_gc_against_this_engine_never_prints_the_no_echo_note(env) -> None:
    from nexus.cli import main

    client, cat = env
    fx = build_mixed_collection(cat, "w52-echo")

    result = CliRunner().invoke(main, ["t3", "gc", "-c", fx.name, "--no-dry-run", "--yes"])

    assert result.exit_code == 0, result.output
    assert "did not echo" not in result.output
    assert _ids(client, _qname(fx.name)) == fx.reapable
