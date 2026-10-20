# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor's "Reranker" row (nexus-yuh4a, RDR-210 P3.1).

``GET /v1/status`` names the CHOSEN reranker in ``reranker`` even when its model files are absent and every rerank
degrades, so the name alone says nothing about whether search is reranked. ``reranker_available`` is a bare boolean
the engine reads live from the reranker's model files on every response. It reports files on disk, not that a rerank
would succeed. The row warns on ``false`` and stays green and "not applicable" for an engine that predates the key,
which a client must read as unknown, never as present.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.health import _check_reranker

_FETCH = "nexus.db.http_engine_status.fetch_engine_status"
_LABEL = "Reranker"


def _row(status):
    rows = _check_reranker(status)
    assert len(rows) == 1
    assert rows[0].label == _LABEL
    return rows[0]


def test_an_unreachable_engine_is_not_applicable_and_green() -> None:
    r = _row(None)
    assert r.ok is True and not r.warn and "not applicable" in r.detail


def test_a_probe_that_raises_is_not_applicable_and_green() -> None:
    with patch(_FETCH, side_effect=RuntimeError("boom")):
        (r,) = _check_reranker()
    assert r.ok is True and not r.warn and "not applicable" in r.detail


def test_an_engine_without_the_key_is_unknown_not_available_and_green() -> None:
    r = _row({"embedding_mode": "onnx-local", "embedding_profile": "bge", "reranker": "ms-marco-minilm-l6-v2"})
    assert r.ok is True and not r.warn
    assert "not applicable" in r.detail and "does not report" in r.detail


def test_an_available_reranker_is_green_and_named() -> None:
    r = _row({"reranker": "ms-marco-minilm-l6-v2", "reranker_available": True})
    assert r.ok is True and not r.warn
    assert "ms-marco-minilm-l6-v2" in r.detail and "files are present" in r.detail


def test_an_unavailable_reranker_warns_names_it_and_points_at_the_remedy() -> None:
    r = _row({"reranker": "ms-marco-minilm-l6-v2", "reranker_available": False})
    assert r.ok is False and r.warn is True
    assert "ms-marco-minilm-l6-v2" in r.detail and "files are absent" in r.detail and "without rerank" in r.detail
    assert not any("restart" in s for s in r.fix_suggestions)
    assert any("nx init --service" in s for s in r.fix_suggestions)


def test_an_unavailable_reranker_with_no_name_still_warns() -> None:
    r = _row({"reranker": None, "reranker_available": False})
    assert r.ok is False and r.warn is True and "no reranker" in r.detail


def test_a_non_boolean_value_warns_as_unreadable_never_as_available() -> None:
    for bad in ("true", 1, None):
        r = _row({"reranker": "x", "reranker_available": bad})
        assert r.ok is False and r.warn is True, bad
        assert "could not be read" in r.detail


@pytest.mark.t2_tenant
def test_the_default_sweep_makes_one_status_request_and_prints_the_row(monkeypatch) -> None:
    """The row is registered in the real `nx doctor` run: unregistering it from run_health_checks fails this."""
    calls = {"n": 0}

    def _fake_fetch(**kwargs):
        calls["n"] += 1
        return {"embedding_mode": "onnx-local", "reranker": "ms-marco-minilm-l6-v2", "reranker_available": False,
                "reaper": {"enabled": False}}

    monkeypatch.setattr("nexus.db.http_engine_status.fetch_engine_status", _fake_fetch)
    result = CliRunner().invoke(main, ["doctor"])
    assert calls["n"] == 1, result.output
    assert _LABEL in result.output
    assert "model files are absent" in result.output
