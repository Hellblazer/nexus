# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-144 P1: stable fastembed cache-path chokepoint.

Foundation phase. The single embedding-function construction site
(``LocalEmbeddingFunction._init_ef``) must resolve a stable, XDG-respecting
on-disk cache for the Tier-1 (bge-768) fastembed model and thread it into
``TextEmbedding(cache_dir=...)``. Without this, fastembed downloads to a
volatile ``tempfile.gettempdir()/fastembed_cache`` that is wiped on reboot,
re-downloading the model on every cold start and breaking offline-after-
first-run for the launchd-spawned daemon/MCP processes that never see the
nx-init shell env (RDR-144 CRITICAL-1).
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.config import fastembed_cache_dir
from nexus.db.local_ef import LocalEmbeddingFunction, _TIER0_MODEL, _TIER1_MODEL


# ── fastembed_cache_dir() resolution ──────────────────────────────────────────


class TestFastembedCacheDirResolution:
    def test_config_key_takes_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``local.fastembed_cache_path`` in config.yml wins over XDG."""
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        configured = tmp_path / "my-models"
        (cfg_dir / "config.yml").write_text(
            f"local:\n  fastembed_cache_path: {configured}\n"
        )

        assert fastembed_cache_dir() == configured

    def test_config_key_expands_tilde(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hand-edited ``~/models`` config value resolves to $HOME, not a
        literal ``./~/models`` created relative to the daemon's cwd."""
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (cfg_dir / "config.yml").write_text(
            "local:\n  fastembed_cache_path: ~/models\n"
        )

        resolved = fastembed_cache_dir()
        assert resolved == tmp_path / "home" / "models"
        assert "~" not in str(resolved)

    def test_xdg_fallback_when_no_config_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No config key → ``$XDG_DATA_HOME/nexus/fastembed_cache``."""
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        xdg = tmp_path / "xdg"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg))

        assert fastembed_cache_dir() == xdg / "nexus" / "fastembed_cache"

    def test_home_fallback_when_no_config_no_xdg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No config key, no XDG → ``~/.local/share/nexus/fastembed_cache``."""
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

        assert (
            fastembed_cache_dir()
            == tmp_path / "home" / ".local" / "share" / "nexus" / "fastembed_cache"
        )

    def test_resolution_is_reboot_stable_never_consults_tmpdir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Load-bearing: the resolver NEVER roots the cache in the OS temp
        dir — that is the property that survives a reboot ($TMPDIR is wiped,
        a stable XDG/home dir is not). Proven by making ``gettempdir`` fatal:
        if the resolver consulted it, this would raise."""
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        monkeypatch.setattr(
            tempfile,
            "gettempdir",
            MagicMock(side_effect=AssertionError("resolver must not consult $TMPDIR")),
        )

        resolved = fastembed_cache_dir()
        assert resolved == tmp_path / "xdg" / "nexus" / "fastembed_cache"

    def test_resolver_creates_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pure resolver — mirrors ``_default_local_path`` (nothing created)."""
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

        resolved = fastembed_cache_dir()
        assert not resolved.exists()


# ── _init_ef() threads cache_dir into TextEmbedding ───────────────────────────


class TestInitEfThreadsCacheDir:
    def test_tier1_passes_resolved_cache_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier-1 fastembed construction passes ``cache_dir=`` (today it
        passes none — this is the delta the chokepoint introduces)."""
        cache = tmp_path / "stable-cache"
        monkeypatch.setattr("nexus.config.fastembed_cache_dir", lambda: cache)

        ef = LocalEmbeddingFunction(model_name=_TIER1_MODEL)
        fake_te = MagicMock()
        fake_module = MagicMock(TextEmbedding=fake_te)
        with patch.dict("sys.modules", {"fastembed": fake_module}):
            ef._init_ef()

        fake_te.assert_called_once_with(
            model_name=_TIER1_MODEL, cache_dir=str(cache)
        )

    def test_tier1_creates_cache_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The construction site materialises the stable dir so fastembed
        has somewhere to land the download."""
        cache = tmp_path / "stable-cache"
        assert not cache.exists()
        monkeypatch.setattr("nexus.config.fastembed_cache_dir", lambda: cache)

        ef = LocalEmbeddingFunction(model_name=_TIER1_MODEL)
        fake_module = MagicMock(TextEmbedding=MagicMock())
        with patch.dict("sys.modules", {"fastembed": fake_module}):
            ef._init_ef()

        assert cache.is_dir()

    def test_tier0_unaffected_no_cache_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier-0 bundled ONNX MiniLM does not use fastembed; cache-path is
        irrelevant and must not be threaded (the resolver isn't even read)."""
        sentinel = MagicMock(side_effect=AssertionError("resolver must not run for tier-0"))
        monkeypatch.setattr("nexus.config.fastembed_cache_dir", sentinel)

        ef = LocalEmbeddingFunction(model_name=_TIER0_MODEL)
        fake_onnx = MagicMock()
        fake_module = MagicMock(ONNXMiniLM_L6_V2=fake_onnx)
        with patch.dict(
            "sys.modules",
            {"chromadb.utils.embedding_functions": fake_module},
        ):
            ef._init_ef()

        fake_onnx.assert_called_once_with()
