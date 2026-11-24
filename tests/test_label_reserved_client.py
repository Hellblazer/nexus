# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.7 (engine 0.1.202): a label the engine reserves (409 ``label_reserved``).

After the one-row rename A to B, B's rows still carry the name A until P4.1, so A is reserved: a register, revive, mint,
supersede successor, rename target or per-collection import of A answers 409 ``label_reserved`` with ``{error, reason,
label}`` and writes nothing. The normal way to meet it is rename A to B, then index the repo that was A (the index run
registers A again). The client turns the 409 into ONE message (the label, why, the two ways out), never retries, and
shows no traceback. These tests run a real ``HttpCatalogClient`` over an ``httpx.MockTransport``.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import click
import httpx
import pytest
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from nexus.collection_errors import LabelReservedError
from nexus.commands.collection import _reembed_job_error
from nexus.db.collection_state import CollectionState
from nexus.db.http_vector_client import VectorServiceError
from nexus.indexer import _register_run_collections
from tests._cli_tree import missing_commands, named_commands

LABEL = "docs__papers__bge-base-en-v15-768__v1"


def _reserved_body(label: str = LABEL) -> dict:
    return {"error": f"the label '{label}' is reserved: a collection renamed away from it still has rows that carry it, "
                     "and it can be used again once P4.1 drops the collection name columns (RDR-229)",
            "reason": "label_reserved", "label": label}


def _engine(requests: list, *, refuse: tuple[str, ...], status: int = 409, body: dict | None = None) -> HttpCatalogClient:
    """A catalog client whose transport refuses the POST routes in *refuse* with the engine's 409 and answers every
    registry read as an unregistered name (404). Every request is recorded as ``(method, path, body)``."""
    answer = body if body is not None else _reserved_body()

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, json.loads(request.content) if request.content else {}))
        if request.method == "POST" and request.url.path in refuse:
            return httpx.Response(status, json=answer)
        return httpx.Response(404, json={"error": "not found"})

    cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
    cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handle))
    return cat


def _assert_the_one_message(text: str, label: str = LABEL) -> None:
    out = " ".join(text.split())
    assert f"the collection label '{label}' is reserved" in out, out
    assert "renamed away from it still has rows that carry it" in out and "P4.1" in out, out
    assert "Nothing was written" in out, out
    assert "Pick another label" in out and f"rename that collection back to '{label}'" in out, out
    assert "Traceback" not in text and "HTTPStatusError" not in text
    assert "retry" not in out.lower(), "a retry cannot help: the engine refuses the label whole"
    assert named_commands(out) and missing_commands(out) == [], out  # the command it names exists


@pytest.fixture(autouse=True)
def _no_prod_write_guard():
    with patch("nexus.db.service_endpoint.guard_production_write"):
        yield


# ── the typed error ──────────────────────────────────────────────────────────


def test_a_409_label_reserved_is_the_typed_error_from_every_catalog_post_and_is_sent_once() -> None:
    """The conversion is in the shared transport, so every route that names a label answers the same way."""
    routes = {
        "register": ("/v1/catalog/collections/upsert", lambda c: c.register_collection(
            LABEL, content_type="docs", owner_id="papers", embedding_model="bge-base-en-v15-768", model_version="v1")),
        "mint": ("/v1/catalog/collections/upsert", lambda c: c.register_collection(
            content_type="docs", owner_id="papers", embedding_model="bge-base-en-v15-768", model_version="v1")),
        "supersede": ("/v1/catalog/collections/supersede", lambda c: c.supersede_collection("docs__old", LABEL)),
        "rename": ("/v1/catalog/collections/rename", lambda c: c.rename_collection_cascade("docs__old", LABEL)),
    }
    for name, (path, call) in routes.items():
        requests: list = []
        with pytest.raises(LabelReservedError) as raised:
            call(_engine(requests, refuse=(path,)))
        assert raised.value.label == LABEL, name
        assert [p for m, p, _b in requests if m == "POST"] == [path], f"{name}: sent once, never resent"
        _assert_the_one_message(str(raised.value))


