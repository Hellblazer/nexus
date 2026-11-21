# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.8 (nexus-p2m1x.28) on P3.1's client retry: when the engine refuses a cached name with the typed
unregistered-collection 422, the retry's one invalidation point (``registration_retry.reregister_after_unregistered``,
marked "P3.1 INVALIDATION POINT") also drops the row ``collection_mint`` holds for that name. Otherwise the writer
would keep reading a minted row the engine has just said is not registered, for up to the cache's TTL.
"""
from __future__ import annotations

import pytest

import nexus.collection_mint as collection_mint
import nexus.corpus as corpus
from nexus.catalog.collection_name import CollectionAttributes
from nexus.registration_retry import reregister_after_unregistered

_LABEL = "c4812"


@pytest.fixture(autouse=True)
def _clean_mint_cache():
    collection_mint.forget_all()
    yield
    collection_mint.forget_all()


def _minted(label: str) -> CollectionAttributes:
    return CollectionAttributes.minted(
        label=label, content_type="knowledge", owner_id="p38-forget-subject",
        embedding_model="bge-base-en-v15-768", model_version=1,
    )


def test_the_retry_after_an_unregistered_422_forgets_the_minted_row_of_that_name(monkeypatch) -> None:
    collection_mint._remember(_minted(_LABEL))
    assert collection_mint.minted_row(_LABEL) is not None, "guard: the mint cache holds the row"

    monkeypatch.setattr(corpus, "_read_collection_row", lambda name, registrar: {})
    monkeypatch.setattr(corpus, "_superseded_successor", lambda row: None)
    reregistered: list[str] = []
    monkeypatch.setattr(corpus, "ensure_collection_registered", lambda name, registrar=None: reregistered.append(name))

    reregister_after_unregistered(_LABEL, RuntimeError("422 collection not registered"))

    assert reregistered == [_LABEL], "guard: the retry re-registered the name"
    assert collection_mint.minted_row(_LABEL) is None, "the refused name's minted row is forgotten"


def test_the_retry_leaves_other_names_minted_rows_alone(monkeypatch) -> None:
    collection_mint._remember(_minted(_LABEL))
    collection_mint._remember(_minted("c4813"))
    monkeypatch.setattr(corpus, "_read_collection_row", lambda name, registrar: {})
    monkeypatch.setattr(corpus, "_superseded_successor", lambda row: None)
    monkeypatch.setattr(corpus, "ensure_collection_registered", lambda name, registrar=None: None)

    reregister_after_unregistered(_LABEL, RuntimeError("422 collection not registered"))

    assert collection_mint.minted_row("c4813") is not None
