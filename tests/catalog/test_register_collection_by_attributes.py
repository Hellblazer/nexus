# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.8 (nexus-p2m1x.28) client half: ``HttpCatalogClient.register_collection`` registers by attributes.

The label is optional (the engine mints ``c<collection_id>`` for a nameless registration and answers the row),
the four attributes are required and never derived from a name, ``legacy_grandfathered`` is never sent, and the
call returns the registered row as :class:`~nexus.catalog.collection_name.CollectionAttributes`. A nameless
registration against an engine older than the mint's release is refused before any write, naming both versions.

Wire-shape tests on an ``httpx.MockTransport``; the engine side is pinned Java-side
(``CollectionMint*Test``) and the end-to-end leg is ``tests/integration/test_rdr229_p38_mint_by_attributes.py``.
"""
from __future__ import annotations

import json

import httpx
import pytest

from nexus.catalog.collection_name import CollectionAttributes
from nexus.catalog.engine_writes import floors
from nexus.catalog.engine_writes.floors import CollectionMintUnsupportedError
from nexus.catalog.http_catalog_client import HttpCatalogClient

_ATTRS = {
    "content_type": "knowledge", "owner_id": "p38-subject",
    "embedding_model": "bge-base-en-v1.5", "model_version": "v1",
}

_MINTED_ROW = {
    "collection_id": 4242, "name": "c4242", "content_type": "knowledge", "owner_id": "p38-subject",
    "embedding_model": "bge-base-en-v1.5", "model_version": "v1", "display_name": "",
    "lifecycle_state": "live", "dimension": 768, "ok": True,
}


def _client(handler) -> HttpCatalogClient:
    cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
    cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handler))
    return cat


def _engine(release: str | None, upsert_reply: dict, posts: list):
    """``/version`` answers *release*; ``/collections/upsert`` answers *upsert_reply* and records its body."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/version"):
            return httpx.Response(200, json={"release_version": release})
        posts.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=upsert_reply)

    return handler


# ── the nameless registration ───────────────────────────────────────────────


def test_a_nameless_registration_sends_no_name_and_no_legacy_flag_and_returns_the_row() -> None:
    posts: list = []
    cat = _client(_engine("0.1.201", _MINTED_ROW, posts))

    row = cat.register_collection(**_ATTRS)

    assert len(posts) == 1
    path, body = posts[0]
    assert path.endswith("/collections/upsert")
    assert "name" not in body, f"the client renders no name: {body}"
    assert "legacy_grandfathered" not in body
    assert {k: body[k] for k in _ATTRS} == _ATTRS
    assert isinstance(row, CollectionAttributes)
    assert row.collection_id == 4242
    assert row.label == "c4242"
    assert (row.content_type, row.owner_id, row.embedding_model) == (
        "knowledge", "p38-subject", "bge-base-en-v1.5",
    )
    assert row.model_version == 1 and row.dimension == 768 and row.lifecycle_state == "live"


def test_a_nameless_registration_with_a_blank_label_is_the_same_call() -> None:
    posts: list = []
    cat = _client(_engine("0.1.201", _MINTED_ROW, posts))

    row = cat.register_collection("", **_ATTRS)

    assert "name" not in posts[0][1]
    assert row.label == "c4242"


def test_a_row_for_another_tuple_is_refused_not_taken_for_the_registered_one() -> None:
    posts: list = []
    cat = _client(_engine("0.1.201", {**_MINTED_ROW, "owner_id": "somebody-else"}, posts))

    with pytest.raises(ValueError, match="somebody-else"):
        cat.register_collection(**_ATTRS)


# ── the four attributes are required, never derived ────────────────────────


@pytest.mark.parametrize("missing", sorted(_ATTRS))
def test_every_attribute_is_a_required_keyword(missing: str) -> None:
    cat = _client(_engine("0.1.201", _MINTED_ROW, []))
    attrs = {k: v for k, v in _ATTRS.items() if k != missing}

    with pytest.raises(TypeError, match=missing):
        cat.register_collection(**attrs)


@pytest.mark.parametrize("blank", sorted(_ATTRS))
def test_a_blank_attribute_is_refused_naming_the_field_and_nothing_is_sent(blank: str) -> None:
    posts: list = []
    cat = _client(_engine("0.1.201", _MINTED_ROW, posts))

    with pytest.raises(ValueError, match=blank):
        cat.register_collection(**{**_ATTRS, blank: ""})

    assert posts == []


def test_a_name_alone_derives_nothing() -> None:
    """The old bare ``register_collection(name)`` read content_type, owner and model out of the name."""
    cat = _client(_engine("0.1.201", {"ok": True}, []))

    with pytest.raises(TypeError):
        cat.register_collection("docs__myrepo-1-1__bge-base-en-v1.5__v1")  # type: ignore[call-arg]


# ── a named registration (an existing label: the migration and re-registration callers) ──


def test_a_named_registration_sends_the_name_and_attributes_it_was_given_and_never_the_legacy_flag() -> None:
    posts: list = []
    cat = _client(_engine("0.1.150", {"ok": True}, posts))  # a named upsert needs no mint floor

    row = cat.register_collection("knowledge__old-slug", **_ATTRS)

    body = posts[0][1]
    assert body["name"] == "knowledge__old-slug"
    assert "legacy_grandfathered" not in body
    assert {k: body[k] for k in _ATTRS} == _ATTRS
    assert isinstance(row, CollectionAttributes)
    assert row.label == "knowledge__old-slug"
    assert row.collection_id is None, "a named upsert answers no row, so no id is claimed"


def test_a_named_registration_makes_no_version_probe() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    _client(handler).register_collection("knowledge__old-slug", **_ATTRS)

    assert not any(p.endswith("/version") for p in seen)


# ── the engine floor ────────────────────────────────────────────────────────


def test_the_floor_is_the_engine_release_that_carries_the_mint() -> None:
    assert floors.COLLECTION_MINT_MIN_ENGINE_VERSION == (0, 1, 201)


def test_an_older_engine_refuses_a_nameless_registration_naming_both_versions_and_writes_nothing() -> None:
    posts: list = []
    cat = _client(_engine("0.1.199", _MINTED_ROW, posts))

    with pytest.raises(CollectionMintUnsupportedError) as exc:
        cat.register_collection(**_ATTRS)

    msg = str(exc.value)
    assert "0.1.199" in msg and "0.1.201" in msg
    assert "Nothing was written" in msg
    assert posts == [], "the refusal comes before the upsert, not as its opaque 409"


def test_a_dev_engine_with_no_release_stamp_is_not_refused() -> None:
    """A build from a tree carries ``release_version: null``; it is newer than any release, so it is let through."""
    posts: list = []
    cat = _client(_engine(None, _MINTED_ROW, posts))

    assert cat.register_collection(**_ATTRS).label == "c4242"


def test_an_unreadable_version_probe_does_not_refuse_the_registration_itself_fails_loudly() -> None:
    """The probe says nothing about the engine, so the write is attempted and its own error stands."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/version"):
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(409, json={"error": "refused"})

    with pytest.raises(httpx.HTTPStatusError) as exc:
        _client(handler).register_collection(**_ATTRS)
    assert exc.value.response.status_code == 409
