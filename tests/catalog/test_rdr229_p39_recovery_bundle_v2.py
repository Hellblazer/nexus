# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.9 (nexus-p2m1x.29), acceptance item (b): a bundle of format 2 or older still imports by name.

Format 3 added the ``collection`` registry records and each note's ``collection_id``. A bundle written before that
carries neither: only a note's recorded ``collection`` NAME. The importer resolves such a note through the name path
(``target_collection_for``), so the target registers (or finds) the collection under its own active model and mints
its own id; nothing in the bundle names an id. A link is resolved by the note's source URI, through the same name path.

The fixtures are built in code, in the shapes the older writers produced (``git show 9edad9ea67:
src/nexus/catalog/recovery_bundle.py`` for format 1, ``git show 328bb67294^:...`` for format 2):

* format 1: ``tags`` a comma STRING, ``chroma://`` source URIs (the importer canonicalises them to ``nx-note://``),
  no ``ttl_days``, ``entity_key`` or ``sensitive`` keys;
* format 2 (nexus-6pfgb.38): ``tags`` an ARRAY, ``nx-note://`` URIs, plus ``ttl_days``, ``entity_key`` and ``sensitive``.

Neither has a ``collection`` record or a ``collection_id``. Real engine substrate (``t2_service_env``), no registry mocks.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from nexus.aspect_readers import uri_for
from nexus.catalog.factory import make_catalog_reader
from nexus.catalog.recovery_bundle import import_bundle, read_bundle
from nexus.catalog.rows.legacy_names import parse_conformant_collection_name
from nexus.db.http_vector_client import HttpVectorClient
from tests.catalog.test_rdr229_p39_recovery_bundle_v3 import _active_model, _label

_OLD_MODEL = "voyage-context-3-1024"      # a source install's model token, other than the target's (checked below)


def _doc(version: int, collection: str, title: str, *, tags: list[str], category: str = "", **v2_only) -> dict:
    """One ``knowledge_doc`` record as the version's writer produced it: no ``collection_id`` in either."""
    if version == 1:
        assert not v2_only, "format 1 wrote no ttl_days, entity_key or sensitive"
        return {"record": "knowledge_doc", "source_uri": f"chroma://{collection}/{title}", "collection": collection,
                "title": title, "tags": ",".join(tags), "category": category,
                "content": f"p39 v1 body of {title} in {collection}"}
    return {"record": "knowledge_doc", "source_uri": uri_for(collection, title), "collection": collection,
            "title": title, "tags": tags, "category": category, "ttl_days": v2_only.get("ttl_days"),
            "entity_key": v2_only.get("entity_key", ""), "sensitive": False,
            "content": f"p39 v2 body of {title} in {collection}"}


def _link(version: int, from_collection: str, from_title: str, to_collection: str, to_title: str) -> dict:
    def uri(collection, title):
        return f"chroma://{collection}/{title}" if version == 1 else uri_for(collection, title)

    return {"record": "link", "from_source_uri": uri(from_collection, from_title),
            "to_source_uri": uri(to_collection, to_title), "link_type": "relates", "from_span": "", "to_span": "",
            "created_by": "user"}


def _write_bundle(path, version: int, docs: list[dict], links: list[dict]) -> None:
    header = {"format": "nexus-recovery-bundle", "format_version": version, "exported_at": "2026-11-01T00:00:00Z",
              "knowledge_docs": len(docs), "links": len(links), "ghosts_skipped": 0, "notes_incomplete": 0}
    with path.open("w", encoding="utf-8") as f:
        for rec in [header, *docs, *links]:
            f.write(json.dumps(rec) + "\n")


