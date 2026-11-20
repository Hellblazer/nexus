# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.8 (nexus-p2m1x.13): a knowledge subject and a repo collection are
found by their catalog row's attributes, not by rendering a name (item 5).

- ``HttpCatalogClient.find_collection`` reads ``/collections/for_tuple`` and
  answers the row as :class:`CollectionAttributes`, or ``None`` on a 404.
- ``collection_for_repo`` answers the registered row, whatever shape its label
  has. Only a new tuple or a ``bump`` still renders a name (the mint class,
  which empties at P3.8).
- ``t3_collection_name`` finds a knowledge subject through
  ``find_collection('knowledge', subject, model)``: a label that is not of the
  conformant shape is found by its subject.

The fake transport answers with the engine's row shape (``collRow``).
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from nexus.catalog.collection_attributes import CollectionAttributes
from nexus.catalog.http_catalog_client import HttpCatalogClient

_MODEL = "bge-base-en-v15-768"


def _client(monkeypatch: pytest.MonkeyPatch, rows: dict, *, status: int | None = None) -> HttpCatalogClient:
    """A client whose ``for_tuple`` answers ``rows[(content_type, owner_id, model)]``,
    404 when absent, or *status* for every request when given."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/catalog/collections/for_tuple":
            if status is not None:
                return httpx.Response(status, json={"error": "boom"})
            p = request.url.params
            row = rows.get((p["content_type"], p["owner_id"], p["embedding_model"]))
            if row is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=row)
        return httpx.Response(200, json={})

    monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")
    client = HttpCatalogClient(base_url="http://mock.test")
    client._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handler))
    return client


def _row(name: str, content_type: str, owner_id: str, *, model: str = _MODEL, version: str = "v1") -> dict:
    return {
        "name": name, "content_type": content_type, "owner_id": owner_id,
        "embedding_model": model, "model_version": version, "dimension": 768,
    }


# -- find_collection ---------------------------------------------------------

def test_find_collection_answers_the_row_as_attributes(monkeypatch) -> None:
    row = _row("notes-from-2024", "knowledge", "field-notes", version="v2")
    cat = _client(monkeypatch, {("knowledge", "field-notes", _MODEL): row})
    got = cat.find_collection("knowledge", "field-notes", _MODEL)
    assert got == CollectionAttributes(
        collection_id=None, label="notes-from-2024", content_type="knowledge",
        owner_id="field-notes", embedding_model=_MODEL, model_version=2,
        dimension=768, lifecycle_state=None,
    )


def test_find_collection_answers_none_for_an_unregistered_tuple(monkeypatch) -> None:
    cat = _client(monkeypatch, {})
    assert cat.find_collection("knowledge", "field-notes", _MODEL) is None


def test_find_collection_raises_when_the_read_fails(monkeypatch) -> None:
    """A failed read is not "no row": the caller must not mint over it."""
    cat = _client(monkeypatch, {}, status=500)
    with pytest.raises(httpx.HTTPStatusError):
        cat.find_collection("knowledge", "field-notes", _MODEL)


def test_find_collection_refuses_a_row_for_another_tuple(monkeypatch) -> None:
    row = _row("knowledge__other__bge-base-en-v15-768__v1", "knowledge", "other")
    cat = _client(monkeypatch, {("knowledge", "field-notes", _MODEL): row})
    with pytest.raises(ValueError, match="for_tuple answered"):
        cat.find_collection("knowledge", "field-notes", _MODEL)


# -- collection_for_repo -----------------------------------------------------

def _repo_client(monkeypatch, rows: dict) -> HttpCatalogClient:
    import nexus.repo_identity as repo_identity

    cat = _client(monkeypatch, rows)
    monkeypatch.setattr(repo_identity, "_repo_identity", lambda repo: ("nexus", "abc123"))
    monkeypatch.setattr(cat, "owner_for_repo", lambda repo_hash: "1.7")
    import nexus.corpus as corpus

    monkeypatch.setattr(
        corpus, "resolve_write_embedding_model", lambda ct, *, collection_exists: _MODEL,
    )
    return cat


def test_collection_for_repo_answers_the_registered_row_whatever_its_label(monkeypatch) -> None:
    """A repo whose collection is registered under a legacy slug gets that slug
    back, not the conformant name a render would produce."""
    row = _row("code__nexus-legacy-slug", "code", "1-7", version="v3")
    cat = _repo_client(monkeypatch, {("code", "1-7", _MODEL): row})
    got = cat.collection_for_repo(Path("/repo"), "code")
    assert got.label == "code__nexus-legacy-slug"
    assert got.model_version == 3


def test_collection_for_repo_bump_mints_the_next_version(monkeypatch) -> None:
    row = _row("code__1-7__bge-base-en-v15-768__v3", "code", "1-7", version="v3")
    cat = _repo_client(monkeypatch, {("code", "1-7", _MODEL): row})
    got = cat.collection_for_repo(Path("/repo"), "code", bump=True)
    assert got.label == "code__1-7__bge-base-en-v15-768__v4"
    assert got.model_version == 4
    assert got.collection_id is None


def test_collection_for_repo_with_no_row_mints_v1(monkeypatch) -> None:
    cat = _repo_client(monkeypatch, {})
    got = cat.collection_for_repo(Path("/repo"), "code")
    assert got.label == "code__1-7__bge-base-en-v15-768__v1"
    assert got.model_version == 1


# -- t3_collection_name: the knowledge subject --------------------------------

class _T3:
    """A vector store holding *names*; every other name is absent."""

    def __init__(self, names: set[str]) -> None:
        self.names = names

    def collection_exists(self, name: str) -> bool:
        return name in self.names


class _Reader:
    def __init__(self, rows: dict) -> None:
        self.rows = rows
        self.calls: list[tuple[str, str, str]] = []

    def find_collection(self, content_type: str, owner_id: str, embedding_model: str):
        self.calls.append((content_type, owner_id, embedding_model))
        return self.rows.get((content_type, owner_id))

    def close(self) -> None:
        pass


def _attrs(label: str, owner_id: str) -> CollectionAttributes:
    return CollectionAttributes(
        collection_id=None, label=label, content_type="knowledge", owner_id=owner_id,
        embedding_model=_MODEL, model_version=1, dimension=768, lifecycle_state=None,
    )


@pytest.fixture
def reader(monkeypatch):
    import nexus.catalog.factory as factory

    box: dict = {"reader": _Reader({})}
    monkeypatch.setattr(factory, "make_catalog_reader", lambda **kw: box["reader"])
    return box


@pytest.mark.parametrize("for_write", [False, True])
def test_a_non_conformant_knowledge_label_is_found_by_its_subject(reader, for_write) -> None:
    from nexus.corpus import t3_collection_name

    reader["reader"] = _Reader({("knowledge", "field-notes"): _attrs("notes-from-2024", "field-notes")})
    got = t3_collection_name("field-notes", t3=_T3({"notes-from-2024"}), for_write=for_write)
    assert got == "notes-from-2024"
    assert reader["reader"].calls and reader["reader"].calls[0][:2] == ("knowledge", "field-notes")


def test_an_underscored_subject_is_looked_up_by_its_owner_segment(reader) -> None:
    from nexus.corpus import t3_collection_name

    reader["reader"] = _Reader({("knowledge", "field-notes"): _attrs("notes-from-2024", "field-notes")})
    assert t3_collection_name("field_notes", t3=_T3(set())) == "notes-from-2024"


def test_a_row_whose_label_is_the_rendered_name_keeps_the_existing_resolution(reader) -> None:
    """The row overrides only when it names something other than the render;
    a conformant row with no chunks still yields to the legacy two-segment name
    that holds the data (nexus-6mr0), as before P2.8."""
    from nexus.corpus import _resolve_promoted_model_token, t3_collection_name

    promoted = f"knowledge__field-notes__{_resolve_promoted_model_token('knowledge', for_write=False)}__v1"
    reader["reader"] = _Reader({("knowledge", "field-notes"): _attrs(promoted, "field-notes")})
    got = t3_collection_name("field-notes", t3=_T3({"knowledge__field-notes"}))
    assert got == "knowledge__field-notes"


def test_a_failed_row_read_falls_back_to_the_name_resolution(reader) -> None:
    from nexus.corpus import t3_collection_name

    class _Broken(_Reader):
        def find_collection(self, *a):  # type: ignore[override]
            raise httpx.ConnectError("down")

    reader["reader"] = _Broken({})
    assert t3_collection_name("field-notes", t3=_T3({"knowledge__field-notes"})) == "knowledge__field-notes"


def test_without_t3_the_resolver_stays_pure(reader) -> None:
    """No t3 means no live lookup (the static contexts and the indexers)."""
    from nexus.corpus import t3_collection_name

    reader["reader"] = _Reader({("knowledge", "field-notes"): _attrs("notes-from-2024", "field-notes")})
    got = t3_collection_name("field-notes")
    assert got.startswith("knowledge__field-notes__")
    assert reader["reader"].calls == []
