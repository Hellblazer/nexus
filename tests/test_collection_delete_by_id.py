# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.12 (nexus-p2m1x.32): ``nx collection delete`` by label or by ``--id``.

The client half of engine 0.1.202's delete by ``collection_id`` (``CollectionDeleteRoute``). The tests run the real
``HttpCatalogClient`` over an ``httpx.MockTransport`` that answers as the engine does, with the wire shapes read from the
route's Java (``CollectionDeleteByIdTest``, ``CollectionDeleteLabelClashTest``):

* 200 ``{deleted, collection_id, name}``;
* 404 ``unknown_collection_id`` + ``collection_id``;
* 409 ``collection_superseded`` + ``collection``, ``collection_id``, ``superseded_by`` (the successor's label) and
  ``successor_collection_id``, for a RETIRED ID (a retired LABEL is deleted with the 200 it always got: the refusal that
  names the successor is this client's, taken from ``/collections/list`` before anything is sent);
* 409 ``collection_label_ambiguous`` + ``collection_ids``;
* ``GET /version`` flag ``catalog_collection_delete_by_id_supported``.

The fixtures and the invoke helper are ``tests/test_collection_cmd.py``'s, as ``tests/test_collection_busy_delete.py``'s are.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from nexus.catalog.engine_writes.collection_delete import (
    AmbiguousCollectionLabelError, SupersededCollectionDeleteError, UnknownCollectionIdError, check_label_deletable, refusal_of)
from nexus.catalog.http_catalog_client import HttpCatalogClient
from tests.test_collection_cmd import (  # noqa: F401 — the fixtures are the module's
    _invoke, env_creds, mock_db, runner,
)

FLAG = "catalog_collection_delete_by_id_supported"
DELETE_PATH = "/v1/catalog/collections/delete"


def _row(name: str, cid: int, *, superseded_by: str = "") -> dict:
    return {"name": name, "collection_id": cid, "content_type": "knowledge", "owner_id": "1-1",
            "embedding_model": "voyage-context-3", "dimension": 1024,
            "lifecycle_state": "live", "superseded_by": superseded_by}


class FakeEngine:
    """The engine's registry and delete route, as ``CollectionDeleteRoute`` answers them. Every request is recorded."""

    def __init__(self, rows: list[dict], *, flag: bool = True, release: str = "0.1.202") -> None:
        self.rows = list(rows)
        self.flag = flag
        self.release = release
        self.requests: list[tuple[str, str, dict]] = []

    def posts(self) -> list[dict]:
        return [b for m, p, b in self.requests if m == "POST" and p == DELETE_PATH]

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body))
        if request.url.path == "/version":
            return httpx.Response(200, json={"release_version": self.release, **({FLAG: True} if self.flag else {})})
        if request.url.path == "/v1/catalog/collections/list":
            return httpx.Response(200, json={"collections": self.rows})
        if request.url.path == DELETE_PATH:
            return self._delete(body)
        return httpx.Response(404, json={"error": "no route"})

    def _delete(self, body: dict) -> httpx.Response:
        if "collection_id" in body:
            cid = body["collection_id"]
            row = next((r for r in self.rows if r["collection_id"] == cid), None)
            if row is None:
                return httpx.Response(404, json={"error": f"collection_id {cid} is not a registered collection",
                                                 "reason": "unknown_collection_id", "collection_id": cid})
            if row["superseded_by"]:
                succ = next(r for r in self.rows if r["name"] == row["superseded_by"] and not r["superseded_by"])
                return httpx.Response(409, json={
                    "error": "collection_superseded", "reason": "collection_superseded", "message": "superseded",
                    "collection": row["name"], "collection_id": cid,
                    "superseded_by": succ["name"], "successor_collection_id": succ["collection_id"]})
        else:
            held = [r for r in self.rows if r["name"] == body["name"] and not r["superseded_by"]]
            if len(held) > 1:
                return httpx.Response(409, json={
                    "error": f"collection '{body['name']}' is held by {len(held)} live rows",
                    "reason": "collection_label_ambiguous", "collection": body["name"],
                    "collection_ids": [r["collection_id"] for r in held]})
            row = held[0] if held else next((r for r in self.rows if r["name"] == body["name"]), None)
            if row is None:
                return httpx.Response(200, json={"deleted": {}, "name": body["name"]})
        self.rows.remove(row)
        return httpx.Response(200, json={"deleted": {"chunks": 3, "catalog_collections": 1},
                                         "collection_id": row["collection_id"], "name": row["name"]})

    def catalog(self) -> HttpCatalogClient:
        cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
        cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(self.handle))
        return cat


