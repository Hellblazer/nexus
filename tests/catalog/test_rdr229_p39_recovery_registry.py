# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.9 (nexus-p2m1x.29): the edges of the format 3 import that the main file
(``test_rdr229_p39_recovery_bundle_v3.py``) does not pin: a note whose source id has no collection record, a note
with no source id (a bundle of the old shape, or a collection the source held no row for), a registration the engine
refuses (it refuses that collection only, naming the cause), a link into a collection the import held back, and a
label of the minted form (``c<digits>``), which names the SOURCE install's collection and is never kept.

The refusal and the minted-label cases run on the real engine (``t2_service_env``); the rest use a duck-typed
catalog and a recording ``import_doc``, because they test the importer's bookkeeping, not the registry.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from nexus.aspect_readers import uri_for
from nexus.catalog.factory import make_catalog_reader
from nexus.catalog.recovery_bundle import _resolve_link_endpoint, import_bundle
from nexus.collection_mint import register_by_attributes
from nexus.db.http_vector_client import HttpVectorClient
from tests.catalog.test_rdr229_p39_recovery_bundle_v3 import _active_model, _coll, _doc, _label, _write_bundle


def _note(label: str, src_id: int, title: str) -> dict:
    """A note record with a literal source_uri: ``uri_for`` resolves the label through the catalog, which a test
    with no tenant must not reach."""
    return {
        "record": "knowledge_doc", "source_uri": f"nx-note://{label}/{title}", "collection": label,
        "collection_id": src_id, "title": title, "tags": [], "category": "", "ttl_days": None,
        "entity_key": "", "sensitive": False, "content": f"body of {title}"}


class _Catalog:
    """A registry with a dict of rows: ``get_collection`` and ``register_collection`` of the real client."""

    def __init__(self, rows: dict[str, dict] | None = None, refuse: dict[str, Exception] | None = None) -> None:
        self.rows, self.refuse, self.registered = dict(rows or {}), dict(refuse or {}), []

    def get_collection(self, label: str) -> dict | None:
        return self.rows.get(label)

    def register_collection(self, label: str, *, content_type: str, owner_id: str, embedding_model: str,
                            model_version: str) -> None:
        if label in self.refuse:
            raise self.refuse[label]
        self.registered.append(label)
        self.rows[label] = {"name": label, "content_type": content_type, "owner_id": owner_id,
                            "embedding_model": embedding_model, "model_version": model_version,
                            "lifecycle_state": "live"}


def _import(tmp_path, catalog, collections, docs, links=()):
    bundle = tmp_path / "b.jsonl"
    _write_bundle(bundle, collections, docs)
    if links:
        with bundle.open("a", encoding="utf-8") as f:
            for rec in links:
                f.write(json.dumps(rec) + "\n")
    seen: list[dict] = []
    summary = import_bundle(catalog, catalog, None, bundle, import_doc=lambda _t3, rec: seen.append(rec))
    return summary, seen


def test_a_note_whose_source_id_has_no_collection_record_is_a_failure_and_is_never_written(tmp_path):
    summary, seen = _import(
        tmp_path, _Catalog(), [_coll("known", 1, model="m")], [_note("known", 1, "a"), _note("orphan", 2, "b")])

    assert [r["title"] for r in seen] == ["a"] and seen[0]["target_collection"] == "known"
    assert summary.docs_imported == 1 and summary.docs_failed == 1
    assert "collection_id 2" in summary.doc_failures[0]["error"] and "orphan" in summary.doc_failures[0]["error"]


@pytest.mark.parametrize("shape", ["null", "absent"])
def test_a_note_with_no_source_id_goes_through_the_name_path(tmp_path, shape):
    doc = _note("legacy__name", 1, "a")
    if shape == "null":
        doc["collection_id"] = None
    else:
        del doc["collection_id"]

    summary, seen = _import(tmp_path, _Catalog(), [], [doc])

    assert summary.docs_imported == 1 and len(seen) == 1
    assert "target_collection" not in seen[0]            # _default_import_doc resolves it by name, as for format 2


def test_a_registration_the_engine_refuses_refuses_that_collection_alone_and_names_the_cause(tmp_path):
    request = httpx.Request("POST", "http://x")
    response = httpx.Response(400, json={"error": "label 'bad' breaks the label rule"}, request=request)
    catalog = _Catalog(refuse={"bad": httpx.HTTPStatusError("400", request=request, response=response)})

    summary, seen = _import(
        tmp_path, catalog, [_coll("bad", 1, model="m"), _coll("good", 2, model="m")],
        [_note("bad", 1, "b1"), _note("bad", 1, "b2"), _note("good", 2, "g1")])

    [entry] = summary.collections_refused_label_clash
    assert entry["label"] == "bad" and entry["docs_skipped"] == 2
    assert "400" in entry["error"] and "breaks the label rule" in entry["error"]
    assert [r["title"] for r in seen] == ["g1"] and summary.docs_failed == 0


def test_a_collection_the_registry_cannot_read_is_refused_not_raised(tmp_path):
    class Broken(_Catalog):
        def get_collection(self, label):
            raise RuntimeError("the catalog is down")

    summary, seen = _import(tmp_path, Broken(), [_coll("x", 1, model="m")], [_note("x", 1, "a")])

    [entry] = summary.collections_refused_label_clash
    assert "the catalog is down" in entry["error"] and entry["docs_skipped"] == 1 and seen == []


