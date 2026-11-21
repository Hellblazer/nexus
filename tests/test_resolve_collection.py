# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.2 (nexus-p2m1x.7): ``resolve_collection`` and ``find_collection``.

``resolve_collection(ref)`` answers a label or a collection id with one of three
outcomes (Technical Design item 1), the three the write paths tell apart today:

- ``Row``: the registry row answers, as a ``CollectionAttributes``;
- ``NoRow``: no row backs the name (the cache also omits a collection with no chunk yet);
- ``Unreadable``: the read failed. It is never folded into ``NoRow``: a write that meets
  it answers with the name too, flagged, so the unchanged-content skip does not judge a
  stored stamp against that guess (``doc_indexer._target_model``, nexus-s8r5o).

A Row whose ``model_version`` is blank (53 live rows until D2 completes) takes the same
conformant-name fallback ``collection_for`` uses, with the same warning; under a
non-conformant name it reads as Unreadable for the model.
"""
from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import httpx
import pytest
from structlog.testing import capture_logs

import nexus.mcp_infra as mcp_infra
from nexus.catalog.collection_name import CollectionAttributes
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.rows import resolve as resolve_mod
from nexus.catalog.rows.resolve import (
    NoRow,
    Row,
    Unreadable,
    embedding_model_for,
    find_collection,
    resolve_collection,
)
from nexus.embed_window import window_for_collection
from nexus.mcp_infra import ServiceNotReadyError

pytestmark = pytest.mark.usefixtures("cloud_mode")

_NAME = "code__1-1__bge-base-en-v15-768__v2"
_BGE = "bge-base-en-v15-768"


def _row(**over: object) -> dict:
    return {
        "content_type": "code", "owner_id": "1-1", "embedding_model": _BGE,
        "lifecycle_state": "live", **over,
    }


def _resolver(rows: dict[str, dict | None]) -> Callable[[str], dict | None]:
    return lambda name: rows.get(name)


# -- the three outcomes ---------------------------------------------------------


def test_a_row_answers_as_collection_attributes() -> None:
    out = resolve_collection(_NAME, row_resolver=_resolver({_NAME: _row()}))
    assert out == Row(CollectionAttributes(
        collection_id=None, label=_NAME, content_type="code", owner_id="1-1",
        embedding_model=_BGE, model_version=None, dimension=None, lifecycle_state="live",
    ))


def test_a_row_carrying_the_extra_columns_hands_them_through() -> None:
    row = _row(collection_id=17, model_version="v3", dimension=768)
    out = resolve_collection(_NAME, row_resolver=_resolver({_NAME: row}))
    assert isinstance(out, Row)
    got = out.attributes
    assert (got.collection_id, got.model_version, got.dimension) == (17, 3, 768)
    assert got.label == _NAME


def test_the_row_is_the_authority_over_the_name() -> None:
    """The name says bge; the row says another model. The row answers."""
    out = resolve_collection(_NAME, row_resolver=_resolver({_NAME: _row(embedding_model="voyage-code-3")}))
    assert isinstance(out, Row) and out.attributes.embedding_model == "voyage-code-3"


def test_a_name_no_row_backs_is_no_row() -> None:
    out = resolve_collection("code__1-1__x__v1", row_resolver=_resolver({}))
    assert out == NoRow("code__1-1__x__v1")


def test_a_listing_that_fails_is_unreadable_never_no_row() -> None:
    boom = ServiceNotReadyError("the service is not answering")

    def resolver(name: str) -> dict | None:
        raise boom

    out = resolve_collection(_NAME, row_resolver=resolver)
    assert isinstance(out, Unreadable) and not isinstance(out, NoRow)
    assert out.ref == _NAME and out.reason == "listing" and out.cause is boom


def test_a_label_is_read_through_the_cache_strictly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default reader is the shared collections cache, asked strictly: a listing that failed
    re-raises, which is what makes Unreadable distinguishable from NoRow (nexus-s8r5o)."""
    seen: list[tuple[str, bool]] = []

    def fake(name: str, *, refresh: bool = True, strict: bool = False) -> dict | None:
        seen.append((name, strict))
        return _row()

    monkeypatch.setattr(mcp_infra, "get_collection_row", fake)
    assert isinstance(resolve_collection(_NAME), Row)
    assert seen == [(_NAME, True)]

    def failing(name: str, *, refresh: bool = True, strict: bool = False) -> dict | None:
        raise ServiceNotReadyError("down")

    monkeypatch.setattr(mcp_infra, "get_collection_row", failing)
    assert isinstance(resolve_collection(_NAME), Unreadable)


