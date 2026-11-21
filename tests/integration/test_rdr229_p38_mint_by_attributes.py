# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.8 (nexus-p2m1x.28) against the real engine: a new knowledge subject is registered by
its attributes and the engine mints its label.

The step's test (RDR Test Plan, item 8): the first ``store_put`` to a new knowledge subject
registers a collection whose label is the minted ``c<id>``, with the subject in ``owner_id`` and the
row's ``embedding_model`` stamped by the registration (no name is parsed for it); a second
``store_put`` to the same subject finds that collection through ``for_tuple`` and writes there,
so the tuple still has one live collection and no conformant name was ever created in T3.

Failing first: before P3.8 the first put registers the rendered conformant name
``knowledge__<subject>__<model>__v1``.
"""
from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from nexus.catalog.engine_writes.floors import COLLECTION_MINT_MIN_ENGINE_VERSION
from nexus.catalog.factory import make_catalog_reader
from nexus.corpus import _resolve_promoted_model_token
from nexus.db.http_engine_status import fetch_engine_status
from nexus.db.http_vector_client import HttpVectorClient
from nexus.engine_version import parse_engine_version
from nexus.mcp.core import store_put

pytestmark = pytest.mark.integration

_MINTED = re.compile(r"^c[0-9]+$")


def _put(client: HttpVectorClient, subject: str, title: str, body: str) -> None:
    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content=body, collection=subject, title=title)


def _row(subject: str):
    reader = make_catalog_reader()
    assert reader is not None
    try:
        return reader.find_collection("knowledge", subject, _resolve_promoted_model_token("knowledge", for_write=False))
    finally:
        close = getattr(reader, "close", None)
        if close is not None:
            close()


def _engine_serves_collection_id() -> bool:
    """Does this engine put ``collection_id`` on a collection row (P3.5a, shipped with the mint, engine-service-v0.1.201)?

    An older engine sends no key and an engine that sends null says the same to the client, so ``None`` cannot tell them
    apart from a bug: the release says, from the floor the client itself reads for the mint
    (``COLLECTION_MINT_MIN_ENGINE_VERSION``). The gate jar is stamped with the client's pin (0.1.199), below that floor, though
    it is built from this tree and serves the column; there the engine's own ``GET /v1/status`` says so, through the additive
    boolean ``collection_tuple_index_present`` that every engine carrying the mint sends and no older one does.
    """
    reader = make_catalog_reader()
    assert reader is not None
    try:
        release = parse_engine_version(reader.engine_release_version())
    finally:
        close = getattr(reader, "close", None)
        if close is not None:
            close()
    if release is not None and release >= COLLECTION_MINT_MIN_ENGINE_VERSION:
        return True
    status = fetch_engine_status() or {}
    return isinstance(status.get("collection_tuple_index_present"), bool)


def test_the_first_put_to_a_new_subject_registers_a_minted_label_by_attributes(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "p38-mint-first-put"
    assert _row(subject) is None, "guard: the subject is new"

    _put(client, subject, "p38 first", "p38 the first note of a new knowledge subject")

    row = _row(subject)
    assert row is not None, "the put registered a collection for the subject's tuple"
    assert _MINTED.match(row.label), f"the engine minted the label, got {row.label!r}"
    assert row.owner_id == subject
    assert row.embedding_model == _resolve_promoted_model_token("knowledge", for_write=False)
    if _engine_serves_collection_id():
        assert row.collection_id is not None and row.label == f"c{row.collection_id}"
    else:
        assert row.collection_id is None, "an engine older than the mint sends no id, which the client reads as None"


def test_a_second_put_finds_the_minted_collection_through_for_tuple(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "p38-mint-second-put"
    model = _resolve_promoted_model_token("knowledge", for_write=False)

    _put(client, subject, "p38 one", "p38 the first note, which mints the subject's collection")
    first = _row(subject)
    assert first is not None and _MINTED.match(first.label), f"guard: minted, got {first and first.label!r}"

    _put(client, subject, "p38 two", "p38 the second note, which must land in the same collection")

    again = _row(subject)
    assert again is not None and again.label == first.label, "the second put kept the one collection"
    assert client.collection_exists(first.label)
    assert not client.collection_exists(f"knowledge__{subject}__{model}__v1"), (
        "no conformant name was rendered into T3 for the subject"
    )
