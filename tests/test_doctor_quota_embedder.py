# SPDX-License-Identifier: AGPL-3.0-or-later
"""7.86.1 shakeout: `nx doctor --check-quotas` names the model T3 writes are
embedded with, not the client's Python embedder.

On a fresh service-mode install without the `[local]` extra the Python EF
falls back to MiniLM while the service embeds bge-768 and T3 writes land in
`*__bge-base-en-v15-768__v1`. The report used to list MiniLM and go red on a
MiniLM tokenizer nothing on the install uses. It also printed "no transient
backoffs observed" from a counter that lives in one process, so the CLI could
only ever say so.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from nexus import embed_window
from nexus.commands.doctor import _collect_quota_report, _format_quota_report

BGE = "bge-base-en-v15-768"
MINILM = "minilm-l6-v2-384"


@pytest.fixture(autouse=True)
def _fresh_window_cache():
    embed_window.window_for_model.cache_clear()
    yield
    embed_window.window_for_model.cache_clear()


@pytest.fixture()
def bge_tokenizer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tok = Tokenizer(models.WordLevel(vocab={"[UNK]": 0}, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.save(str(tmp_path / "tokenizer.json"))
    monkeypatch.setenv("NX_SERVICE_BGE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture()
def client_ef_is_minilm(monkeypatch: pytest.MonkeyPatch):
    """The shakeout box: service mode, no [local] extra, so the Python
    embedder's own resolution says MiniLM."""
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)
    monkeypatch.setattr("nexus.db.local_ef._resolve_local_model", lambda *, warn: "all-MiniLM-L6-v2")


def test_service_embedded_local_install_reports_bge_not_the_client_minilm(
    client_ef_is_minilm, bge_tokenizer,
) -> None:
    with (
        patch("nexus.config.is_local_mode", return_value=True),
        patch("nexus.config.local_embed_profile", return_value="bge"),
        patch("nexus.config.get_credential", return_value=""),
    ):
        report = _collect_quota_report()
        text = _format_quota_report(report)

    models_ = report["voyage"]["models"]
    assert list(models_) == [BGE], models_
    assert models_[BGE]["embedding_dims"] == 768
    assert models_[BGE]["max_tokens"] == 512
    assert MINILM not in text and "minilm" not in text.lower(), text
    assert "tokenizer not found" not in text, text


def test_a_missing_service_tokenizer_is_still_reported_for_the_model_that_embeds(
    client_ef_is_minilm, tmp_path, monkeypatch,
) -> None:
    """The window row is kept for the real model: bge's own tokenizer, at the
    path the chunkers check, not MiniLM's."""
    monkeypatch.setenv("NX_SERVICE_BGE_DIR", str(tmp_path / "absent"))
    with (
        patch("nexus.config.is_local_mode", return_value=True),
        patch("nexus.config.local_embed_profile", return_value="bge"),
        patch("nexus.config.get_credential", return_value=""),
    ):
        text = _format_quota_report(_collect_quota_report())
    flagged = [ln for ln in text.splitlines() if "tokenizer not found" in ln]
    assert flagged and BGE in flagged[0] and str(tmp_path / "absent") in flagged[0], text
    assert "minilm" not in text.lower(), text


def test_voyage_profile_local_install_reports_the_voyage_models() -> None:
    with (
        patch("nexus.config.is_local_mode", return_value=True),
        patch("nexus.config.local_embed_profile", return_value="voyage"),
        patch("nexus.config.get_credential", return_value="sk-test"),
    ):
        report = _collect_quota_report()
    assert set(report["voyage"]["models"]) == {"voyage-code-3", "voyage-context-3"}
    assert all(c["embedding_dims"] == 1024 for c in report["voyage"]["models"].values())


def test_voyage_profile_without_a_key_says_so_instead_of_naming_a_model() -> None:
    with (
        patch("nexus.config.is_local_mode", return_value=True),
        patch("nexus.config.local_embed_profile", return_value="voyage"),
        patch("nexus.config.local_embed_model_choice", return_value="voyage-context-3"),
        patch("nexus.config.get_credential", return_value=""),
    ):
        report = _collect_quota_report()
        text = _format_quota_report(report)
    assert report["voyage"]["models"] == {}
    assert "requires a Voyage API key" in text, text


def test_retry_line_says_it_covers_this_process_only() -> None:
    report = {
        "vector_store": {"reachable": True, "detail": "ok", "limits": {}},
        "voyage": {"api_key_set": False, "target_rpm": 0, "models": {}},
        "retry": {"total_count": 0},
    }
    line = next(
        ln for ln in _format_quota_report(report).splitlines() if "Retry accumulator" in ln
    )
    assert "no transient backoffs observed in this process" in line, line
    assert "earlier nx runs are not counted" in line, line
