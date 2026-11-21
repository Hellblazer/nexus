# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 254-P2-S3: the Python catalog client's move wire methods, the capability probe and the refusal floor.

Wire-shape tests on an ``httpx.MockTransport`` (the engine side is pinned Java-side by CatalogDocumentMoveTest,
CatalogOwnerMoveTest and CatalogMoveRoutesTest), a probe table, and the refusal every move command calls before any
route call. The command over a mock engine is in tests/commands/test_catalog_owners_move.py, and the substrate leg of the
same wire is in tests/integration/test_catalog_owners_move_journey.py."""
from __future__ import annotations

import json

import httpx
import pytest

from nexus.catalog import catalog_protocol
from nexus.catalog.catalog_protocol import CATALOG_WRITE_OPS
from nexus.catalog.engine_writes import floors
from nexus.catalog.engine_writes.floors import CatalogMoveUnsupportedError, require_catalog_move
from nexus.catalog.factory import _ServiceCatalogWriter
from nexus.catalog.http_catalog_client import HttpCatalogClient


def _client(handler) -> HttpCatalogClient:
    cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
    cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handler))
    return cat


_REPORT = {
    "dry_run": True, "repointed": 1, "absorbed": 1, "trashed": 0, "skipped": 0, "unchanged": 0,
    "aspect_rows_rewritten": 2, "aspect_rows_deleted": 0, "highlight_rows_rewritten": 0, "highlight_rows_deleted": 0,
    "links_remapped": 1, "links_collapsed": 0, "links_dropped": 0, "absorbed_tumblers": ["1.2.9"],
}


def _recording(posts: list, reply: dict | None = None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        posts.append((request.url.path, json.loads(request.content)))
        return httpx.Response(status, json=reply if reply is not None else _REPORT)

    return handler


# ── the floor ───────────────────────────────────────────────────────────────


def test_refusal_says_not_yet_released_while_floor_is_none(monkeypatch) -> None:
    monkeypatch.setattr(floors, "CATALOG_MOVE_MIN_ENGINE_VERSION", None)  # pinned: 254-P5-S1 fills the real one
    cat = _client(lambda r: httpx.Response(200, json={"release_version": "0.1.150"}))
    with pytest.raises(CatalogMoveUnsupportedError) as exc:
        require_catalog_move(cat, what="nx catalog owners --execute move")
    msg = str(exc.value)
    assert isinstance(exc.value, RuntimeError)
    assert "not yet released" in msg and "RDR-254" in msg and "nx catalog owners --execute move" in msg
    assert "Nothing was written" in msg


def test_refusal_names_engine_version_when_flag_absent() -> None:
    """The old-engine refusal, against a flag-less /version: the engine answered, so the message names it."""
    cat = _client(lambda r: httpx.Response(200, json={"release_version": "0.1.150"}))
    with pytest.raises(CatalogMoveUnsupportedError) as exc:
        require_catalog_move(cat)
    msg = str(exc.value)
    assert "0.1.150" in msg and "minimum engine version" in msg
    assert "could not confirm" not in msg, "the engine answered; this is not an unknown"


def test_refusal_names_the_floor_once_it_is_set(monkeypatch) -> None:
    monkeypatch.setattr(floors, "CATALOG_MOVE_MIN_ENGINE_VERSION", (0, 1, 199))
    cat = _client(lambda r: httpx.Response(200, json={"release_version": "0.1.150"}))
    with pytest.raises(CatalogMoveUnsupportedError) as exc:
        require_catalog_move(cat)
    assert "0.1.199" in str(exc.value) and "not yet released" not in str(exc.value)


def test_the_move_floor_is_the_engine_release_that_carries_the_routes() -> None:
    """254-P5-S1: engine-service-v0.1.199 carries both move routes and the catalog_move_supported flag (P1,
    5476be38d..60f7d84da), so an older engine's refusal names it."""
    assert floors.CATALOG_MOVE_MIN_ENGINE_VERSION == (0, 1, 199)
    cat = _client(lambda r: httpx.Response(200, json={"release_version": "0.1.150"}))
    with pytest.raises(CatalogMoveUnsupportedError) as exc:
        require_catalog_move(cat)
    assert "minimum engine version: 0.1.199" in str(exc.value)


