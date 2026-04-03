"""AC: nx config set/get/list/init — credential and config management."""
import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    # Remove any real API keys from env so config-file fallback is tested
    for key in ("CHROMA_API_KEY", "VOYAGE_API_KEY", "MXBAI_API_KEY",
                "CHROMA_TENANT", "CHROMA_DATABASE"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


# ── nx config set ─────────────────────────────────────────────────────────────

def test_config_set_writes_credential(runner: CliRunner, fake_home: Path) -> None:
    """nx config set chroma_api_key=abc writes to ~/.config/nexus/config.yml."""
    result = runner.invoke(main, ["config", "set", "chroma_api_key=abc123"])
    assert result.exit_code == 0, result.output

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    assert config_path.exists()
    data = yaml.safe_load(config_path.read_text())
    assert data["credentials"]["chroma_api_key"] == "abc123"


def test_config_set_creates_config_dir(runner: CliRunner, fake_home: Path) -> None:
    """nx config set creates ~/.config/nexus/ directory if missing."""
    config_dir = fake_home / ".config" / "nexus"
    assert not config_dir.exists()

    runner.invoke(main, ["config", "set", "voyage_api_key=vk-test"])

    assert config_dir.exists()


def test_config_set_preserves_existing_settings(runner: CliRunner, fake_home: Path) -> None:
    """nx config set preserves pre-existing config keys."""
    config_path = fake_home / ".config" / "nexus" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump({"server": {"port": 9999}}))

    runner.invoke(main, ["config", "set", "chroma_api_key=x"])

    data = yaml.safe_load(config_path.read_text())
    assert data["server"]["port"] == 9999
    assert data["credentials"]["chroma_api_key"] == "x"


def test_config_set_updates_existing_credential(runner: CliRunner, fake_home: Path) -> None:
    """nx config set overwrites an existing credential value."""
    runner.invoke(main, ["config", "set", "chroma_api_key=first"])
    runner.invoke(main, ["config", "set", "chroma_api_key=second"])

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    data = yaml.safe_load(config_path.read_text())
    assert data["credentials"]["chroma_api_key"] == "second"


def test_config_set_accepts_space_separated_key_value(runner: CliRunner, fake_home: Path) -> None:
    """nx config set KEY VALUE (space-separated) also works."""
    result = runner.invoke(main, ["config", "set", "voyage_api_key", "vk-space"])
    assert result.exit_code == 0, result.output

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    data = yaml.safe_load(config_path.read_text())
    assert data["credentials"]["voyage_api_key"] == "vk-space"


# ── nx config set — dotted keys ──────────────────────────────────────────────


def test_config_set_dotted_key_writes_nested_yaml(runner: CliRunner, fake_home: Path) -> None:
    """nx config set pdf.extractor=mineru writes nested YAML."""
    result = runner.invoke(main, ["config", "set", "pdf.extractor=mineru"])
    assert result.exit_code == 0, result.output

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    data = yaml.safe_load(config_path.read_text())
    assert data["pdf"]["extractor"] == "mineru"


def test_config_set_dotted_key_preserves_existing(runner: CliRunner, fake_home: Path) -> None:
    """Dotted key set preserves existing config."""
    runner.invoke(main, ["config", "set", "chroma_api_key=abc"])
    runner.invoke(main, ["config", "set", "pdf.extractor=docling"])

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    data = yaml.safe_load(config_path.read_text())
    assert data["credentials"]["chroma_api_key"] == "abc"
    assert data["pdf"]["extractor"] == "docling"


def test_config_set_dotted_key_overwrites(runner: CliRunner, fake_home: Path) -> None:
    """Dotted key set overwrites existing value."""
    runner.invoke(main, ["config", "set", "pdf.extractor=mineru"])
    runner.invoke(main, ["config", "set", "pdf.extractor=auto"])

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    data = yaml.safe_load(config_path.read_text())
    assert data["pdf"]["extractor"] == "auto"


