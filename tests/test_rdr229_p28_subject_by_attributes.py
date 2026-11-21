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
from unittest.mock import MagicMock

import httpx
import pytest

import nexus.catalog.factory as factory
import nexus.corpus as corpus
import nexus.repo_identity as repo_identity
from nexus.catalog.collection_name import CollectionAttributes
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.corpus import CollectionRegistryUnreadableError, _resolve_promoted_model_token, t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient, _process_default_tenant

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

    cat = _client(monkeypatch, rows)
    monkeypatch.setattr(repo_identity, "_repo_identity", lambda repo: ("nexus", "abc123"))
    monkeypatch.setattr(cat, "owner_for_repo", lambda repo_hash: "1.7")

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
    """A vector store holding *names*, with a catalog answering *rows* keyed by
    ``(content_type, owner_id)``; every other name and tuple is absent."""

    def __init__(self, names: set[str], rows: dict | None = None) -> None:
        self.names = names
        self.rows = rows or {}
        self.calls: list[tuple[str, str, str]] = []

    def collection_exists(self, name: str) -> bool:
        return name in self.names

    def find_collection(self, content_type: str, owner_id: str, embedding_model: str):
        self.calls.append((content_type, owner_id, embedding_model))
        return self.rows.get((content_type, owner_id))


class _T3NoCatalog:
    """A store with no ``find_collection``: the name resolution alone."""

    def __init__(self, names: set[str]) -> None:
        self.names = names

    def collection_exists(self, name: str) -> bool:
        return name in self.names


def _attrs(label: str, owner_id: str) -> CollectionAttributes:
    return CollectionAttributes(
        collection_id=None, label=label, content_type="knowledge", owner_id=owner_id,
        embedding_model=_MODEL, model_version=1, dimension=768, lifecycle_state=None,
    )


_ROWS = {("knowledge", "field-notes"): _attrs("notes-from-2024", "field-notes")}


@pytest.mark.parametrize("for_write", [False, True])
def test_a_non_conformant_knowledge_label_is_found_by_its_subject(for_write) -> None:

    t3 = _T3({"notes-from-2024"}, _ROWS)
    assert t3_collection_name("field-notes", t3=t3, for_write=for_write) == "notes-from-2024"
    model = _resolve_promoted_model_token("knowledge", for_write=False)
    assert t3.calls == [("knowledge", "field-notes", model)]


def test_an_underscored_subject_is_looked_up_by_its_owner_segment() -> None:

    assert t3_collection_name("field_notes", t3=_T3({"notes-from-2024"}, _ROWS)) == "notes-from-2024"


def test_a_row_whose_label_is_the_rendered_name_keeps_the_existing_resolution() -> None:
    """The row overrides only when it names something other than the render;
    a conformant row with no chunks still yields to the legacy two-segment name
    that holds the data (nexus-6mr0), as before P2.8."""

    promoted = f"knowledge__field-notes__{_resolve_promoted_model_token('knowledge', for_write=False)}__v1"
    t3 = _T3({"knowledge__field-notes"}, {("knowledge", "field-notes"): _attrs(promoted, "field-notes")})
    assert t3_collection_name("field-notes", t3=t3) == "knowledge__field-notes"


def test_the_subjects_row_wins_over_an_existing_conformant_collection() -> None:
    """RDR-229 P3.8 (nexus-p2m1x.28) flips P2.8's placement: once labels are minted, a subject's
    registered collection is its row, so the row is read first and answers even when a collection
    under the conformant name exists (P2.8 read it only after that collection was found empty)."""
    promoted = f"knowledge__field-notes__{_resolve_promoted_model_token('knowledge', for_write=False)}__v1"
    t3 = _T3({promoted, "notes-from-2024"}, _ROWS)
    assert t3_collection_name("field-notes", t3=t3) == "notes-from-2024"
    assert t3_collection_name("field-notes", t3=t3, for_write=True) == "notes-from-2024"