def _run(runner, mock_db, engine: FakeEngine, args: list[str]):
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=engine.catalog()), \
         patch("nexus.db.service_endpoint.guard_production_write"), \
         patch("nexus.db.collection_purge._purge_pipeline_db", side_effect=lambda name, counts: counts) as pipeline:
        result = _invoke(runner, mock_db, ["delete", *args])
    result.pipeline = pipeline  # type: ignore[attr-defined]
    return result


def _flat(result) -> str:
    return " ".join(result.output.split())


# ── 1. the same collection by label and by id ───────────────────────────────────────────────────────────────────────


def test_delete_by_id_purges_the_same_collection_as_delete_by_its_label(runner, env_creds, mock_db) -> None:
    rows = [_row("knowledge__alpha", 9001), _row("knowledge__beta", 9002)]
    by_label, by_id = FakeEngine(rows), FakeEngine(rows)

    got_label = _run(runner, mock_db, by_label, ["knowledge__alpha", "--yes"])
    got_id = _run(runner, mock_db, by_id, ["--id", "9001", "--yes"])

    assert got_label.exit_code == 0, got_label.output
    assert got_id.exit_code == 0, got_id.output
    assert [r["name"] for r in by_label.rows] == [r["name"] for r in by_id.rows] == ["knowledge__beta"]
    assert by_id.posts() == [{"collection_id": 9001}], "the id goes out as a JSON integer and nothing else rides with it"
    assert by_label.posts() == [{"name": "knowledge__alpha"}], "the label form is unchanged on the wire"
    assert "Deleted: knowledge__alpha" in _flat(got_id) and "9001" in _flat(got_id), "the output names label and id"
    assert "3 chunks" in _flat(got_id) and "3 chunks" in _flat(got_label)


def test_delete_by_id_purges_the_pipeline_buffer_under_the_label_the_engine_returned(runner, env_creds, mock_db) -> None:
    """The pipeline purge is keyed by label; the CLI never looked the id up, so the label comes from the 200."""
    engine = FakeEngine([_row("knowledge__alpha", 9001)])
    result = _run(runner, mock_db, engine, ["--id", "9001", "--yes"])
    assert result.exit_code == 0, result.output
    assert [c.args[0] for c in result.pipeline.call_args_list] == ["knowledge__alpha"]


def test_delete_by_id_does_no_lookup_from_id_to_label(runner, env_creds, mock_db) -> None:
    """A rename between a lookup and the delete would delete whatever then holds the label: the id goes straight out."""
    engine = FakeEngine([_row("knowledge__alpha", 9001)])
    result = _run(runner, mock_db, engine, ["--id", "9001", "--yes"])
    assert result.exit_code == 0, result.output
    assert [(m, p) for m, p, _b in engine.requests if p.endswith("/collections/list")] == []
    assert {"name", "collection"}.isdisjoint(engine.posts()[0])


def test_the_confirmation_prompt_names_the_id_and_declining_sends_nothing(runner, env_creds, mock_db) -> None:
    engine = FakeEngine([_row("knowledge__alpha", 9001)])
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=engine.catalog()):
        result = _invoke(runner, mock_db, ["delete", "--id", "9001"], input="n\n")
    assert result.exit_code != 0
    assert "9001" in result.output
    assert engine.posts() == []


# ── 2. a label two live rows share ──────────────────────────────────────────────────────────────────────────────────


def test_a_label_shared_by_two_live_rows_is_refused_naming_both_ids_and_sends_nothing(runner, env_creds, mock_db) -> None:
    """The client computes the clash from the registry listing (``_label_clash``) before it sends anything."""
    engine = FakeEngine([_row("knowledge__twin", 7001), _row("knowledge__twin", 7002), _row("knowledge__other", 7003)])
    result = _run(runner, mock_db, engine, ["knowledge__twin", "--yes"])

    assert result.exit_code != 0
    out = _flat(result)
    assert "knowledge__twin" in out and "7001" in out and "7002" in out
    assert "7003" not in out
    assert "--id" in out, "the way out is the delete by id"
    assert "Deleted:" not in out
    assert engine.posts() == [], "no delete request is sent"
    assert len(engine.rows) == 3


def test_the_engines_own_label_clash_refusal_is_typed_too(runner, env_creds, mock_db) -> None:
    """The listing may be stale (a row was added between the read and the delete): the engine's 409
    ``collection_label_ambiguous`` is read by its reason and names the ids it names, never called unconfirmed."""
    engine = FakeEngine([_row("knowledge__twin", 7001)])
    real_handle = engine.handle

    def late_twin(request: httpx.Request) -> httpx.Response:
        if request.url.path == DELETE_PATH:
            engine.rows.append(_row("knowledge__twin", 7002))
        return real_handle(request)

    engine.handle = late_twin  # type: ignore[method-assign]
    result = _run(runner, mock_db, engine, ["knowledge__twin", "--yes"])
    out = _flat(result)
    assert result.exit_code != 0
    assert "7001" in out and "7002" in out
    assert "unconfirmed" not in out and "Deleted:" not in out


