# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1: t3_stores factory functions — PersistentClient injection and path configuration."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.db.t3 import T3Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_config(tmp_path: Path) -> dict:
    """Return a minimal config dict pointing all four stores at tmp_path subdirs."""
    return {
        "chromadb": {
            "code_path":      str(tmp_path / "code"),
            "docs_path":      str(tmp_path / "docs"),
            "rdr_path":       str(tmp_path / "rdr"),
            "knowledge_path": str(tmp_path / "knowledge"),
            "path":           "",
        }
    }


# ---------------------------------------------------------------------------
# P1a: Each factory returns a T3Database backed by a PersistentClient
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory_name,path_key", [
    ("t3_code",      "code_path"),
    ("t3_docs",      "docs_path"),
    ("t3_rdr",       "rdr_path"),
    ("t3_knowledge", "knowledge_path"),
])
def test_factory_returns_persistent_t3database(
    factory_name: str, path_key: str, tmp_path: Path
) -> None:
    """Each factory returns a T3Database with an is_persistent PersistentClient."""
    from nexus.db import t3_stores  # import under test

    cfg = _fake_config(tmp_path)
    expected_path = str(Path(cfg["chromadb"][path_key]).expanduser())
    factory = getattr(t3_stores, factory_name)

    with (
        patch("nexus.db.t3_stores.load_config", return_value=cfg),
        patch("nexus.db.t3_stores.get_credential", return_value="vkey-test"),
    ):
        db = factory()

    assert isinstance(db, T3Database)
    settings = db._client.get_settings()
    assert settings.is_persistent is True
    assert settings.persist_directory == expected_path


# ---------------------------------------------------------------------------
# P1b: Missing voyage_api_key raises RuntimeError before opening the store
# ---------------------------------------------------------------------------

def test_factory_raises_when_voyage_key_missing(tmp_path: Path) -> None:
    """RuntimeError raised when voyage_api_key is absent."""
    from nexus.db import t3_stores

    cfg = _fake_config(tmp_path)

    with (
        patch("nexus.db.t3_stores.load_config", return_value=cfg),
        patch("nexus.db.t3_stores.get_credential", return_value=""),
    ):
        with pytest.raises(RuntimeError, match="voyage_api_key not configured"):
            t3_stores.t3_code()


# ---------------------------------------------------------------------------
# P1c: Missing/empty path config raises RuntimeError
# ---------------------------------------------------------------------------

def test_factory_raises_when_path_not_configured(tmp_path: Path) -> None:
    """RuntimeError raised when the store path key is absent from config."""
    from nexus.db import t3_stores

    cfg = {"chromadb": {}}  # no path keys at all

    with (
        patch("nexus.db.t3_stores.load_config", return_value=cfg),
        patch("nexus.db.t3_stores.get_credential", return_value="vkey-test"),
    ):
        with pytest.raises(RuntimeError, match="T3 store not configured"):
            t3_stores.t3_docs()


# ---------------------------------------------------------------------------
# P1d: t3_knowledge() falls back to legacy "path" key when knowledge_path is empty
# ---------------------------------------------------------------------------

def test_t3_knowledge_falls_back_to_legacy_path(tmp_path: Path) -> None:
    """t3_knowledge() uses chromadb.path when knowledge_path is absent/empty."""
    from nexus.db import t3_stores

    legacy_path = str(tmp_path / "legacy_chroma")
    cfg = {
        "chromadb": {
            "code_path":      str(tmp_path / "code"),
            "docs_path":      str(tmp_path / "docs"),
            "rdr_path":       str(tmp_path / "rdr"),
            "knowledge_path": "",        # empty — should fall back
            "path":           legacy_path,
        }
    }

    with (
        patch("nexus.db.t3_stores.load_config", return_value=cfg),
        patch("nexus.db.t3_stores.get_credential", return_value="vkey-test"),
    ):
        db = t3_stores.t3_knowledge()

    assert isinstance(db, T3Database)
    settings = db._client.get_settings()
    assert settings.is_persistent is True
    assert settings.persist_directory == legacy_path


# ---------------------------------------------------------------------------
# P1e: Config defaults include all four path keys with non-empty values
# ---------------------------------------------------------------------------

def test_config_defaults_contain_four_path_keys() -> None:
    """_DEFAULTS['chromadb'] must include all four *_path keys with non-empty defaults."""
    from nexus.config import _DEFAULTS

    chroma = _DEFAULTS["chromadb"]
    for key in ("code_path", "docs_path", "rdr_path", "knowledge_path"):
        assert key in chroma, f"_DEFAULTS['chromadb'] missing '{key}'"
        assert chroma[key], f"_DEFAULTS['chromadb']['{key}'] must be non-empty"

    # Legacy alias present but empty
    assert "path" in chroma
    assert chroma["path"] == ""
