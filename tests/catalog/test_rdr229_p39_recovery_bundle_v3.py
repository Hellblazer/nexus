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
  the model change. A conformant-shaped label keeps its old model token; the row's model decides what is
  written (RDR :2432).

New ``ImportSummary`` fields, named like ``links_refused_span`` (a list of dicts, ``error`` for the why):
``collections_refused_label_clash``, ``collections_skipped_not_live``, ``collections_changed_model``. ``nx catalog
import`` prints each only when it is nonzero.

The v2 and legacy-name non-stranding tests stay in ``tests/catalog/test_recovery_bundle.py`` (not grown here).
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.aspect_readers import uri_for
from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.catalog.recovery_bundle import ImportSummary, export_bundle, import_bundle, read_bundle
from nexus.catalog.rows.legacy_names import parse_conformant_collection_name  # moved out of nexus.corpus by P2.2
from nexus.cli import main
from nexus.corpus import t3_collection_name
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


def _write_bundle(path, collections: list[dict], docs: list[dict], *, version: int = 3,
                  docs_first: bool = False) -> None:
    header = {"format": "nexus-recovery-bundle", "format_version": version, "exported_at": "2026-11-21T00:00:00Z",
              "collections": len(collections), "knowledge_docs": len(docs), "links": 0,
              "ghosts_skipped": 0, "notes_incomplete": 0}
    body = [*docs, *collections] if docs_first else [*collections, *docs]
    with path.open("w", encoding="utf-8") as f:
        for rec in [header, *body]:
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
        # b and c carry their own owners: the engine refuses a second live collection on one
        # (content_type, owner_id, embedding_model, model_version) tuple, so a bundle that held two would not import whole.
        [_coll(clash, _SRC + 1, model=model), _coll(b, _SRC + 2, owner_id="p39b", model=model),
         _coll(c, _SRC + 3, owner_id="p39c", model=model)],
        [_doc(clash, _SRC + 1, "a1"), _doc(clash, _SRC + 1, "a2"), _doc(b, _SRC + 2, "b1"), _doc(c, _SRC + 3, "c1")],
    )

    summary = _import(client, bundle)

    refused = _by_label(summary.collections_refused_label_clash, clash)
    assert len(summary.collections_refused_label_clash) == 1 and len(refused) == 1, summary.collections_refused_label_clash
    reason = refused[0]["error"]
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

    assert summary.collections_refused_label_clash == [] and (summary.docs_imported, summary.docs_failed) == (2, 0), summary
    assert _id(held) == held_id                              # the holder keeps its id
    assert _id(y) != held_id and _id(z) != far               # neither imported collection adopted a source id
    reader = make_catalog_reader()
    assert reader.by_source_uri(uri_for(y, "y1")).physical_collection == y   # not filed under the id's other holder
    assert client.count(held) == 0 and client.count(y) > 0 and client.count(z) > 0


@pytest.mark.integration
def test_docs_that_come_before_their_collection_records_still_import_under_the_registered_collection(
    t2_service_env, tmp_path,
):
    """After P3.1 a document whose collection has no registry row is refused (422, UnregisteredCollectionException,
    from the NULLIF CHECK's guard). So the importer reads EVERY collection record and registers them first,
    whatever their position in the file; a doc never reaches the engine ahead of its collection's row."""
    client = HttpVectorClient(tenant=t2_service_env)
    model = _active_model(client)
    p, q = _label(client, "p39first-p"), _label(client, "p39first-q")
    bundle = tmp_path / "b.jsonl"
    _write_bundle(
        bundle,
        [_coll(p, _SRC + 11, owner_id="p39first-p", model=model), _coll(q, _SRC + 12, owner_id="p39first-q", model=model)],
        [_doc(p, _SRC + 11, "p1"), _doc(q, _SRC + 12, "q1"), _doc(p, _SRC + 11, "p2")],
        docs_first=True,
    )
    _header, records = read_bundle(bundle)
    assert [r["record"] for r in records] == ["knowledge_doc"] * 3 + ["collection"] * 2   # the premise: docs first

    summary = _import(client, bundle)

    assert (summary.docs_imported, summary.docs_failed, summary.docs_uncertain) == (3, 0, 0), summary.doc_failures
    assert summary.collections_refused_label_clash == []
    reader = make_catalog_reader()
    for label, src_id, titles in ((p, _SRC + 11, ("p1", "p2")), (q, _SRC + 12, ("q1",))):
        assert _id(label) != src_id                          # registered under the target's own id
        for title in titles:
            entry = reader.by_source_uri(uri_for(label, title))
            assert entry is not None and entry.physical_collection == label
        assert client.count(label) > 0


# ── (3) a re-import onto the bundle's own source maps every collection and creates nothing ───────────────────