def test_a_link_into_a_collection_the_import_held_back_resolves_to_nothing():
    reader = SimpleNamespace(by_source_uri=lambda uri: SimpleNamespace(tumbler="1.1.1"))
    held = frozenset({"held"})

    assert _resolve_link_endpoint(reader, None, "nx-note://held/a", held_back=held) is None
    assert _resolve_link_endpoint(reader, None, "nx-note://kept/a", held_back=held) is not None
    assert _resolve_link_endpoint(reader, None, "file:///x/held/a.py", held_back=held) is not None


def test_a_link_into_a_remapped_collection_is_looked_up_under_the_label_it_was_written_to():
    looked_up: list[str] = []

    def by_source_uri(uri):
        looked_up.append(uri)
        return SimpleNamespace(tumbler="1.1.1")

    reader = SimpleNamespace(by_source_uri=by_source_uri)

    # uri_for reads the label's row to tell a note from a file identity; a test with no tenant has none to read.
    with patch("nexus.catalog.recovery_bundle.uri_for", side_effect=lambda label, title: f"nx-note://{label}/{title}"):
        assert _resolve_link_endpoint(reader, None, "nx-note://c15/a", remap={"c15": "c97"}) is not None
    assert looked_up == ["nx-note://c97/a"]              # never the other install's c15


@pytest.mark.integration
def test_a_tuple_held_under_another_label_refuses_that_collection_only(t2_service_env, tmp_path):
    """The engine refuses a second live collection on one (content_type, owner_id, model, version) tuple (409). That
    refusal is the second collection's alone: the first imports, and the refusal names both labels."""
    client = HttpVectorClient(tenant=t2_service_env)
    model = _active_model(client)
    first, second = _label(client, "p39tuple1"), _label(client, "p39tuple2")
    bundle = tmp_path / "b.jsonl"
    _write_bundle(
        bundle,
        [_coll(first, 7_100_001, owner_id="p39tuple", model=model), _coll(second, 7_100_002, owner_id="p39tuple", model=model)],
        [_doc(first, 7_100_001, "f1"), _doc(second, 7_100_002, "s1"), _doc(second, 7_100_002, "s2")],
    )
    reader = make_catalog_reader()
    with patch("nexus.mcp.core._get_t3", return_value=client):
        summary = import_bundle(reader, reader, client, bundle)

    [entry] = summary.collections_refused_label_clash
    assert entry["label"] == second and entry["docs_skipped"] == 2
    assert first in entry["error"] and second in entry["error"], entry["error"]
    assert (summary.docs_imported, summary.docs_failed) == (1, 0), summary.doc_failures
    assert reader.get_collection(second) is None
    assert reader.by_source_uri(uri_for(first, "f1")) is not None


@pytest.mark.integration
def test_a_minted_label_is_not_kept_and_never_clashes_with_this_installs_label_of_that_form(t2_service_env, tmp_path):
    """``c<digits>`` is an engine-minted label: the source install's ``c15`` and this install's ``c15`` are two
    collections. The import registers the bundle's attributes, so the engine mints this install's own label for
    them, writes the notes under it, and resolves the links that name the source's label to the notes it wrote."""
    client = HttpVectorClient(tenant=t2_service_env)
    model = _active_model(client)
    # This install already has a collection with a minted label, with attributes the bundle's collection does not share.
    held = register_by_attributes(content_type="knowledge", owner_id="p39minted-held", embedding_model=model).label
    assert held[0] == "c" and held[1:].isdigit(), held
    bundle = tmp_path / "b.jsonl"
    _write_bundle(
        bundle,
        [_coll(held, 7_200_001, owner_id="p39minted-src", model=model)],      # the SOURCE's collection of that label
        [_doc(held, 7_200_001, "m1"), _doc(held, 7_200_001, "m2")],
    )
    with bundle.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"record": "link", "from_source_uri": f"nx-note://{held}/m1",
                            "to_source_uri": f"nx-note://{held}/m2", "link_type": "cites",
                            "from_span": "", "to_span": "", "created_by": "p39"}) + "\n")
    reader = make_catalog_reader()

    with patch("nexus.mcp.core._get_t3", return_value=client):
        summary = import_bundle(reader, reader, client, bundle)

    assert summary.collections_refused_label_clash == [], summary.collections_refused_label_clash
    assert (summary.docs_imported, summary.docs_failed, summary.docs_uncertain) == (2, 0, 0), summary.doc_failures
    rows = [r for r in reader.list_collections() if (r["content_type"], r["owner_id"]) == ("knowledge", "p39minted-src")]
    assert len(rows) == 1 and rows[0]["name"] != held and rows[0]["name"][0] == "c"
    landed = rows[0]["name"]
    assert reader.by_source_uri(uri_for(landed, "m1")) is not None
    assert reader.by_source_uri(uri_for(held, "m1")) is None         # nothing landed in this install's own c<id>
    assert (summary.links_created, summary.unresolvable_links) == (1, [])