def test_a_non_strict_read_keeps_the_model_reads_legacy_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """``strict=False`` is what ``embedding_model_for``, ``embedding_model_row_preferred`` and the funnel helpers
    use: a fetch that cannot complete is Unreadable, which they treat as no row, not an exception into a write.
    A strict read is Unreadable for it too."""
    monkeypatch.setattr(mcp_infra, "get_collection_row", lambda name, *, refresh=True, strict=False: _row())
    assert isinstance(resolve_collection(_NAME, strict=False), Row)

    def boom(name: str, **kw: object) -> dict | None:
        raise ConnectionError("simulated unreachable service")

    monkeypatch.setattr(mcp_infra, "get_collection_row", boom)
    out = resolve_collection(_NAME, strict=False)
    assert isinstance(out, Unreadable) and isinstance(out.cause, ConnectionError)
    strict = resolve_collection(_NAME)
    assert isinstance(strict, Unreadable) and isinstance(strict.cause, ConnectionError)


def test_a_collection_id_finds_the_row_that_carries_it(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = {
        "alpha": _row(collection_id=3, content_type="docs"),
        "beta": _row(collection_id=9),
    }
    monkeypatch.setattr(
        mcp_infra, "get_collection_row_by_id",
        lambda cid, *, strict=False: next(({"name": n, **r} for n, r in rows.items() if r["collection_id"] == cid), None),
    )
    out = resolve_collection(9)
    assert isinstance(out, Row) and out.attributes.label == "beta" and out.attributes.collection_id == 9
    assert resolve_collection(404) == NoRow(404)


def test_a_collection_id_read_that_fails_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(cid: int, *, strict: bool = False) -> dict | None:
        raise ServiceNotReadyError("down")

    monkeypatch.setattr(mcp_infra, "get_collection_row_by_id", failing)
    assert isinstance(resolve_collection(1), Unreadable)


# -- a blank model_version ------------------------------------------------------


def test_a_blank_version_under_a_conformant_name_reads_the_names_version_and_warns() -> None:
    resolver = _resolver({_NAME: _row(model_version="")})
    with capture_logs() as logs:
        out = resolve_collection(_NAME, row_resolver=resolver)
    assert isinstance(out, Row) and out.attributes.model_version == 2
    warned = [e for e in logs if e["event"] == "collection_for_row_model_version_empty"]
    assert len(warned) == 1 and warned[0]["collection"] == _NAME and warned[0]["log_level"] == "warning"


def test_a_blank_version_under_a_non_conformant_name_is_unreadable_for_the_model() -> None:
    out = resolve_collection("notes-from-2024", row_resolver=_resolver({"notes-from-2024": _row(model_version="")}))
    assert isinstance(out, Unreadable) and out.reason == "model_version" and out.ref == "notes-from-2024"


def test_a_blank_version_under_a_v0_name_is_unreadable_for_the_model() -> None:
    name = "code__1-1__bge-base-en-v15-768__v0"
    out = resolve_collection(name, row_resolver=_resolver({name: _row(model_version="")}))
    assert isinstance(out, Unreadable) and out.reason == "model_version"


@pytest.mark.parametrize("bad", ["3", "v", "vx", "v0", "V2", None])
def test_a_malformed_version_is_unreadable_for_the_model(bad: object) -> None:
    out = resolve_collection(_NAME, row_resolver=_resolver({_NAME: _row(model_version=bad)}))
    assert isinstance(out, Unreadable) and out.reason == "model_version"


def test_a_reader_that_does_not_take_the_version_is_not_refused_over_it() -> None:
    """A diagnostic that reads a legacy slug's model has no use for its (blank) version."""
    resolver = _resolver({"notes-from-2024": _row(model_version="")})
    out = resolve_collection("notes-from-2024", row_resolver=resolver, need_version=False)
    assert isinstance(out, Row) and out.attributes.model_version is None
    assert out.attributes.embedding_model == _BGE


# -- the model read the P2.2 sites use -----------------------------------------


def test_the_embedding_model_is_the_rows_when_a_row_backs_the_name() -> None:
    rows = {_NAME: _row(embedding_model="voyage-code-3")}
    assert embedding_model_for(_NAME, row_resolver=_resolver(rows)) == "voyage-code-3"


def test_with_no_row_the_embedding_model_is_the_conformant_names_token() -> None:
    assert embedding_model_for(_NAME, row_resolver=_resolver({})) == _BGE


def test_with_no_row_a_legacy_name_gets_the_prefix_dispatch_model() -> None:
    assert embedding_model_for("code__legacy-abcd1234", row_resolver=_resolver({})) == "voyage-code-3"
    assert embedding_model_for("docs__legacy-abcd1234", row_resolver=_resolver({})) == "voyage-context-3"


def test_an_unreadable_listing_falls_to_the_name_as_no_row_does() -> None:
    def resolver(name: str) -> dict | None:
        raise ServiceNotReadyError("down")

    assert embedding_model_for(_NAME, row_resolver=resolver) == _BGE


def test_a_row_with_no_model_is_unreadable_and_the_read_path_then_answers_the_name_and_says_so() -> None:
    """The row is the source of the model, so a row without one is not a row to read it from."""
    row = _row()
    del row["embedding_model"]
    rows = {_NAME: row}
    out = resolve_collection(_NAME, row_resolver=_resolver(rows), need_version=False, need_model=True)
    assert isinstance(out, Unreadable) and out.reason == "embedding_model"
    for blank in (None, ""):
        rows = {_NAME: _row(embedding_model=blank)}
        out = resolve_collection(_NAME, row_resolver=_resolver(rows), need_version=False, need_model=True)
        assert isinstance(out, Unreadable) and out.reason == "embedding_model"
    # a reader that does not ask for the model is not refused over it
    assert isinstance(resolve_collection(_NAME, row_resolver=_resolver({_NAME: row}), need_version=False), Row)
    with capture_logs() as logs:
        assert embedding_model_for(_NAME, row_resolver=_resolver({_NAME: row})) == _BGE
    assert [e for e in logs if e["event"] == "embedding_model_row_unreadable" and e["reason"] == "embedding_model"]


def test_a_blank_version_under_a_non_canonical_model_token_is_unreadable_for_the_model() -> None:
    name = "code__1-1__stub-code-1024__v3"
    out = resolve_collection(name, row_resolver=_resolver({name: _row(embedding_model="stub-code-1024", model_version="")}))
    assert isinstance(out, Unreadable) and out.reason == "model_version"


# -- window_for_collection sizes from the row (handoff from P2.3) ---------------


def test_the_token_window_is_sized_by_the_rows_model_not_the_names_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The name says voyage (no window); the row says bge (512). A rename keeps the source row's model
    under the new name, so only the row tells what the engine embeds with."""
    asked: list[str] = []
    monkeypatch.setattr("nexus.embed_window.window_for_model", lambda model: asked.append(model) or None)
    name = "docs__1-1__voyage-context-3__v1"
    window_for_collection(name, "all-MiniLM-L6-v2", row_resolver=_resolver({name: _row(content_type="docs", embedding_model=_BGE)}))
    assert asked == [_BGE]


def test_the_token_window_of_a_legacy_name_with_a_row_is_the_rows_model(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr("nexus.embed_window.window_for_model", lambda model: asked.append(model) or None)
    name = "docs__legacy-abcd1234"
    window_for_collection(name, "voyage-context-3", row_resolver=_resolver({name: _row(content_type="docs")}))
    assert asked == [_BGE]


def test_the_token_window_with_no_row_follows_the_names_token_then_the_given_model(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr("nexus.embed_window.window_for_model", lambda model: asked.append(model) or None)
    window_for_collection(_NAME, "all-MiniLM-L6-v2", row_resolver=_resolver({}))
    window_for_collection("code__legacy-abcd1234", "voyage-code-3", row_resolver=_resolver({}))
    window_for_collection("", "voyage-code-3", row_resolver=_resolver({}))
    assert asked == [_BGE, "voyage-code-3", "voyage-code-3"]


# -- find_collection ------------------------------------------------------------


def _catalog(monkeypatch: pytest.MonkeyPatch, row: dict | None) -> HttpCatalogClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/catalog/collections/for_tuple":
            if row is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=row)
        return httpx.Response(200, json={})

    monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")
    client = HttpCatalogClient(base_url="http://mock.test")
    client._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handler))
    return client


def test_find_collection_answers_the_row_for_the_tuple_with_the_rows_own_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """The for_tuple row says v3 while its name's suffix says v1: the attributes take the row's."""
    cat = _catalog(monkeypatch, {
        "name": "code__1-1__bge-base-en-v15-768__v1", "content_type": "code", "owner_id": "1-1",
        "embedding_model": _BGE, "model_version": "v3",
    })
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: cat)
    got = find_collection("code", "1-1", _BGE)
    assert got is not None and got.model_version == 3 and got.embedding_model == _BGE


def test_find_collection_is_none_on_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: _catalog(monkeypatch, None))
    assert find_collection("code", "1-1", _BGE) is None


def test_find_collection_closes_the_reader_it_built(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = MagicMock()
    reader.find_collection.return_value = None
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: reader)
    assert find_collection("knowledge", "delos", "voyage-context-3") is None
    reader.find_collection.assert_called_once_with("knowledge", "delos", "voyage-context-3")
    reader.close.assert_called_once_with()


def test_find_collection_lets_a_failed_read_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller never takes an unreadable registry for an empty one and mints over it."""
    reader = MagicMock()
    reader.find_collection.side_effect = RuntimeError("catalog down")
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: reader)
    with pytest.raises(RuntimeError, match="catalog down"):
        find_collection("code", "1-1", _BGE)
    reader.close.assert_called_once_with()


