# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P1.4 (nexus-p2m1x.4): the next collection version is read from the
``/collections/for_tuple`` row's ``model_version``, not parsed from its name.

The fake transport answers with the engine's real row shape (``collRow``): a
``name``, the tuple attributes and a ``model_version`` TEXT of the form
``v<N>``. The first case is the one that separates the two readings: the row's
version is ``v3`` while its name's suffix says ``v1``.

An EMPTY row version (``catalog_collections.model_version`` is ``NOT NULL
DEFAULT ''`` and an upsert never rewrites it) falls back to the conformant name
with a warning, until P1.2's census (nexus-p2m1x.2) reads zero such rows.
"""
from __future__ import annotations

import httpx
import pytest
from structlog.testing import capture_logs

from nexus.catalog.http_catalog_client import HttpCatalogClient

_OWNER = "1.1"
_MODEL = "bge-base-en-v15-768"


def _client(monkeypatch: pytest.MonkeyPatch, row: dict | None) -> HttpCatalogClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/catalog/collections/for_tuple":
            if row is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=row)
        return httpx.Response(200, json={})

    monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")
    client = HttpCatalogClient(base_url="http://mock.test")
    client._client = httpx.Client(
        base_url="http://mock.test", transport=httpx.MockTransport(handler),
    )
    return client


def _row(*, name: str = "code__1-1__bge-base-en-v15-768__v1", **over: object) -> dict:
    return {
        "name": name, "content_type": "code", "owner_id": "1-1",
        "embedding_model": _MODEL, "model_version": "v1", **over,
    }


def test_next_version_comes_from_the_row_not_the_name(monkeypatch) -> None:
    """name says v1, the row says v3: the row wins, with and without a bump."""
    cat = _client(monkeypatch, _row(model_version="v3"))
    assert cat.collection_for("code", _OWNER, _MODEL).model_version == 3
    assert cat.collection_for("code", _OWNER, _MODEL, bump=True).model_version == 4


def test_a_name_that_cannot_parse_does_not_matter(monkeypatch) -> None:
    """A legacy slug resolves: its version is the row's, not the name's."""
    cat = _client(monkeypatch, _row(name="a-legacy-slug", model_version="v7"))
    assert cat.collection_for("code", _OWNER, _MODEL, bump=True).model_version == 8


def test_a_new_tuple_is_still_v1(monkeypatch) -> None:
    cat = _client(monkeypatch, None)
    assert cat.collection_for("code", _OWNER, _MODEL).model_version == 1


@pytest.mark.parametrize("bad", ["3", "v", "vx", "v-1", "v0", "V2", " v2", None])
def test_a_malformed_row_version_is_loud(monkeypatch, bad) -> None:
    """No fallback to the name for a non-empty bad value (n9xjy). v0 is malformed."""
    row = _row(name="code__1-1__bge-base-en-v15-768__v9")
    if bad is None:
        del row["model_version"]
    else:
        row["model_version"] = bad
    cat = _client(monkeypatch, row)
    with pytest.raises(ValueError, match="model_version"):
        cat.collection_for("code", _OWNER, _MODEL)


def test_an_empty_row_version_falls_back_to_the_conformant_name_loudly(monkeypatch) -> None:
    cat = _client(monkeypatch, _row(name="code__1-1__bge-base-en-v15-768__v4", model_version=""))
    with capture_logs() as logs:
        assert cat.collection_for("code", _OWNER, _MODEL).model_version == 4
        assert cat.collection_for("code", _OWNER, _MODEL, bump=True).model_version == 5
    warned = [e for e in logs if e["event"] == "collection_for_row_model_version_empty"]
    assert len(warned) == 2
    assert all(e["collection"] == "code__1-1__bge-base-en-v15-768__v4" for e in warned)
    assert all(e["log_level"] == "warning" for e in warned)


def test_an_empty_row_version_with_a_v0_name_raises(monkeypatch) -> None:
    cat = _client(monkeypatch, _row(name="code__1-1__bge-base-en-v15-768__v0", model_version=""))
    with pytest.raises(ValueError, match="empty model_version"):
        cat.collection_for("code", _OWNER, _MODEL)


def test_an_empty_row_version_with_a_slug_name_is_answered_as_it_is(monkeypatch) -> None:
    """Engine 0.1.200 (RDR-229 P2.8): a grandfathered slug row with a blank version can win its
    tuple. It is returned unchanged; only a bump refuses (tests/test_rdr229_p28_subject_by_attributes.py)."""
    cat = _client(monkeypatch, _row(name="notes-from-2024", model_version="", legacy_grandfathered=True))
    got = cat.collection_for("code", _OWNER, _MODEL)
    assert got.render() == "notes-from-2024"
    with pytest.raises(ValueError, match="model_version"):
        cat.collection_for("code", _OWNER, _MODEL, bump=True)


@pytest.mark.parametrize(
    "field,value",
    [("content_type", "docs"), ("owner_id", "1-2"), ("embedding_model", "minilm-l6-v2-384")],
)
def test_a_row_for_another_tuple_is_loud(monkeypatch, field, value) -> None:
    """The engine answered for some other tuple: never take its version."""
    cat = _client(monkeypatch, _row(**{field: value}))
    with pytest.raises(ValueError, match="for_tuple answered"):
        cat.collection_for("code", _OWNER, _MODEL)


def test_a_row_with_no_readable_version_logs_the_warning_once(monkeypatch) -> None:
    """The version is read once per call: a second read logged the unreadable warning twice."""
    cat = _client(monkeypatch, _row(name="notes-from-2024", model_version="", legacy_grandfathered=True))
    with capture_logs() as logs:
        cat.collection_for("code", _OWNER, _MODEL)
    assert [e["event"] for e in logs].count("collection_for_row_model_version_unreadable") == 1
