# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o residual 1, belt and braces: ``GET /version``'s ``embedding_mode`` never overrides a collection's own record.

The probe's transport now comes from ``config.yml`` (``tests/test_ec37o_probe_transport.py``). A client that still
reaches an engine a repository's env URL named would read an ``embedding_mode`` the engine chose. ``embedding_mode`` is an
engine-wide string; every decision it feeds that is made PER COLLECTION already takes the collection's recorded model
(the catalog row's ``embedding_model``, the name's token only when no row backs the name) over the string, and these tests
pin that: a mode that says "voyage" cannot put a Voyage-calibrated threshold on a bge-recorded collection, nor widen its
upsert batch past the memory-bound cap, nor lift its byte budget. (A mode that says ``onnx-local`` makes the same decisions
more cautious, never less: a smaller batch, no threshold.)
"""
from __future__ import annotations

import pytest

from nexus.db import http_vector_client as hvc
from nexus.search_engine import _threshold_for_collection_model

pytestmark = pytest.mark.usefixtures("cloud_mode")  # the tests name voyage models and a voyage engine mode: RDR-109 mode lint

BGE_ROW = {"embedding_model": "bge-base-en-v15-768", "content_type": "docs"}
VOYAGE_ROW = {"embedding_model": "voyage-context-3", "content_type": "docs"}
NAME = "docs__1-1__bge-base-en-v15-768__v1"
CFG = {"search": {"distance_threshold": {"docs": 0.65, "default": 0.55}}}


@pytest.mark.parametrize("forged", ["voyage", "dual", "unknown"])
def test_a_forged_voyage_family_cannot_widen_a_bge_recorded_collections_batch(monkeypatch, forged):
    monkeypatch.setattr(hvc, "_serving_embedding_mode", lambda: forged)
    assert hvc.per_collection_chunk_cap(NAME, row_resolver=lambda _n: BGE_ROW) == hvc._ONNX_LOCAL_UPSERT_CHUNK_CAP
    assert hvc._upsert_byte_budget(NAME, row_resolver=lambda _n: BGE_ROW) is None  # the Voyage token budget is not this collection's


def test_the_same_forged_mode_leaves_a_voyage_recorded_collection_on_its_own_split(monkeypatch):
    """Non-vacuity: the record, not a blanket rule, decides: the Voyage-recorded collection keeps the Voyage cap."""
    monkeypatch.setattr(hvc, "_serving_embedding_mode", lambda: "voyage")
    voyage_name = "docs__1-1__voyage-context-3__v1"
    assert hvc.per_collection_chunk_cap(voyage_name, row_resolver=lambda _n: VOYAGE_ROW) == 64


@pytest.mark.parametrize("dual", [False, True])
def test_a_forged_mode_cannot_put_a_voyage_threshold_on_a_bge_recorded_collection(dual):
    docs = {"content_type_of": lambda _n: "docs"}
    assert _threshold_for_collection_model(NAME, CFG, dual=dual, model_of=lambda _n: "bge-base-en-v15-768", **docs) is None
    # and the record wins over a name that says otherwise (a row whose model is bge under a voyage-looking name)
    voyage_looking = "docs__1-1__voyage-context-3__v1"
    assert _threshold_for_collection_model(voyage_looking, CFG, dual=dual, model_of=lambda _n: "bge-base-en-v15-768", **docs) is None
    assert _threshold_for_collection_model(voyage_looking, CFG, dual=dual, model_of=lambda _n: "voyage-context-3", **docs) == pytest.approx(0.65)