def test_the_subject_finder_the_writers_pass_is_find_collection() -> None:
    import nexus.corpus as corpus

    assert corpus.catalog_subject_finder() is resolve_mod.find_collection


# -- one read path: the funnel helpers and resolve_row_preferred ----------------


def test_the_funnel_helpers_and_resolve_row_preferred_read_through_resolve_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """RDR-229 item 1: no second reader. All four go through ``resolve_collection``, non-strict and
    without a version, and a row_resolver reaches it."""
    import nexus.corpus as corpus

    asked: list[dict] = []
    attrs = CollectionAttributes(
        collection_id=None, label="docs__x", content_type="docs", owner_id="1-1",
        embedding_model=_BGE, model_version=None, dimension=None, lifecycle_state=None,
    )

    raw = {"content_type": "docs", "owner_id": "1-1", "embedding_model": _BGE}

    def spy(ref, **kw):
        asked.append({"ref": ref, **kw})
        return Row(attrs, raw=raw)

    monkeypatch.setattr(resolve_mod, "resolve_collection", spy)
    resolver = _resolver({})
    assert corpus.collection_content_type("docs__x") == "docs"
    assert corpus.collection_owner("docs__x") == "1-1"
    assert corpus.collection_model("docs__x") == _BGE
    assert corpus.resolve_row_preferred("docs__x", "embedding_model", lambda n: "name", row_resolver=resolver) == _BGE
    assert len(asked) == 4
    assert all(a["need_version"] is False and a["strict"] is False for a in asked)
    assert asked[3]["row_resolver"] is resolver