def test_config_set_dotted_key_space_separated(runner: CliRunner, fake_home: Path) -> None:
    """nx config set pdf.extractor mineru (space-separated) works."""
    result = runner.invoke(main, ["config", "set", "pdf.extractor", "mineru"])
    assert result.exit_code == 0, result.output

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    data = yaml.safe_load(config_path.read_text())
    assert data["pdf"]["extractor"] == "mineru"


# ── get_pdf_extractor ────────────────────────────────────────────────────────


def test_get_pdf_extractor_default(fake_home: Path) -> None:
    """get_pdf_extractor returns 'auto' when no config exists."""
    from nexus.config import get_pdf_extractor
    assert get_pdf_extractor() == "auto"


def test_get_pdf_extractor_from_global_config(fake_home: Path) -> None:
    """get_pdf_extractor reads from global config.yml."""
    from nexus.config import get_pdf_extractor

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump({"pdf": {"extractor": "mineru"}}))

    assert get_pdf_extractor() == "mineru"


def test_get_pdf_extractor_invalid_falls_back_to_auto(fake_home: Path) -> None:
    """get_pdf_extractor returns 'auto' for invalid config values."""
    from nexus.config import get_pdf_extractor

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump({"pdf": {"extractor": "bogus"}}))

    assert get_pdf_extractor() == "auto"


def test_get_pdf_extractor_repo_overrides_global(fake_home: Path, tmp_path: Path) -> None:
    """Per-repo .nexus.yml overrides global config."""
    from nexus.config import get_pdf_extractor

    global_path = fake_home / ".config" / "nexus" / "config.yml"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(yaml.dump({"pdf": {"extractor": "auto"}}))

    repo_config = tmp_path / ".nexus.yml"
    repo_config.write_text(yaml.dump({"pdf": {"extractor": "mineru"}}))

    assert get_pdf_extractor(repo_root=tmp_path) == "mineru"


# ── CLI --extractor reads config default ─────────────────────────────────────


def test_index_pdf_extractor_from_config(runner: CliRunner, fake_home: Path) -> None:
    """nx index pdf uses pdf.extractor from config when --extractor not passed."""
    from unittest.mock import patch

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump({"pdf": {"extractor": "docling"}}))

    pdf = fake_home / "test.pdf"
    pdf.write_bytes(b"dummy")

    with patch("nexus.doc_indexer.index_pdf", return_value={"chunks": 1, "pages": [], "title": "", "author": ""}) as mock_index:
        result = runner.invoke(main, ["index", "pdf", str(pdf)])

    assert result.exit_code == 0, result.output
    _, kwargs = mock_index.call_args
    assert kwargs["extractor"] == "docling"


def test_index_pdf_flag_overrides_config(runner: CliRunner, fake_home: Path) -> None:
    """--extractor flag overrides pdf.extractor config."""
    from unittest.mock import patch

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump({"pdf": {"extractor": "docling"}}))

    pdf = fake_home / "test.pdf"
    pdf.write_bytes(b"dummy")

    with patch("nexus.doc_indexer.index_pdf", return_value={"chunks": 1, "pages": [], "title": "", "author": ""}) as mock_index:
        result = runner.invoke(main, ["index", "pdf", str(pdf), "--extractor", "mineru"])

    assert result.exit_code == 0, result.output
    _, kwargs = mock_index.call_args
    assert kwargs["extractor"] == "mineru"


# ── nx config get ─────────────────────────────────────────────────────────────

def test_config_get_returns_value_from_file(runner: CliRunner, fake_home: Path) -> None:
    """nx config get --show prints the full credential value from config.yml."""
    runner.invoke(main, ["config", "set", "voyage_api_key=vk-123"])
    result = runner.invoke(main, ["config", "get", "--show", "voyage_api_key"])
    assert result.exit_code == 0, result.output
    assert "vk-123" in result.output


