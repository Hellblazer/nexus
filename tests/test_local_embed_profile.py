# SPDX-License-Identifier: AGPL-3.0-or-later
"""``local_embed_profile()`` (RDR-210 Technical Design 2, bead nexus-yx833).

One function decides which embedder family new collections are minted for.
``voyage`` when ``local.embed_model`` names a Voyage model, or when no choice is
recorded and ``config.yml`` PERSISTS a Voyage key; ``bge`` otherwise. An
environment-only key serves Voyage collections but does not choose the profile.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nexus import config


def _write(cfg_dir: Path, body: dict) -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yml").write_text(yaml.safe_dump(body), encoding="utf-8")


@pytest.fixture
def cfg_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "nexus-config"
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(d))
    for var in ("VOYAGE_API_KEY", "NX_VOYAGE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return d


def test_voyage_choice_gives_voyage(cfg_dir: Path) -> None:
    _write(cfg_dir, {"local": {"embed_model": "voyage-context-3"}})
    assert config.local_embed_profile() == "voyage"


def test_bge_choice_gives_bge_even_with_a_persisted_key(cfg_dir: Path) -> None:
    _write(
        cfg_dir,
        {"local": {"embed_model": "bge-base-en-v1.5"}, "credentials": {"voyage_api_key": "k"}},
    )
    assert config.local_embed_profile() == "bge"


def test_no_choice_with_a_persisted_key_gives_voyage(cfg_dir: Path) -> None:
    _write(cfg_dir, {"credentials": {"voyage_api_key": "pa-persisted"}})
    assert config.local_embed_profile() == "voyage"


def test_no_choice_with_an_env_only_key_gives_bge(
    cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(cfg_dir, {})
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-env-only")
    monkeypatch.setenv("NX_VOYAGE_API_KEY", "pa-env-only")
    assert config.local_embed_profile() == "bge"


def test_no_choice_and_no_key_gives_bge(cfg_dir: Path) -> None:
    assert config.local_embed_profile() == "bge"


def test_blank_persisted_key_is_not_a_key(cfg_dir: Path) -> None:
    _write(cfg_dir, {"credentials": {"voyage_api_key": "   "}})
    assert config.local_embed_profile() == "bge"


def test_is_voyage_stays_choice_only_with_no_choice_recorded(cfg_dir: Path) -> None:
    # init.py relies on this meaning for its "Kept:" line; a persisted key must not flip it.
    _write(cfg_dir, {"credentials": {"voyage_api_key": "pa-persisted"}})
    assert config.local_embed_model_is_voyage() is False