def test_the_helper_passes_when_the_engine_advertises_the_flag() -> None:
    require_catalog_move(_client(lambda r: httpx.Response(200, json={"catalog_move_supported": True})))


def test_probe_counts_only_json_true() -> None:
    def make(body):
        return _client(lambda r: httpx.Response(200, json=body))

    assert make({"catalog_move_supported": True}).supports_catalog_move() is True
    for not_true in ("true", 1, "yes", None, [True], {}):
        assert make({"catalog_move_supported": not_true}).supports_catalog_move() is False, repr(not_true)
        with pytest.raises(CatalogMoveUnsupportedError):
            require_catalog_move(make({"catalog_move_supported": not_true, "release_version": "0.1.150"}))
    assert make({}).supports_catalog_move() is False
    assert make({"catalog_sensitivity_supported": True}).supports_catalog_move() is False, "its own flag"
    assert make({"catalog_move_supported": True, "release_version": "0.1.199"}).catalog_move_probe() == (True, "0.1.199")
    assert make({"app_version": "9"}).catalog_move_probe() == (False, "9")


def test_unknown_probe_answer_refuses() -> None:
    """A failed probe, a transport error and a handle with no probe are unknowns, and an unknown refuses."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    for cat in (_client(lambda r: httpx.Response(500, json={})), _client(boom)):
        assert cat.supports_catalog_move() is False
        with pytest.raises(CatalogMoveUnsupportedError, match="could not confirm"):
            require_catalog_move(cat)

    class NoProbe:
        pass

    with pytest.raises(CatalogMoveUnsupportedError, match="could not confirm"):
        require_catalog_move(NoProbe())

    class RaisingProbe:
        def catalog_move_probe(self):
            raise RuntimeError("no")

    with pytest.raises(CatalogMoveUnsupportedError, match="could not confirm"):
        require_catalog_move(RaisingProbe())


# ── the wire ────────────────────────────────────────────────────────────────


def test_move_document_posts_the_route_body_and_returns_the_report() -> None:
    posts: list = []
    out = _client(_recording(posts)).move_document(
        "1.2.3", new_source_uri="file:///new/x.pdf", new_file_path="/new/x.pdf", dry_run=True)
    assert posts == [("/v1/catalog/documents/move", {
        "tumbler": "1.2.3", "new_source_uri": "file:///new/x.pdf", "new_file_path": "/new/x.pdf", "dry_run": True})]
    assert out == _REPORT


def test_move_owner_posts_the_route_body_and_returns_the_report() -> None:
    posts: list = []
    reply = {**_REPORT, "dry_run": False, "owners_folded": 1}
    out = _client(_recording(posts, reply)).move_owner(
        "1.5", new_root="/new/repo", new_repo_hash="abcd1234", dry_run=False)
    assert posts == [("/v1/catalog/owners/move", {
        "owner": "1.5", "new_root": "/new/repo", "new_repo_hash": "abcd1234", "dry_run": False})]
    assert out["owners_folded"] == 1


def test_dry_run_is_required_and_must_be_a_bool_before_any_request() -> None:
    seen: list = []
    cat = _client(lambda r: seen.append(r) or httpx.Response(200, json=_REPORT))
    with pytest.raises(TypeError):
        cat.move_document("1.2.3", new_source_uri="file:///n", new_file_path="/n")  # type: ignore[call-arg]
    for bad in ("true", 1, None):
        with pytest.raises(TypeError, match="dry_run"):
            cat.move_document("1.2.3", new_source_uri="file:///n", new_file_path="/n", dry_run=bad)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="dry_run"):
            cat.move_owner("1.5", new_root="/n", new_repo_hash="h", dry_run=bad)  # type: ignore[arg-type]
    assert seen == []


def test_blank_arguments_are_refused_before_any_request() -> None:
    seen: list = []
    cat = _client(lambda r: seen.append(r) or httpx.Response(200, json=_REPORT))
    with pytest.raises(ValueError, match="new_file_path"):
        cat.move_document("1.2.3", new_source_uri="file:///n", new_file_path=" ", dry_run=True)
    with pytest.raises(ValueError, match="new_repo_hash"):
        cat.move_owner("1.5", new_root="/n", new_repo_hash="", dry_run=True)
    assert seen == []


def _call(cat: HttpCatalogClient, which: str) -> None:
    if which == "document":
        cat.move_document("1.2.3", new_source_uri="file:///new/x.pdf", new_file_path="/new/x.pdf", dry_run=False)
    else:
        cat.move_owner("1.5", new_root="/new", new_repo_hash="h", dry_run=False)


@pytest.mark.parametrize("which", ["document", "owner"])
def test_a_refusal_is_a_409_the_caller_sees(which: str) -> None:
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(409, json={"error": "another live row holds file:///new/x.pdf"})

    with pytest.raises(httpx.HTTPStatusError) as exc:
        _call(_client(handler), which)
    assert exc.value.response.status_code == 409 and len(calls) == 1


@pytest.mark.parametrize("which", ["document", "owner"])
@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.ConnectError("reset")])
def test_a_write_that_fails_in_transport_is_not_resent(which: str, failure: Exception) -> None:
    """The write's reply can be lost after the engine acted, so the transport must make exactly ONE attempt:
    a resend could report different counts or re-fold. The handler raises on call 1 and a second call would be
    visible, so this goes red if ``idempotent=False`` is dropped."""
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise failure

    with pytest.raises(type(failure)):
        _call(_client(handler), which)
    assert len(calls) == 1, calls


def test_the_transport_never_resends_a_move_and_a_dry_run_is_not_a_production_write() -> None:
    cat = _client(lambda r: httpx.Response(200, json=_REPORT))
    seen: list[dict] = []
    real = cat._post

    def spy(path, body=None, **kw):
        seen.append(kw)
        return real(path, body, **kw)

    cat._post = spy  # type: ignore[method-assign]
    cat.move_document("1.2.3", new_source_uri="file:///n", new_file_path="/n", dry_run=True)
    cat.move_document("1.2.3", new_source_uri="file:///n", new_file_path="/n", dry_run=False)
    assert [k["idempotent"] for k in seen] == [False, False]
    assert [k["mutates"] for k in seen] == [False, True], "only the write rides the production-write guard"
    assert all(k["timeout"] > 30 for k in seen), "an owner fold outlasts the 30 s client default"


def test_a_reply_that_is_not_an_object_is_refused() -> None:
    cat = _client(lambda r: httpx.Response(200, json=[1]))
    with pytest.raises(RuntimeError, match="not a JSON object"):
        cat.move_document("1.2.3", new_source_uri="file:///n", new_file_path="/n", dry_run=True)


# ── the write whitelist (the set_sensitivity shape) ─────────────────────────


def test_the_move_methods_are_whitelisted_write_ops_and_protocol_methods() -> None:
    for op in ("move_document", "move_owner"):
        assert op in CATALOG_WRITE_OPS
        assert hasattr(catalog_protocol.CatalogWriter, op)


def test_service_writer_proxy_exposes_move_methods(monkeypatch) -> None:
    cat = _client(lambda r: httpx.Response(200, json=_REPORT))
    proxy = _ServiceCatalogWriter(cat)
    assert callable(proxy.move_document) and callable(proxy.move_owner)
    assert proxy.move_document("1.2.3", new_source_uri="file:///n", new_file_path="/n", dry_run=True) == _REPORT
    monkeypatch.setattr(
        "nexus.catalog.factory.CATALOG_WRITE_OPS", tuple(op for op in CATALOG_WRITE_OPS if op != "move_owner"))
    with pytest.raises(AttributeError, match="move_owner"):
        _ServiceCatalogWriter(cat).move_owner
