"""AC6: Config merges global + per-repo YAML with env var overrides."""
from pathlib import Path

import pytest
import yaml

from nexus.config import load_config


def test_config_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without any config files, defaults are returned."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = load_config(repo_root=tmp_path)
    assert config["server"]["port"] == 7890
    assert config["server"]["headPollInterval"] == 10
    assert config["embeddings"]["rerankerModel"] == "rerank-2.5"
    assert "codeModel" not in config["embeddings"]
    assert "docsModel" not in config["embeddings"]
    assert config["pm"]["archiveTtl"] == 90


def test_config_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-repo config overrides global; global fills missing per-repo keys."""
    global_dir = tmp_path / ".config" / "nexus"
    global_dir.mkdir(parents=True)
    (global_dir / "config.yml").write_text(
        yaml.dump({"server": {"port": 7890, "headPollInterval": 10}})
    )

    (tmp_path / ".nexus.yml").write_text(yaml.dump({"server": {"port": 9999}}))

    monkeypatch.setenv("HOME", str(tmp_path))
    config = load_config(repo_root=tmp_path)

    assert config["server"]["port"] == 9999          # per-repo wins
    assert config["server"]["headPollInterval"] == 10  # global preserved


def test_config_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var NX_SERVER_PORT overrides config file values."""
    global_dir = tmp_path / ".config" / "nexus"
    global_dir.mkdir(parents=True)
    (global_dir / "config.yml").write_text(yaml.dump({"server": {"port": 7890}}))

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NX_SERVER_PORT", "8888")

    config = load_config(repo_root=tmp_path)
    assert config["server"]["port"] == 8888


def test_config_pm_archive_ttl_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NX_PM_ARCHIVE_TTL overrides pm.archiveTtl."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NX_PM_ARCHIVE_TTL", "60")
    config = load_config(repo_root=tmp_path)
    assert config["pm"]["archiveTtl"] == 60


def test_config_missing_files_returns_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config files → defaults only, no FileNotFoundError."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = load_config(repo_root=tmp_path)
    assert isinstance(config, dict)
    assert "server" in config


# ── Gap 9: _apply_env_overrides ValueError for non-integer port ──────────

def test_config_env_override_invalid_int_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NX_SERVER_PORT=notanint raises ValueError from _apply_env_overrides."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NX_SERVER_PORT", "notanint")

    with pytest.raises(ValueError, match="Invalid value.*NX_SERVER_PORT"):
        load_config(repo_root=tmp_path)


# ── Gap 10: set_credential temp file cleanup on write failure ──────────────

def test_set_credential_cleans_up_temp_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the temp file write fails, os.unlink is called on the temp file."""
    import os
    import tempfile
    from unittest.mock import patch

    from nexus.config import set_credential

    monkeypatch.setenv("HOME", str(tmp_path))

    # Track unlink calls on temp files
    original_unlink = os.unlink
    unlinked_paths: list[str] = []

    def tracking_unlink(path, *args, **kwargs):
        unlinked_paths.append(str(path))
        return original_unlink(path, *args, **kwargs)

    # Make os.fdopen raise to simulate write failure
    original_fdopen = os.fdopen

    def failing_fdopen(fd, *args, **kwargs):
        # Close the fd first to avoid resource leak, then raise
        os.close(fd)
        raise IOError("simulated write failure")

    with patch("nexus.config.os.fdopen", side_effect=failing_fdopen):
        with patch("nexus.config.os.unlink", side_effect=tracking_unlink):
            with pytest.raises(IOError, match="simulated write failure"):
                set_credential("anthropic_api_key", "test-key")

    # Verify unlink was called on a temp file
    assert len(unlinked_paths) >= 1, "os.unlink should have been called on the temp file"


# ── Gap 11: load_config non-dict YAML ────────────────────────────────────

def test_config_global_non_dict_yaml_returns_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_config returns defaults when global config is a bare string (not dict)."""
    monkeypatch.setenv("HOME", str(tmp_path))

    global_dir = tmp_path / ".config" / "nexus"
    global_dir.mkdir(parents=True)
    (global_dir / "config.yml").write_text("just a bare string\n")

    config = load_config(repo_root=tmp_path)
    assert isinstance(config, dict)
    # Should fall back to defaults since the YAML was not a dict
    assert config["server"]["port"] == 7890


def test_config_repo_non_dict_yaml_returns_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_config returns defaults when per-repo config is a list (not dict)."""
    monkeypatch.setenv("HOME", str(tmp_path))

    (tmp_path / ".nexus.yml").write_text("- item1\n- item2\n")

    config = load_config(repo_root=tmp_path)
    assert isinstance(config, dict)
    # Should fall back to defaults since the YAML was not a dict
    assert config["server"]["port"] == 7890


# ── Gap 12: set_credential unknown credential name ──────────────────────

def test_set_credential_unknown_name_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """set_credential raises ValueError for an unknown credential name."""
    from nexus.config import set_credential

    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(ValueError, match="Unknown credential"):
        set_credential("totally_unknown_credential", "some-value")
