# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.9 (nexus-p2m1x.29): recovery bundle format 3 carries each collection's registry row and its
``collection_id``; import re-maps ids and checks label clashes (RDR item 10; scenario "a recovery bundle in the
new format imported into an installation where its label is live").

Real engine substrate (``t2_service_env``), no registry mocks: the target's rows are registered and read through
the catalog client. These tests land BEFORE the implementation: format 3 and ``collection_id`` do not exist yet.

Rules under test (linda's rulings on the bead, Sam's P3.8 ruling):

* The bundle's ``collection_id`` is a bundle-local key. The target mints (or keeps) its own id and NEVER adopts
  the source's, even where a different target collection already holds that number.
* A clash is the same live label with a different content_type or owner_id. The same label with the same
  attributes maps to that row (an idempotent re-import). A clash refuses that collection only.
* A non-live row is skipped, counted and named; the rest imports. It is never registered live.
* A label with no target row is registered with the TARGET's active model and keeps its label; the summary names
  the model change.

The v2 and legacy-name non-stranding tests stay in ``tests/catalog/test_recovery_bundle.py`` (not grown here).
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from nexus.aspect_readers import uri_for
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.catalog.recovery_bundle import export_bundle, import_bundle, read_bundle
from nexus.corpus import parse_conformant_collection_name, t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient
from nexus.mcp.core import store_put

# Far above any id a test engine mints, so "the target's id differs from the source's" holds by construction.
_SRC = 7_000_000
_BUNDLE_OWNER = "p39-bundle-owner"
_TARGET_OWNER = "p39-target-owner"
_VOYAGE = "voyage-context-3-1024"


def _label(client, stem: str) -> str:
    """A conformant label under the TARGET's active model (what ``store_put`` to a fresh subject mints)."""
    return t3_collection_name(f"knowledge__{stem}", t3=client)


def _active_model(client) -> str:
    return parse_conformant_collection_name(_label(client, "p39probe"))["embedding_model"]


def _row(label: str) -> dict | None:
    return make_catalog_reader().get_collection(label)


def _id(label: str) -> int:
    """The target's own id for *label*, from its registry row (P3.1 adds the column)."""
    row = _row(label)
    assert row is not None, f"{label!r} has no registry row on the target"
    assert "collection_id" in row, f"the registry row carries no collection_id (RDR-229 P3.1 not landed): {sorted(row)}"
    return int(row["collection_id"])


def _register(label: str, *, content_type: str, owner_id: str, model: str) -> None:
    make_catalog_writer().register_collection(
        label, content_type=content_type, owner_id=owner_id, embedding_model=model, model_version="v1")


def _coll(label: str, src_id: int, *, content_type="knowledge", owner_id=_BUNDLE_OWNER, model: str,
          lifecycle_state="live") -> dict:
    return {"record": "collection", "collection_id": src_id, "label": label, "content_type": content_type,
            "owner_id": owner_id, "embedding_model": model, "model_version": "v1",
            "lifecycle_state": lifecycle_state}


def _doc(label: str, src_id: int, title: str) -> dict:
    return {"record": "knowledge_doc", "source_uri": uri_for(label, title), "collection": label,
            "collection_id": src_id, "title": title, "tags": [], "category": "", "ttl_days": None,
            "entity_key": "", "sensitive": False, "content": f"p39 body of {title} in {label}"}


def _write_bundle(path, collections: list[dict], docs: list[dict], *, version: int = 3) -> None:
    header = {"format": "nexus-recovery-bundle", "format_version": version, "exported_at": "2026-11-21T00:00:00Z",
              "collections": len(collections), "knowledge_docs": len(docs), "links": 0,
              "ghosts_skipped": 0, "notes_incomplete": 0}
    with path.open("w", encoding="utf-8") as f:
        for rec in [header, *collections, *docs]:
            f.write(json.dumps(rec) + "\n")


def _import(client, bundle):
    reader = make_catalog_reader()
    with patch("nexus.mcp.core._get_t3", return_value=client):
        return import_bundle(reader, reader, client, bundle)


def _by_label(entries: list[dict], label: str) -> list[dict]:
    return [e for e in entries if e.get("label") == label]


# ── (1) a live label with other attributes is refused; the others import ─────────────────────────────────────


@pytest.mark.integration
def test_a_live_label_with_other_attributes_is_refused_naming_both_and_the_others_import(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    model = _active_model(client)
    clash = "r229-p39-clash"
    b, c = _label(client, "p39b"), _label(client, "p39c")
    _register(clash, content_type="docs", owner_id=_TARGET_OWNER, model=model)
    bundle = tmp_path / "b.jsonl"
    _write_bundle(
        bundle,
        [_coll(clash, _SRC + 1, model=model), _coll(b, _SRC + 2, model=model), _coll(c, _SRC + 3, model=model)],
        [_doc(clash, _SRC + 1, "a1"), _doc(clash, _SRC + 1, "a2"), _doc(b, _SRC + 2, "b1"), _doc(c, _SRC + 3, "c1")],
    )

    summary = _import(client, bundle)

    refused = _by_label(summary.collections_refused, clash)
    assert len(summary.collections_refused) == 1 and len(refused) == 1, summary.collections_refused
    reason = refused[0]["reason"]
    # Both sides, by label and attribute: the bundle's (knowledge, p39-bundle-owner) and the target's (docs, p39-target-owner).
    for needle in (clash, "knowledge", _BUNDLE_OWNER, "docs", _TARGET_OWNER):
        assert needle in reason, (needle, reason)
    assert refused[0]["docs_skipped"] == 2
    # The target's row is untouched and none of the refused collection's docs landed in it.
    row = _row(clash)
    assert (row["content_type"], row["owner_id"]) == ("docs", _TARGET_OWNER)
    assert make_catalog_reader().by_source_uri(uri_for(clash, "a1")) is None
    # The others import, under the id the TARGET minted (not the bundle's), and their docs resolve there.
    assert (summary.docs_imported, summary.docs_failed, summary.docs_uncertain) == (2, 0, 0), summary.doc_failures
    for label, src_id, title in ((b, _SRC + 2, "b1"), (c, _SRC + 3, "c1")):
        assert _id(label) != src_id
        entry = make_catalog_reader().by_source_uri(uri_for(label, title))
        assert entry is not None and entry.physical_collection == label
        assert client.count(label) > 0


# ── (2) the target never adopts the source id ────────────────────────────────────────────────────────────────


@pytest.mark.integration
def test_the_target_never_adopts_the_source_id_even_where_another_collection_holds_that_number(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    model = _active_model(client)
    held, y, z = _label(client, "p39held"), _label(client, "p39y"), _label(client, "p39z")
    _register(held, content_type="knowledge", owner_id="p39held", model=model)
    held_id = _id(held)
    far = _SRC + 99                                          # no target collection holds this number
    bundle = tmp_path / "b.jsonl"
    # y's source id IS the number the target's other collection holds; z's is a number no one holds.
    _write_bundle(
        bundle,
        [_coll(y, held_id, owner_id="p39y", model=model), _coll(z, far, owner_id="p39z", model=model)],
        [_doc(y, held_id, "y1"), _doc(z, far, "z1")],
    )

    summary = _import(client, bundle)

    assert summary.collections_refused == [] and (summary.docs_imported, summary.docs_failed) == (2, 0), summary
    assert _id(held) == held_id                              # the holder keeps its id
    assert _id(y) != held_id and _id(z) != far               # neither imported collection adopted a source id
    reader = make_catalog_reader()
    assert reader.by_source_uri(uri_for(y, "y1")).physical_collection == y   # not filed under the id's other holder
    assert client.count(held) == 0 and client.count(y) > 0 and client.count(z) > 0


# ── (3) a re-import onto the bundle's own source maps every collection and creates nothing ───────────────────


def _seed(client, subjects: tuple[str, ...]) -> dict[str, str]:
    with patch("nexus.mcp.core._get_t3", return_value=client):
        for subject in subjects:
            for n in (1, 2):
                store_put(content=f"p39 {subject} note {n} body", collection=subject, title=f"{subject}-{n}")
    return {s: t3_collection_name(s, t3=client) for s in subjects}


@pytest.mark.integration
def test_export_writes_each_collections_registry_row_and_ids_every_doc(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    labels = _seed(client, ("p39exp-a", "p39exp-b"))
    bundle = tmp_path / "b.jsonl"

    export_bundle(make_catalog_reader(), client, bundle)

    header, records = read_bundle(bundle)
    assert header["format_version"] == 3 and header["collections"] == 2
    rows = {r["label"]: r for r in records if r["record"] == "collection"}
    assert set(rows) == set(labels.values())
    for label, rec in rows.items():
        reg = _row(label)
        assert rec["collection_id"] == _id(label)
        assert (rec["content_type"], rec["owner_id"], rec["embedding_model"]) == (
            reg["content_type"], reg["owner_id"], reg["embedding_model"])
        assert rec["model_version"] and rec["lifecycle_state"] == "live"
    docs = [r for r in records if r["record"] == "knowledge_doc"]
    assert len(docs) == 4 and all(d["collection_id"] == rows[d["collection"]]["collection_id"] for d in docs)


@pytest.mark.integration
def test_reimporting_a_bundle_onto_its_own_source_maps_every_collection_and_creates_nothing(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    labels = _seed(client, ("p39re-a", "p39re-b"))
    reader = make_catalog_reader()
    bundle = tmp_path / "b.jsonl"
    export_bundle(reader, client, bundle)
    before = {r["name"]: r["collection_id"] for r in reader.list_collections()}
    assert set(labels.values()) <= set(before)
    tumblers = {t: str(reader.by_source_uri(uri_for(c, f"{t}-{n}")).tumbler)
                for t, c in labels.items() for n in (1, 2)}

    summary = _import(client, bundle)

    assert summary.collections_refused == [], summary.collections_refused
    assert (summary.docs_imported, summary.docs_failed, summary.docs_uncertain) == (4, 0, 0), summary.doc_failures
    assert {r["name"]: r["collection_id"] for r in reader.list_collections()} == before   # no row, no new id
    for subject, label in labels.items():
        for n in (1, 2):
            assert str(reader.by_source_uri(uri_for(label, f"{subject}-{n}")).tumbler) == tumblers[subject]


# ── (4) a non-live collection row is skipped, counted and named ──────────────────────────────────────────────


@pytest.mark.integration
def test_a_non_live_collection_row_is_skipped_counted_and_named_and_the_rest_imports(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    model = _active_model(client)
    live, dead = _label(client, "p39live"), "quarantine-" + _label(client, "p39dead")
    bundle = tmp_path / "b.jsonl"
    _write_bundle(
        bundle,
        [_coll(dead, _SRC + 5, owner_id="p39dead", model=model, lifecycle_state="quarantine"),
         _coll(live, _SRC + 6, owner_id="p39live", model=model)],
        [_doc(dead, _SRC + 5, "d1"), _doc(dead, _SRC + 5, "d2"), _doc(live, _SRC + 6, "l1")],
    )

    summary = _import(client, bundle)

    skipped = _by_label(summary.collections_skipped_non_live, dead)
    assert len(summary.collections_skipped_non_live) == 1 and len(skipped) == 1, summary.collections_skipped_non_live
    assert "quarantine" in json.dumps(skipped[0]) and skipped[0]["docs_skipped"] == 2
    assert summary.collections_refused == []                 # skipped is not refused: they are counted apart
    assert (summary.docs_imported, summary.docs_failed) == (1, 0), summary.doc_failures
    assert _row(dead) is None                                # never registered, and never registered live
    assert make_catalog_reader().by_source_uri(uri_for(dead, "d1")) is None
    assert _id(live) != _SRC + 6 and make_catalog_reader().by_source_uri(uri_for(live, "l1")) is not None


# ── (5) a model change registers under the target's active model, keeps the label, and is named ──────────────


@pytest.mark.integration
def test_a_source_model_the_target_does_not_use_registers_under_the_active_model_and_is_named(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    active = _active_model(client)
    assert active != _VOYAGE, "the substrate's active model is the source model: the test would prove nothing"
    label = "knowledge__p39model"                            # a token-free label: nothing in the name says a model
    bundle = tmp_path / "b.jsonl"
    _write_bundle(bundle, [_coll(label, _SRC + 7, owner_id="p39model", model=_VOYAGE)], [_doc(label, _SRC + 7, "m1")])

    summary = _import(client, bundle)

    row = _row(label)
    assert row is not None and row["name"] == label         # the label is kept
    assert row["embedding_model"] == active                  # the target's active model, not the row's
    changed = _by_label(summary.collections_model_changed, label)
    assert len(changed) == 1, summary.collections_model_changed
    assert {_VOYAGE, active} <= set(changed[0].values())     # names both models
    assert summary.collections_refused == [] and (summary.docs_imported, summary.docs_failed) == (1, 0), summary.doc_failures
    assert make_catalog_reader().by_source_uri(uri_for(label, "m1")) is not None


# ── (6) the version gate ─────────────────────────────────────────────────────────────────────────────────────


def test_read_bundle_reads_format_3_and_refuses_format_4(tmp_path):
    for version in (3, 4):
        _write_bundle(tmp_path / f"v{version}.jsonl", [], [], version=version)

    header, records = read_bundle(tmp_path / "v3.jsonl")
    assert header["format_version"] == 3 and records == []

    with pytest.raises(ValueError, match=r"format_version 4 exceeds the maximum this build reads \(3\)"):
        read_bundle(tmp_path / "v4.jsonl")
