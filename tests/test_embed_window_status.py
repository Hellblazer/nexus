# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ajvjx: nx doctor shows when a small-window model's chunks go
unchecked because its tokenizer is missing.

nexus-spujb made a missing tokenizer warn and skip the window check rather
than refuse to index. On a genuinely broken local install that reopens the
silent truncation, with only a structlog line to say so; the quota report
is the visible backstop.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from nexus import embed_window
from nexus.commands.doctor import _format_quota_report
from nexus.corpus import CANONICAL_EMBEDDING_MODELS
from nexus.embed_window import window_status

BGE = "bge-base-en-v15-768"


@pytest.fixture(autouse=True)
def _fresh_window_cache():
    embed_window.window_for_model.cache_clear()
    yield
    embed_window.window_for_model.cache_clear()


def _tokenizer(path: Path) -> None:
    tok = Tokenizer(models.WordLevel(vocab={"[UNK]": 0}, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.save(str(path))


def _report(caps: dict) -> dict:
    return {
        "vector_store": {"reachable": True, "detail": "ok", "limits": {}},
        "voyage": {"api_key_set": False, "target_rpm": 0, "models": {BGE: caps}},
        "retry": {"total_count": 0},
    }


def test_status_is_enforced_when_the_tokenizer_is_there(tmp_path, monkeypatch) -> None:
    _tokenizer(tmp_path / "tokenizer.json")
    monkeypatch.setenv("NX_SERVICE_BGE_DIR", str(tmp_path))
    assert window_status(BGE) == {
        "max_tokens": 512,
        "enforced": True,
        "tokenizer_path": str(tmp_path / "tokenizer.json"),
    }


def test_status_is_not_enforced_and_names_the_path_when_it_is_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NX_SERVICE_BGE_DIR", str(tmp_path / "absent"))
    status = window_status(BGE)
    assert status is not None and status["enforced"] is False
    assert status["tokenizer_path"] == str(tmp_path / "absent" / "tokenizer.json")


def test_a_model_with_no_window_to_enforce_has_no_status() -> None:
    for model in CANONICAL_EMBEDDING_MODELS:
        assert window_status(model) is None, model


def test_doctor_flags_an_unenforced_window(tmp_path) -> None:
    missing = str(tmp_path / "absent" / "tokenizer.json")
    out = _format_quota_report(_report({
        "max_tokens": 512, "embedding_dims": 768,
        "window": {"max_tokens": 512, "enforced": False, "tokenizer_path": missing},
    }))
    flagged = [line for line in out.splitlines() if "tokenizer not found" in line]
    assert flagged and "✗" in flagged[0] and missing in flagged[0], out


def test_doctor_is_quiet_when_the_window_is_enforced(tmp_path) -> None:
    out = _format_quota_report(_report({
        "max_tokens": 512, "embedding_dims": 768,
        "window": {"max_tokens": 512, "enforced": True, "tokenizer_path": "x"},
    }))
    assert "tokenizer not found" not in out
