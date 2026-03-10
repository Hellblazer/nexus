# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for TuningConfig loading, defaults, overrides, and invalid values.

Added for RDR-032: Configuration Externalization (Phase 2).
"""
from pathlib import Path

import pytest
import yaml

from nexus.config import TuningConfig, _tuning_from_dict, get_tuning_config, load_config


# ── TuningConfig defaults ────────────────────────────────────────────────────


def test_tuning_config_defaults_match_previous_hardcoded_values() -> None:
    """TuningConfig() defaults must exactly match previous hard-coded constants."""
    cfg = TuningConfig()
    assert cfg.vector_weight == 0.7
    assert cfg.frecency_weight == 0.3
    assert cfg.file_size_threshold == 30
    assert cfg.decay_rate == 0.01
    assert cfg.code_chunk_lines == 150
    assert cfg.pdf_chunk_chars == 1500
    assert cfg.git_log_timeout == 30
    assert cfg.ripgrep_timeout == 10


def test_tuning_from_dict_empty_gives_defaults() -> None:
    """_tuning_from_dict({}) returns a TuningConfig with all defaults."""
    cfg = _tuning_from_dict({})
    assert cfg == TuningConfig()


def test_tuning_from_dict_partial_override() -> None:
    """Partial [tuning] section overrides only specified fields."""
    raw = {
        "scoring": {"vector_weight": 0.8, "frecency_weight": 0.2},
    }
    cfg = _tuning_from_dict(raw)
    assert cfg.vector_weight == 0.8
    assert cfg.frecency_weight == 0.2
    # Unspecified fields remain at defaults
    assert cfg.file_size_threshold == 30
    assert cfg.decay_rate == 0.01
    assert cfg.code_chunk_lines == 150


def test_tuning_from_dict_all_sections_override() -> None:
    """All [tuning] subsections can be overridden simultaneously."""
    raw = {
        "scoring": {
            "vector_weight": 1.0,
            "frecency_weight": 0.0,
            "file_size_threshold": 50,
        },
        "frecency": {"decay_rate": 0.05},
        "chunking": {
            "code_chunk_lines": 100,
            "pdf_chunk_chars": 2000,
        },
        "timeouts": {
            "git_log": 60,
            "ripgrep": 5,
        },
    }
    cfg = _tuning_from_dict(raw)
    assert cfg.vector_weight == 1.0
    assert cfg.frecency_weight == 0.0
    assert cfg.file_size_threshold == 50
    assert cfg.decay_rate == 0.05
    assert cfg.code_chunk_lines == 100
    assert cfg.pdf_chunk_chars == 2000
    assert cfg.git_log_timeout == 60
    assert cfg.ripgrep_timeout == 5


def test_tuning_from_dict_unknown_keys_ignored() -> None:
    """Unknown keys in [tuning] subsections are silently ignored."""
    raw = {
        "scoring": {"vector_weight": 0.6, "unknown_key": "ignored"},
        "unknown_section": {"also_ignored": True},
    }
    cfg = _tuning_from_dict(raw)
    assert cfg.vector_weight == 0.6
    assert cfg.frecency_weight == 0.3  # default


def test_tuning_from_dict_invalid_float_raises() -> None:
    """Non-numeric value for a float field raises ValueError."""
    with pytest.raises(ValueError, match="vector_weight"):
        _tuning_from_dict({"scoring": {"vector_weight": "not_a_number"}})


def test_tuning_from_dict_invalid_int_raises() -> None:
    """Non-numeric value for an int field raises ValueError."""
    with pytest.raises(ValueError, match="file_size_threshold"):
        _tuning_from_dict({"scoring": {"file_size_threshold": "many"}})


# ── get_tuning_config integration with .nexus.yml ───────────────────────────


def test_get_tuning_config_returns_defaults_when_no_nexus_yml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_tuning_config() returns defaults when no .nexus.yml exists."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = get_tuning_config(repo_root=tmp_path)
    assert cfg == TuningConfig()


def test_get_tuning_config_loads_from_nexus_yml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_tuning_config() reads [tuning] from .nexus.yml correctly."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".nexus.yml").write_text(
        yaml.dump({
            "tuning": {
                "scoring": {"vector_weight": 0.9, "frecency_weight": 0.1},
                "chunking": {"code_chunk_lines": 75},
            }
        })
    )
    cfg = get_tuning_config(repo_root=tmp_path)
    assert cfg.vector_weight == 0.9
    assert cfg.frecency_weight == 0.1
    assert cfg.code_chunk_lines == 75
    # Unspecified remain at defaults
    assert cfg.file_size_threshold == 30


def test_load_config_includes_tuning_section_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_config() includes tuning defaults even without a .nexus.yml tuning section."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config(repo_root=tmp_path)
    assert "tuning" in cfg
    assert cfg["tuning"]["scoring"]["vector_weight"] == 0.7
    assert cfg["tuning"]["frecency"]["decay_rate"] == 0.01
    assert cfg["tuning"]["chunking"]["code_chunk_lines"] == 150
    assert cfg["tuning"]["timeouts"]["ripgrep"] == 10


def test_get_tuning_config_nexus_yml_overrides_global_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-repo .nexus.yml tuning overrides global config defaults."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Global config has one tuning override
    global_dir = tmp_path / ".config" / "nexus"
    global_dir.mkdir(parents=True)
    (global_dir / "config.yml").write_text(
        yaml.dump({"tuning": {"timeouts": {"ripgrep": 20}}})
    )
    # Per-repo config has a different one
    (tmp_path / ".nexus.yml").write_text(
        yaml.dump({"tuning": {"timeouts": {"ripgrep": 5, "git_log": 45}}})
    )
    cfg = get_tuning_config(repo_root=tmp_path)
    # Per-repo wins for ripgrep; per-repo adds git_log
    assert cfg.ripgrep_timeout == 5
    assert cfg.git_log_timeout == 45


# ── TuningConfig backward-compat: no behavior change when absent ─────────────


def test_tuning_config_equals_old_hardcoded_values() -> None:
    """TuningConfig defaults match the exact constants replaced in each module."""
    cfg = TuningConfig()
    # scoring.py: _FILE_SIZE_THRESHOLD = 30, hybrid_score uses 0.7/0.3
    assert cfg.file_size_threshold == 30
    assert cfg.vector_weight == 0.7
    assert cfg.frecency_weight == 0.3
    # frecency.py: decay 0.01, timeout 30
    assert cfg.decay_rate == 0.01
    assert cfg.git_log_timeout == 30
    # chunker.py: _CHUNK_LINES = 150
    assert cfg.code_chunk_lines == 150
    # pdf_chunker.py: _DEFAULT_CHUNK_CHARS = 1500
    assert cfg.pdf_chunk_chars == 1500
    # ripgrep_cache.py: timeout = 10
    assert cfg.ripgrep_timeout == 10