def test_a_write_refuses_an_unreadable_registry_even_with_a_conformant_collection() -> None:
    """With the row read first, a write that cannot read it refuses rather than guess the
    conformant name (P2.8's rule for a write); a read still falls back to it."""
    promoted = f"knowledge__field-notes__{_resolve_promoted_model_token('knowledge', for_write=False)}__v1"

    class _Broken(_T3):
        def find_collection(self, *a):  # type: ignore[override]
            raise httpx.ConnectError("down")

    with pytest.raises(CollectionRegistryUnreadableError):
        t3_collection_name("field-notes", t3=_Broken({promoted}), for_write=True)
    assert t3_collection_name("field-notes", t3=_Broken({promoted})) == promoted


def test_a_row_is_found_before_the_legacy_two_segment_name() -> None:
    """With the conformant name empty, the subject's row answers ahead of the
    ``knowledge__<subject>`` fallback (the row is the registered collection)."""
    t3 = _T3({"knowledge__field-notes", "notes-from-2024"}, _ROWS)
    assert t3_collection_name("field-notes", t3=t3) == "notes-from-2024"


def test_a_failed_row_read_falls_back_to_the_name_resolution() -> None:

    class _Broken(_T3):
        def find_collection(self, *a):  # type: ignore[override]
            raise httpx.ConnectError("down")

    assert t3_collection_name("field-notes", t3=_Broken({"knowledge__field-notes"})) == "knowledge__field-notes"


def test_a_store_with_no_catalog_keeps_the_name_resolution() -> None:

    assert t3_collection_name("field-notes", t3=_T3NoCatalog({"knowledge__field-notes"})) == "knowledge__field-notes"


def test_a_code_subject_is_not_looked_up() -> None:
    """Only a knowledge subject resolves through the row in P2.8."""

    t3 = _T3(set(), _ROWS)
    t3_collection_name("code__field-notes", t3=t3)
    assert t3.calls == []


def test_without_t3_the_resolver_stays_pure() -> None:
    """No t3 means no lookup (the static contexts and the indexers)."""

    assert t3_collection_name("field-notes").startswith("knowledge__field-notes__")


def _vector_client(tenant: str) -> HttpVectorClient:
    vc = object.__new__(HttpVectorClient)
    vc._tenant = tenant
    return vc


def test_the_vector_client_reads_its_own_tenants_row_through_the_catalog_reader(monkeypatch) -> None:
    seen: list = []

    class _Reader:
        def find_collection(self, *a):
            seen.append(a)
            return _ROWS[("knowledge", "field-notes")]

        def close(self) -> None:
            seen.append("closed")

    monkeypatch.setattr(factory, "make_catalog_reader", lambda **kw: _Reader())
    vc = _vector_client(_process_default_tenant())
    assert vc.find_collection("knowledge", "field-notes", _MODEL).label == "notes-from-2024"
    assert seen == [("knowledge", "field-notes", _MODEL), "closed"]


def test_a_vector_client_for_another_tenant_never_reads_the_default_catalog(monkeypatch) -> None:
    """The reader is the process default's tenant (nexus-fryrd): a client for
    another tenant answers no row instead of the default tenant's."""
    def _boom(**kw):
        raise AssertionError("the default catalog was read for another tenant")

    monkeypatch.setattr(factory, "make_catalog_reader", _boom)
    assert _vector_client("tenant-b").find_collection("knowledge", "field-notes", _MODEL) is None


@pytest.mark.parametrize("exc", [httpx.ConnectError("down"), ValueError("for_tuple answered a malformed row")])
def test_a_write_refuses_when_the_subject_row_cannot_be_read(exc) -> None:
    """A write must not take an unreadable registry for an empty one and mint
    the rendered name over the subject's real collection (item 1, Unreadable)."""
    class _Broken(_T3):
        def find_collection(self, *a):  # type: ignore[override]
            raise exc

    with pytest.raises(CollectionRegistryUnreadableError, match="field-notes"):
        t3_collection_name("field-notes", t3=_Broken({"knowledge__field-notes"}), for_write=True)