# ── 3. a superseded label ───────────────────────────────────────────────────────────────────────────────────────────


def test_a_superseded_label_is_refused_naming_the_successor_id_and_no_delete_is_sent(runner, env_creds, mock_db) -> None:
    engine = FakeEngine([_row("knowledge__old", 8001, superseded_by="knowledge__new"), _row("knowledge__new", 8002)])
    result = _run(runner, mock_db, engine, ["knowledge__old", "--yes"])

    assert result.exit_code != 0
    out = _flat(result)
    assert "knowledge__old" in out and "superseded" in out
    assert "knowledge__new" in out and "8002" in out, "the successor is named by label and id"
    assert "Deleted:" not in out
    assert engine.posts() == [], "no delete request is sent"
    assert [r["name"] for r in engine.rows] == ["knowledge__old", "knowledge__new"], "the tombstone stays"


def test_a_superseded_label_that_still_has_a_live_row_under_it_deletes_the_live_row(runner, env_creds, mock_db) -> None:
    """The engine takes the live row first when a label has both: only a label with NO live row is refused."""
    engine = FakeEngine([_row("knowledge__twin", 8101, superseded_by="knowledge__new"), _row("knowledge__twin", 8102),
                         _row("knowledge__new", 8103)])
    result = _run(runner, mock_db, engine, ["knowledge__twin", "--yes"])
    assert result.exit_code == 0, result.output
    assert engine.posts() == [{"name": "knowledge__twin"}]


def test_a_superseded_id_is_refused_by_the_engine_naming_the_successor(runner, env_creds, mock_db) -> None:
    engine = FakeEngine([_row("knowledge__old", 8001, superseded_by="knowledge__new"), _row("knowledge__new", 8002)])
    result = _run(runner, mock_db, engine, ["--id", "8001", "--yes"])
    out = _flat(result)
    assert result.exit_code != 0
    assert "8001" in out and "knowledge__new" in out and "8002" in out
    assert "unconfirmed" not in out and "Deleted:" not in out
    assert len(engine.rows) == 2


# ── 4. an unknown id ────────────────────────────────────────────────────────────────────────────────────────────────


def test_an_unknown_id_is_a_typed_error_not_a_silent_no_op(runner, env_creds, mock_db) -> None:
    engine = FakeEngine([_row("knowledge__alpha", 9001)])
    result = _run(runner, mock_db, engine, ["--id", "424242", "--yes"])
    out = _flat(result)
    assert result.exit_code != 0
    assert "no collection with id 424242" in out
    assert "unconfirmed" not in out and "Deleted:" not in out
    assert len(engine.rows) == 1


# ── 5. usage ────────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("args", [["knowledge__alpha", "--id", "9001", "--yes"], ["--yes"], ["--id", "0", "--yes"],
                                  ["--id", "nine", "--yes"]])
def test_a_label_with_an_id_neither_or_a_bad_id_is_a_usage_error_and_sends_nothing(runner, env_creds, mock_db, args) -> None:
    engine = FakeEngine([_row("knowledge__alpha", 9001)])
    result = _run(runner, mock_db, engine, args)
    assert result.exit_code == 2, result.output
    assert "Usage" in result.output or "Error" in result.output
    assert engine.requests == [], "a usage error is refused before any request"


def test_the_usage_error_for_both_names_the_two_forms(runner, env_creds, mock_db) -> None:
    result = _run(runner, mock_db, FakeEngine([]), ["knowledge__alpha", "--id", "9001", "--yes"])
    assert "either a label or --id" in _flat(result).lower() or "not both" in _flat(result).lower()


# ── 6. an engine without the flag ───────────────────────────────────────────────────────────────────────────────────


def test_id_on_an_engine_without_the_flag_refuses_clearly_and_sends_no_delete(runner, env_creds, mock_db) -> None:
    engine = FakeEngine([_row("knowledge__alpha", 9001)], flag=False, release="0.1.150")
    result = _run(runner, mock_db, engine, ["--id", "9001", "--yes"])
    out = _flat(result)
    assert result.exit_code != 0
    assert FLAG in out and "0.1.150" in out and "0.1.202" in out, "the refusal names the flag, the engine and the floor"
    assert "nx collection delete <label>" in out or "delete by label" in out.lower()
    assert engine.posts() == []
    assert not any(p.endswith("/collections/list") for _m, p, _b in engine.requests), "no id-to-label fallback"


