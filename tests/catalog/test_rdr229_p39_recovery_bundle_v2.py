# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.9 (nexus-p2m1x.29), acceptance item (b): a bundle of format 2 (or older) still imports by name.

Format 3 added the ``collection`` registry records and each note's ``collection_id``. A bundle written before that
carries neither: only a note's recorded ``collection`` NAME. The importer resolves such a note through the name path
(``target_collection_for``), so the target registers (or finds) the collection under its own active model and mints
its own id; nothing in the bundle names an id.

The fixture is built in code, in the shape the pre-P3.9 writer produced (``git show 328bb67294^:
src/nexus/catalog/recovery_bundle.py``, ``write_bundle`` / ``export_knowledge_doc``): a header of ``format_version``
2 with no ``collections`` count, ``knowledge_doc`` records with no ``collection_id``, a tags ARRAY (version 2's change
from the comma string), and no ``collection`` record.

Real engine substrate (``t2_service_env``), no registry mocks.
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


def _v2_doc(collection: str, title: str, *, tags: list[str], category: str = "") -> dict:
    """One ``knowledge_doc`` record as the version 2 writer produced it: no ``collection_id``."""
    return {"record": "knowledge_doc", "source_uri": uri_for(collection, title), "collection": collection,
            "title": title, "tags": tags, "category": category, "ttl_days": None, "entity_key": "",
            "sensitive": False, "content": f"p39 v2 body of {title} in {collection}"}


def _write_v2_bundle(path, docs: list[dict]) -> None:
    header = {"format": "nexus-recovery-bundle", "format_version": 2, "exported_at": "2026-11-01T00:00:00Z",
              "knowledge_docs": len(docs), "links": 0, "ghosts_skipped": 0, "notes_incomplete": 0}
    with path.open("w", encoding="utf-8") as f:
        for rec in [header, *docs]:
            f.write(json.dumps(rec) + "\n")


@pytest.mark.integration
def test_a_format_2_bundle_imports_by_name_and_the_target_mints_the_ids(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    target_model = _active_model(client)
    assert target_model != _OLD_MODEL, "the premise needs a source model token unlike the target's"
    # Two recorded shapes of name: the source's conformant name (its own model token, which the target does NOT
    # keep) and a legacy name with no model token (the resolver promotes it to the target's active model).
    conformant = f"knowledge__p39v2conf__{_OLD_MODEL}__v1"
    legacy = "knowledge__p39v2legacy"
    wanted = {conformant: _label(client, "p39v2conf"), legacy: _label(client, "p39v2legacy")}
    assert parse_conformant_collection_name(wanted[conformant])["embedding_model"] == target_model
    bundle = tmp_path / "v2.jsonl"
    _write_v2_bundle(bundle, [
        _v2_doc(conformant, "c1", tags=["alpha", "one"], category="design"),
        _v2_doc(conformant, "c2", tags=[]),
        _v2_doc(legacy, "l1", tags=["beta"]),
    ])

    header, records = read_bundle(bundle)
    assert header["format_version"] == 2 and "collections" not in header
    assert all(r["record"] == "knowledge_doc" and "collection_id" not in r for r in records), "the premise: a v2 shape"
    reader = make_catalog_reader()
    assert all(reader.get_collection(label) is None for label in wanted.values()), "the premise: nothing registered yet"

    with patch("nexus.mcp.core._get_t3", return_value=client):
        summary = import_bundle(reader, reader, client, bundle)

    # The bundle named no collection row and no id, so nothing was held back, refused, or reported as a model change.
    assert (summary.docs_imported, summary.docs_failed, summary.docs_uncertain) == (3, 0, 0), summary.doc_failures
    assert summary.collections_refused_label_clash == [] and summary.collections_skipped_not_live == []
    assert summary.collections_changed_model == []
    for recorded, label in wanted.items():
        row = reader.get_collection(label)
        # Resolved by name onto the TARGET's model; the target minted the id (the bundle held none).
        assert row is not None and row["collection_id"] is not None, f"{recorded!r} -> {label!r} was never registered"
        assert row["embedding_model"] == target_model
        assert client.count(label) > 0
    assert reader.get_collection(conformant) is None, "the source's own model-token name must not be kept"
    ids = {reader.get_collection(label)["collection_id"] for label in wanted.values()}
    assert len(ids) == 2, "each collection got its own id"
    # The notes landed under the resolved collections, with their version 2 tags (an array) and category.
    for label, title, tags, category in ((wanted[conformant], "c1", ["alpha", "one"], "design"),
                                         (wanted[conformant], "c2", [], ""),
                                         (wanted[legacy], "l1", ["beta"], "")):
        entry = reader.by_source_uri(uri_for(label, title))
        assert entry is not None and entry.physical_collection == label, (label, title)
        assert client.count(label) > 0
        meta = entry.meta or {}                      # a note's tags and category live in its catalog meta
        assert list(meta.get("tags") or []) == tags, (title, meta)
        assert (meta.get("category") or "") == category, (title, meta)
