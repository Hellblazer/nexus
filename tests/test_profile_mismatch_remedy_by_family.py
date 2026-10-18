# SPDX-License-Identifier: AGPL-3.0-or-later
"""The profile-mismatch remedy names the family the collection needs (RDR-210 TD10, nexus-yx833).

On a ``dual`` engine the engine-wide mode string says nothing about which family a
refused collection needs, so the message reads the collection's own model segment.
"""
from __future__ import annotations

import pytest

from nexus.errors import SearchEmbeddingProfileMismatchError

VOY = "docs__nexus-1-1__voyage-context-3__v1"
BGE = "knowledge__nexus-1-1__bge-base-en-v15-768__v1"
MARKER = "this install's profile names a model this mode cannot serve"


_ROWS = {
    VOY: {"embedding_model": "voyage-context-3"},
    BGE: {"embedding_model": "bge-base-en-v15-768"},
}


@pytest.fixture(autouse=True)
def _catalog_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", lambda name: _ROWS.get(name))


def _err(*cols: str, mode: str | None = "dual") -> str:
    return str(
        SearchEmbeddingProfileMismatchError(
            serving_mode=mode, mismatches={c: f"{MARKER}: {c}" for c in cols}, collections=list(cols)
        )
    )


def test_a_voyage_collection_names_the_service_key_remedy_and_not_bge_files() -> None:
    text = _err(VOY)
    assert "NX_VOYAGE_API_KEY" in text
    assert "bge" not in text.lower().replace("embedding", "")  # no bge-model-files remedy


def test_a_bge_collection_names_the_bge_model_remedy_and_not_the_voyage_key() -> None:
    text = _err(BGE)
    assert "nx init" in text
    assert "NX_VOYAGE_API_KEY" not in text


def test_mixed_families_name_both_remedies() -> None:
    text = _err(VOY, BGE)
    assert "NX_VOYAGE_API_KEY" in text and "nx init" in text


def test_an_unclassifiable_collection_keeps_the_original_two_causes() -> None:
    text = _err("legacy-name", mode="onnx-local")
    assert "(1) Managed/cloud service intended" in text
    assert "NX_VOYAGE_API_KEY" in text


def test_the_mode_is_still_reported() -> None:
    assert "(dual)" in _err(VOY)


def test_classify_vector_service_error_passes_the_group_so_the_remedy_narrows() -> None:
    from nexus.errors import classify_vector_service_error

    out = classify_vector_service_error(RuntimeError(MARKER + ": x"), t3=None, collections=[VOY, VOY])
    text = str(out)
    assert "NX_VOYAGE_API_KEY" in text and "nx init" not in text
