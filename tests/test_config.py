"""AC6: Config merges global + per-repo YAML with env var overrides."""
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from nexus.config import _DEFAULTS, detect_test_command, get_verification_config, load_config


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


# ── Verification config section ───────────────────────────────────────────────


def test_defaults_include_verification_section() -> None:
    """_DEFAULTS contains the verification section with all 5 keys and correct types."""
    assert "verification" in _DEFAULTS
    v = _DEFAULTS["verification"]
    assert v["on_stop"] is False
    assert v["on_close"] is False
    assert v["test_command"] == ""
    assert v["lint_command"] == ""
    assert v["test_timeout"] == 120
    assert isinstance(v["test_timeout"], int)


def test_get_verification_config_returns_defaults_when_no_nexus_yml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_verification_config returns all defaults when no .nexus.yml exists."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = get_verification_config(repo_root=tmp_path)
    assert cfg["on_stop"] is False
    assert cfg["on_close"] is False
    assert cfg["test_command"] == ""
    assert cfg["lint_command"] == ""
    assert cfg["test_timeout"] == 120


def test_get_verification_config_merges_partial_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial verification section in .nexus.yml; rest comes from defaults."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".nexus.yml").write_text("verification:\n  on_stop: true\n")
    cfg = get_verification_config(repo_root=tmp_path)
    assert cfg["on_stop"] is True
    # remaining keys stay at defaults
    assert cfg["on_close"] is False
    assert cfg["test_command"] == ""
    assert cfg["lint_command"] == ""
    assert cfg["test_timeout"] == 120


def test_get_verification_config_all_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All verification fields in .nexus.yml override defaults."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".nexus.yml").write_text(
        "verification:\n"
        "  on_stop: true\n"
        "  on_close: true\n"
        "  test_command: uv run pytest\n"
        "  lint_command: ruff check .\n"
        "  test_timeout: 60\n"
    )
    cfg = get_verification_config(repo_root=tmp_path)
    assert cfg["on_stop"] is True
    assert cfg["on_close"] is True
    assert cfg["test_command"] == "uv run pytest"
    assert cfg["lint_command"] == "ruff check ."
    assert cfg["test_timeout"] == 60


# ── detect_test_command ───────────────────────────────────────────────────────


def test_detect_test_command_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[build-system]\n")
    assert detect_test_command(repo_root=tmp_path) == "uv run pytest"


def test_detect_test_command_pom(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text("<project/>\n")
    assert detect_test_command(repo_root=tmp_path) == "mvn test"


def test_detect_test_command_gradle(tmp_path: Path) -> None:
    (tmp_path / "build.gradle").write_text("// gradle\n")
    assert detect_test_command(repo_root=tmp_path) == "./gradlew test"


def test_detect_test_command_package_json(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}\n')
    assert detect_test_command(repo_root=tmp_path) == "npm test"


def test_detect_test_command_cargo(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "foo"\n')
    assert detect_test_command(repo_root=tmp_path) == "cargo test"


def test_detect_test_command_makefile(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\tpython -m pytest\n")
    assert detect_test_command(repo_root=tmp_path) == "make test"


def test_detect_test_command_go_mod(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/foo\n")
    assert detect_test_command(repo_root=tmp_path) == "go test ./..."


def test_detect_test_command_none(tmp_path: Path) -> None:
    assert detect_test_command(repo_root=tmp_path) == ""


def test_detect_test_command_priority(tmp_path: Path) -> None:
    """When both pyproject.toml and Makefile exist, pyproject.toml wins (first match)."""
    (tmp_path / "pyproject.toml").write_text("[build-system]\n")
    (tmp_path / "Makefile").write_text("test:\n\tpython -m pytest\n")
    assert detect_test_command(repo_root=tmp_path) == "uv run pytest"


def test_detect_test_command_gradle_kts(tmp_path: Path) -> None:
    """build.gradle.kts (Kotlin DSL) is detected as Gradle."""
    (tmp_path / "build.gradle.kts").write_text("// kotlin gradle\n")
    assert detect_test_command(repo_root=tmp_path) == "./gradlew test"


def test_detect_table_matches_reader_script() -> None:
    """config.py _DETECT_TABLE and reader script DETECT_TABLE must be identical."""
    import importlib.util

    from nexus.config import _DETECT_TABLE

    script = Path(__file__).parents[1] / "nx" / "hooks" / "scripts" / "read_verification_config.py"
    spec = importlib.util.spec_from_file_location("reader", script)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    assert _DETECT_TABLE == reader.DETECT_TABLE
