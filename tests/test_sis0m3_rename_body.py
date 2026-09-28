# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sis0m.3: the rename request carries the new row's content_type and
owner_id, derived client-side from the new name (the engine does not parse
names, RDR-204). Fast-loop pins; the engine behaviour is covered by
tests/test_sis0m3_rename_listing.py and CatalogRenameIdentityCascadeTest."""
from __future__ import annotations

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.corpus import collection_registration_kwargs, collection_type_and_owner


@pytest.mark.parametrize(
    "name, expected",
    [
        ("knowledge__distributed-systems__voyage-context-3__v1", ("knowledge", "distributed-systems")),
        ("code__1-1__voyage-code-3__v1", ("code", "1-1")),
        ("knowledge__gamma_sub", ("knowledge", "gamma_sub")),
        ("hren__keep-tgt", ("hren", "keep-tgt")),
        ("my-notes", ("knowledge", "my-notes")),
    ],
)
def test_type_and_owner_agree_with_registration(name, expected):
    assert collection_type_and_owner(name) == expected
    kw = collection_registration_kwargs(name)
    assert (kw["content_type"], kw["owner_id"]) == expected


@pytest.mark.parametrize("name", ["__orphan", "docs__", "docs____x"])
def test_a_shapeless_name_raises(name):
    with pytest.raises(ValueError):
        collection_type_and_owner(name)


def _capture(monkeypatch):
    sent: list[dict] = []
    client = HttpCatalogClient.__new__(HttpCatalogClient)
    monkeypatch.setattr(client, "_post", lambda path, body, **kw: sent.append(body) or {"renamed": {}}, raising=False)
    return client, sent


def test_rename_sends_the_new_names_type_and_owner(monkeypatch):
    client, sent = _capture(monkeypatch)
    client.rename_collection_cascade("knowledge__old-subj", "knowledge__new-subj")
    assert sent[0]["content_type"] == "knowledge"
    assert sent[0]["owner_id"] == "new-subj"


def test_cross_model_rename_sends_no_attributes(monkeypatch):
    client, sent = _capture(monkeypatch)
    client.rename_collection_cascade("docs__a__m__v1", "docs__a__n__v1", cross_model=True)
    assert "owner_id" not in sent[0] and "content_type" not in sent[0]