@pytest.mark.integration
@pytest.mark.parametrize("version", [1, 2])
def test_an_older_bundle_imports_by_name_and_the_target_mints_the_ids(t2_service_env, tmp_path, version):
    client = HttpVectorClient(tenant=t2_service_env)
    target_model = _active_model(client)
    assert target_model != _OLD_MODEL, "the premise needs a source model token unlike the target's"
    # Two recorded shapes of name: the source's conformant name (its own model token, which the target does NOT
    # keep) and a legacy name with no model token (the resolver promotes it to the target's active model).
    conf_stem, legacy_stem = f"p39v{version}conf", f"p39v{version}legacy"
    conformant = f"knowledge__{conf_stem}__{_OLD_MODEL}__v1"
    legacy = f"knowledge__{legacy_stem}"
    wanted = {conformant: _label(client, conf_stem), legacy: _label(client, legacy_stem)}
    assert parse_conformant_collection_name(wanted[conformant])["embedding_model"] == target_model
    # ttl_days and entity_key are format 2 keys: the importer hands both to the note's put, so they must land.
    # Format 1 never wrote them, so a v1 note carries neither.
    extra = {"ttl_days": 14, "entity_key": f"p39-entity-{version}"} if version == 2 else {}
    bundle = tmp_path / f"v{version}.jsonl"
    _write_bundle(bundle, version, [
        _doc(version, conformant, "c1", tags=["alpha", "one"], category="design", **extra),
        _doc(version, conformant, "c2", tags=[]),
        _doc(version, legacy, "l1", tags=["beta"]),
    ], [
        _link(version, conformant, "c1", conformant, "c2"),     # inside one collection
        _link(version, conformant, "c1", legacy, "l1"),         # across the two
    ])

    header, records = read_bundle(bundle)
    assert header["format_version"] == version and "collections" not in header
    docs = [r for r in records if r["record"] == "knowledge_doc"]
    assert len(docs) == 3 and all("collection_id" not in r for r in docs), "the premise: an old shape"
    assert all(r["record"] in ("knowledge_doc", "link") for r in records)
    assert isinstance(docs[0]["tags"], str if version == 1 else list)
    if version == 1:
        assert all(d["source_uri"].startswith("chroma://") for d in docs), "the premise: a chroma:// bundle"
    reader = make_catalog_reader()
    assert reader is not None
    assert all(reader.get_collection(label) is None for label in wanted.values()), "the premise: nothing registered yet"

    with patch("nexus.mcp.core._get_t3", return_value=client):
        summary = import_bundle(reader, reader, client, bundle)

    # The bundle named no collection row and no id, so nothing was held back, refused, or reported as a model change.
    assert (summary.docs_imported, summary.docs_failed, summary.docs_uncertain) == (3, 0, 0), summary.doc_failures
    assert summary.collections_refused_label_clash == [] and summary.collections_skipped_not_live == []
    assert summary.collections_changed_model == []
    for recorded, label in wanted.items():
        row = reader.get_collection(label)
        # Resolved by name onto the TARGET's model; the target minted the id (the bundle held none); the row keeps
        # the attributes the recorded name carried.
        assert row is not None and row["collection_id"] is not None, f"{recorded!r} -> {label!r} was never registered"
        assert row["embedding_model"] == target_model
        base = recorded.split("__")
        assert (row["content_type"], row["owner_id"]) == (base[0], base[1]), (recorded, row)
        assert client.count(label) > 0
    assert reader.get_collection(conformant) is None, "the source's own model-token name must not be kept"
    ids = {reader.get_collection(label)["collection_id"] for label in wanted.values()}
    assert len(ids) == 2, "each collection got its own id"

    # The notes landed under the resolved collections, under the stored nx-note:// identity (a chroma:// URI is
    # canonicalised), with their tags split into an array and their category in the catalog meta.
    entries = {}
    for label, title, tags, category in ((wanted[conformant], "c1", ["alpha", "one"], "design"),
                                         (wanted[conformant], "c2", [], ""),
                                         (wanted[legacy], "l1", ["beta"], "")):
        entry = reader.by_source_uri(uri_for(label, title))
        assert entry is not None and entry.physical_collection == label, (label, title)
        assert entry.source_uri.startswith("nx-note://"), entry.source_uri
        meta = entry.meta or {}                      # a note's tags and category live in its catalog meta
        assert list(meta.get("tags") or []) == tags, (title, meta)
        assert (meta.get("category") or "") == category, (title, meta)
        entries[title] = entry
    c1_meta = entries["c1"].meta or {}
    if version == 2:
        assert c1_meta.get("ttl_days") == 14 and c1_meta.get("entity_key") == "p39-entity-2", c1_meta
    else:
        assert not c1_meta.get("ttl_days") and not c1_meta.get("entity_key"), c1_meta

    # Links resolve by name: the recorded URI names the source's collection, the target holds the note under its own.
    assert summary.unresolvable_links == [] and summary.links_refused_span == []
    assert summary.links_created + summary.links_merged == 2, summary
    out = {str(link.to_tumbler) for link in reader.links_from(entries["c1"].tumbler, link_type="relates")}
    assert out == {str(entries["c2"].tumbler), str(entries["l1"].tumbler)}, out