def test_a_row_that_exists_but_lacks_the_column_is_none_not_the_name_fallback() -> None:
    """resolve_row_preferred's tolerance of a partial row, kept: row presence gates the fallback."""
    import nexus.corpus as corpus

    partial = {"content_type": "docs"}
    got = corpus.resolve_row_preferred(
        "docs__x", "embedding_model", lambda n: "from-name", row_resolver=_resolver({"docs__x": partial}),
    )
    assert got is None


def test_the_funnel_helpers_raise_not_registered_for_no_row_and_for_an_unreadable_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.corpus as corpus

    monkeypatch.setattr(mcp_infra, "get_collection_row", lambda name, *, refresh=True, strict=False: None)
    with pytest.raises(corpus.CollectionNotRegisteredError, match="collection_model"):
        corpus.collection_model("docs__missing")

    def boom(name: str, *, refresh: bool = True, strict: bool = False) -> dict | None:
        raise ConnectionError("down")

    monkeypatch.setattr(mcp_infra, "get_collection_row", boom)
    with pytest.raises(corpus.CollectionNotRegisteredError, match="collection_owner"):
        corpus.collection_owner("docs__missing")


# -- review round 1 ---------------------------------------------------------------


def test_strict_reaches_a_resolver_that_takes_it_so_a_failed_listing_is_unreadable_not_no_row() -> None:
    """The per-tenant reader (HttpVectorClient._resolve_collection_row) answers None for a failed
    listing unless asked strictly. Under strict it is asked, so the failure is never NoRow."""
    seen: list[dict] = []

    def reader(name: str, *, strict: bool = False) -> dict | None:
        seen.append({"strict": strict})
        if strict:
            raise ServiceNotReadyError("listing failed")
        return None  # the lenient answer to a failed listing

    out = resolve_collection(_NAME, row_resolver=reader)
    assert isinstance(out, Unreadable) and out.reason == "listing" and isinstance(out.cause, ServiceNotReadyError)
    assert seen == [{"strict": True}]
    assert resolve_collection(_NAME, row_resolver=reader, strict=False) == NoRow(_NAME)


