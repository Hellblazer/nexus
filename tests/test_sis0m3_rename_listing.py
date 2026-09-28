# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sis0m.3 (shakeout 7.64.1 Surface F F9): after ``nx collection rename
A B``, ``collection list`` showed A with 0 chunks and ``collection info A``
said "collection not found". The engine keeps A's catalog row as a retired
tombstone on purpose (nexus-cecqy); the client listed it as a live collection.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.http_vector_client import HttpVectorClient

pytestmark = pytest.mark.integration

_OLD = "docs__sis0m3-old__bge-base-en-v15-768__v1"
_NEW = "docs__sis0m3-new__bge-base-en-v15-768__v1"


def test_a_renamed_collection_is_not_listed_as_live(t2_service_env, tmp_path):
    from nexus.doc_indexer import index_markdown

    client = HttpVectorClient(tenant=t2_service_env)
    md = tmp_path / "sis0m3.md"
    md.write_text("# sis0m3\n\n" + " ".join(f"sis0m3 sentence {j}." for j in range(60)))
    assert index_markdown(md, corpus="sis0m3", t3=client, collection_name=_OLD)

    runner = CliRunner()
    with patch("nexus.commands.collection._t3", return_value=client):
        renamed = runner.invoke(main, ["collection", "rename", _OLD, _NEW])
        assert renamed.exit_code == 0, renamed.output

        listed = runner.invoke(main, ["collection", "list"])
        listed_all = runner.invoke(main, ["collection", "list", "--all"])
        info_old = runner.invoke(main, ["collection", "info", _OLD])

    assert listed.exit_code == 0, listed.output
    names = [ln.split()[0] for ln in listed.output.splitlines()[1:] if ln.strip()]
    assert _NEW in names, listed.output
    assert _OLD not in names, listed.output

    # --all still shows the tombstone, labelled with its successor.
    assert listed_all.exit_code == 0, listed_all.output
    old_line = next(ln for ln in listed_all.output.splitlines() if ln.startswith(_OLD))
    assert f"superseded->{_NEW}" in old_line, listed_all.output

    # info on the old name names where it went, not "not found".
    assert info_old.exit_code != 0
    assert f"renamed to {_NEW}" in info_old.output, info_old.output
    assert "not found" not in info_old.output, info_old.output


def test_a_retired_name_still_holding_chunks_stays_listed(monkeypatch):
    """Critique of e91a9daed: only an EMPTY tombstone is hidden. One that holds
    chunks (written after the rename, or never fully moved) must stay visible in
    plain ``list``, labelled with its successor."""
    from types import SimpleNamespace

    import nexus.commands.collection as coll

    listed = [
        {"name": _NEW, "count": 3, "stored_count": 3},
        {"name": _OLD, "count": 0, "stored_count": 2},
    ]
    monkeypatch.setattr(coll, "_t3", lambda: SimpleNamespace(list_collections=lambda strict=False: listed))
    rows = {
        _NEW: {"name": _NEW, "superseded_by": "", "lifecycle_state": "live"},
        _OLD: {"name": _OLD, "superseded_by": _NEW, "lifecycle_state": "live"},
    }
    monkeypatch.setattr(coll, "_catalog_collection_rows", lambda: (rows, ""))

    out = CliRunner().invoke(main, ["collection", "list"])
    assert out.exit_code == 0, out.output
    old_line = next(ln for ln in out.output.splitlines() if ln.startswith(_OLD))
    assert f"superseded->{_NEW}" in old_line, out.output


def test_a_renamed_note_keeps_one_identity_under_the_new_name(t2_service_env, tmp_path):
    """Engine half (Surface F F9): after renaming a knowledge collection, the new
    row carries the new subject as its owner, the note's source_uri names the
    new collection, and re-putting the same title into it updates that note
    rather than minting a second document."""
    from nexus.aspect_readers import uri_for
    from nexus.catalog.factory import make_catalog_reader
    from nexus.corpus import t3_collection_name

    client = HttpVectorClient(tenant=t2_service_env)
    note = tmp_path / "rn-note.md"
    note.write_text("sis0m3 renamed note body, one identity across a rename")

    def _put(subject: str):
        with patch("nexus.commands.store._t3", return_value=client):
            return CliRunner().invoke(main, ["store", "put", str(note), "-c", subject, "-t", "rn-note"])

    assert (r := _put("sis0m3-src")).exit_code == 0, r.output
    old = t3_collection_name("sis0m3-src", t3=client)
    new = old.replace("sis0m3-src", "sis0m3-dst")
    with patch("nexus.commands.collection._t3", return_value=client):
        renamed = CliRunner().invoke(main, ["collection", "rename", old, new])
    assert renamed.exit_code == 0, renamed.output

    reader = make_catalog_reader()
    assert reader is not None
    row = next(c for c in reader.list_collections() if c.get("name") == new)
    assert row.get("owner_id") == "sis0m3-dst", row
    doc = reader.by_source_uri(uri_for(new, "rn-note"))
    assert doc is not None, "the note's source_uri still names the old collection"
    assert doc.physical_collection == new
    assert reader.by_source_uri(uri_for(old, "rn-note")) is None

    assert (r := _put("sis0m3-dst")).exit_code == 0, r.output
    titled = [d for d in reader.list_by_collection(new) if d.title == "rn-note"]
    assert len(titled) == 1, [(d.tumbler, d.source_uri) for d in titled]
