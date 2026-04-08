# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from nexus.config import _DEFAULTS, detect_test_command, get_verification_config, load_config


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ── load_config ──────────────────────────────────────────────────────────────


def test_config_defaults(home: Path) -> None:
    config = load_config(repo_root=home)
    assert config["embeddings"]["rerankerModel"] == "rerank-2.5"
    assert "codeModel" not in config["embeddings"]
    assert "docsModel" not in config["embeddings"]


def test_config_merge(home: Path) -> None:
    global_dir = home / ".config" / "nexus"
    global_dir.mkdir(parents=True)
    (global_dir / "config.yml").write_text(yaml.dump({"embeddings": {"rerankerModel": "rerank-2.5"}}))
    (home / ".nexus.yml").write_text(yaml.dump({"embeddings": {"rerankerModel": "rerank-3.0"}}))
    assert load_config(repo_root=home)["embeddings"]["rerankerModel"] == "rerank-3.0"


def test_config_env_override(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_dir = home / ".config" / "nexus"
    global_dir.mkdir(parents=True)
    (global_dir / "config.yml").write_text(yaml.dump({"embeddings": {"rerankerModel": "rerank-2.5"}}))
    monkeypatch.setenv("NX_EMBEDDINGS_RERANKER_MODEL", "rerank-3.0")
    assert load_config(repo_root=home)["embeddings"]["rerankerModel"] == "rerank-3.0"


def test_config_voyageai_default(home: Path) -> None:
    assert load_config(repo_root=home)["voyageai"]["read_timeout_seconds"] == 120


def test_config_voyageai_env_override(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_VOYAGEAI_READ_TIMEOUT_SECONDS", "60")
    cfg = load_config(repo_root=home)
    assert cfg["voyageai"]["read_timeout_seconds"] == 60
    assert isinstance(cfg["voyageai"]["read_timeout_seconds"], int)


def test_config_missing_files_returns_defaults(home: Path) -> None:
    config = load_config(repo_root=home)
    assert isinstance(config, dict) and "embeddings" in config


@pytest.mark.parametrize("content", [
    "just a bare string\n",
    "- item1\n- item2\n",
])
def test_config_non_dict_yaml_returns_defaults(home: Path, content: str) -> None:
    (home / ".nexus.yml").write_text(content)
    config = load_config(repo_root=home)
    assert isinstance(config, dict)
    assert config["embeddings"]["rerankerModel"] == "rerank-2.5"


def test_config_global_non_dict_yaml_returns_defaults(home: Path) -> None:
    global_dir = home / ".config" / "nexus"
    global_dir.mkdir(parents=True)
    (global_dir / "config.yml").write_text("just a bare string\n")
    config = load_config(repo_root=home)
    assert isinstance(config, dict) and config["embeddings"]["rerankerModel"] == "rerank-2.5"


# ── set_credential ───────────────────────────────────────────────────────────


def test_set_credential_cleans_up_temp_on_write_failure(home: Path) -> None:
    import os
    from nexus.config import set_credential

    unlinked: list[str] = []
    orig_unlink = os.unlink

    def tracking_unlink(path, *a, **kw):
        unlinked.append(str(path))
        return orig_unlink(path, *a, **kw)

    def failing_fdopen(fd, *a, **kw):
        os.close(fd)
        raise IOError("simulated write failure")

    with (
        patch("nexus.config.os.fdopen", side_effect=failing_fdopen),
        patch("nexus.config.os.unlink", side_effect=tracking_unlink),
    ):
        with pytest.raises(IOError, match="simulated write failure"):
            set_credential("voyage_api_key", "test-key")
    assert len(unlinked) >= 1


def test_set_credential_unknown_name_raises(home: Path) -> None:
    from nexus.config import set_credential
    with pytest.raises(ValueError, match="Unknown credential"):
        set_credential("totally_unknown_credential", "some-value")


# ── Indexing config ──────────────────────────────────────────────────────────


def test_defaults_include_indexing_section() -> None:
    assert _DEFAULTS["indexing"]["code_extensions"] == []
    assert _DEFAULTS["indexing"]["prose_extensions"] == []
    assert _DEFAULTS["indexing"]["rdr_paths"] == ["docs/rdr"]


def test_load_config_returns_indexing_defaults(home: Path) -> None:
    cfg = load_config(repo_root=home)
    assert cfg["indexing"]["code_extensions"] == []
    assert cfg["indexing"]["prose_extensions"] == []
    assert cfg["indexing"]["rdr_paths"] == ["docs/rdr"]


def test_nexus_yml_indexing_overrides(home: Path) -> None:
    (home / ".nexus.yml").write_text(
        "indexing:\n  code_extensions: [.sql, .proto]\n  rdr_paths: [docs/rdr, design/decisions]\n"
    )
    cfg = load_config(repo_root=home)
    assert cfg["indexing"]["code_extensions"] == [".sql", ".proto"]
    assert cfg["indexing"]["rdr_paths"] == ["docs/rdr", "design/decisions"]
    assert cfg["indexing"]["prose_extensions"] == []


# ── Verification config ─────────────────────────────────────────────────────


def test_defaults_include_verification_section() -> None:
    v = _DEFAULTS["verification"]
    assert v == {
        "on_stop": False, "on_close": False,
        "test_command": "", "lint_command": "", "test_timeout": 120,
    }


def test_get_verification_config_defaults(home: Path) -> None:
    cfg = get_verification_config(repo_root=home)
    assert cfg == {
        "on_stop": False, "on_close": False,
        "test_command": "", "lint_command": "", "test_timeout": 120,
    }


def test_get_verification_config_merges_partial(home: Path) -> None:
    (home / ".nexus.yml").write_text("verification:\n  on_stop: true\n")
    cfg = get_verification_config(repo_root=home)
    assert cfg["on_stop"] is True
    assert cfg["on_close"] is False and cfg["test_command"] == "" and cfg["test_timeout"] == 120


def test_get_verification_config_all_fields(home: Path) -> None:
    (home / ".nexus.yml").write_text(
        "verification:\n  on_stop: true\n  on_close: true\n"
        "  test_command: uv run pytest\n  lint_command: ruff check .\n  test_timeout: 60\n"
    )
    cfg = get_verification_config(repo_root=home)
    assert cfg == {
        "on_stop": True, "on_close": True,
        "test_command": "uv run pytest", "lint_command": "ruff check .", "test_timeout": 60,
    }


# ── detect_test_command ──────────────────────────────────────────────────────


@pytest.mark.parametrize("filename,content,expected", [
    ("pyproject.toml", "[build-system]\n", "uv run pytest"),
    ("pom.xml", "<project/>\n", "mvn test"),
    ("build.gradle", "// gradle\n", "./gradlew test"),
    ("package.json", '{"scripts": {"test": "jest"}}\n', "npm test"),
    ("Cargo.toml", '[package]\nname = "foo"\n', "cargo test"),
    ("Makefile", "test:\n\tpython -m pytest\n", "make test"),
    ("go.mod", "module example.com/foo\n", "go test ./..."),
    ("build.gradle.kts", "// kotlin gradle\n", "./gradlew test"),
])
def test_detect_test_command(tmp_path: Path, filename: str, content: str, expected: str) -> None:
    (tmp_path / filename).write_text(content)
    assert detect_test_command(repo_root=tmp_path) == expected


def test_detect_test_command_none(tmp_path: Path) -> None:
    assert detect_test_command(repo_root=tmp_path) == ""


def test_detect_test_command_priority(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[build-system]\n")
    (tmp_path / "Makefile").write_text("test:\n\tpython -m pytest\n")
    assert detect_test_command(repo_root=tmp_path) == "uv run pytest"


def test_detect_table_matches_reader_script() -> None:
    import importlib.util
    from nexus.config import _DETECT_TABLE
    script = Path(__file__).parents[1] / "nx" / "hooks" / "scripts" / "read_verification_config.py"
    spec = importlib.util.spec_from_file_location("reader", script)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    assert _DETECT_TABLE == reader.DETECT_TABLE