def test_a_resolver_that_takes_no_strict_keyword_is_called_with_the_name_alone() -> None:
    assert resolve_collection(_NAME, row_resolver=lambda n: None) == NoRow(_NAME)


@pytest.mark.parametrize("bug", [TypeError("x"), AttributeError("x"), KeyError("x")])
@pytest.mark.parametrize("strict", [True, False])
def test_a_programming_error_in_a_reader_raises_and_is_never_an_outcome(monkeypatch: pytest.MonkeyPatch, bug, strict) -> None:
    def resolver(name: str) -> dict | None:
        raise bug

    with pytest.raises(type(bug)):
        resolve_collection(_NAME, row_resolver=resolver, strict=strict)

    def cache(name: str, *, refresh: bool = True, strict: bool = False) -> dict | None:
        raise bug

    monkeypatch.setattr(mcp_infra, "get_collection_row", cache)
    with pytest.raises(type(bug)):
        resolve_collection(_NAME, strict=strict)


def test_an_unreadable_listing_has_its_own_funnel_message_and_chains_the_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.corpus as corpus

    cause = ConnectionError("down")

    def boom(name: str, *, refresh: bool = True, strict: bool = False) -> dict | None:
        raise cause

    monkeypatch.setattr(mcp_infra, "get_collection_row", boom)
    with pytest.raises(corpus.CollectionNotRegisteredError) as exc:
        corpus.collection_owner("docs__x")
    assert exc.value.__cause__ is cause
    assert "could not be read" in str(exc.value) and "unregistered" not in str(exc.value)
    monkeypatch.setattr(mcp_infra, "get_collection_row", lambda name, *, refresh=True, strict=False: None)
    with pytest.raises(corpus.CollectionNotRegisteredError, match="unregistered"):
        corpus.collection_owner("docs__x")