def test_id_when_the_version_probe_fails_refuses_rather_than_guessing(runner, env_creds, mock_db) -> None:
    engine = FakeEngine([_row("knowledge__alpha", 9001)])
    real_handle = engine.handle

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(500, json={"error": "boom"})
        return real_handle(request)

    engine.handle = broken  # type: ignore[method-assign]
    result = _run(runner, mock_db, engine, ["--id", "9001", "--yes"])
    assert result.exit_code != 0
    assert "could not confirm" in _flat(result)
    assert engine.posts() == []


def test_the_flag_is_checked_before_the_confirmation_prompt(runner, env_creds, mock_db) -> None:
    engine = FakeEngine([_row("knowledge__alpha", 9001)], flag=False)
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=engine.catalog()):
        result = _invoke(runner, mock_db, ["delete", "--id", "9001"], input="")
    assert result.exit_code != 0
    assert "Delete collection" not in result.output, "a refusal that is certain comes before the question"


# ── the positional form keeps working ───────────────────────────────────────────────────────────────────────────────


def test_the_label_form_still_deletes_an_ordinary_collection(runner, env_creds, mock_db) -> None:
    engine = FakeEngine([_row("knowledge__alpha", 9001)])
    result = _run(runner, mock_db, engine, ["knowledge__alpha", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Deleted: knowledge__alpha" in _flat(result)
    assert engine.rows == []


def test_the_label_form_proceeds_when_the_registry_listing_cannot_be_read(runner, env_creds, mock_db) -> None:
    """The listing is a pre-check; the engine is the authority. An unreadable listing is said on stderr and the delete
    goes on (a name with no registry row, a T3 orphan, is not in the listing at all and must still be deletable)."""
    engine = FakeEngine([_row("knowledge__alpha", 9001)])
    real_handle = engine.handle

    def no_list(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/collections/list"):
            return httpx.Response(503, json={"error": "busy"})
        return real_handle(request)

    engine.handle = no_list  # type: ignore[method-assign]
    result = _run(runner, mock_db, engine, ["knowledge__alpha", "--yes"])
    assert result.exit_code == 0, result.output
    assert "registry listing" in result.output
    assert engine.posts() == [{"name": "knowledge__alpha"}]


# ── the pure pieces ─────────────────────────────────────────────────────────────────────────────────────────────────


def _status_error(status: int, body: dict) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://mock.test" + DELETE_PATH)
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, json=body, request=request))


def test_refusal_of_reads_status_and_reason_never_the_status_alone() -> None:
    assert isinstance(refusal_of(_status_error(404, {"reason": "unknown_collection_id", "collection_id": 5})), UnknownCollectionIdError)
    assert isinstance(refusal_of(_status_error(409, {"reason": "collection_label_ambiguous", "collection": "x",
                                                     "collection_ids": [1, 2]})), AmbiguousCollectionLabelError)
    sup = refusal_of(_status_error(409, {"reason": "collection_superseded", "collection": "x", "collection_id": 1,
                                         "superseded_by": "y", "successor_collection_id": 2}))
    assert isinstance(sup, SupersededCollectionDeleteError) and (sup.successor, sup.successor_id) == ("y", 2)
    # a 409 that is another reason, a 404 that is not the id reason, and a body missing what the error names, are not these errors
    assert refusal_of(_status_error(409, {"reason": "collection_busy", "collection": "x", "job_id": "j"})) is None
    assert refusal_of(_status_error(404, {"reason": "something_else"})) is None
    assert refusal_of(_status_error(409, {"reason": "collection_label_ambiguous", "collection": "x"})) is None
    assert refusal_of(RuntimeError("no response")) is None


def test_a_chain_of_tombstones_names_the_live_end_of_the_chain() -> None:
    rows = [_row("a", 1, superseded_by="b"), _row("b", 2, superseded_by="c"), _row("c", 3)]
    with pytest.raises(SupersededCollectionDeleteError) as caught:
        check_label_deletable(rows, "a")
    assert (caught.value.successor, caught.value.successor_id) == ("c", 3)
    check_label_deletable(rows, "c")  # a live label passes
    check_label_deletable(rows, "no-row-at-all")  # a T3 orphan with no registry row is still deletable


def test_a_cycle_of_tombstones_ends_without_a_successor_instead_of_looping() -> None:
    rows = [_row("a", 1, superseded_by="b"), _row("b", 2, superseded_by="a")]
    with pytest.raises(SupersededCollectionDeleteError) as caught:
        check_label_deletable(rows, "a")
    assert caught.value.successor_id is None and "no live successor" in str(caught.value)
