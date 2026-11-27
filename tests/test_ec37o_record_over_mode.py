# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o residual 1: what ``GET /version``'s ``embedding_mode`` can and cannot change for a collection with a record.

The probe's transport now comes from ``config.yml`` (``tests/test_ec37o_probe_transport.py``). A client that still reaches an engine
a repository's env URL named (an accepted limit) would read an ``embedding_mode`` that engine chose. ``embedding_mode`` is an
engine-wide string. What it feeds, per decision:

* The Voyage-calibrated distance threshold: decided PER COLLECTION by the collection's recorded model, in every mode. A forged
  ``voyage`` or ``dual`` cannot put a Voyage threshold on a bge collection. Pinned here.
* The upsert chunk cap: a forged ``voyage`` or ``dual`` cannot widen a bge-recorded collection's batch (pinned here). A forged
  ``onnx-local`` narrows every batch to the memory cap: slower, never less cautious. That engine-wide rule is a pinned design
  (``tests/db/test_cap_and_budget_follow_the_collection_model.py``).
* The upsert byte budget: a forged ``voyage`` or ``dual`` cannot lift a voyage-recorded code collection's budget, but a forged
  ``onnx-local`` does, because ``onnx-local`` is engine-wide by the same pinned design (a real onnx-local engine embeds locally,
  and has no Voyage token ceiling). The last test below records that as known, not as a guarantee: the consequence is a request
  the Voyage side may refuse, never a disclosure, and reaching it takes the accepted env-URL limit.
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


def test_known_a_forged_onnx_local_lifts_the_byte_budget_of_a_voyage_recorded_collection(monkeypatch):
    """Characterisation of the accepted limit above: the engine-wide ``onnx-local`` rule is not overridden by the record."""
    monkeypatch.setattr(hvc, "_serving_embedding_mode", lambda: "onnx-local")
    code_name = "code__1-1__voyage-code-3__v1"
    row = {"embedding_model": "voyage-code-3", "content_type": "code"}
    assert hvc._upsert_byte_budget(code_name, row_resolver=lambda _n: row) is None
    monkeypatch.setattr(hvc, "_serving_embedding_mode", lambda: "voyage")
    assert hvc._upsert_byte_budget(code_name, row_resolver=lambda _n: row) == hvc._CODE_UPSERT_BYTE_BUDGET
