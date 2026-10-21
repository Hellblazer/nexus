# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-210 P1.3 (nexus-11rjq): the orphan finder follows the served model set.

A collection is an orphan only when its model is in NEITHER the engine's
``/version`` ``embedding_models`` NOR the Voyage model set. The fake sits at
the HTTP boundary (``http_vector_client._get``), so the real finder, the real
``nx collection prune`` verb and the real doctor row run end to end.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands.collection import _find_dimension_mismatched_collections

_BGE = "bge-base-en-v15-768"
_VOYAGE_ALL = ["voyage-3", "voyage-code-3", "voyage-context-3"]

_MINILM = "knowledge__scratch__minilm-l6-v2-384__v1"
_BGE_COLL = "knowledge__notes__bge-base-en-v15-768__v1"
_VOYAGE_CODE = "code__repo__voyage-code-3__v1"
_VOYAGE_CTX = "knowledge__research__voyage-context-3__v1"

_ALL = [
    {"name": _MINILM, "count": 1},
    {"name": _BGE_COLL, "count": 20},
    {"name": _VOYAGE_CODE, "count": 30},
    {"name": _VOYAGE_CTX, "count": 40},
]


class _FakeEngineT3:
    """The slice of ``HttpVectorClient`` the finder touches."""

    _tenant = "default"

    def __init__(self, collections: list[dict], mode: str | None) -> None:
        self._collections = collections
        self._mode = mode

    def embedding_mode(self) -> str | None:
        return self._mode

    def list_collections(self) -> list[dict]:
        return list(self._collections)


def _serve(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any] | Exception) -> None:
    """Make the engine's ``GET /version`` answer ``body`` (or fail)."""

    def fake_get(path: str, *, tenant: str = "default") -> Any:
        assert path == "/version"
        if isinstance(body, Exception):
            raise body
        return body

    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)


def _names(mismatches: list[dict]) -> list[str]:
    return [m["name"] for m in mismatches]


@pytest.mark.parametrize(
    ("mode", "models"),
    [
        ("onnx-local", [_BGE]),
        ("dual", [_BGE, *_VOYAGE_ALL]),
    ],
    ids=["bge-only", "dual"],
)
def test_finder_lists_minilm_never_a_served_or_voyage_collection(
    monkeypatch: pytest.MonkeyPatch, mode: str, models: list[str],
) -> None:
    _serve(monkeypatch, {"embedding_mode": mode, "embedding_models": models})
    t3 = _FakeEngineT3(_ALL, mode)

    mismatches, skipped, label = _find_dimension_mismatched_collections(t3)

    assert _names(mismatches) == [_MINILM]
    assert mismatches[0]["declared_model"] == "minilm-l6-v2-384"
    assert mismatches[0]["declared_dim"] == 384
    assert skipped == 0
    assert label != "unknown"
    assert mode in label


def test_keyless_engine_does_not_orphan_a_voyage_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bge-only engine, Voyage collections present: they need the key, they
    are not orphans (the Voyage set is always counted as servable)."""
    _serve(monkeypatch, {"embedding_mode": "onnx-local", "embedding_models": [_BGE]})
    t3 = _FakeEngineT3(_ALL, "onnx-local")

    mismatches, _, _ = _find_dimension_mismatched_collections(t3)

    assert _VOYAGE_CODE not in _names(mismatches)
    assert _VOYAGE_CTX not in _names(mismatches)


def test_voyage_only_engine_never_lists_a_bge_collection_as_an_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sam, 2026-10-18: a keyed engine that merely lacks the bge files reports only
    the Voyage set, but `nx init` serves the bge collection again, so it is never
    an orphan. A stale local generation (minilm-384) still is."""
    _serve(monkeypatch, {"embedding_mode": "voyage", "embedding_models": _VOYAGE_ALL})
    mismatches, _, _ = _find_dimension_mismatched_collections(_FakeEngineT3(_ALL, "voyage"))
    assert _names(mismatches) == [_MINILM]


def test_prune_yes_on_a_voyage_only_engine_deletes_no_bge_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.db.collection_purge import CascadeCounts

    _serve(monkeypatch, {"embedding_mode": "voyage", "embedding_models": _VOYAGE_ALL})
    t3 = _FakeEngineT3(_ALL, "voyage")
    with patch(
        "nexus.db.collection_purge.purge_collection_cascade", return_value=CascadeCounts(),
    ) as cascade:
        result = _prune(t3, "--yes")
    assert result.exit_code == 0, result.output
    assert [call.args[1] for call in cascade.call_args_list] == [_MINILM]