def test_a_409_with_another_reason_is_not_taken_for_a_reserved_label() -> None:
    body = {"error": "collection 'x' is held by 2 live rows", "reason": "collection_label_ambiguous", "collection": "x"}
    with pytest.raises(httpx.HTTPStatusError):
        _engine([], refuse=("/v1/catalog/collections/rename",), body=body).rename_collection_cascade("docs__old", LABEL)


# ── nx collection rename ─────────────────────────────────────────────────────


def test_rename_to_a_reserved_label_prints_the_one_message_exits_nonzero_and_does_not_retry(monkeypatch) -> None:
    requests: list = []
    label = "docs__papers"
    cat = _engine(requests, refuse=("/v1/catalog/collections/rename",), body=_reserved_body(label))
    monkeypatch.setenv("NX_LOCAL", "1")
    states = {"docs__papers_old": CollectionState.PRESENT, label: CollectionState.ABSENT}
    with patch("nexus.catalog.factory.make_catalog_reader", return_value=cat), \
         patch("nexus.commands.collection._t3", return_value=object()), \
         patch("nexus.collection_rename.probe_collection_state", side_effect=lambda _db, name: states[name]):
        result = CliRunner().invoke(main, ["collection", "rename", "docs__papers_old", label])
    assert result.exit_code != 0, result.output
    _assert_the_one_message(result.output, label)
    assert "service rename failed" not in result.output and "Fix the error" not in result.output
    assert [p for m, p, _b in requests if m == "POST" and "rename" in p] == ["/v1/catalog/collections/rename"]


# ── the index run's registration (rename A to B, then index the repo that was A) ──


def test_the_index_runs_registration_of_a_reserved_label_stops_the_run_with_the_one_message(monkeypatch) -> None:
    requests: list = []
    monkeypatch.setattr("nexus.corpus._profile_model_for_content_type", lambda content_type: None)
    monkeypatch.setattr("nexus.corpus._registration_cache_add", lambda *a, **k: pytest.fail("a refused label is never cached"))
    # the run closes each writer it takes, so every call gets its own client over the one recorded transport
    with patch("nexus.catalog.factory.make_catalog_writer",
               side_effect=lambda **_k: _engine(requests, refuse=("/v1/catalog/collections/upsert",))):
        with pytest.raises(LabelReservedError) as raised:
            _register_run_collections(((LABEL, "bge-base-en-v15-768"),))
    _assert_the_one_message(str(raised.value))
    assert [p for m, p, _b in requests if m == "POST"] == ["/v1/catalog/collections/upsert"], "one attempt, no race reading"


def test_the_cli_boundary_renders_a_reserved_label_as_a_usage_error_not_a_traceback() -> None:
    """An index command surfaces the typed error through ``nexus.cli``'s boundary: exit 1, the message, no traceback."""
    @click.command("zz-register")
    def zz_register() -> None:
        raise LabelReservedError(LABEL)

    main.add_command(zz_register)
    try:
        result = CliRunner().invoke(main, ["zz-register"])
    finally:
        main.commands.pop("zz-register", None)
    assert result.exit_code == 1, result.output
    _assert_the_one_message(result.output)
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_a_reembed_target_label_the_engine_reserves_is_the_one_message_not_the_engines_bare_text() -> None:
    """``nx collection re-embed`` starts a job whose target label the engine registers; 409 ``label_reserved`` names
    it. The vector client carries the body and reason; the command renders the same message and exits nonzero."""
    exc = VectorServiceError("HTTP 409", code=409, reason="label_reserved", engine_body=_reserved_body())
    err = _reembed_job_error(exc)
    _assert_the_one_message(err.message)
    other = _reembed_job_error(VectorServiceError("HTTP 409", code=409, engine_body={"error": "job 1 holds it", "job_id": "1"}))
    assert "reserved" not in other.message