def test_config_get_masks_by_default(runner: CliRunner, fake_home: Path) -> None:
    """nx config get masks the credential value by default."""
    runner.invoke(main, ["config", "set", "voyage_api_key=secret-voyage-key-xyz"])
    result = runner.invoke(main, ["config", "get", "voyage_api_key"])
    assert result.exit_code == 0, result.output
    assert "secret-voyage-key-xyz" not in result.output
    assert "***" in result.output


def test_config_get_prefers_env_var(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nx config get --show returns env var value when both env and file set."""
    runner.invoke(main, ["config", "set", "chroma_api_key=file-val"])
    monkeypatch.setenv("CHROMA_API_KEY", "env-val")
    result = runner.invoke(main, ["config", "get", "--show", "chroma_api_key"])
    assert "env-val" in result.output


def test_config_get_missing_key_reports_not_set(runner: CliRunner, fake_home: Path) -> None:
    """nx config get reports 'not set' for unset credential."""
    result = runner.invoke(main, ["config", "get", "chroma_api_key"])
    assert result.exit_code == 0
    assert "not set" in result.output.lower()


# ── nx config get — dotted keys ───────────────────────────────────────────────


def test_config_get_dotted_key_returns_value(runner: CliRunner, fake_home: Path) -> None:
    """nx config get pdf.mineru_server_url returns the set value."""
    runner.invoke(main, ["config", "set", "pdf.mineru_server_url=http://10.0.0.1:9000"])
    result = runner.invoke(main, ["config", "get", "pdf.mineru_server_url"])
    assert result.exit_code == 0, result.output
    assert "http://10.0.0.1:9000" in result.output


def test_config_get_dotted_key_returns_default(runner: CliRunner, fake_home: Path) -> None:
    """nx config get pdf.mineru_server_url returns the built-in default when not set."""
    result = runner.invoke(main, ["config", "get", "pdf.mineru_server_url"])
    assert result.exit_code == 0, result.output
    assert "127.0.0.1" in result.output


def test_config_get_dotted_key_show_flag_not_masked(runner: CliRunner, fake_home: Path) -> None:
    """nx config get --show pdf.extractor shows the plain value (settings are not masked)."""
    runner.invoke(main, ["config", "set", "pdf.extractor=docling"])
    result = runner.invoke(main, ["config", "get", "--show", "pdf.extractor"])
    assert result.exit_code == 0, result.output
    assert "docling" in result.output


def test_config_get_dotted_key_missing_leaf_reports_not_set(runner: CliRunner, fake_home: Path) -> None:
    """nx config get reports 'not set' for a nonexistent leaf under a known section."""
    result = runner.invoke(main, ["config", "get", "pdf.nonexistent_key"])
    assert result.exit_code == 0, result.output
    assert "not set" in result.output.lower()


def test_config_get_dotted_key_missing_section_reports_not_set(runner: CliRunner, fake_home: Path) -> None:
    """nx config get reports 'not set' for a completely nonexistent section.key."""
    result = runner.invoke(main, ["config", "get", "ghost.setting"])
    assert result.exit_code == 0, result.output
    assert "not set" in result.output.lower()


# ── nx config list ────────────────────────────────────────────────────────────

def test_config_list_masks_api_key_values(runner: CliRunner, fake_home: Path) -> None:
    """nx config list shows masked value (not plaintext) for API keys."""
    runner.invoke(main, ["config", "set", "chroma_api_key=super-secret"])
    result = runner.invoke(main, ["config", "list"])
    assert result.exit_code == 0, result.output
    assert "super-secret" not in result.output
    assert "chroma_api_key" in result.output


def test_config_list_shows_credential_names(runner: CliRunner, fake_home: Path) -> None:
    """nx config list shows all known credential names."""
    result = runner.invoke(main, ["config", "list"])
    assert result.exit_code == 0, result.output
    assert "chroma_api_key" in result.output
    assert "voyage_api_key" in result.output


def test_config_list_shows_non_secret_settings(runner: CliRunner, fake_home: Path) -> None:
    """nx config list shows indexing and other non-secret settings."""
    result = runner.invoke(main, ["config", "list"])
    assert result.exit_code == 0, result.output
    # Indexing section should be visible
    assert "indexing" in result.output.lower()


# ── nx config init ────────────────────────────────────────────────────────────

def test_config_init_writes_provided_values(runner: CliRunner, fake_home: Path) -> None:
    """nx config init with all inputs writes all credentials to config.yml."""
    inputs = "chroma-key\nmy-db\nvoyage-key\n"
    result = runner.invoke(main, ["config", "init"], input=inputs)
    assert result.exit_code == 0, result.output

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    assert config_path.exists()
    data = yaml.safe_load(config_path.read_text())
    creds = data.get("credentials", {})
    assert creds.get("chroma_api_key") == "chroma-key"
    assert creds.get("chroma_database") == "my-db"
    assert creds.get("voyage_api_key") == "voyage-key"


def test_config_init_provisions_databases_success(runner: CliRunner, fake_home: Path) -> None:
    """nx config init reports database status when provisioning succeeds."""
    from unittest.mock import MagicMock, patch

    mock_admin = MagicMock()
    created = {"mydb": True}
    with patch("nexus.commands._provision._cloud_admin_client", return_value=mock_admin), \
         patch("nexus.commands._provision.ensure_databases", return_value=created):
        result = runner.invoke(main, ["config", "init"], input="ck-key\nmydb\nvoyage-key\n")

    assert result.exit_code == 0, result.output
    assert "mydb" in result.output
    assert "created" in result.output


def test_config_init_provisioning_failure_is_a_warning(runner: CliRunner, fake_home: Path) -> None:
    """Provisioning failure is shown as a warning; init still succeeds."""
    from unittest.mock import patch

    with patch("nexus.commands._provision._cloud_admin_client",
               side_effect=Exception("network timeout")):
        result = runner.invoke(main, ["config", "init"], input="ck-key\nmydb\nvoyage-key\n")

    assert result.exit_code == 0, result.output
    assert "warning" in result.output.lower() or "could not" in result.output.lower()


def test_config_init_shows_signup_urls(runner: CliRunner, fake_home: Path) -> None:
    """nx config init output includes URLs to obtain the required keys."""
    result = runner.invoke(main, ["config", "init"], input="\n\n\n")
    output = result.output
    assert "trychroma.com" in output or "chromadb" in output.lower()
    assert "voyageai.com" in output or "voyage" in output.lower()


def test_config_init_skips_keys_already_in_env(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nx config init does not prompt for keys already set in environment."""
    monkeypatch.setenv("CHROMA_API_KEY", "env-chroma")
    result = runner.invoke(main, ["config", "init"], input="\n\n\n")
    # Should mention the key is already set via env, not prompt to re-enter
    assert "env-chroma" in result.output or "already set" in result.output.lower() or "environment" in result.output.lower()


# ── get_credential() fallback ────────────────────────────────────────────────

def test_get_credential_env_var_takes_precedence(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_credential returns env var over config file value."""
    from nexus.config import get_credential

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump({"credentials": {"chroma_api_key": "file-val"}}))

    monkeypatch.setenv("CHROMA_API_KEY", "env-val")
    assert get_credential("chroma_api_key") == "env-val"


def test_get_credential_falls_back_to_config_file(fake_home: Path) -> None:
    """get_credential returns config file value when env var is absent."""
    from nexus.config import get_credential

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump({"credentials": {"voyage_api_key": "vk-file"}}))

    assert get_credential("voyage_api_key") == "vk-file"


def test_get_credential_returns_empty_when_unset(fake_home: Path) -> None:
    """get_credential returns empty string when neither env nor file set."""
    from nexus.config import get_credential

    assert get_credential("chroma_api_key") == ""
