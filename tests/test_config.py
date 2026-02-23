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
