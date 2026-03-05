"""AC6: Config merges global + per-repo YAML with env var overrides."""
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from nexus.config import _DEFAULTS, load_config


def test_config_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without any config files, defaults are returned."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = load_config(repo_root=tmp_path)
    assert config["embeddings"]["rerankerModel"] == "rerank-2.5"
    assert "codeModel" not in config["embeddings"]
    assert "docsModel" not in config["embeddings"]


def test_config_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-repo config overrides global; global fills missing per-repo keys."""
    global_dir = tmp_path / ".config" / "nexus"
    global_dir.mkdir(parents=True)
    (global_dir / "config.yml").write_text(
        yaml.dump({"embeddings": {"rerankerModel": "rerank-2.5"}})
    )

    (tmp_path / ".nexus.yml").write_text(yaml.dump({"embeddings": {"rerankerModel": "rerank-3.0"}}))

    monkeypatch.setenv("HOME", str(tmp_path))
    config = load_config(repo_root=tmp_path)

    assert config["embeddings"]["rerankerModel"] == "rerank-3.0"  # per-repo wins


def test_config_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var NX_EMBEDDINGS_RERANKER_MODEL overrides config file values."""
    global_dir = tmp_path / ".config" / "nexus"
    global_dir.mkdir(parents=True)
    (global_dir / "config.yml").write_text(yaml.dump({"embeddings": {"rerankerModel": "rerank-2.5"}}))

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NX_EMBEDDINGS_RERANKER_MODEL", "rerank-3.0")

    config = load_config(repo_root=tmp_path)
    assert config["embeddings"]["rerankerModel"] == "rerank-3.0"



def test_config_voyageai_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_config() returns voyageai.read_timeout_seconds == 120 by default."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = load_config(repo_root=tmp_path)
    assert config["voyageai"]["read_timeout_seconds"] == 120


def test_config_voyageai_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """NX_VOYAGEAI_READ_TIMEOUT_SECONDS overrides the default."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NX_VOYAGEAI_READ_TIMEOUT_SECONDS", "60")
    config = load_config(repo_root=tmp_path)
    assert config["voyageai"]["read_timeout_seconds"] == 60
    assert isinstance(config["voyageai"]["read_timeout_seconds"], int)


def test_config_missing_files_returns_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config files → defaults only, no FileNotFoundError."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = load_config(repo_root=tmp_path)
    assert isinstance(config, dict)
    assert "embeddings" in config



# ── Gap 10: set_credential temp file cleanup on write failure ──────────────

def test_set_credential_cleans_up_temp_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the temp file write fails, os.unlink is called on the temp file."""
    import os
    import tempfile

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
                set_credential("voyage_api_key", "test-key")

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
    assert config["embeddings"]["rerankerModel"] == "rerank-2.5"


def test_config_repo_non_dict_yaml_returns_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_config returns defaults when per-repo config is a list (not dict)."""
    monkeypatch.setenv("HOME", str(tmp_path))

    (tmp_path / ".nexus.yml").write_text("- item1\n- item2\n")

    config = load_config(repo_root=tmp_path)
    assert isinstance(config, dict)
    # Should fall back to defaults since the YAML was not a dict
    assert config["embeddings"]["rerankerModel"] == "rerank-2.5"


# ── Gap 12: set_credential unknown credential name ──────────────────────

def test_set_credential_unknown_name_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """set_credential raises ValueError for an unknown credential name."""
    from nexus.config import set_credential

    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(ValueError, match="Unknown credential"):
        set_credential("totally_unknown_credential", "some-value")


# ── Indexing config section ────────────────────────────────────────────────


def test_defaults_include_indexing_section() -> None:
    """_DEFAULTS contains the indexing section with expected keys."""
    assert "indexing" in _DEFAULTS
    assert _DEFAULTS["indexing"]["code_extensions"] == []
    assert _DEFAULTS["indexing"]["prose_extensions"] == []
    assert _DEFAULTS["indexing"]["rdr_paths"] == ["docs/rdr"]


def test_load_config_returns_indexing_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_config returns indexing defaults when no config files exist."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config(repo_root=tmp_path)
    assert cfg["indexing"]["code_extensions"] == []
    assert cfg["indexing"]["prose_extensions"] == []
    assert cfg["indexing"]["rdr_paths"] == ["docs/rdr"]


def test_nexus_yml_indexing_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-repo .nexus.yml can override indexing defaults via deep merge."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".nexus.yml").write_text(
        "indexing:\n"
        "  code_extensions: [.sql, .proto]\n"
        "  rdr_paths:\n"
        "    - docs/rdr\n"
        "    - design/decisions\n"
    )
    cfg = load_config(repo_root=tmp_path)
    assert cfg["indexing"]["code_extensions"] == [".sql", ".proto"]
    assert cfg["indexing"]["rdr_paths"] == ["docs/rdr", "design/decisions"]
    # prose_extensions not set in override → stays default
    assert cfg["indexing"]["prose_extensions"] == []
