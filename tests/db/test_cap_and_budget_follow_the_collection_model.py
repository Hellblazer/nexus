# SPDX-License-Identifier: AGPL-3.0-or-later
"""Chunk cap and byte budget follow the TARGET COLLECTION's model, not the engine's
single mode string (RDR-210 Technical Design 10, bead nexus-yx833).

A ``dual`` engine serves bge-768 and Voyage-1024 collections side by side. The ONNX
memory cap (nexus-33hpq) binds writes to a bge collection whatever the mode; the
Voyage cap and byte budget bind writes to a Voyage collection.
"""
from __future__ import annotations

import pytest

from nexus.db import http_vector_client as hvc

BGE = "knowledge__nexus-1-1__bge-base-en-v15-768__v1"
BGE_CODE = "code__nexus-1-1__bge-base-en-v15-768__v1"
VOY_DOCS = "docs__nexus-1-1__voyage-context-3__v1"
VOY_CODE = "code__nexus-1-1__voyage-code-3__v1"


def _mode(monkeypatch: pytest.MonkeyPatch, mode: str | None) -> None:
    monkeypatch.setattr(hvc, "_serving_embedding_mode", lambda: mode)


def _no_row(_name: str) -> None:
    return None


@pytest.mark.parametrize("mode", ["onnx-local", "voyage", "dual"])
@pytest.mark.parametrize("name", [BGE, BGE_CODE])
def test_a_bge_collection_gets_the_onnx_cap_and_no_budget_under_every_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str, name: str
) -> None:
    _mode(monkeypatch, mode)
    assert hvc.per_collection_chunk_cap(name, row_resolver=_no_row) == hvc._ONNX_LOCAL_UPSERT_CHUNK_CAP
    assert hvc._upsert_byte_budget(name, row_resolver=_no_row) is None


@pytest.mark.parametrize("mode", ["voyage", "dual"])
def test_a_voyage_collection_keeps_the_voyage_cap_and_budget_under_voyage_and_dual(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _mode(monkeypatch, mode)
    assert hvc.per_collection_chunk_cap(VOY_DOCS, row_resolver=_no_row) == hvc._CCE_UPSERT_CHUNK_CAP
    assert hvc._upsert_byte_budget(VOY_DOCS, row_resolver=_no_row) is None
    assert hvc.per_collection_chunk_cap(VOY_CODE, row_resolver=_no_row) == hvc._CODE_UPSERT_CHUNK_CAP
    assert hvc._upsert_byte_budget(VOY_CODE, row_resolver=_no_row) == hvc._CODE_UPSERT_BYTE_BUDGET


def test_onnx_local_keeps_its_engine_wide_memory_cap_for_every_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mode(monkeypatch, "onnx-local")
    for name in (VOY_DOCS, VOY_CODE):
        assert hvc.per_collection_chunk_cap(name, row_resolver=_no_row) == hvc._ONNX_LOCAL_UPSERT_CHUNK_CAP
        assert hvc._upsert_byte_budget(name, row_resolver=_no_row) is None


def test_the_catalog_row_model_wins_over_the_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _mode(monkeypatch, "dual")
    # A non-conformant name whose row says bge: the row is authoritative.
    row = {"embedding_model": "bge-base-en-v15-768", "content_type": "knowledge"}
    assert hvc.per_collection_chunk_cap("legacy-name", row_resolver=lambda _n: row) == hvc._ONNX_LOCAL_UPSERT_CHUNK_CAP
    assert hvc._upsert_byte_budget("legacy-name", row_resolver=lambda _n: row) is None


def test_an_unresolvable_collection_on_a_dual_engine_takes_the_smallest_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mode(monkeypatch, "dual")
    cap = hvc.per_collection_chunk_cap("legacy-name", row_resolver=_no_row)
    assert cap == min(hvc._ONNX_LOCAL_UPSERT_CHUNK_CAP, hvc._CCE_UPSERT_CHUNK_CAP)


def test_an_unknown_mode_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _mode(monkeypatch, None)
    assert hvc.per_collection_chunk_cap(VOY_DOCS, row_resolver=_no_row) == hvc._CCE_UPSERT_CHUNK_CAP
    assert hvc.per_collection_chunk_cap(VOY_CODE, row_resolver=_no_row) == hvc._CODE_UPSERT_CHUNK_CAP


def test_the_cap_and_the_budget_each_read_the_catalog_row_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _mode(monkeypatch, "dual")
    calls: list[str] = []

    def resolver(name: str) -> dict:
        calls.append(name)
        return {"embedding_model": "model-code", "content_type": "code"}

    hvc.per_collection_chunk_cap("legacy-name", row_resolver=resolver)
    assert calls == ["legacy-name"]
    calls.clear()
    hvc._upsert_byte_budget("legacy-name", row_resolver=resolver)
    assert calls == ["legacy-name"]


def test_an_unknown_mode_gives_a_bge_collection_the_memory_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _mode(monkeypatch, None)
    assert hvc.per_collection_chunk_cap(BGE, row_resolver=_no_row) == hvc._ONNX_LOCAL_UPSERT_CHUNK_CAP
    assert hvc._upsert_byte_budget(BGE, row_resolver=_no_row) is None