@pytest.mark.parametrize(
    "body",
    [
        {"embedding_mode": "dual"},
        {"embedding_mode": "onnx-local", "embedding_models": None},
        {"embedding_mode": "onnx-local", "embedding_models": "bge"},
        {"embedding_mode": "onnx-local", "embedding_models": [_BGE, 7]},
        RuntimeError("engine unreachable"),
    ],
    ids=["absent", "null", "not-a-list", "non-string-member", "probe-fails"],
)
def test_models_unreadable_lists_nothing_and_says_cannot_verify(
    monkeypatch: pytest.MonkeyPatch, body: dict | Exception,
) -> None:
    _serve(monkeypatch, body)
    t3 = _FakeEngineT3(_ALL, "onnx-local")

    mismatches, skipped, label = _find_dimension_mismatched_collections(t3)

    assert mismatches == []
    assert skipped == 0
    assert label == "unknown"


def test_unrecognised_mode_string_does_not_decide_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old client meeting a mode it has never seen: the model set decides,
    the mode is display only, so a served collection is never listed."""
    _serve(monkeypatch, {"embedding_mode": "triple", "embedding_models": [_BGE, "voyage-3"]})
    mismatches, _, label = _find_dimension_mismatched_collections(_FakeEngineT3(_ALL, "triple"))
    assert _names(mismatches) == [_MINILM]
    assert "triple" in label


def test_empty_models_list_is_a_verdict_not_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An engine that really reports ``[]`` serves nothing locally: the Voyage set and
    the current local model (bge) still protect collections. Distinct from the field
    being absent."""
    _serve(monkeypatch, {"embedding_mode": "voyage", "embedding_models": []})
    mismatches, _, label = _find_dimension_mismatched_collections(_FakeEngineT3(_ALL, "voyage"))
    assert _names(mismatches) == [_MINILM]
    assert label != "unknown"


# ── nx collection prune against the fake ────────────────────────────────────


def _prune(t3: _FakeEngineT3, *args: str) -> Any:
    with patch("nexus.commands.collection._t3", return_value=t3):
        return CliRunner().invoke(main, ["collection", "prune", *args])


def test_prune_yes_against_dual_fake_deletes_nothing_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.db.collection_purge import CascadeCounts

    _serve(monkeypatch, {"embedding_mode": "dual", "embedding_models": [_BGE, *_VOYAGE_ALL]})
    t3 = _FakeEngineT3(_ALL, "dual")

    with patch(
        "nexus.db.collection_purge.purge_collection_cascade", return_value=CascadeCounts(),
    ) as cascade:
        result = _prune(t3, "--yes")

    assert result.exit_code == 0, result.output
    deleted = [call.args[1] for call in cascade.call_args_list]
    assert deleted == [_MINILM]
    for served in (_BGE_COLL, _VOYAGE_CODE, _VOYAGE_CTX):
        assert served not in deleted
        assert served not in result.output


def test_prune_yes_with_only_served_collections_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, {"embedding_mode": "dual", "embedding_models": [_BGE, *_VOYAGE_ALL]})
    t3 = _FakeEngineT3(_ALL[1:], "dual")

    with patch("nexus.db.collection_purge.purge_collection_cascade") as cascade:
        result = _prune(t3, "--yes")

    assert result.exit_code == 0, result.output
    cascade.assert_not_called()
    assert "No dimension-mismatched collections found" in result.output
    assert "dual" in result.output


def test_prune_yes_with_models_absent_deletes_nothing_and_says_cannot_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, {"embedding_mode": "onnx-local"})
    t3 = _FakeEngineT3(_ALL, "onnx-local")

    with patch("nexus.db.collection_purge.purge_collection_cascade") as cascade:
        result = _prune(t3, "--yes")

    assert result.exit_code == 0, result.output
    cascade.assert_not_called()
    assert "cannot verify" in result.output
    assert "embedding_models" in result.output
    assert "No dimension-mismatched collections found" not in result.output


# ── the doctor row shares the finder ────────────────────────────────────────


def test_doctor_row_lists_minilm_only_under_a_dual_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus import health

    _serve(monkeypatch, {"embedding_mode": "dual", "embedding_models": [_BGE, *_VOYAGE_ALL]})
    monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeEngineT3(_ALL, "dual"))

    (row,) = health._check_dimension_orphans()

    assert row.ok is False and row.warn is True
    assert _MINILM in row.detail
    for served in (_BGE_COLL, _VOYAGE_CODE, _VOYAGE_CTX):
        assert served not in row.detail


def test_doctor_row_models_absent_says_cannot_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus import health

    _serve(monkeypatch, {"embedding_mode": "onnx-local"})
    monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeEngineT3(_ALL, "onnx-local"))

    (row,) = health._check_dimension_orphans()

    assert row.ok is True
    assert "cannot verify" in row.detail
    assert _MINILM not in row.detail