def test_a_mock_store_answers_the_name_resolution() -> None:
    """A test double whose find_collection answers something other than a row
    (a MagicMock) leaves the name resolution as it was."""
    t3 = MagicMock()
    t3.collection_exists.side_effect = lambda name: name == "knowledge__field-notes"
    assert t3_collection_name("field-notes", t3=t3) == "knowledge__field-notes"


def test_the_no_row_repo_path_reads_for_tuple_once(monkeypatch) -> None:
    calls: list[str] = []
    cat = _repo_client(monkeypatch, {})
    real_get = cat._get

    def counting_get(path, **kw):
        calls.append(path)
        return real_get(path, **kw)

    monkeypatch.setattr(cat, "_get", counting_get)
    cat.collection_for_repo(Path("/repo"), "code")
    assert calls.count("/collections/for_tuple") == 1


# -- a row with no readable version (lead ruling 2026-11-20, engine 0.1.200) --
# for_tuple now answers a grandfathered row, so a non-conformant label with a blank
# model_version can win its tuple. It is answered as it is, with no version: nothing
# is minted from it, and only a bump (which needs a version to add one to) refuses.

_BLANK = {**_row("notes-from-2024", "code", "1-7", version=""), "legacy_grandfathered": True}


def test_find_collection_answers_a_blank_version_row_with_no_version(monkeypatch) -> None:
    """The lead's row: ``legacy_grandfathered`` true, a NON-conformant name (a slug) and an empty
    ``model_version``. ``version_from_for_tuple_row`` raised on it; the row is now answered as it is,
    with ``model_version=None`` (RDR-229 Technical Design item 11 (legacy_grandfathered goes, D5): "the P2.8 test for that row is not yet written")."""
    cat = _client(monkeypatch, {("code", "1-7", _MODEL): _BLANK})
    got = cat.find_collection("code", "1-7", _MODEL)
    assert got is not None
    assert got.label == "notes-from-2024"
    assert got.model_version is None


@pytest.mark.parametrize("bad", ["v0", "3", "vx"])
def test_a_malformed_non_empty_version_still_raises(monkeypatch, bad) -> None:
    cat = _client(monkeypatch, {("code", "1-7", _MODEL): _row("notes-from-2024", "code", "1-7", version=bad)})
    with pytest.raises(ValueError, match="model_version"):
        cat.find_collection("code", "1-7", _MODEL)


def test_collection_for_returns_a_blank_version_row_as_it_is(monkeypatch) -> None:
    cat = _client(monkeypatch, {("code", "1-7", _MODEL): _BLANK})
    got = cat.collection_for("code", "1.7", _MODEL)
    assert got.render() == "notes-from-2024"
    assert got.model_version is None


def test_collection_for_with_a_bump_refuses_a_blank_version_row(monkeypatch) -> None:
    """The refusal names both fixes: set the row's model_version, or re-register the collection (RDR-229 Technical Design item 11 (legacy_grandfathered goes, D5): "the P2.8 test for that row is not yet written")."""
    cat = _client(monkeypatch, {("code", "1-7", _MODEL): _BLANK})
    with pytest.raises(ValueError, match="set the row's model_version, or re-register the collection"):
        cat.collection_for("code", "1.7", _MODEL, bump=True)


def test_collection_for_repo_answers_a_blank_version_row_and_refuses_a_bump(monkeypatch) -> None:
    cat = _repo_client(monkeypatch, {("code", "1-7", _MODEL): _BLANK})
    got = cat.collection_for_repo(Path("/repo"), "code")
    assert (got.label, got.model_version) == ("notes-from-2024", None)
    with pytest.raises(ValueError, match="set the row's model_version"):
        cat.collection_for_repo(Path("/repo"), "code", bump=True)