def test_resolve_row_preferred_keeps_is_not_none_semantics() -> None:
    import nexus.corpus as corpus

    empty = {"content_type": "docs", "embedding_model": ""}
    got = corpus.resolve_row_preferred("docs__x", "embedding_model", lambda n: "name", row_resolver=_resolver({"docs__x": empty}))
    assert got == ""
    assert corpus.resolve_row_preferred("docs__x", "owner_id", lambda n: "name", row_resolver=_resolver({"docs__x": empty})) is None


def test_collection_owner_on_a_row_without_an_owner_raises_key_error_as_it_always_did(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.corpus as corpus

    monkeypatch.setattr(
        mcp_infra, "get_collection_row", lambda name, *, refresh=True, strict=False: {"content_type": "docs"},
    )
    assert corpus.collection_content_type("docs__x") == "docs"
    with pytest.raises(KeyError):
        corpus.collection_owner("docs__x")
    with pytest.raises(KeyError):
        corpus.collection_model("docs__x")


# -- the write read: a failed listing answers the name, flagged -----------------------


def test_the_write_model_is_the_rows_and_known_when_a_row_backs_the_name() -> None:
    import nexus.corpus as corpus

    name = "docs__x__voyage-context-3__v1"
    assert corpus.embedding_model_for_write(name, row_resolver=_resolver({name: _row(embedding_model=_BGE)})) == (_BGE, True)


def test_the_write_model_of_a_name_no_row_backs_is_the_names_and_known() -> None:
    import nexus.corpus as corpus

    name = f"docs__x__{_BGE}__v1"
    assert corpus.embedding_model_for_write(name, row_resolver=_resolver({})) == (_BGE, True)


def test_the_write_model_of_a_row_with_no_model_is_the_names_and_flagged() -> None:
    import nexus.corpus as corpus

    name = f"docs__x__{_BGE}__v1"
    row = _row(content_type="docs")
    del row["embedding_model"]
    with capture_logs() as logs:
        assert corpus.embedding_model_for_write(name, row_resolver=_resolver({name: row})) == (_BGE, False)
    assert [e for e in logs if e["event"] == "embedding_model_row_unreadable" and e["reason"] == "embedding_model"]


def test_the_write_model_of_an_unreadable_listing_is_the_names_and_flagged() -> None:
    import nexus.corpus as corpus

    name = f"docs__x__{_BGE}__v1"

    def reader(n: str, *, strict: bool = False) -> dict | None:
        if strict:
            raise ServiceNotReadyError("down")
        return None

    with capture_logs() as logs:
        assert corpus.embedding_model_for_write(name, row_resolver=reader) == (_BGE, False)
    assert [e for e in logs if e["event"] == "embedding_model_row_unreadable"]


def test_a_note_write_over_an_unreadable_listing_stamps_the_names_model_and_says_it_is_a_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.catalog.note_write import _chunk_payload
    from nexus.catalog.store_hook import note_pieces

    def boom(name: str, *, refresh: bool = True, strict: bool = False) -> dict | None:
        raise ServiceNotReadyError("down")

    monkeypatch.setattr(mcp_infra, "get_collection_row", boom)
    name = f"knowledge__subject__{_BGE}__v1"
    chunks, known = _chunk_payload(name, ["one piece"], [{"chash": "a" * 64}], content_type="prose")
    assert known is False
    assert chunks[0]["metadata"]["embedding_model"] == _BGE
    assert note_pieces("a short note", name) == ["a short note"]  # the write goes on
    monkeypatch.setattr(mcp_infra, "get_collection_row", lambda name, *, refresh=True, strict=False: _row(embedding_model=_BGE))
    assert _chunk_payload(name, ["one piece"], [{"chash": "a" * 64}], content_type="prose")[1] is True


# -- one version parser --------------------------------------------------------------


@pytest.mark.parametrize("raw", ["v1", "v12", "v0", "3", "v", "vx", "V2", None, 3])
def test_resolve_collection_and_collection_for_read_a_version_with_one_parser(raw: object) -> None:
    from nexus.catalog.collection_name import row_version_number, version_from_for_tuple_row

    expected = row_version_number(raw)
    out = resolve_collection(_NAME, row_resolver=_resolver({_NAME: _row(model_version=raw)}))
    if expected is None:
        assert isinstance(out, Unreadable)
        with pytest.raises(ValueError):
            version_from_for_tuple_row(
                {"name": _NAME, "content_type": "code", "owner_id": "1-1", "embedding_model": _BGE, "model_version": raw},
                content_type="code", owner_id="1-1", embedding_model=_BGE,
            )
    else:
        assert isinstance(out, Row) and out.attributes.model_version == expected


# -- the cache keeps the row's other attributes ----------------------------------------


def test_the_collections_cache_keeps_collection_id_version_and_dimension_and_finds_by_id() -> None:
    """Through the real cache, not a patched reader: prime it as a listing would and read by id."""
    mcp_infra.invalidate_collections_cache()
    try:
        mcp_infra.prime_collections_cache([
            {"name": "docs__a", "count": 3, "content_type": "docs", "owner_id": "1-1", "embedding_model": _BGE,
             "lifecycle_state": "live", "collection_id": 41, "model_version": "v2", "dimension": 768},
            {"name": "docs__b", "count": 1, "content_type": "docs", "owner_id": "1-2", "embedding_model": _BGE},
        ])
        by_label = resolve_collection("docs__a")
        assert isinstance(by_label, Row)
        a = by_label.attributes
        assert (a.collection_id, a.model_version, a.dimension, a.label) == (41, 2, 768, "docs__a")
        by_id = resolve_collection(41)
        assert isinstance(by_id, Row) and by_id.attributes.label == "docs__a"
        assert resolve_collection(404) == NoRow(404)
        assert mcp_infra.get_collection_row_by_id(41)["name"] == "docs__a"
        # a listing that sends none of the three still answers, with None for them
        b = resolve_collection("docs__b")
        assert isinstance(b, Row) and (b.attributes.collection_id, b.attributes.model_version) == (None, None)
    finally:
        mcp_infra.invalidate_collections_cache()


# -- the window: no second read --------------------------------------------


def test_the_window_takes_a_row_model_already_read_and_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr("nexus.embed_window.window_for_model", lambda model: asked.append(model) or None)

    def never(name: str) -> dict | None:
        raise AssertionError("a second read")

    window_for_collection("docs__1-1__voyage-context-3__v1", "m", row_model=_BGE, row_resolver=never)
    assert asked == [_BGE]


def test_a_spec_d_mock_of_the_client_has_the_reader_and_a_bare_mock_has_none() -> None:
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.embed_window import store_row_resolver

    spec = MagicMock(spec=HttpVectorClient)
    spec._resolve_collection_row.return_value = _row()
    assert store_row_resolver(spec)(_NAME)["embedding_model"] == _BGE
    assert store_row_resolver(MagicMock()) is None


def test_a_store_with_no_catalog_reads_a_model_from_the_name_never_from_the_shared_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The re-embed source model and the export model for a store that has no row reader (a bare double,
    the test-only T3Database): the shared cache is another store's, so it is not consulted."""
    from nexus.commands.collection import _reembed_source_model

    def never(name: str, **kw: object) -> dict | None:
        raise AssertionError("the shared cache was read")

    monkeypatch.setattr(mcp_infra, "get_collection_row", never)
    assert _reembed_source_model(MagicMock(), _NAME) == _BGE
    assert _reembed_source_model(object(), _NAME) == _BGE