def _seed(client, subjects: tuple[str, ...]) -> dict[str, str]:
    with patch("nexus.mcp.core._get_t3", return_value=client):
        for subject in subjects:
            for n in (1, 2):
                store_put(content=f"p39 {subject} note {n} body", collection=subject, title=f"{subject}-{n}")
    # Since P3.8 the engine mints the label (c<id>) of a collection a put creates, so the label is read from the
    # registry by the attributes the put registered, not derived from the subject.
    rows = make_catalog_reader().list_collections()
    return {s: next(r["name"] for r in rows if (r["content_type"], r["owner_id"]) == ("knowledge", s)) for s in subjects}


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
    tumblers = {(t, n): str(reader.by_source_uri(uri_for(c, f"{t}-{n}")).tumbler)
                for t, c in labels.items() for n in (1, 2)}

    summary = _import(client, bundle)

    assert summary.collections_refused_label_clash == [], summary.collections_refused_label_clash
    assert (summary.docs_imported, summary.docs_failed, summary.docs_uncertain) == (4, 0, 0), summary.doc_failures
    assert {r["name"]: r["collection_id"] for r in reader.list_collections()} == before   # no row, no new id
    for subject, label in labels.items():
        for n in (1, 2):
            assert str(reader.by_source_uri(uri_for(label, f"{subject}-{n}")).tumbler) == tumblers[(subject, n)]


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

    skipped = _by_label(summary.collections_skipped_not_live, dead)
    assert len(summary.collections_skipped_not_live) == 1 and len(skipped) == 1, summary.collections_skipped_not_live
    assert "quarantine" in json.dumps(skipped[0]) and skipped[0]["docs_skipped"] == 2
    assert summary.collections_refused_label_clash == []                 # skipped is not refused: they are counted apart
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
    changed = _by_label(summary.collections_changed_model, label)
    assert len(changed) == 1, summary.collections_changed_model
    assert {_VOYAGE, active} <= set(changed[0].values())     # names both models
    assert summary.collections_refused_label_clash == [] and (summary.docs_imported, summary.docs_failed) == (1, 0), summary.doc_failures
    assert make_catalog_reader().by_source_uri(uri_for(label, "m1")) is not None


@pytest.mark.integration
def test_a_conformant_label_whose_token_is_the_source_model_imports_and_writes_the_rows_model(t2_service_env, tmp_path):
    """The RDR states this rule already (Test Plan, docs/rdr/rdr-229-collection-name-becomes-a-label.md:2432-2434,
    and P1.4's test at :1965-1966): "a collection whose row's model differs from its name's segment is written to
    -- Verify: chunk metadata carries the row's model". A bundle label of the old four-segment form keeps its
    token (item 8, :1475: "Existing collections keep their names as labels"); the row, not the token, decides."""
    client = HttpVectorClient(tenant=t2_service_env)
    active = _active_model(client)
    label = f"knowledge__p39tok__{_VOYAGE}__v1"
    bundle = tmp_path / "b.jsonl"
    _write_bundle(bundle, [_coll(label, _SRC + 8, owner_id="p39tok", model=_VOYAGE)], [_doc(label, _SRC + 8, "t1")])

    summary = _import(client, bundle)

    assert (summary.docs_imported, summary.docs_failed, summary.docs_uncertain) == (1, 0, 0), summary.doc_failures
    row = _row(label)
    assert row is not None and row["name"] == label and row["embedding_model"] == active
    got = client.get_or_create_collection(label).get(include=["metadatas"], limit=50, sensitivity="include")
    assert got["ids"] and {(m or {}).get("embedding_model") for m in got["metadatas"]} == {active}
    assert len(_by_label(summary.collections_changed_model, label)) == 1


# ── CLI: each new count prints only when nonzero ─────────────────────────────────────────────────────────────

_NEW_FIELDS = {
    "collections_refused_label_clash": [{"label": "cli-clash-label", "error": "label is live with other attributes",
                                          "docs_skipped": 2}],
    "collections_skipped_not_live": [{"label": "cli-dead-label", "lifecycle_state": "quarantine", "docs_skipped": 3}],
    "collections_changed_model": [{"label": "cli-model-label", "from_model": "src-model-x", "to_model": "dst-model-y"}],
}


def _invoke_cli_import(tmp_path, monkeypatch, summary):
    """The seam test of ``tests/test_catalog_export_import_cli.py``: the CLI renders a given ImportSummary."""
    bundle = tmp_path / "recovery.jsonl"
    bundle.write_text(json.dumps({"format": "nexus-recovery-bundle", "format_version": 3}) + "\n")
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: object())
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: object())
    monkeypatch.setattr("nexus.catalog.recovery_bundle.import_bundle", lambda r, w, t, p: summary)
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())
    return CliRunner().invoke(main, ["catalog", "import", str(bundle)])


def test_the_cli_prints_nothing_for_the_new_counts_when_all_are_zero(tmp_path, monkeypatch):
    summary = ImportSummary(docs_imported=3)
    assert all(getattr(summary, f) == [] for f in _NEW_FIELDS), "each new field defaults to empty"

    result = _invoke_cli_import(tmp_path, monkeypatch, summary)

    assert result.exit_code == 0, result.output
    # The existing two lines only (the header and the docs/links line), as before format 3.
    assert len(result.output.strip().splitlines()) == 2, result.output


@pytest.mark.parametrize("field", sorted(_NEW_FIELDS))
def test_the_cli_prints_a_new_count_only_when_it_is_nonzero_and_names_each_entry(tmp_path, monkeypatch, field):
    summary = ImportSummary(docs_imported=3, **{field: _NEW_FIELDS[field]})

    out = _invoke_cli_import(tmp_path, monkeypatch, summary).output

    entry = _NEW_FIELDS[field][0]
    assert entry["label"] in out, out
    for other, entries in _NEW_FIELDS.items():
        if other != field:
            assert entries[0]["label"] not in out, (field, other, out)
    # The count of this class is printed (one entry, so the number 1) on a line that also names the entry's detail.
    detail = entry.get("docs_skipped", entry.get("to_model"))
    assert str(detail) in out, out


# Exit status (linda's ruling). Precedent: ``import_cmd`` exits 1 once any NOTE did not land (docs_failed,
# docs_uncertain, docs_stamp_refused: src/nexus/commands/catalog_cmds/recovery.py:186-192, "Exits non-zero when
# any note did not verify", :134) and prints the whole summary before it does; the export exits 1 for a bundle
# that does not hold every note (:105-115). LINK outcomes (unresolvable_links, links_refused_span) do not change
# the status (tests/test_catalog_export_import_cli.py::test_unresolvable_links_alone_do_not_change_the_exit_status).
# ``nx store import`` has no partial-failure precedent: it imports one collection, leaves records out and exits 0
# (store.py:1295, ``_echo_left_out``), and every failure is a ClickException (exit 1, store.py:1259).
# A refused collection leaves its notes unrestored, the note-level class, so it exits 1; skipped-not-live and
# changed-model are expected outcomes and exit 0.


def test_a_refused_collection_exits_1_and_the_full_summary_still_prints(tmp_path, monkeypatch):
    summary = ImportSummary(
        docs_imported=3, links_created=1, **_NEW_FIELDS,
        unresolvable_links=[{"from_source_uri": "nx-note://k/a", "to_source_uri": "nx-note://k/b",
                             "link_type": "cites", "missing": "to"}],
    )

    result = _invoke_cli_import(tmp_path, monkeypatch, summary)

    assert result.exit_code == 1, result.output
    out = result.output
    assert "docs imported: 3" in out and "links created: 1" in out
    for entries in _NEW_FIELDS.values():                     # all three classes, not just the refusal
        assert entries[0]["label"] in out, out
    assert _NEW_FIELDS["collections_refused_label_clash"][0]["error"] in out, out
    assert "UNRESOLVABLE LINKS: 1" in out and "nx-note://k/a -[cites]-> nx-note://k/b" in out


def test_a_refused_collection_alone_exits_1(tmp_path, monkeypatch):
    summary = ImportSummary(
        docs_imported=3, collections_refused_label_clash=_NEW_FIELDS["collections_refused_label_clash"])

    assert _invoke_cli_import(tmp_path, monkeypatch, summary).exit_code == 1


def test_skipped_not_live_and_changed_model_alone_exit_0_and_print(tmp_path, monkeypatch):
    summary = ImportSummary(
        docs_imported=3, collections_skipped_not_live=_NEW_FIELDS["collections_skipped_not_live"],
        collections_changed_model=_NEW_FIELDS["collections_changed_model"])

    result = _invoke_cli_import(tmp_path, monkeypatch, summary)

    assert result.exit_code == 0, result.output
    assert "cli-dead-label" in result.output and "cli-model-label" in result.output


# ── (6) the version gate ─────────────────────────────────────────────────────────────────────────────────────


def test_read_bundle_reads_format_3_and_refuses_format_4(tmp_path):
    for version in (3, 4):
        _write_bundle(tmp_path / f"v{version}.jsonl", [], [], version=version)

    header, records = read_bundle(tmp_path / "v3.jsonl")
    assert header["format_version"] == 3 and records == []

    with pytest.raises(ValueError, match=r"format_version 4 exceeds the maximum this build reads \(3\)"):
        read_bundle(tmp_path / "v4.jsonl")
